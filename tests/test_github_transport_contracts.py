from __future__ import annotations

import unittest
from unittest.mock import Mock

from pullwise_server.github_transport import GitHubRESTTransport, GitHubUnavailable


class GitHubTransportContractsTest(unittest.TestCase):
    def setUp(self):
        self.response = Mock(status_code=200, headers={"Content-Type": "application/json"})
        self.response.iter_content.return_value = [b'{"id":123}']
        self.request = Mock(return_value=self.response)
        self.transport = GitHubRESTTransport(request=self.request, clock=lambda: 1000)

    def test_fixed_origin_get_is_streamed_and_never_follows_redirects(self):
        result = self.transport("/repositories/123", token="fixture-token")
        self.assertEqual(result.payload, {"id": 123})
        self.assertEqual(result.status, 200)
        args, kwargs = self.request.call_args
        self.assertEqual(args, ("https://api.github.com/repositories/123",))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fixture-token")
        self.assertEqual(kwargs["timeout"], (5, 10))
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.response.close.assert_called_once()

    def test_path_rejects_external_origins_traversal_and_credentials(self):
        for path in ("https://evil.test/", "//evil.test/", "/../user", "/repos/%2e%2e/user",
                     "/repos/%252e%252e/user", "/user#fragment", "/user\\other", "/user\r\nx:y",
                     "/user?access_token=secret", "/user?token=secret", "/user?client_secret=secret"):
            with self.subTest(path=path), self.assertRaises(GitHubUnavailable):
                self.transport(path, token="fixture")
        self.request.assert_not_called()

    def test_token_is_optional_but_must_not_inject_headers(self):
        self.transport("/repositories/123", token="")
        self.assertNotIn("Authorization", self.request.call_args.kwargs["headers"])
        self.request.reset_mock()
        for token in (None, "a\r\nX: a", "Bearer fixture", " fixture "):
            with self.subTest(token=token), self.assertRaises(GitHubUnavailable):
                self.transport("/user", token=token)
        self.request.assert_not_called()

    def test_decoded_body_size_is_bounded_even_without_content_length(self):
        transport = GitHubRESTTransport(request=self.request, max_body_bytes=16)
        self.response.iter_content.return_value = [b" " * 8, b" " * 9]
        with self.assertRaises(GitHubUnavailable):
            transport("/user", token="fixture")
        self.response.close.assert_called_once()

    def test_oversized_content_length_rejected_before_reading(self):
        self.response.headers["Content-Length"] = "1048577"
        with self.assertRaises(GitHubUnavailable):
            self.transport("/user", token="fixture")
        self.response.iter_content.assert_not_called()
        self.response.close.assert_called_once()

    def test_no_redirect_or_error_body_becomes_valid_authority(self):
        for status in (301, 302, 307, 500, 503):
            self.response.status_code = status
            with self.subTest(status=status), self.assertRaises(GitHubUnavailable):
                self.transport("/user", token="fixture")

    def test_denial_status_is_preserved_for_domain_interpretation(self):
        for status in (401, 403, 404):
            self.response.status_code = status
            with self.subTest(status=status):
                result = self.transport("/user", token="fixture")
                self.assertEqual(result.status, status)

    def test_malformed_or_non_json_body_is_unavailable(self):
        for body in (b"<html>secret</html>", b'{"x":NaN}', b'[] trailing', b'\xff'):
            self.response.iter_content.return_value = [body]
            with self.subTest(body=body), self.assertRaises(GitHubUnavailable) as caught:
                self.transport("/user", token="fixture-token")
            self.assertNotIn("secret", str(caught.exception))

    def test_only_pagination_and_rate_metadata_are_returned(self):
        self.response.headers.update({"ETag": '"v1"', "Link": '<https://api.github.com/repos/a/b/releases?page=2>; rel="next"',
                                      "Set-Cookie": "private", "X-Debug-Token": "private"})
        result = self.transport("/repos/a/b/releases?per_page=100&page=1", token="fixture")
        self.assertEqual(result.headers["etag"], '"v1"')
        self.assertIn("link", result.headers)
        self.assertNotIn("set-cookie", result.headers)
        self.assertNotIn("x-debug-token", result.headers)

    def test_rate_limit_uses_later_retry_after_or_reset_without_sleeping(self):
        self.response.status_code = 403
        self.response.headers.update({"Retry-After": "120", "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1500"})
        with self.assertRaises(GitHubUnavailable) as caught:
            self.transport("/user", token="fixture")
        self.assertEqual(caught.exception.retry_at, 1500)
        self.request.assert_called_once()

    def test_missing_or_invalid_rate_limit_metadata_has_minimum_backoff(self):
        self.response.status_code = 429
        self.response.headers["Retry-After"] = "broken"
        with self.assertRaises(GitHubUnavailable) as caught:
            self.transport("/user", token="fixture")
        self.assertEqual(caught.exception.retry_at, 1060)

    def test_network_exception_is_sanitized_and_not_retried(self):
        self.request.side_effect = RuntimeError("fixture-token PRIVATE-BODY")
        with self.assertRaises(GitHubUnavailable) as caught:
            self.transport("/user", token="fixture-token")
        self.assertNotIn("fixture-token", str(caught.exception))
        self.assertNotIn("PRIVATE-BODY", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)
        self.request.assert_called_once()

    def test_stream_exception_closes_response(self):
        self.response.iter_content.side_effect = RuntimeError("private")
        with self.assertRaises(GitHubUnavailable):
            self.transport("/user", token="fixture")
        self.response.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
