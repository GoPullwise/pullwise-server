from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from pullwise_server.github_ci_logs import GitHubCILogReader
from pullwise_server.github_ci_transport import GitHubCILogTransport, _allowed_url, _public_address, _read_https


REDIRECT = "https://productionresultssa.blob.core.windows.net/logs/job.txt?sig=opaque"


def _fixture_fetch(url, headers, deadline, max_bytes):
    if url.startswith("https://api.github.com/"):
        assert headers.get("Authorization") == "Bearer fixture-token"
        return 302, {"Location": REDIRECT}, b""
    assert url == REDIRECT
    assert "Authorization" not in headers
    return 200, {"Content-Type": "text/plain"}, b"step failed\n"


def _slow_fetch(url, headers, deadline, max_bytes):
    time.sleep(5)
    return 200, {}, b"too late"


def _large_fetch(url, headers, deadline, max_bytes):
    return 200, {"Content-Type": "text/plain"}, b"x" * (4 * 1024 * 1024)


class GitHubCILogTransportTest(unittest.TestCase):
    def test_public_peer_check_rejects_private_loopback_reserved(self):
        for address in ("127.0.0.1", "10.0.0.1", "169.254.1.1", "192.0.2.1", "::1", "fc00::1"):
            self.assertFalse(_public_address(address))
        self.assertTrue(_public_address("8.8.8.8"))

    def test_url_allowlist_rejects_private_or_credential_hosts(self):
        for url in ("http://api.github.com/x", "https://127.0.0.1/x", "https://api.github.com.evil.test/x",
                    "https://api.github.com:443/x", "https://user@api.github.com/x",
                    "https://api.github.com/x\r\nX-Evil: yes"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                _allowed_url(url)
        self.assertEqual(_allowed_url(REDIRECT)[0], "productionresultssa.blob.core.windows.net")

    def test_resolved_private_peer_is_rejected_before_connect(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), \
             patch("socket.socket") as socket_factory:
            with self.assertRaises(OSError):
                _read_https("https://api.github.com/x", {}, time.monotonic() + 1, 100)
        socket_factory.assert_not_called()

    def test_isolated_redirect_drops_token_and_reads_log(self):
        transport = GitHubCILogTransport(fetch=_fixture_fetch)
        result = GitHubCILogReader(request=transport)("owner/repo", 12, token="fixture-token")
        self.assertEqual(result.coverage, "complete")
        self.assertEqual(result.text, "step failed\n")

    def test_blocked_child_exits_by_parent_deadline(self):
        transport = GitHubCILogTransport(fetch=_slow_fetch)
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            transport("https://api.github.com/repos/owner/repo/actions/jobs/12/logs",
                headers={}, stream=True, allow_redirects=False, deadline=start + 0.5)
        self.assertLess(time.monotonic() - start, 2.5)

    def test_large_bounded_response_returns_without_pipe_deadlock(self):
        response = GitHubCILogTransport(fetch=_large_fetch)(
            "https://api.github.com/repos/owner/repo/actions/jobs/12/logs",
            headers={}, stream=True, allow_redirects=False, deadline=time.monotonic() + 5)
        self.assertEqual(len(response.body), 4 * 1024 * 1024)

    def test_reader_marks_blocked_child_as_deadline(self):
        result = GitHubCILogReader(request=GitHubCILogTransport(fetch=_slow_fetch),
                                   total_seconds=0.5)("owner/repo", 12, token="fixture-token")
        self.assertEqual(result.coverage, "unavailable")
        self.assertEqual(result.reason, "deadline")


if __name__ == "__main__":
    unittest.main()
