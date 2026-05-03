"""Unit tests for OllamaClient — transport retries, JSON re-ask, error handling."""

import json
import urllib.error
from unittest.mock import MagicMock, call, patch

import pytest

from ai.ollama_client import MalformedResponseError, ModelUnavailableError, OllamaClient


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client(max_retries: int = 2) -> OllamaClient:
    """OllamaClient with short timeouts and controllable retry count."""
    return OllamaClient(
        host        = "http://localhost:11434",
        model       = "test-model",
        timeout     = 5,
        max_retries = max_retries,
    )


def _ok_response(text: str = '{"ordered_task_ids": []}') -> MagicMock:
    """Fake urlopen context-manager whose .read() returns a valid Ollama response.

    `with urlopen(...) as resp:` calls `__enter__()` to obtain `resp`.
    We set `__enter__.return_value = m` so resp is the same mock we configured.
    """
    m = MagicMock()
    m.__enter__.return_value = m   # `with m as resp:` → resp is m, not a child mock
    m.read.return_value = json.dumps({"response": text}).encode()
    return m


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://localhost:11434/api/generate", code, "err", {}, None)


# ── Transport retry ───────────────────────────────────────────────────────────

@patch("time.sleep")                          # suppress actual sleeps in tests
@patch("urllib.request.urlopen")
def test_transport_retry_eventually_succeeds(mock_urlopen, mock_sleep):
    """URLError on the first two attempts, then success — complete_json returns parsed dict."""
    transport_err = urllib.error.URLError("connection refused")
    mock_urlopen.side_effect = [transport_err, transport_err, _ok_response()]

    result = _client(max_retries=2).complete_json("ping")

    assert result == {"ordered_task_ids": []}
    assert mock_urlopen.call_count == 3           # initial + 2 retries
    assert mock_sleep.call_count == 2             # slept before each retry


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_transport_retry_exhausted_raises_model_unavailable(mock_urlopen, mock_sleep):
    """After max_retries+1 attempts all failing with URLError, raise ModelUnavailableError."""
    mock_urlopen.side_effect = urllib.error.URLError("refused")

    with pytest.raises(ModelUnavailableError):
        _client(max_retries=2).complete_json("ping")

    assert mock_urlopen.call_count == 3           # 1 initial + 2 retries
    assert mock_sleep.call_count == 2


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_transport_retry_uses_exponential_backoff(mock_urlopen, mock_sleep):
    """Retry delays follow 1 s, 2 s, … pattern (2^(attempt-1))."""
    mock_urlopen.side_effect = urllib.error.URLError("refused")

    with pytest.raises(ModelUnavailableError):
        _client(max_retries=2).complete_json("ping")

    sleep_delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_delays == [1, 2]                 # 2^0, 2^1


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_retryable_http_status_retried(mock_urlopen, mock_sleep):
    """HTTP 503 is retried; success on the third call returns the parsed dict."""
    mock_urlopen.side_effect = [_http_error(503), _http_error(503), _ok_response()]

    result = _client(max_retries=2).complete_json("ping")

    assert result == {"ordered_task_ids": []}
    assert mock_urlopen.call_count == 3


# ── 404 is NOT retried ────────────────────────────────────────────────────────

@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_404_raises_immediately_no_retry(mock_urlopen, mock_sleep):
    """HTTP 404 (model not pulled) must raise ModelUnavailableError without retrying."""
    mock_urlopen.side_effect = _http_error(404)

    with pytest.raises(ModelUnavailableError, match="not pulled"):
        _client(max_retries=2).complete_json("ping")

    assert mock_urlopen.call_count == 1           # no retry
    mock_sleep.assert_not_called()


# ── Non-retryable HTTP errors ─────────────────────────────────────────────────

@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_non_retryable_http_error_raises_immediately(mock_urlopen, mock_sleep):
    """HTTP 400 (bad request) is not retryable and raises immediately."""
    mock_urlopen.side_effect = _http_error(400)

    with pytest.raises(ModelUnavailableError, match="HTTP 400"):
        _client(max_retries=2).complete_json("ping")

    assert mock_urlopen.call_count == 1
    mock_sleep.assert_not_called()


# ── JSON re-ask (complete_json layer) ────────────────────────────────────────

@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_json_reask_on_malformed_then_valid(mock_urlopen, mock_sleep):
    """First response is not JSON; second (re-ask) is valid — complete_json returns parsed dict."""
    valid_json = '{"ordered_task_ids": ["x"]}'
    mock_urlopen.side_effect = [
        _ok_response("not valid json {{{{"),   # first call: malformed
        _ok_response(valid_json),              # re-ask: valid
    ]

    result = _client(max_retries=1).complete_json("ping")

    assert result == {"ordered_task_ids": ["x"]}
    assert mock_urlopen.call_count == 2


@patch("time.sleep")
@patch("urllib.request.urlopen")
def test_json_reask_exhausted_raises_malformed(mock_urlopen, mock_sleep):
    """All re-asks return bad JSON — MalformedResponseError is raised."""
    mock_urlopen.return_value = _ok_response("still not json ~~~~")

    with pytest.raises(MalformedResponseError):
        _client(max_retries=1).complete_json("ping")
