from __future__ import annotations

import json
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app
from pullwise_server._generated_agent_task_contract import (
    package_tuple,
    verify_document_digest,
)
from pullwise_server.agent_first_authority_migrations import (
    CURRENT_AUTHORITY_TABLES,
)
from tests.agent_first_authority_support import AuthorityHarness, WORKER_ID


class _RouteHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict[str, object],
        *,
        session: dict[str, object] | None = None,
    ) -> None:
        self.path = path
        self._body = body
        self._session = session
        self.headers: dict[str, str] = {}
        self.status: int | None = None
        self.payload: dict[str, object] | None = None
        self.binary_payload = b""

    def read_json(self) -> dict[str, object]:
        return self._body

    def current_session(self) -> dict[str, object] | None:
        return self._session

    def json(
        self,
        payload: dict[str, object],
        status: int = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        del headers
        self.payload = payload
        self.status = status

    def binary(
        self,
        payload: bytes,
        status: int = HTTPStatus.OK,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        del content_type, headers
        self.binary_payload = payload
        self.status = status

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class AgentFirstCurrentHttpTest(AuthorityHarness, unittest.TestCase):
    def _post(
        self,
        path: str,
        body: dict[str, object],
        *,
        worker_record: dict[str, object] | None = None,
        operator: bool = False,
        operator_authorized: bool | None = None,
    ) -> _RouteHarness:
        session = {"userId": "operator"} if operator else None
        authorized = operator if operator_authorized is None else operator_authorized
        handler = _RouteHarness(path, body, session=session)
        segments = [part for part in path.split("/") if part]
        users = {"operator": {"id": "operator"}}
        with (
            patch.object(app.db, "connect", self.connect),
            patch.object(app, "worker_token_record", return_value=worker_record),
            patch.object(app, "user_is_admin", return_value=authorized),
            patch.dict(app.USERS, users, clear=False),
        ):
            app.PullwiseHandler.handle_post(handler, path, {}, segments)
        return handler

    def test_operator_accepts_then_worker_registers_and_claims_current_task(
        self,
    ) -> None:
        worker = {"worker_id": WORKER_ID}
        register_request = self.register_request()
        accept_request = self.accept_request()
        claim_request = self.claim_request()

        register = self._post(
            "/v1/agent-first/workers/register",
            register_request,
            worker_record=worker,
        )
        accepted = self._post(
            "/v1/agent-first/tasks/accept",
            accept_request,
            operator=True,
        )
        claimed = self._post(
            "/v1/agent-first/tasks/claim",
            claim_request,
            worker_record=worker,
        )

        self.assertEqual(HTTPStatus.OK, register.status)
        self.assertEqual(HTTPStatus.OK, accepted.status)
        self.assertEqual(HTTPStatus.OK, claimed.status)
        registration = verify_document_digest(
            "agent-worker-register-response/v1",
            json.loads(register.binary_payload),
        )
        acceptance = verify_document_digest(
            "agent-task-accept-response/v1",
            json.loads(accepted.binary_payload),
        )
        bootstrap = verify_document_digest(
            "agent-task-runtime-bootstrap/v1",
            json.loads(claimed.binary_payload),
        )
        authority = bootstrap["authority"]
        self.assertEqual(package_tuple(), registration["package"])
        self.assertEqual(package_tuple(), acceptance["package"])
        self.assertEqual(package_tuple(), authority["package"])
        self.assertEqual(acceptance["task_id"], authority["task_id"])
        self.assertEqual(accept_request, bootstrap["accept_request"])
        self.assertEqual(WORKER_ID, registration["worker_id"])

        replayed = (
            self._post(
                "/v1/agent-first/workers/register",
                register_request,
                worker_record=worker,
            ).binary_payload,
            self._post(
                "/v1/agent-first/tasks/accept",
                accept_request,
                operator=True,
            ).binary_payload,
            self._post(
                "/v1/agent-first/tasks/claim",
                claim_request,
                worker_record=worker,
            ).binary_payload,
        )
        self.assertEqual(
            (register.binary_payload, accepted.binary_payload, claimed.binary_payload),
            replayed,
        )

    def test_untrusted_principals_are_rejected_without_authority_writes(
        self,
    ) -> None:
        other_worker = {"worker_id": "worker_" + "b" * 32}
        cases = (
            (
                "anonymous operator",
                "/v1/agent-first/tasks/accept",
                self.accept_request(),
                {},
                HTTPStatus.UNAUTHORIZED,
            ),
            (
                "non-admin operator",
                "/v1/agent-first/tasks/accept",
                self.accept_request(),
                {"operator": True, "operator_authorized": False},
                HTTPStatus.FORBIDDEN,
            ),
            (
                "anonymous worker",
                "/v1/agent-first/workers/register",
                self.register_request(),
                {},
                HTTPStatus.UNAUTHORIZED,
            ),
            (
                "mismatched worker",
                "/v1/agent-first/workers/register",
                self.register_request(),
                {"worker_record": other_worker},
                HTTPStatus.FORBIDDEN,
            ),
            (
                "anonymous claimant",
                "/v1/agent-first/tasks/claim",
                self.claim_request(),
                {},
                HTTPStatus.UNAUTHORIZED,
            ),
        )
        before = self.counts(*CURRENT_AUTHORITY_TABLES)

        for name, path, body, credentials, expected_status in cases:
            with self.subTest(name=name):
                response = self._post(path, body, **credentials)
                payload = json.loads(response.binary_payload)
                self.assertEqual(expected_status, response.status)
                self.assertEqual(
                    "AUTHORITY_INPUT_UNTRUSTED",
                    payload["error"]["code"],
                )
                verify_document_digest("stable-error/v1", payload["error"])
                self.assertEqual(
                    before,
                    self.counts(*CURRENT_AUTHORITY_TABLES),
                )


if __name__ == "__main__":
    unittest.main()
