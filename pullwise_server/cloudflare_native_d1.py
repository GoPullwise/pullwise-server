"""Preserve safe Python integers at the Workers JavaScript D1 boundary.

Place this directly around env.DB, underneath the SQL and quota adapters. Their
logical parameters remain Python integers. Only the final native bind receives
an exactly representable JavaScript Number instead of a Python FFI BigInt.
"""


MAX_SAFE_INTEGER = 9007199254740991


def _native_value(value):
    if type(value) is int:
        if not -MAX_SAFE_INTEGER <= value <= MAX_SAFE_INTEGER:
            raise ValueError("D1 parameter must be a safe integer")
        return float(value)
    return value


class NativeD1:
    def __init__(self, binding):
        self.binding = binding

    def prepare(self, sql):
        return _Statement(self, self.binding.prepare(sql))

    async def batch(self, statements):
        statements = list(statements)
        if any(not isinstance(statement, _Statement) or statement.owner is not self
               for statement in statements):
            raise ValueError("D1 batch requires this adapter's statements")
        return await self.binding.batch([statement.native for statement in statements])


class _Statement:
    def __init__(self, owner, native):
        self.owner, self.native = owner, native

    def bind(self, *params):
        return _Statement(self.owner, self.native.bind(*map(_native_value, params)))

    async def first(self, column=None):
        return await self.native.first() if column is None else await self.native.first(column)

    async def all(self):
        return await self.native.all()

    async def run(self):
        return await self.native.run()
