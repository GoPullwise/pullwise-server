"""Local Server fact worker assembly from current saved account authority."""
from __future__ import annotations

import re
import time

from .github_credentials import GitHubCredentialResolver
from .github_ci_logs import GitHubCILogReader
from .github_ci_transport import GitHubCILogTransport
from .github_ingestion import build_github_fact_sync
from .github_local_identity import repository_for_target
from .github_pr_threads import GitHubPRThreadReader
from .github_transport import GitHubGraphQLTransport


def _analysis_disabled(*args):
    raise RuntimeError("JEV_ANALYSIS_DISABLED")


def build_local_fact_sync(store, *, account_snapshot, identities_for_user,
                          installation_access_for_user, app_id: str, webhook_secret: str,
                          app_token, installation_token, clock=time.time, get_json=None,
                          query_json=None):
    if not isinstance(app_id, str) or not re.fullmatch(r"[1-9][0-9]*", app_id) or not webhook_secret:
        raise ValueError("GITHUB_FACT_SYNC_UNCONFIGURED")
    credentials = GitHubCredentialResolver(
        current_target=store.discovery_target, current_account=account_snapshot,
        identities_for_user=identities_for_user,
        installation_access_for_user=installation_access_for_user,
        repository_for_account=lambda account, repository_id, target, role:
            repository_for_target(store, account, repository_id, target, role),
        app_id=app_id, app_token=app_token,
        installation_token=installation_token, clock=clock)
    sync = build_github_fact_sync(store, credentials=credentials,
        processing_budget=_analysis_disabled,
        app_id=app_id, webhook_secret=webhook_secret, get_json=get_json,
        pr_thread_reader=GitHubPRThreadReader(query_json=query_json or GitHubGraphQLTransport()),
        ci_log_reader=GitHubCILogReader(request=GitHubCILogTransport()),
        analysis_admission_enabled=False,
        seed_targets=lambda *, now: store.seed_discovery_targets(app_id=app_id, now=now))
    sync.credentials = credentials
    return sync
