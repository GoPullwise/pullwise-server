"""Optional project links preserve the existing ledger write authority."""
import asyncio

import pytest

from pullwise_server.cloudflare_api_key_write import create_api_key
from pullwise_server.cloudflare_project_repositories import MAX_PROJECT_URL_BYTES, project_url
from test_project_repositories import ledger, member, update


FIELDS = {"developmentUrl": "http://localhost:3000/dev", "productUrl": "https://product.example/app"}


def blank(ledger, **fields):
    status, project = ledger.call("POST", "/api/v1/projects", {"name": "Project links", **fields})
    assert status == 201, project
    return project


@pytest.mark.parametrize("value,expected", [
    (None, None),
    (" https://EXAMPLE.com ", "https://example.com/"),
    ("HTTP://LOCALHOST:80/dashboard", "http://localhost/dashboard"),
    ("http://intranet:8080/report", "http://intranet:8080/report"),
    ("http://10.0.0.8/report", "http://10.0.0.8/report"),
    ("https://[2001:0db8::1]:8443/report", "https://[2001:db8::1]:8443/report"),
    ("https://例子.测试/项目", "https://xn--fsqu00a.xn--0zwm56d/%E9%A1%B9%E7%9B%AE"),
    ("https://apps.apple.com/app/example/id123", "https://apps.apple.com/app/example/id123"),
    ("https://play.google.com/store/apps/details?id=com.example.app", "https://play.google.com/store/apps/details?id=com.example.app"),
])
def test_project_url_accepts_bounded_web_and_explicit_internal_development_links(value, expected):
    assert project_url(value) == expected


INVALID_URLS = [
    "", " ", "example.com", "//example.com", "/relative/path", "https://", "http:example.com",
    "https:///example.com", "javascript:alert(1)", "data:text/html,hello", "ftp://example.com",
    "https://user:secret@example.com", "https://@example.com", "https://:secret@example.com",
    "https://example.com\\evil", "https://example.com/a b", "https://example.com/\u00a0hidden",
    "\nhttps://example.com", "https://example.com\r", "https://example.com/a\x00b",
    "https://example.com/a\x7fb", "https://example.com/a\x85b", "https://example.com/a\x9fb",
    "https://example.com:65536", "https://example.com:abc", "https://[not-an-ipv6]/",
    "https://[::1]suffix/", "https://[fe80::1%eth0]/", "https://256.1.1.1",
    "https://exa%40mple.com", False, 42, [], {},
]


@pytest.mark.parametrize("value", INVALID_URLS)
def test_invalid_project_urls_are_rejected(value):
    with pytest.raises(ValueError):
        project_url(value)


def test_project_url_byte_limit_covers_both_input_and_serialized_unicode():
    prefix = "https://example.com/"
    exact = prefix + "a" * (MAX_PROJECT_URL_BYTES - len(prefix))
    assert len(project_url(exact).encode("utf-8")) == MAX_PROJECT_URL_BYTES
    with pytest.raises(ValueError):
        project_url(exact + "a")
    unicode_url = prefix + "界" * 675
    assert len(unicode_url.encode("utf-8")) < MAX_PROJECT_URL_BYTES
    with pytest.raises(ValueError):
        project_url(unicode_url)


def test_default_and_explicit_null_links_are_returned_in_create_list_and_detail(ledger):
    first = blank(ledger)
    second = blank(ledger, developmentUrl=None, productUrl=None)
    for project in (first, second):
        assert project["developmentUrl"] is None and project["productUrl"] is None
        status, loaded = ledger.call("GET", "/api/v1/projects/" + project["id"])
        assert status == 200 and loaded == project
    status, page = ledger.call("GET", "/api/v1/projects")
    assert status == 200 and len(page["items"]) == 2
    assert all(item["developmentUrl"] is None and item["productUrl"] is None for item in page["items"])
    assert ledger.gateway.calls == []


def test_links_persist_and_omitted_patch_fields_retain_then_null_explicitly_clears(ledger):
    project = blank(ledger, **FIELDS)
    assert all(project[field] == value for field, value in FIELDS.items())
    status, changed = update(ledger, project, {"productUrl": " https://NEXT.example "})
    assert status == 200 and changed["revision"] == 2
    assert changed["developmentUrl"] == FIELDS["developmentUrl"]
    assert changed["productUrl"] == "https://next.example/"
    status, retained = update(ledger, changed, {"description": "Only the description changes"})
    assert status == 200 and retained["developmentUrl"] == changed["developmentUrl"]
    assert retained["productUrl"] == changed["productUrl"]
    status, cleared = update(ledger, retained, {"developmentUrl": None, "productUrl": None})
    assert status == 200 and cleared["revision"] == 4
    assert cleared["developmentUrl"] is None and cleared["productUrl"] is None
    with ledger.store.connect() as db:
        assert tuple(db.execute("SELECT development_url,product_url FROM ledger_projects").fetchone()) == (None, None)
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1] == cleared
    assert ledger.gateway.calls == []


