"""
tests/unit/test_registry.py

Step 3 tests: FieldDefinition model + loader internal functions.

Two test classes:
  TestFieldDefinition  — stdlib only, no xlsx needed
  TestParseMainRows    — tests _parse_main_rows with synthetic data
  TestValidation       — tests _validate with deliberately broken data
  TestQueryHelpers     — tests fields_by_section, get_field, etc.
  TestRealRegistry     — integration test against real Registry_base.xlsx
                          uses raw XML parsing (no openpyxl needed)

Run:
  pytest tests/unit/test_registry.py -v
  pytest tests/unit/test_registry.py -v -k "not TestRealRegistry"  # skip integration
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.registry.models import FieldDefinition, FORM_SECTION_HINTS
from app.registry.loader import (
    _parse_main_rows,
    _parse_allowed_values,
    _validate,
    _check_warnings,
    _check_allowed_values_column,
    unresolved_parent_warnings,
    _clean_identifier,
    _bool,
    _str,
    _normalise_operator,
    fields_by_section,
    fields_by_form,
    get_field,
    canonical_fields,
    conditional_fields,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_row(
    field_name="test_field",
    field_base="test_field",
    form_id="A",
    timepoint="anam",
    field_type="open",
    value_type="text",
    allowed_values="[]",
    enum_set=None,
    source_policy="DISCHARGE_PRIMARY",
    section_hint=None,
    canonical="0",
    dedupe_key=None,
    multiple_answers_allowed="0",
    missing_marker="NA",
    not_applicable_marker="ERVENYTELEN",
    condition_parent_field=None,
    condition_operator=None,
    condition_value=None,
    column_header="Test Field",
    date_format_hint=None,
    date_granularity_allowed=None,
    list_storage_format=None,
    notes=None,
    unit=None,
    repeat_group=None,
    scope="soft_scalar",
) -> dict:
    return {
        "field_name": field_name,
        "field_base": field_base,
        "form_id": form_id,
        "timepoint": timepoint,
        "field_type": field_type,
        "value_type": value_type,
        "allowed_values": allowed_values,
        "enum_set": enum_set,
        "source_policy": source_policy,
        "section_hint": section_hint,
        "canonical": canonical,
        "dedupe_key": dedupe_key,
        "multiple_answers_allowed": multiple_answers_allowed,
        "missing_marker": missing_marker,
        "not_applicable_marker": not_applicable_marker,
        "condition_parent_field": condition_parent_field,
        "condition_operator": condition_operator,
        "condition_value": condition_value,
        "column_header": column_header,
        "date_format_hint": date_format_hint,
        "date_granularity_allowed": date_granularity_allowed,
        "list_storage_format": list_storage_format,
        "notes": notes,
        "unit": unit,
        "repeat_group": repeat_group,
        "scope": scope,
    }


# ── FieldDefinition ───────────────────────────────────────────────────────────

class TestFieldDefinition:
    def _make(self, **overrides) -> FieldDefinition:
        defaults = dict(
            field_name="test_field", field_base="test_field",
            column_header="Test Field", form_id="A", timepoint="anam",
            field_type="open", value_type="text",
            allowed_values=(), enum_set=None,
            source_policy="DISCHARGE_PRIMARY",
            section_hint="Anamnézis", routing=(), repeat_group=None, scope=None,
            canonical=False, dedupe_key=None,
            multiple_answers_allowed=False,
            missing_marker="NA", not_applicable_marker="ERVENYTELEN",
            condition_parent_field=None, condition_operator=None,
            condition_value=None,
            date_format_hint=None, date_granularity_allowed=None,
            list_storage_format=None,
            notes=None, unit=None,
        )
        defaults.update(overrides)
        return FieldDefinition(**defaults)

    def test_is_enum(self):
        assert self._make(field_type="enum").is_enum
        assert not self._make(field_type="open").is_enum

    def test_is_open(self):
        assert self._make(field_type="open").is_open
        assert not self._make(field_type="enum").is_open

    def test_is_conditional_true(self):
        f = self._make(condition_parent_field="parent_field",
                       condition_operator="==", condition_value="Igen")
        assert f.is_conditional
        assert f.is_applicable_when == ("parent_field", "==", "Igen")

    def test_is_conditional_false(self):
        f = self._make()
        assert not f.is_conditional
        assert f.is_applicable_when is None

    def test_is_canonical(self):
        assert self._make(canonical=True).is_canonical
        assert not self._make(canonical=False).is_canonical

    def test_validate_value_enum_valid(self):
        f = self._make(field_type="enum", allowed_values=("Igen", "Nem"))
        assert f.validate_value("Igen")
        assert f.validate_value("Nem")

    def test_validate_value_enum_invalid(self):
        f = self._make(field_type="enum", allowed_values=("Igen", "Nem"))
        assert not f.validate_value("Maybe")

    def test_validate_value_special_markers(self):
        f = self._make(field_type="enum", allowed_values=("Igen", "Nem"))
        assert f.validate_value("NA")
        assert f.validate_value("ERVENYTELEN")
        assert f.validate_value("Unknown")
        assert f.validate_value("Not applicable")
        assert f.validate_value("Uncertain")
        assert f.validate_value(None)

    def test_validate_value_open_anything_valid(self):
        f = self._make(field_type="open")
        assert f.validate_value("any string")
        assert f.validate_value(42)

    def test_frozen(self):
        f = self._make()
        try:
            f.field_name = "other"  # type: ignore[misc]
            assert False, "Should have raised"
        except (AttributeError, TypeError):
            pass

    def test_form_section_hints_all_forms(self):
        for form_id in ("A", "B1", "Bmid", "Bdis"):
            assert form_id in FORM_SECTION_HINTS


# ── _parse_allowed_values ─────────────────────────────────────────────────────

class TestParseAllowedValues:
    def test_enum_set_takes_priority(self):
        sets = {"YES_NO": ["Igen", "Nem"]}
        result = _parse_allowed_values("[]", "YES_NO", sets)
        assert result == ["Igen", "Nem"]

    def test_json_fallback_when_no_enum_set(self):
        result = _parse_allowed_values('["Alpha", "Beta"]', None, {})
        assert result == ["Alpha", "Beta"]

    def test_json_fallback_when_enum_set_not_found(self):
        result = _parse_allowed_values('["Alpha", "Beta"]', "UNKNOWN_SET", {})
        assert result == ["Alpha", "Beta"]

    def test_empty_json_returns_empty(self):
        result = _parse_allowed_values("[]", None, {})
        assert result == []

    def test_none_returns_empty(self):
        result = _parse_allowed_values(None, None, {})
        assert result == []

    def test_malformed_json_returns_empty(self):
        result = _parse_allowed_values("not json", None, {})
        assert result == []


# ── Private utilities ─────────────────────────────────────────────────────────

class TestUtilities:
    def test_clean_identifier_strips_spaces(self):
        assert _clean_identifier("etiologia_egyeb_ megnevezese") == "etiologia_egyeb_megnevezese"

    def test_clean_identifier_strips_leading_trailing(self):
        assert _clean_identifier("  test_field  ") == "test_field"

    def test_clean_identifier_none(self):
        assert _clean_identifier(None) == ""

    def test_bool_truthy(self):
        assert _bool("1") is True
        assert _bool("true") is True
        assert _bool("True") is True
        assert _bool("yes") is True
        assert _bool(True) is True

    def test_bool_falsy(self):
        assert _bool("0") is False
        assert _bool("false") is False
        assert _bool(None) is False
        assert _bool(False) is False
        assert _bool("") is False

    def test_str_strips(self):
        assert _str("  hello  ") == "hello"

    def test_str_none_for_empty(self):
        assert _str("") is None
        assert _str(None) is None
        assert _str("   ") is None

    def test_normalise_operator_eq(self):
        assert _normalise_operator("==") == "=="
        assert _normalise_operator("=") == "=="
        assert _normalise_operator("eq") == "=="

    def test_normalise_operator_none(self):
        assert _normalise_operator(None) is None
        assert _normalise_operator("") is None


# ── _parse_main_rows ──────────────────────────────────────────────────────────

class TestParseMainRows:
    def test_basic_open_field(self):
        rows = [make_row()]
        fields = _parse_main_rows(rows, {})
        assert len(fields) == 1
        f = fields[0]
        assert f.field_name == "test_field"
        assert f.field_type == "open"
        assert f.allowed_values == ()

    def test_enum_field_with_enum_set(self):
        rows = [make_row(
            field_name="neme", field_type="enum",
            value_type="enum", enum_set="YES_NO",
        )]
        fields = _parse_main_rows(rows, {"YES_NO": ["Igen", "Nem"]})
        assert fields[0].allowed_values == ("Igen", "Nem")

    def test_enum_field_json_fallback(self):
        rows = [make_row(
            field_name="status", field_type="enum",
            value_type="enum", allowed_values='["Active", "Inactive"]',
        )]
        fields = _parse_main_rows(rows, {})
        assert fields[0].allowed_values == ("Active", "Inactive")

    def test_section_hint_derived_from_form_id(self):
        for form_id, expected in FORM_SECTION_HINTS.items():
            rows = [make_row(form_id=form_id, section_hint=None)]
            fields = _parse_main_rows(rows, {})
            assert fields[0].section_hint == expected, (
                f"form_id={form_id}: expected {expected!r}, got {fields[0].section_hint!r}"
            )

    def test_section_hint_explicit_overrides_derived(self):
        rows = [make_row(form_id="A", section_hint="Custom Section")]
        fields = _parse_main_rows(rows, {})
        assert fields[0].section_hint == "Custom Section"

    def test_field_base_space_stripped(self):
        rows = [make_row(
            field_base="etiologia_egyeb_ megnevezese",
            field_name="etiologia_egyeb_megnevezese",
        )]
        fields = _parse_main_rows(rows, {})
        assert " " not in fields[0].field_base

    def test_canonical_field(self):
        rows = [make_row(canonical="1", dedupe_key="patient.name")]
        fields = _parse_main_rows(rows, {})
        assert fields[0].canonical is True
        assert fields[0].dedupe_key == "patient.name"

    def test_conditional_field(self):
        rows = [make_row(
            condition_parent_field="majbiopszia_tortent_e",
            condition_operator="==",
            condition_value="Igen",
        )]
        fields = _parse_main_rows(rows, {})
        f = fields[0]
        assert f.condition_parent_field == "majbiopszia_tortent_e"
        assert f.condition_operator == "=="
        assert f.condition_value == "Igen"
        assert f.is_conditional

    def test_skips_rows_with_no_field_name(self):
        rows = [make_row(field_name=""), make_row(field_name="valid_field")]
        fields = _parse_main_rows(rows, {})
        assert len(fields) == 1
        assert fields[0].field_name == "valid_field"

    def test_multiple_rows(self):
        rows = [
            make_row(field_name="f1", form_id="A",    timepoint="anam"),
            make_row(field_name="f2", form_id="B1",   timepoint="adm"),
            make_row(field_name="f3", form_id="Bmid", timepoint="inhosp"),
            make_row(field_name="f4", form_id="Bdis", timepoint="dis"),
        ]
        fields = _parse_main_rows(rows, {})
        assert len(fields) == 4
        assert [f.timepoint for f in fields] == ["anam", "adm", "inhosp", "dis"]

    def test_missing_marker_kept_as_na(self):
        rows = [make_row(missing_marker="NA")]
        fields = _parse_main_rows(rows, {})
        assert fields[0].missing_marker == "NA"

    def test_not_applicable_marker(self):
        rows = [make_row(not_applicable_marker="ERVENYTELEN")]
        fields = _parse_main_rows(rows, {})
        assert fields[0].not_applicable_marker == "ERVENYTELEN"


# ── _validate ─────────────────────────────────────────────────────────────────

class TestValidation:
    def _make_field(self, **overrides) -> FieldDefinition:
        defaults = dict(
            field_name="f1", field_base="f1", column_header="F1",
            form_id="A", timepoint="anam", field_type="open",
            value_type="text", allowed_values=(), enum_set=None,
            source_policy="DISCHARGE_PRIMARY", section_hint="Anamnézis",
            routing=(), repeat_group=None, scope=None, canonical=False, dedupe_key=None,
            multiple_answers_allowed=False, missing_marker="NA",
            not_applicable_marker="ERVENYTELEN", condition_parent_field=None,
            condition_operator=None, condition_value=None,
            date_format_hint=None, date_granularity_allowed=None,
            list_storage_format=None,
            notes=None, unit=None,
        )
        defaults.update(overrides)
        return FieldDefinition(**defaults)

    def test_valid_registry_no_errors(self):
        fields = [self._make_field()]
        assert _validate(fields) == []

    def test_duplicate_field_name(self):
        f1 = self._make_field(field_name="dup")
        f2 = self._make_field(field_name="dup")
        errors = _validate([f1, f2])
        assert any("Duplicate" in e for e in errors)

    def test_space_in_field_name(self):
        f = self._make_field(field_name="bad field")
        errors = _validate([f])
        assert any("spaces" in e for e in errors)

    def test_unknown_condition_parent(self):
        """Unknown condition_parent_field is a WARNING, not a hard error."""
        parent = self._make_field(field_name="parent")
        child = self._make_field(
            field_name="child",
            condition_parent_field="nonexistent_field",
            condition_operator="==",
            condition_value="Igen",
        )
        # Hard validation should NOT flag this
        errors = _validate([parent, child])
        assert not any("nonexistent_field" in e for e in errors)
        # Soft check SHOULD flag it
        warnings = _check_warnings([parent, child])
        assert any("nonexistent_field" in w for w in warnings)

    def test_condition_value_not_in_parent_enum_is_flagged(self):
        """'Egyéb' never equals the parent's real answer 'Egyéb (megnevezni)'."""
        parent = self._make_field(
            field_name="parent", field_type="enum",
            allowed_values=("Furosemid", "Egyéb (megnevezni)"),
        )
        child = self._make_field(
            field_name="child", condition_parent_field="parent",
            condition_operator="==", condition_value="Egyéb",
        )
        assert any("condition_value" in w and "'child'" in w
                   for w in _check_warnings([parent, child]))
        flagged = unresolved_parent_warnings([parent, child])
        assert flagged["child"].startswith("REGISTRY_WARNING: condition_value")
        assert _validate([parent, child]) == []

    def test_condition_value_matching_parent_enum_not_flagged(self):
        parent = self._make_field(
            field_name="parent", field_type="enum",
            allowed_values=("Furosemid", "Egyéb (megnevezni)"),
        )
        child = self._make_field(
            field_name="child", condition_parent_field="parent",
            condition_operator="==", condition_value="Egyéb (megnevezni)",
        )
        assert _check_warnings([parent, child]) == []
        assert unresolved_parent_warnings([parent, child]) == {}

    def test_allowed_values_column_drift_is_flagged(self):
        rows = [{"field_name": "f", "enum_set": "S",
                 "allowed_values": '["Furosemid", "Egyéb"]'}]
        sets = {"S": ["Furosemid", "Egyéb (megnevezni)"]}
        warnings = _check_allowed_values_column(rows, sets)
        assert len(warnings) == 1
        assert "only in Main ['Egyéb']" in warnings[0]
        assert "only in Enum_sets ['Egyéb (megnevezni)']" in warnings[0]

    def test_allowed_values_column_order_ignored(self):
        rows = [{"field_name": "f", "enum_set": "S",
                 "allowed_values": '["Egyéb", "Furosemid"]'}]
        assert _check_allowed_values_column(rows, {"S": ["Furosemid", "Egyéb"]}) == []

    def test_missing_enum_set_is_flagged(self):
        rows = [{"field_name": "f", "enum_set": "NOPE", "allowed_values": '["Igen"]'}]
        warnings = _check_allowed_values_column(rows, {"S": ["Igen"]})
        assert len(warnings) == 1 and "'NOPE'" in warnings[0]

    def test_field_without_enum_set_not_checked(self):
        rows = [{"field_name": "f", "enum_set": None, "allowed_values": '["Igen"]'}]
        assert _check_allowed_values_column(rows, {}) == []

    def test_valid_condition_parent(self):
        parent = self._make_field(field_name="parent")
        child = self._make_field(
            field_name="child",
            condition_parent_field="parent",
            condition_operator="==",
            condition_value="Igen",
        )
        errors = _validate([parent, child])
        assert not any("nonexistent" in e for e in errors)

    def test_invalid_timepoint(self):
        f = self._make_field(timepoint="invalid_tp")
        errors = _validate([f])
        assert any("timepoint" in e for e in errors)

    def test_invalid_source_policy(self):
        f = self._make_field(source_policy="UNKNOWN_POLICY")
        errors = _validate([f])
        assert any("source_policy" in e for e in errors)

    def test_enum_field_without_allowed_values(self):
        f = self._make_field(field_type="enum", allowed_values=())
        errors = _validate([f])
        assert any("no allowed_values" in e for e in errors)

    def test_multiple_errors_all_reported(self):
        f1 = self._make_field(field_name="dup")
        f2 = self._make_field(field_name="dup")
        f3 = self._make_field(timepoint="bad_tp")
        errors = _validate([f1, f2, f3])
        assert len(errors) >= 2


