from __future__ import annotations

import gzip
import json

try:
    import security_contracts_base as _security_contracts_base
except ModuleNotFoundError:  # pragma: no cover - package-style unittest invocation
    from . import security_contracts_base as _security_contracts_base

globals().update(
    {name: getattr(_security_contracts_base, name) for name in dir(_security_contracts_base) if not name.startswith("_")}
)


class SecurityContractsPart07Test(SecurityContractsBase):
    def test_request_body_rejects_malformed_json_without_parser_details(self) -> None:
        handler = RawBodyRouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "1"},
            raw_body=b"{",
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")
    def test_request_body_rejects_non_utf8_json_without_decoder_details(self) -> None:
        handler = RawBodyRouteHarness(
            "/auth/sign-out",
            headers={"Content-Length": "1"},
            raw_body=b"\xff",
        )

        app.PullwiseHandler.route(handler, "POST")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["message"], "Request body must be valid JSON.")

    def test_unauthenticated_gzip_json_uses_small_decompression_limit(self) -> None:
        payload = json.dumps({"payload": "x" * 256}).encode("utf-8")
        raw_body = gzip.compress(payload)
        compressed_limit = len(raw_body) + 8
        decompressed_limit = len(payload) + 8
        handler = RawBodyRouteHarness(
            "/auth/sign-out",
            headers={"Content-Encoding": "gzip", "Content-Length": str(len(raw_body))},
            raw_body=raw_body,
        )

        with patch.dict(
            os.environ,
            {
                "PULLWISE_MAX_BODY_BYTES": str(compressed_limit),
                "PULLWISE_MAX_DECOMPRESSED_BODY_BYTES": str(decompressed_limit),
            },
            clear=False,
        ):
            app.PullwiseHandler.route(handler, "POST")

        self.assertLess(len(raw_body), compressed_limit)
        self.assertLess(len(payload), decompressed_limit)
        self.assertGreater(len(payload), compressed_limit)
        self.assertEqual(handler.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(handler.payload["message"], "Request body is too large after decompression.")


__all__ = ["SecurityContractsPart07Test"]
