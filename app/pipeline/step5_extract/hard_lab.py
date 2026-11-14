"""
app/pipeline/step5_extract/hard_lab.py

Step 5 (extraction) — hard lab parser.

Two deterministic parsers extract numeric lab values from documents and
produce ExtractedField objects with column keys of the form:
    {analyte}_d{day}          — first draw on that day
    {analyte}_d{day}_1        — second draw on that day
    {analyte}_d{day}_2        — third draw
    …

TextLabParser
    Handles two text-based formats found in real documents:
      1. Inline block format (Zárójelentés pages 3-5):
             LMI Központi Laboratórium (KIZKEMIA) 2026.01.13 09:21
             (Mintavétel: 2026.01.13 08:00)
             Nátrium: 136 mmol/L (133-146); Kálium: 3.9 mmol/L (3.5-5.3); …
      2. Plain-text table format (structured lab reports after pdfminer):
             Minta dátuma:\n2026.01.16 17:10
             Vizsgálat  Eredmény  Ref.tart.  M.egység
             Fehérvérsejt szám  5.87  4.50-10.80  giga/L

TableLabParser
    Handles Camelot-extracted grid tables from lab-report PDFs:
        Each table is list[list[str]] — rows of cell strings.
        Date is detected from any cell matching YYYY.MM.DD.

Both parsers:
  - Skip non-numeric values (logged as debug); a censored value with a
    qualifier (">2000.0", "< 10") is kept as the text ">2000.0" / "<10"
  - Store ascites results that the discharge summary writes as a note on the
    serum analyte ("Glükóz: lásd megjAsc: Glükóz: 7,4") as ascites_<analyte>
  - Skip negative values
  - Skip unmapped analyte names
  - Never produce day 0 (compute_day invariant)
  - Return list[ExtractedField] with source=HARD, confidence=0.95, engine set
"""
from __future__ import annotations

import logging
import re
from datetime import date

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField, PatientAnchor
from app.pipeline.step4_normalize import compute_day
from app.pipeline.step5_extract._utils import col_key, parse_lab_value, to_float_hu

log = logging.getLogger(__name__)


# ── Hungarian → canonical analyte mapping ─────────────────────────────────────
#
# Keys are lowercase, stripped.  Values are snake_case codes used in column
# names.  Both parsers normalise analyte names through this dict before
# producing keys — unmapped names are silently skipped.
#
# Add entries here when the registry or new document types introduce new labs.

