"""Duplicate hints use exact ledger money/date and current candidate facts."""
import json

import pytest

import test_ledger_automatic_assistance as automatic
from test_ledger_review_adversarial import financial_rows, review, standalone_project


ledger = automatic.ledger


def saved_candidate(ledger, category_id):
    status, candidate = automatic.call(ledger, automatic.expense(category_id), key="candidate")
    assert status == 201
    return candidate


@pytest.mark.parametrize("change", [
    {"occurredOn": "2026-09-26"}, {"occurredOn": "2026-09-28"},
    {"currency": "EUR"}, {"amount": "12.51"},
])
def test_automatic_create_never_suggests_different_date_currency_or_amount(ledger, change):
    category = automatic.category(ledger)
    candidate = saved_candidate(ledger, category)
    provider = automatic.Provider()
    status, created = automatic.call(ledger, automatic.expense(category, **change), provider, key="new")
    assert status == 201 and len(provider.calls) == 1
    assert "duplicateExpenseId" not in created["assistance"]["suggestions"]
    assert candidate["id"] not in json.dumps(created["assistance"])


def test_automatic_create_matches_integer_amount_even_when_purpose_and_format_differ(ledger):
    category = automatic.category(ledger)
    candidate = saved_candidate(ledger, category)
    provider = automatic.Provider()
    status, created = automatic.call(ledger,
        automatic.expense(category, purpose="A different description", amount="12.5"), provider, key="new")
    assert status == 201
    assert created["assistance"]["suggestions"]["duplicateExpenseId"] == candidate["id"]
    assert provider.calls[0]["state"]["purpose"] == "A different description"
    assert candidate["purpose"] not in json.dumps(provider.calls)


@pytest.mark.parametrize("change", ["date", "currency", "amount", "delete", "move"])
def test_automatic_create_rechecks_candidates_after_provider(ledger, change):
    category = automatic.category(ledger)
    candidate = saved_candidate(ledger, category)
    project_id = standalone_project(ledger) if change == "move" else None

    def mutate_candidate():
        with ledger.store.connect() as db:
            if change == "date":
                db.execute("UPDATE expenses SET occurred_on='2026-09-28',revision=revision+1 WHERE id=?",
                    (candidate["id"],))
            elif change == "currency":
                db.execute("UPDATE expenses SET currency='EUR',revision=revision+1 WHERE id=?", (candidate["id"],))
            elif change == "amount":
                db.execute("UPDATE expenses SET amount_minor=amount_minor+1,revision=revision+1 WHERE id=?",
                    (candidate["id"],))
            elif change == "delete":
                db.execute("UPDATE expenses SET deleted_at='local',revision=revision+1 WHERE id=?", (candidate["id"],))
            else:
                db.execute("UPDATE expenses SET target_kind='project',project_id=?,revision=revision+1 WHERE id=?",
                    (project_id, candidate["id"]))

    provider = automatic.Provider(on_call=mutate_candidate)
    status, created = automatic.call(ledger, automatic.expense(category), provider, key="new")
    assert status == 201 and len(provider.calls) == 1
    assert "duplicateExpenseId" not in created["assistance"]["suggestions"]
    assert candidate["id"] not in json.dumps(created["assistance"])


def test_automatic_create_rechecks_new_candidate_created_during_provider(ledger):
    category = automatic.category(ledger)
    candidates = []

    # Call the async helper directly in the hook; nested asyncio.run would fail.
    async def add_candidate(request):
        from pullwise_server.cloudflare_ledger_api import handle_ledger_request
        from test_cloudflare_github_identity_http import GitHubStub
        status, candidate = await handle_ledger_request(binding=ledger.binding, gateway=GitHubStub(),
            method="POST", path="/api/v1/expenses", params={}, body=automatic.expense(category),
            headers={**ledger.headers, "Idempotency-Key": "candidate"}, now=ledger.now + 3)
        assert status == 201
        candidates.append(candidate)
        return await original_evaluate(request)

    provider = automatic.Provider()
    original_evaluate = provider.evaluate
    provider.evaluate = add_candidate
    status, created = automatic.call(ledger, automatic.expense(category), provider, key="new")
    assert status == 201
    assert created["assistance"]["suggestions"]["duplicateExpenseId"] == candidates[0]["id"]


@pytest.mark.parametrize("mode", ["review", "suggestion"])
@pytest.mark.parametrize("field,value", [
    ("occurred_on", "2026-09-28"), ("currency", "EUR"), ("amount_minor", 1251),
])
def test_candidate_exact_facts_are_fenced_in_publication_batch(ledger, monkeypatch, mode, field, value):
    category = automatic.category(ledger)
    candidate = saved_candidate(ledger, category)
    if mode == "review":
        status, source = automatic.call(ledger, automatic.expense(category), key="source")
        assert status == 201
    original_batch = ledger.binding.batch
    changed = []

    async def batch(commands):
        if any("INSERT INTO expense_suggestion_events" in command.sql for command in commands):
            with ledger.store.connect() as db:
                # Even a malformed writer that fails to advance revision cannot
                # evade the three exact duplicate predicates in the final guard.
                db.execute("UPDATE expenses SET " + field + "=? WHERE id=?", (value, candidate["id"]))
            changed.append(financial_rows(ledger))
        return await original_batch(commands)

    monkeypatch.setattr(ledger.binding, "batch", batch)
    provider = automatic.Provider()
    if mode == "review":
        status, result = review(ledger, source, provider)
    else:
        draft = {key: value for key, value in automatic.expense(category).items() if key != "categoryId"}
        status, result = automatic.call(ledger, draft, provider, path="/api/v1/expense-suggestions")
    assert status == (412 if mode == "review" else 409)
    assert "error" in result and len(provider.calls) == 1
    assert changed and financial_rows(ledger) == changed[0]
    with ledger.store.connect() as db:
        assert db.execute("SELECT count(*) FROM expense_suggestion_events").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM d1_command_guard").fetchone()[0] == 0
