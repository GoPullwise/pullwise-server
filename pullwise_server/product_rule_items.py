"""Publish deterministic fact actions through the normal fenced Item boundary."""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from .product_projection import RuleItemProjection, project_rule_source


def _material(snapshot):
    return [{key: value for key, value in row.items() if key != 'sourceVersion'}
            for row in snapshot.get('evidence', [])]


def _parent_overlay(db, snapshot, refs, fences, context, now, *, allow_reopen=False):
    """Bind explicit current PR authority without changing child GitHub facts."""
    number = snapshot.get('sourceFacts', {}).get('pullNumber')
    repository = snapshot.get('repositoryId')
    if type(number) is not int or not repository:
        return snapshot, refs, fences
    parents = db.execute('''SELECT s.*, c.context_version,c.configuration_revision,c.authorization_revision
        FROM source_records s JOIN source_contexts c ON c.source_id=s.source_id
        WHERE s.source_type='pr_state' AND s.repository_id=? AND c.context_id=?
          AND c.accessible=1 AND c.authorization_valid_until>=?''', (repository, context, now)).fetchall()
    for parent in parents:
        facts = json.loads(parent['source_facts_json'])
        if facts.get('pullNumber') != number or facts.get('state') not in {'open', 'closed'}:
            continue
        marker = snapshot.get('ruleParentClosure')
        if facts['state'] == 'open' and marker != parent['source_id']:
            continue
        if snapshot.get('lifecycle') in {'source_deleted', 'superseded'}:
            continue
        if snapshot.get('lifecycle') == 'source_closed' and not marker:
            # A request withdrawn independently of PR closure must not revive.
            continue
        if facts['state'] == 'closed' or not allow_reopen:
            snapshot.update(lifecycle='source_closed', attentionState='closed', closureReason='source_closed',
                            ruleParentClosure=parent['source_id'])
        else:
            snapshot.update(lifecycle='active', attentionState='needs_confirmation', closureReason=None,
                            assessments=[], ruleEvidenceComplete=False,
                            attentionUpdatedAt=datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z'))
            snapshot.pop('ruleParentClosure', None)
        refs = [ref for ref in refs if ref['sourceId'] != parent['source_id']]
        refs.append(dict(sourceId=parent['source_id'], sourceVersion=parent['latest_version'],
                         sourceRevision=parent['source_revision']))
        fences = [f for f in fences if f['sourceId'] != parent['source_id']]
        fences.append(dict(sourceId=parent['source_id'], contextId=context,
            contextVersion=parent['context_version'], configurationRevision=parent['configuration_revision'],
            authorizationRevision=parent['authorization_revision']))
        break
    return snapshot, refs, fences


def reconcile_pr_items(store, *, target, now):
    """Run after the fact batch, inside its transaction, to apply explicit PR state."""
    if target['module'] != 'pr':
        return
    with store.atomic() as store, store._immediate() as db:
        rows = db.execute('''SELECT i.*,v.snapshot_json,v.sources_json,v.context_fences_json
            FROM items i JOIN item_versions v ON v.item_id=i.id AND v.item_version=i.current_item_version
            WHERE i.context_id=?''', (target['context_id'],)).fetchall()
        for row in rows:
            before = json.loads(row['snapshot_json'])
            snapshot, refs, fences = _parent_overlay(db, deepcopy(before), json.loads(row['sources_json']),
                json.loads(row['context_fences_json']), target['context_id'], now, allow_reopen=True)
            if snapshot == before and refs == json.loads(row['sources_json']) and fences == json.loads(row['context_fences_json']):
                continue
            updated = store.publish_item_snapshot(item_id=row['id'], expected_item_revision=row['revision'],
                sources=refs, context_fences=fences, snapshot=snapshot, observed_at=now)
            if before.get('ruleParentClosure') and not snapshot.get('ruleParentClosure'):
                db.execute('''INSERT INTO item_handling_events(id,item_id,item_version,actor_id,disposition,
                    event_kind,created_at) VALUES (?,?,?,'system','open','pr_reopened',?)''',
                    ('handling_' + uuid.uuid4().hex, row['id'], updated['itemVersion'], now))


def publish_rule_items(store, *, source, record, target, now):
    """Compose with fact persistence; never perform model/network/usage work."""
    with store.atomic() as store, store._immediate() as db:
        if source['sourceType'] == 'pr_review_comment':
            from .pr_followup import reconcile_thread
            reconcile_thread(store, source_id=record['id'], context_id=target['context_id'], now=now)
            return
        context = target['context_id']
        source_id = record['id']
        current_source = dict(sourceId=source_id, sourceVersion=record['sourceVersion'],
                              sourceRevision=record['sourceRevision'])
        current_fence = dict(sourceId=source_id, contextId=context,
            contextVersion=target['context_version'], configurationRevision=target['configuration_revision'],
            authorizationRevision=target['authorization_revision'])
        attention_time = datetime.fromtimestamp(now, timezone.utc).isoformat().replace('+00:00', 'Z')
        projections = list(project_rule_source(dict(source, sourceVersion=record['sourceVersion']), target))
        existing = db.execute(
            '''SELECT i.*, v.snapshot_json, v.sources_json, v.context_fences_json FROM items i
               JOIN item_versions v ON v.item_id=i.id AND v.item_version=i.current_item_version
               WHERE i.context_id=?''', (context,)).fetchall()
        facts = source.get('sourceFacts', {})
        # Complete request lists are withdrawal evidence only for this PR state.
        if (source['sourceType'] == 'pr_state' and facts.get('state') in {'open', 'closed'}
                and isinstance(facts.get('requestedReviewers'), list)
                and isinstance(facts.get('requestedTeams'), list)):
            active_keys = {p.unit_key for p in projections}
            for row in existing:
                if row['unit_type'] != 'pr_review_request' or row['unit_key'] in active_keys:
                    continue
                if source_id not in {s['sourceId'] for s in json.loads(row['sources_json'])}:
                    continue
                old = json.loads(row['snapshot_json'])
                snapshot = deepcopy(old)
                snapshot.update(sourceFacts=dict(deepcopy(facts), ruleClosureDetail='review_request_withdrawn'),
                                lifecycle='source_closed', attentionState='closed', closureReason='source_closed')
                signature = 'withdrawn:' + row['unit_key']
                projections.append(RuleItemProjection(row['unit_type'], row['unit_key'], snapshot, signature, True))
        if (source['sourceType'] == 'pr_review_body'
                and facts.get('reviewState') in {'CHANGES_REQUESTED', 'DISMISSED'}
                and facts.get('formalReviewStatus') in {'superseded', 'dismissed'}
                and source.get('content', {}).get('body') == ''):
            for row in existing:
                if row['unit_type'] != 'pr_review_body' or row['unit_key'] != source['externalKey']:
                    continue
                if source_id not in {ref['sourceId'] for ref in json.loads(row['sources_json'])}:
                    continue
                snapshot = deepcopy(json.loads(row['snapshot_json']))
                snapshot.update(sourceFacts=dict(deepcopy(facts), ruleClosureDetail='formal_review_withdrawn'),
                                actionTypes=[], evidence=[], assessments=[], nextActors=[],
                                lifecycle='superseded', attentionState='closed', closureReason='superseded')
                projections.append(RuleItemProjection('pr_review_body', row['unit_key'], snapshot,
                    'formal-review-withdrawn:' + facts['formalReviewStatus'], True))
        projected = {(p.unit_type, p.unit_key) for p in projections}
        # Missing semantic conclusions leave existing actions explicitly unconfirmed.
        # PR request-list omissions have their own conservative reconciliation above.
        if source['sourceType'] != 'pr_state':
            for row in existing:
                if (row['unit_type'], row['unit_key']) in projected:
                    continue
                refs = json.loads(row['sources_json'])
                if source_id not in {ref['sourceId'] for ref in refs}:
                    continue
                snapshot = json.loads(row['snapshot_json'])
                fences = json.loads(row['context_fences_json'])
                if current_source in refs and current_fence in fences:
                    # Publication revalidates every dependency and authority even on replay.
                    snapshot, refs, fences = _parent_overlay(db, snapshot, refs, fences, context, now)
                    store.publish_item_snapshot(item_id=row['id'], expected_item_revision=row['revision'],
                        sources=refs, context_fences=fences, snapshot=snapshot, observed_at=now)
                    continue
                if snapshot.get('assessments'):
                    snapshot['historicalAssessments'] = snapshot['assessments']
                snapshot.update(assessments=[], attentionState='needs_confirmation', closureReason=None,
                                lifecycle='active', ruleEvidenceComplete=False, sourceFacts=deepcopy(facts))
                for evidence in snapshot.get('evidence', []):
                    evidence['status'] = 'expired'
                refs = [current_source if ref['sourceId'] == source_id else ref for ref in refs]
                fences = [current_fence if f['sourceId'] == source_id and f['contextId'] == context else f for f in fences]
                if current_fence not in fences:
                    fences.append(current_fence)
                snapshot, refs, fences = _parent_overlay(db, snapshot, refs, fences, context, now)
                store.publish_item_snapshot(item_id=row['id'], expected_item_revision=row['revision'],
                    sources=refs, context_fences=fences, snapshot=snapshot, observed_at=now)
        for projection in projections:
            row = db.execute('SELECT * FROM items WHERE context_id=? AND unit_type=? AND unit_key=?',
                             (context, projection.unit_type, projection.unit_key)).fetchone()
            item = store._item_dto(row) if row else store.create_item(
                context_id=context, unit_type=projection.unit_type, unit_key=projection.unit_key)
            version = item['itemVersion']
            old_row = db.execute('SELECT * FROM item_versions WHERE item_id=? AND item_version=?',
                                 (item['id'], version)).fetchone()
            old = json.loads(old_row['snapshot_json']) if old_row else {}
            if old.get('assessments') and current_source in json.loads(old_row['sources_json']) and current_fence in json.loads(old_row['context_fences_json']):
                preserved, refs, fences = _parent_overlay(db, deepcopy(old), json.loads(old_row['sources_json']),
                    json.loads(old_row['context_fences_json']), context, now)
                store.publish_item_snapshot(item_id=item['id'], expected_item_revision=item['revision'],
                    sources=refs, context_fences=fences, snapshot=preserved, observed_at=now)
                continue
            same_action = (old.get('ruleActionSignature') == projection.action_signature
                           and old.get('ruleEvidenceComplete') is True and projection.evidence_complete
                           and old.get('ruleContextVersion') == target['context_version']
                           and old.get('ruleConfigurationRevision') == target['configuration_revision']
                           and _material(old) == _material(projection.snapshot))
            snapshot = deepcopy(projection.snapshot)
            if old.get('ruleParentClosure'):
                snapshot['ruleParentClosure'] = old['ruleParentClosure']
            snapshot.update(ruleActionSignature=projection.action_signature,
                            ruleEvidenceComplete=projection.evidence_complete,
                            ruleContextVersion=target['context_version'],
                            ruleConfigurationRevision=target['configuration_revision'],
                            attentionUpdatedAt=old.get('attentionUpdatedAt', attention_time)
                            if same_action or snapshot.get('lifecycle') != 'active' else attention_time)
            snapshot, refs, fences = _parent_overlay(db, snapshot, [current_source], [current_fence], context, now)
            if snapshot.get('ruleParentClosure') and old.get('ruleParentClosure'):
                snapshot['attentionUpdatedAt'] = old.get('attentionUpdatedAt', attention_time)
            updated = store.publish_item_snapshot(item_id=item['id'], expected_item_revision=item['revision'],
                sources=refs, snapshot=snapshot, context_fences=fences, observed_at=now)
            if projection.unit_type != 'pr_review_request':
                db.execute('UPDATE source_contexts SET item_id=? WHERE source_id=? AND context_id=?',
                           (item['id'], source_id, context))
            if updated['itemVersion'] == version:
                continue
            handling = db.execute('SELECT * FROM item_handling_events WHERE item_id=? ORDER BY rowid DESC LIMIT 1',
                                  (item['id'],)).fetchone()
            if handling is None:
                continue
            carry = same_action and handling['item_version'] == version
            # Append system provenance; the user event remains immutable history.
            db.execute('''INSERT INTO item_handling_events(
                id,item_id,item_version,actor_id,disposition,assignee_id,note,feedback,
                event_kind,carried_from_item_version,created_at) VALUES (?,?,?,'system',?,?,?,?,?,?,?)''',
                ('handling_' + uuid.uuid4().hex, item['id'], updated['itemVersion'],
                 handling['disposition'] if carry else 'open', handling['assignee_id'] if carry else None,
                 handling['note'] if carry else None, handling['feedback'] if carry else None,
                 'handling_carried' if carry else 'rule_action_changed', version if carry else None, now))