LAB_MAPPING: dict[str, str] = {
    # ── Chemistry ──────────────────────────────────────────────────────────────
    "nátrium":                      "sodium",
    "na":                           "sodium",
    "kálium":                       "potassium",
    "k":                            "potassium",
    "klorid":                       "chloride",
    "cl":                           "chloride",
    # Urea and Karbamid-nitrogén (KN) are functionally identical in Hungarian
    # labs (both mmol/L) — collapsed into one canonical. Do NOT treat KN as
    # US-standard BUN (mg/dL) here.
    "urea":                         "urea",
    "karbamid-nitrogén":            "urea",
    "kn":                           "urea",
    "kreatinin":                    "creatinine",
    "krea":                         "creatinine",
    "gfr (epi)":                    "gfr_epi",
    "gfr":                          "gfr_epi",
    "gfr (kreatininból számolva)":  "gfr_epi",   # Bartók HQ
    "egfr":                         "gfr_epi",
    "kreatinin klírensz":           "gfr_epi",
    "totál bilirubin":              "bilirubin_total",
    "öbil":                         "bilirubin_total",
    "direkt bilirubin":             "bilirubin_direct",
    "konjugált bilirubin":          "bilirubin_direct",
    "dbil":                         "bilirubin_direct",
    "indirekts bilirubin":          "bilirubin_indirect",
    "crp (szenzitív)":              "crp",
    "crp":                          "crp",
    "c-reaktív protein":            "crp",
    "albumin":                      "albumin",
    "alb":                          "albumin",
    "totál protein":                "total_protein",
    "tp":                           "total_protein",
    "aszpartát transzamináz":       "ast",
    "alanin transzamináz":          "alt",
    "ast":                          "ast",
    "alt":                          "alt",
    "got":                          "ast",   # old German clinical name for AST
    "gpt":                          "alt",   # old German clinical name for ALT
    "ggt":                          "ggt",
    "gamma-glutamil transzpeptidáz": "ggt",
    "alkalikus foszfatáz":          "alp",
    "ap":                           "alp",
    "ldh":                          "ldh",
    "amiláz":                       "amylase",
    "amy":                          "amylase",
    "lipáz":                        "lipase",
    "lip":                          "lipase",
    "glükóz":                       "glucose",
    "gluc":                         "glucose",
    "glu":                          "glucose",
    "vércukor":                     "glucose",
    "triglicerid":                  "triglyceride",
    "tg":                           "triglyceride",
    "koleszterin":                  "cholesterol",
    "chol":                         "cholesterol",
    "hdl koleszterin":              "hdl",
    "hdl-c":                        "hdl",
    "ldl koleszterin":              "ldl",
    "ldl-c":                        "ldl",
    "non-hdl koleszterin":          "non_hdl",
    "nonhdl":                       "non_hdl",
    "húgysav":                      "uric_acid",
    "húgys.":                       "uric_acid",
    "ammónia":                      "ammonia",
    "laktát":                       "lactate",
    "cpk":                          "cpk",
    "b12 vitamin":                  "vitamin_b12",
    "ferritin":                     "ferritin",
    "folsav":                       "folate",
    "foszfát":                      "phosphate",
    "foszfá":                       "phosphate",
    "ionizált kálcium":             "calcium_ionized",
    "ica":                          "calcium_ionized",
    "normalizált ionizált kálcium": "calcium_ionized_normalized",
    "totál kálcium":                "calcium_total",
    "tca":                          "calcium_total",
    "magnézium":                    "magnesium",
    "mg":                           "magnesium",
    "normalizált ionizált magnézium": "magnesium_ionized_normalized",
    "kolinészteráz":                "cholinesterase",
    "koline":                       "cholinesterase",
    "hemoglobin a1c":               "hba1c",
    "hemoglobin a1cifcc":           "hba1c_ifcc",
    "vas":                          "iron",
    "transzferrin":                 "transferrin",
    "transzferrin szaturáció":      "transferrin_saturation",
    "szol. transzferrin receptor":  "soluble_transferrin_receptor",
    "prokalcitonin":                "procalcitonin",
    "troponin":                     "troponin",
    "htni":                         "troponin",
    "stsh":                         "tsh",

    # ── Coagulation (mixing studies are intentionally excluded below) ──────────
    "inr":                          "inr",
    "protrombin idő":               "pt_sec",
    "pi":                           "pt_sec",
    "ido":                          "pt_sec",    # Bartók HQ "IDo" (idő)
    "apti":                         "aptt_sec",
    "aptt":                         "aptt_sec",
    "trombin idő":                  "tt_sec",
    "thrombin idő":                 "tt_sec",
    "fibrinogén":                   "fibrinogen",
    "fibr":                         "fibrinogen",
    "d-dimer":                      "d_dimer",

    # ── Complete blood count (CBC) ─────────────────────────────────────────────
    "fehérvérsejt szám":            "wbc",
    "wbc":                          "wbc",
    "fvs":                          "wbc",
    "vörösvértest szám":            "rbc",
    "rbc":                          "rbc",
    "vvt":                          "rbc",
    "hemoglobin":                   "hemoglobin",
    "hgb":                          "hemoglobin",
    "hematokrit":                   "hematocrit",
    "hct":                          "hematocrit",
    "htc":                          "hematocrit",
    "mcv":                          "mcv",
    "mch":                          "mch",
    "mchc":                         "mchc",
    "rdw-sd":                       "rdw_sd",
    "rdw%":                         "rdw_pct",
    "rdw":                          "rdw_pct",
    "red cell distribution width%": "rdw_pct",
    "red cell distribution width":  "rdw_pct",
    "thrombocytaszám":              "platelet",
    "thrombocyta":                  "platelet",
    "plt":                          "platelet",
    "thr":                          "platelet",
    "mean platelet volume":         "mpv",
    "mpv":                          "mpv",
    "plateletcrit":                 "pct",
    "pct%":                         "pct",
    "platelet distribution width":  "pdw",
    "pdw":                          "pdw",
    "platelet-large cell ratio":    "plcr",
    "p-lcr":                        "plcr",
    "neutrofil abszolút érték":     "neutrophil_abs",
    "neutrofil%":                   "neutrophil_pct",
    "neutrofil #":                  "neutrophil_abs",
    "neutrofil#":                   "neutrophil_abs",
    "neu":                          "neutrophil_abs",
    "neu%":                         "neutrophil_pct",
    "neut#":                        "neutrophil_abs",
    "neut%":                        "neutrophil_pct",
    "limfocita abszolút érték":     "lymphocyte_abs",
    "limfocita%":                   "lymphocyte_pct",
    "limfocita#":                   "lymphocyte_abs",
    "limfocita #":                  "lymphocyte_abs",
    "lym":                          "lymphocyte_abs",
    "lym%":                         "lymphocyte_pct",
    "lymp#":                        "lymphocyte_abs",
    "lymp%":                        "lymphocyte_pct",
    "monocita abszolút érték":      "monocyte_abs",
    "monocita%":                    "monocyte_pct",
    "monocita#":                    "monocyte_abs",
    "monocita #":                   "monocyte_abs",
    "mono":                         "monocyte_abs",
    "mon%":                         "monocyte_pct",
    "mono#":                        "monocyte_abs",
    "mono%":                        "monocyte_pct",
    "eozinofil abszolút érték":     "eosinophil_abs",
    "eozinofil%":                   "eosinophil_pct",
    "eozinofil#":                   "eosinophil_abs",
    "eozinofil #":                  "eosinophil_abs",
    "eos":                          "eosinophil_abs",
    "eos%":                         "eosinophil_pct",
    "bazofil abszolút érték":       "basophil_abs",
    "bazofil%":                     "basophil_pct",
    "bazofil#":                     "basophil_abs",
    "bazofil #":                    "basophil_abs",
    "baso":                         "basophil_abs",
    "bas%":                         "basophil_pct",
    "baso#":                        "basophil_abs",
    "baso%":                        "basophil_pct",
    "nrbc%":                        "nrbc_pct",
    "nrbc#":                        "nrbc_abs",
    "cell ratio":                   "cell_ratio",

    # ── Ascites fluid cell analysis ────────────────────────────────────────────
    # Reported on ascitic fluid, NOT on whole blood. Absolute PMN count is the
    # sole diagnostic criterion for Spontaneous Bacterial Peritonitis (SBP) —
    # must not collapse into neutrophil_abs.
    "tc-bf#":                       "ascites_total_cell_count",
    "pmn#":                         "ascites_pmn_abs",
    "pmn%":                         "ascites_pmn_pct",
    "mn#":                          "ascites_mn_abs",
    "mn%":                          "ascites_mn_pct",

    # ── Serum total immunoglobulins (AIH / PBC / ALD workup) ───────────────────
    # Bare IgA/IgG/IgM measured in g/L. Distinct from viral-serology IgA/IgG/IgM
    # sub-class antibodies (e.g. anti-CMV IgG) which are categorical (below).
    "iga":                          "serum_iga",
    "igg":                          "serum_igg",
    "igm":                          "serum_igm",

    # ── Tumour markers ─────────────────────────────────────────────────────────
    "carcino-embrionális antigén":  "cea",
    "cea":                          "cea",
    "afp":                          "afp",
    "alfa-fetoprotein":             "afp",
    "alfa-fetoprotein(t.marker)":   "afp",
    "ca 19-9":                      "ca_19_9",
    "ca19-9":                       "ca_19_9",

    # ── Urine dipstick chemistry (quantitative / semi-quantitative) ───────────
    # Urine sediment MICROSCOPY (Viz. ül. *) is qualitative/semi-quantitative
    # and intentionally excluded — the soft-LLM path handles it.
    "vizelet fehérje":              "urine_protein",
    "vprot":                        "urine_protein",
    "vizelet glükóz":               "urine_glucose",
    "vglu":                         "urine_glucose",
    "vizelet bilirubin":            "urine_bilirubin",
    "vbil":                         "urine_bilirubin",
    "vizelet fajsúly":              "urine_specific_gravity",
    "vfajs":                        "urine_specific_gravity",
    "vizelet ph":                   "urine_ph",
    "vph":                          "urine_ph",
    "vizelet fehérvérsejt":         "urine_wbc",
    "vfvs":                         "urine_wbc",
    "vizelet hemoglobin(vvt.)":     "urine_blood",
    "vhgb":                         "urine_blood",
    "vizelet keton":                "urine_ketones",
    "vket":                         "urine_ketones",
    "vizelet nitrit":               "urine_nitrite",
    "vnit":                         "urine_nitrite",
    "vizelet ubg":                  "urine_urobilinogen",
    "vubg":                         "urine_urobilinogen",
}


