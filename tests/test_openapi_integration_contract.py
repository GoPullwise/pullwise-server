"""Public integration contract exposes the same authorized ledger resources."""
from pathlib import Path

import yaml

from pullwise_server.api_key_dto_rules import ALLOWED_SCOPES, DEFAULT_SCOPES
from pullwise_server.cloudflare_ledger_expenses import SUPPORTED
from pullwise_server.billing_projection import billing_account_dto, subscription_event_dto
from pullwise_server.billing_catalog_rules import catalog_payload
from pullwise_server.creem_public_catalog_rules import verified_public_catalog


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = yaml.safe_load((ROOT / "openapi/ledger-v1.yaml").read_text())
SCHEMAS = CONTRACT["components"]["schemas"]
SCHEMA_KEYS = {
    "$ref", "$schema", "$id", "$defs", "type", "title", "description", "default",
    "examples", "enum", "const", "required", "properties", "additionalProperties",
    "unevaluatedProperties", "minProperties", "maxProperties", "items", "maxItems",
    "minItems", "uniqueItems", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "format", "allOf", "anyOf", "oneOf", "not",
    "if", "then", "else", "deprecated", "readOnly", "writeOnly", "nullable",
}


def validate_schema(schema):
    assert isinstance(schema, (dict, bool))
    if isinstance(schema, bool):
        return
    assert set(schema) <= SCHEMA_KEYS, set(schema) - SCHEMA_KEYS
    for field in ("properties", "$defs"):
        for child in schema.get(field, {}).values():
            validate_schema(child)
    for field in ("items", "additionalProperties", "unevaluatedProperties", "not", "if", "then", "else"):
        if field in schema:
            validate_schema(schema[field])
    for field in ("allOf", "anyOf", "oneOf"):
        for child in schema.get(field, []):
            validate_schema(child)


def test_descriptions_cannot_parse_into_unintended_schema_fields_and_refs_resolve():
    for schema in SCHEMAS.values():
        validate_schema(schema)

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                target = CONTRACT
                for field in value["$ref"].removeprefix("#/").split("/"):
                    target = target[field]
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
    visit(CONTRACT)


def test_bearer_contract_covers_projects_recurring_and_member_governance():
    for path, resource in CONTRACT["paths"].items():
        governed = path in {"/api/v1/workspaces", "/api/v1/workspace-invitation-requests"} or path.startswith("/api/v1/workspaces/")
        recurring = path.startswith("/api/v1/expense-recurring-rules")
        for method, operation in resource.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            if governed or recurring or path == "/api/v1/projects/{id}" and method == "delete":
                assert {"bearerKey": []} in operation.get("security", CONTRACT["security"])
            if governed:
                assert operation["x-pullwise-scope"] == "members:" + ("read" if method == "get" else "write")
            if recurring:
                assert operation["x-pullwise-scope"] == "expenses:" + ("read" if method == "get" else "write")
    for action in ("preview", "accept"):
        operation = CONTRACT["paths"]["/api/v1/workspace-invitations/" + action]["post"]
        assert operation["security"] == [{"cookieSession": []}]
        assert operation["x-pullwise-scope"] == "profile:read"


def test_account_credentials_use_cookie_bootstrap_and_error_bodies_are_described():
    assert set(SCHEMAS["ApiKeyScope"]["enum"]) == ALLOWED_SCOPES
    assert SCHEMAS["ApiKeyInput"]["properties"]["scopes"]["default"] == DEFAULT_SCOPES
    for path in ("/api-keys", "/api-keys/{id}"):
        for method, operation in CONTRACT["paths"][path].items():
            if method in {"get", "post", "delete"}:
                assert operation["security"] == [{"cookieSession": []}]
    assert SCHEMAS["Error"]["required"] == ["error"]
    assert SCHEMAS["Error"]["properties"]["error"]["required"] == ["code"]
    assert "requestId" not in SCHEMAS["Error"]["properties"]
    for resource in CONTRACT["paths"].values():
        for method, operation in resource.items():
            if method not in {"get", "post", "patch", "delete"}:
                continue
            for status, response in operation["responses"].items():
                if not str(status).isdigit() or int(status) < 400:
                    continue
                if "$ref" in response:
                    response = CONTRACT["components"]["responses"][response["$ref"].split("/")[-1]]
                assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}


def test_money_full_expense_replacement_and_list_dtos_match_runtime():
    assert set(SCHEMAS["CurrencyCode"]["enum"]) == SUPPORTED
    assert set(SCHEMAS["ExpenseFields"]["required"]) == {"target", "occurredOn", "amount", "currency", "purpose"}
    assert SCHEMAS["ExpensePatchInput"]["allOf"] == [{"$ref": "#/components/schemas/ExpenseInput"}]
    assert CONTRACT["paths"]["/api/v1/categories"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["type"] == "array"
    for name in ("ProjectPage", "ExpensePage", "RecurringExpensePage"):
        assert SCHEMAS[name]["required"] == ["items", "nextCursor"]
    assert SCHEMAS["LedgerUsage"]["required"] == ["workspaceId", "projects", "expenseRecords"]


def test_billing_reads_describe_actual_account_catalog_and_cookie_only_personalization():
    import json

    account = billing_account_dto({"id": "local-owner"}, "free", timestamp=100)
    event = subscription_event_dto({})
    assert set(SCHEMAS["BillingAccount"]["required"]) == set(account)
    assert set(SCHEMAS["BillingAccount"]["properties"]) == set(account)
    assert set(SCHEMAS["BillingSubscriptionEvent"]["required"]) == set(event)
    assert set(SCHEMAS["BillingSubscriptionEvent"]["properties"]) == set(event)
    saved = verified_public_catalog({"pro": {}, "max": {}}, {})
    catalog = catalog_payload([{"payload_json": json.dumps(saved), "expires_at": 200}], 100)
    assert set(SCHEMAS["BillingCatalog"]["required"]) <= set(catalog)
    assert set(catalog) <= set(SCHEMAS["BillingCatalog"]["properties"])
    for plan in catalog["plans"]:
        assert set(SCHEMAS["BillingPlan"]["required"]) <= set(plan)
        for price in plan["prices"].values():
            assert set(SCHEMAS["BillingPrice"]["required"]) <= set(price)
            assert set(price) <= set(SCHEMAS["BillingPrice"]["properties"])
    assert CONTRACT["paths"]["/billing"]["get"]["security"] == [{"cookieSession": []}]
    assert CONTRACT["paths"]["/billing/plan"]["get"]["security"] == []
    assert "ledgerUsage" in SCHEMAS["BillingPage"]["required"]
    assert not {"account", "ledgerUsage"}.intersection(SCHEMAS["BillingCatalog"]["required"])
