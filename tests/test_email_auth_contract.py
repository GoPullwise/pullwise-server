"""Offline email auth contract and enabled-provider configuration guards."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

from pullwise_server.cloudflare_github_identity_http import session_payload

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def checker():
    spec = importlib.util.spec_from_file_location("email_auth_checker", ROOT / "scripts/check-ledger-s01.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def preview():
    return json.loads((ROOT / "cloudflare/server/wrangler.preview.jsonc").read_text())


def validate_fixture(checker, monkeypatch, tmp_path, config, environment="preview"):
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/entry.py").touch()
    (tmp_path / f"wrangler.{environment}.jsonc").write_text(json.dumps(config))
    monkeypatch.setattr(checker, "SERVER", tmp_path)
    checker.validate_config(environment, allow_placeholders=False)


def test_enabled_email_configuration_has_a_fixed_sender_and_no_recipient_restriction(checker, preview, monkeypatch, tmp_path):
    assert preview["vars"]["PULLWISE_EMAIL_AUTH_ENABLED"] == "1"
    validate_fixture(checker, monkeypatch, tmp_path, preview)


@pytest.mark.parametrize("case", [
    "missing_sender", "other_sender", "binding_absent", "wrong_binding", "sender_absent",
    "sender_mismatch", "extra_sender", "destination", "allowed_destinations", "allowed_recipients",
    "duplicate_binding", "missing_coordinator", "paused_access",
])
def test_enabled_email_rejects_missing_or_broadened_provider_controls(checker, preview, monkeypatch, tmp_path, case):
    config = copy.deepcopy(preview)
    binding = config["send_email"][0]
    if case == "missing_sender":
        config["vars"].pop("PULLWISE_EMAIL_FROM")
    elif case == "other_sender":
        config["vars"]["PULLWISE_EMAIL_FROM"] = "other@auth.pull-wise.com"
    elif case == "binding_absent":
        config.pop("send_email")
    elif case == "wrong_binding":
        binding["name"] = "OTHER_EMAIL"
    elif case == "sender_absent":
        binding.pop("allowed_sender_addresses")
    elif case == "sender_mismatch":
        binding["allowed_sender_addresses"] = ["other@auth.pull-wise.com"]
    elif case == "extra_sender":
        binding["allowed_sender_addresses"].append("other@auth.pull-wise.com")
    elif case == "destination":
        binding["destination_address"] = "one-recipient@example.test"
    elif case == "allowed_destinations":
        binding["allowed_destination_addresses"] = ["one-recipient@example.test"]
    elif case == "allowed_recipients":
        binding["allowed_recipient_addresses"] = ["one-recipient@example.test"]
    elif case == "duplicate_binding":
        config["send_email"].append(copy.deepcopy(binding))
    elif case == "missing_coordinator":
        config["durable_objects"]["bindings"] = []
    else:
        config["vars"]["PULLWISE_D1_ACCESS_ENABLED"] = "0"
    with pytest.raises(ValueError):
        validate_fixture(checker, monkeypatch, tmp_path, config)


@pytest.mark.parametrize("flag", [None, 0, 1, "true", ""])
def test_email_enable_flag_accepts_only_string_zero_or_one(checker, preview, monkeypatch, tmp_path, flag):
    preview["vars"]["PULLWISE_EMAIL_AUTH_ENABLED"] = flag
    with pytest.raises(ValueError):
        validate_fixture(checker, monkeypatch, tmp_path, preview)


def test_legacy_configs_without_an_email_flag_remain_disabled(checker, preview, monkeypatch, tmp_path):
    preview["vars"].pop("PULLWISE_EMAIL_AUTH_ENABLED")
    preview["vars"].pop("PULLWISE_EMAIL_FROM")
    preview.pop("send_email")
    validate_fixture(checker, monkeypatch, tmp_path, preview)


def test_email_work_cannot_activate_production(checker, monkeypatch, tmp_path):
    config = json.loads((ROOT / "cloudflare/server/wrangler.production.jsonc").read_text())
    assert config["vars"]["PULLWISE_D1_ACCESS_ENABLED"] == "0"
    assert config["vars"]["PULLWISE_EMAIL_AUTH_ENABLED"] == "0"
    validate_fixture(checker, monkeypatch, tmp_path, config, "production")
    config["vars"].update(PULLWISE_EMAIL_AUTH_ENABLED="1", PULLWISE_EMAIL_FROM=checker.EMAIL_SENDER)
    config["send_email"] = [{"name": "EMAIL", "allowed_sender_addresses": [checker.EMAIL_SENDER]}]
    with pytest.raises(ValueError):
        validate_fixture(checker, monkeypatch, tmp_path, config, "production")


def test_auth_operations_do_not_inherit_ledger_api_key_authority(checker):
    checker.validate_contract()
    contract = yaml.safe_load((ROOT / "openapi/ledger-v1.yaml").read_text())
    expected = {"/auth/session": "get", "/auth/sign-out": "post",
        "/auth/email/request-code": "post", "/auth/email/verify-code": "post",
        "/auth/github/authorize": "get", "/auth/github/callback": "get"}
    for path, method in expected.items():
        operation = contract["paths"][path][method]
        assert operation["security"] == [] and operation["x-pullwise-scope"] == "account:auth"
    schemas = contract["components"]["schemas"]
    assert schemas["EmailCodeRequest"]["additionalProperties"] is False
    assert set(schemas["EmailCodeRequest"]["required"]) == {"email", "purpose"}
    assert schemas["EmailCodeRequest"]["properties"]["purpose"]["enum"] == ["login", "link"]
    assert set(schemas["EmailCodeVerification"]["required"]) == {"email", "challengeId", "code"}
    assert schemas["EmailCodeVerification"]["additionalProperties"] is False
    assert schemas["EmailCodeVerification"]["properties"]["code"]["pattern"] == "^[0-9]{6}$"
    assert set(schemas["EmailCodeChallenge"]["properties"]) == {"challengeId", "expiresIn", "retryAfter"}
    assert schemas["EmailCodeChallenge"]["properties"]["expiresIn"]["const"] == 600
    assert schemas["EmailCodeChallenge"]["properties"]["retryAfter"]["const"] == 60


def test_session_contract_matches_email_github_and_signed_out_dtos():
    contract = yaml.safe_load((ROOT / "openapi/ledger-v1.yaml").read_text())
    schemas = contract["components"]["schemas"]
    users = [None, {"id": "email-user", "name": "Alice", "email": "alice@example.test",
        "providers": ["email"], "emailVerifiedAt": 1},
        {"id": "github-user", "githubAccessToken": "sealed-fixture", "email": "unverified@example.test",
         "providers": ["github"]}]
    for user in users:
        payload = session_payload(user)
        assert set(payload) == set(schemas["AccountSession"]["required"])
        assert set(payload["github"]) == set(schemas["AccountSession"]["properties"]["github"]["required"])
        if user is None:
            assert payload["authenticated"] is False and payload["user"] is None
        else:
            assert set(payload["user"]) == set(schemas["SessionUser"]["required"])
            assert payload["user"]["emailVerified"] is ("email" in user["providers"])
            assert payload["user"]["email"] == (user["email"] if "email" in user["providers"] else None)
