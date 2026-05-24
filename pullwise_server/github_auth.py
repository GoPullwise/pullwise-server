from __future__ import annotations

import base64
import binascii
import os
import secrets
from urllib.parse import quote, urlencode

import requests


DEFAULT_API_URL = "https://api.github.com"
DEFAULT_USER_AGENT = "PullwiseDevAPI/0.1"


class GitHubError(ValueError):
    pass


def env_any(names: list[str], default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def oauth_client_id() -> str:
    return env_any(["PULLWISE_GITHUB_CLIENT_ID", "GITHUB_CLIENT_ID"])


def oauth_client_secret() -> str:
    return env_any(["PULLWISE_GITHUB_CLIENT_SECRET", "GITHUB_CLIENT_SECRET"])


def oauth_configured() -> bool:
    return bool(oauth_client_id() and oauth_client_secret())


def app_slug() -> str:
    return env_any(["PULLWISE_GITHUB_APP_SLUG", "GITHUB_APP_SLUG"])


def app_install_url_override() -> str:
    return env_any(["PULLWISE_GITHUB_APP_INSTALL_URL", "GITHUB_APP_INSTALL_URL"])


def app_id() -> str:
    return env_any(["PULLWISE_GITHUB_APP_ID", "GITHUB_APP_ID"])


def app_id_int() -> int:
    try:
        value = int(app_id())
    except ValueError:
        raise GitHubError("PULLWISE_GITHUB_APP_ID must be a positive integer.") from None
    if value <= 0:
        raise GitHubError("PULLWISE_GITHUB_APP_ID must be a positive integer.")
    return value


def app_install_configured() -> bool:
    return bool(app_slug() or app_install_url_override())


def app_visibility_check_enabled() -> bool:
    raw = env_any(["PULLWISE_GITHUB_APP_VISIBILITY_CHECK"], "true").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def app_api_configured() -> bool:
    if not app_id():
        return False
    try:
        app_id_int()
        return bool(app_private_key())
    except (GitHubError, OSError):
        return False


def github_web_url() -> str:
    return env_any(["PULLWISE_GITHUB_WEB_URL", "GITHUB_WEB_URL"], "https://github.com").rstrip("/")


def github_api_url() -> str:
    return env_any(["PULLWISE_GITHUB_API_URL", "GITHUB_API_URL"], DEFAULT_API_URL).rstrip("/")


def github_api_version() -> str:
    return env_any(["PULLWISE_GITHUB_API_VERSION"], "2022-11-28")


def request_timeout() -> int:
    raw = env_any(["PULLWISE_GITHUB_TIMEOUT_SECONDS"], "12")
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


def oauth_scope() -> str:
    return env_any(["PULLWISE_GITHUB_OAUTH_SCOPE"], "read:user user:email")


def make_code_verifier() -> str:
    return authlib_generate_token(48)


def build_oauth_authorize_url(redirect_uri: str, state: str, code_verifier: str) -> str:
    client = oauth_session(scope=oauth_scope(), code_challenge_method="S256")
    kwargs = {"redirect_uri": redirect_uri}
    prompt = env_any(["PULLWISE_GITHUB_OAUTH_PROMPT"])
    if prompt:
        kwargs["prompt"] = prompt
    allow_signup = env_any(["PULLWISE_GITHUB_ALLOW_SIGNUP"])
    if allow_signup:
        kwargs["allow_signup"] = allow_signup

    authorize_url, _ = client.create_authorization_url(
        f"{github_web_url()}/login/oauth/authorize",
        state=state,
        code_verifier=code_verifier,
        **kwargs,
    )
    return authorize_url


def build_app_install_url(state: str) -> str:
    configured = app_install_url_override()
    base_url = configured.rstrip("/") if configured else f"{github_web_url()}/apps/{app_slug()}/installations/new"
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'state': state})}"


def app_slug_publicly_installable() -> bool | None:
    slug = app_slug()
    if not slug:
        return None

    try:
        response = requests.get(
            f"{github_api_url()}/apps/{slug}",
            headers=github_api_headers(),
            timeout=request_timeout(),
        )
    except Exception:
        return None

    if response.status_code == 404:
        return False
    try:
        response.raise_for_status()
    except Exception:
        return None
    return True


