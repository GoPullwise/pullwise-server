from __future__ import annotations

import json
import os
import tempfile
import unittest
from http import HTTPStatus
from unittest.mock import patch

from pullwise_server import app, db
from pullwise_server.product_store import ProductStore


class RouteHarness(app.PullwiseHandler):
    def __init__(
        self,
        path: str,
        body: dict | None = None,
        *,
        cookie: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.path = path
        self._body = {} if body is None else body
        self._raw_body = json.dumps(self._body).encode("utf-8")
        self.headers = {
            "Host": "api.pullwise.dev",
            "Cookie": cookie,
            **(headers or {}),
        }
        self.payload = None
        self.status = None
        self.headers_out: dict[str, str] = {}
        self.client_address = ("203.0.113.10", 51234)

    def read_json(self) -> dict:
        return self._body

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.status = status
        self.headers_out = headers or {}

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


class ProductApiRoutesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = os.path.join(self.temp_dir.name, "pullwise.sqlite3")
        self.env = patch.dict(
            os.environ,
            {
                "PULLWISE_DB_PATH": self.db_path,
                "PULLWISE_APP_URL": "https://app.pullwise.dev",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.persist = patch.object(app, "persist_state")
        self.persist.start()
        self.addCleanup(self.persist.stop)
        self.rate_limit = patch.object(app.system_config, "rate_limit_enabled", return_value=False)
        self.rate_limit.start()
        self.addCleanup(self.rate_limit.stop)
        db.reset_initialization_cache()
        app.USERS = {
            "usr_1": {
                "id": "usr_1",
                "name": "Dev",
                "email": "dev@example.com",
                "providers": [],
                "billing": {"plan": "free", "status": "active"},
                "githubRepositoryAccess": {
                    "mode": "github-app",
                    "scope": "selected",
                    "repositorySelection": "selected",
                    "authorizedUserId": "usr_1",
                    "installationId": "111",
                    "installationIds": ["111"],
                    "repositories": ["acme/api"],
                    "repositoryItems": [{
                        "id": "123",
                        "githubRepoId": "123",
                        "fullName": "acme/api",
                        "installationId": "111",
                        "defaultBranch": "main",
                        "permissions": {"pull": True, "push": True, "admin": True},
                    }],
                    "repositoriesNeedSync": False,
                },
            }
        }
        app.SESSIONS = {
            "ses_1": {
                "id": "ses_1",
                "userId": "usr_1",
                "createdAt": app.now(),
                "expiresAt": app.now() + 3600,
            }
        }
        app.STATE_LOADED = True
        self.repository = db.upsert_repository(
            {
                "github_repo_id": "123",
                "full_name": "acme/api",
                "default_branch": "main",
                "private": 1,
                "html_url": "https://github.com/acme/api",
                "clone_url": "https://github.com/acme/api.git",
            }
        )
        self.store = ProductStore(self.db_path)
        self.store.initialize()
        self.watch = self.store.create_watch(
            owner_id="usr_1",
            target_repository_id=None,
            upstream_repository_id="github:123",
            billing_owner_id="usr_1",
            interests=["OAuth 登录"],
            enabled=True,
            analysis_enabled=False,
        )

    def api_key(self, scopes: list[str], *, restrictions: dict | None = None) -> str:
        token = f"{app.API_KEY_PREFIX}product_test_{len(scopes)}_{len(restrictions or {})}"
        db.create_api_key(
            {
                "id": f"key_{len(scopes)}_{len(restrictions or {})}",
                "user_id": "usr_1",
                "name": "Product API",
                "key_prefix": app.api_key_prefix(token),
                "key_hash": app.api_key_hash(token),
                "scopes": scopes,
                "restrictions": restrictions or {},
            }
        )
        return token

    def test_session_and_api_key_read_the_same_watch_contract(self) -> None:
        session = RouteHarness("/api/v1/watches", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(session, "GET")
        token = self.api_key(["watches:read"])
        api_key = RouteHarness("/v1/watches", headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(api_key, "GET")

        self.assertEqual(session.status, HTTPStatus.OK)
        self.assertEqual(api_key.status, HTTPStatus.OK)
        self.assertEqual(api_key.payload["items"], session.payload["items"])
        self.assertEqual(session.payload["items"][0]["watchScopeKey"], self.watch["watchScopeKey"])

    def test_watch_list_pages_without_exposing_other_owner(self) -> None:
        second = self.store.create_watch(owner_id="usr_1", target_repository_id=None,
            upstream_repository_id="github:456", billing_owner_id="usr_1",
            interests=["CI"], enabled=True, analysis_enabled=False)
        self.store.create_watch(owner_id="other", target_repository_id=None,
            upstream_repository_id="github:789", billing_owner_id="other",
            interests=["Private"], enabled=True, analysis_enabled=False)
        cookie = f"{app.SESSION_COOKIE}=ses_1"
        first = RouteHarness("/api/v1/watches?limit=1", cookie=cookie)
        app.PullwiseHandler.route(first, "GET")
        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertTrue(first.payload["hasMore"])
        cursor = first.payload["nextCursor"]
        next_page = RouteHarness(f"/api/v1/watches?limit=1&cursor={cursor}", cookie=cookie)
        app.PullwiseHandler.route(next_page, "GET")
        self.assertEqual(next_page.status, HTTPStatus.OK)
        self.assertEqual({first.payload["items"][0]["id"],
                          next_page.payload["items"][0]["id"]},
                         {self.watch["id"], second["id"]})
        self.assertFalse(next_page.payload["hasMore"])

    def test_repository_list_pages_authorized_manifest_only(self) -> None:
        other = db.upsert_repository({"github_repo_id": "456", "full_name": "acme/other",
            "default_branch": "main", "private": 0,
            "html_url": "https://github.com/acme/other",
            "clone_url": "https://github.com/acme/other.git"})
        app.USERS["usr_1"]["githubRepositoryAccess"]["repositoryItems"].append({
            "id": "456", "githubRepoId": "456", "fullName": "acme/other",
            "installationId": "111", "defaultBranch": "main"})
        cookie = f"{app.SESSION_COOKIE}=ses_1"
        first = RouteHarness("/api/v1/repositories?limit=1", cookie=cookie)
        app.PullwiseHandler.route(first, "GET")
        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertTrue(first.payload["hasMore"])
        cursor = first.payload["nextCursor"]
        next_page = RouteHarness(f"/api/v1/repositories?limit=1&cursor={cursor}",
            cookie=cookie)
        app.PullwiseHandler.route(next_page, "GET")
        self.assertEqual({first.payload["items"][0]["id"],
                          next_page.payload["items"][0]["id"]},
                         {self.repository["id"], other["id"]})

    def test_watch_detail_reuses_owner_and_key_restrictions(self) -> None:
        cookie = RouteHarness(f"/api/v1/watches/{self.watch['id']}",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(cookie, "GET")
        self.assertEqual(cookie.status, HTTPStatus.OK)
        self.assertEqual(cookie.payload, self.store.get_watch(self.watch["id"]))
        token = self.api_key(["watches:read"], restrictions={"watchIds": ["other"]})
        restricted = RouteHarness(f"/api/v1/watches/{self.watch['id']}",
            headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(restricted, "GET")
        self.assertEqual(restricted.status, HTTPStatus.NOT_FOUND)

    def test_item_identity_filters_require_module_and_repository(self) -> None:
        request = RouteHarness("/api/v1/items?pullNumber=12",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(request, "GET")
        self.assertEqual(request.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.assertEqual(request.payload["error"]["code"], "INVALID_CONFIGURATION")

    def test_usage_events_list_only_consumed_owner_ledger(self) -> None:
        with self.store._immediate() as connection:
            connection.execute("""INSERT INTO processing_usage_ledger(
                charge_key,reservation_id,billing_owner_id,period,module,state,
                reserved_at,finished_at) VALUES('event-one','res-one','usr_1',
                'period','pr','consumed',1,2)""")
            connection.execute("""INSERT INTO processing_usage_ledger(
                charge_key,reservation_id,billing_owner_id,period,module,state,
                reserved_at,finished_at) VALUES('other-event','res-other','other',
                'period','ci','consumed',1,2)""")
        request = RouteHarness("/api/v1/usage/events?module=pr",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(request, "GET")
        self.assertEqual(request.status, HTTPStatus.OK)
        self.assertEqual([row["id"] for row in request.payload["items"]], ["res-one"])
        self.assertFalse(request.payload["hasMore"])

    def test_manual_sync_from_session_and_key_never_creates_analysis_job(self) -> None:
        session = RouteHarness(
            f"/api/v1/watches/{self.watch['id']}/sync",
            {},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "Idempotency-Key": "sync-1"},
        )
        with patch.object(app, "cookie_same_site", return_value="None"):
            app.PullwiseHandler.route(session, "POST")

        token = self.api_key(["watches:read", "sync:write"])
        api_key = RouteHarness(
            f"/v1/watches/{self.watch['id']}/sync",
            {},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "sync-2"},
        )
        app.PullwiseHandler.route(api_key, "POST")

        self.assertEqual(session.status, HTTPStatus.ACCEPTED)
        self.assertEqual(api_key.status, HTTPStatus.ACCEPTED)
        self.assertEqual(api_key.payload["id"], session.payload["id"])
        self.assertEqual(ProductStore(self.db_path).count_jobs(job_type="analyze_source"), 0)

    def test_mixed_session_and_api_key_identity_is_rejected(self) -> None:
        token = self.api_key(["watches:read"])
        handler = RouteHarness(
            "/api/v1/watches",
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Authorization": f"Bearer {token}"},
        )

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(handler.payload["error"]["code"], "AMBIGUOUS_AUTH")

    def test_retired_scan_scope_does_not_authorize_product_watch_reads(self) -> None:
        token = self.api_key(["scans:read"])
        handler = RouteHarness("/api/v1/watches", headers={"Authorization": f"Bearer {token}"})

        app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(handler.status, HTTPStatus.FORBIDDEN)
        self.assertEqual(handler.payload["error"]["code"], "INSUFFICIENT_SCOPE")

    def seed_source_item(
        self,
        *,
        source_id: str,
        source_type: str,
        unit_type: str | None,
        module: str,
        attention_state: str = "needs_action",
        source_facts: dict | None = None,
    ) -> dict:
        source = self.store.upsert_source_snapshot(
            source_id=source_id,
            source_type=source_type,
            external_key=f"github:{source_type}:{source_id}",
            repository_id="repo-1" if module != "updates" else "upstream-1",
            content={"text": f"evidence for {source_id}"},
            source_facts={"fixture": source_id, **(source_facts or {})},
            source_url=f"https://github.com/acme/repo/{source_id}",
            processing_mode="model",
            completeness="complete",
            lifecycle="active",
            observed_at=1_800_000_000,
        )
        context_id = self.watch["watchScopeKey"] if module == "updates" else "repo:repo-1"
        self.store.set_source_context(
            source_id=source_id,
            context_id=context_id,
            context_version=1,
            configuration_revision=1,
            authorization_revision=1,
            authorization_valid_until=1_900_000_000,
            accessible=True,
            billing_owner_id="usr_1",
            watch_id=self.watch["id"] if module == "updates" else None,
            processing_status="assessed",
            analysis_enabled=True,
            context_stale=False,
            coverage={"state": "complete", "selectedUnits": 1, "rawSourcePartial": False, "limitations": []},
        )
        if unit_type is None:
            return source
        item = self.store.create_item(
            context_id=context_id,
            unit_type=unit_type,
            unit_key=f"unit:{source_id}",
        )
        return self.store.publish_item_snapshot(
            item_id=item["id"],
            expected_item_revision=item["revision"],
            sources=[{
                "sourceId": source_id,
                "sourceVersion": source["sourceVersion"],
                "sourceRevision": source["sourceRevision"],
            }],
            context_fences=[{
                "sourceId": source_id,
                "contextId": context_id,
                "contextVersion": 1,
                "configurationRevision": 1,
                "authorizationRevision": 1,
            }],
            snapshot={
                "module": module,
                "sourceFacts": {"fixture": source_id, **(source_facts or {})},
                "repositoryId": "repo-1" if module != "updates" else None,
                "watchId": self.watch["id"] if module == "updates" else None,
                "title": f"{module} item",
                "actionTypes": [
                    "investigate_failure" if module == "ci" else "review_update" if module == "updates" else "reply_needed"
                ],
                "attentionState": attention_state,
                "lifecycle": "active",
                "evidence": [{"id": f"ev-{source_id}", "sourceId": source_id, "text": "fixture evidence"}],
                "nextActors": [],
            },
            observed_at=1_800_000_000,
        )

    def test_item_detail_returns_ordered_versioned_handling_history_only_to_authorized_readers(self):
        item = self.seed_source_item(source_id="history", source_type="pr_comment", unit_type="pr_comment", module="pr")
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"], actor_id="usr_1", disposition="done", note="Verified")
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"] + 1, actor_id="usr_1", disposition="open")
        token = self.api_key(["items:read"])
        detail = RouteHarness(f"/v1/items/{item['id']}", headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(detail, "GET")
        history = detail.payload["handlingHistory"]
        self.assertEqual([event["disposition"] for event in history], ["done", "open"])
        self.assertEqual(history[0]["note"], "Verified")
        self.assertEqual(history[0]["itemVersion"], item["itemVersion"])
        listing = RouteHarness("/api/v1/items", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(listing, "GET")
        self.assertNotIn("handlingHistory", listing.payload["items"][0])
        restricted_token = self.api_key(["items:read"], restrictions={"repositoryIds": ["other"]})
        blocked = RouteHarness(f"/v1/items/{item['id']}", headers={"Authorization": f"Bearer {restricted_token}"})
        app.PullwiseHandler.route(blocked, "GET")
        self.assertEqual(blocked.status, HTTPStatus.NOT_FOUND)
        self.assertNotIn("Verified", json.dumps(blocked.payload))

    def test_item_timeline_uses_saved_observation_and_handling_without_inventing_event_time(self):
        item = self.seed_source_item(source_id="timeline", source_type="pr_comment",
            unit_type="pr_comment", module="pr")
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"], actor_id="usr_1", disposition="done")
        request = RouteHarness(f"/api/v1/items/{item['id']}/timeline",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(request, "GET")
        self.assertEqual(request.status, HTTPStatus.OK)
        self.assertEqual({event["eventType"] for event in request.payload["items"]},
            {"snapshot_observed", "disposition_changed"})
        snapshot = next(event for event in request.payload["items"]
            if event["sourceKind"] == "github")
        handling = next(event for event in request.payload["items"]
            if event["sourceKind"] == "handling")
        self.assertIsNone(snapshot["occurredAt"])
        self.assertEqual(snapshot["timeBasis"], "observed")
        self.assertEqual(snapshot["sourceKind"], "github")
        self.assertEqual(handling["sourceKind"], "handling")
        self.assertEqual(handling["actor"], {"kind": "user", "id": "usr_1"})
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)
        page = RouteHarness(f"/api/v1/items/{item['id']}/timeline?limit=1",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(page, "GET")
        self.assertTrue(page.payload["hasMore"])
        cursor = page.payload["nextCursor"]
        next_page = RouteHarness(f"/api/v1/items/{item['id']}/timeline?limit=1&cursor={cursor}",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(next_page, "GET")
        self.assertEqual(len(next_page.payload["items"]), 1)
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"] + 1, actor_id="usr_1", disposition="dismissed")
        stale_page = RouteHarness(f"/api/v1/items/{item['id']}/timeline?limit=1&cursor={cursor}",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(stale_page, "GET")
        self.assertEqual(stale_page.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        token = self.api_key(["items:read"], restrictions={"repositoryIds": ["other"]})
        denied = RouteHarness(f"/v1/items/{item['id']}/timeline",
            headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(denied, "GET")
        self.assertEqual(denied.status, HTTPStatus.NOT_FOUND)

    def test_source_detail_exposes_current_material_without_creating_an_item_or_model_work(self):
        self.seed_source_item(source_id="release-material", source_type="release", unit_type=None, module="updates")
        detail = RouteHarness("/api/v1/sources/release-material", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(detail, "GET")
        self.assertEqual(detail.payload["content"], {"text": "evidence for release-material"})
        self.assertIsNone(detail.payload["contexts"][0]["itemId"])
        listing = RouteHarness("/api/v1/sources?module=updates", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(listing, "GET")
        self.assertNotIn("content", listing.payload["items"][0])
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_sources_items_and_overview_cover_all_modules_and_updates_without_item(self) -> None:
        self.seed_source_item(source_id="pr-1", source_type="pr_comment", unit_type="pr_comment", module="pr")
        self.seed_source_item(source_id="ci-1", source_type="ci_failure", unit_type="ci_job", module="ci")
        self.seed_source_item(source_id="update-1", source_type="release", unit_type="update_release", module="updates")
        self.seed_source_item(source_id="update-no-item", source_type="release", unit_type=None, module="updates")
        before_jobs = self.store.count_jobs(job_type="analyze_source")

        sources = RouteHarness("/api/v1/sources?module=updates", cookie=f"{app.SESSION_COOKIE}=ses_1")
        items = RouteHarness("/api/v1/items", cookie=f"{app.SESSION_COOKIE}=ses_1")
        overview = RouteHarness("/api/v1/items/overview", cookie=f"{app.SESSION_COOKIE}=ses_1")
        for handler in (sources, items, overview):
            app.PullwiseHandler.route(handler, "GET")

        self.assertEqual(sources.status, HTTPStatus.OK)
        self.assertEqual({item["id"] for item in sources.payload["items"]}, {"update-1", "update-no-item"})
        self.assertEqual(len(items.payload["items"]), 3)
        self.assertEqual(overview.payload["totalCount"], 3)
        self.assertEqual(overview.payload["viewCounts"]["unassigned"], 3)
        self.assertEqual(overview.payload["sourceCoverage"]["total"], 4)
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), before_jobs)

    def test_item_list_pages_bind_scope_and_fail_on_changed_snapshot(self) -> None:
        for index in range(3):
            self.seed_source_item(source_id=f"paged-{index}",
                source_type="pr_comment", unit_type="pr_comment", module="pr")
        cookie = f"{app.SESSION_COOKIE}=ses_1"
        first = RouteHarness("/api/v1/items?module=pr&limit=1", cookie=cookie)
        app.PullwiseHandler.route(first, "GET")
        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertEqual(len(first.payload["items"]), 1)
        self.assertTrue(first.payload["hasMore"])
        cursor = first.payload["nextCursor"]
        second = RouteHarness(f"/api/v1/items?module=pr&limit=1&cursor={cursor}", cookie=cookie)
        app.PullwiseHandler.route(second, "GET")
        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertNotEqual(second.payload["items"][0]["id"], first.payload["items"][0]["id"])
        other_scope = RouteHarness(f"/api/v1/items?module=ci&limit=1&cursor={cursor}",
            cookie=cookie)
        app.PullwiseHandler.route(other_scope, "GET")
        self.assertEqual(other_scope.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        token = self.api_key(["items:read"], restrictions={"repositoryIds": ["repo-1"]})
        other_authority = RouteHarness(f"/v1/items?module=pr&limit=1&cursor={cursor}",
            headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(other_authority, "GET")
        self.assertEqual(other_authority.status, HTTPStatus.UNPROCESSABLE_ENTITY)
        self.seed_source_item(source_id="paged-new", source_type="pr_comment",
            unit_type="pr_comment", module="pr")
        stale = RouteHarness(f"/api/v1/items?module=pr&limit=1&cursor={cursor}", cookie=cookie)
        app.PullwiseHandler.route(stale, "GET")
        self.assertEqual(stale.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_source_list_pages_include_unclassified_releases(self) -> None:
        for index in range(3):
            self.seed_source_item(source_id=f"release-page-{index}",
                source_type="release", unit_type=None, module="updates")
        cookie = f"{app.SESSION_COOKIE}=ses_1"
        first = RouteHarness("/api/v1/sources?module=updates&limit=1", cookie=cookie)
        app.PullwiseHandler.route(first, "GET")
        self.assertEqual(first.status, HTTPStatus.OK)
        self.assertEqual(len(first.payload["items"]), 1)
        self.assertTrue(first.payload["hasMore"])
        cursor = first.payload["nextCursor"]
        second = RouteHarness(f"/api/v1/sources?module=updates&limit=1&cursor={cursor}",
            cookie=cookie)
        app.PullwiseHandler.route(second, "GET")
        self.assertEqual(second.status, HTTPStatus.OK)
        self.assertNotEqual(first.payload["items"][0]["id"], second.payload["items"][0]["id"])
        changed_scope = RouteHarness(f"/api/v1/sources?module=pr&limit=1&cursor={cursor}",
            cookie=cookie)
        app.PullwiseHandler.route(changed_scope, "GET")
        self.assertEqual(changed_scope.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_workload_visualization_uses_item_filters_and_authorized_distinct_counts(self) -> None:
        pr = self.seed_source_item(source_id="pr-visual", source_type="pr_comment",
            unit_type="pr_comment", module="pr")
        ci = self.seed_source_item(source_id="ci-visual", source_type="ci_failure",
            unit_type="ci_job", module="ci", attention_state="waiting")
        before_jobs = self.store.count_jobs(job_type="analyze_source")
        cookie = f"{app.SESSION_COOKIE}=ses_1"
        visual = RouteHarness("/api/v1/visualizations?kind=workload&view=all", cookie=cookie)
        app.PullwiseHandler.route(visual, "GET")
        self.assertEqual(visual.status, HTTPStatus.OK)
        self.assertEqual(visual.payload["kind"], "workload")
        self.assertEqual(visual.payload["countUnit"], "item")
        self.assertEqual(visual.payload["totalCount"], 2)
        rows = {row["key"]: row for row in visual.payload["data"]["rows"]}
        self.assertEqual(rows["pr"]["totalCount"], 1)
        self.assertEqual(rows["ci"]["totalCount"], 1)
        cell = next(cell for cell in rows["pr"]["cells"] if cell["key"] == "needs_action")
        self.assertEqual(cell["count"], 1)
        self.assertEqual(cell["drilldown"], {"resource": "items", "filters": {
            "view": "all", "module": "pr", "attentionState": "needs_action"}})
        detail = RouteHarness("/api/v1/items?view=all&module=pr&attentionState=needs_action",
            cookie=cookie)
        app.PullwiseHandler.route(detail, "GET")
        self.assertEqual([item["id"] for item in detail.payload["items"]], [pr["id"]])
        filtered = RouteHarness("/api/v1/visualizations?kind=workload&module=ci", cookie=cookie)
        app.PullwiseHandler.route(filtered, "GET")
        self.assertEqual(filtered.payload["totalCount"], 1)
        self.assertEqual(filtered.payload["data"]["rows"][0]["key"], "ci")
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), before_jobs)
        token = self.api_key(["items:read"], restrictions={"repositoryIds": ["other"]})
        denied = RouteHarness("/v1/visualizations?kind=workload",
            headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(denied, "GET")
        self.assertEqual(denied.payload["totalCount"], 0)
        invalid = RouteHarness("/api/v1/visualizations?kind=unknown", cookie=cookie)
        app.PullwiseHandler.route(invalid, "GET")
        self.assertEqual(invalid.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_pr_matrix_route_rejects_invalid_cursor_and_other_module(self) -> None:
        cookie = f"{app.SESSION_COOKIE}=ses_1"
        empty = RouteHarness("/api/v1/visualizations?kind=pr_actions", cookie=cookie)
        app.PullwiseHandler.route(empty, "GET")
        self.assertEqual(empty.status, HTTPStatus.OK)
        self.assertEqual(empty.payload["data"]["rowsTotal"], 0)
        self.assertEqual(empty.payload["totalCount"], 0)
        for query in ("kind=pr_actions&module=ci", "kind=pr_actions&cursor=invalid"):
            invalid = RouteHarness(f"/api/v1/visualizations?{query}", cookie=cookie)
            app.PullwiseHandler.route(invalid, "GET")
            self.assertEqual(invalid.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_ci_matrix_route_preserves_unclassified_entry_and_scope(self) -> None:
        self.seed_source_item(source_id="ci-matrix", source_type="ci_failure",
            unit_type="ci_job", module="ci", source_facts={"runId": "7",
                "runAttempt": 1, "jobId": "9", "windows": []})
        request = RouteHarness("/api/v1/visualizations?kind=ci_failures&module=ci",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(request, "GET")
        self.assertEqual(request.status, HTTPStatus.OK)
        self.assertEqual(request.payload["totalCount"], 1)
        self.assertEqual(request.payload["data"]["unclassifiedCount"], 1)
        self.assertEqual(request.payload["data"]["unclassifiedDrilldown"], {
            "resource": "items", "filters": {"module": "ci", "view": "all",
                "classificationState": "unclassified"}})

    def test_updates_table_route_keeps_unclassified_release_without_item(self) -> None:
        self.seed_source_item(source_id="release-table", source_type="release",
            unit_type=None, module="updates", source_facts={"releaseId": "77",
                "tagName": "v2", "name": "SDK 2"})
        request = RouteHarness("/api/v1/visualizations?kind=updates_releases",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(request, "GET")
        self.assertEqual(request.status, HTTPStatus.OK)
        self.assertEqual(request.payload["totalCount"], 1)
        row = request.payload["data"]["rows"][0]
        self.assertEqual(row["sourceId"], "release-table")
        self.assertEqual(row["watchId"], self.watch["id"])
        self.assertIsNone(row["itemId"])
        self.assertIsNone(row["relevance"])
        invalid = RouteHarness("/api/v1/visualizations?kind=updates_releases&view=mine",
            cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(invalid, "GET")
        self.assertEqual(invalid.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_source_permission_revocation_removes_detail_and_overview_count(self) -> None:
        self.seed_source_item(source_id="pr-revoked", source_type="pr_comment", unit_type="pr_comment", module="pr")
        visible = RouteHarness("/api/v1/items/overview", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(visible, "GET")
        self.assertEqual(visible.payload["totalCount"], 1)

        self.store.set_source_context(
            source_id="pr-revoked",
            context_id="repo:repo-1",
            context_version=1,
            configuration_revision=1,
            authorization_revision=2,
            authorization_valid_until=1_900_000_000,
            accessible=False,
            billing_owner_id="usr_1",
            processing_status="pending",
            context_stale=True,
            coverage={"state": "unavailable", "selectedUnits": 0, "rawSourcePartial": False, "limitations": ["permission_revoked"]},
        )
        source = RouteHarness("/api/v1/sources/pr-revoked", cookie=f"{app.SESSION_COOKIE}=ses_1")
        overview = RouteHarness("/api/v1/items/overview", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(source, "GET")
        app.PullwiseHandler.route(overview, "GET")

        self.assertEqual(source.status, HTTPStatus.NOT_FOUND)
        self.assertEqual(overview.payload["totalCount"], 0)
        self.assertEqual(overview.payload["sourceCoverage"]["total"], 0)

    def test_session_and_api_key_share_versioned_item_handling_contract(self) -> None:
        item = self.seed_source_item(
            source_id="pr-handling",
            source_type="pr_comment",
            unit_type="pr_comment",
            module="pr",
        )
        session = RouteHarness(
            f"/api/v1/items/{item['id']}",
            {"itemVersion": item["itemVersion"], "disposition": "done"},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": f'"{item["revision"]}"'},
        )
        with patch.object(app, "cookie_same_site", return_value="None"):
            app.PullwiseHandler.route(session, "PATCH")

        token = self.api_key(["items:read", "items:write"])
        api_key = RouteHarness(
            f"/v1/items/{item['id']}",
            {"itemVersion": item["itemVersion"], "feedback": "classification_inaccurate"},
            headers={
                "Authorization": f"Bearer {token}",
                "If-Match": f'"{session.payload["revision"]}"',
            },
        )
        app.PullwiseHandler.route(api_key, "PATCH")

        self.assertEqual(session.status, HTTPStatus.OK)
        self.assertEqual(session.payload["handling"]["disposition"], "done")
        self.assertIsNone(session.payload["handling"]["note"])
        self.assertEqual(api_key.status, HTTPStatus.OK)
        self.assertEqual(api_key.payload["handling"]["disposition"], "done")
        self.assertEqual(api_key.payload["handling"]["feedback"], "classification_inaccurate")
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_done_item_leaves_action_views_and_reports_handling_closure(self):
        item = self.seed_source_item(source_id="ci-done", source_type="ci_failure", unit_type="ci_job", module="ci")
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"], actor_id="usr_1", disposition="done")
        detail = RouteHarness(f"/api/v1/items/{item['id']}", cookie=f"{app.SESSION_COOKIE}=ses_1")
        overview = RouteHarness("/api/v1/items/overview", cookie=f"{app.SESSION_COOKIE}=ses_1")
        for request in (detail, overview):
            app.PullwiseHandler.route(request, "GET")
        self.assertEqual(detail.payload["attentionState"], "closed")
        self.assertEqual(detail.payload["closureReason"], "handled_done")
        self.assertEqual(detail.payload["lifecycle"], "active")
        self.assertEqual(overview.payload["viewCounts"]["unassigned"], 0)

    def test_rule_sync_creates_readable_handleable_ci_item_without_model_work(self):
        from unittest.mock import Mock
        from pullwise_server.github_sources import ci_failure_source
        from pullwise_server.product_discovery import FactPage, ProductFactSync
        now = app.now()
        repository_id = self.repository["id"]
        self.store.put_repository_service(repository_id=repository_id, installation_id="111", billing_owner_id="usr_1",
            expected_revision=0, enabled=True, modules={"pr": False, "ci": True},
            analysis_enabled={"pr": False, "ci": False}, allow_member_sync=False,
            default_assignee_id="7", priority_order=0)
        target = self.store.set_discovery_authorization(resource_kind="repository", resource_id=repository_id,
            module="ci", github_repository_id="123", installation_id="111", app_id="8",
            authorization_revision=1, accessible=True, valid_until=now + 300, observed_at=now)
        source = ci_failure_source(repository_id=repository_id, run={"id": 1, "run_attempt": 1, "workflow_id": 2},
            job={"id": 3, "name": "test", "status": "completed", "conclusion": "failure",
                 "completed_at": "2026-09-22T00:00:00Z", "html_url": "https://github.com/acme/api/actions/runs/1/job/3"},
            evidence_windows=[])
        sync = ProductFactSync(self.store, read_page=Mock(return_value=FactPage((source,), None, "")),
            processing_budget=Mock(side_effect=AssertionError("manual sync cannot budget")), app_id="8", webhook_secret="fixture")
        self.assertEqual(sync.run_manual(target, now=now), {"status": "completed", "sources": 1})
        app.USERS["usr_1"]["githubId"] = "7"
        listing = RouteHarness("/api/v1/items?view=mine", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(listing, "GET")
        self.assertEqual(len(listing.payload["items"]), 1)
        item = listing.payload["items"][0]
        self.assertEqual(item["actionTypes"], ["investigate_failure"])
        self.assertEqual(item["assessments"], [])
        self.assertIsInstance(item["attentionUpdatedAt"], str)
        handled = RouteHarness(f"/api/v1/items/{item['id']}", {"itemVersion": item["itemVersion"], "disposition": "done"},
            cookie=f"{app.SESSION_COOKIE}=ses_1", headers={"Origin": "https://app.pullwise.dev", "If-Match": str(item["revision"])})
        with patch.object(app, "cookie_same_site", return_value="None"):
            app.PullwiseHandler.route(handled, "PATCH")
        self.assertEqual(handled.status, HTTPStatus.OK)
        with patch("pullwise_server.product_store._now", return_value=now + 10):
            sync.run_manual(target, now=now + 10)
        refreshed = self.store.list_items_for_billing_owner("usr_1")[0]
        self.assertEqual(refreshed["itemVersion"], item["itemVersion"])
        self.assertEqual(refreshed["attentionUpdatedAt"], item["attentionUpdatedAt"])
        self.assertNotEqual(refreshed["lastSyncedAt"], item["lastSyncedAt"])
        overview = RouteHarness("/api/v1/items/overview", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(overview, "GET")
        self.assertEqual(overview.payload["viewCounts"]["mine"], 0)
        self.assertEqual(overview.payload["totalCount"], 1)
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_item_views_use_github_identity_and_manual_assignment_precedes_roles(self):
        from pullwise_server.product_item_filters import item_in_view as _item_in_view
        item = {"attentionState": "needs_action", "nextActors": [{"kind": "user", "githubId": "7"}], "handling": {}}
        self.assertTrue(_item_in_view(item, "mine", "7"))
        self.assertFalse(_item_in_view(item, "mine", "usr_1"))
        item["handling"] = {"assigneeId": "8"}
        self.assertFalse(_item_in_view(item, "mine", "7"))
        self.assertTrue(_item_in_view(item, "waiting", "7"))
        item["handling"] = {}
        item["nextActors"] = [{"kind": "team", "githubId": "99"}]
        self.assertTrue(_item_in_view(item, "unassigned", "7"))
        self.assertFalse(_item_in_view(item, "waiting", "7"))

    def test_my_items_route_resolves_current_accounts_github_id(self):
        item = self.seed_source_item(source_id="ci-assigned", source_type="ci_failure", unit_type="ci_job", module="ci")
        app.USERS["usr_1"]["githubId"] = "7"
        self.store.patch_item_handling(item_id=item["id"], item_version=item["itemVersion"],
            expected_revision=item["revision"], actor_id="usr_1", assignee_id="7")
        request = RouteHarness("/api/v1/items?view=mine", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(request, "GET")
        self.assertEqual([row["id"] for row in request.payload["items"]], [item["id"]])

    def test_item_handling_reports_stale_item_and_revision_separately(self) -> None:
        item = self.seed_source_item(
            source_id="pr-stale",
            source_type="pr_comment",
            unit_type="pr_comment",
            module="pr",
        )
        stale_revision = RouteHarness(
            f"/api/v1/items/{item['id']}",
            {"itemVersion": item["itemVersion"], "disposition": "done"},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": '"1"'},
        )
        app.PullwiseHandler.route(stale_revision, "PATCH")
        stale_item = RouteHarness(
            f"/api/v1/items/{item['id']}",
            {"itemVersion": item["itemVersion"] + 1, "disposition": "done"},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": f'"{item["revision"]}"'},
        )
        app.PullwiseHandler.route(stale_item, "PATCH")

        self.assertEqual(stale_revision.status, HTTPStatus.PRECONDITION_FAILED)
        self.assertEqual(stale_revision.payload["error"]["code"], "REVISION_MISMATCH")
        self.assertEqual(stale_item.status, HTTPStatus.CONFLICT)
        self.assertEqual(stale_item.payload["error"]["code"], "STALE_ITEM")

    def test_api_key_watch_restriction_is_enforced_before_list_and_detail(self) -> None:
        self.seed_source_item(
            source_id="update-restricted",
            source_type="release",
            unit_type="update_release",
            module="updates",
        )
        token = self.api_key(
            ["watches:read", "items:read"],
            restrictions={"watchIds": ["watch_not_allowed"]},
        )
        watches = RouteHarness("/api/v1/watches", headers={"Authorization": f"Bearer {token}"})
        sources = RouteHarness("/api/v1/sources", headers={"Authorization": f"Bearer {token}"})

        app.PullwiseHandler.route(watches, "GET")
        app.PullwiseHandler.route(sources, "GET")

        self.assertEqual(watches.status, HTTPStatus.OK)
        self.assertEqual(watches.payload["items"], [])
        self.assertEqual(sources.status, HTTPStatus.OK)
        self.assertEqual(sources.payload["items"], [])

    def test_session_and_api_key_read_same_new_product_usage_without_model_work(self) -> None:
        before_jobs = self.store.count_jobs(job_type="analyze_source")
        session = RouteHarness("/api/v1/usage", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(session, "GET")
        token = self.api_key(["usage:read"])
        api_key = RouteHarness("/v1/usage", headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(api_key, "GET")

        self.assertEqual(session.status, HTTPStatus.OK)
        self.assertEqual(api_key.status, HTTPStatus.OK)
        self.assertEqual(session.payload["service"], "github_followups")
        self.assertEqual(session.payload["entitlements"], {
            "activeRepositoryLimit": 1,
            "activeWatchLimit": 3,
            "monthlyProcessingLimit": 200,
        })
        self.assertEqual(api_key.payload["usage"], session.payload["usage"])
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), before_jobs)

    def test_me_and_sync_job_status_use_same_identity_contract(self) -> None:
        session_me = RouteHarness("/api/v1/me", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(session_me, "GET")
        profile_key = self.api_key(["profile:read"])
        key_me = RouteHarness("/v1/me", headers={"Authorization": f"Bearer {profile_key}"})
        app.PullwiseHandler.route(key_me, "GET")

        sync = RouteHarness(
            f"/api/v1/watches/{self.watch['id']}/sync",
            {},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "Idempotency-Key": "sync-status"},
        )
        app.PullwiseHandler.route(sync, "POST")
        job = RouteHarness(
            f"/api/v1/jobs/{sync.payload['id']}",
            cookie=f"{app.SESSION_COOKIE}=ses_1",
        )
        app.PullwiseHandler.route(job, "GET")

        self.assertEqual(session_me.status, HTTPStatus.OK)
        self.assertEqual(key_me.payload, session_me.payload)
        self.assertNotIn("billing", session_me.payload)
        self.assertEqual(job.status, HTTPStatus.OK)
        self.assertEqual(job.payload["operation"], "sync_watch")
        self.assertNotIn("trustedTrigger", job.payload)

    def test_watch_patch_and_delete_keep_context_version_semantics(self) -> None:
        patch_watch = RouteHarness(
            f"/api/v1/watches/{self.watch['id']}",
            {"interests": ["数据库迁移"], "analysisEnabled": True},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": f'"{self.watch["revision"]}"'},
        )
        app.PullwiseHandler.route(patch_watch, "PATCH")
        toggle = RouteHarness(
            f"/api/v1/watches/{self.watch['id']}",
            {"enabled": False},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": f'"{patch_watch.payload["revision"]}"'},
        )
        app.PullwiseHandler.route(toggle, "PATCH")
        delete = RouteHarness(
            f"/api/v1/watches/{self.watch['id']}",
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": f'"{toggle.payload["revision"]}"'},
        )
        app.PullwiseHandler.route(delete, "DELETE")
        after = RouteHarness("/api/v1/watches", cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(after, "GET")

        self.assertEqual(patch_watch.status, HTTPStatus.OK)
        self.assertEqual(patch_watch.payload["contextVersion"], 2)
        self.assertTrue(patch_watch.payload["analysisEnabled"])
        self.assertEqual(toggle.payload["contextVersion"], 2)
        self.assertFalse(toggle.payload["enabled"])
        self.assertEqual(delete.status, HTTPStatus.NO_CONTENT)
        self.assertEqual(after.payload["items"], [])

    def test_create_watch_is_idempotent_across_session_and_api_key_without_analysis_submission(self) -> None:
        body = {
            "upstream": {"owner": "acme", "repository": "toolkit"},
            "targetRepositoryId": None,
            "interests": ["OAuth"],
            "includePrerelease": False,
            "analysisEnabled": True,
            "enabled": True,
        }
        with patch(
            "pullwise_server.product_api.resolve_upstream_repository",
            return_value={"id": "github:999", "private": False, "fullName": "acme/toolkit"},
        ):
            session = RouteHarness(
                "/api/v1/watches",
                body,
                cookie=f"{app.SESSION_COOKIE}=ses_1",
                headers={"Origin": "https://app.pullwise.dev", "Idempotency-Key": "create-watch"},
            )
            app.PullwiseHandler.route(session, "POST")
            token = self.api_key(["watches:write", "watches:read"])
            replay = RouteHarness(
                "/v1/watches",
                body,
                headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "create-watch"},
            )
            app.PullwiseHandler.route(replay, "POST")

        self.assertEqual(session.status, HTTPStatus.CREATED)
        self.assertEqual(replay.status, HTTPStatus.CREATED)
        self.assertEqual(replay.payload, session.payload)
        self.assertEqual(session.payload["upstreamRepositoryId"], "github:999")
        self.assertTrue(session.payload["analysisEnabled"])
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_new_api_keys_default_to_current_read_scopes_only(self) -> None:
        create = RouteHarness(
            "/api-keys",
            {"name": "Read automation"},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev"},
        )

        app.PullwiseHandler.route(create, "POST")

        self.assertEqual(create.status, HTTPStatus.CREATED)
        self.assertEqual(
            create.payload["scopes"],
            ["profile:read", "repositories:read", "items:read", "watches:read", "usage:read"],
        )
        self.assertNotIn("scans:write", create.payload["scopes"])
        self.assertNotIn("sync:write", create.payload["scopes"])

    def test_repository_service_put_and_get_share_contract_without_model_submission(self) -> None:
        body = {
            "enabled": True,
            "modules": {"pr": True, "ci": True},
            "analysisEnabled": {"pr": True, "ci": False},
            "allowMemberSync": False,
            "defaultAssigneeId": None,
            "priorityOrder": 0,
        }
        put = RouteHarness(
            f"/api/v1/repositories/{self.repository['id']}/service",
            body,
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "If-Match": '"0"'},
        )
        app.PullwiseHandler.route(put, "PUT")
        token = self.api_key(["repositories:read"])
        get = RouteHarness(
            f"/v1/repositories/{self.repository['id']}/service",
            headers={"Authorization": f"Bearer {token}"},
        )
        app.PullwiseHandler.route(get, "GET")
        session_list = RouteHarness("/api/v1/repositories", cookie=f"{app.SESSION_COOKIE}=ses_1")
        key_list = RouteHarness("/v1/repositories", headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(session_list, "GET")
        app.PullwiseHandler.route(key_list, "GET")

        self.assertEqual(put.status, HTTPStatus.OK)
        self.assertEqual(get.status, HTTPStatus.OK)
        self.assertEqual(get.payload, put.payload)
        self.assertEqual(put.payload["modules"], {"pr": True, "ci": True})
        self.assertEqual(put.payload["analysisEnabled"], {"pr": True, "ci": False})
        self.assertEqual(session_list.status, HTTPStatus.OK)
        self.assertEqual(key_list.payload["items"], session_list.payload["items"])
        self.assertEqual(session_list.payload["items"][0]["service"]["revision"], 1)
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_repository_manual_sync_is_fact_only_and_shared_by_session_and_key(self) -> None:
        self.store.put_repository_service(
            repository_id=self.repository["id"],
            installation_id="111",
            billing_owner_id="usr_1",
            expected_revision=0,
            enabled=True,
            modules={"pr": True, "ci": True},
            analysis_enabled={"pr": True, "ci": True},
            allow_member_sync=True,
            default_assignee_id=None,
            priority_order=0,
        )
        session = RouteHarness(
            f"/api/v1/repositories/{self.repository['id']}/sync",
            {},
            cookie=f"{app.SESSION_COOKIE}=ses_1",
            headers={"Origin": "https://app.pullwise.dev", "Idempotency-Key": "repo-sync"},
        )
        app.PullwiseHandler.route(session, "POST")
        token = self.api_key(["repositories:read", "sync:write"])
        key = RouteHarness(
            f"/v1/repositories/{self.repository['id']}/sync",
            {},
            headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "repo-sync-key"},
        )
        app.PullwiseHandler.route(key, "POST")

        self.assertEqual(session.status, HTTPStatus.ACCEPTED)
        self.assertEqual(key.status, HTTPStatus.ACCEPTED)
        self.assertEqual(session.payload["id"], key.payload["id"])
        self.assertEqual(session.payload["operation"], "sync_repository")
        self.assertEqual(self.store.count_jobs(job_type="analyze_source"), 0)

    def test_sync_job_read_respects_api_key_resource_scope(self) -> None:
        from pullwise_server.product_jobs import ProductJobScheduler
        job = ProductJobScheduler(self.store).request_manual_sync(
            resource_kind="watch", resource_id=self.watch["id"], requester_id="usr_1")
        token = self.api_key(["items:read"], restrictions={"watchIds": ["another-watch"]})
        handler = RouteHarness(f"/api/v1/jobs/{job['id']}",
            headers={"Authorization": f"Bearer {token}"})
        app.PullwiseHandler.route(handler, "GET")
        self.assertEqual(handler.status, HTTPStatus.NOT_FOUND)

    def test_sync_job_read_hides_disabled_repository_service(self) -> None:
        from pullwise_server.product_jobs import ProductJobScheduler
        self.store.put_repository_service(repository_id=self.repository["id"],
            installation_id="111", billing_owner_id="usr_1", expected_revision=0,
            enabled=True, modules={"pr": True, "ci": False},
            analysis_enabled={"pr": False, "ci": False}, allow_member_sync=False,
            default_assignee_id=None, priority_order=0)
        job = ProductJobScheduler(self.store).request_manual_sync(
            resource_kind="repository", resource_id=self.repository["id"], requester_id="usr_1")
        path = f"/api/v1/jobs/{job['id']}"
        current = RouteHarness(path, cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(current, "GET")
        self.assertEqual(current.status, HTTPStatus.OK)
        with self.store._immediate() as connection:
            connection.execute("UPDATE repository_services SET enabled=0,status='paused' WHERE repository_id=?",
                (self.repository["id"],))
        stale = RouteHarness(path, cookie=f"{app.SESSION_COOKIE}=ses_1")
        app.PullwiseHandler.route(stale, "GET")
        self.assertEqual(stale.status, HTTPStatus.NOT_FOUND)


if __name__ == "__main__":
    unittest.main()
