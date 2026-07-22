"""Generated Python facade semantic validators."""

from __future__ import annotations

from .agent_first_contract_bundle_python_budget import PYTHON_BUDGET
from .agent_first_contract_bundle_python_control import PYTHON_CONTROL
from .agent_first_contract_bundle_python_dispatch import PYTHON_DISPATCH
from .agent_first_contract_bundle_python_gate import PYTHON_GATE
from .agent_first_contract_bundle_python_gate_input import PYTHON_GATE_INPUT
from .agent_first_contract_bundle_python_gate_preparation import (
    PYTHON_GATE_PREPARATION,
)
from .agent_first_contract_bundle_python_pre_gate import PYTHON_PRE_GATE
from .agent_first_contract_bundle_python_publication import PYTHON_PUBLICATION
from .agent_first_contract_bundle_python_quality_policy import (
    PYTHON_QUALITY_POLICY,
)
from .agent_first_contract_bundle_python_result import PYTHON_RESULT
from .agent_first_contract_bundle_python_rules import PYTHON_RULES
from .agent_first_contract_bundle_python_task_evidence import (
    PYTHON_TASK_EVIDENCE,
)
from .agent_first_contract_bundle_python_task_control_helpers import (
    PYTHON_TASK_CONTROL_HELPERS,
)
from .agent_first_contract_bundle_python_task_control_rules import (
    PYTHON_TASK_CONTROL_RULES,
)
from .agent_first_contract_bundle_python_tool_evidence import PYTHON_TOOL_EVIDENCE