def exchange_oauth_code(code: str, redirect_uri: str, code_verifier: str, state: str) -> dict:
    client = oauth_session(state=state, token_endpoint_auth_method="client_secret_post")
    authorization_response = f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
    try:
        token = client.fetch_token(
            f"{github_web_url()}/login/oauth/access_token",
            authorization_response=authorization_response,
            code_verifier=code_verifier,
            headers={"Accept": "application/json"},
            timeout=request_timeout(),
        )
    except Exception as exc:
        raise GitHubError(f"GitHub OAuth token exchange failed: {exc}") from exc
    if not token.get("access_token"):
        raise GitHubError("GitHub did not return an access token.")
    return dict(token)


def fetch_user_profile(access_token: str) -> dict:
    client = oauth_session(token={"access_token": access_token, "token_type": "bearer"})
    profile = authlib_get_json(client, "/user")
    if not profile.get("login"):
        raise GitHubError("GitHub user profile response is missing login.")
    profile["primaryEmail"] = clean_email_address(profile.get("email")) or fetch_primary_email(access_token)
    return profile


def fetch_primary_email(access_token: str) -> str | None:
    client = oauth_session(token={"access_token": access_token, "token_type": "bearer"})
    try:
        emails = authlib_get_json(client, "/user/emails")
    except GitHubError:
        return None
    if not isinstance(emails, list):
        return None

    email_records = [email for email in emails if isinstance(email, dict)]
    verified = [email for email in email_records if email.get("verified") is True]
    for email in verified:
        address = email_record_address(email)
        if email.get("primary") is True and address:
            return address
    for email in verified:
        address = email_record_address(email)
        if address:
            return address
    for email in email_records:
        address = email_record_address(email)
        if address:
            return address
    return None


def email_record_address(email: dict) -> str | None:
    return clean_email_address(email.get("email"))


def clean_email_address(address: object) -> str | None:
    if not isinstance(address, str):
        return None
    address = address.strip()
    return address or None


def fetch_installation(installation_id: str) -> dict:
    integration = app_integration()
    try:
        installation = integration.get_app_installation(int(installation_id))
        return installation_to_dict(installation)
    finally:
        integration.close()


def create_installation_access_token(installation_id: str) -> dict:
    try:
        integration = app_integration()
        try:
            token = integration.get_access_token(int(installation_id))
            return {"token": token.token, "expires_at": str(token.expires_at)}
        finally:
            integration.close()
    except GitHubError:
        raise
    except Exception as exc:
        raise GitHubError(f"GitHub installation token request failed: {exc}") from exc


