"""
tests/unit/test_normalize.py

Step 7 tests: day normalisation (app/pipeline/step4_normalize.py).

Tests cover:
  - compute_day() — all boundary cases, no day 0 invariant
  - is_within_admission_window() — endpoints inclusive, out-of-window cases
  - is_within_48h_of_admission() — boundary at ±2 days
  - classify_window() — all four WindowType values, boundary conditions
  - Combined scenarios matching clinical data patterns
"""
from __future__ import annotations

import sys
import traceback
from datetime import date, datetime, timedelta

from app.pipeline.step4_normalize import (
    classify_window,
    compute_day,
    is_within_48h_of_admission,
    is_within_admission_window,
)
from app.domain.enums import WindowType
from app.domain.models import PatientAnchor


# ─────────────────────────────────────────────────────────────────────────────
# Shared fixture anchor
# Admission: 2026-01-12, Discharge: 2026-01-20  (8-night stay)
# ─────────────────────────────────────────────────────────────────────────────

def make_anchor(
    admission: date = date(2026, 1, 12),
    discharge: date = date(2026, 1, 20),
) -> PatientAnchor:
    return PatientAnchor.create(
        name="Teszt Béla",
        dob=date(1960, 3, 15),
        taj="123456789",
        admission_start=datetime(admission.year, admission.month, admission.day, 4, 43),
        discharge_end=datetime(discharge.year, discharge.month, discharge.day, 13, 32),
    )


ANCHOR = make_anchor()
ADM = date(2026, 1, 12)   # admission date
DIS = date(2026, 1, 20)   # discharge date


# ─────────────────────────────────────────────────────────────────────────────
# compute_day() — core day calculation
# ─────────────────────────────────────────────────────────────────────────────

class TestComputeDay:
    """
    Spec:  delta >= 0 → delta + 1  |  delta < 0 → delta
    Day 0 must never be returned.
    """

    # ── Positive / zero delta ────────────────────────────────────────────────

    def test_admission_day_is_day_1(self):
        assert compute_day(ADM, ANCHOR) == 1

    def test_day_after_admission_is_day_2(self):
        assert compute_day(ADM + timedelta(days=1), ANCHOR) == 2

    def test_two_days_after_is_day_3(self):
        assert compute_day(ADM + timedelta(days=2), ANCHOR) == 3

    def test_discharge_day(self):
        # delta = 8 → day 9
        assert compute_day(DIS, ANCHOR) == 9

    def test_large_positive_delta(self):
        assert compute_day(ADM + timedelta(days=29), ANCHOR) == 30

    # ── Negative delta ───────────────────────────────────────────────────────

    def test_day_before_admission_is_minus_1(self):
        assert compute_day(ADM - timedelta(days=1), ANCHOR) == -1

    def test_two_days_before_is_minus_2(self):
        assert compute_day(ADM - timedelta(days=2), ANCHOR) == -2

    def test_three_days_before_is_minus_3(self):
        assert compute_day(ADM - timedelta(days=3), ANCHOR) == -3

    def test_large_negative_delta(self):
        assert compute_day(ADM - timedelta(days=30), ANCHOR) == -30

    # ── No day 0 invariant ───────────────────────────────────────────────────

    def test_day_0_never_returned_admission(self):
        assert compute_day(ADM, ANCHOR) != 0

    def test_day_0_never_returned_day_before(self):
        assert compute_day(ADM - timedelta(days=1), ANCHOR) != 0

    def test_day_0_never_returned_any_date(self):
        """Exhaustive: no date in a ±60 day window should produce day 0."""
        for delta in range(-60, 61):
            d = ADM + timedelta(days=delta)
            assert compute_day(d, ANCHOR) != 0, (
                f"compute_day returned 0 for delta={delta} (date={d})"
            )

    # ── Sequence monotonicity ────────────────────────────────────────────────

    def test_sequence_is_monotone(self):
        """
        Day numbers must strictly increase with date, with the only
        discontinuity at delta=-1 → delta=0 (jumping from -1 to +1).
        """
        days = [compute_day(ADM + timedelta(days=d), ANCHOR) for d in range(-3, 4)]
        # Expected: -3, -2, -1, 1, 2, 3, 4
        assert days == [-3, -2, -1, 1, 2, 3, 4]

    def test_jump_from_minus1_to_plus1(self):
        """The gap from day -1 to day 1 is intentional (no day 0)."""
        day_before = compute_day(ADM - timedelta(days=1), ANCHOR)
        day_of     = compute_day(ADM, ANCHOR)
        assert day_before == -1
        assert day_of == 1
        assert day_of - day_before == 2   # gap of 2 across the discontinuity

    # ── Return type ──────────────────────────────────────────────────────────

    def test_returns_int(self):
        result = compute_day(ADM, ANCHOR)
        assert isinstance(result, int)

    def test_positive_result_is_positive_int(self):
        result = compute_day(ADM + timedelta(days=5), ANCHOR)
        assert result > 0

    def test_negative_result_is_negative_int(self):
        result = compute_day(ADM - timedelta(days=5), ANCHOR)
        assert result < 0


