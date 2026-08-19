from __future__ import annotations

import json
from pathlib import Path
import unittest

from pullwise_server.reviewer.canonical import (
    canonical_bytes,
    canonical_sha256,
    decode_strict_json,
    require_registered_value,
)


ROOT = Path(__file__).resolve().parents[2]
REGISTRY = json.loads(
    (ROOT / "contracts" / "pullwise-review" / "v1" / "registry.json").read_text(
        encoding="utf-8"
    )
)["registries"]


class CanonicalJsonTest(unittest.TestCase):
    def test_frozen_vectors_match_exact_bytes_and_digests(self) -> None:
        vectors = (
            ({}, b"{}", "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
            (
                {"b": "x", "a": 1},
                b'{"a":1,"b":"x"}',
                "sha256:ecf9e98ec0641e23113ff3ce8bdc78d0ddd249886517fd4a7f68cc83d4e65667",
            ),
            (
                {"text": "审查"},
                '{"text":"审查"}'.encode(),
                "sha256:22fb58b4ca59907c273ff22aed489c7c9a43087d64d245bea2e50d035fb663ce",
            ),
        )

        for value, expected_bytes, expected_digest in vectors:
            with self.subTest(value=value):
                self.assertEqual(expected_bytes, canonical_bytes(value))
                self.assertEqual(expected_digest, canonical_sha256(value))

    def test_strict_decoder_rejects_forbidden_json(self) -> None:
        forbidden = (
            b"\xef\xbb\xbf{}",
            b'{"text":"\xff"}',
            b'{"a":1,"a":2}',
            b'{"number":1.5}',
            b'{"number":-0}',
            b'{"number":9007199254740992}',
            '{"e\u0301":"value"}'.encode(),
            '{"text":"e\u0301"}'.encode(),
        )

        for raw in forbidden:
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    decode_strict_json(raw)

    def test_registry_rejects_every_unregistered_value(self) -> None:
        for registry_name, allowed_values in REGISTRY.items():
            with self.subTest(registry_name=registry_name, case="registered"):
                self.assertEqual(
                    allowed_values[0],
                    require_registered_value(
                        REGISTRY, registry_name, allowed_values[0]
                    ),
                )
            with self.subTest(registry_name=registry_name, case="unregistered"):
                with self.assertRaises(ValueError):
                    require_registered_value(
                        REGISTRY, registry_name, "__UNREGISTERED__"
                    )


if __name__ == "__main__":
    unittest.main()
