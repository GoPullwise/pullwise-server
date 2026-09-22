import pytest

from pullwise_server.github_ci_logs import CILogResult, GitHubCILogReader, redact_ci_log, select_ci_log_windows
from pullwise_server.github_transport import GitHubUnavailable


URL = "https://productionresultssa1.blob.core.windows.net/logs/job?sig=secret"


class Response:
    def __init__(self, status, headers=None, chunks=()):
        self.status_code = status
        self.headers = headers or {}
        self.chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        yield from self.chunks

    def close(self):
        self.closed = True


def reader_for(*responses, **kwargs):
    calls = []
    def request(url, **options):
        calls.append((url, options))
        response = responses[len(calls) - 1]
        if isinstance(response, Exception):
            raise response
        return response
    return GitHubCILogReader(request=request, **kwargs), calls


def test_single_job_redacts_and_never_forwards_credentials():
    a = Response(302, {"Location": URL, "Set-Cookie": "secret"})
    b = Response(200, {"Content-Type": "text/plain"}, [b"Authorization: Bearer TOPSECRET\nCookie: SECRET\nhttps://u:p@host/a?sig=secret\nfailed\n"])
    reader, calls = reader_for(a, b)
    result = reader("octo/repo", "12", token="TOPSECRET")
    assert result.coverage == "complete"
    assert "failed" in result.text
    assert "secret" not in result.text.lower()
    assert "TOPSECRET" not in repr(result)
    assert calls[0][0] == "https://api.github.com/repos/octo/repo/actions/jobs/12/logs"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer TOPSECRET"
    assert set(calls[1][1]["headers"]) == {"Accept", "Accept-Encoding", "User-Agent"}
    assert all(c[1]["allow_redirects"] is False for c in calls)
    assert a.closed and b.closed


@pytest.mark.parametrize("url", ["http://productionresultssa1.blob.core.windows.net/a", "https://127.0.0.1/a", "https://169.254.169.254/a", "https://localhost/a", "https://evil.com/a", "https://api.github.com/a", "https://productionresultssa1.blob.core.windows.net.evil.com/a", "https://user@productionresultssa1.blob.core.windows.net/a", "https://productionresultssa1.blob.core.windows.net:444/a", "https://other.blob.core.windows.net/a", "https://productionresultssa1.blob.core.windows.net/a#fragment"])
def test_unsafe_location_is_not_requested(url):
    reader, calls = reader_for(Response(302, {"Location": url}))
    assert reader("octo/repo", 12, token="").coverage == "unavailable"
    assert len(calls) == 1


def test_stream_limit_keeps_only_complete_lines():
    reader, _ = reader_for(Response(302, {"Location": URL}), Response(200, chunks=[b"abc\n", b"secretincomplete"]), max_bytes=8)
    result = reader("octo/repo", 12, token="")
    assert (result.text, result.coverage, result.reason) == ("abc\n", "partial", "size_limit")


def test_deadline_and_network_failure_are_sanitized():
    now = [0.0]
    def chunks():
        yield b"first\n"
        now[0] = 21
        yield b"late\n"
    reader, calls = reader_for(Response(302, {"Location": URL}), Response(200, chunks=chunks()), clock=lambda: now[0])
    result = reader("octo/repo", 12, token="")
    assert (result.text, result.coverage, result.reason) == ("first\n", "partial", "deadline")
    assert calls[0][1]["deadline"] == calls[1][1]["deadline"] == 20
    reader, _ = reader_for(RuntimeError(URL))
    assert reader("octo/repo", 12, token="").reason == "unavailable"


def test_redirect_count_bounded_and_all_responses_closed():
    responses = [Response(302, {"Location": URL}) for _ in range(4)]
    reader, calls = reader_for(*responses)
    assert reader("octo/repo", 12, token="").reason == "redirect_limit"
    assert len(calls) == 3
    assert all(r.closed for r in responses[:3])


