"""
app/services/llm_client.py

LLM transport layer for soft field extraction (Step 5c).

Three providers, selected by the ``LLM_PROVIDER`` setting in config / .env:

  ollama      → OllamaClient   — OpenAI-compat API on localhost:11434
  llamacpp    → LlamaCppClient — llama-cpp-python server on localhost:8000
  disabled    → DisabledClient — returns empty string for all calls (CI/no-LLM mode)

Switching between Ollama and llama.cpp requires only a .env change.
All configuration is read from ``config.settings`` — no direct ``os.environ``
access anywhere in this module.

Contract
--------
All clients implement a single transport method:

    chat(system: str, user: str) -> str

Return value:
  - The raw assistant response string from the LLM.
  - DisabledClient always returns ``"{}"`` (empty JSON object).

Prompt construction, JSON parsing, field validation, and retry logic live
in ``app.pipeline.step5_extract.prompts`` and ``soft_llm.SoftExtractor``,
keeping this module focused on HTTP transport only.

Design notes
------------
- Uses only stdlib http.client + json — no vendor SDK.
- check_llm_endpoint() refuses public-cloud hosts and Ollama cloud models
  before any client is built.
- OllamaClient and LlamaCppClient share _BaseHTTPClient to avoid duplication.
- ConfigurationError (not ValueError) is raised for unknown LLM_PROVIDER so
  callers can catch it specifically.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import socket
import threading
from http.client import HTTPConnection, HTTPException
from urllib.parse import urlparse

from config import settings

log = logging.getLogger(__name__)

# ── Exceptions ────────────────────────────────────────────────────────────────

class ConfigurationError(Exception):
    """Raised when LLM_PROVIDER is unknown or required config is missing."""


class LLMCallError(Exception):
    """Raised when the LLM call fails after all retries."""


# ── Endpoint guard: local / institutional servers only ────────────────────────
#
# Patient text must never reach a public cloud LLM. An endpoint is accepted
# when its host is listed in LLM_ALLOWED_HOSTS (institutional servers on public
# IP ranges), or when every address it resolves to is non-global (loopback,
# private, link-local). Ollama "-cloud" model tags are rejected as well: Ollama
# forwards those to ollama.com even when the server itself is local.

_CLOUD_MODEL_RE = re.compile(r"(?:^|[:\-])cloud$", re.IGNORECASE)


def check_llm_endpoint(base_url: str, model: str, allowed_hosts: str = "") -> None:
    """Raise ConfigurationError unless *base_url* / *model* stay on-premises."""
    if _CLOUD_MODEL_RE.search(model.strip()):
        raise ConfigurationError(
            f"LLM_MODEL={model!r} is an Ollama cloud model — it runs on "
            f"ollama.com, not locally. Use a locally pulled model."
        )

    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        raise ConfigurationError(f"LLM_BASE_URL={base_url!r} has no host.")

    allowed = {h.strip().lower() for h in allowed_hosts.split(",") if h.strip()}
    if host in allowed:
        return

    try:
        addrs = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise ConfigurationError(
                f"Cannot resolve LLM host {host!r} ({exc}), so it cannot be "
                f"verified as local. If it is an institutional server, add it "
                f"to LLM_ALLOWED_HOSTS."
            ) from exc
        addrs = {ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos}

    for addr in addrs:
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        if addr.is_global:
            raise ConfigurationError(
                f"LLM_BASE_URL host {host!r} resolves to the public address "
                f"{addr}. Only local or institutional LLM servers are allowed; "
                f"if this is an institutional server, add it to LLM_ALLOWED_HOSTS."
            )


# ── Inference concurrency guard ───────────────────────────────────────────────
#
# Local LLM backends (Ollama single-slot, llama.cpp) are designed for one
# inference request at a time.  If future code adds parallelism to the soft
# extraction loop, this lock ensures the actual HTTP call is serialised so
# the 16 GB RAM budget is never exceeded by concurrent model loads.
#
# The lock is module-level so it is shared across all client instances.
_INFERENCE_LOCK = threading.Lock()


# ── Request parameters ────────────────────────────────────────────────────────
#
# Fixed on purpose, not .env settings: temperature 0 makes the same document
# give the same answer. app.services.run_config reads these so the GUI and
# <case_id>_run_config.json show the values actually sent.
LLM_TEMPERATURE = 0.0
LLM_MAX_TOKENS = 1000
LLM_TIMEOUT_S = 300


# ── Base HTTP client ──────────────────────────────────────────────────────────

class _BaseHTTPClient:
    """
    Shared HTTP+JSON chat-completion client for OpenAI-compatible APIs.

    Used by both OllamaClient and LlamaCppClient — they differ only in
    base URL and model name.
    """

    def __init__(self, base_url: str, model: str):
        check_llm_endpoint(base_url, model, settings.llm_allowed_hosts)
        parsed = urlparse(base_url)
        self._host = parsed.hostname or "localhost"
        self._port = parsed.port or 80
        self._path = (parsed.path.rstrip("/") or "") + "/v1/chat/completions"
        self._model = model
        log.debug("LLM client initialised: %s %s", base_url, model)

    def _post(self, payload: dict, timeout: int = LLM_TIMEOUT_S) -> str:
        """HTTP POST to the chat completions endpoint. Returns response text."""
        body = json.dumps(payload).encode("utf-8")
        conn = HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            conn.request(
                "POST",
                self._path,
                body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
            )
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            if resp.status != 200:
                raise LLMCallError(
                    f"HTTP {resp.status} from LLM server: {raw[:200]}"
                )
            return raw
        except (HTTPException, OSError) as exc:
            raise LLMCallError(f"LLM HTTP error: {exc}") from exc
        finally:
            conn.close()

    def chat(self, system: str, user: str) -> str:
        """
        Single chat completion call.

        The actual HTTP request is serialised by ``_INFERENCE_LOCK`` so that
        even if callers run in parallel threads, only one inference request
        reaches the LLM backend at a time (hardware safety for 16 GB RAM).

        Parameters
        ----------
        system : str
            System prompt text.
        user : str
            User prompt text.

        Returns
        -------
        str — the raw assistant message content from the LLM.
        """
        payload = {
            "model": self._model,
            "think": False,          # ← disable chain-of-thought for JSON extraction
            "messages": [
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            "temperature": LLM_TEMPERATURE,
            "max_tokens": LLM_MAX_TOKENS,
            "stream": False,
            "response_format": {"type": "json_object"},
            "reasoning": {"effort": settings.llm_reasoning_effort},
        }
        with _INFERENCE_LOCK:
            raw = self._post(payload)
        try:
            data = json.loads(raw)
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise LLMCallError(f"Unexpected LLM response shape: {raw[:300]}") from exc


# ── Concrete clients ──────────────────────────────────────────────────────────

class OllamaClient(_BaseHTTPClient):
    """
    Ollama OpenAI-compatible client.

    Default base URL and model are taken from ``config.settings``
    (``LLM_BASE_URL`` / ``LLM_MODEL`` in .env).

    Typical Ollama default: http://localhost:11434
    Recommended model:      qwen2.5:7b-instruct-q4_K_M
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ):
        super().__init__(
            base_url=base_url if base_url is not None else settings.llm_base_url,
            model=model if model is not None else settings.llm_model,
        )


