"""
app/pipeline/step5_extract/units.py

Unit normalisation, mismatch detection, and magnitude conversion for numeric
extracted values.

Contract
--------
The registry `unit` column is the canonical unit for each numeric field. At
extraction time we capture whatever unit the source document used, then:

  - **normalise** it (ASCII-fold µ → u; collapse whitespace; case-insensitive)
    so trivial typographic variants do NOT count as a mismatch
    (e.g. ``µmol/L`` vs ``umol/L``).
  - **compare** the normalised seen-unit against the normalised expected-unit.
    Equal → ``unit_ok = True``. Different → ``unit_ok = False`` and the value
    is recorded in the audit ``unit_mismatches.xlsx`` sheet.

For the *audit* path (``unit_ok`` / unit_mismatches.xlsx) no conversions are
performed: ``g/L`` vs ``g/dL`` is flagged rather than silently rewritten.

For the *merge comparison* path (step6_merge conflict detection) this module
additionally provides magnitude conversion so that ``1 g`` vs ``1000 mg`` is
recognised as the same quantity and ``2 mg`` vs ``2 g`` as a genuine
conflict. Conversion covers metric-prefixed mass / volume / amount / enzyme
units and their ``X/Y`` compounds; anything unrecognised returns None and
the caller must fall back to strict comparison.

Public API
----------
normalise_unit(unit)
    Lower-cased, ASCII-folded, whitespace-collapsed form of a unit string.

units_equivalent(expected, seen)
    True iff the two units normalise to the same string.

split_value_and_unit(raw)
    Best-effort parse of a ``"<number> <unit>"`` string into
    ``(numeric_text, unit_text)``. Returns ``(raw, None)`` if no trailing
    unit is present.

unit_to_base(unit)
    ``(dimension, factor)`` for a recognised unit (compounds supported),
    or ``None``. Two units are convertible iff their dimensions are equal;
    ``value * factor`` puts both on a common scale.
"""
from __future__ import annotations

import re

# ── Normalisation ─────────────────────────────────────────────────────────────

_WS_RE = re.compile(r"\s+")

# ASCII-fold table for characters that commonly appear in lab units.
# Extend deliberately — do NOT fold `d` ↔ `D` to anything; we want
# "g/L" vs "g/dL" to stay distinct.
_UNIT_FOLD: dict[str, str] = {
    "µ": "u",   # micro sign U+00B5
    "μ": "u",   # Greek small mu U+03BC
    "Ω": "ohm",
    "°": "",    # degree sign — rarely meaningful for unit identity
}


def normalise_unit(unit: str | None) -> str:
    """
    Return a canonical form of *unit* for comparison.

    Rules:
      - ``None`` / empty → empty string
      - strip leading/trailing whitespace; collapse internal runs
      - lowercase
      - ASCII-fold per ``_UNIT_FOLD`` (µ/μ → u, °-sign dropped, etc.)
      - leave slash and punctuation alone so ``g/L`` ≠ ``g/dL``
    """
    if unit is None:
        return ""
    s = unit.strip()
    if not s:
        return ""
    for src, dst in _UNIT_FOLD.items():
        s = s.replace(src, dst)
    s = _WS_RE.sub(" ", s).lower()
    return s


def units_equivalent(expected: str | None, seen: str | None) -> bool:
    """
    True iff *expected* and *seen* normalise to the same canonical string.

    A missing (``None``/empty) *seen* unit is treated as equivalent: it means
    the source document did not repeat the unit and the value is assumed to
    be in the registry-declared unit (this matches how structured lab
    parsers emit bare numbers).
    """
    if not seen or not str(seen).strip():
        return True
    return normalise_unit(expected) == normalise_unit(seen)


# ── Value / unit split ────────────────────────────────────────────────────────

# Match a leading signed/unsigned number (decimal point or comma) then an
# optional unit suffix. The unit is everything after the number and whitespace,
# trimmed. We intentionally accept any non-empty trailing text as a unit
# candidate — downstream comparison via ``units_equivalent`` decides whether
# it matches.
_NUM_WITH_UNIT_RE = re.compile(
    r"""
    ^\s*
    (?P<num>[-+]?\d+(?:[.,]\d+)?)
    \s*
    (?P<unit>\S.*?)?
    \s*$
    """,
    re.VERBOSE,
)


def split_value_and_unit(raw: str) -> tuple[str, str | None]:
    """
    Split a ``"<number> <unit>"`` string into ``(number_text, unit_text | None)``.

    Examples
    --------
    >>> split_value_and_unit("84 µmol/L")
    ('84', 'µmol/L')
    >>> split_value_and_unit("1,4 mg/dL")
    ('1,4', 'mg/dL')
    >>> split_value_and_unit("12.3")
    ('12.3', None)
    >>> split_value_and_unit("Unknown")
    ('Unknown', None)
    """
    if not raw:
        return ("", None)
    m = _NUM_WITH_UNIT_RE.match(raw)
    if not m:
        return (raw, None)
    unit = (m.group("unit") or "").strip() or None
    return (m.group("num"), unit)


# ── Magnitude conversion (merge-comparison path) ──────────────────────────────
#
# Simple (non-compound) units, keyed by their *normalised* form (lower-case,
# µ already folded to "u" by normalise_unit). Each maps to
# (dimension, factor-to-base). Extend deliberately; unknown units must stay
# unknown (None) so the caller falls back to strict comparison — never guess.
_SIMPLE_UNIT_BASE: dict[str, tuple[str, float]] = {
    # mass (base: gram)
    "kg": ("mass", 1e3),
    "g":  ("mass", 1.0),
    "mg": ("mass", 1e-3),
    "ug": ("mass", 1e-6),
    "ng": ("mass", 1e-9),
    # volume (base: litre)
    "l":  ("vol", 1.0),
    "dl": ("vol", 1e-1),
    "ml": ("vol", 1e-3),
    "ul": ("vol", 1e-6),
    # amount of substance (base: mole)
    "mol":  ("mol", 1.0),
    "mmol": ("mol", 1e-3),
    "umol": ("mol", 1e-6),
    "nmol": ("mol", 1e-9),
    "pmol": ("mol", 1e-12),
    # enzyme activity (base: unit)
    "ku": ("enz", 1e3),
    "u":  ("enz", 1.0),
    "mu": ("enz", 1e-3),
    "ne": ("enz", 1.0),   # Hungarian "NE" (nemzetközi egység) == IU
    "iu": ("enz", 1.0),
}


def unit_to_base(unit: str | None) -> tuple[str, float] | None:
    """
    Resolve *unit* to ``(dimension, factor)`` on a common scale, or None.

    Compound ``X/Y`` units are supported when both components are known:
    the dimension is ``"X/Y"`` and the factor is ``factor_X / factor_Y``
    (e.g. ``g/dL`` → ``("mass/vol", 10.0)`` relative to ``g/L``).

    Returns None for unrecognised units — the caller must then compare
    strictly. Never guesses.
    """
    norm = normalise_unit(unit)
    if not norm:
        return None

    if "/" in norm:
        parts = norm.split("/")
        if len(parts) != 2:
            return None
        num = _SIMPLE_UNIT_BASE.get(parts[0].strip())
        den = _SIMPLE_UNIT_BASE.get(parts[1].strip())
        if num is None or den is None:
            return None
        return (f"{num[0]}/{den[0]}", num[1] / den[1])

    simple = _SIMPLE_UNIT_BASE.get(norm)
    if simple is None:
        return None
    return simple
