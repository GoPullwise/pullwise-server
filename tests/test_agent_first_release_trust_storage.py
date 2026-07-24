from __future__ import annotations

from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import ModuleType
import unittest

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server.agent_first_release_trust import AgentFirstReleaseTrust
from pullwise_server.agent_first_release_trust_migrations import (
    CURRENT_RELEASE_TRUST_TABLES,
    install_current_release_trust_tables,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source"


class AgentFirstReleaseTrustStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        built = build_bundle(SOURCE_ROOT)
        cls.contract = ModuleType("_release_trust_storage_contract")
        exec(built.python_wrapper, cls.contract.__dict__)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "release-trust.sqlite3"
        with closing(self.connect()) as connection:
            install_current_release_trust_tables(connection)
        self.root = deepcopy(
            self.contract.fixture("release_trust_root_golden_external_pin")[
                "document"
            ]
        )
        self.principal = deepcopy(
            self.contract.fixture("release_principal_golden_benchmark_owner")[
                "document"
            ]
        )
        self.signing_key = deepcopy(
            self.contract.fixture("release_signing_key_golden_benchmark_owner")[
                "document"
            ]
        )
        self.now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        self.trust = AgentFirstReleaseTrust(
            self.connect,
            trusted_root_digests={
                self.root["organization_id"]: {self.root["root_digest"]}
            },
            contract=self.contract,
            clock=lambda: self.now,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def test_registers_a_pinned_chain_and_verifies_a_benchmark_signature(
        self,
    ) -> None:
        stored = self.trust.register_authority(
            self.root,
            self.principal,
            self.signing_key,
        )
        benchmark = deepcopy(
            self.contract.fixture("benchmark_bundle_golden_current")["document"]
        )

        verified = self.trust.verify_document(benchmark)

        self.assertEqual(self.root["trust_root_id"], stored.trust_root_id)
        self.assertEqual(self.principal["principal_id"], stored.principal_id)
        self.assertEqual(self.signing_key["key_id"], stored.key_id)
        self.assertEqual("benchmark_signing", verified.key_purpose)
        self.assertEqual(self.principal["principal_id"], verified.principal_id)
        self.assertEqual(self.signing_key["key_id"], verified.key_id)
        with closing(self.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_TRUST_TABLES
            )
        self.assertEqual((1, 1, 1, 0), counts)


if __name__ == "__main__":
    unittest.main()
