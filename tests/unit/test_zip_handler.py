"""
tests/unit/test_zip_handler.py

Tests for ZIP archive handling (protection detection, extraction).

Covers:
  - Encrypted ZIP detection (AES + ZipCrypto)
  - Document-bundle vs page-archive classification
  - Extraction with correct/wrong/missing password
  - Corrupt / empty / non-ZIP files
  - ZIP-slip protection
  - Name collision handling
  - Unsupported file filtering inside ZIPs
  - End-to-end flow simulations

Run:
    PYTHONPATH=. python3 tests/unit/test_zip_handler.py
"""
from __future__ import annotations

import io
import inspect
import os
import sys
import traceback
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

from app.pipeline.zip_handler import is_encrypted_zip, is_document_bundle, extract_zip

# pyzipper is needed to CREATE encrypted test ZIPs.
# Detection tests use stdlib and always run.
try:
    import pyzipper
    HAS_PYZIPPER = True
except ImportError:
    HAS_PYZIPPER = False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_encrypted_zip(path: Path, members: dict[str, bytes], password: str) -> None:
    """Create an AES-256 encrypted ZIP using pyzipper."""
    if not HAS_PYZIPPER:
        raise RuntimeError("pyzipper required to create encrypted ZIPs (skip)")
    with pyzipper.AESZipFile(
        path, "w",
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES,
    ) as zf:
        zf.setpassword(password.encode("utf-8"))
        for name, data in members.items():
            zf.writestr(name, data)


def _make_plain_zip(path: Path, members: dict[str, str | bytes]) -> None:
    """Create an unencrypted ZIP."""
    with zipfile.ZipFile(path, "w") as zf:
        for name, content in members.items():
            if isinstance(content, str):
                zf.writestr(name, content.encode("utf-8"))
            else:
                zf.writestr(name, content)


# ── is_encrypted_zip ─────────────────────────────────────────────────────────

class TestIsEncryptedZip:
    def test_unencrypted_zip_returns_false(self, tmp_path):
        p = tmp_path / "plain.zip"
        _make_plain_zip(p, {"test.txt": "hello"})
        assert is_encrypted_zip(p) is False

    def test_encrypted_zip_returns_true(self, tmp_path):
        if not HAS_PYZIPPER:
            return  # skip — needs pyzipper to create test ZIP
        p = tmp_path / "encrypted.zip"
        _make_encrypted_zip(p, {"doc.pdf": b"%PDF-1.4 test"}, password="secret")
        assert is_encrypted_zip(p) is True

    def test_not_a_zip_returns_false(self, tmp_path):
        p = tmp_path / "notazip.zip"
        p.write_bytes(b"this is not a zip file")
        assert is_encrypted_zip(p) is False

    def test_nonexistent_file_returns_false(self, tmp_path):
        p = tmp_path / "does_not_exist.zip"
        assert is_encrypted_zip(p) is False

    def test_empty_file_returns_false(self, tmp_path):
        p = tmp_path / "empty.zip"
        p.write_bytes(b"")
        assert is_encrypted_zip(p) is False

    def test_corrupt_zip_returns_false(self, tmp_path):
        p = tmp_path / "corrupt.zip"
        p.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        assert is_encrypted_zip(p) is False

    def test_empty_zip_returns_false(self, tmp_path):
        """A valid ZIP with zero entries."""
        p = tmp_path / "empty_valid.zip"
        with zipfile.ZipFile(p, "w"):
            pass
        assert is_encrypted_zip(p) is False


# ── is_document_bundle ───────────────────────────────────────────────────────

