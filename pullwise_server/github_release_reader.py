"""Bounded read-only Releases adapter; network and credentials are injected.

GitHub's Releases REST representation has no authoritative release updated_at.
Only published_at is retained for discovery eligibility; created_at is NOT an
edit time. Polling re-reads pages, including old releases, without a timestamp
cutoff. Old edits update facts but cannot claim a new automatic eligibility time.
Page omission and 404 never prove deletion. This module does not schedule work.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from .github_sources import release_source
from .github_transport import GitHubResponse, GitHubUnavailable
from .product_discovery import FactPage


_ID = re.compile(r"[1-9][0-9]{0,19}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}\Z")


def _id(value: object) -> str:
    if type(value) not in (str, int) or not _ID.fullmatch(str(value)):
        raise ValueError("GITHUB_RELEASE_BINDING_INVALID")
    return str(value)


def _time(value: object) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) > 40:
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("GITHUB_RELEASE_RESPONSE_INVALID") from None


class GitHubReleaseReader:
    def __init__(self, *, get_json: Callable, token_for_target: Callable,
                 page_size: int = 100):
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise ValueError("GITHUB_RELEASE_PAGE_SIZE_INVALID")
        self.get_json = get_json
        self.token_for_target = token_for_target
        self.page_size = page_size

    def _get(self, path: str, token: str) -> GitHubResponse:
        try:
            response = self.get_json(path, token=token)
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_RELEASE_UNAVAILABLE") from None
        if not isinstance(response, GitHubResponse) or response.status != 200:
            raise GitHubUnavailable("GITHUB_RELEASE_UNAVAILABLE")
        return response

    @staticmethod
    def _repository(payload: object, repository_id: str) -> str:
        if (not isinstance(payload, Mapping) or type(payload.get("id")) is not int
                or str(payload["id"]) != repository_id or type(payload.get("private")) is not bool
                or not isinstance(payload.get("full_name"), str)
                or not _NAME.fullmatch(payload["full_name"])
                or any(part in {".", ".."} for part in payload["full_name"].split("/"))):
            raise ValueError("GITHUB_RELEASE_RESPONSE_INVALID")
        return payload["full_name"]

    def _next_page(self, headers: Mapping, path: str, repository_id: str, page: int) -> int | None:
        if not isinstance(headers, Mapping):
            raise ValueError("GITHUB_RELEASE_PAGINATION_INVALID")
        link = headers.get("link", "")
        if not isinstance(link, str) or len(link) > 8192:
            raise ValueError("GITHUB_RELEASE_PAGINATION_INVALID")
        next_page = None
        for part in link.split(","):
            if not part.strip():
                continue
            match = re.fullmatch(r'\s*<([^<>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
            if match is None:
                raise ValueError("GITHUB_RELEASE_PAGINATION_INVALID")
            if match[2] != "next":
                continue
            if next_page is not None:
                raise ValueError("GITHUB_RELEASE_PAGINATION_INVALID")
            try:
                url = urlsplit(match[1])
                query = parse_qs(url.query, strict_parsing=True, keep_blank_values=True)
                if (url.scheme != "https" or url.netloc != "api.github.com"
                        or url.path not in {path, f"/repositories/{repository_id}/releases"}
                        or url.fragment or set(query) != {"page", "per_page"}
                        or query["per_page"] != [str(self.page_size)]
                        or len(query["page"]) != 1 or query["page"][0] != str(page + 1)):
                    raise ValueError
            except ValueError:
                raise ValueError("GITHUB_RELEASE_PAGINATION_INVALID") from None
            next_page = page + 1
        if next_page is not None and next_page > 2**31 - 1:
            raise ValueError("GITHUB_RELEASE_PAGINATION_INVALID")
        return next_page

    def read_page(self, *, target: Mapping, cursor: str | None,
                  high_watermark: str | None, event: Mapping | None) -> FactPage:
        if target.get("module") != "updates" or not isinstance(target.get("control_key"), str):
            raise ValueError("GITHUB_RELEASE_BINDING_INVALID")
        repository_id = _id(target.get("github_repository_id"))
        if not isinstance(target.get("repository_id"), str) or not target["repository_id"]:
            raise ValueError("GITHUB_RELEASE_BINDING_INVALID")
        watermark = _time(high_watermark) if high_watermark else ""
        scope = hashlib.sha256(json.dumps([target["control_key"], target["repository_id"], repository_id,
                                          self.page_size], separators=(",", ":")).encode()).hexdigest()
        page_number = 1
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError
                saved = json.loads(cursor)
                if (not isinstance(saved, dict) or set(saved) != {"scope", "page", "watermark"}
                        or saved["scope"] != scope or saved["watermark"] != watermark
                        or type(saved["page"]) is not int or not 2 <= saved["page"] <= 2**31 - 1):
                    raise ValueError
                page_number = saved["page"]
            except (ValueError, TypeError):
                raise ValueError("GITHUB_RELEASE_CURSOR_INVALID") from None
        resource_id = None
        if event is not None:
            if (cursor is not None or event.get("event") != "release"
                    or _id(event.get("repository_id")) != repository_id):
                raise ValueError("GITHUB_RELEASE_BINDING_INVALID")
            resource_id = _id(event.get("resource_id"))
        try:
            token = self.token_for_target(dict(target))
        except GitHubUnavailable:
            raise
        except Exception:
            raise GitHubUnavailable("GITHUB_RELEASE_UNAVAILABLE") from None
        if not isinstance(token, str):
            raise GitHubUnavailable("GITHUB_RELEASE_UNAVAILABLE")
        repository = self._get(f"/repositories/{repository_id}", token).payload
        full_name = self._repository(repository, repository_id)
        if target.get("target_repository_id") is not None and repository["private"]:
            raise GitHubUnavailable("GITHUB_RELEASE_UNAVAILABLE")
        path = f"/repos/{full_name}/releases"
        response = self._get(f"{path}/{resource_id}" if resource_id else
                             f"{path}?per_page={self.page_size}&page={page_number}", token)
        records = [response.payload] if resource_id else response.payload
        if not isinstance(records, list) or len(records) > self.page_size:
            raise ValueError("GITHUB_RELEASE_RESPONSE_INVALID")
        sources = []
        ids = set()
        for record in records:
            if (not isinstance(record, Mapping) or type(record.get("id")) is not int or record["id"] < 1
                    or record["id"] in ids or (resource_id and str(record["id"]) != resource_id)
                    or type(record.get("draft")) is not bool or type(record.get("prerelease")) is not bool
                    or not isinstance(record.get("tag_name"), str) or not record["tag_name"]
                    or any(record.get(field) is not None and not isinstance(record[field], str) for field in ("name", "body"))
                    or not isinstance(record.get("html_url"), str)
                    or not record["html_url"].startswith(f"https://github.com/{full_name}/releases/")):
                raise ValueError("GITHUB_RELEASE_RESPONSE_INVALID")
            ids.add(record["id"])
            published = _time(record.get("published_at"))
            source = release_source(upstream_repository_id=target["repository_id"],
                                    release=dict(record, published_at=published, updated_at=None))
            sources.append(source)
            if published and (not watermark or datetime.fromisoformat(published.replace("Z", "+00:00")) > datetime.fromisoformat(watermark.replace("Z", "+00:00"))):
                watermark = published
        next_page = None if resource_id else self._next_page(response.headers, path, repository_id, page_number)
        # Names are mutable. Recheck the name route after fetching its records.
        after = self._get(f"/repos/{full_name}", token).payload
        if self._repository(after, repository_id) != full_name or after["private"] != repository["private"]:
            raise ValueError("GITHUB_RELEASE_RESPONSE_INVALID")
        next_cursor = None if next_page is None else json.dumps(
            {"scope": scope, "page": next_page, "watermark": watermark}, separators=(",", ":"))
        return FactPage(tuple(sources), next_cursor, watermark)
