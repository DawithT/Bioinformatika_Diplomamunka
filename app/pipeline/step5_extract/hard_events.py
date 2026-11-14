"""
app/pipeline/step5_extract/hard_events.py

Step 5 (extraction) — hard event parsers.

Four deterministic, block-based parsers produce ExtractedField lists from
clinical event documents.  All use source=HEURISTIC, confidence=0.9,
engine="block_parser".

Column key format for all event parsers
----------------------------------------
    {prefix}_d{day}_{n}_{field}

where:
  prefix  — event type prefix (endo_gastro, endo_colono, fibro, uh, rtg, ct, mri, micro)
  day     — signed day number from compute_day() — never 0
  n       — 1-based occurrence index for (prefix, day) pair
  field   — field name (date, result, opinion, E_kpa, …)

Examples:
  endo_gastro_d2_1_date       — first gastroscopy on day 2, date field
  fibro_d10_1_E_kpa           — first fibroscan on day 10, elastography kPa
  uh_d-1_2_result             — second ultrasound on day -1, result text
  micro_d6_1_pathogen         — first microbiology result on day 6, pathogen

Parsers
-------
EndoscopyParser
    Finds all ``Endoszkópos lelet:`` blocks.  Detects gastroscopy vs
    colonoscopy from ``gastroscop`` / ``colonoscop`` in the surrounding
    context window.  Extracts date, result text, and opinion text.

FibroscanParser
    Handles two document formats:
      - Expert format (``FibroScan Expert :``) in standalone ambuláns docs
      - Brief summary format (``{date} Fibroscan`` / ``fibroscan`` header)
        embedded in discharge summaries
    Extracts E_kpa, CAP_dbm, IQR_pct, steatosis_grade, fibrosis_stage.

ImagingParser
    Finds ``Röntgen``, ``Ultrahang``, ``CT``, and ``MR`` / ``MRI`` section
    headers containing a date.  Extracts the result text block between
    consecutive headers.
    Modalities: rtg, uh, ct, mri.

MicrobiologyParser
    Finds ``Bakterológia`` header lines (or lines starting with
    ``Tenyésztés``) and dates them by the header itself.  Extracts sample_type,
    pathogen (or "negatív"), and multiresistant flag.

Design notes
------------
- All parsers accept a ``RoutedDoc`` (not raw text) so they can access
  page-level info and meta for provenance tracking.
- None of them call the LLM — everything is deterministic regex/block parsing.
- Date search always looks *backward* from the block trigger so that the
  most-recently-seen date in the document is used (headers appear before blocks).
- All parsers read the text without the per-page print footer
  (``strip_print_furniture``): its print timestamp would otherwise be the
  most-recently-seen date for a block that starts after a page break.
- Result and opinion text stop at the next section heading, also in the
  markdown form pymupdf4llm produces ("## **GAD5 - …**"), and never carry
  the "Leletező orvos: …" line.
- The ``_find_date_before`` helper returns the latest date found within
  *search_chars* characters before *pos* in *text*.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField, PatientAnchor, RoutedDoc
from app.pipeline.step4_normalize import compute_day
from app.pipeline.step5_extract._utils import strip_print_furniture, to_float_hu

log = logging.getLogger(__name__)

# ── Column-key helpers ────────────────────────────────────────────────────────

def event_col_key(prefix: str, day: int, n: int, field: str) -> str:
    """
    Build a wide-table column key for an event field.

    Format: ``{prefix}_d{day}_{n}_{field}``

    Parameters
    ----------
    prefix : str   e.g. "endo_gastro", "fibro", "uh", "rtg", "mri", "micro"
    day    : int   signed day number (never 0)
    n      : int   1-based event index for this (prefix, day) pair
    field  : str   e.g. "date", "result", "E_kpa"
    """
    return f"{prefix}_d{day}_{n}_{field}"


def _next_event_n(counter: dict[tuple[str, int], int], prefix: str, day: int) -> int:
    """
    Increment and return the 1-based occurrence index for (prefix, day).

    Mutates *counter*.
    """
    k = (prefix, day)
    n = counter.get(k, 0) + 1
    counter[k] = n
    return n


def _make_event_field(
    key: str,
    value: Any,
    doc_type: DocType,
    page: int | None = None,
) -> ExtractedField:
    return ExtractedField(
        key=key,
        value=value,
        source=FieldSource.HEURISTIC,
        doc_type=doc_type,
        confidence=0.9,
        page=page,
        engine="block_parser",
    )


# ── Date parsing helpers ──────────────────────────────────────────────────────

# Matches YYYY.MM.DD in various contexts (dot or dash separators)
_DATE_RE = re.compile(r'(\d{4})[.\-/](\d{2})[.\-/](\d{2})')

# Preferred date header patterns — tried in priority order inside _find_date_before.
# Each regex captures group(1) = YYYY.MM.DD.

# "Érkezés dátuma: YYYY.MM.DD" — endoscopy and fibroscan standalone docs
_ERKEZES_RE = re.compile(
    r'[EÉ]rkez[eé]s\s+d[aá]tuma\s*:\s*(\d{4}\.\d{2}\.\d{2})',
    re.IGNORECASE,
)

# "Teljesítés dátuma: YYYY.MM.DD" — imaging standalone docs
_TELJESITES_RE = re.compile(
    r'Teljes[ií]t[eé]s\s+d[aá]tuma\s*:\s*(\d{4}\.\d{2}\.\d{2})',
    re.IGNORECASE,
)

# "Minta dátuma: YYYY.MM.DD" — labs and microbiology standalone docs
_MINTA_DATE_RE = re.compile(
    r'Minta\s+d[aá]tuma\s*[:\-]?\s*\n?\s*(\d{4}[\.\-/]\d{2}[\.\-/]\d{2})',
    re.IGNORECASE,
)

# "Felvétel dátuma: YYYY.MM.DD" — admission date header (microbiology standalone docs)
_FELVETELI_DATUM_RE = re.compile(
    r'Felv[eé]tel\s+d[aá]tuma\s*:\s*(\d{4}\.\d{2}\.\d{2})',
    re.IGNORECASE,
)

# Discharge summary inline event date: YYYY.MM.DD followed by HH.MM (dots) or HH:MM (colons)
# Used as a fallback when no document-level date header is present.
_DISCHARGE_EVENT_DATE_RE = re.compile(
    r'(\d{4}\.\d{2}\.\d{2})[\s.]*\d{2}[.:]\d{2}',
)


def _parse_date_str(s: str) -> date | None:
    """Parse YYYY.MM.DD (or YYYY-MM-DD) string to date, or return None."""
    if not s:
        return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _find_date_before(
    text: str,
    pos: int,
    search_chars: int = 2000,
    preferred_patterns: list | None = None,
) -> date | None:
    """
    Return the *last* date found in the *search_chars* characters before *pos*.
    Priority:
    1. Any pattern in *preferred_patterns* (caller-supplied, e.g. _ERKEZES_RE
       for endoscopy, _TELJESITES_RE for imaging, _MINTA_DATE_RE for micro).
    2. Discharge-summary inline event date (YYYY.MM.DD HH.MM or HH:MM).
    3. Any bare YYYY.MM.DD in the window (last occurrence).
    Returns None if no date is found.
    """
    window = text[max(0, pos - search_chars): pos]

    # 1. Preferred document-header patterns
    if preferred_patterns:
        for pattern in preferred_patterns:
            matches = list(pattern.finditer(window))
            if matches:
                return _parse_date_str(matches[-1].group(1))

    # 2. Discharge-summary inline date with time component
    inline_matches = list(_DISCHARGE_EVENT_DATE_RE.finditer(window))
    if inline_matches:
        return _parse_date_str(inline_matches[-1].group(1))

    # 3. Bare date — last YYYY.MM.DD in window
    all_dates = list(_DATE_RE.finditer(window))
    if all_dates:
        return _parse_date_str(all_dates[-1].group(0))

    return None


# "Leletező orvos: Dr. …" — the reporting physician, part of the report
# header; removed from stored result text (it is a name, not a finding).
_REPORTER_RE = re.compile(r'Leletez[őo]\s+orvos\b[^\n]*', re.IGNORECASE)


def _truncate_text(text: str, max_chars: int = 4000) -> str:
    """
    Remove the reporting physician's name, trim whitespace and cap length.

    The cap only guards against a runaway block; the stop patterns end a
    report at the next section header well before it.
    """
    cleaned = _REPORTER_RE.sub('', text)
    cleaned = re.sub(r'[\r\n]+', '\n', cleaned.strip())
    cleaned = re.sub(r' {2,}', ' ', cleaned)
    return cleaned[:max_chars].strip()


# Optional markdown prefix of a line: pymupdf4llm renders section headers as
# "## **GAD5 - …**", so line-start stop patterns must look past "#" and "*".
_MD_LINE_PREFIX = r'\n\s*(?:#{1,6}\s*)?(?:\*\*)?\s*'

# A discharge-summary section heading: "## **SZZ - Kardiológiai Klinika …",
# "## **BRA-Bőrgyógyászati …", "## **2026.01.16 Fibroscan**".
_SECTION_TITLE = (
    r'\s*(?:\*\*)?\s*'
    r'(?:[A-ZÁÉÍÓÖŐÚÜŰ]{2,5}\d*\s*[-–]|\d{4}\.\d{2}\.\d{2})'
)
_SECTION_HEADING = r'\n\s*#{1,6}' + _SECTION_TITLE


# ── EndoscopyParser ───────────────────────────────────────────────────────────

# Block trigger: Hungarian "Endoszkópos lelet:"
_ENDO_LELET_RE = re.compile(r'Endoszk[oó]pos\s+lelet\s*:', re.IGNORECASE)

# Opinion block start
_VELEMENYRE = re.compile(r'V[eé]lem[eé]ny\s*:', re.IGNORECASE)

# Opinion block terminators (stop extracting opinion when one of these appears)
_OPINION_STOP_RE = re.compile(
    _MD_LINE_PREFIX
    + r'(?:Javasolt|Diagnózis|Beavatkozás|Szövettani|Fotódok|BNO|WHO|Debrecen)'
    # …or the next section of the discharge summary
    + r'|' + _SECTION_HEADING,
    re.IGNORECASE,
)

# Sub-type detection (look in the preceding ~1500 chars)
_GASTRO_SUBTYPE_RE = re.compile(r'gastroscop|gastroscopia|gasztroszkóp', re.IGNORECASE)
_COLONO_SUBTYPE_RE = re.compile(r'colonoscop|colonoscopia|kolonoscop|COLONOSCOPIA', re.IGNORECASE)


class EndoscopyParser:
    """
    Extract endoscopy events (gastroscopy and colonoscopy) from a document.

    Detects all ``Endoszkópos lelet:`` blocks, classifies each as
    gastro/colono, extracts date, result text, and opinion text.
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        """
        Parse all endoscopy events in *doc*.

        Parameters
        ----------
        doc    : RoutedDoc  — full ingested document
        anchor : PatientAnchor — for day computation

        Returns
        -------
        list[ExtractedField]
        """
        text = strip_print_furniture(doc.pages)
        results: list[ExtractedField] = []
        event_counter: dict[tuple[str, int], int] = {}

        for match in _ENDO_LELET_RE.finditer(text):
            block_start = match.end()

            # ── Determine sub-type ────────────────────────────────────────
            context_window = text[max(0, match.start() - 1500): match.start()]
            if _COLONO_SUBTYPE_RE.search(context_window):
                subtype = "colono"
            elif _GASTRO_SUBTYPE_RE.search(context_window):
                subtype = "gastro"
            else:
                # Can't classify — look forward in the next 200 chars too
                fwd = text[match.start(): match.start() + 200]
                if _COLONO_SUBTYPE_RE.search(fwd):
                    subtype = "colono"
                else:
                    subtype = "gastro"  # default
            prefix = f"endo_{subtype}"

            # ── Extract date ──────────────────────────────────────────────
            event_date = _find_date_before(
                text, match.start(), 2000,
                preferred_patterns=[_ERKEZES_RE],
            )
            if event_date is None:
                log.warning("EndoscopyParser: no date found before block at pos %d", match.start())
                continue

            day = compute_day(event_date, anchor)
            if day == 0:
                log.warning("EndoscopyParser: day 0 for %s on %s — skipped", prefix, event_date)
                continue

            n = _next_event_n(event_counter, prefix, day)

            # ── Extract result block ──────────────────────────────────────
            # Result: from block_start to "Vélemény:"
            vel_m = _VELEMENYRE.search(text, block_start)
            if vel_m:
                result_text = text[block_start: vel_m.start()]
                opinion_start = vel_m.end()

                # Opinion: from "Vélemény:" to next structural terminator
                stop_m = _OPINION_STOP_RE.search(text, opinion_start)
                if stop_m:
                    opinion_text = text[opinion_start: stop_m.start()]
                else:
                    # No explicit terminator — take up to 500 chars
                    opinion_text = text[opinion_start: opinion_start + 500]
            else:
                # No "Vélemény:" — everything after trigger is the result
                result_text = text[block_start: block_start + 1000]
                opinion_text = ""

            date_str = event_date.isoformat()
            result_clean = _truncate_text(result_text)
            opinion_clean = _truncate_text(opinion_text)

            # ── Build ExtractedField objects ──────────────────────────────
            results.append(_make_event_field(
                event_col_key(prefix, day, n, "date"),
                date_str,
                doc.doc_type,
            ))
            if result_clean:
                results.append(_make_event_field(
                    event_col_key(prefix, day, n, "result"),
                    result_clean,
                    doc.doc_type,
                ))
            if opinion_clean:
                results.append(_make_event_field(
                    event_col_key(prefix, day, n, "opinion"),
                    opinion_clean,
                    doc.doc_type,
                ))

        return results