# ─────────────────────────────────────────────────────────────────────────────
# is_within_admission_window()
# ─────────────────────────────────────────────────────────────────────────────

class TestIsWithinAdmissionWindow:
    def test_admission_day_is_within(self):
        assert is_within_admission_window(ADM, ANCHOR) is True

    def test_discharge_day_is_within(self):
        assert is_within_admission_window(DIS, ANCHOR) is True

    def test_mid_stay_day_is_within(self):
        assert is_within_admission_window(ADM + timedelta(days=4), ANCHOR) is True

    def test_day_before_admission_is_not_within(self):
        assert is_within_admission_window(ADM - timedelta(days=1), ANCHOR) is False

    def test_day_after_discharge_is_not_within(self):
        assert is_within_admission_window(DIS + timedelta(days=1), ANCHOR) is False

    def test_far_before_admission_is_not_within(self):
        assert is_within_admission_window(ADM - timedelta(days=30), ANCHOR) is False

    def test_far_after_discharge_is_not_within(self):
        assert is_within_admission_window(DIS + timedelta(days=30), ANCHOR) is False

    def test_single_day_stay(self):
        """Edge case: admission_date == discharge_date."""
        anchor = make_anchor(admission=date(2026, 3, 1), discharge=date(2026, 3, 1))
        assert is_within_admission_window(date(2026, 3, 1), anchor) is True
        assert is_within_admission_window(date(2026, 3, 2), anchor) is False


# ─────────────────────────────────────────────────────────────────────────────
# is_within_48h_of_admission()
# ─────────────────────────────────────────────────────────────────────────────

class TestIsWithin48hOfAdmission:
    def test_admission_day_is_within_48h(self):
        assert is_within_48h_of_admission(ADM, ANCHOR) is True

    def test_1_day_before_is_within_48h(self):
        assert is_within_48h_of_admission(ADM - timedelta(days=1), ANCHOR) is True

    def test_2_days_before_is_within_48h(self):
        assert is_within_48h_of_admission(ADM - timedelta(days=2), ANCHOR) is True

    def test_3_days_before_is_not_within_48h(self):
        assert is_within_48h_of_admission(ADM - timedelta(days=3), ANCHOR) is False

    def test_1_day_after_is_within_48h(self):
        assert is_within_48h_of_admission(ADM + timedelta(days=1), ANCHOR) is True

    def test_2_days_after_is_within_48h(self):
        assert is_within_48h_of_admission(ADM + timedelta(days=2), ANCHOR) is True

    def test_3_days_after_is_not_within_48h(self):
        assert is_within_48h_of_admission(ADM + timedelta(days=3), ANCHOR) is False

    def test_boundary_is_symmetric(self):
        """The 48h window is symmetric: ±2 days in/out."""
        for d in [1, 2]:
            assert is_within_48h_of_admission(ADM - timedelta(days=d), ANCHOR) is True
            assert is_within_48h_of_admission(ADM + timedelta(days=d), ANCHOR) is True
        for d in [3, 4, 10]:
            assert is_within_48h_of_admission(ADM - timedelta(days=d), ANCHOR) is False
            assert is_within_48h_of_admission(ADM + timedelta(days=d), ANCHOR) is False


