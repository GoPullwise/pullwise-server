"""Measure finite DO SQLite row costs with canonical admission/accounting.

Three local HTTP calls, four representative synthetic transactions, no D1 or
provider access. Native cursor metrics include the actual storage SQL effects;
synthetic D1 metadata only exercises journal methods and is never D1 evidence.
Uses the existing local native runner/cache, not a deployed diagnostic route.
"""
from __future__ import annotations

import argparse
import importlib.util
import hashlib
import json
from pathlib import Path
import sys


ENTRY = r'''
from workers import WorkerEntrypoint, DurableObject, Response
from pullwise_server.cloudflare_validation_budget import BudgetJournal
from pullwise_server.cloudflare_preview_rate import PreviewRateLimiter
from pullwise_server.cloudflare_preview_budget import initial_data


class ObservedSql:
    def __init__(self, native):
        self.native, self.cursors = native, []

    def exec(self, query, *params):
        cursor = self.native.exec(query, *params)
        self.cursors.append(cursor)
        return cursor

    def start(self):
        self.cursors = []

    def measurement(self):
        reads, writes = [], []
        for cursor in self.cursors:
            read, write = cursor.rowsRead, cursor.rowsWritten
            assert type(read) is int and read >= 0
            assert type(write) is int and write >= 0
            reads.append(read)
            writes.append(write)
        return {"rowsRead": sum(reads), "rowsWritten": sum(writes),
                "sqlStatements": len(self.cursors)}


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        if getattr(self.env, "PULLWISE_MODE", "") != "local":
            return Response.json({"error": "LOCAL_ONLY"}, status=503)
        name = str(request.url).rsplit("/", 1)[-1]
        if name not in {"product", "bounded", "legacy"}:
            return Response.json({"error": "NOT_FOUND"}, status=404)
        return await self.env.FIXTURE_JOURNAL.get(
            self.env.FIXTURE_JOURNAL.idFromName("cost-only-fixture")).fetch(request)


class FixtureJournal(DurableObject):
    def __init__(self, ctx, env):
        self.ctx, self.env = ctx, env
        self.sql = ObservedSql(ctx.storage.sql)
        self.journal = self.rate = None

    def construct(self):
        self.sql.start()
        self.journal = BudgetJournal(self.sql, preview_product=True, product_operations=True)
        self.rate = PreviewRateLimiter(self.sql)
        return self.sql.measurement()

    def stage(self, stages, name, callback):
        self.sql.start()
        callback()
        stages[name] = self.sql.measurement()

    def transaction(self, *, name, groups, write_groups=()):
        stages, now = {}, 1000
        self.stage(stages, "ingress", lambda: self.rate.ingress(
            {"cf-connecting-ip": "192.0.2.1", "cookie": "pw_session=synthetic-cost-fixture"},
            method="POST" if write_groups else "GET", path="/api/v1/expenses", now=now))
        self.stage(stages, "authenticatedActor", lambda: self.rate.actor(
            "synthetic-cost-actor", channel="write" if write_groups else "read", now=now))
        self.sql.start()
        ticket = self.journal.begin_product(now=now)
        stages["begin"] = self.sql.measurement()
        self.sql.start()
        for operation in range(1, groups + 1):
            writes = 9 if operation in write_groups else 0
            self.journal.reserve_operation(ticket, reads=20, writes=writes, now=now + 1)
            # Labeled synthetic, complete one-attempt result. No D1 statement
            # exists in this fixture and these are not customer invoice facts.
            self.journal.record(ticket, operation, 2, 3 if writes else 0)
            self.journal.settle_product_reads(ticket, operation, 20, now=now + 2)
            if writes:
                # Represents each normal mutation's post-write state publish.
                self.journal.save_product_state(ticket, initial_data(), now=now + 2, verified=True)
        stages["operationAccounting"] = self.sql.measurement()
        self.stage(stages, "finish", lambda: self.journal.finish(ticket, now=now + 3))
        state = self.journal.snapshot()
        assert state["stopped"] is None and state["active"] is None
        return {"name": name, "operationGroups": groups,
            "syntheticWriteGroups": len(write_groups), "stages": stages,
            "rowsRead": sum(stage["rowsRead"] for stage in stages.values()),
            "rowsWritten": sum(stage["rowsWritten"] for stage in stages.values()),
            "sqlStatements": sum(stage["sqlStatements"] for stage in stages.values())}

    async def fetch(self, request):
        name = str(request.url).rsplit("/", 1)[-1]
        if name == "product":
            overhead = self.construct()
            # Fixture scaffold is measured separately; native schema/D1 writes
            # are deliberately excluded from this DO-only representative run.
            self.sql.start()
            ticket = self.journal.begin_product(now=900)
            self.journal.save_product_state(ticket, initial_data(), now=901,
                initialized=True, verified=True)
            self.journal.finish(ticket, now=902)
            scaffold = self.sql.measurement()
            result = self.transaction(name="first-3-groups", groups=3)
            return Response.json({"passed": True, "firstConstructorOverhead": overhead,
                "fixtureScaffoldExcluded": scaffold, "transactions": [result],
                "d1Operations": 0, "providerRequests": 0})
        assert self.journal is not None
        if name == "bounded":
            result = self.transaction(name="stable-3-groups", groups=3)
            return Response.json({"passed": True, "transactions": [result],
                "d1Operations": 0, "providerRequests": 0})
        stable = self.transaction(name="stable-12-groups", groups=12, write_groups=(3,6,10))
        before = self.journal.snapshot()
        restart = self.construct()
        assert self.journal.snapshot() == before
        reconstructed = self.transaction(name="reconstructed-12-groups", groups=12, write_groups=(3,6,10))
        return Response.json({"passed": True, "reconstructedConstructorOverhead": restart,
            "transactions": [stable, reconstructed], "d1Operations": 0, "providerRequests": 0,
            "countersSurviveReconstruction": True})
'''


