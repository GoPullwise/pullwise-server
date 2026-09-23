"""Saved PR answers -> one fenced thread Item. No network or model calls."""
from __future__ import annotations

import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone

from .product_domain import _canonical_hash
from .jev_questions import pr_questions


_ACTIONS = {"change_request": "change_requested", "question": "reply_needed",
            "optional_suggestion": "optional_suggestion", "rereview_request": "review_requested"}


def _segments(assessment, evidence, source):
    """Missing/ambiguous bindings cannot become a reliable negative result."""
    if not assessment or assessment.get("questionVersion") != "pr-followup/v3":
        return [], False
    answers, bindings = assessment["answers"], assessment["bindings"]
    prefixes = {key.split("_", 1)[0] for key in answers}
    if not prefixes or len(prefixes) > 8:
        return [], False
    result, complete = [], True
    by_id = {row["id"]: row for row in evidence}
    for prefix in sorted(prefixes):
        values, ids, anchors = {}, set(), set()
        for question in pr_questions(1):
            name = question[3:]
            key = prefix + "_" + name
            answer, binding = answers.get(key, {}), bindings.get(key, {})
            if (binding.get("sourceId") != source["id"]
                    or binding.get("sourceVersion") != source["sourceVersion"]
                    or not binding.get("segmentAnchor") or not binding.get("evidenceIds")
                    or not set(binding["evidenceIds"]).issubset(by_id)
                    or any(by_id[e].get("sourceId") != source["id"]
                           or by_id[e].get("sourceVersion") != source["sourceVersion"]
                           for e in binding["evidenceIds"])):
                complete = False
                values[name] = "unclear"
                continue
            ids.update(binding["evidenceIds"])
            anchors.add(binding["segmentAnchor"])
            confidence = answer.get("confidence")
            value = answer.get("choice")
            if type(confidence) not in (int, float) or not 0.8 <= confidence <= 1:
                value = "unclear"
            if value not in pr_questions(1)[question]["criteria"]:
                value = "unclear"
            values[name] = value
            complete &= value != "unclear"
        if len(anchors) != 1:
            complete = False
            continue
        result.append((values, [deepcopy(by_id[key]) for key in sorted(ids)]))
    return result, complete


