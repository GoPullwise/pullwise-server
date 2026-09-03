from __future__ import annotations

import unittest

from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from tests.model_gateway_fakes import RecordingSecretWriter


class ModelGatewayEndpointSecurityTest(unittest.TestCase):
    def test_provider_endpoint_must_match_the_selected_official_provider(self) -> None:
        def forbidden_connect():
            raise AssertionError("invalid endpoint reached persistence")

        service = ModelGatewayControlPlane(
            connect_factory=forbidden_connect,
            secret_writer=RecordingSecretWriter(),
            clock=lambda: 1_788_259_200,
        )
        for endpoint in (
            "https://attacker.example",
            "https://127.0.0.1",
            "https://169.254.169.254",
            "https://api.openai.com/redirect",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaisesRegex(ValueError, "official origin"):
                service.create_provider_connection(
                    actor_user_id="usr_admin",
                    request_id="req_ssrf",
                    payload={
                        "provider_connection_id": "openai-production",
                        "display_name": "OpenAI Production",
                        "provider": "openai",
                        "adapter": "openai-completions",
                        "endpoint_origin": endpoint,
                        "secret": "not-written",
                    },
                )


if __name__ == "__main__":
    unittest.main()
