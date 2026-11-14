"""
tests/unit/test_export.py

Unit tests for app/pipeline/step8_export.py

Run with:
    PYTHONPATH=. \
        python3 tests/unit/test_export.py
"""
from __future__ import annotations

import sys
import tempfile
import traceback
from datetime import date
from pathlib import Path
from typing import Any

_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT))

from app.domain.enums import DocType, FieldSource, WindowType
from app.domain.models import (
    CaseResult,
    ConflictRecord,
    ExtractedField,
    MismatchRecord,
    OutOfWindowRecord,
)
from app.pipeline.step8_export import (
    MAX_COL_LEN,
    _csv_cell,
    _lab_sort_key,
    _event_sort_key,
    build_col_order,
    build_safe_cols,
    export,
    sanitise_col,
)
from app.registry.models import FieldDefinition

# ── Runner ────────────────────────────────────────────────────────────────────

_passed: list[str] = []
_failed: list[str] = []


def run(name: str, fn):
    try:
        fn()
        _passed.append(name)
        print(f"  ✓  {name}")
    except Exception:
        _failed.append(name)
        print(f"  ✗  {name}")
        traceback.print_exc()


def assert_eq(a, b, msg=""):
    if a != b:
        raise AssertionError(f"{msg}\n  expected: {b!r}\n  actual:   {a!r}")


def assert_true(c, msg=""):
    if not c:
        raise AssertionError(msg or "assertion failed")


def assert_false(c, msg=""):
    if c:
        raise AssertionError(msg or "expected False")


def assert_in(item, container, msg=""):
    if item not in container:
        raise AssertionError(f"{msg}\n  {item!r} not found in container")


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _ef(key: str, value: Any, source=FieldSource.HARD, confidence=0.95) -> ExtractedField:
    return ExtractedField(
        key=key, value=value, source=source,
        doc_type=DocType.DISCHARGE_SUMMARY,
        confidence=confidence, engine="test",
    )


def _make_fd(
    name: str,
    header: str,
    form_id: str = "A",
    timepoint: str = "anam",
    canonical: bool = False,
) -> FieldDefinition:
    return FieldDefinition(
        field_name=name,
        field_base=name,
        column_header=header,
        form_id=form_id,
        timepoint=timepoint,
        field_type="open",
        value_type="text",
        allowed_values=(),
        enum_set=None,
        source_policy="DISCHARGE_ONLY",
        section_hint="Test",
        routing=(),
        repeat_group=None,
        scope=None,
        canonical=canonical,
        dedupe_key=None,
        multiple_answers_allowed=False,
        missing_marker="NA",
        not_applicable_marker="ERVENYTELEN",
        condition_parent_field=None,
        condition_operator=None,
        condition_value=None,
        date_format_hint=None,
        date_granularity_allowed=None,
        list_storage_format=None,
        notes=None,
        unit=None,
    )


def _tiny_registry() -> list[FieldDefinition]:
    return [
        _make_fd("a_beteg_neve", "Beteg neve", "A", "anam", canonical=True),
        _make_fd("taj_szam", "TAJ szám", "A", "anam", canonical=True),
        _make_fd("alkohol", "Alkohol", "A", "anam"),
        _make_fd("child_pugh_adm", "Child-Pugh felvételkor", "B1", "adm"),
        _make_fd("meld_adm", "MELD felvételkor", "B1", "adm"),
        _make_fd("ascites_inhosp", "Ascites bennfekvés", "Bmid", "inhosp"),
        _make_fd("elbocsatas_tipusa_dis", "Elbocsátás típusa", "Bdis", "dis"),
    ]


def _make_result(
    case_id: str = "case1",
    fields: dict[str, Any] | None = None,
    conflicts=None,
    mismatches=None,
    out_of_window=None,
) -> CaseResult:
    result = CaseResult(case_id=case_id)
    for k, v in (fields or {}).items():
        result.set_field(k, _ef(k, v))
    result.conflicts   = conflicts or []
    result.mismatches  = mismatches or []
    result.out_of_window = out_of_window or []
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: sanitise_col
# ═══════════════════════════════════════════════════════════════════════════════

