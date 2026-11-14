"""
app/pipeline/step4_normalize.py

Step 4 of the pipeline: day normalisation.

Converts an absolute event date into a signed day-number relative to the
patient's admission date.  All event columns in the wide table are keyed
by this day number (e.g. creatinine_d1, wbc_d3).

Day-numbering convention (matches clinical practice — no day 0):
    delta = event_date - admission_date   (in whole days)
    delta >= 0  →  day = delta + 1        (admission day = day 1)
    delta <  0  →  day = delta            (day before admission = day -1)

Examples:
    Admission day       (delta=0)  → day  1
    Day after           (delta=1)  → day  2
    Day before          (delta=-1) → day -1
    Two days before     (delta=-2) → day -2
    Day 0 is NEVER returned.

Public API
----------
compute_day(event_date, anchor) -> int
    Core day calculation.

is_within_admission_window(event_date, anchor) -> bool
    True if the event falls within [admission_start.date, discharge_end.date].

is_within_48h_of_admission(event_date, anchor) -> bool
    True if |event_date - admission_date| <= 2 days.
    Used to include pre-admission labs in the "admission" window.

classify_window(event_date, anchor) -> WindowType
    ADMISSION      — within ±48 h of admission_start (inclusive)
    IN_HOSPITAL    — inside admission window but > 48 h from admission
    PRE_ADMISSION  — before the 48 h pre-admission boundary
    POST_DISCHARGE — after discharge_end

Design notes
------------
- Works with date objects only (not datetime) — callers strip time.
- No external dependencies; stdlib only (importable without any packages).
- All functions are pure (no side-effects, no logging) for easy testing.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.domain.enums import WindowType
from app.domain.models import PatientAnchor

# The ±48 h window used to define "admission period" for labs and events.
_ADMISSION_WINDOW_DAYS = 2


# ── Core calculation ──────────────────────────────────────────────────────────

def compute_day(event_date: date, anchor: PatientAnchor) -> int:
    """
    Return the signed day number of *event_date* relative to admission.

    Convention: no day 0 — admission day is day 1.
        delta >= 0  →  delta + 1
        delta <  0  →  delta

    Parameters
    ----------
    event_date : date
        The date of the event (labs drawn, procedure performed, etc.).
    anchor : PatientAnchor
        The patient anchor containing the admission date.

    Returns
    -------
    int
        Signed day number.  Never 0.
    """
    delta = (event_date - anchor.admission_date).days
    return delta + 1 if delta >= 0 else delta


# ── Window predicates ─────────────────────────────────────────────────────────

def is_within_admission_window(event_date: date, anchor: PatientAnchor) -> bool:
    """
    Return True if *event_date* falls within the admission window
    [admission_date, discharge_date] (both endpoints inclusive).

    Does NOT include pre-admission or post-discharge events.
    """
    return anchor.admission_date <= event_date <= anchor.discharge_date


def is_within_48h_of_admission(event_date: date, anchor: PatientAnchor) -> bool:
    """
    Return True if *event_date* is within ±48 h (2 days) of the admission date.

    This includes:
      - The admission day itself
      - Up to 2 days before admission  (pre-admission labs drawn prior to admission)
      - Up to 2 days after admission   (early in-hospital period)

    Used to classify events as WindowType.ADMISSION.
    """
    delta_days = abs((event_date - anchor.admission_date).days)
    return delta_days <= _ADMISSION_WINDOW_DAYS


def classify_window(event_date: date, anchor: PatientAnchor) -> WindowType:
    """
    Classify *event_date* into one of four WindowType values.

    Priority order (first matching rule wins):
      1. ADMISSION      — within ±48 h of admission_start (regardless of discharge)
      2. POST_DISCHARGE — strictly after discharge_date
      3. PRE_ADMISSION  — strictly before admission_date - 48 h
      4. IN_HOSPITAL    — within admission window but outside ±48 h boundary
                          (days 3 through discharge day)

    Parameters
    ----------
    event_date : date
    anchor : PatientAnchor

    Returns
    -------
    WindowType
    """
    # Rule 1: ±48 h of admission_start (highest priority — catches pre/post that
    #         are close to admission even if technically outside the window)
    if is_within_48h_of_admission(event_date, anchor):
        return WindowType.ADMISSION

    # Rule 2: strictly after discharge
    if event_date > anchor.discharge_date:
        return WindowType.POST_DISCHARGE

    # Rule 3: strictly before the 48 h pre-admission boundary
    pre_admission_boundary = anchor.admission_date - timedelta(days=_ADMISSION_WINDOW_DAYS)
    if event_date < pre_admission_boundary:
        return WindowType.PRE_ADMISSION

    # Rule 4: within window, more than 48 h after admission
    return WindowType.IN_HOSPITAL
