from dataclasses import replace
import unittest

from pullwise_server.github_authorization import GitHubAuthorizationBinding, GitHubAuthorizationChecker
from pullwise_server.github_transport import GitHubResponse, GitHubUnavailable


class GitHubAuthorizationContractsTest(unittest.TestCase):
    def setUp(self):
        self.binding = GitHubAuthorizationBinding(
            billing_owner_id="usr_1", github_user_id="42", repository_id="123",
            repository_full_name="org/repo", user_token="secret-user", app_id="7",
            installation_id="8", app_token="secret-app", installation_token="secret-install")
        self.target = dict(module="pr", billing_owner_id="usr_1", github_repository_id="123",
                           app_id="7", installation_id="8")
        self.repo = dict(id=123, private=False, permissions=dict(admin=False, maintain=True))
        self.install = dict(id=8, app_id=7, suspended_at=None,
                            permissions=dict(metadata="read", pull_requests="read", actions="read"))
        self.responses = {
            ("/user", "secret-user"): dict(id=42),
            ("/repos/org/repo", "secret-user"): self.repo,
            ("/repos/org/repo/installation", "secret-app"): self.install,
            ("/repos/org/repo/pulls?per_page=1", "secret-install"): [],
            ("/repos/org/repo/actions/runs?per_page=1", "secret-install"): dict(workflow_runs=[]),
            ("/repos/org/repo/releases?per_page=1", "secret-user"): [],
        }
        self.calls = []

    def get(self, path, *, token):
        self.calls.append((path, token))
        result = self.responses[(path, token)]
        if isinstance(result, Exception):
            raise result
        return result if isinstance(result, GitHubResponse) else GitHubResponse(200, result, {})

    def check(self):
        return GitHubAuthorizationChecker(get_json=self.get, resolve_binding=lambda target: self.binding)(
            target=self.target, now=1000)

    def test_pr_and_ci_fresh_module_proofs(self):
        for module in ("pr", "ci"):
            with self.subTest(module=module):
                self.target["module"] = module
                proof = self.check()
                self.assertTrue(proof.accessible)
                self.assertEqual((proof.observed_at, proof.valid_until), (1000, 1300))

    def test_identity_and_owner_mismatches_deny(self):
        for field, value in (("billing_owner_id", "other"), ("github_user_id", "43"),
                             ("repository_id", "124"), ("app_id", "9"), ("installation_id", "9")):
            with self.subTest(field=field):
                original = self.binding
                self.binding = replace(original, **{field: value})
                self.assertFalse(self.check().accessible)
                self.binding = original

    def test_write_or_public_visibility_is_not_maintenance(self):
        self.repo["permissions"] = dict(admin=False, maintain=False, push=True)
        self.assertFalse(self.check().accessible)

    def test_suspension_and_missing_module_permissions_deny(self):
        self.install["suspended_at"] = "2026-01-01T00:00:00Z"
        self.assertFalse(self.check().accessible)
        self.install["suspended_at"] = None
        self.install["permissions"].pop("pull_requests")
        self.assertFalse(self.check().accessible)

    def test_inconclusive_responses_do_not_revoke(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.responses[("/user", "secret-user")] = GitHubResponse(status, {}, {})
                with self.assertRaises(GitHubUnavailable):
                    self.check()

    def test_malformed_authority_is_inconclusive(self):
        for payload in ({}, dict(id=True), [], dict(id=42.0)):
            with self.subTest(payload=payload):
                self.responses[("/user", "secret-user")] = payload
                with self.assertRaises(GitHubUnavailable):
                    self.check()

    def test_exception_and_repr_do_not_expose_credentials(self):
        self.responses[("/user", "secret-user")] = RuntimeError("secret-user")
        with self.assertRaises(GitHubUnavailable) as error:
            self.check()
        self.assertNotIn("secret", str(error.exception))
        self.assertNotIn("secret", repr(self.binding))

    def test_personal_watch_requires_current_owner_and_release_read(self):
        self.target.update(module="updates", owner_id="usr_1", target_repository_id=None)
        self.repo["private"] = True
        self.assertTrue(self.check().accessible)
        self.assertIn(("/repos/org/repo/releases?per_page=1", "secret-user"), self.calls)
        self.assertFalse(any("installation" in path for path, token in self.calls))
        self.target["owner_id"] = "other"
        self.assertFalse(self.check().accessible)

    def test_shared_watch_checks_target_maintenance_and_public_upstream(self):
        self.target.update(module="updates", owner_id="usr_1", target_repository_id="github:456",
                           target_installation_id="8", target_billing_owner_id="usr_1")
        self.binding = replace(self.binding, target_repository_id="456", target_repository_full_name="org/target")
        target_repo = dict(id=456, permissions=dict(maintain=True, admin=False))
        self.responses[("/repos/org/target", "secret-user")] = target_repo
        self.responses[("/repos/org/target/installation", "secret-app")] = self.install
        self.assertTrue(self.check().accessible)
        self.assertIn(("/repos/org/target/installation", "secret-app"), self.calls)
        self.repo["private"] = True
        self.assertFalse(self.check().accessible)
        self.repo["private"] = False
        target_repo["permissions"]["maintain"] = False
        self.assertFalse(self.check().accessible)

    def test_shared_watch_binds_target_installation_owner_and_app(self):
        self.target.update(module="updates", owner_id="usr_1", target_repository_id="github:456",
                           target_installation_id="8", target_billing_owner_id="usr_1")
        self.binding = replace(self.binding, target_repository_id="456", target_repository_full_name="org/target")
        self.responses[("/repos/org/target", "secret-user")] = dict(id=456, permissions=dict(maintain=True, admin=False))
        self.responses[("/repos/org/target/installation", "secret-app")] = self.install
        for field, value in (("target_installation_id", "9"), ("target_billing_owner_id", "other"),
                             ("app_id", "9")):
            with self.subTest(field=field):
                original = self.target[field]
                self.target[field] = value
                self.assertFalse(self.check().accessible)
                self.target[field] = original

    def test_absent_binding_is_denial_and_bad_path_is_inconclusive(self):
        self.binding = None
        self.assertFalse(self.check().accessible)
        self.setUp()
        self.binding = replace(self.binding, repository_full_name="org/../secrets")
        with self.assertRaises(GitHubUnavailable):
            self.check()

    def test_missing_permission_boolean_is_not_assumed_denial(self):
        self.repo["permissions"] = dict(push=True)
        with self.assertRaises(GitHubUnavailable):
            self.check()

    def test_module_endpoint_failure_is_inconclusive(self):
        self.responses[("/repos/org/repo/pulls?per_page=1", "secret-install")] = GitHubResponse(403, {}, {})
        with self.assertRaises(GitHubUnavailable):
            self.check()

    def test_rate_limit_deadline_survives_checker(self):
        self.responses[("/user", "secret-user")] = GitHubUnavailable("GITHUB_RATE_LIMITED", retry_at=1600)
        with self.assertRaises(GitHubUnavailable) as error:
            self.check()
        self.assertEqual(error.exception.retry_at, 1600)

    def test_adapter_unavailable_message_is_sanitized(self):
        self.responses[("/user", "secret-user")] = GitHubUnavailable("secret-user", retry_at=1600)
        with self.assertRaises(GitHubUnavailable) as error:
            self.check()
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(error.exception.retry_at, 1600)

    def test_resolver_failure_is_sanitized(self):
        def broken(target):
            raise RuntimeError("secret-install")
        checker = GitHubAuthorizationChecker(get_json=self.get, resolve_binding=broken)
        with self.assertRaises(GitHubUnavailable) as error:
            checker(target=self.target, now=1000)
        self.assertNotIn("secret", str(error.exception))

    def test_untrusted_target_token_does_not_replace_binding(self):
        self.target.update(user_token="attacker", github_user_id="999", repository_full_name="evil/repo")
        self.assertTrue(self.check().accessible)
        self.assertNotIn(("/user", "attacker"), self.calls)
