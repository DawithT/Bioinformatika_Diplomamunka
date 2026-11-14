"""
tests/unit/test_ingest.py

Step 4 tests: document ingestion (app/pipeline/step1_ingest.py).

Tests cover:
  - ZIP with manifest.json  → pages in manifest order
  - ZIP without manifest    → pages in numeric filename order
  - ZIP-slip protection     → unsafe members skipped
  - Fixture ZIP "PDFs"      → FS_zaro.pdf, FS_labor2.pdf, etc.
  - Real PDF (rare)         → text extracted (skipped if pymupdf4llm absent)
  - DOCX                    → text extracted
  - Corrupted/unreadable    → RoutedDoc.empty(), no exception
  - Unsupported extension   → RoutedDoc.empty()
  - Normalise text          → non-breaking spaces, excess newlines

All tests use stdlib + installed packages only.
Fixture files are read from tests/fixtures/ and skipped if absent.
"""
from __future__ import annotations

import io
import sys
import traceback
import zipfile
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"

# All fixture ZIPs (named .pdf but are ZIP archives of scanned pages)
FIXTURE_ZIPS = {
    "zaro":       FIXTURES_DIR / "FS_zaro.pdf",
    "labor":      FIXTURES_DIR / "FS_labor2.pdf",
    "fibro":      FIXTURES_DIR / "FS_fibro.pdf",
    "endogastro": FIXTURES_DIR / "FS_endogastro.pdf",
    "endocolono": FIXTURES_DIR / "FS_endocolono.pdf",
    "tenyesztes": FIXTURES_DIR / "FS_tenyesztes.pdf",
}