# ── Query helpers ─────────────────────────────────────────────────────────────

class TestQueryHelpers:
    def _fields(self) -> list[FieldDefinition]:
        base = dict(
            field_base="x", column_header="X", field_type="open",
            value_type="text", allowed_values=(), enum_set=None,
            source_policy="DISCHARGE_PRIMARY", routing=(), repeat_group=None, scope=None,
            canonical=False, dedupe_key=None, multiple_answers_allowed=False,
            missing_marker="NA", not_applicable_marker="ERVENYTELEN",
            condition_parent_field=None, condition_operator=None,
            condition_value=None,
            date_format_hint=None, date_granularity_allowed=None,
            list_storage_format=None,
            notes=None, unit=None,
        )
        return [
            FieldDefinition(field_name="f1", form_id="A",    timepoint="anam",  section_hint="Anamnézis",       **base),
            FieldDefinition(field_name="f2", form_id="B1",   timepoint="adm",   section_hint="Felvételi státus",**base),
            FieldDefinition(field_name="f3", form_id="Bmid", timepoint="inhosp",section_hint="Vizsgálatok",     **base),
            FieldDefinition(field_name="f4", form_id="A",    timepoint="anam",  section_hint="Anamnézis",
                            canonical=True, dedupe_key="patient.name",          **{k:v for k,v in base.items() if k not in ("canonical","dedupe_key")}),
            FieldDefinition(field_name="f5", form_id="A",    timepoint="anam",  section_hint="Anamnézis",
                            condition_parent_field="f1", condition_operator="==", condition_value="Igen",
                            **{k:v for k,v in base.items() if k not in ("condition_parent_field","condition_operator","condition_value")}),
        ]

    def test_fields_by_section(self):
        groups = fields_by_section(self._fields())
        assert "Anamnézis" in groups
        assert "Felvételi státus" in groups
        assert len(groups["Anamnézis"]) == 3

    def test_fields_by_form(self):
        groups = fields_by_form(self._fields())
        assert "A" in groups
        assert "B1" in groups
        assert len(groups["A"]) == 3

    def test_get_field_found(self):
        f = get_field(self._fields(), "f2")
        assert f is not None
        assert f.field_name == "f2"

    def test_get_field_not_found(self):
        assert get_field(self._fields(), "nonexistent") is None

    def test_canonical_fields(self):
        canons = canonical_fields(self._fields())
        assert len(canons) == 1
        assert canons[0].field_name == "f4"

    def test_conditional_fields(self):
        conds = conditional_fields(self._fields())
        assert len(conds) == 1
        assert conds[0].field_name == "f5"


