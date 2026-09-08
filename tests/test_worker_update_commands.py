from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pullwise_server import app, db
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.test_worker_admin_routes import RouteHarness, reset_state


class WorkerUpdateCommandTest(unittest.TestCase):
    def setUp(self):
        reset_state()
        start_fast_sqlite_connections(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, {'PULLWISE_DB_PATH': os.path.join(temporary.name, 'server.sqlite3'),
            'PULLWISE_ADMIN_EMAILS': 'admin@example.com'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        install_initialized_db_template(os.environ['PULLWISE_DB_PATH'])
        self.worker = db.create_worker({'name': 'Managed worker', 'version': '0.10.24'})

    def test_update_snapshots_version_and_succeeds_only_after_new_version_heartbeat(self):
        worker_id = self.worker['worker_id']
        handler = RouteHarness(f'/admin/workers/{worker_id}/commands',
            {'command': 'update', 'version': '0.10.25'}, cookie='pw_session=ses_admin')
        app.PullwiseHandler.route(handler, 'POST')
        self.assertEqual(handler.status, 202, handler.payload)
        command = handler.payload['command']
        self.assertEqual(command['payload']['version'], '0.10.25')
        self.assertTrue(command['payload']['package_url'].endswith('/v0.10.25/pullwise-worker-0.10.25.tgz'))
        self.assertTrue(db.get_worker(worker_id)['enabled'], 'queued updates must allow the active run to keep renewing its profile')
        self.assertEqual(json.loads(db.get_worker_command(command['id'])['payload_json']), {'version': '0.10.25'})
        db.update_worker_command_status({'id': command['id'], 'worker_id': worker_id, 'status': 'running'})
        with self.assertRaisesRegex(ValueError, 'heartbeat'):
            db.update_worker_command_status({'id': command['id'], 'worker_id': worker_id, 'status': 'succeeded'})
        db.upsert_worker_heartbeat({'worker_id': worker_id, 'version': '0.10.25', 'timestamp': app.now()})
        succeeded = db.update_worker_command_status({'id': command['id'], 'worker_id': worker_id, 'status': 'succeeded'})
        self.assertEqual(succeeded['status'], 'succeeded')

    def test_invalid_update_version_is_rejected_without_queuing(self):
        handler = RouteHarness(f"/admin/workers/{self.worker['worker_id']}/commands",
            {'command': 'update', 'version': '1.2.3; unsafe'}, cookie='pw_session=ses_admin')
        app.PullwiseHandler.route(handler, 'POST')
        self.assertEqual(handler.status, 400)
        self.assertIsNone(db.get_next_worker_command(self.worker['worker_id']))

    def test_update_blocks_new_leases_and_waits_for_the_active_run(self):
        worker_id = self.worker['worker_id']
        scan = {'id': 'scan-update-drain', 'repo': 'demo/repo', 'repoId': 'demo/repo',
                'branch': 'main', 'commit': 'abc', 'userId': 'u_chen', 'status': 'queued'}
        app.create_scan_job_for_scan(scan)
        command = db.create_worker_command({'worker_id': worker_id, 'command': 'update',
                                            'payload': {'version': '0.10.25'}})
        self.assertIsNone(db.claim_next_scan_job(worker_id))
        with db.connect() as connection:
            connection.execute("UPDATE scan_jobs SET status = 'running', claimed_by_worker_id = ?", (worker_id,))
        with self.assertRaisesRegex(ValueError, 'active run'):
            db.update_worker_command_status({'id': command['id'], 'worker_id': worker_id, 'status': 'running'})

    def test_deleted_instance_can_retry_its_final_acknowledgement(self):
        worker_id = self.worker['worker_id']
        command = db.create_worker_command({'worker_id': worker_id, 'command': 'uninstall'})
        for _ in range(2):
            handler = RouteHarness(f"/worker/commands/{command['id']}/status",
                {'worker_id': worker_id, 'status': 'succeeded'},
                headers={'Authorization': f"Bearer {self.worker['worker_token']}"})
            app.PullwiseHandler.route(handler, 'POST')
            self.assertEqual(handler.status, 200, handler.payload)
            self.assertEqual(handler.payload['command']['status'], 'succeeded')
