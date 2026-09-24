"""Shared Item view and API-key resource filters."""
from __future__ import annotations

from typing import Mapping


CI_STAGES = ("dependency_install", "build", "test", "deploy", "runtime", "unknown")
CI_SYMPTOMS = ("connection_timeout", "name_resolution_failure",
    "authentication_denied", "authorization_denied", "assertion_failure",
    "syntax_or_type_error", "package_resolution_failure",
    "resource_exhausted", "configuration_error")


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
    ci_stage = _query_value(params, "ciStage")
    ci_symptom = _query_value(params, "ciSymptom")
    classification_state = _query_value(params, "classificationState")
    query = _query_value(params, "q")
    view = _query_value(params, "view") or "all"
    if view not in {"mine", "unassigned", "waiting", "all"}:
        raise ValueError("INVALID_VIEW")
    if (pull_number and (module != "pr" or not repository_id or not pull_number.isdigit()
                         or int(pull_number) < 1)
            or run_id and (module != "ci" or not repository_id or not run_id.isdigit()
                           or int(run_id) < 1)
            or (ci_stage or ci_symptom or classification_state) and module != "ci"
            or ci_stage and ci_stage not in CI_STAGES
            or ci_symptom and ci_symptom not in CI_SYMPTOMS
            or classification_state and classification_state not in {"identified", "unclassified"}):
        raise ValueError("INVALID_CONFIGURATION")
    if len(query) > 200 or any(ord(character) < 32 for character in query):
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
        if query:
            searchable = (item.get("title"), item.get("id"), item.get("sourceUrl"),
                facts.get("pullNumber"), facts.get("runId"), facts.get("jobId"),
                facts.get("tagName"), facts.get("name")) if isinstance(facts, Mapping) else (
                item.get("title"), item.get("id"), item.get("sourceUrl"))
            if not any(query.casefold() in str(value).casefold()
                       for value in searchable if value is not None):
                continue
        if ci_stage or ci_symptom or classification_state:
            windows = facts.get("windows") if isinstance(facts, Mapping) else None
            windows = windows if isinstance(windows, list) else []
            pairs = [(window.get("stage"), symptom)
                     for window in windows if isinstance(window, Mapping)
                     for symptom in (window.get("symptoms") or ())
                     if isinstance(symptom, str) and symptom in CI_SYMPTOMS]
            if classification_state == "identified" and not pairs:
                continue
            if classification_state == "unclassified" and pairs:
                continue
            if ci_stage or ci_symptom:
                if not any(isinstance(window, Mapping)
                           and (not ci_stage or window.get("stage") == ci_stage)
                           and (not ci_symptom or ci_symptom in (window.get("symptoms") or ()))
                           for window in windows):
                    continue
        if pull_number and str(facts.get("pullNumber") or "") != pull_number:
            continue
        if run_id and str(facts.get("runId") or "") != run_id:
            continue
        if include_view and not item_in_view(item, view, user_id):
            continue
        result.append(item)
    return result