# ── Integration test against real Registry_base.xlsx (no openpyxl needed) ────

class TestRealRegistry:
    """
    Reads the actual Registry_base.xlsx using stdlib XML parsing and runs
    it through _parse_main_rows + _validate.  No openpyxl required.
    """
    REGISTRY_PATH = "config/Registry_base.xlsx"

    def _load_with_raw_xml(self):
        """Load registry using stdlib zipfile + xml.etree (no openpyxl).

        Handles both shared-string xlsx files (xl/sharedStrings.xml present)
        and inline-string xlsx files (sharedStrings.xml absent, t=inlineStr).
        Registry_base.xlsx uses inline strings exclusively, so sharedStrings.xml
        is absent — the previous version crashed here for every TestRealRegistry test.
        """
        import zipfile, xml.etree.ElementTree as ET, re

        path = self.REGISTRY_PATH
        if not __import__('os').path.exists(path):
            return None, None

        ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'

        with zipfile.ZipFile(path) as z:
            # sharedStrings.xml is optional — xlsx may use inlineStr instead
            if 'xl/sharedStrings.xml' in z.namelist():
                ss_root = ET.fromstring(z.read('xl/sharedStrings.xml'))
                strings = [
                    ''.join(t.text or '' for t in si.iter(ns + 't'))
                    for si in ss_root.findall('.//' + ns + 'si')
                ]
            else:
                strings = []

            ws1_xml = z.read('xl/worksheets/sheet1.xml')
            ws2_xml = z.read('xl/worksheets/sheet2.xml')

        def col_idx(col):
            idx = 0
            for ch in col:
                idx = idx * 26 + (ord(ch.upper()) - ord('A') + 1)
            return idx - 1

        def read_sheet(xml_bytes):
            root = ET.fromstring(xml_bytes)
            rows_el = root.findall('.//' + ns + 'row')
            result = []
            for row_el in rows_el:
                row_dict = {}
                for cell in row_el.findall(ns + 'c'):
                    ref = cell.get('r', '')
                    m = re.match(r'([A-Z]+)', ref)
                    if not m:
                        continue
                    ci = col_idx(m.group(1))
                    t = cell.get('t', '')
                    if t == 'inlineStr':
                        # Inline string: value is in <is><t>…</t></is>
                        is_el = cell.find(ns + 'is')
                        val = ''.join(
                            te.text or '' for te in is_el.iter(ns + 't')
                        ) if is_el is not None else None
                    elif t == 's':
                        # Shared string index into strings[]
                        v_el = cell.find(ns + 'v')
                        val = strings[int(v_el.text)] if (v_el is not None and v_el.text is not None) else None
                    else:
                        # Numeric, boolean, or plain value
                        v_el = cell.find(ns + 'v')
                        val = v_el.text if v_el is not None else None
                    row_dict[ci] = val
                if row_dict:
                    result.append(row_dict)
            return result

        main_rows = read_sheet(ws1_xml)
        headers = dict(main_rows[0].items())

        def to_dict(row):
            return {headers[idx]: val for idx, val in row.items()}

        data_dicts = [to_dict(r) for r in main_rows[1:]]

        # Enum sets
        enum_rows = read_sheet(ws2_xml)
        enum_headers = dict(enum_rows[0].items())
        enum_data = [{enum_headers[idx]: val for idx, val in r.items()}
                     for r in enum_rows[1:]]

        grouped: dict[str, list] = {}
        for row in enum_data:
            name = row.get('enum_set') or ''
            label = row.get('label') or ''
            order = row.get('order') or '0'
            if not name or not label:
                continue
            try:
                o = int(float(order))
            except (ValueError, TypeError):
                o = 0
            grouped.setdefault(name, []).append((o, label))
        enum_sets = {
            k: [lbl for _, lbl in sorted(v, key=lambda x: x[0])]
            for k, v in grouped.items()
        }

        return data_dicts, enum_sets

    def test_real_registry_loads_558_fields(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            print("SKIP: Registry_base.xlsx not found at", self.REGISTRY_PATH)
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        assert len(fields) == 558, f"Expected 558 fields, got {len(fields)}"

    def test_real_registry_passes_hard_validation(self):
        """Hard validation (duplicate names, spaces, bad timepoints) passes."""
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        errors = _validate(fields)
        assert errors == [], f"Hard validation errors:\n" + "\n".join(errors)

    def test_real_registry_soft_warnings_within_known_budget(self):
        """
        Soft warnings come from manual registry edits that leave
        condition_parent_field references pointing at names that no longer
        resolve (typical after a rename). These warnings are not fatal — the
        rules engine silently skips each affected rule and the wide-table
        export surfaces a REGISTRY_WARNING marker for the maintainer.

        The current registry has a non-zero but bounded number of such
        unresolved references. This test guards against significant
        regressions (e.g. a bulk rename breaking many rows at once) without
        blocking ongoing small edits.
        """
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        warnings = _check_warnings(fields)
        assert len(warnings) <= 100, (
            f"Registry has {len(warnings)} soft warning(s) — above the "
            f"100-warning budget. Investigate condition_parent_field drift.\n"
            + "\n".join(warnings[:10])
        )

    def test_real_registry_no_duplicate_field_names(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        names = [f.field_name for f in fields]
        assert len(names) == len(set(names)), "Duplicate field_names found"

    def test_real_registry_no_spaces_in_field_names(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        bad = [f.field_name for f in fields if " " in f.field_name]
        assert bad == [], f"field_names with spaces: {bad}"

    def test_real_registry_section_hints_all_populated(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        empty = [f.field_name for f in fields if not f.section_hint]
        assert empty == [], f"Fields with empty section_hint: {empty[:5]}"

    def test_real_registry_canonical_fields(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        canons = canonical_fields(fields)
        # Expect at least the 3 patient identity fields
        assert len(canons) >= 3, f"Expected ≥3 canonical fields, got {len(canons)}"

    def test_real_registry_conditional_fields_warnings_not_errors(self):
        """
        condition_parent issues must remain *soft* (warning, not hard error)
        even though the current registry resolves cleanly.  This guards the
        contract that future data drift never blocks startup.
        """
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        # Hard errors must not include condition_parent issues
        errors = _validate(fields)
        parent_errors = [e for e in errors if "condition_parent_field" in e]
        assert parent_errors == [], (
            "condition_parent issues should be warnings not hard errors:\n"
            + "\n".join(parent_errors)
        )
        # Inject a deliberately unresolvable parent and confirm it surfaces
        # only as a soft warning (not as a hard validation error).
        from dataclasses import replace
        broken = list(fields)
        broken.append(replace(
            fields[0],
            field_name="__test_broken_child__",
            condition_parent_field="__nonexistent_parent__",
            condition_operator="==",
            condition_value="X",
        ))
        injected_warnings = _check_warnings(broken)
        assert any("__nonexistent_parent__" in w for w in injected_warnings), (
            "Soft warning system must detect injected unresolved parent"
        )
        # And the same broken set must NOT raise hard errors for that case.
        injected_errors = _validate(broken)
        injected_parent_errors = [
            e for e in injected_errors if "condition_parent_field" in e
        ]
        assert injected_parent_errors == [], (
            "Injected unresolved parent should be soft, not hard"
        )

    def test_real_registry_condition_values_match_parent_answers(self):
        """Every condition_value is spelled exactly like a parent answer."""
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return
        fields = _parse_main_rows(data_dicts, enum_sets)
        assert unresolved_parent_warnings(fields) == {}

    def test_real_registry_allowed_values_column_matches_enum_sets(self):
        """The Main allowed_values column and Enum_sets hold the same values."""
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return
        drift = _check_allowed_values_column(data_dicts, enum_sets)
        assert drift == [], "\n".join(drift)

    def test_hard_coded_parser_outputs_match_registry(self):
        """
        Every literal (field_name, value) a deterministic parser writes must
        exist in the registry, and enum values must be allowed values.
        Catches registry renames/relabels that silently orphan parser output
        (e.g. 'vernyomas_dyastole_hgmm_adm', dm_tipusa '1-es típusú').
        Dynamically named keys (labs, events) are built at runtime and are
        not literals, so they are not checked here.
        """
        import ast
        from pathlib import Path
        from app.pipeline.step5_extract import gender, hard_structured

        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return
        by_name = {f.field_name: f for f in _parse_main_rows(data_dicts, enum_sets)}

        def literal(node):
            return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

        emitted: list[tuple[str, str | None, str]] = []
        for src in ("app/pipeline/step5_extract/hard_scores.py",
                    "app/pipeline/step5_extract/hard_structured.py",
                    "app/pipeline/orchestrator.py"):
            for node in ast.walk(ast.parse(Path(src).read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call):
                    continue
                fn = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if fn in ("_make_field", "_make_viro_field", "_make_bno_field") and node.args:
                    key = literal(node.args[0])
                    value = literal(node.args[1]) if len(node.args) > 1 else None
                    if key:
                        emitted.append((key, value, src))
                elif fn == "ExtractedField":
                    kw = {k.arg: k.value for k in node.keywords}
                    key = literal(kw.get("key"))
                    if key:
                        emitted.append((key, literal(kw.get("value")), src))
        for prefix, entries in hard_structured._BNO_FIELD_MAP.items():
            for key, value, _ in entries:
                emitted.append((key, value, f"BNO {prefix}"))
        for value in gender._VALID_GENDERS:
            emitted.append(("a_beteg_neme", value, "gender"))

        assert len(emitted) > 30, "AST scan found too few parser outputs — scan is broken"
        problems = []
        for key, value, src in emitted:
            fd = by_name.get(key)
            if fd is None:
                problems.append(f"{src}: field {key!r} not in registry")
            elif value is not None and fd.allowed_values and value not in fd.allowed_values:
                problems.append(f"{src}: {key} = {value!r} not in {list(fd.allowed_values)}")
        assert problems == [], "\n".join(problems)

    def test_real_registry_all_timepoints_present(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        timepoints = {f.timepoint for f in fields}
        assert "anam" in timepoints
        assert "adm" in timepoints
        assert "inhosp" in timepoints
        assert "dis" in timepoints

    def test_real_registry_enum_fields_have_values(self):
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        bad = [f.field_name for f in fields
               if f.field_type == "enum" and not f.allowed_values]
        assert bad == [], f"Enum fields with no allowed_values: {bad[:10]}"

    def test_real_registry_section_hint_distribution(self):
        """Every expected section must be reachable from at least one field.

        section_hint may now be pipe-joined (e.g. "Epikrízis|Vizsgálatok
        eredményei") to scope a field across multiple discharge sections, so
        group keys are not always equal to a single section name. Check each
        expected section as a substring of a group key instead.
        """
        data_dicts, enum_sets = self._load_with_raw_xml()
        if data_dicts is None:
            return

        fields = _parse_main_rows(data_dicts, enum_sets)
        groups = fields_by_section(fields)
        expected_sections = set(FORM_SECTION_HINTS.values())
        for section in expected_sections:
            matching = [
                (key, members) for key, members in groups.items()
                if section in (key or "").split("|")
            ]
            assert matching, (
                f"Section missing from any section_hint: {section!r}. "
                f"Available keys: {sorted(groups)}"
            )
            assert sum(len(m) for _, m in matching) > 0


# ── Run directly ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Allow running as: python tests/unit/test_registry.py
    import traceback

    all_classes = [
        TestFieldDefinition, TestParseAllowedValues, TestUtilities,
        TestParseMainRows, TestValidation, TestQueryHelpers, TestRealRegistry,
    ]

    passed = failed = 0
    for cls in all_classes:
        instance = cls()
        methods = [m for m in dir(cls) if m.startswith("test_")]
        for method_name in methods:
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL  {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR {cls.__name__}.{method_name}: {e}")
                traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  PASSED: {passed}/{passed+failed}")
    if failed:
        print(f"  FAILED: {failed}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
