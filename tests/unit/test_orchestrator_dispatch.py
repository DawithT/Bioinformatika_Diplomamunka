"""
tests/unit/test_orchestrator_dispatch.py

Tests for the _extract_doc dispatch logic in the orchestrator.

Covers:
  - DISCHARGE_SUMMARY runs all four hard event parsers
  - Standalone event doc types run only their specific parser
  - HISTOLOGY gets soft extraction only (no hard parser)
  - MRI events extracted from DISCHARGE_SUMMARY text
  - Microbiology events extracted from DISCHARGE_SUMMARY text

These tests call _extract_doc directly with a mock SoftExtractor to
isolate hard extraction dispatch logic from LLM/config dependencies.

Run with:
    PYTHONPATH=. python3 tests/unit/test_orchestrator_dispatch.py
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from pathlib import Path

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField, PatientAnchor, RoutedDoc
from app.pipeline.orchestrator import _extract_doc_hard, _extract_doc_soft


# ── Helpers ──────────────────────────────────────────────────────────────────

ANCHOR = PatientAnchor.create(
    name="Beteg Neve",
    dob=date(1960, 1, 1),
    taj="111-222-333",
    admission_start=datetime(2026, 1, 12, 4, 43),
    discharge_end=datetime(2026, 1, 20, 13, 32),
)


class _NullSoftExtractor:
    """Mock soft extractor that returns no fields (LLM disabled)."""
    def extract(self, doc, registry, anchor, populated_keys=None):
        return []


_SOFT = _NullSoftExtractor()


def _extract_doc(doc, anchor, registry, soft_extractor):
    """Convenience wrapper combining hard + soft for tests."""
    hard = _extract_doc_hard(doc, anchor)
    return _extract_doc_soft(doc, hard, registry, soft_extractor, anchor)


def _make_doc(text: str, doc_type: DocType) -> RoutedDoc:
    return RoutedDoc(
        path=Path("test.txt"),
        doc_type=doc_type,
        pages=(text,),
        tables=(),
        meta={},
    )


# ── Discharge summary text with embedded events ─────────────────────────────

_ZARO_WITH_ALL_EVENTS = """\
Felvétel dátuma: 2026.01.12 04:43
Elbocsátás dátuma: 2026.01.20 13:32

GAD5 - Gasztroenterológiai Klinika 2026.01.13. 08.00 (Vizsgálati részlelet)
Eszköz: gastroscop SN: K859
Endoszkópos lelet: Fundális varicositas látható.
Vélemény: Fundális varicositas.

2026.01.16 Fibroscan
Máj 75.0 kPa, IQR 5%, CAP 230 dB/m
S0 F4

KRTS-OKK RAD KC Röntgen 1. szakrendelés SBO 2026.01.11. 14.44 (Vizsgálati részlelet)
Helyszíni fekvő mellkas felvétel:
A rekesz szabályos helyzetű.

KRMR - OKK RAD KC MR szakrendelés 2026.01.15. 10.00 (Vizsgálati részlelet)
Koponya MR natív: Kóros jelzésfokozódás nem látható.