# ── FibroscanParser ───────────────────────────────────────────────────────────

# Expert format (standalone ambuláns doc)
_FIBRO_EXPERT_RE = re.compile(r'FibroScan\s+Expert\s*:', re.IGNORECASE)
_FIBRO_E_RE    = re.compile(r'E\s*:\s*([\d.,]+)\s*kPa', re.IGNORECASE)
_FIBRO_CAP_RE  = re.compile(r'CAP\s*:\s*([\d.,]+)\s*dB', re.IGNORECASE)
_FIBRO_IQR_RE  = re.compile(r'IQR(?:/Med)?\s*:\s*([\d.,]+)\s*%', re.IGNORECASE)
_FIBRO_STEAT_LABEL_RE = re.compile(r'Steatosis\s+grade\s*:\s*(S[0-3])', re.IGNORECASE)
_FIBRO_FIBR_LABEL_RE  = re.compile(r'Fibrosis\s+st[aá]dium\s*:\s*(F[0-4])', re.IGNORECASE)

# Brief/summary format embedded in discharge summary
# e.g. "2026.01.16 Fibroscan" or "Fibroscan:" standalone line
_FIBRO_BRIEF_RE = re.compile(
    r'(?:(\d{4}\.\d{2}\.\d{2})\s+)?[Ff]ibroscan\b',
)
# Values in brief format (whitespace-separated, no colon)
_FIBRO_BRIEF_E_RE   = re.compile(r'([\d.,]+)\s*kPa', re.IGNORECASE)
_FIBRO_BRIEF_CAP_RE = re.compile(r'CAP\s+([\d.,]+)\s*dB', re.IGNORECASE)
_FIBRO_BRIEF_IQR_RE = re.compile(r'IQR\s+([\d.,]+)%', re.IGNORECASE)
# Steatosis/fibrosis grade on a line like "S0 F4"
_FIBRO_GRADE_LINE_RE = re.compile(r'\b(S[0-3])\b.*\b(F[0-4])\b')


