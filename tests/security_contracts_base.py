from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app


class RouteHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict | None = None,
        cookie: str = "",
        headers: dict | None = None,
        raw_body: bytes | None = None,
    ) -> None:
        self.path = path
        self._body = body or {}
        self._raw_body = raw_body
        self.headers = {"Host": "api.pullwise.dev", "Cookie": cookie, **(headers or {})}
        self.payload = None
        self.status = None

    def read_json(self) -> dict:
        return self._body

    def read_raw_body(self) -> bytes:
        return self._raw_body if self._raw_body is not None else super().read_raw_body()

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)

    def redirect(self, location: str, set_cookie: str | None = None) -> None:
        self.status = HTTPStatus.FOUND
        self.location = location
        self.headers_out = {"Set-Cookie": set_cookie} if set_cookie else {}


class RawBodyRouteHarness(RouteHarness):
    def read_json(self) -> dict:
        return app.PullwiseHandler.read_json(self)


class DisconnectingRouteHarness(RouteHarness):
    def handle_get(self, path: str, params: dict, segments: list[str]) -> None:
        raise app.ClientDisconnected("Client disconnected during response write.")

    def error(self, status: int, message: str) -> None:
        raise AssertionError("client disconnects must not be converted into error responses")


class SecurityContractsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.persist_patcher = patch.object(app, "persist_state")
        self.persist_patcher.start()
        self.addCleanup(self.persist_patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.db_patcher = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.db_patcher.start()
        self.addCleanup(self.db_patcher.stop)
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "name": "Dev",
                "email": "dev@example.com",
                "createdAt": app.now(),
                "providers": ["email"],
                "githubId": "1",
                "githubLogin": "octocat",
                "githubRepositoryAccess": {"repositories": ["owner/repo"]},
            }
        }
        app.SESSIONS = {}
        app.ISSUES = [
            {
                "id": "iss_1",
                "userId": "usr_1",
                "status": "open",
                "title": "Example",
            }
        ]
        app.SCANS = [
            {
                "id": "sc_1",
                "userId": "usr_1",
                "status": "done",
                "repo": "owner/repo",
            }
        ]
        app.STATE_LOADED = True
        app.STATE_DIRTY = False
        app.GITHUB_STATES = {}
        app.SETTINGS = {}
        with app.PREVIEW_SCAN_LOCKS_GUARD:
            app.PREVIEW_SCAN_LOCKS.clear()

    def signed_in(self):
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        return "pw_session=ses_1"

