"""
app/registry/models.py

FieldDefinition: the in-memory representation of one row in Registry_base.xlsx.
Frozen dataclass — loaded once at startup, never mutated.

Design notes:
  - allowed_values is always a list[str], never None.
    For 'open' fields it's an empty list.
  - section_hint targets discharge-summary sections. It is NOT used to decide
    whether a field is eligible for a document — that comes from `routing`.
  - routing is the parsed form of the registry `routing` column: a tuple of
    (DocType, subtype | None) pairs. A field is eligible for a document when
    its routing contains an entry matching the document's doc_type and (if
    the entry specifies a subtype) its routing_subtype.
  - condition_* fields define when the field IS applicable:
      field is applicable when (condition_parent_field <op> condition_value)
      The operator is usually '==' but '!=' is also supported; the rules
      engine negates the operator in memory when deriving the
      NOT-applicable trigger at pipeline start.
  - missing_marker / not_applicable_marker are kept exactly as they appear
    in the registry ('Unknown' / 'ERVENYTELEN') — the extraction pipeline
    and export layer use these constants directly.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import DocType


# Fallback section hints by form_id — used only if the section_hint column
# is blank for a row. The column is fully populated in the current registry;
# this dict is a safety net for future rows added without a section_hint.
FORM_SECTION_HINTS: dict[str, str] = {
    "A":    "Anamnézis",
    "B1":   "Felvételi státus",
    "Bmid": "Vizsgálatok eredményei",
    "Bdis": "Epikrízis",
}


@dataclass(frozen=True)
class FieldDefinition:
    """
    One field as defined in Registry_base.xlsx Main sheet.

    All fields are populated by loader.py.  No defaults here — every
    attribute must be explicitly set so loader bugs surface immediately.
    """
    # ── Identity ──────────────────────────────────────────────────────────────
    field_name: str          # final column name in the wide table
    field_base: str          # raw base name from registry (may differ from field_name)
    column_header: str       # human-readable label (for label sheet + SPSS import)
    form_id: str             # A | B1 | Bmid | Bdis
    timepoint: str           # anam | adm | inhosp | dis

    # ── Type & values ─────────────────────────────────────────────────────────
    field_type: str          # 'enum' | 'open'
    value_type: str          # 'enum' | 'text' | 'number' | 'date' | ...
    allowed_values: tuple[str, ...]  # enum options; empty tuple for open fields
    enum_set: str | None     # enum set name (references Enum_sets sheet)

    # ── Extraction policy ─────────────────────────────────────────────────────
    source_policy: str       # DISCHARGE_ONLY | DISCHARGE_PRIMARY | EXAM_DOC_PRIMARY
    section_hint: str        # pipe-joined discharge-section scope (e.g. 'Epikrízis|Vizsgálatok eredményei')
    routing: tuple[tuple["DocType", str | None], ...]  # (DocType, subtype | None) pairs
    repeat_group: str | None # e.g. 'gastroscopy_exam', 'bacterial_culture_exam'
    scope: str | None        # e.g. 'soft_scalar'

    # ── Identity / deduplication ──────────────────────────────────────────────
    canonical: bool          # True = patient identity field (name, dob, taj)
    dedupe_key: str | None   # e.g. 'patient.name', 'patient.dob'
    multiple_answers_allowed: bool

    # ── Markers (kept verbatim from registry) ─────────────────────────────────
    missing_marker: str      # 'Unknown' — field relevant but no evidence found
    not_applicable_marker: str  # 'ERVENYTELEN' — logically excluded

    # ── Conditional applicability ─────────────────────────────────────────────
    # Field is applicable when: condition_parent_field == condition_value
    # If any condition_* is None, the field is unconditionally applicable.
    condition_parent_field: str | None
    condition_operator: str | None   # '==' or '!=' (negated by rules generator)
    condition_value: str | None

    # ── Date handling ─────────────────────────────────────────────────────────
    # date_format_hint: partial-date format accepted for this field.
    # "YYYY[-MM[-DD]]" means year-only or year-month or full date are all valid.
    date_format_hint: str | None
    # date_granularity_allowed: coarsest unit acceptable.
    #   "day"   → only YYYY-MM-DD is valid
    #   "month" → YYYY-MM or YYYY-MM-DD are valid
    #   "year"  → YYYY, YYYY-MM, or YYYY-MM-DD are valid
    # Anamnesis fields commonly use "year" so "2019 ősz" can be normalised to
    # "2019-10" before validation.
    date_granularity_allowed: str | None

    # ── Multi-value fields ────────────────────────────────────────────────────
    # list_storage_format: how multi-answer fields are stored in the wide table.
    # "json_string" means the cell value is a JSON array string, e.g. '["A","B"]'.
    list_storage_format: str | None

    # ── Extras ────────────────────────────────────────────────────────────────
    notes: str | None
    unit: str | None

    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def is_conditional(self) -> bool:
        """True if this field has a parent condition."""
        return self.condition_parent_field is not None

    @property
    def is_canonical(self) -> bool:
        """Return True if this field holds patient identity data (name, DOB, TAJ)."""
        return self.canonical

    @property
    def is_enum(self) -> bool:
        """Return True if this field has a fixed set of allowed values (enum type)."""
        return self.field_type == "enum"

    @property
    def is_open(self) -> bool:
        """Return True if this field accepts free-text or numeric values (open type)."""
        return self.field_type == "open"

    @property
    def is_list_field(self) -> bool:
        """Return True if this field stores multiple answers as a JSON array string."""
        return self.list_storage_format == "json_string"

    @property
    def allows_partial_date(self) -> bool:
        """Return True if any partial date (year or month granularity) is acceptable."""
        return self.date_granularity_allowed in ("year", "month")

    @property
    def allows_year_only(self) -> bool:
        """Return True if year-only dates (YYYY) are acceptable for this field."""
        return self.date_granularity_allowed == "year"

    @property
    def allows_year_month(self) -> bool:
        """Return True if year-month dates (YYYY-MM) are acceptable for this field."""
        return self.date_granularity_allowed in ("year", "month")

    @property
    def is_applicable_when(self) -> tuple[str, str, str] | None:
        """
        Returns (parent_field, operator, value) tuple if conditional,
        else None.  Used by the rules engine.
        """
        if self.condition_parent_field is None:
            return None
        return (
            self.condition_parent_field,
            self.condition_operator or "==",
            self.condition_value or "",
        )

    def validate_value(self, value: object) -> bool:
        """
        Check whether a value is valid for this field.
        Always returns True for open fields or special markers.
        """
        if value in (self.missing_marker, self.not_applicable_marker,
                     "Unknown", "No Data", "Not applicable", "Uncertain", None):
            return True
        if self.is_open:
            return True
        return str(value) in self.allowed_values

    def is_eligible_for(
        self,
        doc_type: "DocType",
        routing_subtype: str | None = None,
    ) -> bool:
        """
        Return True if this field may be extracted from a document of the
        given doc_type (and optional subtype).

        A routing entry `(T, None)` matches any document of type T.
        A routing entry `(T, s)` matches only documents of type T whose
        runtime routing_subtype equals s (case-insensitive).

        Fields with no parsed routing entries default to ineligible for all
        non-discharge doc types — the registry must declare routing
        explicitly. DISCHARGE_SUMMARY has a permissive fallback here because
        the registry intentionally lists it on every routed field.
        """
        if not self.routing:
            return doc_type == DocType.DISCHARGE_SUMMARY
        sub = (routing_subtype or "").strip().lower() or None
        for entry_type, entry_sub in self.routing:
            if entry_type != doc_type:
                continue
            if entry_sub is None:
                return True
            if sub is not None and entry_sub.lower() == sub:
                return True
        return False


# ── Routing parser ────────────────────────────────────────────────────────────

def parse_routing(raw: str | None) -> tuple[tuple[DocType, str | None], ...]:
    """
    Parse the registry `routing` column into a tuple of (DocType, subtype) pairs.

    Grammar (pipe-separated list of entries):
        entry  := TYPE | TYPE ":" SUBTYPE
        TYPE   := one of the DocType enum names (case-insensitive)
        SUBTYPE := free-form lowercase identifier (e.g. 'gastroscopy', 'uh')

    Unknown DocType names are skipped silently — the registry is the source
    of truth and should only contain known values, so this keeps the loader
    resilient to forward additions while still surfacing those values in
    validation warnings if needed.

    Returns
    -------
    tuple[tuple[DocType, str | None], ...]
        Empty tuple when `raw` is None / empty.
    """
    if not raw:
        return ()
    entries: list[tuple[DocType, str | None]] = []
    for part in str(raw).split("|"):
        token = part.strip()
        if not token:
            continue
        if ":" in token:
            type_str, sub_str = token.split(":", 1)
            subtype: str | None = sub_str.strip().lower() or None
        else:
            type_str, subtype = token, None
        try:
            dt = DocType(type_str.strip().upper())
        except ValueError:
            continue
        entries.append((dt, subtype))
    return tuple(entries)