class FibroscanParser:
    """
    Extract fibroscan (transient elastography) events from a document.

    Handles two formats:
      1. **Expert format** — ``FibroScan Expert : E: 75.0 kPa, CAP:250 dB/m ...``
         followed by ``Steatosis grade: S0`` and ``Fibrosis stádium: F4``
      2. **Brief summary format** — ``{date} Fibroscan\\nMáj ... 75.0 kPa, IQR 0%, CAP 250 dB/m``
         followed by ``S0 F4`` on a single line

    Both formats produce the same five fields per event:
      E_kpa, CAP_dbm, IQR_pct, steatosis_grade, fibrosis_stage
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        """
        Parse all FibroScan events from *doc*.

        Handles the Expert format (detailed per-measurement block) and the
        brief summary format embedded in discharge summaries.  Returns one set
        of sub-fields per event: E_kpa, CAP_dbm, IQR_pct, steatosis_grade,
        fibrosis_stage.

        Parameters
        ----------
        doc    : RoutedDoc      — fully ingested document
        anchor : PatientAnchor  — admission anchor for day computation

        Returns
        -------
        list[ExtractedField] — one entry per (event, sub-field) pair
        """
        text = strip_print_furniture(doc.pages)
        results: list[ExtractedField] = []
        event_counter: dict[tuple[str, int], int] = {}
        # Track character positions already consumed to avoid double-counting
        consumed_positions: set[int] = set()

        # ── Expert format first ───────────────────────────────────────────
        for match in _FIBRO_EXPERT_RE.finditer(text):
            consumed_positions.add(match.start())
            block = text[match.start(): match.start() + 400]

            event_date = _find_date_before(
                text, match.start(), 3000,
                preferred_patterns=[_ERKEZES_RE],
            )
            if event_date is None:
                log.warning("FibroscanParser(Expert): no date found near pos %d", match.start())
                continue

            fields = self._parse_expert_block(block, event_date, doc.doc_type, anchor, event_counter)
            results.extend(fields)

        # ── Brief/summary format ──────────────────────────────────────────
        for match in _FIBRO_BRIEF_RE.finditer(text):
            # Skip if this position overlaps with an expert block
            if any(abs(match.start() - cp) < 200 for cp in consumed_positions):
                continue

            # Date: prefer the inline date in group(1); fall back to backward search
            if match.group(1):
                event_date = _parse_date_str(match.group(1))
            else:
                event_date = _find_date_before(
                    text, match.start(), 3000,
                    preferred_patterns=[_ERKEZES_RE],
                )

            if event_date is None:
                log.debug("FibroscanParser(Brief): no date at pos %d", match.start())
                continue

            # Extract the block (up to 200 chars after the trigger)
            block = text[match.start(): match.start() + 250]
            fields = self._parse_brief_block(block, event_date, doc.doc_type, anchor, event_counter)
            results.extend(fields)

        return results

    # ── Private: Expert block ─────────────────────────────────────────────

    def _parse_expert_block(
        self,
        block: str,
        event_date: date,
        doc_type: DocType,
        anchor: PatientAnchor,
        counter: dict,
    ) -> list[ExtractedField]:
        day = compute_day(event_date, anchor)
        if day == 0:
            return []
        n = _next_event_n(counter, "fibro", day)

        fields: list[ExtractedField] = []

        e_m   = _FIBRO_E_RE.search(block)
        cap_m = _FIBRO_CAP_RE.search(block)
        iqr_m = _FIBRO_IQR_RE.search(block)
        # Grades may appear on lines after the main values line
        # Search in a wider window (up to 300 chars)
        steat_m = _FIBRO_STEAT_LABEL_RE.search(block)
        fibr_m  = _FIBRO_FIBR_LABEL_RE.search(block)

        if e_m:
            v = to_float_hu(e_m.group(1))
            if v is not None:
                fields.append(_make_event_field(
                    event_col_key("fibro", day, n, "E_kpa"), v, doc_type))

        if cap_m:
            v = to_float_hu(cap_m.group(1))
            if v is not None:
                fields.append(_make_event_field(
                    event_col_key("fibro", day, n, "CAP_dbm"), v, doc_type))

        if iqr_m:
            v = to_float_hu(iqr_m.group(1))
            if v is not None:
                fields.append(_make_event_field(
                    event_col_key("fibro", day, n, "IQR_pct"), v, doc_type))

        if steat_m:
            fields.append(_make_event_field(
                event_col_key("fibro", day, n, "steatosis_grade"),
                steat_m.group(1), doc_type))

        if fibr_m:
            fields.append(_make_event_field(
                event_col_key("fibro", day, n, "fibrosis_stage"),
                fibr_m.group(1), doc_type))

        return fields

    # ── Private: Brief summary block ──────────────────────────────────────

    def _parse_brief_block(
        self,
        block: str,
        event_date: date,
        doc_type: DocType,
        anchor: PatientAnchor,
        counter: dict,
    ) -> list[ExtractedField]:
        day = compute_day(event_date, anchor)
        if day == 0:
            return []

        e_m   = _FIBRO_BRIEF_E_RE.search(block)
        cap_m = _FIBRO_BRIEF_CAP_RE.search(block)
        iqr_m = _FIBRO_BRIEF_IQR_RE.search(block)
        grade_m = _FIBRO_GRADE_LINE_RE.search(block)

        # Only register the event if at least E_kpa or CAP_dbm was found
        if e_m is None and cap_m is None:
            return []

        n = _next_event_n(counter, "fibro", day)
        fields: list[ExtractedField] = []

        if e_m:
            v = to_float_hu(e_m.group(1))
            if v is not None:
                fields.append(_make_event_field(
                    event_col_key("fibro", day, n, "E_kpa"), v, doc_type))

        if cap_m:
            v = to_float_hu(cap_m.group(1))
            if v is not None:
                fields.append(_make_event_field(
                    event_col_key("fibro", day, n, "CAP_dbm"), v, doc_type))

        if iqr_m:
            v = to_float_hu(iqr_m.group(1))
            if v is not None:
                fields.append(_make_event_field(
                    event_col_key("fibro", day, n, "IQR_pct"), v, doc_type))

        if grade_m:
            fields.append(_make_event_field(
                event_col_key("fibro", day, n, "steatosis_grade"),
                grade_m.group(1), doc_type))
            fields.append(_make_event_field(
                event_col_key("fibro", day, n, "fibrosis_stage"),
                grade_m.group(2), doc_type))

        return fields


# ── ImagingParser ─────────────────────────────────────────────────────────────

# Section header that introduces an imaging result block.
# Captures: modality keyword, date in the header line.
# Format: "...CODE... (Ultrahang|Röntgen|CT) ... YYYY.MM.DD. HH.MM ..."
# Classic format: modality keyword then date on same line
# e.g. "Mellkas Röntgen 2026.01.13 08:00"
_IMAGING_HEADER_CLASSIC_RE = re.compile(
    r'[^\n]*?\b(Röntgen(?:vizsgálat)?|Ultrahang|CT|MR[IT]?(?:\svizsgálat)?)\b[^\n]*?'
    r'(\d{4}\.\d{2}\.\d{2})[^\n]*\n',
    re.IGNORECASE,
)
# Date-first format (older discharge summary):
# e.g. "2018.10.03 08:00 - (RKQRT) Röntgenvizsgálat"
_IMAGING_HEADER_DATE_FIRST_RE = re.compile(
    r'(\d{4}\.\d{2}\.\d{2})\s+\d{2}[.:]\d{2}\s*-[^\n]*?'
    r'(Röntgen(?:vizsgálat)?|Ultrahang|CT|MR[IT]?(?:\svizsgálat)?)[^\n]*\n',
    re.IGNORECASE,
)
_MODALITY_MAP = {
    'röntgen':        'rtg',
    'ultrahang':      'uh',
    'ct':             'ct',
    'mr':             'mri',
    'mri':            'mri',
    'mrt':            'mri',
    'mr vizsgálat':   'mri',
    'mri vizsgálat':  'mri',
    'mrt vizsgálat':  'mri',
}

# Stop reading imaging result when one of these patterns appears on a new line
_IMAGING_STOP_RE = re.compile(
    _SECTION_HEADING + r'|'
    + _MD_LINE_PREFIX + r'(?:'
    r'[A-ZÁÉÍÓÖŐÚÜŰ]{2,5}\s*[-–]\s*[^\n]{5,}'  # new section header (CODE - Name...)
    r'|GAD\d+\s*[-–]'                            # GAD laboratory header
    r'|MBZ\s*[-–]'                               # bacteriology header
    r'|SZZ\s*[-–]'                               # cardiology header
    r'|BRA\s*[-–]'                               # dermatology header
    r'|LMI\s+K'                                  # LMI lab header
    r'|Bartók'                                   # external lab header
    r')',
    re.IGNORECASE,
)

# Markdown heading lines directly after an imaging header: the wrapped rest
# of that header, not the report.  A heading that starts the next section is
# left alone so the stop pattern still sees it.
_HEADER_TAIL_RE = re.compile(
    r'^\s*(?:#{1,6}(?!' + _SECTION_TITLE + r')'
    r'[^\n]*(?:\n|$)\s*)+',
    re.IGNORECASE,
)


class ImagingParser:
    """
    Extract imaging events (UH, RTG, CT, MRI) from a document.

    Detects section headers that contain the modality keyword and a date,
    then extracts the result text until the next section header.
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        """
        Parse all imaging events (UH, RTG, CT, MRI) from *doc*.

        Detects section headers containing a modality keyword and a date, then
        extracts the result text until the next section header.

        Parameters
        ----------
        doc    : RoutedDoc      — fully ingested document
        anchor : PatientAnchor  — admission anchor for day computation

        Returns
        -------
        list[ExtractedField] — one entry per (event, sub-field) pair
        """
        text = strip_print_furniture(doc.pages)
        results: list[ExtractedField] = []
        event_counter: dict[tuple[str, int], int] = {}

        # Collect from both header formats, normalise to
        # (modality_raw, event_date, match_start, match_end)
        raw_hits: list[tuple[str, "date | None", int, int]] = []

        for m in _IMAGING_HEADER_CLASSIC_RE.finditer(text):
            raw_hits.append((m.group(1), _parse_date_str(m.group(2)), m.start(), m.end()))

        classic_positions = {m.start() for m in _IMAGING_HEADER_CLASSIC_RE.finditer(text)}

        for m in _IMAGING_HEADER_DATE_FIRST_RE.finditer(text):
            # Skip if position overlaps with a classic match
            if any(abs(m.start() - cp) < 10 for cp in classic_positions):
                continue
            raw_hits.append((m.group(2), _parse_date_str(m.group(1)), m.start(), m.end()))

        # Sort by document position so event numbering is deterministic
        raw_hits.sort(key=lambda t: t[2])

        for i, (modality_raw, event_date, _hit_start, hit_end) in enumerate(raw_hits):
            modality = _MODALITY_MAP.get(modality_raw.lower())
            if modality is None:
                continue

            if event_date is None:
                continue

            day = compute_day(event_date, anchor)
            if day == 0:
                log.warning("ImagingParser: day 0 for %s on %s — skipped", modality, event_date)
                continue

            n = _next_event_n(event_counter, modality, day)

            # Result text: from end of this header to start of the next
            result_start = hit_end
            next_hit_start = raw_hits[i + 1][2] if i + 1 < len(raw_hits) else len(text)
            next_header_start = next_hit_start

            # A header that wraps onto the next line leaves its tail there as
            # another markdown heading ("## **részlelet) Leletező orvos: …**")
            result_text = _HEADER_TAIL_RE.sub('', text[result_start: next_header_start])

            # Also stop at any strong structural break within that window
            stop_m = _IMAGING_STOP_RE.search(result_text)
            if stop_m:
                result_text = result_text[:stop_m.start()]

            result_clean = _truncate_text(result_text)

            results.append(_make_event_field(
                event_col_key(modality, day, n, "date"),
                event_date.isoformat(),
                doc.doc_type,
            ))
            if result_clean:
                results.append(_make_event_field(
                    event_col_key(modality, day, n, "result"),
                    result_clean,
                    doc.doc_type,
                ))

        return results


