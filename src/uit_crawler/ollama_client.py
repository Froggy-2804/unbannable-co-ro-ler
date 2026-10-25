"""HTTP client for local or cloud Ollama server with preflight and error handling."""

from __future__ import annotations

import ipaddress
import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

import httpx
DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_CLOUD_OLLAMA_URL = "https://ollama.com"
DEFAULT_MODEL = "gpt-oss:20b"


def is_cloud_origin(url: str | None) -> bool:
    """Return True if URL matches the exact trusted HTTPS ollama.com origin."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except Exception:
        return False

    if parsed.scheme.lower() != "https":
        return False

    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        return False

    hostname = (parsed.hostname or "").lower()
    if hostname != "ollama.com":
        return False

    if parsed.port is not None and parsed.port != 443:
        return False

    if parsed.path not in ("", "/"):
        return False

    if parsed.query or parsed.fragment:
        return False

    return True


def is_loopback_url(url: str | None) -> bool:
    """Return True if URL host resolves to a loopback address."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
    except Exception:
        return False
    host = (parsed.hostname or "").lower().strip("[]")
    if not host:
        return False
    if host in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_loopback
    except ValueError:
        return False

class OllamaError(Exception):
    """Base error for Ollama client operations."""


class OllamaConnectionError(OllamaError):
    """Ollama service is unreachable or not running."""


class OllamaModelNotFoundError(OllamaError):
    """Requested model is not downloaded or found on Ollama."""


class OllamaTimeoutError(OllamaError):
    """Request to Ollama timed out."""