@pytest.mark.parametrize("field", ["developmentUrl", "productUrl"])
def test_invalid_url_create_and_patch_never_write_or_call_providers(ledger, field):
    project = blank(ledger, **FIELDS)
    ledger.gateway.calls.clear()
    with ledger.store.connect() as db:
        before = list(db.iterdump())
    for value in INVALID_URLS + ["https://example.com/" + "x" * 2048]:
        expected = (422, {"error": {"code": "INVALID_INPUT"}})
        assert ledger.call("POST", "/api/v1/projects", {"name": "Invalid", field: value}) == expected
        assert update(ledger, project, {field: value}) == expected
    assert ledger.gateway.calls == []
    with ledger.store.connect() as db:
        assert list(db.iterdump()) == before


def test_link_patch_keeps_revision_cas(ledger):
    project = blank(ledger, **FIELDS)
    status, changed = update(ledger, project, {"productUrl": None})
    assert status == 200
    assert update(ledger, project, {"developmentUrl": None})[0] == 412
    assert ledger.call("PATCH", "/api/v1/projects/" + project["id"], {"developmentUrl": None})[0] == 428
    assert ledger.call("GET", "/api/v1/projects/" + project["id"])[1] == changed


@pytest.mark.parametrize("role,write_status", [("admin", 200), ("editor", 403), ("viewer", 403)])
def test_current_ledger_role_controls_link_edits_while_members_can_read(ledger, role, write_status):
    project = blank(ledger, **FIELDS)
    headers = member(ledger, role)
    status, loaded = ledger.call("GET", "/api/v1/projects/" + project["id"], headers=headers)
    assert status == 200 and loaded["productUrl"] == FIELDS["productUrl"]
    status, changed = update(ledger, project, {"productUrl": None}, headers=headers)
    assert status == write_status
    loaded = ledger.call("GET", "/api/v1/projects/" + project["id"])[1]
    assert loaded["productUrl"] == (None if role == "admin" else FIELDS["productUrl"])
    assert ledger.gateway.calls == []


def test_link_edits_do_not_change_api_key_scope_or_project_restrictions(ledger):
    allowed, forbidden = blank(ledger, **FIELDS), blank(ledger, **FIELDS)
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "Project link editor", "scopes": ["projects:read", "projects:write"],
              "restrictions": {"projectIds": [allowed["id"]], "shared": False}}, now=ledger.now + 3))
    assert status == 201
    headers = {"Authorization": "Bearer " + key["key"]}
    assert update(ledger, allowed, {"productUrl": None}, headers=headers)[0] == 200
    assert update(ledger, forbidden, {"productUrl": None}, headers=headers)[0] == 403
    status, key = asyncio.run(create_api_key(binding=ledger.binding, headers=ledger.headers,
        body={"name": "Project link reader", "scopes": ["projects:read"]}, now=ledger.now + 3))
    assert status == 201
    assert update(ledger, forbidden, {"productUrl": None}, headers={"Authorization": "Bearer " + key["key"]})[0] == 403


def test_links_survive_attach_detach_archive_and_actor_gitHub_metadata_loss(ledger):
    project = blank(ledger, **FIELDS)
    status, linked = update(ledger, project, {"githubRepoIds": [101, 102], "githubOrganizationId": 1001})
    assert status == 200 and linked["githubAccess"] == "authorized"
    assert all(linked[field] == value for field, value in FIELDS.items())
    ledger.gateway.visible["synthetic-access-token"] = []
    status, history = ledger.call("GET", "/api/v1/projects/" + project["id"])
    assert status == 200 and history["githubAccess"] == "lost"
    assert all(history[field] == value for field, value in FIELDS.items())
    assert all(repo["githubFullName"] is None and repo["account"] is None for repo in history["repositories"])
    assert history["githubOrganization"]["login"] is None
    status, edited = update(ledger, linked, {"productUrl": "https://product.example/new"})
    assert status == 200 and edited["githubAccess"] == "lost" and edited["githubRepoIds"] == [101, 102]
    status, unlinked = update(ledger, edited, {"githubRepoIds": []})
    assert status == 200 and unlinked["githubAccess"] == "not_linked"
    assert unlinked["developmentUrl"] == FIELDS["developmentUrl"] and unlinked["productUrl"] == edited["productUrl"]
    status, archived = update(ledger, unlinked, {"status": "archived"})
    assert status == 200 and not archived["canCreateExpense"]
    assert archived["developmentUrl"] == FIELDS["developmentUrl"] and archived["productUrl"] == edited["productUrl"]
