"""Independent comparison must remain active with optimized Python bytecode."""

import asyncio
from pathlib import Path

import pytest

from pg_play import logical_oracle


class Transaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class Connection:
    def __init__(self, value, kind):
        self.value, self.kind = value, kind

    def is_in_transaction(self):
        return False

    def transaction(self, **kwargs):
        return Transaction()

    async def execute(self, *args):
        pass

    async def fetch(self, *args):
        if self.kind == "matview":
            return [dict(oid=1, nspname="public", relname="v", relkind="m", relispopulated=True)]
        return []

    async def fetchrow(self, *args):
        return self.value, True

    async def fetchval(self, *args):
        return False


@pytest.mark.parametrize("optimize", [0, 1])
@pytest.mark.parametrize("kind", ["sequence", "matview"])
def test_deferred_mismatch_is_rejected_under_optimization(optimize, kind):
    namespace = {}
    path = Path(logical_oracle.__file__)
    exec(compile(path.read_text(), str(path), "exec", optimize=optimize), namespace)

    async def catalog(*args):
        return {"sequence_definitions": [("public", "s")] if kind == "sequence" else []}

    namespace["catalog"] = catalog
    with pytest.raises(AssertionError, match=kind):
        asyncio.run(
            namespace["compare"](
                Connection(100, kind),
                Connection(1, kind),
                deferred=True,
            )
        )
