"""S05 Cookie/Token scope, target, and immediate revocation contract."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from pullwise_server.cloudflare_api_key_write import create_api_key, revoke_api_key
from pullwise_server.cloudflare_ledger_auth import ledger_principal
from pullwise_server.cloudflare_ledger_profile import read_ledger_me
from pullwise_server.cloudflare_principal import PrincipalAuthError
from test_cloudflare_github_identity_http import D1ShapedSQLite, GitHubStub, call, login, seed


class LedgerApiKeyHttpTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        fixture, _, _ = seed(Path(self.directory.name) / "domain.db")
        self.store, self.now = fixture.store, fixture.now
        with self.store.connect() as db:
            migrations = Path(__file__).resolve().parents[1] / "cloudflare/server/migrations"
            for migration in sorted(migrations.glob("*.sql")):
                db.executescript(migration.read_text())
        self.binding = D1ShapedSQLite(self.store)
        _, _, login_headers = login(self.binding, GitHubStub(), self.now)
        self.cookie = login_headers["Set-Cookie"].split(";", 1)[0]

    def tearDown(self):
        self.directory.cleanup()

    def run_async(self, coroutine):
        return asyncio.run(coroutine)

    def authorize(self, headers, scope, target_kind=None, project_id=None):
        async def check():
            user, restrictions, commands, validate = await ledger_principal(
                binding=self.binding, headers=headers, scope=scope, now=self.now + 3,
                target_kind=target_kind, project_id=project_id)
            parts = await self.binding.batch(commands)
            validate([part.results for part in parts])
            return user, restrictions
        return self.run_async(check())

    def test_cookie_and_key_project_shared_scope_and_revoke(self):
        cookie = {"Cookie": self.cookie}
        self.assertEqual(self.authorize(cookie, "expenses:write", "shared")[0]["id"], "usr_github_77")
        status, key = self.run_async(create_api_key(binding=self.binding, headers=cookie,
            body={"name": "project editor", "scopes": ["expenses:read", "expenses:write"],
                  "restrictions": {"projectIds": ["prj_a"], "shared": False}}, now=self.now + 2))
        self.assertEqual(status, 201)
        self.assertEqual(key["restrictions"], {"projectIds": ["prj_a"], "shared": False})
        token = {"Authorization": "Bearer " + key["key"]}
        self.assertEqual(self.authorize(token, "expenses:read", "project", "prj_a")[0]["id"], "usr_github_77")
        with self.assertRaises(PrincipalAuthError) as denied:
            self.authorize(token, "expenses:read", "project", "prj_b")
        self.assertEqual(denied.exception.status, 403)
        with self.assertRaises(PrincipalAuthError):
            self.authorize(token, "expenses:read", "shared")
        with self.assertRaises(PrincipalAuthError):
            self.authorize(token, "projects:read", "project", "prj_a")
        status, _ = self.run_async(revoke_api_key(binding=self.binding, key_id=key["id"],
            headers=cookie, now=self.now + 4))
        self.assertEqual(status, 200)
        with self.assertRaises(PrincipalAuthError) as revoked:
            self.authorize(token, "expenses:read", "project", "prj_a")
        self.assertEqual(revoked.exception.status, 401)

    def test_restriction_change_fails_snapshot_validation(self):
        cookie = {"Cookie": self.cookie}
        status, key = self.run_async(create_api_key(binding=self.binding, headers=cookie,
            body={"scopes": ["expenses:read"], "restrictions": {"shared": True}}, now=self.now + 2))
        self.assertEqual(status, 201)

        async def check():
            headers = {"Authorization": "Bearer " + key["key"]}
            _, _, commands, validate = await ledger_principal(binding=self.binding,
                headers=headers, scope="expenses:read", now=self.now + 3,
                target_kind="shared")
            with self.store.connect() as db:
                db.execute("UPDATE api_keys SET restrictions=? WHERE id=?",
                           (json.dumps({"shared": False}), key["id"]))
            parts = await self.binding.batch(commands)
            with self.assertRaises(PrincipalAuthError):
                validate([part.results for part in parts])
        self.run_async(check())

    def test_profile_route_gives_same_owner_and_key_scopes(self):
        cookie = {"Cookie": self.cookie}
        status, profile = self.run_async(read_ledger_me(binding=self.binding,
            headers=cookie, now=self.now + 2))
        self.assertEqual(status, 200)
        self.assertEqual(profile["id"], "usr_github_77")
        self.assertIn("expenses:write", profile["scopes"])
        self.assertEqual(profile["ledgerUsage"], {"workspaceId": profile["id"],
            "projects": {"used": 0, "limit": 3, "remaining": 3},
            "expenseRecords": {"used": 0, "limit": 100, "remaining": 100}, "jev": None})
        status, key = self.run_async(create_api_key(binding=self.binding, headers=cookie,
            body={}, now=self.now + 2))
        self.assertEqual(status, 201)
        status, token_profile = self.run_async(read_ledger_me(binding=self.binding,
            headers={"Authorization": "Bearer " + key["key"]}, now=self.now + 3))
        self.assertEqual(status, 200)
        self.assertEqual(token_profile["id"], profile["id"])
        self.assertEqual(token_profile["ledgerUsage"], profile["ledgerUsage"])
        self.assertEqual(token_profile["scopes"], key["scopes"])
        self.assertNotIn("expenses:write", token_profile["scopes"])

    def test_repository_route_requires_projects_read_for_token(self):
        cookie = {"Cookie": self.cookie}
        status, key = self.run_async(create_api_key(binding=self.binding, headers=cookie,
            body={"scopes": ["projects:read"]}, now=self.now + 2))
        self.assertEqual(status, 201)
        status, payload, _ = call(self.binding, GitHubStub(), self.now + 3,
            "GET", "/api/v1/repositories", headers={"Authorization": "Bearer " + key["key"]})
        self.assertEqual(status, 200)
        self.assertEqual([item["githubRepoId"] for item in payload["items"]], [202])
        self.assertIsNone(payload["nextCursor"])
        status, limited = self.run_async(create_api_key(binding=self.binding, headers=cookie,
            body={"scopes": ["expenses:read"]}, now=self.now + 2))
        self.assertEqual(status, 201)
        status, payload, _ = call(self.binding, GitHubStub(), self.now + 3,
            "GET", "/api/v1/repositories", headers={"Authorization": "Bearer " + limited["key"]})
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "INSUFFICIENT_SCOPE")

    def test_member_scopes_are_explicit_and_reject_project_allowlists(self):
        cookie = {"Cookie": self.cookie}
        for scope in ("members:read", "members:write"):
            with self.subTest(scope=scope):
                status, key = self.run_async(create_api_key(binding=self.binding, headers=cookie,
                    body={"scopes": [scope], "restrictions": {"shared": False}}, now=self.now + 2))
                self.assertEqual(status, 201)
                self.assertEqual(self.authorize({"Authorization": "Bearer " + key["key"]}, scope)[0]["id"], "usr_github_77")
                for projects in ([], ["prj_a"]):
                    status, denied = self.run_async(create_api_key(binding=self.binding, headers=cookie,
                        body={"scopes": [scope], "restrictions": {"projectIds": projects}}, now=self.now + 2))
                    self.assertEqual((status, denied["error"]["code"]), (400, "INVALID_RESTRICTION"))

    def test_member_authorization_rejects_a_persisted_project_allowlist(self):
        cookie = {"Cookie": self.cookie}
        status, key = self.run_async(create_api_key(binding=self.binding, headers=cookie,
            body={"scopes": ["members:read"]}, now=self.now + 2))
        self.assertEqual(status, 201)
        with self.store.connect() as db:
            db.execute("UPDATE api_keys SET restrictions=? WHERE id=?",
                       (json.dumps({"shared": False, "projectIds": []}), key["id"]))
        with self.assertRaises(PrincipalAuthError) as denied:
            self.authorize({"Authorization": "Bearer " + key["key"]}, "members:read")
        self.assertEqual((denied.exception.status, denied.exception.code), (403, "TARGET_FORBIDDEN"))


if __name__ == "__main__":
    unittest.main()
