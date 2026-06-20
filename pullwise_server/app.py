from __future__ import annotations

import sys as _sys
import types as _types

from . import (
    _app_part_01_bootstrap_state,
    _app_part_02_http_auth_settings,
    _app_part_03_billing_pages,
    _app_part_04_scan_audit_bundle,
    _app_part_05_worker_results,
    _app_part_06_worker_admin,
    _app_part_07_issue_payloads,
    _app_part_08_fix_pr_repository_access,
    _app_part_09_billing_cookie_security,
    _app_part_10_handler_main as _assembled_app,
)
from ._app_imports import import_compat_globals as _import_compat_globals
from ._app_imports import register_compat_targets as _register_compat_targets

_APP_PARTS = (
    _app_part_01_bootstrap_state,
    _app_part_02_http_auth_settings,
    _app_part_03_billing_pages,
    _app_part_04_scan_audit_bundle,
    _app_part_05_worker_results,
    _app_part_06_worker_admin,
    _app_part_07_issue_payloads,
    _app_part_08_fix_pr_repository_access,
    _app_part_09_billing_cookie_security,
    _assembled_app,
)

_register_compat_targets(*_APP_PARTS, globals())

for _part in _APP_PARTS:
    _import_compat_globals(vars(_assembled_app), vars(_part))

_import_compat_globals(vars(_assembled_app), globals())


class _CompatAppModule(_types.ModuleType):
    def __setattr__(self, name: str, value: object) -> None:
        super().__setattr__(name, value)
        if name.startswith("__"):
            return
        for part in _APP_PARTS:
            setattr(part, name, value)


_sys.modules[__name__].__class__ = _CompatAppModule

del _CompatAppModule, _assembled_app, _import_compat_globals, _part, _register_compat_targets, _sys, _types

if __name__ == "__main__":
    main()
