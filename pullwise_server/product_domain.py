from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping


_HANDLING_DISPOSITIONS = frozenset({"open", "done", "dismissed"})
_UPDATE_RELEVANCE = frozenset({"relevant", "not_relevant", "unclear"})
_UPDATE_SIGNAL_ANSWERS = frozenset({"present", "absent", "unclear"})
_UPDATE_SIGNAL_FIELDS = {
    "migration": "migration_stated",
    "deprecation": "deprecation_stated",
    "breaking_change": "breaking_change_stated",
    "security_fix": "security_fix_stated",
}


def _required_identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _canonical_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_interests(interests: Iterable[str]) -> tuple[str, ...]:
    if isinstance(interests, str):
        raise ValueError("interests must be a collection of strings")
    normalized: set[str] = set()
    for interest in interests:
        if not isinstance(interest, str):
            raise ValueError("each interest must be a string")
        text = " ".join(interest.split())
        if text:
            normalized.add(text)
    return tuple(sorted(normalized))


def validate_watch_interests(interests: Iterable[str]) -> tuple[str, ...]:
    normalized = normalize_interests(interests)
    if not 1 <= len(normalized) <= 20:
        raise ValueError("watch interests must contain 1..20 unique topics")
    if sum(len(interest.encode("utf-8")) for interest in normalized) > 2048:
        raise ValueError("watch interests must not exceed 2048 UTF-8 bytes")
    return normalized


def watch_scope_key(
    *,
    owner_id: str,
    target_repository_id: str | None,
    upstream_repository_id: str,
) -> str:
    upstream = _required_identifier(upstream_repository_id, "upstream_repository_id")
    if target_repository_id is None:
        scope = {"kind": "personal", "ownerId": _required_identifier(owner_id, "owner_id")}
    else:
        scope = {
            "kind": "repository",
            "repositoryId": _required_identifier(target_repository_id, "target_repository_id"),
        }
    return f"uws_v1_{_canonical_hash({'scope': scope, 'upstreamRepositoryId': upstream})}"


def context_hash(interests: Iterable[str]) -> str:
    return f"ctx_v1_{_canonical_hash({'interests': normalize_interests(interests)})}"


@dataclass(frozen=True)
class WatchContextState:
    context_version: int
    context_hash: str
    interests: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            isinstance(self.context_version, bool)
            or not isinstance(self.context_version, int)
            or self.context_version < 1
        ):
            raise ValueError("context_version must be a positive integer")
        if not isinstance(self.context_hash, str) or not self.context_hash:
            raise ValueError("context_hash must be a non-empty string")
        if self.interests != normalize_interests(self.interests):
            raise ValueError("interests must already be normalized")


def advance_watch_context(
    current: WatchContextState | None,
    interests: Iterable[str],
) -> WatchContextState:
    normalized = validate_watch_interests(interests)
    semantic_hash = context_hash(normalized)
    if current is not None and current.context_hash == semantic_hash:
        return current
    return WatchContextState(
        context_version=1 if current is None else current.context_version + 1,
        context_hash=semantic_hash,
        interests=normalized,
    )


@dataclass(frozen=True)
class ItemVersionState:
    item_version: int
    snapshot_hash: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.item_version, bool)
            or not isinstance(self.item_version, int)
            or self.item_version < 1
        ):
            raise ValueError("item_version must be a positive integer")
        if not isinstance(self.snapshot_hash, str) or not self.snapshot_hash:
            raise ValueError("snapshot_hash must be a non-empty string")


def advance_item_version(
    current: ItemVersionState | None,
    *,
    snapshot: Mapping[str, object],
) -> ItemVersionState:
    if not isinstance(snapshot, Mapping):
        raise ValueError("snapshot must be an object")
    snapshot_hash = f"ivs_v1_{_canonical_hash(snapshot)}"
    if current is not None and current.snapshot_hash == snapshot_hash:
        return current
    return ItemVersionState(
        item_version=1 if current is None else current.item_version + 1,
        snapshot_hash=snapshot_hash,
    )


@dataclass(frozen=True)
class HandlingSnapshot:
    disposition: str
    item_version: int
    carried_from_item_version: int | None = None

    def __post_init__(self) -> None:
        if self.disposition not in _HANDLING_DISPOSITIONS:
            raise ValueError("invalid handling disposition")
        if (
            isinstance(self.item_version, bool)
            or not isinstance(self.item_version, int)
            or self.item_version < 1
        ):
            raise ValueError("item_version must be a positive integer")
        carried = self.carried_from_item_version
        if carried is not None and (
            isinstance(carried, bool)
            or not isinstance(carried, int)
            or carried < 1
            or carried >= self.item_version
        ):
            raise ValueError("carried_from_item_version must identify an earlier version")


