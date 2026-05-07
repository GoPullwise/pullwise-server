from __future__ import annotations

import base64
import hashlib
import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_API_VERSION = "2022-11-28"
DEFAULT_USER_AGENT = "PullwiseDevAPI/0.1"


class GitHubError(ValueError):
    pass


def env_any(names: list[str], default: str = "") -> str:
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def is_truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def oauth_client_id() -> str:
    return env_any(["PULLWISE_GITHUB_CLIENT_ID", "GITHUB_CLIENT_ID"])


def oauth_client_secret() -> str:
    return env_any(["PULLWISE_GITHUB_CLIENT_SECRET", "GITHUB_CLIENT_SECRET"])


def oauth_configured() -> bool:
    return bool(oauth_client_id() and oauth_client_secret())


def app_slug() -> str:
    return env_any(["PULLWISE_GITHUB_APP_SLUG", "GITHUB_APP_SLUG"])


def app_install_configured() -> bool:
    return bool(app_slug() or env_any(["PULLWISE_GITHUB_APP_INSTALL_URL", "GITHUB_APP_INSTALL_URL"]))


def app_issuer() -> str:
    return env_any(
        [
            "PULLWISE_GITHUB_APP_CLIENT_ID",
            "GITHUB_APP_CLIENT_ID",
            "PULLWISE_GITHUB_APP_ID",
            "GITHUB_APP_ID",
            "PULLWISE_GITHUB_CLIENT_ID",
            "GITHUB_CLIENT_ID",
        ]
    )


def app_api_configured() -> bool:
    return bool(app_issuer() and app_private_key())


def github_web_url() -> str:
    return env_any(["PULLWISE_GITHUB_WEB_URL", "GITHUB_WEB_URL"], "https://github.com").rstrip("/")


def github_api_url() -> str:
    return env_any(["PULLWISE_GITHUB_API_URL", "GITHUB_API_URL"], "https://api.github.com").rstrip("/")


def api_version() -> str:
    return env_any(["PULLWISE_GITHUB_API_VERSION", "GITHUB_API_VERSION"], DEFAULT_API_VERSION)


def request_timeout() -> int:
    raw = env_any(["PULLWISE_GITHUB_TIMEOUT_SECONDS"], "12")
    try:
        return max(1, int(raw))
    except ValueError:
        return 12


def oauth_scope() -> str:
    return env_any(["PULLWISE_GITHUB_OAUTH_SCOPE"], "read:user user:email")


def build_oauth_authorize_url(redirect_uri: str, state: str, code_challenge: str) -> str:
    params = {
        "client_id": oauth_client_id(),
        "redirect_uri": redirect_uri,
        "scope": oauth_scope(),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    prompt = env_any(["PULLWISE_GITHUB_OAUTH_PROMPT"])
    if prompt:
        params["prompt"] = prompt
    allow_signup = env_any(["PULLWISE_GITHUB_ALLOW_SIGNUP"])
    if allow_signup:
        params["allow_signup"] = allow_signup
    return f"{github_web_url()}/login/oauth/authorize?{urlencode(params)}"


def build_app_install_url(state: str) -> str:
    configured = env_any(["PULLWISE_GITHUB_APP_INSTALL_URL", "GITHUB_APP_INSTALL_URL"])
    base_url = configured.rstrip("/") if configured else f"{github_web_url()}/apps/{app_slug()}/installations/new"
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}{urlencode({'state': state})}"


