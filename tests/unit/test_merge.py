"""
tests/unit/test_merge.py

Unit tests for app/pipeline/step6_merge.py

Run with:
    PYTHONPATH=. \
        python3 tests/unit/test_merge.py

All tests use a fixed anchor:
    admission  2026-01-12
    discharge  2026-01-20
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT))

from app.domain.enums import DocType, FieldSource, WindowType
from app.domain.models import (
    ExtractedField,
    PatientAnchor,
)
from app.pipeline.step6_merge import DocInput, _values_conflict, merge


# ── Fixtures ──────────────────────────────────────────────────────────────────

ANCHOR = PatientAnchor.create(
    name="Beteg Neve",
    dob=date(1960, 1, 1),
    taj="123456789",
    admission_start=datetime(2026, 1, 12, 4, 43),
    discharge_end=datetime(2026, 1, 20, 13, 32),
)

CORRECT_FP = ANCHOR.fingerprint


def _ef(
    key: str,
    value: Any,
    source: FieldSource = FieldSource.HARD,
    doc_type: DocType = DocType.DISCHARGE_SUMMARY,
    confidence: float = 0.95,
    page: int | None = None,
    engine: str = "test",
) -> ExtractedField:
    return ExtractedField(
        key=key,
        value=value,
        source=source,
        doc_type=doc_type,
        confidence=confidence,
        page=page,
        engine=engine,
    )


def _dis_doc(fields: list[ExtractedField], **kwargs) -> DocInput:
    """Convenience: create a discharge DocInput."""
    return DocInput(
        doc_type=DocType.DISCHARGE_SUMMARY,
        fields=fields,
        filename="FS_záró.pdf",
        **kwargs,
    )


def _anc_doc(
    fields: list[ExtractedField],
    doc_type: DocType = DocType.LAB_REPORT,
    doc_date: date | None = None,
    doc_fingerprint: str | None = None,
    **kwargs,
) -> DocInput:
    """Convenience: create an ancillary DocInput."""
    return DocInput(
        doc_type=doc_type,
        fields=fields,
        doc_date=doc_date,
        doc_fingerprint=doc_fingerprint,
        **kwargs,
    )

def _make_registry_fd(field_name: str, source_policy: str = "DISCHARGE_PRIMARY"):
    """Minimal FieldDefinition stand-in for registry= argument in merge()."""
    # Import inline to avoid circular deps in the test module
    from app.registry.models import FieldDefinition
    return FieldDefinition(
        field_name=field_name,
        field_base=field_name,
        column_header=field_name,
        form_id="Bmid",
        timepoint="inhosp",
        field_type="open",
        value_type="text",
        allowed_values=(),
        enum_set=None,
        source_policy=source_policy,
        section_hint="Vizsgálatok eredményei",
        routing=(),
        repeat_group=None,
        scope=None,
        canonical=False,
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


# ── Test runner helpers ───────────────────────────────────────────────────────

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


def assert_eq(actual, expected, msg: str = ""):
    if actual != expected:
        raise AssertionError(
            f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}"
        )


def assert_true(cond, msg: str = ""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


def assert_false(cond, msg: str = ""):
    if cond:
        raise AssertionError(msg or "expected False, got True")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: _values_conflict helper
# ═══════════════════════════════════════════════════════════════════════════════

def test_values_conflict_both_meaningful_and_differ():
    assert_true(_values_conflict("Igen", "Nem"), "Igen vs Nem should conflict")


def test_values_conflict_same_value():
    assert_false(_values_conflict("Igen", "Igen"), "same value should not conflict")


def test_values_conflict_one_side_unknown():
    assert_false(_values_conflict("Unknown", "Igen"), "Unknown vs real = enrichment, not conflict")
    assert_false(_values_conflict("Igen", "Unknown"))
    assert_false(_values_conflict(None, "Igen"))
    assert_false(_values_conflict("Igen", None))
    assert_false(_values_conflict("NA", "Igen"))


def test_values_conflict_both_absent():
    assert_false(_values_conflict(None, None))
    assert_false(_values_conflict("Unknown", "Unknown"))


def test_values_conflict_numeric_string_normalisation():
    # "1.0" vs "1" — treated as same (both stringify to different forms though)
    # Ensure we don't crash; values that truly differ still conflict
    assert_true(_values_conflict("1.5", "2.0"))
    assert_false(_values_conflict("1.5", "1.5"))


def test_values_conflict_no_data_is_absent():
    assert_false(_values_conflict("No Data", "Igen"))
    assert_false(_values_conflict("Igen", "No Data"))


def test_values_conflict_fuzzy_case_whitespace():
    assert_false(_values_conflict("Negatív  lelet", "negatív lelet"))


def test_values_conflict_fuzzy_number_with_unit():
    # Same number, one side carries the unit → agreement, not conflict
    assert_false(_values_conflict("75.0 kpa", "75"))
    assert_false(_values_conflict("1", "1.0"))
    # Different numbers always conflict, even with similar text
    assert_true(_values_conflict("75.0 kpa", "35"))
    assert_true(_values_conflict("75.0 kpa", "75.1 kpa"))


def test_values_conflict_fuzzy_prefix_truncation():
    long = "Negatív colonoscopos lelet.\n**2026.01.16 Fibroscan is történt**"
    short = "Negatív colonoscopos lelet."
    assert_false(_values_conflict(short, long))
    assert_false(_values_conflict(long, short))
    # Short enums must NOT be prefix-matched ("Igen" vs "Igen, súlyos")
    assert_true(_values_conflict("Igen", "Igen, súlyos fokú"))


def test_values_conflict_fuzzy_similarity():
    # Small OCR/punctuation noise in long strings → no conflict
    assert_false(_values_conflict(
        "Az előkészítés megfelelő (BBPS: 3-2-2). Az eszköz felvezethető.",
        "Az elökészítés megfelelö (BBPS: 3-2-2). Az eszköz felvezethető.",
    ))
    # Genuinely different findings still conflict
    assert_true(_values_conflict(
        "['Gyomorvarix vérzés']", "['Nyelőcsővarix vérzés']",
    ))


def test_values_conflict_dates_always_strict():
    assert_true(_values_conflict("2026-01-16", "2026-01-17"))
    assert_true(_values_conflict("2026.01.16", "2026.01.17"))


def test_values_conflict_unit_magnitude_conversion():
    # Same physical quantity expressed in convertible units → agreement
    assert_false(_values_conflict("1 g", "1000 mg"))
    assert_false(_values_conflict("1,5 g/dl", "15 g/L"))
    assert_false(_values_conflict("84 µmol/L", "0.084 mmol/L"))
    # Same numeral but different magnitude / non-convertible unit → conflict
    assert_true(_values_conflict("2 mg", "2 g"))
    assert_true(_values_conflict("2 mg", "2 mmol"))
    assert_true(_values_conflict("75 kPa", "75 mmHg"))
    # Different quantity in convertible units → conflict
    assert_true(_values_conflict("1 g", "500 mg"))


def test_values_conflict_antonym_guard():
    """Long strings differing only in a clinical antonym must never be
    fuzzy-merged, regardless of similarity ratio."""
    a = "máj: benignus elváltozás, egyebekben eltérés nélkül, cysta."
    b = "máj: malignus elváltozás, egyebekben eltérés nélkül, cysta."
    assert_true(_values_conflict(a, b), "benignus vs malignus must conflict")
    assert_true(_values_conflict(
        "bal lebenyben 2 cm-es cysta ábrázolódik, egyéb eltérés nélkül",
        "jobb lebenyben 2 cm-es cysta ábrázolódik, egyéb eltérés nélkül",
    ), "bal vs jobb must conflict")
    assert_true(_values_conflict(
        "szérum albumin szint emelkedett, kontroll javasolt hetente",
        "szérum albumin szint csökkent, kontroll javasolt hetente",
    ), "emelkedett vs csökkent must conflict")


def test_values_conflict_negation_guard():
    """A negation particle present on only one side disables fuzzy merging."""
    assert_true(_values_conflict(
        "varix a nyelőcsőben látható, tágult formában",
        "varix a nyelőcsőben nem látható, nem tágult formában",
    ))
    # Negation present on BOTH sides → guard passes, prefix rule may apply
    assert_false(_values_conflict(
        "varix nem látható a nyelőcsőben",
        "varix nem látható a nyelőcsőben. kontroll nem szükséges",
    ))


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: merge() — basic field operations
# ═══════════════════════════════════════════════════════════════════════════════

def test_single_discharge_doc_fields_are_kept():
    """All discharge fields end up in master."""
    dis = _dis_doc([
        _ef("creatinine_d1", 108.0),
        _ef("inr_d1", 1.62),
    ])
    master, conflicts, mismatches, oow = merge([dis], ANCHOR, "case1")
    assert_eq(len(master), 2)
    assert_eq(master["creatinine_d1"].value, 108.0)
    assert_eq(master["inr_d1"].value, 1.62)
    assert_eq(len(conflicts), 0)
    assert_eq(len(mismatches), 0)
    assert_eq(len(oow), 0)


def test_ancillary_fills_field_missing_from_discharge():
    """Ancillary value enriches master when discharge didn't have the field."""
    dis = _dis_doc([_ef("creatinine_d1", 108.0)])
    anc = _anc_doc(
        [_ef("wbc_d5", 5.87, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
        doc_date=date(2026, 1, 16),
    )
    master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1")
    assert_eq(len(master), 2, "should have both fields")
    assert_eq(master["wbc_d5"].value, 5.87)
    assert_eq(len(conflicts), 0)


def test_lab_report_wins_over_discharge_on_conflict():
    """LAB_REPORT is authoritative: its value wins over the discharge summary
    even when the registry policy for the field is DISCHARGE_PRIMARY. The
    merge step promotes LAB_REPORT contributions to EXAM_DOC_PRIMARY at
    conflict time."""
    registry = [_make_registry_fd("creatinine_d1", "DISCHARGE_PRIMARY")]
    dis = _dis_doc([_ef("creatinine_d1", 108.0, confidence=0.7, source=FieldSource.SOFT)])
    anc = _anc_doc(
        [_ef("creatinine_d1", 120.0, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1", registry=registry)
    assert_eq(master["creatinine_d1"].value, 120.0, "LAB_REPORT value must win on conflict")
    assert_eq(len(conflicts), 1)
    c = conflicts[0]
    assert_eq(c.source_policy, "EXAM_DOC_PRIMARY")
    assert_eq(c.retained_value, 120.0)
    assert_eq(c.retained_doc_type, DocType.LAB_REPORT)
    assert_eq(c.overridden_value, 108.0)
    assert_eq(c.overridden_doc_type, DocType.DISCHARGE_SUMMARY)


def test_event_primary_ancillary_beats_discharge_on_conflict():
    """EXAM_DOC_PRIMARY: event document value wins when sources disagree."""
    registry = [_make_registry_fd("fibroscan_f_kategoria_inhosp", "EXAM_DOC_PRIMARY")]
    dis = _dis_doc([_ef("fibroscan_f_kategoria_inhosp", "F3", source=FieldSource.SOFT, confidence=0.7)])
    anc = _anc_doc(
        [_ef("fibroscan_f_kategoria_inhosp", "F4", source=FieldSource.HARD, confidence=0.95,
             doc_type=DocType.ELASTOGRAPHY)],
        doc_type=DocType.ELASTOGRAPHY,
    )
    master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1", registry=registry)
    assert_eq(master["fibroscan_f_kategoria_inhosp"].value, "F4",
              "event doc value must win on EXAM_DOC_PRIMARY conflict")
    assert_eq(len(conflicts), 1)
    c = conflicts[0]
    assert_eq(c.source_policy, "EXAM_DOC_PRIMARY")
    assert_eq(c.retained_value, "F4")
    assert_eq(c.retained_doc_type, DocType.ELASTOGRAPHY)
    assert_eq(c.overridden_value, "F3")
    assert_eq(c.overridden_doc_type, DocType.DISCHARGE_SUMMARY)


def test_event_primary_agreement_no_conflict():
    """EXAM_DOC_PRIMARY: same value from both sources produces no conflict."""
    registry = [_make_registry_fd("fibroscan_f_kategoria_inhosp", "EXAM_DOC_PRIMARY")]
    dis = _dis_doc([_ef("fibroscan_f_kategoria_inhosp", "F4", source=FieldSource.SOFT)])
    anc = _anc_doc(
        [_ef("fibroscan_f_kategoria_inhosp", "F4", source=FieldSource.HARD, doc_type=DocType.ELASTOGRAPHY)],
        doc_type=DocType.ELASTOGRAPHY,
    )
    master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1", registry=registry)
    assert_eq(master["fibroscan_f_kategoria_inhosp"].value, "F4")
    assert_eq(len(conflicts), 0, "no conflict when values agree")


def test_event_primary_discharge_enriches_when_event_doc_absent():
    """EXAM_DOC_PRIMARY: discharge value fills the field when no event doc has it."""
    registry = [_make_registry_fd("fibroscan_f_kategoria_inhosp", "EXAM_DOC_PRIMARY")]
    dis = _dis_doc([_ef("fibroscan_f_kategoria_inhosp", "F4", source=FieldSource.SOFT)])
    master, conflicts, _mismatches, _oow = merge([dis], ANCHOR, "case1", registry=registry)
    assert_eq(master["fibroscan_f_kategoria_inhosp"].value, "F4",
              "discharge value should enrich when no event doc exists")
    assert_eq(len(conflicts), 0)


def test_same_value_in_discharge_and_ancillary_no_conflict():
    """Same value from both sources → no conflict record."""
    dis = _dis_doc([_ef("inr_d1", 1.62)])
    anc = _anc_doc(
        [_ef("inr_d1", 1.62, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    _master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1")
    assert_eq(len(conflicts), 0, "no conflict when values agree")


def test_ancillary_unknown_does_not_conflict_with_discharge_value():
    """Ancillary field with Unknown value → enrichment attempt, no conflict."""
    dis = _dis_doc([_ef("ascites_grade_adm", "Mérsékelt")])
    anc = _anc_doc(
        [_ef("ascites_grade_adm", "Unknown", doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1")
    assert_eq(len(conflicts), 0)
    assert_eq(master["ascites_grade_adm"].value, "Mérsékelt", "discharge kept")


def test_discharge_unknown_ancillary_real_value_enrichment():
    """When discharge has Unknown and ancillary has real value → enrich, no conflict."""
    dis = _dis_doc([_ef("wbc_d5", "Unknown", source=FieldSource.SOFT, confidence=0.7)])
    anc = _anc_doc(
        [_ef("wbc_d5", 5.87, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1")
    assert_eq(len(conflicts), 0, "Unknown→real should be enrichment not conflict")
    # The HARD ancillary value should win (set_field allows this since it's better)
    assert_eq(master["wbc_d5"].value, 5.87, "ancillary HARD value should enrich Unknown discharge")


def test_multiple_conflicts_all_logged():
    """Multiple differing fields each produce a ConflictRecord."""
    dis = _dis_doc([
        _ef("creatinine_d1", 108.0),
        _ef("inr_d1", 1.62),
        _ef("sodium_d1", 132.0),
    ])
    anc = _anc_doc(
        [
            _ef("creatinine_d1", 115.0, doc_type=DocType.LAB_REPORT),
            _ef("inr_d1", 1.70, doc_type=DocType.LAB_REPORT),
            _ef("sodium_d1", 132.0, doc_type=DocType.LAB_REPORT),  # same → no conflict
        ],
        doc_type=DocType.LAB_REPORT,
    )
    _master, conflicts, _mismatches, _oow = merge([dis, anc], ANCHOR, "case1")
    assert_eq(len(conflicts), 2, "creatinine and inr conflict; sodium agrees")


def test_empty_docs_returns_empty_master():
    """No documents → empty master, no records."""
    master, conflicts, mismatches, oow = merge([], ANCHOR, "case1")
    assert_eq(master, {})
    assert_eq(conflicts, [])
    assert_eq(mismatches, [])
    assert_eq(oow, [])


def test_ancillary_only_no_discharge():
    """If there's no discharge doc, ancillary fields still populate master."""
    anc = _anc_doc(
        [_ef("creatinine_d1", 108.0, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    master, conflicts, _mismatches, _oow = merge([anc], ANCHOR, "case1")
    assert_eq(master["creatinine_d1"].value, 108.0)
    assert_eq(len(conflicts), 0)


def test_discharge_processed_first_regardless_of_input_order():
    """Even if discharge is passed last, it's processed first and wins.

    Uses a MICROBIOLOGY ancillary because LAB_REPORT is now promoted to
    EXAM_DOC_PRIMARY at merge time — the goal here is to verify input order
    does not change DISCHARGE_PRIMARY resolution, so we pick a doc_type that
    is not subject to the LAB promotion rule.
    """
    registry = [_make_registry_fd("creatinine_d1", "DISCHARGE_PRIMARY")]
    anc = _anc_doc(
        [_ef("creatinine_d1", 999.0, doc_type=DocType.MICROBIOLOGY, source=FieldSource.HARD, confidence=0.99)],
        doc_type=DocType.MICROBIOLOGY,
    )
    dis = _dis_doc([_ef("creatinine_d1", 108.0, source=FieldSource.SOFT, confidence=0.7)])
    master, conflicts, _mismatches, _oow = merge([anc, dis], ANCHOR, "case1", registry=registry)
    assert_eq(master["creatinine_d1"].value, 108.0, "discharge must win even when passed last")
    assert_eq(len(conflicts), 1)
    assert_eq(conflicts[0].retained_doc_type, DocType.DISCHARGE_SUMMARY)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: fingerprint mismatch
# ═══════════════════════════════════════════════════════════════════════════════

def test_correct_fingerprint_no_mismatch():
    """Document with matching fingerprint is merged normally."""
    anc = _anc_doc(
        [_ef("wbc_d2", 8.12, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
        doc_fingerprint=CORRECT_FP,
        found_name="Beteg Neve",
        found_taj="123456789",
    )
    master, _conflicts, mismatches, _oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(mismatches), 0)
    assert_eq(master["wbc_d2"].value, 8.12)


def test_wrong_fingerprint_mismatch_record_and_fields_skipped():
    """Document with mismatched fingerprint produces MismatchRecord; fields skipped."""
    anc = _anc_doc(
        [_ef("wbc_d2", 8.12, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
        doc_fingerprint="wrong123fp00",
        found_name="Mas Beteg",
        found_taj="999999999",
        filename="wrong_patient.pdf",
    )
    master, _conflicts, mismatches, _oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(mismatches), 1)
    m = mismatches[0]
    assert_eq(m.expected_fingerprint, CORRECT_FP)
    assert_eq(m.found_name, "Mas Beteg")
    assert_eq(m.found_taj, "999999999")
    assert_eq(m.filename, "wrong_patient.pdf")
    assert_eq(len(master), 0, "fields from mismatched doc must be skipped")


def test_no_fingerprint_provided_skips_identity_check():
    """If doc_fingerprint is None, no identity check is performed."""
    anc = _anc_doc(
        [_ef("wbc_d2", 8.12, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
        doc_fingerprint=None,  # explicitly None → skip check
    )
    master, _conflicts, mismatches, _oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(mismatches), 0, "no fingerprint → no check → no mismatch")
    assert_eq(master["wbc_d2"].value, 8.12)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: out-of-window documents
# ═══════════════════════════════════════════════════════════════════════════════

def test_in_hospital_document_not_out_of_window():
    """Document dated during admission → NOT out-of-window → fields merged."""
    anc = _anc_doc(
        [_ef("fibro_d5_1_e_kpa", 75.0, doc_type=DocType.ELASTOGRAPHY)],
        doc_type=DocType.ELASTOGRAPHY,
        doc_date=date(2026, 1, 16),   # day 5 — IN_HOSPITAL
    )
    master, _conflicts, _mismatches, oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(oow), 0)
    assert_eq(master["fibro_d5_1_e_kpa"].value, 75.0)


def test_post_discharge_document_out_of_window():
    """Fibroscan dated after discharge → OutOfWindowRecord, fields skipped."""
    anc = _anc_doc(
        [_ef("fibro_d10_1_e_kpa", 75.0, doc_type=DocType.ELASTOGRAPHY)],
        doc_type=DocType.ELASTOGRAPHY,
        doc_date=date(2026, 1, 21),   # one day after discharge (2026-01-20)
        filename="FS_fibro.pdf",
    )
    master, _conflicts, _mismatches, oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(oow), 1)
    r = oow[0]
    assert_eq(r.doc_type, DocType.ELASTOGRAPHY)
    assert_eq(r.event_date, date(2026, 1, 21))
    assert_true(r.days_from_admission > 0, "should be positive (post-discharge)")
    assert_eq(r.window_type, WindowType.POST_DISCHARGE)
    assert_eq(r.filename, "FS_fibro.pdf")
    assert_eq(len(master), 0, "fields from out-of-window doc must be skipped")


def test_pre_admission_document_out_of_window():
    """Document dated well before admission → OutOfWindowRecord, fields skipped."""
    anc = _anc_doc(
        [_ef("some_old_lab", 1.0, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
        doc_date=date(2025, 12, 1),   # way before admission
        filename="old_lab.pdf",
    )
    master, _conflicts, _mismatches, oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(oow), 1)
    r = oow[0]
    assert_eq(r.window_type, WindowType.PRE_ADMISSION)
    assert_true(r.days_from_admission < 0)
    assert_eq(len(master), 0)


def test_no_doc_date_skips_window_check():
    """If doc_date is None, no window check is performed; fields are merged."""
    anc = _anc_doc(
        [_ef("wbc_d2", 8.12, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
        doc_date=None,  # unknown date → skip check
    )
    master, _conflicts, _mismatches, oow = merge([anc], ANCHOR, "case1")
    assert_eq(len(oow), 0, "no date → no check → no out-of-window")
    assert_eq(master["wbc_d2"].value, 8.12)


def test_post_discharge_day_keyed_fields_dropped_rest_merged():
    """Ancillary doc without doc_date: its day-10 fields (after discharge
    2026-01-20 = day 9) are logged and dropped; its registry answer merges."""
    anc = _anc_doc(
        [
            _ef("fibro_d10_1_E_kpa", 75.0, doc_type=DocType.ELASTOGRAPHY),
            _ef("fibro_d10_1_fibrosis_stage", "F4", doc_type=DocType.ELASTOGRAPHY),
            _ef("tortent_e_fibroscan_inhosp", "Igen", doc_type=DocType.ELASTOGRAPHY),
        ],
        doc_type=DocType.ELASTOGRAPHY,
        filename="FS_fibro.pdf",
    )
    registry = [_make_registry_fd("tortent_e_fibroscan_inhosp")]
    master, _conflicts, _mismatches, oow = merge([anc], ANCHOR, "case1", registry=registry)
    assert_eq(sorted(master), ["tortent_e_fibroscan_inhosp"])
    assert_eq(len(oow), 1)
    r = oow[0]
    assert_eq(r.event_date, date(2026, 1, 21))
    assert_eq(r.days_from_admission, 10)
    assert_eq(r.window_type, WindowType.POST_DISCHARGE)
    assert_eq(r.filename, "FS_fibro.pdf")
    assert_true("fibro_d10_1_E_kpa" in r.summary and "fibro_d10_1_fibrosis_stage" in r.summary,
                r.summary)


def test_pre_admission_lab_dropped_admission_window_lab_kept():
    """Day -1/-2 is the admission window (kept); day -30 is pre-admission."""
    anc = _anc_doc(
        [
            _ef("creatinine_d-30", 150.0),
            _ef("creatinine_d-2", 120.0),
            _ef("creatinine_d-1", 108.0),
            _ef("creatinine_d9", 80.0),
        ],
        filename="labor.pdf",
    )
    master, _conflicts, _mismatches, oow = merge([anc], ANCHOR, "case1")
    assert_eq(sorted(master), ["creatinine_d-1", "creatinine_d-2", "creatinine_d9"])
    assert_eq([r.window_type for r in oow], [WindowType.PRE_ADMISSION])
    assert_eq(oow[0].event_date, date(2025, 12, 13))


def test_discharge_day_keyed_fields_never_dropped():
    """The window rule applies to ancillary documents only."""
    dis = _dis_doc([_ef("endo_gastro_d79_1_date", "2026-03-31")])
    master, _conflicts, _mismatches, oow = merge([dis], ANCHOR, "case1")
    assert_eq(len(oow), 0)
    assert_true("endo_gastro_d79_1_date" in master)


def test_discharge_doc_never_checked_for_window():
    """Discharge document is never out-of-window even if its date is weird."""
    # This shouldn't happen in practice, but ensure discharge is never rejected.
    dis = DocInput(
        doc_type=DocType.DISCHARGE_SUMMARY,
        fields=[_ef("creatinine_d1", 108.0)],
        filename="FS_záró.pdf",
        doc_date=date(2030, 1, 1),  # absurd future date
    )
    master, _conflicts, _mismatches, oow = merge([dis], ANCHOR, "case1")
    assert_eq(len(oow), 0, "discharge doc must never be out-of-window")
    assert_eq(master["creatinine_d1"].value, 108.0)


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: priority and confidence resolution
# ═══════════════════════════════════════════════════════════════════════════════

def test_higher_confidence_ancillary_beats_absent_discharge_field():
    """Ancillary HARD 0.95 fills field not in discharge, winning over any soft."""
    anc1 = _anc_doc(
        [_ef("plt_d2", 104.0, source=FieldSource.SOFT, confidence=0.7, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    anc2 = _anc_doc(
        [_ef("plt_d2", 104.0, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT)],
        doc_type=DocType.LAB_REPORT,
    )
    master, conflicts, _mismatches, _oow = merge([anc1, anc2], ANCHOR, "case1")
    assert_eq(master["plt_d2"].source, FieldSource.HARD, "HARD should win")
    assert_eq(len(conflicts), 0, "same value → no conflict")


def test_conflict_record_contains_correct_case_id():
    """Uses MICROBIOLOGY (not LAB_REPORT) so the registry's DISCHARGE_PRIMARY
    policy is actually the one applied — LAB_REPORT would be promoted to
    EXAM_DOC_PRIMARY at merge time."""
    registry = [_make_registry_fd("inr_d1", "DISCHARGE_PRIMARY")]
    dis = _dis_doc([_ef("inr_d1", 1.62)])
    anc = _anc_doc(
        [_ef("inr_d1", 1.86, doc_type=DocType.MICROBIOLOGY)],
        doc_type=DocType.MICROBIOLOGY,
    )
    _, conflicts, _, _ = merge([dis, anc], ANCHOR, "CASE_XYZ", registry=registry)
    assert_eq(conflicts[0].case_id, "CASE_XYZ")
    assert_eq(conflicts[0].source_policy, "DISCHARGE_PRIMARY")


# ═══════════════════════════════════════════════════════════════════════════════
# Tests: integration scenario (realistic fixture-like case)
# ═══════════════════════════════════════════════════════════════════════════════

def test_realistic_case_discharge_plus_lab_plus_endo_plus_post_fibro():
    """
    Simulated realistic merge:
    - Discharge: soft fields + some labs
    - Lab report (day 5, in-window): additional labs
    - Endoscopy (day 2, in-window): event fields
    - Fibroscan (day 10, post-discharge): → out-of-window
    """
    dis_fields = [
        _ef("child_pugh_adm", "C/11", source=FieldSource.SOFT, confidence=0.7),
        _ef("meld_adm", 25, source=FieldSource.SOFT, confidence=0.7),
        _ef("creatinine_d1", 108.0, source=FieldSource.HARD, confidence=0.95),
        _ef("inr_d1", 1.62, source=FieldSource.HARD, confidence=0.95),
    ]
    dis = _dis_doc(dis_fields)

    lab_fields = [
        _ef("wbc_d5", 5.87, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT),
        _ef("plt_d5", 104.0, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT),
        _ef("creatinine_d5", 95.0, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT),
        # Conflict: discharge says 108, lab says 110 for day1 (different draw labelled same key)
        _ef("creatinine_d1", 110.0, source=FieldSource.HARD, confidence=0.95, doc_type=DocType.LAB_REPORT),
    ]
    lab = _anc_doc(lab_fields, doc_type=DocType.LAB_REPORT, doc_date=date(2026, 1, 16))

    endo_fields = [
        _ef("endo_gastro_d2_1_date", "2026-01-13", source=FieldSource.HEURISTIC, confidence=0.9, doc_type=DocType.ENDOSCOPY),
        _ef("endo_gastro_d2_1_result", "Fundális varicositas", source=FieldSource.HEURISTIC, confidence=0.9, doc_type=DocType.ENDOSCOPY),
    ]
    endo = _anc_doc(endo_fields, doc_type=DocType.ENDOSCOPY, doc_date=date(2026, 1, 13))

    fibro_fields = [
        _ef("fibro_d10_1_e_kpa", 75.0, source=FieldSource.HEURISTIC, confidence=0.9, doc_type=DocType.ELASTOGRAPHY),
    ]
    fibro = _anc_doc(fibro_fields, doc_type=DocType.ELASTOGRAPHY, doc_date=date(2026, 1, 21))

    registry = [_make_registry_fd("creatinine_d1", "DISCHARGE_PRIMARY")]
    master, conflicts, mismatches, oow = merge(
        [dis, lab, endo, fibro], ANCHOR, "case_realistic", registry=registry
    )

    # Discharge scalar fields present
    assert_true("child_pugh_adm" in master)
    assert_true("meld_adm" in master)

    # Lab day-5 fields enriched
    assert_eq(master["wbc_d5"].value, 5.87)
    assert_eq(master["plt_d5"].value, 104.0)
    assert_eq(master["creatinine_d5"].value, 95.0)

    # Endoscopy fields enriched
    assert_eq(master["endo_gastro_d2_1_date"].value, "2026-01-13")

    # Conflict on creatinine_d1 — LAB_REPORT is authoritative, so 110 wins
    # even though registry policy is DISCHARGE_PRIMARY (promoted at merge
    # time to EXAM_DOC_PRIMARY because the competing value comes from a lab
    # report).
    assert_eq(master["creatinine_d1"].value, 110.0, "LAB_REPORT creatinine_d1 must win")
    assert_eq(len(conflicts), 1, "one conflict (creatinine_d1)")
    assert_eq(conflicts[0].retained_value, 110.0)
    assert_eq(conflicts[0].retained_doc_type, DocType.LAB_REPORT)
    assert_eq(conflicts[0].overridden_value, 108.0)
    assert_eq(conflicts[0].overridden_doc_type, DocType.DISCHARGE_SUMMARY)
    assert_eq(conflicts[0].source_policy, "EXAM_DOC_PRIMARY")

    # Fibroscan is post-discharge → out-of-window, NOT in master
    assert_true("fibro_d10_1_e_kpa" not in master, "post-discharge fibro must be skipped")
    assert_eq(len(oow), 1)
    assert_eq(oow[0].doc_type, DocType.ELASTOGRAPHY)

    assert_eq(len(mismatches), 0)


# ═══════════════════════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tests = [
        # _values_conflict
        ("values_conflict: both meaningful and differ",
         test_values_conflict_both_meaningful_and_differ),
        ("values_conflict: same value",
         test_values_conflict_same_value),
        ("values_conflict: one side unknown/absent",
         test_values_conflict_one_side_unknown),
        ("values_conflict: both absent",
         test_values_conflict_both_absent),
        ("values_conflict: numeric string normalisation",
         test_values_conflict_numeric_string_normalisation),
        ("_values_conflict: No Data treated as absent",
         test_values_conflict_no_data_is_absent),
        ("_values_conflict: fuzzy case/whitespace",
         test_values_conflict_fuzzy_case_whitespace),
        ("_values_conflict: fuzzy number+unit",
         test_values_conflict_fuzzy_number_with_unit),
        ("_values_conflict: fuzzy prefix truncation",
         test_values_conflict_fuzzy_prefix_truncation),
        ("_values_conflict: fuzzy similarity",
         test_values_conflict_fuzzy_similarity),
        ("_values_conflict: dates strict",
         test_values_conflict_dates_always_strict),
        ("_values_conflict: unit magnitude conversion",
         test_values_conflict_unit_magnitude_conversion),
        ("_values_conflict: antonym guard",
         test_values_conflict_antonym_guard),
        ("_values_conflict: negation guard",
         test_values_conflict_negation_guard),
        # merge — basic
        ("merge: single discharge doc fields kept",
         test_single_discharge_doc_fields_are_kept),
        ("merge: ancillary fills field missing from discharge",
         test_ancillary_fills_field_missing_from_discharge),
        ("merge: LAB_REPORT wins over discharge on conflict (promoted policy)",
         test_lab_report_wins_over_discharge_on_conflict),
        ("merge: same value no conflict",
         test_same_value_in_discharge_and_ancillary_no_conflict),
        ("merge: ancillary Unknown does not conflict with discharge value",
         test_ancillary_unknown_does_not_conflict_with_discharge_value),
        ("merge: discharge Unknown enriched by ancillary HARD",
         test_discharge_unknown_ancillary_real_value_enrichment),
        ("merge: multiple conflicts all logged",
         test_multiple_conflicts_all_logged),
        ("merge: empty docs → empty master",
         test_empty_docs_returns_empty_master),
        ("merge: ancillary only no discharge",
         test_ancillary_only_no_discharge),
        ("merge: discharge processed first regardless of input order",
         test_discharge_processed_first_regardless_of_input_order),
        # fingerprint
        ("fingerprint: correct fingerprint no mismatch",
         test_correct_fingerprint_no_mismatch),
        ("fingerprint: wrong fingerprint → mismatch + fields skipped",
         test_wrong_fingerprint_mismatch_record_and_fields_skipped),
        ("fingerprint: None skips identity check",
         test_no_fingerprint_provided_skips_identity_check),
        # out-of-window
        ("window: in-hospital doc not out-of-window",
         test_in_hospital_document_not_out_of_window),
        ("window: post-discharge doc → out-of-window",
         test_post_discharge_document_out_of_window),
        ("window: pre-admission doc → out-of-window",
         test_pre_admission_document_out_of_window),
        ("window: None date skips window check",
         test_no_doc_date_skips_window_check),
        ("window: discharge doc never out-of-window",
         test_discharge_doc_never_checked_for_window),
        ("out-of-window: post-discharge day-keyed fields dropped, rest merged",
         test_post_discharge_day_keyed_fields_dropped_rest_merged),
        ("out-of-window: pre-admission lab dropped, admission-window lab kept",
         test_pre_admission_lab_dropped_admission_window_lab_kept),
        ("out-of-window: discharge day-keyed fields never dropped",
         test_discharge_day_keyed_fields_never_dropped),
        # priority
        ("priority: higher-confidence ancillary beats soft ancillary",
         test_higher_confidence_ancillary_beats_absent_discharge_field),
        ("priority: conflict record has correct case_id",
         test_conflict_record_contains_correct_case_id),
        # source_policy
        ("policy: LAB_REPORT promoted to EXAM_DOC_PRIMARY beats discharge",
         test_lab_report_wins_over_discharge_on_conflict),
        ("policy: EXAM_DOC_PRIMARY event doc wins on conflict",
         test_event_primary_ancillary_beats_discharge_on_conflict),
        ("policy: EXAM_DOC_PRIMARY agreement → no conflict",
         test_event_primary_agreement_no_conflict),
        ("policy: EXAM_DOC_PRIMARY discharge enriches when no event doc",
         test_event_primary_discharge_enriches_when_event_doc_absent),
        # integration
        ("integration: realistic case with 4 docs",
         test_realistic_case_discharge_plus_lab_plus_endo_plus_post_fibro),
    ]

    print(f"\n{'─' * 60}")
    print("  test_merge.py — Step 11: Merge & Conflict Detection")
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
