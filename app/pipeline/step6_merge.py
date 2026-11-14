"""
app/pipeline/step6_merge.py

Step 6 of the pipeline — Merge & Conflict Detection.

Combines ExtractedField lists from all documents in a case bundle into a
single master dict, following the Discharge-First hierarchy of truth.

Public API
----------
DocInput
    Thin wrapper for one document's contribution (doc_type, fields, metadata).

merge(docs, anchor, case_id)
    Main entry point.  Returns a 4-tuple:
        (
            master_fields     : dict[str, ExtractedField],
            conflicts         : list[ConflictRecord],
            mismatches        : list[MismatchRecord],
            out_of_window     : list[OutOfWindowRecord],
        )

Design
------
Merge order:  DISCHARGE_SUMMARY documents are processed first regardless of
              input order.  All ancillary documents follow.

Conflict rule:
    A conflict is logged when:
      1. The field key already has a value from the discharge summary,
      2. An ancillary document provides a *different* meaningful value,
      3. Both values are non-empty (None / "Unknown" side is treated as
         absent — enrichment, not conflict).
    Which side wins depends on `source_policy`:
      - DISCHARGE_PRIMARY / DISCHARGE_ONLY → discharge wins
      - EXAM_DOC_PRIMARY  → exam document wins
    The conflict record is emitted for the audit trail (→ conflicts.xlsx).

Enrichment rule:
    If the discharge summary did not extract a field, any ancillary document
    may fill it.  Priority is settled by set_field() (HARD > HEURISTIC > SOFT,
    then confidence).

Fingerprint mismatch:
    If a DocInput carries a pre-computed fingerprint (typically computed by
    step3_anchor when extracting patient headers from ancillary docs), it is
    compared to anchor.fingerprint.  Mismatched documents are logged as
    MismatchRecord and ALL their fields are skipped.

Out-of-window fields:
    A day-keyed field (lab "{analyte}_d{day}", event "{prefix}_d{day}_{n}_…")
    from an ancillary document whose day is classified as PRE_ADMISSION or
    POST_DISCHARGE by step4_normalize.classify_window() is not merged; the
    document's registry fields still are.  One OutOfWindowRecord per
    (document, date) lists the dropped keys → history_followup.xlsx.
    Example: a post-discharge ambulatory sheet that repeats an in-hospital
    FibroScan yields fibro_d10_* — dropped, while its answers still merge.

Out-of-window documents:
    If a DocInput carries a doc_date that is classified as PRE_ADMISSION or
    POST_DISCHARGE, the document is logged as OutOfWindowRecord and ALL its
    fields are skipped.  The orchestrator does not set doc_date (it has no
    reliable per-document date), so in the pipeline only the field-level
    rule above applies.

Notes
-----
- `set_field()` in domain.models handles priority + confidence comparison.
- No LLM, no I/O, no side effects beyond building the return values.
- Column-name safety (replacing '-' in negative day keys like 'd-1') is the
  responsibility of step8_export.py, not this module.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, TYPE_CHECKING

from app.domain.enums import DocType, WindowType
from app.domain.models import (
    ConflictRecord,
    ExtractedField,
    MismatchRecord,
    OutOfWindowRecord,
    PatientAnchor,
    set_field,
)
from app.pipeline.step4_normalize import classify_window, compute_day

if TYPE_CHECKING:
    from app.registry.models import FieldDefinition

log = logging.getLogger(__name__)

# Values that are considered "absent" — they do not represent meaningful data
# and should not trigger a conflict record.
# "Unknown"  = the field was never successfully examined (regex/LLM never ran).
# "No Data"  = the LLM examined the document but found no evidence.
_ABSENT_VALUES: frozenset = frozenset(
    {None, "", "Unknown", "No Data", "NA", "ERVENYTELEN", "Not applicable",
     "Uncertain"}
)


# ── Input record ──────────────────────────────────────────────────────────────

@dataclass
class DocInput:
    """
    One document's contribution to the merge step.

    Attributes
    ----------
    doc_type : DocType
        Classified type of the document.
    fields : list[ExtractedField]
        All fields extracted from this document (hard + soft combined).
    filename : str
        Original filename — used in MismatchRecord / OutOfWindowRecord.
    doc_date : date | None
        Representative date for the document (e.g. exam date, lab draw date).
        Required for out-of-window detection.  None → skip window check.
    doc_fingerprint : str | None
        Pre-computed patient fingerprint from the document header.
        If provided and != anchor.fingerprint → MismatchRecord.
        None → skip identity check (trust the document).
    found_name : str | None
        Patient name found in the document header (for MismatchRecord detail).
    found_taj : str | None
        TAJ number found in the document header (for MismatchRecord detail).
    """
    doc_type: DocType
    fields: list[ExtractedField]
    filename: str = ""
    doc_date: date | None = None
    doc_fingerprint: str | None = None
    found_name: str | None = None
    found_taj: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────────

# Fuzzy-equivalence tuning (see _values_conflict):
#   - prefix rule applies only when the shorter value is at least this long,
#     so short enums ("Igen" vs "Igen, súlyos") still conflict;
#   - similarity rule applies only to reasonably long strings and requires a
#     high ratio, so OCR/whitespace/punctuation noise is forgiven but real
#     clinical differences are not.
_FUZZY_MIN_PREFIX_LEN = 20
_FUZZY_MIN_SIM_LEN = 12
_FUZZY_SIM_THRESHOLD = 0.92

# Leading number + optional trailing unit/text, e.g. "75.0 kPa", "35", "1,5 g".
_NUM_UNIT_RE = re.compile(r"^([-+]?\d+(?:[.,]\d+)?)\s*(.*)$")

# Date-like strings (ISO or dotted): compared strictly, never fuzzily —
# "2026-01-16" and "2026-01-17" are one character apart but different days.
_DATEISH_RE = re.compile(r"^\d{4}[-./]\d{1,2}([-./]\d{1,2})?")

# ── Clinical semantic guard ───────────────────────────────────────────────────
#
# Fuzzy string shortcuts (prefix / similarity) must NEVER merge values whose
# clinical meaning is opposite. Long Hungarian findings that differ only in
# one antonym ("benignus" ↔ "malignus") or in a negation particle ("varix
# látható" ↔ "varix nem látható") can score above the similarity threshold.
#
# Each entry is (flag_name, word-boundary regex, applied to the casefolded
# string). If the two values raise DIFFERENT flag sets, the fuzzy shortcuts
# are disabled and the pair is reported as a conflict for human review.
# This is deliberately conservative: a false conflict costs one reviewer
# glance; a false merge silently corrupts clinical data.
_SEMANTIC_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("negation",      r"\b(?:nem|nincs|nincsenek|sem|kizárt|sine)\b"),
        ("positive",      r"\bpozit[ií]v\b|\bpoz\."),
        ("negative",      r"\bnegat[ií]v\b|\bneg\."),
        ("benign",        r"\bbenignus\b|\bjóindulat"),
        ("malign",        r"\bmalignus\b|\brosszindulat"),
        ("left",          r"\bbal\b"),
        ("right",         r"\bjobb\b"),
        ("increased",     r"\bemelkedett\b|\bfokozott\b|\bmagas\b"),
        ("decreased",     r"\bcsökkent\b|\balacsony\b"),
        ("compensated",   r"\bkompenzált\b"),
        ("decompensated", r"\bdekompenzált\b"),
        ("acute",         r"\bakut\b|\bacut\b"),
        ("chronic",       r"\bkrónikus\b|\bchronicus\b|\bchronic\b"),
    )
)


def _semantic_flags(normed: str) -> frozenset[str]:
    """Return the set of clinical semantic markers present in *normed*."""
    return frozenset(
        name for name, pattern in _SEMANTIC_MARKERS if pattern.search(normed)
    )


def _norm(value: Any) -> str:
    """Normalise a value for comparison: str, strip, collapse ws, casefold."""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def _values_conflict(v1: Any, v2: Any) -> bool:
    """
    Return True only when both values are meaningfully set AND differ
    *substantively*.

    Absent / sentinel values (None, "Unknown", "No Data", …) on either side
    indicate an absent field — treat as enrichment, not conflict.

    Fuzzy equivalence — the following pairs are NOT conflicts:
      - case / whitespace / trailing-punctuation variants of the same text;
      - the same physical quantity with convertible units ("1 g" vs
        "1000 mg", "1.5 g/dL" vs "15 g/L") or with the unit omitted on one
        side ("75" vs "75.0 kPa");
      - one value being a long prefix of the other (context truncation, or a
        discharge summary quoting the exam text and appending further notes);
      - near-identical long strings (small OCR / formatting noise).

    Clinical safety guards — always strict conflicts:
      - date-like values;
      - numerically different quantities;
      - equal numerals under non-convertible units ("75 kPa" vs "75 mmHg",
        "2 mg" vs "2 g" of different dimension is caught by conversion);
      - values whose clinical semantic markers differ (antonym pairs like
        benignus/malignus, bal/jobb, or a negation particle present on only
        one side) — the fuzzy shortcuts are disabled for those.
    """
    if v1 in _ABSENT_VALUES or v2 in _ABSENT_VALUES:
        return False

    s1, s2 = _norm(v1), _norm(v2)
    if s1 == s2:
        return False

    # Date-like values: strict comparison only.
    if _DATEISH_RE.match(s1) or _DATEISH_RE.match(s2):
        return True

    # Numeric equivalence / difference — unit-magnitude aware.
    m1, m2 = _NUM_UNIT_RE.match(s1), _NUM_UNIT_RE.match(s2)
    if m1 and m2:
        try:
            n1 = float(m1.group(1).replace(",", "."))
            n2 = float(m2.group(1).replace(",", "."))
        except ValueError:
            pass
        else:
            from app.pipeline.step5_extract.units import (
                normalise_unit, unit_to_base,
            )
            u1, u2 = m1.group(2).strip(), m2.group(2).strip()
            if not u1 or not u2:
                # Unit omitted on (at least) one side — the bare number is
                # assumed to be in the same unit as the annotated one
                # (discharge summaries quote lab values without units).
                return n1 != n2
            if normalise_unit(u1) == normalise_unit(u2):
                return n1 != n2
            b1, b2 = unit_to_base(u1), unit_to_base(u2)
            if b1 is not None and b2 is not None and b1[0] == b2[0]:
                # Convertible units: compare on the common base scale.
                import math
                return not math.isclose(
                    n1 * b1[1], n2 * b2[1], rel_tol=1e-6,
                )
            # Different units that cannot be converted — genuine mismatch
            # regardless of the numerals ("75 kPa" vs "75 mmHg").
            return True

    # Clinical semantic guard: differing antonym/negation markers disable
    # every fuzzy shortcut below.
    if _semantic_flags(s1) != _semantic_flags(s2):
        return True

    # Truncation / quoted-prefix equivalence.
    shorter, longer = (s1, s2) if len(s1) <= len(s2) else (s2, s1)
    if len(shorter) >= _FUZZY_MIN_PREFIX_LEN and longer.startswith(shorter):
        return False

    # High-similarity equivalence (OCR / formatting noise).
    if min(len(s1), len(s2)) >= _FUZZY_MIN_SIM_LEN:
        import difflib
        if difflib.SequenceMatcher(None, s1, s2).ratio() >= _FUZZY_SIM_THRESHOLD:
            return False

    return True


# Prefixes for dynamically generated exam-doc fields that should default to
# EXAM_DOC_PRIMARY when they are not explicitly defined in the registry.
_EXAM_KEY_PREFIXES = (
    "endo_gastro_", "endo_colono_",
    "fibro_d",
    "uh_d", "ct_d", "rtg_d", "mri_d",
    "micro_d",
)


def _default_policy(key: str) -> str:
    """
    Return the default source_policy for a field key not found in the registry.

    Dynamically generated exam-doc fields (endoscopy, elastography, imaging,
    microbiology) default to EXAM_DOC_PRIMARY — the standalone examination
    document is the authoritative source for its findings.

    All other fields (including dynamically generated lab keys) default to
    DISCHARGE_PRIMARY here. The merge loop promotes DISCHARGE_PRIMARY →
    EXAM_DOC_PRIMARY when the value comes from a LAB_REPORT document, so lab
    reports remain authoritative without needing to enumerate every analyte
    prefix in this list.
    """
    if any(key.startswith(prefix) for prefix in _EXAM_KEY_PREFIXES):
        return "EXAM_DOC_PRIMARY"
    return "DISCHARGE_PRIMARY"


def _is_discharge(doc: DocInput) -> bool:
    return doc.doc_type == DocType.DISCHARGE_SUMMARY


# Day-keyed (dynamic) columns: events "{prefix}_d{day}_{n}_{field}" and labs
# "{analyte}_d{day}" / "{analyte}_d{day}_{n}" (same shapes as step8_export).
_DAY_KEY_RE = re.compile(
    r"^(?:endo_gastro|endo_colono|fibro|rtg|uh|ct|mri|micro)_d(-?\d+)_\d+_.+$"
    r"|^.+?_d(-?\d+)(?:_\d+)?$"
)


def _event_date_of_key(key: str, anchor: PatientAnchor) -> date | None:
    """Date of a day-keyed column (inverse of compute_day), else None."""
    m = _DAY_KEY_RE.match(key)
    if not m:
        return None
    day = int(m.group(1) or m.group(2))
    return anchor.admission_date + timedelta(days=day - 1 if day > 0 else day)


# ── Main entry point ──────────────────────────────────────────────────────────

def merge(
    docs: list[DocInput],
    anchor: PatientAnchor,
    case_id: str,
    registry: list["FieldDefinition"] | None = None,
) -> tuple[
    dict[str, ExtractedField],
    list[ConflictRecord],
    list[MismatchRecord],
    list[OutOfWindowRecord],
]:
    """
    Merge all document contributions into a single field dict.

    Parameters
    ----------
    docs : list[DocInput]
        All documents (discharge + ancillary) for this case.  Order does not
        matter — discharge is always processed first internally.
    anchor : PatientAnchor
        Master patient identity + admission window.
    case_id : str
        Identifier for the case (used in record objects).

    Returns
    -------
    master_fields : dict[str, ExtractedField]
        Final merged fields.  Key = column name, value = winning ExtractedField.
    conflicts : list[ConflictRecord]
        Disagreements between discharge and ancillary documents.
    mismatches : list[MismatchRecord]
        Documents whose patient fingerprint does not match the anchor.
    out_of_window : list[OutOfWindowRecord]
        Documents dated outside the admission window (pre-admission or
        post-discharge), and day-keyed fields of ancillary documents that
        fall outside it.
    """
    master_fields: dict[str, ExtractedField] = {}
    # Track which keys were populated from the discharge summary so we can
    # detect conflicts correctly even when ancillary values are of higher
    # source priority.
    discharge_keys: set[str] = set()

    conflicts: list[ConflictRecord] = []
    mismatches: list[MismatchRecord] = []
    out_of_window: list[OutOfWindowRecord] = []

    # ── Sort: discharge first, then ancillary ─────────────────────────────────
    ordered = sorted(docs, key=lambda d: (0 if _is_discharge(d) else 1))

    # ── Build per-field lookups from registry ─────────────────────────────────
    _policy: dict[str, str] = {}
    _repeat_group: dict[str, str | None] = {}
    if registry:
        for fd in registry:
            _policy[fd.field_name] = fd.source_policy
            _repeat_group[fd.field_name] = fd.repeat_group
    # NOTE: repeat_group is stored here for future multi-occurrence column
    # generation. It is not yet used in the merge loop — same-day deduplication
    # across discharge summary and event documents is handled implicitly through
    # key identity. Multi-day multi-occurrence support is deferred to a later PR.

    for doc in ordered:
        is_dis = _is_discharge(doc)

        # ── Fingerprint mismatch check (ancillary docs only) ──────────────────
        if not is_dis and doc.doc_fingerprint is not None:
            if doc.doc_fingerprint != anchor.fingerprint:
                log.warning(
                    "Fingerprint mismatch: %s (expected %s, got %s)",
                    doc.filename, anchor.fingerprint, doc.doc_fingerprint,
                )
                mismatches.append(
                    MismatchRecord(
                        case_id=case_id,
                        filename=doc.filename,
                        expected_fingerprint=anchor.fingerprint,
                        found_name=doc.found_name,
                        found_taj=doc.found_taj,
                    )
                )
                continue  # skip ALL fields from this document

        # ── Out-of-window check (ancillary docs only) ─────────────────────────
        if not is_dis and doc.doc_date is not None:
            window = classify_window(doc.doc_date, anchor)
            if window in (WindowType.PRE_ADMISSION, WindowType.POST_DISCHARGE):
                days = compute_day(doc.doc_date, anchor)
                label = doc.doc_type.value if doc.doc_type else "UNKNOWN"
                summary = (
                    f"{label} dated {doc.doc_date.isoformat()} "
                    f"({'pre-admission' if window == WindowType.PRE_ADMISSION else 'post-discharge'}; "
                    f"day {days:+d} relative to admission)"
                )
                log.info(
                    "Out-of-window document: %s (%s, day %+d)",
                    doc.filename, window.value, days,
                )
                out_of_window.append(
                    OutOfWindowRecord(
                        case_id=case_id,
                        filename=doc.filename,
                        doc_type=doc.doc_type,
                        event_date=doc.doc_date,
                        days_from_admission=days,
                        window_type=window,
                        summary=summary,
                    )
                )
                continue  # skip ALL fields from this document

        # ── Field-level merge ─────────────────────────────────────────────────
        # Day-keyed fields of an ancillary document dated outside the window
        # are not merged; the document's other fields still are.
        dropped: dict[date, list[str]] = {}
        for ef in doc.fields:
            if not is_dis and ef.key not in _policy:
                event_date = _event_date_of_key(ef.key, anchor)
                if event_date is not None and classify_window(event_date, anchor) in (
                    WindowType.PRE_ADMISSION, WindowType.POST_DISCHARGE,
                ):
                    dropped.setdefault(event_date, []).append(ef.key)
                    continue

            policy = _policy.get(ef.key) or _default_policy(ef.key)
            # LAB_REPORT is the authoritative source for any lab value it
            # produces: promote DISCHARGE_PRIMARY → EXAM_DOC_PRIMARY for
            # fields that came from a lab report document. DISCHARGE_ONLY
            # fields are untouched (they never reach this branch from a lab
            # report anyway — the soft extractor filters them out).
            if ef.doc_type == DocType.LAB_REPORT and policy == "DISCHARGE_PRIMARY":
                policy = "EXAM_DOC_PRIMARY"
            exam_primary = policy == "EXAM_DOC_PRIMARY"

            if is_dis:
                # Discharge summary: always record, always set.
                set_field(master_fields, ef.key, ef)
                discharge_keys.add(ef.key)
            else:
                # Ancillary document.
                if ef.key in discharge_keys:
                    existing = master_fields[ef.key]
                    if _values_conflict(existing.value, ef.value):
                        log.debug(
                            "Conflict on '%s': policy=%s discharge=%r, ancillary=%r (from %s)",
                            ef.key, policy, existing.value, ef.value, doc.doc_type.value,
                        )
                        if exam_primary:
                            # EXAM_DOC_PRIMARY: exam document wins. Policy has
                            # decided — force the override unconditionally
                            # instead of going through set_field's priority
                            # check (which would keep the discharge value when
                            # both sides share the same source tier and
                            # confidence).
                            conflicts.append(ConflictRecord(
                                case_id=case_id,
                                field=ef.key,
                                source_policy=policy,
                                retained_value=ef.value,
                                retained_doc_type=ef.doc_type,
                                overridden_value=existing.value,
                                overridden_doc_type=DocType.DISCHARGE_SUMMARY,
                                page=ef.page,
                            ))
                            master_fields[ef.key] = ef
                        else:
                            # DISCHARGE_PRIMARY: discharge wins.
                            conflicts.append(ConflictRecord(
                                case_id=case_id,
                                field=ef.key,
                                source_policy=policy,
                                retained_value=existing.value,
                                retained_doc_type=DocType.DISCHARGE_SUMMARY,
                                overridden_value=ef.value,
                                overridden_doc_type=ef.doc_type,
                                page=ef.page,
                            ))
                            # Do NOT call set_field — discharge value is kept.
                    else:
                        # Same value (or one side absent) — agreement, no conflict.
                        # Apply set_field so HARD source can upgrade confidence.
                        set_field(master_fields, ef.key, ef)
                else:
                    # Field not seen in discharge — enrich unconditionally.
                    set_field(master_fields, ef.key, ef)

        for event_date, keys in sorted(dropped.items()):
            window = classify_window(event_date, anchor)
            days = compute_day(event_date, anchor)
            label = doc.doc_type.value if doc.doc_type else "UNKNOWN"
            log.info(
                "Out-of-window fields: %s, %d field(s) on %s (day %+d) not merged",
                doc.filename, len(keys), event_date.isoformat(), days,
            )
            out_of_window.append(
                OutOfWindowRecord(
                    case_id=case_id,
                    filename=doc.filename,
                    doc_type=doc.doc_type,
                    event_date=event_date,
                    days_from_admission=days,
                    window_type=window,
                    summary=(
                        f"{label}: {len(keys)} day-keyed field(s) dated "
                        f"{event_date.isoformat()} "
                        f"({'pre-admission' if window == WindowType.PRE_ADMISSION else 'post-discharge'}; "
                        f"day {days:+d} relative to admission) not merged: "
                        + ", ".join(keys)
                    ),
                )
            )

    log.info(
        "Merge complete: %d fields, %d conflicts, %d mismatches, %d out-of-window",
        len(master_fields), len(conflicts), len(mismatches), len(out_of_window),
    )
    return master_fields, conflicts, mismatches, out_of_window