def reconcile_thread(store, *, source_id, context_id, now):
    """Caller supplies a transaction-bound store; re-read every member there."""
    with store._immediate() as db:
        trigger = db.execute("SELECT * FROM source_records WHERE source_id=?", (source_id,)).fetchone()
        if trigger is None or trigger["source_type"] != "pr_review_comment":
            return None
        trigger_facts = json.loads(trigger["source_facts_json"])
        thread = trigger_facts.get("threadId")
        if not thread or trigger_facts.get("associationVerified") is not True:
            old_items = db.execute('''SELECT i.*,v.sources_json FROM items i JOIN item_versions v
                ON v.item_id=i.id AND v.item_version=i.current_item_version
                WHERE i.context_id=? AND i.unit_type='pr_thread' ''', (context_id,)).fetchall()
            match = next((i for i in old_items if source_id in
                          {r['sourceId'] for r in json.loads(i['sources_json'])}), None)
            if match is None:
                return None
            thread = match['unit_key']  # Historical membership only; never fresh positive proof.
        context = db.execute("SELECT * FROM source_contexts WHERE source_id=? AND context_id=?",
                             (source_id, context_id)).fetchone()
        owner = context["billing_owner_id"]
        rows = db.execute('''SELECT s.*,c.context_version,c.configuration_revision,c.authorization_revision
            FROM source_records s JOIN source_contexts c ON c.source_id=s.source_id
            WHERE c.context_id=? AND s.repository_id=? AND s.source_type='pr_review_comment'
            ORDER BY s.source_id''', (context_id, trigger["repository_id"])).fetchall()
        members = []
        for row in rows:
            facts = json.loads(row["source_facts_json"])
            if ((facts.get("threadId") == thread or row["source_id"] == source_id)
                    and facts.get("pullNumber") == trigger_facts.get("pullNumber")):
                members.append(row)
        visible = {s["id"]: s for s in store.list_sources_for_billing_owner(owner, include_content=True)}
        if any(row["source_id"] not in visible for row in members):
            raise ValueError("STALE_AUTHORIZATION")
        previous = db.execute("SELECT * FROM items WHERE context_id=? AND unit_type='pr_thread' AND unit_key=?",
                              (context_id, thread)).fetchone()
        old_row = db.execute("SELECT * FROM item_versions WHERE item_id=? AND item_version=?",
                            (previous["id"], previous["current_item_version"])).fetchone() if previous else None
        old = json.loads(old_row["snapshot_json"]) if old_row else {}
        refs, fences, evidence, assessments, material = [], [], [], [], []
        actions, actors, complete = set(), set(), True
        comment_ids = {json.loads(row["source_facts_json"]).get("commentId"): row["source_id"] for row in members}
        for row in members:
            sid = row["source_id"]
            source = visible[sid]
            facts = source["sourceFacts"]
            current_context = next(c for c in source["contexts"] if c["id"] == context_id)
            refs.append(dict(sourceId=sid, sourceVersion=source["sourceVersion"], sourceRevision=source["sourceRevision"]))
            fences.append(dict(sourceId=sid, contextId=context_id, contextVersion=row["context_version"],
                configurationRevision=row["configuration_revision"], authorizationRevision=row["authorization_revision"]))
            if source["lifecycle"] == "source_deleted":
                continue
            complete &= (facts.get("associationVerified") is True and facts.get("threadCoverage") == "complete"
                         and source["completeness"] == "complete")
            roster = facts.get("threadCommentIds")
            complete &= isinstance(roster, list) and bool(roster) and set(roster).issubset(comment_ids)
            saved = current_context.get("assessments", [])
            assessment = saved[0] if saved else None
            if facts.get("associationVerified") is not True:
                assessment = None
            reply = facts.get("inReplyToId")
            parent = comment_ids.get(reply) if reply else None
            if reply and (not parent or not assessment or parent not in
                          {r.get("sourceId") for r in assessment.get("dependencies", [])}):
                assessment = None
            segments, known = _segments(assessment, current_context.get("evidence", []), source)
            complete &= known
            if assessment:
                assessments.append(assessment)
                # A title/parent context may live outside the inline members.
                # Keep its authority in the Item's read/publication boundary too.
                for dependency in assessment.get("dependencies", []):
                    if dependency["sourceId"] in {ref["sourceId"] for ref in refs} or dependency["sourceId"] in {r["source_id"] for r in members}:
                        continue
                    dependency_context = db.execute("SELECT * FROM source_contexts WHERE source_id=? AND context_id=?",
                        (dependency["sourceId"], context_id)).fetchone()
                    refs.append(dependency)
                    fences.append(dict(sourceId=dependency["sourceId"], contextId=context_id,
                        contextVersion=dependency_context["context_version"],
                        configurationRevision=dependency_context["configuration_revision"],
                        authorizationRevision=dependency_context["authorization_revision"]))
            for values, rows_evidence in segments:
                labels = sorted(action for question, action in _ACTIONS.items() if values.get(question) == "present")
                actions.update(labels)
                author = (facts.get("author") or {}).get("githubId")
                pull_author = (facts.get("pullAuthor") or {}).get("githubId")
                recipient = None
                if labels:
                    # Identity comes only from authoritative reply relationships, never prose.
                    if author == pull_author and parent:
                        recipient = (visible[parent]["sourceFacts"].get("author") or {}).get("githubId")
                    elif author and pull_author and author != pull_author:
                        recipient = pull_author
                    if recipient:
                        actors.add(recipient)
                    material.append(dict(sourceId=sid, labels=labels, recipient=recipient,
                        text=[e.get("text") for e in rows_evidence], blocking=values.get("blocking_language"),
                        dependencies=assessment.get("dependencies", [])))
                for e in rows_evidence:
                    e["actionTypes"] = labels
                    if values.get("completion_claim") == "present":
                        e["progressType"] = "completion_claim"
                    evidence.append(e)
        if not actions and not previous:
            return None  # Pure progress/unknown/thanks do not create a task.
        def thread_fact(name):
            values = [visible[r["source_id"]]["sourceFacts"].get(name)
                      if visible[r["source_id"]]["sourceFacts"].get("associationVerified") is True else None
                      for r in members]
            return values[0] if values and type(values[0]) is bool and all(v is values[0] for v in values) else None
        resolved = thread_fact("isResolved")
        lifecycle = "source_closed" if resolved or trigger_facts.get("pullState") == "closed" else "active"
        if all(visible[r["source_id"]]["lifecycle"] == "source_deleted" for r in members):
            lifecycle = "source_deleted"
        if lifecycle == "active" and complete and not actions:
            lifecycle = "superseded"
        signature = _canonical_hash(dict(material=material, actors=sorted(actors), lifecycle=lifecycle,
                                        context=sorted({(f["contextVersion"], f["configurationRevision"]) for f in fences})))
        same = (complete and old.get("semanticComplete") is True
                and old.get("semanticSignature") == signature)
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        if not complete:
            available = {e["id"] for e in evidence}
            evidence += [dict(e, status="expired") for e in old.get("evidence", []) if e["id"] not in available]
        snapshot = dict(module="pr", repositoryId=trigger["repository_id"], watchId=None,
            actionTypes=sorted(actions), title="Follow up review thread", sourceUrl=members[0]["source_url"],
            sourceFacts=dict(pullNumber=trigger_facts.get("pullNumber"), threadId=thread,
                threadResolved=resolved, threadOutdated=thread_fact("isOutdated")),
            evidence=evidence, assessments=assessments,
            nextActors=[dict(kind="user", githubId=actor) for actor in sorted(actors)],
            lifecycle=lifecycle, closureReason=lifecycle if lifecycle != "active" else None,
            attentionState="closed" if lifecycle != "active" else "needs_confirmation" if not complete else
                "optional" if actions == {"optional_suggestion"} else "needs_action",
            semanticComplete=bool(complete), semanticSignature=signature,
            attentionUpdatedAt=old.get("attentionUpdatedAt", timestamp) if same or not complete else timestamp)
        from .product_rule_items import _parent_overlay
        snapshot, refs, fences = _parent_overlay(db, snapshot, refs, fences, context_id, now)
        item = store._item_dto(previous) if previous else store.create_item(
            context_id=context_id, unit_type="pr_thread", unit_key=thread)
        updated = store.publish_item_snapshot(item_id=item["id"], expected_item_revision=item["revision"],
            sources=refs, context_fences=fences, snapshot=snapshot, observed_at=now)
        for member in members:
            db.execute("UPDATE source_contexts SET item_id=? WHERE source_id=? AND context_id=?",
                       (item["id"], member["source_id"], context_id))
        if updated["itemVersion"] != item["itemVersion"]:
            handling = db.execute("SELECT * FROM item_handling_events WHERE item_id=? ORDER BY rowid DESC LIMIT 1",
                                  (item["id"],)).fetchone()
            if handling:
                carry = same and handling["item_version"] == item["itemVersion"]
                db.execute('''INSERT INTO item_handling_events(id,item_id,item_version,actor_id,disposition,
                    assignee_id,note,feedback,event_kind,carried_from_item_version,created_at)
                    VALUES (?,?,?,'system',?,?,?,?,?,?,?)''',
                    ("handling_" + uuid.uuid4().hex, item["id"], updated["itemVersion"],
                     handling["disposition"] if carry else "open", handling["assignee_id"] if carry else None,
                     handling["note"] if carry else None, handling["feedback"] if carry else None,
                     "handling_carried" if carry else "semantic_action_changed", item["itemVersion"] if carry else None, now))
        return updated
