"""Local-only CF1 experiment. Never mount this probe in the product router."""
import importlib
import json
import sys
import re
import time
from urllib.parse import urlsplit, parse_qs
from workers import Response, WorkerEntrypoint

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS probe_budget (scope TEXT PRIMARY KEY, used INTEGER NOT NULL CHECK(used BETWEEN 0 AND 1))",
    "CREATE TABLE IF NOT EXISTS probe_guard (ok INTEGER CHECK(ok=1))",
    "CREATE TABLE IF NOT EXISTS probe_attempt (id TEXT PRIMARY KEY, owner TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS probe_clock (id INTEGER PRIMARY KEY CHECK(id=1), ticks INTEGER NOT NULL)",
]

DOMAIN_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS probe_domain_clock (id INTEGER PRIMARY KEY, now INTEGER, auth INTEGER, config INTEGER, accessible INTEGER)",
    "CREATE TABLE IF NOT EXISTS probe_domain_job (id INTEGER PRIMARY KEY, state TEXT, token TEXT, lease INTEGER, next INTEGER, auth INTEGER, config INTEGER, attempt INTEGER)",
    "CREATE TABLE IF NOT EXISTS probe_domain_usage (id INTEGER PRIMARY KEY, reserved INTEGER CHECK(reserved>=0), used INTEGER CHECK(used>=0))",
    "CREATE TABLE IF NOT EXISTS probe_domain_result (job_id INTEGER PRIMARY KEY, assessment TEXT)",
    "CREATE TABLE IF NOT EXISTS probe_domain_guard (ok INTEGER CHECK(ok=1))",
]
DOMAIN_STATE = """SELECT j.*, c.now, u.reserved, u.used,
    (SELECT COUNT(*) FROM probe_domain_result) AS results
    FROM probe_domain_job j, probe_domain_clock c, probe_domain_usage u"""
DOMAIN_FENCE = """id=1 AND state='running' AND token=?
    AND lease>(SELECT now FROM probe_domain_clock)
    AND auth=(SELECT auth FROM probe_domain_clock)
    AND config=(SELECT config FROM probe_domain_clock)
    AND (SELECT accessible FROM probe_domain_clock)=1"""

ATTEMPT_SCHEMA = [
    "CREATE TABLE IF NOT EXISTS probe_attempt_clock (id INTEGER PRIMARY KEY, now INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS probe_attempt_counter (scope TEXT NOT NULL, period TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(scope,period))",
    "CREATE TABLE IF NOT EXISTS probe_attempt_event (id TEXT PRIMARY KEY, owner TEXT NOT NULL, at INTEGER NOT NULL, period TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS probe_attempt_guard (ok INTEGER CHECK(ok=1))",
]
ATTEMPT_PERIOD = "strftime('%Y-%m',(SELECT now FROM probe_attempt_clock WHERE id=1),'unixepoch')"
SERVER_SCHEDULE_SCHEMA = """CREATE TABLE IF NOT EXISTS probe_server_schedule_enabled(
    id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL CHECK(enabled IN (0,1))
)"""


