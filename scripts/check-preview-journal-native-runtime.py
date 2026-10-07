"""Finite local Python/SQLite-DO checks; optional local D1 cardinality probe.

Requires the official Workerd runtime bundle cache prepared by the Projects
runner. All journal policy methods are canonical source, with no mocked SQL.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import time
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
ENTRY = r'''
from workers import WorkerEntrypoint, DurableObject, Response
from pullwise_server.cloudflare_validation_budget import BudgetJournal, BudgetError
from pullwise_server.cloudflare_preview_rate import PreviewRateLimiter, PreviewRateLimit, request_channel
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, initial_data, SCHEMA_VERSION, SCHEMA_FINGERPRINT, migrate_product_state_records
from pullwise_server.cloudflare_native_d1 import NativeD1
from pullwise_server.cloudflare_validation_budget import _field


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if getattr(self.env, "PULLWISE_MODE", "") != "local":
            return Response.json({"error": "LOCAL_ONLY"}, status=503)
        name = str(request.url).rsplit("/", 1)[-1]
        if name not in {"product", "bounded", "legacy", "frequency", "cardinality"}:
            return Response.json({"error": "NOT_FOUND"}, status=404)
        return await self.env.FIXTURE_JOURNAL.get(self.env.FIXTURE_JOURNAL.idFromName(name)).fetch(request)


class FixtureJournal(DurableObject):
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env

    async def fetch(self, request):
        name = str(request.url).rsplit("/", 1)[-1]
        sql = self.ctx.storage.sql
        if name == "cardinality":
            journal = BudgetJournal(sql, preview_product=True, product_operations=True)
            ticket = journal.begin_product(now=1000)
            # The local D1 has just applied all canonical migrations.
            # Model the existing initialized journal without the new marker.
            journal.save_product_state(ticket, initial_data(), now=1001, initialized=True)
            state = journal.snapshot()
            state.update(schema_version=SCHEMA_VERSION, schema_fingerprint=SCHEMA_FINGERPRINT)
            state.pop("product_data_verified", None)
            journal._save(state)
            class ObservedD1(NativeD1):
                def __init__(self, binding):
                    super().__init__(binding)
                    self.results, self.batches, self.meter, self.writing = [], 0, None, False
                    self.marker_false_at_write = False
                async def batch(self, statements):
                    if self.writing and len(statements) == 1:
                        self.marker_false_at_write = self.meter.cardinality_verified is False
                        assert self.marker_false_at_write
                    self.batches += 1
                    results = await super().batch(statements)
                    for result in results:
                        meta = _field(result, "meta")
                        self.results.append({"rowsRead": _field(meta, "rows_read"),
                            "rowsWritten": _field(meta, "rows_written"),
                            "attempts": _field(meta, "total_attempts")})
                    return results
            native = ObservedD1(self.env.DB)
            journal.finish(ticket, now=1003)
            # The canonical fixed cutover runs once before ordinary record SQL.
            # Keep its native usage in the same cumulative journal/evidence.
            await migrate_product_state_records(native, journal, clock=lambda: 1004)
            assert journal.snapshot()["state_storage_version"] == 1
            migration_batches = native.batches
            state = journal.snapshot()
            state.pop("product_data_verified", None)
            journal._save(state)
            ticket = journal.begin_product(now=1010)
            meter = ProductMeteredD1(native, journal, ticket, clock=lambda: 1011)
            assert not meter.cardinality_verified
            await meter.ensure_cardinality()
            assert native.batches == migration_batches + 1 and journal.snapshot()["product_data_verified"] is True
            assert meter.data["rows"]["app_state"] == 4
            journal.finish(ticket, now=1012)
            journal = BudgetJournal(sql, preview_product=True, product_operations=True)
            ticket = journal.begin_product(now=2000)
            meter = ProductMeteredD1(native, journal, ticket, clock=lambda: 2001)
            native.meter = meter
            assert meter.cardinality_verified
            await meter.ensure_cardinality()
            assert native.batches == migration_batches + 1
            # An ordinary read uses its own query without a full-table refresh.
            row = await meter.prepare("SELECT COUNT(*) AS count FROM expense_categories").first()
            assert row["count"] == 0 and native.batches == migration_batches + 2
            native.writing = True
            await meter.batch([meter.prepare("""INSERT INTO expense_categories
                (id,owner_id,name,revision,created_at,updated_at) VALUES(?,?,?,1,?,?)""").bind(
                    "native-category", "synthetic-owner", "Native category", "local", "local")])
            native.writing = False
            assert native.marker_false_at_write and native.batches == migration_batches + 4
            assert meter.cardinality_verified and journal.snapshot()["product_data_verified"] is True
            assert meter.data["rows"]["expense_categories"] == 1
            await meter.ensure_cardinality()
            assert native.batches == migration_batches + 4
            journal.finish(ticket, now=2002)
            journal = BudgetJournal(sql, preview_product=True, product_operations=True)
            ticket = journal.begin_product(now=3000)
            meter = ProductMeteredD1(native, journal, ticket, clock=lambda: 3001)
            await meter.ensure_cardinality()
            assert native.batches == migration_batches + 4 and meter.cardinality_verified
            assert meter.data["rows"]["expense_categories"] == 1
            journal.finish(ticket, now=3002)
            final = journal.snapshot()
            assert final["stopped"] is None and final["active"] is None
            return Response.json({"passed": True, "initialMarkerlessRefresh": True,
                "firstRefreshPersisted": True, "restartSkipsFullScan": True,
                "ordinaryReadNoRefresh": True, "mutationFlagFalseAtDispatch": True,
                "mutationRefreshPersisted": True, "verifiedPostWriteRestart": True,
                "stateRecordStorageVersion": final["state_storage_version"],
                "canonicalCutoverNativeBatches": migration_batches,
                "nativeBatches": native.batches, "nativeStatements": len(native.results),
                "nativeRowsRead": sum(item["rowsRead"] for item in native.results),
                "nativeRowsWritten": sum(item["rowsWritten"] for item in native.results),
                "nativeAttempts": [item["attempts"] for item in native.results],
                "journalReservedRead": final["reserved_read"],
                "journalActualRead": final["actual_read"],
                "journalReservedWritten": final["reserved_written"],
                "journalActualWritten": final["actual_written"], "stopped": final["stopped"]})
        if name == "frequency":
            rate = PreviewRateLimiter(sql)
            attempts = 0
            def admitted(callback, times):
                nonlocal attempts
                for _ in range(times):
                    callback()
                    attempts += 1
            def rejected(callback):
                nonlocal attempts
                attempts += 1
                try:
                    callback()
                except PreviewRateLimit as error:
                    assert 1 <= error.retry_after <= 120
                    return
                raise AssertionError("Rate saturation was not rejected")
            admitted(lambda: rate.actor("synthetic-actor", channel="write", now=1000), 60)
            rejected(lambda: rate.actor("synthetic-actor", channel="write", now=1000))
            restarted = PreviewRateLimiter(sql)
            rejected(lambda: restarted.actor("synthetic-actor", channel="write", now=1000))
            admitted(lambda: rate.actor("other-synthetic-actor", channel="write", now=1000), 1)
            admitted(lambda: rate.actor("synthetic-actor", channel="read", now=1000), 120)
            rejected(lambda: rate.actor("synthetic-actor", channel="read", now=1000))
            admitted(lambda: rate.actor("synthetic-actor", channel="security", now=1000), 120)
            rejected(lambda: rate.actor("synthetic-actor", channel="security", now=1000))
            ip = {"cf-connecting-ip": "192.0.2.1"}
            admitted(lambda: rate.ingress(ip, method="GET", path="/api/v1/projects", now=1000), 600)
            rejected(lambda: rate.ingress(ip, method="GET", path="/api/v1/projects", now=1000))
            credential = {"cf-connecting-ip": "192.0.2.2", "cookie": "pw_session=synthetic-session"}
            admitted(lambda: rate.ingress(credential, method="GET", path="/api/v1/projects", now=1000), 240)
            rejected(lambda: rate.ingress(credential, method="GET", path="/api/v1/projects", now=1000))
            oauth = {"cf-connecting-ip": "192.0.2.3"}
            admitted(lambda: rate.ingress(oauth, method="GET", path="/auth/github/authorize", now=1000), 10)
            rejected(lambda: rate.ingress(oauth, method="GET", path="/auth/github/authorize", now=1000))
            assert request_channel("DELETE", "/api-keys/local-key") == "security"
            assert request_channel("PATCH", "/api/v1/expenses/local-expense") == "write"
            clock = sql.exec("SELECT minute,subjects FROM preview_rate_clock WHERE id=1").one()
            rows = sql.exec("SELECT subject,calls FROM preview_request_rates").toArray()
            assert len(rows) == clock.subjects and len(rows) < 20
            assert all(len(row.subject) == 64 and all(char in "0123456789abcdef" for char in row.subject) for row in rows)
            admitted(lambda: rate.actor("synthetic-actor", channel="write", now=1020), 1)
            admitted(lambda: rate.actor("synthetic-actor", channel="write", now=900), 1)
            assert sql.exec("SELECT minute FROM preview_rate_clock WHERE id=1").one().minute == 17
            admitted(lambda: rate.actor("expiry-check", channel="read", now=1200), 1)
            final = sql.exec("SELECT subjects FROM preview_rate_clock WHERE id=1").one()
            assert final.subjects == 1 and len(sql.exec("SELECT subject FROM preview_request_rates").toArray()) == 1
            return Response.json({"passed": True, "directRateAdmissions": attempts,
                "ip600": True, "credential240": True, "oauth10": True,
                "actorWrite60": True, "actorRead120": True, "actorSecurity120": True,
                "otherActorAllowed": True, "restartKeepsSaturation": True,
                "nextMinuteAllowed": True, "lateRequestsCannotResetClock": True,
                "hashedStorageOnly": True, "expiredSubjectCount": final.subjects,
                "emergencyRevocationSeparated": True, "d1Operations": 0})
        if name == "product":
            journal = BudgetJournal(sql, preview_product=True, product_operations=True)
            for index in range(205):
                now = 1000 + index * 5
                ticket = journal.begin_product(now=now)
                journal.reserve_operation(ticket, reads=100001, writes=1001, now=now + 1)
                if index == 0:
                    journal.save_product_state(ticket, {"fixture": 1}, now=now + 1, initialized=True)
                journal.record(ticket, 1, 2, 1)
                journal.settle_product_reads(ticket, 1, 100001, now=now + 2)
                journal.finish(ticket, now=now + 3)
            before = journal.evidence_snapshot()
            assert before["requests"] == 205 and before["stopped"] is None
            assert before["reserved_read"] == before["actual_read"] == 410
            assert before["reserved_written"] == 205205 and before["actual_written"] == 205
            assert before["product_evidence_rows"] == 205 and len(before["product_evidence"]) == 128
            assert before["product_evidence_truncated"] is True and before["evidence"] == []
            assert before["schema_ready"] is True and before["product_data"] == {"fixture": 1}
            ticket = journal.begin_product(now=3000)
            journal.reserve_operation(ticket, reads=5, writes=0, now=3001)
            journal.record(ticket, 1, 1, 0)
            journal.settle_product_reads(ticket, 1, 5, now=3002)
            journal.finish_accounted_product_failure(ticket, now=3003, timeout=True)
            closed = journal.snapshot()
            assert closed["active"] is None and closed["stopped"] is None and closed["product_closed_failures"] == 1
            journal.stop("MANUAL_STOP")
            restored = BudgetJournal(sql, preview_product=True, product_operations=True)
            try:
                restored.begin_product(now=4000)
            except BudgetError as error:
                assert str(error) == "MANUAL_STOP"
            else:
                raise AssertionError("Restart cleared manual stop")
            return Response.json({"passed": True, "requests": closed["requests"],
                "reservedRead": closed["reserved_read"], "actualRead": closed["actual_read"],
                "reservedWritten": closed["reserved_written"], "actualWritten": closed["actual_written"],
                "evidenceRows": closed["product_evidence_rows"], "boundedEvidenceResponse": 128,
                "legacyEvidenceRetained": True, "schemaRetained": True,
                "accountedTimeoutClosed": True, "manualStopSurvivesRestart": True})
        journal = BudgetJournal(sql, preview_product=True)
        if name == "legacy":
            ticket = journal.begin_product(now=1000)
            journal.reserve_operation(ticket, reads=2, writes=1, now=1001)
            journal.save_product_state(ticket, {"fixture": 2}, now=1001, initialized=True)
            journal.record(ticket, 1, 2, 1)
            journal.finish(ticket, now=1002)
        ticket = journal.begin_product(now=2000)
        try:
            journal.reserve_operation(ticket, reads=100001, writes=1001, now=2001)
        except BudgetError as error:
            assert str(error) == "BUDGET_EXHAUSTED"
        else:
            raise AssertionError("Generic finite journal lost its caps")
        original = journal.snapshot()
        if name == "bounded":
            return Response.json({"passed": True, "genericBudgetExhaustionRetained": original["stopped"] == "BUDGET_EXHAUSTED"})
        transitioned = BudgetJournal(sql, preview_product=True, product_operations=True)
        current = transitioned.snapshot()
        assert current["stopped"] is None and current["active"] is None
        for field in ("requests", "reserved_read", "reserved_written", "actual_read", "actual_written", "evidence", "product_data", "schema_ready"):
            assert current[field] == original[field]
        assert current["preview_product_operation_policy"]["previous_stop"] == "BUDGET_EXHAUSTED"
        next_ticket = transitioned.begin_product(now=3000)
        transitioned.reserve_operation(next_ticket, reads=100001, writes=1001, now=3001)
        transitioned.record(next_ticket, 1, 2, 1)
        transitioned.settle_product_reads(next_ticket, 1, 100001, now=3002)
        transitioned.finish(next_ticket, now=3003)
        final = transitioned.snapshot()
        assert final["evidence"] == original["evidence"] and final["reserved_written"] == 1002
        return Response.json({"passed": True, "accountedLegacyBudgetStopMigrated": True,
            "legacyEvidenceRetained": True, "noCounterReset": True,
            "reservedWritten": final["reserved_written"], "actualWritten": final["actual_written"]})
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8897)
    parser.add_argument("--frequency-only", action="store_true", help="Run only the newly added native frequency case")
    parser.add_argument("--cardinality-only", action="store_true", help="One native DO+D1 marker/restart/write-refresh case")
    parser.add_argument("--journal-only", action="store_true", help="Run only the three journal accounting and policy cases")
    args = parser.parse_args()
    directory = args.run_dir.resolve()
    if not directory.is_relative_to(Path("/workspace")):
        raise SystemExit("Local run directory must be in /workspace")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "src").mkdir()
    (directory / "src/entry.py").write_text(ENTRY, encoding="utf-8")
    shutil.copytree(ROOT / "pullwise_server", directory / "src/pullwise_server",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(WORKER / "python_modules", directory / "python_modules")
    config = {"name": "pullwise-journal-acceptance-local", "main": "src/entry.py",
        "compatibility_date": "2026-09-23", "compatibility_flags": ["python_workers"],
        "workers_dev": False, "preview_urls": False, "routes": [],
        "vars": {"PULLWISE_MODE": "local"},
        "durable_objects": {"bindings": [{"name": "FIXTURE_JOURNAL", "class_name": "FixtureJournal"}]},
        "migrations": [{"tag": "local-fixture-v1", "new_sqlite_classes": ["FixtureJournal"]}]}
    if args.cardinality_only:
        shutil.copytree(WORKER / "migrations", directory / "migrations")
        config["d1_databases"] = [{"binding": "DB", "database_name": "pullwise-cardinality-local-only",
            "database_id": "00000000-0000-0000-0000-000000000002", "remote": False,
            "migrations_dir": "migrations"}]
    config_path = directory / "wrangler.jsonc"
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    wrapper = directory / "workerd-local-cache.sh"
    wrapper.write_text("#!/bin/sh\nexec " + shlex.quote(str(WORKER / "node_modules/@cloudflare/workerd-linux-64/bin/workerd"))
        + ' "$@" --pyodide-bundle-disk-cache-dir=/workspace/.cache/pullwise-pyodide\n')
    wrapper.chmod(0o700)
    env = {**os.environ, "XDG_CACHE_HOME": "/workspace/.cache", "XDG_CONFIG_HOME": "/workspace/.config",
        "UV_CACHE_DIR": "/workspace/.cache/uv", "UV_PYTHON_INSTALL_DIR": "/workspace/.python",
        "WRANGLER_SEND_METRICS": "false", "MINIFLARE_WORKERD_PATH": str(wrapper),
        "WRANGLER_LOG_PATH": str(directory / "wrangler-debug.log")}
    if args.cardinality_only:
        subprocess.run(["node", str(WORKER / "node_modules/wrangler/wrangler-dist/cli.js"),
            "d1", "migrations", "apply", "DB", "--local", "--config", str(config_path),
            "--persist-to", str(directory / "state")], cwd=WORKER, env=env,
            capture_output=True, text=True, timeout=90, check=True)
    process, results = None, []
    log_path = directory / "runtime.log"
    evidence = {"passed": False, "localOnly": True, "nativePythonFFI": True,
        "nativeDurableObjectSQLite": True, "remoteRequests": 0, "remoteD1Operations": 0,
        "realProviderRequests": 0, "clientRetries": 0,
        "canonicalJournalSha256": hashlib.sha256((ROOT / "pullwise_server/cloudflare_validation_budget.py").read_bytes()).hexdigest()}
    try:
        with log_path.open("w") as log:
            process = subprocess.Popen([str(WORKER / ".venv/bin/pywrangler"), "dev", "--local",
                "--config", str(config_path), "--ip", "127.0.0.1", "--port", str(args.port),
                "--inspector-port", str(args.port + 1000), "--persist-to", str(directory / "state")],
                cwd=WORKER, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError("Native DO startup failed")
                try:
                    with socket.create_connection(("127.0.0.1", args.port), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                raise AssertionError("Native DO startup timeout")
            opener = build_opener(ProxyHandler({}))
            cases = ("cardinality",) if args.cardinality_only else ("frequency",) if args.frequency_only else ("product", "bounded", "legacy") if args.journal_only else ("product", "bounded", "legacy", "frequency")
            for case in cases:
                with opener.open(Request(f"http://127.0.0.1:{args.port}/" + case, method="POST"), timeout=60) as response:
                    result = json.loads(response.read())
                    assert response.status == 200 and result["passed"], result
                    results.append({"case": case, **result})
            evidence.update({"passed": True, "cases": results})
    except Exception as error:
        evidence["failure"] = str(error)
        raise
    finally:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        evidence.update({"localHttpRequestsCompleted": len(results), "localHttpCap": 1 if args.frequency_only or args.cardinality_only else 3 if args.journal_only else 4,
            "runtimeStopped": process is None or process.poll() is not None})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence))


if __name__ == "__main__":
    main()
