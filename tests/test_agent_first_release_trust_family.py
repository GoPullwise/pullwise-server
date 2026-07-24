from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
FAMILY_PATH = (
    ROOT
    / "contracts"
    / "agent-first"
    / "current"
    / "source"
    / "families"
    / "release-trust-authority.json"
)


class AgentFirstReleaseTrustFamilyTest(unittest.TestCase):
    def test_external_root_is_closed_and_organization_scoped(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schemas = {item["$id"]: item for item in family["schemas"]}

        root = schemas["release-trust-root/v1"]

        self.assertEqual("release-trust-authority", family["family_id"])
        self.assertIs(False, root["additionalProperties"])
        self.assertEqual(set(root["required"]), set(root["properties"]))
        self.assertEqual(
            {
                "schema_id",
                "trust_root_id",
                "organization_id",
                "root_principal_id",
                "root_key_id",
                "signature_algorithm",
                "public_key",
                "issued_at",
                "expires_at",
                "root_digest",
            },
            set(root["required"]),
        )
        self.assertNotIn("signature", root["properties"])
        self.assertEqual(
            "^[A-Za-z0-9_-]{43}$",
            root["properties"]["public_key"]["pattern"],
        )
        self.assertEqual(
            {
                "field": "root_digest",
                "domain": "pullwise:release-trust-root:v1",
            },
            root["x-pullwise-digest"],
        )


if __name__ == "__main__":
    unittest.main()
