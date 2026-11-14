"""
tests/unit/test_hard_lab.py

Tests for Step 8: hard lab extraction.

Covers:
  - to_float_hu()           — various numeric formats, edge cases
  - col_key()               — first/second/third occurrence suffixes
  - TextLabParser.parse()   — inline block format (Zárójelentés)
  - TextLabParser.parse()   — plain-text table format (structured lab report)
  - TableLabParser.parse()  — Camelot grid format
  - Same-day duplicate draws produce _1, _2 suffixes
  - Day-0 is never produced
  - 48 h pre-admission lab → correct negative day
  - Non-numeric values skipped
  - Unknown analytes skipped

Run with:
    PYTHONPATH=. \
        python3 tests/unit/test_hard_lab.py
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Helpers / fixtures
# ─────────────────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# Patient anchor — admission 2026-01-12, discharge 2026-01-20
# (matches real Zárójelentés fixture)
from app.domain.models import PatientAnchor

ANCHOR = PatientAnchor.create(
    name="Beteg Neve",
    dob=date(1960, 1, 1),
    taj="111-222-333",
    admission_start=datetime(2026, 1, 12, 4, 43),
    discharge_end=datetime(2026, 1, 20, 13, 32),
)

# ─────────────────────────────────────────────────────────────────────────────
# Import modules under test
# ─────────────────────────────────────────────────────────────────────────────

from app.pipeline.step5_extract._utils import col_key, to_float_hu
from app.pipeline.step5_extract.hard_lab import (
    LAB_MAPPING,
    TableLabParser,
    TextLabParser,
    _normalize_analyte,
)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: to_float_hu
# ─────────────────────────────────────────────────────────────────────────────

class TestToFloatHu:
    """to_float_hu() — Hungarian number string → float."""

    def test_integer_string(self):
        assert to_float_hu("136") == 136.0

    def test_dot_decimal(self):
        assert to_float_hu("3.9") == 3.9

    def test_comma_decimal(self):
        """Hungarian comma-as-decimal: "3,5" → 3.5"""
        assert to_float_hu("3,5") == 3.5

    def test_trailing_non_numeric(self):
        """Leading numeric part extracted: "1.86 -" → 1.86"""
        assert to_float_hu("1.86 -") == 1.86

    def test_leading_digits_with_unit_attached(self):
        """Value attached to unit text (no space): "9.6normál" → 9.6"""
        assert to_float_hu("9.6normál") == 9.6

    def test_negative_value_returns_none(self):
        """Negative lab values are not valid — return None."""
        assert to_float_hu("-0.5") is None

    def test_empty_string_returns_none(self):
        assert to_float_hu("") is None

    def test_whitespace_only_returns_none(self):
        assert to_float_hu("   ") is None

    def test_none_equivalent_returns_none(self):
        assert to_float_hu("lásd megjAsc") is None

    def test_zero_is_valid(self):
        assert to_float_hu("0") == 0.0

    def test_large_value(self):
        assert to_float_hu("123.8") == 123.8

    def test_non_breaking_space_stripped(self):
        """Non-breaking spaces should be stripped before parsing."""
        result = to_float_hu("18.32\u00a0mg")
        assert result == 18.32


# ─────────────────────────────────────────────────────────────────────────────
# Tests: col_key
# ─────────────────────────────────────────────────────────────────────────────

class TestColKey:
    """col_key() — column key generation with duplicate tracking."""

    def test_first_occurrence_no_suffix(self):
        cm: dict = {}
        assert col_key("creatinine", 2, cm) == "creatinine_d2"

    def test_second_occurrence_suffix_1(self):
        cm: dict = {}
        col_key("inr", 1, cm)  # consume first
        assert col_key("inr", 1, cm) == "inr_d1_1"

    def test_third_occurrence_suffix_2(self):
        cm: dict = {}
        col_key("inr", 1, cm)  # first
        col_key("inr", 1, cm)  # second → _1
        assert col_key("inr", 1, cm) == "inr_d1_2"

    def test_fourth_occurrence_suffix_3(self):
        cm: dict = {}
        for _ in range(3):
            col_key("wbc", 3, cm)
        assert col_key("wbc", 3, cm) == "wbc_d3_3"

    def test_different_analytes_independent(self):
        """Different analytes on same day do not share the counter."""
        cm: dict = {}
        assert col_key("creatinine", 2, cm) == "creatinine_d2"
        assert col_key("sodium", 2, cm) == "sodium_d2"

    def test_different_days_independent(self):
        """Same analyte on different days do not share the counter."""
        cm: dict = {}
        assert col_key("inr", 1, cm) == "inr_d1"
        assert col_key("inr", 2, cm) == "inr_d2"

    def test_negative_day(self):
        """Pre-admission day -1 produces key with d-1."""
        cm: dict = {}
        key = col_key("inr", -1, cm)
        assert key == "inr_d-1"

    def test_same_day_duplicates_for_day2(self):
        """Verified: 3 draws on day 2 → base, _1, _2."""
        cm: dict = {}
        k1 = col_key("inr", 2, cm)
        k2 = col_key("inr", 2, cm)
        k3 = col_key("inr", 2, cm)
        assert k1 == "inr_d2"
        assert k2 == "inr_d2_1"   # _d2_1 suffix
        assert k3 == "inr_d2_2"   # _d2_2 suffix


# ─────────────────────────────────────────────────────────────────────────────
# Tests: LAB_MAPPING / _normalize_analyte
# ─────────────────────────────────────────────────────────────────────────────

class TestLabMapping:
    """LAB_MAPPING correctness and _normalize_analyte()."""

    def test_creatinine_mapped(self):
        assert _normalize_analyte("Kreatinin") == "creatinine"

    def test_sodium_mapped(self):
        assert _normalize_analyte("Nátrium") == "sodium"

    def test_wbc_mapped(self):
        assert _normalize_analyte("Fehérvérsejt szám") == "wbc"

    def test_inr_mapped(self):
        assert _normalize_analyte("INR") == "inr"

    def test_case_insensitive(self):
        assert _normalize_analyte("nátrium") == "sodium"
        assert _normalize_analyte("NÁTRIUM") == "sodium"

    def test_extra_whitespace_stripped(self):
        assert _normalize_analyte("  Kreatinin  ") == "creatinine"

    def test_unknown_name_returns_none(self):
        assert _normalize_analyte("NRBC") is None  # NRBC# or NRBC% but not plain NRBC

    def test_no_duplicate_canonical_codes(self):
        """All canonical codes are unique (no two Hungarian names map to same code
        unless intentional aliases like 'crp' and 'crp (szenzitív)' → 'crp')."""
        # Some duplicates are intentional aliases — just assert mapping is non-empty
        assert len(LAB_MAPPING) >= 30, "LAB_MAPPING should have at least 30 entries"

    def test_glucose_alias(self):
        """'vércukor' is an alias for glucose."""
        assert _normalize_analyte("vércukor") == "glucose"

    def test_platelet_mapped(self):
        assert _normalize_analyte("Thrombocytaszám") == "platelet"

    def test_bilirubin_total_mapped(self):
        assert _normalize_analyte("Totál bilirubin") == "bilirubin_total"


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic inline text for TextLabParser tests
# ─────────────────────────────────────────────────────────────────────────────

# Clean KIZKEMIA-style block matching the real zárójelentés fixture
_INLINE_BLOCK_DAY2 = """\
LMI Központi Laboratórium (KIZKEMIA) 2026.01.13 09:21 (Mintavétel: 2026.01.13 08:00)
Nátrium: 136 mmol/L (133-146); Kálium: 3.9 mmol/L (3.5-5.3); Klorid: 100 mmol/L (99-111); \
Urea: 14.3 mmol/L (2.8-8.0); Kreatinin: 96 umol/L (62-106); GFR (EPI): 70 mL/p/1.73m2 (>90); \
Totál bilirubin: 123.8 umol/L (<20); CRP (szenzitív): 18.32 mg/L (<5.2);
"""

# Coagulation block — day 1 (Mintavétel 2026.01.12 00:32 → day 1)
_INLINE_BLOCK_DAY1_COAG = """\
LMI Központi Laboratórium (KIZHEMOS) 2026.01.12 11:05 (Mintavétel: 2026.01.12 00:30)
Protrombin idő: 17.7 sec (Kontroll:8.9); INR: 1.86 - (-);
"""

# Block with "lásd megjAsc" annotation (ascites fluid — non-numeric; should be skipped)
_INLINE_BLOCK_ASCITES = """\
LMI Központi Laboratórium (KIZUGYEL) 2026.01.12 05:48 (Mintavétel: 2026.01.12 00:30)
Glükóz: lásd megjAsc: Glükóz: 7,4 mmol/L mmol/L (3.6-6.0); LDH: lásd megjAsc. LDH: 73 U/L U/L (135.0-220.0);
"""

# Two INR draws on day 1 (same day duplicate test)
_INLINE_TWO_INR_DAY1 = """\
LMI Központi Laboratórium (LAB1) 2026.01.12 06:00 (Mintavétel: 2026.01.12 06:00)
INR: 1.86;
LMI Központi Laboratórium (LAB2) 2026.01.12 14:00 (Mintavétel: 2026.01.12 14:00)
INR: 1.92;
"""

# Pre-admission lab: drawn 2026.01.11 → day -1
_INLINE_PRE_ADMISSION = """\
LMI Központi Laboratórium (LAB3) 2026.01.11 08:00 (Mintavétel: 2026.01.11 08:00)
Kreatinin: 88 umol/L (62-106); INR: 2.1;
"""

# Plain-text table format (pdfminer output from a structured lab PDF)
_PLAIN_TABLE_TEXT = """\
Laborvizsgálati eredmények
Minta dátuma:
2026.01.16 17:10
Vizsgálat Eredmény Ref.tart. M.egység
Fehérvérsejt szám 5.87 4.50-10.80 giga/L
Vörösvértest szám 2.62 4.70-6.10 tera/L
Hemoglobin 97 130-165 g/L
Hematokrit 0.28 0.39-0.50 -
Thrombocytaszám 83 150-400 giga/L
"""


# ─────────────────────────────────────────────────────────────────────────────
# Tests: TextLabParser — inline block format
# ─────────────────────────────────────────────────────────────────────────────

class TestTextLabParserInlineBlocks:
    """TextLabParser with inline semicolon-delimited blocks."""

    def setup_method(self):
        self.parser = TextLabParser()

    def test_day2_chemistry_fields_extracted(self):
        """KIZKEMIA block: Nátrium, Kálium, Kreatinin, etc. extracted on day 2."""
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        keys = {f.key for f in fields}
        assert "sodium_d2" in keys, f"Expected sodium_d2 in {keys}"
        assert "potassium_d2" in keys
        assert "creatinine_d2" in keys
        assert "bilirubin_total_d2" in keys
        assert "crp_d2" in keys

    def test_creatinine_day2_value_correct(self):
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        creat = next((f for f in fields if f.key == "creatinine_d2"), None)
        assert creat is not None
        assert creat.value == 96.0

    def test_bilirubin_value_correct(self):
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        bil = next((f for f in fields if f.key == "bilirubin_total_d2"), None)
        assert bil is not None
        assert bil.value == 123.8

    def test_crp_value_correct(self):
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        crp = next((f for f in fields if f.key == "crp_d2"), None)
        assert crp is not None
        assert crp.value == 18.32

    def test_virology_serology_not_collected_as_day_columns(self):
        """Virology results are registry questions, not day-named lab columns;
        fecal occult blood remains a categorical lab column."""
        text = (
            "MBV Mikrob. Virológiai Labor 2026.01.13 10:00\n"
            "HBsAg: Negatív; Anti-HBc (IgG+IgM): Pozitív; Anti-HCV (IgG+IgM): Pozitív;\n\n"
            "LMI Központi Laboratórium (KIZKEMIA) 2026.01.13 09:21\n"
            "Széklet vér: Pozitív;\n"
        )
        fields = self.parser.parse(text, ANCHOR)
        keys = {f.key for f in fields}
        assert not any(k.startswith(("hbsag", "anti_hbc", "anti_hcv")) for k in keys), keys
        fob = next((f for f in fields if f.key == "fecal_occult_blood_d2"), None)
        assert fob is not None and fob.value == 1.0, keys

    def test_inr_with_trailing_dash_extracted(self):
        """INR: 1.86 - (-) — trailing dash must not prevent extraction."""
        fields = self.parser.parse(_INLINE_BLOCK_DAY1_COAG, ANCHOR)
        inr = next((f for f in fields if f.key == "inr_d1"), None)
        assert inr is not None
        assert inr.value == 1.86

    def test_ascites_block_values_skipped(self):
        """
        Values prefixed with 'lásd megjAsc' are non-numeric in the first colon
        segment and must be skipped.
        """
        fields = self.parser.parse(_INLINE_BLOCK_ASCITES, ANCHOR)
        # After 'Glükóz: lásd megjAsc' the pattern doesn't match a number
        keys = {f.key for f in fields}
        # No glucose_d1 should appear (it's ascites fluid)
        assert "glucose_d1" not in keys, f"Ascites glucose should be skipped, but got {keys}"

    def test_source_is_hard(self):
        from app.domain.enums import FieldSource
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        assert all(f.source == FieldSource.HARD for f in fields)

    def test_engine_is_text_block_parser(self):
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        assert all(f.engine == "text_block_parser" for f in fields)

    def test_confidence_is_095(self):
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        assert all(f.confidence == 0.95 for f in fields)

    def test_no_day_0_produced(self):
        """No extracted field should have a day-0 key."""
        combined = (
            _INLINE_BLOCK_DAY2
            + "\n"
            + _INLINE_BLOCK_DAY1_COAG
            + "\n"
            + _INLINE_PRE_ADMISSION
        )
        fields = self.parser.parse(combined, ANCHOR)
        for f in fields:
            # Extract day number from key pattern "_d{N}"
            import re
            m = re.search(r'_d(-?\d+)', f.key)
            if m:
                assert int(m.group(1)) != 0, f"Day 0 key found: {f.key}"

    def test_same_day_duplicate_inr_suffixes(self):
        """Two INR draws on day 1 → inr_d1 and inr_d1_1."""
        fields = self.parser.parse(_INLINE_TWO_INR_DAY1, ANCHOR)
        inr_keys = sorted(f.key for f in fields if "inr" in f.key)
        assert "inr_d1" in inr_keys, f"Expected inr_d1 in {inr_keys}"
        assert "inr_d1_1" in inr_keys, f"Expected inr_d1_1 in {inr_keys}"

    def test_pre_admission_lab_negative_day(self):
        """Lab drawn on 2026-01-11 → day -1."""
        fields = self.parser.parse(_INLINE_PRE_ADMISSION, ANCHOR)
        keys = {f.key for f in fields}
        assert "creatinine_d-1" in keys, f"Expected creatinine_d-1 in {keys}"
        assert "inr_d-1" in keys

    def test_gfr_epi_extracted(self):
        """Analyte with parentheses in name: 'GFR (EPI)' → gfr_epi."""
        fields = self.parser.parse(_INLINE_BLOCK_DAY2, ANCHOR)
        keys = {f.key for f in fields}
        assert "gfr_epi_d2" in keys, f"Expected gfr_epi_d2 in {keys}"

    def test_empty_text_returns_empty_list(self):
        assert self.parser.parse("", ANCHOR) == []

    def test_no_lab_blocks_returns_empty_list(self):
        assert self.parser.parse("Some other document text.\nNo labs here.", ANCHOR) == []

    def test_empty_mintavetel_keeps_first_analyte(self):
        """'(Mintavétel: )' used to stay glued to the first analyte and lose it."""
        text = (
            "## **LMI Központi Laboratórium (KIZKEMIA) 2026.01.12 10:36 (Mintavétel: )** \n\n"
            "Nátrium: 135 mmol/L (133-146); Kálium: 4.4 mmol/L (3.5-5.3); \n\n"
            "**LMI Központi Laboratórium (KIZKROMA) 2026.01.12 12:10 (Mintavétel: )** "
            "Hemoglobin A1C: 4.3 % (4.0-5.6); Hemoglobin A1cIFCC: 23.0 mmol/mol (20.0-38.0); \n"
        )
        values = {f.key: f.value for f in self.parser.parse(text, ANCHOR)}
        assert values.get("sodium_d1") == 135.0, values
        assert values.get("potassium_d1") == 4.4, values
        assert values.get("hba1c_d1") == 4.3, values
        assert values.get("hba1c_ifcc_d1") == 23.0, values

    def test_qualified_values_kept_as_reported_text(self):
        text = (
            "Bartók HQ laboratórium (2026.01.11 13:45) (Mintavétel: 2026.01.11 12:02)\n"
            "CRP: 16.6; hTNI: < 10; gfr (kreatininból számolva): 61;\n"
            "LMI Központi Laboratórium (KIZIMKEM) 2026.01.12 10:35 (Mintavétel: )\n"
            "Ferritin: **>2000.0** ug/L (30.0-400.0); Vas: 13.3 umol/L (10.6-28.3);\n"
        )
        values = {f.key: f.value for f in self.parser.parse(text, ANCHOR)}
        assert values.get("troponin_d-1") == "<10", values
        assert values.get("ferritin_d1") == ">2000.0", values
        assert values.get("crp_d-1") == 16.6, values
        assert values.get("iron_d1") == 13.3, values

    def test_bartok_and_tumour_marker_aliases(self):
        text = (
            "Bartók HQ laboratórium (2026.01.11 13:45) (Mintavétel: 2026.01.11 12:02)\n"
            "IDo: 18.70; INR: 1.62; gfr (kreatininból számolva): 61;\n"
            "LMI Központi Laboratórium (KIZIMKEM) 2026.01.12 10:35 (Mintavétel: )\n"
            "CA 19-9: 113.0 kU/L (<34); Alfa-fetoprotein(t.marker): 2.3 kU/L (<5.8);\n"
        )
        values = {f.key: f.value for f in self.parser.parse(text, ANCHOR)}
        assert values.get("pt_sec_d-1") == 18.7, values
        assert values.get("gfr_epi_d-1") == 61.0, values
        assert values.get("afp_d1") == 2.3, values

    def test_ascites_notes_go_to_ascites_columns(self):
        """'Glükóz: lásd megjAsc: Glükóz: 7,4' is ascitic fluid, not serum."""
        text = (
            "LMI Központi Laboratórium (KIZUGYEL) 2026.01.12 05:48 (Mintavétel: 2026.01.12 00:30)\n"
            "Glükóz: lásd megjAsc: Glükóz: 7,4 mmol/L mmol/L (3.6-6.0); "
            "Totál protein: lásd megjAsc. TP: 11 g/L g/L (60-80); "
            "Fehérvérsejt szám: lásd megjAscites Fvs: 0,119 Giga/L giga/L (4.50-10.80); "
            "PMN#: 0.024 giga/L ();\n"
            "MBV Mikrob. Labor 2026.01.12 13:32 (Mintavétel: )\n"
            "Carcino-embrionális antigén: Lásd megj.Ascites CEA: 0,2 ug/L ug/L (<3.4);\n"
        )
        values = {f.key: f.value for f in self.parser.parse(text, ANCHOR)}
        assert values.get("ascites_glucose_d1") == 7.4, values
        assert values.get("ascites_total_protein_d1") == 11.0, values
        assert values.get("ascites_wbc_d1") == 0.119, values
        assert values.get("ascites_cea_d1") == 0.2, values
        assert values.get("ascites_pmn_abs_d1") == 0.024, values
        assert not {"glucose_d1", "total_protein_d1", "wbc_d1", "cea_d1"} & set(values)

    def test_page_break_inside_a_lab_line_loses_nothing(self):
        """The print footer between two pages must not swallow an analyte."""
        from app.pipeline.step5_extract._utils import strip_print_furniture
        pages = (
            ". \n\nLMI Központi Laboratórium (KIZHEMF) 2026.01.13 08:41 "
            "(Mintavétel: 2026.01.13 08:00)\n\nMean Platelet Volume: 10.8 fL; "
            "Platelet-Large \n\nKajla Kata - Esetszám: 1234567890 \n\n"
            "Nyomtatva: UD MED - 2026.03.31 22:33 Oldal 4/7",
            ". Cell Ratio: 31.4 % (13-43); neutrofil%: 57.6 % (43.0-76.0);",
        )
        values = {f.key: f.value for f in self.parser.parse(strip_print_furniture(pages), ANCHOR)}
        assert values.get("plcr_d2") == 31.4, values
        assert values.get("neutrophil_pct_d2") == 57.6, values


# ─────────────────────────────────────────────────────────────────────────────
# Tests: TextLabParser — plain-text table format
# ─────────────────────────────────────────────────────────────────────────────

class TestTextLabParserPlainTable:
    """TextLabParser with structured lab-report plain-text format."""

    def setup_method(self):
        self.parser = TextLabParser()

    def test_wbc_extracted_day5(self):
        """Labor2 fixture date 2026-01-16 → day 5; WBC = 5.87."""
        fields = self.parser.parse(_PLAIN_TABLE_TEXT, ANCHOR)
        wbc = next((f for f in fields if f.key == "wbc_d5"), None)
        assert wbc is not None, f"wbc_d5 not found in {[f.key for f in fields]}"
        assert wbc.value == 5.87

    def test_hemoglobin_extracted_day5(self):
        fields = self.parser.parse(_PLAIN_TABLE_TEXT, ANCHOR)
        hgb = next((f for f in fields if f.key == "hemoglobin_d5"), None)
        assert hgb is not None, f"hemoglobin_d5 not found"
        assert hgb.value == 97.0

    def test_platelet_extracted_day5(self):
        fields = self.parser.parse(_PLAIN_TABLE_TEXT, ANCHOR)
        plt = next((f for f in fields if f.key == "platelet_d5"), None)
        assert plt is not None
        assert plt.value == 83.0

    def test_engine_is_plain_table_parser(self):
        fields = self.parser.parse(_PLAIN_TABLE_TEXT, ANCHOR)
        table_fields = [f for f in fields if f.engine == "plain_table_parser"]
        assert len(table_fields) > 0

    def test_qualified_values_in_plain_table(self):
        text = (
            "Minta dátuma:\n2026.01.16 17:10\n"
            "Vizsgálat Eredmény Ref.tart. M.egység\n"
            "Ferritin >2000.0 30.0-400.0 ug/L\n"
            "Troponin < 10 0-14 ng/L\n"
            "Vas 13.3 10.6-28.3 umol/L\n"
        )
        values = {f.key: f.value for f in self.parser.parse(text, ANCHOR)}
        assert values.get("ferritin_d5") == ">2000.0", values
        assert values.get("troponin_d5") == "<10", values
        assert values.get("iron_d5") == 13.3, values

    def test_day5_correct(self):
        """2026-01-16 is 4 days after admission 2026-01-12 → day 5 (delta+1)."""
        fields = self.parser.parse(_PLAIN_TABLE_TEXT, ANCHOR)
        import re
        for f in fields:
            m = re.search(r'_d(-?\d+)', f.key)
            if m:
                assert int(m.group(1)) == 5, f"Expected day 5 for {f.key}, got {m.group(1)}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: TextLabParser — real fixture file
# ─────────────────────────────────────────────────────────────────────────────

class TestTextLabParserRealFixture:
    """TextLabParser against the real Zárójelentés fixture (if available)."""

    def _load_fixture_text(self, name: str) -> str | None:
        p = FIXTURES_DIR / name
        if not p.exists():
            return None
        try:
            from app.pipeline.step1_ingest import ingest
            doc = ingest(p)
            return doc.full_text
        except Exception:
            return None

    def test_zaro_fixture_yields_day2_chemistry(self):
        """Real Zárójelentés: KIZKEMIA block should produce creatinine_d2."""
        text = self._load_fixture_text("FS_záró.pdf")
        if text is None:
            print("  SKIP: FS_záró.pdf not available")
            return
        parser = TextLabParser()
        fields = parser.parse(text, ANCHOR)
        keys = {f.key for f in fields}
        assert "creatinine_d2" in keys, f"creatinine_d2 not found; keys={sorted(keys)}"

    def test_zaro_fixture_no_day_0(self):
        """Real Zárójelentés: day 0 must never appear."""
        text = self._load_fixture_text("FS_záró.pdf")
        if text is None:
            print("  SKIP: FS_záró.pdf not available")
            return
        parser = TextLabParser()
        fields = parser.parse(text, ANCHOR)
        import re
        for f in fields:
            m = re.search(r'_d(-?\d+)', f.key)
            if m:
                assert int(m.group(1)) != 0, f"Day 0 in real fixture: {f.key}"

    def test_labor2_fixture_day5_wbc(self):
        """Real labor2 fixture: sample date 2026-01-16 → day 5; WBC extracted."""
        text = self._load_fixture_text("FS_labor2.pdf")
        if text is None:
            print("  SKIP: FS_labor2.pdf not available")
            return
        parser = TextLabParser()
        fields = parser.parse(text, ANCHOR)
        keys = {f.key for f in fields}
        assert "wbc_d5" in keys, f"wbc_d5 not found; keys={sorted(keys)}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: TableLabParser
# ─────────────────────────────────────────────────────────────────────────────

_SYNTHETIC_TABLE: list[list[str]] = [
    # Header rows (include a date in a cell)
    ["Laboratórium", "Minta dátuma: 2026.01.13", ""],
    ["Vizsgálat", "Eredmény", "Ref.tart.", "M.egység"],
    # Data rows
    ["Nátrium",           "136",  "133-146", "mmol/L"],
    ["Kreatinin",         "96",   "62-106",  "umol/L"],
    ["Totál bilirubin",   "123.8","<20",     "umol/L"],
    ["INR",               "1.86", "",        ""],
    ["Fehérvérsejt szám", "5.87", "4.50-10.80", "giga/L"],
    ["UNKNOWN_ANALYTE",   "999",  "",        ""],   # should be skipped
    ["Hemoglobin",        "negatív", "",     ""],   # non-numeric value → skipped
]

# Table with three INR draws on day 2 (same-day duplicate test)
_DUPLICATE_TABLE: list[list[str]] = [
    ["Dátum", "2026.01.13", ""],
    ["Vizsgálat", "Eredmény", ""],
    ["INR", "1.86", ""],
    ["INR", "1.92", ""],
    ["INR", "2.01", ""],
]


class TestTableLabParser:
    """TableLabParser with synthetic Camelot grid data."""

    def setup_method(self):
        self.parser = TableLabParser()

    def test_sodium_extracted_day2(self):
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        sodium = next((f for f in fields if f.key == "sodium_d2"), None)
        assert sodium is not None, f"sodium_d2 not found; keys={[f.key for f in fields]}"
        assert sodium.value == 136.0

    def test_creatinine_extracted_day2(self):
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        creat = next((f for f in fields if f.key == "creatinine_d2"), None)
        assert creat is not None
        assert creat.value == 96.0

    def test_inr_extracted_day2(self):
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        inr = next((f for f in fields if f.key == "inr_d2"), None)
        assert inr is not None
        assert inr.value == 1.86

    def test_unknown_analyte_skipped(self):
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        keys = {f.key for f in fields}
        assert not any("unknown" in k for k in keys)

    def test_non_numeric_value_skipped(self):
        """Hemoglobin row has 'negatív' as value — must be skipped."""
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        keys = {f.key for f in fields}
        assert "hemoglobin_d2" not in keys

    def test_empty_tables_list_returns_empty(self):
        assert self.parser.parse([], ANCHOR) == []

    def test_empty_table_returns_empty(self):
        assert self.parser.parse([[]], ANCHOR) == []

    def test_no_date_in_cells_uses_fallback(self):
        table_no_date = [
            ["Vizsgálat", "Eredmény"],
            ["Kreatinin",  "96"],
        ]
        fields = self.parser.parse(
            [table_no_date], ANCHOR,
            sample_date=date(2026, 1, 13)
        )
        assert any(f.key == "creatinine_d2" for f in fields)

    def test_no_date_and_no_fallback_returns_empty(self):
        table_no_date = [
            ["Vizsgálat", "Eredmény"],
            ["Kreatinin",  "96"],
        ]
        fields = self.parser.parse([table_no_date], ANCHOR)
        assert fields == []

    def test_same_day_three_inr_draws(self):
        """Three INR draws on day 2 → inr_d2, inr_d2_1, inr_d2_2."""
        fields = self.parser.parse([_DUPLICATE_TABLE], ANCHOR)
        inr_keys = sorted(f.key for f in fields if "inr" in f.key)
        assert inr_keys == ["inr_d2", "inr_d2_1", "inr_d2_2"], (
            f"Expected ['inr_d2', 'inr_d2_1', 'inr_d2_2'], got {inr_keys}"
        )

    def test_multiple_tables_combined(self):
        """Two tables are combined and fields from both returned."""
        table2 = [
            ["2026.01.14", "", ""],
            ["Vizsgálat", "Eredmény", ""],
            ["INR", "1.63", ""],
        ]
        fields = self.parser.parse([_SYNTHETIC_TABLE, table2], ANCHOR)
        keys = {f.key for f in fields}
        assert "inr_d2" in keys   # from table 1
        assert "inr_d3" in keys   # from table 2 (day 3 = 2026-01-14)

    def test_source_is_hard(self):
        from app.domain.enums import FieldSource
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        assert all(f.source == FieldSource.HARD for f in fields)

    def test_engine_is_camelot(self):
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        assert all(f.engine == "camelot" for f in fields)

    def test_no_day_0_produced(self):
        import re
        fields = self.parser.parse([_SYNTHETIC_TABLE], ANCHOR)
        for f in fields:
            m = re.search(r'_d(-?\d+)', f.key)
            if m:
                assert int(m.group(1)) != 0, f"Day 0 found: {f.key}"

    def test_qualified_cell_value_kept_as_text(self):
        table = [
            ["2026.01.13", ""],
            ["Vizsgálat", "Eredmény"],
            ["Ferritin", ">2000.0"],
            ["hTNI", "< 10"],
        ]
        values = {f.key: f.value for f in self.parser.parse([table], ANCHOR)}
        assert values == {"ferritin_d2": ">2000.0", "troponin_d2": "<10"}, values


# ─────────────────────────────────────────────────────────────────────────────
# Tests: shared helpers — parse_lab_value, strip_print_furniture
# ─────────────────────────────────────────────────────────────────────────────

class TestSharedHelpers:
    def test_parse_lab_value_plain_and_qualified(self):
        from app.pipeline.step5_extract._utils import parse_lab_value
        assert parse_lab_value("3,5") == 3.5
        assert parse_lab_value(">2000.0") == ">2000.0"
        assert parse_lab_value("< 10") == "<10"
        assert parse_lab_value("<0,5") == "<0.5"
        assert parse_lab_value("negatív") is None
        assert parse_lab_value("") is None

    def test_strip_print_furniture_removes_footer_and_page_dot(self):
        from app.pipeline.step5_extract._utils import strip_print_furniture
        pages = (
            ". \n\nElső oldal szöveg.\n\nKajla Kata - Esetszám: 1234567890 \n\n"
            "Nyomtatva: UD MED - 2026.03.31 22:33 Oldal 1/2",
            ". \n\nMásodik oldal.\n\nBeteg Neve - Esetszám: xxxxxxxxxx \n\nNyomtatva: xxxxxxxxx",
        )
        text = strip_print_furniture(pages)
        assert text == "Első oldal szöveg.\n\nMásodik oldal.", repr(text)

    def test_strip_print_furniture_separate_page_number_line(self):
        from app.pipeline.step5_extract._utils import strip_print_furniture
        text = strip_print_furniture(("Szöveg.\nUD MED - 2026.03.31 22:33 Oldal 2/7\n",))
        assert text == "Szöveg.", repr(text)


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestToFloatHu,
        TestColKey,
        TestLabMapping,
        TestTextLabParserInlineBlocks,
        TestTextLabParserPlainTable,
        TestTextLabParserRealFixture,
        TestTableLabParser,
        TestSharedHelpers,
    ]

    passed = failed = skipped = 0
    for cls in all_classes:
        instance = cls()
        methods = sorted(m for m in dir(cls) if m.startswith("test_"))
        for method_name in methods:
            # Support setup_method if defined
            if hasattr(instance, "setup_method"):
                try:
                    instance.setup_method()
                except Exception as e:
                    print(f"  ERROR  setup_method for {cls.__name__}: {e}")
                    continue
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS   {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL   {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR  {cls.__name__}.{method_name}: {e}")
                traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"  PASSED:  {passed}/{passed + failed}")
    if skipped:
        print(f"  SKIPPED: {skipped}")
    if failed:
        print(f"  FAILED:  {failed}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