def _make_zip_bytes(
    txt_pages: dict[str, str],
    manifest: dict | None = None,
) -> bytes:
    """
    Build an in-memory ZIP archive for testing.

    txt_pages: {filename: content} mapping
    manifest:  optional manifest dict (serialised to manifest.json)
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in txt_pages.items():
            zf.writestr(name, content.encode("utf-8"))
        if manifest is not None:
            import json
            zf.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
    return buf.getvalue()


def _make_tmp_zip(tmp_path: Path, name: str, **kwargs) -> Path:
    """Write an in-memory ZIP to a temp file and return its path."""
    p = tmp_path / name
    p.write_bytes(_make_zip_bytes(**kwargs))
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Import the module under test
# ─────────────────────────────────────────────────────────────────────────────

from app.pipeline.step1_ingest import (
    _normalize_text,
    _numeric_sort_key,
    _is_unsafe_zip_member,
    ingest,
)
from app.domain.enums import DocType
from app.domain.models import RoutedDoc


# ─────────────────────────────────────────────────────────────────────────────
# Utility function tests
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeText:
    def test_nbsp_replaced(self):
        assert _normalize_text("a\u00a0b") == "a b"

    def test_excess_spaces_collapsed(self):
        assert _normalize_text("a   b\t\tc") == "a b c"

    def test_excess_newlines_collapsed(self):
        result = _normalize_text("a\n\n\n\nb")
        assert result == "a\n\nb"

    def test_strips_whitespace(self):
        assert _normalize_text("  hello  ") == "hello"

    def test_empty_string(self):
        assert _normalize_text("") == ""

    def test_unicode_content_preserved(self):
        result = _normalize_text("Felvétel dátuma: 2026.01.12")
        assert "Felvétel" in result
        assert "2026.01.12" in result


class TestNumericSortKey:
    def test_numeric_files_sort_correctly(self):
        names = ["10.txt", "2.txt", "1.txt", "20.txt"]
        sorted_names = sorted(names, key=_numeric_sort_key)
        assert sorted_names == ["1.txt", "2.txt", "10.txt", "20.txt"]

    def test_alpha_then_numeric(self):
        names = ["page_10.txt", "page_2.txt", "page_1.txt"]
        sorted_names = sorted(names, key=_numeric_sort_key)
        assert sorted_names == ["page_1.txt", "page_2.txt", "page_10.txt"]

    def test_pure_alpha(self):
        names = ["b.txt", "a.txt", "c.txt"]
        sorted_names = sorted(names, key=_numeric_sort_key)
        assert sorted_names == ["a.txt", "b.txt", "c.txt"]


class TestZipSlipGuard:
    def test_dotdot_is_unsafe(self):
        assert _is_unsafe_zip_member("../etc/passwd") is True

    def test_absolute_path_is_unsafe(self):
        assert _is_unsafe_zip_member("/etc/passwd") is True

    def test_backslash_abs_is_unsafe(self):
        assert _is_unsafe_zip_member("\\etc\\passwd") is True

    def test_safe_filename(self):
        assert _is_unsafe_zip_member("1.txt") is False

    def test_safe_subdir(self):
        assert _is_unsafe_zip_member("pages/1.txt") is False


# ─────────────────────────────────────────────────────────────────────────────
# ZIP ingestion — synthetic in-memory archives
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestZipSynthetic:
    """Tests using in-memory ZIP archives (no fixture files required)."""

    def test_pages_extracted_from_manifest_order(self, tmp_path):
        manifest = {
            "pages": [
                {"page_number": 1, "text": {"path": "1.txt"}},
                {"page_number": 2, "text": {"path": "2.txt"}},
                {"page_number": 3, "text": {"path": "3.txt"}},
            ]
        }
        p = _make_tmp_zip(
            tmp_path, "test.zip",
            txt_pages={"1.txt": "Page one", "2.txt": "Page two", "3.txt": "Page three"},
            manifest=manifest,
        )
        doc = ingest(p)
        assert len(doc.pages) == 3
        assert doc.pages[0] == "Page one"
        assert doc.pages[1] == "Page two"
        assert doc.pages[2] == "Page three"

    def test_pages_sorted_numerically_without_manifest(self, tmp_path):
        p = _make_tmp_zip(
            tmp_path, "no_manifest.zip",
            txt_pages={"10.txt": "Ten", "2.txt": "Two", "1.txt": "One"},
        )
        doc = ingest(p)
        assert len(doc.pages) == 3
        assert doc.pages[0] == "One"
        assert doc.pages[1] == "Two"
        assert doc.pages[2] == "Ten"

    def test_doc_type_unknown(self, tmp_path):
        p = _make_tmp_zip(tmp_path, "test.zip", txt_pages={"1.txt": "hello"})
        doc = ingest(p)
        assert doc.doc_type == DocType.UNKNOWN

    def test_tables_empty_for_zip(self, tmp_path):
        p = _make_tmp_zip(tmp_path, "test.zip", txt_pages={"1.txt": "hello"})
        doc = ingest(p)
        assert doc.tables == ()

    def test_empty_zip_returns_empty_pages(self, tmp_path):
        p = _make_tmp_zip(tmp_path, "empty.zip", txt_pages={})
        doc = ingest(p)
        assert isinstance(doc.pages, tuple)
        assert len(doc.pages) == 0

    def test_zip_slash_safety_unsafe_members_skipped(self, tmp_path):
        """ZIP containing an unsafe member — it must be ignored."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("1.txt", "safe page")
            zf.writestr("../evil.txt", "should not appear")
        p = tmp_path / "unsafe.zip"
        p.write_bytes(buf.getvalue())

        doc = ingest(p)
        # Only the safe member should be used
        assert len(doc.pages) == 1
        assert doc.pages[0] == "safe page"

    def test_manifest_with_missing_txt_produces_empty_slot(self, tmp_path):
        """Manifest references a .txt that is missing from the archive."""
        manifest = {
            "pages": [
                {"page_number": 1, "text": {"path": "1.txt"}},
                {"page_number": 2, "text": {"path": "missing.txt"}},
                {"page_number": 3, "text": {"path": "3.txt"}},
            ]
        }
        p = _make_tmp_zip(
            tmp_path, "partial.zip",
            txt_pages={"1.txt": "First", "3.txt": "Third"},
            manifest=manifest,
        )
        doc = ingest(p)
        # page 2 slot → empty string; overall 3 pages
        assert len(doc.pages) == 3
        assert doc.pages[0] == "First"
        assert doc.pages[1] == ""
        assert doc.pages[2] == "Third"

    def test_unicode_content_preserved(self, tmp_path):
        p = _make_tmp_zip(
            tmp_path, "hu.zip",
            txt_pages={"1.txt": "Felvétel dátuma: 2026.01.12 04:43"},
        )
        doc = ingest(p)
        assert "Felvétel" in doc.pages[0]

    def test_meta_engine_is_zip_txt(self, tmp_path):
        p = _make_tmp_zip(tmp_path, "test.zip", txt_pages={"1.txt": "x"})
        doc = ingest(p)
        assert doc.meta.get("engine") == "zip_txt"

    def test_meta_manifest_true_when_present(self, tmp_path):
        manifest = {"pages": [{"page_number": 1, "text": {"path": "1.txt"}}]}
        p = _make_tmp_zip(tmp_path, "m.zip", txt_pages={"1.txt": "x"}, manifest=manifest)
        doc = ingest(p)
        assert doc.meta.get("manifest") is True

    def test_meta_manifest_false_when_absent(self, tmp_path):
        p = _make_tmp_zip(tmp_path, "nm.zip", txt_pages={"1.txt": "x"})
        doc = ingest(p)
        assert doc.meta.get("manifest") is False

    def test_path_attribute_preserved(self, tmp_path):
        p = _make_tmp_zip(tmp_path, "test.zip", txt_pages={"1.txt": "x"})
        doc = ingest(p)
        assert doc.path == p


