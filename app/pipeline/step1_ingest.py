"""
app/pipeline/step1_ingest.py

Step 1 of the pipeline: document ingestion.

Loads any supported file format and normalises it into a RoutedDoc with:
  - pages: tuple of per-page text strings
  - tables: tuple of extracted table grids (PDFs only)
  - meta:   hints extracted during ingestion (e.g. source engine used)

Supported inputs:
  - ZIP archive  → txt pages sorted by manifest.json page_number, or
                   by filename (numeric-aware) if no manifest present
  - PDF file     → pymupdf4llm (primary) → pdfminer.six (fallback)
  - DOCX file    → python-docx
  - All others   → RoutedDoc.empty() with a warning

Design contracts:
  - ingest() NEVER raises.  Any unrecoverable error returns RoutedDoc.empty()
    with a descriptive warning stored in meta["warning"].
  - Third-party imports are inside functions — this module is importable with
    stdlib only for fast unit testing.
  - Table extraction (Camelot lattice) is attempted for PDFs; failures are
    silent and return an empty tuple.
"""
from __future__ import annotations

import json
import logging
import re
import zipfile
from pathlib import Path
from typing import Any

from app.domain.enums import DocType
from app.domain.models import RoutedDoc

log = logging.getLogger(__name__)


# ── Public entry-point ────────────────────────────────────────────────────────

def _fold_doc(doc: RoutedDoc) -> RoutedDoc:
    """
    Apply Hungarian mojibake folding (õ→ő, û→ű) to all text carried by *doc*
    (pages and table cells).

    Runs once at the ingest boundary so every downstream consumer — router
    keywords, anchor regexes, lab/event parsers, ancillary section
    extraction, LLM context, exports — sees clean text.
    """
    from app.pipeline.text_normalize import fold_hungarian_mojibake as _fold

    if not doc.pages and not doc.tables:
        return doc

    pages = tuple(_fold(p) for p in doc.pages)
    tables = tuple(
        tuple(
            _fold(cell) if isinstance(cell, str) else cell
            for cell in row
        )
        for row in doc.tables
    )
    return RoutedDoc(
        path=doc.path,
        doc_type=doc.doc_type,
        pages=pages,
        tables=tables,
        meta=doc.meta,
    )


def ingest(path: Path) -> RoutedDoc:
    """
    Load *path* and return a RoutedDoc with extracted text and tables.

    doc_type is set to UNKNOWN — the router (Step 2) will classify it.
    All extracted text is normalised via _fold_doc (Hungarian mojibake).
    This function never raises; errors produce RoutedDoc.empty() + a warning.
    """
    try:
        suffix = path.suffix.lower()

        # DOCX files are ZIP archives — check extension BEFORE zipfile.is_zipfile()
        # so we don't fall into the ZIP/text branch for Office documents.
        if suffix in (".docx", ".doc"):
            return _fold_doc(_ingest_docx(path))

        if zipfile.is_zipfile(path):
            return _fold_doc(_ingest_zip(path))

        if suffix == ".pdf":
            return _fold_doc(_ingest_pdf(path))

        # Unsupported extension
        log.warning("ingest: unsupported file type", extra={"path": str(path)})
        return RoutedDoc.empty(path, DocType.UNKNOWN)

    except Exception as exc:  # pylint: disable=broad-except
        log.warning(
            "ingest: unrecoverable error loading %s: %s",
            path.name,
            exc,
        )
        return RoutedDoc(
            path=path,
            doc_type=DocType.UNKNOWN,
            pages=(),
            tables=(),
            meta={"warning": str(exc), "engine": "failed"},
        )


# ── ZIP branch ────────────────────────────────────────────────────────────────

def _ingest_zip(path: Path) -> RoutedDoc:
    """
    Extract per-page text from a ZIP archive.

    Supported layouts:
      a) manifest.json present → pages listed in manifest order (page_number)
      b) no manifest           → .txt members sorted numerically by filename

    ZIP-slip protection: members that resolve outside the archive root are
    silently skipped (never written to disk — we read into memory only, but
    we still validate the names to be safe).
    """
    pages: list[str] = []
    meta: dict[str, Any] = {"engine": "zip_txt"}

    with zipfile.ZipFile(path, "r") as zf:
        names = zf.namelist()

        # Zip-slip guard: only accept members without path traversal sequences.
        safe_names = [n for n in names if not _is_unsafe_zip_member(n)]

        # Prefer manifest-driven ordering when manifest.json is present.
        if "manifest.json" in safe_names:
            pages = _pages_from_manifest(zf, safe_names)
            meta["manifest"] = True
        else:
            pages = _pages_from_sorted_txt(zf, safe_names)
            meta["manifest"] = False

    if not pages:
        log.warning("ingest: ZIP contained no readable text pages: %s", path.name)

    return RoutedDoc(
        path=path,
        doc_type=DocType.UNKNOWN,
        pages=tuple(pages),
        tables=(),
        meta=meta,
    )


