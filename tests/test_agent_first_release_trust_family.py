from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from types import ModuleType
import unittest

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server.agent_first_contract_bundle_npm import render_npm_wrapper
from pullwise_server.agent_first_contract_bundle_python import render_python_wrapper
from pullwise_server.agent_first_contract_bundle_source import (
    canonical_bytes,
    load_family,
)


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

    def test_external_root_has_exact_golden_and_negative_fixtures(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        loaded = load_family(
            FAMILY_PATH,
            "release-trust-authority",
            {},
            set(),
        )
        fixtures = {
            item["fixture_id"]: item for item in loaded["fixtures"]
            if item["schema_id"] == "release-trust-root/v1"
        }

        self.assertEqual(
            {
                "release_trust_root_golden_external_pin",
                "release_trust_root_idempotency_external_pin",
                "release_trust_root_negative_public_key",
            },
            set(fixtures),
        )
        golden = fixtures["release_trust_root_golden_external_pin"]["document"]
        unsigned = {
            key: value for key, value in golden.items() if key != "root_digest"
        }
        self.assertEqual(
            hashlib.sha256(
                b"pullwise:release-trust-root:v1\0"
                + canonical_bytes(unsigned)
            ).hexdigest(),
            golden["root_digest"],
        )
        self.assertEqual(43, len(golden["public_key"]))
        negative = fixtures["release_trust_root_negative_public_key"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", negative["expected_code"])
        self.assertNotEqual(43, len(negative["document"]["public_key"]))
        self.assertEqual(family["fixtures"], loaded["fixtures"])

    def test_principal_has_signed_golden_idempotency_and_negative_fixtures(
        self,
    ) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        fixtures = {
            item["fixture_id"]: item
            for item in family["fixtures"]
            if item["schema_id"] == "release-principal/v1"
        }

        self.assertEqual(
            {
                "release_principal_golden_benchmark_owner",
                "release_principal_idempotency_benchmark_owner",
                "release_principal_negative_time_order",
            },
            set(fixtures),
        )
        golden = fixtures["release_principal_golden_benchmark_owner"][
            "document"
        ]
        idempotent = fixtures[
            "release_principal_idempotency_benchmark_owner"
        ]["document"]
        self.assertEqual(canonical_bytes(golden), canonical_bytes(idempotent))
        projected = {
            key: value
            for key, value in golden.items()
            if key not in {"signature", "principal_digest"}
        }
        root = next(
            item["document"]
            for item in family["fixtures"]
            if item["fixture_id"] == "release_trust_root_golden_external_pin"
        )
        public_key = base64.urlsafe_b64decode(root["public_key"] + "=")
        Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(
                "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
            )
        ).public_key().verify(
            base64.urlsafe_b64decode(golden["signature"] + "=="),
            b"pullwise-release-principal/v1\0" + canonical_bytes(projected),
        )
        self.assertEqual(32, len(public_key))
        negative = fixtures["release_principal_negative_time_order"]
        self.assertEqual("CONTRACT_DOCUMENT_INVALID", negative["expected_code"])
        self.assertGreater(
            negative["document"]["issued_at"],
            negative["document"]["expires_at"],
        )

    def test_external_root_canonical_bytes_are_idempotent(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        fixtures = {item["fixture_id"]: item for item in family["fixtures"]}
        core = json.loads(
            (FAMILY_PATH.parent / "core.json").read_text(encoding="utf-8")
        )
        error_family = json.loads(
            (FAMILY_PATH.parent / "receipt-error.json").read_text(
                encoding="utf-8"
            )
        )
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical_bundle = canonical_bytes(
            {
                "root_manifest": {
                    "schema_registry": [
                        {
                            "schema_id": schema["$id"],
                            "role": "public_document",
                        }
                        for schema in core["schemas"] + family["schemas"]
                    ]
                },
                "families": [
                    core,
                    family,
                    {
                        "family_id": "receipt-error",
                        "schemas": [],
                        "fixtures": [error_fixture],
                    },
                ],
            }
        )
        wrapper_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"release-trust-root-test").hexdigest(),
            hashlib.sha256(canonical_bundle).hexdigest(),
            canonical_bundle,
        )
        wrapper = ModuleType("_release_trust_root_wrapper")
        exec(wrapper_bytes, wrapper.__dict__)
        golden = fixtures["release_trust_root_golden_external_pin"]["document"]
        idempotent = fixtures[
            "release_trust_root_idempotency_external_pin"
        ]["document"]

        self.assertEqual(
            wrapper.canonical_validated_bytes("release-trust-root/v1", golden),
            wrapper.canonical_validated_bytes(
                "release-trust-root/v1", idempotent
            ),
        )

    def test_principal_is_root_signed_and_has_one_organization_role(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        schemas = {item["$id"]: item for item in family["schemas"]}

        principal = schemas["release-principal/v1"]

        self.assertIs(False, principal["additionalProperties"])
        self.assertEqual(set(principal["required"]), set(principal["properties"]))
        self.assertEqual(
            ["benchmark_owner", "release_operator"],
            principal["properties"]["role"]["enum"],
        )
        self.assertEqual(
            "release-trust-root/v1",
            principal["properties"]["trust_root_ref"][
                "x-pullwise-content-schema-id"
            ],
        )
        self.assertEqual(
            "^[A-Za-z0-9_-]{86}$",
            principal["properties"]["signature"]["pattern"],
        )
        self.assertEqual(
            {
                "algorithm": "Ed25519",
                "domain": "pullwise-release-principal/v1",
                "domain_separator": "NUL",
                "encoding": "base64url_no_padding",
                "signed_projection": "document_without_signature_and_digest",
            },
            principal["x-pullwise-semantics"]["signature_contract"],
        )

    def test_principal_signature_message_is_exact_and_verifiable(self) -> None:
        family = json.loads(FAMILY_PATH.read_text(encoding="utf-8"))
        fixtures = {item["fixture_id"]: item for item in family["fixtures"]}
        root = fixtures["release_trust_root_golden_external_pin"]["document"]
        root_bytes = canonical_bytes(root)
        unsigned = {
            "schema_id": "release-principal/v1",
            "principal_id": "principal_benchmark_owner",
            "organization_id": root["organization_id"],
            "role": "benchmark_owner",
            "trust_root_id": root["trust_root_id"],
            "trust_root_ref": {
                "schema_id": "content-ref/v1",
                "artifact_id": "art_11111111111111111111111111111111",
                "content_schema_id": "release-trust-root/v1",
                "sha256": hashlib.sha256(root_bytes).hexdigest(),
                "size_bytes": len(root_bytes),
                "media_type": "application/json",
                "encoding": "utf-8",
            },
            "trust_root_digest": root["root_digest"],
            "signer_id": root["root_principal_id"],
            "key_id": root["root_key_id"],
            "signature_algorithm": "Ed25519",
            "issued_at": "2026-07-02T00:00:00.000Z",
            "expires_at": "2027-06-30T00:00:00.000Z",
        }
        expected_message = (
            b"pullwise-release-principal/v1\0" + canonical_bytes(unsigned)
        )
        private_key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(
                "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
            )
        )
        signature = base64.urlsafe_b64encode(
            private_key.sign(expected_message)
        ).decode("ascii").rstrip("=")
        signed = {**unsigned, "signature": signature}
        complete = {
            **signed,
            "principal_digest": hashlib.sha256(
                b"pullwise:release-principal:v1\0" + canonical_bytes(signed)
            ).hexdigest(),
        }
        core = json.loads(
            (FAMILY_PATH.parent / "core.json").read_text(encoding="utf-8")
        )
        error_family = json.loads(
            (FAMILY_PATH.parent / "receipt-error.json").read_text(
                encoding="utf-8"
            )
        )
        error_fixture = next(
            item
            for item in error_family["fixtures"]
            if item["fixture_id"] == "error_golden_current_registry"
        )
        canonical_bundle = canonical_bytes(
            {
                "root_manifest": {
                    "schema_registry": [
                        {
                            "schema_id": schema["$id"],
                            "role": "public_document",
                        }
                        for schema in core["schemas"] + family["schemas"]
                    ]
                },
                "families": [
                    core,
                    family,
                    {
                        "family_id": "receipt-error",
                        "schemas": [],
                        "fixtures": [error_fixture],
                    },
                ],
            }
        )
        wrapper_bytes = render_python_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"release-trust-signature-test").hexdigest(),
            hashlib.sha256(canonical_bundle).hexdigest(),
            canonical_bundle,
        )
        wrapper = ModuleType("_release_trust_signature_wrapper")
        exec(wrapper_bytes, wrapper.__dict__)

        self.assertEqual(
            expected_message,
            wrapper.signature_message("release-principal/v1", complete),
        )
        private_key.public_key().verify(
            base64.urlsafe_b64decode(signature + "=="),
            wrapper.signature_message("release-principal/v1", complete),
        )

        npm_wrapper = render_npm_wrapper(
            "@pullwise/agent-task-contract",
            "0.1.0",
            hashlib.sha256(b"release-trust-signature-test").hexdigest(),
            hashlib.sha256(canonical_bundle).hexdigest(),
            canonical_bundle,
        )
        with tempfile.TemporaryDirectory(prefix="release-trust-signature-") as scratch:
            scratch_path = Path(scratch)
            facade_path = scratch_path / "facade.mjs"
            runner_path = scratch_path / "runner.mjs"
            facade_path.write_bytes(npm_wrapper)
            runner_path.write_text(
                "\n".join(
                    (
                        "import {signatureMessage, signature_message} from './facade.mjs';",
                        f"const document = {json.dumps(complete, separators=(',', ':'))};",
                        "const camel = await signatureMessage('release-principal/v1', document);",
                        "const snake = await signature_message('release-principal/v1', document);",
                        "process.stdout.write(JSON.stringify({",
                        "  camel: Buffer.from(camel).toString('base64url'),",
                        "  snake: Buffer.from(snake).toString('base64url'),",
                        "}));",
                    )
                ),
                encoding="utf-8",
            )
            completed = subprocess.run(
                ["node", str(runner_path)],
                check=True,
                capture_output=True,
                encoding="utf-8",
            )
        expected_encoded = base64.urlsafe_b64encode(expected_message).decode(
            "ascii"
        ).rstrip("=")
        self.assertEqual(
            {"camel": expected_encoded, "snake": expected_encoded},
            json.loads(completed.stdout),
        )


if __name__ == "__main__":
    unittest.main()