class Default(WorkerEntrypoint):
    async def server_mapping(self, request, url):
        from server_fixture import DATA
        from pullwise_server.cloudflare_d1_batch import execute_d1_batch
        from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
        from pullwise_server.cloudflare_analysis_adapter import D1AnalysisTransactions
        from pullwise_server.cloudflare_webhook_receipts import D1WebhookReceipts
        from pullwise_server.cloudflare_creem_handler import accept_signed_creem_webhook
        from pullwise_server.cloudflare_source_read import D1SourceReads
        from pullwise_server.cloudflare_watch_adapter import D1WatchTransactions
        from pullwise_server.cloudflare_repository_adapter import D1RepositoryTransactions
        from pullwise_server.cloudflare_manual_sync import D1ManualSyncTransactions
        from pullwise_server.cloudflare_session_adapter import D1SessionTransactions
        from pullwise_server.cloudflare_oauth_state_adapter import D1OAuthStates
        from pullwise_server.cloudflare_billing_catalog_write import D1BillingCatalogTransactions
        from pullwise_server import creem_event_rules
        import server_mapping as mapping
        name = url.path.removeprefix('/server-map/')
        if request.method == 'GET' and name == 'session-state':
            row = await self.env.DB.prepare("SELECT payload FROM app_state WHERE name='sessions'").first()
            sessions = json.loads(row['payload']) if row else {}
            return Response.json({'count': len(sessions), 'syntheticPresent': 'ses-synthetic' in sessions})
        if request.method == 'GET' and name == 'oauth-state':
            row = await self.env.DB.prepare("SELECT payload FROM app_state WHERE name='githubStates'").first()
            states = json.loads(row['payload']) if row else {}
            return Response.json({'count': len(states), 'syntheticPresent': 'state-synthetic' in states})
        if request.method == 'GET' and name == 'billing-catalog-state':
            row = await self.env.DB.prepare("""SELECT payload_json,source_revision
                FROM billing_public_catalog WHERE id=1""").first()
            if row is None:
                return Response.json({'revision': 0, 'amount': None})
            catalog = json.loads(row['payload_json'])
            return Response.json({'revision': row['source_revision'],
                'amount': catalog['plans'][1]['prices']['month']['amount']})
        if request.method == 'GET' and name == 'watch-state':
            row = await self.env.DB.prepare('''SELECT
                (SELECT COUNT(*) FROM update_watches WHERE archived_at IS NULL) AS active,
                (SELECT MAX(context_version) FROM watch_controls) AS contextVersion,
                (SELECT COUNT(*) FROM provider_attempts) AS attempts,
                (SELECT reserved FROM processing_usage_buckets LIMIT 1) AS reserved,
                (SELECT state FROM processing_usage_ledger WHERE charge_key='charge') AS chargeState,
                (SELECT state FROM background_jobs LIMIT 1) AS jobState,
                (SELECT accessible FROM source_contexts WHERE source_id='1') AS firstAccessible,
                (SELECT context_version FROM source_contexts WHERE source_id='1') AS firstContextVersion,
                (SELECT context_stale FROM source_contexts WHERE source_id='1') AS firstContextStale''').first()
            return Response.json(row)
        if request.method == 'GET' and name == 'repository-state':
            row = await self.env.DB.prepare('''SELECT
                (SELECT revision FROM repository_services WHERE repository_id='repo') AS revision,
                (SELECT state FROM background_jobs LIMIT 1) AS jobState,
                (SELECT state FROM processing_usage_ledger WHERE charge_key='charge') AS chargeState,
                (SELECT reserved FROM processing_usage_buckets LIMIT 1) AS reserved,
                (SELECT configuration_revision FROM source_contexts WHERE source_id='1') AS config,
                (SELECT analysis_enabled FROM source_contexts WHERE source_id='1') AS analysis,
                (SELECT accessible FROM source_contexts WHERE source_id='1') AS accessible,
                (SELECT accessible FROM discovery_targets WHERE resource_kind='watch' LIMIT 1) AS watchProof,
                (SELECT COUNT(*) FROM provider_attempts) AS attempts''').first()
            return Response.json(row)
        if request.method == 'GET' and name == 'manual-sync-state':
            return Response.json(await self.env.DB.prepare('''SELECT
                (SELECT COUNT(*) FROM background_jobs WHERE job_type='sync_watch') AS jobs,
                (SELECT generation FROM background_jobs WHERE id='job-manual-probe') AS generation,
                (SELECT state FROM background_jobs WHERE id='job-manual-probe') AS state,
                (SELECT COUNT(*) FROM provider_attempts) AS attempts,
                (SELECT reserved FROM processing_usage_buckets LIMIT 1) AS reserved''').first())
        if request.method == 'GET' and name == 'source-read':
            try:
                items = await D1SourceReads(self.env.DB).list_sources_for_billing_owner(
                    owner_id='owner', source_id='1', include_content=True,
                    now=DATA['claim']['now'])
                return Response.json({'items': items})
            except Exception:
                return Response.json({'error': 'read failed'}, status=409)
        if request.method == 'GET' and name == 'state':
            return Response.json(await self.env.DB.prepare('''SELECT
                (SELECT used FROM processing_usage_buckets LIMIT 1) AS used,
                (SELECT reserved FROM processing_usage_buckets LIMIT 1) AS reserved,
                (SELECT limit_value FROM processing_usage_buckets LIMIT 1) AS limitValue,
                (SELECT COUNT(*) FROM processing_usage_ledger WHERE charge_key='probe-second') AS secondReservation,
                (SELECT COUNT(*) FROM billing_webhook_receipts) AS webhookReceipts,
                (SELECT COUNT(*) FROM billing_webhook_receipts WHERE state='applied') AS appliedReceipts,
                (SELECT COUNT(*) FROM background_jobs) AS jobCount,
                (SELECT state FROM processing_usage_ledger WHERE charge_key='probe-second') AS secondLedgerState,
                (SELECT processing_status FROM source_contexts WHERE source_id='3') AS thirdSourceStatus,
                (SELECT reservation_id FROM processing_usage_ledger WHERE charge_key='charge') AS chargeReservationId,
                (SELECT json_extract(value,'$.githubAccessToken.__encrypted')='pullwise-state-secret-v1'
                    FROM app_state,json_each(payload) WHERE name='users' AND key='owner') AS encryptedToken,
                (SELECT COUNT(*) FROM provider_attempts) AS attempts,
                (SELECT COUNT(*) FROM assessments) AS results,
                (SELECT COUNT(*) FROM item_versions) AS versions,
                (SELECT state FROM background_jobs LIMIT 1) AS jobState,
                (SELECT state FROM processing_usage_ledger WHERE charge_key='charge') AS chargeState,
                (SELECT attempt FROM background_jobs LIMIT 1) AS jobAttempt,
                (SELECT next_attempt_at FROM background_jobs LIMIT 1) AS nextAttemptAt,
                (SELECT revision FROM account_entitlement_authority LIMIT 1) AS accountRevision,
                (SELECT dirty FROM account_entitlement_authority LIMIT 1) AS accountDirty,
                (SELECT json_type(payload,'$."event-a"') IS NOT NULL FROM app_state
                    WHERE name='billingEvents') AS eventA,
                (SELECT json_type(payload,'$."event-b"') IS NOT NULL FROM app_state
                    WHERE name='billingEvents') AS eventB,
                (SELECT json_type(payload,'$."later"') IS NOT NULL FROM app_state
                    WHERE name='billingEvents') AS laterEvent,
                (SELECT json_array_length(payload) FROM app_state
                    WHERE name='billingPendingUpdates') AS pendingCount,
                (SELECT payload='{"event_fixture":{"status":"processed"}}' FROM app_state
                    WHERE name='billingEvents') AS paymentFactsPreserved,
                (SELECT json_type(payload,'$."event_fixture"') IS NOT NULL FROM app_state
                    WHERE name='billingEvents') AS originalPaymentFactPreserved''').first())
        if request.method != 'POST':
            return Response('Not found', status=404)
        if name in {'repository-create', 'repository-disable', 'repository-change-installation',
                    'repository-pause'}:
            try:
                result = await D1RepositoryTransactions(self.env.DB).put_service(
                    repository_id='repo', installation_id=(
                        'inst-2' if name == 'repository-change-installation' else 'inst-1'),
                    owner_id='owner',
                    expected_revision=0 if name == 'repository-create' else 1,
                    enabled=name != 'repository-pause', modules={'pr': True, 'ci': False},
                    analysis_enabled={'pr': name == 'repository-create', 'ci': False},
                    allow_member_sync=False, default_assignee_id=None,
                    priority_order=0, now=DATA['claim']['now'])
                return Response.json(result)
            except Exception:
                return Response.json({'error': 'repository rejected'}, status=409)
        if name == 'repository-seed-shared':
            try:
                watch = await D1WatchTransactions(self.env.DB).create_public_watch(
                    owner_id='owner', resolved_public_repository_id='github:101',
                    interests=['OAuth'], enabled=True, analysis_enabled=True,
                    now=DATA['claim']['now'])
                await self.env.DB.batch([
                    self.env.DB.prepare("UPDATE update_watches SET target_repository_id='repo' WHERE id=?").bind(watch['id']),
                    self.env.DB.prepare("UPDATE watch_controls SET target_repository_id='repo' WHERE watch_scope_key=?").bind(watch['watchScopeKey']),
                    self.env.DB.prepare("""INSERT INTO discovery_targets(
                        control_key,resource_kind,resource_id,context_id,module,
                        repository_id,github_repository_id,installation_id,app_id,
                        billing_owner_id,authorization_revision,accessible,valid_until)
                        VALUES(?,'watch',?,?,'updates','github:101','101',NULL,'app','owner',1,1,?)""").bind(
                            watch['watchScopeKey'], watch['id'], watch['watchScopeKey'], DATA['claim']['now']+300),
                    self.env.DB.prepare("UPDATE source_records SET source_type='release',repository_id='github:101' WHERE source_id='1'"),
                    self.env.DB.prepare("UPDATE source_contexts SET context_id=?,watch_id=?,analysis_enabled=1 WHERE source_id='1'").bind(
                        watch['watchScopeKey'],watch['id']),
                    self.env.DB.prepare("UPDATE background_jobs SET context_id=?,state='queued' WHERE source_id='1'").bind(watch['watchScopeKey']),
                    self.env.DB.prepare("UPDATE processing_usage_ledger SET state='reserved' WHERE charge_key='charge'"),
                    self.env.DB.prepare("UPDATE processing_usage_buckets SET reserved=1 WHERE billing_owner_id='owner'"),
                ])
                return Response.json({'watchId': watch['id']})
            except Exception:
                return Response.json({'error': 'shared watch fixture rejected'}, status=409)
        if name == 'manual-sync':
            watch = await self.env.DB.prepare("""SELECT id FROM update_watches
                WHERE upstream_repository_id='github:101' AND archived_at IS NULL
                LIMIT 1""").first()
            if watch is None:
                return Response.json({'error': 'watch missing'}, status=409)
            try:
                result = await D1ManualSyncTransactions(self.env.DB).request(
                    resource_kind='watch', resource_id=watch['id'],
                    owner_id='owner', job_id='job-manual-probe', now=DATA['claim']['now'])
                return Response.json(result)
            except Exception:
                return Response.json({'error': 'manual sync rejected'}, status=409)
        if name == 'watch-create':
            try:
                watch = await D1WatchTransactions(self.env.DB).create_public_watch(
                    owner_id='owner', resolved_public_repository_id='github:101',
                    interests=['OAuth'], enabled=True, analysis_enabled=False,
                    now=DATA['claim']['now'])
                return Response.json(watch)
            except Exception:
                return Response.json({'error': 'watch create rejected'}, status=409)
        if name == 'session-issue':
            try:
                session = await D1SessionTransactions(self.env.DB).issue_session(
                    owner_id='owner', session_id='ses-synthetic',
                    now=DATA['claim']['now'], expires_at=DATA['claim']['now'] + 86400)
                return Response.json(session)
            except Exception:
                return Response.json({'error': 'session issue rejected'}, status=409)
        if name == 'oauth-issue':
            try:
                await D1OAuthStates(self.env.DB).issue(state_id='state-synthetic',
                    record={'kind': 'login', 'redirectTo': 'dashboard',
                            'codeVerifier': 'synthetic-verifier',
                            'expiresAt': DATA['claim']['now'] + 300},
                    now=DATA['claim']['now'])
                return Response.json({'issued': True})
            except Exception:
                return Response.json({'error': 'oauth state rejected'}, status=409)
        if name == 'oauth-consume':
            try:
                record = await D1OAuthStates(self.env.DB).consume(
                    state_id='state-synthetic', expected_kind='login',
                    now=DATA['claim']['now'])
                return Response.json({'kind': record['kind'],
                    'redirectTo': record['redirectTo']})
            except Exception:
                return Response.json({'error': 'oauth state rejected'}, status=409)
        if name in {'billing-catalog-stage-1', 'billing-catalog-stage-2'}:
            revision = 1 if name.endswith('-1') else 2
            product = {'id': 'prod-pro-month', 'name': 'Pullwise Pro',
                'description': 'PR CI Updates',
                'price': 2900 if revision == 1 else 3000,
                'currency': 'USD', 'billing_type': 'recurring',
                'billing_period': 'every-month', 'status': 'active'}
            try:
                changed = await D1BillingCatalogTransactions(self.env.DB).stage_from_products(
                    configured_ids={'pro': ['prod-pro-month'], 'max': []},
                    fetched_products={'prod-pro-month': product},
                    source_revision=revision,
                    now=DATA['claim']['now'], expires_at=DATA['claim']['now'] + 3600)
                return Response.json({'changed': changed})
            except Exception:
                return Response.json({'error': 'catalog stage rejected'}, status=409)
        if name == 'session-revoke':
            try:
                revoked = await D1SessionTransactions(self.env.DB).revoke_session(
                    owner_id='owner', session_id='ses-synthetic', now=DATA['claim']['now'])
                return Response.json({'revoked': revoked})
            except Exception:
                return Response.json({'error': 'session revoke rejected'}, status=409)
        if name in {'watch-update-queued', 'watch-update-a'}:
            row = await self.env.DB.prepare("""SELECT id,revision FROM update_watches
                WHERE upstream_repository_id='github:101' AND archived_at IS NULL""").first()
            if not row:
                return Response.json({'error': 'watch missing'}, status=409)
            if name == 'watch-update-queued':
                await self.env.DB.prepare("UPDATE source_contexts SET watch_id=? WHERE source_id='1'").bind(row['id']).run()
                changes = {'interests': ['database'], 'analysisEnabled': False}
            else:
                changes = {'interests': ['OAuth']}
            try:
                watch = await D1WatchTransactions(self.env.DB).update_public_watch(
                    owner_id='owner', watch_id=row['id'],
                    expected_revision=row['revision'], changes=changes,
                    now=DATA['claim']['now'])
                return Response.json(watch)
            except Exception:
                return Response.json({'error': 'watch update rejected'}, status=409)
        if name == 'archive-secondary-watch':
            row = await self.env.DB.prepare("""SELECT id FROM update_watches
                WHERE upstream_repository_id='github:101' AND archived_at IS NULL""").first()
            if not row:
                return Response.json({'error': 'watch missing'}, status=409)
            await self.env.DB.prepare("UPDATE source_contexts SET watch_id=? WHERE source_id='2'").bind(row['id']).run()
            try:
                await D1WatchTransactions(self.env.DB).archive_watch(owner_id='owner',
                    watch_id=row['id'], expected_revision=1, now=DATA['claim']['now'])
            except Exception:
                return Response.json({'error': 'watch archive rejected'}, status=409)
            return Response.json({'archived': True})
        if name == 'disable-analysis-secondary':
            await self.env.DB.prepare("""UPDATE source_contexts
                SET analysis_enabled=0,configuration_revision=configuration_revision+1
                WHERE source_id='2'""").run()
            return Response.json({'disabled': True})
        if name == 'watch-archive-queued':
            row = await self.env.DB.prepare("""SELECT id FROM update_watches
                WHERE upstream_repository_id='github:101' AND archived_at IS NULL""").first()
            if not row:
                return Response.json({'error': 'watch missing'}, status=409)
            await self.env.DB.prepare("UPDATE source_contexts SET watch_id=? WHERE source_id='1'").bind(row['id']).run()
            try:
                await D1WatchTransactions(self.env.DB).archive_watch(owner_id='owner',
                    watch_id=row['id'], expected_revision=1, now=DATA['claim']['now'])
            except Exception:
                return Response.json({'error': 'watch archive rejected'}, status=409)
            return Response.json({'archived': True})
        if name == 'reset':
            await self.env.DB.batch([self.env.DB.prepare(sql) for sql in DATA['schemas']]
                + [self.env.DB.prepare(sql) for sql, _ in mapping.schema()])
            commands = [('DELETE FROM ' + table, ()) for table in reversed(DATA['names'])] + DATA['inserts']
            await self.env.DB.prepare(SERVER_SCHEDULE_SCHEMA).run()
            await self.env.DB.prepare("""INSERT INTO probe_server_schedule_enabled(id,enabled)
                VALUES (1,0) ON CONFLICT(id) DO UPDATE SET enabled=0""").run()
        elif name == 'claim-frozen':
            commands = mapping.claim(**DATA['claim'])
        elif name in {'claim', 'claim-current', 'claim-reconciled'}:
            args = DATA['claim']
            try:
                await D1AnalysisTransactions(self.env.DB).claim(
                    job_id=args['job_id'], token=args['token'], now=args['now'],
                    global_monthly_limit=args['global_monthly_limit'],
                    owner_rolling_limit=args['owner_rolling_limit'],
                    global_rolling_limit=args['global_rolling_limit'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name in {'publish', 'publish-current', 'publish-reconciled'}:
            args = dict(DATA['publication'])
            args.pop('account_snapshot')
            args.pop('account_revision')
            try:
                await D1AnalysisTransactions(self.env.DB).publication(**args)
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name in {'event-a', 'event-b'}:
            account = json.loads(DATA['claim']['account_snapshot'])
            changed = dict(account, billing=dict(account['billing'], plan='free'))
            changed = json.dumps(changed, separators=(',', ':'))
            next_value, revision = (changed, 1) if name == 'event-a' else (
                DATA['claim']['account_snapshot'], 2)
            try:
                await D1AccountTransactions(self.env.DB).stage_account_event(
                    owner_id='owner', expected_revision=revision, next_account_json=next_value,
                    event_id=name, event_record_json='{"applied":true}', now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'upgrade-max':
            account = json.loads(DATA['claim']['account_snapshot'])
            changed = json.dumps(dict(account, billing=dict(account['billing'], plan='max')),
                separators=(',', ':'))
            try:
                await D1AccountTransactions(self.env.DB).stage_account_event(
                    owner_id='owner', expected_revision=1, next_account_json=changed,
                    event_id='upgrade-max', event_record_json='{"applied":true}',
                    now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'refresh-upgrade':
            try:
                await D1AccountTransactions(self.env.DB).refresh_account_entitlement(
                    owner_id='owner', expected_revision=2, now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name in {'account-write-a', 'account-write-b'}:
            account = json.loads(DATA['claim']['account_snapshot'])
            changed = json.dumps(dict(account, githubLogin='renamed'), separators=(',', ':'))
            next_value, revision = (changed, 1) if name == 'account-write-a' else (
                DATA['claim']['account_snapshot'], 2)
            try:
                await D1AccountTransactions(self.env.DB).stage_account_write(
                    owner_id='owner', expected_revision=revision,
                    next_account_json=next_value, now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'refresh-account':
            try:
                await D1AccountTransactions(self.env.DB).refresh_account_entitlement(
                    owner_id='owner', expected_revision=3, now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'pending-event':
            try:
                await D1AccountTransactions(self.env.DB).park_webhook_receipt(
                    receipt_event_id='later', now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'reconcile-pending':
            account = json.loads(DATA['claim']['account_snapshot'])
            changed = json.dumps(dict(account, billing=dict(account['billing'], customerId='customer')),
                separators=(',', ':'))
            try:
                account = D1AccountTransactions(self.env.DB)
                await account.stage_account_write(owner_id='owner', expected_revision=1,
                    next_account_json=changed, now=DATA['claim']['now'])
                await account.reconcile_pending_for_owner(owner_id='owner',
                    now=DATA['claim']['now'] + 1)
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'refresh-reconciled':
            try:
                await D1AccountTransactions(self.env.DB).refresh_account_entitlement(
                    owner_id='owner', expected_revision=3, now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'reserve-first':
            try:
                await D1AnalysisTransactions(self.env.DB).reserve_first_processing_unit(
                    owner_id='owner', charge_key='probe-second', reservation_id='probe-second-reservation',
                    module='ci', now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name in {'enqueue-first', 'enqueue-denied'}:
            try:
                result = await D1AnalysisTransactions(self.env.DB).enqueue_first_analysis_job(
                    job_id='job-third', logical_key='analyze_source:third', source_id='3',
                    context_id='repo:repo:pr', reservation_id='probe-second-reservation',
                    trigger='scheduled_discovery', now=DATA['claim']['now'],
                    global_active_limit=2, owner_active_limit=1 if name == 'enqueue-denied' else 2)
                return Response.json(result)
            except Exception:
                return Response.json({'error': 'rejected'}, status=409)
        elif name == 'reserve-charge':
            try:
                result = await D1AnalysisTransactions(self.env.DB).reserve_processing_unit(
                    owner_id='owner', charge_key='charge', reservation_id='reopened-reservation',
                    module='pr', now=DATA['claim']['now'])
                return Response.json({'committed': True, 'reservation': result})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'schedule-enable':
            commands = [("UPDATE probe_server_schedule_enabled SET enabled=1 WHERE id=1", ())]
        elif name == 'creem-compose':
            try:
                length = request.headers.get('content-length')
                if not length or not length.isdigit() or int(length) > 65536:
                    return Response.json({'committed': False}, status=413)
                body = await request.bytes()
                raw = body if isinstance(body, bytes) else body.to_bytes()
                result = await accept_signed_creem_webhook(binding=self.env.DB,
                    raw_body=raw, signature=request.headers.get('creem-signature'),
                    secret='synthetic-webhook-secret',
                    configured_products={'pro': ('synthetic-pro',),
                        'max': ('synthetic-max',)}, now=DATA['claim']['now'])
                return Response.json(result)
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name in {'webhook-receipt', 'webhook-bad', 'webhook-conflict'}:
            secret = 'synthetic-webhook-secret'
            try:
                length = request.headers.get('content-length')
                if not length or not length.isdigit() or int(length) > 65536:
                    return Response.json({'committed': False}, status=413)
                body = await request.bytes()
                raw = body if isinstance(body, bytes) else body.to_bytes()
                if len(raw) > 65536:
                    return Response.json({'committed': False}, status=413)
                await D1WebhookReceipts(self.env.DB).record_signed_creem_event(
                    raw_body=raw, signature=request.headers.get('creem-signature'),
                    secret=secret, normalize_event=lambda event:
                        creem_event_rules.billing_update_from_creem_event(
                            event, {'pro': (), 'max': ()}),
                    now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'webhook-apply':
            try:
                await D1AccountTransactions(self.env.DB).settle_webhook_receipt(
                    receipt_event_id='evt-local', owner_id='owner', now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name in {'retry-claim', 'terminal-claim'}:
            retry = name == 'retry-claim'
            try:
                await D1AnalysisTransactions(self.env.DB).record_claim_failure(
                    job_id=DATA['claim']['job_id'],
                    token='claim-fixture' if retry else 'claim-after-retry',
                    now=DATA['claim']['now'] + (1 if retry else 41),
                    retryable=retry, next_attempt_at=DATA['claim']['now'] + 40 if retry else None)
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'claim-after-retry':
            args = DATA['claim']
            try:
                await D1AnalysisTransactions(self.env.DB).claim(
                    job_id=args['job_id'], token='claim-after-retry', now=args['now'] + 40,
                    global_monthly_limit=args['global_monthly_limit'],
                    owner_rolling_limit=args['owner_rolling_limit'],
                    global_rolling_limit=args['global_rolling_limit'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'edit-parent':
            commands = [("UPDATE source_records SET source_revision=source_revision+1 WHERE source_id='2'", ())]
        elif name == 'stale-analysis-source':
            commands = [("UPDATE source_records SET source_revision=source_revision+1 WHERE source_id='1'", ())]
        elif name == 'old-analysis-cycle':
            commands = [("UPDATE account_entitlement_authority SET period='new-cycle' WHERE owner_id='owner'", ())]
        elif name == 'expired-third-attempt':
            commands = [("""UPDATE background_jobs SET state='running',attempt=3,
                claim_token='last-attempt',claimed_until=?""", (DATA['claim']['now'] - 1,))]
        elif name == 'change-account':
            commands = [("UPDATE app_state SET payload='{}' WHERE name='users'", ())]
        elif name == 'break-reservation':
            commands = [("UPDATE processing_usage_buckets SET reserved=0", ())]
        else:
            return Response('Not found', status=404)
        try:
            await execute_d1_batch(self.env.DB, commands)
            return Response.json({'committed': True})
        except Exception:
            return Response.json({'committed': False}, status=409)

    async def attempt(self, request, url):
        """Finite two-scope admission probe; the clock is deterministic UTC."""
        name = url.path.removeprefix("/attempt/")
        if request.method == "GET" and name == "state":
            clock = await self.env.DB.prepare(
                "SELECT now, strftime('%Y-%m',now,'unixepoch') AS period FROM probe_attempt_clock WHERE id=1"
            ).first()
            counters = await self.env.DB.prepare("SELECT scope,period,used FROM probe_attempt_counter").all()
            events = await self.env.DB.prepare("SELECT owner,at,period FROM probe_attempt_event ORDER BY at,id").all()
            return Response.json({"now": clock["now"], "period": clock["period"],
                "counters": {row["scope"] + ":" + row["period"]: row["used"] for row in counters.results},
                "attempts": events.results})
        if request.method != "POST":
            return Response("Not found", status=404)
        if name == "reset":
            await self.batch(ATTEMPT_SCHEMA)
            await self.batch(["DELETE FROM probe_attempt_counter", "DELETE FROM probe_attempt_event",
                "DELETE FROM probe_attempt_guard", "DELETE FROM probe_attempt_clock",
                "INSERT INTO probe_attempt_clock VALUES(1,1790812785)"])
            return Response.json({"reset": True})
        if name in {"advance30", "advance31", "advance61"}:
            seconds = {"advance30": 30, "advance31": 31, "advance61": 61}[name]
            await self.env.DB.prepare("UPDATE probe_attempt_clock SET now=now+? WHERE id=1").bind(seconds).run()
            return Response.json({"advanced": seconds})
        if name not in {"a", "b"}:
            return Response("Not found", status=404)
        token = parse_qs(url.query).get("token", [""])[0]
        if not re.fullmatch(r"[a-f0-9]{32}", token):
            return Response("Invalid probe token", status=400)
        global_counter = """UPDATE probe_attempt_counter SET used=used+1
            WHERE scope='global' AND period=""" + ATTEMPT_PERIOD + """ AND used<3
            AND (SELECT COUNT(*) FROM probe_attempt_event
                 WHERE at>(SELECT now-60 FROM probe_attempt_clock))<2"""
        owner_counter = """UPDATE probe_attempt_counter SET used=used+1
            WHERE scope=? AND period=""" + ATTEMPT_PERIOD + """ AND used<2
            AND (SELECT COUNT(*) FROM probe_attempt_event
                 WHERE owner=? AND at>(SELECT now-60 FROM probe_attempt_clock))<1"""
        statements = [
            self.env.DB.prepare("INSERT INTO probe_attempt_counter SELECT 'global'," + ATTEMPT_PERIOD + ",0 ON CONFLICT DO NOTHING"),
            self.env.DB.prepare("INSERT INTO probe_attempt_counter SELECT ?," + ATTEMPT_PERIOD + ",0 ON CONFLICT DO NOTHING").bind(name),
            self.env.DB.prepare(global_counter),
            self.env.DB.prepare("INSERT INTO probe_attempt_guard VALUES(changes())"),
            self.env.DB.prepare(owner_counter).bind(name, name),
            self.env.DB.prepare("INSERT INTO probe_attempt_guard VALUES(changes())"),
            self.env.DB.prepare("INSERT INTO probe_attempt_event SELECT ?,?,now," + ATTEMPT_PERIOD + " FROM probe_attempt_clock WHERE id=1").bind(token, name),
        ]
        try:
            await self.env.DB.batch(statements)
            return Response.json({"admitted": True})
        except Exception:
            return Response.json({"admitted": False}, status=409)

    async def domain(self, request, url):
        """Finite local protocol probe with a deterministic clock; not a Store adapter."""
        name = url.path.removeprefix("/domain/")
        if request.method == "GET" and name == "state":
            return Response.json(await self.env.DB.prepare(DOMAIN_STATE).all())
        if request.method != "POST":
            return Response("Not found", status=404)
        token = parse_qs(url.query).get("token", [""])[0]
        guard = ("INSERT INTO probe_domain_guard VALUES(changes())", ())
        commands = {
            "advance": [("UPDATE probe_domain_clock SET now=now+60 WHERE id=1", ())],
            "break-reservation": [("UPDATE probe_domain_usage SET reserved=0 WHERE id=1", ())],
            "claim": [("""UPDATE probe_domain_job SET state='running', token=?,
                lease=(SELECT now+120 FROM probe_domain_clock), attempt=attempt+1
                WHERE id=1 AND attempt<3 AND next<=(SELECT now FROM probe_domain_clock)
                AND (state IN ('queued','retry_wait') OR (state='running' AND lease<=(SELECT now FROM probe_domain_clock)))
                AND auth=(SELECT auth FROM probe_domain_clock) AND config=(SELECT config FROM probe_domain_clock)
                AND (SELECT accessible FROM probe_domain_clock)=1""", (token,)), guard],
            "publish": [
                ("UPDATE probe_domain_job SET state='succeeded' WHERE " + DOMAIN_FENCE, (token,)), guard,
                ("INSERT INTO probe_domain_result VALUES(1,'synthetic assessment')", ()),
                ("UPDATE probe_domain_usage SET reserved=reserved-1, used=used+1 WHERE id=1 AND reserved=1", ()), guard,
            ],
            "retry": [("UPDATE probe_domain_job SET state='retry_wait', token=NULL, lease=0, next=(SELECT now+60 FROM probe_domain_clock) WHERE " + DOMAIN_FENCE, (token,)), guard],
        }
        if name == "reset":
            await self.batch(DOMAIN_SCHEMA)
            commands[name] = [(sql, ()) for sql in (
                "DELETE FROM probe_domain_job", "DELETE FROM probe_domain_clock", "DELETE FROM probe_domain_usage",
                "DELETE FROM probe_domain_result", "DELETE FROM probe_domain_guard",
                "INSERT INTO probe_domain_clock VALUES(1,100,1,1,1)",
                "INSERT INTO probe_domain_job VALUES(1,'queued',NULL,0,0,1,1,0)",
                "INSERT INTO probe_domain_usage VALUES(1,1,0)",
            )]
        elif name in {"revoke", "configure"}:
            change = "auth=auth+1, accessible=0" if name == "revoke" else "config=config+1"
            commands[name] = [(sql, ()) for sql in (
                "UPDATE probe_domain_clock SET " + change + " WHERE id=1",
                "UPDATE probe_domain_job SET state='cancelled', token=NULL WHERE id=1 AND state IN ('queued','running','retry_wait')",
                "UPDATE probe_domain_usage SET reserved=0 WHERE id=1",
            )]
        if name not in commands:
            return Response("Not found", status=404)
        if name in {"claim", "publish", "retry"} and not re.fullmatch(r"[a-f0-9]{32}", token):
            return Response("Invalid probe token", status=400)
        try:
            await self.env.DB.batch([self.env.DB.prepare(sql).bind(*params) if params else self.env.DB.prepare(sql)
                                     for sql, params in commands[name]])
            return Response.json({"applied": True})
        except Exception:
            return Response.json({"applied": False}, status=409)

    async def batch(self, statements):
        return await self.env.DB.batch([self.env.DB.prepare(sql) for sql in statements])

    async def scheduled(self, controller, env, ctx):
        await self.env.DB.prepare(SCHEMA[3]).run()
        await self.env.DB.prepare("INSERT INTO probe_clock VALUES (1,1) ON CONFLICT(id) DO UPDATE SET ticks=ticks+1").run()
        try:
            enabled = await self.env.DB.prepare("SELECT enabled FROM probe_server_schedule_enabled WHERE id=1").first()
        except Exception:
            return
        if enabled and enabled['enabled']:
            from server_fixture import DATA
            from pullwise_server.cloudflare_analysis_adapter import D1AnalysisTransactions
            args = DATA['claim']
            await D1AnalysisTransactions(self.env.DB).claim_due_analysis(
                now=args['now'], token='scheduled-claim',
                global_monthly_limit=args['global_monthly_limit'],
                owner_rolling_limit=args['owner_rolling_limit'],
                global_rolling_limit=args['global_rolling_limit'])
            await self.env.DB.prepare("UPDATE probe_server_schedule_enabled SET enabled=0 WHERE id=1").run()

    async def fetch(self, request):
        url = urlsplit(request.url)
        if url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return Response("Local probe only", status=403)
        if url.path.startswith("/server-map/"):
            return await self.server_mapping(request, url)
        if url.path.startswith("/domain/"):
            return await self.domain(request, url)
        if url.path.startswith("/attempt/"):
            return await self.attempt(request, url)
        if request.method == "POST" and url.path == "/sdk-network":
            import logging
            import httpx2
            from typesafe_sdk import Choice, RetryPolicy, TypeSafeClient
            from bounded_transport import BoundedTransport
            logging.getLogger("typesafe_sdk").disabled = True
            started = time.monotonic()
            try:
                with TypeSafeClient(api_key="local-fixture-not-a-credential",
                        base_url="http://127.0.0.1:8795", model="jev-1.13.0",
                        retry=RetryPolicy(max_retries=0), timeout=0.1,
                        transport=BoundedTransport(deadline_seconds=2, max_bytes=1024 * 1024)) as client:
                    result = client.system_one(state="Local fixture", questions={
                        "q": Choice(instructions="Fixture only", criteria={"present": "Present", "absent": "Absent"})
                    })
                    outcome = result.model
            except Exception as error:
                outcome = type(error).__name__
            return Response.json({"outcome": outcome, "elapsed": round(time.monotonic()-started, 3), "modelCalls": 0})
        if request.method == "GET" and url.path == "/":
            imports = {}
            for name in ("typesafe_sdk", "httpx2", "pydantic"):
                try:
                    importlib.import_module(name)
                    imports[name] = "ok"
                except Exception as error:
                    imports[name] = type(error).__name__
            return Response.json({"python": sys.version, "imports": imports, "modelCalls": 0})
        if request.method == "POST" and url.path == "/reset":
            await self.batch(SCHEMA)
            await self.batch([
                "DELETE FROM probe_budget", "DELETE FROM probe_guard", "DELETE FROM probe_attempt",
                "INSERT INTO probe_budget VALUES ('global',0),('a',0),('b',0)",
            ])
            return Response.json({"reset": True})
        if request.method == "POST" and url.path in {"/admit/a", "/admit/b"}:
            owner = url.path[-1]  # Fixed enum, never arbitrary SQL.
            try:
                await self.batch([
                    "UPDATE probe_budget SET used=used+1 WHERE scope='global' AND used<1",
                    "INSERT INTO probe_guard VALUES (changes())",
                    f"UPDATE probe_budget SET used=used+1 WHERE scope='{owner}' AND used<1",
                    "INSERT INTO probe_guard VALUES (changes())",
                    f"INSERT INTO probe_attempt VALUES ('attempt-{owner}','{owner}')",
                ])
                return Response.json({"admitted": True, "owner": owner})
            except Exception:
                return Response.json({"admitted": False, "owner": owner}, status=409)
        if request.method == "POST" and url.path == "/rollback":
            try:
                await self.batch([
                    "UPDATE probe_budget SET used=1 WHERE scope='global'",
                    "INSERT INTO probe_guard VALUES (0)",
                ])
                return Response.json({"rollback": False}, status=500)
            except Exception:
                return Response.json({"rollback": True})
        if request.method == "GET" and url.path == "/state":
            # D1's JSON serialization is tested by the HTTP driver, not assumed.
            return Response.json(await self.env.DB.prepare("SELECT scope,used FROM probe_budget ORDER BY scope").all())
        if request.method == "GET" and url.path == "/clock":
            return Response.json(await self.env.DB.prepare("SELECT ticks FROM probe_clock WHERE id=1").all())
        return Response("Not found", status=404)
