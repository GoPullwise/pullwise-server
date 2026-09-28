"""Finite loopback-only acceptance against a running local Server Worker.

Run migrations first with the same local persistence directory. Never use this
synthetic fixture or its credentials in a remote database.
"""
import argparse
import csv
import hashlib
import io
import json
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "cloudflare/server"
TOKEN = "pwk_loopback_runtime_fixture"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def literal(value):
    return "NULL" if value is None else "'" + str(value).replace("'", "''") + "'"


def seed_sql():
    now = int(time.time())
    account = {"id": "runtime-owner", "createdAt": now, "billing": {"plan": "free"}}
    scopes = ["profile:read", "projects:read", "categories:read", "categories:write",
              "expenses:read", "expenses:write", "reports:read"]
    sql = [
        "UPDATE app_state SET payload=" + literal(json.dumps({account["id"]: account})) + " WHERE name='users';",
        "UPDATE app_state SET payload=" + literal(json.dumps({"runtime-session": {
            "userId": account["id"], "expiresAt": now + 3600}})) + " WHERE name='sessions';",
        "INSERT INTO api_keys(id,user_id,name,key_prefix,key_hash,scopes,restrictions,created_at) VALUES(" +
        ",".join(literal(value) for value in ("runtime-key", account["id"], "Local only", TOKEN[:16],
            hashlib.sha256(TOKEN.encode()).hexdigest(), json.dumps(scopes), '{"shared":true}', now)) + ");",
        "INSERT INTO expense_categories(id,owner_id,name,revision,created_at,updated_at) VALUES"
        "('runtime-category','runtime-owner','Runtime',1,'local','local');",
    ]
    for index in range(251):
        values = (f"runtime-{index:04}", account["id"], "shared", "runtime-category", "2026-09-27",
                  100, "USD", "=formula" if index == 0 else "Runtime fixture", "local", "local")
        sql.append("INSERT INTO expenses(id,owner_id,target_kind,category_id,occurred_on,amount_minor,"
                   "currency,purpose,revision,created_at,updated_at) VALUES(" +
                   ",".join(literal(value) for value in values[:8]) + ",1," +
                   ",".join(literal(value) for value in values[8:]) + ");")
    return "\n".join(sql)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8891)
    parser.add_argument("--persist-to", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((WORKER / "wrangler.jsonc").read_text(encoding="utf-8"))
    if config.get("routes") or config.get("workers_dev") or any(
            db.get("remote") is not False for db in config["d1_databases"]):
        raise SystemExit("Refusing a configuration that is not explicitly local-only")
    with tempfile.TemporaryDirectory() as directory:
        seed = Path(directory) / "seed.sql"
        seed.write_text(seed_sql(), encoding="utf-8")
        subprocess.run(["node", str(WORKER / "node_modules/wrangler/wrangler-dist/cli.js"),
            "d1", "execute", "DB", "--local", "--config", str(WORKER / "wrangler.jsonc"),
            "--persist-to", str(args.persist_to), "--file", str(seed), "--json"],
            cwd=WORKER, check=True, capture_output=True)

    count = 0
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(method, path, body=None, headers=None, expected=200, cookie=False):
        nonlocal count
        count += 1
        if count > 30:
            raise AssertionError("Local runtime request cap exceeded")
        auth = {"Cookie": "pw_session=runtime-session"} if cookie else {"Authorization": "Bearer " + TOKEN}
        request = Request(f"http://127.0.0.1:{args.port}{path}", method=method,
            headers={**auth, "Content-Type": "application/json", **(headers or {})},
            data=json.dumps(body).encode() if body is not None else None)
        try:
            response = opener.open(request, timeout=45)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            assert response.status == expected, (method, path, response.status, raw)
            content_type = response.headers.get("Content-Type", "")
            return json.loads(raw) if "application/json" in content_type else raw

    assert call("GET", "/health")["ok"]
    assert call("GET", "/api/v1/me")["id"] == "runtime-owner"
    assert call("GET", "/api/v1/me", cookie=True)["id"] == "runtime-owner"
    rows = list(csv.reader(io.StringIO(call("GET", "/api/v1/expenses/export"))))
    assert len(rows) == 252 and rows[1][7] == "'=formula", (len(rows), rows[:2])
    page = call("GET", "/api/v1/expenses?limit=100")
    assert len(page["items"]) == 100 and page["nextCursor"]
    category = call("POST", "/api/v1/categories", {"name": "Mutation"}, expected=201)
    draft = {"target": {"kind": "shared"}, "occurredOn": "2026-09-27", "amount": "12.30",
             "currency": "USD", "categoryId": category["id"], "purpose": "Runtime write"}
    expense = call("POST", "/api/v1/expenses", draft,
                   {"Idempotency-Key": "runtime-create"}, expected=201)
    assert expense["amountMinor"] == 1230
    replay = call("POST", "/api/v1/expenses", draft,
                  {"Idempotency-Key": "runtime-create"}, expected=201)
    assert replay["id"] == expense["id"]
    path = "/api/v1/expenses/" + expense["id"]
    changed = call("PATCH", path, {**draft, "amount": "13.00"}, {"If-Match": '"1"'})
    assert changed["revision"] == 2 and changed["amountMinor"] == 1300
    call("PATCH", path, draft, {"If-Match": '"1"'}, expected=412)
    summary = call("GET", "/api/v1/reports/summary")
    assert any(group["target"] == "account" and group["amountMinor"] == 26400 for group in summary["groups"])
    call("GET", "/api/v1/reports/timeseries?bucket=month")
    call("GET", "/api/v1/reports/categories")
    call("DELETE", path, headers={"If-Match": '"2"'}, expected=204)
    call("DELETE", path, headers={"If-Match": '"2"'}, expected=204)
    call("GET", path, expected=404)
    summary = call("GET", "/api/v1/reports/summary")
    assert any(group["target"] == "account" and group["amountMinor"] == 25100 for group in summary["groups"])
    print(json.dumps({"passed": True, "local_http_requests": count, "csv_records": 251,
                      "remote_d1_rows_read": 0, "remote_d1_rows_written": 0}))


if __name__ == "__main__":
    main()
