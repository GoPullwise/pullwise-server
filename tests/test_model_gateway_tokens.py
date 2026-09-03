from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pullwise_server import db
from pullwise_server.model_gateway_tokens import (
    GatewayGrantStatus,
    GatewayTokenAuthority,
    GatewayTokenError,
    GatewayTokenVerifier,
)
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections


class ModelGatewayTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(os.environ, {"PULLWISE_DB_PATH": self.db_path}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        install_initialized_db_template(self.db_path)

    def test_worker_token_is_signed_scoped_and_only_its_hash_is_persisted(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        authority = GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=private_key,
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: 1_788_259_200,
            jti_factory=lambda: "gtj_fixed",
        )

        grant = authority.issue(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="a" * 64,
            route_ids=["gpt-primary"],
            generation=3,
        )

        claims = GatewayTokenVerifier(
            public_keys={"gateway-signing-2026-09": private_key.public_key()},
            issuer="https://api.pull-wise.com",
            clock=lambda: 1_788_259_201,
            grant_status_resolver=lambda _jti, _token_hash: GatewayGrantStatus(
                active=True,
                worker_enabled=True,
                generation=3,
                desired_profile_revision=12,
            ),
        ).verify(
            grant.token,
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="a" * 64,
            route_id="gpt-primary",
        )
        self.assertEqual(claims["aud"], "pullwise-model-gateway")
        self.assertEqual(claims["sub"], "worker_a")
        self.assertEqual(claims["routes"], ["gpt-primary"])
        self.assertEqual(claims["manifest_digest"], "a" * 64)
        self.assertEqual(grant.expires_at, 1_788_259_500)

        connection = sqlite3.connect(self.db_path)
        try:
            row = connection.execute(
                "SELECT jti, token_hash, worker_id, profile_set_id, profile_revision, "
                "route_ids_json, generation, expires_at, revoked_at "
                "FROM gateway_token_grants"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            row,
            (
                "gtj_fixed",
                grant.token_hash,
                "worker_a",
                "reviewer-production",
                12,
                '["gpt-primary"]',
                3,
                1_788_259_500,
                None,
            ),
        )
        self.assertNotIn(grant.token, repr(row))

    def test_revoked_or_stale_grant_is_rejected_after_signature_verification(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        authority = GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=private_key,
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: 1_788_259_200,
            jti_factory=lambda: "gtj_revoked",
        )
        grant = authority.issue(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="b" * 64,
            route_ids=["gpt-primary"],
            generation=3,
        )
        verifier = GatewayTokenVerifier(
            public_keys={"gateway-signing-2026-09": private_key.public_key()},
            issuer="https://api.pull-wise.com",
            clock=lambda: 1_788_259_201,
            grant_status_resolver=lambda _jti, _token_hash: GatewayGrantStatus(
                active=False,
                worker_enabled=True,
                generation=3,
                desired_profile_revision=12,
            ),
        )

        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_TOKEN_REVOKED"):
            verifier.verify(
                grant.token,
                worker_id="worker_a",
                profile_set_id="reviewer-production",
                profile_revision=12,
                manifest_digest="b" * 64,
                route_id="gpt-primary",
            )

    def test_expiry_cross_worker_scope_and_wrong_audience_fail_closed(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        authority = GatewayTokenAuthority(
            connect_factory=db.connect,
            private_key=private_key,
            key_id="gateway-signing-2026-09",
            issuer="https://api.pull-wise.com",
            ttl_seconds=300,
            clock=lambda: 1_788_259_200,
            jti_factory=lambda: "gtj_boundaries",
        )
        grant = authority.issue(
            worker_id="worker_a",
            profile_set_id="reviewer-production",
            profile_revision=12,
            manifest_digest="c" * 64,
            route_ids=["gpt-primary"],
            generation=3,
        )

        def verifier(now: int, *, generation: int = 3, desired_revision: int = 12):
            return GatewayTokenVerifier(
                public_keys={"gateway-signing-2026-09": private_key.public_key()},
                issuer="https://api.pull-wise.com",
                clock=lambda: now,
                grant_status_resolver=lambda _jti, _hash: GatewayGrantStatus(
                    active=True,
                    worker_enabled=True,
                    generation=generation,
                    desired_profile_revision=desired_revision,
                ),
            )

        arguments = {
            "worker_id": "worker_a",
            "profile_set_id": "reviewer-production",
            "profile_revision": 12,
            "manifest_digest": "c" * 64,
            "route_id": "gpt-primary",
        }
        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_TOKEN_EXPIRED"):
            verifier(1_788_259_500).verify(grant.token, **arguments)
        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_TOKEN_SCOPE_DENIED"):
            verifier(1_788_259_201).verify(grant.token, **{**arguments, "worker_id": "worker_b"})
        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_TOKEN_SCOPE_DENIED"):
            verifier(1_788_259_201).verify(grant.token, **{**arguments, "route_id": "deepseek-primary"})
        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_TOKEN_GENERATION_STALE"):
            verifier(1_788_259_201, generation=4).verify(grant.token, **arguments)
        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_PROFILE_REVISION_STALE"):
            verifier(1_788_259_201, desired_revision=13).verify(grant.token, **arguments)

        encoded_header, encoded_claims, _signature = grant.token.split(".")
        claims = json.loads(base64.urlsafe_b64decode(encoded_claims + "=" * (-len(encoded_claims) % 4)))
        claims["aud"] = "pullwise-server"
        wrong_claims = base64.urlsafe_b64encode(
            json.dumps(claims, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
        ).rstrip(b"=").decode("ascii")
        signing_input = f"{encoded_header}.{wrong_claims}"
        wrong_audience = (
            f"{signing_input}."
            f"{base64.urlsafe_b64encode(private_key.sign(signing_input.encode('ascii'))).rstrip(b'=').decode('ascii')}"
        )
        with self.assertRaisesRegex(GatewayTokenError, "GATEWAY_TOKEN_AUDIENCE_INVALID"):
            verifier(1_788_259_201).verify(wrong_audience, **arguments)


if __name__ == "__main__":
    unittest.main()
