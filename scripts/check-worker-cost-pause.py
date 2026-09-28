"""Four finite HTTP checks of the fail-closed D1 pause; no retries."""
import argparse
import json
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="http://127.0.0.1:8892")
    parser.add_argument("--remote", action="store_true")
    args = parser.parse_args()
    origin = urlsplit(args.origin)
    if origin.username or origin.password or origin.query or origin.fragment or origin.path:
        raise SystemExit("Use a bare reviewed origin")
    if args.remote:
        if args.origin != "https://api.pull-wise.com":
            raise SystemExit("Remote checks are restricted to the approved Server origin")
    elif origin.scheme != "http" or origin.hostname not in {"127.0.0.1", "localhost"}:
        raise SystemExit("Local checks require loopback HTTP")
    opener = (build_opener(NoRedirect()) if args.remote
              else build_opener(ProxyHandler({}), NoRedirect()))
    for path in ("/health", "/api/v1/me", "/api/v1/expenses", "/auth/github/authorize"):
        request = Request(args.origin + path, headers={"Accept": "application/json"})
        try:
            response = opener.open(request, timeout=60)
        except HTTPError as error:
            response = error
        with response:
            payload = json.loads(response.read())
            if response.status != 503 or payload != {"error": {"code": "D1_ACCESS_PAUSED"}}:
                code = payload.get("error", {}).get("code") if isinstance(payload, dict) else None
                raise SystemExit(f"Cost pause check failed (status={response.status}, code={code}); stop without retrying")
    print(json.dumps({"passed": True, "http_requests": 4,
                      "d1_access_blocked": True, "remote": args.remote}))


if __name__ == "__main__":
    main()
