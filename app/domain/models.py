"""
app/domain/models.py

Core domain models for the pipeline.

Design notes:
  - Immutable inputs (CaseBundle, RoutedDoc, PatientAnchor, all record types)
    use frozen=True.  This prevents accidental mutation and makes them
    hashable / safe to pass across threads.
  - ExtractedField and CaseResult are mutable because the pipeline builds
    them incrementally.
  - No external dependencies — stdlib dataclasses + typing only.
    This module must be importable even before any packages are installed.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.domain.enums import DocType, FieldSource, WindowType


# ── Inputs ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CaseBundle:
    """
    A set of documents that all belong to a single patient admission.

    Immutable: created once at ingest time and passed through the pipeline.
    """
    case_id: str
    files: tuple[Path, ...]   # tuple (not list) to stay hashable

    @classmethod
    def from_paths(cls, case_id: str, paths: list[Path]) -> "CaseBundle":
        """Construct a CaseBundle from a list of file paths, converting to a tuple."""
        return cls(case_id=case_id, files=tuple(paths))


@dataclass(frozen=True)
class RoutedDoc:
    """
    A single document after ingestion and routing.

    pages: list of per-page text strings.  Keeping pages separate (not
    joining into one blob) lets the soft extractor identify which section
    of the document a field was found in.

    tables: list of extracted table grids.  Each grid is a list of rows,
    each row is a list of cell strings.  Empty list for non-PDF or
    documents with no tables.

    meta: freeform dict for router/parser hints, e.g.
      {'lab_name': 'Bartók HQ', 'sample_date': '2026-01-11'}
    """
    path: Path
    doc_type: DocType
    pages: tuple[str, ...]           # per-page text, in order
    tables: tuple[tuple, ...]        # extracted grids (may be empty)
    meta: dict[str, Any]             # router/parser hints

    @property
    def full_text(self) -> str:
        """All pages joined — used by parsers that don't need page boundaries."""
        return "\n\n".join(self.pages)

    @property
    def first_3000(self) -> str:
        """First 3000 chars of full text — used by the router classifier."""
        return self.full_text[:3000]

    @classmethod
    def empty(cls, path: Path, doc_type: DocType = DocType.UNKNOWN) -> "RoutedDoc":
        """Construct a document with no content (ingest failure fallback)."""
        return cls(path=path, doc_type=doc_type, pages=(), tables=(), meta={})

    def with_doc_type(
        self,
        doc_type: DocType,
        meta_update: dict[str, Any] | None = None,
    ) -> "RoutedDoc":
        """Return a new RoutedDoc with doc_type replaced (used by router).

        If *meta_update* is provided, its entries are merged into a **copy**
        of the current meta dict (original is never mutated).
        """
        merged = {**self.meta, **(meta_update or {})}
        return RoutedDoc(
            path=self.path,
            doc_type=doc_type,
            pages=self.pages,
            tables=self.tables,
            meta=merged,
        )


@dataclass(frozen=True)
class PatientAnchor:
    """
    Core patient identity + admission window, extracted from the
    discharge summary.  All other documents are validated against
    this anchor.

    fingerprint: 12-char prefix of sha256(name + dob.isoformat() + taj)
    Used for patient identity checking across documents.
    """
    name: str
    dob: date
    taj: str                  # TAJ szám, stored as normalised string
    admission_start: datetime
    discharge_end: datetime
    fingerprint: str          # computed; see make_fingerprint()

    @staticmethod
    def make_fingerprint(name: str, dob: date, taj: str) -> str:
        """
        Deterministic 12-char fingerprint for identity matching.
        Normalised: lowercase, stripped, TAJ digits only.
        """
        normalised = (
            name.strip().lower()
            + dob.isoformat()
            + "".join(c for c in taj if c.isdigit())
        )
        return hashlib.sha256(normalised.encode()).hexdigest()[:12]

    @classmethod
    def create(
        cls,
        name: str,
        dob: date,
        taj: str,
        admission_start: datetime,
        discharge_end: datetime,
    ) -> "PatientAnchor":
        """Factory that computes the fingerprint automatically."""
        return cls(
            name=name,
            dob=dob,
            taj=taj,
            admission_start=admission_start,
            discharge_end=discharge_end,
            fingerprint=cls.make_fingerprint(name, dob, taj),
        )

    @property
    def admission_date(self) -> date:
        """Return the calendar date of admission (time component discarded)."""
        return self.admission_start.date()

    @property
    def discharge_date(self) -> date:
        """Return the calendar date of discharge (time component discarded)."""
        return self.discharge_end.date()