class OllamaAPIError(OllamaError):
    """Ollama API returned an error status code or unexpected payload."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"Ollama API error ({status_code}): {message}")
        self.status_code = status_code
        self.message = message


class OllamaLengthExceededError(OllamaError):
    """Generation stopped due to output length limit (done_reason == 'length')."""


@dataclass(slots=True, frozen=True)
class OllamaModelInfo:
    name: str
    digest: str
    size: int
    thinking_values: list[str] | None
    thinking_default: str | None
    ollama_version: str | None


@dataclass(slots=True, frozen=True)
class OllamaChatResult:
    content: str
    thinking: str | None
    done: bool
    done_reason: str | None
    prompt_eval_count: int | None
    eval_count: int | None
    total_duration_ns: int | None
    raw_response: dict[str, Any]


class OllamaClient:
    """Dedicated HTTP client for Ollama local or cloud service."""

    def __init__(
        self,
        base_url: str = DEFAULT_OLLAMA_URL,
        api_key: str | None = None,
        timeout: float = 300.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not api_key and self.is_cloud and "OLLAMA_API_KEY" not in os.environ:
            from .runtime import load_env_file
            load_env_file()
        self.api_key = api_key or (os.environ.get("OLLAMA_API_KEY") if self.is_cloud else None)

        if self.api_key:
            parsed = urllib.parse.urlsplit(self.base_url)
            scheme = (parsed.scheme or "").lower()
            if scheme != "https" and not is_loopback_url(self.base_url):
                raise ValueError(
                    f"Insecure API key transmission to '{self.base_url}'. "
                    "HTTPS is required for remote endpoints unless using loopback."
                )

        self.default_timeout = timeout
        self._owns_client = client is None
        headers = self._auth_headers if self._owns_client else None
        self._client = client or httpx.Client(timeout=timeout, headers=headers)

    @property
    def is_cloud(self) -> bool:
        """Return True if connecting to hosted Ollama Cloud."""
        return is_cloud_origin(self.base_url)

    @property
    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def get_version(self) -> str:
        """Fetch Ollama daemon version."""
        url = f"{self.base_url}/api/version"
        try:
            resp = self._client.get(url, timeout=10.0, headers=self._auth_headers)
            if resp.status_code != 200:
                raise OllamaAPIError(resp.status_code, resp.text)
            data = resp.json()
            return str(data.get("version", "unknown"))
        except httpx.ConnectError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.base_url}. Ensure Ollama is running."
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(
                f"Timeout connecting to Ollama at {self.base_url}."
            ) from exc
        except Exception as exc:
            if isinstance(exc, OllamaError):
                raise
            raise OllamaConnectionError(f"Error checking Ollama version: {exc}") from exc

    def list_models(self) -> list[dict[str, Any]]:
        """List models currently pulled and available locally."""
        url = f"{self.base_url}/api/tags"
        try:
            resp = self._client.get(url, timeout=15.0, headers=self._auth_headers)
            if resp.status_code != 200:
                raise OllamaAPIError(resp.status_code, resp.text)
            data = resp.json()
            return data.get("models", [])
        except httpx.ConnectError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.base_url} to list models."
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(
                f"Timeout listing models from Ollama at {self.base_url}."
            ) from exc
        except Exception as exc:
            if isinstance(exc, OllamaError):
                raise
            raise OllamaConnectionError(f"Error querying Ollama models: {exc}") from exc

    def show_model(self, model: str) -> dict[str, Any]:
        """Fetch model details including parameters, template, and thinking controls."""
        url = f"{self.base_url}/api/show"
        try:
            resp = self._client.post(url, json={"model": model}, timeout=20.0, headers=self._auth_headers)
            if resp.status_code != 200:
                raise OllamaAPIError(resp.status_code, resp.text)
            return resp.json()
        except httpx.ConnectError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.base_url}."
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(
                f"Timeout retrieving details for model '{model}'."
            ) from exc
        except Exception as exc:
            if isinstance(exc, OllamaError):
                raise
            raise OllamaConnectionError(
                f"Error querying model details for '{model}': {exc}"
            ) from exc

    def preflight(self, model: str = DEFAULT_MODEL) -> OllamaModelInfo:
        """Verify Ollama daemon is running, model is available locally, and retrieve digest."""
        version = self.get_version()
        models = self.list_models()

        # Find matching model in tags
        target_norm = model.lower()
        matched_model = None
        for m in models:
            m_name = m.get("name", "").lower()
            if m_name == target_norm or m_name.startswith(f"{target_norm}:"):
                matched_model = m
                break
            # Also check if user passed model without tag and tag is :latest
            if ":" not in target_norm and m_name == f"{target_norm}:latest":
                matched_model = m
                break

        if not matched_model:
            available_names = [m.get("name") for m in models]
            raise OllamaModelNotFoundError(
                f"Model '{model}' is not available in local Ollama. "
                f"Available models: {available_names}. Run: ollama pull {model}"
            )

        digest = matched_model.get("digest", "")
        size = matched_model.get("size", 0)

        # Query show to discover thinking settings
        thinking_values = None
        thinking_default = None
        try:
            show_data = self.show_model(model)
            thinking_obj = show_data.get("thinking")
            if isinstance(thinking_obj, dict):
                raw_values = thinking_obj.get("values")
                if isinstance(raw_values, list):
                    thinking_values = [str(v) for v in raw_values]
                thinking_default = thinking_obj.get("default")
        except Exception:
            # show_model is non-fatal if daemon is an older build without /api/show
            pass

        return OllamaModelInfo(
            name=model,
            digest=digest,
            size=size,
            thinking_values=thinking_values,
            thinking_default=thinking_default,
            ollama_version=version,
        )

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any] | None = None,
        think: str | bool | None = "low",
        options: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> OllamaChatResult:
        """Execute a non-streaming chat completion with optional JSON schema and thinking level."""
        url = f"{self.base_url}/api/chat"
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": False,
        }

        if schema is not None:
            # Ollama Cloud currently requires format='json' rather than full JSON schema
            payload["format"] = "json" if self.is_cloud else schema

        if think is not None and not (isinstance(think, str) and think.strip().lower() == "none"):
            payload["think"] = think

        if options:
            payload["options"] = options

        call_timeout = timeout if timeout is not None else self.default_timeout

        try:
            resp = self._client.post(url, json=payload, timeout=call_timeout, headers=self._auth_headers)
        except httpx.ConnectError as exc:
            raise OllamaConnectionError(
                f"Cannot connect to Ollama at {self.base_url} during chat."
            ) from exc
        except httpx.TimeoutException as exc:
            raise OllamaTimeoutError(
                f"Ollama chat request timed out after {call_timeout}s."
            ) from exc
        except Exception as exc:
            raise OllamaAPIError(0, f"HTTP request failed: {exc}") from exc

        if resp.status_code == 401:
            raise OllamaAPIError(
                401,
                "Unauthorized: Vui lòng cung cấp OLLAMA_API_KEY hợp lệ cho Ollama Cloud.",
            )
        if resp.status_code != 200:
            raise OllamaAPIError(resp.status_code, resp.text)
        try:
            data = resp.json()
        except Exception as exc:
            raise OllamaAPIError(
                resp.status_code, f"Failed to parse Ollama response as JSON: {exc}"
            ) from exc

        msg = data.get("message", {})
        content = msg.get("content", "")
        thinking = msg.get("thinking")
        done = bool(data.get("done", False))
        done_reason = data.get("done_reason")

        if done_reason == "length":
            raise OllamaLengthExceededError(
                "Generation stopped because output token limit (num_predict) was reached."
            )

        if not done:
            raise OllamaAPIError(
                resp.status_code, "Ollama response marked done=False unexpectedly."
            )

        return OllamaChatResult(
            content=content,
            thinking=thinking,
            done=done,
            done_reason=done_reason,
            prompt_eval_count=data.get("prompt_eval_count"),
            eval_count=data.get("eval_count"),
            total_duration_ns=data.get("total_duration"),
            raw_response=data,
        )
