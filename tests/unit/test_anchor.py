"""
tests/unit/test_anchor.py

Step 6 tests: patient anchor extraction (app/pipeline/step3_anchor.py).

Tests cover:
  - Extraction from a real fixture zárójelentés (admission + discharge dates)
  - Missing admission date → AnchorNotFoundError raised
  - Missing discharge date → defaults to admission_start, no raise
  - Fingerprint is deterministic (same inputs → same hash, always 12 chars)
  - Name / TAJ / DOB extraction from well-formed text
  - Fallback placeholders for missing name / TAJ / DOB
  - Date format variants (with/without HH:MM, spaced Hungarian format)
  - Anonymised fixture values produce valid (non-crashing) anchor
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from pathlib import Path

from app.pipeline.step3_anchor import (
    AnchorNotFoundError,
    _extract_dob,
    _extract_name,
    _extract_taj,
    _parse_datetime_field,
    _RE_ADMISSION,
    _RE_DISCHARGE,
    extract_anchor,
)
from app.pipeline.step1_ingest import ingest
from app.domain.enums import DocType
from app.domain.models import PatientAnchor, RoutedDoc

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
ZARO_FIXTURE = FIXTURES_DIR / "FS_zaro.pdf"

# ── Representative text snippets ───────────────────────────────────────────────

_FULL_ZARO_TEXT = """
ZÁRÓJELENTÉS
Név: Kovács János
Születési hely és dátum: Budapest, 1960. 03. 15.
TAJ: 123-456-789
Felvétel dátuma: 2026.01.12 04:43
Elbocsátás dátuma: 2026.01.20 13:32
Záródiagnózis: Máj cirrózis
"""

_NO_ADMISSION_TEXT = """
ZÁRÓJELENTÉS
Név: Kovács János
TAJ: 123-456-789
Elbocsátás dátuma: 2026.01.20 13:32
"""

_NO_DISCHARGE_TEXT = """
ZÁRÓJELENTÉS
Név: Kovács János
Születési hely és dátum: Budapest, 1960. 03. 15.
TAJ: 123-456-789
Felvétel dátuma: 2026.01.12 04:43
"""

_NO_NAME_TEXT = """
ZÁRÓJELENTÉS
Születési hely és dátum: Budapest, 1960. 03. 15.
TAJ: 123-456-789
Felvétel dátuma: 2026.01.12 04:43
Elbocsátás dátuma: 2026.01.20 13:32
"""

_NO_TAJ_TEXT = """
ZÁRÓJELENTÉS
Név: Kovács János
Születési hely és dátum: Budapest, 1960. 03. 15.
Felvétel dátuma: 2026.01.12 04:43
Elbocsátás dátuma: 2026.01.20 13:32
"""

_NO_DOB_TEXT = """
ZÁRÓJELENTÉS
Név: Kovács János
TAJ: 123-456-789
Felvétel dátuma: 2026.01.12 04:43
Elbocsátás dátuma: 2026.01.20 13:32
"""

_DATE_NO_TIME_TEXT = """
ZÁRÓJELENTÉS
Felvétel dátuma: 2026.01.12
Elbocsátás dátuma: 2026.01.20
"""

_COMPACT_DOB_TEXT = """
ZÁRÓJELENTÉS
Név: Teszt Béla
Születési hely és dátum: Miskolc, 1975.06.25.
TAJ: 987654321
Felvétel dátuma: 2026.02.01 08:00
Elbocsátás dátuma: 2026.02.10 12:00
"""


def _make_doc(text: str, path: Path | None = None) -> RoutedDoc:
    """Create a minimal RoutedDoc for anchor extraction tests."""
    if path is None:
        path = Path("test_zaro.pdf")
    return RoutedDoc(
        path=path,
        doc_type=DocType.DISCHARGE_SUMMARY,
        pages=(text,),
        tables=(),
        meta={},
    )


# ─────────────────────────────────────────────────────────────────────────────
# _parse_datetime_field
# ─────────────────────────────────────────────────────────────────────────────

class TestParseDatetimeField:
    def test_admission_with_time(self):
        dt = _parse_datetime_field(
            "Felvétel dátuma: 2026.01.12 04:43",
            _RE_ADMISSION,
            "Felvétel dátuma",
        )
        assert dt == datetime(2026, 1, 12, 4, 43)

    def test_admission_without_time(self):
        dt = _parse_datetime_field(
            "Felvétel dátuma: 2026.01.12",
            _RE_ADMISSION,
            "Felvétel dátuma",
        )
        assert dt == datetime(2026, 1, 12, 0, 0)

    def test_discharge_with_time(self):
        dt = _parse_datetime_field(
            "Elbocsátás dátuma: 2026.01.20 13:32",
            _RE_DISCHARGE,
            "Elbocsátás dátuma",
        )
        assert dt == datetime(2026, 1, 20, 13, 32)

    def test_no_match_returns_none(self):
        dt = _parse_datetime_field(
            "Ez egy véletlen szöveg.",
            _RE_ADMISSION,
            "Felvétel dátuma",
        )
        assert dt is None

    def test_single_digit_month_day(self):
        dt = _parse_datetime_field(
            "Felvétel dátuma: 2026.1.5 09:00",
            _RE_ADMISSION,
            "Felvétel dátuma",
        )
        assert dt == datetime(2026, 1, 5, 9, 0)

    def test_invalid_date_returns_none(self):
        # month=13 is invalid
        dt = _parse_datetime_field(
            "Felvétel dátuma: 2026.13.01 00:00",
            _RE_ADMISSION,
            "Felvétel dátuma",
        )
        assert dt is None


# ─────────────────────────────────────────────────────────────────────────────
# _extract_name / _extract_taj / _extract_dob
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractIdentityFields:
    def test_name_extracted(self):
        name = _extract_name("Név: Kovács János", Path("x.pdf"))
        assert name == "Kovács János"

    def test_name_stripped(self):
        name = _extract_name("Név:   Nagy Béla  ", Path("x.pdf"))
        assert name == "Nagy Béla"

    def test_name_missing_returns_placeholder(self):
        name = _extract_name("Ez a szöveg nem tartalmaz nevet.", Path("x.pdf"))
        assert name == "UNKNOWN"

    def test_taj_digits_only(self):
        taj = _extract_taj("TAJ: 123-456-789", Path("x.pdf"))
        assert taj == "123456789"

    def test_taj_with_spaces(self):
        taj = _extract_taj("TAJ: 123 456 789", Path("x.pdf"))
        assert taj == "123456789"

    def test_taj_anonymised_preserved(self):
        """Anonymised TAJ (xxx-xxx-xxx) should produce 'xxx...' not raise."""
        taj = _extract_taj("TAJ: xxx-xxx-xxx", Path("x.pdf"))
        assert "x" in taj   # placeholder x characters preserved

    def test_taj_missing_returns_placeholder(self):
        taj = _extract_taj("Ez a szöveg nem tartalmaz TAJ számot.", Path("x.pdf"))
        assert taj == "000000000"

    def test_dob_hungarian_spaced(self):
        dob = _extract_dob(
            "Születési hely és dátum: Budapest, 1960. 03. 15.",
            Path("x.pdf"),
        )
        assert dob == date(1960, 3, 15)

    def test_dob_compact(self):
        dob = _extract_dob(
            "Születési hely és dátum: Miskolc, 1975.06.25.",
            Path("x.pdf"),
        )
        assert dob == date(1975, 6, 25)

    def test_dob_missing_returns_placeholder(self):
        dob = _extract_dob("Nincs születési dátum.", Path("x.pdf"))
        assert dob == date(1900, 1, 1)

    def test_dob_single_digit_month_day(self):
        dob = _extract_dob(
            "Születési hely és dátum: Pécs, 1988. 7. 4.",
            Path("x.pdf"),
        )
        assert dob == date(1988, 7, 4)


# ─────────────────────────────────────────────────────────────────────────────
# extract_anchor() — full extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractAnchor:
    def test_full_extraction(self):
        doc = _make_doc(_FULL_ZARO_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.name == "Kovács János"
        assert anchor.dob == date(1960, 3, 15)
        assert anchor.taj == "123456789"
        assert anchor.admission_start == datetime(2026, 1, 12, 4, 43)
        assert anchor.discharge_end == datetime(2026, 1, 20, 13, 32)

    def test_returns_patient_anchor_instance(self):
        doc = _make_doc(_FULL_ZARO_TEXT)
        anchor = extract_anchor(doc)
        assert isinstance(anchor, PatientAnchor)

    def test_missing_admission_raises(self):
        doc = _make_doc(_NO_ADMISSION_TEXT)
        raised = False
        try:
            extract_anchor(doc)
        except AnchorNotFoundError:
            raised = True
        assert raised, "Expected AnchorNotFoundError for missing admission date"

    def test_anchor_not_found_error_is_informative(self):
        doc = _make_doc(_NO_ADMISSION_TEXT, path=Path("my_doc.pdf"))
        try:
            extract_anchor(doc)
            assert False, "Should have raised"
        except AnchorNotFoundError as e:
            assert "my_doc.pdf" in str(e) or "Felvétel" in str(e)

    def test_missing_discharge_defaults_to_admission(self):
        doc = _make_doc(_NO_DISCHARGE_TEXT)
        anchor = extract_anchor(doc)
        # Should not raise; discharge_end defaults to admission_start
        assert anchor.discharge_end == anchor.admission_start

    def test_missing_name_uses_placeholder(self):
        doc = _make_doc(_NO_NAME_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.name == "UNKNOWN"

    def test_missing_taj_uses_placeholder(self):
        doc = _make_doc(_NO_TAJ_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.taj == "000000000"

    def test_missing_dob_uses_placeholder(self):
        doc = _make_doc(_NO_DOB_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.dob == date(1900, 1, 1)

    def test_dates_without_time(self):
        doc = _make_doc(_DATE_NO_TIME_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.admission_start == datetime(2026, 1, 12, 0, 0)
        assert anchor.discharge_end == datetime(2026, 1, 20, 0, 0)

    def test_compact_dob_format(self):
        doc = _make_doc(_COMPACT_DOB_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.dob == date(1975, 6, 25)

    def test_admission_date_properties(self):
        doc = _make_doc(_FULL_ZARO_TEXT)
        anchor = extract_anchor(doc)
        assert anchor.admission_date == date(2026, 1, 12)
        assert anchor.discharge_date == date(2026, 1, 20)

    def test_multipage_doc_first_page_scanned(self):
        """Admission date on page 2 should still be found via full_text."""
        doc = RoutedDoc(
            path=Path("test.pdf"),
            doc_type=DocType.DISCHARGE_SUMMARY,
            pages=(
                "ZÁRÓJELENTÉS\nNév: Teszt Béla",
                "TAJ: 111222333\nFelvétel dátuma: 2026.03.01 10:00\nElbocsátás dátuma: 2026.03.08 09:00",
            ),
            tables=(),
            meta={},
        )
        anchor = extract_anchor(doc)
        assert anchor.admission_start == datetime(2026, 3, 1, 10, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint
# ─────────────────────────────────────────────────────────────────────────────

class TestFingerprint:
    def test_fingerprint_is_12_chars(self):
        doc = _make_doc(_FULL_ZARO_TEXT)
        anchor = extract_anchor(doc)
        assert len(anchor.fingerprint) == 12

    def test_fingerprint_is_deterministic(self):
        """Same name + dob + taj must always produce the same fingerprint."""
        fp1 = PatientAnchor.make_fingerprint("Kovács János", date(1960, 3, 15), "123456789")
        fp2 = PatientAnchor.make_fingerprint("Kovács János", date(1960, 3, 15), "123456789")
        assert fp1 == fp2

    def test_different_names_different_fingerprints(self):
        fp1 = PatientAnchor.make_fingerprint("Kovács János", date(1960, 3, 15), "123456789")
        fp2 = PatientAnchor.make_fingerprint("Nagy Béla",   date(1960, 3, 15), "123456789")
        assert fp1 != fp2

    def test_different_dobs_different_fingerprints(self):
        fp1 = PatientAnchor.make_fingerprint("Kovács János", date(1960, 3, 15), "123456789")
        fp2 = PatientAnchor.make_fingerprint("Kovács János", date(1961, 3, 15), "123456789")
        assert fp1 != fp2

    def test_different_taj_different_fingerprints(self):
        fp1 = PatientAnchor.make_fingerprint("Kovács János", date(1960, 3, 15), "123456789")
        fp2 = PatientAnchor.make_fingerprint("Kovács János", date(1960, 3, 15), "987654321")
        assert fp1 != fp2

    def test_fingerprint_is_hexadecimal(self):
        fp = PatientAnchor.make_fingerprint("Test", date(2000, 1, 1), "000000000")
        assert all(c in "0123456789abcdef" for c in fp)

    def test_fingerprint_same_from_extract_anchor(self):
        """extract_anchor produces the same fingerprint as make_fingerprint."""
        doc = _make_doc(_FULL_ZARO_TEXT)
        anchor = extract_anchor(doc)
        expected = PatientAnchor.make_fingerprint(
            "Kovács János", date(1960, 3, 15), "123456789"
        )
        assert anchor.fingerprint == expected

    def test_taj_formatting_normalised_for_fingerprint(self):
        """TAJ with dashes and without should produce the same fingerprint."""
        fp_dashes = PatientAnchor.make_fingerprint("A", date(2000, 1, 1), "123-456-789")
        fp_digits  = PatientAnchor.make_fingerprint("A", date(2000, 1, 1), "123456789")
        # make_fingerprint strips non-digits from TAJ
        assert fp_dashes == fp_digits


# ─────────────────────────────────────────────────────────────────────────────
# Integration: real fixture zárójelentés
# ─────────────────────────────────────────────────────────────────────────────

class TestAnchorFromFixture:
    def _require_zaro(self) -> RoutedDoc:
        if not ZARO_FIXTURE.exists():
            try:
                import pytest
                pytest.skip(f"Fixture not found: {ZARO_FIXTURE}")
            except ImportError:
                raise RuntimeError(f"Fixture not found (skip): {ZARO_FIXTURE}")
        return ingest(ZARO_FIXTURE)

    def test_fixture_anchor_no_crash(self):
        """Anonymised fixture must not raise (missing fields → placeholders)."""
        doc = self._require_zaro()
        try:
            anchor = extract_anchor(doc)
            assert isinstance(anchor, PatientAnchor)
        except AnchorNotFoundError as e:
            raise AssertionError(f"AnchorNotFoundError on fixture: {e}") from e

    def test_fixture_admission_date_correct(self):
        """The fixture has 'Felvétel dátuma: 2026.01.12 04:43'."""
        doc = self._require_zaro()
        anchor = extract_anchor(doc)
        assert anchor.admission_start == datetime(2026, 1, 12, 4, 43)

    def test_fixture_discharge_date_correct(self):
        """The fixture has 'Elbocsátás dátuma: 2026.01.20 13:32'."""
        doc = self._require_zaro()
        anchor = extract_anchor(doc)
        assert anchor.discharge_end == datetime(2026, 1, 20, 13, 32)

    def test_fixture_fingerprint_stable(self):
        """Fingerprint must be 12 hex chars and stable across calls."""
        doc = self._require_zaro()
        a1 = extract_anchor(doc)
        a2 = extract_anchor(doc)
        assert a1.fingerprint == a2.fingerprint
        assert len(a1.fingerprint) == 12

    def test_fixture_admission_date_property(self):
        doc = self._require_zaro()
        anchor = extract_anchor(doc)
        assert anchor.admission_date == date(2026, 1, 12)

    def test_fixture_discharge_date_property(self):
        doc = self._require_zaro()
        anchor = extract_anchor(doc)
        assert anchor.discharge_date == date(2026, 1, 20)

    def test_fixture_discharge_after_admission(self):
        doc = self._require_zaro()
        anchor = extract_anchor(doc)
        assert anchor.discharge_end > anchor.admission_start


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestParseDatetimeField,
        TestExtractIdentityFields,
        TestExtractAnchor,
        TestFingerprint,
        TestAnchorFromFixture,
    ]

    passed = failed = skipped = 0
    for cls in all_classes:
        instance = cls()
        methods = sorted(m for m in dir(cls) if m.startswith("test_"))
        for method_name in methods:
            method = getattr(instance, method_name)
            try:
                method()
                passed += 1
                print(f"  PASS  {cls.__name__}.{method_name}")
            except (RuntimeError, AssertionError) as e:
                msg = str(e)
                if "skip" in msg.lower() or "Fixture not found" in msg:
                    skipped += 1
                    print(f"  SKIP  {cls.__name__}.{method_name}: {e}")
                else:
                    failed += 1
                    print(f"  FAIL  {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR {cls.__name__}.{method_name}: {e}")
                traceback.print_exc()

    print(f"\n{'='*60}")
    print(f"  PASSED:  {passed}/{passed + failed + skipped}")
    if skipped:
        print(f"  SKIPPED: {skipped}")
    if failed:
        print(f"  FAILED:  {failed}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
