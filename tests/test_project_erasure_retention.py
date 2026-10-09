"""Physical project cleanup and oldest replacement share active capacity."""

from test_expense_retention import app, create, expense, preference, request


def test_erasing_an_already_retired_project_does_not_free_a_second_active_slot(app):
    app.policy["pro"]["records"] = 2
    retired = create(app, expense(project=True, date="2026-08-01"), key="old-project")
    shared = create(app, expense(date="2026-09-01"), key="old-shared")
    assert preference(app, enabled=True, revision=1)[0] == 200
    replacement = create(app, expense(date="2026-10-01"), key="replacement")
    assert next(row for row in app.rows("expenses") if row["id"] == retired["id"])["deleted_at"]
    assert app.rows("ledger_plan_usage")[0]["records"] == 2

    assert request(app, "/api/v1/projects/prj_1", method="DELETE", revision=1) == (204, None)
    usage = app.rows("ledger_plan_usage")[0]
    assert (usage["projects"], usage["records"]) == (1, 2)
    assert {row["id"] for row in app.rows("expenses")} == {shared["id"], replacement["id"]}
    assert all(row["expense_id"] != retired["id"] for row in app.rows("expense_events"))
    assert all(row["expense_id"] != retired["id"] for row in app.rows("expense_create_idempotency"))

    newest = create(app, expense(date="2026-10-02"), key="next-replacement")
    assert next(row for row in app.rows("expenses") if row["id"] == shared["id"])["deleted_at"]
    assert {row["id"] for row in app.rows("expenses") if row["deleted_at"] is None} == {
        replacement["id"], newest["id"]}
    assert app.rows("ledger_plan_usage")[0]["records"] == 2


def test_erasing_active_project_frees_places_without_retiring_surviving_shared_data(app):
    app.policy["pro"]["records"] = 2
    erased = create(app, expense(project=True), key="project")
    shared = create(app, expense(date="2026-08-01"), key="shared")
    assert preference(app, enabled=True, revision=1)[0] == 200
    assert request(app, "/api/v1/projects/prj_1", method="DELETE", revision=1) == (204, None)
    surviving_events = app.rows("expense_events")
    assert {row["id"] for row in app.rows("expenses")} == {shared["id"]}
    assert app.rows("ledger_plan_usage")[0]["records"] == 1

    saved = create(app, key="freed-place")
    assert {row["id"] for row in app.rows("expenses") if row["deleted_at"] is None} == {shared["id"], saved["id"]}
    assert all(row["action"] != "delete" for row in app.rows("expense_events"))
    assert all(row in app.rows("expense_events") for row in surviving_events)
    assert all(row["expense_id"] != erased["id"] for row in app.rows("expense_events"))
    assert (app.rows("ledger_plan_usage")[0]["projects"], app.rows("ledger_plan_usage")[0]["records"]) == (1, 2)
