from __future__ import annotations

import base64
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server.agent_first_contract_bundle import build_bundle
from pullwise_server import db
from pullwise_server.agent_first_release_attestation import AgentFirstReleaseAttestor
from pullwise_server.agent_first_release_attestation_migrations import (
    CURRENT_RELEASE_ATTESTATION_TABLES,
    install_current_release_attestation_tables,
)
from pullwise_server.agent_first_release_evaluator_migrations import (
    CURRENT_RELEASE_EVALUATOR_TABLES,
    install_current_release_evaluator_tables,
)
from pullwise_server.agent_first_release_trust import AgentFirstReleaseTrust
from pullwise_server.agent_first_release_trust_migrations import (
    install_current_release_trust_tables,
)


ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "contracts" / "agent-first" / "current" / "source"
ROOT_SEED = "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
BENCHMARK_SEED = "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb"
RELEASE_SEED = "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7"


def _public_key(seed: str) -> str:
    public = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)).public_key()
    encoded = public.public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _seal_signed(
    contract: ModuleType,
    schema_id: str,
    domain: str,
    digest_field: str,
    document: dict[str, object],
    seed: str,
) -> dict[str, object]:
    value = deepcopy(document)
    value.pop("signature", None)
    value.pop(digest_field, None)
    message = domain.encode("ascii") + b"\0" + contract.canonical_document_bytes(value)
    signature = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed)).sign(
        message
    )
    value["signature"] = base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    value[digest_field] = contract.document_digest(schema_id, value)
    return value


def _content_ref(
    contract: ModuleType,
    original: dict[str, object],
    schema_id: str,
    document: dict[str, object],
) -> dict[str, object]:
    encoded = contract.canonical_validated_bytes(schema_id, document)
    return {
        **original,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size_bytes": len(encoded),
    }


def _release_digest(contract: ModuleType, domain: str, value: object) -> str:
    return hashlib.sha256(
        domain.encode("ascii") + b"\0" + contract.canonical_document_bytes(value)
    ).hexdigest()


class AgentFirstReleaseAttestationStorageTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        built = build_bundle(SOURCE_ROOT)
        cls.contract = ModuleType("_release_attestation_storage_contract")
        exec(built.python_wrapper, cls.contract.__dict__)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / "release-attestation.sqlite3"
        with closing(self.connect()) as connection:
            install_current_release_evaluator_tables(connection)
            install_current_release_trust_tables(connection)
            install_current_release_attestation_tables(connection)
        self.now = datetime(2026, 7, 24, tzinfo=timezone.utc)
        self.root = deepcopy(
            self.contract.fixture("release_trust_root_golden_external_pin")["document"]
        )
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
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _authorities(self) -> tuple[dict[str, object], dict[str, object]]:
        benchmark_principal = deepcopy(
            self.contract.fixture("release_principal_golden_benchmark_owner")["document"]
        )
        benchmark_key = deepcopy(
            self.contract.fixture("release_signing_key_golden_benchmark_owner")["document"]
        )
        self.trust.register_authority(self.root, benchmark_principal, benchmark_key)

        release_principal = deepcopy(benchmark_principal)
        release_principal.update(
            principal_id="principal_release_operator",
            role="release_operator",
        )
        release_principal = _seal_signed(
            self.contract,
            "release-principal/v1",
            "pullwise-release-principal/v1",
            "principal_digest",
            release_principal,
            ROOT_SEED,
        )
        release_key = deepcopy(benchmark_key)
        release_key.update(
            key_id="key_release_2026_01",
            principal_id=release_principal["principal_id"],
            principal_digest=release_principal["principal_digest"],
            principal_ref=_content_ref(
                self.contract,
                release_key["principal_ref"],
                "release-principal/v1",
                release_principal,
            ),
            key_purpose="release_signing",
            public_key=_public_key(RELEASE_SEED),
        )
        release_key = _seal_signed(
            self.contract,
            "release-signing-key/v1",
            "pullwise-release-signing-key/v1",
            "signing_key_digest",
            release_key,
            ROOT_SEED,
        )
        self.trust.register_authority(self.root, release_principal, release_key)
        return release_principal, release_key

    def _documents(self) -> tuple[dict[str, object], ...]:
        package = self.contract.package_tuple()
        benchmark = deepcopy(
            self.contract.fixture("benchmark_bundle_golden_current")["document"]
        )
        benchmark["package"] = package
        benchmark = _seal_signed(
            self.contract,
            "benchmark-bundle/v1",
            "pullwise-benchmark-bundle/v1",
            "bundle_digest",
            benchmark,
            BENCHMARK_SEED,
        )

        policy = deepcopy(
            self.contract.fixture("release_gate_policy_golden_bootstrap")["document"]
        )
        policy["package"] = package
        policy["benchmark_digest"] = benchmark["bundle_digest"]
        policy["benchmark_ref"] = _content_ref(
            self.contract, policy["benchmark_ref"], "benchmark-bundle/v1", benchmark
        )
        policy["candidate_digest"] = _release_digest(
            self.contract,
            "pullwise:candidate-digest:v1",
            {
                field: policy[field]
                for field in (
                    "package", "candidate_build_id", "control_plane_digest",
                    "evaluation_runtime_digest", "benchmark_ref", "benchmark_digest",
                    "threshold_table_digest", "profile_budget_digest", "canary_plan_digest",
                )
            },
        )
        policy = _seal_signed(
            self.contract,
            "release-gate-policy/v1",
            "pullwise-release-gate-policy/v1",
            "policy_digest",
            policy,
            RELEASE_SEED,
        )

        report = deepcopy(
            self.contract.fixture("release_gate_report_golden_bootstrap_pass")["document"]
        )
        for field in (
            "package", "candidate_build_id", "candidate_digest", "release_mode",
            "stable_package", "stable_candidate_digest", "stable_control_plane_digest",
            "benchmark_digest", "benchmark_version", "task_inventory_digest",
            "oracle_rubric_digest", "environment_image_digest", "control_plane_digest",
            "evaluation_runtime_digest", "statistical_implementation_version",
            "threshold_table_digest", "profile_budget_digest", "canary_plan_digest",
        ):
            report[field] = deepcopy(policy[field])
        report["benchmark_ref"] = _content_ref(
            self.contract, report["benchmark_ref"], "benchmark-bundle/v1", benchmark
        )
        report["policy_digest"] = policy["policy_digest"]
        report["policy_ref"] = _content_ref(
            self.contract, report["policy_ref"], "release-gate-policy/v1", policy
        )
        report.pop("report_digest", None)
        report["report_digest"] = self.contract.document_digest(
            "release-gate-report/v1", report
        )

        attestation = deepcopy(
            self.contract.fixture("release_gate_attestation_golden_bootstrap_pass")["document"]
        )
        for field in (
            "package", "candidate_build_id", "candidate_digest", "release_mode",
            "stable_package", "stable_candidate_digest", "stable_control_plane_digest",
            "policy_id", "policy_digest",
        ):
            attestation[field] = deepcopy(policy[field])
        attestation["policy_ref"] = _content_ref(
            self.contract, attestation["policy_ref"], "release-gate-policy/v1", policy
        )
        attestation["report_id"] = report["report_id"]
        attestation["report_digest"] = report["report_digest"]
        attestation["report_ref"] = _content_ref(
            self.contract, attestation["report_ref"], "release-gate-report/v1", report
        )
        attestation = _seal_signed(
            self.contract,
            "release-gate-attestation/v1",
            "pullwise-release-gate-attestation/v1",
            "attestation_digest",
            attestation,
            RELEASE_SEED,
        )
        return benchmark, policy, report, attestation

    def test_verified_pass_attestation_is_append_only_and_reloads(self) -> None:
        self._authorities()
        benchmark, policy, report, attestation = self._documents()
        attestor = AgentFirstReleaseAttestor(
            self.connect,
            trust=self.trust,
            contract=self.contract,
        )

        first = attestor.attest_and_store(benchmark, policy, report, attestation)
        replay = attestor.attest_and_store(
            deepcopy(benchmark), deepcopy(policy), deepcopy(report), deepcopy(attestation)
        )
        loaded = attestor.load_attestation(attestation["attestation_id"])

        self.assertEqual(first, replay)
        self.assertEqual(first, loaded)
        self.assertEqual("PASS", first.verdict)
        self.assertEqual(0, first.exit_code)
        self.assertEqual("principal_release_operator", first.principal_id)
        self.assertEqual("key_release_2026_01", first.key_id)
        self.assertEqual("2026-07-24T00:00:00.000Z", first.verified_at)
        with closing(self.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in CURRENT_RELEASE_ATTESTATION_TABLES
            )
        self.assertEqual((1,), counts)

    def test_invalid_attestation_signature_writes_no_evaluation_or_attestation(
        self,
    ) -> None:
        self._authorities()
        benchmark, policy, report, attestation = self._documents()
        attestation["signature"] = (
            "A" if attestation["signature"][0] != "A" else "B"
        ) + attestation["signature"][1:]
        attestation.pop("attestation_digest")
        attestation["attestation_digest"] = self.contract.document_digest(
            "release-gate-attestation/v1", attestation
        )
        attestor = AgentFirstReleaseAttestor(
            self.connect, trust=self.trust, contract=self.contract
        )

        with self.assertRaisesRegex(RuntimeError, "AUTHORITY_INPUT_UNTRUSTED"):
            attestor.attest_and_store(benchmark, policy, report, attestation)

        with closing(self.connect()) as connection:
            counts = tuple(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    *CURRENT_RELEASE_EVALUATOR_TABLES,
                    *CURRENT_RELEASE_ATTESTATION_TABLES,
                )
            )
        self.assertEqual((0, 0, 0, 0), counts)

    def test_same_attestation_id_with_different_valid_document_conflicts(self) -> None:
        self._authorities()
        benchmark, policy, report, attestation = self._documents()
        attestor = AgentFirstReleaseAttestor(
            self.connect, trust=self.trust, contract=self.contract
        )
        attestor.attest_and_store(benchmark, policy, report, attestation)
        collision = deepcopy(attestation)
        collision["expires_at"] = "2026-07-29T01:00:00.000Z"
        collision = _seal_signed(
            self.contract,
            "release-gate-attestation/v1",
            "pullwise-release-gate-attestation/v1",
            "attestation_digest",
            collision,
            RELEASE_SEED,
        )

        with self.assertRaisesRegex(RuntimeError, "IDEMPOTENCY_CONFLICT"):
            attestor.attest_and_store(benchmark, policy, report, collision)

        with closing(self.connect()) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM agent_current_release_gate_attestations"
            ).fetchone()[0]
        self.assertEqual(1, count)

    def test_attestation_is_immutable_and_corruption_fails_closed_on_reload(
        self,
    ) -> None:
        self._authorities()
        benchmark, policy, report, attestation = self._documents()
        attestor = AgentFirstReleaseAttestor(
            self.connect, trust=self.trust, contract=self.contract
        )
        attestor.attest_and_store(benchmark, policy, report, attestation)
        table = CURRENT_RELEASE_ATTESTATION_TABLES[0]
        with closing(self.connect()) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE {table} SET created_at = created_at + 1")
        with closing(self.connect()) as connection, connection:
            connection.execute(f"DROP TRIGGER {table}_immutable_update")
            connection.execute(
                f"UPDATE {table} SET document_bytes = ?",
                (b"{}",),
            )

        with self.assertRaisesRegex(RuntimeError, "AUTHORITY_RELOAD_REQUIRED"):
            attestor.load_attestation(attestation["attestation_id"])

    def test_reload_revalidates_at_recorded_time_after_expiry_and_revocation(
        self,
    ) -> None:
        release_principal, release_key = self._authorities()
        benchmark, policy, report, attestation = self._documents()
        attestor = AgentFirstReleaseAttestor(
            self.connect, trust=self.trust, contract=self.contract
        )
        stored = attestor.attest_and_store(
            benchmark, policy, report, attestation
        )
        revocation = deepcopy(
            self.contract.fixture("release_key_revocation_golden_superseded")[
                "document"
            ]
        )
        revocation.update(
            revocation_id="release_key_revocation_99999999999999999999999999999999",
            revoked_key_id=release_key["key_id"],
            revoked_key_digest=release_key["signing_key_digest"],
            revoked_key_ref=_content_ref(
                self.contract,
                revocation["revoked_key_ref"],
                "release-signing-key/v1",
                release_key,
            ),
            revoked_principal_id=release_principal["principal_id"],
            issued_at="2026-12-01T00:00:00.000Z",
            effective_at="2027-01-01T00:00:00.000Z",
        )
        revocation = _seal_signed(
            self.contract,
            "release-key-revocation/v1",
            "pullwise-release-key-revocation/v1",
            "revocation_digest",
            revocation,
            ROOT_SEED,
        )
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        self.trust.revoke_key(revocation)
        self.now = datetime(2027, 1, 2, tzinfo=timezone.utc)

        self.assertEqual(
            stored,
            attestor.load_attestation(attestation["attestation_id"]),
        )

    def test_main_database_initialization_installs_trust_and_attestation_tables(
        self,
    ) -> None:
        db_path = Path(self.temporary.name) / "main.sqlite3"
        with patch.dict(os.environ, {"PULLWISE_DB_PATH": str(db_path)}, clear=True):
            db.initialize()
            with closing(db.connect()) as connection:
                installed = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }

        self.assertIn("agent_current_release_trust_roots", installed)
        self.assertIn("agent_current_release_principals", installed)
        self.assertIn("agent_current_release_signing_keys", installed)
        self.assertIn("agent_current_release_key_revocations", installed)
        self.assertTrue(set(CURRENT_RELEASE_ATTESTATION_TABLES).issubset(installed))


if __name__ == "__main__":
    unittest.main()