def test_sanitise_negative_day():
    assert_eq(sanitise_col("creatinine_d-1"), "creatinine_dm1")
    assert_eq(sanitise_col("wbc_d-12"), "wbc_dm12")


def test_sanitise_positive_day_unchanged():
    assert_eq(sanitise_col("creatinine_d1"), "creatinine_d1")
    assert_eq(sanitise_col("inr_d2_1"), "inr_d2_1")


def test_sanitise_strips_special_chars():
    assert_eq(sanitise_col("field-name"), "field_name")
    assert_eq(sanitise_col("field name"), "field_name")


def test_sanitise_truncates_to_max_len():
    long = "a" * 100
    result = sanitise_col(long)
    assert_true(len(result) <= MAX_COL_LEN, f"expected ≤{MAX_COL_LEN}, got {len(result)}")


def test_sanitise_prefix_digit():
    assert_eq(sanitise_col("123field"), "v_123field"[:MAX_COL_LEN])


def test_sanitise_normal_snake_case_unchanged():
    assert_eq(sanitise_col("hepatitis_c_virus"), "hepatitis_c_virus")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: build_safe_cols deduplication
# ═══════════════════════════════════════════════════════════════════════════════

def test_build_safe_cols_no_collision():
    mapping = build_safe_cols(["alpha", "beta", "gamma"])
    assert_eq(mapping["alpha"], "alpha")
    assert_eq(mapping["beta"],  "beta")
    assert_eq(mapping["gamma"], "gamma")


def test_build_safe_cols_collision_resolved():
    """Two keys that truncate to the same name get a hash suffix on the second."""
    # Use two different raw keys that truncate identically (62 'x's then differing tail)
    a = "x" * 62 + "a"
    b = "x" * 62 + "b"
    mapping = build_safe_cols([a, b])
    vals = list(mapping.values())
    assert_eq(len(set(vals)), 2, "both must get unique safe names")
    for v in vals:
        assert_true(len(v) <= MAX_COL_LEN + 5, f"name too long: {v!r}")


def test_build_safe_cols_all_names_within_limit():
    keys = [f"field_{i}" for i in range(20)]
    mapping = build_safe_cols(keys)
    for raw, safe in mapping.items():
        assert_true(len(safe) <= MAX_COL_LEN + 5, f"{raw!r} → {safe!r} is too long")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: sort keys
# ═══════════════════════════════════════════════════════════════════════════════

def test_lab_sort_key_day_first():
    k1 = _lab_sort_key("creatinine_d1")
    k2 = _lab_sort_key("creatinine_d2")
    assert_true(k1 < k2, "day 1 before day 2")


def test_lab_sort_key_analyte_second():
    k1 = _lab_sort_key("alt_d1")
    k2 = _lab_sort_key("wbc_d1")
    assert_true(k1 < k2, "analyte alphabetical within same day")


def test_lab_sort_key_occurrence_third():
    k1 = _lab_sort_key("creatinine_d2")
    k2 = _lab_sort_key("creatinine_d2_1")
    k3 = _lab_sort_key("creatinine_d2_2")
    assert_true(k1 < k2 < k3)


def test_event_sort_key_prefix_first():
    k1 = _event_sort_key("ct_d3_1_date")
    k2 = _event_sort_key("fibro_d3_1_date")
    assert_true(k1 < k2, "ct before fibro alphabetically")


def test_event_sort_key_day_second():
    k1 = _event_sort_key("endo_gastro_d2_1_date")
    k2 = _event_sort_key("endo_gastro_d5_1_date")
    assert_true(k1 < k2)