def carry_handling(
    previous: HandlingSnapshot,
    *,
    previous_action_signature: str,
    next_action_signature: str,
    matching_predecessors: int,
    context_changed: bool,
    new_rule_action: bool,
    next_item_version: int,
) -> HandlingSnapshot:
    if isinstance(matching_predecessors, bool) or matching_predecessors < 0:
        raise ValueError("matching_predecessors must be a non-negative integer")
    if next_item_version != previous.item_version + 1:
        raise ValueError("handling can only be considered across adjacent item versions")
    can_carry = (
        previous.disposition in {"done", "dismissed"}
        and bool(previous_action_signature)
        and previous_action_signature == next_action_signature
        and matching_predecessors == 1
        and not context_changed
        and not new_rule_action
    )
    if not can_carry:
        return HandlingSnapshot(disposition="open", item_version=next_item_version)
    return HandlingSnapshot(
        disposition=previous.disposition,
        item_version=next_item_version,
        carried_from_item_version=previous.item_version,
    )


def formal_review_actions(*, review_state: str, body: str | None) -> tuple[str, ...]:
    del body
    if not isinstance(review_state, str):
        raise ValueError("review_state must be a string")
    return ("change_requested",) if review_state.strip().upper() == "CHANGES_REQUESTED" else ()


@dataclass(frozen=True)
class PendingItemProjection:
    attention_state: str
    attention_updated_at: str
    processing_status: str = "pending"


def project_existing_item_while_pending(
    *,
    previous_attention_state: str,
    previous_attention_updated_at: str,
) -> PendingItemProjection:
    if previous_attention_state not in {
        "needs_action",
        "needs_confirmation",
        "waiting",
        "optional",
    }:
        raise ValueError("pending projection requires an existing active item")
    timestamp = _required_identifier(previous_attention_updated_at, "previous_attention_updated_at")
    return PendingItemProjection(
        attention_state="needs_confirmation",
        attention_updated_at=timestamp,
    )


@dataclass(frozen=True)
class UpdateUnitAnswers:
    relevance: str
    migration_stated: str
    deprecation_stated: str
    breaking_change_stated: str
    security_fix_stated: str

    def __post_init__(self) -> None:
        if self.relevance not in _UPDATE_RELEVANCE:
            raise ValueError("invalid Updates relevance answer")
        for field_name in _UPDATE_SIGNAL_FIELDS.values():
            if getattr(self, field_name) not in _UPDATE_SIGNAL_ANSWERS:
                raise ValueError(f"invalid Updates signal answer: {field_name}")


@dataclass(frozen=True)
class UpdateUnitProjection:
    attention_state: str | None
    action_types: tuple[str, ...]
    update_signals: tuple[str, ...]
    release_relevance: str | None
    release_signal_states: Mapping[str, str | None]
    coverage_partial: bool


def project_update_unit(
    answers: UpdateUnitAnswers,
    *,
    coverage_complete: bool,
) -> UpdateUnitProjection:
    signal_states = {
        signal: getattr(answers, field_name)
        for signal, field_name in _UPDATE_SIGNAL_FIELDS.items()
    }
    positive_signals = tuple(signal for signal, answer in signal_states.items() if answer == "present")
    has_unclear_signal = any(answer == "unclear" for answer in signal_states.values())

    if answers.relevance == "not_relevant" and not positive_signals and not has_unclear_signal:
        attention_state = None
    elif answers.relevance == "relevant" and positive_signals:
        attention_state = "needs_action"
    elif answers.relevance == "relevant" and not has_unclear_signal:
        attention_state = "optional"
    else:
        attention_state = "needs_confirmation"

    conflict = answers.relevance != "relevant" and bool(positive_signals)
    release_signals: dict[str, str | None] = {}
    for signal, answer in signal_states.items():
        if conflict:
            release_signals[signal] = "unclear" if answer == "present" else answer
        elif not coverage_complete and answer == "absent":
            release_signals[signal] = None
        else:
            release_signals[signal] = answer

    return UpdateUnitProjection(
        attention_state=attention_state,
        action_types=("review_update",) if attention_state == "needs_action" else (),
        update_signals=() if conflict else positive_signals,
        release_relevance=(
            "relevant"
            if answers.relevance == "relevant"
            else "not_relevant"
            if coverage_complete and answers.relevance == "not_relevant" and not conflict
            else "unclear"
        ),
        release_signal_states=release_signals,
        coverage_partial=not coverage_complete,
    )
