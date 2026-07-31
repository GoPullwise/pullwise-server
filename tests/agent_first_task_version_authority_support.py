from __future__ import annotations

from copy import deepcopy


def _artifact_id(index: int) -> str:
    return "art_" + f"{0x7000 + index:032x}"


def build_transport_version_authority_proof(
    owner: object,
    authority: dict[str, object],
    full_fence: dict[str, object],
    task_result: dict[str, object],
) -> dict[str, object]:
    """Build deterministic test evidence for one local terminal publication."""
    base_version = authority["task_version"]
    published_version = task_result["published_from_version"]
    terminal_version = task_result["terminal_task_version"]
    if (
        not isinstance(base_version, int)
        or not isinstance(published_version, int)
        or published_version <= base_version
        or terminal_version != published_version + 1
    ):
        raise ValueError("TASK_VERSION_AUTHORITY_TEST_INPUT_INVALID")

    ref_index = 1

    def reference(
        schema_id: str, marker: dict[str, object]
    ) -> dict[str, object]:
        nonlocal ref_index
        result = owner.content_ref(
            _artifact_id(ref_index), schema_id, marker
        )
        ref_index += 1
        return result

    chain: list[dict[str, object]] = []
    previous_version = base_version
    while previous_version + 1 < published_version:
        task_version = previous_version + 1
        chain.append(
            {
                "transition_kind": "checkpoint",
                "previous_task_version": previous_version,
                "task_version": task_version,
                "transition_ref": reference(
                    "committed-checkpoint-manifest/v1",
                    {"checkpoint_task_version": task_version},
                ),
                "task_record_ref": reference(
                    "task-record/v1", {"task_version": task_version}
                ),
            }
        )
        previous_version = task_version
    chain.append(
        {
            "transition_kind": "terminalization_requested",
            "previous_task_version": previous_version,
            "task_version": published_version,
            "transition_ref": reference(
                "task-control-event/v1",
                {"event_kind": "terminalization_requested"},
            ),
            "task_record_ref": reference(
                "task-record/v1", {"task_version": published_version}
            ),
        }
    )
    chain.append(
        {
            "transition_kind": "task_result_published",
            "previous_task_version": published_version,
            "task_version": terminal_version,
            "transition_ref": reference(
                "task-control-event/v1",
                {"event_kind": "task_result_published"},
            ),
            "task_record_ref": reference(
                "task-record/v1", {"task_version": terminal_version}
            ),
        }
    )
    return owner.reseal(
        "task-version-authority-proof/v1",
        {
            "schema_id": "task-version-authority-proof/v1",
            "package": deepcopy(authority["package"]),
            "task_id": task_result["task_id"],
            "authority_digest": authority["authority_digest"],
            "grant_digest": authority["grant"]["grant_digest"],
            "full_fence": deepcopy(full_fence),
            "base_task_record_ref": reference(
                "task-record/v1", {"task_version": base_version}
            ),
            "version_chain": chain,
            "published_from_version": published_version,
            "terminal_task_version": terminal_version,
            "task_result_ref": reference("task-result/v1", task_result),
        },
    )


__all__ = ["build_transport_version_authority_proof"]
