from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

from pullwise_server.agent_first_contract_bundle import (
    FIXTURE_CLASSES,
    REQUIRED_FAMILIES,
    ContractBundleError,
    build_bundle,
    generated_paths,
)
from pullwise_server import _generated_agent_task_contract as python_wrapper


REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = REPO_ROOT / "contracts" / "agent-first" / "current" / "source"


class AgentFirstContractBundleTest(unittest.TestCase):
    def test_layered_root_is_complete_atomic_and_canonical(self) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        root = bundle.document["root_manifest"]
        families = bundle.document["families"]

        self.assertEqual(list(REQUIRED_FAMILIES), root["required_families"])
        self.assertEqual(
            list(REQUIRED_FAMILIES),
            [family["family_id"] for family in families],
        )
        self.assertEqual(
            list(REQUIRED_FAMILIES),
            [family["family_id"] for family in root["families"]],
        )
        self.assertEqual(
            hashlib.sha256(bundle.canonical_bytes).hexdigest(),
            bundle.content_sha256,
        )
        self.assertEqual(root["root_sha256"], bundle.root_sha256)

        registered: set[str] = set()
        fixture_classes: set[str] = set()
        for family in families:
            schemas = {
                item["$id"]: item for item in family["schemas"]
            }
            registry = {
                item["schema_id"]: item for item in family["registry"]
            }
            self.assertEqual(set(schemas), set(registry))
            self.assertTrue(schemas)
            for schema_id, schema in schemas.items():
                self.assertEqual(
                    hashlib.sha256(_canonical_bytes(schema)).hexdigest(),
                    registry[schema_id]["sha256"],
                )
            registered.update(schemas)
            fixture_classes.update(
                fixture["fixture_class"] for fixture in family["fixtures"]
            )

        self.assertEqual(set(FIXTURE_CLASSES), fixture_classes)
        self.assertEqual(
            registered,
            {edge["schema_id"] for edge in root["reference_dag"]},
        )

    def test_missing_required_family_makes_publication_impossible(self) -> None:
        for family_id in REQUIRED_FAMILIES:
            with self.subTest(family_id=family_id), tempfile.TemporaryDirectory(
                prefix="agent-contract-family-"
            ) as scratch:
                copied = Path(scratch) / "source"
                shutil.copytree(SOURCE_ROOT, copied)
                (copied / "families" / f"{family_id}.json").unlink()

                with self.assertRaisesRegex(
                    ContractBundleError,
                    f"required_family_missing: {re.escape(family_id)}",
                ):
                    build_bundle(copied)

    def test_python_and_npm_wrappers_embed_identical_canonical_bytes(self) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        paths = generated_paths(REPO_ROOT)
        published_bytes = paths.bundle.read_bytes()
        npm_text = paths.npm_index.read_text(encoding="utf-8")
        match = re.search(
            r'^export const BUNDLE_BASE64 = "([A-Za-z0-9+/=]+)";$',
            npm_text,
            flags=re.MULTILINE,
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(bundle.canonical_bytes, published_bytes)
        self.assertEqual(bundle.canonical_bytes, python_wrapper.bundle_bytes())
        self.assertEqual(bundle.canonical_bytes, base64.b64decode(match.group(1)))
        self.assertEqual(bundle.content_sha256, python_wrapper.CONTENT_SHA256)
        self.assertEqual(bundle.root_sha256, python_wrapper.ROOT_SHA256)

    def test_generated_artifacts_are_current_and_wrapper_lock_is_exact(self) -> None:
        bundle = build_bundle(SOURCE_ROOT)
        paths = generated_paths(REPO_ROOT)
        npm_package = json.loads(paths.npm_package.read_text(encoding="utf-8"))

        self.assertEqual(bundle.root_bytes, paths.root_manifest.read_bytes())
        self.assertEqual(bundle.canonical_bytes, paths.bundle.read_bytes())
        self.assertEqual(bundle.python_wrapper, paths.python_wrapper.read_bytes())
        self.assertEqual(bundle.npm_wrapper, paths.npm_index.read_bytes())
        self.assertEqual(bundle.npm_package, paths.npm_package.read_bytes())
        self.assertEqual(bundle.package_identity, python_wrapper.PACKAGE_IDENTITY)
        self.assertEqual(bundle.package_version, python_wrapper.PACKAGE_VERSION)
        self.assertEqual(bundle.package_identity, npm_package["name"])
        self.assertEqual(bundle.package_version, npm_package["version"])
        self.assertEqual(bundle.content_sha256, npm_package["pullwiseContentSha256"])
        self.assertEqual(bundle.root_sha256, npm_package["pullwiseRootSha256"])


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
