"""
app/services/cache.py

Prompt cache for LLM calls — in memory, for one job only.

The orchestrator creates one ``PromptCache`` per job and drops it when the
job ends, so cached answers never outlive the uploaded documents they came
from and nothing derived from patient text is written to disk.  A re-run of
the same case calls the LLM again; benchmark runs are therefore always clean.

Keys are sha256 of ``scope`` + the prompt text.  The prompt text already
carries the full system prompt; ``scope`` adds the rest of what decides the
answer — model, request parameters and registry hash (see
``app.services.run_config``).

Caching can be switched off with ``ENABLE_LLM_CACHE=false``
(``config.settings.enable_llm_cache``).  The orchestrator also switches it
off for any run without the LLM, so a no-LLM run never reads an answer.
"""
from __future__ import annotations

import hashlib
import logging

from config import settings as _settings

log = logging.getLogger(__name__)


class PromptCache:
    """
    In-memory prompt → response cache.

    Parameters
    ----------
    enabled : bool | None
        If False, all operations are no-ops.  Defaults to
        ``config.settings.enable_llm_cache``.
    scope : str
        Mixed into every key — model, request parameters, registry hash.
    """

    def __init__(self, enabled: bool | None = None, scope: str = ""):
        self._enabled = _settings.enable_llm_cache if enabled is None else enabled
        self._scope = scope
        self._entries: dict[str, str] = {}

    def _key(self, prompt_text: str) -> str:
        """Compute a cache key from the scope and the prompt text."""
        return hashlib.sha256(f"{self._scope}\0{prompt_text}".encode()).hexdigest()

    def get(self, prompt_text: str) -> str | None:
        """Return the cached LLM response for *prompt_text*, or None."""
        if not self._enabled:
            return None
        hit = self._entries.get(self._key(prompt_text))
        if hit is not None:
            log.debug("PromptCache HIT")
        return hit

    def set(self, prompt_text: str, response: str) -> None:
        """Store *response* under the key derived from *prompt_text*."""
        if self._enabled:
            self._entries[self._key(prompt_text)] = response

    @property
    def enabled(self) -> bool:
        """Return True if the cache is active; False when operating in no-op mode."""
        return self._enabled