# ── Categorical (binary) analyte mapping ──────────────────────────────────────
#
# Fecal-occult-blood results are reported qualitatively (Pozitív / Negatív /
# POS / NEG / +/-) rather than as numbers; the flag marks GI-bleeding risk
# (variceal source). We encode Positive = 1.0, Negative = 0.0 and store it
# alongside numeric labs with the same day-number / ExtractedField machinery.
#
# Viral serology is deliberately NOT collected here: virology results are
# registry questions (virologia_*_inhosp, filled by the LLM; anamnesis
# etiology, filled by VirologyParser), not day-named lab columns.

CATEGORICAL_LAB_MAPPING: dict[str, str] = {
    # GI bleeding flag (variceal risk)
    "széklet vér":                  "fecal_occult_blood",
}

# Raw qualitative-value → numeric encoding.
# Compared after lower()+strip(); the lookup is exact-match.
CATEGORICAL_VALUE_MAP: dict[str, float] = {
    # Positive
    "pozitív":      1.0,
    "pozitiv":      1.0,
    "positive":     1.0,
    "pos":          1.0,
    "poz":          1.0,
    "poz.":         1.0,
    "+":            1.0,
    "reaktív":      1.0,
    "reaktiv":      1.0,
    "reactive":     1.0,
    # Negative
    "negatív":      0.0,
    "negativ":      0.0,
    "negative":     0.0,
    "neg":          0.0,
    "neg.":         0.0,
    "-":            0.0,
    "non-reaktív":  0.0,
    "non-reaktiv":  0.0,
    "nonreactive":  0.0,
}


