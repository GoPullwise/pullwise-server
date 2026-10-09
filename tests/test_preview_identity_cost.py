"""Offline trace generation and native accounting must never hide unknown usage."""
import importlib.util
from pathlib import Path

import pytest


def checker():
    path = Path(__file__).resolve().parents[1] / "scripts/check-preview-identity-cost.py"
    spec = importlib.util.spec_from_file_location("preview_identity_cost", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sql_split_preserves_literals_comments_and_rejects_partial_statement():
    module = checker()
    source = "-- migration\nCREATE TABLE t(v TEXT); INSERT INTO t VALUES('a;b');"
    assert len(module.split_sql(source)) == 2
    with pytest.raises(ValueError):
        module.split_sql("CREATE TABLE unfinished(")


def test_native_evidence_requires_every_statement_and_integer_counters():
    module = checker()
    trace = [{"case": "schema", "statements": [{"sql": "SELECT 1", "params": []}]}]
    good = {"complete": True, "operations": [{"index": 0, "meta": [
        {"rows_read": 2, "rows_written": 1}]}]}
    assert module.validate_evidence(trace, good) == {"rows_read": 2, "rows_written": 1}
    for bad in [{"complete": False, "operations": []},
                {"complete": True, "operations": []},
                {"complete": True, "operations": [{"index": 0, "meta": [{}]}]},
                {"complete": True, "operations": [{"index": 0, "meta": [
                    {"rows_read": True, "rows_written": 1}]}]}]:
        with pytest.raises(ValueError):
            module.validate_evidence(trace, bad)


def test_identity_trace_replays_fresh_schema_and_keeps_providers_synthetic(tmp_path):
    module = checker()
    trace, manifest = module.collect_trace(tmp_path)
    cases = {operation["case"] for operation in trace}
    assert {"schema", "login_authorize", "login_callback", "callback_replay",
            "session", "install_authorize", "install_callback", "repositories",
            "sign_out", "signed_out_session"} <= cases
    assert manifest["tables"] == 22 and manifest["indexes"] == 50
    assert manifest["http_cases"] == 9
    assert manifest["remote_admissible"] is False
    assert manifest["final_state"] == {"users": 1, "sessions": 0, "githubStates": 0}
    assert manifest["state_storage_version"] == 1
    # Current v11 adds the preserving occurrence rebuild and replay index.
    # This is an offline trace, explicitly not an admitted remote plan.
    assert len(trace) == 128
