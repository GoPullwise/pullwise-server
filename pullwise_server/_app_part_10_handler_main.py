from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

class PullwiseHandler(BaseHTTPRequestHandler):
    server_version = "PullwiseDevAPI/0.1"

    def log_message(self, fmt: str, *args) -> None:
        access_logger.info("%s - %s", self.address_string(), fmt % args)

    def apply_rate_limit(self, method: str, path: str) -> bool:
        if not rate_limit_enabled() or rate_limit_exempt_path(method, path):
            self._rate_limit_headers = {}
            return False
        if path.startswith("/worker/") and worker_token_record(self, allow_disabled=True):
            self._rate_limit_headers = {}
            return False
        limit = rate_limit_requests()
        if limit <= 0:
            self._rate_limit_headers = {}
            return False

        try:
            rate = db.record_rate_limit_hit(
                self.rate_limit_subject(),
                limit=limit,
                window_seconds=rate_limit_window_seconds(),
            )
        except Exception:
            logger.exception("Failed to apply API rate limit.")
            self._rate_limit_headers = {}
            return False
        headers = {
            "X-RateLimit-Limit": str(rate["limit"]),
            "X-RateLimit-Remaining": str(rate["remaining"]),
            "X-RateLimit-Reset": str(rate["resetAt"]),
        }
        self._rate_limit_headers = headers
        if rate["allowed"]:
            return False

        retry_after = str(rate["retryAfter"])
        self.json(
            {"message": "API rate limit exceeded. Try again later."},
            HTTPStatus.TOO_MANY_REQUESTS,
            headers={**headers, "Retry-After": retry_after},
        )
        return True

    def rate_limit_subject(self) -> str:
        session = self.current_session()
        if session:
            return f"user:{session['userId']}"
        return f"ip:{self.client_ip_address()}"

    def client_ip_address(self) -> str:
        if env_flag("PULLWISE_TRUST_PROXY_HEADERS"):
            forwarded = first_header_value(self, "X-Forwarded-For")
            if forwarded:
                candidate = forwarded.split(",", 1)[0].strip()
                if candidate and not any(char in candidate for char in "\r\n"):
                    return candidate[:128]
        address = getattr(self, "client_address", None)
        if isinstance(address, tuple | list) and address:
            return str(address[0])[:128]
        return "unknown"

    def do_OPTIONS(self) -> None:
        try:
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_cors_headers()
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Pullwise-Api-Key")
            self.end_headers()
        except _CLIENT_DISCONNECT_EXCEPTIONS:
            logger.debug("Client disconnected while handling OPTIONS %s", self.path)

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")

    def do_PATCH(self) -> None:
        self.route("PATCH")

    def do_DELETE(self) -> None:
        self.route("DELETE")

    def route(self, method: str) -> None:
        ensure_state_loaded()
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        segments = [unquote(part) for part in path.split("/") if part]
        self._rate_limit_headers = {}

        try:
            try:
                if self.apply_rate_limit(method, path):
                    return
                self.enforce_body_size_limit(method)
                if cookie_state_change_needs_origin_check(method, path, segments, self) and not request_origin_is_trusted(self):
                    return self.error(HTTPStatus.FORBIDDEN, "State-changing requests must come from a trusted origin.")
                if method == "GET":
                    return self.handle_get(path, params, segments)
                if method == "POST":
                    return self.handle_post(path, params, segments)
                if method == "PATCH":
                    return self.handle_patch(segments)
                if method == "DELETE":
                    return self.handle_delete(segments)
                return self.error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            except ClientDisconnected:
                raise
            except RequestBodyTooLarge as exc:
                return self.error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            except ResourceNotFound as exc:
                return self.error(HTTPStatus.NOT_FOUND, str(exc))
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            except billing.BillingProviderResponseError as exc:
                return self.error(HTTPStatus.BAD_GATEWAY, str(exc))
            except billing.BillingConfigurationError as exc:
                return self.error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
            except Exception as exc:
                logger.exception("Unhandled server error while handling %s %s", method, self.path)
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Server error.")
        except ClientDisconnected:
            logger.debug("Client disconnected while handling %s %s", method, self.path)
            return
        finally:
            cleanup_server_resources_if_due()
            persist_state()

    def handle_get(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/health":
            return self.json({
                "ok": True,
                "service": "pullwise-server",
                "time": now(),
                "mode": env("PULLWISE_MODE", "local"),
                "database": {"type": "sqlite", "configured": True},
                "scanSystem": scan_system_status_payload(),
                **readiness_payload(),
            })
        if path == "/install-worker.sh":
            return self.text(worker_install_script(), content_type="text/x-shellscript; charset=utf-8")
        if path == "/status/system":
            return self.json(scan_system_status_payload())
        if segments and segments[0] == "admin":
            return self.handle_admin_get(segments, params)
        if path == "/pricing":
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            return self.json(pricing_payload(user))
        if path == "/docs/subscription-plans":
            return self.json(subscription_plan_agent_configs_payload())
        if path == "/docs/server-config":
            return self.json(system_config.public_docs_payload())
        if path in {"/api-docs", "/api/docs"}:
            return self.json(api_docs_payload())
        api_segments = external_api_segments(segments)
        if api_segments is not None:
            return self.handle_external_api_get(api_segments, params)
        if path == "/auth/session":
            return self.json(session_payload(self.current_session()))
        if path == "/dashboard/overview":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing the dashboard.")
            return self.json(dashboard_overview_payload(session))
        if path == "/api-keys":
            return self.handle_api_keys_get(params)
        if path == "/auth/github/authorize":
            return self.handle_github_authorize(params)
        if path == "/auth/github/callback":
            return self.handle_github_callback(params)
        if path == "/integrations":
            return self.json(self.integrations_payload())
        if path == "/integrations/github/authorize":
            return self.handle_github_repository_authorize(params)
        if path == "/integrations/github/callback":
            return self.handle_github_repository_callback(params)
        if path == "/integrations/github/install/start":
            return self.handle_github_install_start(params)
        if path == "/integrations/github/manage/start":
            return self.handle_github_manage_start(params)
        if path == "/dev/magic-links" or path == "/auth/email/callback":
            return self.error(HTTPStatus.NOT_FOUND, "Route not found")
        if path == "/repositories":
            return self.json(self.repositories_payload())
        if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "branches":
            return self.handle_repository_branches(segments[1])
        if path == "/scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scans = filter_user_scan_payloads([scan_payload(scan) for scan in user_scans(session)], params)
            return self.json(paginated_response(scans, keys=("scans",), params=params))
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "audit-bundle.zip":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scan = self.find_or_404(user_scans(session), segments[1], "Scan")
            filename_scan_id = audit_bundle_safe_artifact_name(public_issue_text(scan.get("id")) or "scan")
            cache_key = audit_bundle_cache_key(scan)
            return self.binary(
                get_or_create_scan_audit_bundle_zip_bytes(scan),
                content_type="application/zip",
                headers={
                    "Content-Disposition": f'attachment; filename="pullwise-audit-{filename_scan_id}.zip"',
                    "ETag": f'"{cache_key}"',
                    "Cache-Control": "private, max-age=3600",
                },
            )
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "audit-bundle":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scan = self.find_or_404(user_scans(session), segments[1], "Scan")
            return self.json(scan_audit_bundle_payload(scan))
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "impact-graph":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scan = self.find_or_404(user_scans(session), segments[1], "Scan")
            public_scan = scan_payload(scan)
            impact_graph = public_scan.get("impactGraph") if isinstance(public_scan.get("impactGraph"), dict) else {}
            return self.json({"impactGraph": impact_graph})
        if len(segments) == 4 and segments[0] == "scans" and segments[2] == "impact-graph" and segments[3] == "focus":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            scan = self.find_or_404(user_scans(session), segments[1], "Scan")
            focus_path = fix_workflow.safe_issue_file(params.get("path"))
            if not focus_path:
                return self.error(HTTPStatus.BAD_REQUEST, "A repo-relative path is required.")
            public_scan = scan_payload(scan)
            impact_graph = public_scan.get("impactGraph") if isinstance(public_scan.get("impactGraph"), dict) else {}
            repository_graph = (
                public_scan.get("repositoryGraph") if isinstance(public_scan.get("repositoryGraph"), dict) else {}
            )
            return self.json(public_impact_graph_focus(impact_graph, focus_path, repository_graph=repository_graph))
        if len(segments) == 2 and segments[0] == "scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing scans.")
            return self.json(scan_payload(self.find_or_404(user_scans(session), segments[1], "Scan")))
        if path == "/issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            issue_payloads = filter_user_issue_payloads([issue_payload(issue) for issue in user_issues(session)], params)
            return self.json(paginated_response(issue_payloads, keys=("issues",), params=params))
        if len(segments) == 2 and segments[0] == "issues":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing issues.")
            return self.json(issue_payload(self.find_or_404(user_issues(session), segments[1], "Issue")))
        if path == "/settings":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing settings.")
            return self.json(settings_payload(session["userId"]))
        if path == "/billing/plan":
            session = self.current_session()
            user = USERS.get(session["userId"]) if session else None
            return self.json(pricing_payload(user))
        if path == "/billing":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing billing.")
            return self.json(billing_page_payload(USERS[session["userId"]]))
        # Static file serving + SPA fallback for client-side routing
        root = web_root()
        if os.path.isdir(root):
            # Try to serve the exact file
            rel = path.lstrip("/")
            root_path = os.path.abspath(root)
            candidate = os.path.abspath(os.path.join(root_path, rel))
            # Prevent path traversal
            try:
                inside_root = os.path.commonpath([root_path, candidate]) == root_path
            except ValueError:
                inside_root = False
            if inside_root and os.path.isfile(candidate):
                return self.serve_static_file(candidate)
            # SPA fallback: serve index.html for any other GET
            return self.serve_spa()
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_post(self, path: str, params: dict, segments: list[str]) -> None:
        if path == "/webhooks/creem":
            return self.handle_creem_webhook()
        body = self.read_json()
        api_segments = external_api_segments(segments)
        if api_segments is not None:
            return self.handle_external_api_post(api_segments, body)
        if segments and segments[0] == "worker":
            return self.handle_worker_post(segments, body)
        if segments and segments[0] == "admin":
            return self.handle_admin_post(segments, body)
        if path == "/auth/sign-out":
            self.clear_current_session()
            return self.json({"ok": True}, headers={"Set-Cookie": clear_cookie_header()})
        if path == "/api-keys":
            return self.handle_api_keys_post(body)
        if (
            len(segments) == 5
            and segments[0] == "integrations"
            and segments[1] == "github"
            and segments[2] == "installations"
            and segments[4] == "manage-sessions"
        ):
            return self.handle_github_installation_manage_session(segments[3], body)
        if path == "/repositories/sync":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            installation_id = clean_github_access_text(body.get("installationId"), allow_int=True)
            github_identity_id = clean_github_access_text(body.get("githubIdentityId"))
            user = USERS.get(session["userId"])
            if not user:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before syncing repositories.")
            if installation_id or github_identity_id:
                if not installation_id:
                    return self.error(HTTPStatus.BAD_REQUEST, "installationId is required for scoped repository sync.")
                sync_github_repository_installation_scope(
                    user,
                    installation_id,
                    github_identity_id=github_identity_id,
                )
                payload = self.repositories_payload(refresh=False)
            else:
                payload = self.repositories_payload(
                    refresh=repository_sync_should_refresh(
                        user,
                        user.get("githubRepositoryAccess"),
                        body,
                    )
                )
            payload.update({"ok": True, "syncedAt": now()})
            return self.json(payload)
        if path == "/scans/preflight":
            return self.handle_scan_preflight(body)
        if path == "/scans":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting a scan.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            requested_repo_id = clean_github_access_text(body.get("repoId"), allow_int=True)
            requested_repository = clean_repository_full_name(body.get("repo"))
            if not requested_repo_id and not requested_repository:
                return self.error(HTTPStatus.BAD_REQUEST, "A repository is required to start a scan.")
            repository = requested_repository or requested_repo_id or ""
            request_id = scan_request_id_from_body(body)
            scan_error: tuple[int, str] | None = None
            scan_error_code: str | None = None
            scan_error_repo_id: str | None = None
            scan = None
            scan_created = False
            branch = ""
            commit = "pending"
            changed_files = public_changed_files(body.get("changedFiles") or body.get("changed_files"))
            base_commit = clean_github_access_text(body.get("baseCommit") or body.get("base_commit"))
            with STATE_LOCK:
                user = USERS.get(session["userId"]) or {}
                github_access = user.get("githubRepositoryAccess")
                if not github_access:
                    scan_error = (HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
                elif github_repository_authorization_pending(user):
                    scan_error = (HTTPStatus.FORBIDDEN, "Complete GitHub repository authorization before starting a scan.")
                elif not github_repository_access_authorized_for_user(user, github_access):
                    scan_error = (HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
                elif github_repositories_need_sync(github_access):
                    scan_error = (HTTPStatus.FORBIDDEN, "Sync GitHub repositories before starting a scan.")
                    scan_error_code = "REPOSITORY_SYNC_REQUIRED"
                else:
                    scan = user_scan_by_request_id(session["userId"], request_id)
                    if scan is not None and not scan_matches_requested_repository(
                        scan,
                        requested_repo_id=requested_repo_id,
                        requested_repository=requested_repository,
                    ):
                        scan_error = (HTTPStatus.CONFLICT, IDEMPOTENCY_KEY_REUSED_MESSAGE)
                        scan_error_code = "IDEMPOTENCY_KEY_REUSED"
                        scan_error_repo_id = clean_github_access_text(scan.get("repoId"), allow_int=True)
                        scan = None
                    elif scan is None:
                        limit_error = scan_queue_limit_error(session["userId"])
                        if limit_error:
                            scan_error = (limit_error[0], limit_error[1])
                            scan_error_code = limit_error[2]
                        if scan_error is not None:
                            pass
                        else:
                            repo_meta, request_key = repository_item_for_scan_request(github_access, body)
                            if not repo_meta:
                                scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                                scan_error_code = "REPOSITORY_NOT_AUTHORIZED"
                            else:
                                repository = clean_repository_full_name(repo_meta.get("fullName"), requested_repository)
                                if not repository:
                                    scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                                    scan_error_code = "REPOSITORY_NOT_AUTHORIZED"
                                elif request_key != "repoId" and not repository_is_authorized(github_access, repository):
                                    scan_error = (HTTPStatus.FORBIDDEN, "Repository is not authorized for this GitHub App installation.")
                                    scan_error_code = "REPOSITORY_NOT_AUTHORIZED"
                        if scan_error is None:
                            commit, commit_error = scan_commit_from_body(body)
                            if commit_error:
                                scan_error = (HTTPStatus.BAD_REQUEST, commit_error)
                                scan_error_code = "INVALID_COMMIT"
                        if scan_error is None:
                            requested_branch = github_auth.clean_branch_name(body.get("branch"))
                            branch = (
                                requested_branch
                                or github_auth.clean_branch_name(repo_meta.get("defaultBranch"))
                                or "main"
                            )
                            if requested_branch:
                                try:
                                    branch_available = scan_branch_is_available(github_access, repo_meta, branch)
                                except github_auth.GitHubError as exc:
                                    scan_error = (HTTPStatus.BAD_GATEWAY, str(exc))
                                    scan_error_code = "BRANCH_LOOKUP_FAILED"
                                if scan_error is None and not branch_available:
                                    scan_error = (
                                        HTTPStatus.BAD_REQUEST,
                                        "Selected branch is not available for this repository.",
                                    )
                                    scan_error_code = "BRANCH_NOT_AVAILABLE"
                        if scan_error is None:
                            scan_id = make_id("sc")
                            try:
                                scan_user, repository_record = scan_resource_context(user, github_access, repo_meta)
                            except ValueError as exc:
                                code = str(exc)
                                if code == "REPOSITORY_SYNC_REQUIRED":
                                    scan_error = (
                                        HTTPStatus.CONFLICT,
                                        "Sync GitHub repositories before starting a scan so Pullwise can verify the stable repository ID.",
                                    )
                                    scan_error_code = "REPOSITORY_SYNC_REQUIRED"
                                else:
                                    scan_error = (HTTPStatus.BAD_REQUEST, "Unable to resolve repository context.")
                            else:
                                try:
                                    quota_result = quota.consume_scan_quota(
                                        user=scan_user,
                                        repository=repository_record,
                                        requested_by_user_id=session["userId"],
                                        scan_id=scan_id,
                                        request_id=request_id or None,
                                    )
                                except quota.QuotaExceeded as exc:
                                    scan_error = (HTTPStatus.PAYMENT_REQUIRED, exc.message)
                                    scan_error_code = exc.code
                                    scan_error_repo_id = exc.repo_id
                                else:
                                    entitlement = quota_result["user"]
                                    if quota_result.get("deduplicated"):
                                        scan = user_scan_by_request_id(session["userId"], request_id)
                                        if scan is None or not scan_matches_requested_repository(
                                            scan,
                                            requested_repo_id=requested_repo_id,
                                            requested_repository=requested_repository,
                                        ):
                                            scan_error = (HTTPStatus.CONFLICT, IDEMPOTENCY_KEY_REUSED_MESSAGE)
                                            scan_error_code = "IDEMPOTENCY_KEY_REUSED"
                        if scan_error:
                            pass
                        elif scan is not None:
                            pass
                        else:
                            review_output_language = settings_payload(session["userId"])["review"]["outputLanguage"]
                            scan = {
                                "id": scan_id,
                                "repo": repository,
                                "branch": branch,
                                "commit": commit,
                                "status": "queued",
                                "userId": session["userId"],
                                "createdAt": now(),
                                "queuedAt": now(),
                                "progress": 0,
                                "phase": None,
                                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                                "reviewOutputLanguage": review_output_language,
                                "installationId": (
                                    clean_github_access_text(repo_meta.get("installationId"), allow_int=True)
                                    or clean_github_access_text(github_access.get("installationId"), allow_int=True)
                                ),
                                "installationAccount": (
                                    clean_github_access_text(repo_meta.get("installationAccount"))
                                    or clean_github_access_text(github_access.get("installationAccount"))
                                ),
                                "repositorySelection": (
                                    clean_github_access_text(repo_meta.get("repositorySelection"))
                                    or clean_github_access_text(github_access.get("repositorySelection"))
                                ),
                                "repoId": repository_record["id"],
                                "githubRepoId": repository_record["github_repo_id"],
                                "quotaBucketIds": quota_result["bucketIds"],
                                "cloneUrl": repo_meta.get("cloneUrl"),
                                "repositoryPrivate": bool(repo_meta.get("private")),
                                "repoPath": None,
                                "billingUsage": quota_result["user"],
                                "repoUsage": quota_result["repository"],
                                "by": "you",
                            }
                            if request_id:
                                scan["requestId"] = request_id
                            if changed_files:
                                scan["changedFiles"] = changed_files
                            if base_commit:
                                scan["baseCommit"] = base_commit
                            SCANS.insert(0, scan)
                            scan_created = True
                            mark_state_dirty()
                            try:
                                create_scan_job_for_scan(scan)
                            except Exception:
                                SCANS[:] = [item for item in SCANS if item.get("id") != scan_id]
                                scan_created = False
                                mark_state_dirty()
                                if not quota_result.get("deduplicated"):
                                    quota.rollback_scan_quota(
                                        scan_id=scan_id,
                                        requested_by_user_id=session["userId"],
                                        request_id=request_id or None,
                                    )
                                raise

            if scan_error:
                scan_logging.log_event(
                    "scan_create_rejected",
                    userId=session["userId"],
                    repo=repository,
                    provider="worker",
                    httpStatus=int(scan_error[0]),
                    reason=scan_error[1],
                    requestId=request_id or None,
                    code=scan_error_code,
                    repoId=scan_error_repo_id,
                )
                payload = {"message": scan_error[1]}
                if scan_error_code:
                    payload["code"] = scan_error_code
                if scan_error_repo_id:
                    payload["repoId"] = scan_error_repo_id
                return self.json(payload, scan_error[0])
            if scan is None:
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to create scan.")
            if scan_created:
                scan_logging.log_event(
                    "scan_queued",
                    scanId=scan["id"],
                    userId=scan.get("userId"),
                    repo=scan.get("repo"),
                    branch=scan.get("branch"),
                    commit=scan.get("commit"),
                    provider="worker",
                    requestId=scan.get("requestId"),
                    installationId=scan.get("installationId"),
                    repoId=scan.get("repoId"),
                    githubRepoId=scan.get("githubRepoId"),
                    quotaBucketIds=scan.get("quotaBucketIds"),
                )
            else:
                scan_logging.log_event(
                    "scan_request_reused",
                    scanId=scan.get("id"),
                    userId=scan.get("userId"),
                    repo=scan.get("repo"),
                    branch=scan.get("branch"),
                    commit=scan.get("commit"),
                    provider="worker",
                    requestId=request_id or None,
                    status=scan.get("status"),
                )
            return self.json(scan_payload(scan), HTTPStatus.CREATED if scan_created else HTTPStatus.OK)
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "retry":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before retrying a scan.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            retry_request_id = scan_request_id_from_body(body)
            quota_result = None
            scan = None
            repository = None
            try:
                with STATE_LOCK:
                    user = USERS.get(session["userId"])
                    if not user:
                        return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before retrying a scan.")
                    scan = self.find_or_404(user_scans(session), segments[1], "Scan")
                    job = db.get_scan_job_for_scan(public_issue_text(scan.get("id")))
                    job_status = public_issue_text((job or {}).get("status")).lower()
                    scan_status = public_issue_text(scan.get("status")).lower()
                    if scan_status not in {"failed", "lost", "cancelled"} or (
                        job and job_status not in {"failed", "lost", "cancelled"}
                    ):
                        return self.error(HTTPStatus.CONFLICT, "Only failed, lost, or cancelled scans can be retried.")
                    if not retry_request_id or retry_request_id == public_issue_text(scan.get("requestId")):
                        retry_request_id = make_id("retry")
                    repo_id = public_issue_text(scan.get("repoId"))
                    repository = db.get_repository(repo_id) if repo_id else None
                    if not repository:
                        return self.error(HTTPStatus.BAD_REQUEST, "Unable to resolve repository context for retry.")
                    scan_id = public_issue_text(scan.get("id"))
                quota_result = quota.consume_scan_quota(
                    user=user,
                    repository=repository,
                    requested_by_user_id=session["userId"],
                    scan_id=scan_id,
                    request_id=retry_request_id,
                )
                if quota_result.get("deduplicated"):
                    return self.json(
                        {"message": "Retry requestId has already been used.", "code": "IDEMPOTENCY_KEY_REUSED"},
                        HTTPStatus.CONFLICT,
                    )
                with STATE_LOCK:
                    scan = self.find_or_404(user_scans(session), segments[1], "Scan")
                    job = db.get_scan_job_for_scan(public_issue_text(scan.get("id")))
                    job_status = public_issue_text((job or {}).get("status")).lower()
                    scan_status = public_issue_text(scan.get("status")).lower()
                    if scan_status not in {"failed", "lost", "cancelled"} or (
                        job and job_status not in {"failed", "lost", "cancelled"}
                    ):
                        raise RuntimeError("Only failed, lost, or cancelled scans can be retried.")
                    retried_job = retry_scan_job_for_scan_locked(scan, queued_at=now())
                    scan["requestId"] = retry_request_id
                    scan["quotaBucketIds"] = quota_result["bucketIds"]
                    scan["billingUsage"] = quota_result["user"]
                    scan["repoUsage"] = quota_result["repository"]
                    mark_state_dirty()
                scan_logging.log_event(
                    "scan_retry_queued",
                    scanId=scan.get("id"),
                    userId=scan.get("userId"),
                    repo=scan.get("repo"),
                    branch=scan.get("branch"),
                    commit=scan.get("commit"),
                    provider="worker",
                    requestId=retry_request_id,
                    jobId=retried_job.get("job_id"),
                    quotaBucketIds=scan.get("quotaBucketIds"),
                )
                return self.json(scan_payload(scan), HTTPStatus.CREATED)
            except quota.QuotaExceeded as exc:
                payload = {"message": exc.message, "code": exc.code}
                if exc.repo_id:
                    payload["repoId"] = exc.repo_id
                return self.json(payload, HTTPStatus.PAYMENT_REQUIRED)
            except RuntimeError as exc:
                if quota_result and not quota_result.get("deduplicated"):
                    quota.rollback_scan_quota(
                        scan_id=public_issue_text((scan or {}).get("id")) or segments[1],
                        requested_by_user_id=session["userId"],
                        request_id=retry_request_id,
                    )
                return self.error(HTTPStatus.CONFLICT, str(exc))
            except Exception:
                if quota_result and not quota_result.get("deduplicated"):
                    quota.rollback_scan_quota(
                        scan_id=public_issue_text((scan or {}).get("id")) or segments[1],
                        requested_by_user_id=session["userId"],
                        request_id=retry_request_id,
                    )
                raise
        if len(segments) == 3 and segments[0] == "scans" and segments[2] == "cancel":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before cancelling a scan.")
            with STATE_LOCK:
                scan = self.find_or_404(user_scans(session), segments[1], "Scan")
                if scan.get("status") not in {"queued", "running"}:
                    return self.error(HTTPStatus.CONFLICT, "Only queued or running scans can be cancelled.")
                scan["status"] = "cancelled"
                scan["completedAt"] = now()
                mark_state_dirty()
                db.cancel_scan_job_for_scan(str(scan.get("id") or ""))
            return self.json(scan_payload(scan))
        if len(segments) == 4 and segments[0] == "issues" and segments[2] == "fixes" and segments[3] == "preview":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before previewing fixes.")
            issue = self.find_or_404(user_issues(session), segments[1], "Issue")
            try:
                preview = preview_issue_fix_for_user(USERS[session["userId"]], issue)
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            return self.json(preview, HTTPStatus.OK if preview.get("valid") else HTTPStatus.BAD_REQUEST)
        if len(segments) == 4 and segments[0] == "issues" and segments[2] == "fixes" and segments[3] == "apply":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "Applying fixes is not implemented on this backend.")
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "pull-requests":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before creating pull requests.")
            issue = next((item for item in user_issues(session) if item.get("id") == segments[1]), None)
            if not issue:
                return self.error(HTTPStatus.NOT_FOUND, "Issue not found.")
            try:
                pull_request = create_issue_pull_request(USERS[session["userId"]], issue)
            except github_auth.GitHubError as exc:
                return self.error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            return self.json(pull_request)
        if len(segments) == 2 and segments[0] == "integrations":
            return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration writes are not implemented on this backend.")
        if path == "/billing/checkout-sessions":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting checkout.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            if effective_billing_plan(user) in billing.PAID_PLAN_IDS:
                return self.error(HTTPStatus.CONFLICT, "An active paid subscription already exists. Change or cancel billing from the Billing page.")
            success_url = safe_redirect_to(body.get("successUrl"), "settings")
            plan = str(body.get("plan") or "pro")
            interval = str(body.get("interval") or "month")
            checkout = billing.create_checkout_session(
                user,
                success_url=success_url,
                cancel_url=safe_redirect_to(body.get("cancelUrl"), "settings"),
                plan=plan,
                interval=interval,
            )
            checkout = safe_billing_redirect_response(checkout, "Checkout", require_url=True)
            if checkout.get("customerId"):
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": checkout.get("provider") or current_billing.get("provider"),
                    "customerId": checkout.get("customerId"),
                }
            user["billingCheckout"] = {
                "provider": checkout.get("provider"),
                "id": checkout.get("id"),
                "requestId": checkout.get("requestId"),
                "plan": checkout.get("plan"),
                "interval": checkout.get("interval"),
                "createdAt": now(),
            }
            mark_state_dirty()
            return self.json(checkout)
        if path == "/billing/change-interval":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before changing your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.change_subscription_interval(
                user,
                interval=str(body.get("interval") or "year"),
                plan=str(body.get("plan") or ""),
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "subscription change")
            if result.get("alreadyActive"):
                return self.json(result)
            if result.get("provider") == "creem":
                current_billing = user.get("billing") or {}
                next_status = result.get("status") or current_billing.get("status") or "active"
                restored_subscription = billing.normalize_subscription_status(next_status) in {"active", "trialing"}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": next_status,
                    "plan": billing.normalize_plan(result.get("plan") or current_billing.get("plan") or "pro"),
                    "interval": billing.normalize_interval(result.get("interval") or current_billing.get("interval")),
                    "cancelAtPeriodEnd": result.get("cancelAtPeriodEnd")
                    if isinstance(result.get("cancelAtPeriodEnd"), bool)
                    else (False if restored_subscription else current_billing.get("cancelAtPeriodEnd")),
                    "canceledAt": result.get("canceledAt")
                    if "canceledAt" in result
                    else (None if restored_subscription else current_billing.get("canceledAt")),
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                }
                mark_state_dirty()
            return self.json(result)
        if path == "/billing/cancel-subscription":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before canceling your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.cancel_subscription(
                user,
                mode=str(body.get("mode") or "scheduled"),
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "subscription cancellation")
            if result.get("provider") == "creem":
                current_billing = user.get("billing") or {}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": result.get("status") or current_billing.get("status") or "canceling",
                    "plan": billing.normalize_plan(result.get("plan") or current_billing.get("plan") or "pro"),
                    "interval": billing.normalize_interval(result.get("interval") or current_billing.get("interval")),
                    "cancelAtPeriodEnd": result.get("cancelAtPeriodEnd")
                    if isinstance(result.get("cancelAtPeriodEnd"), bool)
                    else current_billing.get("cancelAtPeriodEnd"),
                    "canceledAt": result.get("canceledAt") or current_billing.get("canceledAt"),
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                }
                mark_state_dirty()
            return self.json(result)
        if path == "/billing/resume-subscription":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before resuming your subscription.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            user = USERS[session["userId"]]
            result = billing.resume_subscription(
                user,
                return_url=safe_redirect_to(body.get("returnUrl"), "billing"),
            )
            result = safe_billing_redirect_response(result, "subscription resume")
            if result.get("provider") == "creem":
                current_billing = user.get("billing") or {}
                next_status = result.get("status") or current_billing.get("status") or "active"
                restored_subscription = billing.normalize_subscription_status(next_status) in {"active", "trialing"}
                user["billing"] = {
                    **current_billing,
                    "provider": "creem",
                    "subscriptionId": result.get("subscriptionId") or current_billing.get("subscriptionId"),
                    "status": next_status,
                    "plan": billing.normalize_plan(result.get("plan") or current_billing.get("plan") or "pro"),
                    "interval": billing.normalize_interval(result.get("interval") or current_billing.get("interval")),
                    "cancelAtPeriodEnd": result.get("cancelAtPeriodEnd")
                    if isinstance(result.get("cancelAtPeriodEnd"), bool)
                    else (False if restored_subscription else current_billing.get("cancelAtPeriodEnd")),
                    "canceledAt": result.get("canceledAt")
                    if "canceledAt" in result
                    else (None if restored_subscription else current_billing.get("canceledAt")),
                    "currentPeriodStart": result.get("currentPeriodStart") or current_billing.get("currentPeriodStart"),
                    "currentPeriodEnd": result.get("currentPeriodEnd") or current_billing.get("currentPeriodEnd"),
                }
                mark_state_dirty()
            return self.json(result)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_patch(self, segments: list[str]) -> None:
        body = self.read_json()
        if segments and segments[0] == "admin":
            return self.handle_admin_patch(segments, body)
        if len(segments) == 3 and segments[0] == "issues" and segments[2] == "status":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before updating issue status.")
            issue = find_issue_for_status_update(user_issues(session), segments[1], body)
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            next_status = str(body.get("status") or issue["status"]).strip().lower()
            if next_status not in ISSUE_STATUSES:
                return self.error(HTTPStatus.BAD_REQUEST, "Issue status must be open, fixed, or snoozed.")
            issue["status"] = next_status
            feedback_reason, _ = review_user_feedback_reason(body)
            if feedback_reason:
                issue["feedbackReason"] = feedback_reason
            record_issue_status_outcome_label(issue, next_status=next_status, body=body, user_id=session["userId"])
            mark_state_dirty()
            return self.json(issue_payload(issue))
        if len(segments) == 1 and segments[0] == "settings":
            session = self.current_session()
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before updating settings.")
            if not isinstance(body, dict):
                return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            return self.json(apply_settings_update(session["userId"], body))
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_delete(self, segments: list[str]) -> None:
        if segments == ["worker", "registry"]:
            worker_record = self.require_worker(allow_disabled=True, include_deleted=True)
            if not worker_record:
                return
            worker_id = public_issue_text(worker_record.get("worker_id"))
            worker = db.soft_delete_worker(worker_id) or db.get_worker(worker_id, include_deleted=True)
            if not worker:
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            db.record_worker_audit_event(
                {
                    "actor_user_id": "",
                    "action": "worker_self_unregister",
                    "worker_id": worker_id,
                    "changed_fields": {"deleted": True},
                    "request_id": request_id_from_handler(self),
                    "created_at": now(),
                }
            )
            return self.json({"ok": True, "worker": worker_public_payload(worker, admin=True), "deleted": True})
        if segments and segments[0] == "admin":
            return self.handle_admin_delete(segments)
        if len(segments) == 2 and segments[0] == "api-keys":
            return self.handle_api_key_delete(segments[1])
        if len(segments) == 2 and segments[0] == "integrations":
            session = self.current_session()
            if segments[1] != "github":
                return self.error(HTTPStatus.NOT_IMPLEMENTED, f"{segments[1]} integration disconnect is not implemented on this backend.")
            if not session:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before disconnecting GitHub.")
            USERS[session["userId"]]["githubRepositoryAccess"] = None
            USERS[session["userId"]].pop("githubRepositoryAccessPending", None)
            mark_state_dirty()
            return self.json({"ok": True, "provider": "github", "connected": False})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_github_authorize(self, params: dict) -> None:
        redirect_to = safe_redirect_to(params.get("redirectTo"), "dashboard")
        redirect_response = params.get("response") == "redirect"
        if not github_auth.oauth_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
            callback = f"{api_base_url(self)}/auth/github/callback?{urlencode({'redirectTo': redirect_to})}"
            if redirect_response:
                return self.redirect(callback)
            return self.json({"url": callback, "mode": "local"})

        verifier = github_auth.make_code_verifier()
        state = remember_github_state("login", redirect_to, codeVerifier=verifier)
        callback_url = f"{api_base_url(self)}/auth/github/callback"
        authorize_url = github_auth.build_oauth_authorize_url(
            callback_url,
            state,
            verifier,
        )
        if redirect_response:
            return self.redirect(authorize_url)
        return self.json({"url": authorize_url, "mode": "github"})

    def handle_github_callback(self, params: dict) -> None:
        if not github_auth.oauth_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured.")
            user = get_or_create_github_user()
            session = create_session(user)
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "dashboard"), cookie_header(session["id"]))

        state = params.get("state") or ""
        record = pop_any_github_state(state)
        if record.get("kind") == "manage_installation":
            return self.handle_github_manage_callback(params, record, state)
        if record.get("kind") == "install_identity":
            return self.handle_github_install_identity_callback(params, record, state)
        if record.get("kind") != "login":
            raise ValueError("GitHub authorization state is invalid or expired.")
        redirect_to = str(record["redirectTo"])
        if params.get("error"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        user = get_or_create_real_github_user(profile, token_payload)
        session = create_session(user)
        return self.redirect(redirect_to, cookie_header(session["id"]))

    def handle_github_installation_manage_session(self, installation_id: str, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before managing GitHub installations.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        if not github_auth.app_install_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured. Set PULLWISE_GITHUB_APP_SLUG or PULLWISE_GITHUB_APP_INSTALL_URL.")

        user = USERS.get(session["userId"])
        github_access = user.get("githubRepositoryAccess") if user else None
        if not github_repository_access_authorized_for_user(user, github_access) or not github_repository_access_connected(github_access):
            return self.error(HTTPStatus.FORBIDDEN, "Connect GitHub repositories before managing an installation.")

        clean_installation_id = clean_github_access_text(installation_id, allow_int=True)
        if not clean_installation_id:
            return self.error(HTTPStatus.BAD_REQUEST, "A GitHub App installation id is required.")
        installation = installation_summary_by_id(github_access, clean_installation_id)
        if not installation:
            return self.error(HTTPStatus.NOT_FOUND, "GitHub App installation is not connected to this Pullwise account.")

        identity_id = clean_github_access_text(body.get("githubIdentityId"))
        if identity_id and not github_identity_by_id(user, identity_id):
            return self.error(HTTPStatus.BAD_REQUEST, "GitHub identity is not linked to this Pullwise account.")

        redirect_to = safe_redirect_to(body.get("returnUrl") or body.get("redirectTo"), "repos")
        state = remember_github_installation_manage_state(
            user,
            installation,
            redirect_to,
            expected_github_identity_id=identity_id,
        )
        url = f"{api_base_url(self)}/integrations/github/manage/start?{urlencode({'state': state})}"
        return self.json({
            "mode": "github-installation-manage",
            "url": url,
            "installationId": clean_installation_id,
        })

    def handle_github_install_start(self, params: dict) -> None:
        state = params.get("state") or ""
        record = peek_github_state("install_identity", state)
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        verifier = github_auth.make_code_verifier()
        record["codeVerifier"] = verifier
        record["oauthStartedAt"] = now()
        mark_state_dirty()
        authorize_url = github_auth.build_oauth_authorize_url(
            f"{api_base_url(self)}/auth/github/callback",
            state,
            verifier,
            prompt="select_account",
        )
        return self.redirect(authorize_url)

    def handle_github_install_identity_callback(self, params: dict, record: dict, state: str) -> None:
        redirect_to = str(record["redirectTo"])
        user = USERS.get(str(record.get("userId") or ""))
        if not user:
            raise ValueError("The GitHub installation identity session belongs to a user session that no longer exists.")
        if params.get("error"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        identity = upsert_github_identity(user, profile, token_payload)
        install_state = remember_github_repository_authorization(
            user,
            redirect_to,
            str(record.get("requestedScope") or "selected"),
            manage=record.get("manage") is True,
            selected_github_identity_id=clean_github_access_text(identity.get("id")),
        )
        return self.redirect(github_auth.build_app_install_url(install_state))

    def handle_github_manage_start(self, params: dict) -> None:
        state = params.get("state") or ""
        record = peek_github_state("manage_installation", state)
        if not github_auth.oauth_configured():
            return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub OAuth is not configured. Set PULLWISE_GITHUB_CLIENT_ID and PULLWISE_GITHUB_CLIENT_SECRET.")
        verifier = github_auth.make_code_verifier()
        record["codeVerifier"] = verifier
        record["oauthStartedAt"] = now()
        mark_state_dirty()
        authorize_url = github_auth.build_oauth_authorize_url(
            f"{api_base_url(self)}/auth/github/callback",
            state,
            verifier,
            prompt="select_account",
        )
        return self.redirect(authorize_url)

    def handle_github_manage_callback(self, params: dict, record: dict, state: str) -> None:
        redirect_to = str(record["redirectTo"])
        if params.get("error"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": params.get("error_description") or params["error"]}))
        if not params.get("code"):
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "missing_oauth_code"}))

        user = USERS.get(str(record.get("userId") or ""))
        if not user:
            raise ValueError("The GitHub manage session belongs to a user session that no longer exists.")
        token_payload = github_auth.exchange_oauth_code(
            params["code"],
            f"{api_base_url(self)}/auth/github/callback",
            str(record.get("codeVerifier") or ""),
            state,
        )
        profile = github_auth.fetch_user_profile(token_payload["access_token"])
        identity = upsert_github_identity(user, profile, token_payload)
        expected_installation_id = clean_github_access_text(record.get("expectedInstallationId"), allow_int=True)
        if not expected_installation_id:
            return self.redirect(redirect_with_params(redirect_to, {"github_error": "github_installation_not_visible"}))

        if self.github_manage_identity_mismatch(identity, record):
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_account_mismatch",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_account_mismatch", identity, record))

        try:
            installations = github_auth.list_current_app_installations_for_user(identity.get("accessToken"))
        except github_auth.GitHubError:
            identity["status"] = "needs_reauth"
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_identity_reauth_required",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_identity_reauth_required", identity, record))

        installation = next(
            (
                item
                for item in installations
                if str(item.get("id") or "") == str(expected_installation_id)
            ),
            None,
        )
        if not installation:
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_not_visible", identity, record))
        if installation.get("suspended_at"):
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_deleted",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_deleted", identity, record))

        html_url = trusted_github_web_url(installation.get("html_url") or record.get("expectedInstallationHtmlUrl"))
        if not html_url:
            upsert_github_identity_installation_access(
                user,
                identity,
                expected_installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
            return self.redirect(self.github_manage_error_redirect(redirect_to, "github_installation_not_visible", identity, record))

        upsert_github_identity_installation_access(
            user,
            identity,
            expected_installation_id,
            can_access=True,
        )
        return self.redirect(
            redirect_with_params(redirect_to, {"github_manage_continue_url": html_url})
        )

    def github_manage_identity_mismatch(self, identity: dict, record: dict) -> bool:
        expected_identity_id = clean_github_access_text(record.get("expectedGithubIdentityId"))
        if expected_identity_id and identity.get("id") != expected_identity_id:
            return True
        expected_target_type = str(record.get("expectedInstallationTargetType") or "").casefold()
        expected_account = str(record.get("expectedAccountLogin") or "").casefold()
        selected_login = str(identity.get("githubLogin") or identity.get("login") or "").casefold()
        return expected_target_type == "user" and expected_account and selected_login and selected_login != expected_account

    def github_manage_error_redirect(self, redirect_to: str, code: str, identity: dict, record: dict) -> str:
        return redirect_with_params(
            redirect_to,
            {
                "github_error": code,
                "github_login": clean_github_access_text(identity.get("githubLogin") or identity.get("login")) or "",
                "installation_account": clean_github_access_text(record.get("expectedAccountLogin")) or "",
            },
        )

    def handle_github_repository_authorize(self, params: dict) -> None:
        scope = params.get("scope") if params.get("scope") in {"all", "selected"} else "all"
        manage = str(params.get("manage") or "").lower() in {"1", "true", "yes", "on"}
        add_installation = str(params.get("add") or "").lower() in {"1", "true", "yes", "on"}
        redirect_to = safe_redirect_to(params.get("redirectTo"), "repos")
        if not github_auth.app_install_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured. Set PULLWISE_GITHUB_APP_SLUG or PULLWISE_GITHUB_APP_INSTALL_URL.")
            callback = f"{api_base_url(self)}/integrations/github/callback?{urlencode({'scope': scope, 'redirectTo': redirect_to})}"
            return self.json({"url": callback, "mode": "local"})

        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before authorizing GitHub repositories.")
        user = USERS.get(session["userId"])
        if not has_github_repository_authorization_identity(user):
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in with GitHub before authorizing repositories.")
        if github_auth.app_visibility_check_enabled():
            if not github_auth.app_slug():
                return self.error(
                    HTTPStatus.NOT_IMPLEMENTED,
                    "PULLWISE_GITHUB_APP_SLUG is required for user repository installs so Pullwise can verify the GitHub App is public.",
                )
            public_installable = github_auth.app_slug_publicly_installable()
            if public_installable is False:
                return self.error(
                    HTTPStatus.CONFLICT,
                    (
                        f"GitHub App '{github_auth.app_slug()}' is private or not publicly visible. "
                        "Make the GitHub App public before connecting repositories from user accounts, "
                        "and keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled for user repository installs."
                    ),
                )
            if public_installable is None:
                return self.error(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    (
                        f"Unable to verify GitHub App '{github_auth.app_slug()}' is public before repository authorization. "
                        "Try again after GitHub API access is available, and keep PULLWISE_GITHUB_APP_VISIBILITY_CHECK enabled for user repository installs."
                    ),
                )

        existing_access = try_bind_existing_github_repository_access(user)
        if add_installation:
            state = remember_github_repository_identity_authorization(user, redirect_to, scope, add=True)
            url = f"{api_base_url(self)}/integrations/github/install/start?{urlencode({'state': state})}"
            return self.json({"url": url, "mode": "github-app-add"})

        if manage:
            existing_installations = installation_summaries_for_access(existing_access)
            if github_repository_access_connected(existing_access) and len(existing_installations) == 1:
                installation = existing_installations[0]
                installation_id = clean_github_access_text(installation.get("installationId"), allow_int=True)
                state = remember_github_installation_manage_state(user, installation, redirect_to)
                url = f"{api_base_url(self)}/integrations/github/manage/start?{urlencode({'state': state})}"
                return self.json({
                    "ok": True,
                    "connected": True,
                    "url": url,
                    "mode": "github-installation-manage",
                    "installationId": installation_id,
                })
            if github_repository_access_connected(existing_access) and existing_installations:
                return self.json({
                    "ok": True,
                    "connected": True,
                    "mode": "github-app-existing-manage-list",
                    "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
                    "installationIds": clean_github_access_text_list(existing_access.get("installationIds"), allow_int=True),
                    "installationAccount": clean_github_access_text(existing_access.get("installationAccount")),
                    "installationAccounts": clean_github_access_text_list(existing_access.get("installationAccounts")),
                    "installations": public_installation_summaries(user, existing_access),
                    "identities": public_github_identities(user),
                })
            state = remember_github_repository_authorization(user, redirect_to, scope, manage=True)
            return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

        if github_repository_access_connected(existing_access):
            payload = {
                "ok": True,
                "connected": True,
                "mode": "github-app-existing",
                "installationId": clean_github_access_text(existing_access.get("installationId"), allow_int=True),
            }
            return self.json(payload)
        existing_url = trusted_github_web_url(existing_access.get("installationHtmlUrl") if existing_access else None)
        if existing_access and existing_url:
            return self.json({
                "ok": True,
                "url": existing_url,
                "mode": "github-app-existing-pending",
                "installationId": existing_access.get("installationId"),
            })

        state = remember_github_repository_authorization(user, redirect_to, scope)
        return self.json({"url": github_auth.build_app_install_url(state), "mode": "github-app"})

    def handle_github_repository_callback(self, params: dict) -> None:
        if not github_auth.app_install_configured():
            if not local_github_mocks_enabled():
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "GitHub App installation is not configured.")
            session = self.current_or_demo_session()
            scope = params.get("scope") or "all"
            repository_items = REPOSITORIES if scope == "all" else REPOSITORIES[:1]
            USERS[session["userId"]]["githubRepositoryAccess"] = {
                "mode": "local",
                "scope": scope,
                "authorizedAt": now(),
                "installationId": "dev_installation_1",
                "repositories": [repo["fullName"] for repo in repository_items],
                "repositoryItems": repository_items,
                "repositoriesNeedSync": True,
            }
            mark_state_dirty()
            return self.redirect(safe_redirect_to(params.get("redirectTo"), "repos"), cookie_header(session["id"]))

        record = self.github_install_record_from_callback(params)
        user = USERS.get(str(record["userId"]))
        if not user:
            raise ValueError("The GitHub installation belongs to a user session that no longer exists.")
        if not has_github_repository_authorization_identity(user):
            raise ValueError("Sign in with GitHub before authorizing repositories.")
        state = params.get("state") or None
        if params.get("setup_action") == "request":
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "github_app_installation_not_completed"})
            )
        if not params.get("installation_id"):
            clear_github_repository_authorization_pending(user, state)
            return self.redirect(
                redirect_with_params(str(record["redirectTo"]), {"github_error": "missing_installation_id"})
            )

        installation_id = str(params["installation_id"])
        selected_identity = github_identity_by_id(
            user,
            clean_github_access_text(record.get("selectedGithubIdentityId")),
        )
        selected_token = selected_identity.get("accessToken") if selected_identity else user.get("githubAccessToken")
        installations = (
            [
                installation
                for installation in github_auth.list_current_app_installations_for_user(selected_token)
                if installation_allowed_for_identity(selected_identity, installation)
            ]
            if selected_identity
            else current_user_github_app_installations(user)
        )
        target_installation = next(
            (
                installation
                for installation in installations
                if str(installation.get("id") or "") == installation_id
            ),
            None,
        )
        if not target_installation:
            if selected_identity:
                upsert_github_identity_installation_access(
                    user,
                    selected_identity,
                    installation_id,
                    can_access=False,
                    last_error_code="github_installation_not_visible",
                )
            raise ValueError("Unable to verify this GitHub App installation belongs to the signed-in GitHub user.")

        requested_scope = params.get("scope") or record.get("requestedScope") or "selected"
        if selected_identity:
            bind_github_repository_installation_for_identity(
                user,
                target_installation,
                selected_token,
                requested_scope,
            )
            identity = selected_identity
        else:
            bind_github_repository_installations(
                user,
                installations,
                requested_scope,
            )
            identity = upsert_github_identity(
                user,
                {
                    "id": user.get("githubId"),
                    "login": user.get("githubLogin"),
                    "html_url": user.get("githubHtmlUrl"),
                    "avatar_url": user.get("avatarUrl"),
                },
                {
                    "access_token": user.get("githubAccessToken"),
                    "scope": user.get("githubOAuthScope"),
                },
            )
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
            verification_method="setup_callback",
        )
        clear_github_repository_authorization_pending(user, state)
        session = create_session(user)
        return self.redirect(str(record["redirectTo"]), cookie_header(session["id"]))

    def github_install_record_from_callback(self, params: dict) -> dict:
        state = params.get("state") or ""
        if not state:
            raise ValueError("GitHub authorization state is invalid or expired.")
        return pop_github_state("install", state)

    def integrations_payload(self) -> dict:
        session = self.current_session()
        user = USERS.get(session["userId"]) if session else None
        github_access = user.get("githubRepositoryAccess") if user else None
        pending = bool(github_repository_authorization_pending(user))
        visible_access = None if pending or not github_repository_access_authorized_for_user(user, github_access) else github_access
        github = {
            "provider": "github",
            "connected": github_repository_access_authorized_for_user(user, github_access)
            and github_repository_access_connected(github_access)
            and not pending,
            "authorizationPending": pending,
            "mode": clean_github_access_text(visible_access.get("mode")) if visible_access else None,
            "scope": clean_github_access_text(visible_access.get("scope")) if visible_access else None,
            "repositorySelection": clean_github_access_text(visible_access.get("repositorySelection")) if visible_access else None,
            "installationId": clean_github_access_text(visible_access.get("installationId"), allow_int=True) if visible_access else None,
            "installationIds": clean_github_access_text_list(visible_access.get("installationIds"), allow_int=True) if visible_access else [],
            "installationAccount": clean_github_access_text(visible_access.get("installationAccount")) if visible_access else None,
            "installationAccounts": clean_github_access_text_list(visible_access.get("installationAccounts")) if visible_access else [],
            "installationHtmlUrl": None,
            "identities": public_github_identities(user),
            "installations": public_installation_summaries(user, visible_access),
            "repositories": clean_github_access_text_list(visible_access.get("repositories")) if visible_access else [],
            "repositoriesNeedSync": github_repositories_need_sync(visible_access),
        }
        items = [github]
        return {"items": items, "github": github}

    def handle_repository_branches(self, repo_id: str) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing repository branches.")

        with STATE_LOCK:
            user = USERS.get(session["userId"])
            if not user:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing repository branches.")
            github_access = user.get("githubRepositoryAccess")
            if not github_access:
                return self.error(HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before viewing branches.")
            if github_repository_authorization_pending(user):
                return self.error(HTTPStatus.FORBIDDEN, "Complete GitHub repository authorization before viewing branches.")
            if not github_repository_access_authorized_for_user(user, github_access):
                return self.error(HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before viewing branches.")
            if github_repositories_need_sync(github_access):
                return self.json(
                    {
                        "message": "Sync GitHub repositories before viewing branches.",
                        "code": "REPOSITORY_SYNC_REQUIRED",
                    },
                    HTTPStatus.FORBIDDEN,
                )
            repo_meta = repository_item_by_repo_id(github_access, repo_id)
            if not repo_meta:
                full_name = clean_repository_full_name(repo_id)
                repo_meta = repository_item(github_access, full_name) if full_name else None
            if not repo_meta:
                return self.json(
                    {
                        "message": "Repository is not authorized for this GitHub App installation.",
                        "code": "REPOSITORY_NOT_AUTHORIZED",
                    },
                    HTTPStatus.FORBIDDEN,
                )
            github_access_snapshot = dict(github_access)
            repo_meta_snapshot = dict(repo_meta)

        try:
            return self.json(repository_branch_payload(github_access_snapshot, repo_meta_snapshot))
        except github_auth.GitHubError as exc:
            return self.error(HTTPStatus.BAD_GATEWAY, str(exc))

    def handle_scan_preflight(self, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting a scan.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        requests = body.get("repositories")
        if requests is None and (body.get("repo") or body.get("repoId")):
            requests = [body]
        if not isinstance(requests, list) or not requests:
            return self.error(HTTPStatus.BAD_REQUEST, "At least one repository is required to check scan quota.")
        if len(requests) > 100:
            return self.error(HTTPStatus.BAD_REQUEST, "At most 100 repositories can be checked at once.")

        with STATE_LOCK:
            user = USERS.get(session["userId"])
            if not user:
                return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before starting a scan.")
            github_access = user.get("githubRepositoryAccess")
            if not github_access:
                return self.error(HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
            if github_repository_authorization_pending(user):
                return self.error(HTTPStatus.FORBIDDEN, "Complete GitHub repository authorization before starting a scan.")
            if not github_repository_access_authorized_for_user(user, github_access):
                return self.error(HTTPStatus.FORBIDDEN, "Authorize GitHub repositories before starting a scan.")
            if github_repositories_need_sync(github_access):
                return self.json(
                    {
                        "message": "Sync GitHub repositories before starting a scan.",
                        "code": "REPOSITORY_SYNC_REQUIRED",
                    },
                    HTTPStatus.FORBIDDEN,
                )

            user_quota = quota.quota_payload_for_user(user)
            user_remaining = non_negative_int(user_quota.get("remaining"))
            repository_capacity_used: dict[str, int] = {}
            rows = []
            repo_available_count = 0
            for index, request in enumerate(requests):
                if not isinstance(request, dict):
                    return self.error(HTTPStatus.BAD_REQUEST, "Each repository request must be a JSON object.")
                requested_repo_id = clean_github_access_text(request.get("repoId"), allow_int=True)
                requested_repository = clean_repository_full_name(request.get("repo"))
                if not requested_repo_id and not requested_repository:
                    return self.error(HTTPStatus.BAD_REQUEST, "Each repository request requires repo or repoId.")
                repo_meta, request_key = repository_item_for_scan_request(github_access, request)
                if not repo_meta:
                    return self.json(
                        {
                            "message": "Repository is not authorized for this GitHub App installation.",
                            "code": "REPOSITORY_NOT_AUTHORIZED",
                        },
                        HTTPStatus.FORBIDDEN,
                    )
                repository = clean_repository_full_name(repo_meta.get("fullName"), requested_repository)
                if not repository or (request_key != "repoId" and not repository_is_authorized(github_access, repository)):
                    return self.json(
                        {
                            "message": "Repository is not authorized for this GitHub App installation.",
                            "code": "REPOSITORY_NOT_AUTHORIZED",
                        },
                        HTTPStatus.FORBIDDEN,
                    )
                try:
                    scan_user, repository_record = scan_resource_context(user, github_access, repo_meta)
                except ValueError as exc:
                    code = str(exc)
                    if code == "REPOSITORY_SYNC_REQUIRED":
                        return self.json(
                            {
                                "message": (
                                    "Sync GitHub repositories before starting a scan so Pullwise can verify "
                                    "the stable repository ID."
                                ),
                                "code": "REPOSITORY_SYNC_REQUIRED",
                            },
                            HTTPStatus.CONFLICT,
                        )
                    return self.error(HTTPStatus.BAD_REQUEST, "Unable to resolve repository context.")

                repository_quota = quota.quota_payload_for_repository(repository_record, scan_user)
                repository_remaining = non_negative_int(repository_quota.get("remaining"))
                capacity_key = clean_github_access_text(repository_quota.get("bucketId"), allow_int=True) or str(
                    repository_record["id"]
                )
                capacity_used = repository_capacity_used.get(capacity_key, 0)
                available = repository_remaining > capacity_used
                if available:
                    repository_capacity_used[capacity_key] = capacity_used + 1
                    repo_available_count += 1
                rows.append(
                    {
                        "index": index,
                        "repo": repository,
                        "branch": (
                            clean_github_access_text(request.get("branch"))
                            or clean_github_access_text(repo_meta.get("defaultBranch"))
                            or "main"
                        ),
                        "repoId": repository_record["id"],
                        "githubRepoId": repository_record["github_repo_id"],
                        "requestId": scan_request_id_from_body(request),
                        "available": available,
                        "reason": "ok" if available else "repository_quota_exceeded",
                        "repoQuota": repository_quota,
                    }
                )

        allowed_count = min(user_remaining, repo_available_count)
        return self.json(
            {
                "requestedCount": len(requests),
                "allowedCount": allowed_count,
                "userQuota": user_quota,
                "repositories": rows,
            }
        )

    def repositories_payload(self, refresh: bool = False) -> dict:
        session = self.current_session()
        if not session:
            return {"items": [], "repositories": [], "needsAuthorization": True}

        user = USERS.get(session["userId"])
        user_quota = quota.quota_payload_for_user(user) if user else None
        github_access = user.get("githubRepositoryAccess") if user else None
        bound_existing_access = False
        pending = bool(github_repository_authorization_pending(user))
        if pending:
            if not refresh:
                payload = pending_repositories_payload()
                payload["userQuota"] = user_quota
                return payload
            github_access = (
                bind_pending_selected_github_identity_access(user)
                or try_bind_existing_github_repository_access(user, force_refresh=True)
            )
            if github_repository_access_connected(github_access):
                clear_github_repository_authorization_pending(user)
                pending = False
                bound_existing_access = True
            else:
                payload = pending_repositories_payload()
                payload["userQuota"] = user_quota
                return payload

        if github_access and not github_repository_access_authorized_for_user(user, github_access):
            github_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            bound_existing_access = bool(github_access)

        if not github_access:
            github_access = try_bind_existing_github_repository_access(user)
            bound_existing_access = bool(github_access)
        if not github_access:
            return {"items": [], "repositories": [], "needsAuthorization": True, "userQuota": user_quota}

        if refresh and not bound_existing_access and github_access.get("mode") == "github-app":
            refreshed_access = try_bind_existing_github_repository_access(user, force_refresh=True)
            if refreshed_access:
                github_access = refreshed_access
                bound_existing_access = True

        repository_items = repository_items_for_response(user, github_access)
        if not github_repository_access_connected(github_access):
            payload = unavailable_repositories_payload(github_access)
            payload["userQuota"] = user_quota
            return payload

        return {
            "items": repository_items,
            "repositories": repository_items,
            "userQuota": user_quota,
            "needsAuthorization": False,
            "installationId": clean_github_access_text(github_access.get("installationId"), allow_int=True),
            "installationIds": clean_github_access_text_list(github_access.get("installationIds"), allow_int=True),
            "repositorySelection": clean_github_access_text(github_access.get("repositorySelection")),
            "installationAccount": clean_github_access_text(github_access.get("installationAccount")),
            "installationAccounts": clean_github_access_text_list(github_access.get("installationAccounts")),
            "installations": public_installation_summaries(user, github_access),
            "repositoriesNeedSync": github_repositories_need_sync(github_access),
        }

    def repositories_connected(self) -> bool:
        session = self.current_session()
        if not session:
            return False
        return github_repositories_connected_for_user(USERS.get(session["userId"]))

    def current_or_demo_session(self) -> dict:
        session = self.current_session()
        if session:
            return session
        user = get_or_create_github_user()
        return create_session(user)

    def current_session(self) -> dict | None:
        for session_id in self.current_session_id_candidates():
            session = self.current_session_for_id(session_id)
            if session:
                return session
        return None

    def current_session_for_id(self, session_id: str) -> dict | None:
        session = SESSIONS.get(session_id)
        if not session:
            return None
        if not isinstance(session, dict):
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        expires_at = pull_request_timestamp(session.get("expiresAt"))
        user_id = session.get("userId")
        if expires_at is None or not isinstance(user_id, str) or not user_id:
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        if expires_at < now():
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        user = USERS.get(user_id)
        if not user:
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        if github_auth.oauth_configured() and user and "github" in user.get("providers", []) and not user.get("githubAccessToken"):
            SESSIONS.pop(session_id, None)
            mark_state_dirty()
            return None
        return session

    def current_session_id(self) -> str | None:
        candidates = self.current_session_id_candidates()
        for session_id in candidates:
            if self.current_session_for_id(session_id):
                return session_id
        return candidates[0] if candidates else None

    def current_session_id_candidates(self) -> list[str]:
        authorization_token = bearer_token(self)
        if authorization_token and not authorization_token.startswith(API_KEY_PREFIX):
            return [authorization_token]
        raw_cookie = request_header(self, "Cookie") or ""
        session_ids: list[str] = []
        for item in raw_cookie.split(";"):
            name, separator, value = item.partition("=")
            if separator and name.strip() == SESSION_COOKIE:
                session_id = value.strip().strip('"')
                if session_id:
                    session_ids.append(session_id)
        return session_ids

    def current_api_key_context(self) -> dict | None:
        cached = getattr(self, "_api_key_context", None)
        if cached is not None:
            return cached
        token = api_key_token(self)
        if not token:
            self._api_key_context = None
            return None
        token_hash = api_key_hash(token)
        record = db.get_api_key_by_hash(token_hash)
        if not record:
            self._api_key_context = None
            return None
        user = USERS.get(str(record.get("user_id") or ""))
        if not user:
            self._api_key_context = None
            return None
        db.mark_api_key_used(record["id"])
        context = {"apiKey": record, "user": user, "scopes": parse_api_key_scopes(record.get("scopes"))}
        self._api_key_context = context
        return context

    def require_api_key_context(self, scope: str) -> dict | None:
        context = self.current_api_key_context()
        if not context:
            self.error(HTTPStatus.UNAUTHORIZED, "A valid Pullwise API key is required.")
            return None
        if scope not in context.get("scopes", []):
            self.error(HTTPStatus.FORBIDDEN, f"API key scope {scope} is required.")
            return None
        return context

    def api_repository_context(self, context: dict, repo_id: str) -> tuple[dict, dict] | None:
        repo_id = clean_github_access_text(repo_id, allow_int=True) or ""
        if not repo_id:
            self.error(HTTPStatus.BAD_REQUEST, "repoId is required.")
            return None
        user = context.get("user") if isinstance(context.get("user"), dict) else None
        github_access = user.get("githubRepositoryAccess") if user else None
        if api_repository_access_denial_for_user(user, github_access):
            self.error(HTTPStatus.NOT_FOUND, "Repository is not authorized for this account.")
            return None
        if user and isinstance(github_access, dict):
            sync_repository_access_for_user(user, github_access)
        repository = db.get_repository(repo_id)
        if not repository or not api_repository_authorized_for_user(user, repository):
            self.error(HTTPStatus.NOT_FOUND, "Repository is not authorized for this account.")
            return None
        repository_item_meta = (
            repository_item_by_repo_id(github_access, repo_id)
            or repository_item_by_repo_id(github_access, str(repository.get("id") or ""))
            or repository_item_by_repo_id(github_access, str(repository.get("github_repo_id") or ""))
            or repository_item(github_access, str(repository.get("full_name") or ""))
            or {}
        )
        return repository, repository_item_meta

    def handle_api_keys_get(self, params: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before viewing API keys.")
        user = USERS[session["userId"]]
        keys = [api_key_public_payload(item) for item in db.list_api_keys_for_user(user["id"])]
        return self.json({"items": keys, "apiKeys": keys})

    def handle_api_keys_post(self, body: dict) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before creating API keys.")
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        user = USERS[session["userId"]]
        scopes, scopes_error = requested_api_key_scopes(body.get("scopes"), provided="scopes" in body)
        if scopes_error:
            return self.error(HTTPStatus.BAD_REQUEST, scopes_error)
        token = API_KEY_PREFIX + secrets.token_urlsafe(32)
        record = db.create_api_key(
            {
                "id": make_id("ak"),
                "user_id": user["id"],
                "name": public_issue_text(body.get("name")) or "API key",
                "key_prefix": api_key_prefix(token),
                "key_hash": api_key_hash(token),
                "scopes": scopes,
            }
        )
        return self.json(api_key_public_payload(record, token=token), HTTPStatus.CREATED)

    def handle_api_key_delete(self, key_id: str) -> None:
        session = self.current_session()
        if not session:
            return self.error(HTTPStatus.UNAUTHORIZED, "Sign in before revoking API keys.")
        if not db.revoke_api_key(key_id, session["userId"]):
            return self.error(HTTPStatus.NOT_FOUND, "API key not found.")
        return self.json({"ok": True, "id": public_issue_text(key_id), "revoked": True})

    def require_admin_session(self) -> dict | None:
        session = self.current_session()
        if not session:
            self.error(HTTPStatus.UNAUTHORIZED, "Sign in before using admin APIs.")
            return None
        user = USERS.get(session["userId"])
        if not user_is_admin(user):
            self.error(HTTPStatus.FORBIDDEN, "Admin access is required.")
            return None
        return session

    def audit_worker_action(
        self,
        session: dict,
        action: str,
        *,
        worker_id: str | None = None,
        changed_fields: dict | None = None,
        success: bool = True,
        error: str | None = None,
    ) -> None:
        db.record_worker_audit_event(
            {
                "actor_user_id": session.get("userId"),
                "action": action,
                "worker_id": worker_id,
                "changed_fields": changed_fields or {},
                "request_id": request_id_from_handler(self),
                "created_at": now(),
                "success": success,
                "error": clean_scan_error(error),
            }
        )

    def handle_admin_get(self, segments: list[str], params: dict) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if segments == ["admin", "status"]:
            return self.json(scan_system_status_payload(admin=True))
        if segments == ["admin", "server-metrics"]:
            storage_path = env("PULLWISE_SERVER_METRICS_STORAGE_PATH", "") or os.path.dirname(db.database_path()) or project_root()
            metrics = system_metrics.server_metrics_payload(storage_path=storage_path, timestamp=now())
            history = system_metrics.server_metrics_history(
                db.load_state_item(system_metrics.SERVER_METRICS_HISTORY_STATE_KEY),
                metrics,
            )
            db.save_state_item(system_metrics.SERVER_METRICS_HISTORY_STATE_KEY, history)
            metrics["history"] = history
            metrics["historyMeta"] = {
                "limit": system_metrics.SERVER_METRICS_HISTORY_LIMIT,
                "minIntervalSeconds": system_metrics.SERVER_METRICS_HISTORY_MIN_INTERVAL_SECONDS,
            }
            return self.json(metrics)
        if segments == ["admin", "users"]:
            return self.json(admin_users_payload(current_user_id=session["userId"]))
        if segments == ["admin", "system-config"]:
            return self.json(system_config.admin_payload())
        if segments == ["admin", "subscription-plans", "agent-configs"]:
            return self.json(billing.review_agent_configs_admin_payload())
        if segments == ["admin", "review-calibration"]:
            scope_key = review_calibration_scope_key_from_params(params)
            if not scope_key:
                return self.error(HTTPStatus.BAD_REQUEST, "scope_key or user/repo/branch parameters are required.")
            return self.json(review_calibration_admin_payload(scope_key))
        if segments == ["admin", "workers", "defaults"]:
            force_refresh = public_issue_text(params.get("refresh") or params.get("force")).lower() in {"1", "true", "yes", "on"}
            return self.json(worker_defaults_payload(force_refresh=force_refresh))
        if segments == ["admin", "workers"]:
            workers = [worker_public_payload(worker, admin=True) for worker in db.list_workers()]
            return self.json({"items": workers, "workers": workers})
        if len(segments) == 3 and segments[:2] == ["admin", "workers"]:
            worker = db.get_worker(segments[2], include_deleted=True)
            if not worker:
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            audit = db.list_worker_audit_events(segments[2], limit=50)
            task_activity = [
                worker_task_activity_payload(job)
                for job in db.list_worker_task_activity(segments[2], limit=50)
            ]
            return self.json(
                {
                    "worker": worker_public_payload(worker, admin=True, include_machine_metrics=True),
                    "auditEvents": audit,
                    "taskActivity": task_activity,
                }
            )
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_admin_post(self, segments: list[str], body: dict) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if segments == ["admin", "server", "restart"]:
            try:
                payload = start_server_restart_process()
            except FileNotFoundError:
                logger.exception("Admin server restart requested, but launcher.sh was not found.")
                return self.error(HTTPStatus.NOT_IMPLEMENTED, "Server restart launcher is not available.")
            except OSError:
                logger.exception("Failed to start Pullwise server restart process.")
                return self.error(HTTPStatus.INTERNAL_SERVER_ERROR, "Unable to start server restart.")
            logger.warning(
                "Admin requested Pullwise server restart user_id=%s pid=%s",
                session.get("userId"),
                payload.get("pid"),
            )
            return self.json(payload, HTTPStatus.ACCEPTED)
        if segments == ["admin", "review-calibration", "labels"]:
            try:
                payload = record_admin_manual_review_outcome(body, reviewer_id=session["userId"])
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            return self.json(payload, HTTPStatus.CREATED)
        if segments == ["admin", "workers", "releases"]:
            return self.handle_admin_worker_release(session, body)
        if segments == ["admin", "workers"]:
            return self.handle_admin_worker_create(session, body)
        if len(segments) == 4 and segments[:2] == ["admin", "workers"]:
            worker_id = clean_github_access_text(segments[2]) or ""
            action = segments[3]
            if action == "commands":
                return self.handle_admin_worker_command(session, worker_id, body)
            if action == "enable":
                worker = db.set_worker_enabled(worker_id, True)
                if not worker:
                    self.audit_worker_action(session, "enable_worker", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                self.audit_worker_action(session, "enable_worker", worker_id=worker_id, changed_fields={"enabled": True})
                return self.json({"worker": worker_public_payload(worker, admin=True)})
            if action == "disable":
                worker = db.set_worker_enabled(worker_id, False)
                if not worker:
                    self.audit_worker_action(session, "disable_worker", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                self.audit_worker_action(session, "disable_worker", worker_id=worker_id, changed_fields={"enabled": False})
                return self.json({"worker": worker_public_payload(worker, admin=True)})
            if action == "rotate-token":
                worker = db.rotate_worker_token(worker_id)
                if not worker:
                    self.audit_worker_action(session, "rotate_worker_token", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                self.audit_worker_action(session, "rotate_worker_token", worker_id=worker_id, changed_fields={"tokenHash": "rotated"})
                return self.json(worker_create_payload(worker))
            if action == "test":
                worker = db.get_worker(worker_id, include_deleted=True)
                if not worker:
                    self.audit_worker_action(session, "test_worker", worker_id=worker_id, success=False, error="Worker not found.")
                    return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
                result = worker_test_payload(worker)
                self.audit_worker_action(session, "test_worker", worker_id=worker_id, changed_fields={"result": result.get("ok")})
                return self.json({"worker": worker_public_payload(worker, admin=True), "result": result})
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_admin_worker_create(self, session: dict, body: dict) -> None:
        try:
            max_concurrent_jobs = worker_admin_capacity(body.get("max_concurrent_jobs"))
            provider_chain = worker_provider_chain(
                body.get("providerChain"),
                strict=("providerChain" in body),
            )
        except ValueError as exc:
            self.audit_worker_action(session, "create_worker", success=False, error=str(exc))
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        worker = db.create_worker(
            {
                "name": public_issue_text(body.get("name")) or "Worker",
                "provider": provider_chain[0],
                "provider_chain": provider_chain,
                "region": public_issue_text(body.get("region")),
                "version": public_issue_text(body.get("version")),
                "max_concurrent_jobs": max_concurrent_jobs,
                "max_concurrency_cap": system_config.worker_max_concurrency_cap(),
            }
        )
        worker["provider_chain"] = provider_chain
        self.audit_worker_action(
            session,
            "create_worker",
            worker_id=worker.get("worker_id"),
            changed_fields={
                "name": worker.get("name"),
                "provider": worker.get("provider"),
                "providerChain": provider_chain,
                "region": worker.get("region"),
            },
        )
        return self.json(worker_create_payload(worker), HTTPStatus.CREATED)

    def handle_admin_worker_release(self, session: dict, body: dict) -> None:
        raw_version = body.get("version")
        try:
            payload = dispatch_worker_release_workflow(raw_version)
        except WorkerReleaseConfigurationError as exc:
            self.audit_worker_action(session, "release_worker", success=False, error=str(exc))
            return self.error(HTTPStatus.NOT_IMPLEMENTED, str(exc))
        except ValueError as exc:
            self.audit_worker_action(session, "release_worker", success=False, error=str(exc))
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        except WorkerReleaseDispatchError as exc:
            self.audit_worker_action(session, "release_worker", success=False, error=str(exc))
            return self.error(HTTPStatus.BAD_GATEWAY, str(exc))
        self.audit_worker_action(
            session,
            "release_worker",
            changed_fields={
                "version": payload["version"],
                "tag": payload["tag"],
                "repository": payload["repository"],
                "workflow": payload["workflow"],
                "ref": payload["ref"],
            },
        )
        return self.json(payload, HTTPStatus.ACCEPTED)

    def handle_admin_worker_command(self, session: dict, worker_id: str, body: dict) -> None:
        command = public_issue_text(body.get("command")).lower()
        action_name = "delete_worker_service" if command == "uninstall" else "stop_worker_service"
        try:
            command = db.normalize_worker_lifecycle_command(command)
        except ValueError as exc:
            self.audit_worker_action(session, action_name, worker_id=worker_id, success=False, error=str(exc))
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        action_name = "delete_worker_service" if command == "uninstall" else "stop_worker_service"
        try:
            worker_command = db.create_worker_command(
                {
                    "worker_id": worker_id,
                    "command": command,
                    "requested_by_user_id": session.get("userId"),
                    "request_id": request_id_from_handler(self),
                    "created_at": now(),
                }
            )
        except ValueError as exc:
            self.audit_worker_action(session, action_name, worker_id=worker_id, success=False, error=str(exc))
            return self.error(HTTPStatus.CONFLICT, str(exc))
        if not worker_command:
            self.audit_worker_action(session, action_name, worker_id=worker_id, success=False, error="Worker not found.")
            return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
        self.audit_worker_action(
            session,
            action_name,
            worker_id=worker_id,
            changed_fields={
                "command": command,
                "commandId": worker_command.get("id"),
                **({"deleted": True} if command == "uninstall" else {}),
            },
        )
        worker = db.get_worker(worker_id, include_deleted=True) or {}
        return self.json(
            {
                "ok": True,
                "worker": worker_public_payload(worker, admin=True),
                "command": worker_command_payload(worker_command, admin=True),
            },
            HTTPStatus.ACCEPTED,
        )

    def handle_admin_patch(self, segments: list[str], body: dict) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if len(segments) == 3 and segments[:2] == ["admin", "workers"]:
            worker_id = clean_github_access_text(segments[2]) or ""
            changed = {
                key: body.get(key)
                for key in ("name", "provider", "region", "version", "max_concurrent_jobs")
                if key in body
            }
            if "providerChain" in body:
                try:
                    changed["provider_chain"] = worker_provider_chain(body.get("providerChain"), strict=True)
                    changed["provider"] = changed["provider_chain"][0]
                except ValueError as exc:
                    self.audit_worker_action(session, "update_worker", worker_id=worker_id, success=False, error=str(exc))
                    return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            if "max_concurrent_jobs" in changed:
                try:
                    changed["max_concurrent_jobs"] = worker_admin_capacity(changed["max_concurrent_jobs"])
                except ValueError as exc:
                    self.audit_worker_action(session, "update_worker", worker_id=worker_id, success=False, error=str(exc))
                    return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            worker = db.update_worker(
                worker_id,
                changed,
                max_concurrency_cap=system_config.worker_max_concurrency_cap(),
            )
            if not worker:
                self.audit_worker_action(session, "update_worker", worker_id=worker_id, success=False, error="Worker not found.")
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            self.audit_worker_action(session, "update_worker", worker_id=worker_id, changed_fields=changed)
            return self.json({"worker": worker_public_payload(worker, admin=True)})
        if len(segments) == 4 and segments[:3] == ["admin", "subscription-plans", "agent-configs"]:
            plan = clean_github_access_text(segments[3]) or ""
            try:
                agent_config = billing.update_review_agent_config(plan, body)
            except ValueError as exc:
                self.audit_worker_action(session, "update_plan_agent_config", success=False, error=str(exc))
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            self.audit_worker_action(
                session,
                "update_plan_agent_config",
                changed_fields={
                    "plan": agent_config["plan"],
                    "providerChain": agent_config["providerChain"],
                    "model": agent_config["codex"]["model"],
                    "reasoningEffort": agent_config["codex"]["reasoningEffort"],
                },
            )
            return self.json(
                {
                    "plan": {
                        "id": agent_config["plan"],
                        "name": {"free": "Free", "pro": "Pro", "max": "Max"}[agent_config["plan"]],
                        "reviewLimit": billing.review_limit(agent_config["plan"]),
                        "repositoryReviewLimit": billing.repository_review_limit(agent_config["plan"]),
                        "repositoryLimits": billing.repository_limits(agent_config["plan"]),
                        "agentConfig": agent_config,
                        "source": "database",
                    },
                    "agentConfig": agent_config,
                    "source": "database",
                }
            )
        if segments == ["admin", "system-config"]:
            try:
                payload = system_config.update(body)
            except ValueError as exc:
                self.audit_worker_action(session, "update_system_config", success=False, error=str(exc))
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
            self.audit_worker_action(
                session,
                "update_system_config",
                changed_fields={"fields": sorted(system_config.flatten_paths(body.get("settings") if isinstance(body.get("settings"), dict) else body))},
            )
            return self.json(payload)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_admin_delete(self, segments: list[str]) -> None:
        session = self.require_admin_session()
        if not session:
            return
        if len(segments) == 3 and segments[:2] == ["admin", "workers"]:
            worker_id = clean_github_access_text(segments[2]) or ""
            existing_worker = db.get_worker(worker_id)
            if not existing_worker:
                self.audit_worker_action(session, "delete_worker", worker_id=worker_id, success=False, error="Worker not found.")
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            worker = db.soft_delete_worker(worker_id)
            if not worker:
                self.audit_worker_action(session, "delete_worker", worker_id=worker_id, success=False, error="Worker not found.")
                return self.error(HTTPStatus.NOT_FOUND, "Worker not found.")
            self.audit_worker_action(
                session,
                "delete_worker",
                worker_id=worker_id,
                changed_fields={"deleted": True},
            )
            return self.json({"worker": worker_public_payload(worker, admin=True), "deleted": True})
        if len(segments) == 3 and segments[:2] == ["admin", "users"]:
            user_id = clean_github_access_text(segments[2]) or ""
            try:
                return self.json(delete_authorized_user(user_id, actor_user_id=session["userId"]))
            except ResourceNotFound:
                return self.error(HTTPStatus.NOT_FOUND, "User not found.")
            except ValueError as exc:
                return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def require_worker(self, *, allow_disabled: bool = False, include_deleted: bool = False) -> dict | None:
        record = worker_token_record(self, allow_disabled=allow_disabled, include_deleted=include_deleted)
        if not record:
            self.error(HTTPStatus.UNAUTHORIZED, "A valid worker token is required.")
            return None
        return record

    def handle_worker_post(self, segments: list[str], body: dict) -> None:
        allow_disabled = segments == ["worker", "heartbeat"] or (
            len(segments) == 4 and segments[:2] == ["worker", "commands"] and segments[3] == "status"
        )
        allow_deleted = allow_disabled
        worker_record = self.require_worker(allow_disabled=allow_disabled, include_deleted=allow_deleted)
        if not worker_record:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        if segments == ["worker", "heartbeat"]:
            return self.handle_worker_heartbeat(body, worker_record)
        if segments == ["worker", "agent-configs"]:
            return self.handle_worker_agent_configs(body, worker_record)
        if segments == ["worker", "jobs", "claim"]:
            return self.handle_worker_job_claim(body, worker_record)
        if len(segments) == 4 and segments[:2] == ["worker", "jobs"] and segments[3] == "progress":
            return self.handle_worker_job_progress(segments[2], body, worker_record)
        if len(segments) == 4 and segments[:2] == ["worker", "jobs"] and segments[3] == "result":
            return self.handle_worker_job_result(segments[2], body, worker_record)
        if len(segments) == 4 and segments[:2] == ["worker", "commands"] and segments[3] == "status":
            return self.handle_worker_command_status(segments[2], body, worker_record)
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def authenticated_worker_id_matches(self, worker_record: dict, worker_id: str) -> bool:
        authenticated_worker_id = public_issue_text(worker_record.get("worker_id"))
        return bool(authenticated_worker_id and worker_id and authenticated_worker_id == worker_id)

    def handle_worker_agent_configs(self, body: dict, worker_record: dict) -> None:
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        return self.json(billing.review_agent_configs_admin_payload())

    def handle_worker_heartbeat(self, body: dict, worker_record: dict) -> None:
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        reported_capacity = public_scan_count(body.get("max_concurrent_jobs")) or 1
        heartbeat_capacity = worker_heartbeat_capacity(body.get("max_concurrent_jobs"))
        last_error = clean_scan_error(body.get("last_error"))
        if reported_capacity > heartbeat_capacity:
            clamp_error = f"max_concurrent_jobs clamped to {heartbeat_capacity}"
            last_error = f"{last_error}; {clamp_error}" if last_error else clamp_error
        heartbeat_region = public_issue_text(body.get("region")) if "region" in body else ""
        heartbeat_timestamp = now()
        machine_metrics = system_metrics.sanitize_machine_metrics_payload(
            body.get("machine_metrics"),
            identity_key="worker",
            fallback_timestamp=heartbeat_timestamp,
        )
        machine_metrics_history = None
        if machine_metrics:
            machine_metrics_history = system_metrics.machine_metrics_history(
                decoded_worker_json_payload(worker_record.get("machine_metrics_history"), list),
                machine_metrics,
            )
        raw_active_job_ids = body.get("active_job_ids") or body.get("activeJobIds")
        active_job_ids = []
        if isinstance(raw_active_job_ids, list):
            for value in raw_active_job_ids[:100]:
                job_id = clean_github_access_text(value)
                if job_id and job_id not in active_job_ids:
                    active_job_ids.append(job_id)
        try:
            record = db.upsert_worker_heartbeat(
                {
                    "worker_id": worker_id,
                    "version": public_issue_text(body.get("version")),
                    "provider": public_issue_text(body.get("provider")) or "codex",
                    "provider_chain": body.get("providerChain") or body.get("provider_chain"),
                    "max_concurrent_jobs": heartbeat_capacity,
                    "running_jobs": public_scan_count(body.get("running_jobs")),
                    "free_slots": public_scan_count(body.get("free_slots")),
                    "hostname": public_issue_text(body.get("hostname")),
                    "region": heartbeat_region or None,
                    "last_error": last_error,
                    "doctor_status": public_issue_text(body.get("doctor_status")),
                    "codex_ready": 1 if body.get("codex_ready") is True else 0 if body.get("codex_ready") is False else None,
                    "ready_providers": body.get("readyProviders") if "readyProviders" in body else body.get("ready_providers"),
                    "systemd_active": 1 if body.get("systemd_active") is True else 0 if body.get("systemd_active") is False else None,
                    "doctor_checked_at": pull_request_timestamp(body.get("doctor_checked_at")),
                    "machine_metrics": machine_metrics,
                    "machine_metrics_history": machine_metrics_history,
                    "timestamp": heartbeat_timestamp,
                    "max_concurrency_cap": system_config.worker_max_concurrency_cap(),
                }
            )
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if active_job_ids:
            db.renew_worker_scan_job_leases(
                worker_id,
                active_job_ids,
                lease_seconds=system_config.scan_job_lease_seconds(),
                timestamp=heartbeat_timestamp,
            )
        command = db.get_next_worker_command(worker_id)
        return self.json(
            {
                "ok": True,
                "worker": {
                    "worker_id": record.get("worker_id"),
                    "status": record.get("status"),
                    "last_heartbeat_at": record.get("last_heartbeat_at"),
                },
                "command": worker_command_payload(command),
            }
        )

    def handle_worker_command_status(self, command_id: str, body: dict, worker_record: dict) -> None:
        command_id = clean_github_access_text(command_id) or ""
        if not command_id:
            return self.error(HTTPStatus.BAD_REQUEST, "command id is required.")
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not worker_id:
            return self.error(HTTPStatus.BAD_REQUEST, "worker_id is required.")
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        try:
            command = db.update_worker_command_status(
                {
                    "id": command_id,
                    "worker_id": worker_id,
                    "status": public_issue_text(body.get("status")),
                    "error": clean_scan_error(body.get("error")),
                    "timestamp": now(),
                }
            )
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if not command:
            return self.error(HTTPStatus.NOT_FOUND, "Worker command not found.")
        return self.json({"ok": True, "command": worker_command_payload(command)})

    def handle_worker_job_claim(self, body: dict, worker_record: dict) -> None:
        worker_id = clean_github_access_text(body.get("worker_id")) or ""
        if not worker_id:
            return self.error(HTTPStatus.BAD_REQUEST, "worker_id is required.")
        if not self.authenticated_worker_id_matches(worker_record, worker_id):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match worker_id.")
        allowed, worker_status = worker_can_claim(worker_record)
        if not allowed:
            return self.error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                f"Worker is not ready to claim jobs: {worker_status}.",
            )
        if "max_jobs" in body:
            max_jobs = public_scan_count(body.get("max_jobs"))
        elif "free_slots" in body:
            max_jobs = public_scan_count(body.get("free_slots"))
        else:
            max_jobs = 1
        if max_jobs <= 0:
            return self.json({"job": None, "jobs": []})
        max_jobs = min(
            max_jobs,
            worker_available_claim_slots(worker_record),
            system_config.worker_max_claim_jobs(),
        )
        if max_jobs <= 0:
            return self.json({"job": None, "jobs": []})
        ready_providers = worker_record_ready_providers(worker_record)
        if not ready_providers:
            return self.json({"job": None, "jobs": []})
        try:
            recovered_jobs = db.recover_expired_scan_jobs(
                now(),
                worker_heartbeat_timeout_seconds=system_config.worker_heartbeat_timeout_seconds(),
            )
            if recovered_jobs:
                with STATE_LOCK:
                    apply_recovered_scan_jobs_locked(recovered_jobs)
            jobs = db.claim_next_scan_jobs(
                worker_id,
                max_jobs=max_jobs,
                lease_seconds=system_config.scan_job_lease_seconds(),
                per_user_running_limit=max_scan_concurrency_per_user(),
                worker_heartbeat_timeout_seconds=system_config.worker_heartbeat_timeout_seconds(),
                ready_providers=ready_providers,
            )
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if not jobs:
            return self.json({"job": None, "jobs": []})
        try:
            payloads = [scan_job_payload(job, include_clone_token=True) for job in jobs]
        except github_auth.GitHubError as exc:
            for job in jobs:
                db.requeue_interrupted_scan_job(
                    str(job.get("scan_id") or ""),
                    reason="clone_token_unavailable",
                    timestamp=now(),
                )
            return self.error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        with STATE_LOCK:
            for job in jobs:
                scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
                if scan and scan.get("status") == "queued":
                    scan.update(
                        {
                            "status": "running",
                            "claimedAt": job.get("claimed_at"),
                            "claimedByWorkerId": worker_id,
                            "progress": 0,
                            "phase": "clone",
                            "jobId": job.get("job_id"),
                        }
                    )
                    mark_state_dirty()
                scan_logging.log_event(
                    "worker_job_claimed",
                    scanId=job.get("scan_id"),
                    repo=job.get("repo"),
                    repoId=job.get("repo_id"),
                    githubRepoId=job.get("github_repo_id"),
                    branch=job.get("branch"),
                    commit=job.get("commit"),
                    workerId=worker_id,
                    jobId=job.get("job_id"),
                    attempt=job.get("attempt"),
                )
        return self.json({"job": payloads[0], "jobs": payloads})

    def handle_worker_job_progress(self, job_id: str, body: dict, worker_record: dict) -> None:
        job_id = clean_github_access_text(job_id) or ""
        if not job_id:
            return self.error(HTTPStatus.BAD_REQUEST, "job_id is required.")
        current_job = db.get_scan_job(job_id)
        if not current_job:
            return self.error(HTTPStatus.NOT_FOUND, "Job not found.")
        if not self.authenticated_worker_id_matches(worker_record, public_issue_text(current_job.get("claimed_by_worker_id"))):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match claimed job.")
        if public_issue_text(current_job.get("status")) not in {"claimed", "running"}:
            return self.error(HTTPStatus.CONFLICT, "Job is no longer accepting progress updates.")
        job = db.update_scan_job_progress(
            job_id,
            {
                "phase": public_scan_phase(body.get("phase")),
                "progress": public_scan_progress(body.get("progress")),
                "message": public_issue_text(body.get("message")),
                "started_at": pull_request_timestamp(body.get("started_at")) or now(),
                "timeout_at": now() + system_config.scan_job_lease_seconds(),
                "logs_summary": public_issue_text(body.get("logs_summary")),
            },
        )
        if not job:
            return self.error(HTTPStatus.CONFLICT, "Job is no longer accepting progress updates.")
        audit_swarm = public_scan_audit_swarm(body.get("audit_swarm") or body.get("auditSwarm"))
        completion_audit = public_scan_completion_audit(body.get("completionAudit") or body.get("completion_audit"))
        job_trace = public_scan_job_trace(body.get("jobTrace") or body.get("job_trace"))
        raw_repository_graph = body.get("repositoryGraph")
        repository_graph = public_repository_graph(raw_repository_graph)
        semantic_graph = public_repository_semantic_graph(body.get("semanticGraph"))
        if not semantic_graph and isinstance(raw_repository_graph, dict):
            semantic_graph = public_repository_semantic_graph(raw_repository_graph.get("semanticGraph"))
        raw_impact_graph = body.get("impactGraph")
        if not raw_impact_graph and isinstance(raw_repository_graph, dict):
            raw_impact_graph = raw_repository_graph.get("impactGraph")
        impact_graph = public_impact_graph(raw_impact_graph, repository_graph=repository_graph)
        if repository_graph and impact_graph:
            repository_graph["impactGraph"] = impact_graph
        with STATE_LOCK:
            scan = next((item for item in SCANS if item.get("id") == job.get("scan_id")), None)
            if scan and scan.get("status") == "running":
                update = {
                    "phase": public_scan_phase(body.get("phase")),
                    "progress": public_scan_progress(body.get("progress")),
                    "startedAt": job.get("started_at"),
                    "updatedAt": now(),
                }
                if audit_swarm:
                    update["auditSwarm"] = audit_swarm
                if completion_audit:
                    update["completionAudit"] = completion_audit
                if job_trace:
                    update["jobTrace"] = job_trace
                if repository_graph:
                    update["repositoryGraph"] = repository_graph
                if semantic_graph:
                    update["semanticGraph"] = semantic_graph
                if impact_graph:
                    update["impactGraph"] = impact_graph
                scan.update(update)
                mark_state_dirty()
        return self.json({"ok": True, "job": scan_job_payload(job)})

    def handle_worker_job_result(self, job_id: str, body: dict, worker_record: dict) -> None:
        job_id = clean_github_access_text(job_id) or ""
        if not job_id:
            return self.error(HTTPStatus.BAD_REQUEST, "job_id is required.")
        job = db.get_scan_job(job_id)
        if not job:
            return self.error(HTTPStatus.NOT_FOUND, "Job not found.")
        if not self.authenticated_worker_id_matches(worker_record, public_issue_text(job.get("claimed_by_worker_id"))):
            return self.error(HTTPStatus.FORBIDDEN, "Worker token does not match claimed job.")
        try:
            result = apply_worker_job_result(job, body)
        except ValueError as exc:
            return self.error(HTTPStatus.BAD_REQUEST, str(exc))
        if result.get("conflict"):
            return self.json({"message": "Result checksum conflicts with an existing attempt result."}, HTTPStatus.CONFLICT)
        scan_logging.log_event(
            "worker_job_result",
            scanId=job.get("scan_id"),
            repo=job.get("repo"),
            repoId=job.get("repo_id"),
            githubRepoId=job.get("github_repo_id"),
            branch=job.get("branch"),
            commit=job.get("commit"),
            jobId=job.get("job_id"),
            status=body.get("status"),
            duplicate=result.get("duplicate"),
            issueCount=result.get("issueCount"),
        )
        return self.json({"ok": True, **result})

    def handle_external_api_get(self, segments: list[str], params: dict) -> None:
        if segments == ["repositories"]:
            context = self.require_api_key_context("repositories:read")
            if not context:
                return
            user = context["user"]
            github_access = user.get("githubRepositoryAccess")
            access_denial = api_repository_access_denial_for_user(user, github_access)
            if access_denial:
                code, message = access_denial
                return self.json({"message": message, "code": code}, HTTPStatus.FORBIDDEN)
            items = repository_items_for_response(user, github_access)
            return self.json(
                {
                    "items": items,
                    "repositories": items,
                    "userQuota": quota.quota_payload_for_user(user),
                    "apiKey": api_key_public_payload(context["apiKey"]),
                }
            )
        if len(segments) == 4 and segments[0] == "repositories" and segments[2] == "scans" and segments[3] == "current":
            context = self.require_api_key_context("scans:read")
            if not context:
                return
            repo_context = self.api_repository_context(context, segments[1])
            if not repo_context:
                return
            repository = repo_context[0]
            scan = latest_scan_for_user_repo(context["user"]["id"], repository["id"])
            return self.json(
                {
                    "repoId": repository["id"],
                    "scan": scan_payload(scan) if scan else None,
                    "status": public_scan_status(scan.get("status")) if scan else "idle",
                }
            )
        if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "quota":
            context = self.require_api_key_context("quota:read")
            if not context:
                return
            repo_context = self.api_repository_context(context, segments[1])
            if not repo_context:
                return
            user = context["user"]
            repository = repo_context[0]
            return self.json(
                {
                    "repoId": repository["id"],
                    "user": quota.quota_payload_for_user(user),
                    "repository": quota.quota_payload_for_repository(repository, user),
                }
            )
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_external_api_post(self, segments: list[str], body: dict) -> None:
        if len(segments) == 3 and segments[0] == "repositories" and segments[2] == "scans":
            return self.handle_external_api_scan_start(segments[1], body)
        if len(segments) == 4 and segments[0] == "repositories" and segments[2] == "scans" and segments[3] == "stop":
            return self.handle_external_api_scan_stop(segments[1])
        return self.error(HTTPStatus.NOT_FOUND, "Route not found")

    def handle_external_api_scan_start(self, repo_id: str, body: dict) -> None:
        context = self.require_api_key_context("scans:write")
        if not context:
            return
        if not isinstance(body, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        repo_context = self.api_repository_context(context, repo_id)
        if not repo_context:
            return
        repository = repo_context[0]
        request_id = scan_request_id_from_body(body)
        github_access = context["user"].get("githubRepositoryAccess") or {}
        repository_item_meta = repo_context[1] if isinstance(repo_context[1], dict) else {}
        with STATE_LOCK:
            existing = user_scan_by_request_id(context["user"]["id"], request_id)
            if existing and existing.get("repoId") == repository["id"]:
                return self.json(scan_payload(existing))
            if existing:
                return self.json(idempotency_key_reused_payload(existing), HTTPStatus.CONFLICT)
            commit, commit_error = scan_commit_from_body(body)
            if commit_error:
                return self.json({"message": commit_error, "code": "INVALID_COMMIT"}, HTTPStatus.BAD_REQUEST)
            requested_branch = github_auth.clean_branch_name(body.get("branch"))
            branch = (
                requested_branch
                or github_auth.clean_branch_name(repository_item_meta.get("defaultBranch"))
                or github_auth.clean_branch_name(repository.get("default_branch"))
                or "main"
            )
            if requested_branch:
                try:
                    branch_available = scan_branch_is_available(github_access, repository_item_meta, branch)
                except github_auth.GitHubError as exc:
                    return self.json({"message": str(exc), "code": "BRANCH_LOOKUP_FAILED"}, HTTPStatus.BAD_GATEWAY)
                if not branch_available:
                    return self.json(
                        {
                            "message": "Selected branch is not available for this repository.",
                            "code": "BRANCH_NOT_AVAILABLE",
                        },
                        HTTPStatus.BAD_REQUEST,
                    )
            limit_error = scan_queue_limit_error(context["user"]["id"])
            if limit_error:
                return self.json({"message": limit_error[1], "code": limit_error[2]}, limit_error[0])
            scan_id = make_id("sc")
            try:
                quota_result = quota.consume_scan_quota(
                    user=context["user"],
                    repository=repository,
                    requested_by_user_id=context["user"]["id"],
                    scan_id=scan_id,
                    request_id=request_id or None,
                )
            except quota.QuotaExceeded as exc:
                payload = {"message": exc.message, "code": exc.code}
                if exc.repo_id:
                    payload["repoId"] = exc.repo_id
                return self.json(payload, HTTPStatus.PAYMENT_REQUIRED)
            if quota_result.get("deduplicated"):
                existing = user_scan_by_request_id(context["user"]["id"], request_id)
                if existing and existing.get("repoId") == repository["id"]:
                    return self.json(scan_payload(existing))
                if existing:
                    return self.json(idempotency_key_reused_payload(existing), HTTPStatus.CONFLICT)
                return self.json({"message": IDEMPOTENCY_KEY_REUSED_MESSAGE, "code": "IDEMPOTENCY_KEY_REUSED"}, HTTPStatus.CONFLICT)

            review_output_language = settings_payload(context["user"]["id"])["review"]["outputLanguage"]
            scan = {
                "id": scan_id,
                "repo": repository["full_name"],
                "branch": branch,
                "commit": commit,
                "status": "queued",
                "userId": context["user"]["id"],
                "apiKeyId": context["apiKey"]["id"],
                "createdAt": now(),
                "queuedAt": now(),
                "progress": 0,
                "phase": None,
                "issues": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
                "reviewOutputLanguage": review_output_language,
                "installationId": clean_github_access_text(repository_item_meta.get("installationId"), allow_int=True)
                or clean_github_access_text(github_access.get("installationId"), allow_int=True),
                "installationAccount": clean_github_access_text(repository_item_meta.get("installationAccount"))
                or clean_github_access_text(github_access.get("installationAccount")),
                "repositorySelection": clean_github_access_text(repository_item_meta.get("repositorySelection"))
                or clean_github_access_text(github_access.get("repositorySelection")),
                "repoId": repository["id"],
                "githubRepoId": repository["github_repo_id"],
                "quotaBucketIds": quota_result["bucketIds"],
                "cloneUrl": trusted_github_web_url(repository_item_meta.get("cloneUrl")) or repository.get("clone_url"),
                "repositoryPrivate": bool(repository.get("private")),
                "repoPath": None,
                "billingUsage": quota_result["user"],
                "repoUsage": quota_result["repository"],
                "by": "api key",
            }
            if request_id:
                scan["requestId"] = request_id
            SCANS.insert(0, scan)
            mark_state_dirty()
            try:
                create_scan_job_for_scan(scan)
            except Exception:
                SCANS[:] = [item for item in SCANS if item.get("id") != scan_id]
                mark_state_dirty()
                quota.rollback_scan_quota(
                    scan_id=scan_id,
                    requested_by_user_id=context["user"]["id"],
                    request_id=request_id or None,
                )
                raise
        scan_logging.log_event(
            "scan_queued",
            scanId=scan["id"],
            userId=scan.get("userId"),
            repo=scan.get("repo"),
            branch=scan.get("branch"),
            commit=scan.get("commit"),
            provider="worker",
            requestId=scan.get("requestId"),
            installationId=scan.get("installationId"),
            repoId=scan.get("repoId"),
            githubRepoId=scan.get("githubRepoId"),
            quotaBucketIds=scan.get("quotaBucketIds"),
            apiKeyId=scan.get("apiKeyId"),
        )
        return self.json(scan_payload(scan), HTTPStatus.CREATED)

    def handle_external_api_scan_stop(self, repo_id: str) -> None:
        context = self.require_api_key_context("scans:write")
        if not context:
            return
        repo_context = self.api_repository_context(context, repo_id)
        if not repo_context:
            return
        with STATE_LOCK:
            repository = repo_context[0]
            scan = active_scan_for_user_repo(context["user"]["id"], repository["id"])
            if not scan:
                return self.error(HTTPStatus.NOT_FOUND, "No queued or running scan exists for this repository.")
            scan["status"] = "cancelled"
            scan["completedAt"] = now()
            mark_state_dirty()
            db.cancel_scan_job_for_scan(str(scan.get("id") or ""))
        return self.json(scan_payload(scan))

    def clear_current_session(self) -> None:
        session_id = self.current_session_id()
        if session_id and SESSIONS.pop(session_id, None):
            mark_state_dirty()

    def find_or_404(self, collection: list[dict], item_id: str, label: str) -> dict:
        for item in collection:
            if item.get("id") == item_id:
                return item
        raise ResourceNotFound(label)

    def read_json(self) -> dict:
        return decode_json_body(self.read_raw_body())

    def read_raw_body(self) -> bytes:
        length = self.request_content_length()
        if length == 0:
            return b""
        if length > max_body_bytes():
            raise RequestBodyTooLarge("Request body is too large.")
        return self.rfile.read(length)

    def enforce_body_size_limit(self, method: str) -> None:
        if method not in {"POST", "PATCH"}:
            return
        length = self.request_content_length()
        if length > max_body_bytes():
            raise RequestBodyTooLarge("Request body is too large.")

    def request_content_length(self) -> int:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return 0
        raw_text = str(raw_length).strip()
        if not raw_text:
            return 0
        if not raw_text.isdigit():
            raise ValueError("Invalid Content-Length header.")
        return int(raw_text)

    def handle_creem_webhook(self) -> None:
        raw = self.read_raw_body()
        if not billing.verify_creem_webhook(raw, self.headers.get("creem-signature")):
            logger.warning("Rejected Creem webhook with invalid signature.")
            return self.error(HTTPStatus.BAD_REQUEST, "Invalid Creem webhook signature.")
        event = decode_json_body(raw)
        if not isinstance(event, dict):
            return self.error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
        update = billing.billing_update_from_creem_event(event)
        if update:
            result = self.apply_billing_update(update)
            logger.info(
                "Processed Creem webhook eventType=%s eventId=%s result=%s",
                update.get("eventType"),
                update.get("eventId"),
                result,
            )
        else:
            logger.info(
                "Ignored Creem webhook eventType=%s eventId=%s result=unsupported_or_unmapped",
                event.get("eventType") or event.get("type"),
                event.get("id") or event.get("eventId"),
            )
        return self.json({"received": True})

    def apply_billing_update(self, update: dict) -> str:
        with STATE_LOCK:
            if billing_event_processed(update):
                return "duplicate"
            user = billing_user_for_update(update)
            if user:
                applied = apply_billing_update_to_user(user, update)
                apply_pending_billing_updates_for_user(user)
                return "applied" if applied else "stale"
            pending_count = len(BILLING_PENDING_UPDATES)
            remember_pending_billing_update(update)
            if len(BILLING_PENDING_UPDATES) > pending_count:
                return "pending"
            return "unmatched"

    def send_cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        allowed = trusted_browser_origins()
        if origin and origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Vary", "Origin")

    def json(self, payload: dict, status: int = HTTPStatus.OK, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            response_headers = {**getattr(self, "_rate_limit_headers", {}), **(headers or {})}
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def text(self, payload: str, status: int = HTTPStatus.OK, *, content_type: str = "text/plain; charset=utf-8") -> None:
        body = payload.encode("utf-8")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for key, value in getattr(self, "_rate_limit_headers", {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def binary(
        self,
        payload: bytes,
        status: int = HTTPStatus.OK,
        *,
        content_type: str = "application/octet-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        body = payload if isinstance(payload, bytes) else bytes(payload or b"")
        try:
            self.send_response(status)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            response_headers = {**getattr(self, "_rate_limit_headers", {}), **(headers or {})}
            for key, value in response_headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def redirect(self, location: str, set_cookie: str | None = None) -> None:
        try:
            self.send_response(HTTPStatus.FOUND)
            self.send_cors_headers()
            self.send_header("Location", location)
            if set_cookie:
                self.send_header("Set-Cookie", set_cookie)
            self.end_headers()
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def serve_static_file(self, file_path: str) -> None:
        """Serve a static file from disk with appropriate headers."""
        try:
            stat = os.stat(file_path)
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self.error(HTTPStatus.NOT_FOUND, "File not found")
        content_type, _ = mimetypes.guess_type(file_path)
        content_type = content_type or "application/octet-stream"
        try:
            with open(file_path, "rb") as f:
                body = f.read()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return self.error(HTTPStatus.NOT_FOUND, "File not found")
        try:
            self.send_response(HTTPStatus.OK)
            self.send_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(stat.st_size))
            # Cache static assets for 1 year (hashed filenames), don't cache index.html
            if os.path.basename(file_path) == "index.html":
                self.send_header("Cache-Control", "no-cache")
            elif "/assets/" in file_path.replace("\\", "/"):
                self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.end_headers()
            self.wfile.write(body)
        except _CLIENT_DISCONNECT_EXCEPTIONS as exc:
            raise ClientDisconnected("Client disconnected before the response was sent.") from exc

    def serve_spa(self) -> None:
        """Serve the SPA index.html for client-side routing."""
        root = web_root()
        index = os.path.join(root, "index.html")
        if os.path.isfile(index):
            self.serve_static_file(index)
        else:
            self.error(HTTPStatus.NOT_FOUND, "Frontend not built. Run 'npm run build' in pullwise-web.")

    def error(self, status: int, message: str) -> None:
        self.json({"message": message}, status)


def main() -> None:
    load_env_file()
    logging_config.configure_logging(project_root=project_root())
    parser = argparse.ArgumentParser(description="Run the Pullwise local API server.")
    parser.add_argument("--host", default=env("PULLWISE_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=parse_port, default=server_port())
    args = parser.parse_args()

    ensure_state_loaded()
    recovered_scans = recover_interrupted_scans()
    if recovered_scans:
        logger.info("Recovered %s interrupted scan(s).", recovered_scans)
    cleanup_server_resources_if_due(force=True)
    persist_state()
    httpd = ThreadingHTTPServer((args.host, args.port), PullwiseHandler)
    logger.info("Pullwise API listening on http://%s:%s", args.host, args.port)
    logger.info("Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
