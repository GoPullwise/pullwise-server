import unittest

from pullwise_server.api_key_dto_rules import (
    DEFAULT_SCOPES, requested_api_key_scopes, parse_api_key_restrictions,
)
from pullwise_server.cloudflare_ledger_auth import ROLE_SCOPES, target_allowed


class LedgerApiKeyRulesTests(unittest.TestCase):
    def test_default_is_read_only_and_old_scopes_are_rejected(self):
        self.assertTrue(DEFAULT_SCOPES)
        self.assertTrue(all(scope.endswith(":read") for scope in DEFAULT_SCOPES))
        self.assertEqual(requested_api_key_scopes(None, provided=False)[0], DEFAULT_SCOPES)
        self.assertIsNotNone(requested_api_key_scopes(["items:read"], provided=True)[1])
        self.assertEqual(requested_api_key_scopes(["expenses:write"], provided=True)[0], ["expenses:write"])

    def test_project_and_shared_permissions_are_independent(self):
        restricted = parse_api_key_restrictions({"projectIds": ["prj_a", "prj_a"], "shared": False})
        self.assertEqual(restricted, {"projectIds": ["prj_a"], "shared": False})
        self.assertTrue(target_allowed(restricted, "project", "prj_a"))
        self.assertFalse(target_allowed(restricted, "project", "prj_b"))
        self.assertFalse(target_allowed(restricted, "shared", None))
        self.assertTrue(target_allowed({"projectIds": [], "shared": True}, "shared", None))
        self.assertFalse(target_allowed({"projectIds": [], "shared": True}, "project", "prj_a"))

    def test_explicit_member_scopes_intersect_ledger_roles(self):
        scopes, error = requested_api_key_scopes(["members:read", "members:write"], provided=True)
        self.assertIsNone(error)
        self.assertEqual(scopes, ["members:read", "members:write"])
        self.assertNotIn("members:read", DEFAULT_SCOPES)
        for role in ROLE_SCOPES:
            self.assertIn("members:read", ROLE_SCOPES[role])
            self.assertEqual("members:write" in ROLE_SCOPES[role], role in {"owner", "admin"})

    def test_restriction_shape_fails_closed(self):
        for value in ({"repositoryIds": ["old"]}, {"projectIds": ["prj_a"], "shared": "yes"},
                      {"projectIds": ["prj_a", 5]}, {"projectIds": ["prj_a"], "kind": "audit_bundle"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_api_key_restrictions(value)


if __name__ == "__main__":
    unittest.main()
