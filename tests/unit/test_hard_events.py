"""
tests/unit/test_hard_events.py

Tests for Step 9: hard event extraction.

Covers:
  - event_col_key()             — key format with n always present
  - EndoscopyParser             — gastro/colono sub-type, date, result, opinion
  - FibroscanParser             — Expert format and brief summary format
  - ImagingParser               — RTG/UH/CT from section headers
  - MicrobiologyParser          — sample type, pathogen, multiresistant
  - Multiple events per day → correct _1, _2 indices
  - Embedded events from Zárójelentés text
  - Day-0 never produced
  - Real fixture files (skipped if absent)

Run with:
    PYTHONPATH=. \
        python3 tests/unit/test_hard_events.py
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

from app.domain.enums import DocType, FieldSource
from app.domain.models import PatientAnchor, RoutedDoc

# Anchor: admission 2026-01-12, discharge 2026-01-20
ANCHOR = PatientAnchor.create(
    name="Beteg Neve",
    dob=date(1960, 1, 1),
    taj="111-222-333",
    admission_start=datetime(2026, 1, 12, 4, 43),
    discharge_end=datetime(2026, 1, 20, 13, 32),
)

from app.pipeline.step5_extract.hard_events import (
    EndoscopyParser,
    FibroscanParser,
    ImagingParser,
    MicrobiologyParser,
    event_col_key,
    _next_event_n,
    _find_date_before,
    _parse_date_str,
    _FELVETELI_DATUM_RE,
)


def _make_doc(text: str, doc_type: DocType = DocType.DISCHARGE_SUMMARY) -> RoutedDoc:
    """Create a minimal RoutedDoc from raw text for testing."""
    return RoutedDoc(
        path=Path("test.txt"),
        doc_type=doc_type,
        pages=(text,),
        tables=(),
        meta={},
    )


def _load_fixture(name: str, doc_type: DocType) -> RoutedDoc | None:
    p = FIXTURES_DIR / name
    if not p.exists():
        return None
    try:
        from app.pipeline.step1_ingest import ingest
        doc = ingest(p)
        return doc.with_doc_type(doc_type)
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: event_col_key
# ─────────────────────────────────────────────────────────────────────────────

class TestEventColKey:
    """event_col_key() — always includes n (1-based)."""

    def test_basic_format(self):
        assert event_col_key("endo_gastro", 2, 1, "date") == "endo_gastro_d2_1_date"

    def test_second_event(self):
        assert event_col_key("endo_gastro", 2, 2, "result") == "endo_gastro_d2_2_result"

    def test_fibroscan_format(self):
        assert event_col_key("fibro", 10, 1, "E_kpa") == "fibro_d10_1_E_kpa"

    def test_negative_day(self):
        assert event_col_key("uh", -1, 1, "date") == "uh_d-1_1_date"

    def test_rtg(self):
        assert event_col_key("rtg", -1, 1, "result") == "rtg_d-1_1_result"

    def test_mri(self):
        assert event_col_key("mri", 4, 1, "date") == "mri_d4_1_date"

    def test_micro(self):
        assert event_col_key("micro", 6, 1, "pathogen") == "micro_d6_1_pathogen"


class TestNextEventN:
    """_next_event_n() — 1-based, increments per (prefix, day) pair."""

    def test_first_call_returns_1(self):
        c: dict = {}
        assert _next_event_n(c, "fibro", 10) == 1

    def test_second_call_returns_2(self):
        c: dict = {}
        _next_event_n(c, "fibro", 10)
        assert _next_event_n(c, "fibro", 10) == 2

    def test_different_prefix_independent(self):
        c: dict = {}
        assert _next_event_n(c, "endo_gastro", 2) == 1
        assert _next_event_n(c, "endo_colono", 2) == 1

    def test_different_day_independent(self):
        c: dict = {}
        assert _next_event_n(c, "uh", -1) == 1
        assert _next_event_n(c, "uh", 1) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Tests: date helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestDateHelpers:
    def test_parse_date_dot_format(self):
        assert _parse_date_str("2026.01.13") == date(2026, 1, 13)

    def test_parse_date_dash_format(self):
        assert _parse_date_str("2026-01-13") == date(2026, 1, 13)

    def test_parse_date_in_sentence(self):
        assert _parse_date_str("Érkezés: 2026.01.13 08:00") == date(2026, 1, 13)

    def test_parse_date_none_on_garbage(self):
        assert _parse_date_str("no date here") is None

    def test_find_date_before_erkezes(self):
        text = "Foo\nÉrkezés dátuma: 2026.01.13 08:00\nBar\nEndoszkópos lelet:"
        pos = text.index("Endoszkópos")
        d = _find_date_before(text, pos)
        assert d == date(2026, 1, 13)

    def test_find_date_before_any_date(self):
        text = "Some text 2026.01.15 some more text.\nBlock starts here."
        pos = len(text)
        d = _find_date_before(text, pos)
        assert d == date(2026, 1, 15)

    def test_find_date_before_returns_latest(self):
        """Multiple dates — should return last one before pos."""
        text = "2026.01.11\n2026.01.13\n2026.01.16\nBlock"
        pos = text.index("Block")
        d = _find_date_before(text, pos)
        assert d == date(2026, 1, 16)

    def test_find_date_before_none_when_absent(self):
        assert _find_date_before("No dates here.", 14) is None


# ─────────────────────────────────────────────────────────────────────────────
# Tests: EndoscopyParser — synthetic text
# ─────────────────────────────────────────────────────────────────────────────

_GASTRO_TEXT = """\
Érkezés dátuma: 2026.01.13 08:00
Gastroscopia
Eszköz: Fujinon EG-720R típusú gastroscop, SN: K859
Endoszkópos lelet: A nyelőcső ép. Fundális varicositas látható.
Szövettani vizsgálatkérés: nem történt.
Vélemény: Fundális varicositas. Pangásos gastropathia gr II.
Javasolt: NSBB
"""

_COLONO_TEXT = """\
Érkezés dátuma: 2026.01.16 08:00
COLONOSCOPIA
Eszköz: Fujinon EC-720R típusú colonoscop, SN:K035
Endoszkópos lelet: Az előkészítés megfelelő. A lumen szabad, a nyálkahártya ép.
Szövettani vizsgálatkérés: nem történt.
Vélemény: Negatív colonoscopos lelet.
"""

_TWO_ENDOSCOPY_TEXT = """\
GAD5 - Gasztroenterológiai Klinika 2026.01.13. 08.00 (Vizsgálati részlelet)
Eszköz: gastroscop SN: K859
Endoszkópos lelet: Fundális varicositas látható.
Vélemény: Fundális varicositas.
GAD5 - Gasztroenterológiai Klinika 2026.01.16. 08.00 (Röntgen vizsgálati lelet)
Eszköz: colonoscop SN:K035
Endoszkópos lelet: Normál mucosa.
Vélemény: Negatív colonoscopos lelet.
"""


class TestEndoscopyParserSynthetic:
    def setup_method(self):
        self.parser = EndoscopyParser()

    def test_gastro_date_key_present(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_gastro_d2_1_date" in keys, f"Keys: {keys}"

    def test_gastro_date_value_correct(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "endo_gastro_d2_1_date")
        assert f.value == "2026-01-13"

    def test_gastro_result_present(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_gastro_d2_1_result" in keys

    def test_gastro_result_contains_text(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "endo_gastro_d2_1_result")
        assert "varicositas" in f.value.lower() or len(f.value) > 5

    def test_gastro_opinion_present(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_gastro_d2_1_opinion" in keys

    def test_gastro_opinion_content(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "endo_gastro_d2_1_opinion")
        assert "varicositas" in f.value.lower() or "gastropathia" in f.value.lower()

    def test_colono_subtype_detected(self):
        doc = _make_doc(_COLONO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_colono_d5_1_date" in keys, f"Keys: {keys}"

    def test_colono_date_day5(self):
        """2026-01-16 = day 5 (delta=4, day=5)."""
        doc = _make_doc(_COLONO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if "endo_colono" in f.key and "_date" in f.key)
        assert f.value == "2026-01-16"

    def test_two_events_both_extracted(self):
        """Two endoscopy blocks in one text → both extracted."""
        doc = _make_doc(_TWO_ENDOSCOPY_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_gastro_d2_1_date" in keys, f"Keys: {keys}"
        assert "endo_colono_d5_1_date" in keys, f"Keys: {keys}"

    def test_source_is_heuristic(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.source == FieldSource.HEURISTIC for f in fields)

    def test_engine_is_block_parser(self):
        doc = _make_doc(_GASTRO_TEXT, DocType.ENDOSCOPY)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.engine == "block_parser" for f in fields)

    def test_no_day_0(self):
        doc = _make_doc(_TWO_ENDOSCOPY_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        import re
        for f in fields:
            m = re.search(r'_d(-?\d+)_', f.key)
            if m:
                assert int(m.group(1)) != 0, f"Day 0: {f.key}"

    def test_empty_doc_returns_empty(self):
        doc = _make_doc("No endoscopy here.")
        assert self.parser.parse(doc, ANCHOR) == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests: EndoscopyParser — real fixtures
# ─────────────────────────────────────────────────────────────────────────────

class TestEndoscopyParserFixtures:
    def setup_method(self):
        self.parser = EndoscopyParser()

    def _get_doc(self, name: str, doc_type: DocType) -> RoutedDoc | None:
        return _load_fixture(name, doc_type)

    def test_gastro_fixture_date_day2(self):
        doc = self._get_doc("FS_endogastro.pdf", DocType.ENDOSCOPY)
        if doc is None:
            print("  SKIP: FS_endogastro.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_gastro_d2_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_colono_fixture_date_day5(self):
        doc = self._get_doc("FS_endocolono.pdf", DocType.ENDOSCOPY)
        if doc is None:
            print("  SKIP: FS_endocolono.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_colono_d5_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_gastro_fixture_opinion_varicositas(self):
        doc = self._get_doc("FS_endogastro.pdf", DocType.ENDOSCOPY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "opinion" in f.key), None)
        assert f is not None, "No opinion field found"
        assert "varicositas" in f.value.lower(), f"Opinion: {f.value!r}"

    def test_zaro_fixture_both_endoscopy_events(self):
        """Zárójelentés contains both gastro (day 2) and colono (day 5)."""
        doc = self._get_doc("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        if doc is None:
            print("  SKIP: FS_záró.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "endo_gastro_d2_1_date" in keys, f"Keys: {sorted(keys)}"
        assert "endo_colono_d5_1_date" in keys, f"Keys: {sorted(keys)}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: FibroscanParser — synthetic text
# ─────────────────────────────────────────────────────────────────────────────

_FIBRO_EXPERT_TEXT = """\
AMBULÁNS KEZELŐLAP
Érkezés dátuma: 2026.01.21 15:45
Vizsgálatok eredményei:
Fibroscan:
Máj
FibroScan Expert : E: 75.0 kPa, CAP:250 dB/m IQR/Med:0 %
Steatosis grade: S0
Fibrosis stádium: F4
"""

_FIBRO_BRIEF_TEXT = """\
Egyéb szöveg
2026.01.16 Fibroscan
Máj (XL-es vizsgálófej) 75.0 kPa, IQR 0%, CAP 250 dB/m, SD 9.
S0 F4
Következő sor
"""

_FIBRO_TWO_EVENTS_TEXT = """\
2026.01.16 Fibroscan
Máj 75.0 kPa, IQR 5%, CAP 230 dB/m
S1 F3

