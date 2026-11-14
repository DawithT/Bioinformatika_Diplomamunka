"""
app/pipeline/step5_extract/hard_structured.py

Step 5 — hard extraction of structured data from discharge summaries.

BNOParser
    Extracts BNO/ICD-10 diagnosis codes from the diagnosis table and maps
    known codes to registry field values (etiology, comorbidities, etc.).

    The diagnosis table can appear ANYWHERE in the document — at the
    beginning (old format) or at the end (new format).  The parser searches
    the entire text for the "Diagnózisok" / "BNO" header, then parses the
    table that follows.

VirologyParser
    Extracts hepatitis and other virology serology results from virology
    lab blocks ("MBV Mikrob. Virológiai Labor" or similar headers).  Interprets
    marker results (HBsAg, anti-HBc, anti-HCV, HCV RNS) into the anamnesis
    etiology fields (hepatitis_b/c_virus_etiologia, hbv/hcv_statusz).

    Run on discharge summaries only.  The in-hospital virology fields
    (virologia_*_inhosp) are filled by the LLM extractor instead.

Anamnézis handling
------------------
Neither parser restricts its search to, or excludes, the Anamnézis
section — both scan the whole document text.

Source metadata
---------------
BNO fields:   source=HEURISTIC, confidence=0.90, engine="bno_parser"
Virology:     source=HEURISTIC, confidence=0.90, engine="virology_parser"
"""
from __future__ import annotations

import logging
import re
from typing import Any

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField, PatientAnchor, RoutedDoc

log = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

_SOURCE = FieldSource.HEURISTIC
_BNO_CONFIDENCE = 0.90
_BNO_ENGINE = "bno_parser"
_VIRO_CONFIDENCE = 0.90
_VIRO_ENGINE = "virology_parser"


def _make_bno_field(key: str, value: Any, doc_type: DocType) -> ExtractedField:
    return ExtractedField(
        key=key,
        value=value,
        source=_SOURCE,
        doc_type=doc_type,
        confidence=_BNO_CONFIDENCE,
        engine=_BNO_ENGINE,
    )


def _make_viro_field(key: str, value: Any, doc_type: DocType) -> ExtractedField:
    return ExtractedField(
        key=key,
        value=value,
        source=_SOURCE,
        doc_type=doc_type,
        confidence=_VIRO_CONFIDENCE,
        engine=_VIRO_ENGINE,
    )


# ══════════════════════════════════════════════════════════════════════════════
# BNO PARSER
# ══════════════════════════════════════════════════════════════════════════════

# ── BNO table detection ──────────────────────────────────────────────────────
# Finds the diagnosis table header.  The table appears under a header like:
#   "Diagnózisok:"
#   "BNO kód  M  D  Onkológia  Típus  Megnevezés"
# Both old (beginning) and new (end) positions are handled.

_DIAG_HEADER_RE = re.compile(
    r'(?:Diagn[oó]zis(?:ok)?|Z[aá]r[oó]diagn[oó]zis(?:ok)?)\s*:?',
    re.IGNORECASE,
)

# BNO code line:  K7030   1   Alkoholos májzsugorodás
# The pattern captures the BNO code, optional type number, and description.
# BNO codes are: letter + 2-4 digits + optional letter(s) e.g. K7030, N1780, R17H0
_BNO_LINE_RE = re.compile(
    r'^\s*([A-Z]\d{2,4}[A-Z]?\d?)\s+'    # BNO code
    r'(?:\S+\s+)*?'                        # skip M, D, Onkológia columns
    r'(\d)\s+'                             # Típus (meaning: see allowed-Típus table below)
    r'(.+)$',                              # Megnevezés (description)
    re.IGNORECASE | re.MULTILINE,
)

# Simpler fallback: just BNO code + description on a line (no type column)
_BNO_SIMPLE_RE = re.compile(
    r'^\s*([A-Z]\d{2,4}[A-Z]?\d?)\s{2,}(.+)$',
    re.IGNORECASE | re.MULTILINE,
)

