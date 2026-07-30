from __future__ import annotations

from copy import deepcopy
import hashlib

from tests.agent_first_bootstrap_support import golden_bootstrap


def content_ref(
    contract: object,
    artifact_id: str,
    schema_id: str,
    document: dict[str, object],
) -> dict[str, object]:
    raw = contract.canonical_document_bytes(document)
    return {
        "schema_id": "content-ref/v1",
        "artifact_id": artifact_id,
        "content_schema_id": schema_id,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
        "media_type": "application/json",
        "encoding": "utf-8",
    }


def placeholder_ref(
    artifact_id: str,
    schema_id: str,
    marker: str,
) -> dict[str, object]:
    return {
        "schema_id": "content-ref/v1",
        "artifact_id": artifact_id,
        "content_schema_id": schema_id,
        "sha256": marker * 64,
        "size_bytes": 1,
        "media_type": "application/json",
        "encoding": "utf-8",
    }


def golden_checkpoint_set(
    contract: object,
    *,
    generation: int = 1,
    previous: dict[str, object] | None = None,
) -> dict[str, dict[str, object]]:
    bootstrap = golden_bootstrap(contract)
    task = bootstrap["construction_roots"]["task_record"]
    attempt = bootstrap["construction_roots"]["attempt"]
    owner = bootstrap["construction_roots"]["owner"]
    accept = bootstrap["accept_request"]
    package = contract.package_tuple()
    task_version = (
        previous["committed_task_version"]
        if previous is not None
        else task["task_version"]
    )
    watermark = generation - 1
    created_at = f"2026-07-22T00:00:{generation + 1:02d}.000Z"

    machine = contract.seal_document(
        "machine-checkpoint/v1",
        {
            "schema_id": "machine-checkpoint/v1",
            "package": package,
            "task_id": task["task_id"],
            "generation": generation,
            "task_version": task_version,
            "attempt_id": attempt["attempt_id"],
            "native_epoch": attempt["native_epoch"],
            "owner_id": owner["owner_id"],
            "owner_epoch": owner["owner_epoch"],
            "session_id": owner["session_id"],
            "transport_binding": {
                "outer_job_id": task["outer_job_id"],
                "run_id": task["run_id"],
                "lease_id": task["lease_id"],
                "transport_epoch": task["transport_epoch"],
            },
            "runtime_thread_id": "thread-checkpoint-1",
            "workspace_state_ref": placeholder_ref(
                "art_10000000000000000000000000000001",
                "source-tree-manifest/v1",
                "1",
            ),
            "execution_state_ref": placeholder_ref(
                "art_10000000000000000000000000000002",
                "execution-state-manifest/v1",
                "2",
            ),
            "in_flight_tool_invocation_ids": [],
            "budget_watermark": watermark,
            "effect_watermark": 0,
            "observation_watermark": watermark,
            "event_seq": watermark,
            "created_at": created_at,
        },
    )
    semantic = contract.seal_document(
        "semantic-checkpoint/v1",
        {
            "schema_id": "semantic-checkpoint/v1",
            "package": package,
            "task_id": task["task_id"],
            "generation": generation,
            "task_version": task_version,
            "owner_id": owner["owner_id"],
            "owner_epoch": owner["owner_epoch"],
            "task_request_ref": content_ref(
                contract,
                "art_10000000000000000000000000000003",
                "task-request/v1",
                accept["task_request"],
            ),
            "charter_ref": None,
            "requirement_ledger_ref": content_ref(
                contract,
                "art_10000000000000000000000000000004",
                "requirement-ledger/v1",
                accept["requirement_ledger"],
            ),
            "owner_summary": {
                "objective_restated": accept["task_request"]["objective"],
                "completed_requirement_ids": [],
                "next_requirement_ids": accept["requirement_ledger"][
                    "active_requirement_ids"
                ],
                "unresolved_question_ids": [],
                "residual_risk_ids": [],
            },
            "pending_interaction_ids": [],
            "proposal_round": 0,
            "evidence_refs": [],
            "created_at": created_at,
        },
    )
    manifest = contract.seal_document(
        "committed-checkpoint-manifest/v1",
        {
            "schema_id": "committed-checkpoint-manifest/v1",
            "package": package,
            "task_id": task["task_id"],
            "generation": generation,
            "previous_generation": 0 if previous is None else previous["generation"],
            "previous_manifest_hash": (
                None if previous is None else previous["manifest_hash"]
            ),
            "committed_from_task_version": task_version,
            "committed_task_version": task_version + 1,
            "native_epoch": attempt["native_epoch"],
            "attempt_id": attempt["attempt_id"],
            "owner_epoch": owner["owner_epoch"],
            "machine_state_ref": content_ref(
                contract,
                "art_10000000000000000000000000000005",
                "machine-checkpoint/v1",
                machine,
            ),
            "semantic_state_ref": content_ref(
                contract,
                "art_10000000000000000000000000000006",
                "semantic-checkpoint/v1",
                semantic,
            ),
            "budget_watermark": machine["budget_watermark"],
            "effect_watermark": machine["effect_watermark"],
            "observation_watermark": machine["observation_watermark"],
            "event_seq": machine["event_seq"],
            "created_at": created_at,
        },
    )
    return {"machine": machine, "semantic": semantic, "manifest": manifest}


def reseal_adversarial(
    contract: object,
    schema_id: str,
    document: dict[str, object],
) -> dict[str, object]:
    result = deepcopy(document)
    spec = contract.schema(schema_id)["x-pullwise-digest"]
    field = spec["field"]
    unsigned = {key: value for key, value in result.items() if key != field}
    result[field] = hashlib.sha256(
        spec["domain"].encode("utf-8")
        + b"\0"
        + contract.canonical_document_bytes(unsigned)
    ).hexdigest()
    return result
