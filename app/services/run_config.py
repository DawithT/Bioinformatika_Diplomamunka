"""
app/services/run_config.py

Describes the configuration a run actually uses: whether the LLM is on and
with which parameters, the cache mode, and the registry file.  One dict feeds
three places so they cannot disagree:

  - the read-only "Rendszerbeállítások" panel in the GUI,
  - the prompt-cache scope (app/services/cache.py),
  - <case_id>_run_config.json in every result folder.

No secrets are included — the LLM settings carry no credentials.

Public API
----------
describe_run(llm_enabled) -> dict
"""
from __future__ import annotations

import hashlib
from pathlib import Path


def _sha256_of_file(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _prompts_sha256() -> str:
    """Hash of every system prompt the LLM can receive."""
    from app.pipeline.step5_extract.gender import _GENDER_SYSTEM_PROMPT
    from app.pipeline.step5_extract.prompts import REVIEW_SYSTEM_PROMPT, SYSTEM_PROMPT

    text = "\0".join((SYSTEM_PROMPT, REVIEW_SYSTEM_PROMPT, _GENDER_SYSTEM_PROMPT))
    return hashlib.sha256(text.encode()).hexdigest()


def describe_run(llm_enabled: bool) -> dict:
    """
    Return the configuration of a run.

    *llm_enabled* is the effective switch for this run: the server allows the
    LLM (``llm_client.llm_configured()``) and the user did not turn it off.
    The LLM parameters are listed only when it is on; the cache counts as on
    only when both the LLM and ``ENABLE_LLM_CACHE`` are.
    """
    from app.pipeline.step5_extract.soft_llm import _MAX_CONTEXT_CHARS
    from app.services.llm_client import LLM_MAX_TOKENS, LLM_TEMPERATURE, LLM_TIMEOUT_S
    from config import settings

    llm: dict = {"enabled": llm_enabled}
    if llm_enabled:
        llm.update(
            provider=settings.llm_provider,
            model=settings.llm_model,
            base_url=settings.llm_base_url,
            temperature=LLM_TEMPERATURE,
            max_tokens=LLM_MAX_TOKENS,
            reasoning_effort=settings.llm_reasoning_effort,
            max_retries=settings.llm_max_retries,
            context_chars=_MAX_CONTEXT_CHARS,
            timeout_s=LLM_TIMEOUT_S,
            prompts_sha256=_prompts_sha256(),
        )

    registry_path = settings.registry_path_obj
    return {
        "llm": llm,
        "cache": {"enabled": llm_enabled and settings.enable_llm_cache},
        "registry": {
            "file": registry_path.name,
            "sha256": _sha256_of_file(registry_path),
        },
    }