# ── BNO → registry field mapping ────────────────────────────────────────────
# Maps BNO code prefixes to (registry_field_name, value, allowed_tipus) tuples.
#
# Allowed Típus filter — drops mappings whose target timepoint does not match
# the diagnosis Típus column on the BNO row:
#
#   Típus 0: admitting diagnosis             → _ADM_TIPUS
#   Típus 1: underlying cause of principal   → _ADM_TIPUS
#   Típus 2: transfer-justifying             → _INHOSP_TIPUS
#   Típus 3: principal diagnosis of care     → _ADM_TIPUS
#   Típus 4: complication                    → _INHOSP_TIPUS
#   Típus 5: comorbidity                     → etiology/anamnesis only (_ANY_TIPUS)
#   Típus 6-8: death / autopsy               → _DIS_TIPUS
#       6 = immediate cause of death (clinical)
#       7 = underlying disease (pathological)
#       8 = immediate cause of death (pathological)
#   Also possible (not mapped, per source-system info): 9 (meaning unknown),
#       B = underlying cause of death (clinical), C = nosocomial complication.
#       The row regex only reads a digit, so B / C rows are not parsed.
#
# Etiology / anamnesis targets accept ANY Típus (including 5 for comorbidity
# history). Adm-timepoint targets (felvetelkor_*, *_adm) only fire on codes
# that actually represent admission-time state: 0, 1, 3.
#
# Only high-confidence, unambiguous mappings are included.
# The LLM can handle more nuanced interpretation when enabled.

_ANY_TIPUS: frozenset[str] | None = None           # no filter
_ADM_TIPUS: frozenset[str] = frozenset({"0", "1", "3"})
_INHOSP_TIPUS: frozenset[str] = frozenset({"2", "4"})
_DIS_TIPUS: frozenset[str] = frozenset({"6", "7", "8"})

# Entry type: (registry_field_name, value, allowed_tipus_or_None)
_BNOEntry = tuple[str, str, "frozenset[str] | None"]

