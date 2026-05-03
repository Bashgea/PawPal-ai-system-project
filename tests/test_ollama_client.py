"""Unit tests for OllamaClient — transport retries, JSON re-ask, error handling,
localhost normalisation, proxy bypass, and health_check."""

import json
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from ai.ollama_client import (
    MalformedResponseError,
    ModelUnavailableError,
    OllamaClient,
    _normalize_host,
    _no_proxy_opener,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _client(max_retries: int = 2) -> OllamaClient:
    """OllamaClient with short timeouts, controllable retry count, and a fresh
    mock opener injected so tests don't touch the real network."""
    c = OllamaClient(
        host        = "http://localhost:11434",
        model       = "test-model",
        timeout     = 5,
        max_retries = max_retries,
    )
    return c


def _ok_generate(text: str = '{"ordered_task_ids": []}') -> MagicMock:
    """Context-manager mock for a successful /api/generate response.

    ``with opener.open(req, timeout=...) as resp:`` calls ``__enter__()``
    which returns the mock itself; ``mock.read()`` returns the JSON payload.
    """
    m = MagicMock()
    m.__enter__.return_value = m
    m.read.return_value = json.dumps({"response": text}).encode()
    return m


def _ok_tags(models: list[str] | None = None) -> MagicMock:
    """Context-manager mock for a successful /api/tags response."""
    m = MagicMock()
    m.__enter__.return_value = m
    m.read.return_value = json.dumps({
        "models": [{"name": n} for n in (models or ["test-model:latest"])]
    }).encode()
    return m


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://127.0.0.1:11434/api/generate", code, "err", {}, None)


def _mock_opener(*side_effects) -> MagicMock:
    """Return a mock opener whose .open() yields *side_effects* in sequence."""
    opener = MagicMock()
    opener.open.side_effect = list(side_effects)
    return opener


# ── _normalize_host ───────────────────────────────────────────────────────────

def test_normalize_host_replaces_localhost():
    """localhost should become 127.0.0.1 to avoid Windows IPv6 delay."""
    assert _normalize_host("http://localhost:11434") == "http://127.0.0.1:11434"


def test_normalize_host_leaves_ip_unchanged():
    assert _normalize_host("http://127.0.0.1:11434") == "http://127.0.0.1:11434"


def test_normalize_host_leaves_remote_unchanged():
    assert _normalize_host("http://192.168.1.10:11434") == "http://192.168.1.10:11434"


def test_client_normalizes_localhost_in_init():
    """OllamaClient.__init__ must normalize 'localhost' to '127.0.0.1'."""
    c = OllamaClient(host="http://localhost:11434", model="m", timeout=5, max_retries=0)
    assert c._host == "http://127.0.0.1:11434"


# ── _no_proxy_opener ──────────────────────────────────────────────────────────

def test_no_proxy_opener_returns_opener_director():
    opener = _no_proxy_opener()
    assert isinstance(opener, __import__("urllib.request", fromlist=["OpenerDirector"]).OpenerDirector)


def test_client_uses_proxy_bypassing_opener():
    """The opener stored on the client must have been created by _no_proxy_opener,
    meaning ProxyHandler({}) was included — we verify by checking no proxy
    handlers with a non-empty proxies dict are present."""
    c = OllamaClient(host="http://127.0.0.1:11434", model="m", timeout=5, max_retries=0)
    # All ProxyHandlers in the opener must have empty proxy dicts (bypass mode).
    from urllib.request import ProxyHandler
    for handler in c._opener.handlers:
        if isinstance(handler, ProxyHandler):
            assert handler.proxies == {}, (
                "ProxyHandler in opener has non-empty proxies — system proxy not bypassed."
            )


# ── health_check ──────────────────────────────────────────────────────────────

@patch("ai.ollama_client._no_proxy_opener")
def test_health_check_reachable(mock_opener_factory):
    mock_opener_factory.return_value = _mock_opener(_ok_tags(["llama3.1:8b", "mistral:7b"]))

    result = _client().health_check()

    assert result["reachable"] is True
    assert result["latency_ms"] >= 0
    assert "llama3.1:8b" in result["models"]


@patch("ai.ollama_client._no_proxy_opener")
def test_health_check_unreachable(mock_opener_factory):
    mock_opener = MagicMock()
    mock_opener.open.side_effect = urllib.error.URLError("connection refused")
    mock_opener_factory.return_value = mock_opener

    result = _client().health_check()

    assert result["reachable"] is False
    assert "error" in result
    assert result["latency_ms"] >= 0


# ── complete_json fast-fail when Ollama is down ───────────────────────────────

@patch("ai.ollama_client._no_proxy_opener")
def test_complete_json_fast_fails_when_health_check_fails(mock_opener_factory):
    """complete_json must raise ModelUnavailableError immediately when the
    health check fails, without attempting the slow generate call."""
    mock_opener = MagicMock()
    # health_check call → URLError; generate call must never happen
    mock_opener.open.side_effect = urllib.error.URLError("refused")
    mock_opener_factory.return_value = mock_opener

    with pytest.raises(ModelUnavailableError, match="not reachable"):
        _client(max_retries=2).complete_json("ping")

    # Only 1 call total: the /api/tags health check. No generate retries.
    assert mock_opener.open.call_count == 1


# ── Transport retry ───────────────────────────────────────────────────────────

@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_transport_retry_eventually_succeeds(mock_opener_factory, mock_sleep):
    """URLError on the first two generate attempts, then success."""
    transport_err = urllib.error.URLError("connection refused")
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(),              # health_check
        transport_err,           # generate attempt 1
        transport_err,           # generate attempt 2
        _ok_generate(),          # generate attempt 3 → success
    )

    result = _client(max_retries=2).complete_json("ping")

    assert result == {"ordered_task_ids": []}
    assert mock_opener_factory.return_value.open.call_count == 4  # 1 health + 3 generate
    assert mock_sleep.call_count == 2                              # slept before retries 2 & 3