# ─────────────────────────────────────────────────────────────────────────────
# classify_window()
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyWindow:

    # ── ADMISSION window ─────────────────────────────────────────────────────

    def test_admission_day_is_admission(self):
        assert classify_window(ADM, ANCHOR) == WindowType.ADMISSION

    def test_1_day_before_is_admission(self):
        assert classify_window(ADM - timedelta(days=1), ANCHOR) == WindowType.ADMISSION

    def test_2_days_before_is_admission(self):
        assert classify_window(ADM - timedelta(days=2), ANCHOR) == WindowType.ADMISSION

    def test_1_day_after_is_admission(self):
        assert classify_window(ADM + timedelta(days=1), ANCHOR) == WindowType.ADMISSION

    def test_2_days_after_is_admission(self):
        assert classify_window(ADM + timedelta(days=2), ANCHOR) == WindowType.ADMISSION

    # ── IN_HOSPITAL window ───────────────────────────────────────────────────

    def test_3_days_after_is_in_hospital(self):
        assert classify_window(ADM + timedelta(days=3), ANCHOR) == WindowType.IN_HOSPITAL

    def test_mid_stay_is_in_hospital(self):
        assert classify_window(ADM + timedelta(days=5), ANCHOR) == WindowType.IN_HOSPITAL

    def test_discharge_day_is_in_hospital(self):
        # discharge_date is within window, outside 48 h → IN_HOSPITAL
        # (delta from ADM to DIS = 8 days > 2)
        assert classify_window(DIS, ANCHOR) == WindowType.IN_HOSPITAL

    # ── PRE_ADMISSION window ─────────────────────────────────────────────────

    def test_3_days_before_is_pre_admission(self):
        assert classify_window(ADM - timedelta(days=3), ANCHOR) == WindowType.PRE_ADMISSION

    def test_far_before_is_pre_admission(self):
        assert classify_window(ADM - timedelta(days=30), ANCHOR) == WindowType.PRE_ADMISSION

    def test_boundary_before_48h_is_pre_admission(self):
        # 3 days before = just outside the ±48 h boundary
        assert classify_window(ADM - timedelta(days=3), ANCHOR) == WindowType.PRE_ADMISSION

    # ── POST_DISCHARGE window ────────────────────────────────────────────────

    def test_day_after_discharge_is_post_discharge(self):
        assert classify_window(DIS + timedelta(days=1), ANCHOR) == WindowType.POST_DISCHARGE

    def test_far_after_discharge_is_post_discharge(self):
        assert classify_window(DIS + timedelta(days=60), ANCHOR) == WindowType.POST_DISCHARGE

    # ── Boundary conditions ──────────────────────────────────────────────────

    def test_all_four_types_reachable(self):
        """Sanity: all four WindowType values can actually be produced."""
        results = {
            classify_window(ADM, ANCHOR),                      # ADMISSION
            classify_window(ADM + timedelta(days=5), ANCHOR),  # IN_HOSPITAL
            classify_window(ADM - timedelta(days=10), ANCHOR), # PRE_ADMISSION
            classify_window(DIS + timedelta(days=5), ANCHOR),  # POST_DISCHARGE
        }
        assert results == {
            WindowType.ADMISSION,
            WindowType.IN_HOSPITAL,
            WindowType.PRE_ADMISSION,
            WindowType.POST_DISCHARGE,
        }

    def test_returns_windowtype_instance(self):
        result = classify_window(ADM, ANCHOR)
        assert isinstance(result, WindowType)

    def test_single_day_stay_in_hospital(self):
        """When admission == discharge, mid-window == IN_HOSPITAL for day 3+ from ADM."""
        anchor = make_anchor(admission=date(2026, 3, 1), discharge=date(2026, 3, 10))
        assert classify_window(date(2026, 3, 5), anchor) == WindowType.IN_HOSPITAL

    def test_48h_boundary_is_exclusive_outward(self):
        """Exactly 3 days before admission → PRE_ADMISSION (not ADMISSION)."""
        three_days_before = ADM - timedelta(days=3)
        assert classify_window(three_days_before, ANCHOR) == WindowType.PRE_ADMISSION

    def test_48h_boundary_is_inclusive_inward(self):
        """Exactly 2 days before admission → ADMISSION."""
        two_days_before = ADM - timedelta(days=2)
        assert classify_window(two_days_before, ANCHOR) == WindowType.ADMISSION


