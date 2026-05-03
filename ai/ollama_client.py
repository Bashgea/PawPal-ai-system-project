"""ai/ollama_client.py — HTTP wrapper around Ollama. Retries, timeouts, JSON mode."""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

import config

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying (rate-limit, transient server errors).
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}

# Short timeout used only for /api/tags reachability checks.
_HEALTH_TIMEOUT_S = 5


# ── Module-level transport helpers ────────────────────────────────────────────

def _normalize_host(host: str) -> str:
    """Replace ``localhost`` with ``127.0.0.1`` to avoid a 2-second IPv6 penalty.

    On Windows, ``getaddrinfo("localhost", ...)`` returns ``::1`` (AF_INET6)
    **before** ``127.0.0.1`` (AF_INET).  Ollama's default listener is IPv4-only,
    so the OS attempts a TCP SYN to ``::1:11434``, waits ~2 s for a RST, and
    only then falls back to the IPv4 address.  With the default three retries
    this wastes 6+ seconds on every call — even when Ollama is healthy.

    Replacing ``localhost`` with ``127.0.0.1`` skips the IPv6 probe entirely.
    """
    return host.replace("//localhost:", "//127.0.0.1:")


def _no_proxy_opener() -> urllib.request.OpenerDirector:
    """Return an opener that bypasses all system/env proxies.

    ``urllib.request`` automatically consults ``HTTP_PROXY`` / ``HTTPS_PROXY``
    environment variables and, on Windows, the IE/registry proxy settings.
    Routing a request to ``localhost`` through a corporate proxy fails silently
    or times out.  ``ProxyHandler({})`` disables all proxy routing without
    touching global state.
    """
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


# ── Exceptions ────────────────────────────────────────────────────────────────

class ModelUnavailableError(Exception):
    """Ollama is unreachable or the model has not been pulled."""


class MalformedResponseError(Exception):
    """The model returned something that is not valid JSON after all retries."""


# ── Client ────────────────────────────────────────────────────────────────────