@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_transport_retry_exhausted_raises_model_unavailable(mock_opener_factory, mock_sleep):
    """After max_retries+1 generate attempts all failing, raise ModelUnavailableError."""
    transport_err = urllib.error.URLError("refused")
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(),         # health_check passes
        transport_err,      # attempt 1
        transport_err,      # attempt 2
        transport_err,      # attempt 3
    )

    with pytest.raises(ModelUnavailableError):
        _client(max_retries=2).complete_json("ping")

    assert mock_opener_factory.return_value.open.call_count == 4
    assert mock_sleep.call_count == 2


@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_transport_retry_uses_exponential_backoff(mock_opener_factory, mock_sleep):
    """Retry delays follow 1 s, 2 s, … pattern (2^(attempt-1))."""
    transport_err = urllib.error.URLError("refused")
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(), transport_err, transport_err, transport_err,
    )

    with pytest.raises(ModelUnavailableError):
        _client(max_retries=2).complete_json("ping")

    sleep_delays = [c.args[0] for c in mock_sleep.call_args_list]
    assert sleep_delays == [1, 2]


@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_retryable_http_status_retried(mock_opener_factory, mock_sleep):
    """HTTP 503 is retried; success on the third generate attempt."""
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(),
        _http_error(503),
        _http_error(503),
        _ok_generate(),
    )

    result = _client(max_retries=2).complete_json("ping")

    assert result == {"ordered_task_ids": []}
    assert mock_opener_factory.return_value.open.call_count == 4


# ── 404 is NOT retried ────────────────────────────────────────────────────────

@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_404_raises_immediately_no_retry(mock_opener_factory, mock_sleep):
    """HTTP 404 (model not pulled) must raise ModelUnavailableError without retrying."""
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(),
        _http_error(404),
    )

    with pytest.raises(ModelUnavailableError, match="not pulled"):
        _client(max_retries=2).complete_json("ping")

    assert mock_opener_factory.return_value.open.call_count == 2  # health + 1 generate
    mock_sleep.assert_not_called()


# ── Non-retryable HTTP errors ─────────────────────────────────────────────────

@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_non_retryable_http_error_raises_immediately(mock_opener_factory, mock_sleep):
    """HTTP 400 (bad request) is not retryable and raises immediately."""
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(),
        _http_error(400),
    )

    with pytest.raises(ModelUnavailableError, match="HTTP 400"):
        _client(max_retries=2).complete_json("ping")

    assert mock_opener_factory.return_value.open.call_count == 2
    mock_sleep.assert_not_called()


# ── JSON re-ask (complete_json layer) ────────────────────────────────────────

@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_json_reask_on_malformed_then_valid(mock_opener_factory, mock_sleep):
    """First generate returns malformed JSON; second (re-ask) is valid."""
    valid_json = '{"ordered_task_ids": ["x"]}'
    mock_opener_factory.return_value = _mock_opener(
        _ok_tags(),
        _ok_generate("not valid json {{{{"),
        _ok_generate(valid_json),
    )

    result = _client(max_retries=1).complete_json("ping")

    assert result == {"ordered_task_ids": ["x"]}
    assert mock_opener_factory.return_value.open.call_count == 3  # health + 2 generate


@patch("time.sleep")
@patch("ai.ollama_client._no_proxy_opener")
def test_json_reask_exhausted_raises_malformed(mock_opener_factory, mock_sleep):
    """All re-asks return bad JSON — MalformedResponseError is raised."""
    mock_opener = MagicMock()
    # health_check → ok; all generate calls → bad JSON
    mock_opener.open.side_effect = [
        _ok_tags(),
        _ok_generate("still not json ~~~~"),
        _ok_generate("still not json ~~~~"),
    ]
    mock_opener_factory.return_value = mock_opener

    with pytest.raises(MalformedResponseError):
        _client(max_retries=1).complete_json("ping")
