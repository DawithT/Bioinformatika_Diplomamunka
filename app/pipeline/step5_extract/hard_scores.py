"""
app/pipeline/step5_extract/hard_scores.py

Step 5 — hard extraction of clinical scores and vital signs from discharge
summaries.

ScoresVitalsParser
    Extracts clinical scoring values (Child-Pugh, MELD, Maddrey, CLIF-C)
    and vital signs (BP, pulse, height, weight, SpO2, temperature) from
    the document text.

Document format independence
----------------------------
Both old-style (e.g. Régi_záró) and new-style (e.g. FS záró) documents can
contain ANY of these fields — scores, vitals, and clinical findings.  The
parser searches the **entire document** with flexible regex patterns that
accommodate all known formats:

  - Abbreviated labels:   RR: 112/91, P: 102/min, TM: 189, TS: 98.9
  - Full-word labels:     Vérnyomás: 120/80 Hgmm, Pulzus: 105/min
  - Structured score line: Child: C/11 pont  MELD-Na: 25 pont  Maddrey: 37.6
  - Inline score mention:  Child C stádiumú (in Epikrízis)
  - Standalone measures:   178 cm, 95 kg (in Státus blocks)

Anamnézis handling
------------------
The Anamnézis section describes **prior medical history**, not the current
admission.  Scores and vitals extracted there relate to earlier events and
must NOT be mapped to ``_adm`` (admission) registry fields.

The Anamnézis section ends (and the current case begins) when one of the
following appears:
  - A section header such as "Jelen felvétel", "Felvételi státus", "Státus", "Vizsgálatok"
  - The keyword "Jelenleg" or "Jelen felvétel"  — even when these appear mid-paragraph
    rather than as a standalone header.

Everything **before** that boundary is Anamnézis (historical); everything
after it is current-admission data.

Source metadata
---------------
All fields: source=HEURISTIC, confidence=0.85, engine="scores_parser".
When LLM is enabled, these can be reviewed and upgraded.
"""
from __future__ import annotations

import logging
import re

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField, PatientAnchor, RoutedDoc
from app.pipeline.step5_extract._utils import to_float_hu

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_SOURCE = FieldSource.HEURISTIC
_CONFIDENCE = 0.85
_ENGINE = "scores_parser"


def _make_field(key: str, value, doc_type: DocType) -> ExtractedField:
    return ExtractedField(
        key=key,
        value=value,
        source=_SOURCE,
        doc_type=doc_type,
        confidence=_CONFIDENCE,
        engine=_ENGINE,
    )


# ── Anamnézis boundary detection ─────────────────────────────────────────────
# The Anamnézis section starts with its header and ends when either:
#   (a) a subsequent section header appears, OR
#   (b) "Jelenleg" / "Jelen felvétel"  appears (even inline).

_ANAM_START_RE = re.compile(
    r'^[ \t]*Anamn(?:e.?|é)zis',
    re.IGNORECASE | re.MULTILINE,
)

# Patterns that mark the END of historical anamnesis — either a section
# header on its own line, or the inline keyword "Jelenleg" / "Jelen felvétel"
# which signals the start of the current admission
# narrative even within the Anamnézis section.
_ANAM_END_RE = re.compile(
    r'(?:'
    # Section headers (start of line)
    r'^[ \t]*(?:'
        r'Jelen\s+felv[eé]tel'
        r'|Felv[eé]teli\s+st[aá]tus'
        r'|St[aá]tus'
        r'|Vizsg[aá]lat'
        r'|Diagn[oó]z'
        r'|Alkalmazott'
        r'|Therap'
        r'|Epikr[ií]z'
        r'|Javasl'
        r'|CAVE'
    r')[\s:]*'
    r'|'
    # Inline current-case markers (can appear mid-paragraph)
    r'Jelenleg\b'
    r'|'
    r'Jelen\s+felv[eé]tel'
    r')',
    re.IGNORECASE | re.MULTILINE,
)


def _anamnesis_span(text: str) -> tuple[int, int] | None:
    """Return (start, end) of the Anamnézis section, or None if not found."""
    m_start = _ANAM_START_RE.search(text)
    if not m_start:
        return None
    anam_begin = m_start.start()
    m_end = _ANAM_END_RE.search(text, m_start.end())
    anam_end = m_end.start() if m_end else len(text)
    return (anam_begin, anam_end)


