"""
app/pipeline/step5_extract/gender.py

LLM-based gender deduction from Hungarian patient names.

Hungarian names follow family-given order and use a `-né` (wife-of) suffix
that unambiguously marks female form regardless of the first name. The LLM
handles first-name-based inference while we enforce the `-né` rule in the
system prompt:

    Fekete Piroska         → Nő     (female first name)
    Kovácsné Fekete Piroska → Nő    (né suffix on family name)
    Kovács Jánosné          → Nő    (né suffix on given name)
    Kovács János            → Férfi (male first name, no né)

The result is emitted as an ExtractedField for the registry field
``a_beteg_neme`` (enum: Férfi | Nő). Missing / ambiguous results map to
``"Unknown"`` and go through the normal soft-extraction pathway.

Public API
----------
deduce_gender(client, cache, name) -> str | None
    Return "Férfi", "Nő", or None when the LLM cannot decide. Cached by
    name so repeated runs for the same case hit the cache.

extract_gender_field(client, cache, name) -> ExtractedField | None
    Convenience wrapper that returns a HARD ExtractedField for
    ``a_beteg_neme`` when the gender can be resolved; ``None`` otherwise.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField

if TYPE_CHECKING:
    from app.services.cache import PromptCache

log = logging.getLogger(__name__)


_GENDER_SYSTEM_PROMPT = (
    "Te egy magyar nyelvű névelemző asszisztens vagy. "
    "Megkapsz egy magyar nevet, és eldöntöd, hogy a név viselője férfi vagy nő.\n"
    "Szabályok:\n"
    "1. Ha a név bármely része (családi vagy keresztnév) '-né' végződésű, "
    "az NŐ (ez a 'feleség' toldalék, pl. 'Kovácsné' vagy 'Jánosné').\n"
    "2. Ha a keresztnév tipikusan női (pl. Piroska, Anna, Mária), az NŐ.\n"
    "3. Ha a keresztnév tipikusan férfi (pl. János, István, Péter) és NINCS "
    "'-né' toldalék a névben, az FÉRFI.\n"
    "4. A magyar névsorrend: családi név először, keresztnév utoljára "
    "('Fekete Piroska' → a keresztnév 'Piroska').\n"
    "5. Ha nem tudod egyértelműen eldönteni (pl. a név anonimizált, "
    "\"UNKNOWN\", \"xxx\", vagy több bizonytalan elem van), a válasz \"Unknown\".\n"
    "Példák:\n"
    '  "Fekete Piroska"         → {"gender": "Nő"}\n'
    '  "Kovácsné Fekete Piroska" → {"gender": "Nő"}\n'
    '  "Kovács Jánosné"          → {"gender": "Nő"}\n'
    '  "Kovács János"            → {"gender": "Férfi"}\n'
    '  "xxx xxx"                 → {"gender": "Unknown"}\n'
    "Csak JSON-t adj vissza, más szöveget ne:\n"
    '{"gender": "Férfi" | "Nő" | "Unknown"}'
)


_VALID_GENDERS = {"Férfi", "Nő"}
_ANCHOR_PLACEHOLDER_NAMES = {"UNKNOWN", "", "xxx", "XXX"}


def _cache_key_for_name(name: str) -> str:
    """Cache key used by ``PromptCache`` — distinct from section prompts."""
    return f"GENDER_V1::{name.strip().lower()}"


def deduce_gender(client, cache: "PromptCache | None", name: str) -> str | None:
    """
    Return the inferred gender ("Férfi" | "Nő") for *name*, or None when the
    LLM cannot decide or the client is disabled.

    Parameters
    ----------
    client
        An LLM client instance (OllamaClient, LlamaCppClient, or DisabledClient).
    cache
        Optional ``PromptCache`` instance. The cache key is derived from the
        normalised name, so identical cases in a batch cost only one LLM call.
    name
        The patient's full name as extracted by the anchor regex.
    """
    from app.services.llm_client import DisabledClient

    if not name or name.strip().upper() in _ANCHOR_PLACEHOLDER_NAMES:
        return None

    if isinstance(client, DisabledClient):
        return None

    cache_key = _cache_key_for_name(name)
    if cache is not None:
        cached = cache.get(cache_key)
        if cached is not None:
            try:
                verdict = json.loads(cached).get("gender")
                if verdict in _VALID_GENDERS:
                    return verdict
                return None
            except json.JSONDecodeError:
                pass  # fall through to a fresh call

    try:
        raw = client.chat(_GENDER_SYSTEM_PROMPT, f"Név: {name.strip()}")
    except Exception as exc:  # noqa: BLE001
        log.warning("gender: LLM call failed for %r: %s", name, exc)
        return None

    try:
        verdict = json.loads(raw).get("gender")
    except json.JSONDecodeError:
        # Some backends wrap the JSON in prose — try the shared extractor.
        from app.pipeline.step5_extract.prompts import extract_json_from_response
        verdict = extract_json_from_response(raw).get("gender")

    if verdict not in _VALID_GENDERS:
        log.info("gender: LLM returned non-deterministic answer for %r: %r", name, verdict)
        if cache is not None:
            cache.set(cache_key, json.dumps({"gender": "Unknown"}))
        return None

    if cache is not None:
        cache.set(cache_key, json.dumps({"gender": verdict}))
    return verdict


def extract_gender_field(
    client,
    cache: "PromptCache | None",
    name: str,
) -> ExtractedField | None:
    """
    Build an ``ExtractedField`` for ``a_beteg_neme`` when the gender can be
    deduced from *name*. Returns ``None`` when the LLM is disabled or the
    answer is ambiguous — in that case the soft-extraction pipeline falls
    back to reading gender from the discharge-summary narrative.
    """
    verdict = deduce_gender(client, cache, name)
    if verdict is None:
        return None
    return ExtractedField(
        key="a_beteg_neme",
        value=verdict,
        source=FieldSource.HARD,
        doc_type=DocType.DISCHARGE_SUMMARY,
        confidence=0.90,
        engine="gender_llm",
    )
