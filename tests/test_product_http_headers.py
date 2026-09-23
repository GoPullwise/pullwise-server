from http.client import HTTPMessage
from types import SimpleNamespace

from pullwise_server.product_api import _if_match_revision, _header


def test_real_http_message_preserves_revision_and_idempotency_headers():
    headers = HTTPMessage()
    headers["if-match"] = '"7"'
    headers["idempotency-key"] = "same-operation"
    handler = SimpleNamespace(headers=headers)
    assert _if_match_revision(handler) == 7
    assert _header(handler, "Idempotency-Key") == "same-operation"