# ── MicrobiologyParser ────────────────────────────────────────────────────────

# Block header patterns
# Match the full header line: a department keyword anywhere on the line, or a
# line that starts with "Tenyésztés".  A result sentence that merely mentions
# culturing ("Gomba tenyésztése negatív.") is not a header.
_MICRO_HEADER_RE = re.compile(
    r'[^\n]*(?:Bakterológia|MIKROBIOL)[^\n]*'
    r'|^[ \t#*]*Tenyésztés[^\n]*',
    re.IGNORECASE | re.MULTILINE,
)
# Used to extract the last parenthetical on a line (sample type)
_LAST_PAREN_RE = re.compile(r'\(([^)\n]+)\)[^\(]*$')

# Negative result indicators
_MICRO_NEGATIVE_BACTERIA_RE = re.compile(
    r'Baktérium\s+nem\s+tenyészett\s+ki',
    re.IGNORECASE,
)
_MICRO_NEGATIVE_FUNGUS_RE = re.compile(
    r'Gomba\s+tenyésztése\s+negat[ií]v',
    re.IGNORECASE,
)

# Multi-resistant indicators
_MULTI_RE = re.compile(r'MRSA|ESBL|MDR|multirezisztens|multi.rezisztens', re.IGNORECASE)


class MicrobiologyParser:
    """
    Extract microbiology culture results from a document.

    Finds ``Bakterológia`` / ``tenyésztés`` headers, extracts:
      - sample_type   (from header parenthetical, e.g. "ascites")
      - pathogen      (pathogen name or "negatív" for no growth)
      - multiresistant (True/False/"negatív")
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        """
        Parse all microbiology culture events from *doc*.

        Finds Bakterológia / tenyésztés section headers and extracts:
        sample_type (from the header parenthetical), pathogen name or
        "negatív" for negative cultures, and multiresistant flag.

        Parameters
        ----------
        doc    : RoutedDoc      — fully ingested document
        anchor : PatientAnchor  — admission anchor for day computation

        Returns
        -------
        list[ExtractedField] — one entry per (event, sub-field) pair
        """
        text = strip_print_furniture(doc.pages)
        results: list[ExtractedField] = []
        event_counter: dict[tuple[str, int], int] = {}

        for match in _MICRO_HEADER_RE.finditer(text):
            header_line = match.group(0)

            # Date: a labelled "Minta dátuma:" / "Felvétel dátuma:" before the
            # header (standalone reports), else the date on the header line
            # itself, else the nearest date before it.  The header's own date
            # must win over the backward search, which in a discharge summary
            # lands in the previous section.
            window = text[max(0, match.start() - 3000): match.start()]
            event_date = None
            for pattern in (_MINTA_DATE_RE, _FELVETELI_DATUM_RE):
                labelled = list(pattern.finditer(window))
                if labelled:
                    event_date = _parse_date_str(labelled[-1].group(1))
                    break
            if event_date is None:
                event_date = (_parse_date_str(header_line)
                              or _find_date_before(text, match.start(), 3000))
            if event_date is None:
                continue

            day = compute_day(event_date, anchor)
            if day == 0:
                log.warning("MicrobiologyParser: day 0 on %s — skipped", event_date)
                continue

            # Sample type: last parenthetical on the header line, if present
            paren_m = _LAST_PAREN_RE.search(header_line)
            sample_type = paren_m.group(1).strip() if paren_m else "Unknown"

            # Block text (next ~300 chars after header)
            block = text[match.end(): match.end() + 300]

            n = _next_event_n(event_counter, "micro", day)

            # Determine pathogen / multiresistant from block text
            bacteria_negative = bool(_MICRO_NEGATIVE_BACTERIA_RE.search(block))
            fungus_negative   = bool(_MICRO_NEGATIVE_FUNGUS_RE.search(block))
            multiresistant    = bool(_MULTI_RE.search(block))

            if bacteria_negative and fungus_negative:
                pathogen = "negatív"
                multiresistant = False
            elif bacteria_negative:
                pathogen = "baktérium negatív"
            elif fungus_negative:
                pathogen = "gomba negatív"
            else:
                # Attempt to find the pathogen name — look for a line not
                # starting with a label keyword
                pathogen = self._extract_pathogen(block)

            results.append(_make_event_field(
                event_col_key("micro", day, n, "sample_type"),
                sample_type,
                doc.doc_type,
            ))
            results.append(_make_event_field(
                event_col_key("micro", day, n, "pathogen"),
                pathogen,
                doc.doc_type,
            ))
            results.append(_make_event_field(
                event_col_key("micro", day, n, "multiresistant"),
                multiresistant,
                doc.doc_type,
            ))

        return results

    def _extract_pathogen(self, block: str) -> str:
        """
        Try to find the pathogen name in the block text.

        Looks for lines that look like organism names (containing genus/species
        or common pathogen words). Returns the first matching line, or "Unknown".
        """
        known_pathogens = re.compile(
            r'(?:Staphylococcus|Streptococcus|Escherichia|Klebsiella|Pseudomonas|'
            r'Enterococcus|Candida|Aspergillus|Enterobacter|Proteus|'
            r'Acinetobacter|Clostridium)',
            re.IGNORECASE,
        )
        for line in block.splitlines():
            line = line.strip()
            if known_pathogens.search(line):
                return line
        return "Unknown"
