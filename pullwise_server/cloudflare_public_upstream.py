"""Trusted local mapping of bounded public GitHub repository resolution."""
from __future__ import annotations

import re
from typing import Any, Awaitable, Callable


_PART = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")


def lookup_key(owner: str, repository: str) -> str:
    if (not isinstance(owner, str) or not isinstance(repository, str)
            or not _PART.fullmatch(owner) or not _PART.fullmatch(repository)):
        raise ValueError("invalid upstream coordinates")
    return f"{owner.casefold()}/{repository.casefold()}"


class D1PublicUpstreamProofs:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def refresh(self, *, owner: str, repository: str,
                      fetch_repository: Callable[[str, str], Awaitable[dict]],
                      source_revision: int, observed_at: int) -> str:
        """One trusted injected read; never follow redirects or retry here."""
        key = lookup_key(owner, repository)
        if (type(source_revision) is not int or source_revision < 1
                or type(observed_at) is not int or observed_at < 0):
            raise ValueError("invalid public upstream refresh")
        try:
            response = await fetch_repository(owner, repository)
        except Exception as error:
            await self.revoke(owner=owner, repository=repository,
                source_revision=source_revision, observed_at=observed_at)
            raise ValueError("UPSTREAM_PROOF_UNAVAILABLE") from error
        item = response.get("repository") if isinstance(response, dict) else None
        if (isinstance(response, dict) and type(response.get("status")) is int
                and response["status"] == 200 and isinstance(item, dict)
                and type(item.get("id")) is int and item["id"] > 0
                and isinstance(item.get("full_name"), str)
                and item["full_name"].casefold() == key
                and item.get("private") is False
                and item.get("visibility") == "public"):
            repo_id = f"github:{item['id']}"
            await self.stage(owner=owner, repository=repository,
                github_repo_id=repo_id, full_name=item["full_name"],
                public_visible=True, private=False,
                source_revision=source_revision, observed_at=observed_at,
                valid_until=observed_at + 300)
            return repo_id
        await self.revoke(owner=owner, repository=repository,
            source_revision=source_revision, observed_at=observed_at)
        raise ValueError("UPSTREAM_PROOF_UNAVAILABLE")

    async def revoke(self, *, owner: str, repository: str,
                     source_revision: int, observed_at: int) -> None:
        """A newer negative observation fences every older positive proof."""
        key = lookup_key(owner, repository)
        if (type(source_revision) is not int or source_revision < 1
                or type(observed_at) is not int or observed_at < 0):
            raise ValueError("invalid public upstream revocation")
        await self.binding.batch([
            self.binding.prepare("""INSERT INTO d1_command_guard VALUES(CASE WHEN
                NOT EXISTS(SELECT 1 FROM public_upstream_proofs
                  WHERE lookup_key=? AND source_revision>=?) THEN 1 ELSE 0 END)""").bind(
                    key, source_revision),
            self.binding.prepare("""INSERT INTO public_upstream_proofs
                (lookup_key,github_repo_id,full_name,public_visible,private,
                 source_revision,observed_at,valid_until) VALUES(?,?,?,0,1,?,?,?)
                ON CONFLICT(lookup_key) DO UPDATE SET
                public_visible=0,private=1,source_revision=excluded.source_revision,
                observed_at=excluded.observed_at,valid_until=excluded.valid_until""").bind(
                    key, "unverified", f"{owner}/{repository}",
                    source_revision, observed_at, observed_at),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])

    async def stage(self, *, owner: str, repository: str, github_repo_id: str,
                    full_name: str, public_visible: bool, private: bool,
                    source_revision: int, observed_at: int, valid_until: int) -> None:
        key = lookup_key(owner, repository)
        if (not isinstance(github_repo_id, str)
                or not re.fullmatch(r"github:[1-9][0-9]*", github_repo_id)
                or not isinstance(full_name, str) or full_name.casefold() != key
                or type(public_visible) is not bool or type(private) is not bool
                or not public_visible or private
                or type(source_revision) is not int or source_revision < 1
                or type(observed_at) is not int or type(valid_until) is not int
                or valid_until <= observed_at or valid_until - observed_at > 300):
            raise ValueError("invalid public upstream proof")
        await self.binding.batch([
            self.binding.prepare("""INSERT INTO d1_command_guard VALUES(CASE WHEN
                NOT EXISTS(SELECT 1 FROM public_upstream_proofs
                  WHERE lookup_key=? AND source_revision>=?) THEN 1 ELSE 0 END)""").bind(
                    key, source_revision),
            self.binding.prepare("""INSERT INTO public_upstream_proofs
                (lookup_key,github_repo_id,full_name,public_visible,private,
                 source_revision,observed_at,valid_until) VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(lookup_key) DO UPDATE SET
                github_repo_id=excluded.github_repo_id,full_name=excluded.full_name,
                public_visible=excluded.public_visible,private=excluded.private,
                source_revision=excluded.source_revision,
                observed_at=excluded.observed_at,valid_until=excluded.valid_until""").bind(
                    key, github_repo_id, full_name, 1, 0, source_revision,
                    observed_at, valid_until),
            self.binding.prepare("DELETE FROM d1_command_guard"),
        ])
