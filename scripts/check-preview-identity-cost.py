#!/usr/bin/env python3
"""Finite local SQL replay from real identity handlers and synthetic providers.

This prepares an isolated local-only fixture, never launches Wrangler or calls a
provider. Native measurements are evidence, not remote worst-case row bounds.
"""
import argparse
import asyncio
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def split_sql(source):
    statements, pending = [], ""
    for char in source:
        pending += char
        if char == ";" and sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    remainder = "\n".join(line.split("--", 1)[0] for line in pending.splitlines()).strip()
    if remainder:
        raise ValueError("incomplete migration statement")
    return statements


def collect_trace(directory):
    from test_cloudflare_github_identity_http import Store, D1ShapedSQLite, GitHubStub
    from pullwise_server.cloudflare_github_identity_http import handle_identity_request

    trace = []
    store = Store.__new__(Store)
    store.path = directory / "identity-reference.sqlite"
    if store.path.exists():
        raise ValueError("reference database already exists; do not reuse a fixture")
    migrations = []
    with store.connect() as connection:
        for path in sorted((ROOT / "cloudflare/server/migrations").glob("*.sql")):
            source = path.read_text(encoding="utf-8")
            migrations.append({"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
            for sql in split_sql(source):
                connection.execute(sql)
                trace.append({"case": "schema", "statements": [{"sql": sql, "params": []}]})
        tables = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        indexes = connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index'").fetchone()[0]

    class RecordingStatement:
        def __init__(self, binding, sql, params=()):
            self.binding, self.sql, self.params = binding, sql, params

        def bind(self, *params):
            return RecordingStatement(self.binding, self.sql, params)

        async def first(self):
            self.binding.record([self])
            return await self.binding.raw.prepare(self.sql).bind(*self.params).first()

    class RecordingBinding:
        def __init__(self):
            self.raw = D1ShapedSQLite(store)
            self.case = None

        def prepare(self, sql):
            return RecordingStatement(self, sql)

        def record(self, statements):
            trace.append({"case": self.case, "statements": [
                {"sql": item.sql, "params": list(item.params)} for item in statements]})

        async def batch(self, statements):
            self.record(statements)
            return await self.raw.batch([
                self.raw.prepare(item.sql).bind(*item.params) for item in statements])

    binding, gateway = RecordingBinding(), GitHubStub()
    now = 1_800_000_000

    async def journey():
        async def call(case, path, *, params=None, headers=None, method="GET", expected=200):
            binding.case = case
            result = await handle_identity_request(binding=binding, gateway=gateway, now=now,
                method=method, path=path, params=params or {}, headers=headers or {},
                app_url="https://app.example.test",
                callback_url="https://app.example.test/api/auth/github/callback",
                cookie_same_site="None", trusted_origins={"https://app.example.test"})
            if result is None or result[0] != expected:
                raise ValueError("unexpected synthetic identity response")
            return result

        _, payload, _ = await call("login_authorize", "/auth/github/authorize")
        state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
        _, _, headers = await call("login_callback", "/auth/github/callback",
            params={"state": state, "code": "synthetic-code"}, expected=302)
        cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
        await call("session", "/auth/session", headers=cookie)
        await call("callback_replay", "/auth/github/callback",
            params={"state": state, "code": "synthetic-code"}, expected=400)
        _, payload, _ = await call("install_authorize", "/integrations/github/authorize", headers=cookie)
        state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
        await call("install_callback", "/integrations/github/callback", headers=cookie,
            params={"state": state, "installation_id": "501"}, expected=302)
        await call("repositories", "/repositories", headers=cookie)
        await call("sign_out", "/auth/sign-out", method="POST",
            headers={**cookie, "Origin": "https://app.example.test"})
        await call("signed_out_session", "/auth/session", headers=cookie)

    asyncio.run(journey())
    with store.connect() as connection:
        final = {name: connection.execute(
            "SELECT COUNT(*) FROM app_state WHERE name GLOB ?", (f"record:{name}:*",)).fetchone()[0]
            for name in ("users", "sessions", "githubStates")}
    return trace, {"migrations": migrations, "tables": tables, "indexes": indexes,
        "http_cases": 9, "sql_operations": len(trace), "remote_admissible": False,
        "final_state": final, "state_storage_version": 1, "provider_mode": "synthetic"}


def validate_evidence(trace, evidence):
    operations = evidence.get("operations")
    if evidence.get("complete") is not True or not isinstance(operations, list) or len(operations) != len(trace):
        raise ValueError("native evidence incomplete; stop without retry")
    reads = writes = 0
    for index, (expected, operation) in enumerate(zip(trace, operations)):
        meta = operation.get("meta")
        if (operation.get("index") != index or not isinstance(meta, list)
                or len(meta) != len(expected["statements"])):
            raise ValueError("native statement evidence incomplete")
        for item in meta:
            read, write = item.get("rows_read"), item.get("rows_written")
            if type(read) is not int or type(write) is not int or min(read, write) < 0:
                raise ValueError("native row counters missing or invalid")
            reads += read
            writes += write
    return {"rows_read": reads, "rows_written": writes}


WORKER = """
let started = false;
export default {
  async fetch(request, env) {
    if (request.method !== 'POST' || new URL(request.url).pathname !== '/run')
      return new Response(null, {status: 404});
    if (started) return Response.json({complete: false, reason: 'CASE_LIMIT'}, {status: 409});
    started = true;
    const operations = [];
    let reads = 0, writes = 0;
    for (const [index, operation] of trace.entries()) {
      try {
        const results = await env.DB.batch(operation.statements.map(s => env.DB.prepare(s.sql).bind(...s.params)));
        const meta = results.map(r => ({rows_read:r.meta?.rows_read, rows_written:r.meta?.rows_written}));
        operations.push({index, case:operation.case, meta});
        if (results.length !== operation.statements.length || results.some(r => r.success !== true) ||
            meta.some(m => !Number.isSafeInteger(m.rows_read) || m.rows_read < 0 ||
                           !Number.isSafeInteger(m.rows_written) || m.rows_written < 0))
          return Response.json({complete:false, reason:'METERING_MISSING', operations}, {status:503});
        reads += meta.reduce((n,m) => n+m.rows_read, 0);
        writes += meta.reduce((n,m) => n+m.rows_written, 0);
        if (reads > 10000 || writes > 1000)
          return Response.json({complete:false, reason:'LOCAL_OBSERVATION_LIMIT', operations}, {status:503});
      } catch (_) {
        return Response.json({complete:false, reason:'D1_OUTCOME_UNKNOWN', operations}, {status:503});
      }
    }
    return Response.json({complete:true, operations});
  }
};
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    directory = args.prepare_dir.resolve()
    runtime = ROOT.parent / ".agents/runtime"
    if not directory.is_relative_to(runtime.resolve()):
        parser.error("fixture must be inside the ignored workspace .agents/runtime directory")
    if args.evidence:
        trace = json.loads((directory / "trace.json").read_text(encoding="utf-8"))
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
        totals = validate_evidence(trace, evidence)
        print(json.dumps({"local_observed": totals, "remote_admissible": False}))
        return
    directory.mkdir(parents=True, exist_ok=False)
    trace, manifest = collect_trace(directory)
    (directory / "trace.json").write_text(json.dumps(trace), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (directory / "entry.js").write_text("const trace=" + json.dumps(trace) + ";\n" + WORKER, encoding="utf-8")
    config = {"name": "pullwise-identity-cost-local-only", "main": "entry.js",
        "compatibility_date": "2026-09-23", "workers_dev": False, "preview_urls": False,
        "routes": [], "d1_databases": [{"binding": "DB", "database_name": "identity-cost-local-only",
            "database_id": "00000000-0000-0000-0000-000000000029", "remote": False}]}
    (directory / "wrangler.jsonc").write_text(json.dumps(config, indent=2), encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