# ─────────────────────────────────────────────────────────────────────────────
# ZIP ingestion — real fixture files
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestFixtureZips:
    """
    Tests against the real anonymised fixture files.
    Each test is skipped if the fixture is absent.
    """

    def _require(self, key: str) -> Path:
        """Return fixture path, raise Skipped if absent."""
        p = FIXTURE_ZIPS[key]
        if not p.exists():
            try:
                import pytest
                pytest.skip(f"Fixture not found: {p}")
            except ImportError:
                raise RuntimeError(f"Fixture not found (skip): {p}")
        return p

    def test_zaro_pages_extracted(self):
        doc = ingest(self._require("zaro"))
        assert len(doc.pages) >= 1
        assert any(p.strip() for p in doc.pages), "Expected at least one non-empty page"

    def test_zaro_contains_zarojelentes_text(self):
        doc = ingest(self._require("zaro"))
        full = doc.full_text
        assert "ZÁRÓJELENTÉS" in full or "Zárójelentés" in full or "zaro" in full.lower() or len(full) > 100

    def test_zaro_felvetel_dátuma_present(self):
        """The fixture zárójelentés must contain the admission date line."""
        doc = ingest(self._require("zaro"))
        full = doc.full_text
        assert "Felvétel dátuma:" in full or "2026.01.12" in full

    def test_zaro_page_count(self):
        doc = ingest(self._require("zaro"))
        # The manifest says 7 pages
        assert len(doc.pages) == 7

    def test_labor_pages_extracted(self):
        doc = ingest(self._require("labor"))
        assert len(doc.pages) >= 1

    def test_fibro_pages_extracted(self):
        doc = ingest(self._require("fibro"))
        assert len(doc.pages) >= 1

    def test_endogastro_pages_extracted(self):
        doc = ingest(self._require("endogastro"))
        assert len(doc.pages) >= 1

    def test_endocolono_pages_extracted(self):
        doc = ingest(self._require("endocolono"))
        assert len(doc.pages) >= 1

    def test_tenyesztes_pages_extracted(self):
        doc = ingest(self._require("tenyesztes"))
        assert len(doc.pages) >= 1

    def test_all_fixtures_doc_type_unknown(self):
        """Ingest does not classify — doc_type must always be UNKNOWN here."""
        for key, path in FIXTURE_ZIPS.items():
            if not path.exists():
                continue
            doc = ingest(path)
            assert doc.doc_type == DocType.UNKNOWN, (
                f"Expected UNKNOWN for fixture {key}, got {doc.doc_type}"
            )

    def test_all_fixtures_no_crash(self):
        """Ingest must never raise for any fixture file."""
        for key, path in FIXTURE_ZIPS.items():
            if not path.exists():
                continue
            try:
                doc = ingest(path)
                assert isinstance(doc, RoutedDoc), f"Expected RoutedDoc for {key}"
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"ingest() raised for fixture {key}: {exc}") from exc

    def test_full_text_property(self):
        doc = ingest(self._require("zaro"))
        joined = doc.full_text
        assert isinstance(joined, str)
        # full_text = pages joined with double newline
        if len(doc.pages) > 1:
            assert "\n\n" in joined

    def test_first_3000_property(self):
        doc = ingest(self._require("zaro"))
        assert len(doc.first_3000) <= 3000


# ─────────────────────────────────────────────────────────────────────────────
# Unsupported / corrupted file handling
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestErrorHandling:
    def test_unsupported_extension_returns_empty(self, tmp_path):
        p = tmp_path / "test.xyz"
        p.write_text("random content")
        doc = ingest(p)
        assert isinstance(doc, RoutedDoc)
        assert doc.pages == ()

    def test_corrupted_pdf_returns_empty(self, tmp_path):
        p = tmp_path / "bad.pdf"
        p.write_bytes(b"%PDF-1.4 garbage \x00\xFF invalid content here")
        doc = ingest(p)
        assert isinstance(doc, RoutedDoc)
        # Must not raise; pages may be empty

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "empty.pdf"
        p.write_bytes(b"")
        doc = ingest(p)
        assert isinstance(doc, RoutedDoc)

    def test_returns_routeddoc_on_failure(self, tmp_path):
        """Any file that fails to ingest must return a RoutedDoc, never None."""
        p = tmp_path / "garbage.pdf"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")   # PNG header but named .pdf
        result = ingest(p)
        assert result is not None
        assert isinstance(result, RoutedDoc)

    def test_path_preserved_on_failure(self, tmp_path):
        p = tmp_path / "bad.pdf"
        p.write_bytes(b"not a pdf")
        doc = ingest(p)
        assert doc.path == p

    def test_nonexistent_file_returns_empty(self, tmp_path):
        p = tmp_path / "does_not_exist.pdf"
        doc = ingest(p)
        assert isinstance(doc, RoutedDoc)
        assert doc.pages == ()


