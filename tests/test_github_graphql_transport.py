from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from pullwise_server.github_transport import GitHubGraphQLTransport, GitHubUnavailable


class GitHubGraphQLTransportTest(unittest.TestCase):
    def response(self, status=200, payload=None, headers=None):
        body = json.dumps(payload if payload is not None else {"data": {}}).encode()
        result = Mock(status_code=status, headers=headers or {})
        result.iter_content.return_value = [body]
        return result

    def test_posts_only_fixed_graphql_endpoint_with_bounded_request(self):
        response = self.response(payload={"data": {"repository": {"databaseId": 123}}})
        request = Mock(return_value=response)
        transport = GitHubGraphQLTransport(request=request)
        result = transport("query Q($id: Int!) { repository { databaseId } }",
                           variables={"id": 123}, token="fixture-token")
        self.assertEqual(result.payload["data"]["repository"]["databaseId"], 123)
        args, kwargs = request.call_args
        self.assertEqual(args, ("https://api.github.com/graphql",))
        self.assertEqual(json.loads(kwargs["data"])["variables"], {"id": 123})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fixture-token")
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["stream"])
        response.close.assert_called_once()

    def test_rejects_redirect_oversize_body_and_invalid_input(self):
        for response in (self.response(status=302), self.response(headers={"Content-Length": "2000000"})):
            with self.assertRaises(GitHubUnavailable):
                GitHubGraphQLTransport(request=Mock(return_value=response))(
                    "query Q { viewer { id } }", variables={}, token="fixture-token")
            response.close.assert_called_once()
        request = Mock()
        transport = GitHubGraphQLTransport(request=request)
        for query, variables, token in (("", {}, "token"), ("mutation Q { deleteThing }", {}, "token"),
                                        ("query Q { viewer { id } }", [], "token"),
                                        ("query Q { viewer { id } }", {}, "bad\ntoken")):
            with self.assertRaises(GitHubUnavailable):
                transport(query, variables=variables, token=token)
        request.assert_not_called()

    def test_rate_limit_retry_is_preserved_without_exposing_payload(self):
        response = self.response(status=429, payload={"message": "private response"},
                                 headers={"Retry-After": "120"})
        transport = GitHubGraphQLTransport(request=Mock(return_value=response), clock=lambda: 100)
        with self.assertRaises(GitHubUnavailable) as captured:
            transport("query Q { viewer { id } }", variables={}, token="fixture-token")
        self.assertEqual(captured.exception.retry_at, 220)
        self.assertNotIn("private", str(captured.exception))


if __name__ == "__main__":
    unittest.main()