def test_event_sort_key_index_third():
    k1 = _event_sort_key("fibro_d5_1_e_kpa")
    k2 = _event_sort_key("fibro_d5_2_e_kpa")
    assert_true(k1 < k2)


def test_event_sort_key_mri_recognised():
    """MRI columns must be recognised by _EVENT_RE and sortable."""
    k = _event_sort_key("mri_d4_1_date")
    assert_eq(k, ("mri", 4, 1, "date"), "MRI event sort key")


def test_event_sort_key_mri_sorts_with_others():
    """MRI sorts between micro and rtg alphabetically."""
    k_mri = _event_sort_key("mri_d4_1_date")
    k_rtg = _event_sort_key("rtg_d4_1_date")
    k_micro = _event_sort_key("micro_d4_1_pathogen")
    # micro < mri < rtg (alphabetical prefix sort)
    assert_true(k_micro < k_mri, "micro < mri")
    assert_true(k_mri < k_rtg, "mri < rtg")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: build_col_order
# ═══════════════════════════════════════════════════════════════════════════════

def test_col_order_canonical_first():
    reg = _tiny_registry()
    fields = {"a_beteg_neve": _ef("a_beteg_neve", "X"), "taj_szam": _ef("taj_szam", "Y"),
               "alkohol": _ef("alkohol", "Z")}
    order = build_col_order(fields, reg)
    assert_eq(order.index("a_beteg_neve"), 0)
    assert_eq(order.index("taj_szam"), 1)


def test_col_order_timepoints_after_canonical():
    reg = _tiny_registry()
    fields = {
        "a_beteg_neve": _ef("a_beteg_neve", "X"),
        "alkohol":      _ef("alkohol", "Z"),
        "child_pugh_adm": _ef("child_pugh_adm", "C"),
        "ascites_inhosp": _ef("ascites_inhosp", "A"),
        "elbocsatas_tipusa_dis": _ef("elbocsatas_tipusa_dis", "D"),
    }
    order = build_col_order(fields, reg)
    # anam (alkohol) before adm before inhosp before dis
    assert_true(order.index("alkohol") < order.index("child_pugh_adm"))
    assert_true(order.index("child_pugh_adm") < order.index("ascites_inhosp"))
    assert_true(order.index("ascites_inhosp") < order.index("elbocsatas_tipusa_dis"))


def test_col_order_lab_columns_after_registry():
    reg = _tiny_registry()
    fields = {
        "a_beteg_neve": _ef("a_beteg_neve", "X"),
        "creatinine_d1": _ef("creatinine_d1", 108.0),
        "wbc_d5": _ef("wbc_d5", 5.87),
    }
    order = build_col_order(fields, reg)
    assert_true(order.index("a_beteg_neve") < order.index("creatinine_d1"))
    assert_true(order.index("creatinine_d1") < order.index("wbc_d5"),
                "d1 before d5")


def test_col_order_event_columns_after_lab():
    reg = _tiny_registry()
    fields = {
        "creatinine_d1": _ef("creatinine_d1", 108.0),
        "endo_gastro_d2_1_date": _ef("endo_gastro_d2_1_date", "2026-01-13"),
        "fibro_d5_1_e_kpa": _ef("fibro_d5_1_e_kpa", 75.0),
    }
    order = build_col_order(fields, reg)
    assert_true(order.index("creatinine_d1") < order.index("endo_gastro_d2_1_date"))
    assert_true(order.index("endo_gastro_d2_1_date") < order.index("fibro_d5_1_e_kpa"),
                "endo before fibro alphabetically")


def test_col_order_all_keys_present():
    reg = _tiny_registry()
    all_keys = ["a_beteg_neve", "creatinine_d1", "endo_gastro_d2_1_date", "unknown_key"]
    fields = {k: _ef(k, "x") for k in all_keys}
    order = build_col_order(fields, reg)
    assert_eq(set(order), set(all_keys), "all keys must appear exactly once")
    assert_eq(len(order), len(all_keys), "no duplicates")