_BNO_FIELD_MAP: dict[str, list[_BNOEntry]] = {
    # ── Etiology / anamnesis targets (any Típus, including 5) ─────────────
    # Alcoholic liver disease → alkoholos / toxikus etiology
    "K70":  [("alkoholos_toxikus_etiologia", "Igen", _ANY_TIPUS)],

    # Chronic hepatitis B → HBV etiology
    "B181": [("hepatitis_b_virus_etiologia", "Igen", _ANY_TIPUS)],
    "B180": [("hepatitis_b_virus_etiologia", "Igen", _ANY_TIPUS)],

    # Chronic hepatitis C → HCV etiology
    "B182": [("hepatitis_c_virus_etiologia", "Igen", _ANY_TIPUS)],

    # Autoimmune hepatitis
    "K754": [("autoimmun_hepatitisz_etiologia", "Igen", _ANY_TIPUS)],

    # Diabetes mellitus type 1
    "E10":  [
        ("cukorbetegseg_dm", "Igen", _ANY_TIPUS),
        ("dm_tipusa", "1-es", _ANY_TIPUS),
    ],
    # Diabetes mellitus type 2
    "E11":  [
        ("cukorbetegseg_dm", "Igen", _ANY_TIPUS),
        ("dm_tipusa", "2-es", _ANY_TIPUS),
    ],

    # Hypertension (anamnesis)
    "I10":  [("magas_vernyomas", "Igen", _ANY_TIPUS)],
    "I11":  [("magas_vernyomas", "Igen", _ANY_TIPUS)],
    "I12":  [("magas_vernyomas", "Igen", _ANY_TIPUS)],
    "I13":  [("magas_vernyomas", "Igen", _ANY_TIPUS)],
    "I15":  [("magas_vernyomas", "Igen", _ANY_TIPUS)],

    # Hepatocellular carcinoma (anamnesis — "has had HCC")
    "C22":  [("hepatocellularis_carcinoma_hcc", "Igen", _ANY_TIPUS)],
    "C220": [("hepatocellularis_carcinoma_hcc", "Igen", _ANY_TIPUS)],

    # ── Admission-state targets (Típus 0/1/3 only) ────────────────────────
    # Jaundice / icterus at admission
    "R17":  [("icterus_adm", "Igen", _ADM_TIPUS)],

    # Ascites at admission
    "R18":  [("felvetelkor_van_ascites_adm", "Igen", _ADM_TIPUS)],

    # Hepatic encephalopathy at admission
    "K72":  [("felvetelkor_hepatikus_enkefalopatia_he_van_adm", "Igen", _ADM_TIPUS)],

    # Acute kidney injury / renal failure at admission
    "N17":  [("aki_akut_vese_elegtelenseg_adm", "Igen", _ADM_TIPUS)],

    # GI bleeding at admission
    "K920": [("felvetelkori_gasztrointesztinalis_gi_verzes_adm", "Igen", _ADM_TIPUS)],
    "K921": [("felvetelkori_gasztrointesztinalis_gi_verzes_adm", "Igen", _ADM_TIPUS)],
    "K922": [("felvetelkori_gasztrointesztinalis_gi_verzes_adm", "Igen", _ADM_TIPUS)],

    # Esophageal varices with bleeding
    "I850": [
        ("felvetelkori_gasztrointesztinalis_gi_verzes_adm", "Igen", _ADM_TIPUS),
    ],
    # Esophageal varices without bleeding
    "I859": [],

    # Sepsis at admission
    "A41":  [("felvetelkori_szepszis_adm", "Igen", _ADM_TIPUS)],
    "R65":  [("felvetelkori_szepszis_adm", "Igen", _ADM_TIPUS)],

    # Portal hypertension — informational only
    "K766": [],
}


def _lookup_bno(code: str, tipus: str) -> list[tuple[str, str]]:
    """
    Look up a BNO code in the mapping, trying progressively shorter prefixes,
    then filter the entries by the diagnosis Típus column.

    Returns list of (field_name, value) tuples for mappings whose allowed_tipus
    set contains *tipus* (or is None, meaning any Típus is accepted).
    """
    code = code.replace(".", "").replace(" ", "").upper()
    tipus = (tipus or "").strip()

    entries: list[_BNOEntry] = []
    for length in range(len(code), 2, -1):
        prefix = code[:length]
        if prefix in _BNO_FIELD_MAP:
            entries = _BNO_FIELD_MAP[prefix]
            break

    result: list[tuple[str, str]] = []
    for field_name, value, allowed in entries:
        if allowed is None or tipus in allowed:
            result.append((field_name, value))
    return result


