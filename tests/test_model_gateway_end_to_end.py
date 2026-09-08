from __future__ import annotations

import ipaddress
import json
import os
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from pullwise_server import app, db
from pullwise_server.model_gateway_control_plane import ModelGatewayControlPlane
from pullwise_server.model_gateway_http import ModelGatewayHttpApplication, make_handler
from pullwise_server.model_gateway_introspection_client import (
    HttpGatewayRouteResolver,
    HttpGrantStatusResolver,
)
from pullwise_server.model_gateway_limits import GatewayLimiter, GatewayLimitPolicy
from pullwise_server.model_gateway_runtime import GatewayRoute, ModelGatewayRuntime
from pullwise_server.model_gateway_secret_broker import GatewaySecretBroker, HttpSecretWriter
from pullwise_server.model_gateway_secrets import EncryptedFileSecretStore
from pullwise_server.model_gateway_token_codec import GatewayTokenVerifier
from tests.db_template import install_initialized_db_template, start_fast_sqlite_connections
from tests.test_worker_admin_routes import reset_state


class FakeProviderValidator:
    def validate(self, **_kwargs: object) -> list[str]:
        return ["gpt-5.5"]

    def canary(self, **_kwargs: object) -> None:
        return None


class FakeCompletionAdapter:
    def __init__(self) -> None:
        self.calls: list[tuple[GatewayRoute, bytes, dict[str, object]]] = []

    def complete(
        self,
        route: GatewayRoute,
        secret: bytes,
        request: dict[str, object],
        *, cancellation=None,
    ) -> dict[str, object]:
        self.calls.append((route, secret, request))
        return {
            "id": "chatcmpl_local_gateway",
            "object": "chat.completion",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "fake-ready"}}],
        }

    def stream(self, *_args: object, **_kwargs: object):
        yield b"data: [DONE]\n\n"