def test_col_order_lab_day_sort_correct():
    """creatinine_d1 < creatinine_d2 < creatinine_d2_1."""
    reg = _tiny_registry()
    fields = {
        "creatinine_d2_1": _ef("creatinine_d2_1", 1.0),
        "creatinine_d1":   _ef("creatinine_d1",   1.0),
        "creatinine_d2":   _ef("creatinine_d2",   1.0),
    }
    order = build_col_order(fields, reg)
    lab = [k for k in order if "creatinine" in k]
    assert_eq(lab, ["creatinine_d1", "creatinine_d2", "creatinine_d2_1"])


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: CSV helper
# ═══════════════════════════════════════════════════════════════════════════════

def test_csv_cell_plain():
    assert_eq(_csv_cell("hello"), "hello")


def test_csv_cell_comma_quoted():
    assert_eq(_csv_cell("a,b"), '"a,b"')


def test_csv_cell_quote_escaped():
    assert_eq(_csv_cell('say "hi"'), '"say ""hi"""')


def test_csv_cell_none_empty():
    assert_eq(_csv_cell(None), "")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: export() integration — file creation and content
# ═══════════════════════════════════════════════════════════════════════════════

def test_export_creates_eight_files():
    reg = _tiny_registry()
    result = _make_result(
        case_id="test001",
        fields={"a_beteg_neve": "Beteg Neve", "creatinine_d1": 108.0},
    )
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        paths = export(result, out_dir, reg)
        assert_eq(len(paths), 8, "should return exactly 8 file paths")
        for p in paths:
            assert_true(p.exists(), f"file must exist: {p.name}")
        names = [p.name for p in paths]
        assert_true(any("wide_table.xlsx" in n for n in names), "wide_table.xlsx missing")
        assert_true(any("wide_table_labels.xlsx" in n for n in names), "labels missing")
        assert_true(any("wide_table.csv" in n for n in names), "csv missing")
        assert_true(any("conflicts.xlsx" in n for n in names), "conflicts missing")
        assert_true(any("mismatch.xlsx" in n for n in names), "mismatch missing")
        assert_true(any("history_followup.xlsx" in n for n in names), "history_followup missing")
        assert_true(any("unit_mismatches.xlsx" in n for n in names),
                    "unit_mismatches.xlsx missing")
        assert_true(any("ancillary_docs.xlsx" in n for n in names),
                    "ancillary_docs.xlsx missing")


def test_export_wide_table_column_count():
    """Wide table xlsx must have the correct number of columns."""
    import openpyxl
    reg = _tiny_registry()
    fields = {"a_beteg_neve": "X", "taj_szam": "Y", "creatinine_d1": 108.0, "wbc_d5": 5.87}
    result = _make_result("case1", fields=fields)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        ws = wb.active
        assert_eq(ws.max_column, len(fields),
                  f"expected {len(fields)} columns, got {ws.max_column}")


def test_export_wide_table_uncertain_cell_is_yellow():
    """Cells with value 'Uncertain' must have a yellow fill."""
    import openpyxl
    reg = _tiny_registry()
    result = _make_result("case1", fields={"alkohol": "Uncertain", "a_beteg_neve": "X"})
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        ws = wb.active
        # Find the 'Uncertain' cell in row 2
        yellow_cells = [
            c for c in ws[2]
            if c.fill and c.fill.fill_type == "solid"
            and c.fill.fgColor and c.fill.fgColor.rgb.endswith("FFFF00")
        ]
        assert_true(len(yellow_cells) >= 1, "at least one yellow cell expected")
        yellow_vals = [c.value for c in yellow_cells]
        assert_in("Uncertain", yellow_vals, "yellow cell must contain 'Uncertain'")