class BNOParser:
    """
    Extract BNO/ICD-10 diagnosis codes and map them to registry fields.

    Searches the entire document for the diagnosis table, parses each
    BNO code, and returns registry field implications.
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        text = doc.full_text
        dt = doc.doc_type
        codes = self._extract_bno_codes(text)

        if not codes:
            return []

        fields: list[ExtractedField] = []
        seen_keys: set[str] = set()

        for code, bno_typus, _desc in codes:
            mappings = _lookup_bno(code, bno_typus)
            for field_name, value in mappings:
                if field_name not in seen_keys:
                    fields.append(_make_bno_field(field_name, value, dt))
                    seen_keys.add(field_name)

        if fields:
            log.info(
                "BNOParser: %d BNO codes found, mapped to %d registry fields",
                len(codes), len(fields),
            )

        return fields

    def _extract_bno_codes(self, text: str) -> list[tuple[str, str, str]]:
        """
        Parse the BNO diagnosis table from the document text.

        Returns list of (code, type, description) tuples.
        Type is "1" for primary, other digits for secondary/comorbidity.
        """
        # Find the diagnosis header
        header_m = _DIAG_HEADER_RE.search(text)
        if not header_m:
            return []

        # Extract the block after the header (up to 3000 chars or next major section)
        block_start = header_m.end()
        block_end = min(block_start + 3000, len(text))

        # Stop at next major section header
        section_stop = re.search(
            r'\n\s*(?:Alkalmazott|Epikr[ií]z|Javasl|Therap|Gy[oó]gyszer)',
            text[block_start:block_end],
            re.IGNORECASE,
        )
        if section_stop:
            block_end = block_start + section_stop.start()

        block = text[block_start:block_end]

        # Try structured format first (with type column)
        codes: list[tuple[str, str, str]] = []
        for m in _BNO_LINE_RE.finditer(block):
            codes.append((m.group(1).strip(), m.group(2).strip(), m.group(3).strip()))

        if codes:
            return codes

        # Fallback: simple format (code + description, no type)
        for m in _BNO_SIMPLE_RE.finditer(block):
            codes.append((m.group(1).strip(), "0", m.group(2).strip()))

        return codes


# ══════════════════════════════════════════════════════════════════════════════
# VIROLOGY PARSER
# ══════════════════════════════════════════════════════════════════════════════

# ── Virology block detection ─────────────────────────────────────────────────
# The virology block typically starts with a header like:
#   "MBV Mikrob. Virológiai Labor" or "Szerológia" or "Vírus" header
#   followed by semicolon-delimited results.

_VIRO_HEADER_RE = re.compile(
    r'(?:MBV\s+Mikrob|Virol[oó]gi|Szerol[oó]gi|V[ií]rus\s+szero)',
    re.IGNORECASE,
)

# Individual marker patterns — case-insensitive, flexible spacing
_HBSAG_RE = re.compile(r'HBs[-\s]?Ag[\s:]*(\w+)', re.IGNORECASE)
_ANTI_HBC_RE = re.compile(r'anti[-\s]?HBc(?:\s*\([^)]*\))?[\s:]*(\w+)', re.IGNORECASE)
_ANTI_HBS_RE = re.compile(r'anti[-\s]?HBs(?:\s*\([^)]*\))?[\s:]*([<>\d\w]+)', re.IGNORECASE)
_ANTI_HCV_RE = re.compile(r'anti[-\s]?HCV(?:\s*\([^)]*\))?[\s:]*(\w+)', re.IGNORECASE)
_HCV_RNA_RE = re.compile(r'HCV\s*(?:RNS|RNA)[\s:]*(\w+)', re.IGNORECASE)
_HBV_DNA_RE = re.compile(r'HBV\s*(?:DNS|DNA)[\s:]*(\w+)', re.IGNORECASE)

_POSITIVE_WORDS = frozenset({"pozitív", "pozitiv", "positive", "poz", "reaktív", "reaktiv"})
_NEGATIVE_WORDS = frozenset({"negatív", "negativ", "negative", "neg", "nem"})


def _is_positive(value: str) -> bool:
    return value.strip().lower() in _POSITIVE_WORDS


def _is_negative(value: str) -> bool:
    return value.strip().lower() in _NEGATIVE_WORDS


class VirologyParser:
    """
    Extract hepatitis serology results and map to registry fields.

    Interpretation logic:
      - HBsAg negative + anti-HBc negative → no HBV exposure
      - HBsAg negative + anti-HBc positive → resolved HBV
      - HBsAg positive → active HBV
      - anti-HCV negative → no HCV
      - anti-HCV positive + HCV RNA negative → resolved HCV
      - anti-HCV positive + HCV RNA positive → active HCV
    """

    def parse(
        self,
        doc: RoutedDoc,
        anchor: PatientAnchor,
    ) -> list[ExtractedField]:
        text = doc.full_text
        dt = doc.doc_type
        fields: list[ExtractedField] = []

        # Find virology block(s)
        blocks = self._find_viro_blocks(text)
        if not blocks:
            return []

        # Parse markers from all blocks (may be split across blocks)
        markers: dict[str, str] = {}
        for block in blocks:
            markers.update(self._parse_markers(block))

        if not markers:
            return []

        # ── HBV interpretation ───────────────────────────────────────────
        hbsag = markers.get("hbsag")
        anti_hbc = markers.get("anti_hbc")

        if hbsag:
            if _is_negative(hbsag):
                if anti_hbc and _is_positive(anti_hbc):
                    # Resolved HBV (HBsAg neg, anti-HBc pos)
                    fields.append(_make_viro_field("hepatitis_b_virus_etiologia", "Igen", dt))
                    fields.append(_make_viro_field(
                        "hbv_statusz",
                        "Lezajlott (HBs-ag negatív, anti-HBc pozitív)",
                        dt,
                    ))
                elif anti_hbc and _is_negative(anti_hbc):
                    # No HBV exposure
                    fields.append(_make_viro_field("hepatitis_b_virus_etiologia", "Nem", dt))
                else:
                    # HBsAg negative but anti-HBc unknown
                    fields.append(_make_viro_field("hepatitis_b_virus_etiologia", "Nem", dt))
            elif _is_positive(hbsag):
                # Active HBV
                fields.append(_make_viro_field("hepatitis_b_virus_etiologia", "Igen", dt))
                fields.append(_make_viro_field(
                    "hbv_statusz",
                    "Aktív (HBs-antigén pozitív)",
                    dt,
                ))

        # ── HCV interpretation ───────────────────────────────────────────
        anti_hcv = markers.get("anti_hcv")
        hcv_rna = markers.get("hcv_rna")

        if anti_hcv:
            if _is_negative(anti_hcv):
                fields.append(_make_viro_field("hepatitis_c_virus_etiologia", "Nem", dt))
            elif _is_positive(anti_hcv):
                fields.append(_make_viro_field("hepatitis_c_virus_etiologia", "Igen", dt))
                if hcv_rna:
                    if _is_negative(hcv_rna):
                        fields.append(_make_viro_field(
                            "hcv_statusz",
                            "Lezajlott (RNS negatív, anti-HCV pozitív)",
                            dt,
                        ))
                    elif _is_positive(hcv_rna):
                        fields.append(_make_viro_field(
                            "hcv_statusz",
                            "Aktív (HCV RNS pozitív)",
                            dt,
                        ))

        if fields:
            log.info(
                "VirologyParser: extracted %d fields (markers: %s)",
                len(fields), list(markers.keys()),
            )

        return fields

    def _find_viro_blocks(self, text: str) -> list[str]:
        """Find all virology result blocks in the text."""
        blocks: list[str] = []
        for m in _VIRO_HEADER_RE.finditer(text):
            # Take up to 800 chars after the header
            block_end = min(m.start() + 800, len(text))
            blocks.append(text[m.start():block_end])
        return blocks

    def _parse_markers(self, block: str) -> dict[str, str]:
        """Parse virology markers from a block of text."""
        markers: dict[str, str] = {}

        m = _HBSAG_RE.search(block)
        if m:
            markers["hbsag"] = m.group(1)

        m = _ANTI_HBC_RE.search(block)
        if m:
            markers["anti_hbc"] = m.group(1)

        m = _ANTI_HBS_RE.search(block)
        if m:
            markers["anti_hbs"] = m.group(1)

        m = _ANTI_HCV_RE.search(block)
        if m:
            markers["anti_hcv"] = m.group(1)

        m = _HCV_RNA_RE.search(block)
        if m:
            markers["hcv_rna"] = m.group(1)

        m = _HBV_DNA_RE.search(block)
        if m:
            markers["hbv_dna"] = m.group(1)

        return markers