FibroScan Expert : E: 80.0 kPa, CAP:260 dB/m IQR/Med:10 %
Steatosis grade: S1
Fibrosis stádium: F4
Érkezés dátuma: 2026.01.21 15:45
"""


class TestFibroscanParserSynthetic:
    def setup_method(self):
        self.parser = FibroscanParser()

    # Expert format
    def test_expert_E_kpa(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "E_kpa" in f.key), None)
        assert f is not None, f"Keys: {[f.key for f in fields]}"
        assert f.value == 75.0

    def test_expert_CAP_dbm(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "CAP_dbm" in f.key), None)
        assert f is not None
        assert f.value == 250.0

    def test_expert_IQR_pct(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "IQR_pct" in f.key), None)
        assert f is not None
        assert f.value == 0.0

    def test_expert_steatosis_grade(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "steatosis_grade" in f.key), None)
        assert f is not None
        assert f.value == "S0"

    def test_expert_fibrosis_stage(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "fibrosis_stage" in f.key), None)
        assert f is not None
        assert f.value == "F4"

    def test_expert_day_is_10(self):
        """2026-01-21 = 9 days after 2026-01-12 → day 10."""
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        assert all("_d10_" in f.key for f in fields), f"Keys: {[f.key for f in fields]}"

    def test_expert_key_format(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        assert any(f.key == "fibro_d10_1_E_kpa" for f in fields)

    # Brief format
    def test_brief_E_kpa(self):
        doc = _make_doc(_FIBRO_BRIEF_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "E_kpa" in f.key), None)
        assert f is not None, f"Keys: {[f.key for f in fields]}"
        assert f.value == 75.0

    def test_brief_CAP_dbm(self):
        doc = _make_doc(_FIBRO_BRIEF_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "CAP_dbm" in f.key), None)
        assert f is not None
        assert f.value == 250.0

    def test_brief_steatosis_S0(self):
        doc = _make_doc(_FIBRO_BRIEF_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "steatosis_grade" in f.key), None)
        assert f is not None
        assert f.value == "S0"

    def test_brief_fibrosis_F4(self):
        doc = _make_doc(_FIBRO_BRIEF_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "fibrosis_stage" in f.key), None)
        assert f is not None
        assert f.value == "F4"

    def test_brief_day_is_5(self):
        """2026-01-16 → day 5."""
        doc = _make_doc(_FIBRO_BRIEF_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        assert any("_d5_" in f.key for f in fields), f"Keys: {[f.key for f in fields]}"

    # Source/engine
    def test_source_is_heuristic(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.source == FieldSource.HEURISTIC for f in fields)

    def test_engine_block_parser(self):
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.engine == "block_parser" for f in fields)

    def test_no_day_0(self):
        import re
        doc = _make_doc(_FIBRO_EXPERT_TEXT, DocType.ELASTOGRAPHY)
        fields = self.parser.parse(doc, ANCHOR)
        for f in fields:
            m = re.search(r'_d(-?\d+)_', f.key)
            if m:
                assert int(m.group(1)) != 0

    def test_empty_doc_returns_empty(self):
        doc = _make_doc("No fibroscan here.")
        assert self.parser.parse(doc, ANCHOR) == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests: FibroscanParser — real fixtures
# ─────────────────────────────────────────────────────────────────────────────

class TestFibroscanParserFixtures:
    def setup_method(self):
        self.parser = FibroscanParser()

    def test_fibro_fixture_E_kpa_75(self):
        doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)
        if doc is None:
            print("  SKIP: FS_fibro.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        f = next((x for x in fields if "E_kpa" in x.key), None)
        assert f is not None, f"Keys: {[x.key for x in fields]}"
        assert f.value == 75.0

    def test_fibro_fixture_CAP_250(self):
        doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        f = next((x for x in fields if "CAP_dbm" in x.key), None)
        assert f is not None
        assert f.value == 250.0

    def test_fibro_fixture_S0(self):
        doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        f = next((x for x in fields if "steatosis_grade" in x.key), None)
        assert f is not None
        assert f.value == "S0"

    def test_fibro_fixture_F4(self):
        doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        f = next((x for x in fields if "fibrosis_stage" in x.key), None)
        assert f is not None
        assert f.value == "F4"

    def test_fibro_fixture_day_10(self):
        """Fibroscan date 2026-01-21 → day 10 (POST_DISCHARGE)."""
        doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        assert any("_d10_" in f.key for f in fields), f"Keys: {[f.key for f in fields]}"

    def test_zaro_has_embedded_fibroscan(self):
        """Zárójelentés page 2 has a brief fibroscan summary (2026-01-16 → day 5)."""
        doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        if doc is None:
            print("  SKIP: FS_záró.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        # At minimum, E_kpa or CAP_dbm on day 5
        found = any("fibro_d5_" in k for k in keys)
        assert found, f"Expected fibro_d5_* keys; got: {sorted(k for k in keys if 'fibro' in k)}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ImagingParser — synthetic text
# ─────────────────────────────────────────────────────────────────────────────

_RTG_TEXT = """\
KRTS-OKK RAD KC Röntgen 1. szakrendelés SBO 2026.01.11. 14.44 (Vizsgálati részlelet)
Leletező orvos: Dr. Vámos Izabella
Helyszíni fekvő mellkas felvétel:
A rekesz szabályos helyzetű.
A tüdőkben parenchymás árnyék nem látható.
KRU2 - OKK RAD KC Ultrahang 2. szakrendelés 2026.01.11. 14.46 (Vizsgálati részlelet)
Leletező orvos: Dr. Kiss Dávid
Hepatopathia. Nagy mennyiségű ascites.
Vélemény: Hydrothorax l.s. min. grad.
"""

_TWO_UH_TEXT = """\
KRU2 - OKK RAD KC Ultrahang 2. szakrendelés 2026.01.11. 14.46 (Vizsgálati részlelet)
A bal sinusban folyadék. Hepatopathia.
Vélemény: Hydrothorax.
RKUS - OKK Sürgősségi Ultrahang szakrendelés 2026.01.12. 00.36 (Vizsgálati részlelet)
A véna porta szabad.
"""


class TestImagingParserSynthetic:
    def setup_method(self):
        self.parser = ImagingParser()

    def test_rtg_date_key_present(self):
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "rtg_d-1_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_rtg_date_value(self):
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "rtg_d-1_1_date")
        assert f.value == "2026-01-11"

    def test_rtg_result_key_present(self):
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "rtg_d-1_1_result" in keys

    def test_rtg_result_contains_text(self):
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "rtg_d-1_1_result")
        assert len(f.value) > 10

    def test_uh_day_minus1(self):
        """2026-01-11 → day -1."""
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "uh_d-1_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_two_uh_on_different_days(self):
        """UH on day -1 and day 1 — separate events."""
        doc = _make_doc(_TWO_UH_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "uh_d-1_1_date" in keys, f"Keys: {sorted(keys)}"
        assert "uh_d1_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_source_is_heuristic(self):
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.source == FieldSource.HEURISTIC for f in fields)

    def test_no_day_0(self):
        import re
        doc = _make_doc(_RTG_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        for f in fields:
            m = re.search(r'_d(-?\d+)_', f.key)
            if m:
                assert int(m.group(1)) != 0

    def test_empty_doc_returns_empty(self):
        doc = _make_doc("No imaging here.")
        assert self.parser.parse(doc, ANCHOR) == []


class TestImagingParserFixtures:
    def setup_method(self):
        self.parser = ImagingParser()

    def test_zaro_has_rtg_day_minus1(self):
        doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        if doc is None:
            print("  SKIP: FS_záró.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        # RTG on 2026-01-11 = day -1
        assert "rtg_d-1_1_date" in keys, f"Keys: {sorted(k for k in keys if 'rtg' in k or 'uh' in k)}"

    def test_zaro_has_uh_day_minus1(self):
        doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "uh_d-1_1_date" in keys, f"Keys: {sorted(k for k in keys if 'uh' in k)}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ImagingParser — MRI modality support
# ─────────────────────────────────────────────────────────────────────────────

_MRI_STANDALONE_TEXT = """\
KRMR - OKK RAD KC MR szakrendelés 2026.01.15. 10.00 (Vizsgálati részlelet)
Leletező orvos: Dr. Kovács Péter
Koponya MR natív:
Kóros jelzésfokozódás a jobb temporális lebenyben nem látható.
Vélemény: Demyelinizáció nem igazolódik.
"""

_MRI_IN_DISCHARGE_TEXT = """\
KRU2 - OKK RAD KC Ultrahang 2. szakrendelés 2026.01.11. 14.46 (Vizsgálati részlelet)
Hepatopathia. Nagy mennyiségű ascites.
KRMR - OKK RAD KC MR szakrendelés 2026.01.15. 10.00 (Vizsgálati részlelet)
Koponya MR natív: Kóros jelzésfokozódás nem látható.
Vélemény: Demyelinizáció nem igazolódik.
"""

_MRI_VIZSGALAT_TEXT = """\
MRI vizsgálat 2026.01.18. 12.00
Máj MR vizsgálat kontrasztanyaggal.
Vélemény: Hemangioma a jobb lebenyben.
"""


class TestImagingParserMRI:
    """MRI modality — must be handled identically to UH/RTG/CT."""

    def setup_method(self):
        self.parser = ImagingParser()

    def test_mri_standalone_date_key(self):
        doc = _make_doc(_MRI_STANDALONE_TEXT, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "mri_d4_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_mri_standalone_date_value(self):
        doc = _make_doc(_MRI_STANDALONE_TEXT, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "mri_d4_1_date")
        assert f.value == "2026-01-15"

    def test_mri_standalone_result_present(self):
        doc = _make_doc(_MRI_STANDALONE_TEXT, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "mri_d4_1_result" in keys

    def test_mri_standalone_result_content(self):
        doc = _make_doc(_MRI_STANDALONE_TEXT, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        f = next(f for f in fields if f.key == "mri_d4_1_result")
        assert "demyelinizáció" in f.value.lower() or len(f.value) > 10

    def test_mri_in_discharge_alongside_uh(self):
        doc = _make_doc(_MRI_IN_DISCHARGE_TEXT, DocType.DISCHARGE_SUMMARY)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "uh_d-1_1_date" in keys, f"Keys: {sorted(keys)}"
        assert "mri_d4_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_mri_vizsgalat_header(self):
        """'MRI vizsgálat' header variant."""
        doc = _make_doc(_MRI_VIZSGALAT_TEXT, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        # 2026-01-18 - 2026-01-12 = 6 days → day 7
        assert "mri_d7_1_date" in keys, f"Keys: {sorted(keys)}"

    def test_mri_source_is_heuristic(self):
        doc = _make_doc(_MRI_STANDALONE_TEXT, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.source == FieldSource.HEURISTIC for f in fields)

    def test_mrsa_does_not_false_match(self):
        """MRSA in microbiology text must not be mistaken for MR imaging."""
        text = "Staphylococcus aureus MRSA isolált 2026.01.17 (vér)\n"
        doc = _make_doc(text, DocType.IMAGING)
        fields = self.parser.parse(doc, ANCHOR)
        mri_fields = [f for f in fields if "mri_" in f.key]
        assert mri_fields == [], f"MRSA falsely matched as MRI: {[f.key for f in mri_fields]}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: MicrobiologyParser — synthetic text
# ─────────────────────────────────────────────────────────────────────────────

_MICRO_NEGATIVE_TEXT = """\
MBZ - Bakterológia 2026.01.17 14:51 (ascites)
Gomba tenyésztése negatív.; Baktérium nem tenyészett ki.;
"""

_MICRO_POSITIVE_TEXT = """\
MBZ - Bakterológia 2026.01.17 14:51 (vér)
Staphylococcus aureus MRSA isolált.;
"""

_MICRO_UNKNOWN_DATE_TEXT = """\
Tenyésztés 2026.01.15 (vizelet)
Escherichia coli tenyészett ki.;
"""


class TestMicrobiologyParserSynthetic:
    def setup_method(self):
        self.parser = MicrobiologyParser()

    def test_negative_sample_type_ascites(self):
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "sample_type" in f.key), None)
        assert f is not None, f"Keys: {[f.key for f in fields]}"
        assert f.value == "ascites"

    def test_negative_pathogen_negatív(self):
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "pathogen" in f.key), None)
        assert f is not None
        assert f.value == "negatív"

    def test_negative_multiresistant_false(self):
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "multiresistant" in f.key), None)
        assert f is not None
        assert f.value is False

    def test_negative_day_is_6(self):
        """2026-01-17 = delta=5 → day 6."""
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        assert any("micro_d6_" in f.key for f in fields), (
            f"Keys: {[f.key for f in fields]}"
        )

    def test_negative_key_format(self):
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "micro_d6_1_sample_type" in keys
        assert "micro_d6_1_pathogen" in keys
        assert "micro_d6_1_multiresistant" in keys

    def test_positive_pathogen_staphylococcus(self):
        doc = _make_doc(_MICRO_POSITIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "pathogen" in f.key), None)
        assert f is not None
        assert "staphylococcus" in f.value.lower()

    def test_positive_multiresistant_true(self):
        doc = _make_doc(_MICRO_POSITIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "multiresistant" in f.key), None)
        assert f is not None
        assert f.value is True

    def test_tenyesztes_header_also_detected(self):
        doc = _make_doc(_MICRO_UNKNOWN_DATE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        assert len(fields) > 0, "Tenyésztés header should be detected"

    def test_escherichia_pathogen_detected(self):
        doc = _make_doc(_MICRO_UNKNOWN_DATE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "pathogen" in f.key), None)
        if f:  # only check if field found
            assert "escherichia" in f.value.lower()

    def test_source_is_heuristic(self):
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        assert all(f.source == FieldSource.HEURISTIC for f in fields)

    def test_no_day_0(self):
        import re
        doc = _make_doc(_MICRO_NEGATIVE_TEXT)
        fields = self.parser.parse(doc, ANCHOR)
        for f in fields:
            m = re.search(r'_d(-?\d+)_', f.key)
            if m:
                assert int(m.group(1)) != 0

    def test_empty_doc_returns_empty(self):
        doc = _make_doc("No microbiology here.")
        assert self.parser.parse(doc, ANCHOR) == []


# ── Regression: _FELVETELI_DATUM_RE must exist and work ─────────────────────

_MICRO_WITH_FELVETEL_TEXT = """\
Felvétel dátuma: 2026.01.14
MBZ - Bakterológia 2026.01.14 09:00 (vizelet)
Escherichia coli tenyészett ki.;
"""


class TestFelvetelDatumRE:
    """Regression tests for _FELVETELI_DATUM_RE (was undefined → NameError)."""

    def test_pattern_matches_standard_header(self):
        m = _FELVETELI_DATUM_RE.search("Felvétel dátuma: 2026.01.14")
        assert m is not None
        assert m.group(1) == "2026.01.14"

    def test_pattern_matches_without_accent(self):
        m = _FELVETELI_DATUM_RE.search("Felvetel datuma: 2026.01.14")
        assert m is not None
        assert m.group(1) == "2026.01.14"

    def test_pattern_case_insensitive(self):
        m = _FELVETELI_DATUM_RE.search("FELVÉTEL DÁTUMA: 2026.01.14")
        assert m is not None

    def test_find_date_before_uses_felvetel(self):
        text = "Felvétel dátuma: 2026.01.14\nMBZ - Bakterológia 2026.01.14 09:00"
        pos = text.index("MBZ")
        d = _find_date_before(
            text, pos, preferred_patterns=[_FELVETELI_DATUM_RE],
        )
        assert d == date(2026, 1, 14)

    def test_microbiology_parser_with_felvetel_header(self):
        """MicrobiologyParser should not crash on text with Felvétel dátuma header."""
        parser = MicrobiologyParser()
        doc = _make_doc(_MICRO_WITH_FELVETEL_TEXT)
        fields = parser.parse(doc, ANCHOR)
        # Should produce results (day 3: 2026-01-14 - 2026-01-12 = delta 2, day 3)
        assert len(fields) > 0, "MicrobiologyParser should extract fields"
        assert any("micro_d3_" in f.key for f in fields), (
            f"Expected day 3; keys: {[f.key for f in fields]}"
        )


class TestMicrobiologyParserFixtures:
    def setup_method(self):
        self.parser = MicrobiologyParser()

    def test_zaro_has_microbiology_day6(self):
        """Zárójelentés has MBZ block dated 2026-01-17 → day 6."""
        doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        if doc is None:
            print("  SKIP: FS_záró.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "micro_d6_1_pathogen" in keys, f"Keys: {sorted(k for k in keys if 'micro' in k)}"

    def test_zaro_micro_ascites_sample(self):
        doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        if doc is None:
            return
        fields = self.parser.parse(doc, ANCHOR)
        f = next((f for f in fields if "sample_type" in f.key), None)
        assert f is not None
        assert "ascites" in f.value.lower()

    def test_tenyesztes_fixture_empty(self):
        """Microbiology fixture is fully rasterised — no text, no fields."""
        doc = _load_fixture("FS_tenyésztés.pdf", DocType.MICROBIOLOGY)
        if doc is None:
            print("  SKIP: FS_tenyésztés.pdf not available")
            return
        fields = self.parser.parse(doc, ANCHOR)
        # Rasterised doc has no text → parser returns empty list
        assert fields == [], f"Expected empty; got {[f.key for f in fields]}"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Multiple events on same day — correct n index
# ─────────────────────────────────────────────────────────────────────────────

_TWO_UH_SAME_DAY = """\
KRU2 - Ultrahang szakrendelés 2026.01.11. 09.00 (Vizsgálati részlelet)
Első UH lelet szövege.
KRU3 - Ultrahang szakrendelés 2026.01.11. 14.46 (Vizsgálati részlelet)
Második UH lelet szövege.
"""


class TestMultipleEventsOnSameDay:
    def test_two_uh_same_day_get_n1_and_n2(self):
        parser = ImagingParser()
        doc = _make_doc(_TWO_UH_SAME_DAY)
        fields = parser.parse(doc, ANCHOR)
        keys = {f.key for f in fields}
        assert "uh_d-1_1_date" in keys, f"Keys: {sorted(keys)}"
        assert "uh_d-1_2_date" in keys, f"Keys: {sorted(keys)}"

    def test_two_uh_values_distinct(self):
        parser = ImagingParser()
        doc = _make_doc(_TWO_UH_SAME_DAY)
        fields = parser.parse(doc, ANCHOR)
        d_fields = [f for f in fields if "_date" in f.key and "uh_d-1" in f.key]
        dates = [f.value for f in d_fields]
        # Both should be the same date (2026-01-11)
        assert all(d == "2026-01-11" for d in dates)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: discharge-summary layout (pymupdf4llm markdown, page footers)
# ─────────────────────────────────────────────────────────────────────────────

def _make_pages_doc(*pages: str) -> RoutedDoc:
    return RoutedDoc(
        path=Path("zaro.pdf"),
        doc_type=DocType.DISCHARGE_SUMMARY,
        pages=pages,
        tables=(),
        meta={},
    )


def _footer(n: int) -> str:
    return (f"\n\nKajla Kata - Esetszám: 1234567890 \n\n"
            f"Nyomtatva: UD MED - 2026.03.31 22:33 Oldal {n}/7")


class TestDischargeLayout:
    def test_footer_timestamp_is_not_the_endoscopy_date(self):
        """The header ends one page, the report starts the next: the print
        timestamp in between must not date the gastroscopy (was d79)."""
        doc = _make_pages_doc(
            ". \n\n## **GAD5 - Gasztroenterológiai Klinika, Osztályos endoszkópos labor, "
            "I. emelet. 2026.01.13. 08.00 (Vizsgálati részlelet) Leletező orvos: Dr. X Y**"
            "\n\nIndikáció: Varix? Asszisztencia: Z W" + _footer(2),
            ". \n\nPremedicatio: Lidocain spray Eszköz: Fujinon EG-720R típusú gastroscop\n\n"
            "Endoszkópos lelet: Fundális varicositas látható.\n\n"
            "Vélemény: Fundális varicositas.\n\nJavasolt: NSBB" + _footer(3),
        )
        keys = {f.key for f in EndoscopyParser().parse(doc, ANCHOR)}
        assert "endo_gastro_d2_1_date" in keys, sorted(keys)
        assert not any("_d79_" in k for k in keys), sorted(keys)

    def test_opinion_stops_at_the_next_markdown_section(self):
        doc = _make_pages_doc(
            "## **GAD5 - Gasztroenterológiai Klinika 2026.01.16. 08.00 (Röntgen vizsgálati lelet)**\n\n"
            "Eszköz: colonoscop\n\nEndoszkópos lelet: Normál mucosa.\n\n"
            "Vélemény: Negatív colonoscopos lelet. \n\n"
            "## **2026.01.16 Fibroscan** \n\nMáj 75.0 kPa, IQR 0%, CAP 250 dB/m\n\n"
            "## **SZZ - Kardiológiai Klinika Echokardiográfia 2026.01.19. 10.00 (Ambuláns lap)**\n\n"
            "SpO2: 92-92 %"
        )
        fields = {f.key: f.value for f in EndoscopyParser().parse(doc, ANCHOR)}
        assert fields["endo_colono_d5_1_opinion"] == "Negatív colonoscopos lelet.", fields

    def test_ultrasound_stops_at_the_next_markdown_section(self):
        """uh_d1 used to run on into the gastroscopy and the page footer."""
        doc = _make_pages_doc(
            "## **RKUS - OKK Sürgősségi Ultrahang szakrendelés 2026.01.12. 00.36 "
            "(Vizsgálati részlelet) Leletező orvos: Dr. X Y** \n\n"
            "A véna porta szabad, az áramlás megtartott. \n\n"
            "## **GAD5 - Gasztroenterológiai Klinika, Osztályos endoszkópos labor, "
            "I. emelet. 2026.01.13. 08.00 (Vizsgálati részlelet)** \n\n"
            "Indikáció: Varix?" + _footer(2),
        )
        fields = {f.key: f.value for f in ImagingParser().parse(doc, ANCHOR)}
        assert fields["uh_d1_1_result"] == "A véna porta szabad, az áramlás megtartott.", fields

    def test_wrapped_header_tail_and_reporter_are_not_result_text(self):
        doc = _make_pages_doc(
            "## **KRU2 - OKK RAD KC Ultrahang 2. szakrendelés 2026.01.11. 14.46 (Vizsgálati** \n\n"
            "## **részlelet) Leletező orvos: Dr. Kiss Dávid** \n\n"
            "A bal sinusban 12 mm folyadék. \n\nVélemény: Hydrothorax. Nagy mennyiségű ascites."
        )
        fields = {f.key: f.value for f in ImagingParser().parse(doc, ANCHOR)}
        result = fields["uh_d-1_1_result"]
        assert result.startswith("A bal sinusban"), repr(result)
        assert "Kiss" not in result and "Leletező" not in result, repr(result)

    def test_reporter_line_removed_from_plain_layout(self):
        doc = _make_doc(_RTG_TEXT)
        fields = {f.key: f.value for f in ImagingParser().parse(doc, ANCHOR)}
        assert "Vámos" not in fields["rtg_d-1_1_result"], fields["rtg_d-1_1_result"]

    def test_long_report_is_not_cut_at_1000_characters(self):
        body = " ".join(["A máj mérete csökkent, felszíne egyenetlen."] * 30)  # ~1300 chars
        doc = _make_pages_doc(
            "## **KRU2 - OKK RAD KC Ultrahang 2. szakrendelés 2026.01.11. 14.46 (Vizsgálati részlelet)** \n\n"
            + body + " Vélemény: Nagy mennyiségű ascites."
        )
        fields = {f.key: f.value for f in ImagingParser().parse(doc, ANCHOR)}
        assert fields["uh_d-1_1_result"].endswith("Nagy mennyiségű ascites."), \
            fields["uh_d-1_1_result"][-60:]

    def test_culture_result_line_is_not_a_second_culture(self):
        """'Gomba tenyésztése negatív.' used to open a second record (micro_d6
        Unknown/Unknown) while the real one took the echo date above it (d8)."""
        doc = _make_pages_doc(
            "## **SZZ - Kardiológiai Klinika Echokardiográfia 2026.01.19. 10.00 (Ambuláns lap)** \n\n"
            "EF:66%. \n\n"
            "## **MBZ - Bakterológia 2026.01.17 14:51 (ascites)** \n\n"
            "Gomba tenyésztése negatív.; Baktérium nem tenyészett ki.; Minta:hemokultura,"
        )
        fields = {f.key: f.value for f in MicrobiologyParser().parse(doc, ANCHOR)}
        assert fields == {
            "micro_d6_1_sample_type": "ascites",
            "micro_d6_1_pathogen": "negatív",
            "micro_d6_1_multiresistant": False,
        }, fields


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestEventColKey,
        TestNextEventN,
        TestDateHelpers,
        TestEndoscopyParserSynthetic,
        TestEndoscopyParserFixtures,
        TestFibroscanParserSynthetic,
        TestFibroscanParserFixtures,
        TestImagingParserSynthetic,
        TestImagingParserMRI,
        TestImagingParserFixtures,
        TestMicrobiologyParserSynthetic,
        TestFelvetelDatumRE,
        TestMicrobiologyParserFixtures,
        TestMultipleEventsOnSameDay,
        TestDischargeLayout,
    ]

    passed = failed = 0
    for cls in all_classes:
        instance = cls()
        methods = sorted(m for m in dir(cls) if m.startswith("test_"))
        for method_name in methods:
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
    if failed:
        print(f"  FAILED:  {failed}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