def test_export_non_uncertain_cell_not_yellow():
    """Normal cells must not be yellow."""
    import openpyxl
    reg = _tiny_registry()
    result = _make_result("case1", fields={"a_beteg_neve": "Beteg Neve"})
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        ws = wb.active
        yellow = [
            c for c in ws[2]
            if c.fill and c.fill.fill_type == "solid"
            and c.fill.fgColor and c.fill.fgColor.rgb.endswith("FFFF00")
        ]
        assert_eq(len(yellow), 0, "no yellow cells for non-Uncertain values")


def test_export_column_names_no_special_chars():
    """All column names in wide_table.xlsx must be safe (no '-')."""
    import openpyxl
    reg = _tiny_registry()
    # Include a negative-day lab column
    result = _make_result("case1", fields={
        "creatinine_d-1": 95.0,
        "creatinine_d1": 108.0,
    })
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        ws = wb.active
        headers = [ws.cell(row=1, column=i).value for i in range(1, ws.max_column + 1)]
        for h in headers:
            assert_true(h and "-" not in h, f"header contains '-': {h!r}")
        # Check negative day was sanitised
        assert_in("creatinine_dm1", headers, "negative day must become dm1")


def test_export_column_names_max_length():
    """No column name in the output may exceed MAX_COL_LEN chars."""
    import openpyxl
    reg = _tiny_registry()
    long_key = "a" * 80
    result = _make_result("case1", fields={long_key: "value"})
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        ws = wb.active
        headers = [ws.cell(row=1, column=i).value for i in range(1, ws.max_column + 1)]
        for h in headers:
            assert_true(len(h) <= MAX_COL_LEN + 5,
                        f"column name too long: {h!r} ({len(h)} chars)")


def test_export_csv_has_bom():
    """CSV file must start with UTF-8 BOM."""
    reg = _tiny_registry()
    result = _make_result("case1", fields={"a_beteg_neve": "Beteg Neve"})
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        csv_bytes = (out_dir / "case1" / "case1_wide_table.csv").read_bytes()
        assert_true(csv_bytes.startswith(b"\xef\xbb\xbf"),
                    "CSV must start with UTF-8 BOM (ef bb bf)")


def test_export_csv_column_count_matches_xlsx():
    """CSV header must have the same number of columns as the xlsx."""
    import openpyxl
    reg = _tiny_registry()
    fields = {"a_beteg_neve": "X", "creatinine_d1": 108.0, "wbc_d2": 5.87}
    result = _make_result("case1", fields=fields)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        csv_text = (out_dir / "case1" / "case1_wide_table.csv").read_text(encoding="utf-8-sig")
        csv_cols = len(csv_text.splitlines()[0].split(","))
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        xlsx_cols = wb.active.max_column
        assert_eq(csv_cols, xlsx_cols, "CSV and XLSX must have same column count")


def test_export_csv_hungarian_format():
    """csv_format="hu": ";" separator and "," decimals, also for numbers
    stored as text; other text is unchanged."""
    reg = _tiny_registry()
    fields = {"a_beteg_neve": "Kovács, Anna", "meld_adm": "98.9",
              "glucose_d-1": 6.4, "wbc_d2": 5.87, "ferritin_d1": ">2000.0"}
    result = _make_result("case1", fields=fields)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg, csv_format="hu")
        lines = (out_dir / "case1" / "case1_wide_table.csv").read_text(
            encoding="utf-8-sig").splitlines()
        row = dict(zip(lines[0].split(";"), lines[1].split(";")))
        assert_eq(row["glucose_dm1"], "6,4")
        assert_eq(row["wbc_d2"], "5,87")
        assert_eq(row["meld_adm"], "98,9")
        assert_eq(row["ferritin_d1"], ">2000.0")
        assert_eq(row["a_beteg_neve"], "Kovács, Anna")


def test_export_csv_default_format_unchanged():
    reg = _tiny_registry()
    result = _make_result("case1", fields={"glucose_d-1": 6.4})
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        lines = (out_dir / "case1" / "case1_wide_table.csv").read_text(
            encoding="utf-8-sig").splitlines()
        row = dict(zip(lines[0].split(","), lines[1].split(",")))
        assert_eq(row["glucose_dm1"], "6.4")


