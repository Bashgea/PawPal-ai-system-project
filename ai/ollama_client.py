"""ai/ollama_client.py — HTTP wrapper around Ollama. Retries, timeouts, JSON mode."""

import json
import logging
import time
import urllib.error
import urllib.request

import config

logger = logging.getLogger(__name__)

# HTTP statuses worth retrying (rate-limit, transient server errors).
_RETRYABLE_HTTP_STATUSES = {429, 500, 502, 503, 504}


class ModelUnavailableError(Exception):
    """Ollama is unreachable or the model has not been pulled."""


class MalformedResponseError(Exception):
    """The model returned something that is not valid JSON after all retries."""


class OllamaClient:
    """Sends prompts to Ollama and returns parsed JSON.

    Args:
        host: Ollama base URL (default: config.OLLAMA_HOST).
        model: Model name (default: config.PAWPAL_MODEL).
        timeout: Request timeout in seconds (default: config.MODEL_TIMEOUT_S).
        max_retries: Max retries for transport errors and JSON re-asks (default: config.MODEL_MAX_RETRIES).
    """

    def __init__(
        self,
        host:        str | None = None,
        model:       str | None = None,
        timeout:     int | None = None,
        max_retries: int | None = None,
    ) -> None:
        self._host        = host        or config.OLLAMA_HOST
        self._model       = model       or config.PAWPAL_MODEL
        self._timeout     = timeout     or config.MODEL_TIMEOUT_S
        self._max_retries = max_retries or config.MODEL_MAX_RETRIES

    def complete_json(self, prompt: str) -> dict:
        """Send *prompt* and return the parsed JSON response dict.

        Raises:
            ModelUnavailableError: Ollama is down or the model is not pulled.
            MalformedResponseError: JSON could not be parsed after all retries.
        """
        raw = self._call(prompt)
        for attempt in range(self._max_retries + 1):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                if attempt == self._max_retries:
                    logger.error("All %d retries exhausted — still not valid JSON.", self._max_retries)
                    raise MalformedResponseError("Model did not return valid JSON.")
                logger.warning("Malformed JSON on attempt %d — re-asking.", attempt + 1)
                raw = self._call(prompt + "\n\nIMPORTANT: Return ONLY raw JSON. No markdown, no prose.")

        raise MalformedResponseError("Model did not return valid JSON.")

    def complete(self, prompt: str) -> str:
        """Send *prompt* and return the raw text response.

        Raises:
            ModelUnavailableError: Ollama is down or the model is not pulled.
        """
        return self._call(prompt)

    def _call(self, prompt: str) -> str:
        """POST to Ollama /api/generate with transport-level retries and exponential backoff.

        Retries on transient errors (URLError, TimeoutError, OSError) and retryable HTTP
        statuses (429, 5xx). Raises immediately on 404 (model not pulled) and other
        non-retryable HTTP errors.

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
                    "Transport error — retrying in %ds (attempt %d/%d).",
                    delay, attempt + 1, self._max_retries + 1,
                )
                time.sleep(delay)
            try:
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = json.loads(resp.read())
                    return body.get("response", "")
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    logger.error(
                        "Model %r not found. Run: ollama pull %s",
                        self._model, self._model,
                    )
                    raise ModelUnavailableError(f"Model {self._model!r} not pulled.") from exc
                if exc.code not in _RETRYABLE_HTTP_STATUSES:
                    logger.error("Ollama HTTP error %d: %s", exc.code, exc.reason)
                    raise ModelUnavailableError(f"Ollama returned HTTP {exc.code}.") from exc
                logger.warning("Ollama HTTP %d — will retry.", exc.code)
                last_exc = exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                logger.warning("Transport error talking to Ollama: %s", exc)
                last_exc = exc

        logger.error(
            "Cannot reach Ollama at %s after %d attempt(s). Run: ollama serve",
            self._host, self._max_retries + 1,
        )
        raise ModelUnavailableError(
            f"Cannot reach Ollama at {self._host} after {self._max_retries + 1} attempt(s)."
        ) from last_exc