def _in_anamnesis(pos: int, anam_span: tuple[int, int] | None) -> bool:
    """Return True if character position *pos* falls inside the Anamnézis block."""
    if anam_span is None:
        return False
    return anam_span[0] <= pos < anam_span[1]


# ── Score patterns ───────────────────────────────────────────────────────────

# Child-Pugh: "Child: C/11 pont" | "Child C" | "Child-Pugh C/11" | "Child C stádiumú"
_CHILD_RE = re.compile(
    r'Child(?:[-\s]Pugh)?[\s:]*([A-C])\s*(?:/\s*(\d{1,2}))?\s*(?:pont|st[aá]dium)?',
    re.IGNORECASE,
)

# MELD-Na: "MELD-Na: 25 pont" | "MELD-Na 25"
_MELD_NA_RE = re.compile(
    r'MELD[-\s]*Na[\s:]*(\d{1,3})\s*(?:pont)?',
    re.IGNORECASE,
)

# MELD (standalone, not MELD-Na): "MELD: 25" | "MELD 25 pont"
_MELD_RE = re.compile(
    r'(?<![/-])MELD(?![-\s]*Na)[\s:]*(\d{1,3})\s*(?:pont)?',
    re.IGNORECASE,
)

# Maddrey: "Maddrey: 37.6" | "Maddrey score: 37,6" | "Maddrey score alapján (37.6)"
_MADDREY_RE = re.compile(
    r'Maddrey(?:\s+score)?[\s:]*(?:alapj[aá]n\s*)?[\s(]*(\d+[\.,]?\d*)',
    re.IGNORECASE,
)

# CLIF-C OF: "CLIF-C OF 7 pont" | "CLIF-C OF: 7"
_CLIF_C_OF_RE = re.compile(
    r'CLIF[-\s]*C\s*OF[\s:]*(\d{1,3})\s*(?:pont)?',
    re.IGNORECASE,
)

# CLIF-C AD: "AD 62 pont" | "CLIF-C AD: 62" — only valid near CLIF context
_CLIF_C_AD_RE = re.compile(
    r'(?:CLIF[-\s]*C\s*)?AD[\s:]*(\d{1,3})\s*(?:pont)?',
    re.IGNORECASE,
)

# ACLF: "ACLF nincs" | "ACLF 2" | "ACLF: 1" | "ACLF igen"
_ACLF_RE = re.compile(
    r'ACLF[\s:]*(\d|nincs|nem|igen)',
    re.IGNORECASE,
)

# ── Vital sign patterns ─────────────────────────────────────────────────────

# Blood pressure: "RR: 112/91" | "Vérnyomás: 120/80 Hgmm"
_BP_RE = re.compile(
    r'(?:RR|V[eé]rnyom[aá]s)[\s:]*(\d{2,3})\s*/\s*(\d{2,3})',
    re.IGNORECASE,
)

# Pulse: "P: 102/min" | "Pulzus: 105/min" | "Szívfrekvencia: 89/min"
_PULSE_RE = re.compile(
    r'(?:(?:^|\s)P(?:ulzus)?|Sz[ií]vfrekvencia)[\s:]*(\d{2,3})\s*/?\s*min',
    re.IGNORECASE | re.MULTILINE,
)

# Height (labeled): "TM: 189 cm" | "Testmagasság: 189"
_HEIGHT_RE = re.compile(
    r'(?:TM|Testmagass[aá]g)[\s:]*(\d{2,3})\s*(?:cm)?',
    re.IGNORECASE,
)

# Weight (labeled): "TS: 98.9 kg" | "Testsúly: 98,9 kg" (takes first value if range)
_WEIGHT_RE = re.compile(
    r'(?:TS|Tests[uú]ly)[\s:]*(\d{2,3}[\.,]?\d*)\s*(?:[-–][\s]*[\d.,]+\s*)?(?:kg)?',
    re.IGNORECASE,
)

# Height standalone: "178 cm," or "178 cm" at start of line
_HEIGHT_STANDALONE_RE = re.compile(
    r'(?:^|\n)\s*(\d{3})\s*cm\b',
    re.IGNORECASE,
)

# Weight standalone: "95 kg" at start of line
_WEIGHT_STANDALONE_RE = re.compile(
    r'(?:^|\n)\s*(\d{2,3}[\.,]?\d*)\s*kg\b',
    re.IGNORECASE,
)