# ─────────────────────────────────────────────────────────────────────────────
# Clinical scenario tests
# ─────────────────────────────────────────────────────────────────────────────

class TestClinicalScenarios:
    """
    Integration-style tests using the real fixture dates:
      Admission: 2026-01-12  Discharge: 2026-01-20  (8 nights)
    """

    def test_lab_drawn_on_admission_day(self):
        # Lab drawn same day as admission → day 1, ADMISSION window
        lab_date = date(2026, 1, 12)
        assert compute_day(lab_date, ANCHOR) == 1
        assert classify_window(lab_date, ANCHOR) == WindowType.ADMISSION

    def test_lab_drawn_day_before_admission(self):
        # Emergency lab drawn day before → day -1, ADMISSION window (48 h rule)
        lab_date = date(2026, 1, 11)
        assert compute_day(lab_date, ANCHOR) == -1
        assert classify_window(lab_date, ANCHOR) == WindowType.ADMISSION

    def test_lab_drawn_2_days_before_admission(self):
        # Lab drawn 2 days before → day -2, ADMISSION window (within 48 h)
        lab_date = date(2026, 1, 10)
        assert compute_day(lab_date, ANCHOR) == -2
        assert classify_window(lab_date, ANCHOR) == WindowType.ADMISSION

    def test_lab_drawn_3_days_before_admission(self):
        # Lab drawn 3 days before → day -3, PRE_ADMISSION (outside 48 h)
        lab_date = date(2026, 1, 9)
        assert compute_day(lab_date, ANCHOR) == -3
        assert classify_window(lab_date, ANCHOR) == WindowType.PRE_ADMISSION

    def test_fibroscan_after_discharge(self):
        # FibroScan done a week after discharge → POST_DISCHARGE
        scan_date = date(2026, 1, 27)
        assert compute_day(scan_date, ANCHOR) == 16
        assert classify_window(scan_date, ANCHOR) == WindowType.POST_DISCHARGE

    def test_endoscopy_on_day_2(self):
        endo_date = date(2026, 1, 13)
        assert compute_day(endo_date, ANCHOR) == 2
        assert classify_window(endo_date, ANCHOR) == WindowType.ADMISSION

    def test_in_hospital_lab_series(self):
        """Verify day numbering across the entire 8-night stay."""
        expected = {
            date(2026, 1, 12): 1,   # admission
            date(2026, 1, 13): 2,
            date(2026, 1, 14): 3,
            date(2026, 1, 15): 4,
            date(2026, 1, 16): 5,
            date(2026, 1, 17): 6,
            date(2026, 1, 18): 7,
            date(2026, 1, 19): 8,
            date(2026, 1, 20): 9,   # discharge
        }
        for event_date, expected_day in expected.items():
            assert compute_day(event_date, ANCHOR) == expected_day, (
                f"Date {event_date}: expected day {expected_day}, "
                f"got {compute_day(event_date, ANCHOR)}"
            )

    def test_no_day_0_in_full_stay(self):
        """No date during or around the stay should produce day 0."""
        for delta in range(-5, 15):
            d = date(2026, 1, 12) + timedelta(days=delta)
            assert compute_day(d, ANCHOR) != 0, f"Got day 0 for {d}"


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestComputeDay,
        TestIsWithinAdmissionWindow,
        TestIsWithin48hOfAdmission,
        TestClassifyWindow,
        TestClinicalScenarios,
    ]

    passed = failed = 0
    for cls in all_classes:
        instance = cls()
        methods = sorted(m for m in dir(cls) if m.startswith("test_"))
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
    print(f"  PASSED:  {passed}/{passed + failed}")
    if failed:
        print(f"  FAILED:  {failed}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