# ── Regex patterns ────────────────────────────────────────────────────────────

# Block header: any lab name containing "Laboratórium" or "Labor" followed by
# a date.  Captures:
#   group(1) → block header date   (YYYY.MM.DD)
#   group(2) → Mintavétel date     (YYYY.MM.DD) — preferred; may be None
#   group(3) → Mintavétel time     (HH:MM) — may be None
# The "(Mintavétel: …)" parenthetical is consumed even when it is empty
# ("(Mintavétel: )"); otherwise it stays glued to the first analyte and that
# analyte is lost.
_BLOCK_HEADER_RE = re.compile(
    r'[^\n]*(?:Laboratórium|Labor)[^\n]*?'
    r'(\d{4}\.\d{2}\.\d{2})\s+\d{2}:\d{2}'
    r'(?:[^\n]*?\(Mintavétel:\s*(?:(\d{4}\.\d{2}\.\d{2})\s+(\d{2}:\d{2}))?[^\)]*\))?',
    re.IGNORECASE,
)
# Groups: (1) block header date, (2) Mintavétel date, (3) Mintavétel time HH:MM

# Date-first lab block header (older discharge summary format):
# "2018.10.03 07:26 - Hematológia (fekvőbeteg) (KIZ)"
# Captures:
#   group(1) → block date  (YYYY.MM.DD)
#   group(2) → time        (HH:MM)       — always present in this format
_DATE_FIRST_BLOCK_HEADER_RE = re.compile(
    r'^(\d{4}\.\d{2}\.\d{2})\s+(\d{2}[.:]\d{2})\s*-[^\n]+$',
    re.MULTILINE,
)

# "Minta dátuma:" header found in structured lab-report plain text
_SAMPLE_DATE_RE = re.compile(
    r'Minta\s+d[aá]tuma\s*[:\-]?\s*\n?\s*'
    r'(\d{4}[\.\-/]\d{2}[\.\-/]\d{2})'
    r'(?:\s+(\d{2}[:\-\.]\d{2}))?',  # optional HH:MM or HH.MM
    re.IGNORECASE,
)
# Groups: (1) date YYYY.MM.DD, (2) time HH:MM (may be None)

# "Mintavisszaigazolás" — older lab documents use this instead of "Minta dátuma"
_MINTAVISSZAIGAZOLAS_RE = re.compile(
    r'Mintavisszaigazol[aá]s\s*[:\.\-]?\s*\n?\s*'
    r'(\d{4}[\.\-/]\d{2}[\.\-/]\d{2})'
    r'(?:\s+(\d{2}[:\-\.]\d{2}))?',
    re.IGNORECASE,
)
# Groups: (1) date, (2) time (may be None) — same structure as _SAMPLE_DATE_RE

# "Megjelenés ideje:" — fallback when "Minta dátuma" is 0000.00.00
_APPEARANCE_DATE_RE = re.compile(
    r'Megjelen[eé]s\s+ideje\s*[:\-]?\s*\n?\s*'
    r'(\d{4}[\.\-/]\d{2}[\.\-/]\d{2})'
    r'(?:\s+(\d{2}[:\-\.]\d{2}))?',
    re.IGNORECASE,
)
# Groups: (1) date, (2) time (may be None)

# "Vizsgálat  Eredmény" header that marks the start of a plain-text lab table
_TABLE_HEADER_RE = re.compile(r'Vizsgálat\s+Eredm[eé]ny', re.IGNORECASE)

# Date in any cell of a Camelot table
_CELL_DATE_RE = re.compile(r'(\d{4}[\.\-/]\d{2}[\.\-/]\d{2})')

