"""Server-owned credential composition; no secret store, HTTP route or scheduler.

Inject existing account helpers ``github_identities_for_user`` and
``latest_installation_access_record``. ``repository_for_account(user, github_id)``
reads account repository metadata (id/fullName), never public search. The checker
still verifies remote user identity and maintenance authority; cached installation
``canAccess`` is only an identity selection fence, not maintenance proof.

Existing account identity persistence does not retain OAuth expiry or refresh
tokens. This resolver does not claim to refresh them: remote uncertainty cannot
renew a proof. Supplied ``expires_at`` is checked when present. Installation
callbacks use existing create_installation_access_token's token/expires_at shape.
App JWT issuance is an explicit server callback. Nothing is wired by default.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import math
from typing import Callable

from .github_authorization import GitHubAuthorizationBinding, _id, _repo_path
from .github_transport import GitHubUnavailable


class GitHubCredentialResolver:
    def __init__(self, *, current_target: Callable, current_account: Callable,
                 identities_for_user: Callable, installation_access_for_user: Callable,
                 repository_for_account: Callable, app_id: str, app_token: Callable,
                 installation_token: Callable, clock: Callable):
        self.current_target = current_target
        self.current_account = current_account
        self.identities_for_user = identities_for_user
        self.installation_access_for_user = installation_access_for_user
        self.repository_for_account = repository_for_account
        self.app_id = app_id
        self.app_token = app_token
        self.installation_token = installation_token
        self.clock = clock

    @staticmethod
    def _reject():
        raise GitHubUnavailable('GITHUB_CREDENTIAL_UNAVAILABLE')

    def _target(self, target: dict, *, read: bool) -> None:
        current = self.current_target(target['control_key'])
        if current != target or not target.get('enabled'):
            self._reject()
        if target.get('module') not in {'pr', 'ci', 'updates'}:
            self._reject()
        if read and (not target.get('accessible') or target.get('valid_until', 0) <= self.clock()):
            self._reject()

    def _token(self, value, *, expiry_required=False) -> str:
        if isinstance(value, dict):
            expiry = value.get('expires_at')
            if expiry is not None:
                if type(expiry) in (int, float):
                    timestamp = expiry
                else:
                    parsed = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
                    if parsed.tzinfo is None:
                        self._reject()
                    timestamp = parsed.timestamp()
                if not math.isfinite(timestamp) or not timestamp > self.clock():
                    self._reject()
            elif expiry_required:
                self._reject()
            value = value.get('token')
        elif expiry_required:
            self._reject()
        if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
            self._reject()
        return value

    def _repository(self, account: dict, repository_id: str) -> str:
        record = self.repository_for_account(deepcopy(account), repository_id)
        if not isinstance(record, dict) or _id(record.get('id')) != repository_id:
            self._reject()
        name = record.get('fullName')
        _repo_path(name)
        return name

    def _resolve(self, target: dict, *, read: bool):
        target = deepcopy(target)
        self._target(target, read=read)
        owner = target['billing_owner_id']
        account = deepcopy(self.current_account(owner))
        if account is None:
            return None
        if account.get('id') != owner:
            self._reject()
        module = target['module']
        installation_id = target.get('installation_id')
        target_repository_id = None
        if module == 'updates':
            if target.get('owner_id') != owner:
                self._reject()
            target_repository_id = target.get('target_repository_id')
            if target_repository_id is not None:
                if target.get('target_billing_owner_id') != owner or not target_repository_id.startswith('github:'):
                    self._reject()
                target_repository_id = _id(target_repository_id[7:])
                installation_id = target.get('target_installation_id')
            else:
                installation_id = None
        identities = deepcopy(self.identities_for_user(deepcopy(account)))
        if installation_id is not None:
            installation_id = _id(installation_id)
            if _id(self.app_id) != target.get('app_id'):
                self._reject()
            access = deepcopy(self.installation_access_for_user(deepcopy(account), installation_id))
            if not isinstance(access, dict) or access.get('canAccess') is not True or str(access.get('githubAppInstallationId')) != installation_id:
                self._reject()
            candidates = [i for i in identities if i.get('id') == access.get('githubIdentityId')]
        elif module == 'updates' and target_repository_id is None:
            candidates = [i for i in identities if str(i.get('githubUserId')) == str(account.get('githubId'))]
        else:
            self._reject()
        if len(candidates) != 1:
            self._reject()
        identity = candidates[0]
        if identity.get('userId') != owner or identity.get('status') != 'active':
            self._reject()
        github_user_id = _id(identity.get('githubUserId'))
        user_token = self._token(dict(token=identity.get('accessToken'), expires_at=identity.get('expires_at')))
        repository_id = _id(target['github_repository_id'])
        name = self._repository(account, repository_id)
        target_name = self._repository(account, target_repository_id) if target_repository_id else None
        app_token = installation_token = None
        app_payload = installation_payload = None
        if installation_id is not None:
            # Fact reads do not need an App JWT or an authority check.
            if not read:
                app_payload = self.app_token()
                app_token = self._token(app_payload)
            if not read or module != 'updates':
                installation_payload = self.installation_token(installation_id)
                installation_token = self._token(installation_payload, expiry_required=True)
        self._target(target, read=read)
        if self.current_account(owner) != account:
            self._reject()
        # Identity/install bindings can be stored independently of the account
        # row. Re-read only the selected identity, never fence unrelated links.
        current_identities = self.identities_for_user(deepcopy(account))
        selected = [candidate for candidate in current_identities
                    if candidate.get('id') == identity.get('id')]
        if len(selected) != 1 or selected[0] != identity:
            self._reject()
        if installation_id is not None:
            current_access = self.installation_access_for_user(deepcopy(account), installation_id)
            if (not isinstance(current_access, dict)
                    or current_access.get('canAccess') is not True
                    or current_access.get('githubIdentityId') != identity.get('id')
                    or str(current_access.get('githubAppInstallationId')) != installation_id):
                self._reject()
        # Issuance may cross the lifetime of an earlier credential.
        self._token(dict(token=user_token, expires_at=identity.get('expires_at')))
        if app_payload is not None:
            self._token(app_payload)
        if installation_payload is not None:
            self._token(installation_payload, expiry_required=True)
        return GitHubAuthorizationBinding(
            billing_owner_id=owner, github_user_id=github_user_id,
            repository_id=repository_id, repository_full_name=name, user_token=user_token,
            app_id=self.app_id if installation_id else None, installation_id=installation_id,
            app_token=app_token, installation_token=installation_token,
            target_repository_id=target_repository_id, target_repository_full_name=target_name)

    def _safe_resolve(self, target: dict, *, read: bool):
        try:
            return self._resolve(target, read=read)
        except GitHubUnavailable as error:
            raise GitHubUnavailable('GITHUB_CREDENTIAL_UNAVAILABLE', retry_at=error.retry_at) from None
        except Exception:
            raise GitHubUnavailable('GITHUB_CREDENTIAL_UNAVAILABLE') from None

    def resolve_binding(self, target: dict) -> GitHubAuthorizationBinding | None:
        """Resolve credentials for independent trusted renewal, including expired proofs."""
        return self._safe_resolve(target, read=False)

    def token_for_target(self, target: dict) -> str:
        """Read only under an existing unexpired proof; never renew authority."""
        binding = self._safe_resolve(target, read=True)
        if binding is None:
            self._reject()
        return binding.user_token if target['module'] == 'updates' else binding.installation_token
