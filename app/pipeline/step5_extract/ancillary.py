"""
app/pipeline/step5_extract/ancillary.py

Ancillary-document section extractor.

An "ancillary" document in this pipeline is one that is either
classified-but-unrouted (``AMBULANSLAP``) or unclassified (``UNKNOWN``).
Neither type contributes to the wide table, but both are surfaced in the
``ancillary_docs.xlsx`` audit sheet so reviewers can confirm nothing
load-bearing was missed.

For ``AMBULANSLAP`` documents we additionally pull three Hungarian-titled
sections commonly present in outpatient summaries:

  - ``VÉLEMÉNY``       — narrative clinical opinion (raw text)
  - ``DIAGNÓZISOK``    — structured: BNO codes + descriptions
  - ``BEAVATKOZÁSOK``  — narrative list of procedures (raw text)

For ``UNKNOWN`` documents the same extractors run best-effort. If none of the
section headers are present (likely) the returned record carries only the
routing-triage fields that ``UnknownDocRecord`` previously held.

Public API
----------
extract(doc, case_id, routing_method, failure_reason, best_score)
    Return one ``AncillaryRecord`` summarising the document.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

from app.domain.enums import DocType
from app.domain.models import AncillaryRecord

if TYPE_CHECKING:
    from app.domain.models import RoutedDoc


# ── Section header patterns ───────────────────────────────────────────────────
#
# Each header must start on its own line (anchored with \n or ^) and may be
# followed by punctuation/whitespace. We accept singular/plural Hungarian
# forms and the ASCII-folded variants produced by OCR.

_VELEMENY_RE = re.compile(
    r'(?:^|\n)\s*(?:V[ÉE]LEM[ÉE]NY|Velemeny|Összefoglal[óo])\s*:?\s*\n',
    re.IGNORECASE,
)

_DIAGNOZISOK_RE = re.compile(
    r'(?:^|\n)\s*(?:DIAGN[ÓO]ZIS(?:OK)?|Diagnozis(?:ok)?)\s*:?\s*\n',
    re.IGNORECASE,
)

_BEAVATKOZASOK_RE = re.compile(
    r'(?:^|\n)\s*(?:BEAVATKOZ[ÁA]S(?:OK)?|Beavatkozas(?:ok)?)\s*:?\s*\n',
    re.IGNORECASE,
)

# BNO code line shape (re-uses the same grammar as hard_structured._BNO_SIMPLE_RE
# but is kept local so this module has no coupling to the discharge BNO parser).
_BNO_LINE_RE = re.compile(
    r'^\s*([A-Z]\d{2,4}[A-Z]?\d?)\s+(.+?)\s*$',
    re.IGNORECASE | re.MULTILINE,
)

# Maximum characters retained per raw section — avoids pathological OCR output
# blowing up the audit workbook.
_MAX_SECTION_CHARS = 2000


# ── Section extraction ────────────────────────────────────────────────────────

def _section_bounds(
    text: str,
    header_re: re.Pattern[str],
    other_headers: tuple[re.Pattern[str], ...],
) -> tuple[int, int] | None:
    """
    Locate the body of the section introduced by *header_re*.

    Returns ``(start, end)`` char indices of the section body (header
    excluded) or ``None`` if the header is not found.
    The end is the earliest position of any of the *other_headers* that
    appears after the start of this section, or ``len(text)``.
    """
    m = header_re.search(text)
    if not m:
        return None
    body_start = m.end()
    body_end = len(text)
    for other in other_headers:
        om = other.search(text, body_start)
        if om and om.start() < body_end:
            body_end = om.start()
    return (body_start, body_end)


def _extract_section_text(
    text: str,
    header_re: re.Pattern[str],
    other_headers: tuple[re.Pattern[str], ...],
) -> str | None:
    """Return the raw (stripped, truncated) body of one section, or ``None``."""
    bounds = _section_bounds(text, header_re, other_headers)
    if bounds is None:
        return None
    body = text[bounds[0]:bounds[1]].strip()
    if not body:
        return None
    return body[:_MAX_SECTION_CHARS]


def _extract_diagnozisok(text: str) -> tuple[dict, ...]:
    """
    Parse the DIAGNÓZISOK section into a tuple of ``{"code", "description"}``
    dicts. Falls back to line-split descriptions when no BNO codes are found.
    """
    body = _extract_section_text(
        text,
        _DIAGNOZISOK_RE,
        (_VELEMENY_RE, _BEAVATKOZASOK_RE),
    )
    if not body:
        return ()

    entries: list[dict] = []
    for bno_match in _BNO_LINE_RE.finditer(body):
        code = bno_match.group(1).upper()
        description = bno_match.group(2).strip()
        entries.append({"code": code, "description": description})

    if entries:
        return tuple(entries)

    # Fallback: no recognisable BNO codes → keep the raw lines as descriptions.
    for line in body.splitlines():
        line = line.strip(" \t-•·")
        if line:
            entries.append({"code": None, "description": line})
    return tuple(entries)


# ── Public entry point ────────────────────────────────────────────────────────

def extract(
    doc: "RoutedDoc",
    case_id: str,
    routing_method: str,
    failure_reason: str,
    best_score: int,
) -> AncillaryRecord:
    """
    Build an ``AncillaryRecord`` for *doc*.

    Parameters
    ----------
    doc
        The routed document. ``doc.doc_type`` must be
        ``DocType.AMBULANSLAP`` or ``DocType.UNKNOWN``; any other value
        indicates a caller bug.
    case_id, routing_method, failure_reason, best_score
        Audit-trail fields preserved from the previous ``UnknownDocRecord``
        shape so the downstream exporter does not need to branch.

    Returns
    -------
    AncillaryRecord
        For ``AMBULANSLAP`` the record carries extracted section text;
        for ``UNKNOWN`` the same extractors run best-effort (most of the
        time they find nothing).
    """
    text = doc.full_text
    preview = (text[:200] if text else "").strip()

    velemeny = _extract_section_text(
        text,
        _VELEMENY_RE,
        (_DIAGNOZISOK_RE, _BEAVATKOZASOK_RE),
    )
    beavatkozasok = _extract_section_text(
        text,
        _BEAVATKOZASOK_RE,
        (_VELEMENY_RE, _DIAGNOZISOK_RE),
    )
    diagnozisok = _extract_diagnozisok(text)

    return AncillaryRecord(
        case_id=case_id,
        filename=doc.path.name,
        routing_method=routing_method,
        failure_reason=failure_reason,
        best_score=best_score,
        text_preview=preview,
        doc_type=doc.doc_type if doc.doc_type in (
            DocType.AMBULANSLAP, DocType.UNKNOWN,
        ) else DocType.UNKNOWN,
        velemeny=velemeny,
        beavatkozasok=beavatkozasok,
        diagnozisok=diagnozisok,
    )