def write_loopback_certificate(root: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Pullwise local test")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certificate_path = root / "loopback-ca.pem"
    key_path = root / "loopback-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def start_tls_server(server: object, certificate_path: Path, key_path: Path) -> threading.Thread:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


class ModelGatewayEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        reset_state()
        start_fast_sqlite_connections(self)
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "pullwise.sqlite3"
        self.certificate_path, self.tls_key_path = write_loopback_certificate(self.root)
        self.signing_key = Ed25519PrivateKey.generate()
        self.signing_key_path = self.root / "gateway-signing.pem"
        self.signing_key_path.write_bytes(
            self.signing_key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        self.environment = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": str(self.db_path),
                "PULLWISE_MODEL_GATEWAY_INTROSPECTION_TOKEN": "local-introspection-token",
                "PULLWISE_MODEL_GATEWAY_SIGNING_KEY_PATH": str(self.signing_key_path),
                "PULLWISE_MODEL_GATEWAY_SIGNING_KEY_ID": "gateway-local-test",
                "PULLWISE_MODEL_GATEWAY_TOKEN_ISSUER": "https://pullwise.local.test",
                "PULLWISE_MODEL_GATEWAY_TOKEN_TTL_SECONDS": "300",
                "PULLWISE_RATE_LIMIT_ENABLED": "false",
                "REQUESTS_CA_BUNDLE": str(self.certificate_path),
            },
            clear=False,
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        install_initialized_db_template(str(self.db_path))

    def test_admin_to_server_to_worker_to_gateway_secure_slice(self) -> None:
        secret = b"end-to-end-upstream-secret-SENTINEL"
        store = EncryptedFileSecretStore(
            root=self.root / "gateway-secrets",
            master_key=bytes(range(32)),
            version_factory=lambda: "version-local-1",
        )
        broker = GatewaySecretBroker(
            secret_store=store,
            write_token="local-broker-token",
            validator=FakeProviderValidator(),
        )

        def broker_transport(url: str, **kwargs: object) -> dict[str, object]:
            authorization = kwargs["headers"]["Authorization"]
            payload = kwargs["payload"]
            if url.endswith("/validate"):
                return broker.validate_candidate(authorization=authorization, payload=payload)
            if url.endswith("/canary"):
                return broker.canary_candidate(authorization=authorization, payload=payload)
            if url.endswith("/retire"):
                return broker.retire_version(authorization=authorization, payload=payload)
            return broker.put_candidate(authorization=authorization, payload=payload)

        identifiers = iter(range(20))
        control = ModelGatewayControlPlane(
            connect_factory=db.connect,
            secret_writer=HttpSecretWriter(
                broker_url="https://gateway.local/internal/provider-secrets",
                write_token="local-broker-token",
                transport=broker_transport,
            ),
            clock=lambda: int(time.time()),
            id_factory=lambda prefix: f"{prefix}_e2e_{next(identifiers)}",
        )
        provider = control.create_provider_connection(
            actor_user_id="usr_admin",
            request_id="req_provider",
            payload={
                "provider_connection_id": "openai-production",
                "display_name": "OpenAI Production",
                "provider": "openai",
                "adapter": "openai-completions",
                "endpoint_origin": "https://api.openai.com",
                "secret": secret.decode("ascii"),
            },
        )
        profile = control.publish_profile_set(
            actor_user_id="usr_admin",
            request_id="req_profile",
            payload={
                "profile_set_id": "reviewer-production",
                "display_name": "Reviewer production",
                "routes": [{
                    "route_id": "gpt-primary",
                    "provider_connection_id": "openai-production",
                    "provider": "pullwise-gateway",
                    "model_alias": "gpt-reviewer",
                    "upstream_model": "gpt-5.5",
                    "api": "openai-completions",
                    "enabled": True,
                }],
            },
        )
        worker = db.create_worker({"name": "End-to-end Worker", "provider": "unconfigured"})
        control.create_worker_pool(
            actor_user_id="usr_admin",
            request_id="req_pool",
            payload={
                "worker_pool_id": "reviewers-primary",
                "display_name": "Primary reviewers",
                "profile_set_id": "reviewer-production",
                "profile_revision": profile["revision"],
            },
        )
        control.bind_worker_to_pool(
            actor_user_id="usr_admin",
            request_id="req_bind",
            worker_id=worker["worker_id"],
            worker_pool_id="reviewers-primary",
        )

        server = app.PullwiseThreadingHTTPServer(("127.0.0.1", 0), app.PullwiseHandler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        start_tls_server(server, self.certificate_path, self.tls_key_path)
        server_url = f"https://localhost:{server.server_address[1]}"
        adapter = FakeCompletionAdapter()
        audit_events: list[dict[str, object]] = []
        gateway_runtime = ModelGatewayRuntime(
            token_verifier=GatewayTokenVerifier(
                public_keys={"gateway-local-test": self.signing_key.public_key()},
                issuer="https://pullwise.local.test",
                clock=lambda: int(time.time()),
                grant_status_resolver=HttpGrantStatusResolver(
                    introspection_url=f"{server_url}/internal/model-gateway/grants/introspect",
                    introspection_token="local-introspection-token",
                ),
            ),
            route_resolver=HttpGatewayRouteResolver(
                route_url=f"{server_url}/internal/model-gateway/routes/resolve",
                introspection_token="local-introspection-token",
            ),
            secret_store=store,
            adapters={"openai-completions": adapter},
            audit_sink=audit_events.append,
            request_id_factory=lambda: "mgr_end_to_end",
            clock=time.time,
            limiter=GatewayLimiter(
                policy=GatewayLimitPolicy(60, 600, 1, 16, 500_000, 10_000_000),
                clock=time.time,
            ),
        )
        gateway = app.PullwiseThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(ModelGatewayHttpApplication(runtime=gateway_runtime, secret_broker=broker)),
        )
        self.addCleanup(gateway.server_close)
        self.addCleanup(gateway.shutdown)
        start_tls_server(gateway, self.certificate_path, self.tls_key_path)
        gateway_url = f"https://localhost:{gateway.server_address[1]}/v1"
        os.environ["PULLWISE_MODEL_GATEWAY_URL"] = gateway_url

        profile_root = self.root / "worker-profiles"
        worker_state = self.root / "worker-state"
        worker_root = Path(__file__).resolve().parents[2] / "pullwise-worker"
        result = subprocess.run(
            ["node", "src/main.ts", "sync"],
            cwd=worker_root,
            env={
                **os.environ,
                "NODE_EXTRA_CA_CERTS": str(self.certificate_path),
                "PULLWISE_SERVER_URL": server_url,
                "PULLWISE_WORKER_ID": str(worker["worker_id"]),
                "PULLWISE_WORKER_TOKEN": str(worker["worker_token"]),
                "PULLWISE_PI_PROFILE_ROOT": str(profile_root),
                "PULLWISE_WORKER_STATE_ROOT": str(worker_state),
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pointer = json.loads((profile_root / "managed-current.json").read_text(encoding="utf-8"))
        generation_root = profile_root / "generations" / pointer["generation"]
        agent_root = generation_root / "profiles" / "gateway-reviewer-production"
        authorization = json.loads((agent_root / "auth.json").read_text(encoding="utf-8"))
        access_token = authorization["pullwise-gateway"]["key"]
        models = json.loads((agent_root / "models.json").read_text(encoding="utf-8"))
        completion_url = models["providers"]["pullwise-gateway"]["baseUrl"] + "/chat/completions"
        request = urllib.request.Request(
            completion_url,
            data=json.dumps({
                "model": "gpt-reviewer",
                "messages": [{"role": "user", "content": "sensitive prompt"}],
            }).encode("utf-8"),
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            method="POST",
        )
        tls_context = ssl.create_default_context(cafile=str(self.certificate_path))
        with urllib.request.urlopen(request, context=tls_context, timeout=10) as response:
            completion = json.loads(response.read())
        self.assertEqual(completion["id"], "chatcmpl_local_gateway")
        self.assertEqual(adapter.calls[0][1], secret)
        self.assertEqual(adapter.calls[0][2]["model"], "gpt-5.5")

        cross_worker = urllib.request.Request(
            completion_url.replace(f"/workers/{worker['worker_id']}/", "/workers/worker_other/"),
            data=json.dumps({"model": "gpt-reviewer", "messages": [{"role": "user", "content": "x"}]}).encode(),
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            urllib.request.urlopen(cross_worker, context=tls_context, timeout=10)
        self.assertEqual(rejected.exception.code, 401)

        self.assertNotIn(secret, self.db_path.read_bytes())
        for path in profile_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(secret, path.read_bytes())
        serialized_audit = json.dumps(audit_events, sort_keys=True)
        self.assertNotIn(secret.decode("ascii"), serialized_audit)
        self.assertNotIn("sensitive prompt", serialized_audit)
        self.assertNotIn("fake-ready", serialized_audit)
        self.assertNotIn(secret.decode("ascii"), json.dumps(provider))


if __name__ == "__main__":
    unittest.main()