class OllamaClient:
    """Sends prompts to Ollama and returns parsed JSON.

    Args:
        host:        Ollama base URL (default: config.OLLAMA_HOST).
        model:       Model name (default: config.PAWPAL_MODEL).
        timeout:     Read/response timeout in seconds (default: config.MODEL_TIMEOUT_S).
                     Covers the entire round-trip including model generation.  Set
                     higher (e.g. 180) for cold model loads on slower hardware.
        max_retries: Max transport-level retries (default: config.MODEL_MAX_RETRIES).
    """

    def __init__(
        self,
        host:        str | None = None,
        model:       str | None = None,
        timeout:     int | None = None,
        max_retries: int | None = None,
    ) -> None:
        # Normalize localhost → 127.0.0.1 to avoid the Windows IPv6 delay.
        self._host        = _normalize_host(host or config.OLLAMA_HOST)
        self._model       = model       or config.PAWPAL_MODEL
        self._timeout     = timeout     or config.MODEL_TIMEOUT_S
        self._max_retries = max_retries or config.MODEL_MAX_RETRIES
        self._opener      = _no_proxy_opener()

    # ── Public API ────────────────────────────────────────────────────────────

    def health_check(self, timeout: int = _HEALTH_TIMEOUT_S) -> dict[str, Any]:
        """GET ``/api/tags`` with a short timeout to test reachability.

        Args:
            timeout: Seconds to wait (default: 5).

        Returns:
            ``{"reachable": True,  "latency_ms": float, "models": list[str]}``
            ``{"reachable": False, "latency_ms": float, "error": str}``
        """
        url = f"{self._host}/api/tags"
        req = urllib.request.Request(url)
        t0  = time.monotonic()
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                body       = json.loads(resp.read())
                latency_ms = (time.monotonic() - t0) * 1000
                models     = [m.get("name", "") for m in body.get("models", [])]
                logger.debug(
                    "Ollama /api/tags reachable in %.0f ms — %d model(s) available.",
                    latency_ms, len(models),
                )
                return {"reachable": True, "latency_ms": round(latency_ms, 1), "models": models}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            latency_ms = (time.monotonic() - t0) * 1000
            logger.debug("Ollama health check failed in %.0f ms: %s", latency_ms, exc)
            return {"reachable": False, "latency_ms": round(latency_ms, 1), "error": str(exc)}

    def complete_json(self, prompt: str) -> dict:
        """Send *prompt* and return the parsed JSON response dict.

        Performs a fast reachability check first so callers fail in <5 s when
        Ollama is not running, rather than waiting through all retries.

        Raises:
            ModelUnavailableError: Ollama is down or the model is not pulled.
            MalformedResponseError: JSON could not be parsed after all retries.
        """
        # Fast path: confirm Ollama is reachable before the slow generation call.
        hc = self.health_check()
        if not hc["reachable"]:
            logger.warning(
                "Ollama not reachable at %s (health check: %s). Run: ollama serve",
                self._host, hc["error"],
            )
            raise ModelUnavailableError(
                f"Ollama not reachable at {self._host}: {hc['error']}"
            )

        raw = self._call(prompt)
        for attempt in range(self._max_retries + 1):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                if attempt == self._max_retries:
                    logger.error(
                        "All %d JSON re-ask(s) exhausted for model %r — still not valid JSON.",
                        self._max_retries, self._model,
                    )
                    raise MalformedResponseError("Model did not return valid JSON.")
                logger.warning(
                    "Malformed JSON from %r on attempt %d — re-asking with stricter prompt.",
                    self._model, attempt + 1,
                )
                raw = self._call(prompt + "\n\nIMPORTANT: Return ONLY raw JSON. No markdown, no prose.")

        raise MalformedResponseError("Model did not return valid JSON.")

    def complete(self, prompt: str) -> str:
        """Send *prompt* and return the raw text response.

        Raises:
            ModelUnavailableError: Ollama is down or the model is not pulled.
        """
        return self._call(prompt)

    # ── Internal transport ────────────────────────────────────────────────────

    def _call(self, prompt: str) -> str:
        """POST to ``/api/generate`` with transport-level retries and exponential backoff.

        Uses a proxy-bypassing opener so system/env proxies never intercept
        local Ollama traffic.  The host has already been normalized to
        ``127.0.0.1`` in ``__init__``, avoiding the Windows IPv6 delay.

        Retries on transient errors (URLError, TimeoutError, OSError) and
        retryable HTTP statuses (429, 5xx).  Raises immediately on 404 (model
        not pulled) and other non-retryable errors.

        Raises:
            ModelUnavailableError: after all retries exhausted, or immediately on 404.
        """
        url     = f"{self._host}/api/generate"
        payload = json.dumps({
            "model":  self._model,
            "prompt": prompt,
            "format": "json",
            "stream": False,
        }).encode()

        req = urllib.request.Request(
            url,
            data    = payload,
            headers = {"Content-Type": "application/json"},
        )

        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            if attempt > 0:
                delay = 2 ** (attempt - 1)  # 1 s, 2 s, 4 s, …
                logger.warning(
                    "Transport error — retrying in %ds (attempt %d/%d, model=%r, host=%s).",
                    delay, attempt + 1, self._max_retries + 1, self._model, self._host,
                )
                time.sleep(delay)
            t0 = time.monotonic()
            try:
                with self._opener.open(req, timeout=self._timeout) as resp:
                    body    = json.loads(resp.read())
                    elapsed = (time.monotonic() - t0) * 1000
                    logger.debug(
                        "Ollama generate completed in %.0f ms (model=%r).",
                        elapsed, self._model,
                    )
                    return body.get("response", "")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    logger.error(
                        "Model %r not found at %s. Run: ollama pull %s",
                        self._model, self._host, self._model,
                    )
                    raise ModelUnavailableError(f"Model {self._model!r} not pulled.") from exc
                if exc.code not in _RETRYABLE_HTTP_STATUSES:
                    logger.error(
                        "Ollama returned HTTP %d from %s (model=%r): %s",
                        exc.code, self._host, self._model, exc.reason,
                    )
                    raise ModelUnavailableError(f"Ollama returned HTTP {exc.code}.") from exc
                logger.warning(
                    "Ollama HTTP %d — will retry (model=%r, host=%s).",
                    exc.code, self._model, self._host,
                )
                last_exc = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                elapsed = (time.monotonic() - t0) * 1000
                logger.warning(
                    "Transport error after %.0f ms talking to Ollama "
                    "(timeout=%ds, model=%r, host=%s): %s",
                    elapsed, self._timeout, self._model, self._host, exc,
                )
                last_exc = exc

        logger.error(
            "Cannot reach Ollama at %s after %d attempt(s) "
            "(model=%r, timeout=%ds). Run: ollama serve",
            self._host, self._max_retries + 1, self._model, self._timeout,
        )
        raise ModelUnavailableError(
            f"Cannot reach Ollama at {self._host} after {self._max_retries + 1} attempt(s) "
            f"(timeout={self._timeout}s)."
        ) from last_exc


