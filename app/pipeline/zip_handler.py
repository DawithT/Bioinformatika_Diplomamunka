"""
app/pipeline/zip_handler.py

ZIP archive utilities for the upload layer.

Responsibilities:
  - Detect whether a ZIP archive is password-protected (encrypted).
  - Detect whether a ZIP is a "document bundle" (contains .pdf/.docx)
    vs. a "page archive" (only .txt files used by step1_ingest).
  - Extract document-bundle ZIPs (optionally with password) into individual
    files that the ingest pipeline can process normally.

Security:
  - ZIP-slip protection: member names are flattened (basename only).
  - Passwords are accepted as strings and used only during extraction;
    they are never logged, stored, or returned.
  - Uses ``pyzipper`` for AES-256 + ZipCrypto support.  Falls back to
    stdlib ``zipfile`` (ZipCrypto only) if pyzipper is not installed.

Why pyzipper:
  Python's stdlib ``zipfile`` only supports the legacy ZipCrypto scheme.
  Most modern ZIP tools (WinZip, 7-Zip) default to AES-256 encryption.
  ``pyzipper`` is a minimal drop-in replacement that handles both.
"""
from __future__ import annotations

import logging
import zipfile
from pathlib import Path

log = logging.getLogger(__name__)

# Extensions that signal a "document bundle" ZIP (vs. a page-archive of .txt)
_DOCUMENT_EXTENSIONS = frozenset({".pdf", ".docx", ".doc"})

# Extensions we extract from a document bundle
_EXTRACTABLE_EXTENSIONS = frozenset({".pdf", ".docx", ".doc", ".zip"})


# ── Detection ────────────────────────────────────────────────────────────────


def is_encrypted_zip(path: Path) -> bool:
    """
    Return True if *path* is a ZIP archive containing at least one encrypted
    entry.  Works for both ZipCrypto and AES-encrypted archives (the general
    purpose bit flag is set for both schemes).

    Returns False for non-ZIP files, corrupt ZIPs, or unencrypted ZIPs.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            return any(info.flag_bits & 0x1 for info in zf.infolist())
    except (zipfile.BadZipFile, OSError):
        return False


def is_document_bundle(path: Path) -> bool:
    """
    Return True if the ZIP contains document files (.pdf, .docx, .doc).

    A ZIP that contains only .txt files (and optionally manifest.json) is
    a "page archive" handled by step1_ingest._ingest_zip().  Those must NOT
    be extracted at upload time.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                suffix = Path(info.filename).suffix.lower()
                if suffix in _DOCUMENT_EXTENSIONS:
                    return True
    except (zipfile.BadZipFile, OSError):
        pass
    return False


# ── Extraction ───────────────────────────────────────────────────────────────


def extract_zip(
    zip_path: Path,
    dest_dir: Path,
    password: str | None = None,
) -> list[Path]:
    """
    Extract supported document files from a ZIP archive.

    Parameters
    ----------
    zip_path : Path
        Path to the ZIP archive.
    dest_dir : Path
        Directory to write extracted files into.
    password : str | None
        Archive password (never logged or stored).

    Returns
    -------
    list[Path]
        Paths of successfully extracted files.

    Raises
    ------
    RuntimeError
        Wrong password, corrupt archive, or unsupported encryption.
    """
    pwd_bytes = password.encode("utf-8") if password else None

    # Prefer pyzipper (AES-256 + ZipCrypto); fall back to stdlib.
    try:
        import pyzipper  # type: ignore[import-not-found]  # noqa: PLC0415  # pylint: disable=import-error  # pyright: ignore[reportMissingImports]
        zf_cls = pyzipper.AESZipFile
    except ImportError:
        zf_cls = zipfile.ZipFile

    extracted: list[Path] = []
    try:
        with zf_cls(zip_path, "r") as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue

                # ZIP-slip protection: flatten to basename, reject traversal
                if ".." in info.filename:
                    continue
                name = Path(info.filename).name
                if not name:
                    continue

                suffix = Path(name).suffix.lower()
                if suffix not in _EXTRACTABLE_EXTENSIONS:
                    continue

                data = zf.read(info.filename, pwd=pwd_bytes)

                # Handle name collisions with a counter suffix
                dest = dest_dir / name
                counter = 1
                while dest.exists():
                    dest = dest_dir / f"{Path(name).stem}_{counter}{suffix}"
                    counter += 1

                dest.write_bytes(data)
                extracted.append(dest)

    except NotImplementedError as exc:
        # NotImplementedError is a subclass of RuntimeError; it must be caught
        # *before* the broader RuntimeError handler below or it would be
        # unreachable.  Stdlib zipfile raises this for unsupported AES.
        raise RuntimeError(
            "Az archívum AES titkosítást használ. "
            "Telepítse a pyzipper csomagot: pip install pyzipper"
        ) from exc
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "password" in msg or "bad password" in msg:
            raise RuntimeError("Hibás jelszó.") from exc
        if "not supported" in msg or "not implemented" in msg:
            raise RuntimeError(
                "Az archívum AES titkosítást használ. "
                "Telepítse a pyzipper csomagot: pip install pyzipper"
            ) from exc
        raise RuntimeError(f"ZIP hiba: {exc}") from exc
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"Sérült ZIP fájl: {exc}") from exc
    except Exception as exc:
        raise RuntimeError(f"ZIP feldolgozási hiba: {exc}") from exc

    return extracted
