from __future__ import annotations

from pathlib import Path as _Path

_PART_FILES = (
    "_app_part_01_bootstrap_state.py",
    "_app_part_02_http_auth_settings.py",
    "_app_part_03_billing_pages.py",
    "_app_part_04_scan_audit_bundle.py",
    "_app_part_05_worker_results.py",
    "_app_part_06_worker_admin.py",
    "_app_part_07_issue_payloads.py",
    "_app_part_08_fix_pr_repository_access.py",
    "_app_part_09_billing_cookie_security.py",
    "_app_part_10_handler_main.py",
)


def _load_part(filename: str) -> None:
    path = _Path(__file__).with_name(filename)
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), globals(), globals())


for _part_file in _PART_FILES:
    _load_part(_part_file)

del _part_file, _load_part, _PART_FILES, _Path