# ── Extraction ────────────────────────────────────────────────────────────────

@dataclass
class ExtractedField:
    """
    A single extracted key-value pair with full provenance.

    key:        final column name in the wide table
                (e.g. 'creatinine_d2', 'ascites_grade_adm')
    value:      extracted value — str, float, int, or None
    source:     FieldSource enum (HARD / HEURISTIC / SOFT)
    doc_type:   which document type the value came from
    confidence: 0.0–1.0; used by set_field() to decide which value wins
    page:       0-based page index within the source document (None if unknown)
    engine:     e.g. 'regex', 'camelot', 'block_parser', 'llm'
    unit_seen:  the unit string observed in the source document, if any.
                None when the field is non-numeric or the document did not
                repeat a unit suffix. Compared against the registry `unit`
                column at merge time to flag mismatches.
    unit_ok:    tri-state result of the unit comparison.
                None = not yet evaluated / field has no registry unit;
                True = seen unit matches the registry unit (or was absent);
                False = seen unit differs from the registry unit → mismatch.
    """
    key: str
    value: Any
    source: FieldSource
    doc_type: DocType
    confidence: float
    page: int | None = None
    engine: str | None = None
    unit_seen: str | None = None
    unit_ok: bool | None = None

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"ExtractedField.confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )

    def is_better_than(self, other: "ExtractedField") -> bool:
        """
        Return True if this field should replace `other` in the merged result.

        Priority:  source type first (HARD > HEURISTIC > SOFT),
                   then confidence as tiebreaker.
        This matches the Code Mine [A9-01] merge logic.
        """
        if self.source != other.source:
            return self.source > other.source   # IntEnum comparison
        return self.confidence > other.confidence

    def to_provenance_dict(self) -> dict:
        """Compact provenance record for the artifact store."""
        return {
            "source": self.source.name,
            "doc_type": self.doc_type.value,
            "confidence": round(self.confidence, 3),
            "page": self.page,
            "engine": self.engine,
            "unit_seen": self.unit_seen,
            "unit_ok": self.unit_ok,
        }


# ── Reports ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ConflictRecord:
    """
    Records a disagreement between two sources for the same field.
    For DISCHARGE_PRIMARY fields: the discharge value is retained.
    For EXAM_DOC_PRIMARY fields:  the exam document value is retained.
    Goes to conflicts.xlsx.
    """
    case_id: str
    field: str
    source_policy: str               # DISCHARGE_PRIMARY | EXAM_DOC_PRIMARY
    retained_value: Any              # the value that was kept
    retained_doc_type: DocType       # source of the retained value
    overridden_value: Any            # the value that was discarded
    overridden_doc_type: DocType     # source of the overridden value
    page: int | None


@dataclass(frozen=True)
class MismatchRecord:
    """
    Records a document whose patient fingerprint does not match the master.
    The document is skipped; this record goes to mismatch.xlsx.
    """
    case_id: str
    filename: str
    expected_fingerprint: str
    found_name: str | None
    found_taj: str | None


@dataclass(frozen=True)
class OutOfWindowRecord:
    """
    Records a document dated outside the admission window.
    Goes to history_followup.xlsx.
    """
    case_id: str
    filename: str
    doc_type: DocType
    event_date: date
    days_from_admission: int    # negative = pre-admission, positive = post-discharge
    window_type: WindowType
    summary: str                # one-line description


@dataclass(frozen=True)
class UnitMismatchRecord:
    """
    Records a numeric field whose extracted unit did not match the registry
    canonical unit. The pipeline does NOT auto-convert — it flags only.
    Goes to unit_mismatches.xlsx.
    """
    case_id: str
    field: str
    expected_unit: str
    seen_unit: str
    raw_value: str
    doc_type: DocType
    page: int | None