class TestIsDocumentBundle:
    def test_zip_with_pdfs(self, tmp_path):
        p = tmp_path / "bundle.zip"
        _make_plain_zip(p, {"report.pdf": b"%PDF", "notes.txt": "some notes"})
        assert is_document_bundle(p) is True

    def test_zip_with_docx(self, tmp_path):
        p = tmp_path / "bundle.zip"
        _make_plain_zip(p, {"letter.docx": b"PK docx content"})
        assert is_document_bundle(p) is True

    def test_zip_with_only_txt_is_page_archive(self, tmp_path):
        """Page-archive ZIP should NOT be classified as a document bundle."""
        p = tmp_path / "pages.zip"
        _make_plain_zip(p, {"1.txt": "page one", "2.txt": "page two"})
        assert is_document_bundle(p) is False

    def test_zip_with_txt_and_manifest_is_page_archive(self, tmp_path):
        p = tmp_path / "pages.zip"
        _make_plain_zip(p, {"1.txt": "page one", "manifest.json": "{}"})
        assert is_document_bundle(p) is False

    def test_corrupt_zip_returns_false(self, tmp_path):
        p = tmp_path / "bad.zip"
        p.write_bytes(b"not a zip")
        assert is_document_bundle(p) is False

    def test_empty_zip_returns_false(self, tmp_path):
        p = tmp_path / "empty.zip"
        with zipfile.ZipFile(p, "w"):
            pass
        assert is_document_bundle(p) is False

    def test_zip_with_doc_extension(self, tmp_path):
        p = tmp_path / "bundle.zip"
        _make_plain_zip(p, {"letter.doc": b"old word doc"})
        assert is_document_bundle(p) is True


# ── extract_zip ──────────────────────────────────────────────────────────────

