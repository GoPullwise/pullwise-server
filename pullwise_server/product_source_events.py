"""Conservative event labels for consecutive verified GitHub fact snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Mapping


def source_transition_events(source_type: str, previous: Mapping[str, object],
                             current: Mapping[str, object], *,
                             previous_content_hash: str = "",
                             current_content_hash: str = "",
                             previous_lifecycle: str = "active",
                             current_lifecycle: str = "active") -> tuple[str, ...]:
    """First observation and incomplete proof never imply a historical action."""
    if previous_lifecycle != "source_deleted" and current_lifecycle == "source_deleted":
        return ("source_deleted",)
    events = []
    if (source_type == "pr_review_comment"
            and previous.get("associationVerified") is True
            and current.get("associationVerified") is True
            and previous.get("threadCoverage") == "complete"
            and current.get("threadCoverage") == "complete"
            and isinstance(previous.get("threadId"), str)
            and previous["threadId"] == current.get("threadId")
            and type(previous.get("isResolved")) is bool
            and type(current.get("isResolved")) is bool
            and previous["isResolved"] != current["isResolved"]):
        events.append("thread_resolved" if current["isResolved"] else "thread_reopened")
    if (source_type in {"pr_comment", "pr_review_comment"}
            and previous_content_hash and current_content_hash
            and previous_content_hash != current_content_hash
            and _verified_later(previous.get("updatedAt"), current.get("updatedAt"))):
        events.append("comment_edited")
    if (source_type == "pr_state" and previous.get("state") == "open"
            and current.get("state") == "closed"):
        events.append("pr_merged" if current.get("mergedAt") else "pr_closed")
    if (source_type == "release" and previous.get("releaseId")
            and previous.get("releaseId") == current.get("releaseId")
            and _verified_later(previous.get("updatedAt"), current.get("updatedAt"))):
        events.append("release_edited")
    return tuple(events)


def source_transition_time(event_type: str, facts: Mapping[str, object]) -> int | None:
    field = {"comment_edited": "updatedAt", "release_edited": "updatedAt",
             "pr_closed": "closedAt", "pr_merged": "mergedAt"}.get(event_type)
    return _timestamp(facts.get(field)) if field else None


def _verified_later(before: object, after: object) -> bool:
    previous = _timestamp(before)
    current = _timestamp(after)
    return previous is not None and current is not None and current > previous


def _timestamp(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        utc = parsed.astimezone(timezone.utc)
        return int(utc.timestamp()) if utc.year >= 1970 else None
    except (ValueError, OverflowError):
        return None
