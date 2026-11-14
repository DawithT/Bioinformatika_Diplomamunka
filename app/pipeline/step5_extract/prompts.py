"""
app/pipeline/step5_extract/prompts.py

Prompt construction, response parsing, and field validation for LLM-based
soft extraction (Step 5c).

This module owns all domain-aware logic that sits between the LLM transport
layer (``app.services.llm_client``) and the extraction orchestrator
(``soft_llm.SoftExtractor``):

  SYSTEM_PROMPT                — fixed Hungarian hepatology system instruction
  build_user_prompt()          — per-section user prompt builder
  extract_json_from_response() — robust JSON extraction from raw LLM output
  validate_enum_fields()       — enum / partial-date validation with error messages

The LLM client is kept transport-only (HTTP call → raw string); this module
handles everything prompt- and parse-related.
"""
from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.registry.models import FieldDefinition


# ── System prompt ─────────────────────────────────────────────────────────────

# Hungarian season names → canonical ISO month strings.
# This single mapping drives both the SYSTEM_PROMPT rule text and the
# validator's season-normalisation regex (see _SEASON_TO_MONTH below).
_SEASONS: tuple[tuple[str, str], ...] = (
    ("tavasz", "04"),
    ("nyár",   "07"),
    ("ősz",    "10"),
    ("tél",    "01"),
)

_SEASON_PROMPT_RULE = (
    "Évszakokat alakítsd át a középső hónapra: "
    + ", ".join(f"{name}→{month}" for name, month in _SEASONS)
    + '. Pl. "2019 őszén" → "2019-10".'
)

SYSTEM_PROMPT = (
    "Te egy hepatológiai asszisztens vagy. "
    "Magyar nyelvű kórházi dokumentumokból kinyered a kért adatokat.\n"
    "Szabályok:\n"
    '1. Csak JSON-t adj vissza, más szöveget nem.\n'
    '2. BÁRMELY mező esetén, ha az adat nem szerepel a szövegben, az értéke: "Unknown". '
    'Ez enum mezőkre is vonatkozik — NE találj ki értéket, ha az adat hiányzik.\n'
    '3. Ha logikailag kizárt: "Not applicable"\n'
    '4. Ha megtalálható, de nem egyértelmű: "Uncertain"\n'
    '5. Dátumoknál a mező saját formátumjelzését kövesd (descriptor).\n'
    f'6. {_SEASON_PROMPT_RULE}\n'
    '7. Enum mezőknél az engedélyezett értékek egyikét add meg, vagy "Unknown" ha nem található.\n'
    '8. Számértékeknél a megadott mértékegységet használd. '
    'Ha a szövegben más egység szerepel, add vissza "{érték} {egység}" formában.'
)


# ── Prompt builder ────────────────────────────────────────────────────────────

_GRANULARITY_HINT: dict[str, str] = {
    "day":   "YYYY-MM-DD",
    "month": "YYYY-MM (vagy YYYY-MM-DD)",
    "year":  "YYYY (vagy YYYY-MM, YYYY-MM-DD)",
}


def _build_field_descriptor(fd: FieldDefinition) -> str:
    """
    Build the per-field value descriptor used in the user prompt.

    Rules (in order):
      1. List + enum  → JSON array of allowed values.
      2. List (open)  → generic JSON array.
      3. Enum         → allowed-values list; date hint appended if field also
                        carries one (rare, but supported).
      4. Date         → descriptor driven by date_format_hint +
                        date_granularity_allowed.
      5. Numeric with unit → "number, mértékegység: <unit>".
      6. Otherwise    → "string".
    """
    date_suffix = ""
    if fd.date_format_hint or fd.date_granularity_allowed:
        parts: list[str] = []
        if fd.date_format_hint:
            parts.append(f"formátum: {fd.date_format_hint}")
        gran_hint = _GRANULARITY_HINT.get(fd.date_granularity_allowed or "")
        if gran_hint and (
            not fd.date_format_hint or gran_hint not in fd.date_format_hint
        ):
            parts.append(f"granularitás: {gran_hint}")
        date_suffix = " — " + "; ".join(parts) if parts else ""

    if fd.is_list_field and fd.is_enum and fd.allowed_values:
        allowed = ", ".join(f'"{v}"' for v in fd.allowed_values)
        return f"[{allowed}]  # TÖBB IS LEHETSÉGES — JSON tömb"
    if fd.is_list_field:
        return '["érték1", "érték2", ...]  # TÖBB IS LEHETSÉGES — JSON tömb'
    if fd.is_enum and fd.allowed_values:
        allowed = ", ".join(f'"{v}"' for v in fd.allowed_values)
        return f"[{allowed}]{date_suffix}"
    if date_suffix:
        return f"date{date_suffix}"
    if fd.unit:
        return f"number, mértékegység: {fd.unit}"
    return "string"