class LlamaCppClient(_BaseHTTPClient):
    """
    llama-cpp-python server client.

    Base URL and model are taken from ``config.settings``
    (``LLM_BASE_URL`` / ``LLM_MODEL`` in .env).

    When switching to llama.cpp, set LLM_PROVIDER=llamacpp and
    LLM_BASE_URL=http://127.0.0.1:8000 in .env.

    The model name is ignored by llama-cpp-python (any non-empty string works),
    but is forwarded for API compatibility.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str | None = None,
    ):
        super().__init__(
            base_url=base_url if base_url is not None else settings.llm_base_url,
            model=model if model is not None else settings.llm_model,
        )


class DisabledClient:
    """
    No-op LLM client for CI and environments without a local LLM.

    ``chat()`` always returns ``"{}"`` (empty JSON object string).
    SoftExtractor maps missing keys to "Unknown".
    """

    def chat(self, system: str, user: str) -> str:
        """Return empty JSON object string without making any LLM call."""
        log.debug("DisabledClient: skipping LLM call (LLM disabled)")
        return "{}"


# ── Factory ───────────────────────────────────────────────────────────────────

def llm_configured() -> bool:
    """True when the server configuration allows LLM calls at all."""
    return settings.enable_llm and settings.llm_provider.strip().lower() != "disabled"


def get_llm_client() -> OllamaClient | LlamaCppClient | DisabledClient:
    """
    Return the appropriate LLM client based on ``config.settings``.

    Resolution order:
      1. If ``settings.enable_llm`` is False → DisabledClient (no LLM call made).
      2. Otherwise dispatch on ``settings.llm_provider``:
           ``ollama``    → OllamaClient
           ``llamacpp``  → LlamaCppClient
           ``disabled``  → DisabledClient

    Raises
    ------
    ConfigurationError
        If ``llm_provider`` is set to an unrecognised value (should not happen
        when config is validated by pydantic, but kept as a safety net).
    """
    if not settings.enable_llm:
        log.info("settings.enable_llm=False → using DisabledClient")
        return DisabledClient()

    provider = settings.llm_provider.strip().lower()

    if provider == "ollama":
        return OllamaClient()
    if provider in ("llamacpp", "llama_cpp", "llama-cpp"):
        return LlamaCppClient()
    if provider == "disabled":
        return DisabledClient()
    raise ConfigurationError(
        f"Unknown llm_provider={provider!r}. "
        f"Valid values: 'ollama', 'llamacpp', 'disabled'."
    )
