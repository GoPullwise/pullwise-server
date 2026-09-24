"""Trusted, complete GitHub App directory publication for candidate reads."""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable


class D1RepositoryDirectory:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def collect_and_publish(self, *, owner_id: str, account_snapshot: str,
                                  fetch_page: Callable[[str | None], Awaitable[dict]],
                                  source_revision: int, observed_at: int) -> None:
        """Consume an injected trusted App discovery before one atomic publish."""
        items: list[dict] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        expected_count: int | None = None
        for _ in range(10):
            page = await fetch_page(cursor)
            if not isinstance(page, dict) or page.get("ownerId") != owner_id:
                raise ValueError("repository discovery owner changed")
            count = page.get("expectedCount")
            current = page.get("items")
            if (type(count) is not int or count < 0 or count > 500
                    or not isinstance(current, list)
                    or (expected_count is not None and count != expected_count)):
                raise ValueError("repository discovery is incomplete")
            expected_count = count
            items.extend(current)
            if len(items) > count:
                raise ValueError("repository discovery exceeded its total")
            if page.get("hasMore") is False:
                if page.get("nextCursor") is not None or len(items) != count:
                    raise ValueError("repository discovery ended early")
                await self.publish(owner_id=owner_id, account_snapshot=account_snapshot,
                    items=items, expected_count=count, complete=True,
                    source_revision=source_revision, observed_at=observed_at,
                    valid_until=observed_at + 300)
                return
            next_cursor = page.get("nextCursor")
            if (page.get("hasMore") is not True or not isinstance(next_cursor, str)
                    or not next_cursor or next_cursor in seen_cursors
                    or not current or len(items) >= count):
                raise ValueError("repository discovery cursor is invalid")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        raise ValueError("repository discovery exceeded page limit")

    async def publish(self, *, owner_id: str, account_snapshot: str,
                      items: list[dict], expected_count: int, complete: bool,
                      source_revision: int, observed_at: int, valid_until: int) -> None:
        account = json.loads(account_snapshot)
        access = account.get("githubRepositoryAccess") if isinstance(account, dict) else None
        if (not complete or not isinstance(account, dict) or account.get("id") != owner_id
                or not isinstance(access, dict) or access.get("mode") != "github-app"
                or access.get("authorizedUserId") != owner_id
                or access.get("repositoriesNeedSync") is True
                or not isinstance(items, list) or len(items) != expected_count
                or expected_count < 0 or expected_count > 500
                or valid_until <= observed_at or valid_until - observed_at > 300
                or source_revision < 1):
            raise ValueError("incomplete or invalid repository directory")
        ids = set()
        for item in items:
            if (not isinstance(item, dict) or not all(isinstance(item.get(field), str)
                    and item[field] for field in ("id", "githubRepoId", "fullName",
                                                   "installationId", "appId"))
                    or not isinstance(item.get("private"), bool)
                    or item.get("appAccessible") is not True
                    or item["id"] in ids):
                raise ValueError("invalid repository directory item")
            ids.add(item["id"])
        encoded = json.dumps(items, separators=(",", ":"), sort_keys=True)
        commands = [
            self.binding.prepare("""INSERT INTO d1_command_guard VALUES(CASE WHEN
                EXISTS(SELECT 1 FROM app_state a,json_each(a.payload) u
                    WHERE a.name='users' AND u.key=? AND u.value=?)
                AND NOT EXISTS(SELECT 1 FROM repository_directory
                    WHERE owner_id=? AND source_revision>=?)
                THEN 1 ELSE 0 END)""").bind(owner_id, account_snapshot,
                                              owner_id, source_revision),
        ]
        commands.extend([
            self.binding.prepare("""INSERT INTO repository_directory
                (owner_id,account_snapshot,source_revision,observed_at,valid_until,item_count,items_json)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_id) DO UPDATE SET
                account_snapshot=excluded.account_snapshot,
                source_revision=excluded.source_revision,
                observed_at=excluded.observed_at,valid_until=excluded.valid_until,
                item_count=excluded.item_count,items_json=excluded.items_json""").bind(
                    owner_id, account_snapshot, source_revision, observed_at,
                    valid_until, expected_count, encoded),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])
        await self.binding.batch(commands)
