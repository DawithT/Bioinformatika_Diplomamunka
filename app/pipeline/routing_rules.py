"""
app/pipeline/routing_rules.py

Declarative rule set for the document router (step2_route).

This module contains DATA ONLY — no matching logic. The engine in
``step2_route.py`` compiles and applies these rules. Separating the two
means:

  - rule changes are reviewable as data diffs, not logic diffs;
  - the rule set is validated once at import (typos in DocType names,
    empty keyword lists, non-compiling regexes fail fast);
  - a future rule source (JSON file, learned weights from an ML router)
    can replace ``DEFAULT_RULES`` without touching the engine.

Rule semantics (applied by the engine, in order)
------------------------------------------------
1. ``definitive_tier1`` — regexes that decide the DocType outright.
   Only truly unambiguous titles belong here ("zárójelentés").
2. Ambulánslap soft-definitive — if ``ambulanslap_title_pattern`` matches
   and ``modality_override_pattern`` does NOT, classify as AMBULANSLAP.
3. ``definitive_tier2`` — regexes that decide the DocType outright but must
   not outrank the Ambulánslap title (date headers also appear on
   outpatient sheets).
4. Weighted keyword scoring over ``keyword_rules`` — highest score wins;
   AMBULANSLAP receives ``ambulanslap_title_bonus`` when the title pattern
   matched. Scores below ``min_score`` → UNKNOWN.

Keywords are matched case-insensitively as literal substrings; the engine
deduplicates case-variants so a keyword never double-counts. Definitive /
title / override entries are regular expressions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.domain.enums import DocType


# ── Rule data types ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class KeywordRule:
    """One weighted keyword group: each matching keyword adds *weight*."""
    doc_type: DocType
    keywords: tuple[str, ...]
    weight: int


@dataclass(frozen=True)
class DefinitivePattern:
    """A regex whose presence decides the DocType outright."""
    pattern: str
    doc_type: DocType


@dataclass(frozen=True)
class RoutingRuleSet:
    """Complete declarative configuration of the document router."""
    min_score: int
    ambulanslap_title_bonus: int
    ambulanslap_title_pattern: str
    modality_override_pattern: str
    definitive_tier1: tuple[DefinitivePattern, ...]
    definitive_tier2: tuple[DefinitivePattern, ...]
    keyword_rules: tuple[KeywordRule, ...]
    filename_hints: tuple[tuple[str, DocType], ...] = field(default=())

    def validate(self) -> None:
        """Fail fast on malformed rules. Raises ValueError."""
        errors: list[str] = []
        if self.min_score < 1:
            errors.append(f"min_score must be >= 1, got {self.min_score}")
        if self.ambulanslap_title_bonus < 0:
            errors.append("ambulanslap_title_bonus must be >= 0")
        for name, pat in (
            ("ambulanslap_title_pattern", self.ambulanslap_title_pattern),
            ("modality_override_pattern", self.modality_override_pattern),
        ):
            try:
                re.compile(pat, re.IGNORECASE)
            except re.error as exc:
                errors.append(f"{name} does not compile: {exc}")
        for tier_name, tier in (
            ("definitive_tier1", self.definitive_tier1),
            ("definitive_tier2", self.definitive_tier2),
        ):
            for dp in tier:
                try:
                    re.compile(dp.pattern, re.IGNORECASE)
                except re.error as exc:
                    errors.append(f"{tier_name} pattern {dp.pattern!r}: {exc}")
        for kr in self.keyword_rules:
            if not kr.keywords:
                errors.append(f"keyword rule for {kr.doc_type} has no keywords")
            if kr.weight < 1:
                errors.append(f"keyword rule for {kr.doc_type} has weight < 1")
        if errors:
            raise ValueError("Invalid RoutingRuleSet:\n  " + "\n  ".join(errors))


# ── Default rule set ──────────────────────────────────────────────────────────

# Letter-spaced Ambulánslap title, e.g. "A M B U L Á N S  K E Z E L Ő L A P".
_SPACED_TITLE = r"A\s*M\s*B\s*U\s*L\s*Á\s*N\s*S\s+K\s*E\s*Z\s*E\s*L\s*Ő\s*L\s*A\s*P"

DEFAULT_RULES = RoutingRuleSet(
    # Minimum score to accept a keyword-scoring classification.
    min_score=2,

    # Score bonus granted to AMBULANSLAP during weighted scoring when the
    # Ambulánslap title is present. A document that reached the scoring stage
    # despite carrying the title did so because a modality marker was also
    # seen; the bonus ensures an *incidental* mention (e.g. "Kontroll hasi
    # ultrahang javasolt" in the recommendation section) does not outweigh
    # the title, while a genuine modality report (dozens of modality
    # keywords) still wins easily.
    ambulanslap_title_bonus=4,

    ambulanslap_title_pattern=(
        r"ambuláns\s+kezelőlap|ambulans\s+kezelolap|"
        r"\bambulánslap\b|\bambuláns\s+lap\b|" + _SPACED_TITLE
    ),

    # Strong modality-specific markers that OUTRANK the Ambulánslap title:
    # if any is present, the engine defers to tier-2 definitives / scoring so
    # the modality-specific classification can win (that's where the
    # extractable data lives). Lab / virology markers are included so result
    # sheets printed on an Ambuláns kezelőlap form still reach their tier-2
    # definitive rules.
    modality_override_pattern=(
        r"fibroscan|tranziens\s+elasztográfia|\bswe\b|\bpswe\b|"
        r"gastroscopia|gasztroszkóp|colonoscopia|kolonszkóp|"
        r"endoszkópos\s+lelet|endoszkópos\s+labor|"
        r"patológiai\s+osztály|biopszia\s+típusa|hisztológiai\s+diagnózis|"
        r"mikrobiológiai\s+laboratórium|"
        r"ultrahang\s+vizsgálat|hasi\s+ultrahang|ct\s+vizsgálat|mri\s+vizsgálat|"
        r"\bvirológiai?\b|laborvizsgálati\s+eredmények"
    ),

    # Tier 1: checked BEFORE the Ambulánslap title rule. "Zárójelentés" is an
    # unambiguous title that never appears on an outpatient sheet.
    definitive_tier1=(
        DefinitivePattern(r"zárójelentés", DocType.DISCHARGE_SUMMARY),
    ),

    # Tier 2: checked AFTER the Ambulánslap title rule. "Felvétel dátuma" /
    # "Elbocsátás dátuma" header lines also appear on some Ambuláns kezelőlap
    # forms, so they must not steal a document carrying the outpatient title.
    definitive_tier2=(
        DefinitivePattern(r"felvétel\s+dátuma", DocType.DISCHARGE_SUMMARY),
        DefinitivePattern(r"elbocsátás\s+dátuma", DocType.DISCHARGE_SUMMARY),
        # "Virológia" / "Virológiai" unambiguously marks a serology report —
        # even when the same document also lists lab-style analyte rows.
        DefinitivePattern(r"\bvirológiai?\b", DocType.VIROLOGY),
        # New-type lab report title (discharge summaries quote values inline,
        # they do not reproduce this exact phrase).
        DefinitivePattern(r"laborvizsgálati\s+eredmények", DocType.LAB_REPORT),
    ),

    # Weighted keyword scoring. ORDERING RULE: ELASTOGRAPHY before IMAGING —
    # FibroScan reports contain ultrasound vocabulary and would be
    # mis-classified as IMAGING if that rule scored first on a tie.
    # NOTE: generic section headers ("Vélemény:") must NOT appear here —
    # they occur in nearly every Hungarian outpatient document.
    keyword_rules=(
        KeywordRule(DocType.DISCHARGE_SUMMARY, (
            "ZÁRÓJELENTÉS",
            "Felvétel dátuma",
            "Elbocsátás dátuma",
            "Elbocsátva",
            "Felvételi diagnózis",
            "Záródiagnózis",
            "Kezelési összefoglaló",
            "Felvétel típusa",
        ), 3),
        KeywordRule(DocType.LAB_REPORT, (
            "Laborvizsgálati eredmények",
            "Laborvizsgálat",
            "Laboratóriumi Medicina",   # old-type reports: site of measurement
            "L E L E T",                # old-type reports: spaced-caps title
            "Ref.tart",                 # reference range column header
            "Vizsgálat Eredmény",
            "Fehérvérsejt szám",
            "Vörösvértest szám",
            "Hemoglobin",
            "Trombocita",
            "Minta dátuma",
            "Napi sorsz",
            "Közponfi Laboratórium",
            "Központi Laboratórium",
            "giga/L",
            "tera/L",
        ), 3),
        KeywordRule(DocType.ELASTOGRAPHY, (
            "FibroScan Expert",         # exact product name — strongest signal
            "Fibroscan:",
            "Fibroscan :",
            "Tranziens elasztográfia",
            "Nyírási hullám",           # Hungarian: "shear wave"
            "Shear wave",
            "shear-wave",
            "SWE",
            "pSWE",
            "2D-SWE",
            "Fibrosis stádium",
            "Steatosis grade",
            "CAP:",
            "CAP ",
            "IQR/Med",
            "kPa",                      # elastography unit
            "F0", "F1", "F2", "F3", "F4",   # fibrosis staging
            "S0", "S1", "S2", "S3",         # steatosis staging
        ), 2),
        KeywordRule(DocType.ENDOSCOPY, (
            "Gastroscopia",
            "Gastroszkópia",
            "COLONOSCOPIA",
            "Colonoszkópia",
            "Endoszkópos lelet",
            "endoszkópos labor",
            "colonoscop",
            "gastroscop",
            "gastroszkóp",
            "Premedicatio",
            "Asszisztencia",
            "Eszköz visszahúzási idő",
        ), 3),
        KeywordRule(DocType.IMAGING, (
            "Ultrahang vizsgálat",
            "hasi ultrahang",
            "CT vizsgálat",
            "MRI vizsgálat",
            "Képalkotó",
            "Röntgen",
            "echogenitás",
            "parenchyma",
            "lép",                      # spleen — imaging finding
        ), 2),
        KeywordRule(DocType.MICROBIOLOGY, (
            "Tenyésztés",
            "Mikrobiológiai",
            "Baktérium",
            "Antibiotikum",
            "Érzékenység",
            "Rezisztencia",
            "Hemokultúra",
            "Vizeletkultúra",
            "CFU",
            "MIC",
            "Kórokozó",
        ), 3),
        KeywordRule(DocType.VIROLOGY, (
            "Virológiai",
            "Szerológia",
            "HBsAg",
            "anti-HBs",
            "anti-HBc",
            "anti-HCV",
            "HCV-RNS",
            "HBV-DNS",
            "anti-HAV",
            "anti-HEV",
            "anti-HIV",
            "Hepatitis B",
            "Hepatitis C",
            "CMV IgG",
            "EBV IgG",
        ), 3),
        KeywordRule(DocType.AMBULANSLAP, (
            "Ambulánslap",
            "Ambuláns lap",
            "Ambuláns kezelőlap",
            "Ambulans kezelolap",
            "Ambuláns vizsgálat",
            "Járóbeteg",
        ), 3),
        KeywordRule(DocType.HISTOLOGY, (
            "Szövettani",
            "Patológiai",
            "Biopszia",
            "Szövettani lelet",
            "Hisztológiai",
            "Preparátum",
        ), 3),
    ),

    # Filename fallback hints (lower-cased substring → DocType), used when
    # the text layer yields nothing (rasterised scans).
    filename_hints=(
        # "záró"/"zaro" also cover "zárójelentés"/"zarojelentes".
        ("záró",        DocType.DISCHARGE_SUMMARY),
        ("zaro",         DocType.DISCHARGE_SUMMARY),
        ("labor",        DocType.LAB_REPORT),
        ("lelet",        DocType.LAB_REPORT),
        ("fibro",        DocType.ELASTOGRAPHY),
        ("fibroscan",    DocType.ELASTOGRAPHY),
        ("elastogr",     DocType.ELASTOGRAPHY),
        ("shear",        DocType.ELASTOGRAPHY),
        ("swe",          DocType.ELASTOGRAPHY),
        ("gastro",       DocType.ENDOSCOPY),
        ("colono",       DocType.ENDOSCOPY),
        ("endoscop",     DocType.ENDOSCOPY),
        ("tenyésztés",   DocType.MICROBIOLOGY),
        ("tenyesztes",   DocType.MICROBIOLOGY),
        ("mikrobiol",    DocType.MICROBIOLOGY),
        ("ct",           DocType.IMAGING),
        ("mri",          DocType.IMAGING),
        ("ultrahang",    DocType.IMAGING),
        ("histol",       DocType.HISTOLOGY),
        ("szövett",      DocType.HISTOLOGY),
        ("patol",        DocType.HISTOLOGY),
        ("virol",        DocType.VIROLOGY),
        ("szerolog",     DocType.VIROLOGY),
        ("ambuláns",     DocType.AMBULANSLAP),
        ("ambulans",     DocType.AMBULANSLAP),
        ("kezelőlap",    DocType.AMBULANSLAP),
        ("kezelolap",    DocType.AMBULANSLAP),
    ),
)

# Fail fast at import time if the shipped rule set is malformed.
DEFAULT_RULES.validate()
