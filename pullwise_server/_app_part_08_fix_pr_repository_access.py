from __future__ import annotations

# Loaded by app.py; keep definitions in that module's globals for compatibility.

@contextmanager
def preview_scan_lock(scan_id: str) -> Iterator[None]:
    with PREVIEW_SCAN_LOCKS_GUARD:
        entry = PREVIEW_SCAN_LOCKS.get(scan_id)
        if entry is None:
            entry = PreviewScanLockEntry()
            PREVIEW_SCAN_LOCKS[scan_id] = entry
        entry.refs += 1

    entry.lock.acquire()
    try:
        yield
    finally:
        entry.lock.release()
        with PREVIEW_SCAN_LOCKS_GUARD:
            entry.refs -= 1
            if entry.refs == 0 and PREVIEW_SCAN_LOCKS.get(scan_id) is entry:
                PREVIEW_SCAN_LOCKS.pop(scan_id, None)


def preview_issue_fix_for_user(user: dict, issue: dict) -> dict:
    scan_id = issue.get("scanId")
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        raise ValueError("Scan not found for issue.")
    user_id = str(user.get("id") or "")
    scan_id = str(scan.get("id") or scan_id or "")
    if str(scan.get("userId") or "") != user_id:
        raise ValueError("Scan does not belong to the signed-in user.")
    if scan.get("status") != "done":
        raise ValueError("Scan must be completed before previewing fixes.")

    with preview_scan_lock(scan_id):
        repo_path = scan.get("repoPath")
        if repo_path:
            repo_path = str(repo_path)
            if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id):
                raise ValueError("Scan checkout path is outside the scan workspace.")
            if os.path.exists(repo_path):
                return fix_workflow.preview_issue_fix(repo_path, issue)

        try:
            repo_path = checkout.prepare_checkout(scan_id, scan, lambda: False)
        except (RuntimeError, OSError, checkout.CheckoutCancelled) as exc:
            try:
                checkout.cleanup_scan_workspace(user_id, scan_id)
            except (RuntimeError, OSError) as cleanup_exc:
                raise ValueError(f"Unable to clean up failed preview checkout: {cleanup_exc}") from cleanup_exc
            raise ValueError(str(exc)) from exc

        try:
            repo_path = str(repo_path)
            if not checkout.path_in_scan_workspace(repo_path, user_id, scan_id):
                raise ValueError("Prepared checkout path is outside the scan workspace.")
            return fix_workflow.preview_issue_fix(repo_path, issue)
        finally:
            try:
                checkout.cleanup_scan_workspace(user_id, scan_id)
            except (RuntimeError, OSError) as exc:
                raise ValueError(f"Unable to clean up preview checkout: {exc}") from exc


