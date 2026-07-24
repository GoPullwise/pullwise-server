"""Server-only composition of trusted release evaluation and attestation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
from types import ModuleType
from typing import Callable

from . import _generated_agent_task_contract as _default_contract
from . import db
from .agent_first_authority import AuthorityError
from .agent_first_release_attestation_store import (
    ReleaseAttestationStore,
    ReleaseAttestationStoreError,
    StoredReleaseAttestationRow,
)
from .agent_first_release_evaluator import (
    AgentFirstReleaseEvaluator,
    StoredReleaseEvaluation,
)
from .agent_first_release_trust import (
    AgentFirstReleaseTrust,
    VerifiedReleaseSignature,
)


@dataclass(frozen=True)
class StoredReleaseAttestation:
    benchmark_bytes: bytes
    policy_bytes: bytes
    report_bytes: bytes
    attestation_bytes: bytes
    verdict: str
    exit_code: int
    organization_id: str
    principal_id: str
    key_id: str
    verified_at: str


class AgentFirstReleaseAttestor:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection] = db.connect,
        *,
        trust: AgentFirstReleaseTrust,
        contract: ModuleType = _default_contract,
        evaluator: AgentFirstReleaseEvaluator | None = None,
    ) -> None:
        contract.verify_bundle()
        self._contract = contract
        self._trust = trust
        self._evaluator = evaluator or AgentFirstReleaseEvaluator(
            connect_factory,
            contract=contract,
        )
        self._store = ReleaseAttestationStore(
            connect_factory,
            contract.PACKAGE_TUPLE,
        )

    @staticmethod
    def _store_error(error: ReleaseAttestationStoreError) -> AuthorityError:
        return AuthorityError(
            {
                "AUTHORITY_STORAGE_CORRUPT": "AUTHORITY_RELOAD_REQUIRED",
                "IDEMPOTENCY_CONFLICT": "IDEMPOTENCY_CONFLICT",
                "RELEASE_ATTESTATION_NOT_FOUND": "AUTHORITY_INPUT_UNTRUSTED",
            }.get(error.code, "AUTHORITY_INPUT_UNTRUSTED")
        )

    @staticmethod
    def _verified_time(value: str) -> datetime:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=timezone.utc
            )
        except (TypeError, ValueError):
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None

    @staticmethod
    def _public_result(
        evaluation: StoredReleaseEvaluation,
        stored: StoredReleaseAttestationRow,
    ) -> StoredReleaseAttestation:
        return StoredReleaseAttestation(
            evaluation.benchmark_bytes,
            evaluation.policy_bytes,
            evaluation.report_bytes,
            stored.attestation_bytes,
            evaluation.verdict,
            evaluation.exit_code,
            stored.organization_id,
            stored.principal_id,
            stored.key_id,
            stored.verified_at,
        )

    def _verify_input_signatures(
        self,
        benchmark: object,
        policy: object,
        attestation: object,
    ) -> VerifiedReleaseSignature:
        benchmark_signature = self._trust.verify_document(benchmark)
        verified_at = self._verified_time(benchmark_signature.verified_at)
        policy_signature = self._trust.verify_document_at(policy, verified_at)
        attestation_signature = self._trust.verify_document_at(
            attestation, verified_at
        )
        if not (
            benchmark_signature.organization_id
            == policy_signature.organization_id
            == attestation_signature.organization_id
        ):
            raise AuthorityError("AUTHORITY_INPUT_UNTRUSTED")
        return attestation_signature

    def attest_and_store(
        self,
        benchmark_bundle: object,
        policy: object,
        report: object,
        attestation: object,
    ) -> StoredReleaseAttestation:
        signature = self._verify_input_signatures(
            benchmark_bundle, policy, attestation
        )
        try:
            checked = self._contract.verify_release_gate_attestation_context(
                attestation,
                policy,
                report,
            )
            attestation_bytes = self._contract.canonical_validated_bytes(
                "release-gate-attestation/v1", checked
            )
        except (
            self._contract.ContractValidationError,
            KeyError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise AuthorityError("AUTHORITY_INPUT_UNTRUSTED") from None
        evaluation = self._evaluator.evaluate_and_store(
            benchmark_bundle,
            policy,
            report,
        )
        if evaluation.verdict != "PASS" or evaluation.exit_code != 0:
            raise AuthorityError("AUTHORITY_INPUT_UNTRUSTED")
        try:
            stored = self._store.store_attestation(
                attestation=checked,
                attestation_bytes=attestation_bytes,
                principal_id=signature.principal_id,
                key_id=signature.key_id,
                verified_at=signature.verified_at,
            )
        except ReleaseAttestationStoreError as error:
            raise self._store_error(error) from None
        return self._public_result(evaluation, stored)

    def load_attestation(self, attestation_id: str) -> StoredReleaseAttestation:
        if not isinstance(attestation_id, str):
            raise AuthorityError("CONTRACT_DOCUMENT_INVALID")
        try:
            stored = self._store.load_attestation(attestation_id)
        except ReleaseAttestationStoreError as error:
            raise self._store_error(error) from None
        evaluation = self._evaluator.load_evaluation(stored.report_id)
        try:
            benchmark = json.loads(evaluation.benchmark_bytes)
            policy = json.loads(evaluation.policy_bytes)
            report = json.loads(evaluation.report_bytes)
            attestation = json.loads(stored.attestation_bytes)
            checked = self._contract.verify_release_gate_attestation_context(
                attestation,
                policy,
                report,
            )
            if self._contract.canonical_validated_bytes(
                "release-gate-attestation/v1", checked
            ) != stored.attestation_bytes:
                raise ValueError("stored attestation is not canonical")
            verified_at = self._verified_time(stored.verified_at)
            benchmark_signature = self._trust.verify_document_at(
                benchmark, verified_at
            )
            policy_signature = self._trust.verify_document_at(policy, verified_at)
            signature = self._trust.verify_document_at(attestation, verified_at)
            metadata_matches = (
                evaluation.verdict == "PASS"
                and evaluation.exit_code == 0
                and benchmark_signature.organization_id
                == policy_signature.organization_id
                == signature.organization_id
                == stored.organization_id
                and signature.principal_id == stored.principal_id
                and signature.key_id == stored.key_id
            )
        except AuthorityError:
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        except (
            self._contract.ContractValidationError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            UnicodeError,
            ValueError,
        ):
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        if not metadata_matches:
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED")
        return self._public_result(evaluation, stored)


__all__ = ["AgentFirstReleaseAttestor", "StoredReleaseAttestation"]
