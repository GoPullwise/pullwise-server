"""Current-package Server facade for durable release-gate evaluations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Callable

from . import db
from ._generated_agent_task_contract import (
    ContractValidationError,
    canonical_validated_bytes,
    evaluate_release_gate,
    package_tuple,
    verify_bundle,
)
from .agent_first_authority import AuthorityError
from .agent_first_release_evaluator_store import (
    FaultInjector,
    ReleaseEvaluatorStore,
    ReleaseEvaluatorStoreError,
    StoredReleaseEvaluationRows,
)


@dataclass(frozen=True)
class StoredReleaseEvaluation:
    benchmark_bytes: bytes
    policy_bytes: bytes
    report_bytes: bytes
    verdict: str
    exit_code: int


class AgentFirstReleaseEvaluator:
    def __init__(
        self,
        connect_factory: Callable[[], sqlite3.Connection] = db.connect,
        *,
        fault_injector: FaultInjector | None = None,
    ) -> None:
        verify_bundle()
        self._store = ReleaseEvaluatorStore(connect_factory, fault_injector)

    @staticmethod
    def _require_current_package(document: object) -> None:
        if not isinstance(document, dict) or "package" not in document:
            raise AuthorityError("CURRENT_PACKAGE_PIN_MISSING")
        if document["package"] != package_tuple():
            raise AuthorityError("CURRENT_PACKAGE_PIN_MISMATCH")

    @staticmethod
    def _store_error(error: ReleaseEvaluatorStoreError) -> AuthorityError:
        return AuthorityError(
            {
                "AUTHORITY_STORAGE_CORRUPT": "AUTHORITY_RELOAD_REQUIRED",
                "IDEMPOTENCY_CONFLICT": "IDEMPOTENCY_CONFLICT",
                "RELEASE_EVALUATION_NOT_FOUND": "AUTHORITY_INPUT_UNTRUSTED",
            }.get(error.code, "AUTHORITY_INPUT_UNTRUSTED")
        )

    @staticmethod
    def _public_result(
        stored: StoredReleaseEvaluationRows,
    ) -> StoredReleaseEvaluation:
        return StoredReleaseEvaluation(
            benchmark_bytes=stored.benchmark_bytes,
            policy_bytes=stored.policy_bytes,
            report_bytes=stored.report_bytes,
            verdict=stored.verdict,
            exit_code=stored.exit_code,
        )

    def evaluate_and_store(
        self,
        benchmark_bundle: object,
        policy: object,
        report: object,
    ) -> StoredReleaseEvaluation:
        for document in (benchmark_bundle, policy, report):
            self._require_current_package(document)
        try:
            result = evaluate_release_gate(benchmark_bundle, policy, report)
            benchmark_bytes = canonical_validated_bytes(
                "benchmark-bundle/v1", benchmark_bundle
            )
            policy_bytes = canonical_validated_bytes(
                "release-gate-policy/v1", policy
            )
            report_bytes = canonical_validated_bytes(
                "release-gate-report/v1", report
            )
        except (ContractValidationError, TypeError, ValueError, UnicodeError):
            raise AuthorityError("CONTRACT_DOCUMENT_INVALID") from None
        assert isinstance(benchmark_bundle, dict)
        assert isinstance(policy, dict)
        assert isinstance(report, dict)
        try:
            stored = self._store.store_evaluation(
                benchmark=benchmark_bundle,
                benchmark_bytes=benchmark_bytes,
                policy=policy,
                policy_bytes=policy_bytes,
                report=report,
                report_bytes=report_bytes,
                verdict=str(result["verdict"]),
                exit_code=int(result["exit_code"]),
            )
        except ReleaseEvaluatorStoreError as error:
            raise self._store_error(error) from None
        return self._public_result(stored)

    def load_evaluation(self, report_id: str) -> StoredReleaseEvaluation:
        if not isinstance(report_id, str):
            raise AuthorityError("CONTRACT_DOCUMENT_INVALID")
        try:
            stored = self._store.load_evaluation(report_id)
        except ReleaseEvaluatorStoreError as error:
            raise self._store_error(error) from None
        try:
            benchmark = json.loads(stored.benchmark_bytes)
            policy = json.loads(stored.policy_bytes)
            report = json.loads(stored.report_bytes)
            for document in (benchmark, policy, report):
                self._require_current_package(document)
            result = evaluate_release_gate(benchmark, policy, report)
            canonical = (
                canonical_validated_bytes("benchmark-bundle/v1", benchmark),
                canonical_validated_bytes("release-gate-policy/v1", policy),
                canonical_validated_bytes("release-gate-report/v1", report),
            )
            if canonical != (
                stored.benchmark_bytes,
                stored.policy_bytes,
                stored.report_bytes,
            ):
                raise ValueError("stored document is not canonical")
            if (
                report["report_id"] != report_id
                or result["verdict"] != stored.verdict
                or result["exit_code"] != stored.exit_code
            ):
                raise ValueError("stored evaluation metadata does not match")
        except AuthorityError:
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        except (
            ContractValidationError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeError,
            json.JSONDecodeError,
        ):
            raise AuthorityError("AUTHORITY_RELOAD_REQUIRED") from None
        return self._public_result(stored)


__all__ = ["AgentFirstReleaseEvaluator", "StoredReleaseEvaluation"]