@dataclass(frozen=True)
class AncillaryRecord:
    """
    Records a document handled as ancillary — either classified-but-unrouted
    (``AMBULANSLAP``) or unclassified (``UNKNOWN``). Goes to the
    ``ancillary_docs.xlsx`` audit sheet.

    For ``UNKNOWN`` documents the record matches the previous
    ``UnknownDocRecord`` shape (routing_method / failure_reason / best_score /
    text_preview). For ``AMBULANSLAP`` documents the same identifying fields
    are populated **plus** the extracted narrative sections
    (``VÉLEMÉNY``, ``BEAVATKOZÁSOK``) and a structured list of BNO diagnoses.

    Attributes
    ----------
    case_id, filename, routing_method, failure_reason, best_score, text_preview
        Inherited from UnknownDocRecord for UNKNOWN docs; for AMBULANSLAP the
        routing_method reflects how the classifier matched and failure_reason
        is always "classified_ancillary".
    doc_type
        Either ``DocType.UNKNOWN`` or ``DocType.AMBULANSLAP``.
    velemeny, beavatkozasok
        Raw section text from AMBULANSLAP (``None`` when the section is
        absent or the doc is UNKNOWN).
    diagnozisok
        Tuple of ``{"code": str | None, "description": str}`` dicts — BNO
        codes extracted from the DIAGNÓZISOK section when present, otherwise
        line-split descriptions. Empty tuple when the section is absent.
    """
    case_id: str
    filename: str
    routing_method: str
    failure_reason: str
    best_score: int
    text_preview: str
    doc_type: DocType = DocType.UNKNOWN
    velemeny: str | None = None
    beavatkozasok: str | None = None
    diagnozisok: tuple[dict, ...] = ()


# ── Case result ───────────────────────────────────────────────────────────────

@dataclass
class CaseResult:
    """
    The complete output of processing one CaseBundle.

    fields:       merged extracted fields; key → best ExtractedField
    conflicts:    discharge vs ancillary disagreements
    mismatches:   documents with wrong patient identity
    out_of_window: documents outside the admission window
    warnings:     non-fatal issues encountered during processing
    """
    case_id: str
    fields: dict[str, ExtractedField] = field(default_factory=dict)
    conflicts: list[ConflictRecord] = field(default_factory=list)
    mismatches: list[MismatchRecord] = field(default_factory=list)
    out_of_window: list[OutOfWindowRecord] = field(default_factory=list)
    unit_mismatches: list[UnitMismatchRecord] = field(default_factory=list)
    ancillary_docs: list[AncillaryRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def set_field(self, key: str, ef: ExtractedField) -> None:
        """
        Insert or replace a field using priority + confidence merge.

        Rules (matches architecture §4 and Code Mine [A9-01]):
          1. If the key is new → insert.
          2. If new value has higher source priority → replace.
          3. If same source priority, higher confidence → replace.
          4. Otherwise → keep existing.
        """
        existing = self.fields.get(key)
        if existing is None or ef.is_better_than(existing):
            self.fields[key] = ef

    def add_warning(self, msg: str) -> None:
        """Append a human-readable warning to the case result's warning list."""
        self.warnings.append(msg)

    def to_plain(self) -> dict[str, Any]:
        """
        Flat key→value dict for export.  Provenance is discarded here;
        it lives in the artifact store.
        """
        return {k: v.value for k, v in self.fields.items()}

    def to_provenance(self) -> dict[str, dict]:
        """Full provenance map: key → provenance_dict."""
        return {k: v.to_provenance_dict() for k, v in self.fields.items()}

    @property
    def has_conflicts(self) -> bool:
        """Return True if any field was extracted with conflicting values across documents."""
        return len(self.conflicts) > 0

    @property
    def has_mismatches(self) -> bool:
        """Return True if any document's patient fingerprint did not match the anchor."""
        return len(self.mismatches) > 0

    @property
    def field_count(self) -> int:
        """Return the number of successfully extracted fields in this case result."""
        return len(self.fields)


# ── Module-level helpers ──────────────────────────────────────────────────────

def set_field(fields: dict[str, ExtractedField], key: str, ef: ExtractedField) -> None:
    """
    Standalone version of CaseResult.set_field() for use in pipeline steps
    that accumulate fields before building a CaseResult.
    """
    existing = fields.get(key)
    if existing is None or ef.is_better_than(existing):
        fields[key] = ef


def to_plain(fields: dict[str, ExtractedField]) -> dict[str, Any]:
    """Return a plain key→value dict from an ExtractedField dict."""
    return {k: v.value for k, v in fields.items()}