MBZ - Bakterológia 2026.01.17 14:51 (ascites)
Gomba tenyésztése negatív.; Baktérium nem tenyészett ki.;
"""


# ── Test classes ─────────────────────────────────────────────────────────────

class TestDischargeSummaryRunsAllParsers:
    """DISCHARGE_SUMMARY must run all four hard event parsers."""

    def test_endoscopy_extracted_from_discharge(self):
        doc = _make_doc(_ZARO_WITH_ALL_EVENTS, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("endo_gastro" in k for k in keys), f"Keys: {sorted(keys)}"

    def test_fibroscan_extracted_from_discharge(self):
        doc = _make_doc(_ZARO_WITH_ALL_EVENTS, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("fibro" in k for k in keys), f"Keys: {sorted(keys)}"

    def test_imaging_extracted_from_discharge(self):
        doc = _make_doc(_ZARO_WITH_ALL_EVENTS, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("rtg" in k for k in keys), f"Keys: {sorted(keys)}"

    def test_mri_extracted_from_discharge(self):
        doc = _make_doc(_ZARO_WITH_ALL_EVENTS, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("mri" in k for k in keys), f"Keys: {sorted(keys)}"

    def test_microbiology_extracted_from_discharge(self):
        doc = _make_doc(_ZARO_WITH_ALL_EVENTS, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("micro" in k for k in keys), f"Keys: {sorted(keys)}"

    def test_all_five_event_types_present(self):
        """Full integration: all five event type prefixes found."""
        doc = _make_doc(_ZARO_WITH_ALL_EVENTS, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        prefixes = {"endo_gastro", "fibro", "rtg", "mri", "micro"}
        found = {p for p in prefixes if any(k.startswith(p + "_") for k in keys)}
        assert found == prefixes, f"Missing: {prefixes - found}; got keys: {sorted(keys)}"


class TestStandaloneDocTypesUnchanged:
    """Standalone event doc types should still dispatch to their specific parser."""

    def test_endoscopy_doc_still_works(self):
        text = "Érkezés dátuma: 2026.01.13 08:00\nEndoszkópos lelet: Varicositas.\nVélemény: OK\n"
        doc = _make_doc(text, DocType.ENDOSCOPY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        assert any("endo_gastro" in f.key or "endo_colono" in f.key for f in fields)

    def test_imaging_doc_still_works(self):
        text = "KRTS-OKK RAD KC Röntgen 1. szakrendelés SBO 2026.01.11. 14.44 (Vizsgálati részlelet)\nRekesz szabályos.\n"
        doc = _make_doc(text, DocType.IMAGING)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        assert any("rtg" in f.key for f in fields)

    def test_microbiology_doc_still_works(self):
        text = "MBZ - Bakterológia 2026.01.17 14:51 (ascites)\nBaktérium nem tenyészett ki.;\n"
        doc = _make_doc(text, DocType.MICROBIOLOGY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        assert any("micro" in f.key for f in fields)


class TestAnchorFields:
    """Identity / admission fields written from the patient anchor."""

    def test_discharge_date_is_date_only(self):
        from app.pipeline.orchestrator import _anchor_to_fields
        values = {f.key: f.value for f in _anchor_to_fields(ANCHOR)}
        assert values["elbocsajtas_datuma_dis"] == "2026-01-20"
        assert values["korhazi_felvetel_ideje_adm"] == "2026.01.12 04:43"


class TestLlmForRun:
    """Per-run LLM switch and the job-scoped prompt cache."""

    def _patch(self, monkeypatch, configured: bool):
        from app.services import llm_client
        from config import settings
        monkeypatch.setattr(llm_client, "llm_configured", lambda: configured)
        monkeypatch.setattr(llm_client, "get_llm_client", lambda: _FakeLiveClient())
        monkeypatch.setattr(settings, "enable_llm_cache", True)

    def test_user_switch_off_means_no_llm_and_no_cache(self, monkeypatch):
        from app.pipeline.orchestrator import _llm_for_run
        from app.services.llm_client import DisabledClient
        self._patch(monkeypatch, configured=True)
        client, cache, cfg = _llm_for_run(use_llm=False)
        assert isinstance(client, DisabledClient)
        assert cache.enabled is False
        assert cfg["llm"] == {"enabled": False}

    def test_server_off_wins_over_the_user_switch(self, monkeypatch):
        from app.pipeline.orchestrator import _llm_for_run
        from app.services.llm_client import DisabledClient
        self._patch(monkeypatch, configured=False)
        client, cache, _ = _llm_for_run(use_llm=True)
        assert isinstance(client, DisabledClient)
        assert cache.enabled is False

    def test_llm_on_gets_a_fresh_cache_per_job(self, monkeypatch):
        from app.pipeline.orchestrator import _llm_for_run
        self._patch(monkeypatch, configured=True)
        client, cache, cfg = _llm_for_run(use_llm=True)
        assert isinstance(client, _FakeLiveClient)
        assert cache.enabled is True and cfg["llm"]["enabled"] is True
        cache.set("prompt", "answer")
        _, next_cache, _ = _llm_for_run(use_llm=True)
        assert next_cache.get("prompt") is None


class _FakeLiveClient:
    def chat(self, system, user):
        return "{}"


class TestHistologySoftOnly:
    """HISTOLOGY has no hard parser — only soft extraction runs.

    This is intentional: histology reports are free-form narrative text
    that cannot be reliably parsed with regex/block-based methods.
    The LLM-based soft extractor handles them instead.
    """

    def test_histology_no_hard_fields(self):
        """A histology doc should produce zero hard-extracted fields."""
        text = """\
