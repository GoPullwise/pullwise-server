"""Max assistance is part of ordinary expense writes for sessions and keys."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_ledger_api import handle_ledger_request
from pullwise_server.cloudflare_plan_limits import PlanLimitedD1
from pullwise_server.cloudflare_state_records import encode_record, record_name
from pullwise_server.ledger_plan_policy import default_policy
import test_ledger_suggestions as suggestion_fixture
from test_cloudflare_github_identity_http import GitHubStub


@pytest.fixture
def ledger():
    fixture = suggestion_fixture.LedgerSuggestionTests()
    fixture.setUp()
    with fixture.store.connect() as db:
        migrations = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
        for name in ("0004_ledger_plan_usage.sql", "0009_activity_log.sql"):
            db.executescript((migrations / name).read_text())
    fixture.binding = PlanLimitedD1(fixture.binding, now=fixture.now + 3)
    yield fixture
    fixture.tearDown()


def call(ledger, body=None, provider=None, *, method="POST", path="/api/v1/expenses", headers=None, key="create-one"):
    return asyncio.run(handle_ledger_request(binding=ledger.binding, gateway=GitHubStub(),
        suggestion_gateway=provider, method=method, path=path, params={}, body=body,
        headers={**(headers or ledger.headers), "Idempotency-Key": key}, now=ledger.now + 3))


def category(ledger, name="Hosting"):
    status, record = call(ledger, {"name": name}, path="/api/v1/categories")
    assert status == 201
    return record["id"]


def expense(category_id=None, **changes):
    return {"target": {"kind": "shared"}, "occurredOn": "2026-09-27", "amount": "12.50",
        "currency": "USD", "purpose": "September hosting", **({"categoryId": category_id} if category_id else {}), **changes}


class Provider:
    enabled = True
    daily_limit = 100

    def __init__(self, *, confidence=0.95, failure=None, on_call=None):
        self.confidence, self.failure, self.on_call = confidence, failure, on_call
        self.calls = []

    async def evaluate(self, request):
        self.calls.append(request)
        if self.on_call:
            self.on_call()
        if self.failure:
            raise self.failure
        answers = {}
        for key, question in request["questions"].items():
            choices = list(question["criteria"])
            choice = "shared" if key == "target" else next(
                (item for item in choices if "Hosting" in question["criteria"][item]),
                next(item for item in choices if item != "uncertain"))
            others = (1 - self.confidence) / (len(choices) - 1)
            answers[key] = {"type": "choice", "choice": choice, "confidence": self.confidence,
                "probabilities": {item: self.confidence if item == choice else others for item in choices}}
        return json.dumps({"model": "jev-1.13.0", "answers": answers,
            "usage": {"input_tokens": 100, "output_tokens": 5}}).encode()


def test_max_create_automatically_classifies_and_replay_spends_nothing(ledger):
    chosen = category(ledger)
    provider = Provider()
    status, saved = call(ledger, expense(), provider)
    assert status == 201
    assert saved["categoryId"] == chosen
    assert saved["assistance"]["categorySource"] == "jev"
    assert saved["amount"] == "12.50" and saved["target"] == {"kind": "shared"}
    status, replay = call(ledger, expense(), provider)
    assert (status, replay) == (201, saved)
    assert len(provider.calls) == 1
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expenses").fetchone()[0] == 1
        assert db.execute("SELECT attempts FROM expense_suggestion_budget").fetchone()[0] == 1
        assert db.execute("SELECT records FROM ledger_plan_usage").fetchone()[0] == 1
    assert call(ledger, expense(amount="13.50"), provider)[0] == 409
    assert len(provider.calls) == 1


def test_expense_write_key_gets_assistance_without_separate_model_scope(ledger):
    chosen = category(ledger)
    status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "expense importer", "scopes": ["expenses:write"],
            "restrictions": {"projectIds": [], "shared": True}}, now=ledger.now + 3))
    assert status == 201
    provider = Provider()
    status, saved = call(ledger, expense(), provider, headers={"Authorization": "Bearer " + token["key"]})
    assert status == 201 and saved["categoryId"] == chosen and len(provider.calls) == 1
    with ledger.store.connect() as db:
        assert db.execute("SELECT actor_kind FROM expense_events").fetchone()[0] == "api_key"


@pytest.mark.parametrize("plan", ["free", "pro"])
def test_unpaid_tiers_never_call_model_and_require_category(ledger, plan):
    chosen = category(ledger)
    with ledger.store.connect() as db:
        owner_id = "usr_github_77"
        name = record_name("users", owner_id)
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
        user["billing"]["plan"] = plan
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
                   (encode_record("users", owner_id, user), name))
    provider = Provider()
    assert call(ledger, expense(chosen), provider)[0] == 201
    status, failure = call(ledger, expense(), provider, key="missing-category")
    assert status == 422 and failure["error"]["code"] == "CATEGORY_REQUIRED"
    assert provider.calls == []


@pytest.mark.parametrize("provider", [None, Provider(confidence=0.6), Provider(failure=TimeoutError())])
def test_unavailable_or_uncertain_never_blocks_explicit_category_or_invents_one(ledger, provider):
    chosen = category(ledger)
    status, saved = call(ledger, expense(chosen), provider)
    assert status == 201 and saved["categoryId"] == chosen
    assert saved["assistance"]["categorySource"] == "user"
    status, failure = call(ledger, expense(), provider, key="missing-category")
    assert status == 422 and failure["error"]["code"] == "CATEGORY_REQUIRED"
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expenses").fetchone()[0] == 1


def test_manual_category_is_preserved_and_duplicate_check_excludes_edited_record(ledger):
    category(ledger, "A Hosting")
    manual = category(ledger, "Other")
    provider = Provider()
    status, saved = call(ledger, expense(manual), provider)
    assert status == 201 and saved["categoryId"] == manual
    assert saved["assistance"]["suggestions"]["categoryId"] != manual
    status, duplicate = call(ledger, expense(manual), provider, key="another-record")
    assert status == 201 and duplicate["assistance"]["suggestions"]["duplicateExpenseId"] == saved["id"]
    # Remove the other record so the sole remaining record cannot match itself.
    assert call(ledger, method="DELETE", path=f"/api/v1/expenses/{duplicate['id']}",
        headers={**ledger.headers, "If-Match": '"1"'})[0] == 204
    status, edited = call(ledger, expense(manual), provider, method="PATCH",
        path=f"/api/v1/expenses/{saved['id']}", headers={**ledger.headers, "If-Match": '"1"'})
    assert status == 200 and "duplicateExpenseId" not in edited["assistance"]["suggestions"]
    before = len(provider.calls)
    assert call(ledger, method="GET", path=f"/api/v1/expenses/{saved['id']}", provider=provider)[0] == 200
    assert len(provider.calls) == before


def test_exhausted_jev_budget_does_not_block_manual_expense(ledger):
    chosen = category(ledger)
    policy = default_policy()
    policy["max"]["jevMonthlyBudgetUsd"] = "0.00"
    ledger.binding.plan_policy = policy
    provider = Provider()
    status, saved = call(ledger, expense(chosen), provider)
    assert status == 201 and saved["assistance"]["status"] == "unavailable"
    assert provider.calls == []


def test_revoked_key_during_provider_call_cannot_save(ledger):
    category(ledger)
    status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"scopes": ["expenses:write"], "restrictions": {"shared": True}}, now=ledger.now + 3))
    assert status == 201
    def revoke():
        with ledger.store.connect() as db:
            db.execute("UPDATE api_keys SET revoked_at=? WHERE id=?", (ledger.now + 3, token["id"]))
    status, _ = call(ledger, expense(), Provider(on_call=revoke),
        headers={"Authorization": "Bearer " + token["key"]})
    assert status in {401, 403, 409, 422}
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expenses").fetchone()[0] == 0


@pytest.mark.parametrize("occurred", ["0001-01-01", "9999-12-31"])
def test_date_boundaries_do_not_overflow_assistance(ledger, occurred):
    chosen = category(ledger)
    status, saved = call(ledger, expense(chosen, occurredOn=occurred, note="n" * 4000), Provider())
    assert status == 201 and saved["occurredOn"] == occurred


def test_replay_remains_resolvable_after_category_archival(ledger):
    chosen = category(ledger)
    provider = Provider()
    status, saved = call(ledger, expense(chosen), provider)
    assert status == 201
    assert call(ledger, method="DELETE", path=f"/api/v1/categories/{chosen}",
        headers={**ledger.headers, "If-Match": '"1"'})[0] == 204
    status, replay = call(ledger, expense(chosen), provider)
    assert (status, replay) == (201, saved) and len(provider.calls) == 1


def test_invalid_access_never_calls_provider(ledger):
    chosen = category(ledger)
    status, token = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"scopes": ["expenses:write"], "restrictions": {"projectIds": [], "shared": False}},
        now=ledger.now + 3))
    assert status == 201
    provider = Provider()
    assert call(ledger, expense(chosen), provider, headers={"Authorization": "Bearer " + token["key"]})[0] == 403
    assert provider.calls == []


def test_expired_max_has_no_automatic_model_access(ledger):
    chosen = category(ledger)
    with ledger.store.connect() as db:
        owner_id = "usr_github_77"
        name = record_name("users", owner_id)
        user = json.loads(db.execute("SELECT payload FROM app_state WHERE name=?", (name,)).fetchone()[0])
        user["billing"]["currentPeriodEnd"] = ledger.now - 1
        db.execute("UPDATE app_state SET payload=? WHERE name=?",
                   (encode_record("users", owner_id, user), name))
    provider = Provider()
    assert call(ledger, expense(chosen), provider)[0] == 201
    assert call(ledger, expense(), provider, key="automatic-category")[1]["error"]["code"] == "CATEGORY_REQUIRED"
    assert provider.calls == []


def test_category_archived_during_inference_is_not_saved(ledger):
    chosen = category(ledger)
    def archive():
        with ledger.store.connect() as db:
            db.execute("UPDATE expense_categories SET archived_at='now' WHERE id=?", (chosen,))
    status, failure = call(ledger, expense(), Provider(on_call=archive))
    assert status == 409 and failure["error"]["code"] == "EXPENSE_CONFLICT"
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expenses").fetchone()[0] == 0


def test_daily_limit_respects_canonical_schema_and_keeps_manual_save_available(ledger):
    chosen = category(ledger)
    day = datetime.fromtimestamp(ledger.now + 3, timezone.utc).date().isoformat()
    with ledger.store.connect() as db:
        owner = db.execute("SELECT owner_id FROM expense_categories WHERE id=?", (chosen,)).fetchone()[0]
        db.execute("INSERT INTO expense_suggestion_budget(owner_id,day,attempts) VALUES(?,?,19)",
                   (owner, day))
    provider = Provider()  # Intentionally requests 100; the schema permits 20.
    status, first = call(ledger, expense(chosen), provider, key="daily-twentieth")
    assert status == 201 and first["assistance"]["status"] == "available"
    status, second = call(ledger, expense(chosen, purpose="Another hosting bill"), provider, key="daily-limit")
    assert status == 201 and second["assistance"]["status"] == "unavailable"
    assert second["assistance"]["reason"] == "SUGGESTION_LIMIT"
    assert second["categoryId"] == chosen and len(provider.calls) == 1
    with ledger.store.connect() as db:
        assert db.execute("SELECT attempts FROM expense_suggestion_budget WHERE owner_id=? AND day=?",
                          (owner, day)).fetchone()[0] == 20