def create_issue_pull_request(user: dict, issue: dict) -> dict:
    user_id = str(user.get("id") or "")
    if not user_id or str(issue.get("userId") or "") != user_id:
        raise ValueError("Issue does not belong to the signed-in user.")

    scan_id = str(issue.get("scanId") or "")
    scan = next((item for item in SCANS if item.get("id") == scan_id), None)
    if not scan:
        raise ValueError("Scan not found for issue.")
    if str(scan.get("userId") or "") != user_id:
        raise ValueError("Scan does not belong to the signed-in user.")

    issue_id = clean_pull_request_issue_id(issue.get("id"))
    issue_slug = issue_id
    pr_scan_id = f"pr_{issue_slug}"

    with preview_scan_lock(f"pull-request:{issue_slug}"):
        if github_repository_authorization_pending(user):
            raise ValueError("Complete GitHub repository authorization before creating a pull request.")
        if scan.get("status") != "done":
            raise ValueError("Scan must be completed before creating a pull request.")

        github_access = user.get("githubRepositoryAccess")
        if not github_repository_access_authorized_for_user(user, github_access):
            raise ValueError("Authorize GitHub repositories before creating a pull request.")
        if github_repositories_need_sync(github_access):
            raise ValueError("Sync GitHub repositories before creating a pull request.")
        existing = issue.get("pullRequest")
        pending = issue.get("pullRequestPending") if not isinstance(existing, dict) else None
        recovering_pending = isinstance(pending, dict) and pull_request_pending_is_stale(pending)
        if isinstance(pending, dict) and not recovering_pending:
            raise ValueError("Pull request creation is already in progress for this issue.")
        if not github_auth.app_api_configured():
            raise ValueError("GitHub App API is not configured for pull request creation.")
        repo = clean_repository_full_name(issue.get("repo"), scan.get("repo"))
        if not repo:
            raise ValueError("Repository must be a GitHub full name like owner/repo.")
        if not repository_is_authorized(github_access, repo):
            raise ValueError("Repository is not authorized for this GitHub App installation.")

        repo_meta = repository_item(github_access, repo) or {}
        installation_permissions = repo_meta.get("installationPermissions") or github_access.get("installationPermissions")
        if not isinstance(installation_permissions, dict) or not installation_supports_pull_request_creation({"permissions": installation_permissions}):
            raise ValueError(github_app_write_permissions_message())
        title = pull_request_title(issue, issue_id)

        if isinstance(existing, dict):
            safe_existing = safe_existing_pull_request(existing, issue_id=issue_id, fallback_title=title)
            if safe_existing != existing:
                store_issue_pull_request(issue, safe_existing)
                return safe_existing
            return existing

        base_branch = (
            clean_github_access_text(issue.get("branch"))
            or clean_github_access_text(scan.get("branch"))
            or clean_github_access_text(repo_meta.get("defaultBranch"))
            or clean_github_access_text(github_access.get("defaultBranch"))
            or "main"
        )
        installation_id = (
            clean_github_access_text(repo_meta.get("installationId"), allow_int=True)
            or clean_github_access_text(scan.get("installationId"), allow_int=True)
            or clean_github_access_text(github_access.get("installationId"), allow_int=True)
            or ""
        )
        if not installation_id:
            raise ValueError("Repository is missing a GitHub App installation id.")
        clone_url = trusted_github_web_url(repo_meta.get("cloneUrl"))
        if not clone_url:
            clone_url = trusted_github_web_url(scan.get("cloneUrl"))

        recovery_token = ""
        if recovering_pending:
            branch = valid_stored_pull_request_branch(pending.get("branch"))
            if not branch:
                clear_pull_request_pending(issue)
                raise ValueError("Stored pull request branch is invalid.")
            recovery_token = installation_token(installation_id)
            recovered = github_auth.find_pull_request_by_head(recovery_token, repo, head=branch)
            if recovered:
                pull_request = {
                    "issueId": issue_id,
                    "branch": branch,
                    "url": recovered.get("url"),
                    "number": recovered.get("number"),
                    "title": recovered.get("title") or title,
                }
                store_issue_pull_request(issue, pull_request)
                return pull_request

            if github_auth.branch_exists(recovery_token, repo, branch):
                body = (
                    f"Automated deterministic fix for Pullwise issue {issue_id}.\n\n"
                    f"Repository: {repo}\n"
                    "Recovered from an existing Pullwise fix branch."
                )
                try:
                    created = github_auth.create_pull_request(
                        recovery_token,
                        repo,
                        title=title,
                        head=branch,
                        base=base_branch,
                        body=body,
                    )
                except github_auth.GitHubError as exc:
                    record_pull_request_pending_failure(issue, str(exc))
                    raise
                pull_request = {
                    "issueId": issue_id,
                    "branch": branch,
                    "url": created.get("url"),
                    "number": created.get("number"),
                    "title": created.get("title") or title,
                }
                store_issue_pull_request(issue, pull_request)
                return pull_request

        if not recovering_pending:
            random_token = safe_git_ref_component(make_id("fix").split("_", 1)[-1], "branch")[:16]
            branch = f"pullwise/fix-{issue_slug}-{random_token}"
        store_pull_request_pending(issue, issue_id, branch)

        scan_payload = dict(scan)
        scan_payload.update({
            "id": pr_scan_id,
            "userId": user_id,
            "repo": repo,
            "branch": base_branch,
            "installationId": installation_id,
            "cloneUrl": clone_url,
        })

        checkout_started = False
        irreversible_started = False
        try:
            checkout_started = True
            repo_path = checkout.prepare_checkout(pr_scan_id, scan_payload, lambda: False)
            repo_path = str(repo_path)
            if not checkout.path_in_scan_workspace(repo_path, user_id, pr_scan_id):
                raise ValueError("Prepared checkout path is outside the pull request workspace.")

            preview = fix_workflow.apply_issue_fix(repo_path, issue)
            if not preview.get("valid"):
                raise ValueError(str(preview.get("message") or "Issue fix could not be applied."))
            fix_file = str(preview.get("file") or "")
            if not fix_file:
                raise ValueError("Issue fix did not report a file to commit.")

            token = recovery_token or installation_token(installation_id)

            body = (
                f"Automated deterministic fix for Pullwise issue {issue_id}.\n\n"
                f"Repository: {repo}\n"
                f"File: {fix_file}"
            )
            git_env = checkout.git_auth_env(token)
            git_env.update({
                "GIT_AUTHOR_NAME": "Pullwise",
                "GIT_AUTHOR_EMAIL": "pullwise@example.invalid",
                "GIT_COMMITTER_NAME": "Pullwise",
                "GIT_COMMITTER_EMAIL": "pullwise@example.invalid",
            })
            checkout.run_git(
                ["git", "checkout", "-B", branch],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="create fix branch",
            )
            checkout.run_git(
                ["git", "add", "--", fix_file],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="stage issue fix",
            )
            checkout.run_git(
                ["git", "commit", "-m", title],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="commit issue fix",
            )
            irreversible_started = True
            checkout.run_git(
                ["git", "push", "origin", f"HEAD:{branch}"],
                cwd=repo_path,
                extra_env=git_env,
                is_cancelled=lambda: False,
                action="push issue fix",
            )
            irreversible_started = True
            created = github_auth.create_pull_request(
                token,
                repo,
                title=title,
                head=branch,
                base=base_branch,
                body=body,
            )
            pull_request = {
                "issueId": issue_id,
                "branch": branch,
                "url": created.get("url"),
                "number": created.get("number"),
                "title": created.get("title") or title,
            }
            store_issue_pull_request(issue, pull_request)
            return pull_request
        except (RuntimeError, OSError, checkout.CheckoutCancelled) as exc:
            if irreversible_started:
                record_pull_request_pending_failure(issue, str(exc))
                raise github_auth.GitHubError(str(exc)) from exc
            clear_pull_request_pending(issue)
            if github_service_error(exc):
                raise github_auth.GitHubError(str(exc)) from exc
            raise ValueError(str(exc)) from exc
        except github_auth.GitHubError as exc:
            if irreversible_started:
                record_pull_request_pending_failure(issue, str(exc))
            else:
                clear_pull_request_pending(issue)
            raise
        except Exception:
            clear_pull_request_pending(issue)
            raise
        finally:
            if checkout_started:
                try:
                    checkout.cleanup_scan_workspace(user_id, pr_scan_id)
                except (RuntimeError, OSError) as exc:
                    logger.warning("Unable to clean up pull request checkout workspace %s: %s", pr_scan_id, exc)


def installation_token(installation_id: str) -> str:
    token_payload = github_auth.create_installation_access_token(installation_id)
    token = str(token_payload.get("token") or "")
    if not token:
        raise github_auth.GitHubError("GitHub did not return an installation access token.")
    return token


def repository_installation_id(github_access: dict | None, repo_meta: dict | None) -> str:
    if not repo_meta:
        return ""
    return (
        clean_github_access_text(repo_meta.get("installationId"), allow_int=True)
        or clean_github_access_text((github_access or {}).get("installationId"), allow_int=True)
        or ""
    )


def repository_branch_payload(github_access: dict | None, repo_meta: dict) -> dict:
    repository = clean_repository_full_name(repo_meta.get("fullName"))
    if not repository:
        raise ValueError("Repository is not authorized for this GitHub App installation.")
    installation_id = repository_installation_id(github_access, repo_meta)
    if not installation_id:
        raise ValueError("Repository is missing a GitHub App installation id.")

    token = installation_token(installation_id)
    branches = github_auth.list_repository_branches(token, repository)
    default_branch = github_auth.clean_branch_name(repo_meta.get("defaultBranch")) or "main"
    if default_branch and default_branch not in branches:
        branches = [default_branch, *branches]
    return {
        "repoId": (
            clean_github_access_text(repo_meta.get("repoId"), allow_int=True)
            or clean_github_access_text(repo_meta.get("githubRepoId"), allow_int=True)
            or clean_github_access_text(repo_meta.get("id"), allow_int=True)
            or ""
        ),
        "githubRepoId": clean_github_access_text(repo_meta.get("githubRepoId"), allow_int=True) or "",
        "repo": repository,
        "defaultBranch": default_branch,
        "branches": branches,
    }


