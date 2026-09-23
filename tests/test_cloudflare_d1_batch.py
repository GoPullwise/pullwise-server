"""The Server D1 boundary submits every guarded command as one batch."""
import asyncio

import pytest

from pullwise_server.cloudflare_d1_batch import execute_d1_batch


class Statement:
    def __init__(self, sql):
        self.sql = sql
        self.params = None

    def bind(self, *params):
        self.params = params
        return self

    async def run(self):
        raise AssertionError("individual statement execution breaks transaction")


class Binding:
    def __init__(self, fail=False):
        self.prepared = []
        self.batches = []
        self.fail = fail

    def prepare(self, sql):
        statement = Statement(sql)
        self.prepared.append(statement)
        return statement

    async def batch(self, statements):
        self.batches.append(statements)
        if self.fail:
            raise RuntimeError("D1 rejected the guarded batch")
        return ["committed"]


def test_executes_all_guards_and_writes_in_one_d1_batch():
    binding = Binding()
    commands = [("INSERT guard VALUES (?)", (1,)), ("UPDATE account SET revision=2", ())]
    result = asyncio.run(execute_d1_batch(binding, commands))
    assert result == ["committed"]
    assert len(binding.batches) == 1
    assert binding.batches[0] == binding.prepared
    assert [(s.sql, s.params) for s in binding.prepared] == [
        ("INSERT guard VALUES (?)", (1,)), ("UPDATE account SET revision=2", None)]


def test_d1_batch_rejection_propagates_to_caller():
    binding = Binding(fail=True)
    with pytest.raises(RuntimeError, match="D1 rejected"):
        asyncio.run(execute_d1_batch(binding, [("INSERT guard VALUES (1)", ())]))
    assert len(binding.batches) == 1
