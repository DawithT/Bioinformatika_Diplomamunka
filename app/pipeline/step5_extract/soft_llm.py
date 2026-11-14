"""
app/pipeline/step5_extract/soft_llm.py

Step 5c — soft (LLM-assisted) field extraction.

``SoftExtractor`` groups registry fields by ``section_hint``, finds the
relevant document pages for each section, calls the LLM client, and
returns a list of ``ExtractedField`` objects.

Public API
----------
SoftExtractor(client, cache, registry)
    Constructed with a fully initialised LLM client and prompt cache.

SoftExtractor.extract(doc, registry, anchor) -> list[ExtractedField]
    Run soft extraction on one document.

Design notes
------------
- Fields are grouped by ``section_hint``.  Each group = one LLM call.
  This keeps prompt size well under 4k tokens and improves precision.
- Page selection: the extractor scores each page by keyword overlap with
  the section_hint string; it uses the top-scoring page(s) as context.
  Falls back to the full document text if no page scores above zero.
- ``DisabledClient.chat()`` returns ``"{}"`` — SoftExtractor maps
  all registry fields to ``"Unknown"`` in that case (LLM never examined
  the document).  When a real LLM answers but omits or cannot find a
  field, the marker is ``"No Data"`` instead.
- Fields with ``source_policy == "DISCHARGE_ONLY"`` are extracted only when
  the document is a DISCHARGE_SUMMARY.
- Fields with ``source_policy == "EXAM_DOC_PRIMARY"`` are extracted from
  all doc types including DISCHARGE_SUMMARY (the merge step decides
  which source wins when both contribute a value).
- Eligibility per (doc_type, routing_subtype) is resolved by the registry
  via ``FieldDefinition.is_eligible_for(...)`` — no hard-coded maps here.
- ``confidence = 0.7`` for all soft fields (architecture §9c).
- ``engine = "llm"`` for provenance.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import TYPE_CHECKING

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField, PatientAnchor, RoutedDoc

if TYPE_CHECKING:
    from app.registry.models import FieldDefinition
    from app.services.cache import PromptCache
    from app.services.llm_client import DisabledClient, LlamaCppClient, OllamaClient

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

_SOFT_CONFIDENCE = 0.7
_SOFT_ENGINE = "llm"

# Missing-value markers.
#   "Unknown" — the field was never successfully examined (LLM disabled or
#               the call failed after all retries; regex-only paths).
#   "No Data" — the LLM examined the document and found no evidence for the
#               field (missing key, empty value, or an explicit "Unknown"
#               answer from the model).
_UNKNOWN = "Unknown"
_NO_DATA = "No Data"

# Maximum characters of context text sent to the LLM per section call
_MAX_CONTEXT_CHARS = 4000

# Section → keyword hints used to find relevant document pages
# These augment the section_hint string itself
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "Anamnézis":               ["anamnézis", "anam", "jelen panasz", "múltban", "előzménye"],
    "Felvételi státus":        ["felvételi", "felveteli", "statusz", "státus", "érkezés", "felvételkor"],
    "Alkalmazott gyógyszerek": ["alkalmazott gyógyszer", "gyógyszerel", "gyógyszerek", "gyógyszeres kezelés", 
                                "gyógyszereit", "gyógyszerei"],
    "Epikrízis":               ["epikrízis", "epikrizis", "epikrisis", "összefoglal", "kórházi kezelés"],
    "Vizsgálatok":             ["vizsgálat", "eredmény", "lelet", "labor"],
    "Javaslat":                ["javaslat", "záródiagnózis", "diagnózis", "elbocsát", "otthonra"],
}

# Section header that marks the Epikrízis narrative block in discharge summaries.
# It is appended as secondary context ONLY for DISCHARGE_SUMMARY soft extraction
# (non-discharge documents don't have an Epikrízis section, so appending it
# was pure padding that crowded the real context out of the token budget).
_EPIKRISIS_KEYWORDS = ("epikrízis", "epikrizis", "epikrisis",)
_EPIKRISIS_SEPARATOR = "\n\n--- Epikrízis ---\n"

# Fallback threshold: very short documents (≤ _SHORT_DOC_PAGES pages) skip
# page scoring entirely and always return their full text. A one-page
# elastography report has nothing to gain from per-page ranking.
_SHORT_DOC_PAGES = 2

# ── Page selector ─────────────────────────────────────────────────────────────

def _split_section_hint(section_hint: str) -> list[str]:
    """Split a pipe-joined multi-section hint into individual section names."""
    return [s.strip() for s in section_hint.split("|") if s.strip()]


def _score_page(page_text: str, section_hint: str) -> int:
    """
    Score a document page for relevance to a section_hint.

    Multi-section hints (pipe-joined, e.g. "Epikrízis|Vizsgálatok eredményei")
    are scored against each component section — the page is relevant if ANY
    component matches. Returns an integer count of keyword matches
    (case-insensitive).
    """
    lower_page = page_text.lower()
    score = 0
    sections = _split_section_hint(section_hint) or [section_hint]

    for section in sections:
        # Count hits from the section string itself (tokens ≥ 4 chars)
        for token in section.lower().replace("/", " ").split():
            if len(token) >= 4 and token in lower_page:
                score += 1

        # Count hits from pre-defined section keywords
        section_lower = section.lower()
        for hint_key, keywords in _SECTION_KEYWORDS.items():
            if hint_key.lower() in section_lower:
                for kw in keywords:
                    if kw in lower_page:
                        score += 1

    return score


def _select_context(
    doc: RoutedDoc,
    section_hint: str,
    append_epikrisis: bool = False,
) -> str:
    """
    Return the document text most relevant to *section_hint*.

    Strategy:
      1. Short-document fallback: if the document has ≤ _SHORT_DOC_PAGES pages,
         return the full text (no scoring — there's nothing to rank).
      2. Score each page by keyword overlap with section_hint (multi-section
         hints are decomposed and scored per component).
      3. Return the top-scoring page(s), capped at _MAX_CONTEXT_CHARS.
      4. Fall back to full_text[:_MAX_CONTEXT_CHARS] if all scores are 0.
      5. If *append_epikrisis* is True (discharge-summary extraction), append
         the Epikrízis page as secondary context using any remaining budget.
         For non-discharge documents the Epikrízis section does not exist,
         so this is skipped by default.
    """
    if not doc.pages:
        return ""

    if len(doc.pages) <= _SHORT_DOC_PAGES:
        return doc.full_text[:_MAX_CONTEXT_CHARS]

    scored = sorted(
        enumerate(doc.pages),
        key=lambda ip: (_score_page(ip[1], section_hint), ip[0]),
        reverse=True,
    )

    best_score = _score_page(scored[0][1], section_hint)
    if best_score == 0:
        return doc.full_text[:_MAX_CONTEXT_CHARS]

    # ── Primary context: top-scoring pages ───────────────────────────────────
    context_parts: list[str] = []
    included_page_indices: set[int] = set()
    total = 0

    for page_idx, page_text in scored:
        if _score_page(page_text, section_hint) == 0:
            break
        remaining = _MAX_CONTEXT_CHARS - total
        if remaining <= 0:
            break
        context_parts.append(page_text[:remaining])
        included_page_indices.add(page_idx)
        total += min(len(page_text), remaining)

    # ── Secondary context: Epikrízis page (discharge summaries only) ─────────
    if append_epikrisis:
        remaining_budget = _MAX_CONTEXT_CHARS - total
        if remaining_budget > 200:
            epikrisis_page: str | None = None
            for page_idx, page_text in enumerate(doc.pages):
                if page_idx in included_page_indices:
                    continue
                lower = page_text.lower()
                if any(kw in lower for kw in _EPIKRISIS_KEYWORDS):
                    epikrisis_page = page_text
                    break

            if epikrisis_page is not None:
                context_parts.append(_EPIKRISIS_SEPARATOR)
                context_parts.append(epikrisis_page[:remaining_budget])

    return "\n\n".join(context_parts)


# ── Source policy & routing filter ────────────────────────────────────────────


def _fields_for_doc(
    fields: list[FieldDefinition],
    doc_type: DocType,
    routing_subtype: str | None = None,
) -> list[FieldDefinition]:
    """
    Filter *fields* to those eligible for *doc_type* (+ optional subtype).

    Two-stage filter:
      1. **Source policy** — DISCHARGE_ONLY fields are excluded from
         non-discharge documents.
      2. **Registry routing** — for non-discharge document types, only fields
         whose ``routing`` column declares this (doc_type[, subtype]) pair
         are kept. This replaces the previous hard-coded section-keyword
         map with a single source of truth: the registry itself.
    """
    result: list[FieldDefinition] = []
    is_discharge = doc_type == DocType.DISCHARGE_SUMMARY

    for fd in fields:
        # Stage 1: source policy
        policy = fd.source_policy
        if policy == "DISCHARGE_ONLY":
            if not is_discharge:
                continue
        # EXAM_DOC_PRIMARY and DISCHARGE_PRIMARY → always pass stage 1

        # Stage 2: registry-driven eligibility
        # For DISCHARGE_SUMMARY, every non-DISCHARGE_ONLY field is eligible
        # (the registry's DISCHARGE_SUMMARY routing entry or empty routing
        # tuple both admit discharge fields via is_eligible_for).
        if is_discharge:
            result.append(fd)
            continue

        if fd.is_eligible_for(doc_type, routing_subtype):
            result.append(fd)

    return result


# ── SoftExtractor ──────────────────────────────────────────────────────────────

class SoftExtractor:
    """
    Registry-driven LLM soft field extractor.

    One SoftExtractor instance can be shared across the entire pipeline run.
    The LLM client and prompt cache are injected at construction time.

    Parameters
    ----------
    client : LLM client instance (OllamaClient | LlamaCppClient | DisabledClient)
    cache  : PromptCache instance
    """

    def __init__(self, client, cache) -> None:
        self._client = client
        self._cache = cache

    @property
    def is_enabled(self) -> bool:
        """Return True if the LLM client is a real provider (not DisabledClient)."""
        from app.services.llm_client import DisabledClient
        return not isinstance(self._client, DisabledClient)

    def extract(
        self,
        doc: RoutedDoc,
        registry: list[FieldDefinition],
        anchor: PatientAnchor,
        populated_keys: set[str] | None = None,
    ) -> list[ExtractedField]:
        """
        Extract all soft fields from *doc* using the LLM.

        Parameters
        ----------
        doc      : RoutedDoc — ingested document
        registry : list[FieldDefinition] — all field definitions
        anchor   : PatientAnchor — used for provenance only (not day-numbering)
        populated_keys : set[str] | None
            Field keys already populated by higher-priority deterministic
            sources (anchors, hard parsers, BNO/virology).  These fields
            are excluded from the LLM prompt to avoid redundant extraction.

        Returns
        -------
        list[ExtractedField]
            All extracted soft fields.  Missing fields from a real LLM
            response are filled with value="No Data"; fields the LLM never
            examined (disabled/failed client) with value="Unknown".
        """
        # Filter to fields relevant for this doc_type + routing subtype
        routing_subtype = doc.meta.get("routing_subtype")
        relevant_fields = _fields_for_doc(
            registry, doc.doc_type, routing_subtype,
        )

        # Exclude fields already populated by higher-priority sources
        if populated_keys:
            before = len(relevant_fields)
            relevant_fields = [
                fd for fd in relevant_fields
                if fd.field_name not in populated_keys
            ]
            skipped = before - len(relevant_fields)
            if skipped:
                log.debug(
                    "SoftExtractor: skipped %d fields already populated "
                    "by hard/anchor extraction",
                    skipped,
                )

        if not relevant_fields:
            log.debug("SoftExtractor: no relevant fields for %s", doc.doc_type)
            return []

        # Group by section_hint
        groups: dict[str, list[FieldDefinition]] = defaultdict(list)
        for fd in relevant_fields:
            groups[fd.section_hint].append(fd)

        results: list[ExtractedField] = []

        for section_hint, section_fields in groups.items():
            section_results = self._extract_section(
                doc, section_hint, section_fields
            )
            results.extend(section_results)

        return results

    def _extract_section(
        self,
        doc: RoutedDoc,
        section_hint: str,
        fields: list[FieldDefinition],
    ) -> list[ExtractedField]:
        """
        Extract one section group, using cache if available.

        Returns a list of ExtractedField for all fields in the group.
        Fields not returned by a real LLM are given value="No Data";
        when the LLM is disabled or the call fails, value="Unknown".

        Handles prompt construction, JSON parsing, enum validation, and
        retry logic (up to 2 retries with error feedback appended to prompt).
        """
        from app.pipeline.step5_extract.prompts import (
            SYSTEM_PROMPT, build_user_prompt,
            extract_json_from_response, validate_enum_fields,
        )

        append_epikrisis = doc.doc_type == DocType.DISCHARGE_SUMMARY
        context_text = _select_context(
            doc, section_hint, append_epikrisis=append_epikrisis,
        )
        user_prompt = build_user_prompt(context_text, fields)
        cache_key = SYSTEM_PROMPT + "\n\n" + user_prompt

        # Check cache first — never with the LLM disabled, or answers from an
        # earlier LLM run would be passed off as no-LLM results.
        cached = self._cache.get(cache_key) if self.is_enabled else None
        if cached is not None:
            raw_dict = extract_json_from_response(cached)
            return self._map_to_fields(
                raw_dict, fields, doc.doc_type, llm_answered=self.is_enabled,
            )

        # Call LLM with retry + validation loop
        from config import settings
        max_retries = settings.llm_max_retries
        validation_errors: list[str] = []
        raw_dict: dict = {}

        for attempt in range(max_retries + 1):
            current_prompt = build_user_prompt(
                context_text, fields, validation_errors or None,
            )
            try:
                raw_text = self._client.chat(SYSTEM_PROMPT, current_prompt)
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "SoftExtractor: LLM call failed for section %r (attempt %d): %s",
                    section_hint, attempt + 1, exc,
                )
                if attempt == max_retries:
                    # LLM never examined the document → "Unknown"
                    return self._map_to_fields(
                        {}, fields, doc.doc_type, llm_answered=False,
                    )
                continue

            raw_dict = extract_json_from_response(raw_text)
            if not raw_dict:
                log.debug("LLM returned empty/unparseable JSON (attempt %d)", attempt + 1)
                if attempt == max_retries:
                    # No usable answer produced → treat as not examined.
                    # (DisabledClient always lands here via is_enabled=False.)
                    return self._map_to_fields(
                        {}, fields, doc.doc_type, llm_answered=False,
                    )
                expected_keys = ", ".join(f'"{fd.field_name}"' for fd in fields)
                validation_errors = [
                    "Érvénytelen JSON válasz. Csak egy JSON objektumot adj vissza, "
                    f"a következő kulcsokkal: {expected_keys}."
                ]
                continue

            validation_errors = validate_enum_fields(raw_dict, fields)
            if not validation_errors:
                break

            log.debug(
                "LLM enum validation failed (attempt %d): %s",
                attempt + 1, validation_errors,
            )

        # Cache the result
        import json as _json
        self._cache.set(cache_key, _json.dumps(raw_dict))

        return self._map_to_fields(
            raw_dict, fields, doc.doc_type, llm_answered=self.is_enabled,
        )

    def review_heuristic_fields(
        self,
        doc: RoutedDoc,
        heuristic_fields: list[ExtractedField],
    ) -> list[ExtractedField]:
        """
        Ask the LLM to verify/correct regex-extracted HEURISTIC fields.

        When the LLM is disabled, returns the fields unchanged.
        When enabled, sends the fields and document context to the LLM:
          - "confirm" → boost confidence to 0.95 (keep HEURISTIC source)
          - corrected value → replace with SOFT source, confidence 0.80
          - "reject" → drop the field entirely

        Parameters
        ----------
        doc : RoutedDoc
            Source document for context.
        heuristic_fields : list[ExtractedField]
            Fields extracted by hard/heuristic parsers to review.

        Returns
        -------
        list[ExtractedField]
            Reviewed fields (some may be modified, some dropped).
        """
        if not self.is_enabled or not heuristic_fields:
            return list(heuristic_fields)

        from app.pipeline.step5_extract.prompts import (
            REVIEW_SYSTEM_PROMPT, build_review_prompt,
            extract_json_from_response,
        )

        # Build the review prompt with document context
        context = doc.full_text[:4000]
        field_pairs = [(ef.key, str(ef.value)) for ef in heuristic_fields]
        user_prompt = build_review_prompt(context, field_pairs)

        try:
            raw_text = self._client.chat(REVIEW_SYSTEM_PROMPT, user_prompt)
        except Exception as exc:
            log.warning("LLM review call failed: %s — keeping originals", exc)
            return list(heuristic_fields)

        review_dict = extract_json_from_response(raw_text)
        if not review_dict:
            log.debug("LLM review returned empty response — keeping originals")
            return list(heuristic_fields)

        reviewed: list[ExtractedField] = []
        for ef in heuristic_fields:
            verdict = review_dict.get(ef.key)
            if verdict is None:
                # LLM didn't mention this field → keep as-is
                reviewed.append(ef)
                continue

            verdict_str = str(verdict).strip().lower()

            if verdict_str == "confirm":
                # LLM confirmed → boost confidence
                reviewed.append(ExtractedField(
                    key=ef.key,
                    value=ef.value,
                    source=ef.source,
                    doc_type=ef.doc_type,
                    confidence=0.95,
                    page=ef.page,
                    engine=ef.engine,
                ))
            elif verdict_str == "reject":
                # LLM rejected → drop field
                log.info("LLM review rejected field %r (was %r)", ef.key, ef.value)
                continue
            else:
                # LLM provided a corrected value
                corrected_value = str(verdict).strip()
                log.info(
                    "LLM review corrected field %r: %r → %r",
                    ef.key, ef.value, corrected_value,
                )
                reviewed.append(ExtractedField(
                    key=ef.key,
                    value=corrected_value,
                    source=FieldSource.SOFT,
                    doc_type=ef.doc_type,
                    confidence=0.80,
                    page=ef.page,
                    engine="llm_review",
                ))

        log.info(
            "LLM review: %d/%d fields kept (%d confirmed, %d corrected, %d rejected)",
            len(reviewed),
            len(heuristic_fields),
            sum(1 for r in reviewed if r.engine != "llm_review"),
            sum(1 for r in reviewed if r.engine == "llm_review"),
            len(heuristic_fields) - len(reviewed),
        )
        return reviewed

    def _map_to_fields(
        self,
        raw_dict: dict,
        fields: list[FieldDefinition],
        doc_type: DocType,
        llm_answered: bool = False,
    ) -> list[ExtractedField]:
        """
        Convert the raw LLM dict to ExtractedField objects.

        For every field in *fields*:
          - If present in raw_dict with a real value → use the value.
          - Otherwise the missing-value marker depends on *llm_answered*:
              * ``llm_answered=True``  → "No Data"  (the LLM examined the
                document and found nothing — includes missing keys, empty
                values, and an explicit "Unknown" answer from the model).
              * ``llm_answered=False`` → "Unknown"  (the LLM never examined
                the document: disabled client or failed call).

        For fields carrying a registry ``unit``, the LLM is instructed to emit
        ``"<number> <unit>"``. This method splits that form and stamps
        ``unit_seen`` / ``unit_ok`` on the resulting ``ExtractedField`` — the
        stored ``value`` keeps the raw LLM output so downstream flagging can
        show the user exactly what the model returned.
        """
        from app.pipeline.step5_extract.units import (
            split_value_and_unit,
            units_equivalent,
        )

        missing_marker = _NO_DATA if llm_answered else _UNKNOWN
        results: list[ExtractedField] = []

        for fd in fields:
            raw_value = raw_dict.get(fd.field_name)

            if raw_value is None:
                value = missing_marker
            else:
                value = str(raw_value).strip()
                if not value or value == _UNKNOWN:
                    # An explicit "Unknown" answer from the LLM means it
                    # looked and found nothing → same marker as a missing key.
                    value = missing_marker

            unit_seen: str | None = None
            unit_ok: bool | None = None
            if fd.unit and value not in (
                "Unknown", "No Data", "Not applicable", "Uncertain",
            ):
                _, unit_seen = split_value_and_unit(value)
                unit_ok = units_equivalent(fd.unit, unit_seen)

            results.append(ExtractedField(
                key=fd.field_name,
                value=value,
                source=FieldSource.SOFT,
                doc_type=doc_type,
                confidence=_SOFT_CONFIDENCE,
                page=None,
                engine=_SOFT_ENGINE,
                unit_seen=unit_seen,
                unit_ok=unit_ok,
            ))

        return results
