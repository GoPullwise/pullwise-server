import tempfile
import unittest
import json
from pathlib import Path

from pullwise_server.product_store import ProductStore
from pullwise_server.product_rule_items import publish_rule_items, reconcile_pr_items


class RuleItemsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProductStore(Path(self.tmp.name) / 'store.db')
        self.store.initialize()
        self.target = dict(context_id='ctx', module='pr', context_version=1,
                           configuration_revision=1, authorization_revision=1)
        self.source = dict(sourceId='src', sourceType='pr_state', externalKey='pr:1',
                           repositoryId='repo', sourceUrl='https://github.com/a/b/pull/1',
                           content={}, completeness='complete', sourceFacts=dict(
                               state='open', requestedReviewers=[{'githubId': '1'}, {'githubId': '2'}],
                               requestedTeams=[]))

    def publish(self, now=100, accessible=True):
        s = self.source
        with self.store.atomic() as store:
            record = store.upsert_source_snapshot(source_id=s['sourceId'], source_type=s['sourceType'],
                external_key=s['externalKey'], repository_id=s['repositoryId'], content=s['content'],
                source_facts=s['sourceFacts'], source_url=s['sourceUrl'], processing_mode='rules_only',
                completeness=s['completeness'], lifecycle='active', observed_at=now)
            store.set_source_context(source_id='src', context_id='ctx', context_version=1,
                configuration_revision=1, authorization_revision=1, authorization_valid_until=1000,
                accessible=accessible)
            publish_rule_items(store, source=s, record=record, target=self.target, now=now)

    def rows(self):
        with self.store._read() as db:
            return db.execute('SELECT * FROM items ORDER BY unit_key').fetchall()

    def test_requests_are_independent_and_poll_is_idempotent(self):
        self.publish()
        self.assertEqual(len(self.rows()), 2)
        self.publish(101)
        self.assertEqual([r['current_item_version'] for r in self.rows()], [1, 1])
        self.assertEqual(self.current_snapshot()['attentionUpdatedAt'], '1970-01-01T00:01:40Z')

    def test_withdrawal_and_rerequest_reopen_done(self):
        self.publish()
        first = self.rows()[0]
        self.store.patch_item_handling(item_id=first['id'], item_version=1,
            expected_revision=first['revision'], actor_id='human', disposition='done')
        self.source['sourceFacts']['title'] = 'changed metadata'
        self.publish(101)
        events = self.store.list_handling_events(first['id'])
        self.assertEqual(events[-1]['eventKind'], 'handling_carried')
        self.assertEqual(events[-1]['actorId'], 'system')
        self.source['sourceFacts']['requestedReviewers'] = [{'githubId': '2'}]
        self.publish(102)
        self.assertEqual(self.current_snapshot()['closureReason'], 'source_closed')
        self.source['sourceFacts']['requestedReviewers'].append({'githubId': '1'})
        self.publish(103)
        self.assertEqual(self.store.list_handling_events(first['id'])[-1]['disposition'], 'open')
        self.assertEqual(self.rows()[0]['current_item_version'], 4)

    def test_revoked_context_rolls_back(self):
        with self.assertRaisesRegex(ValueError, 'STALE_AUTHORIZATION'):
            self.publish(accessible=False)
        self.assertEqual(self.rows(), [])

    def test_missing_request_list_does_not_close_previous_requests(self):
        self.publish()
        del self.source['sourceFacts']['requestedReviewers']
        self.publish(101)
        self.assertEqual([r['current_item_version'] for r in self.rows()], [1, 1])

    def test_ci_failure_without_logs_or_model_still_creates_item(self):
        self.target['module'] = 'ci'
        self.source.update(sourceType='ci_failure', externalKey='run:1:job:2', completeness='unavailable',
                           content={'windows': []}, sourceFacts={'conclusion': 'failure', 'runId': '1',
                           'jobId': '2', 'runAttempt': 1, 'windows': []})
        self.publish()
        self.assertEqual(len(self.rows()), 1)
        with self.store._read() as db:
            for table in ('assessments', 'processing_usage_ledger', 'background_jobs', 'provider_attempts'):
                self.assertEqual(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
            snapshot = json.loads(db.execute('SELECT snapshot_json FROM item_versions').fetchone()[0])
            self.assertEqual(snapshot['actionTypes'], ['investigate_failure'])
            self.assertEqual(snapshot['sourceFacts']['recoveryStatus'], 'unknown')

    def test_verified_superseding_review_with_empty_body_closes_only_formal_rule(self):
        self.source.update(sourceType='pr_review_body', externalKey='review:8',
            content={'body': ''}, sourceFacts={'pullNumber': 1, 'reviewId': '8',
                'reviewState': 'CHANGES_REQUESTED', 'formalReviewStatus': 'effective',
                'pullAuthor': {'githubId': '1'}, 'reviewer': {'githubId': '2'}})
        self.publish()
        item = self.rows()[0]
        self.assertEqual(self.current_snapshot()['actionTypes'], ['change_requested'])
        self.source['sourceFacts']['formalReviewStatus'] = 'superseded'
        self.publish(101)
        closed = self.current_snapshot()
        self.assertEqual(closed['closureReason'], 'superseded')
        self.assertEqual(closed['actionTypes'], [])
        self.assertEqual(self.rows()[0]['id'], item['id'])
        self.assertEqual(self.rows()[0]['current_item_version'], 2)

    def test_superseded_review_with_text_remains_pending_for_semantic_request(self):
        self.source.update(sourceType='pr_review_body', externalKey='review:8',
            content={'body': 'Please add a test.'}, sourceFacts={'pullNumber': 1, 'reviewId': '8',
                'reviewState': 'CHANGES_REQUESTED', 'formalReviewStatus': 'effective',
                'pullAuthor': {'githubId': '1'}, 'reviewer': {'githubId': '2'}})
        self.publish()
        self.source['sourceFacts']['formalReviewStatus'] = 'superseded'
        self.publish(101)
        snapshot = self.current_snapshot()
        self.assertEqual(snapshot['attentionState'], 'needs_confirmation')
        self.assertNotEqual(snapshot['closureReason'], 'superseded')

    def test_dismissed_formal_review_closes_existing_empty_body_action(self):
        self.source.update(sourceType='pr_review_body', externalKey='review:8',
            content={'body': ''}, sourceFacts={'pullNumber': 1, 'reviewId': '8',
                'reviewState': 'CHANGES_REQUESTED', 'formalReviewStatus': 'effective',
                'pullAuthor': {'githubId': '1'}, 'reviewer': {'githubId': '2'}})
        self.publish()
        self.source['sourceFacts'].update(reviewState='DISMISSED', formalReviewStatus='dismissed')
        self.publish(101)
        self.assertEqual(self.current_snapshot()['closureReason'], 'superseded')

    def test_changed_ci_log_evidence_reopens_handling(self):
        self.target['module'] = 'ci'
        self.source.update(sourceType='ci_failure', externalKey='run:1:job:2',
            content={'windows': [{'windowId': 'w', 'text': 'first'}]}, sourceFacts={
                'conclusion': 'failure', 'runId': '1', 'jobId': '2', 'runAttempt': 1, 'windows': []})
        self.publish()
        item = self.rows()[0]
        self.store.patch_item_handling(item_id=item['id'], item_version=1,
            expected_revision=item['revision'], actor_id='human', disposition='done')
        self.source['content']['windows'][0]['text'] = 'different evidence'
        self.publish(101)
        self.assertEqual(self.store.list_handling_events(item['id'])[-1]['disposition'], 'open')

    def semantic_snapshot(self):
        item = self.rows()[0]
        with self.store._read() as db:
            v = db.execute('SELECT * FROM item_versions WHERE item_id=? AND item_version=?',
                           (item['id'], item['current_item_version'])).fetchone()
        snapshot = json.loads(v['snapshot_json'])
        snapshot['assessments'] = [{'id': 'assessment_saved', 'answers': {'symptom': 'timeout'}}]
        snapshot['sourceFacts']['windows'] = [{'windowId': 'w', 'symptoms': ['timeout']}]
        return self.store.publish_item_snapshot(item_id=item['id'], expected_item_revision=item['revision'],
            sources=json.loads(v['sources_json']), context_fences=json.loads(v['context_fences_json']),
            snapshot=snapshot, observed_at=100)

    def current_snapshot(self):
        item = self.rows()[0]
        with self.store._read() as db:
            return json.loads(db.execute('SELECT snapshot_json FROM item_versions WHERE item_id=? AND item_version=?',
                (item['id'], item['current_item_version'])).fetchone()[0])

    def test_identical_poll_preserves_semantic_snapshot(self):
        self.test_ci_failure_without_logs_or_model_still_creates_item()
        item = self.semantic_snapshot()
        before = self.current_snapshot()
        self.publish(101)
        self.assertEqual(self.rows()[0]['current_item_version'], item['itemVersion'])
        self.assertEqual(self.current_snapshot(), before)
        self.source['sourceFacts']['name'] = 'new job metadata'
        self.publish(102)
        self.assertEqual(self.current_snapshot()['assessments'], [])
        self.assertEqual(self.current_snapshot()['attentionState'], 'needs_action')

    def test_unknown_review_preserves_historical_action_pending_confirmation(self):
        self.source.update(sourceType='pr_review_body', externalKey='review:1', content={'body': 'change it'},
            sourceFacts={'formalReviewStatus': 'effective', 'reviewState': 'CHANGES_REQUESTED',
                         'pullState': 'open', 'pullAuthor': {'githubId': 'author'}})
        self.publish()
        self.semantic_snapshot()
        before = self.current_snapshot()
        self.source['sourceFacts']['formalReviewStatus'] = 'unknown'
        self.source['content']['body'] = 'edited'
        self.publish(101)
        current = self.current_snapshot()
        self.assertEqual(current['attentionState'], 'needs_confirmation')
        self.assertEqual(current['attentionUpdatedAt'], before['attentionUpdatedAt'])
        self.assertEqual(current['assessments'], [])
        self.assertTrue(current['historicalAssessments'])
        self.assertEqual(current['sourceFacts']['formalReviewStatus'], 'unknown')
        self.assertEqual(current['evidence'][0]['status'], 'expired')
        self.assertEqual(current['evidence'][0]['sourceVersion'], before['evidence'][0]['sourceVersion'])
        version = self.rows()[0]['current_item_version']
        self.publish(102)
        self.assertEqual(self.rows()[0]['current_item_version'], version)

    def test_changed_release_marks_existing_semantic_item_unconfirmed(self):
        self.target['module'] = 'updates'
        self.source.update(sourceType='release', externalKey='release:1', content={'body': 'old'}, sourceFacts={})
        self.publish()
        self.assertEqual(self.rows(), [])
        item = self.store.create_item(context_id='ctx', unit_type='update_release', unit_key='release:1')
        with self.store._read() as db:
            source = db.execute('SELECT * FROM source_records WHERE source_id=?', ('src',)).fetchone()
        self.store.publish_item_snapshot(item_id=item['id'], expected_item_revision=item['revision'],
            sources=[dict(sourceId='src', sourceVersion=source['latest_version'], sourceRevision=source['source_revision'])],
            context_fences=[dict(sourceId='src', contextId='ctx', contextVersion=1,
                configurationRevision=1, authorizationRevision=1)], observed_at=100,
            snapshot=dict(module='updates', actionTypes=['upgrade'], evidence=[],
                assessments=[{'id': 'saved'}], lifecycle='active', attentionState='needs_action',
                attentionUpdatedAt='1970-01-01T00:01:40Z', sourceFacts={}))
        self.source['content']['body'] = 'changed'
        self.publish(101)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.current_snapshot()['attentionState'], 'needs_confirmation')
        self.assertEqual(self.current_snapshot()['assessments'], [])
        self.assertEqual(self.current_snapshot()['historicalAssessments'], [{'id': 'saved'}])
        self.assertEqual(self.current_snapshot()['attentionUpdatedAt'], '1970-01-01T00:01:40Z')

    def parent_state(self, state, now):
        with self.store.atomic() as store:
            store.upsert_source_snapshot(source_id='parent', source_type='pr_state', external_key='pr:1',
                repository_id='repo', content={}, source_facts={'pullNumber': 1, 'state': state},
                source_url='https://github.com/a/b/pull/1', processing_mode='rules_only',
                completeness='complete', lifecycle='active', observed_at=now)
            store.set_source_context(source_id='parent', context_id='ctx', context_version=1,
                configuration_revision=1, authorization_revision=1, authorization_valid_until=1000, accessible=True)
            reconcile_pr_items(store, target=self.target, now=now)

    def test_parent_close_late_review_and_reopen(self):
        self.source.update(sourceType='pr_review_body', externalKey='review:1', content={'body': 'change it'},
            sourceFacts={'pullNumber': 1, 'formalReviewStatus': 'effective', 'reviewState': 'CHANGES_REQUESTED',
                         'pullState': 'open', 'pullAuthor': {'githubId': 'author'}})
        self.publish()
        original = self.rows()[0]
        self.store.patch_item_handling(item_id=original['id'], item_version=1,
            expected_revision=original['revision'], actor_id='human', disposition='done')
        self.parent_state('closed', 101)
        self.assertEqual(self.current_snapshot()['lifecycle'], 'source_closed')
        self.assertEqual(self.current_snapshot()['sourceFacts']['pullState'], 'open')
        with self.store._read() as db:
            v = db.execute('SELECT sources_json FROM item_versions WHERE item_id=? ORDER BY item_version DESC LIMIT 1',
                           (original['id'],)).fetchone()
        self.assertEqual({s['sourceId'] for s in json.loads(v[0])}, {'src', 'parent'})
        self.source['content']['body'] = 'late edit with old pullState'
        self.publish(102)
        reconcile_pr_items(self.store, target=self.target, now=102)
        self.assertEqual(self.current_snapshot()['attentionState'], 'closed')
        closed_version = self.rows()[0]['current_item_version']
        self.publish(102)
        reconcile_pr_items(self.store, target=self.target, now=102)
        self.publish(102)
        reconcile_pr_items(self.store, target=self.target, now=102)
        self.assertEqual(self.rows()[0]['current_item_version'], closed_version)
        self.parent_state('open', 103)
        self.assertEqual(self.current_snapshot()['attentionState'], 'needs_confirmation')
        self.assertGreater(self.rows()[0]['current_item_version'], closed_version)
        self.assertEqual(self.store.list_handling_events(original['id'])[-1]['disposition'], 'open')

    def test_parent_closure_does_not_match_other_pr_or_expired_authority(self):
        self.source.update(sourceType='pr_review_body', externalKey='review:1', content={'body': 'change it'},
            sourceFacts={'pullNumber': 2, 'formalReviewStatus': 'effective', 'reviewState': 'CHANGES_REQUESTED',
                         'pullState': 'open', 'pullAuthor': {'githubId': 'author'}})
        self.publish()
        self.parent_state('closed', 101)
        self.assertEqual(self.current_snapshot()['lifecycle'], 'active')
        with self.store._immediate() as db:
            db.execute('UPDATE source_contexts SET authorization_valid_until=100 WHERE source_id=?', ('parent',))
        self.source['sourceFacts']['pullNumber'] = 1
        self.publish(102)
        reconcile_pr_items(self.store, target=self.target, now=102)
        self.assertEqual(self.current_snapshot()['lifecycle'], 'active')