def make_code_verifier() -> str:
    return base64.urlsafe_b64encode(os.urandom(48)).rstrip(b"=").decode("ascii")


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def exchange_oauth_code(code: str, redirect_uri: str, code_verifier: str | None = None) -> dict:
    payload = {
        "client_id": oauth_client_id(),
        "client_secret": oauth_client_secret(),
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if code_verifier:
        payload["code_verifier"] = code_verifier

    response = http_form_json(
        "POST",
        f"{github_web_url()}/login/oauth/access_token",
        payload,
        headers={"Accept": "application/json"},
    )
    if response.get("error"):
        raise GitHubError(response.get("error_description") or response["error"])
    if not response.get("access_token"):
        raise GitHubError("GitHub did not return an access token.")
    return response


def fetch_user_profile(access_token: str) -> dict:
    profile = github_api_json("GET", "/user", token=access_token)
    if not profile.get("login"):
        raise GitHubError("GitHub user profile response is missing login.")
    profile["primaryEmail"] = profile.get("email") or fetch_primary_email(access_token)
    return profile


def fetch_primary_email(access_token: str) -> str | None:
    try:
        emails = github_api_json("GET", "/user/emails", token=access_token)
    except GitHubError:
        return None
    if not isinstance(emails, list):
        return None

    verified = [email for email in emails if email.get("verified")]
    for email in verified:
        if email.get("primary") and email.get("email"):
            return email["email"]
    for email in verified:
        if email.get("email"):
            return email["email"]
    for email in emails:
        if email.get("email"):
            return email["email"]
    return None


def fetch_installation(installation_id: str) -> dict:
    return github_api_json("GET", f"/app/installations/{installation_id}", token=create_app_jwt())


def create_installation_access_token(installation_id: str) -> dict:
    return github_api_json("POST", f"/app/installations/{installation_id}/access_tokens", token=create_app_jwt(), payload={})


def list_installation_repositories(installation_token: str) -> list[dict]:
    return github_paginated_items("/installation/repositories", "repositories", installation_token)


def list_user_installations(user_access_token: str) -> list[dict]:
    return github_paginated_items("/user/installations", "installations", user_access_token)


def user_can_access_installation(user_access_token: str | None, installation_id: str) -> bool | None:
    if not user_access_token:
        return None
    try:
        installations = list_user_installations(user_access_token)
    except GitHubError:
        return None
    return any(str(installation.get("id")) == str(installation_id) for installation in installations)


def github_paginated_items(path: str, key: str, token: str) -> list[dict]:
    items: list[dict] = []
    page = 1
    while True:
        separator = "&" if "?" in path else "?"
        payload = github_api_json("GET", f"{path}{separator}per_page=100&page={page}", token=token)
        batch = payload.get(key, [])
        if not isinstance(batch, list):
            raise GitHubError(f"GitHub response for {path} did not include {key}.")
        items.extend(batch)
        if len(batch) < 100:
            return items
        page += 1


def github_api_json(method: str, path: str, token: str | None = None, payload: dict | None = None) -> dict | list:
    url = path if path.startswith("http") else f"{github_api_url()}{path}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": env_any(["PULLWISE_GITHUB_USER_AGENT"], DEFAULT_USER_AGENT),
        "X-GitHub-Api-Version": api_version(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http_json(method, url, payload=payload, headers=headers)


def http_form_json(method: str, url: str, payload: dict, headers: dict | None = None) -> dict:
    body = urlencode(payload).encode("utf-8")
    request_headers = {"Content-Type": "application/x-www-form-urlencoded", **(headers or {})}
    return read_json_response(Request(url, data=body, method=method, headers=request_headers))


def http_json(method: str, url: str, payload: dict | None = None, headers: dict | None = None) -> dict | list:
    body = None
    request_headers = dict(headers or {})
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    return read_json_response(Request(url, data=body, method=method, headers=request_headers))


def read_json_response(request: Request) -> dict | list:
    try:
        with urlopen(request, timeout=request_timeout()) as response:
            raw = response.read().decode("utf-8")
    except HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise GitHubError(parse_error_message(raw_error) or f"GitHub request failed with HTTP {exc.code}.") from exc
    except URLError as exc:
        raise GitHubError(f"GitHub request failed: {exc.reason}") from exc

    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubError("GitHub returned a non-JSON response.") from exc


def parse_error_message(raw: str) -> str:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    return payload.get("message") or payload.get("error_description") or payload.get("error") or raw.strip()


def app_private_key() -> str:
    direct = env_any(["PULLWISE_GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY"])
    if direct:
        return normalize_private_key(direct)

    encoded = env_any(["PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64", "GITHUB_APP_PRIVATE_KEY_BASE64"])
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8")
        except ValueError as exc:
            raise GitHubError("PULLWISE_GITHUB_APP_PRIVATE_KEY_BASE64 is not valid base64.") from exc

    key_path = env_any(["PULLWISE_GITHUB_APP_PRIVATE_KEY_PATH", "GITHUB_APP_PRIVATE_KEY_PATH"])
    if key_path:
        with open(key_path, "r", encoding="utf-8") as private_key_file:
            return private_key_file.read()
    return ""


def normalize_private_key(value: str) -> str:
    return value.replace("\\n", "\n").strip()


def create_app_jwt() -> str:
    issuer = app_issuer()
    private_key = app_private_key()
    if not issuer or not private_key:
        raise GitHubError("GitHub App JWT requires app id/client id and a private key.")

    current_time = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {"iat": current_time - 60, "exp": current_time + 540, "iss": issuer}
    signing_input = f"{base64url_json(header)}.{base64url_json(payload)}".encode("ascii")
    signature = rsa_sha256_sign(signing_input, private_key)
    return f"{signing_input.decode('ascii')}.{base64url(signature)}"


def base64url_json(payload: dict) -> str:
    return base64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def base64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def rsa_sha256_sign(message: bytes, private_key_pem: str) -> bytes:
    n, d = rsa_private_numbers(private_key_pem)
    key_size = (n.bit_length() + 7) // 8
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(message).digest()
    padding_length = key_size - len(digest_info) - 3
    if padding_length < 8:
        raise GitHubError("RSA private key is too small for RS256.")
    encoded_message = b"\x00\x01" + (b"\xff" * padding_length) + b"\x00" + digest_info
    signature_int = pow(int.from_bytes(encoded_message, "big"), d, n)
    return signature_int.to_bytes(key_size, "big")


def rsa_private_numbers(private_key_pem: str) -> tuple[int, int]:
    der = pem_to_der(private_key_pem)
    reader = DerReader(der)
    sequence = reader.read_sequence()
    first_integer = sequence.read_integer()

    if sequence.peek_tag() == 0x30:
        sequence.read_sequence()
        private_key_der = sequence.read_octet_string()
        return rsa_private_numbers_from_der(private_key_der)

    n = sequence.read_integer()
    sequence.read_integer()
    d = sequence.read_integer()
    return n, d


def rsa_private_numbers_from_der(der: bytes) -> tuple[int, int]:
    sequence = DerReader(der).read_sequence()
    sequence.read_integer()
    n = sequence.read_integer()
    sequence.read_integer()
    d = sequence.read_integer()
    return n, d


def pem_to_der(private_key_pem: str) -> bytes:
    lines = [
        line.strip()
        for line in private_key_pem.strip().splitlines()
        if line.strip() and not line.startswith("-----")
    ]
    if not lines:
        raise GitHubError("GitHub App private key must be a PEM encoded RSA key.")
    try:
        return base64.b64decode("".join(lines))
    except ValueError as exc:
        raise GitHubError("GitHub App private key PEM is not valid base64.") from exc


class DerReader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def peek_tag(self) -> int | None:
        if self.offset >= len(self.data):
            return None
        return self.data[self.offset]

    def read_sequence(self) -> "DerReader":
        return DerReader(self.read_tlv(0x30))

    def read_integer(self) -> int:
        return int.from_bytes(self.read_tlv(0x02), "big")

    def read_octet_string(self) -> bytes:
        return self.read_tlv(0x04)

    def read_tlv(self, expected_tag: int) -> bytes:
        if self.offset >= len(self.data) or self.data[self.offset] != expected_tag:
            raise GitHubError("GitHub App private key has an unsupported PEM structure.")
        self.offset += 1
        length = self.read_length()
        value = self.data[self.offset : self.offset + length]
        if len(value) != length:
            raise GitHubError("GitHub App private key DER is truncated.")
        self.offset += length
        return value

    def read_length(self) -> int:
        if self.offset >= len(self.data):
            raise GitHubError("GitHub App private key DER is truncated.")
        first = self.data[self.offset]
        self.offset += 1
        if first < 0x80:
            return first
        length_size = first & 0x7F
        if length_size == 0 or length_size > 4:
            raise GitHubError("GitHub App private key DER length is unsupported.")
        if self.offset + length_size > len(self.data):
            raise GitHubError("GitHub App private key DER is truncated.")
        length = int.from_bytes(self.data[self.offset : self.offset + length_size], "big")
        self.offset += length_size
        return length


def repo_to_pullwise(repo: dict) -> dict:
    full_name = repo.get("full_name") or repo.get("fullName") or repo.get("name") or ""
    return {
        "id": str(repo.get("id") or full_name),
        "name": repo.get("name") or full_name,
        "fullName": full_name,
        "desc": repo.get("description") or "",
        "description": repo.get("description") or "",
        "lang": repo.get("language") or "-",
        "private": bool(repo.get("private")),
        "stars": format_count(repo.get("stargazers_count")),
        "branches": repo.get("branches") or "-",
        "defaultBranch": repo.get("default_branch") or "main",
        "updated": repo.get("updated_at") or "",
        "htmlUrl": repo.get("html_url"),
        "permissions": repo.get("permissions") or {},
    }


def format_count(value: int | None) -> str:
    if value is None:
        return "-"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1000:
        return f"{value / 1000:.1f}k"
    return str(value)