def build_user_prompt(
    section_text: str,
    fields: list[FieldDefinition],
    validation_errors: list[str] | None = None,
) -> str:
    """
    Build the user-turn prompt for a section extraction call.

    Parameters
    ----------
    section_text : str
        The relevant portion of the document for this section.
    fields : list[FieldDefinition]
        Registry fields to extract in this call.
    validation_errors : list[str] | None
        Previous-round validation errors to include in retry prompt.

    Returns
    -------
    str  — complete user prompt text
    """
    lines = ["A következő szövegből kérlek kinyerd az alábbi adatokat JSON formátumban.\n"]
    lines.append("SZÖVEG:")
    lines.append("---")
    lines.append(section_text.strip()[:4000])  # cap context at ~4k chars
    lines.append("---\n")
    lines.append("MEZŐK:")

    for fd in fields:
        descriptor = _build_field_descriptor(fd)
        lines.append(f'  "{fd.field_name}": {descriptor}  # {fd.column_header}')

    lines.append('\nVálasz formátuma: {{"field_name": "érték", ...}}')

    if validation_errors:
        lines.append("\nELŐZŐ VÁLASZ HIBÁI — javítsd ki:")
        for err in validation_errors:
            lines.append(f"  - {err}")

    return "\n".join(lines)


# ── Review prompt builder ────────────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """\
Te egy hepatológiai asszisztens vagy. Magyar nyelvű kórházi dokumentumokból kinyert adatokat ellenőrzöd.
Szabályok:
1. Csak JSON-t adj vissza, más szöveget nem.
2. Minden megadott mező kulcsára válaszolj "confirm" vagy a javított értékkel.
3. Ha a kinyert érték helyes a szöveg alapján: "confirm"
4. Ha hibás, de a helyes érték megtalálható a szövegben: add meg a javított értéket.
5. Ha az adat nem szerepel a szövegben (hamis pozitív): "reject"
6. Ha bizonytalan: "confirm" (inkább tartsd meg, mint utasítsd el).\
"""


def build_review_prompt(
    context_text: str,
    heuristic_fields: list[tuple[str, str]],
) -> str:
    """
    Build a user prompt asking the LLM to verify/correct regex-extracted fields.

    Parameters
    ----------
    context_text : str
        Relevant document text for context.
    heuristic_fields : list[tuple[str, str]]
        List of (field_name, extracted_value) tuples to review.

    Returns
    -------
    str — user prompt for the review call
    """
    lines = [
        "Az alábbi mezőket regex alapján nyertük ki a szövegből.",
        "Kérlek ellenőrizd mindegyiket: ha helyes, írd: \"confirm\".",
        "Ha hibás, add meg a helyes értéket. Ha hamis pozitív, írd: \"reject\".\n",
        "SZÖVEG:",
        "---",
        context_text.strip()[:4000],
        "---\n",
        "ELLENŐRIZENDŐ MEZŐK:",
    ]
    for field_name, value in heuristic_fields:
        lines.append(f'  "{field_name}": "{value}"')

    lines.append('\nVálasz formátuma: {{"field_name": "confirm" | "reject" | "javított_érték", ...}}')
    return "\n".join(lines)


# ── Response parsing ──────────────────────────────────────────────────────────

def extract_json_from_response(text: str) -> dict:
    """
    Extract the JSON object from a raw LLM response string.

    Handles:
      - Clean JSON
      - JSON wrapped in ```json ... ``` fences
      - JSON preceded/followed by explanatory text
    """
    if not text or not text.strip():
        return {}

    # Try direct parse first
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Strip markdown code fences
    stripped = re.sub(r'^```(?:json)?\s*', '', stripped, flags=re.IGNORECASE)
    stripped = re.sub(r'\s*```$', '', stripped)
    try:
        return json.loads(stripped.strip())
    except json.JSONDecodeError:
        pass

    # Find outermost JSON object
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass

    return {}


# ── Validation helpers ────────────────────────────────────────────────────────

# Per-granularity date regex map.
#   "day"   → YYYY-MM-DD only
#   "month" → YYYY-MM or YYYY-MM-DD
#   "year"  → YYYY, YYYY-MM, or YYYY-MM-DD
_DATE_RE_BY_GRANULARITY: dict[str, re.Pattern[str]] = {
    "day":   re.compile(r'^\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])$'),
    "month": re.compile(
        r'^\d{4}-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?$'
    ),
    "year":  re.compile(
        r'^\d{4}(?:-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12]\d|3[01]))?)?$'
    ),
}

# Hungarian season names → canonical ISO month strings — single source of truth.
_SEASON_TO_MONTH: dict[str, str] = dict(_SEASONS)

# Season normalisation: "2019 őszén" / "2019. ősz:" → "2019-10"
_SEASON_RE = re.compile(
    r'(\d{4})[\.\s]*(' + "|".join(re.escape(name) for name, _ in _SEASONS) + r')\w*',
    re.IGNORECASE,
)


def _normalise_partial_date(value: str) -> str:
    """
    Convert season expressions to YYYY-MM.
    Returns the value unchanged if it is not a season expression.
    """
    m = _SEASON_RE.match(value.strip())
    if m:
        year = m.group(1)
        season_key = m.group(2).lower()
        month = _SEASON_TO_MONTH.get(season_key)
        if month:
            return f"{year}-{month}"
    return value


def validate_enum_fields(
    result: dict,
    fields: list[FieldDefinition],
) -> list[str]:
    """
    Check that enum fields contain only allowed values.
    Returns a list of human-readable error strings (empty = valid).
    Special markers (Unknown, Not applicable, Uncertain, NA, ERVENYTELEN)
    are always accepted.
    Also handles:
    - list fields (JSON array of enum values)
    - date fields, validated against the field's date_granularity_allowed:
      "day" → YYYY-MM-DD; "month" → YYYY-MM or YYYY-MM-DD;
      "year" → YYYY, YYYY-MM, or YYYY-MM-DD
    - season expressions normalised to YYYY-MM before validation (e.g.
      "2019 őszén" → "2019-10"); applies when year or month granularity
      is allowed — anamnesis fields commonly use this.
    """
    _ALWAYS_VALID = {"Unknown", "No Data", "Not applicable", "Uncertain", "NA",
                     "ERVENYTELEN", ""}
    errors: list[str] = []

    fd_map = {fd.field_name: fd for fd in fields}

    for key, value in result.items():
        str_val = str(value).strip() if value is not None else ""
        if str_val in _ALWAYS_VALID:
            continue

        fd = fd_map.get(key)
        if fd is None:
            continue

        # ── List fields ───────────────────────────────────────────────────────
        if fd.is_list_field:
            # Accept a JSON array or a plain string (single value)
            try:
                items = json.loads(str_val) if str_val.startswith("[") else [str_val]
            except (json.JSONDecodeError, ValueError):
                items = [str_val]

            if fd.is_enum and fd.allowed_values:
                for item in items:
                    s = str(item).strip()
                    if s not in _ALWAYS_VALID and s not in fd.allowed_values:
                        allowed_str = ", ".join(fd.allowed_values)
                        errors.append(
                            f'Mező "{key}" listaelemje "{s}" érvénytelen. '
                            f'Engedélyezett értékek: {allowed_str}'
                        )
            continue

        # ── Date fields (granularity-driven) ──────────────────────────────────
        granularity = fd.date_granularity_allowed or ""
        date_re = _DATE_RE_BY_GRANULARITY.get(granularity)
        if date_re is not None:
            # Season normalisation is only meaningful when partial dates are
            # allowed (year/month); at "day" granularity it would widen the
            # accepted set beyond the field's declared precision.
            if granularity in ("year", "month"):
                candidate = _normalise_partial_date(str_val)
            else:
                candidate = str_val
            if date_re.match(candidate):
                result[key] = candidate
                continue
            if not fd.is_enum:
                # Non-enum date field with an invalid value → flag explicitly.
                errors.append(
                    f'Mező "{key}" értéke "{str_val}" nem érvényes dátum '
                    f'({_GRANULARITY_HINT.get(granularity, "YYYY-MM-DD")} '
                    f'formátum szükséges).'
                )
                continue
            # Enum + date: fall through to enum validation below.

        # ── Enum fields ───────────────────────────────────────────────────────
        if fd.is_enum and fd.allowed_values:
            if str_val not in fd.allowed_values:
                allowed_str = ", ".join(fd.allowed_values)
                errors.append(
                    f'Mező "{key}" értéke "{str_val}" érvénytelen. '
                    f'Engedélyezett értékek: {allowed_str}'
                )

    return errors