Szövettan eredménye:
A máj biopszia cirrhosis hepatis képét mutatja.
Fibrosis stádium: F4 (Metavir).
Gyulladásos aktivitás: A2.
"""
        doc = _make_doc(text, DocType.HISTOLOGY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        hard_fields = [f for f in fields if f.source in (FieldSource.HARD, FieldSource.HEURISTIC)]
        assert hard_fields == [], (
            f"HISTOLOGY should have no hard/heuristic fields; got: {[f.key for f in hard_fields]}"
        )

    def test_histology_with_soft_extractor(self):
        """When soft extractor returns fields, they appear in the result."""
        class _MockSoft:
            def extract(self, doc, registry, anchor, populated_keys=None):
                return [ExtractedField(
                    key="biopsy_result", value="F4", source=FieldSource.SOFT,
                    doc_type=doc.doc_type, confidence=0.7, page=None, engine="llm",
                )]

        text = "Szövettan eredménye: cirrhosis hepatis.\n"
        doc = _make_doc(text, DocType.HISTOLOGY)
        fields = _extract_doc(doc, ANCHOR, [], _MockSoft())
        assert len(fields) == 1
        assert fields[0].key == "biopsy_result"
        assert fields[0].source == FieldSource.SOFT


class TestMicrobiologyEndToEnd:
    """Microbiology extraction works end-to-end after _FELVETELI_DATUM_RE fix."""

    def test_standalone_micro_via_extract_doc(self):
        """Standalone microbiology doc processed through _extract_doc."""
        text = "MBZ - Bakterológia 2026.01.17 14:51 (ascites)\nGomba tenyésztése negatív.; Baktérium nem tenyészett ki.;\n"
        doc = _make_doc(text, DocType.MICROBIOLOGY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert "micro_d6_1_pathogen" in keys, f"Keys: {sorted(keys)}"
        f = next(f for f in fields if f.key == "micro_d6_1_pathogen")
        assert f.value == "negatív"

    def test_micro_with_felvetel_header_via_extract_doc(self):
        """Microbiology doc with Felvétel dátuma header (the previously-crashing path)."""
        text = """\
Felvétel dátuma: 2026.01.14
MBZ - Bakterológia 2026.01.14 09:00 (vizelet)
Escherichia coli tenyészett ki.;
"""
        doc = _make_doc(text, DocType.MICROBIOLOGY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("micro" in k for k in keys), f"Expected micro fields; got: {sorted(keys)}"

    def test_micro_embedded_in_discharge_via_extract_doc(self):
        """Microbiology block inside a discharge summary is now extracted."""
        text = """\
Zárójelentés szövege.
MBZ - Bakterológia 2026.01.17 14:51 (ascites)
Gomba tenyésztése negatív.; Baktérium nem tenyészett ki.;
Egyéb szöveg.
"""
        doc = _make_doc(text, DocType.DISCHARGE_SUMMARY)
        fields = _extract_doc(doc, ANCHOR, [], _SOFT)
        keys = {f.key for f in fields}
        assert any("micro" in k for k in keys), (
            f"Micro not extracted from discharge; keys: {sorted(keys)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestDischargeSummaryRunsAllParsers,
        TestStandaloneDocTypesUnchanged,
        TestAnchorFields,
        TestHistologySoftOnly,
        TestMicrobiologyEndToEnd,
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
        print("  All tests passed.")
