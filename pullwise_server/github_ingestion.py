"""Explicit server-owned composition for the three product fact readers.

Construction performs no I/O. The caller owns the credential callbacks, budget
resolver and scheduler lifecycle; this does not enable the default server or
add a user classification route. Cloudflare supplies its validated I/O/runtime.
"""
from __future__ import annotations

from typing import Callable

from .github_authorization import GitHubAuthorizationChecker
from .github_ci_reader import GitHubCIReader
from .github_credentials import GitHubCredentialResolver
from .github_pr_reader import GitHubPRReader
from .github_release_reader import GitHubReleaseReader
from .github_transport import GitHubRESTTransport
from .product_discovery import FactPage, ProductFactSync
from .product_store import ProductStore


class GitHubFactReader:
    def __init__(self, *, get_json: Callable, token_for_target: Callable,
                 pr_thread_reader=None, ci_log_reader: Callable | None = None):
        self.readers = {
            "pr": GitHubPRReader(get_json=get_json, token_for_target=token_for_target, thread_reader=pr_thread_reader),
            "ci": GitHubCIReader(get_json=get_json, token_for_target=token_for_target,
                                  page_size=1 if ci_log_reader is not None else 100, log_reader=ci_log_reader),
            "updates": GitHubReleaseReader(get_json=get_json, token_for_target=token_for_target),
        }

    def read_page(self, *, target: dict, cursor: str | None, high_watermark: str | None, event: dict | None) -> FactPage:
        module = target.get("module")
        if module not in self.readers:
            raise ValueError("GITHUB_MODULE_UNSUPPORTED")
        return self.readers[module].read_page(target=target, cursor=cursor, high_watermark=high_watermark, event=event)


def build_github_fact_sync(store: ProductStore, *, credentials: GitHubCredentialResolver,
                          processing_budget: Callable, app_id: str, webhook_secret: str,
                          get_json: Callable | None = None, pr_thread_reader=None,
                          ci_log_reader: Callable | None = None) -> ProductFactSync:
    if credentials.app_id != app_id:
        raise ValueError("GITHUB_APP_BINDING_MISMATCH")
    transport = get_json if get_json is not None else GitHubRESTTransport()
    reader = GitHubFactReader(get_json=transport, token_for_target=credentials.token_for_target,
                             pr_thread_reader=pr_thread_reader, ci_log_reader=ci_log_reader)
    checker = GitHubAuthorizationChecker(get_json=transport, resolve_binding=credentials.resolve_binding)
    return ProductFactSync(store, read_page=reader.read_page, refresh_authorization=checker,
                           processing_budget=processing_budget, app_id=app_id, webhook_secret=webhook_secret)