def test_export_unknown_csv_format_rejected():
    reg = _tiny_registry()
    result = _make_result("case1", fields={"a_beteg_neve": "X"})
    with tempfile.TemporaryDirectory() as tmp:
        try:
            export(result, Path(tmp), reg, csv_format="xls")
        except ValueError:
            return
        raise AssertionError("csv_format='xls' must raise ValueError")


def test_export_text_starting_with_equals_is_not_a_formula():
    """openpyxl turns "=…" into a formula; Excel then reports the file as
    damaged. Every workbook must store such values as text."""
    import openpyxl
    reg = _tiny_registry()
    result = _make_result("case1", fields={"ascites_inhosp": "= 5 mm",
                                           "uh_d1_1_result": "=(Mintavétel: )"})
    result.conflicts = [ConflictRecord(
        case_id="case1", field="ascites_inhosp", source_policy="DISCHARGE_PRIMARY",
        retained_value="= 5 mm", retained_doc_type=DocType.DISCHARGE_SUMMARY,
        overridden_value="=3 cm", overridden_doc_type=DocType.IMAGING, page=None,
    )]
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        for name in ("case1_wide_table.xlsx", "case1_conflicts.xlsx"):
            wb = openpyxl.load_workbook(str(out_dir / "case1" / name))
            cells = [c for row in wb.active.iter_rows() for c in row
                     if isinstance(c.value, str) and c.value.startswith("=")]
            assert_true(cells, f"{name}: test values missing")
            for c in cells:
                assert_eq(c.data_type, "s", f"{name} {c.coordinate} {c.value!r}")


