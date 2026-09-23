"""Read authorized Sources and their publication fences in one D1 snapshot."""
from __future__ import annotations

import json
from typing import Any

from .product_dto_rules import source_context_dto, source_record_dto
from .update_filter import project_saved_updates


class D1SourceReads:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def list_sources_for_billing_owner(self, *, owner_id: str, now: int,
                                             include_content: bool = False,
                                             source_id: str | None = None) -> list[dict]:
        if not isinstance(owner_id, str) or not owner_id or type(now) is not int:
            raise ValueError("invalid source read identity")
        statements = [
            self.binding.prepare("""SELECT sr.*,sc.context_id,sc.watch_id,sc.item_id,
                uw.target_repository_id,sc.context_version,sc.processing_status,
                sc.analysis_enabled,sc.context_stale,sc.coverage_json,
                CASE WHEN ? THEN sv.content_json END AS content_json
                FROM source_records sr
                JOIN source_versions sv ON sv.id=sr.latest_version
                JOIN source_contexts sc ON sc.source_id=sr.source_id
                LEFT JOIN update_watches uw ON uw.id=sc.watch_id
                WHERE sc.billing_owner_id=? AND sc.accessible=1
                AND (? IS NULL OR sr.source_id=?)
                AND sc.authorization_valid_until>=?
                ORDER BY sr.updated_at DESC,sr.source_id,sc.context_id""").bind(
                    int(include_content), owner_id, source_id, source_id, now),
            self.binding.prepare("""SELECT publication.* FROM source_assessment_publications publication
                JOIN source_contexts context ON context.source_id=publication.source_id
                    AND context.context_id=publication.context_id
                WHERE context.billing_owner_id=? AND context.accessible=1
                AND context.authorization_valid_until>=?""").bind(owner_id, now),
            self.binding.prepare("""SELECT DISTINCT source.source_id,source.latest_version,
                source.source_revision FROM source_records source
                JOIN source_contexts context ON context.source_id=source.source_id
                WHERE context.billing_owner_id=?""").bind(owner_id),
            self.binding.prepare("""SELECT source_id,context_id,accessible,context_stale,
                authorization_valid_until,billing_owner_id,context_version,
                authorization_revision FROM source_contexts
                WHERE billing_owner_id=?""").bind(owner_id),
        ]
        snapshot = await self.binding.batch(statements)
        rows, publications, source_versions, context_versions = (
            result.results for result in snapshot)
        publication_by_key = {(row["source_id"], row["context_id"]): row
                              for row in publications}
        source_by_id = {row["source_id"]: row for row in source_versions}
        context_by_key = {(row["source_id"], row["context_id"]): row
                          for row in context_versions}
        grouped = {}
        for row in rows:
            key = row["source_id"]
            if key not in grouped:
                grouped[key] = (row, [])
            context = source_context_dto(row)
            publication = publication_by_key.get((key, row["context_id"]))
            current = publication is not None and not context["contextStale"]
            if current:
                for dependency in json.loads(publication["sources_json"]):
                    stored = source_by_id.get(dependency["sourceId"])
                    if (stored is None or stored["latest_version"] != dependency["sourceVersion"]
                            or stored["source_revision"] != dependency["sourceRevision"]):
                        current = False
                for fence in json.loads(publication["fences_json"]):
                    stored = context_by_key.get((fence["sourceId"], fence["contextId"]))
                    if (stored is None or not stored["accessible"] or stored["context_stale"]
                            or stored["authorization_valid_until"] < now
                            or stored["billing_owner_id"] != owner_id
                            or stored["context_version"] != fence["contextVersion"]
                            or stored["authorization_revision"] != fence["authorizationRevision"]):
                        current = False
            public_assessment = json.loads(publication["assessment_json"]) if current else None
            if current:
                context["coverage"] = json.loads(publication["coverage_json"])
            if row["source_type"] == "release":
                projection = project_saved_updates(public_assessment, context["coverage"]) if public_assessment else None
                context["relevance"] = projection["relevance"] if projection else None
                context["updateSignals"] = projection["updateSignals"] if projection else {}
            if include_content:
                context["assessments"] = [json.loads(publication["assessment_json"])] if current else []
                context["evidence"] = json.loads(publication["evidence_json"]) if current else []
            grouped[key][1].append(context)
        result = []
        for row, contexts in grouped.values():
            source = source_record_dto(row, contexts)
            if include_content:
                source["content"] = json.loads(row["content_json"])
            result.append(source)
        return result
