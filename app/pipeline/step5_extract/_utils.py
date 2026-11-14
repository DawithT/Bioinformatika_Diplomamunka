"""
app/pipeline/step5_extract/_utils.py

Shared helpers for the Step-5 extraction sub-modules (hard_lab, hard_events,
soft_llm).  Imported by all sub-modules — do not duplicate these helpers.

Public API
----------
to_float_hu(s)
    Parse a Hungarian-locale number string to float.
    Handles comma-decimal-separator ("3,5" → 3.5) and leading numeric parts
    ("1.86 -" → 1.86).  Returns None if not parseable or value is negative.

parse_lab_value(s)
    to_float_hu plus censored results: ">2000.0" / "< 10" are returned as
    the text ">2000.0" / "<10".

strip_print_furniture(pages)
    Join a document's pages without the per-page print footer
    ("… - Esetszám: …", "Nyomtatva: … Oldal n/m") and the page-start "."
    artefact.

col_key(analyte, day, count_map)
    Build a deterministic wide-table column key for a lab draw.
    Mutates count_map to track per-(analyte, day) occurrence counts:
        First draw   → "{analyte}_d{day}"
        Second draw  → "{analyte}_d{day}_1"
        Third draw   → "{analyte}_d{day}_2"
        …

Design notes
------------
- No external dependencies — stdlib only.
- Functions are pure (no logging, no I/O) to keep tests clean.
- count_map is intentionally mutable state: callers pass one dict across all
  analyte-extraction calls for a document so duplicates are detected correctly.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    pass  # keep mypy happy without a hard dep on type stubs


# ── Number parsing ────────────────────────────────────────────────────────────

_LEADING_NUMBER_RE = re.compile(r'^-?[\d]+(?:[.,][\d]+)?')


def to_float_hu(s: str) -> float | None:
    """
    Convert a Hungarian-locale number string to float.

    Handles:
      - Comma as decimal separator: "3,5"  → 3.5
      - Decimal point as-is:        "3.5"  → 3.5
      - Leading numeric part:       "1.86 -" → 1.86
      - Trailing non-numeric:       "70 mL/p" → 70.0
      - Negative values:            "-0.5"  → returns None (lab values are ≥ 0)

    Returns None if:
      - Input is None, empty, or whitespace-only
      - No numeric prefix can be found
      - Parsed value is < 0 (lab values should not be negative)

    Parameters
    ----------
    s : str
        Raw string from the document text.

    Returns
    -------
    float | None
    """
    if not s or not s.strip():
        return None
    cleaned = s.strip().replace(',', '.').replace('\u00a0', '').replace(' ', '')
    m = _LEADING_NUMBER_RE.match(cleaned)
    if not m:
        return None
    try:
        val = float(m.group())
    except ValueError:
        return None
    # Lab values are always ≥ 0; negatives indicate a parsing artefact
    if val < 0:
        return None
    return val


_QUALIFIED_NUMBER_RE = re.compile(r'^([<>≤≥])\s*(\d+(?:[.,]\d+)?)')


def parse_lab_value(s: str) -> float | str | None:
    """
    Parse a lab result: a plain number, or a number with a censoring
    qualifier.

      - "3,5"     → 3.5            (same as to_float_hu)
      - ">2000.0" → ">2000.0"      (qualified: kept as reported, as text)
      - "< 10"    → "<10"          (inner space dropped, comma → dot)

    Returns None if neither form matches.
    """
    if not s or not s.strip():
        return None
    m = _QUALIFIED_NUMBER_RE.match(s.strip())
    if m:
        return m.group(1) + m.group(2).replace(',', '.')
    return to_float_hu(s)


# ── Print furniture ───────────────────────────────────────────────────────────

# Lines the printing system adds to every page of a hospital PDF:
#   "<patient name> - Esetszám: <case number>"
#   "Nyomtatva: <printer> - 2026.03.31 22:33 Oldal 2/7"
# plus a lone "." that pymupdf4llm emits at the top of each page.
_FOOTER_LINE_RE = re.compile(
    r'^[ \t*_]*(?:[^\n]*-\s*Esetsz[aá]m\s*:|Nyomtatva\s*:)[^\n]*$'
    r'|^[^\n]*\bOldal\s*\d+\s*/\s*\d+[ \t*_]*$',
    re.MULTILINE,
)
_PAGE_START_DOT_RE = re.compile(r'^\s*\.(?=\s|$)')


def strip_print_furniture(pages) -> str:
    """
    Join *pages* like ``RoutedDoc.full_text`` but without the per-page print
    footer and the page-start "." artefact.

    The footer carries a print timestamp that date heuristics mistake for an
    event date, and when a page break falls inside a lab line or a report it
    splits the text with the patient's name.  Removing it makes the page
    break invisible to the parsers.
    """
    cleaned = []
    for page in pages:
        page = _PAGE_START_DOT_RE.sub('', page, count=1)
        page = _FOOTER_LINE_RE.sub('', page)
        cleaned.append(page.strip())
    return "\n\n".join(cleaned)


# ── Column key generation ─────────────────────────────────────────────────────

def col_key(analyte: str, day: int, count_map: dict[tuple[str, int], int]) -> str:
    """
    Build the wide-table column key for a single lab draw.

    Naming convention (no day-0 invariant is the caller's responsibility):
        First occurrence  → "{analyte}_d{day}"
        Second occurrence → "{analyte}_d{day}_1"
        Third occurrence  → "{analyte}_d{day}_2"
        N-th  occurrence  → "{analyte}_d{day}_{N-1}"

    Mutates *count_map* to record that this (analyte, day) pair has been seen.
    Pass the same count_map for the full document so duplicates from different
    lab blocks are counted correctly.

    Parameters
    ----------
    analyte : str
        Canonical analyte code (e.g. "creatinine", "inr").
    day : int
        Signed day number (from compute_day).  Never 0.
    count_map : dict[tuple[str, int], int]
        Mutable occurrence counter, shared across the entire parse session.

    Returns
    -------
    str
        Column key, e.g. "creatinine_d1", "inr_d2_1".
    """
    k = (analyte, day)
    n = count_map.get(k, 0)
    count_map[k] = n + 1

    if n == 0:
        return f"{analyte}_d{day}"
    return f"{analyte}_d{day}_{n}"
