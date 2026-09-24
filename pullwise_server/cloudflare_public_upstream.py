"""Trusted local mapping of bounded public GitHub repository resolution."""
from __future__ import annotations

from typing import Any


def lookup_key(owner: str, repository: str) -> str:
    if (not isinstance(owner, str) or not isinstance(repository, str)
            or not owner or not repository or "/" in owner or "/" in repository
            or owner.strip() != owner or repository.strip() != repository):
        raise ValueError("invalid upstream coordinates")
    return f"{owner.casefold()}/{repository.casefold()}"


class D1PublicUpstreamProofs:
    def __init__(self, binding: Any) -> None:
        self.binding = binding

    async def stage(self, *, owner: str, repository: str, github_repo_id: str,
                    full_name: str, public_visible: bool, private: bool,
                    source_revision: int, observed_at: int, valid_until: int) -> None:
        key = lookup_key(owner, repository)
        if (not isinstance(github_repo_id, str) or not github_repo_id
                or not isinstance(full_name, str) or not full_name
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