def scan_branch_is_available(github_access: dict | None, repo_meta: dict, branch: str) -> bool:
    payload = repository_branch_payload(github_access, repo_meta)
    return branch in set(payload["branches"])


def pull_request_pending_is_stale(pending: dict) -> bool:
    try:
        started_at = int(pending.get("startedAt") or 0)
    except (TypeError, ValueError):
        started_at = 0
    return started_at <= now() - pull_request_pending_stale_seconds()


def pull_request_pending_stale_seconds() -> int:
    return max(60, env_int("PULLWISE_PR_PENDING_STALE_SECONDS", 15 * 60))


def valid_stored_pull_request_branch(branch: object) -> str | None:
    value = str(branch or "")
    if not value.startswith("pullwise/fix-"):
        return None
    if value.endswith("/") or value.endswith(".") or ".." in value or "//" in value or " " in value:
        return None
    if not re.match(r"^[A-Za-z0-9._/-]+$", value):
        return None
    parts = value.split("/")
    if any(not part or part.startswith(".") or part.casefold().endswith(".lock") for part in parts):
        return None
    return value


def store_pull_request_pending(issue: dict, issue_id: str, branch: str) -> None:
    with STATE_LOCK:
        issue["pullRequestPending"] = {
            "issueId": issue_id,
            "branch": branch,
            "startedAt": now(),
        }
        mark_state_dirty()
        persist_state()


def store_issue_pull_request(issue: dict, pull_request: dict) -> None:
    with STATE_LOCK:
        issue.pop("pullRequestPending", None)
        issue["pullRequest"] = pull_request
        mark_state_dirty()
        persist_state()


def safe_existing_pull_request(value: dict, *, issue_id: str, fallback_title: str) -> dict:
    number = value.get("number")
    return {
        "issueId": issue_id,
        "branch": valid_stored_pull_request_branch(value.get("branch")) or "",
        "url": trusted_github_web_url(value.get("url")),
        "number": number if isinstance(number, int) and not isinstance(number, bool) else None,
        "title": clean_pull_request_text(value.get("title")) or fallback_title,
    }


def safe_pending_pull_request(value: dict, *, issue_id: str) -> dict:
    payload = {
        "issueId": issue_id,
        "branch": valid_stored_pull_request_branch(value.get("branch")) or "",
        "startedAt": pull_request_timestamp(value.get("startedAt")) or 0,
    }
    if "lastError" in value:
        payload["lastError"] = clean_pull_request_error(value.get("lastError"))
    failed_at = pull_request_timestamp(value.get("failedAt"))
    if failed_at is not None:
        payload["failedAt"] = failed_at
    return payload


