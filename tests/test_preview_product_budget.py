import pytest


def test_point_mutations_include_indexes_and_guard_cleanup():
    from pullwise_server.cloudflare_preview_budget import sql_write_bound
    assert sql_write_bound("UPDATE expenses SET purpose=? WHERE id=? AND owner_id=?") == 9
    assert sql_write_bound("INSERT INTO expenses(id) VALUES(?)") == 5
    with pytest.raises(ValueError):
        sql_write_bound("UPDATE expenses SET purpose=?")
    with pytest.raises(ValueError):
        sql_write_bound("UPDATE expenses SET purpose=? WHERE id=? OR owner_id=?")
    with pytest.raises(ValueError):
        sql_write_bound("INSERT INTO expenses(id) VALUES(?),(?)")
    with pytest.raises(ValueError):
        sql_write_bound("INSERT INTO expenses SELECT * FROM expenses")
    with pytest.raises(ValueError):
        sql_write_bound("INSERT INTO expenses SELECT ? UNION ALL SELECT ?")
    with pytest.raises(ValueError):
        sql_write_bound("INSERT OR REPLACE INTO expenses(id) VALUES(?)")


def test_every_current_literal_update_has_a_unique_key_fence():
    import ast
    from pathlib import Path
    from pullwise_server.cloudflare_preview_budget import sql_write_bound
    count = 0
    for path in (Path(__file__).resolve().parents[1] / "pullwise_server").glob("cloudflare_*.py"):
        if path.stem in {"cloudflare_validation_budget", "cloudflare_preview_budget", "cloudflare_preview_schema"}:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sql = node.value.strip()
                if sql.upper().startswith("UPDATE ") and " SET " in " ".join(sql.upper().split()):
                    assert 0 <= sql_write_bound(sql) <= 9
                    count += 1
    assert count >= 15


def test_compiled_preview_schema_matches_canonical_migrations():
    import hashlib
    from pathlib import Path
    from pullwise_server.cloudflare_preview_schema import MIGRATIONS
    directory = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
    assert {item["name"] for item in MIGRATIONS} == {p.name for p in directory.glob("*.sql")}
    for item in MIGRATIONS:
        assert hashlib.sha256((directory / item["name"]).read_bytes()).hexdigest() == item["sha256"]


def test_product_meter_runs_login_installation_and_signout_with_one_cumulative_budget(tmp_path):
    import asyncio
    import sqlite3
    from types import SimpleNamespace
    from test_cloudflare_github_identity_http import Store, GitHubStub, login, call
    from test_d1_validation_budget import LocalSql
    from pullwise_server.cloudflare_preview_budget import initialize_product, ProductMeteredD1
    from pullwise_server.cloudflare_validation_budget import BudgetJournal
    from urllib.parse import parse_qs, urlsplit

    store = Store.__new__(Store)
    store.path = tmp_path / "product.sqlite"

    class RawStatement:
        def __init__(self, sql, params=()):
            self.sql, self.params = sql, params
        def bind(self, *params):
            return RawStatement(self.sql, params)

    class Raw:
        def prepare(self, sql):
            return RawStatement(sql)
        async def batch(self, statements):
            with store.connect() as connection:
                result = []
                for statement in statements:
                    before = connection.total_changes
                    rows = [dict(r) for r in connection.execute(statement.sql, statement.params).fetchall()]
                    result.append(SimpleNamespace(success=True, results=rows,
                        meta=SimpleNamespace(rows_read=0, rows_written=connection.total_changes-before)))
                return result

    async def run():
        connection = sqlite3.connect(":memory:")
        try:
            journal, raw = BudgetJournal(LocalSql(connection)), Raw()
            await initialize_product(raw, journal, clock=lambda: 10)
            gateway = GitHubStub()

            async def request(path, params=None, headers=None, method="GET"):
                from pullwise_server.cloudflare_github_identity_http import handle_identity_request
                ticket = journal.begin_product(now=11)
                meter = ProductMeteredD1(raw, journal, ticket, clock=lambda: 12)
                await meter.refresh()
                result = await handle_identity_request(binding=meter, gateway=gateway, now=1_800_000_000,
                    method=method, path=path, params=params or {}, headers=headers or {},
                    app_url="https://app.example.test",
                    callback_url="https://app.example.test/api/auth/github/callback",
                    cookie_same_site="None", trusted_origins={"https://app.example.test"})
                journal.finish(ticket, now=13)
                return result

            status, payload, _ = await request("/auth/github/authorize")
            assert status == 200
            state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
            status, _, headers = await request("/auth/github/callback", {"state": state, "code": "synthetic-code"})
            assert status == 302
            cookie = {"Cookie": headers["Set-Cookie"].split(";", 1)[0]}
            status, payload, _ = await request("/auth/session", headers=cookie)
            assert status == 200 and payload["authenticated"] is True
            status, payload, _ = await request("/integrations/github/authorize", headers=cookie)
            state = parse_qs(urlsplit(payload["url"]).query)["state"][0]
            status, _, _ = await request("/integrations/github/callback", {"state": state, "installation_id": "501"}, cookie)
            assert status == 302
            status, _, _ = await request("/auth/sign-out", headers={**cookie, "Origin": "https://app.example.test"}, method="POST")
            assert status == 200
            assert journal.snapshot()["reserved_written"] < 1000
            assert journal.snapshot()["reserved_read"] < 10000
            assert journal.snapshot()["stopped"] is None
        finally:
            connection.close()
    asyncio.run(run())


def test_creem_failure_reports_only_status_not_secret_or_response_body():
    import asyncio
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    from pullwise_server.cloudflare_creem_gateway import WorkerCreemGateway

    async def text():
        return "sensitive response body"
    async def fetch(*args):
        assert args[1]["redirect"] == "manual"
        return SimpleNamespace(ok=False, status=401,
            headers=SimpleNamespace(get=lambda _: None), text=text)
    gateway = WorkerCreemGateway(SimpleNamespace(PULLWISE_CREEM_API_KEY="synthetic-secret",
        PULLWISE_CREEM_API_BASE_URL="https://test-api.creem.io"))
    modules = {"js": SimpleNamespace(fetch=fetch, Object=SimpleNamespace(fromEntries=None),
        AbortSignal=SimpleNamespace(timeout=lambda _: None)),
        "pyodide.ffi": SimpleNamespace(to_js=lambda value, **_: value)}
    with patch.dict(sys.modules, modules), pytest.raises(ValueError, match="Creem HTTP 401"):
        asyncio.run(gateway.product("prod_synthetic"))