class TestExtractZip:
    def test_extract_unencrypted_bundle(self, tmp_path):
        src = tmp_path / "bundle.zip"
        _make_plain_zip(src, {"report.pdf": b"%PDF content", "notes.docx": b"PK docx"})
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        names = {p.name for p in extracted}
        assert "report.pdf" in names
        assert "notes.docx" in names
        assert len(extracted) == 2
        # Verify files actually exist on disk
        for p in extracted:
            assert p.exists()

    def test_extract_encrypted_correct_password(self, tmp_path):
        if not HAS_PYZIPPER:
            return
        src = tmp_path / "encrypted.zip"
        _make_encrypted_zip(src, {"report.pdf": b"%PDF encrypted"}, password="test123")
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest, password="test123")
        assert len(extracted) == 1
        assert extracted[0].name == "report.pdf"
        assert b"%PDF encrypted" in extracted[0].read_bytes()

    def test_extract_encrypted_wrong_password(self, tmp_path):
        if not HAS_PYZIPPER:
            return
        src = tmp_path / "encrypted.zip"
        _make_encrypted_zip(src, {"report.pdf": b"%PDF"}, password="correct")
        dest = tmp_path / "out"
        dest.mkdir()

        try:
            extract_zip(src, dest, password="wrong")
            assert False, "Expected RuntimeError for wrong password"
        except RuntimeError as exc:
            # Should mention password in Hungarian
            assert "jelszó" in str(exc).lower() or "password" in str(exc).lower()

    def test_extract_encrypted_missing_password(self, tmp_path):
        if not HAS_PYZIPPER:
            return
        src = tmp_path / "encrypted.zip"
        _make_encrypted_zip(src, {"report.pdf": b"%PDF"}, password="secret")
        dest = tmp_path / "out"
        dest.mkdir()

        try:
            extract_zip(src, dest)  # no password
            assert False, "Expected RuntimeError for missing password"
        except RuntimeError:
            pass

    def test_skips_unsupported_extensions(self, tmp_path):
        src = tmp_path / "mixed.zip"
        _make_plain_zip(src, {
            "report.pdf": b"%PDF",
            "image.png": b"\x89PNG",
            "script.py": b"print('hello')",
            "data.csv": b"a,b,c",
        })
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        names = {p.name for p in extracted}
        assert "report.pdf" in names
        assert "image.png" not in names
        assert "script.py" not in names
        assert "data.csv" not in names
        assert len(extracted) == 1

    def test_handles_name_collisions(self, tmp_path):
        src = tmp_path / "bundle.zip"
        _make_plain_zip(src, {"report.pdf": b"%PDF from zip"})
        dest = tmp_path / "out"
        dest.mkdir()
        (dest / "report.pdf").write_bytes(b"existing file")

        extracted = extract_zip(src, dest)
        assert len(extracted) == 1
        assert extracted[0].name == "report_1.pdf"
        # Original file untouched
        assert (dest / "report.pdf").read_bytes() == b"existing file"

    def test_multiple_collisions(self, tmp_path):
        """Counter increments past existing _1, _2 suffixes."""
        src = tmp_path / "bundle.zip"
        _make_plain_zip(src, {"report.pdf": b"%PDF from zip"})
        dest = tmp_path / "out"
        dest.mkdir()
        (dest / "report.pdf").write_bytes(b"original")
        (dest / "report_1.pdf").write_bytes(b"first dupe")

        extracted = extract_zip(src, dest)
        assert extracted[0].name == "report_2.pdf"

    def test_corrupt_zip_raises_runtime_error(self, tmp_path):
        src = tmp_path / "bad.zip"
        src.write_bytes(b"not a zip file at all")
        dest = tmp_path / "out"
        dest.mkdir()

        try:
            extract_zip(src, dest)
            assert False, "Expected RuntimeError"
        except RuntimeError:
            pass

    def test_zipslip_dotdot_skipped(self, tmp_path):
        """Members with .. path traversal are silently skipped."""
        src = tmp_path / "evil.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("safe.pdf", b"%PDF safe content")
            zf.writestr("../../../etc/passwd.pdf", b"evil content")
        src.write_bytes(buf.getvalue())
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        names = {p.name for p in extracted}
        assert "safe.pdf" in names
        assert len(extracted) == 1

    def test_nested_zip_extracted_not_recursed(self, tmp_path):
        """A .zip inside a .zip is extracted as a file, not recursively opened."""
        inner_buf = io.BytesIO()
        with zipfile.ZipFile(inner_buf, "w") as inner:
            inner.writestr("page.txt", b"inner content")

        src = tmp_path / "outer.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("report.pdf", b"%PDF content")
            zf.writestr("inner.zip", inner_buf.getvalue())

        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        names = {p.name for p in extracted}
        assert "report.pdf" in names
        assert "inner.zip" in names
        assert len(extracted) == 2

    def test_flattens_subdirectories(self, tmp_path):
        """Members nested in subdirectories are extracted using only the basename."""
        src = tmp_path / "nested.zip"
        with zipfile.ZipFile(src, "w") as zf:
            zf.writestr("subdir/report.pdf", b"%PDF nested")
            zf.writestr("another/deep/path/lab.docx", b"PK docx")

        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        names = {p.name for p in extracted}
        assert "report.pdf" in names
        assert "lab.docx" in names
        # Files should be directly in dest, not in subdirs
        for p in extracted:
            assert p.parent == dest

    def test_empty_zip_returns_empty_list(self, tmp_path):
        src = tmp_path / "empty.zip"
        with zipfile.ZipFile(src, "w"):
            pass
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        assert extracted == []

    def test_zip_with_only_unsupported_returns_empty_list(self, tmp_path):
        src = tmp_path / "images.zip"
        _make_plain_zip(src, {"photo.jpg": b"\xff\xd8\xff", "readme.txt": b"notes"})
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest)
        assert extracted == []

    def test_extract_encrypted_multiple_files(self, tmp_path):
        """Multiple documents inside a single encrypted ZIP."""
        if not HAS_PYZIPPER:
            return
        src = tmp_path / "multi.zip"
        _make_encrypted_zip(src, {
            "discharge.pdf": b"%PDF discharge summary",
            "lab_report.pdf": b"%PDF lab values",
            "radiology.docx": b"PK radiology report",
        }, password="hospit2026")
        dest = tmp_path / "out"
        dest.mkdir()

        extracted = extract_zip(src, dest, password="hospit2026")
        assert len(extracted) == 3
        names = {p.name for p in extracted}
        assert "discharge.pdf" in names
        assert "lab_report.pdf" in names
        assert "radiology.docx" in names


# ── End-to-end flow simulations ──────────────────────────────────────────────

