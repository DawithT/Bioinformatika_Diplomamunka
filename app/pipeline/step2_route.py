"""
app/pipeline/step2_route.py

Step 2 of the pipeline: document classification (routing) — ENGINE.

The rules themselves (keyword groups, weights, definitive patterns, the
Ambulánslap title/override patterns, filename hints) live as declarative
data in ``app/pipeline/routing_rules.py`` (``DEFAULT_RULES``). This module
compiles that rule set once at import and applies it.

classify(text: str) -> DocType
    Pure text-based classifier. Returns DocType.UNKNOWN if no rule matches —
    not an error.

classify_with_trace(text: str) -> (DocType, dict)
    Same, plus a trace dict (reason / per-type scores / thresholds) that the
    orchestrator persists for the ancillary audit sheet.

route(doc: RoutedDoc) -> RoutedDoc
    Full router: classifies by text first, then falls back to filename
    heuristics when text is absent or too sparse (e.g. fully rasterised
    scans where the text layer is empty).

Classification strategy (in order):
  1. Tier-1 definitive regexes ("zárójelentés") — unambiguous, win always.
  2. Ambulánslap title without a modality-specific marker → AMBULANSLAP.
  3. Tier-2 definitive regexes (admission/discharge date headers, virology,
     lab-report title) — must not outrank the Ambulánslap title because the
     date headers also appear on outpatient sheets.
  4. Weighted keyword scoring — highest score wins; AMBULANSLAP receives a
     title bonus when the Ambulánslap title is present so incidental
     modality mentions cannot outweigh the title.

Design notes:
  - No third-party imports at module level — importable with stdlib only.
  - All patterns compiled once at module load from the declarative rule set.
  - Keyword lists are deduplicated case-insensitively (matching is
    IGNORECASE, so case-variant duplicates would silently double-count).
  - Hungarian mojibake (õ→ő, û→ű) is folded before matching. Ingest already
    folds the whole document; the router folds again defensively because
    classify() also receives raw strings directly (tests, CLI probes).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from app.domain.enums import DocType
from app.domain.models import RoutedDoc
from app.pipeline.routing_rules import DEFAULT_RULES, RoutingRuleSet
from app.pipeline.text_normalize import fold_hungarian_mojibake as _fold_text

log = logging.getLogger(__name__)


# ── Rule compilation ──────────────────────────────────────────────────────────

def _dedupe_keywords(keywords: tuple[str, ...]) -> list[str]:
    """
    Drop case-variant duplicates ("Ambulánslap" / "ambulánslap") from a
    keyword list.  Matching is case-insensitive, so keeping both variants
    would silently double-count every hit and skew the weighted scoring.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for kw in keywords:
        key = kw.lower()
        if key not in seen:
            seen.add(key)
            unique.append(kw)
    return unique


class _CompiledRules:
    """Compiled form of a RoutingRuleSet — regexes ready to apply."""

    def __init__(self, rules: RoutingRuleSet) -> None:
        rules.validate()
        self.min_score = rules.min_score
        self.title_bonus = rules.ambulanslap_title_bonus
        self.title_re = re.compile(rules.ambulanslap_title_pattern, re.IGNORECASE)
        self.override_re = re.compile(rules.modality_override_pattern, re.IGNORECASE)
        self.definitive_tier1 = [
            (re.compile(dp.pattern, re.IGNORECASE), dp.doc_type)
            for dp in rules.definitive_tier1
        ]
        self.definitive_tier2 = [
            (re.compile(dp.pattern, re.IGNORECASE), dp.doc_type)
            for dp in rules.definitive_tier2
        ]
        self.keyword_rules = [
            (
                kr.doc_type,
                [re.compile(re.escape(kw), re.IGNORECASE)
                 for kw in _dedupe_keywords(kr.keywords)],
                kr.weight,
            )
            for kr in rules.keyword_rules
        ]
        self.filename_hints = list(rules.filename_hints)


_RULES = _CompiledRules(DEFAULT_RULES)


# ── Subtype detection ─────────────────────────────────────────────────────────
#
# Endoscopy subtypes: gastroscopy vs colonoscopy
# Imaging subtypes:   UH, CT, MRI, RTG
# Other doc types have no meaningful subtype distinction.

_GASTRO_SUBTYPE_RE = re.compile(
    r'gastroscop|gastroscopia|gasztroszkóp', re.IGNORECASE,
)
_COLONO_SUBTYPE_RE = re.compile(
    r'colonoscop|colonoscopia|kolonoscop|kolonszkóp', re.IGNORECASE,
)
_IMAGING_SUBTYPE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("uh",  re.compile(r'\bultrahang\b|\bUH\b', re.IGNORECASE)),
    ("ct",  re.compile(r'\bCT\b|computer.?tomogr', re.IGNORECASE)),
    ("mri", re.compile(r'\bMR[IT]?\b|mágneses\s+rezonancia', re.IGNORECASE)),
    ("rtg", re.compile(r'\bröntgen\b|\bRTG\b', re.IGNORECASE)),
]
_FIBROSCAN_SUBTYPE_RE = re.compile(
    r'fibroscan|tranziens\s+elasztográfia|CAP\b', re.IGNORECASE,
)
_SHEAR_WAVE_SUBTYPE_RE = re.compile(
    r'shear[\s-]?wave|nyírási\s+hullám|\bSWE\b|\bpSWE\b|2D[\s-]?SWE', re.IGNORECASE,
)