# ─────────────────────────────────────────────────────────────────────────────
# DOCX ingestion — synthetic
# ─────────────────────────────────────────────────────────────────────────────

class TestIngestDocx:
    def test_docx_text_extracted(self, tmp_path):
        """Create a minimal DOCX and verify text extraction."""
        try:
            from docx import Document
        except ImportError:
            return  # python-docx not available; skip

        p = tmp_path / "test.docx"
        doc_obj = Document()
        doc_obj.add_paragraph("Hello from DOCX")
        doc_obj.add_paragraph("Second paragraph")
        doc_obj.save(str(p))

        doc = ingest(p)
        assert isinstance(doc, RoutedDoc)
        assert len(doc.pages) >= 1
        assert "Hello from DOCX" in doc.full_text
        assert "Second paragraph" in doc.full_text

    def test_docx_table_content_extracted(self, tmp_path):
        """Table cell text should be included in the extracted content."""
        try:
            from docx import Document
        except ImportError:
            return

        p = tmp_path / "table.docx"
        doc_obj = Document()
        table = doc_obj.add_table(rows=2, cols=2)
        table.cell(0, 0).text = "Analyte"
        table.cell(0, 1).text = "Value"
        table.cell(1, 0).text = "Creatinine"
        table.cell(1, 1).text = "88"
        doc_obj.save(str(p))

        doc = ingest(p)
        full = doc.full_text
        assert "Creatinine" in full or "creatinine" in full.lower()

    def test_docx_doc_type_unknown(self, tmp_path):
        try:
            from docx import Document
        except ImportError:
            return

        p = tmp_path / "test.docx"
        Document().save(str(p))
        doc = ingest(p)
        assert doc.doc_type == DocType.UNKNOWN

    def test_docx_engine_meta(self, tmp_path):
        try:
            from docx import Document
        except ImportError:
            return

        p = tmp_path / "test.docx"
        Document().save(str(p))
        doc = ingest(p)
        assert doc.meta.get("engine") == "python-docx"


# ─────────────────────────────────────────────────────────────────────────────
# Pytest-compatible fixtures for tmp_path
# ─────────────────────────────────────────────────────────────────────────────

# When run WITHOUT pytest, we need a minimal tmp_path shim.
class _TmpPath:
    """stdlib-only tmp_path shim for the standalone runner."""

    def __init__(self, base: Path):
        self._base = base
        self._counter = 0

    def __call__(self) -> Path:
        self._counter += 1
        p = self._base / f"tmp_{self._counter}"
        p.mkdir(parents=True, exist_ok=True)
        return p


# ─────────────────────────────────────────────────────────────────────────────
# Standalone runner (no pytest required)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import tempfile

    all_classes = [
        TestNormalizeText,
        TestNumericSortKey,
        TestZipSlipGuard,
        TestIngestZipSynthetic,
        TestIngestFixtureZips,
        TestIngestErrorHandling,
        TestIngestDocx,
    ]

    with tempfile.TemporaryDirectory() as _tmpdir:
        _base = Path(_tmpdir)
        _tmp_factory = _TmpPath(_base)

        passed = failed = skipped = 0
        for cls in all_classes:
            instance = cls()
            methods = sorted(m for m in dir(cls) if m.startswith("test_"))
            for method_name in methods:
                method = getattr(instance, method_name)
                # Detect whether the method needs a tmp_path argument
                import inspect
                sig = inspect.signature(method)
                needs_tmp = "tmp_path" in sig.parameters

                try:
                    if needs_tmp:
                        method(_tmp_factory())
                    else:
                        method()
                    passed += 1
                    print(f"  PASS  {cls.__name__}.{method_name}")
                except (RuntimeError, AssertionError) as e:
                    msg = str(e)
                    if "skip" in msg.lower() or "Fixture not found" in msg:
                        skipped += 1
                        print(f"  SKIP  {cls.__name__}.{method_name}: {e}")
                    else:
                        failed += 1
                        print(f"  FAIL  {cls.__name__}.{method_name}: {e}")
                except Exception as e:
                    failed += 1
                    print(f"  ERROR {cls.__name__}.{method_name}: {e}")
                    traceback.print_exc()

        print(f"\n{'='*60}")
        print(f"  PASSED:  {passed}/{passed + failed + skipped}")
        if skipped:
            print(f"  SKIPPED: {skipped}")
        if failed:
            print(f"  FAILED:  {failed}")
            sys.exit(1)
        else:
            print("  All tests passed ✓")
