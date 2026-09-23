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


class Default(WorkerEntrypoint):
    async def server_mapping(self, request, url):
        from server_fixture import DATA
        from pullwise_server.cloudflare_d1_batch import execute_d1_batch
        from pullwise_server.cloudflare_account_adapter import D1AccountTransactions
        from pullwise_server.cloudflare_analysis_adapter import D1AnalysisTransactions
        import server_mapping as mapping
        name = url.path.removeprefix('/server-map/')
        if request.method == 'GET' and name == 'state':
            return Response.json(await self.env.DB.prepare('''SELECT
                (SELECT used FROM processing_usage_buckets LIMIT 1) AS used,
                (SELECT reserved FROM processing_usage_buckets LIMIT 1) AS reserved,
                (SELECT limit_value FROM processing_usage_buckets LIMIT 1) AS limitValue,
                (SELECT COUNT(*) FROM processing_usage_ledger WHERE charge_key='probe-second') AS secondReservation,
                (SELECT json_extract(value,'$.githubAccessToken.__encrypted')='pullwise-state-secret-v1'
                    FROM app_state,json_each(payload) WHERE name='users' AND key='owner') AS encryptedToken,
                (SELECT COUNT(*) FROM provider_attempts) AS attempts,
                (SELECT COUNT(*) FROM assessments) AS results,
                (SELECT COUNT(*) FROM item_versions) AS versions,
                (SELECT state FROM background_jobs LIMIT 1) AS jobState,
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
        if name == 'reset':
            await self.env.DB.batch([self.env.DB.prepare(sql) for sql in DATA['schemas']]
                + [self.env.DB.prepare(sql) for sql, _ in mapping.schema()])
            commands = [('DELETE FROM ' + table, ()) for table in reversed(DATA['names'])] + DATA['inserts']
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
                await D1AccountTransactions(self.env.DB).stage_pending_billing_updates(
                    next_pending_json='[{"eventId":"later","customerId":"customer"}]',
                    now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'reconcile-pending':
            account = json.loads(DATA['claim']['account_snapshot'])
            changed = json.dumps(dict(account, billing=dict(account['billing'], customerId='customer')),
                separators=(',', ':'))
            try:
                await D1AccountTransactions(self.env.DB).stage_billing_reconciliation(
                    owner_id='owner', expected_revision=1, next_account_json=changed,
                    next_events_json='{"event_fixture":{"status":"processed"},"later":{"applied":true}}',
                    next_pending_json='[]', now=DATA['claim']['now'])
                return Response.json({'committed': True})
            except Exception:
                return Response.json({'committed': False}, status=409)
        elif name == 'refresh-reconciled':
            try:
                await D1AccountTransactions(self.env.DB).refresh_account_entitlement(
                    owner_id='owner', expected_revision=2, now=DATA['claim']['now'])
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