# Analyte-value pair inside an inline block: "ANALYTE: NUMBER …"
# Non-greedy name, then ":", then numeric value (may use comma as decimal sep).
# Uses re.match (anchored) on each stripped semicolon-segment.
#
# The optional "\*{0,2}" wrappers tolerate pymupdf4llm markdown bold syntax
# (e.g. "Protrombin idő: **15.0** sec") which is emitted for abnormal lab
# values. The `_parse_inline_analytes` also strips stray "**" tokens from
# the block text up front as a belt-and-braces measure.
#
# The value may carry a censoring qualifier (">2000.0", "< 10"); it is kept
# with the value, which is then stored as text (see parse_lab_value).
_INLINE_ANALYTE_RE = re.compile(
    r'^\*{0,2}([^:]+?)\*{0,2}\s*:\s*\*{0,2}([<>≤≥]?\s*[\d,]+(?:[.,]\d+)?)\*{0,2}',
)

# Result reported in a note on ascitic fluid instead of serum:
#   "Glükóz: lásd megjAsc: Glükóz: 7,4 mmol/L"
#   "Totál protein: lásd megjAsc. TP: 11 g/L"
#   "Carcino-embrionális antigén: Lásd megj.Ascites CEA: 0,2 ug/L"
# group(1) → outer (serum) analyte name, group(2) → the ascites value.
# Stored under "ascites_<canonical>" so it never lands in the serum column.
_ASCITES_NOTE_RE = re.compile(
    r'^\*{0,2}([^:]+?)\*{0,2}\s*:\s*l[aá]sd\s*megj\.?\s*Asc[^\d]*?:\s*'
    r'([<>≤≥]?\s*\d+(?:[.,]\d+)?)',
    re.IGNORECASE,
)