class TestFlowSimulations:
    def test_separate_pdfs_no_zip(self, tmp_path):
        """Separate PDF uploads — ZIP handler not involved."""
        pdf1 = tmp_path / "report.pdf"
        pdf2 = tmp_path / "lab.pdf"
        pdf1.write_bytes(b"%PDF-1.4 report")
        pdf2.write_bytes(b"%PDF-1.4 lab")
        # Neither is a ZIP
        assert not is_encrypted_zip(pdf1)
        assert not is_encrypted_zip(pdf2)

    def test_unprotected_document_bundle_flow(self, tmp_path):
        """Unprotected bundle: detect, extract, continue pipeline."""
        src = tmp_path / "bundle.zip"
        _make_plain_zip(src, {
            "discharge.pdf": b"%PDF discharge",
            "lab.pdf": b"%PDF lab",
        })

        assert not is_encrypted_zip(src)
        assert is_document_bundle(src)

        case_dir = tmp_path / "case"
        case_dir.mkdir()
        extracted = extract_zip(src, case_dir)
        assert len(extracted) == 2

    def test_protected_bundle_flow(self, tmp_path):
        """Protected bundle: detect encryption, then unlock with password."""
        if not HAS_PYZIPPER:
            return
        src = tmp_path / "protected.zip"
        _make_encrypted_zip(src, {
            "discharge.pdf": b"%PDF discharge content",
            "lab.pdf": b"%PDF lab content",
        }, password="patient2026")

        # Step 1: detect encryption
        assert is_encrypted_zip(src)

        # Step 2: wrong password
        case_dir = tmp_path / "case"
        case_dir.mkdir()
        try:
            extract_zip(src, case_dir, password="wrong")
            assert False, "Should have raised"
        except RuntimeError:
            pass

        # Step 3: correct password
        extracted = extract_zip(src, case_dir, password="patient2026")
        assert len(extracted) == 2
        names = {p.name for p in extracted}
        assert "discharge.pdf" in names
        assert "lab.pdf" in names

    def test_page_archive_not_extracted_as_bundle(self, tmp_path):
        """Page-archive ZIPs must NOT be classified as document bundles."""
        src = tmp_path / "pages.zip"
        _make_plain_zip(src, {
            "1.txt": "Zárójelentés page 1",
            "2.txt": "Zárójelentés page 2",
            "manifest.json": '{"pages": []}',
        })

        assert not is_encrypted_zip(src)
        assert not is_document_bundle(src)

    def test_mixed_upload_with_encrypted_zip(self, tmp_path):
        """Simulates uploading a PDF + encrypted ZIP together."""
        if not HAS_PYZIPPER:
            return
        case_dir = tmp_path / "case"
        case_dir.mkdir()

        # Separate PDF
        (case_dir / "standalone.pdf").write_bytes(b"%PDF standalone")

        # Encrypted ZIP
        enc_zip = case_dir / "bundle.zip"
        _make_encrypted_zip(enc_zip, {"inner.pdf": b"%PDF inner"}, password="pw")

        # Detection phase
        zips = [p for p in case_dir.iterdir() if p.suffix == ".zip"]
        assert len(zips) == 1
        assert is_encrypted_zip(zips[0])

        # Unlock phase
        extract_zip(enc_zip, case_dir, password="pw")
        enc_zip.unlink()

        # Case dir now has: standalone.pdf + inner.pdf
        remaining = {p.name for p in case_dir.iterdir() if p.is_file()}
        assert "standalone.pdf" in remaining
        assert "inner.pdf" in remaining

    def test_retry_after_wrong_password(self, tmp_path):
        """User can retry with the correct password after a wrong attempt."""
        if not HAS_PYZIPPER:
            return
        src = tmp_path / "enc.zip"
        _make_encrypted_zip(src, {"doc.pdf": b"%PDF"}, password="right")
        dest = tmp_path / "out"
        dest.mkdir()

        # First attempt: wrong password
        try:
            extract_zip(src, dest, password="wrong1")
            assert False
        except RuntimeError:
            pass

        # Second attempt: still wrong
        try:
            extract_zip(src, dest, password="wrong2")
            assert False
        except RuntimeError:
            pass

        # Third attempt: correct
        extracted = extract_zip(src, dest, password="right")
        assert len(extracted) == 1
        assert extracted[0].name == "doc.pdf"


# ── Standalone runner ────────────────────────────────────────────────────────

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


if __name__ == "__main__":
    import tempfile

    all_classes = [
        TestIsEncryptedZip,
        TestIsDocumentBundle,
        TestExtractZip,
        TestFlowSimulations,
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
                    if "skip" in msg.lower() or "pyzipper" in msg.lower():
                        skipped += 1
                        print(f"  SKIP  {cls.__name__}.{method_name}")
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
            print("  All tests passed")
