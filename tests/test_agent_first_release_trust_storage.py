from __future__ import annotations

import base64
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
from types import ModuleType
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server.agent_first_authority import AuthorityError
from pullwise_server.agent_first_release_trust import AgentFirstReleaseTrust
from pullwise_server.agent_first_release_trust_migrations import (
    CURRENT_RELEASE_TRUST_TABLES,
    install_current_release_trust_tables,
)
from pullwise_server.agent_first_release_trust_store import (
    RELEASE_TRUST_FAULT_POINTS,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source"


def _sign_current_document(
    contract: ModuleType,
    schema_id: str,
    signature_domain: str,
    digest_field: str,
    document: dict[str, object],
    private_seed: str,
) -> dict[str, object]:
    value = deepcopy(document)
    value["package"] = contract.package_tuple()
    value.pop("signature", None)
    value.pop(digest_field, None)
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(private_seed))
    message = signature_domain.encode("ascii") + b"\0" + (
        contract.canonical_document_bytes(value)
    )
    value["signature"] = base64.urlsafe_b64encode(key.sign(message)).decode(
        "ascii"
    ).rstrip("=")
    value[digest_field] = contract.document_digest(schema_id, value)
    return value


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
        self.revocation = deepcopy(
            self.contract.fixture("release_key_revocation_golden_superseded")[
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

    def counts(self, connect: object | None = None) -> tuple[int, ...]:
        connection_factory = self.connect if connect is None else connect
        with closing(connection_factory()) as connection:
            return tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_TRUST_TABLES
            )

    def test_registers_a_pinned_chain_and_verifies_a_benchmark_signature(
        self,
    ) -> None:
        stored = self.trust.register_authority(
            self.root,
            self.principal,
            self.signing_key,
        )
        benchmark = _sign_current_document(
            self.contract,
            "benchmark-bundle/v1",
            "pullwise-benchmark-bundle/v1",
            "bundle_digest",
            self.contract.fixture("benchmark_bundle_golden_current")["document"],
            "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
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

    def test_exact_replay_is_a_noop_and_rows_are_immutable(self) -> None:
        first = self.trust.register_authority(
            self.root, self.principal, self.signing_key
        )
        second = self.trust.register_authority(
            deepcopy(self.root), deepcopy(self.principal), deepcopy(self.signing_key)
        )
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.trust.revoke_key(self.revocation)

        self.assertEqual(first, second)
        self.assertEqual((1, 1, 1, 1), self.counts())
        with closing(self.connect()) as connection:
            for table in CURRENT_RELEASE_TRUST_TABLES:
                with self.subTest(table=table, operation="UPDATE"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(
                            f"UPDATE {table} SET created_at = created_at + 1"
                        )
                with self.subTest(table=table, operation="DELETE"):
                    with self.assertRaises(sqlite3.IntegrityError):
                        connection.execute(f"DELETE FROM {table}")

    def test_all_authority_fault_points_roll_back_the_chain(self) -> None:
        for point in RELEASE_TRUST_FAULT_POINTS:
            with self.subTest(point=point), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "fault.sqlite3"

                def connect() -> sqlite3.Connection:
                    connection = sqlite3.connect(path)
                    connection.execute("PRAGMA foreign_keys=ON")
                    return connection

                with closing(connect()) as connection:
                    install_current_release_trust_tables(connection)

                def fault(candidate: str) -> None:
                    if candidate == point:
                        raise RuntimeError(point)

                trust = AgentFirstReleaseTrust(
                    connect,
                    trusted_root_digests={
                        self.root["organization_id"]: {self.root["root_digest"]}
                    },
                    contract=self.contract,
                    clock=lambda: self.now,
                    fault_injector=fault,
                )
                with self.assertRaisesRegex(RuntimeError, point):
                    trust.register_authority(
                        self.root, self.principal, self.signing_key
                    )
                with closing(connect()) as connection:
                    self.assertEqual(
                        (0, 0, 0, 0),
                        tuple(
                            connection.execute(
                                f"SELECT COUNT(*) FROM {table}"
                            ).fetchone()[0]
                            for table in CURRENT_RELEASE_TRUST_TABLES
                        ),
                    )

    def test_unpinned_root_and_invalid_signature_write_nothing(self) -> None:
        unpinned = AgentFirstReleaseTrust(
            self.connect,
            trusted_root_digests={},
            contract=self.contract,
            clock=lambda: self.now,
        )
        with self.assertRaises(AuthorityError) as unpinned_error:
            unpinned.register_authority(
                self.root, self.principal, self.signing_key
            )
        self.assertEqual("AUTHORITY_INPUT_UNTRUSTED", unpinned_error.exception.code)
        self.assertEqual((0, 0, 0, 0), self.counts())

        principal = deepcopy(self.principal)
        principal["signature"] = ("A" if principal["signature"][0] != "A" else "B") + principal[
            "signature"
        ][1:]
        principal.pop("principal_digest")
        principal["principal_digest"] = self.contract.document_digest(
            "release-principal/v1", principal
        )
        with self.assertRaises(AuthorityError) as signature_error:
            self.trust.register_authority(self.root, principal, self.signing_key)
        self.assertEqual(
            "AUTHORITY_INPUT_UNTRUSTED", signature_error.exception.code
        )
        self.assertEqual((0, 0, 0, 0), self.counts())

    def test_revocation_blocks_verification_only_when_effective(self) -> None:
        self.trust.register_authority(self.root, self.principal, self.signing_key)
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)

        self.trust.revoke_key(self.revocation)

        source = deepcopy(
            self.contract.fixture("benchmark_bundle_golden_current")["document"]
        )
        source["issued_at"] = "2026-12-10T00:00:00.000Z"
        source["expires_at"] = "2027-02-01T00:00:00.000Z"
        benchmark = _sign_current_document(
            self.contract,
            "benchmark-bundle/v1",
            "pullwise-benchmark-bundle/v1",
            "bundle_digest",
            source,
            "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        )
        self.now = datetime(2026, 12, 15, tzinfo=timezone.utc)

        self.assertEqual(
            "key_benchmark_2026_01",
            self.trust.verify_document(benchmark).key_id,
        )
        self.now = datetime(2027, 1, 2, tzinfo=timezone.utc)

        with self.assertRaises(AuthorityError) as raised:
            self.trust.verify_document(benchmark)

        self.assertEqual("AUTHORITY_INPUT_UNTRUSTED", raised.exception.code)


if __name__ == "__main__":
    unittest.main()