# ── Diagnostic entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    """Quick connectivity diagnostic — run from the same venv as the app:

        python -m ai.ollama_client
    """
    import socket
    import sys

    logging.basicConfig(level=logging.DEBUG, format="%(levelname)-8s %(name)s — %(message)s")

    host_raw  = config.OLLAMA_HOST
    host_norm = _normalize_host(host_raw)
    model     = config.PAWPAL_MODEL
    timeout   = config.MODEL_TIMEOUT_S

    print(f"\n{'='*55}")
    print(f" PawPal+ Ollama diagnostic")
    print(f"{'='*55}")
    print(f" OLLAMA_HOST      : {host_raw}  →  normalized: {host_norm}")
    print(f" PAWPAL_MODEL     : {model}")
    print(f" MODEL_TIMEOUT_S  : {timeout}s")

    # ── 1. Proxy env vars ─────────────────────────────────────────────────────
    print(f"\n{'─'*45}")
    print(" Proxy environment variables")
    print(f"{'─'*45}")
    _proxy_vars = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy")
    _found_proxies = [(v, os.environ[v]) for v in _proxy_vars if v in os.environ]
    if _found_proxies:
        for var, val in _found_proxies:
            print(f"  {var} = {val}")
        print("  ⚠ Proxy detected — PawPalClient bypasses it via ProxyHandler({})")
    else:
        print("  (none set) — OK")

    # ── 2. localhost resolution ───────────────────────────────────────────────
    print(f"\n{'─'*45}")
    print(" localhost address resolution")
    print(f"{'─'*45}")
    try:
        _infos = socket.getaddrinfo("localhost", 11434, socket.AF_UNSPEC, socket.SOCK_STREAM)
        for _info in _infos:
            _fam  = _info[0].name if hasattr(_info[0], "name") else str(_info[0])
            _addr = _info[4][0]
            _note = "← first attempt (may incur delay if IPv6)" if ":" in _addr else "← IPv4"
            print(f"  localhost → {_addr:20s} {_fam:10s}  {_note}")
    except socket.gaierror as e:
        print(f"  getaddrinfo error: {e}")

    # ── 3. Health check ───────────────────────────────────────────────────────
    print(f"\n{'─'*45}")
    print(f" Ollama /api/tags  (timeout={_HEALTH_TIMEOUT_S}s)")
    print(f"{'─'*45}")
    _client = OllamaClient()
    _hc     = _client.health_check()
    if _hc["reachable"]:
        print(f"  ✓ Reachable in {_hc['latency_ms']:.0f} ms")
        _models = _hc.get("models", [])
        if _models:
            for _m in _models:
                _mark = "✓" if _m == model or _m.startswith(model.split(":")[0]) else " "
                print(f"    [{_mark}] {_m}")
        else:
            print("  (no models listed — run: ollama pull llama3.1:8b)")
    else:
        print(f"  ✗ Not reachable in {_hc['latency_ms']:.0f} ms: {_hc['error']}")
        print("  → Is 'ollama serve' running?")
        print(f"  → Is {host_norm} the correct address?")
        sys.exit(1)

    # ── 4. Minimal generate ───────────────────────────────────────────────────
    print(f"\n{'─'*45}")
    print(f" Minimal generate  (stream=false, format=json, timeout={timeout}s)")
    print(f"{'─'*45}")
    _t0 = time.monotonic()
    try:
        _result = _client.complete_json('Respond with {"ok": true} and nothing else.')
        _elapsed = time.monotonic() - _t0
        print(f"  ✓ Response in {_elapsed:.1f}s: {_result}")
        if _elapsed > timeout * 0.8:
            print(f"  ⚠ Response took {_elapsed:.0f}s — close to timeout ({timeout}s).")
            print(f"    Consider increasing MODEL_TIMEOUT_S in .env (e.g. 180).")
    except (ModelUnavailableError, MalformedResponseError) as e:
        _elapsed = time.monotonic() - _t0
        print(f"  ✗ Failed after {_elapsed:.1f}s: {e}")
        if _elapsed >= timeout:
            print(f"  → Timeout ({timeout}s) hit during generation.")
            print(f"    Increase MODEL_TIMEOUT_S in .env (try 180 for cold loads).")

    print(f"\n{'='*55}\n")