def find_pull_request_by_head(token: str, repo: str, *, head: str) -> dict | None:
    owner = repo.split("/", 1)[0]
    try:
        response = requests.get(
            f"{github_api_url()}/repos/{repo}/pulls",
            headers=github_api_headers(token),
            params={"head": f"{owner}:{head}", "state": "open", "per_page": 1},
            timeout=request_timeout(),
        )
    except Exception as exc:
        raise GitHubError(f"GitHub pull request lookup failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        detail = str(getattr(response, "text", "") or "").strip()
        try:
            response.raise_for_status()
        except Exception as exc:
            message = f"GitHub pull request lookup failed: {exc}"
            if detail:
                message = f"{message}: {detail[:500]}"
            raise GitHubError(message) from exc
        raise GitHubError("GitHub pull request lookup failed.")

    try:
        payload = response.json()
    except Exception as exc:
        raise GitHubError(f"GitHub pull request lookup response was not valid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise GitHubError("GitHub pull request lookup response body was not a list.")
    if not payload:
        return None
    pull_request = payload[0]
    if not isinstance(pull_request, dict):
        raise GitHubError("GitHub pull request lookup response item was not an object.")
    url = pull_request.get("html_url") or pull_request.get("url")
    number = pull_request.get("number")
    if not url or number is None:
        raise GitHubError("GitHub pull request lookup response body was missing url or number.")
    return {
        "url": url,
        "number": number,
        "title": pull_request.get("title") or "",
    }


def branch_exists(token: str, repo: str, branch: str) -> bool:
    encoded_branch = quote(branch, safe="")
    try:
        response = requests.get(
            f"{github_api_url()}/repos/{repo}/git/ref/heads/{encoded_branch}",
            headers=github_api_headers(token),
            timeout=request_timeout(),
        )
    except Exception as exc:
        raise GitHubError(f"GitHub branch lookup failed: {exc}") from exc

    if response.status_code == 404:
        return False
    if response.status_code < 200 or response.status_code >= 300:
        detail = str(getattr(response, "text", "") or "").strip()
        try:
            response.raise_for_status()
        except Exception as exc:
            message = f"GitHub branch lookup failed: {exc}"
            if detail:
                message = f"{message}: {detail[:500]}"
            raise GitHubError(message) from exc
        raise GitHubError("GitHub branch lookup failed.")

    try:
        payload = response.json()
    except Exception as exc:
        raise GitHubError(f"GitHub branch lookup response was not valid JSON: {exc}") from exc
    exact_ref = f"refs/heads/{branch}"
    if isinstance(payload, dict):
        if payload.get("ref") == exact_ref:
            return True
        raise GitHubError("GitHub branch lookup response body did not match the requested branch.")
    if isinstance(payload, list):
        if any(isinstance(item, dict) and item.get("ref") == exact_ref for item in payload):
            return True
        raise GitHubError("GitHub branch lookup response body did not match the requested branch.")
    raise GitHubError("GitHub branch lookup response body was not valid.")


def create_pull_request(token: str, repo: str, *, title: str, head: str, base: str, body: str) -> dict:
    try:
        response = requests.post(
            f"{github_api_url()}/repos/{repo}/pulls",
            headers=github_api_headers(token),
            json={
                "title": title,
                "head": head,
                "base": base,
                "body": body,
            },
            timeout=request_timeout(),
        )
    except Exception as exc:
        raise GitHubError(f"GitHub pull request creation failed: {exc}") from exc

    if response.status_code < 200 or response.status_code >= 300:
        detail = str(getattr(response, "text", "") or "").strip()
        try:
            response.raise_for_status()
        except Exception as exc:
            message = f"GitHub pull request creation failed: {exc}"
            if detail:
                message = f"{message}: {detail[:500]}"
            raise GitHubError(message) from exc
        raise GitHubError("GitHub pull request creation failed.")

    try:
        payload = response.json()
    except Exception as exc:
        raise GitHubError(f"GitHub pull request response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GitHubError("GitHub pull request response body was not an object.")
    url = payload.get("html_url") or payload.get("url")
    number = payload.get("number")
    if not url or number is None:
        raise GitHubError("GitHub pull request response body was missing url or number.")
    return {
        "url": url,
        "number": number,
        "title": payload.get("title") or title,
    }


def list_installation_repositories(installation_id: str) -> list[dict]:
    token_payload = create_installation_access_token(installation_id)
    token = token_payload.get("token")
    if not token:
        raise GitHubError("GitHub did not return an installation access token.")

    repositories = []
    url = f"{github_api_url()}/installation/repositories"
    params = {"per_page": 100}
    while url:
        response = requests.get(
            url,
            headers=github_api_headers(token),
            params=params,
            timeout=request_timeout(),
        )
        try:
            response.raise_for_status()
        except Exception as exc:
            raise GitHubError(f"GitHub installation repositories request failed: {exc}") from exc
        repositories.extend(repo_to_pullwise(repo) for repo in repositories_from_response(response, "installation repositories"))
        url = ((getattr(response, "links", {}) or {}).get("next") or {}).get("url")
        params = None
    return repositories


def list_user_installation_repositories(user_access_token: str | None, installation_id: str) -> list[dict]:
    if not user_access_token:
        return []

    repositories = []
    url = f"{github_api_url()}/user/installations/{installation_id}/repositories"
    params = {"per_page": 100}
    while url:
        response = requests.get(
            url,
            headers=github_api_headers(user_access_token),
            params=params,
            timeout=request_timeout(),
        )
        try:
            response.raise_for_status()
        except Exception as exc:
            raise GitHubError(f"GitHub user installation repositories request failed: {exc}") from exc
        repositories.extend(repo_to_pullwise(repo) for repo in repositories_from_response(response, "user installation repositories"))
        url = ((getattr(response, "links", {}) or {}).get("next") or {}).get("url")
        params = None
    return repositories


def repositories_from_response(response, context: str) -> list:
    try:
        payload = response.json()
    except Exception as exc:
        raise GitHubError(f"GitHub {context} response was not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise GitHubError(f"GitHub {context} response body was not an object.")
    repositories = payload.get("repositories") or []
    if not isinstance(repositories, list):
        raise GitHubError(f"GitHub {context} response repositories field was not a list.")
    return [repo for repo in repositories if valid_repository_payload(repo)]


def valid_repository_payload(repo: object) -> bool:
    if not isinstance(repo, dict):
        return False
    full_name = repo.get("full_name")
    return isinstance(full_name, str) and "/" in full_name and bool(full_name.strip())


def github_api_headers(token: str | None = None) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": github_api_version(),
        "User-Agent": DEFAULT_USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def user_can_access_installation(user_access_token: str | None, installation_id: str) -> bool | None:
    if not user_access_token:
        return None

    try:
        github = github_client(token=user_access_token)
    except GitHubError:
        return None
    try:
        user = github.get_user()
        get_installations = getattr(user, "get_installations", None)
        if callable(get_installations):
            return any(str(installation.id) == str(installation_id) for installation in get_installations())

        _, payload = github.requester.requestJsonAndCheck("GET", "/user/installations")
        installations = payload.get("installations", []) if isinstance(payload, dict) else []
        return any(str(installation.get("id")) == str(installation_id) for installation in installations)
    except Exception:
        return None
    finally:
        github.close()


def list_current_app_installations_for_user(user_access_token: str | None) -> list[dict]:
    if not user_access_token:
        return []

    configured_slug = app_slug()
    configured_app_id = app_id()
    if not configured_slug and not configured_app_id:
        return []

    try:
        github = github_client(token=user_access_token)
    except GitHubError:
        return []
    try:
        user = github.get_user()
        get_installations = getattr(user, "get_installations", None)
        if callable(get_installations):
            installations = [installation_to_dict(installation) for installation in get_installations()]
        else:
            _, payload = github.requester.requestJsonAndCheck("GET", "/user/installations")
            raw_installations = payload.get("installations", []) if isinstance(payload, dict) else []
            installations = [installation_payload_to_dict(installation) for installation in raw_installations]
    except Exception:
        return []
    finally:
        github.close()

    return [
        installation
        for installation in installations
        if installation_matches_configured_app(installation, configured_slug, configured_app_id)
    ]


def installation_matches_configured_app(installation: dict, configured_slug: str, configured_app_id: str) -> bool:
    installation_slug = str(installation.get("app_slug") or "").casefold()
    installation_app_id = str(installation.get("app_id") or "")
    if configured_slug and installation_slug == configured_slug.casefold():
        return True
    return bool(configured_app_id and installation_app_id == str(configured_app_id))


def oauth_session(**kwargs):
    OAuth2Session = import_authlib_session()
    return OAuth2Session(oauth_client_id(), oauth_client_secret(), **kwargs)


def authlib_get_json(client, path: str):
    response = client.get(
        f"{github_api_url()}{path}",
        headers=github_api_headers(),
        timeout=request_timeout(),
    )
    try:
        response.raise_for_status()
    except Exception as exc:
        raise GitHubError(f"GitHub API request failed: {exc}") from exc
    return response.json()


def github_client(token: str | None = None):
    Auth, Github, _GithubIntegration = import_pygithub()
    kwargs = {"timeout": request_timeout(), "per_page": 100, "user_agent": DEFAULT_USER_AGENT}
    if github_api_url() != DEFAULT_API_URL:
        kwargs["base_url"] = github_api_url()
    if token:
        kwargs["auth"] = Auth.Token(token)
    return Github(**kwargs)


def app_integration():
    Auth, _Github, GithubIntegration = import_pygithub()
    kwargs = {"timeout": request_timeout(), "per_page": 100, "user_agent": DEFAULT_USER_AGENT}
    if github_api_url() != DEFAULT_API_URL:
        kwargs["base_url"] = github_api_url()
    return GithubIntegration(auth=Auth.AppAuth(app_id_int(), app_private_key()), **kwargs)


def app_private_key() -> str:
    direct = env_any(["PULLWISE_GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY"])
    if direct:
        return direct.replace("\\n", "\n").strip()

    encoded = env_any(["PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64", "GITHUB_APP_PRIVATE_KEY_BASE64"])
    if encoded:
        try:
            return base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
        except (binascii.Error, ValueError) as exc:
            raise GitHubError("PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64 is not valid base64.") from exc

    key_path = env_any(["PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_PRIVATE_KEY_PATH"])
    if key_path:
        with open(key_path, "r", encoding="utf-8") as private_key_file:
            return private_key_file.read()
    return ""


def installation_to_dict(installation) -> dict:
    account = getattr(installation, "account", None)
    return {
        "id": getattr(installation, "id", None),
        "repository_selection": getattr(installation, "repository_selection", None),
        "target_type": getattr(installation, "target_type", None),
        "account": {"login": getattr(account, "login", None)} if account else {},
        "app_slug": getattr(installation, "app_slug", None),
        "app_id": getattr(installation, "app_id", None),
        "html_url": getattr(installation, "html_url", None),
        "permissions": permission_levels_to_dict(getattr(installation, "permissions", None)),
    }


def installation_payload_to_dict(installation: dict) -> dict:
    account = installation.get("account") or {}
    return {
        "id": installation.get("id"),
        "repository_selection": installation.get("repository_selection"),
        "target_type": installation.get("target_type"),
        "account": {"login": account.get("login")} if isinstance(account, dict) else {},
        "app_slug": installation.get("app_slug"),
        "app_id": installation.get("app_id"),
        "html_url": installation.get("html_url"),
        "permissions": permission_levels_to_dict(installation.get("permissions")),
    }


def permission_levels_to_dict(permissions) -> dict:
    if not permissions:
        return {}
    if isinstance(permissions, dict):
        return {str(key): str(value) for key, value in permissions.items() if value is not None}

    raw_data = getattr(permissions, "raw_data", None) or getattr(permissions, "_rawData", None)
    if isinstance(raw_data, dict):
        return {str(key): str(value) for key, value in raw_data.items() if value is not None}

    result = {}
    for key in ("metadata", "contents", "issues", "pull_requests", "checks", "statuses"):
        try:
            value = getattr(permissions, key)
        except Exception:
            continue
        if value is not None:
            result[key] = str(value)
    return result


def repo_to_pullwise(repo) -> dict:
    if isinstance(repo, dict):
        return repo_payload_to_pullwise(repo)
    full_name = getattr(repo, "full_name", None) or getattr(repo, "name", "") or ""
    return {
        "id": str(getattr(repo, "id", None) or full_name),
        "name": getattr(repo, "name", None) or full_name,
        "fullName": full_name,
        "desc": getattr(repo, "description", None) or "",
        "description": getattr(repo, "description", None) or "",
        "lang": getattr(repo, "language", None) or "-",
        "private": bool(getattr(repo, "private", False)),
        "stars": format_count(getattr(repo, "stargazers_count", None)),
        "branches": "-",
        "defaultBranch": getattr(repo, "default_branch", None) or "main",
        "updated": str(getattr(repo, "updated_at", None) or ""),
        "htmlUrl": getattr(repo, "html_url", None),
        "cloneUrl": getattr(repo, "clone_url", None),
        "permissions": permissions_to_dict(getattr(repo, "permissions", None)),
    }


def repo_payload_to_pullwise(repo: dict) -> dict:
    full_name = repo.get("full_name") or repo.get("name") or ""
    return {
        "id": str(repo.get("id") or full_name),
        "name": repo.get("name") or full_name,
        "fullName": full_name,
        "desc": repo.get("description") or "",
        "description": repo.get("description") or "",
        "lang": repo.get("language") or "-",
        "private": bool(repo.get("private", False)),
        "stars": format_count(repo.get("stargazers_count")),
        "branches": "-",
        "defaultBranch": repo.get("default_branch") or "main",
        "updated": str(repo.get("updated_at") or ""),
        "htmlUrl": repo.get("html_url"),
        "cloneUrl": repo.get("clone_url"),
        "permissions": normalize_permission_mapping(repo.get("permissions") or {}),
    }


def permissions_to_dict(permissions) -> dict:
    if not permissions:
        return {}
    if isinstance(permissions, dict):
        return normalize_permission_mapping(permissions)

    raw_data = getattr(permissions, "raw_data", None) or getattr(permissions, "_rawData", None)
    if isinstance(raw_data, dict):
        return normalize_permission_mapping(raw_data)

    result = {}
    for key in ("admin", "maintain", "push", "triage", "pull"):
        try:
            value = getattr(permissions, key)
        except Exception:
            continue
        if isinstance(value, bool):
            result[key] = value
    return result


def normalize_permission_mapping(mapping: dict) -> dict:
    return {str(key): value for key, value in mapping.items() if isinstance(value, bool)}


def format_count(value: object) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)


def import_authlib_session():
    try:
        from authlib.integrations.requests_client import OAuth2Session
    except ImportError as exc:
        raise GitHubError("Install Authlib to use real GitHub OAuth: python -m pip install -e .") from exc
    return OAuth2Session


def authlib_generate_token(length: int) -> str:
    try:
        from authlib.common.security import generate_token
    except ImportError:
        return secrets.token_urlsafe(length)[:length]
    return generate_token(length)


def import_pygithub():
    try:
        from github import Auth, Github, GithubIntegration
    except ImportError as exc:
        raise GitHubError("Install PyGithub to use real GitHub App authorization: python -m pip install -e .") from exc
    return Auth, Github, GithubIntegration