def test_export_conflicts_xlsx_has_correct_rows():
    """conflicts.xlsx must have header + one row per conflict."""
    import openpyxl
    reg = _tiny_registry()
    result = _make_result("case1", fields={"a_beteg_neve": "X"})
    result.conflicts = [
        ConflictRecord(
            case_id="case1", field="creatinine_d1",
            source_policy="DISCHARGE_PRIMARY",
            retained_value=108.0, retained_doc_type=DocType.DISCHARGE_SUMMARY,
            overridden_value=115.0, overridden_doc_type=DocType.LAB_REPORT,
            page=1,
        ),
        ConflictRecord(
            case_id="case1", field="inr_d1",
            source_policy="DISCHARGE_PRIMARY",
            retained_value=1.62, retained_doc_type=DocType.DISCHARGE_SUMMARY,
            overridden_value=1.70, overridden_doc_type=DocType.LAB_REPORT,
            page=None,
        ),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_conflicts.xlsx"))
        ws = wb.active
        assert_eq(ws.max_row, 3, "header + 2 conflict rows")
        assert_eq(ws.cell(2, 2).value, "creatinine_d1")      # field name
        assert_eq(ws.cell(2, 3).value, "DISCHARGE_PRIMARY")  # source_policy
        assert_eq(ws.cell(2, 4).value, 108.0)                # retained_value
        assert_eq(ws.cell(2, 5).value, "DISCHARGE_SUMMARY")  # retained_doc_type


def test_export_mismatch_xlsx_correct():
    import openpyxl
    reg = _tiny_registry()
    result = _make_result("case1", fields={"a_beteg_neve": "X"})
    result.mismatches = [
        MismatchRecord("case1", "bad_file.pdf", "abc123", "Mas Beteg", "999")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_mismatch.xlsx"))
        ws = wb.active
        assert_eq(ws.max_row, 2)
        assert_eq(ws.cell(2, 2).value, "bad_file.pdf")


def test_export_history_followup_xlsx_correct():
    import openpyxl
    reg = _tiny_registry()
    result = _make_result("case1", fields={"a_beteg_neve": "X"})
    result.out_of_window = [
        OutOfWindowRecord(
            case_id="case1",
            filename="fibro.pdf",
            doc_type=DocType.ELASTOGRAPHY,
            event_date=date(2026, 1, 21),
            days_from_admission=10,
            window_type=WindowType.POST_DISCHARGE,
            summary="Post-discharge fibroscan",
        )
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_history_followup.xlsx"))
        ws = wb.active
        assert_eq(ws.max_row, 2)
        assert_eq(ws.cell(2, 2).value, "fibro.pdf")
        assert_eq(ws.cell(2, 5).value, 10)


def test_export_empty_result_produces_valid_files():
    """Empty CaseResult (no fields, no records) must still produce 8 files."""
    reg = _tiny_registry()
    result = CaseResult(case_id="empty_case")
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        paths = export(result, out_dir, reg)
        assert_eq(len(paths), 8, "should always produce 8 files")
        for p in paths:
            assert_true(p.exists(), f"file must exist: {p.name}")


def test_export_writes_run_config_json():
    """A passed run_config lands beside the tables as <case_id>_run_config.json."""
    import json
    reg = _tiny_registry()
    result = CaseResult(case_id="case1")
    cfg = {"llm": {"enabled": False}, "cache": {"enabled": False}}
    with tempfile.TemporaryDirectory() as tmp:
        paths = export(result, Path(tmp), reg, run_config=cfg)
        assert_eq(len(paths), 9, "8 tables + run_config.json")
        written = Path(tmp) / "case1" / "case1_run_config.json"
        assert_eq(json.loads(written.read_text(encoding="utf-8")), cfg)


def test_export_no_duplicate_column_names():
    """All column names in wide_table.xlsx header row must be unique."""
    import openpyxl
    reg = _tiny_registry()
    # Include many fields to stress the deduplication logic
    fields = {f"field_{i}": i for i in range(30)}
    result = _make_result("case1", fields=fields)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table.xlsx"))
        ws = wb.active
        headers = [ws.cell(row=1, column=i).value for i in range(1, ws.max_column + 1)]
        assert_eq(len(headers), len(set(headers)), "duplicate column names found")


def test_export_labels_xlsx_has_correct_shape():
    """Labels file: header + one row per column in the wide table."""
    import openpyxl
    reg = _tiny_registry()
    fields = {"a_beteg_neve": "X", "creatinine_d1": 108.0}
    result = _make_result("case1", fields=fields)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table_labels.xlsx"))
        ws = wb.active
        assert_eq(ws.max_column, 2, "labels sheet must have exactly 2 columns")
        assert_eq(ws.max_row, 1 + len(fields), "header + one row per field")


def test_export_labels_registry_field_has_human_label():
    """A registry field in the labels sheet should get its column_header as label."""
    import openpyxl
    reg = _tiny_registry()
    fields = {"a_beteg_neve": "Beteg Neve"}
    result = _make_result("case1", fields=fields)
    with tempfile.TemporaryDirectory() as tmp:
        out_dir = Path(tmp)
        export(result, out_dir, reg)
        wb = openpyxl.load_workbook(str(out_dir / "case1" / "case1_wide_table_labels.xlsx"))
        ws = wb.active
        # Row 2: variable_name, label
        label_val = ws.cell(row=2, column=2).value
        assert_eq(label_val, "Beteg neve", "registry field must use column_header as label")


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        # sanitise_col
        ("sanitise: negative day → dm",           test_sanitise_negative_day),
        ("sanitise: positive day unchanged",       test_sanitise_positive_day_unchanged),
        ("sanitise: strips special chars",         test_sanitise_strips_special_chars),
        ("sanitise: truncates to max len",         test_sanitise_truncates_to_max_len),
        ("sanitise: prefix digit with v_",         test_sanitise_prefix_digit),
        ("sanitise: normal snake_case unchanged",  test_sanitise_normal_snake_case_unchanged),
        # build_safe_cols
        ("safe_cols: no collision",                test_build_safe_cols_no_collision),
        ("safe_cols: collision resolved",          test_build_safe_cols_collision_resolved),
        ("safe_cols: all within length limit",     test_build_safe_cols_all_names_within_limit),
        # sort keys
        ("lab_sort: day first",                    test_lab_sort_key_day_first),
        ("lab_sort: analyte second",               test_lab_sort_key_analyte_second),
        ("lab_sort: occurrence third",             test_lab_sort_key_occurrence_third),
        ("event_sort: prefix first",               test_event_sort_key_prefix_first),
        ("event_sort: day second",                 test_event_sort_key_day_second),
        ("event_sort: index third",                test_event_sort_key_index_third),
        ("event_sort: MRI recognised",             test_event_sort_key_mri_recognised),
        ("event_sort: MRI sorts with others",      test_event_sort_key_mri_sorts_with_others),
        # build_col_order
        ("col_order: canonical first",             test_col_order_canonical_first),
        ("col_order: timepoints after canonical",  test_col_order_timepoints_after_canonical),
        ("col_order: lab after registry",          test_col_order_lab_columns_after_registry),
        ("col_order: event after lab",             test_col_order_event_columns_after_lab),
        ("col_order: all keys present",            test_col_order_all_keys_present),
        ("col_order: lab day sort correct",        test_col_order_lab_day_sort_correct),
        # csv helper
        ("csv_cell: plain",                        test_csv_cell_plain),
        ("csv_cell: comma quoted",                 test_csv_cell_comma_quoted),
        ("csv_cell: quote escaped",                test_csv_cell_quote_escaped),
        ("csv_cell: None → empty",                 test_csv_cell_none_empty),
        # export integration
        ("export: creates 8 files",                 test_export_creates_eight_files),
        ("export: writes run_config.json",         test_export_writes_run_config_json),
        ("export: wide table column count",        test_export_wide_table_column_count),
        ("export: Uncertain cell is yellow",       test_export_wide_table_uncertain_cell_is_yellow),
        ("export: non-Uncertain not yellow",       test_export_non_uncertain_cell_not_yellow),
        ("export: column names no special chars",  test_export_column_names_no_special_chars),
        ("export: column names max length",        test_export_column_names_max_length),
        ("export: CSV has BOM",                    test_export_csv_has_bom),
        ("export: CSV col count matches xlsx",     test_export_csv_column_count_matches_xlsx),
        ("export: CSV Hungarian format",           test_export_csv_hungarian_format),
        ("export: CSV default format unchanged",   test_export_csv_default_format_unchanged),
        ("export: unknown CSV format rejected",    test_export_unknown_csv_format_rejected),
        ("export: '=' text is not a formula",      test_export_text_starting_with_equals_is_not_a_formula),
        ("export: conflicts.xlsx rows correct",    test_export_conflicts_xlsx_has_correct_rows),
        ("export: mismatch.xlsx correct",          test_export_mismatch_xlsx_correct),
        ("export: history_followup.xlsx correct",  test_export_history_followup_xlsx_correct),
        ("export: empty result produces valid files", test_export_empty_result_produces_valid_files),
        ("export: no duplicate column names",      test_export_no_duplicate_column_names),
        ("export: labels xlsx shape",              test_export_labels_xlsx_has_correct_shape),
        ("export: labels registry field human label", test_export_labels_registry_field_has_human_label),
    ]

    print(f"\n{'─' * 60}")
    print("  test_export.py — Step 13: Export")
    print(f"{'─' * 60}")
    for name, fn in tests:
        run(name, fn)

    print(f"\n{'─' * 60}")
    total = len(_passed) + len(_failed)
    print(f"  {len(_passed)}/{total} passed", end="")
    if _failed:
        print(f"  |  FAILED: {', '.join(_failed)}")
    else:
        print("  — all passed ✓")
    print(f"{'─' * 60}\n")
    sys.exit(0 if not _failed else 1)
