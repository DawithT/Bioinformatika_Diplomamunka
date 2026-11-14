"""
tests/unit/test_route.py

Step 5 tests: document router (app/pipeline/step2_route.py).

Tests cover:
  - Each DocType classified correctly from representative text snippets.
  - FIBROSCAN text is NOT mis-classified as IMAGING.
  - Unknown / empty document → DocType.UNKNOWN, no exception.
  - Filename fallback for rasterised (text-empty) scans.
  - route() returns new RoutedDoc with correct doc_type.
  - Real fixture files classify to expected DocType.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Imports under test
# ─────────────────────────────────────────────────────────────────────────────
from app.pipeline.step2_route import classify, classify_by_filename, route
from app.pipeline.step1_ingest import ingest
from app.domain.enums import DocType
from app.domain.models import RoutedDoc

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

FIXTURE_EXPECTED: dict[str, DocType] = {
    "FS_zaro.pdf":       DocType.DISCHARGE_SUMMARY,
    "FS_labor2.pdf":     DocType.LAB_REPORT,
    "FS_fibro.pdf":      DocType.ELASTOGRAPHY,
    "FS_endogastro.pdf": DocType.ENDOSCOPY,
    "FS_endocolono.pdf": DocType.ENDOSCOPY,
    "FS_tenyesztes.pdf": DocType.MICROBIOLOGY,  # rasterised — filename fallback
}


# ─────────────────────────────────────────────────────────────────────────────
# Representative text snippets for each DocType
# ─────────────────────────────────────────────────────────────────────────────

DISCHARGE_TEXT = """
ZÁRÓJELENTÉS
Név: Beteg Neve
TAJ: xxx-xxx-xxx
Felvétel dátuma: 2026.01.12 04:43
Elbocsátás dátuma: 2026.01.20 13:32
Záródiagnózis: Máj cirrózis
Kezelési összefoglaló: A beteg 8 napos kórházi kezelésben részesült.
"""

LAB_TEXT = """
Laborvizsgálati eredmények (kérés azonosító: xxxxxxxxxxxxxx)
Közpointi Laboratórium Megjelenés ideje: 2026. 01. 19. 08:00:00
Vizsgálat Eredmény Ref.tart. M.egység
Fehérvérsejt szám 5.87 4.50-10.80 giga/L
Vörösvértest szám 2.62 4.70-6.10 tera/L
Hemoglobin 97 130-175 g/L
Trombocita 88 150-400 giga/L
Minta dátuma: 2026.01.16 17:10
"""

ELASTOGRAPHY_TEXT = """
AMBULÁNS KEZELŐLAP
Vizsgálatok eredményei:
Fibroscan:
Máj
FibroScan Expert : E: 75.0 kPa, CAP:250 dB/m IQR/Med:0 %
Steatosis grade: S0
Fibrosis stádium: F4
Tranziens elasztográfia
"""

ENDOSCOPY_GASTRO_TEXT = """
AMBULÁNS KEZELŐLAP
endoszkópos labor, I. em.
Gastroscopia
Korábbi endoszkópia: 2019 PHG
Endoszkópos lelet: Nyelőcső varixok II. fokozat.
Vélemény: Portalis hypertenzív gastropathia.
Premedicatio: Lidocain spray
Asszisztencia: Asszisztens Neve
"""

ENDOSCOPY_COLONO_TEXT = """
AMBULÁNS KEZELŐLAP
endoszkópos labor, I. em.
COLONOSCOPIA
Indikáció: anaemia
Asszisztencia: Asszisztens Neve
Premedicatio: remimazolam, tramadol
Eszköz: Fujinon EC-720R típusú colonoscop
Endoszkópos lelet: Az előkészítés megfelelő.
Vélemény: Negatív colonoscopos lelet.
"""

MICROBIOLOGY_TEXT = """
Mikrobiológiai Laboratórium
Tenyésztés eredménye:
Vizsgált minta: Vizelet
Kórokozó: Escherichia coli
Érzékenység / Rezisztencia:
Antibiotikum: Amoxicillin  MIC: 2  Érzékeny
CFU: >100 000/mL
"""

HISTOLOGY_TEXT = """
Patológiai Osztály
Szövettani lelet
Biopszia típusa: Máj core biopszia
Hisztológiai diagnózis: Fibrotikus elváltozás
Preparátum kódja: P-2026-001
"""

IMAGING_TEXT = """
Ultrahang vizsgálat
Hasi ultrahang lelet
A máj echogenitása fokozott, parenchyma inhomogén.
A lép megnagyobbodott (lép).
"""

UNKNOWN_TEXT = """
Ez egy általános szöveg, amelyből nem derül ki, hogy milyen dokumentumtípusról van szó.
Nincs benne semmilyen jellemző kulcsszó.
"""


# ─────────────────────────────────────────────────────────────────────────────
# classify() — text-based classification
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyByText:
    def test_discharge_summary(self):
        assert classify(DISCHARGE_TEXT) == DocType.DISCHARGE_SUMMARY

    def test_lab_report(self):
        assert classify(LAB_TEXT) == DocType.LAB_REPORT

    def test_fibroscan(self):
        assert classify(ELASTOGRAPHY_TEXT) == DocType.ELASTOGRAPHY

    def test_endoscopy_gastroscopy(self):
        assert classify(ENDOSCOPY_GASTRO_TEXT) == DocType.ENDOSCOPY

    def test_endoscopy_colonoscopy(self):
        assert classify(ENDOSCOPY_COLONO_TEXT) == DocType.ENDOSCOPY

    def test_microbiology(self):
        assert classify(MICROBIOLOGY_TEXT) == DocType.MICROBIOLOGY

    def test_histology(self):
        assert classify(HISTOLOGY_TEXT) == DocType.HISTOLOGY

    def test_imaging(self):
        assert classify(IMAGING_TEXT) == DocType.IMAGING

    def test_unknown_text_returns_unknown(self):
        assert classify(UNKNOWN_TEXT) == DocType.UNKNOWN

    def test_empty_string_returns_unknown(self):
        assert classify("") == DocType.UNKNOWN

    def test_whitespace_only_returns_unknown(self):
        assert classify("   \n\t  ") == DocType.UNKNOWN

    def test_never_raises(self):
        """classify() must not raise on any input."""
        for text in [None.__class__.__name__, "x" * 10_000, "\x00\xFF\xAB"]:
            try:
                result = classify(text)
                assert isinstance(result, DocType)
            except Exception as exc:
                raise AssertionError(f"classify raised: {exc}") from exc


class TestAmbulanslapClassification:
    """
    Regression tests: 'Ambuláns kezelőlap' documents must be classified as
    AMBULANSLAP (→ ancillary) unless they genuinely carry modality-specific
    report content (fibroscan / endoscopy / imaging results).
    """

    def test_plain_title(self):
        text = "AMBULÁNS KEZELŐLAP\nGasztroenterológia szakambulancia\nAnamnézis: ..."
        assert classify(text) == DocType.AMBULANSLAP

    def test_mojibake_o_tilde_title(self):
        """Latin-1 mojibake (ő→õ) in the text layer must still classify."""
        text = "AMBULÁNS KEZELÕLAP\nGasztroenterológia\nVélemény: stabil."
        assert classify(text) == DocType.AMBULANSLAP

    def test_letter_spaced_title(self):
        text = "A M B U L Á N S   K E Z E L Ő L A P\nGasztroenterológia\nVélemény: stabil."
        assert classify(text) == DocType.AMBULANSLAP

    def test_velemeny_section_alone_is_not_endoscopy(self):
        """'Vélemény:' is a generic section header, not an endoscopy marker."""
        text = "AMBULÁNS KEZELŐLAP\nHepatológia\nVélemény: kompenzált cirrhosis."
        assert classify(text) == DocType.AMBULANSLAP

    def test_felvetel_datuma_header_does_not_steal(self):
        """Ambuláns sheets carrying a 'Felvétel dátuma' header line must not
        be classified as DISCHARGE_SUMMARY."""
        text = "AMBULÁNS KEZELŐLAP\nFelvétel dátuma: 2025.03.12\nVélemény: jó állapot."
        assert classify(text) == DocType.AMBULANSLAP

    def test_incidental_imaging_recommendation_stays_ambulanslap(self):
        text = (
            "AMBULÁNS KEZELŐLAP\nGasztroenterológia\n"
            "Vélemény: kompenzált cirrhosis.\n"
            "Javaslat: Kontroll hasi ultrahang vizsgálat 6 hónap múlva."
        )
        assert classify(text) == DocType.AMBULANSLAP

    def test_real_fibroscan_report_with_ambulans_header_wins(self):
        """A genuine modality report on an Ambuláns kezelőlap form must still
        route to its modality (that's where the extractable data lives)."""
        assert classify(ELASTOGRAPHY_TEXT) == DocType.ELASTOGRAPHY

    def test_real_endoscopy_report_with_ambulans_header_wins(self):
        assert classify(ENDOSCOPY_GASTRO_TEXT) == DocType.ENDOSCOPY

    def test_zarojelentes_still_definitive_over_ambulans_title(self):
        text = "ZÁRÓJELENTÉS\nAmbuláns kezelőlap melléklet\nZáródiagnózis: cirrhosis."
        assert classify(text) == DocType.DISCHARGE_SUMMARY

    def test_mojibake_filename_fallback(self):
        assert classify_by_filename(Path("ambulans_kezelõlap_3.pdf")) == DocType.AMBULANSLAP


class TestFibroscanNotMisclassifiedAsImaging:
    """
    Critical safety: FibroScan reports must NEVER be classified as IMAGING.
    FibroScan contains ultrasound/imaging vocabulary but is a distinct DocType.
    """

    def test_fibroscan_text_not_imaging(self):
        result = classify(ELASTOGRAPHY_TEXT)
        assert result != DocType.IMAGING, (
            "FIBROSCAN text mis-classified as IMAGING — check rule ordering!"
        )

    def test_fibroscan_text_is_fibroscan(self):
        result = classify(ELASTOGRAPHY_TEXT)
        assert result == DocType.ELASTOGRAPHY

    def test_fibroscan_with_kpa_and_imaging_terms(self):
        """Even if imaging terms appear in a fibroscan report, FIBROSCAN wins."""
        mixed_text = """
        FibroScan Expert : E: 42.0 kPa, CAP:220 dB/m IQR/Med:5 %
        Steatosis grade: S1
        Fibrosis stádium: F3
        Ultrahang vezérelt vizsgálat.
        Máj parenchyma echogenitás.
        """
        result = classify(mixed_text)
        assert result == DocType.ELASTOGRAPHY, (
            f"Expected FIBROSCAN, got {result} — FIBROSCAN rule must precede IMAGING"
        )

    def test_fibroscan_keywords_high_weight(self):
        """Minimal fibroscan text should still beat IMAGING classification."""
        minimal = "FibroScan Expert : E: 12.0 kPa"
        assert classify(minimal) == DocType.ELASTOGRAPHY


# ─────────────────────────────────────────────────────────────────────────────
# classify_by_filename()
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyByFilename:
    def test_zaro_filename(self):
        assert classify_by_filename(Path("FS_zaro.pdf")) == DocType.DISCHARGE_SUMMARY

    def test_labor_filename(self):
        assert classify_by_filename(Path("FS_labor2.pdf")) == DocType.LAB_REPORT

    def test_fibro_filename(self):
        assert classify_by_filename(Path("FS_fibro.pdf")) == DocType.ELASTOGRAPHY

    def test_endogastro_filename(self):
        assert classify_by_filename(Path("FS_endogastro.pdf")) == DocType.ENDOSCOPY

    def test_endocolono_filename(self):
        assert classify_by_filename(Path("FS_endocolono.pdf")) == DocType.ENDOSCOPY

    def test_tenyesztes_filename(self):
        assert classify_by_filename(Path("FS_tenyesztes.pdf")) == DocType.MICROBIOLOGY

    def test_unknown_filename(self):
        assert classify_by_filename(Path("document_xyz_123.pdf")) == DocType.UNKNOWN

    def test_empty_filename(self):
        assert classify_by_filename(Path(".pdf")) == DocType.UNKNOWN

    def test_case_insensitive(self):
        # Filename matching should be case-insensitive on the stem
        assert classify_by_filename(Path("FS_FIBRO.pdf")) == DocType.ELASTOGRAPHY


# ─────────────────────────────────────────────────────────────────────────────
# route()
# ─────────────────────────────────────────────────────────────────────────────

class TestRoute:
    def _make_doc(self, text: str, path: Path | None = None) -> RoutedDoc:
        if path is None:
            path = Path("test_doc.pdf")
        return RoutedDoc(
            path=path,
            doc_type=DocType.UNKNOWN,
            pages=(text,) if text else (),
            tables=(),
            meta={},
        )

    def test_route_discharge(self):
        doc = self._make_doc(DISCHARGE_TEXT)
        result = route(doc)
        assert result.doc_type == DocType.DISCHARGE_SUMMARY

    def test_route_lab(self):
        doc = self._make_doc(LAB_TEXT)
        result = route(doc)
        assert result.doc_type == DocType.LAB_REPORT

    def test_route_fibroscan(self):
        doc = self._make_doc(ELASTOGRAPHY_TEXT)
        result = route(doc)
        assert result.doc_type == DocType.ELASTOGRAPHY

    def test_route_endoscopy(self):
        doc = self._make_doc(ENDOSCOPY_GASTRO_TEXT)
        result = route(doc)
        assert result.doc_type == DocType.ENDOSCOPY

    def test_route_returns_new_routeddoc(self):
        """route() must return a new instance, not mutate the input."""
        doc = self._make_doc(DISCHARGE_TEXT)
        result = route(doc)
        assert result is not doc
        assert doc.doc_type == DocType.UNKNOWN  # original unchanged
        assert result.doc_type == DocType.DISCHARGE_SUMMARY

    def test_route_preserves_pages(self):
        doc = self._make_doc(LAB_TEXT)
        result = route(doc)
        assert result.pages == doc.pages

    def test_route_preserves_tables(self):
        doc = RoutedDoc(
            path=Path("x.pdf"),
            doc_type=DocType.UNKNOWN,
            pages=(LAB_TEXT,),
            tables=(( ("A", "B"), ("1", "2") ),),
            meta={},
        )
        result = route(doc)
        assert result.tables == doc.tables

    def test_route_empty_text_uses_filename_fallback(self):
        """When text is empty, route() should fall back to filename classification."""
        doc = RoutedDoc(
            path=Path("FS_fibro.pdf"),
            doc_type=DocType.UNKNOWN,
            pages=(),        # empty — rasterised scan
            tables=(),
            meta={},
        )
        result = route(doc)
        assert result.doc_type == DocType.ELASTOGRAPHY

    def test_route_empty_text_unknown_filename_gives_unknown(self):
        doc = RoutedDoc(
            path=Path("mystery_document.pdf"),
            doc_type=DocType.UNKNOWN,
            pages=(),
            tables=(),
            meta={},
        )
        result = route(doc)
        assert result.doc_type == DocType.UNKNOWN

    def test_route_never_raises(self):
        """route() must not raise for any RoutedDoc input."""
        docs = [
            RoutedDoc(Path("x.pdf"), DocType.UNKNOWN, (), (), {}),
            RoutedDoc(Path("x.pdf"), DocType.UNKNOWN, ("",), (), {}),
            RoutedDoc(Path("x.pdf"), DocType.UNKNOWN, ("x" * 50_000,), (), {}),
        ]
        for doc in docs:
            try:
                result = route(doc)
                assert isinstance(result, RoutedDoc)
            except Exception as exc:
                raise AssertionError(f"route() raised: {exc}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Integration: ingest → route with real fixture files
# ─────────────────────────────────────────────────────────────────────────────

class TestRouteFixtures:
    """
    End-to-end: ingest() then route() each fixture and verify the expected DocType.
    Skipped if fixture is absent.
    """

    def _require(self, filename: str) -> Path:
        p = FIXTURES_DIR / filename
        if not p.exists():
            try:
                import pytest
                pytest.skip(f"Fixture not found: {p}")
            except ImportError:
                raise RuntimeError(f"Fixture not found (skip): {p}")
        return p

    def _classify_fixture(self, filename: str) -> DocType:
        p = self._require(filename)
        doc = ingest(p)
        routed = route(doc)
        return routed.doc_type

    def test_zaro_is_discharge_summary(self):
        result = self._classify_fixture("FS_zaro.pdf")
        assert result == DocType.DISCHARGE_SUMMARY, f"Got {result}"

    def test_labor_is_lab_report(self):
        result = self._classify_fixture("FS_labor2.pdf")
        assert result == DocType.LAB_REPORT, f"Got {result}"

    def test_fibro_is_fibroscan(self):
        result = self._classify_fixture("FS_fibro.pdf")
        assert result == DocType.ELASTOGRAPHY, f"Got {result}"

    def test_fibro_is_not_imaging(self):
        """Critical: fibroscan fixture must never classify as IMAGING."""
        result = self._classify_fixture("FS_fibro.pdf")
        assert result != DocType.IMAGING, (
            "FS_fibro.pdf classified as IMAGING — FIBROSCAN rules must precede IMAGING!"
        )

    def test_endogastro_is_endoscopy(self):
        result = self._classify_fixture("FS_endogastro.pdf")
        assert result == DocType.ENDOSCOPY, f"Got {result}"

    def test_endocolono_is_endoscopy(self):
        result = self._classify_fixture("FS_endocolono.pdf")
        assert result == DocType.ENDOSCOPY, f"Got {result}"

    def test_tenyesztes_is_microbiology(self):
        """Rasterised scan: text empty → must classify by filename."""
        result = self._classify_fixture("FS_tenyesztes.pdf")
        assert result == DocType.MICROBIOLOGY, f"Got {result}"

    def test_all_fixtures_no_crash(self):
        for filename in FIXTURE_EXPECTED:
            p = FIXTURES_DIR / filename
            if not p.exists():
                continue
            try:
                doc = ingest(p)
                result = route(doc)
                assert isinstance(result, RoutedDoc)
            except Exception as exc:
                raise AssertionError(f"ingest→route raised for {filename}: {exc}") from exc

    def test_all_fixtures_doc_type_not_unknown(self):
        """Every fixture we have should classify to something concrete."""
        for filename in FIXTURE_EXPECTED:
            p = FIXTURES_DIR / filename
            if not p.exists():
                continue
            doc = ingest(p)
            result = route(doc)
            assert result.doc_type != DocType.UNKNOWN, (
                f"{filename} classified as UNKNOWN — check keyword patterns"
            )

    def test_all_fixtures_match_expected_type(self):
        """Every fixture must match its expected DocType."""
        for filename, expected in FIXTURE_EXPECTED.items():
            p = FIXTURES_DIR / filename
            if not p.exists():
                continue
            doc = ingest(p)
            result = route(doc)
            assert result.doc_type == expected, (
                f"{filename}: expected {expected}, got {result.doc_type}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (no pytest required)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestClassifyByText,
        TestAmbulanslapClassification,
        TestFibroscanNotMisclassifiedAsImaging,
        TestClassifyByFilename,
        TestRoute,
        TestRouteFixtures,
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
