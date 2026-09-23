"""Python Worker API-key tokens must use Workers Web Crypto bytes."""
import base64
import sys
from types import SimpleNamespace

from pullwise_server.cloudflare_api_key_write import _new_api_token


def test_worker_token_generation_uses_web_crypto(monkeypatch):
    class FakeArray:
        def __init__(self, length):
            self.values = [0] * length

        def __getitem__(self, index):
            return self.values[index]

    def fill(view):
        view.values[:] = [0xA5] * len(view.values)
        return view

    monkeypatch.setitem(sys.modules, "js", SimpleNamespace(
        Uint8Array=SimpleNamespace(new=FakeArray),
        crypto=SimpleNamespace(getRandomValues=fill)))
    token = _new_api_token()
    encoded = token.removeprefix("pwk_")
    assert token.startswith("pwk_")
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)) == bytes([0xA5] * 32)
