"""Valid expense payloads remain admitted through the actual preview wrappers.

Native row metadata and Jev responses are synthetic. These tests verify payload
admission, accounting and atomic SQL, not remote provider quality or D1 costs.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from test_cloudflare_github_identity_http import GitHubStub
from test_d1_validation_budget import LocalSql
from test_jev_preview_admission import MeteredSQLite, preview
from test_ledger_automatic_assistance import Provider, expense
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_preview_budget import ProductMeteredD1, _input_bound
from pullwise_server.cloudflare_preview_schema import INDEX_COUNTS, SCHEMA_VERSION, SCHEMA_FINGERPRINT
from pullwise_server.cloudflare_validation_budget import BudgetJournal
from pullwise_server.ledger_plan_policy import JEV_RESERVATION_MICROUSD


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def session(preview):
    journal = BudgetJournal(LocalSql(preview.connection), preview_product=True)
    with preview.fixture.store.connect() as database:
        counts = {table: database.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                  for table in INDEX_COUNTS}
    state = journal.snapshot()
    state.update(schema_ready=True, schema_version=SCHEMA_VERSION,
        schema_fingerprint=SCHEMA_FINGERPRINT,
        product_data={"rows": counts, "json": {}, "arrays": 0})
    journal._save(state)
    return SimpleNamespace(preview=preview, journal=journal, provider=Provider())


def call(client, body, *, method="POST", item_id=None, revision=None):
    assert len(compact(body).encode("utf-8")) <= 8192
    async def run():
        ticket = client.journal.begin_product(now=10)
        meter = ProductMeteredD1(MeteredSQLite(client.preview.fixture.store), client.journal,
                                 ticket, clock=lambda: 11)
        await meter.refresh()
        binding = PlanLimitedD1(meter, policy=client.preview.policy, now=client.preview.now)
        headers = {**client.preview.fixture.headers, "Idempotency-Key": "preview-jev-payload"}
        if revision is not None:
            headers["If-Match"] = f'"{revision}"'
        result = await handle_ledger_request(binding=binding, gateway=GitHubStub(),
            suggestion_gateway=client.provider, method=method,
            path="/api/v1/expenses" + ("/" + item_id if item_id else ""), params={},
            body=body, now=client.preview.now, headers=headers)
        client.journal.finish(ticket, now=12)
        return result
    return asyncio.run(run())


def assert_accounting(client, *, writes, reservations):
    assert client.journal.snapshot()["stopped"] is None
    with client.preview.fixture.store.connect() as database:
        usage = database.execute("SELECT writes,records,jev_reserved_microusd FROM ledger_plan_usage").fetchone()
        assert tuple(usage) == (writes, 1, reservations * JEV_RESERVATION_MICROUSD)
        assert database.execute("SELECT count(*) FROM expense_suggestion_budget").fetchone()[0] == 0
        assert database.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == reservations
    assert len(client.provider.calls) == reservations


@pytest.mark.parametrize("note", ["Hosting", "a" * 4000, "汉" * 1260],
                         ids=["short", "max-ascii", "long-cjk"])
def test_available_jev_long_valid_expense_and_exact_replay_preserve_preview(preview, note):
    client = session(preview)
    body = expense(note=note)
    status, saved = call(client, body)
    assert status == 201 and saved["note"] == note
    assert saved["assistance"]["status"] == "available"
    assert call(client, body) == (201, saved)
    with preview.fixture.store.connect() as database:
        row = database.execute("SELECT after_json FROM expense_events").fetchone()
        replay = database.execute("SELECT response_json FROM expense_create_idempotency").fetchone()
        assert json.loads(row[0]) == json.loads(replay[0]) == saved
        if "汉" in note:
            assert "汉" in row[0] and "\\u6c49" not in row[0]
    assert_accounting(client, writes=1, reservations=1)


def maximum_wire_body(category_id=None):
    body = expense(category_id, purpose="P" * 500, note="", unit="u" * 40, quantity="1" * 40)
    remaining = 8192 - len(compact(body).encode("utf-8"))
    body["note"] = "汉" * (remaining // 3) + "a" * (remaining % 3)
    assert len(body["note"]) <= 4000 and len(compact(body).encode("utf-8")) == 8192
    return body


def test_maximum_wire_with_thirty_category_probabilities_patch_and_replay(preview):
    category_id = "cat_" + "f" * 32
    with preview.fixture.store.connect() as database:
        database.execute("UPDATE expense_categories SET id=? WHERE id='cat_host'", (category_id,))
        database.executemany("""INSERT INTO expense_categories(id,owner_id,name,created_at,updated_at)
            VALUES(?,'usr_github_77',?,'local','local')""",
            [("cat_" + f"{index:032x}", f"Category {index:02}") for index in range(29)])
    client = session(preview)
    body = maximum_wire_body()
    status, saved = call(client, body)
    assert status == 201 and saved["categoryId"] == category_id
    assert len(saved["assistance"]["probabilities"]["category"]) == 31
    assert 8192 < len(compact(saved).encode("utf-8")) <= 16384
    changed = maximum_wire_body(category_id)
    changed["purpose"] = "Q" * 500
    status, edited = call(client, changed, method="PATCH", item_id=saved["id"], revision=1)
    assert status == 200 and edited["revision"] == 2 and edited["note"] == changed["note"]
    assert call(client, body) == (201, saved)
    with preview.fixture.store.connect() as database:
        rows = database.execute("SELECT action,before_json,after_json FROM expense_events ORDER BY action").fetchall()
        assert len(rows) == 2
        assert json.loads(rows[0][2]) == saved
        before, after = json.loads(rows[1][1]), json.loads(rows[1][2])
        assert before["note"] == saved["note"] and before["revision"] == 1
        assert after == edited
        assert 8192 < len(rows[1][1].encode("utf-8")) <= 16384
        assert 8192 < len(rows[1][2].encode("utf-8")) <= 16384
    assert_accounting(client, writes=2, reservations=2)


@pytest.mark.parametrize("sql", ["SELECT ?", "INSERT INTO app_state(name,payload) VALUES('test',?)",
    "INSERT INTO expenses(id,note) VALUES('test',?)",
    "INSERT INTO expense_events(owner_id,after_json) VALUES(?,NULL)",
    "INSERT INTO expense_create_idempotency(owner_id,response_json) VALUES(?,NULL)"])
def test_larger_expense_json_allowance_does_not_expand_other_sql_parameters(sql):
    with pytest.raises(ValueError):
        _input_bound((compact({"text": "a" * 8192}),), sql=sql)


def test_expense_payload_envelope_keeps_a_finite_size_limit():
    sql = "INSERT INTO expense_events(id,before_json,after_json) VALUES(?,?,?)"
    with pytest.raises(ValueError):
        _input_bound(("test", None, compact({"text": "a" * 16384})), sql=sql)


@pytest.mark.parametrize("payload", ["a" * 8193, "[]", '{"number":NaN}'])
def test_expanded_expense_snapshot_requires_finite_json_object(payload):
    with pytest.raises(ValueError):
        _input_bound((payload,), sql="INSERT INTO expense_events(after_json) VALUES(?)")
