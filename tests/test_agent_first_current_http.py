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
    ) -> _RouteHarness:
        session = {"userId": "operator"} if operator else None
        handler = _RouteHarness(path, body, session=session)
        segments = [part for part in path.split("/") if part]
        users = {"operator": {"id": "operator"}}
        with (
            patch.object(app.db, "connect", self.connect),
            patch.object(app, "worker_token_record", return_value=worker_record),
            patch.object(app, "user_is_admin", return_value=operator),
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
        authority = verify_document_digest(
            "server-authority-envelope/v1",
            json.loads(claimed.binary_payload),
        )
        self.assertEqual(package_tuple(), registration["package"])
        self.assertEqual(package_tuple(), acceptance["package"])
        self.assertEqual(package_tuple(), authority["package"])
        self.assertEqual(acceptance["task_id"], authority["task_id"])
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


if __name__ == "__main__":
    unittest.main()