PYTHON_SEMANTICS_BASE = r'''
def _public_error_code(detail: str, explicit: str | None) -> str:
    document = json.loads(base64.b64decode(BUNDLE_BASE64).decode("utf-8"))
    codes = {
        entry["code"]
        for family in document["families"]
        for item in family["fixtures"]
        if item["fixture_id"] == "error_golden_current_registry"
        for entry in item["document"]["entries"]
    }
    candidate = explicit or detail
    return candidate if candidate in codes else "CONTRACT_DOCUMENT_INVALID"


def _validate_one_of(options: object, value: object, path: str) -> None:
    matches = 0
    for option in options:
        try:
            _validate_node(option, value, path)
        except ContractValidationError:
            continue
        matches += 1
    if matches != 1:
        _fail("CONTRACT_ONE_OF_INVALID", path)


def _pattern_matches(pattern: str, value: str) -> bool:
    match = re.search(pattern, value)
    if match is None:
        return False
    if pattern.startswith("^") and pattern.endswith("$"):
        return match.start() == 0 and match.end() == len(value)
    return True


def _json_equal(left: object, right: object) -> bool:
    return canonical_document_bytes(left) == canonical_document_bytes(right)


def _validate_reference_annotations(
    rule: dict[str, object], value: object, path: str
) -> None:
    expected = rule.get("x-pullwise-content-schema-id")
    allowed = rule.get("x-pullwise-content-schema-ids")
    if expected is not None or allowed is not None:
        targets = [expected] if expected is not None else allowed
        if not isinstance(value, dict) or value.get("content_schema_id") not in targets:
            _fail("CONTENT_REF_SCHEMA_INVALID", path)
    expected = rule.get("x-pullwise-availability-content-schema-id")
    allowed = rule.get("x-pullwise-availability-content-schema-ids")
    if expected is not None or allowed is not None:
        targets = [expected] if expected is not None else allowed
        if (
            isinstance(value, dict)
            and value.get("availability") == "available"
            and (
                not isinstance(value.get("ref"), dict)
                or value["ref"].get("content_schema_id") not in targets
            )
        ):
            _fail("CONTENT_REF_SCHEMA_INVALID", f"{path}.ref")


def verify_content_ref_set(refs: object) -> list[dict[str, object]]:
    if not isinstance(refs, list):
        _fail("CONTRACT_TYPE_INVALID")
    validated = [validate_document("content-ref/v1", item) for item in refs]
    identities: dict[str, tuple[object, ...]] = {}
    fields = ("content_schema_id", "sha256", "size_bytes", "media_type", "encoding")
    for item in validated:
        identity = tuple(item[field] for field in fields)
        previous = identities.setdefault(item["artifact_id"], identity)
        if previous != identity:
            _fail("CONTENT_REF_CONFLICT", "$.artifact_id")
    return validated


def _validate_legacy_semantics(schema_id: str, value: dict[str, object]) -> None:
    if schema_id == "source-content/v1":
        try:
            raw = base64.b64decode(value["data_base64"], validate=True)
        except (ValueError, TypeError):
            _fail("SOURCE_CONTENT_BASE64_INVALID", "$.data_base64")
        if base64.b64encode(raw).decode("ascii") != value["data_base64"]:
            _fail("SOURCE_CONTENT_BASE64_NONCANONICAL", "$.data_base64")
        if len(raw) != value["size_bytes"]:
            _fail("SOURCE_CONTENT_SIZE_MISMATCH", "$.size_bytes")
        if hashlib.sha256(raw).hexdigest() != value["byte_sha256"]:
            _fail("SOURCE_CONTENT_SHA256_MISMATCH", "$.byte_sha256")
    elif schema_id == "elapsed-budget-ledger/v1":
        if value["consumed_ms"] + value["reserved_ms"] > value["elapsed_limit_ms"]:
            _fail("BUDGET_ELAPSED_LIMIT_EXCEEDED", code="BUDGET_EXHAUSTED")
        if value["calls_consumed"] + value["calls_reserved"] > value["tool_call_limit"]:
            _fail("BUDGET_CALL_LIMIT_EXCEEDED", code="BUDGET_EXHAUSTED")
    elif schema_id == "elapsed-budget-settlement/v1":
        if value["consumed_calls"] + value["released_calls"] != 1:
            _fail("BUDGET_CALL_CONSERVATION_INVALID")
    elif schema_id == "agent-claim-abandon-response/v1":
        grant = verify_document_digest("agent-worker-grant/v1", value["grant"])
        exact = ("package", "task_id", "attempt_id", "session_id", "owner_id",
                 "grant_id", "lease_id", "deletion_version", "owner_epoch",
                 "native_epoch", "transport_epoch")
        if any(value[key] != grant[key] for key in exact):
            _fail("AUTHORITY_FENCE_MISMATCH")
        if value["previous_task_version"] != grant["task_version"]:
            _fail("AUTHORITY_PREVIOUS_VERSION_MISMATCH")
        if value["task_version"] != value["previous_task_version"] + 1:
            _fail("AUTHORITY_SUCCESSOR_VERSION_INVALID")
    elif schema_id == "artifact-content-registry/v1":
        expected = [
            {
                "artifact_kind": "change_set",
                "content_schema_id": "change-set/v1",
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            {
                "artifact_kind": "change_set_patch",
                "content_schema_id": "change-set-patch/v1",
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            {
                "artifact_kind": "r0_read_result",
                "content_schema_id": "r0-read-result/v1",
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            {
                "artifact_kind": "source_content",
                "content_schema_id": "source-content/v1",
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            {
                "artifact_kind": "task_report",
                "content_schema_id": "task-report/v1",
                "media_type": "application/json",
                "encoding": "utf-8",
            },
        ]
        if value["entries"] != expected:
            _fail("ARTIFACT_CONTENT_REGISTRY_INVALID")
    elif schema_id == "artifact-content-ref/v1":
        registry = verify_document_digest(
            "artifact-content-registry/v1",
            fixture("publication_golden_artifact_registry")["document"],
        )
        entry = next(
            (
                item
                for item in registry["entries"]
                if item["artifact_kind"] == value["artifact_kind"]
            ),
            None,
        )
        ref = value["ref"]
        if entry is None or any(
            ref[key] != entry[key]
            for key in ("content_schema_id", "media_type", "encoding")
        ):
            _fail("ARTIFACT_CONTENT_TUPLE_INVALID")
    elif schema_id == "budget-summary/v1":
        if value["consumed_ms"] > value["elapsed_limit_ms"]:
            _fail("BUDGET_SUMMARY_ELAPSED_INVALID", code="BUDGET_EXHAUSTED")
        if value["calls_consumed"] > value["tool_call_limit"]:
            _fail("BUDGET_SUMMARY_CALLS_INVALID", code="BUDGET_EXHAUSTED")
    elif schema_id == "task-report/v1":
        if [item["section_id"] for item in value["sections"]] != sorted(
            item["section_id"] for item in value["sections"]
        ):
            _fail("TASK_REPORT_SECTION_ORDER_INVALID")
        for field, limit in (("title", 512), ("summary", 4096)):
            if len(value[field].encode("utf-8")) > limit:
                _fail("TASK_REPORT_UTF8_LIMIT_INVALID", f"$.{field}")
        for index, section in enumerate(value["sections"]):
            if len(section["title"].encode("utf-8")) > 512 or len(
                section["body"].encode("utf-8")
            ) > 65536:
                _fail("TASK_REPORT_UTF8_LIMIT_INVALID", f"$.sections[{index}]")
            verify_content_ref_set(section["evidence_refs"])
    elif schema_id == "waiver-event/v1":
        if value["issued_at"] >= value["expires_at"]:
            _fail("WAIVER_TIME_RANGE_INVALID", code="WAIVER_INVALID")


def verify_waiver_authorization(
    waiver: object, effective_policy: object, now: str
) -> dict[str, object]:
    return verify_waiver_event_authority(waiver, effective_policy, now)


def verify_budget_transition(
    previous_ledger: object,
    reservation: object,
    reserved_ledger: object,
    settlement: object,
    resulting_ledger: object,
) -> bool:
    before = verify_document_digest("elapsed-budget-ledger/v1", previous_ledger)
    held = verify_document_digest("elapsed-budget-reservation/v1", reservation)
    reserved = verify_document_digest("elapsed-budget-ledger/v1", reserved_ledger)
    settled = verify_document_digest("elapsed-budget-settlement/v1", settlement)
    after = verify_document_digest("elapsed-budget-ledger/v1", resulting_ledger)
    if held["task_id"] != before["task_id"]:
        _fail("BUDGET_TASK_MISMATCH")
    previous_fields = (
        ("previous_consumed_ms", "consumed_ms"),
        ("previous_reserved_ms", "reserved_ms"),
        ("previous_calls_consumed", "calls_consumed"),
        ("previous_calls_reserved", "calls_reserved"),
    )
    if any(held[left] != before[right] for left, right in previous_fields):
        _fail("BUDGET_PREVIOUS_STATE_MISMATCH")
    if before["consumed_ms"] + before["reserved_ms"] + held["reserved_ms"] > before["elapsed_limit_ms"]:
        _fail("BUDGET_ELAPSED_LIMIT_EXCEEDED", code="BUDGET_EXHAUSTED")
    if before["calls_consumed"] + before["calls_reserved"] + held["reserved_calls"] > before["tool_call_limit"]:
        _fail("BUDGET_CALL_LIMIT_EXCEEDED", code="BUDGET_EXHAUSTED")
    reserved_expected = {
        "task_id": before["task_id"],
        "grant_digest": before["grant_digest"],
        "elapsed_limit_ms": before["elapsed_limit_ms"],
        "tool_call_limit": before["tool_call_limit"],
        "consumed_ms": before["consumed_ms"],
        "reserved_ms": before["reserved_ms"] + held["reserved_ms"],
        "calls_consumed": before["calls_consumed"],
        "calls_reserved": before["calls_reserved"] + held["reserved_calls"],
    }
    if any(reserved[key] != item for key, item in reserved_expected.items()):
        _fail("BUDGET_RESERVED_LEDGER_MISMATCH")
    if settled["reservation_id"] != held["reservation_id"] or settled["invocation_digest"] != held["invocation_digest"]:
        _fail("BUDGET_SETTLEMENT_IDENTITY_MISMATCH")
    if settled["consumed_ms"] + settled["released_ms"] != held["reserved_ms"]:
        _fail("BUDGET_ELAPSED_CONSERVATION_INVALID")
    if settled["consumed_calls"] + settled["released_calls"] != held["reserved_calls"]:
        _fail("BUDGET_CALL_CONSERVATION_INVALID")
    if settled["outcome"] == "settled":
        if (settled["consumed_calls"], settled["released_calls"]) != (1, 0):
            _fail("BUDGET_SETTLED_CALL_INVALID")
        if settled["elapsed_ms"] > held["reserved_ms"] or settled["consumed_ms"] != settled["elapsed_ms"]:
            _fail("BUDGET_SETTLED_ELAPSED_INVALID")
        if settled["released_ms"] != held["reserved_ms"] - settled["elapsed_ms"]:
            _fail("BUDGET_SETTLED_RELEASE_INVALID")
    elif (
        settled["consumed_calls"] != 0
        or settled["released_calls"] != 1
        or settled["consumed_ms"] != 0
        or settled["released_ms"] != held["reserved_ms"]
    ):
        _fail("BUDGET_ABANDONMENT_RELEASE_INVALID")
    expected = {
        "resulting_consumed_ms": before["consumed_ms"] + settled["consumed_ms"],
        "resulting_reserved_ms": before["reserved_ms"],
        "resulting_calls_consumed": before["calls_consumed"] + settled["consumed_calls"],
        "resulting_calls_reserved": before["calls_reserved"],
    }
    if any(settled[key] != item for key, item in expected.items()):
        _fail("BUDGET_RESULTING_STATE_MISMATCH")
    after_expected = {
        "task_id": before["task_id"],
        "grant_digest": before["grant_digest"],
        "elapsed_limit_ms": before["elapsed_limit_ms"],
        "tool_call_limit": before["tool_call_limit"],
        "consumed_ms": expected["resulting_consumed_ms"],
        "reserved_ms": expected["resulting_reserved_ms"],
        "calls_consumed": expected["resulting_calls_consumed"],
        "calls_reserved": expected["resulting_calls_reserved"],
    }
    if any(after[key] != item for key, item in after_expected.items()):
        _fail("BUDGET_RESULTING_LEDGER_MISMATCH")
    return True
'''

PYTHON_SEMANTICS = "\n".join(
    (
        PYTHON_SEMANTICS_BASE,
        PYTHON_RULES,
        PYTHON_BUDGET,
        PYTHON_CONTROL,
        PYTHON_TASK_CONTROL_RULES,
        PYTHON_TASK_CONTROL_HELPERS,
        PYTHON_TOOL_EVIDENCE,
        PYTHON_PUBLICATION,
        PYTHON_QUALITY_POLICY,
        PYTHON_RESULT,
        PYTHON_PRE_GATE,
        PYTHON_TASK_EVIDENCE,
        PYTHON_GATE_INPUT,
        PYTHON_GATE,
        PYTHON_GATE_PREPARATION,
        PYTHON_DISPATCH,
    )
)


__all__ = ["PYTHON_SEMANTICS"]
