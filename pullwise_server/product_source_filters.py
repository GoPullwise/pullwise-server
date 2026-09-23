"""Source list filters and API-key resource restrictions shared by REST runtimes."""
from __future__ import annotations

from typing import Mapping


def _query_value(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value or "").strip()


def _source_module(source: Mapping[str, object]) -> str:
    source_type = source.get("type")
    if source_type == "ci_failure":
        return "ci"
    if source_type == "release":
        return "updates"
    return "pr"


def filter_sources(sources: list[dict], params: Mapping[str, object]) -> list[dict]:
    module = _query_value(params, "module")
    repository_id = _query_value(params, "repositoryId")
    watch_id = _query_value(params, "watchId")
    relevance = _query_value(params, "relevance")
    signal = _query_value(params, "updateSignal")
    processing = _query_value(params, "processingStatus")
    if ((relevance or signal) and module != "updates"
            or relevance and relevance not in {"relevant", "not_relevant", "unclear"}
            or signal and signal not in {"migration_stated", "deprecation_stated", "breaking_change_stated", "security_fix_stated"}):
        raise ValueError("INVALID_CONFIGURATION")
    result = []
    for source in sources:
        if module and _source_module(source) != module:
            continue
        if repository_id and source.get("repositoryId") != repository_id:
            continue
        contexts = [context for context in source.get("contexts") or []
                    if (not watch_id or context.get("watchId") == watch_id)
                    and (not relevance or context.get("relevance") == relevance)
                    and (not signal or context.get("updateSignals", {}).get(signal) == "present")
                    and (not processing or context.get("processingStatus") == processing)]
        if contexts:
            result.append({**source, "contexts": contexts})
    return result


def apply_source_restrictions(sources: list[dict], restrictions: Mapping[str, object]) -> list[dict]:
    repository_ids = restrictions.get("repositoryIds")
    watch_ids = restrictions.get("watchIds")
    if repository_ids is None and watch_ids is None:
        return sources
    allowed_repositories = set(repository_ids or ())
    allowed_watches = set(watch_ids or ())

    def allowed(source, context):
        if not context.get("watchId"):
            return source.get("repositoryId") in allowed_repositories
        target = context.get("targetRepositoryId")
        if target:
            return ((repository_ids is None or target in allowed_repositories)
                    and (watch_ids is None or context["watchId"] in allowed_watches))
        return context["watchId"] in allowed_watches

    result = []
    for source in sources:
        contexts = [context for context in source.get("contexts") or []
                    if allowed(source, context)]
        if contexts:
            result.append({**source, "contexts": contexts})
    return result