# BMI: "BMI: 27.7" | "BMI 27,7 kg/m2"
_BMI_RE = re.compile(
    r'BMI[\s:]*(\d{1,3}[\.,]?\d*)',
    re.IGNORECASE,
)

# Temperature: "Testhő: 36.5 °C" | "Hőmérséklet: 36,5"
_TEMP_RE = re.compile(
    r'(?:Testh[oő]|H[oő]m[eé]rs[eé]klet)[\s:]*(\d{2}[\.,]\d)',
    re.IGNORECASE,
)

# SpO2: "SpO2: 92%" | "szaturáció: 92 %" | "O2 szaturáció: 92-92 %"
_SPO2_RE = re.compile(
    r'(?:SpO2|O2\s*szatur[aá]ci[oó]|(?:Oxig[eé]n\s*)?szatur[aá]ci[oó])[\s:]*(\d{2,3})',
    re.IGNORECASE,
)

# GCS: "GCS: 15" | "Glasgow: 15"
_GCS_RE = re.compile(
    r'(?:GCS|Glasgow)[\s:]*(\d{1,2})',
    re.IGNORECASE,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _is_clif_context(text: str, match_start: int, window: int = 200) -> bool:
    """Return True if 'CLIF' appears within *window* chars before the match."""
    start = max(0, match_start - window)
    return bool(re.search(r'CLIF', text[start:match_start], re.IGNORECASE))


def _first_outside_anam(
    pattern: re.Pattern,
    text: str,
    anam_span: tuple[int, int] | None,
) -> re.Match | None:
    """Return the first match of *pattern* that is NOT inside Anamnézis."""
    for m in pattern.finditer(text):
        if not _in_anamnesis(m.start(), anam_span):
            return m
    return None


# ── Parser ───────────────────────────────────────────────────────────────────

class ScoresVitalsParser:
    """
    Extract clinical scores and vital signs from a document.

    Designed for DISCHARGE_SUMMARY documents but works on any doc_type.
    Excludes matches found inside the Anamnézis section (prior history)
    to avoid mapping historical values to current-admission fields.
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        text = doc.full_text
        dt = doc.doc_type
        anam_span = _anamnesis_span(text)
        fields: list[ExtractedField] = []

        fields.extend(self._parse_scores(text, dt, anam_span))
        fields.extend(self._parse_vitals(text, dt, anam_span))

        if fields:
            log.info(
                "ScoresVitalsParser: extracted %d fields from %s",
                len(fields), doc.path.name,
            )
        return fields

    def _parse_scores(
        self, text: str, dt: DocType, anam_span: tuple[int, int] | None,
    ) -> list[ExtractedField]:
        fields: list[ExtractedField] = []

        # Child-Pugh
        m = _first_outside_anam(_CHILD_RE, text, anam_span)
        if m:
            stadium = m.group(1).upper()
            fields.append(_make_field("child_stadium_adm", stadium, dt))
            if m.group(2) and 5 <= int(m.group(2)) <= 15:
                fields.append(_make_field("child_score_adm", m.group(2), dt))

        # MELD-Na
        m = _first_outside_anam(_MELD_NA_RE, text, anam_span)
        if m:
            fields.append(_make_field("meld_na_adm", m.group(1), dt))

        # MELD standalone — only if MELD-Na was NOT already found
        if not any(f.key == "meld_na_adm" for f in fields):
            m = _first_outside_anam(_MELD_RE, text, anam_span)
            if m:
                fields.append(_make_field("meld_adm", m.group(1), dt))

        # Maddrey — first match outside Anamnézis
        m = _first_outside_anam(_MADDREY_RE, text, anam_span)
        if m:
            v = to_float_hu(m.group(1))
            if v is not None:
                fields.append(_make_field(
                    "maddrey_adm", str(v) if v != int(v) else str(int(v)), dt
                ))

        # CLIF-C OF
        m = _first_outside_anam(_CLIF_C_OF_RE, text, anam_span)
        if m:
            fields.append(_make_field("clif_c_of_adm", m.group(1), dt))

        # CLIF-C AD — must be near CLIF context and outside Anamnézis
        for m in _CLIF_C_AD_RE.finditer(text):
            if not _in_anamnesis(m.start(), anam_span) and _is_clif_context(text, m.start()):
                fields.append(_make_field("clif_c_ad_adm", m.group(1), dt))
                break

        # ACLF
        m = _first_outside_anam(_ACLF_RE, text, anam_span)
        if m:
            val = m.group(1).lower()
            if val in ("nincs", "nem"):
                fields.append(_make_field("aclf_emlitve_van_a_felvetelkor_adm", "Nem", dt))
            elif val.isdigit():
                fields.append(_make_field("aclf_emlitve_van_a_felvetelkor_adm", "Igen", dt))
                fields.append(_make_field("aclf_grade_a_leiras_szerint_adm", val, dt))

        return fields

    def _parse_vitals(
        self, text: str, dt: DocType, anam_span: tuple[int, int] | None,
    ) -> list[ExtractedField]:
        fields: list[ExtractedField] = []

        # Blood pressure
        m = _first_outside_anam(_BP_RE, text, anam_span)
        if m:
            fields.append(_make_field("vernyomas_systole_hgmm_adm", m.group(1), dt))
            fields.append(_make_field("vernyomas_diastole_hgmm_adm", m.group(2), dt))

        # Pulse
        m = _first_outside_anam(_PULSE_RE, text, anam_span)
        if m:
            fields.append(_make_field("pulzus_min_adm", m.group(1), dt))

        # Height — try labeled first, then standalone
        m = _first_outside_anam(_HEIGHT_RE, text, anam_span)
        if m:
            fields.append(_make_field("testmagassag_cm_adm", m.group(1), dt))
        else:
            m = _first_outside_anam(_HEIGHT_STANDALONE_RE, text, anam_span)
            if m:
                h = int(m.group(1))
                if 100 <= h <= 250:
                    fields.append(_make_field("testmagassag_cm_adm", str(h), dt))

        # Weight — try labeled first, then standalone
        m = _first_outside_anam(_WEIGHT_RE, text, anam_span)
        if m:
            v = to_float_hu(m.group(1))
            if v is not None and 20 <= v <= 300:
                fields.append(_make_field(
                    "testsuly_kg_adm",
                    str(v) if v != int(v) else str(int(v)),
                    dt,
                ))
        else:
            m = _first_outside_anam(_WEIGHT_STANDALONE_RE, text, anam_span)
            if m:
                v = to_float_hu(m.group(1))
                if v is not None and 20 <= v <= 300:
                    fields.append(_make_field(
                        "testsuly_kg_adm",
                        str(v) if v != int(v) else str(int(v)),
                        dt,
                    ))

        # BMI — explicit in text
        m = _first_outside_anam(_BMI_RE, text, anam_span)
        if m:
            v = to_float_hu(m.group(1))
            if v is not None and 10 <= v <= 80:
                fields.append(_make_field("bmi_adm", str(round(v, 1)), dt))

        # Calculate BMI from height + weight if not found explicitly
        if not any(f.key == "bmi_adm" for f in fields):
            height_f = next((f for f in fields if f.key == "testmagassag_cm_adm"), None)
            weight_f = next((f for f in fields if f.key == "testsuly_kg_adm"), None)
            if height_f and weight_f:
                try:
                    h_m = float(height_f.value) / 100.0
                    w_kg = float(weight_f.value)
                    if h_m > 0:
                        bmi = round(w_kg / (h_m * h_m), 1)
                        if 10 <= bmi <= 80:
                            fields.append(_make_field("bmi_adm", str(bmi), dt))
                except (ValueError, ZeroDivisionError):
                    pass

        # Temperature
        m = _first_outside_anam(_TEMP_RE, text, anam_span)
        if m:
            v = to_float_hu(m.group(1))
            if v is not None and 34 <= v <= 42:
                fields.append(_make_field("testho_c_adm", str(v), dt))

        # SpO2
        m = _first_outside_anam(_SPO2_RE, text, anam_span)
        if m:
            val = int(m.group(1))
            if 50 <= val <= 100:
                fields.append(_make_field("oxigen_szaturacio_adm", str(val), dt))

        # GCS
        m = _first_outside_anam(_GCS_RE, text, anam_span)
        if m:
            val = int(m.group(1))
            if 3 <= val <= 15:
                fields.append(_make_field("gcs_score_adm", str(val), dt))

        return fields
