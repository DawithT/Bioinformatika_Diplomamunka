"""
app/pipeline/step8_export.py

Step 8 of the pipeline — Export.

Produces all output files for one processed CaseResult:
  - wide_table.xlsx       one row, all fields, Uncertain cells highlighted yellow
  - wide_table_labels.xlsx  variable_name | label  (SPSS label import)
  - wide_table.csv        UTF-8 with BOM; "," and "." decimals by default
                          (STATA/SPSS/R), or ";" and "," decimals for
                          Excel with Hungarian regional settings (csv_format)
  - conflicts.xlsx        audit trail of discharge vs ancillary disagreements
  - mismatch.xlsx         documents whose patient fingerprint didn't match
  - history_followup.xlsx documents dated outside the admission window
  - unit_mismatches.xlsx  extracted units differing from the registry canonical
  - ancillary_docs.xlsx   audit of UNKNOWN / AMBULANSLAP documents
  - run_config.json       LLM on/off and parameters, cache mode, registry hash
                          (written when the caller passes run_config)

All files are written to out_dir/<case_id>/.  out_dir is the destination
the user chose for the run (settings.output_folder_path when none was given).
No ZIP is written here; the orchestrator adds one beside the case folder when
the user asked for it.

Public API
----------
export(result, out_dir, registry, run_config=None, csv_format="intl") -> list[Path]
    Write all files and return the list of file paths.  No workbook ever
    contains a formula: text starting with "=" is written as text, otherwise
    Excel would report the file as damaged.

sanitise_col(raw_key) -> str
    Make a column name safe for R / SPSS / STATA:
      - replace _d-N  with  _dmN   (negative day numbers)
      - strip remaining non-alphanumeric/underscore characters
      - prefix v_ if result starts with a digit
      - truncate to MAX_COL_LEN (60) characters
      - on collision after truncation: append _{sha256[:4]}

build_col_order(fields, registry) -> list[str]
    Canonical column ordering:
      1. canonical fields (patient identity) in registry order
      2. anam  fields in registry order
      3. _adm  fields in registry order
      4. _inhosp fields in registry order
      5. _dis  fields in registry order
      6. dynamic lab columns sorted by (day, analyte, occurrence_index)
      7. dynamic event columns sorted by (prefix, day, event_index, sub_field)
      8. any remaining fields (sorted) — catch-all for unknown keys

Column name notes
-----------------
Raw keys from extraction use Python-safe names already (snake_case from
registry + col_key helpers).  Negative day keys look like "creatinine_d-1".
These are sanitised on write: "creatinine_d-1" → "creatinine_dm1".
The sanitised names are what appear in the XLSX/CSV headers.

Styling
-------
- Yellow fill (FFFF00) for any cell whose value is exactly "Uncertain"
- Frozen top row (freeze_panes = "A2")
- Auto-filter on header row
- Column widths: max(header_len, max_data_len) capped at MAX_COL_WIDTH (40)
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from app.domain.models import (
    AncillaryRecord, CaseResult, ConflictRecord, MismatchRecord,
    OutOfWindowRecord, UnitMismatchRecord,
)
from app.registry.loader import unresolved_parent_warnings
from app.registry.models import FieldDefinition

log = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

# wide_table.csv layouts — see _write_csv_bom
CSV_FORMATS = ("intl", "hu")

MAX_COL_LEN   = 60      # STATA max is 32 for old versions, 64 for modern — target 60
MAX_COL_WIDTH = 40      # Excel column width cap (characters)
_UNCERTAIN    = "Uncertain"

# Prefix used to flag a wide-table cell whose registry definition has an
# unresolved condition_parent_field.  Inserted by _write_wide_table() so that
# non-coding users can locate the offending row in Registry_base.xlsx without
# reading the application logs.  See app.registry.loader.unresolved_parent_warnings.
_REGISTRY_WARNING_PREFIX = "REGISTRY_WARNING"

# Characters that openpyxl / Excel reject in worksheet cells.
# This includes all C0 control characters except tab (0x09), newline (0x0A),
# carriage return (0x0D), and the small set of allowed whitespace.
_CTRL_CHARS_RE = re.compile(
    r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]"   # C0 controls (excl. HT, LF, CR)
)


def _sanitise_cell(value: Any) -> Any:
    """Strip XML-illegal characters from string cell values.

    openpyxl raises IllegalCharacterError when writing strings that contain
    C0 control characters other than tab/LF/CR.  These can appear when OCR or
    text-layer extraction produces binary artefacts.
    """
    if isinstance(value, str):
        return _CTRL_CHARS_RE.sub("", value)
    return value


def _cell_value(raw: Any) -> Any:
    """
    Prepare a field value for writing to an xlsx cell.
    - None → None (truly empty cell)
    - list-field JSON strings (starting with '[') → kept as string
    - partial date strings (YYYY or YYYY-MM) → kept as string
    - everything else → unchanged
    """
    if raw is None:
        return None
    s = str(raw)

    # JSON array strings must not be parsed as numbers
    if s.startswith("["):
        return s

    # Partial dates: YYYY or YYYY-MM must stay as strings, not become Excel dates
    if re.match(r'^\d{4}(?:-\d{2})?$', s):
        return s

    return raw


# Registry timepoint order (matches architecture §12 column ordering)
_TIMEPOINT_ORDER = ("anam", "adm", "inhosp", "dis")

# Dynamic column patterns
# Lab:   {analyte}_d{day}  or  {analyte}_d{day}_{n}
#        (day may be negative before sanitisation → _d-1)
_LAB_RE = re.compile(
    r"^(.+?)_d(-?\d+)(?:_(\d+))?$"
)
# Event: {prefix}_d{day}_{n}_{subfield}   (always has subfield)
_EVENT_RE = re.compile(
    r"^(endo_gastro|endo_colono|fibro|rtg|uh|ct|mri|micro)_d(-?\d+)_(\d+)_(.+)$"
)

# ── Column-name sanitisation ──────────────────────────────────────────────────

_NEGATIVE_DAY_RE = re.compile(r"_d-(\d+)")
_ILLEGAL_CHARS_RE = re.compile(r"[^a-zA-Z0-9_]")


def sanitise_col(raw_key: str) -> str:
    """
    Make *raw_key* safe for R / SPSS / STATA column names.

    Steps:
    1. Replace ``_d-N`` → ``_dmN``   (negative day number)
    2. Remove all characters that are not alphanumeric or underscore
    3. Prefix ``v_`` if the result starts with a digit
    4. Truncate to MAX_COL_LEN (60) chars

    Collision handling (truncation only):
    Callers that need deduplication should use ``build_safe_cols()``
    which handles the suffix logic across the full column list.
    """
    s = _NEGATIVE_DAY_RE.sub(lambda m: f"_dm{m.group(1)}", raw_key)
    s = _ILLEGAL_CHARS_RE.sub("_", s)
    # Collapse consecutive underscores
    s = re.sub(r"_+", "_", s).strip("_")
    if s and s[0].isdigit():
        s = "v_" + s
    return s[:MAX_COL_LEN] or "unknown"


def build_safe_cols(raw_keys: list[str]) -> dict[str, str]:
    """
    Return a mapping raw_key → safe_col_name with guaranteed uniqueness.

    If truncation produces a collision the shorter key keeps its name and
    the later one gets a 4-char sha256 suffix (``_XXXX``).
    """
    used: dict[str, str] = {}   # safe_name → first raw_key that claimed it
    result: dict[str, str] = {}

    for raw in raw_keys:
        candidate = sanitise_col(raw)
        if candidate not in used:
            used[candidate] = raw
            result[raw] = candidate
        else:
            # Collision: append 4-char hash of the raw key
            suffix = hashlib.sha256(raw.encode()).hexdigest()[:4]
            # Ensure the suffixed name still fits in MAX_COL_LEN
            base = candidate[: MAX_COL_LEN - 5]
            deduped = f"{base}_{suffix}"
            result[raw] = deduped
            log.warning(
                "Column name collision: %r and %r both map to %r — "
                "renamed second to %r",
                used[candidate], raw, candidate, deduped,
            )

    return result


# ── Column ordering ───────────────────────────────────────────────────────────

def _lab_sort_key(raw_key: str) -> tuple:
    """Sort key for dynamic lab columns: (day, analyte, occurrence_index)."""
    m = _LAB_RE.match(raw_key)
    if not m:
        return (9999, raw_key, 0)
    analyte, day_str, idx_str = m.group(1), m.group(2), m.group(3)
    return (int(day_str), analyte, int(idx_str) if idx_str else 0)


def _event_sort_key(raw_key: str) -> tuple:
    """Sort key for dynamic event columns: (prefix, day, event_index, subfield)."""
    m = _EVENT_RE.match(raw_key)
    if not m:
        return ("zzz", 9999, 0, raw_key)
    prefix, day_str, idx_str, subfield = m.groups()
    return (prefix, int(day_str), int(idx_str), subfield)


def build_col_order(
    fields: dict[str, Any],
    registry: list[FieldDefinition],
) -> list[str]:
    """
    Return the canonical column ordering for the wide table.

    Parameters
    ----------
    fields : dict[str, Any]
        Keys are raw field names (may include dynamic lab/event keys).
    registry : list[FieldDefinition]
        Loaded registry — defines order for static soft fields.

    Returns
    -------
    list[str]
        All keys from *fields* in the correct output order.
    """
    # Index static registry fields by their field_name, preserving registry order
    registry_order: list[str] = [fd.field_name for fd in registry]
    registry_set: set[str] = set(registry_order)

    all_keys = set(fields.keys())

    # ── Partition into static (registry) and dynamic (lab/event) ──────────────
    static_keys = all_keys & registry_set
    dynamic_keys = all_keys - registry_set

    lab_keys:   list[str] = []
    event_keys: list[str] = []
    other_keys: list[str] = []

    for k in dynamic_keys:
        if _EVENT_RE.match(k):
            event_keys.append(k)
        elif _LAB_RE.match(k):
            lab_keys.append(k)
        else:
            other_keys.append(k)

    # ── Static fields in timepoint order (canonical first, then by timepoint) ─
    canonical_out:  list[str] = []
    timepoint_out: dict[str, list[str]] = {tp: [] for tp in _TIMEPOINT_ORDER}

    for fd in registry:
        if fd.field_name not in static_keys:
            continue
        if fd.canonical:
            canonical_out.append(fd.field_name)
        else:
            tp = fd.timepoint if fd.timepoint in timepoint_out else "anam"
            timepoint_out[tp].append(fd.field_name)

    static_ordered: list[str] = canonical_out.copy()
    for tp in _TIMEPOINT_ORDER:
        static_ordered.extend(timepoint_out[tp])

    # ── Dynamic fields sorted deterministically ───────────────────────────────
    lab_keys_sorted   = sorted(lab_keys,   key=_lab_sort_key)
    event_keys_sorted = sorted(event_keys, key=_event_sort_key)
    other_keys_sorted = sorted(other_keys)

    return static_ordered + lab_keys_sorted + event_keys_sorted + other_keys_sorted


# ── openpyxl helpers ──────────────────────────────────────────────────────────

def _make_workbook():
    """Import openpyxl and return ``(workbook, active_sheet)``.

    The active sheet is asserted non-None so callers don't need to repeat
    that check (openpyxl always returns a fresh worksheet on a new workbook,
    but the type stubs declare it as ``Worksheet | None``).
    """
    try:
        import openpyxl  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required for export. Install with: pip install openpyxl"
        ) from exc
    wb = openpyxl.Workbook()
    ws = wb.active
    assert ws is not None, "openpyxl Workbook() always has an active sheet"
    return wb, ws


def _save_workbook(wb, path: Path) -> None:
    """
    Save *wb*, writing every cell as data.

    openpyxl stores any string that starts with "=" as a formula.  Extracted
    text such as "= 5 mm" or "=(Mintavétel: )" is not a valid formula, and
    Excel then reports the whole file as damaged and offers to repair it.
    No output is meant to contain formulas, so such cells are written back
    as plain text.
    """
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
    wb.save(str(path))


def _yellow_fill():
    from openpyxl.styles import PatternFill
    return PatternFill(fill_type="solid", fgColor="FFFF00")


def _header_font():
    from openpyxl.styles import Font
    return Font(bold=True)


def _autofit(ws, max_width: int = MAX_COL_WIDTH) -> None:
    """Set column widths based on content, capped at *max_width*."""
    for col_cells in ws.columns:
        max_len = 0
        col_letter = col_cells[0].column_letter
        for cell in col_cells:
            if cell.value is not None:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col_letter].width = min(max_len + 2, max_width)


# ── Wide table ────────────────────────────────────────────────────────────────

def _write_wide_table(
    path: Path,
    result: CaseResult,
    col_order: list[str],
    safe_cols: dict[str, str],
    registry_warnings: dict[str, str] | None = None,
) -> None:
    """Write wide_table.xlsx — one row, highlighted Uncertain cells.

    If *registry_warnings* is provided, any column whose raw key appears
    in the dict has its cell value prefixed with the warning text and
    highlighted yellow so non-coding users can spot the registry data
    issue directly in the output file.
    """
    wb, ws = _make_workbook()
    ws.title = "wide_table"

    yellow = _yellow_fill()
    bold   = _header_font()
    warnings_map = registry_warnings or {}

    # Header row
    for col_idx, raw_key in enumerate(col_order, start=1):
        cell = ws.cell(row=1, column=col_idx, value=safe_cols[raw_key])
        cell.font = bold

    # Data row
    row_data = result.to_plain()
    for col_idx, raw_key in enumerate(col_order, start=1):
        # Pass the extracted data through _cell_value to protect JSON lists and partial dates
        safe_value = _cell_value(row_data.get(raw_key))
        value = _sanitise_cell(safe_value)

        warning_text = warnings_map.get(raw_key)
        if warning_text:
            value = f"{warning_text} | original_value={value}"

        cell = ws.cell(row=2, column=col_idx, value=value)
        if warning_text or value == _UNCERTAIN:
            cell.fill = yellow

    # Freeze header row
    ws.freeze_panes = "A2"

    # Set auto_filter with explicit range (ws.dimensions can be incorrect)
    from openpyxl.utils import get_column_letter
    if col_order:
        last_col = get_column_letter(len(col_order))
        ws.auto_filter.ref = f"A1:{last_col}2"

    _autofit(ws)
    _save_workbook(wb, path)


def _write_labels(
    path: Path,
    col_order: list[str],
    safe_cols: dict[str, str],
    registry: list[FieldDefinition],
) -> None:
    """
    Write wide_table_labels.xlsx — two columns:
        variable_name (sanitised) | label (human-readable column_header)
    For dynamic columns not in the registry, label = variable_name.
    """
    wb, ws = _make_workbook()
    ws.title = "labels"

    bold = _header_font()
    ws.cell(row=1, column=1, value="variable_name").font = bold
    ws.cell(row=1, column=2, value="label").font = bold

    # Build label lookup from registry
    label_lookup: dict[str, str] = {fd.field_name: fd.column_header for fd in registry}

    for row_idx, raw_key in enumerate(col_order, start=2):
        safe = safe_cols[raw_key]
        label = label_lookup.get(raw_key, safe)
        ws.cell(row=row_idx, column=1, value=safe)
        ws.cell(row=row_idx, column=2, value=label)

    _autofit(ws)
    _save_workbook(wb, path)


def _write_csv_bom(
    path: Path,
    result: CaseResult,
    col_order: list[str],
    safe_cols: dict[str, str],
    registry_warnings: dict[str, str] | None = None,
    csv_format: str = "intl",
) -> None:
    """
    Write wide_table.csv with UTF-8 BOM so Excel and STATA/SPSS open it
    correctly without an import wizard.

    *csv_format* (see CSV_FORMATS):
      - "intl" \u2014 "," separator, "." decimals (R / STATA / SPSS defaults);
      - "hu"   \u2014 ";" separator, "," decimals, what Excel with Hungarian
                 regional settings expects.  In the "intl" file such an
                 Excel reads "6.4" as 4 June.

    Cells whose raw key appears in *registry_warnings* are prefixed with
    the warning text (matching the xlsx output).
    """
    if csv_format not in CSV_FORMATS:
        raise ValueError(f"csv_format must be one of {CSV_FORMATS}, got {csv_format!r}")
    sep = ";" if csv_format == "hu" else ","
    decimal_comma = csv_format == "hu"

    row_data = result.to_plain()
    warnings_map = registry_warnings or {}
    header = sep.join(safe_cols[k] for k in col_order)

    def _format_cell(raw_key: str) -> str:
        raw_value = row_data.get(raw_key)
        warning = warnings_map.get(raw_key)
        if warning:
            return _csv_cell(f"{warning} | original_value={raw_value}", sep)
        return _csv_cell(raw_value, sep, decimal_comma)

    data_row = sep.join(_format_cell(k) for k in col_order)
    bom = "\ufeff"
    path.write_text(bom + header + "\n" + data_row + "\n", encoding="utf-8")


# A number written as text ("98.9" from the scores parser)
_DECIMAL_TEXT_RE = re.compile(r"^-?\d+\.\d+$")


def _csv_cell(value: Any, sep: str = ",", decimal_comma: bool = False) -> str:
    """
    Escape a single CSV cell value (strips illegal control characters).

    With *decimal_comma*, numbers \u2014 and text that is just a decimal number \u2014
    are written with "," as the decimal mark.
    """
    if value is None:
        return ""
    s = _CTRL_CHARS_RE.sub("", str(value))
    if decimal_comma and (
        (isinstance(value, (int, float)) and not isinstance(value, bool))
        or _DECIMAL_TEXT_RE.match(s)
    ):
        s = s.replace(".", ",")
    if sep in s or '"' in s or "\n" in s:
        return '"' + s.replace('"', '""') + '"'
    return s


# ── Ancillary reports ─────────────────────────────────────────────────────────

def _write_conflicts(path: Path, records: list[ConflictRecord]) -> None:
    """conflicts.xlsx — one row per ConflictRecord."""
    wb, ws = _make_workbook()
    ws.title = "conflicts"

    headers = [
        "case_id", "field", "source_policy",
        "retained_value", "retained_doc_type",
        "overridden_value", "overridden_doc_type",
        "page",
    ]
    bold = _header_font()
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h).font = bold

    for row_idx, rec in enumerate(records, start=2):
        ws.cell(row=row_idx, column=1, value=rec.case_id)
        ws.cell(row=row_idx, column=2, value=rec.field)
        ws.cell(row=row_idx, column=3, value=rec.source_policy)
        ws.cell(row=row_idx, column=4, value=_sanitise_cell(rec.retained_value))
        ws.cell(row=row_idx, column=5, value=rec.retained_doc_type.value)
        ws.cell(row=row_idx, column=6, value=_sanitise_cell(rec.overridden_value))
        ws.cell(row=row_idx, column=7, value=rec.overridden_doc_type.value)
        ws.cell(row=row_idx, column=8, value=rec.page)

    ws.freeze_panes = "A2"
    _autofit(ws)
    _save_workbook(wb, path)


def _write_mismatches(path: Path, records: list[MismatchRecord]) -> None:
    """mismatch.xlsx — one row per MismatchRecord."""
    wb, ws = _make_workbook()
    ws.title = "mismatch"

    headers = ["case_id", "filename", "expected_fingerprint", "found_name", "found_taj"]
    bold = _header_font()
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h).font = bold

    for row_idx, rec in enumerate(records, start=2):
        ws.cell(row=row_idx, column=1, value=rec.case_id)
        ws.cell(row=row_idx, column=2, value=rec.filename)
        ws.cell(row=row_idx, column=3, value=rec.expected_fingerprint)
        ws.cell(row=row_idx, column=4, value=rec.found_name or "")
        ws.cell(row=row_idx, column=5, value=rec.found_taj or "")

    ws.freeze_panes = "A2"
    _autofit(ws)
    _save_workbook(wb, path)


def _write_history_followup(path: Path, records: list[OutOfWindowRecord]) -> None:
    """history_followup.xlsx — one row per OutOfWindowRecord."""
    wb, ws = _make_workbook()
    ws.title = "history_followup"

    headers = [
        "case_id", "filename", "doc_type",
        "event_date", "days_from_admission", "window_type", "summary",
    ]
    bold = _header_font()
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h).font = bold

    for row_idx, rec in enumerate(records, start=2):
        ws.cell(row=row_idx, column=1, value=rec.case_id)
        ws.cell(row=row_idx, column=2, value=rec.filename)
        ws.cell(row=row_idx, column=3, value=rec.doc_type.value)
        ws.cell(row=row_idx, column=4, value=rec.event_date.isoformat())
        ws.cell(row=row_idx, column=5, value=rec.days_from_admission)
        ws.cell(row=row_idx, column=6, value=rec.window_type.value)
        ws.cell(row=row_idx, column=7, value=rec.summary)

    ws.freeze_panes = "A2"
    _autofit(ws)
    _save_workbook(wb, path)


def _write_unit_mismatches(path: Path, records: list[UnitMismatchRecord]) -> None:
    """unit_mismatches.xlsx — one row per field whose extracted unit
    disagreed with the registry canonical unit. No conversion is performed;
    the raw value is kept for the reviewer to reconcile manually.
    """
    wb, ws = _make_workbook()
    ws.title = "unit_mismatches"

    headers = [
        "case_id", "field", "expected_unit", "seen_unit",
        "raw_value", "doc_type", "page",
    ]
    bold = _header_font()
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h).font = bold

    for row_idx, rec in enumerate(records, start=2):
        ws.cell(row=row_idx, column=1, value=rec.case_id)
        ws.cell(row=row_idx, column=2, value=rec.field)
        ws.cell(row=row_idx, column=3, value=rec.expected_unit)
        ws.cell(row=row_idx, column=4, value=rec.seen_unit)
        ws.cell(row=row_idx, column=5, value=_sanitise_cell(rec.raw_value))
        ws.cell(row=row_idx, column=6, value=rec.doc_type.value)
        ws.cell(row=row_idx, column=7, value=rec.page)

    ws.freeze_panes = "A2"
    _autofit(ws)
    _save_workbook(wb, path)


def _write_ancillary_docs(path: Path, records: list[AncillaryRecord]) -> None:
    """ancillary_docs.xlsx — one row per UNKNOWN or AMBULANSLAP document.

    For UNKNOWN docs the row matches the historical triage shape
    (routing_method / failure_reason / best_score / text_preview).
    For AMBULANSLAP docs the same triage columns are populated
    (failure_reason = "classified_ancillary") and three extra columns carry
    the pulled-out sections: ``velemeny`` (raw text), ``beavatkozasok``
    (raw text), and ``diagnoses_json`` (JSON array of ``{code, description}``).
    """
    import json as _json

    wb, ws = _make_workbook()
    ws.title = "ancillary_docs"

    headers = [
        "case_id", "filename", "doc_type",
        "routing_method", "failure_reason", "best_score",
        "text_preview",
        "velemeny", "beavatkozasok", "diagnoses_json",
    ]
    bold = _header_font()
    for col_idx, h in enumerate(headers, start=1):
        ws.cell(row=1, column=col_idx, value=h).font = bold

    for row_idx, rec in enumerate(records, start=2):
        ws.cell(row=row_idx, column=1, value=rec.case_id)
        ws.cell(row=row_idx, column=2, value=rec.filename)
        ws.cell(row=row_idx, column=3, value=rec.doc_type.value)
        ws.cell(row=row_idx, column=4, value=rec.routing_method)
        ws.cell(row=row_idx, column=5, value=rec.failure_reason)
        ws.cell(row=row_idx, column=6, value=rec.best_score)
        ws.cell(row=row_idx, column=7, value=_sanitise_cell(rec.text_preview))
        ws.cell(
            row=row_idx, column=8,
            value=_sanitise_cell(rec.velemeny) if rec.velemeny else None,
        )
        ws.cell(
            row=row_idx, column=9,
            value=_sanitise_cell(rec.beavatkozasok) if rec.beavatkozasok else None,
        )
        diagnoses_json = (
            _json.dumps(list(rec.diagnozisok), ensure_ascii=False)
            if rec.diagnozisok else None
        )
        ws.cell(row=row_idx, column=10, value=diagnoses_json)

    ws.freeze_panes = "A2"
    _autofit(ws)
    _save_workbook(wb, path)


# ── Public entry point ────────────────────────────────────────────────────────

def export(
    result: CaseResult,
    out_dir: Path,
    registry: list[FieldDefinition],
    run_config: dict | None = None,
    csv_format: str = "intl",
) -> list[Path]:
    """
    Write all output files for *result* into *out_dir*/<case_id>/ and return
    the list of written file paths.

    Parameters
    ----------
    result : CaseResult
        Fully merged + rules-applied CaseResult.
    out_dir : Path
        Parent output directory; a case subfolder is created inside.
    registry : list[FieldDefinition]
        Loaded registry — used for column ordering and label lookup.
    run_config : dict | None
        Configuration of the run (app.services.run_config.describe_run);
        written as <case_id>_run_config.json so every result records whether
        the LLM was used and with which settings.
    csv_format : str
        Layout of wide_table.csv: "intl" (default; "," and ".") or "hu"
        (";" and "," for Excel with Hungarian regional settings).

    Returns
    -------
    list[Path]
        All written file paths (inside the case subfolder).
    """
    case_id = result.case_id
    case_dir = out_dir / case_id
    case_dir.mkdir(parents=True, exist_ok=True)

    # ── Build column order and safe names ─────────────────────────────────────
    col_order = build_col_order(result.fields, registry)
    safe_cols = build_safe_cols(col_order)

    # ── Per-cell registry warnings (e.g. unresolved condition_parent_field) ──
    registry_warnings = unresolved_parent_warnings(registry)
    if registry_warnings:
        log.warning(
            "Registry has %d unresolved condition_parent_field reference(s) — "
            "wide table cells will be flagged with REGISTRY_WARNING",
            len(registry_warnings),
        )

    # ── Write individual files ─────────────────────────────────────────────────
    wide_xlsx  = case_dir / f"{case_id}_wide_table.xlsx"
    labels_xlsx = case_dir / f"{case_id}_wide_table_labels.xlsx"
    wide_csv   = case_dir / f"{case_id}_wide_table.csv"
    conf_xlsx  = case_dir / f"{case_id}_conflicts.xlsx"
    mm_xlsx    = case_dir / f"{case_id}_mismatch.xlsx"
    oow_xlsx   = case_dir / f"{case_id}_history_followup.xlsx"
    unit_xlsx  = case_dir / f"{case_id}_unit_mismatches.xlsx"
    anc_xlsx   = case_dir / f"{case_id}_ancillary_docs.xlsx"

    log.info("Exporting case %r → %s", case_id, case_dir)

    _write_wide_table(wide_xlsx, result, col_order, safe_cols, registry_warnings)
    log.debug("Wrote %s (%d columns)", wide_xlsx.name, len(col_order))

    _write_labels(labels_xlsx, col_order, safe_cols, registry)
    log.debug("Wrote %s", labels_xlsx.name)

    _write_csv_bom(wide_csv, result, col_order, safe_cols, registry_warnings, csv_format)
    log.debug("Wrote %s (%s)", wide_csv.name, csv_format)

    _write_conflicts(conf_xlsx, result.conflicts)
    log.debug("Wrote %s (%d rows)", conf_xlsx.name, len(result.conflicts))

    _write_mismatches(mm_xlsx, result.mismatches)
    log.debug("Wrote %s (%d rows)", mm_xlsx.name, len(result.mismatches))

    _write_history_followup(oow_xlsx, result.out_of_window)
    log.debug("Wrote %s (%d rows)", oow_xlsx.name, len(result.out_of_window))

    _write_unit_mismatches(unit_xlsx, result.unit_mismatches)
    log.debug("Wrote %s (%d rows)", unit_xlsx.name, len(result.unit_mismatches))

    _write_ancillary_docs(anc_xlsx, result.ancillary_docs)
    log.debug("Wrote %s (%d rows)", anc_xlsx.name, len(result.ancillary_docs))

    all_files = [wide_xlsx, labels_xlsx, wide_csv, conf_xlsx, mm_xlsx, oow_xlsx,
                 unit_xlsx, anc_xlsx]

    if run_config is not None:
        cfg_json = case_dir / f"{case_id}_run_config.json"
        cfg_json.write_text(
            json.dumps(run_config, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        all_files.append(cfg_json)
    log.info("Exported %d files to %s", len(all_files), case_dir)

    return all_files
