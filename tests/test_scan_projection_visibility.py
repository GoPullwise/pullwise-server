from unittest import TestCase
from unittest.mock import patch
from pullwise_server import app


class ScanProjectionVisibilityTest(TestCase):
    def test_terminal_result_waits_for_snapshot_and_quota_projection(self):
        snapshot = {'id': 'sc_projection', 'status': 'running', 'progress': 25, 'quotaState': 'reserved'}
        job = {'job_id': 'job_projection', 'scan_id': snapshot['id'], 'status': 'failed',
               'result_checksum': 'abc', 'error': 'Repository exceeds checkout limits'}
        self.assertEqual(app.scan_snapshot_with_job_state(snapshot, job)['status'], 'running')
        projected = {**snapshot, 'status': 'failed', 'errorCode': 'REPOSITORY_TOO_LARGE', 'resultChecksum': 'abc'}
        self.assertEqual(app.scan_snapshot_with_job_state(projected, job)['status'], 'running')
        settled = app.scan_snapshot_with_job_state({**projected, 'quotaState': 'released'}, job)
        self.assertEqual(settled['status'], 'failed')
        self.assertEqual(settled['errorCode'], 'REPOSITORY_TOO_LARGE')

    def test_server_terminal_without_worker_result_remains_terminal(self):
        snapshot = {'id': 'sc_projection', 'status': 'running', 'progress': 25}
        job = {'job_id': 'job_projection', 'scan_id': snapshot['id'], 'status': 'failed', 'error': 'worker lost'}
        self.assertEqual(app.scan_snapshot_with_job_state(snapshot, job)['status'], 'failed')

    def test_warm_reads_reload_reconciled_snapshots_in_one_batch(self):
        old = {'id': 'sc_projection', 'status': 'running', 'progress': 25, 'quotaState': 'reserved'}
        settled = {**old, 'status': 'failed', 'quotaState': 'released', 'errorCode': 'REPOSITORY_TOO_LARGE'}
        job = {'job_id': 'job_projection', 'scan_id': old['id'], 'status': 'failed', 'result_checksum': 'abc'}
        with patch.object(app.db, 'list_scan_snapshots_for_scan_ids', side_effect=[[old], [settled]]) as snapshots, \
             patch.object(app, 'memory_scan_by_id', return_value=dict(old)), \
             patch.object(app.db, 'list_completed_scan_job_results_for_job_ids', return_value=[]), \
             patch.object(app, 'reconcile_scan_job_state_locked', return_value=True):
            result = app.hydrate_scan_jobs_for_read([job])
        self.assertEqual(result[0]['errorCode'], 'REPOSITORY_TOO_LARGE')
        self.assertEqual(result[0]['quotaState'], 'released')
        self.assertEqual(snapshots.call_count, 2)
