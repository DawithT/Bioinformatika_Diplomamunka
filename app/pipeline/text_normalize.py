"""
app/pipeline/text_normalize.py

Shared text normalisation for the whole pipeline.

Hungarian PDF text layers frequently carry a CP1250→Latin-1 glyph artefact:
the double-acute letters ő/ű (present in CP1250/ISO-8859-2 but absent from
Latin-1) are rendered as õ/û (o/u with tilde/circumflex). Real-world example
from production data: ``AMBULÁNS KEZELÕLAP``.

Why not ftfy?
-------------
``ftfy`` repairs *encoding round-trip* mojibake (e.g. UTF-8 bytes decoded as
Latin-1: ``Ã©`` → ``é``). The artefact handled here is different: õ/û are
perfectly valid characters (Portuguese/French use them), so there is no
detectable encoding error — verified empirically with ftfy 6.3.1:
``ftfy.fix_text("AMBULÁNS KEZELÕLAP")`` returns the input unchanged.
The Hungarian alphabet contains no õ/û, which makes a deterministic fold
lossless *for this corpus*, dependency-free, and offline-safe.

This module is the single place such rules live. It is applied once at
ingest (step 1), so every downstream consumer — router keywords, anchor
regexes, lab/event parsers, ancillary section extraction, LLM context —
sees clean text. The router additionally folds defensively for callers
that classify raw strings directly (e.g. tests).
"""
from __future__ import annotations

# õ/û (and uppercase) are the Latin-1 renderings of CP1250 ő/ű.
_HU_MOJIBAKE_TRANSLATION = str.maketrans("õÕûÛ", "őŐűŰ")


def fold_hungarian_mojibake(text: str) -> str:
    """Fold CP1250→Latin-1 mojibake (õ→ő, û→ű) in Hungarian text."""
    return text.translate(_HU_MOJIBAKE_TRANSLATION)