def annotate_evidence(output: Path, run_dir: Path):
    """Add provenance and bounds without another runtime/API operation."""
    evidence = json.loads(output.read_text(encoding="utf-8"))
    evidence.update({
        "measurementKind": "native-do-sql-cursor-accounting-microbenchmark",
        "localD1Operations": 0,
        "applicationD1MetricsMeasured": False,
        "customerDataUsed": False,
        "nativeCursorProperties": ["rowsRead", "rowsWritten"],
        "measuredStages": ["ingress", "authenticatedActor", "begin",
                           "operationAccounting", "finish"],
        "nativeEffectsIncluded": ["DO SQLite row changes", "native trigger effects",
                                  "native index maintenance"],
        "syntheticD1Metadata": {"perGroupReservedReads": 20,
            "perGroupObservedReads": 2, "perWriteGroupReservedWrites": 9,
            "perWriteGroupObservedWrites": 3,
            "completeSingleAttemptResults": True,
            "purpose": "Exercise canonical DO accounting without any D1 dispatch"},
        "fixtureEntrySha256": hashlib.sha256(
            (run_dir / "src/entry.py").read_bytes()).hexdigest(),
        "canonicalSourceSha256": {module: hashlib.sha256(
            (run_dir / "src/pullwise_server" / module).read_bytes()).hexdigest()
            for module in ["cloudflare_validation_budget.py", "cloudflare_preview_rate.py",
                           "cloudflare_preview_budget.py"]},
        "scopeLimits": [
            "Four synthetic transactions; not an online API traffic sample or route maximum",
            "First DO constructor and fixture scaffold reported separately",
            "Reconstruction calls canonical constructors on unchanged native SQLite storage; not a process restart",
            "Normal ProductMeteredD1 check/snapshot reads outside listed stages excluded",
            "Minute rollover/expired-subject deletion and large journal storage not sampled",
            "Application D1, provider latency, Worker CPU, DO duration and invoice not measured",
            "80 DO writes/API remains a planning parameter; 120 sensitivity remains applicable"],
    })
    # These inherited fixture route aliases have no old budget-policy meaning.
    for case, label in zip(evidence.get("cases", []),
                           ["first-read-shape", "stable-read-shape", "write-shapes-and-reconstruction"]):
        case["fixtureRouteAlias"] = case.pop("case")
        case["case"] = label
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8897)
    args = parser.parse_args()
    path = Path(__file__).with_name("check-preview-journal-native-runtime.py")
    spec = importlib.util.spec_from_file_location("preview_do_cost_runner", path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.ENTRY = ENTRY
    sys.argv.append("--journal-only")
    runner.main()
    annotate_evidence(args.output, args.run_dir)


if __name__ == "__main__":
    main()