def pull_request_timestamp(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        if not math.isfinite(value):
            return None
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def record_pull_request_pending_failure(issue: dict, message: str) -> None:
    with STATE_LOCK:
        pending = issue.get("pullRequestPending")
        if isinstance(pending, dict):
            pending["lastError"] = clean_pull_request_error(message)
            pending["failedAt"] = now()
        mark_state_dirty()
        persist_state()


def clear_pull_request_pending(issue: dict) -> None:
    with STATE_LOCK:
        issue.pop("pullRequestPending", None)
        mark_state_dirty()
        persist_state()


def remote_git_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return message.startswith("git clone") or message.startswith("git fetch") or message.startswith("git push")


def github_service_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return remote_git_error(exc) or "installation access token" in message


def github_app_write_permissions_message() -> str:
    return "GitHub App installation must grant Contents: write and Pull requests: write for Pullwise to push fix branches and open pull requests."


def clean_pull_request_error(value: object) -> str:
    if not isinstance(value, str):
        return "Pull request creation failed."
    text = value.replace("\x00", "").splitlines()[0].strip()
    return (text or "Pull request creation failed.")[:500]


def installation_supports_pull_request_creation(installation: dict) -> bool:
    permissions = installation.get("permissions") or {}
    return permissions.get("contents") == "write" and permissions.get("pull_requests") == "write"


def clean_repository_full_name(*values: object) -> str:
    for value in values:
        candidate = clean_github_access_text(value)
        if not candidate:
            continue
        try:
            return checkout.validate_repo_full_name(candidate)
        except RuntimeError:
            continue
    return ""


def pull_request_title(issue: dict, issue_id: str) -> str:
    title = clean_pull_request_text(issue.get("title"))
    fallback = clean_pull_request_text(issue_id) or safe_git_ref_component(issue_id, "issue")
    return f"Fix {title or fallback}"


def clean_pull_request_issue_id(value: object) -> str:
    if not isinstance(value, str):
        return "issue"
    text = value.replace("\x00", "").splitlines()[0].strip()
    return safe_git_ref_component(text, "issue")


def clean_pull_request_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    if any(char in value for char in "\r\n\x00"):
        return ""
    return value.strip()


def safe_git_ref_component(value: object, fallback: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", str(value or "")).strip("-_")
    slug = re.sub(r"-+", "-", slug).strip("-_")
    return slug or fallback


def repository_item(github_access: dict | None, full_name: str) -> dict | None:
    if not github_access:
        return None
    for item in repository_items_for_payload(github_access):
        if item.get("fullName") == full_name:
            return item
    return None


def repository_item_by_repo_id(github_access: dict | None, repo_id: str) -> dict | None:
    if not github_access or not repo_id:
        return None
    for item in repository_items_for_payload(github_access):
        if repo_id in {
            str(item.get("repoId") or ""),
            str(item.get("id") or ""),
            str(item.get("githubRepoId") or ""),
        }:
            return item
    return None


def repository_item_for_scan_request(github_access: dict | None, body: dict) -> tuple[dict | None, str | None]:
    repo_id = clean_github_access_text(body.get("repoId"), allow_int=True)
    if repo_id:
        return repository_item_by_repo_id(github_access, repo_id), "repoId"
    full_name = clean_repository_full_name(body.get("repo"))
    if full_name:
        return repository_item(github_access, full_name), "repo"
    return None, None


def repository_is_authorized(github_access: dict | None, full_name: str) -> bool:
    if not github_access:
        return False
    repositories = clean_github_access_text_list(github_access.get("repositories"))
    if repositories:
        return full_name in repositories
    return repository_item(github_access, full_name) is not None


def api_repository_authorized_for_user(user: dict | None, repository: dict | None) -> bool:
    if not user or not repository:
        return False
    github_access = user.get("githubRepositoryAccess")
    if not isinstance(github_access, dict):
        return False

    repository_id = clean_github_access_text(repository.get("id"), allow_int=True)
    github_repo_id = clean_github_access_text(repository.get("github_repo_id"), allow_int=True)
    full_name = clean_repository_full_name(repository.get("full_name"))
    for candidate in (repository_id, github_repo_id):
        if candidate and repository_item_by_repo_id(github_access, candidate):
            return True
    return bool(full_name and repository_is_authorized(github_access, full_name))


def api_repository_access_denial_for_user(user: dict | None, github_access: dict | None) -> tuple[str, str] | None:
    if not user or not isinstance(github_access, dict):
        return None
    if github_repository_authorization_pending(user):
        return "REPOSITORY_AUTHORIZATION_PENDING", "Complete GitHub repository authorization before using API repository routes."
    if not github_repository_access_authorized_for_user(user, github_access):
        return "REPOSITORY_ACCESS_UNAUTHORIZED", "Authorize GitHub repositories before using API repository routes."
    if github_repositories_need_sync(github_access):
        return "REPOSITORY_SYNC_REQUIRED", "Sync GitHub repositories before using API repository routes."
    return None


def sync_repository_access_for_user(user: dict | None, github_access: dict | None) -> None:
    if not user or not isinstance(github_access, dict):
        return
    try:
        from . import repository_access
        repository_access.sync_access_for_user(user, github_access)
    except Exception:
        logger.exception("Unable to sync repository access for user %s", user.get("id"))


def repository_item_with_quota(item: dict, user: dict | None = None) -> dict:
    payload = dict(item)
    repo_id = clean_github_access_text(payload.get("repoId"), allow_int=True)
    if not repo_id:
        github_repo_id = clean_github_access_text(payload.get("githubRepoId"), allow_int=True)
        if github_repo_id:
            repository = db.get_repository_by_github_repo_id(github_repo_id)
            if repository:
                repo_id = repository.get("id")
                payload["repoId"] = repo_id
    if repo_id and user:
        repository = db.get_repository(repo_id)
        if repository:
            payload["quota"] = quota.quota_payload_for_repository(repository, user)
    link_repo_id = clean_github_access_text(payload.get("repoId"), allow_int=True)
    if link_repo_id:
        payload["href"] = f"/repositories/{link_repo_id}"
        payload["scanAction"] = {"method": "POST", "href": f"/api/v1/repositories/{link_repo_id}/scans"}
    return payload


def repository_items_for_response(user: dict | None, github_access: dict | None) -> list[dict]:
    if user and isinstance(github_access, dict):
        sync_repository_access_for_user(user, github_access)
    return [repository_item_with_quota(item, user) for item in repository_items_for_payload(github_access)]


def scan_resource_context(user: dict, github_access: dict, repo_meta: dict) -> tuple[dict, dict]:
    sync_repository_access_for_user(user, github_access)
    from . import repository_access
    repo_record = repository_access.repository_record_from_item(repo_meta)
    if not repo_record:
        raise ValueError("REPOSITORY_SYNC_REQUIRED")
    repository = db.upsert_repository(repo_record)
    return user, repository


def repository_item_from_full_name(full_name: str) -> dict:
    name = full_name.split("/", 1)[1] if "/" in full_name else full_name
    web_url = github_auth.github_web_url().rstrip("/")
    return {
        "id": full_name,
        "name": name,
        "fullName": full_name,
        "desc": "",
        "description": "",
        "lang": "-",
        "private": False,
        "stars": "-",
        "branches": "-",
        "defaultBranch": "main",
        "updated": "",
        "htmlUrl": f"{web_url}/{full_name}",
        "cloneUrl": f"{web_url}/{full_name}.git",
        "permissions": {},
    }


def repository_items_for_payload(github_access: dict | None) -> list[dict]:
    if not github_access:
        return []
    repository_items = github_access.get("repositoryItems") or []
    if isinstance(repository_items, list):
        safe_items = [
            item
            for repository_item in repository_items
            if (item := safe_repository_item_for_payload(repository_item))
        ]
        if safe_items:
            return safe_items
    if (
        github_access.get("mode") != "github-app"
        and not github_access.get("installationId")
        and not github_access.get("installationIds")
    ):
        return []
    return [
        repository_item_from_full_name(str(full_name))
        for full_name in clean_github_access_text_list(github_access.get("repositories"))
    ]


def safe_repository_item_for_payload(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    full_name = clean_github_access_text(value.get("fullName"))
    if not full_name or "/" not in full_name:
        return None

    base_item = repository_item_from_full_name(full_name)
    description = clean_github_access_text(value.get("description")) or clean_github_access_text(value.get("desc")) or ""
    raw_repo_id = clean_github_access_text(value.get("id"), allow_int=True)
    github_repo_id = (
        clean_github_access_text(value.get("githubRepoId"), allow_int=True)
        or raw_repo_id
    )
    owner = value.get("owner") if isinstance(value.get("owner"), dict) else {}
    parent = value.get("parent") if isinstance(value.get("parent"), dict) else {}
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    return {
        "id": raw_repo_id or full_name,
        "repoId": clean_github_access_text(value.get("repoId"), allow_int=True),
        "githubRepoId": github_repo_id,
        "githubNodeId": clean_github_access_text(value.get("githubNodeId")) or clean_github_access_text(value.get("nodeId")),
        "name": clean_github_access_text(value.get("name")) or base_item["name"],
        "fullName": full_name,
        "desc": description,
        "description": description,
        "owner": {
            key: clean_github_access_text(owner.get(key), allow_int=key == "id")
            for key in ("login", "id", "type")
            if clean_github_access_text(owner.get(key), allow_int=key == "id")
        },
        "ownerLogin": clean_github_access_text(value.get("ownerLogin")) or clean_github_access_text(owner.get("login")),
        "ownerId": clean_github_access_text(value.get("ownerId"), allow_int=True) or clean_github_access_text(owner.get("id"), allow_int=True),
        "lang": clean_github_access_text(value.get("lang")) or clean_github_access_text(value.get("language")) or "-",
        "private": value.get("private") is True,
        "fork": value.get("fork") is True,
        "parentGithubRepoId": clean_github_access_text(value.get("parentGithubRepoId"), allow_int=True) or clean_github_access_text(parent.get("id"), allow_int=True),
        "sourceGithubRepoId": clean_github_access_text(value.get("sourceGithubRepoId"), allow_int=True) or clean_github_access_text(source.get("id"), allow_int=True),
        "stars": clean_github_access_text(value.get("stars")) or "-",
        "branches": clean_github_access_text(value.get("branches")) or "-",
        "defaultBranch": clean_github_access_text(value.get("defaultBranch")) or "main",
        "updated": clean_github_access_text(value.get("updated")) or "",
        "htmlUrl": trusted_github_web_url(value.get("htmlUrl")),
        "cloneUrl": trusted_github_web_url(value.get("cloneUrl")),
        "permissions": github_auth.permissions_to_dict(value.get("permissions") or {}),
        "installationId": clean_github_access_text(value.get("installationId"), allow_int=True),
        "installationAccount": clean_github_access_text(value.get("installationAccount")),
        "installationTargetType": clean_github_access_text(value.get("installationTargetType")),
        "repositorySelection": clean_github_access_text(value.get("repositorySelection")),
        "quota": safe_quota_usage_payload(value.get("quota"), default_scope="repository") if isinstance(value.get("quota"), dict) else None,
    }


def github_repository_access_connected(github_access: dict | None) -> bool:
    if not github_access or github_repositories_need_sync(github_access):
        return False
    return bool(repository_items_for_payload(github_access))


def github_repositories_need_sync(github_access: dict | None) -> bool:
    return bool(github_access and github_access.get("repositoriesNeedSync") is True)


def github_repository_access_authorized_for_user(user: dict | None, github_access: dict | None) -> bool:
    if not user or not isinstance(github_access, dict):
        return False
    if github_access.get("mode") == "local":
        return True
    if github_access.get("mode") != "github-app":
        return False

    authorized_user_id = github_access.get("authorizedUserId")
    if authorized_user_id and authorized_user_id != user.get("id"):
        return False

    authorized_github_id = str(github_access.get("authorizedGithubId") or "")
    current_github_id = str(user.get("githubId") or "")
    if authorized_github_id and current_github_id and authorized_github_id != current_github_id:
        return False

    authorized_login = str(github_access.get("authorizedGithubLogin") or "").casefold()
    current_login = str(user.get("githubLogin") or "").casefold()
    if authorized_login and current_login and authorized_login != current_login:
        return False

    if str(github_access.get("installationTargetType") or "").casefold() == "user":
        installation_account = str(github_access.get("installationAccount") or "").casefold()
        installation_id = clean_github_access_text(github_access.get("installationId"), allow_int=True)
        if (
            installation_account
            and current_login
            and installation_account != current_login
            and not verified_identity_can_access_user_installation(user, installation_id, installation_account)
        ):
            return False

    installations = github_access.get("installations") or []
    if not isinstance(installations, list):
        installations = []
    for installation in installations:
        if not isinstance(installation, dict):
            continue
        if str(installation.get("installationTargetType") or "").casefold() != "user":
            continue
        installation_account = str(installation.get("installationAccount") or "").casefold()
        installation_id = clean_github_access_text(installation.get("installationId"), allow_int=True)
        if (
            installation_account
            and current_login
            and installation_account != current_login
            and not verified_identity_can_access_user_installation(user, installation_id, installation_account)
        ):
            return False

    return bool(authorized_user_id)


def verified_identity_can_access_user_installation(
    user: dict | None,
    installation_id: str | None,
    installation_account: str,
) -> bool:
    if not user or not installation_id or not installation_account:
        return False
    access_record = latest_installation_access_record(user, installation_id)
    if not access_record or access_record.get("canAccess") is not True:
        return False
    identity = github_identity_by_id(user, clean_github_access_text(access_record.get("githubIdentityId")))
    identity_login = str((identity or {}).get("githubLogin") or (identity or {}).get("login") or "").casefold()
    return bool(identity_login and identity_login == installation_account.casefold())


def repository_sync_should_refresh(user: dict | None, github_access: dict | None, body: dict) -> bool:
    if body.get("force") is True:
        return True
    if not user:
        return False
    if github_repository_authorization_pending(user):
        return True
    if not github_access:
        return True
    if not github_repository_access_authorized_for_user(user, github_access):
        return True
    if github_repositories_need_sync(github_access):
        return True
    return not github_repository_access_connected(github_access)


def github_repositories_connected_for_user(user: dict | None) -> bool:
    if not user or github_repository_authorization_pending(user):
        return False
    github_access = user.get("githubRepositoryAccess")
    return github_repository_access_authorized_for_user(user, github_access) and github_repository_access_connected(github_access)


def pending_repositories_payload() -> dict:
    return {
        "items": [],
        "repositories": [],
        "needsAuthorization": True,
        "authorizationPending": True,
        "authorizationIssue": "github_authorization_pending",
        "message": (
            "GitHub repository authorization is still pending. "
            "Complete the GitHub App setup window, then sync repositories again."
        ),
    }


def unavailable_repositories_payload(github_access: dict) -> dict:
    repositories_need_sync = github_repositories_need_sync(github_access)
    payload = {
        "items": [],
        "repositories": [],
        "needsAuthorization": True,
        "installationId": clean_github_access_text(github_access.get("installationId"), allow_int=True),
        "installationIds": clean_github_access_text_list(github_access.get("installationIds"), allow_int=True),
        "repositorySelection": clean_github_access_text(github_access.get("repositorySelection")),
        "installationAccount": clean_github_access_text(github_access.get("installationAccount")),
        "installationAccounts": clean_github_access_text_list(github_access.get("installationAccounts")),
        "installations": safe_installation_summaries(github_access.get("installations") or []),
        "repositoriesNeedSync": repositories_need_sync,
    }
    if repositories_need_sync and not github_auth.app_api_configured():
        payload.update({
            "authorizationIssue": "github_app_api_unconfigured",
            "message": (
                "GitHub App API is not configured, so Pullwise cannot sync authorized repositories. "
                "Set PULLWISE_GITHUB_APP_ID and a valid GitHub App private key path or base64 private key, then restart the server."
            ),
        })
    return payload


def installation_summary_from_access(github_access: dict) -> dict:
    repositories = github_access.get("repositories")
    return safe_installation_summary({
        "installationId": github_access.get("installationId"),
        "installationAccount": github_access.get("installationAccount"),
        "installationTargetType": github_access.get("installationTargetType"),
        "installationAppSlug": github_access.get("installationAppSlug"),
        "installationHtmlUrl": github_access.get("installationHtmlUrl"),
        "repositorySelection": github_access.get("repositorySelection"),
        "scope": github_access.get("scope"),
        "repositoryCount": len(repositories) if isinstance(repositories, list) else 0,
        "repositoriesNeedSync": github_repositories_need_sync(github_access),
    })


def safe_installation_summaries(installations: list[dict]) -> list[dict]:
    if not isinstance(installations, list):
        return []
    return [
        safe_installation_summary(installation)
        for installation in installations
        if isinstance(installation, dict)
    ]


def safe_installation_summary(installation: dict) -> dict:
    safe_url = trusted_github_web_url(installation.get("installationHtmlUrl"))
    return {
        "installationId": clean_installation_summary_text(installation.get("installationId")),
        "installationAccount": clean_installation_summary_text(installation.get("installationAccount")),
        "installationTargetType": clean_installation_summary_text(installation.get("installationTargetType")),
        "installationAppSlug": clean_installation_summary_text(installation.get("installationAppSlug")),
        "installationHtmlUrl": safe_url,
        "repositorySelection": clean_installation_summary_text(installation.get("repositorySelection")),
        "scope": clean_installation_summary_text(installation.get("scope")),
        "repositoryCount": safe_installation_repository_count(installation.get("repositoryCount")),
        "repositoriesNeedSync": installation.get("repositoriesNeedSync") is True,
    }


def public_installation_summary(user: dict | None, installation: dict) -> dict:
    item = safe_installation_summary(installation)
    installation_id = clean_installation_summary_text(item.get("installationId"))
    item["installationHtmlUrl"] = None
    item["manage"] = github_installation_manage_status(user, installation_id)
    return item


def public_installation_summaries(user: dict | None, github_access: dict | None) -> list[dict]:
    return [
        public_installation_summary(user, installation)
        for installation in installation_summaries_for_access(github_access)
    ]


def installation_summaries_for_access(github_access: dict | None) -> list[dict]:
    if not isinstance(github_access, dict):
        return []
    installations = github_access.get("installations")
    if isinstance(installations, list) and installations:
        return [
            safe_installation_summary(installation)
            for installation in installations
            if isinstance(installation, dict)
        ]
    if clean_github_access_text(github_access.get("installationId"), allow_int=True):
        return [installation_summary_from_access(github_access)]
    return []


def installation_summary_by_id(github_access: dict | None, installation_id: str) -> dict | None:
    target = str(installation_id)
    for installation in installation_summaries_for_access(github_access):
        if str(installation.get("installationId") or "") == target:
            return installation
    return None


def github_installation_manage_status(user: dict | None, installation_id: str | None) -> dict:
    access_record = latest_installation_access_record(user, installation_id)
    if not access_record:
        return {"mode": "needs_identity"}
    identity = github_identity_by_id(user, clean_github_access_text(access_record.get("githubIdentityId")))
    if access_record.get("canAccess") is True and identity:
        public_identity = public_github_identity(identity)
        if public_identity["status"] == "needs_reauth":
            return {
                "mode": "needs_reauth",
                "githubIdentityId": public_identity["id"],
                "githubLogin": public_identity["login"],
                "lastVerifiedAt": public_identity["lastVerifiedAt"],
            }
        return {
            "mode": "verified_identity",
            "githubIdentityId": public_identity["id"],
            "githubLogin": public_identity["login"],
            "lastVerifiedAt": pull_request_timestamp(access_record.get("verifiedAt")),
        }
    if access_record.get("lastErrorCode") == "github_identity_reauth_required":
        return {"mode": "needs_reauth"}
    return {"mode": "needs_identity", "lastErrorCode": clean_github_access_text(access_record.get("lastErrorCode"))}


def clean_installation_summary_text(value: object) -> str | None:
    return clean_github_access_text(value, allow_int=True)


def clean_github_access_text(value: object, *, allow_int: bool = False) -> str | None:
    if allow_int and isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or any(char in value for char in "\r\n"):
        return None
    return value


def clean_github_access_text_list(value: object, *, allow_int: bool = False) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        text
        for item in value
        if (text := clean_github_access_text(item, allow_int=allow_int))
    ]


def safe_installation_repository_count(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        count = int(value or 0)
    except (OverflowError, TypeError, ValueError):
        return 0
    return max(0, count)


def repository_item_with_installation_context(repository_item: dict, github_access: dict) -> dict:
    item = dict(repository_item)
    item["installationId"] = clean_github_access_text(github_access.get("installationId"), allow_int=True)
    item["installationAccount"] = clean_github_access_text(github_access.get("installationAccount"))
    item["installationTargetType"] = clean_github_access_text(github_access.get("installationTargetType"))
    item["repositorySelection"] = clean_github_access_text(github_access.get("repositorySelection"))
    item["installationPermissions"] = github_access.get("installationPermissions") or {}
    return item


def aggregate_repository_scope(values: list[str]) -> str | None:
    clean_values = [value for value in values if value]
    if not clean_values:
        return None
    first = clean_values[0]
    if all(value == first for value in clean_values):
        return first
    return "mixed"


def github_repository_access_for_installation(
    installation_id: str,
    requested_scope: str = "selected",
    user_access_token: str | None = None,
    installation_hint: dict | None = None,
) -> dict:
    installation = dict(installation_hint or {})
    repository_items = []
    app_api_configured = github_auth.app_api_configured()
    if app_api_configured:
        installation = github_auth.fetch_installation(installation_id)
        if not installation_supports_pull_request_creation(installation):
            raise ValueError(github_app_write_permissions_message())
        repository_items = github_auth.list_installation_repositories(installation_id)
    elif user_access_token:
        if installation.get("permissions") and not installation_supports_pull_request_creation(installation):
            raise ValueError(github_app_write_permissions_message())
        try:
            repository_items = github_auth.list_user_installation_repositories(user_access_token, installation_id)
        except Exception:
            repository_items = []

    repository_selection = installation.get("repository_selection") or requested_scope or "selected"
    account = installation.get("account") or {}
    github_access = {
        "mode": "github-app",
        "scope": "all" if repository_selection == "all" else "selected",
        "repositorySelection": repository_selection,
        "authorizedAt": now(),
        "installationId": installation_id,
        "installationAccount": account.get("login"),
        "installationTargetType": installation.get("target_type"),
        "installationAppSlug": installation.get("app_slug"),
        "installationHtmlUrl": trusted_github_web_url(installation.get("html_url")),
        "installationPermissions": installation.get("permissions") or {},
        "repositories": [repo["fullName"] for repo in repository_items],
        "repositoriesNeedSync": not app_api_configured and not repository_items,
    }
    github_access["repositoryItems"] = [
        repository_item_with_installation_context(repo, github_access)
        for repo in repository_items
    ]
    return github_access


def aggregate_github_repository_access(user: dict, installation_accesses: list[dict]) -> dict | None:
    if not installation_accesses:
        return None

    timestamp = now()
    repository_items_by_name: dict[str, dict] = {}
    for access in installation_accesses:
        for item in access.get("repositoryItems") or []:
            full_name = str(item.get("fullName") or "")
            if full_name and full_name not in repository_items_by_name:
                repository_items_by_name[full_name] = item

    repository_items = list(repository_items_by_name.values())
    installation_summaries = [installation_summary_from_access(access) for access in installation_accesses]
    installation_ids = [str(access.get("installationId")) for access in installation_accesses if access.get("installationId")]
    installation_accounts = [
        str(access.get("installationAccount"))
        for access in installation_accesses
        if access.get("installationAccount")
    ]
    unique_accounts = list(dict.fromkeys(installation_accounts))
    repository_selections = [str(access.get("repositorySelection") or "") for access in installation_accesses]
    scopes = [str(access.get("scope") or "") for access in installation_accesses]
    single_access = installation_accesses[0] if len(installation_accesses) == 1 else None

    return {
        "mode": "github-app",
        "scope": aggregate_repository_scope(scopes) or "selected",
        "repositorySelection": aggregate_repository_scope(repository_selections) or "selected",
        "authorizedAt": timestamp,
        "authorizedUserId": user.get("id"),
        "authorizedGithubId": user.get("githubId"),
        "authorizedGithubLogin": user.get("githubLogin"),
        "validatedAt": timestamp,
        "repositoriesSyncedAt": timestamp,
        "installationId": single_access.get("installationId") if single_access else None,
        "installationIds": installation_ids,
        "installationAccount": unique_accounts[0] if len(unique_accounts) == 1 else None,
        "installationAccounts": unique_accounts,
        "installationTargetType": single_access.get("installationTargetType") if single_access else None,
        "installationAppSlug": single_access.get("installationAppSlug") if single_access else None,
        "installationHtmlUrl": trusted_github_web_url(single_access.get("installationHtmlUrl")) if single_access else None,
        "installationPermissions": single_access.get("installationPermissions") if single_access else {},
        "installations": installation_summaries,
        "repositories": [item["fullName"] for item in repository_items],
        "repositoryItems": repository_items,
        "repositoriesNeedSync": not repository_items,
    }


def installation_accesses_from_github_access(github_access: dict | None) -> list[dict]:
    if not isinstance(github_access, dict) or github_access.get("mode") != "github-app":
        return []
    if clean_github_access_text(github_access.get("installationId"), allow_int=True):
        return [dict(github_access)]

    repository_items = [
        item
        for item in github_access.get("repositoryItems") or []
        if isinstance(item, dict)
    ]
    accesses = []
    for summary in installation_summaries_for_access(github_access):
        installation_id = clean_github_access_text(summary.get("installationId"), allow_int=True)
        if not installation_id:
            continue
        items = [
            item
            for item in repository_items
            if str(item.get("installationId") or "") == installation_id
        ]
        accesses.append({
            "mode": "github-app",
            "scope": summary.get("scope") or github_access.get("scope") or "selected",
            "repositorySelection": summary.get("repositorySelection") or github_access.get("repositorySelection") or "selected",
            "authorizedAt": github_access.get("authorizedAt") or now(),
            "installationId": installation_id,
            "installationAccount": summary.get("installationAccount"),
            "installationTargetType": summary.get("installationTargetType"),
            "installationAppSlug": summary.get("installationAppSlug"),
            "installationHtmlUrl": trusted_github_web_url(summary.get("installationHtmlUrl")),
            "installationPermissions": github_access.get("installationPermissions") or {},
            "repositories": [item["fullName"] for item in items if item.get("fullName")],
            "repositoryItems": items,
            "repositoriesNeedSync": summary.get("repositoriesNeedSync") is True,
        })
    return accesses


def installation_allowed_for_identity(identity: dict, installation: dict) -> bool:
    if str(installation.get("target_type") or "").casefold() != "user":
        return True
    login = str(identity.get("githubLogin") or identity.get("login") or "").casefold()
    return bool(login) and installation_account_login(installation).casefold() == login


def sync_github_repository_installation_scope(
    user: dict,
    installation_id: str,
    *,
    github_identity_id: str | None = None,
) -> dict | None:
    identity = github_identity_by_id(user, github_identity_id) if github_identity_id else None
    if github_identity_id and not identity:
        raise ValueError("GitHub identity is not linked to this Pullwise account.")
    token = identity.get("accessToken") if identity else user.get("githubAccessToken")
    if not token:
        raise ValueError("Sign in with GitHub before syncing repositories.")

    installations = github_auth.list_current_app_installations_for_user(token)
    if identity:
        installations = [
            installation
            for installation in installations
            if installation_allowed_for_identity(identity, installation)
        ]
    else:
        installations = [
            installation
            for installation in installations
            if installation_allowed_for_user(user, installation)
        ]
    target = next(
        (
            installation
            for installation in installations
            if str(installation.get("id") or "") == str(installation_id)
        ),
        None,
    )
    if not target:
        if identity:
            upsert_github_identity_installation_access(
                user,
                identity,
                installation_id,
                can_access=False,
                last_error_code="github_installation_not_visible",
            )
        raise ValueError("GitHub installation is not visible to the selected GitHub identity.")

    refreshed_access = github_repository_access_for_installation(
        installation_id,
        target.get("repository_selection") or "selected",
        token,
        target,
    )
    existing_accesses = [
        access
        for access in installation_accesses_from_github_access(user.get("githubRepositoryAccess"))
        if str(access.get("installationId") or "") != str(installation_id)
    ]
    github_access = aggregate_github_repository_access(user, [*existing_accesses, refreshed_access])
    if github_access:
        user["githubRepositoryAccess"] = github_access
        mark_state_dirty()
    if identity:
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
        )
    return github_access


def bind_pending_selected_github_identity_access(user: dict | None) -> dict | None:
    pending = github_repository_authorization_pending(user)
    if not user or not isinstance(pending, dict):
        return None
    state = clean_github_access_text(pending.get("state"))
    if not state:
        return None
    try:
        record = peek_github_state("install", state)
    except ValueError:
        return None
    identity = github_identity_by_id(
        user,
        clean_github_access_text(record.get("selectedGithubIdentityId")),
    )
    if not identity:
        return None
    token = identity.get("accessToken")
    if not token:
        return None

    installations = [
        installation
        for installation in github_auth.list_current_app_installations_for_user(token)
        if installation_allowed_for_identity(identity, installation)
    ]
    github_access = user.get("githubRepositoryAccess")
    for installation in installations:
        installation_id = clean_github_access_text(installation.get("id"), allow_int=True)
        if not installation_id:
            continue
        github_access = bind_github_repository_installation_for_identity(
            user,
            installation,
            token,
            str(record.get("requestedScope") or "selected"),
        )
        upsert_github_identity_installation_access(
            user,
            identity,
            installation_id,
            can_access=True,
            verification_method="pending_sync",
        )
    return github_access if isinstance(github_access, dict) else None


def installation_allowed_for_user(user: dict, installation: dict) -> bool:
    if str(installation.get("target_type") or "").casefold() != "user":
        return True
    return installation_matches_user_login(user, installation)


def current_user_github_app_installations(user: dict) -> list[dict]:
    return [
        installation
        for installation in github_auth.list_current_app_installations_for_user(user.get("githubAccessToken"))
        if installation_allowed_for_user(user, installation)
    ]


def bind_github_repository_installations(
    user: dict,
    installations: list[dict],
    requested_scope: str = "selected",
) -> dict | None:
    installation_accesses = []
    for installation in installations:
        installation_id = str(installation.get("id") or "")
        if not installation_id:
            continue
        installation_accesses.append(
            github_repository_access_for_installation(
                installation_id,
                installation.get("repository_selection") or requested_scope,
                user.get("githubAccessToken"),
                installation,
            )
        )

    github_access = aggregate_github_repository_access(user, installation_accesses)
    if github_access:
        user["githubRepositoryAccess"] = github_access
        sync_repository_access_for_user(user, github_access)
        mark_state_dirty()
    return github_access


def bind_github_repository_installation_for_identity(
    user: dict,
    installation: dict,
    token: str | None,
    requested_scope: str = "selected",
) -> dict | None:
    installation_id = str(installation.get("id") or "")
    if not installation_id:
        return None
    refreshed_access = github_repository_access_for_installation(
        installation_id,
        installation.get("repository_selection") or requested_scope,
        token,
        installation,
    )
    existing_accesses = [
        access
        for access in installation_accesses_from_github_access(user.get("githubRepositoryAccess"))
        if str(access.get("installationId") or "") != installation_id
    ]
    github_access = aggregate_github_repository_access(user, [*existing_accesses, refreshed_access])
    if github_access:
        user["githubRepositoryAccess"] = github_access
        sync_repository_access_for_user(user, github_access)
        mark_state_dirty()
    return github_access


def installation_account_login(installation: dict) -> str:
    account = installation.get("account") or {}
    return str(account.get("login") or "")


def installation_matches_user_login(user: dict, installation: dict) -> bool:
    login = str(user.get("githubLogin") or "").casefold()
    if not login:
        return False
    if str(installation.get("target_type") or "").casefold() != "user":
        return False
    return installation_account_login(installation).casefold() == login


def try_bind_existing_github_repository_access(user: dict | None, *, force_refresh: bool = False) -> dict | None:
    if not user:
        return None
    existing_access = user.get("githubRepositoryAccess")
    if existing_access and not force_refresh and github_repository_access_authorized_for_user(user, existing_access):
        return existing_access
    if not github_auth.app_install_configured():
        return existing_access if existing_access and github_repository_access_authorized_for_user(user, existing_access) else None
    if not has_real_github_identity(user):
        return existing_access if existing_access and github_repository_access_authorized_for_user(user, existing_access) else None

    installations = current_user_github_app_installations(user)
    return bind_github_repository_installations(user, installations)


def has_real_github_identity(user: dict | None) -> bool:
    if not user:
        return False
    if not github_auth.oauth_configured():
        return "github" in user.get("providers", [])
    return bool(user.get("githubAccessToken"))


def has_github_repository_authorization_identity(user: dict | None) -> bool:
    if not user:
        return False
    if github_auth.oauth_configured():
        return bool(user.get("githubAccessToken") and user.get("githubLogin"))
    return "github" in user.get("providers", [])


def session_payload(session: dict | None) -> dict:
    if not session:
        return {
            "authenticated": False,
            "user": None,
            "github": {"identityConnected": False, "repositoriesConnected": False, "repositoryScope": None},
            "navigation": navigation_payload(),
            "nextStep": "sign_in",
        }

    user = USERS.get(session["userId"])
    repo_access = user.get("githubRepositoryAccess") if user else None
    repositories_pending = bool(github_repository_authorization_pending(user))
    repositories_authorized = github_repository_access_authorized_for_user(user, repo_access)
    visible_access = repo_access if repositories_authorized and not repositories_pending else None
    repositories_connected = repositories_authorized and github_repository_access_connected(repo_access) and not repositories_pending
    return {
        "authenticated": True,
        "user": user_public(user),
        "admin": user_is_admin(user),
        "github": {
            "identityConnected": has_real_github_identity(user),
            "login": public_issue_text(user.get("githubLogin")) or None,
            "repositoriesConnected": repositories_connected,
            "repositoriesAuthorizationPending": repositories_pending,
            "repositoryScope": clean_github_access_text(visible_access.get("scope")) if visible_access else None,
            "authorizedAt": pull_request_timestamp(visible_access.get("authorizedAt")) if visible_access else None,
            "installationId": clean_github_access_text(visible_access.get("installationId"), allow_int=True) if visible_access else None,
            "installationIds": clean_github_access_text_list(visible_access.get("installationIds"), allow_int=True) if visible_access else [],
            "repositorySelection": clean_github_access_text(visible_access.get("repositorySelection")) if visible_access else None,
            "repositoryCount": len(clean_github_access_text_list(visible_access.get("repositories"))) if visible_access else 0,
        },
        "navigation": navigation_payload(),
        "nextStep": "choose_repositories" if repositories_connected else "connect_github_repositories",
    }