@pytest.mark.parametrize("name,job,token", [("../repo", 1, ""), ("https://evil/a", 1, ""), ("o/r", True, ""), ("o/r", 0, ""), ("o/r", 1, "x\r\ny")])
def test_invalid_locator_or_token_never_requests(name, job, token):
    reader, calls = reader_for()
    with pytest.raises(ValueError, match="GITHUB_CI_LOG_INPUT_INVALID"):
        reader(name, job, token=token)
    assert not calls


@pytest.mark.parametrize("headers,chunks", [({"Content-Type": "application/zip"}, [b"PK\x03\x04zip"]), ({}, [b"\xff"]), ({"Content-Encoding": "gzip"}, [b"bytes"])])
def test_non_plain_payload_unavailable(headers, chunks):
    reader, _ = reader_for(Response(302, {"Location": URL}), Response(200, headers, chunks))
    assert reader("octo/repo", 1, token="").coverage == "unavailable"


def test_unconfigured_reader_does_no_network():
    assert GitHubCILogReader()("o/r", 1, token="").reason == "transport_unconfigured"


def test_windows_are_bounded_utf8_tail_with_unknown_step_and_no_symptoms():
    result = CILogResult(text="prefix\n" + "错" * 1000 + "\n" + "line\n" * 4000, coverage="complete")
    windows = select_ci_log_windows(result, job_id=12)
    assert len(windows) == 1
    window = windows[0]
    assert len(window["text"].encode("utf-8")) <= 20 * 1024
    assert window["stage"] == "unknown" and window["stepNumber"] is None
    assert window["symptoms"] == [] and window["coverage"] == "partial"
    assert window["endLine"] == 4002 and window["startLine"] == 3
    assert windows == select_ci_log_windows(result, job_id=12)


@pytest.mark.parametrize("result", [CILogResult(), CILogResult(text=" " * 100, coverage="complete"), CILogResult(text="x" * (20 * 1024 + 1), coverage="complete")])
def test_no_truncated_oversized_line_or_missing_evidence(result):
    assert select_ci_log_windows(result, job_id=1) == []


def test_redaction_preserves_source_line_numbers():
    text = "before\n-----BEGIN RSA PRIVATE KEY-----\nabc\ndef\n-----END RSA PRIVATE KEY-----\nafter\n"
    redacted = redact_ci_log(text)
    assert "abc" not in redacted and "def" not in redacted
    assert redacted.count("\n") == text.count("\n")
    assert redacted.splitlines()[5] == "after"


def test_rate_limit_retains_retry_at_and_closes_response():
    response = Response(429, {"Retry-After": "120"})
    reader, calls = reader_for(response, wall_clock=lambda: 1000)
    with pytest.raises(GitHubUnavailable) as raised:
        reader("o/r", 1, token="")
    assert raised.value.retry_at == 1120
    assert response.closed and len(calls) == 1


def test_exact_size_boundary_and_network_failure_after_complete_line():
    reader, _ = reader_for(Response(302, {"Location": URL}), Response(200, chunks=[b"abc\n"]), max_bytes=4)
    assert reader("o/r", 1, token="").coverage == "complete"
    def chunks():
        yield b"safe\nunfinished"
        raise OSError(URL)
    reader, _ = reader_for(Response(302, {"Location": URL}), Response(200, chunks=chunks()))
    result = reader("o/r", 1, token="")
    assert (result.text, result.coverage) == ("safe\n", "partial")
    assert "sig" not in repr(result)


def test_invalid_location_response_body_never_read():
    def chunks():
        raise AssertionError("redirect body consumed")
        yield b""
    reader, _ = reader_for(Response(302, {"Location": URL}, chunks()), Response(200, chunks=[b"ok"]))
    assert reader("o/r", 1, token="").text == "ok"


def test_injected_rate_error_message_is_not_exposed():
    reader, _ = reader_for(GitHubUnavailable(URL, retry_at=1200))
    with pytest.raises(GitHubUnavailable) as raised:
        reader("o/r", 1, token="")
    assert raised.value.retry_at == 1200
    assert "sig" not in repr(raised.value)
