"""Async D1 transaction boundary for finite Server domain command lists.

Each list must be built by trusted Server code. D1 runs the prepared list as
one transaction; callers must not split a domain command across awaits.
"""
from __future__ import annotations

from typing import Any, Iterable


async def execute_d1_batch(binding: Any, commands: Iterable[tuple[str, tuple]]) -> Any:
    statements = [binding.prepare(sql).bind(*params) if params else binding.prepare(sql)
                  for sql, params in commands]
    if not statements:
        raise ValueError("D1 command batch must not be empty")
    return await binding.batch(statements)