# Loose "name: value" for categorical/binary analytes (viral serology, fecal
# occult blood). Value captured as the first non-whitespace token after ":",
# then matched against CATEGORICAL_VALUE_MAP.
_INLINE_LOOSE_VALUE_RE = re.compile(
    r'^\*{0,2}([^:]+?)\*{0,2}\s*:\s*\*{0,2}\s*([^\s*;]+)',
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> date | None:
    """Parse YYYY.MM.DD (or YYYY-MM-DD / YYYY/MM/DD) → date, or None."""
    if not s:
        return None
    # Normalise separator
    s = s.strip().replace('.', '-').replace('/', '-')
    try:
        parts = s.split('-')
        if len(parts) != 3:
            return None
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def _normalize_analyte(raw: str) -> str | None:
    """
    Map a raw Hungarian analyte name to its canonical code.

    Normalises: strips, lowercases, squeezes internal whitespace.
    Returns None if not in LAB_MAPPING (caller should skip).
    """
    key = re.sub(r'\s+', ' ', raw.strip().lower())
    return LAB_MAPPING.get(key)


def _normalize_categorical_analyte(raw: str) -> str | None:
    """Same as _normalize_analyte, but consults CATEGORICAL_LAB_MAPPING."""
    key = re.sub(r'\s+', ' ', raw.strip().lower())
    return CATEGORICAL_LAB_MAPPING.get(key)


def _make_field(
    key: str,
    value: float,
    event_date: date,
    anchor: PatientAnchor,
    doc_type: DocType,
    engine: str,
    page: int | None = None,
) -> ExtractedField | None:
    """
    Wrap a parsed lab value in an ExtractedField.

    Returns None if compute_day would produce 0 (should be impossible per
    the Day Normalisation invariant, but guarded here defensively).
    """
    day = compute_day(event_date, anchor)
    if day == 0:
        log.warning("compute_day returned 0 for %s on %s — skipped", key, event_date)
        return None
    return ExtractedField(
        key=key,
        value=value,
        source=FieldSource.HARD,
        doc_type=doc_type,
        confidence=0.95,
        page=page,
        engine=engine,
    )


# ── TextLabParser ─────────────────────────────────────────────────────────────

class TextLabParser:
    """
    Parse lab values from plain text extracted from a document.

    Handles two document formats:

    1. Inline block format (Zárójelentés pages 3-5)
       Each lab block starts with a header line:
           LMI Központi Laboratórium (KIZKEMIA) 2026.01.13 09:21
           (Mintavétel: 2026.01.13 08:00)
       Followed by semicolon-separated "Name: value unit (range);" entries.
       Uses the Mintavétel date when present; falls back to the header date.

    2. Plain-text table format (structured lab reports after pdfminer)
       Header "Minta dátuma:\\n2026.01.16 17:10" provides the sample date.
       A "Vizsgálat  Eredmény" line marks the table start.
       Each subsequent line: "Name  value  ref_range  unit"
    """

    def parse(
        self,
        text: str,
        anchor: PatientAnchor,
        doc_type: DocType = DocType.LAB_REPORT,
    ) -> list[ExtractedField]:
        """
        Extract all lab fields from *text*.

        Parameters
        ----------
        text : str
            Full document text (all pages joined).
        anchor : PatientAnchor
            Used to compute day numbers.
        doc_type : DocType
            Source document type for ExtractedField provenance.

        Returns
        -------
        list[ExtractedField]
            All successfully parsed fields, in encounter order.
        """
        results: list[ExtractedField] = []
        count_map: dict[tuple[str, int], int] = {}

        # Prefer inline-block format (works for Zárójelentés)
        results += self._parse_inline_blocks(text, anchor, doc_type, count_map)

        # Then try plain-text table format (structured lab reports)
        results += self._parse_plain_table(text, anchor, doc_type, count_map)

        return results

    # ── Private: inline block format ─────────────────────────────────────────

    def _parse_inline_blocks(
        self,
        text: str,
        anchor: PatientAnchor,
        doc_type: DocType,
        count_map: dict,
    ) -> list[ExtractedField]:
        """Parse semicolon-delimited analyte blocks from Zárójelentés.
        Blocks are sorted by sample collection time (Mintavétel: HH:MM) before
        suffix assignment so that _1, _2, … reflect chronological order within
        a day, not document order.  Falls back to document order when no time
        is available.
        """
        from datetime import time as dt_time  # pylint: disable=import-outside-toplevel
        results: list[ExtractedField] = []
        headers = list(_BLOCK_HEADER_RE.finditer(text))

        # ── First pass: collect blocks with their sort keys ───────────────────
        # Merge two header patterns: the classic "Laboratórium" format and the
        # older date-first format.  Both yield (event_date, sort_time, block_text).
        blocks: list[tuple] = []  # (sort_key, doc_order, block_text, event_date)

        # Classic headers: already matched above as `headers`
        for i, header in enumerate(headers):
            block_start = header.end()
            block_end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
            block_text = text[block_start:block_end]

            date_str = header.group(2) or header.group(1)
            event_date = _parse_date(date_str)
            if event_date is None:
                log.debug("Could not parse date in lab block header: %s", header.group(0)[:80])
                continue

            time_str = header.group(3)  # Mintavétel HH:MM or None
            if time_str:
                try:
                    h, m_val = int(time_str[:2]), int(time_str[3:5])
                    sort_key = (event_date, dt_time(h, m_val))
                except (ValueError, IndexError):
                    sort_key = (event_date, dt_time(0, 0))
            else:
                sort_key = (event_date, dt_time(0, 0))

            blocks.append((sort_key, header.start(), block_text, event_date))

        # Date-first headers: "2018.10.03 07:26 - Hematológia (KIZ)"
        date_first_headers = list(_DATE_FIRST_BLOCK_HEADER_RE.finditer(text))
        all_classic_positions = {h.start() for h in headers}

        for i, header in enumerate(date_first_headers):
            # Skip if position overlaps with a classic header (avoids double-counting)
            if any(abs(header.start() - cp) < 10 for cp in all_classic_positions):
                continue

            event_date = _parse_date(header.group(1))
            if event_date is None:
                continue

            time_str = header.group(2)  # always present in this format
            try:
                h_val, m_val = int(time_str[:2]), int(time_str[3:5])
                sort_key = (event_date, dt_time(h_val, m_val))
            except (ValueError, IndexError):
                sort_key = (event_date, dt_time(0, 0))

            block_start = header.end()

            # Block ends at the next header of either type
            next_positions = [
                date_first_headers[i + 1].start() if i + 1 < len(date_first_headers) else len(text),
            ]
            # Also stop at any classic header that follows
            later_classic = [h.start() for h in headers if h.start() > block_start]
            if later_classic:
                next_positions.append(min(later_classic))

            block_end = min(next_positions)
            block_text = text[block_start:block_end]
            blocks.append((sort_key, header.start(), block_text, event_date))

        if not blocks:
            return results

        # ── Sort by (date, time); preserve document order as secondary key ────
        blocks.sort(key=lambda t: (t[0], t[1]))

        # ── Second pass: parse in sorted order so count_map builds correctly ──
        for _, _, block_text, event_date in blocks:
            fields = self._parse_inline_analytes(
                block_text, event_date, anchor, doc_type, count_map
            )
            results.extend(fields)

        return results

    def _parse_inline_analytes(
        self,
        block_text: str,
        event_date: date,
        anchor: PatientAnchor,
        doc_type: DocType,
        count_map: dict,
    ) -> list[ExtractedField]:
        """Parse all "Name: value;" entries from one lab block."""
        fields: list[ExtractedField] = []

        # pymupdf4llm wraps out-of-range values in markdown bold; strip the
        # "**" markers so name/value matching doesn't trip on them.
        block_text = block_text.replace("**", "")

        for segment in block_text.split(';'):
            segment = segment.strip()
            if not segment:
                continue

            # 0) Ascites result written as a note on the serum analyte.
            asc = _ASCITES_NOTE_RE.match(segment)
            if asc:
                canonical = _normalize_analyte(asc.group(1))
                value = parse_lab_value(asc.group(2))
                if canonical is None or value is None:
                    log.debug("Unmapped ascites note: %r", segment[:80])
                    continue
                day = compute_day(event_date, anchor)
                if day == 0:
                    continue
                fields.append(ExtractedField(
                    key=col_key(f"ascites_{canonical}", day, count_map),
                    value=value,
                    source=FieldSource.HARD,
                    doc_type=doc_type,
                    confidence=0.95,
                    page=None,
                    engine="text_block_parser",
                ))
                continue

            # 1) Try the strict numeric path first (most labs).
            m = _INLINE_ANALYTE_RE.match(segment)
            if m:
                raw_name = m.group(1).strip()
                raw_value = m.group(2).strip()
                canonical = _normalize_analyte(raw_name)
                if canonical is None:
                    log.debug("Unknown analyte (inline): %r", raw_name)
                    continue
                value = parse_lab_value(raw_value)
                if value is None:
                    log.debug("Non-numeric value for %r: %r", raw_name, raw_value)
                    continue
                day = compute_day(event_date, anchor)
                if day == 0:
                    log.warning("Day 0 computed for %s on %s — skipped", canonical, event_date)
                    continue
                key = col_key(canonical, day, count_map)
                fields.append(ExtractedField(
                    key=key,
                    value=value,
                    source=FieldSource.HARD,
                    doc_type=doc_type,
                    confidence=0.95,
                    page=None,
                    engine="text_block_parser",
                ))
                continue

            # 2) Numeric regex failed — try the categorical path for viral
            # serology / fecal occult blood (Pozitív→1.0, Negatív→0.0).
            loose = _INLINE_LOOSE_VALUE_RE.match(segment)
            if not loose:
                continue
            raw_name = loose.group(1).strip()
            raw_value = loose.group(2).strip()
            cat_canonical = _normalize_categorical_analyte(raw_name)
            if cat_canonical is None:
                log.debug("Unknown analyte (inline): %r", raw_name)
                continue
            value = CATEGORICAL_VALUE_MAP.get(raw_value.lower())
            if value is None:
                log.debug(
                    "Unrecognised categorical value for %r: %r",
                    raw_name, raw_value,
                )
                continue
            day = compute_day(event_date, anchor)
            if day == 0:
                continue
            key = col_key(cat_canonical, day, count_map)
            fields.append(ExtractedField(
                key=key,
                value=value,
                source=FieldSource.HARD,
                doc_type=doc_type,
                confidence=0.95,
                page=None,
                engine="text_block_parser",
            ))

        return fields

    # ── Private: plain-text table format ─────────────────────────────────────

    def _parse_plain_table(
        self,
        text: str,
        anchor: PatientAnchor,
        doc_type: DocType,
        count_map: dict,
    ) -> list[ExtractedField]:
        """
        Parse a structured lab-report plain text table.

        Detects the "Minta dátuma" + "Vizsgálat Eredmény" pattern that
        appears in pdfminer extracts of structured lab PDFs.
        """
        # Only fires if a table header is present (avoids running on zárójelentés)
        if not _TABLE_HEADER_RE.search(text):
            return []

        # Extract sample date — try multiple strategies
        event_date: date | None = None

        # Try "Minta dátuma" or "Mintavisszaigazolás" first
        dm = _SAMPLE_DATE_RE.search(text) or _MINTAVISSZAIGAZOLAS_RE.search(text)
        if dm:
            event_date = _parse_date(dm.group(1))

        # Fallback: "Megjelenés ideje" (used when "Minta dátuma" is 0000.00.00)
        if event_date is None:
            rd = _APPEARANCE_DATE_RE.search(text)
            if rd:
                event_date = _parse_date(rd.group(1))

        if event_date is None:
            log.debug("Plain-text lab table: no sample date found")
            return []

        # Find the table header row and parse subsequent lines
        th = _TABLE_HEADER_RE.search(text)
        if th is None:
            return []

        table_text = text[th.end():]
        lines = table_text.splitlines()

        fields: list[ExtractedField] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Stop at lines that look like footers / page separators
            if re.match(r'[A-Z]{3,}[^a-z]{0,5}$', line):
                continue

            field = self._parse_plain_table_row(
                line, event_date, anchor, doc_type, count_map
            )
            if field:
                fields.append(field)

        return fields

    def _parse_plain_table_row(
        self,
        line: str,
        event_date: date,
        anchor: PatientAnchor,
        doc_type: DocType,
        count_map: dict,
    ) -> ExtractedField | None:
        """
        Parse one row of a plain-text lab table.

        Expected format: "Name  value  ref_range  unit"
        Strategy: split on whitespace, find the first numeric token — everything
        before it is the analyte name.
        """
        tokens = line.split()
        if len(tokens) < 2:
            return None

        value_idx: int | None = None
        raw_value = ""
        for i, tok in enumerate(tokens):
            # A qualifier may stand alone before the number: "< 10"
            if tok in ("<", ">", "≤", "≥") and i + 1 < len(tokens) \
                    and to_float_hu(tokens[i + 1]) is not None:
                value_idx, raw_value = i, tok + tokens[i + 1]
                break
            if parse_lab_value(tok) is not None:
                value_idx, raw_value = i, tok
                break

        if value_idx is None or value_idx == 0:
            return None

        raw_name = ' '.join(tokens[:value_idx])

        canonical = _normalize_analyte(raw_name)
        if canonical is None:
            log.debug("Unknown analyte (plain table): %r", raw_name)
            return None

        value = parse_lab_value(raw_value)
        if value is None:
            return None

        day = compute_day(event_date, anchor)
        if day == 0:
            return None

        key = col_key(canonical, day, count_map)
        return ExtractedField(
            key=key,
            value=value,
            source=FieldSource.HARD,
            doc_type=doc_type,
            confidence=0.95,
            page=None,
            engine="plain_table_parser",
        )


# ── TableLabParser ────────────────────────────────────────────────────────────

class TableLabParser:
    """
    Parse lab values from Camelot-extracted table grids.

    Each table is a list of rows (list[list[str]]).  The parser:
      1. Scans all cells for a date (YYYY.MM.DD) to use as the sample date.
         The first date found is used.  Date may be in any cell (header rows
         often carry it).
      2. Finds the header row containing "Vizsgálat" and "Eredmény".
      3. Reads subsequent rows: column 0 = analyte name, column 1 = value.

    Falls back to *sample_date* argument if no date is found in cells.
    """

    def parse(
        self,
        tables: list[list[list[str]]],
        anchor: PatientAnchor,
        doc_type: DocType = DocType.LAB_REPORT,
        sample_date: date | None = None,
    ) -> list[ExtractedField]:
        """
        Extract all lab fields from Camelot grid tables.

        Parameters
        ----------
        tables : list[list[list[str]]]
            Camelot-extracted grids (each grid is a list of rows).
        anchor : PatientAnchor
            Used to compute day numbers.
        doc_type : DocType
            Source document type for provenance.
        sample_date : date | None
            Fallback sample date if no date is found in table cells.

        Returns
        -------
        list[ExtractedField]
        """
        results: list[ExtractedField] = []
        count_map: dict[tuple[str, int], int] = {}

        for table in tables:
            fields = self._parse_table(table, anchor, doc_type, sample_date, count_map)
            results.extend(fields)

        return results

    def _parse_table(
        self,
        table: list[list[str]],
        anchor: PatientAnchor,
        doc_type: DocType,
        fallback_date: date | None,
        count_map: dict,
    ) -> list[ExtractedField]:
        """Parse a single Camelot grid."""
        if not table:
            return []

        # Step 1: find sample date in any cell
        event_date = fallback_date
        for row in table:
            for cell in row:
                dm = _CELL_DATE_RE.search(str(cell))
                if dm:
                    d = _parse_date(dm.group(1))
                    if d is not None:
                        event_date = d
                        break
            if event_date is not None and event_date != fallback_date:
                break  # found a date in cells — stop searching

        if event_date is None:
            log.debug("TableLabParser: no sample date found, table skipped")
            return []

        # Step 2: find the data-header row
        data_start: int = 0
        for row_idx, row in enumerate(table):
            row_lower = [str(c).lower() for c in row]
            if any('vizsgálat' in c or 'vizsgálat' in c for c in row_lower) \
               or any('eredmény' in c for c in row_lower):
                data_start = row_idx + 1
                break

        # Step 3: parse data rows
        fields: list[ExtractedField] = []
        for row in table[data_start:]:
            if len(row) < 2:
                continue

            raw_name = str(row[0]).strip()
            raw_value = str(row[1]).strip()

            if not raw_name or not raw_value:
                continue

            canonical = _normalize_analyte(raw_name)
            if canonical is None:
                log.debug("Unknown analyte (table): %r", raw_name)
                continue

            value = parse_lab_value(raw_value)
            if value is None:
                log.debug("Non-numeric value for %r: %r (table)", raw_name, raw_value)
                continue

            day = compute_day(event_date, anchor)
            if day == 0:
                continue

            key = col_key(canonical, day, count_map)
            fields.append(ExtractedField(
                key=key,
                value=value,
                source=FieldSource.HARD,
                doc_type=doc_type,
                confidence=0.95,
                page=None,
                engine="camelot",
            ))

        return fields
