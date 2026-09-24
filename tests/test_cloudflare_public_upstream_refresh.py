"""Synthetic, bounded public-upstream proof refresh."""
import asyncio
import json
from contextlib import closing

import pytest

from pullwise_server.cloudflare_public_upstream import D1PublicUpstreamProofs
from pullwise_server.cloudflare_http_contract import handle_http_request
from test_cloudflare_account_adapter import D1ShapedSQLite
from test_cloudflare_server_mapping import seed
from test_cloudflare_product_reads import _seed_auth


def test_refresh_stages_stable_identity_with_one_injected_request(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    proofs = D1PublicUpstreamProofs(D1ShapedSQLite(fixture.store))
    calls = []

    async def fetch(owner, repository):
        calls.append((owner, repository))
        return {"status": 200, "repository": {"id": 101,
            "full_name": "Acme/Toolkit", "private": False,
            "visibility": "public"}}

    result = asyncio.run(proofs.refresh(owner="acme", repository="toolkit",
        fetch_repository=fetch, source_revision=1, observed_at=fixture.now))
    assert result == "github:101"
    assert calls == [("acme", "toolkit")]
    with closing(fixture.store.connect()) as db:
        row = db.execute("SELECT github_repo_id,full_name,public_visible,valid_until "
            "FROM public_upstream_proofs WHERE lookup_key='acme/toolkit'").fetchone()
    assert tuple(row) == ("github:101", "Acme/Toolkit", 1, fixture.now + 300)


@pytest.mark.parametrize("response", [
    {"status": 200, "repository": {"id": 101, "full_name": "Acme/Toolkit",
        "private": True, "visibility": "private"}},
    {"status": 200, "repository": {"id": 101, "full_name": "Acme/Renamed",
        "private": False, "visibility": "public"}},
    {"status": 404},
    {"status": 429},
])
def test_refresh_revokes_old_public_proof_without_followup_request(tmp_path, response):
    fixture, _, _ = seed(tmp_path / "domain.db")
    proofs = D1PublicUpstreamProofs(D1ShapedSQLite(fixture.store))
    asyncio.run(proofs.stage(owner="acme", repository="toolkit",
        github_repo_id="github:101", full_name="acme/toolkit",
        public_visible=True, private=False, source_revision=1,
        observed_at=fixture.now, valid_until=fixture.now + 300))
    calls = []

    async def fetch(owner, repository):
        calls.append((owner, repository))
        return response

    with pytest.raises(ValueError):
        asyncio.run(proofs.refresh(owner="acme", repository="toolkit",
            fetch_repository=fetch, source_revision=2, observed_at=fixture.now + 1))
    assert calls == [("acme", "toolkit")]
    with closing(fixture.store.connect()) as db:
        row = db.execute("SELECT public_visible,valid_until,source_revision "
            "FROM public_upstream_proofs WHERE lookup_key='acme/toolkit'").fetchone()
    assert tuple(row) == (0, fixture.now + 1, 2)


def test_refresh_rejects_invalid_identity_and_stale_revision(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    proofs = D1PublicUpstreamProofs(D1ShapedSQLite(fixture.store))

    async def invalid(owner, repository):
        return {"status": 200, "repository": {"id": True,
            "full_name": "acme/toolkit", "private": False,
            "visibility": "public"}}

    with pytest.raises(ValueError):
        asyncio.run(proofs.refresh(owner="acme", repository="toolkit",
            fetch_repository=invalid, source_revision=1, observed_at=fixture.now))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT public_visible FROM public_upstream_proofs").fetchone()[0] == 0

    async def valid(owner, repository):
        return {"status": 200, "repository": {"id": 101,
            "full_name": "acme/toolkit", "private": False,
            "visibility": "public"}}

    asyncio.run(proofs.refresh(owner="acme", repository="toolkit",
        fetch_repository=valid, source_revision=2, observed_at=fixture.now))
    with pytest.raises(Exception):
        asyncio.run(proofs.refresh(owner="acme", repository="toolkit",
            fetch_repository=valid, source_revision=1, observed_at=fixture.now + 1))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT source_revision FROM public_upstream_proofs").fetchone()[0] == 2


def test_private_refresh_blocks_new_public_watch_post(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    _seed_auth(fixture, scopes=("watches:write",))
    binding = D1ShapedSQLite(fixture.store)
    proofs = D1PublicUpstreamProofs(binding)
    asyncio.run(proofs.stage(owner="acme", repository="toolkit",
        github_repo_id="github:101", full_name="acme/toolkit",
        public_visible=True, private=False, source_revision=1,
        observed_at=fixture.now, valid_until=fixture.now + 300))

    async def private(owner, repository):
        return {"status": 200, "repository": {"id": 101,
            "full_name": "acme/toolkit", "private": True,
            "visibility": "private"}}

    with pytest.raises(ValueError):
        asyncio.run(proofs.refresh(owner="acme", repository="toolkit",
            fetch_repository=private, source_revision=2,
            observed_at=fixture.now + 1))
    body = json.dumps({"upstream": {"owner": "acme", "repository": "toolkit"},
        "interests": ["OAuth"]}).encode()

    async def read_body():
        return body

    status, payload = asyncio.run(handle_http_request(method="POST",
        path="/api/v1/watches", headers={"Cookie": "pw_session=session-local",
            "Content-Length": str(len(body)), "Idempotency-Key": "after-private"},
        read_body=read_body, binding=binding, creem_secret="",
        configured_products={}, now=fixture.now + 1))
    assert status == 503 and payload["error"]["code"] == "UPSTREAM_PROOF_UNAVAILABLE"
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM update_watches").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM request_idempotency").fetchone()[0] == 0


def test_trusted_stage_refuses_unrelated_name_or_unstable_id(tmp_path):
    fixture, _, _ = seed(tmp_path / "domain.db")
    proofs = D1PublicUpstreamProofs(D1ShapedSQLite(fixture.store))
    for repo_id, full_name in (("github:101", "acme/other"),
                               ("github:True", "acme/toolkit")):
        with pytest.raises(ValueError):
            asyncio.run(proofs.stage(owner="acme", repository="toolkit",
                github_repo_id=repo_id, full_name=full_name,
                public_visible=True, private=False, source_revision=1,
                observed_at=fixture.now, valid_until=fixture.now + 300))
    with closing(fixture.store.connect()) as db:
        assert db.execute("SELECT COUNT(*) FROM public_upstream_proofs").fetchone()[0] == 0
