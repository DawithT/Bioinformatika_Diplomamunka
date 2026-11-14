"""
app/pipeline/step3_anchor.py

Step 3 of the pipeline: patient anchor extraction.

Extracts the core patient identity and admission window from a
DISCHARGE_SUMMARY document.

Public API:
    extract_anchor(doc: RoutedDoc) -> PatientAnchor
        Parses the document text and returns a PatientAnchor.
        Raises AnchorNotFoundError if the admission date cannot be found
        (the pipeline cannot proceed without this date).

AnchorNotFoundError is the ONLY exception that halts the entire pipeline.
All other extraction failures (missing name, TAJ, DOB) are tolerated by
substituting a safe placeholder and logging a warning.

Hungarian date formats handled:
    YYYY.MM.DD HH:MM        — used in Felvétel / Elbocsátás lines
    YYYY. MM. DD.           — used in Születési hely és dátum
    YYYY.MM.DD              — compact variant

Anonymised fixture notes:
    The fixture PDFs have names/TAJ/DOB painted over with "xxx" placeholder
    text. The regexes still match the field labels; they extract the
    placeholder text as-is, which is acceptable for fingerprinting purposes.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime
from pathlib import Path

from app.domain.models import PatientAnchor, RoutedDoc

log = logging.getLogger(__name__)


# ── Exceptions ────────────────────────────────────────────────────────────────

class AnchorNotFoundError(Exception):
    """
    Raised when the admission date cannot be extracted from the discharge
    summary.  This is the only error that halts the entire pipeline.

    Without an admission date we cannot compute day-numbers, so there is no
    point continuing.
    """


# ── Compiled regex patterns ───────────────────────────────────────────────────

# Felvétel dátuma: 2026.01.12 04:43   (new format)
# Felvétel......: 2018.10.03 09:27    (old format — dots as padding, no "dátuma")
_RE_ADMISSION = re.compile(
    r"Felv[eé]tel"
    r"(?:\s+d[aá]tuma|[.\s]*)?"   # optional "dátuma" or dot-padding
    r"\s*:\s*"
    r"(\d{4})\.(\d{1,2})\.(\d{1,2})"   # YYYY.MM.DD
    r"(?:\s+(\d{1,2}):(\d{2}))?",       # optional HH:MM
    re.IGNORECASE,
)

# Elbocsátás dátuma: 2026.01.20 13:32  (new format)
# Elbocsátás....: 2018.10.15 16:12     (old format — dots as padding, no "dátuma")
_RE_DISCHARGE = re.compile(
    r"Elbocsát[aá]s"
    r"(?:\s+d[aá]tuma|[.\s]*)?"   # optional "dátuma" or dot-padding
    r"\s*:\s*"
    r"(\d{4})\.(\d{1,2})\.(\d{1,2})"   # YYYY.MM.DD
    r"(?:\s+(\d{1,2}):(\d{2}))?",       # optional HH:MM
    re.IGNORECASE,
)

# "Név: Beteg Neve" (new format) or "Beteg neve: Beteg Neve" (old format)
# Also handles: **Név:** (markdown bold from pymupdf4llm)
#               Név.........: (dot-padded old format)
#
# Terminator: stop capturing when we hit (a) a closing markdown bold "**",
# (b) the next known header label on the same line — pymupdf4llm collapses
# the whole header into one line where labels sit a single space apart, so
# relying on "\s{2,}" alone over-captures into subsequent fields,
# (c) two-or-more spaces (old multi-column layout), or (d) end of line.
_RE_NAME = re.compile(
    r"^[|\s\-]*\*{0,2}\s*(?:Beteg\s+neve|N(?:e.?|é)v)\s*\*{0,2}[.\s]*:\s*\*{0,2}\s*"
    r"(.+?)"
    r"(?=\s*\*\*|"
    r"\s+(?:TAJ|Sz[üu]let[eé]skori|KBA|Eset(?:sz[aá]m)?|T[öo]rzs(?:sz[aá]m)?|Anyja|Lakc[ií]m|"
    r"Felv[eé]tel|Elbocs[aá]t|Telefon|Email|T[eé]r[ií]t[eé]si|Bek[üu]ld|Elhelyez|Sz[üu]let[eé]si)\b|"
    r"\s{2,}|$)",
    re.IGNORECASE | re.MULTILINE,
)

# TAJ: xxx-xxx-xxx  or  TAJ: 123 456 789
_RE_TAJ = re.compile(
    r"TAJ\s*:\s*([\dxX\s\-]+)",
    re.IGNORECASE,
)

# Születési hely és dátum: Város, YYYY. MM. DD.
# Also handles compact: Város, YYYY.MM.DD
_RE_DOB = re.compile(
    r"Sz[üu]let[eé]si\s+hely\s+[eé]s\s+d[aá]tum\s*:\s*"
    r"[^,]*,\s*"                                # birth place (ignored)
    r"(\d{4})\s*\.\s*(\d{1,2})\s*\.\s*(\d{1,2})",
    re.IGNORECASE,
)

# ── Public entry-point ────────────────────────────────────────────────────────

def extract_anchor(doc: RoutedDoc) -> PatientAnchor:
    """
    Extract patient identity and admission window from a discharge summary.

    Parameters
    ----------
    doc : RoutedDoc
        Must be a DISCHARGE_SUMMARY document (doc_type is not checked here —
        callers are responsible for routing correctly).

    Returns
    -------
    PatientAnchor
        Frozen dataclass with name, dob, taj, admission_start,
        discharge_end, and fingerprint.

    Raises
    ------
    AnchorNotFoundError
        If the admission date cannot be found in the document text.
        This is the only error that halts the pipeline.
    """
    text = doc.full_text

    # ── Admission date (required) ────────────────────────────────────────────
    admission_start = _parse_datetime_field(text, _RE_ADMISSION, "Felvétel dátuma")
    if admission_start is None:
        raise AnchorNotFoundError(
            f"Could not extract 'Felvétel dátuma' from {doc.path.name}. "
            "This field is required — pipeline cannot continue without it."
        )

    # ── Discharge date (required for window calculation) ─────────────────────
    discharge_end = _parse_datetime_field(text, _RE_DISCHARGE, "Elbocsátás dátuma")
    if discharge_end is None:
        log.warning(
            "anchor: 'Elbocsátás dátuma' not found in %s; "
            "defaulting to admission_start (single-day window).",
            doc.path.name,
        )
        discharge_end = admission_start

    # ── Patient identity (best-effort; placeholders on failure) ─────────────
    name = _extract_name(text, doc.path)
    taj  = _extract_taj(text, doc.path)
    dob  = _extract_dob(text, doc.path)

    anchor = PatientAnchor.create(
        name=name,
        dob=dob,
        taj=taj,
        admission_start=admission_start,
        discharge_end=discharge_end,
    )
    log.info(
        "anchor: extracted fingerprint=%s admission=%s discharge=%s",
        anchor.fingerprint,
        admission_start.isoformat(),
        discharge_end.isoformat(),
    )
    return anchor


# ── Field extractors ──────────────────────────────────────────────────────────

def _parse_datetime_field(
    text: str,
    pattern: re.Pattern,
    label: str,
) -> datetime | None:
    """
    Match *pattern* in *text* and return a datetime.

    Groups must be: (year, month, day[, hour, minute]).
    Returns None if no match or if the values are out of range.
    """
    m = pattern.search(text)
    if not m:
        return None
    try:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour   = int(m.group(4)) if m.lastindex and m.lastindex >= 4 and m.group(4) else 0
        minute = int(m.group(5)) if m.lastindex and m.lastindex >= 5 and m.group(5) else 0
        return datetime(year, month, day, hour, minute)
    except (ValueError, TypeError) as exc:
        log.warning("anchor: could not parse %s: %s", label, exc)
        return None


def _extract_name(text: str, path: Path) -> str:
    """Extract patient name from 'Név: ...' line.  Returns placeholder on failure."""
    m = _RE_NAME.search(text)
    if not m:
        log.warning("anchor: 'Név' not found in %s; using placeholder", path.name)
        return "UNKNOWN"
    return m.group(1).strip()


def _extract_taj(text: str, path: Path) -> str:
    """
    Extract TAJ szám.  Normalises by keeping only digits and 'x' characters
    (the 'x' preserves anonymised placeholders for testing).
    Returns placeholder on failure.
    """
    m = _RE_TAJ.search(text)
    if not m:
        log.warning("anchor: 'TAJ' not found in %s; using placeholder", path.name)
        return "000000000"
    # Keep digits and lowercase x (anonymisation placeholder)
    raw = m.group(1).strip()
    normalised = re.sub(r"[^\dx]", "", raw.lower())
    return normalised or "000000000"


def _extract_dob(text: str, path: Path) -> date:
    """
    Extract date of birth from 'Születési hely és dátum: Place, YYYY. MM. DD.' line.
    Returns date(1900, 1, 1) as a safe placeholder on failure.
    """
    m = _RE_DOB.search(text)
    if not m:
        log.warning(
            "anchor: 'Születési hely és dátum' not found in %s; using placeholder DOB",
            path.name,
        )
        return date(1900, 1, 1)
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        log.warning("anchor: invalid DOB in %s: %s", path.name, exc)
        return date(1900, 1, 1)
