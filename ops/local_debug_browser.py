from __future__ import annotations

import urllib.parse
import webbrowser
from collections.abc import Callable, Mapping


def local_browser_entry_urls(config: object) -> dict[str, str]:
    server_url = str(getattr(config, "server_url")).rstrip("/")
    destinations = {
        "web": f"{str(getattr(config, 'web_url')).rstrip('/')}/dashboard/overview",
        "admin": f"{str(getattr(config, 'admin_url')).rstrip('/')}/workers",
    }
    return {
        name: f"{server_url}/auth/github/callback?{urllib.parse.urlencode({'redirectTo': destination})}"
        for name, destination in destinations.items()
    }


def open_local_browser_entries(
    entries: Mapping[str, str],
    opener: Callable[[str], object] = webbrowser.open_new_tab,
) -> dict[str, bool]:
    result = {}
    for name in ("web", "admin"):
        try:
            result[name] = bool(opener(entries[name]))
        except Exception:
            result[name] = False
    return result
