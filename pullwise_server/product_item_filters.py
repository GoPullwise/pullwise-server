"""Shared Item view and API-key resource filters."""
from __future__ import annotations

from typing import Mapping


def _query_value(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def apply_item_restrictions(items: list[dict], restrictions: Mapping[str, object]) -> list[dict]:
    repository_ids = restrictions.get("repositoryIds")
    watch_ids = restrictions.get("watchIds")
    if repository_ids is None and watch_ids is None:
        return items
    allowed_repositories = set(repository_ids or ())
    allowed_watches = set(watch_ids or ())
    return [item for item in items if item.get("repositoryId") in allowed_repositories
            or item.get("watchId") in allowed_watches]


def item_in_view(item: Mapping[str, object], view: str, github_user_id: str) -> bool:
    if view == "all":
        return True
    attention = item.get("attentionState")
    handling = item.get("handling") if isinstance(item.get("handling"), Mapping) else {}
    next_actors = item.get("nextActors") if isinstance(item.get("nextActors"), list) else []
    assignee = handling.get("assigneeId")
    users = [actor.get("githubId") for actor in next_actors
             if isinstance(actor, Mapping) and actor.get("kind") == "user" and actor.get("githubId")]
    assigned_to_user = bool(github_user_id) and (assignee == github_user_id if assignee else github_user_id in users)
    has_actor = bool(assignee) or bool(users)
    if view == "mine":
        return attention in {"needs_action", "needs_confirmation"} and assigned_to_user
    if view == "unassigned":
        return attention in {"needs_action", "needs_confirmation"} and not has_actor
    if view == "waiting":
        return attention == "waiting" or (
            attention in {"needs_action", "needs_confirmation"} and has_actor and not assigned_to_user)
    return False


def filter_items(items: list[dict], params: Mapping[str, object], user_id: str,
                 *, include_view: bool) -> list[dict]:
    module = _query_value(params, "module")
    repository_id = _query_value(params, "repositoryId")
    watch_id = _query_value(params, "watchId")
    attention_state = _query_value(params, "attentionState")
    action_type = _query_value(params, "actionType")
    lifecycle = _query_value(params, "lifecycle")
    disposition = _query_value(params, "disposition")
    pull_number = _query_value(params, "pullNumber")
    run_id = _query_value(params, "runId")
    view = _query_value(params, "view") or "all"
    if view not in {"mine", "unassigned", "waiting", "all"}:
        raise ValueError("INVALID_VIEW")
    if (pull_number and (module != "pr" or not repository_id or not pull_number.isdigit()
                         or int(pull_number) < 1)
            or run_id and (module != "ci" or not repository_id or not run_id.isdigit()
                           or int(run_id) < 1)):
        raise ValueError("INVALID_CONFIGURATION")
    result = []
    for item in items:
        if module and item.get("module") != module:
            continue
        if repository_id and item.get("repositoryId") != repository_id:
            continue
        if watch_id and item.get("watchId") != watch_id:
            continue
        if attention_state and item.get("attentionState") != attention_state:
            continue
        if action_type and action_type not in (item.get("actionTypes") or []):
            continue
        if lifecycle and item.get("lifecycle") != lifecycle:
            continue
        if disposition and (item.get("handling") or {}).get("disposition") != disposition:
            continue
        facts = item.get("sourceFacts") or {}
        if pull_number and str(facts.get("pullNumber") or "") != pull_number:
            continue
        if run_id and str(facts.get("runId") or "") != run_id:
            continue
        if include_view and not item_in_view(item, view, user_id):
            continue
        result.append(item)
    return result
