"""
app/registry/loader.py

Loads and validates Registry_base.xlsx at application startup.

Public API:
    load_registry(path: Path) -> list[FieldDefinition]
    RegistryValidationError

Internal functions (_prefixed) are exposed for unit testing:
    _parse_enum_sets(ws)          -> dict[set_name, list[str]]
    _parse_main_rows(rows, sets)  -> list[FieldDefinition]
    _validate(fields)             -> list[str]  (error messages)
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.registry.models import FieldDefinition, FORM_SECTION_HINTS, parse_routing


class RegistryValidationError(Exception):
    """Raised when Registry_base.xlsx fails validation at startup."""


def load_registry(path: Path) -> list[FieldDefinition]:
    """
    Load Registry_base.xlsx and return a validated list of FieldDefinitions.

    Raises:
        FileNotFoundError       if the file does not exist
        RegistryValidationError if validation fails
        ImportError             if openpyxl is not installed
    """
    try:
        import openpyxl
    except ImportError as exc:
        raise ImportError(
            "openpyxl is required to load the registry. "
            "Install it with: pip install openpyxl"
        ) from exc

    if not path.exists():
        raise FileNotFoundError(path)

    wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        enum_sets = _parse_enum_sets(wb["Enum_sets"])
        rows = _sheet_to_row_dicts(wb["Main"])
    finally:
        wb.close()

    fields = _parse_main_rows(rows, enum_sets)

    errors = _validate(fields)
    errors.extend(_validate_routing_tokens(rows))
    if errors:
        bullet = "\n  \u2022 "
        raise RegistryValidationError(
            f"Registry_base.xlsx has {len(errors)} validation error(s):"
            f"{bullet}{bullet.join(errors)}"
        )

    # Soft warnings: log but don't block startup
    soft_warnings = _check_warnings(fields)
    soft_warnings.extend(_check_allowed_values_column(rows, enum_sets))
    if soft_warnings:
        import logging as _logging
        _log = _logging.getLogger(__name__)
        _log.warning(
            "Registry loaded with %d soft warning(s):\n  \u2022 %s",
            len(soft_warnings),
            "\n  \u2022 ".join(soft_warnings),
        )

    return fields


def _sheet_to_row_dicts(ws) -> list[dict[str, Any]]:
    """Convert openpyxl worksheet to list of header-keyed dicts."""
    rows_iter = ws.iter_rows(values_only=True)
    try:
        header_row = next(rows_iter)
    except StopIteration:
        return []

    headers = [
        str(h).strip() if h is not None else f"_col{i}"
        for i, h in enumerate(header_row)
    ]

    result: list[dict[str, Any]] = []
    for raw_row in rows_iter:
        if all(v is None for v in raw_row):
            continue
        row_dict = {}
        for header, value in zip(headers, raw_row):
            row_dict[header] = str(value).strip() if value is not None else None
        result.append(row_dict)
    return result


def _parse_enum_sets(ws) -> dict[str, list[str]]:
    """
    Parse Enum_sets sheet.
    Returns {set_name: [label, ...]} ordered by the 'order' column.
    Labels (not codes) are used because validation and LLM prompts
    work with the human-readable values from allowed_values JSON.
    """
    rows = _sheet_to_row_dicts(ws)
    grouped: dict[str, list[tuple[int, str]]] = {}
    for row in rows:
        name = row.get("enum_set") or ""
        label = row.get("label") or ""
        order = row.get("order") or "0"
        if not name or not label:
            continue
        try:
            order_int = int(float(order))
        except (ValueError, TypeError):
            order_int = 0
        grouped.setdefault(name, []).append((order_int, label))

    return {
        name: [lbl for _, lbl in sorted(items, key=lambda x: x[0])]
        for name, items in grouped.items()
    }


def _parse_main_rows(
    rows: list[dict[str, Any]],
    enum_sets: dict[str, list[str]],
) -> list[FieldDefinition]:
    """
    Convert raw row dicts from Main sheet into FieldDefinition objects.
    Separated from load_registry() to allow unit testing with synthetic data.
    """
    fields: list[FieldDefinition] = []

    for row in rows:
        field_name = _clean_identifier(row.get("field_name") or "")
        if not field_name:
            continue

        field_base = _clean_identifier(row.get("field_base") or field_name)
        form_id = _str(row.get("form_id")) or "A"
        timepoint = _str(row.get("timepoint")) or "anam"

        allowed_values = _parse_allowed_values(
            json_str=row.get("allowed_values"),
            enum_set_id=row.get("enum_set"),
            enum_sets=enum_sets,
        )

        # section_hint: derive from form_id (registry column is empty)
        section_hint = (
            _str(row.get("section_hint"))
            or FORM_SECTION_HINTS.get(form_id, form_id)
        )

        fields.append(FieldDefinition(
            field_name=field_name,
            field_base=field_base,
            column_header=_str(row.get("column_header")) or field_name,
            form_id=form_id,
            timepoint=timepoint,
            field_type=_str(row.get("field_type")) or "open",
            value_type=_str(row.get("value_type")) or "text",
            allowed_values=tuple(allowed_values),
            enum_set=_str(row.get("enum_set")),
            source_policy=_str(row.get("source_policy")) or "DISCHARGE_PRIMARY",
            section_hint=section_hint,
            routing=parse_routing(_str(row.get("routing"))),
            repeat_group=_str(row.get("repeat_group")),
            scope=_str(row.get("scope")),
            canonical=_bool(row.get("canonical")),
            dedupe_key=_str(row.get("dedupe_key")),
            multiple_answers_allowed=_bool(row.get("multiple_answers_allowed")),
            missing_marker=_str(row.get("missing_marker")) or "Unknown",
            not_applicable_marker=_str(row.get("not_applicable_marker")) or "ERVENYTELEN",
            condition_parent_field=_str(row.get("condition_parent_field")),
            condition_operator=_normalise_operator(row.get("condition_operator")),
            condition_value=_str(row.get("condition_value")),
            date_format_hint=_str(row.get("date_format_hint")),
            date_granularity_allowed=_str(row.get("date_granularity_allowed")),
            list_storage_format=_str(row.get("list_storage_format")),
            notes=_str(row.get("notes")),
            unit=_str(row.get("unit")),
        ))

    return fields


def _validate(fields: list[FieldDefinition]) -> list[str]:
    """
    Hard validation: returns errors that prevent the app from starting.
    An empty list means the registry is structurally sound.

    Soft issues (e.g. unresolvable condition_parent_field) are checked
    separately by _check_warnings() and logged at startup without blocking.
    """
    errors: list[str] = []

    # 1. No duplicate field_names
    seen: set[str] = set()
    for f in fields:
        if f.field_name in seen:
            errors.append(f"Duplicate field_name: {f.field_name!r}")
        seen.add(f.field_name)

    # 2. No spaces in field_name (field_base spaces are stripped at parse time)
    for f in fields:
        if " " in f.field_name:
            errors.append(
                f"field_name contains spaces: {f.field_name!r} "
                f"(field_base={f.field_base!r})"
            )

    # 3. Valid timepoints
    valid_timepoints = {"anam", "adm", "inhosp", "dis"}
    for f in fields:
        if f.timepoint not in valid_timepoints:
            errors.append(
                f"Unknown timepoint {f.timepoint!r} on field {f.field_name!r}"
            )

    # 4. Valid source_policy values
    valid_policies = {"DISCHARGE_ONLY", "DISCHARGE_PRIMARY", "EXAM_DOC_PRIMARY"}
    for f in fields:
        if f.source_policy not in valid_policies:
            errors.append(
                f"Unknown source_policy {f.source_policy!r} on field {f.field_name!r}"
            )

    # 5. Enum fields must have allowed_values
    for f in fields:
        if f.field_type == "enum" and not f.allowed_values:
            errors.append(
                f"Enum field {f.field_name!r} has no allowed_values"
            )

    # 6. Valid date_granularity_allowed values
    valid_granularities = {None, "", "year", "month", "day"}
    for f in fields:
        if f.date_granularity_allowed not in valid_granularities:
            errors.append(
                f"Unknown date_granularity_allowed "
                f"{f.date_granularity_allowed!r} on field {f.field_name!r} "
                f"(valid values: 'year', 'month', 'day')"
            )

    return errors


def _validate_routing_tokens(rows: list[dict[str, Any]]) -> list[str]:
    """
    Strict check of the ``routing`` column: every pipe-separated token must
    resolve to a known ``DocType`` name.

    ``parse_routing()`` silently drops unknown tokens so the runtime stays
    forward-compatible, but at load time we surface typos (``LAB_REPORRT``,
    stale aliases) as hard errors — otherwise a single misspelled token can
    make a field ineligible for all ancillary documents with no startup
    signal.
    """
    from app.domain.enums import DocType

    known = {dt.value for dt in DocType}
    errors: list[str] = []

    for row in rows:
        raw = _str(row.get("routing"))
        if not raw:
            continue
        field_name = _clean_identifier(row.get("field_name") or "") or "<unnamed>"
        for part in raw.split("|"):
            token = part.strip()
            if not token:
                continue
            type_str = token.split(":", 1)[0].strip().upper()
            if type_str not in known:
                errors.append(
                    f"Unknown DocType {type_str!r} in routing column for "
                    f"field {field_name!r} (raw routing={raw!r}). "
                    f"Valid DocTypes: {sorted(known)}"
                )

    return errors


_KNOWN_OPERATORS = frozenset({"==", "!="})


def _check_warnings(fields: list[FieldDefinition]) -> list[str]:
    """
    Soft checks: returns warnings that are logged at startup but do NOT
    prevent the app from running.  The rules engine silently skips any
    rule whose parent field cannot be resolved.

    Two classes of warnings are produced:

    1. ``condition_parent_field`` references a name that does not exist
       in the registry as a ``field_name``.  Each affected field will
       also be flagged in the exported wide table by
       ``unresolved_parent_warnings()`` so non-coding users can spot
       and fix the offending row in Registry_base.xlsx.

    2. ``condition_operator`` is set to a value other than '==' or '!=' .
       The rules generator will keep the operator unchanged (no negation)
       and the rules engine will refuse to fire it.

    3. ``condition_value`` is not one of the parent's allowed values.  The
       engine compares exactly, so such a rule never matches the parent's
       real answers ("Egyéb" vs "Egyéb (megnevezni)") and the child is
       always set to Not applicable.  Also flagged in the wide table.
    """
    warnings_list: list[str] = []
    by_name = {f.field_name: f for f in fields}

    for f in fields:
        if f.condition_parent_field and f.condition_parent_field not in by_name:
            warnings_list.append(
                f"condition_parent_field {f.condition_parent_field!r} "
                f"referenced by {f.field_name!r} not found in registry — "
                f"Not-applicable rule will be skipped for this field"
            )
        elif _condition_value_mismatch(f, by_name):
            warnings_list.append(
                f"condition_value {f.condition_value!r} on field "
                f"{f.field_name!r} is not an allowed value of parent "
                f"{f.condition_parent_field!r} — the child will always be "
                f"set to Not applicable"
            )
        if f.condition_operator and f.condition_operator not in _KNOWN_OPERATORS:
            warnings_list.append(
                f"condition_operator {f.condition_operator!r} on field "
                f"{f.field_name!r} is not one of {sorted(_KNOWN_OPERATORS)} — "
                f"the rule will not be negated and the engine will skip it"
            )

    return warnings_list


def _check_allowed_values_column(
    rows: list[dict[str, Any]],
    enum_sets: dict[str, list[str]],
) -> list[str]:
    """
    Soft check: the Main sheet's ``allowed_values`` column duplicates the
    Enum_sets labels, and the application reads only the Enum_sets labels
    (see ``_parse_allowed_values``).  Warn when the two copies hold different
    values, so they cannot drift apart unnoticed, and when a field names an
    ``enum_set`` that does not exist — the one case in which the application
    silently falls back to the Main column.  Order is not compared.
    """
    warnings_list: list[str] = []
    for row in rows:
        name = _clean_identifier(row.get("field_name") or "")
        set_name = _str(row.get("enum_set"))
        if not name or not set_name:
            continue
        if set_name not in enum_sets:
            warnings_list.append(
                f"enum_set {set_name!r} of field {name!r} not found in "
                f"Enum_sets — the Main allowed_values column is used instead"
            )
            continue
        try:
            column = json.loads(str(row.get("allowed_values") or "[]"))
        except (json.JSONDecodeError, ValueError):
            column = None
        if not isinstance(column, list):
            continue
        in_column = {str(v) for v in column if v is not None}
        in_sets = set(enum_sets[set_name])
        if in_column and in_column != in_sets:
            warnings_list.append(
                f"allowed_values of {name!r} differ from Enum_sets "
                f"{set_name!r} (which the application uses): only in Main "
                f"{sorted(in_column - in_sets)}, only in Enum_sets "
                f"{sorted(in_sets - in_column)}"
            )
    return warnings_list


def _condition_value_mismatch(
    f: FieldDefinition, by_name: dict[str, FieldDefinition],
) -> bool:
    """True when *f*'s condition_value is not among its enum parent's allowed values."""
    parent = by_name.get(f.condition_parent_field or "")
    if parent is None or not parent.allowed_values or f.condition_value is None:
        return False
    return f.condition_value.strip() not in parent.allowed_values


def unresolved_parent_warnings(
    registry: list[FieldDefinition],
) -> dict[str, str]:
    """
    Build a mapping of ``field_name -> warning text`` for every field whose
    condition cannot work: its ``condition_parent_field`` does not resolve to
    a real field_name, or its ``condition_value`` is not one of the parent's
    allowed values.  Used by the export layer (step8_export) to insert per-cell
    warning markers into the wide table so non-coding users can locate and
    fix the offending registry row without reading the application logs.

    Returns
    -------
    dict[str, str]
        Empty dict if every condition resolves correctly.
    """
    by_name = {f.field_name: f for f in registry}
    out: dict[str, str] = {}
    for f in registry:
        if f.condition_parent_field and f.condition_parent_field not in by_name:
            out[f.field_name] = (
                f"REGISTRY_WARNING: condition_parent_field "
                f"{f.condition_parent_field!r} not found — fix in "
                f"Registry_base.xlsx (row with field_name={f.field_name!r})"
            )
        elif _condition_value_mismatch(f, by_name):
            out[f.field_name] = (
                f"REGISTRY_WARNING: condition_value {f.condition_value!r} is "
                f"not an allowed value of {f.condition_parent_field!r} — fix "
                f"in Registry_base.xlsx (row with field_name={f.field_name!r})"
            )
    return out


# ── Registry query helpers ────────────────────────────────────────────────────

def fields_by_section(registry: list[FieldDefinition]) -> dict[str, list[FieldDefinition]]:
    """Group fields by section_hint for section-aware LLM prompting."""
    groups: dict[str, list[FieldDefinition]] = {}
    for f in registry:
        groups.setdefault(f.section_hint, []).append(f)
    return groups


def fields_by_form(registry: list[FieldDefinition]) -> dict[str, list[FieldDefinition]]:
    """Group fields by form_id (A, B1, Bmid, Bdis)."""
    groups: dict[str, list[FieldDefinition]] = {}
    for f in registry:
        groups.setdefault(f.form_id, []).append(f)
    return groups


def get_field(registry: list[FieldDefinition], field_name: str) -> FieldDefinition | None:
    """Return the FieldDefinition for a given field_name, or None."""
    for f in registry:
        if f.field_name == field_name:
            return f
    return None


def canonical_fields(registry: list[FieldDefinition]) -> list[FieldDefinition]:
    """Return canonical (patient identity) fields."""
    return [f for f in registry if f.canonical]


def conditional_fields(registry: list[FieldDefinition]) -> list[FieldDefinition]:
    """Return fields that have a condition_parent_field."""
    return [f for f in registry if f.is_conditional]


# ── Private utilities ─────────────────────────────────────────────────────────

def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in ("1", "true", "yes")


def _clean_identifier(value: Any) -> str:
    """Strip all whitespace (including internal) from an identifier string."""
    s = str(value or "").strip()
    return re.sub(r"\s+", "", s)


def _normalise_operator(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if s in ("=", "==", "eq", "equals"):
        return "=="
    return s


def _parse_allowed_values(
    json_str: Any,
    enum_set_id: Any,
    enum_sets: dict[str, list[str]],
) -> list[str]:
    """
    Resolve allowed values.  Priority: enum_set lookup > JSON string > [].
    """
    set_name = _str(enum_set_id)
    if set_name and set_name in enum_sets:
        return enum_sets[set_name]

    if json_str:
        try:
            parsed = json.loads(str(json_str))
            if isinstance(parsed, list):
                return [str(v) for v in parsed if v is not None]
        except (json.JSONDecodeError, ValueError):
            pass

    return []
