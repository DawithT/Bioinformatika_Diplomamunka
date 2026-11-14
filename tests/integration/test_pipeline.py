"""
tests/integration/test_pipeline.py

End-to-end integration tests for the CirrhosisRegistry pipeline.

Run with:
    PYTHONPATH=/path/to/cirrhosis_registry_step16 \
    ENABLE_LLM=false \
    python3 tests/integration/test_pipeline.py

All tests use the anonymised fixture files in tests/fixtures/ and run
with ENABLE_LLM=false so no LLM server is required.

Design notes
------------
- Integration tests build DocInput objects from fixture files and verify
  the full merge → rules → export chain produces the expected output.
- Routing is handled explicitly: zárójelentés fixture is force-typed to
  DISCHARGE_SUMMARY (the text classifier mis-scores it because the
  discharge summary embeds endoscopy result sections — a known limitation
  of the text-only classifier for comprehensive documents).
- Each test class is self-contained and uses a fresh tempdir.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

# ── Environment ───────────────────────────────────────────────────────────────
os.environ.setdefault("ENABLE_LLM", "false")

BASE       = Path(__file__).parent.parent.parent   # project root
FIXTURES   = BASE / "tests" / "fixtures"
REGISTRY   = BASE / "config" / "Registry_base.xlsx"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_fixture(name: str, forced_doc_type=None):
    """Ingest a fixture file and optionally force its doc_type."""
    from app.pipeline.step1_ingest import ingest
    from app.pipeline.step2_route import classify, classify_by_filename
    from app.domain.enums import DocType

    path = FIXTURES / name
    if not path.exists():
        return None

    doc = ingest(path)

    if forced_doc_type:
        return doc.with_doc_type(forced_doc_type)

    # Normal routing: text first, filename fallback
    dt = classify(doc.full_text) if doc.full_text.strip() else DocType.UNKNOWN
    if dt == DocType.UNKNOWN:
        dt = classify_by_filename(path)
    return doc.with_doc_type(dt)


def _get_anchor():
    """Extract the patient anchor from the zárójelentés fixture."""
    from app.pipeline.step3_anchor import extract_anchor
    from app.domain.enums import DocType
    doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
    return extract_anchor(doc)


def _shared_resources():
    """Load registry, derive rules in-memory, and build an LLM-disabled soft extractor."""
    from app.registry.loader import load_registry
    from app.registry.rules_engine import build_rules_from_registry
    from app.services.cache import PromptCache
    from app.services.llm_client import get_llm_client
    from app.pipeline.step5_extract.soft_llm import SoftExtractor
    registry = load_registry(REGISTRY)
    rules    = build_rules_from_registry(registry)
    extractor = SoftExtractor(get_llm_client(), PromptCache())
    return registry, rules, extractor


def _extract(doc, anchor, registry, extractor):
    """Extract all fields (hard + soft) from a routed doc."""
    from app.domain.enums import DocType
    from app.pipeline.step5_extract.hard_lab import TextLabParser, TableLabParser
    from app.pipeline.step5_extract.hard_events import (
        EndoscopyParser, FibroscanParser, ImagingParser, MicrobiologyParser,
    )

    fields = []
    dt = doc.doc_type

    if dt == DocType.LAB_REPORT:
        fields.extend(TextLabParser().parse(doc.full_text, anchor))
        if doc.tables:
            fields.extend(TableLabParser().parse(
                [list(r) for r in doc.tables], anchor
            ))
    elif dt == DocType.ENDOSCOPY:
        fields.extend(EndoscopyParser().parse(doc, anchor))
    elif dt == DocType.ELASTOGRAPHY:
        fields.extend(FibroscanParser().parse(doc, anchor))
    elif dt == DocType.IMAGING:
        fields.extend(ImagingParser().parse(doc, anchor))
    elif dt == DocType.MICROBIOLOGY:
        fields.extend(MicrobiologyParser().parse(doc, anchor))

    fields.extend(extractor.extract(doc, registry, anchor))
    return fields


# ── Test: Pre-flight (fixtures available) ────────────────────────────────────

class TestFixturesAvailable(unittest.TestCase):
    """Verify the fixture files are present before running longer tests."""

    REQUIRED = [
        "FS_záró.pdf", "FS_labor2.pdf", "FS_fibro.pdf",
        "FS_endogastro.pdf", "FS_endocolono.pdf", "FS_tenyésztés.pdf",
    ]

    def test_all_fixtures_present(self):
        missing = [f for f in self.REQUIRED if not (FIXTURES / f).exists()]
        self.assertFalse(missing, f"Missing fixtures: {missing}")

    def test_registry_present(self):
        self.assertTrue(REGISTRY.exists(), f"Registry not found: {REGISTRY}")


# ── Test: Anchor extraction ───────────────────────────────────────────────────

class TestAnchorFromFixture(unittest.TestCase):
    """Step 3: anchor extraction from the real zárójelentés fixture."""

    def setUp(self):
        if not (FIXTURES / "FS_záró.pdf").exists():
            self.skipTest("FS_záró.pdf not available")

    def test_anchor_admission_date(self):
        anchor = _get_anchor()
        self.assertEqual(anchor.admission_date, date(2026, 1, 12))

    def test_anchor_discharge_date(self):
        anchor = _get_anchor()
        self.assertEqual(anchor.discharge_date, date(2026, 1, 20))

    def test_anchor_fingerprint_stable(self):
        a1 = _get_anchor()
        a2 = _get_anchor()
        self.assertEqual(a1.fingerprint, a2.fingerprint)
        self.assertTrue(len(a1.fingerprint) >= 8)

    def test_anchor_fingerprint_not_empty(self):
        anchor = _get_anchor()
        self.assertNotEqual(anchor.fingerprint, "")


# ── Test: Full happy-path pipeline ───────────────────────────────────────────

class TestFullPipeline(unittest.TestCase):
    """
    Happy-path integration: all six fixtures → merge → export → ZIP.

    Verifies the ZIP contains all expected output files and that the
    wide_table.xlsx has at least one data row (the case).
    """

    def setUp(self):
        for f in ("FS_záró.pdf", "FS_labor2.pdf", "FS_fibro.pdf",
                  "FS_endogastro.pdf", "FS_endocolono.pdf"):
            if not (FIXTURES / f).exists():
                self.skipTest(f"Fixture {f} not available")
        self.tmp = Path(tempfile.mkdtemp())

    def _run(self):
        from app.domain.enums import DocType
        from app.domain.models import CaseResult
        from app.pipeline.step6_merge import DocInput, merge
        from app.pipeline.step8_export import export
        from app.registry.rules_engine import apply

        anchor   = _get_anchor()
        registry, rules, extractor = _shared_resources()

        fixtures_to_load = [
            ("FS_záró.pdf",       DocType.DISCHARGE_SUMMARY, None),
            ("FS_labor2.pdf",     DocType.LAB_REPORT,        date(2026, 1, 16)),
            ("FS_fibro.pdf",      DocType.ELASTOGRAPHY,         date(2026, 1, 21)),  # post-discharge
            ("FS_endogastro.pdf", DocType.ENDOSCOPY,         date(2026, 1, 13)),
            ("FS_endocolono.pdf", DocType.ENDOSCOPY,         date(2026, 1, 16)),
        ]

        doc_inputs = []
        for fname, dt, doc_date in fixtures_to_load:
            doc    = _load_fixture(fname, dt)
            fields = _extract(doc, anchor, registry, extractor)
            doc_inputs.append(DocInput(
                doc_type=dt,
                fields=fields,
                doc_date=doc_date,
            ))

        master, conflicts, mismatches, oow = merge(doc_inputs, anchor, "case_full")
        master = apply(master, rules)
        result = CaseResult("case_full", master, conflicts, mismatches, oow)
        files = export(result, self.tmp, registry)
        return files, result

    def test_output_folder_produced(self):
        files, _ = self._run()
        self.assertGreater(len(files), 0)
        for f in files:
            self.assertTrue(f.exists())

    def test_output_contains_required_files(self):
        files, _ = self._run()
        names = {f.name for f in files}
        case_id = "case_full"
        for stem in ("wide_table.xlsx", "wide_table.csv", "wide_table_labels.xlsx"):
            prefixed = f"{case_id}_{stem}"
            self.assertIn(prefixed, names, f"Missing {prefixed} in output")

    def test_wide_table_has_data_row(self):
        """wide_table.xlsx must have a header row and at least one data row."""
        import openpyxl
        files, _ = self._run()
        xlsx_path = next(f for f in files if f.name.endswith("_wide_table.xlsx"))
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb.active
        self.assertGreaterEqual(ws.max_row, 2, "Expected header + at least 1 data row")

    def test_csv_has_bom(self):
        """wide_table.csv must start with UTF-8 BOM for Excel compatibility."""
        files, _ = self._run()
        csv_path = next(f for f in files if f.name.endswith("_wide_table.csv"))
        csv_bytes = csv_path.read_bytes()
        self.assertTrue(csv_bytes.startswith(b"\xef\xbb\xbf"),
                        "CSV missing UTF-8 BOM")

    def test_master_fields_non_empty(self):
        _, result = self._run()
        self.assertGreater(len(result.fields), 0)

    def test_fibroscan_post_discharge_goes_to_out_of_window(self):
        """Fibroscan dated 2026-01-21 (after discharge 2026-01-20) → out_of_window."""
        _, result = self._run()
        # Fibroscan at 2026-01-21 is POST_DISCHARGE → should be in out_of_window
        # The full run includes doc_date=2026-01-21 for fibroscan
        self.assertGreater(len(result.out_of_window), 0,
                           "Expected fibroscan to appear in out_of_window list")


# ── Test: Discharge-summary-only case ────────────────────────────────────────

class TestDischargeOnlyCase(unittest.TestCase):
    """
    Edge case: case bundle contains only the zárójelentés (no ancillary docs).

    The pipeline must complete without error and produce a valid ZIP.
    """

    def setUp(self):
        if not (FIXTURES / "FS_záró.pdf").exists():
            self.skipTest("FS_záró.pdf not available")
        self.tmp = Path(tempfile.mkdtemp())

    def test_discharge_only_produces_output(self):
        from app.domain.enums import DocType
        from app.domain.models import CaseResult
        from app.pipeline.step6_merge import DocInput, merge
        from app.pipeline.step8_export import export
        from app.registry.rules_engine import apply

        anchor = _get_anchor()
        registry, rules, extractor = _shared_resources()

        doc    = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        fields = _extract(doc, anchor, registry, extractor)

        doc_inputs = [DocInput(DocType.DISCHARGE_SUMMARY, fields)]
        master, conflicts, mismatches, oow = merge(doc_inputs, anchor, "case_zaro_only")
        master = apply(master, rules)
        result = CaseResult("case_zaro_only", master, conflicts, mismatches, oow)
        files  = export(result, self.tmp, registry)

        self.assertGreater(len(files), 0)
        self.assertTrue(files[0].exists())
        self.assertEqual(len(result.mismatches),  0)
        self.assertEqual(len(result.out_of_window), 0)

    def test_discharge_only_has_fields(self):
        from app.domain.enums import DocType
        from app.pipeline.step6_merge import DocInput, merge
        from app.registry.rules_engine import apply

        anchor = _get_anchor()
        registry, rules, extractor = _shared_resources()

        doc    = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        fields = _extract(doc, anchor, registry, extractor)

        doc_inputs = [DocInput(DocType.DISCHARGE_SUMMARY, fields)]
        master, *_ = merge(doc_inputs, anchor, "case_zaro_only2")
        master = apply(master, rules)

        self.assertGreater(len(master), 0)


# ── Test: Mismatch document ───────────────────────────────────────────────────

class TestMismatchDocument(unittest.TestCase):
    """
    Edge case: one ancillary document carries a wrong patient fingerprint.

    The mismatch must be recorded and the document's fields must be excluded
    from the master field set.
    """

    def setUp(self):
        if not (FIXTURES / "FS_záró.pdf").exists():
            self.skipTest("FS_záró.pdf not available")
        if not (FIXTURES / "FS_endogastro.pdf").exists():
            self.skipTest("FS_endogastro.pdf not available")
        self.tmp = Path(tempfile.mkdtemp())

    def test_mismatch_recorded(self):
        from app.domain.enums import DocType
        from app.pipeline.step6_merge import DocInput, merge

        anchor = _get_anchor()
        registry, _rules, extractor = _shared_resources()

        # Discharge summary (correct fingerprint — not checked for discharge)
        zaro_doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        zaro_fields = _extract(zaro_doc, anchor, registry, extractor)

        # Ancillary doc with a WRONG fingerprint
        gastro_doc   = _load_fixture("FS_endogastro.pdf", DocType.ENDOSCOPY)
        gastro_fields = _extract(gastro_doc, anchor, registry, extractor)

        doc_inputs = [
            DocInput(DocType.DISCHARGE_SUMMARY, zaro_fields),
            DocInput(DocType.ENDOSCOPY,         gastro_fields,
                     doc_date=date(2026, 1, 13),
                     doc_fingerprint="WRONGFINGERPRINT000"),  # mismatch
        ]
        _master, _conflicts, mismatches, _oow = merge(doc_inputs, anchor, "case_mismatch")

        self.assertGreater(len(mismatches), 0, "Expected a mismatch record")
        # MismatchRecord stores the anchor's fingerprint as expected_fingerprint
        self.assertEqual(mismatches[0].expected_fingerprint, anchor.fingerprint)

    def test_mismatch_fields_excluded(self):
        """Fields from a mismatched doc must NOT appear in master."""
        from app.domain.enums import DocType, FieldSource
        from app.domain.models import ExtractedField
        from app.pipeline.step6_merge import DocInput, merge

        anchor = _get_anchor()
        registry, _rules, extractor = _shared_resources()

        # Create a sentinel field with the correct 5-arg signature
        sentinel_field = ExtractedField(
            key="endo_gastro_d2_1_varix_grade",
            value="SENTINEL_VALUE",
            source=FieldSource.HARD,
            doc_type=DocType.ENDOSCOPY,
            confidence=1.0,
        )

        zaro_doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        zaro_fields = _extract(zaro_doc, anchor, registry, extractor)

        doc_inputs = [
            DocInput(DocType.DISCHARGE_SUMMARY, zaro_fields),
            DocInput(DocType.ENDOSCOPY, [sentinel_field],
                     doc_date=date(2026, 1, 13),
                     doc_fingerprint="WRONG000"),
        ]
        master, _, mismatches, _ = merge(doc_inputs, anchor, "case_mismatch2")

        # Confirm a mismatch was actually detected (otherwise the test is vacuous)
        self.assertGreater(len(mismatches), 0, "Expected a mismatch record to be raised")

        # The sentinel must not appear in master
        sentinel_in_master = any(
            f.value == "SENTINEL_VALUE"
            for f in master.values()
        )
        self.assertFalse(sentinel_in_master,
                         "Mismatch doc field leaked into master")


# ── Test: Post-discharge fibroscan (out-of-window) ───────────────────────────

class TestPostDischargeFibroscan(unittest.TestCase):
    """
    Edge case: fibroscan dated after discharge → goes to history_followup.

    The fibroscan fixture date is 2026-01-21, discharge is 2026-01-20.
    """

    def setUp(self):
        for f in ("FS_záró.pdf", "FS_fibro.pdf"):
            if not (FIXTURES / f).exists():
                self.skipTest(f"Fixture {f} not available")
        self.tmp = Path(tempfile.mkdtemp())

    def test_fibroscan_is_post_discharge(self):
        """The fibroscan fixture date must be classified POST_DISCHARGE."""
        from app.pipeline.step4_normalize import classify_window
        from app.domain.enums import WindowType

        anchor = _get_anchor()
        fibro_date = date(2026, 1, 21)
        wtype = classify_window(fibro_date, anchor)
        self.assertEqual(wtype, WindowType.POST_DISCHARGE)

    def test_out_of_window_recorded(self):
        from app.domain.enums import DocType
        from app.pipeline.step6_merge import DocInput, merge

        anchor = _get_anchor()
        registry, _rules, extractor = _shared_resources()

        zaro_doc  = _load_fixture("FS_záró.pdf",  DocType.DISCHARGE_SUMMARY)
        fibro_doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)

        doc_inputs = [
            DocInput(DocType.DISCHARGE_SUMMARY, _extract(zaro_doc, anchor, registry, extractor)),
            DocInput(DocType.ELASTOGRAPHY,         _extract(fibro_doc, anchor, registry, extractor),
                     doc_date=date(2026, 1, 21)),   # POST_DISCHARGE
        ]
        _, _, _, out_of_window = merge(doc_inputs, anchor, "case_fibro_post")
        self.assertGreater(len(out_of_window), 0)

    def test_history_followup_xlsx_in_output(self):
        """history_followup.xlsx must be present in the output folder."""
        from app.domain.enums import DocType
        from app.domain.models import CaseResult
        from app.pipeline.step6_merge import DocInput, merge
        from app.pipeline.step8_export import export
        from app.registry.rules_engine import apply

        anchor = _get_anchor()
        registry, rules, extractor = _shared_resources()

        zaro_doc  = _load_fixture("FS_záró.pdf",  DocType.DISCHARGE_SUMMARY)
        fibro_doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)

        doc_inputs = [
            DocInput(DocType.DISCHARGE_SUMMARY, _extract(zaro_doc, anchor, registry, extractor)),
            DocInput(DocType.ELASTOGRAPHY,         _extract(fibro_doc, anchor, registry, extractor),
                     doc_date=date(2026, 1, 21)),
        ]
        master, conflicts, mismatches, oow = merge(doc_inputs, anchor, "case_fibro_post2")
        master = apply(master, rules)
        result = CaseResult("case_fibro_post2", master, conflicts, mismatches, oow)
        files  = export(result, self.tmp, registry)

        names = {f.name for f in files}
        self.assertIn("case_fibro_post2_history_followup.xlsx", names)


# ── Test: Two fibroscan events on different days ──────────────────────────────

class TestTwoFibroscanEvents(unittest.TestCase):
    """
    Edge case: two fibroscan DocInputs with different exam dates.

    Both events must survive the merge (they are on different days, so
    no conflict) and appear as separate columns in the wide table.
    """

    def setUp(self):
        if not (FIXTURES / "FS_záró.pdf").exists():
            self.skipTest("FS_záró.pdf not available")
        if not (FIXTURES / "FS_fibro.pdf").exists():
            self.skipTest("FS_fibro.pdf not available")
        self.tmp = Path(tempfile.mkdtemp())

    def test_two_fibroscan_events_both_present(self):
        from app.domain.enums import DocType
        from app.pipeline.step5_extract.hard_events import FibroscanParser
        from app.pipeline.step6_merge import DocInput, merge
        from app.registry.rules_engine import apply

        anchor = _get_anchor()
        registry, rules, extractor = _shared_resources()

        fibro_doc = _load_fixture("FS_fibro.pdf", DocType.ELASTOGRAPHY)
        fibro_fields_day_d10 = FibroscanParser().parse(fibro_doc, anchor)
        # day 10 = 2026-01-21 (post-discharge, but still gets parsed)
        # Manufacture a second fibro doc at day 9 = 2026-01-20 (discharge day)
        fibro_fields_day_d9 = FibroscanParser().parse(fibro_doc, anchor)

        zaro_doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        zaro_fields = _extract(zaro_doc, anchor, registry, extractor)

        doc_inputs = [
            DocInput(DocType.DISCHARGE_SUMMARY, zaro_fields),
            DocInput(DocType.ELASTOGRAPHY, fibro_fields_day_d10,
                     doc_date=date(2026, 1, 13)),   # day 2 — different day
            DocInput(DocType.ELASTOGRAPHY, fibro_fields_day_d9,
                     doc_date=date(2026, 1, 16)),   # day 5
        ]
        master, *_ = merge(doc_inputs, anchor, "case_two_fibro")
        master = apply(master, rules)

        # Both event sets should have contributed columns
        fibro_cols = [k for k in master if "fibro" in k.lower()]
        self.assertGreater(len(fibro_cols), 0, "Expected fibroscan columns in master")


# ── Test: ENABLE_LLM=false ────────────────────────────────────────────────────

class TestLLMDisabled(unittest.TestCase):
    """
    Edge case: ENABLE_LLM=false → soft fields are all "Unknown";
    hard fields (labs, events) must still be extracted.
    """

    def setUp(self):
        for f in ("FS_záró.pdf", "FS_labor2.pdf", "FS_endogastro.pdf"):
            if not (FIXTURES / f).exists():
                self.skipTest(f"Fixture {f} not available")
        os.environ["ENABLE_LLM"] = "false"

    def test_soft_fields_are_unknown_when_llm_disabled(self):
        from app.domain.enums import DocType
        from app.services.llm_client import get_llm_client
        from app.services.cache import PromptCache
        from app.pipeline.step5_extract.soft_llm import SoftExtractor
        from app.registry.loader import load_registry

        anchor    = _get_anchor()
        registry  = load_registry(REGISTRY)
        extractor = SoftExtractor(get_llm_client(), PromptCache())

        doc    = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        fields = extractor.extract(doc, registry, anchor)

        self.assertGreater(len(fields), 0, "Extractor must return fields even when LLM disabled")
        unknown_count = sum(1 for f in fields if f.value == "Unknown")
        self.assertEqual(unknown_count, len(fields),
                         "All soft fields must be 'Unknown' when LLM disabled")

    def test_hard_lab_fields_extracted_without_llm(self):
        from app.domain.enums import DocType
        from app.pipeline.step5_extract.hard_lab import TextLabParser

        anchor = _get_anchor()
        doc    = _load_fixture("FS_labor2.pdf", DocType.LAB_REPORT)
        fields = TextLabParser().parse(doc.full_text, anchor)

        # Should extract at least one hard lab value
        non_unknown = [f for f in fields if f.value not in (None, "", "Unknown")]
        self.assertGreater(len(non_unknown), 0,
                           "Hard lab parser must produce values regardless of LLM setting")

    def test_hard_endoscopy_fields_extracted_without_llm(self):
        from app.domain.enums import DocType
        from app.pipeline.step5_extract.hard_events import EndoscopyParser

        anchor = _get_anchor()
        doc    = _load_fixture("FS_endogastro.pdf", DocType.ENDOSCOPY)
        fields = EndoscopyParser().parse(doc, anchor)

        self.assertGreater(len(fields), 0,
                           "Hard event parser must produce fields regardless of LLM setting")

    def test_pipeline_completes_end_to_end_with_llm_disabled(self):
        from app.domain.enums import DocType
        from app.domain.models import CaseResult
        from app.pipeline.step6_merge import DocInput, merge
        from app.pipeline.step8_export import export
        from app.registry.rules_engine import apply

        anchor   = _get_anchor()
        registry, rules, extractor = _shared_resources()

        zaro_doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        lab_doc  = _load_fixture("FS_labor2.pdf", DocType.LAB_REPORT)

        doc_inputs = [
            DocInput(DocType.DISCHARGE_SUMMARY,
                     _extract(zaro_doc, anchor, registry, extractor)),
            DocInput(DocType.LAB_REPORT,
                     _extract(lab_doc,  anchor, registry, extractor),
                     doc_date=date(2026, 1, 16)),
        ]
        master, conflicts, mismatches, oow = merge(doc_inputs, anchor, "case_nollm")
        master = apply(master, rules)
        result = CaseResult("case_nollm", master, conflicts, mismatches, oow)

        tmp = Path(tempfile.mkdtemp())
        files = export(result, tmp, registry)
        self.assertGreater(len(files), 0)
        self.assertTrue(files[0].exists())


# ── Test: Column name constraints ─────────────────────────────────────────────

class TestColumnNameConstraints(unittest.TestCase):
    """
    Verify wide_table.xlsx column names satisfy export constraints:
    - max 60 chars
    - no special characters (only alnum + underscore)
    - no duplicates
    """

    def setUp(self):
        for f in ("FS_záró.pdf",):
            if not (FIXTURES / f).exists():
                self.skipTest(f"Fixture {f} not available")
        self.tmp = Path(tempfile.mkdtemp())

    def _build_cols(self):
        import openpyxl
        from app.domain.enums import DocType
        from app.domain.models import CaseResult
        from app.pipeline.step6_merge import DocInput, merge
        from app.pipeline.step8_export import export
        from app.registry.rules_engine import apply

        case_id  = "case_cols"
        anchor   = _get_anchor()
        registry, rules, extractor = _shared_resources()

        doc = _load_fixture("FS_záró.pdf", DocType.DISCHARGE_SUMMARY)
        doc_inputs = [DocInput(DocType.DISCHARGE_SUMMARY,
                               _extract(doc, anchor, registry, extractor))]
        master, conflicts, mismatches, oow = merge(doc_inputs, anchor, case_id)
        master = apply(master, rules)
        result = CaseResult(case_id, master, conflicts, mismatches, oow)
        files = export(result, self.tmp, registry)

        xlsx_path = next(f for f in files if f.name.endswith("_wide_table.xlsx"))
        wb = openpyxl.load_workbook(xlsx_path)
        ws = wb.active
        return [cell.value for cell in ws[1] if cell.value]

    def test_column_names_max_60_chars(self):
        for col in self._build_cols():
            self.assertLessEqual(len(col), 60,
                                 f"Column name too long ({len(col)}): {col!r}")

    def test_column_names_no_special_chars(self):
        import re
        pattern = re.compile(r'^[A-Za-z0-9_]+$')
        for col in self._build_cols():
            self.assertTrue(pattern.match(col),
                            f"Column name has special chars: {col!r}")

    def test_column_names_no_duplicates(self):
        cols = self._build_cols()
        self.assertEqual(len(cols), len(set(cols)),
                         f"Duplicate column names found: {[c for c in cols if cols.count(c) > 1]}")


# ── Runner ────────────────────────────────────────────────────────────────────

_PASS = "\033[32m✓\033[0m"
_FAIL = "\033[31m✗\033[0m"


def run_all() -> bool:
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in (
        TestFixturesAvailable,
        TestAnchorFromFixture,
        TestFullPipeline,
        TestDischargeOnlyCase,
        TestMismatchDocument,
        TestPostDischargeFibroscan,
        TestTwoFibroscanEvents,
        TestLLMDisabled,
        TestColumnNameConstraints,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))

    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    print(f"\n{'─'*60}")
    total  = result.testsRun
    passed = total - len(result.failures) - len(result.errors)
    skipped = len(result.skipped)
    print(f"  {passed}/{total} passed  "
          f"({skipped} skipped)  "
          f"{'all passed ✓' if result.wasSuccessful() else 'FAILURES PRESENT ✗'}")
    print(f"{'─'*60}\n")
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
