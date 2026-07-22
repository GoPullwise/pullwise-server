from __future__ import annotations

from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import unittest


HEX64 = re.compile(r"^[0-9a-f]{64}$")
CONTENT_SCHEMA = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*/v[1-9][0-9]*$")
STABLE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,95}$")
TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sealed(document: dict[str, object], schema: dict[str, object]) -> bool:
    spec = schema.get("x-pullwise-digest")
    if not isinstance(spec, dict):
        return True
    field, domain = spec["field"], spec["domain"]
    unsigned = {key: value for key, value in document.items() if key != field}
    expected = hashlib.sha256(
        domain.encode("utf-8") + b"\0" + canonical_bytes(unsigned)
    ).hexdigest()
    return document.get(field) == expected


def ordered_unique(values: list[object], key) -> bool:
    keys = [key(value) for value in values]
    return keys == sorted(keys) and len(keys) == len(set(keys))


def valid_content_ref(value: object, targets: set[str] | None = None) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_id",
        "artifact_id",
        "content_schema_id",
        "sha256",
        "size_bytes",
        "media_type",
        "encoding",
    }:
        return False
    target = value.get("content_schema_id")
    return (
        value.get("schema_id") == "content-ref/v1"
        and isinstance(value.get("artifact_id"), str)
        and re.fullmatch(r"art_[0-9a-f]{32}", value["artifact_id"])
        and isinstance(target, str)
        and CONTENT_SCHEMA.fullmatch(target) is not None
        and (targets is None or target in targets)
        and isinstance(value.get("sha256"), str)
        and HEX64.fullmatch(value["sha256"]) is not None
        and isinstance(value.get("size_bytes"), int)
        and 0 <= value["size_bytes"] <= 9007199254740991
        and isinstance(value.get("media_type"), str)
        and bool(value["media_type"])
        and value.get("encoding") in {"binary", "utf-8"}
    )


def valid_availability(value: object, targets: set[str]) -> bool:
    if not isinstance(value, dict):
        return False
    branch = value.get("availability")
    if branch == "available":
        return set(value) == {"availability", "ref"} and valid_content_ref(
            value["ref"], targets
        )
    return (
        branch in {"unavailable", "not_applicable"}
        and set(value) == {"availability", "reason_code"}
        and isinstance(value.get("reason_code"), str)
        and STABLE_CODE.fullmatch(value["reason_code"]) is not None
    )


def valid_actor(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "schema_id",
        "kind",
        "id",
        "session_id",
    }:
        return False
    sessions = {
        "task_owner",
        "quality_verifier",
        "domain_reviewer",
        "explorer",
        "troubleshooter",
        "implementer",
    }
    controls = {
        "worker_control",
        "server_control",
        "user_control",
        "system_reconciler",
    }
    kind, session = value.get("kind"), value.get("session_id")
    return (
        value.get("schema_id") == "actor/v1"
        and isinstance(value.get("id"), str)
        and bool(value["id"])
        and (
            kind in sessions
            and isinstance(session, str)
            and re.fullmatch(r"sess_[0-9a-f]{32}", session) is not None
            or kind in controls
            and session is None
        )
    )


def timestamp_millis(value: object) -> int | None:
    if not isinstance(value, str) or TIMESTAMP.fullmatch(value) is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def object_nodes(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from object_nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from object_nodes(child)


class FamilyAssertions:
    family: dict[str, object]
    schemas: dict[str, dict[str, object]]

    @classmethod
    def load_family(cls, path: Path) -> None:
        cls.family = json.loads(path.read_text(encoding="utf-8"))
        cls.schemas = {item["$id"]: item for item in cls.family["schemas"]}

    def assert_family_contract(
        self: unittest.TestCase,
        family_id: str,
        schema_ids: tuple[str, ...],
        contextual_helpers: dict[str, list[str]] | None = None,
    ) -> None:
        contextual_helpers = contextual_helpers or {}
        self.assertEqual(family_id, self.family["family_id"])
        self.assertEqual(list(schema_ids), [item["$id"] for item in self.family["schemas"]])
        self.assertEqual(set(schema_ids), set(self.schemas))
        for schema_id in schema_ids:
            schema = self.schemas[schema_id]
            self.assertEqual("https://json-schema.org/draft/2020-12/schema", schema["$schema"])
            self.assertEqual("object", schema["type"])
            self.assertIs(False, schema["additionalProperties"])
            self.assertEqual(
                {
                    "document_rules": [
                        schema_id.removesuffix("/v1").replace("-", "_")
                    ],
                    "contextual_helpers": contextual_helpers.get(schema_id, []),
                },
                schema["x-pullwise-semantics"],
            )
            self.assertEqual(set(schema["required"]), set(schema["properties"]))
            for node in object_nodes(schema):
                if "pattern" in node:
                    self.assertIsInstance(node["pattern"], str, schema_id)

    def assert_fixture_matrix(
        self: unittest.TestCase,
        validators: dict[str, object],
    ) -> None:
        fixtures = self.family["fixtures"]
        self.assertEqual(
            sorted(item["fixture_id"] for item in fixtures),
            [item["fixture_id"] for item in fixtures],
        )
        matrix: dict[str, dict[str, list[dict[str, object]]]] = {}
        for fixture in fixtures:
            schema_id = fixture["schema_id"]
            matrix.setdefault(schema_id, {}).setdefault(
                fixture["fixture_class"], []
            ).append(fixture)
            document = fixture["document"]
            self.assertEqual(
                set(self.schemas[schema_id]["required"]),
                set(document),
                fixture["fixture_id"],
            )
            if fixture["fixture_class"] == "negative":
                self.assertEqual("CONTRACT_DOCUMENT_INVALID", fixture["expected_code"])
            else:
                self.assertIsNone(fixture["expected_code"])
        self.assertEqual(set(self.schemas), set(matrix))
        for schema_id, validator in validators.items():
            classes = matrix[schema_id]
            self.assertTrue(classes["golden"])
            self.assertTrue(classes["negative"])
            self.assertTrue(classes["idempotency"])
            golden = classes["golden"][0]["document"]
            retry = classes["idempotency"][0]["document"]
            self.assertTrue(validator(golden), schema_id)
            self.assertEqual(canonical_bytes(golden), canonical_bytes(retry))
            for fixture in classes["negative"]:
                self.assertFalse(validator(fixture["document"]), fixture["fixture_id"])
