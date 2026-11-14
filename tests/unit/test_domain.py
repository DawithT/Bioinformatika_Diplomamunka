"""
tests/unit/test_domain.py

Step 2 tests: enums, domain models, set_field merge logic.

All tests use stdlib only — no external dependencies required.
"""
from __future__ import annotations

import pytest
from datetime import date, datetime
from pathlib import Path

from app.domain.enums import DocType, FieldSource, JobStatus, WindowType
from app.domain.models import (
    CaseBundle,
    CaseResult,
    ConflictRecord,
    ExtractedField,
    MismatchRecord,
    OutOfWindowRecord,
    PatientAnchor,
    RoutedDoc,
    set_field,
    to_plain,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_ef(
    key: str = "test_field",
    value: object = "value",
    source: FieldSource = FieldSource.SOFT,
    doc_type: DocType = DocType.DISCHARGE_SUMMARY,
    confidence: float = 0.7,
    page: int | None = None,
    engine: str | None = None,
) -> ExtractedField:
    return ExtractedField(
        key=key, value=value, source=source,
        doc_type=doc_type, confidence=confidence,
        page=page, engine=engine,
    )


def make_anchor() -> PatientAnchor:
    return PatientAnchor.create(
        name="Teszt Béla",
        dob=date(1960, 3, 15),
        taj="123456789",
        admission_start=datetime(2026, 1, 12, 4, 43),
        discharge_end=datetime(2026, 1, 20, 13, 32),
    )


# ─────────────────────────────────────────────────────────────────────────────
# DocType
# ─────────────────────────────────────────────────────────────────────────────

class TestDocType:
    def test_all_members_present(self):
        expected = {
            "DISCHARGE_SUMMARY", "LAB_REPORT", "ENDOSCOPY",
            "ELASTOGRAPHY", "IMAGING", "MICROBIOLOGY", "VIROLOGY",
            "HISTOLOGY", "AMBULANSLAP", "UNKNOWN",
        }
        assert {d.value for d in DocType} == expected

    def test_string_comparison(self):
        assert DocType.DISCHARGE_SUMMARY == "DISCHARGE_SUMMARY"

    def test_unknown_is_not_primary(self):
        # UNKNOWN must not equal any real document type
        real_types = [d for d in DocType if d != DocType.UNKNOWN]
        assert DocType.UNKNOWN not in real_types


# ─────────────────────────────────────────────────────────────────────────────
# FieldSource (IntEnum — priority ordering is critical)
# ─────────────────────────────────────────────────────────────────────────────

class TestFieldSource:
    def test_hard_beats_heuristic(self):
        assert FieldSource.HARD > FieldSource.HEURISTIC

    def test_heuristic_beats_soft(self):
        assert FieldSource.HEURISTIC > FieldSource.SOFT

    def test_hard_beats_soft(self):
        assert FieldSource.HARD > FieldSource.SOFT

    def test_int_values_match_priority(self):
        assert FieldSource.SOFT.value < FieldSource.HEURISTIC.value < FieldSource.HARD.value


# ─────────────────────────────────────────────────────────────────────────────
# JobStatus
# ─────────────────────────────────────────────────────────────────────────────

class TestJobStatus:
    def test_done_is_terminal(self):
        assert JobStatus.DONE.is_terminal

    def test_failed_is_terminal(self):
        assert JobStatus.FAILED.is_terminal

    def test_intermediate_not_terminal(self):
        for s in (JobStatus.QUEUED, JobStatus.INGESTING, JobStatus.ROUTING,
                  JobStatus.ANCHORING, JobStatus.EXTRACTING,
                  JobStatus.MERGING, JobStatus.EXPORTING):
            assert not s.is_terminal, f"{s} should not be terminal"

    def test_progress_pct_bounds(self):
        for s in JobStatus:
            pct = s.progress_pct
            assert 0 <= pct <= 100, f"{s}.progress_pct out of range: {pct}"

    def test_done_is_100_pct(self):
        assert JobStatus.DONE.progress_pct == 100

    def test_queued_is_0_pct(self):
        assert JobStatus.QUEUED.progress_pct == 0


# ─────────────────────────────────────────────────────────────────────────────
# CaseBundle
# ─────────────────────────────────────────────────────────────────────────────

class TestCaseBundle:
    def test_create_from_paths(self):
        paths = [Path("a.pdf"), Path("b.pdf")]
        bundle = CaseBundle.from_paths("case-001", paths)
        assert bundle.case_id == "case-001"
        assert len(bundle.files) == 2
        assert isinstance(bundle.files, tuple)  # not a list — must be hashable

    def test_is_frozen(self):
        bundle = CaseBundle.from_paths("x", [Path("f.pdf")])
        with pytest.raises((AttributeError, TypeError)):
            bundle.case_id = "y"  # type: ignore[misc]

    def test_empty_files(self):
        bundle = CaseBundle.from_paths("case-002", [])
        assert bundle.files == ()


# ─────────────────────────────────────────────────────────────────────────────
# RoutedDoc
# ─────────────────────────────────────────────────────────────────────────────

class TestRoutedDoc:
    def test_full_text_joins_pages(self):
        doc = RoutedDoc(
            path=Path("test.pdf"),
            doc_type=DocType.DISCHARGE_SUMMARY,
            pages=("Page one.", "Page two."),
            tables=(),
            meta={},
        )
        assert "Page one." in doc.full_text
        assert "Page two." in doc.full_text

    def test_first_3000_truncates(self):
        long_text = "x" * 5000
        doc = RoutedDoc(
            path=Path("test.pdf"),
            doc_type=DocType.LAB_REPORT,
            pages=(long_text,),
            tables=(),
            meta={},
        )
        assert len(doc.first_3000) == 3000

    def test_empty_factory(self):
        doc = RoutedDoc.empty(Path("bad.pdf"))
        assert doc.doc_type == DocType.UNKNOWN
        assert doc.full_text == ""
        assert doc.pages == ()

    def test_with_doc_type_returns_new_instance(self):
        doc = RoutedDoc.empty(Path("x.pdf"))
        updated = doc.with_doc_type(DocType.ELASTOGRAPHY)
        assert updated.doc_type == DocType.ELASTOGRAPHY
        assert doc.doc_type == DocType.UNKNOWN  # original unchanged

    def test_is_frozen(self):
        doc = RoutedDoc.empty(Path("x.pdf"))
        with pytest.raises((AttributeError, TypeError)):
            doc.doc_type = DocType.LAB_REPORT  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# PatientAnchor
# ─────────────────────────────────────────────────────────────────────────────

class TestPatientAnchor:
    def test_create_computes_fingerprint(self):
        anchor = make_anchor()
        assert isinstance(anchor.fingerprint, str)
        assert len(anchor.fingerprint) == 12

    def test_fingerprint_is_deterministic(self):
        a1 = make_anchor()
        a2 = make_anchor()
        assert a1.fingerprint == a2.fingerprint

    def test_fingerprint_differs_for_different_patients(self):
        a1 = make_anchor()
        a2 = PatientAnchor.create(
            name="Más Beteg",
            dob=date(1975, 6, 20),
            taj="987654321",
            admission_start=datetime(2026, 1, 12, 4, 43),
            discharge_end=datetime(2026, 1, 20, 13, 32),
        )
        assert a1.fingerprint != a2.fingerprint

    def test_taj_normalised_in_fingerprint(self):
        """TAJ with and without dashes should produce same fingerprint."""
        a1 = PatientAnchor.create(
            name="Teszt Béla", dob=date(1960, 3, 15), taj="123-456-789",
            admission_start=datetime(2026, 1, 12, 0, 0),
            discharge_end=datetime(2026, 1, 20, 0, 0),
        )
        a2 = PatientAnchor.create(
            name="Teszt Béla", dob=date(1960, 3, 15), taj="123456789",
            admission_start=datetime(2026, 1, 12, 0, 0),
            discharge_end=datetime(2026, 1, 20, 0, 0),
        )
        assert a1.fingerprint == a2.fingerprint

    def test_admission_date_property(self):
        anchor = make_anchor()
        assert anchor.admission_date == date(2026, 1, 12)

    def test_discharge_date_property(self):
        anchor = make_anchor()
        assert anchor.discharge_date == date(2026, 1, 20)

    def test_is_frozen(self):
        anchor = make_anchor()
        with pytest.raises((AttributeError, TypeError)):
            anchor.name = "Other"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────────────────────
# ExtractedField
# ─────────────────────────────────────────────────────────────────────────────

class TestExtractedField:
    def test_basic_creation(self):
        ef = make_ef(key="wbc_d1", value=5.87, source=FieldSource.HARD,
                     engine="regex", confidence=0.95)
        assert ef.key == "wbc_d1"
        assert ef.value == 5.87
        assert ef.engine == "regex"

    def test_confidence_out_of_range_raises(self):
        with pytest.raises(ValueError):
            make_ef(confidence=1.5)

    def test_confidence_negative_raises(self):
        with pytest.raises(ValueError):
            make_ef(confidence=-0.1)

    def test_confidence_boundaries_valid(self):
        make_ef(confidence=0.0)
        make_ef(confidence=1.0)

    def test_is_better_than_hard_vs_soft(self):
        hard_ef = make_ef(source=FieldSource.HARD, confidence=0.5)
        soft_ef = make_ef(source=FieldSource.SOFT, confidence=0.99)
        # HARD source wins even with lower confidence
        assert hard_ef.is_better_than(soft_ef)
        assert not soft_ef.is_better_than(hard_ef)

    def test_is_better_than_same_source_higher_confidence_wins(self):
        ef_low  = make_ef(source=FieldSource.SOFT, confidence=0.5)
        ef_high = make_ef(source=FieldSource.SOFT, confidence=0.8)
        assert ef_high.is_better_than(ef_low)
        assert not ef_low.is_better_than(ef_high)

    def test_is_better_than_equal_not_better(self):
        ef1 = make_ef(source=FieldSource.SOFT, confidence=0.7)
        ef2 = make_ef(source=FieldSource.SOFT, confidence=0.7)
        # Equal confidence and source → neither is better
        assert not ef1.is_better_than(ef2)
        assert not ef2.is_better_than(ef1)

    def test_is_better_than_heuristic_vs_soft(self):
        heur = make_ef(source=FieldSource.HEURISTIC, confidence=0.3)
        soft = make_ef(source=FieldSource.SOFT, confidence=1.0)
        assert heur.is_better_than(soft)

    def test_provenance_dict_keys(self):
        ef = make_ef(engine="camelot", page=2)
        prov = ef.to_provenance_dict()
        assert "source" in prov
        assert "doc_type" in prov
        assert "confidence" in prov
        assert "page" in prov
        assert "engine" in prov
        assert prov["page"] == 2
        assert prov["engine"] == "camelot"

    def test_none_value_allowed(self):
        ef = make_ef(value=None)
        assert ef.value is None


# ─────────────────────────────────────────────────────────────────────────────
# set_field (module-level helper)
# ─────────────────────────────────────────────────────────────────────────────

class TestSetField:
    def test_insert_new_key(self):
        fields: dict = {}
        ef = make_ef(key="creatinine_d1", value=108.0, source=FieldSource.HARD)
        set_field(fields, "creatinine_d1", ef)
        assert fields["creatinine_d1"] is ef

    def test_hard_replaces_soft(self):
        fields: dict = {}
        soft = make_ef(value="soft_val", source=FieldSource.SOFT, confidence=0.9)
        hard = make_ef(value="hard_val", source=FieldSource.HARD, confidence=0.5)
        set_field(fields, "k", soft)
        set_field(fields, "k", hard)
        assert fields["k"].value == "hard_val"

    def test_soft_does_not_replace_hard(self):
        fields: dict = {}
        hard = make_ef(value="hard_val", source=FieldSource.HARD, confidence=0.5)
        soft = make_ef(value="soft_val", source=FieldSource.SOFT, confidence=1.0)
        set_field(fields, "k", hard)
        set_field(fields, "k", soft)
        assert fields["k"].value == "hard_val"

    def test_higher_confidence_same_source_wins(self):
        fields: dict = {}
        low  = make_ef(value="low",  source=FieldSource.SOFT, confidence=0.4)
        high = make_ef(value="high", source=FieldSource.SOFT, confidence=0.8)
        set_field(fields, "k", low)
        set_field(fields, "k", high)
        assert fields["k"].value == "high"

    def test_lower_confidence_same_source_does_not_replace(self):
        fields: dict = {}
        high = make_ef(value="high", source=FieldSource.SOFT, confidence=0.8)
        low  = make_ef(value="low",  source=FieldSource.SOFT, confidence=0.4)
        set_field(fields, "k", high)
        set_field(fields, "k", low)
        assert fields["k"].value == "high"

    def test_multiple_keys_independent(self):
        fields: dict = {}
        ef1 = make_ef(key="k1", value="a")
        ef2 = make_ef(key="k2", value="b")
        set_field(fields, "k1", ef1)
        set_field(fields, "k2", ef2)
        assert fields["k1"].value == "a"
        assert fields["k2"].value == "b"


# ─────────────────────────────────────────────────────────────────────────────
# CaseResult
# ─────────────────────────────────────────────────────────────────────────────

class TestCaseResult:
    def test_default_empty(self):
        cr = CaseResult(case_id="c1")
        assert cr.fields == {}
        assert cr.conflicts == []
        assert cr.warnings == []

    def test_set_field_inserts(self):
        cr = CaseResult(case_id="c1")
        ef = make_ef(key="sodium_d1", value=132.0, source=FieldSource.HARD)
        cr.set_field("sodium_d1", ef)
        assert cr.fields["sodium_d1"].value == 132.0

    def test_set_field_hard_beats_soft(self):
        cr = CaseResult(case_id="c1")
        soft = make_ef(value="Unknown", source=FieldSource.SOFT, confidence=0.7)
        hard = make_ef(value=132.0,    source=FieldSource.HARD, confidence=0.95)
        cr.set_field("sodium_d1", soft)
        cr.set_field("sodium_d1", hard)
        assert cr.fields["sodium_d1"].value == 132.0

    def test_set_field_soft_does_not_overwrite_hard(self):
        cr = CaseResult(case_id="c1")
        hard = make_ef(value=132.0,  source=FieldSource.HARD, confidence=0.95)
        soft = make_ef(value="high", source=FieldSource.SOFT, confidence=1.0)
        cr.set_field("sodium_d1", hard)
        cr.set_field("sodium_d1", soft)
        assert cr.fields["sodium_d1"].value == 132.0

    def test_to_plain(self):
        cr = CaseResult(case_id="c1")
        cr.set_field("wbc_d1",  make_ef(value=5.87))
        cr.set_field("hgb_d1",  make_ef(value=97.0))
        plain = cr.to_plain()
        assert plain == {"wbc_d1": 5.87, "hgb_d1": 97.0}

    def test_to_provenance_includes_all_keys(self):
        cr = CaseResult(case_id="c1")
        cr.set_field("wbc_d1", make_ef(value=5.87, engine="regex"))
        prov = cr.to_provenance()
        assert "wbc_d1" in prov
        assert prov["wbc_d1"]["engine"] == "regex"

    def test_add_warning(self):
        cr = CaseResult(case_id="c1")
        cr.add_warning("Lab date could not be parsed")
        assert len(cr.warnings) == 1
        assert "parsed" in cr.warnings[0]

    def test_has_conflicts_false_when_empty(self):
        assert not CaseResult(case_id="c1").has_conflicts

    def test_has_conflicts_true_after_append(self):
        cr = CaseResult(case_id="c1")
        cr.conflicts.append(
            ConflictRecord(
                case_id="c1",
                field="sodium_d1",
                source_policy="DISCHARGE_PRIMARY",
                retained_value=132.0,
                retained_doc_type=DocType.DISCHARGE_SUMMARY,
                overridden_value=135.0,
                overridden_doc_type=DocType.LAB_REPORT,
                page=None,
            )
        )
        assert cr.has_conflicts

    def test_field_count(self):
        cr = CaseResult(case_id="c1")
        cr.set_field("a", make_ef())
        cr.set_field("b", make_ef())
        assert cr.field_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# Record dataclasses (ConflictRecord, MismatchRecord, OutOfWindowRecord)
# ─────────────────────────────────────────────────────────────────────────────

class TestRecords:
    def test_conflict_record_frozen(self):
        rec = ConflictRecord(
            case_id="c1", field="sodium_d1",
            source_policy="DISCHARGE_PRIMARY",
            retained_value=132.0,
            retained_doc_type=DocType.DISCHARGE_SUMMARY,
            overridden_value=135.0,
            overridden_doc_type=DocType.LAB_REPORT,
            page=0,
        )
        with pytest.raises((AttributeError, TypeError)):
            rec.field = "other"  # type: ignore[misc]

    def test_mismatch_record(self):
        rec = MismatchRecord(
            case_id="c1", filename="bad_doc.pdf",
            expected_fingerprint="abc123def456",
            found_name="Más Beteg", found_taj="999888777",
        )
        assert rec.expected_fingerprint == "abc123def456"
        assert rec.found_name == "Más Beteg"

    def test_out_of_window_record(self):
        rec = OutOfWindowRecord(
            case_id="c1", filename="followup.pdf",
            doc_type=DocType.ELASTOGRAPHY,
            event_date=date(2026, 2, 15),
            days_from_admission=34,
            window_type=WindowType.POST_DISCHARGE,
            summary="Fibroscan follow-up after discharge",
        )
        assert rec.window_type == WindowType.POST_DISCHARGE
        assert rec.days_from_admission == 34


# ─────────────────────────────────────────────────────────────────────────────
# to_plain module-level helper
# ─────────────────────────────────────────────────────────────────────────────

class TestToPlain:
    def test_returns_values_only(self):
        fields = {
            "k1": make_ef(value="yes"),
            "k2": make_ef(value=42.0),
        }
        plain = to_plain(fields)
        assert plain == {"k1": "yes", "k2": 42.0}

    def test_none_value_preserved(self):
        fields = {"k1": make_ef(value=None)}
        assert to_plain(fields) == {"k1": None}

    def test_empty_dict(self):
        assert to_plain({}) == {}