def _is_unsafe_zip_member(name: str) -> bool:
    """Return True if a ZIP member name contains path-traversal sequences."""
    return ".." in name or name.startswith("/") or name.startswith("\\")


def _pages_from_manifest(zf: zipfile.ZipFile, safe_names: list[str]) -> list[str]:
    """Read page text in manifest.json page_number order."""
    try:
        manifest_data = json.loads(zf.read("manifest.json").decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        log.warning("ingest: failed to parse manifest.json: %s", exc)
        return _pages_from_sorted_txt(zf, safe_names)

    pages: list[str] = []
    for page_entry in manifest_data.get("pages", []):
        txt_path = page_entry.get("text", {}).get("path", "")
        if not txt_path:
            pages.append("")  # no text path defined → empty slot
            continue
        if txt_path not in safe_names:
            log.warning("ingest: manifest references missing member: %s", txt_path)
            pages.append("")  # preserve page slot for correct page numbering
            continue
        try:
            raw = zf.read(txt_path).decode("utf-8", errors="replace")
            pages.append(_normalize_text(raw))
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest: could not read ZIP member %s: %s", txt_path, exc)
            pages.append("")  # preserve page slot
    return pages


def _pages_from_sorted_txt(zf: zipfile.ZipFile, safe_names: list[str]) -> list[str]:
    """Read .txt members from the archive, sorted numerically by name stem."""
    txt_names = [n for n in safe_names if n.lower().endswith(".txt")]
    txt_names.sort(key=_numeric_sort_key)
    pages: list[str] = []
    for name in txt_names:
        try:
            raw = zf.read(name).decode("utf-8", errors="replace")
            pages.append(_normalize_text(raw))
        except Exception as exc:  # noqa: BLE001
            log.warning("ingest: could not read ZIP member %s: %s", name, exc)
    return pages


def _numeric_sort_key(name: str) -> tuple:
    """
    Sort key that orders filenames numerically by their numeric parts.
    e.g. '2.txt' < '10.txt', 'page_1.txt' < 'page_10.txt'
    """
    stem = Path(name).stem
    parts = re.split(r"(\d+)", stem)
    return tuple(int(p) if p.isdigit() else p.lower() for p in parts)


# ── PDF branch ────────────────────────────────────────────────────────────────

def _ingest_pdf(path: Path) -> RoutedDoc:
    """
    Extract text from a PDF file.

    Primary engine:  pymupdf4llm (returns markdown-annotated text per page)
    Fallback engine: pdfminer.six (plain text, page-split)

    If both engines fail (e.g. fully rasterised scan), returns empty pages
    without raising.  Camelot table extraction is always attempted in
    lattice mode; failures are swallowed.
    """
    pages = _pdf_text_pymupdf4llm(path)
    engine = "pymupdf4llm"

    if not any(p.strip() for p in pages):
        pages = _pdf_text_pdfminer(path)
        engine = "pdfminer"

    tables = _pdf_tables_camelot(path)

    return RoutedDoc(
        path=path,
        doc_type=DocType.UNKNOWN,
        pages=tuple(pages),
        tables=tuple(tables),
        meta={"engine": engine},
    )


def _pdf_text_pymupdf4llm(path: Path) -> list[str]:
    """Try pymupdf4llm extraction; return [] on ImportError or any failure."""
    try:
        import pymupdf4llm  # noqa: PLC0415  (intentional late import)
        import fitz  # noqa: PLC0415

        doc = fitz.open(str(path))
        pages: list[str] = []
        for page_num in range(doc.page_count):
            try:
                md = pymupdf4llm.to_markdown(str(path), pages=[page_num])
                pages.append(_normalize_text(md))
            except Exception:  # noqa: BLE001
                pages.append("")
        return pages

    except ImportError:
        log.debug("pymupdf4llm not available, will use pdfminer fallback")
        return []
    except Exception as exc:  # noqa: BLE001
        log.warning("pymupdf4llm failed for %s: %s", path.name, exc)
        return []


def reingest_pdf_with_pdfminer(doc: RoutedDoc) -> RoutedDoc:
    """
    Re-extract text from a PDF using pdfminer, preserving doc_type/meta.

    Used for LAB_REPORT PDFs where pymupdf4llm's markdown-table reconstruction
    fragments rotated column labels (e.g. "Fehérvérsejt" → ``|tje<br>s<br>r<br>é<br>v…``)
    because it misreads vertical text as a stack of single-character cells.
    pdfminer's layout-based extraction keeps the labels intact.

    Returns *doc* unchanged if the path is not a PDF or if pdfminer produces no
    text (e.g. the scan has no text layer at all — pymupdf4llm output is then
    the best we have).
    """
    if doc.path.suffix.lower() != ".pdf":
        return doc
    pages = _pdf_text_pdfminer(doc.path)
    if not any(p.strip() for p in pages):
        return doc
    return _fold_doc(RoutedDoc(
        path=doc.path,
        doc_type=doc.doc_type,
        pages=tuple(pages),
        tables=doc.tables,
        meta={**doc.meta, "engine": "pdfminer", "engine_prev": doc.meta.get("engine")},
    ))


def _pdf_text_pdfminer(path: Path) -> list[str]:
    """
    Extract per-page text from a PDF using pdfminer.six.

    Returns a list of strings (one per page).  Returns an empty list on any
    failure (e.g. fully rasterised scans will produce empty strings, not errors).
    """
    try:
        from pdfminer.high_level import extract_pages  # noqa: PLC0415
        from pdfminer.layout import LTTextContainer  # noqa: PLC0415

        pages: list[str] = []
        for page_layout in extract_pages(str(path)):
            page_text = []
            for element in page_layout:
                if isinstance(element, LTTextContainer):
                    page_text.append(element.get_text())
            pages.append(_normalize_text("".join(page_text)))
        return pages

    except Exception as exc:  # noqa: BLE001
        log.warning("pdfminer failed for %s: %s", path.name, exc)
        return []


def _pdf_tables_camelot(path: Path) -> list[tuple]:
    """
    Extract tables from a PDF using Camelot (lattice mode).

    Returns a list of row-grids: each grid is a tuple of rows, each row is
    a tuple of cell strings.  Returns an empty list on any failure.
    """
    try:
        import camelot  # noqa: PLC0415

        tables_obj = camelot.read_pdf(
            str(path),
            pages="all",
            flavor="lattice",
            suppress_stdout=True,
        )
        grids: list[tuple] = []
        for tbl in tables_obj:
            # tbl.df is a pandas DataFrame; convert to nested tuple
            rows = tuple(
                tuple(str(cell) for cell in row)
                for row in tbl.df.itertuples(index=False, name=None)
            )
            grids.append(rows)
        return grids

    except Exception as exc:  # noqa: BLE001
        log.debug("camelot extraction failed for %s: %s", path.name, exc)
        return []


# ── DOCX branch ───────────────────────────────────────────────────────────────

def _ingest_docx(path: Path) -> RoutedDoc:
    """
    Extract text from a DOCX file using python-docx.

    All paragraphs are joined into a single page (DOCX has no page boundaries
    in the binary format — pagination is a rendering artifact).
    Table cells are appended after paragraph text.
    """
    try:
        from docx import Document  # noqa: PLC0415

        doc = Document(str(path))

        # Paragraph text
        para_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

        # Table text
        table_lines: list[str] = []
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells if c.text.strip()]
                if cells:
                    table_lines.append(" | ".join(cells))

        full = para_text
        if table_lines:
            full += "\n\n" + "\n".join(table_lines)

        page = _normalize_text(full)
        return RoutedDoc(
            path=path,
            doc_type=DocType.UNKNOWN,
            pages=(page,) if page else (),
            tables=(),
            meta={"engine": "python-docx"},
        )

    except Exception as exc:  # noqa: BLE001
        log.warning("python-docx failed for %s: %s", path.name, exc)
        return RoutedDoc(
            path=path,
            doc_type=DocType.UNKNOWN,
            pages=(),
            tables=(),
            meta={"warning": str(exc), "engine": "failed"},
        )


# ── Shared utilities ──────────────────────────────────────────────────────────

def _normalize_text(text: str) -> str:
    """
    Normalise raw extracted text:
    - Replace non-breaking spaces with regular spaces.
    - Collapse runs of spaces/tabs to a single space.
    - Collapse 3+ consecutive newlines to 2.
    - Strip leading/trailing whitespace.
    """
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