def _detect_subtype(text: str, doc_type: DocType) -> str | None:
    """Return a modality subtype string for event documents, or None."""
    if doc_type == DocType.ENDOSCOPY:
        if _COLONO_SUBTYPE_RE.search(text):
            return "colonoscopy"
        if _GASTRO_SUBTYPE_RE.search(text):
            return "gastroscopy"
        return None

    if doc_type == DocType.IMAGING:
        for subtype, pattern in _IMAGING_SUBTYPE_PATTERNS:
            if pattern.search(text):
                return subtype
        return None

    if doc_type == DocType.ELASTOGRAPHY:
        if _FIBROSCAN_SUBTYPE_RE.search(text):
            return "fibroscan"
        if _SHEAR_WAVE_SUBTYPE_RE.search(text):
            return "shear_wave"
        return None

    return None


# ── Public API ─────────────────────────────────────────────────────────────────

def classify(text: str) -> DocType:
    """
    Classify a document by its text content.

    See the module docstring for the four-stage strategy. Returns
    DocType.UNKNOWN if nothing matches.
    """
    doc_type, _ = classify_with_trace(text)
    return doc_type


def classify_with_trace(text: str) -> tuple[DocType, dict]:
    """
    Like :func:`classify` but also returns a classification trace dict.

    The trace contains:
      - ``reason``   : why this DocType was chosen / not chosen
      - ``scores``   : per-DocType keyword scores (empty for definitive matches)
      - ``best_score``: winning score (0 if empty text or definitive)
      - ``min_required``: the minimum score threshold
    """
    r = _RULES

    if not text or not text.strip():
        return DocType.UNKNOWN, {
            "reason": "empty_text",
            "scores": {},
            "best_score": 0,
            "min_required": r.min_score,
        }

    # Fold mojibake (õ→ő, û→ű) so all subsequent matching sees clean text.
    text = _fold_text(text)

    # 1. Tier-1 definitive match — short-circuit
    for pattern, doc_type in r.definitive_tier1:
        if pattern.search(text):
            return doc_type, {
                "reason": "definitive_keyword",
                "scores": {},
                "best_score": 0,
                "min_required": r.min_score,
            }

    # 2. Soft-definitive: Ambulánslap title wins unless a modality-specific
    # signal is also present (which should take precedence).
    has_ambulans_title = bool(r.title_re.search(text))
    if has_ambulans_title and not r.override_re.search(text):
        return DocType.AMBULANSLAP, {
            "reason": "ambulanslap_title_no_modality_override",
            "scores": {},
            "best_score": 0,
            "min_required": r.min_score,
        }

    # 3. Tier-2 definitive match — short-circuit
    for pattern, doc_type in r.definitive_tier2:
        if pattern.search(text):
            return doc_type, {
                "reason": "definitive_keyword",
                "scores": {},
                "best_score": 0,
                "min_required": r.min_score,
            }

    # 4. Weighted scoring
    text_lower = text.lower()
    scores: dict[str, int] = {}
    best_type = DocType.UNKNOWN
    best_score = 0

    for doc_type, patterns, weight in r.keyword_rules:
        score = sum(weight for pat in patterns if pat.search(text_lower))
        if doc_type == DocType.AMBULANSLAP and has_ambulans_title and score > 0:
            score += r.title_bonus
        if score > 0:
            scores[doc_type.value] = score
        if score > best_score:
            best_score = score
            best_type = doc_type

    if best_score < r.min_score:
        return DocType.UNKNOWN, {
            "reason": "scores_below_threshold",
            "scores": scores,
            "best_score": best_score,
            "min_required": r.min_score,
        }

    return best_type, {
        "reason": "keyword_scoring",
        "scores": scores,
        "best_score": best_score,
        "min_required": r.min_score,
    }


def classify_by_filename(path: Path) -> DocType:
    """
    Classify a document by its filename alone.

    Used as a fallback when text extraction yields no usable content (e.g.
    fully rasterised scans).  Returns DocType.UNKNOWN if no hint matches.
    """
    stem_lower = _fold_text(path.stem).lower()
    for hint, doc_type in _RULES.filename_hints:
        if hint in stem_lower:
            return doc_type
    return DocType.UNKNOWN


def route(doc: RoutedDoc) -> RoutedDoc:
    """
    Classify a RoutedDoc and return a new RoutedDoc with doc_type set.

    Strategy:
      1. Try text classification on first_3000 chars.
      2. If text yields UNKNOWN (e.g. empty text layer), fall back to filename.
      3. Log a warning if still UNKNOWN after both strategies.

    The returned RoutedDoc's ``meta`` dict is enriched with:
      - ``routing_method``  : "text" | "filename_fallback" | "unclassified"
      - ``routing_subtype`` : modality subtype (e.g. "gastroscopy", "uh") or None
      - ``routing_trace``   : classification scores and reason dict
    """
    text = doc.first_3000
    doc_type, trace = classify_with_trace(text)
    method = "text"

    if doc_type == DocType.UNKNOWN:
        doc_type = classify_by_filename(doc.path)
        if doc_type != DocType.UNKNOWN:
            method = "filename_fallback"
            log.info(
                "route: text classification failed; classified %s by filename → %s",
                doc.path.name,
                doc_type.value,
            )
        else:
            method = "unclassified"
            log.warning(
                "route: could not classify %s (empty or unrecognised) → UNKNOWN",
                doc.path.name,
            )

    subtype = _detect_subtype(_fold_text(text), doc_type) if text else None

    meta_update: dict = {
        "routing_method": method,
        "routing_subtype": subtype,
        "routing_trace": trace,
    }

    return doc.with_doc_type(doc_type, meta_update=meta_update)
