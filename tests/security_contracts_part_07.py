from __future__ import annotations

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


__all__ = ["SecurityContractsPart07Test"]
