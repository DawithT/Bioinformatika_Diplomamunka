"""
app/services/code_version.py

Fingerprint of the code and settings a process started with.

An RQ worker keeps running the code it imported when it started.  After an
edit, a ``git pull`` or a branch switch it would quietly process cases with
the old code while the web app (reloaded by Flask) already runs the new one,
and a worker left open in another terminal keeps taking jobs from the same
queue.

``STARTUP`` is the fingerprint taken when this module is first imported;
the worker imports it before anything else.  ``current()`` hashes the files
on disk now.  They differ exactly when the process runs code (or ``.env``
settings) older than the files on disk.

- The worker puts ``STARTUP`` in its RQ name (``worker_name()``), and
  ``process_case()`` refuses a job when ``is_stale()``.
- The upload page warns about a live worker whose fingerprint differs from
  the files on disk (``app.services.workers.worker_problems``).

The field registry is not part of it: every job reloads Registry_base.xlsx.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_NAME_PREFIX = "crreg"


def _files() -> list[Path]:
    files = sorted((_ROOT / "app").rglob("*.py"))
    for name in ("config.py", "worker.py", ".env"):
        path = _ROOT / name
        if path.is_file():
            files.append(path)
    return files


def current() -> str:
    """Return the fingerprint of the code and settings on disk now."""
    h = hashlib.sha256()
    for path in _files():
        h.update(path.relative_to(_ROOT).as_posix().encode())
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()[:12]


STARTUP = current()


def is_stale() -> bool:
    """True when the files on disk changed since this process started."""
    return current() != STARTUP


def worker_name() -> str:
    """RQ worker name carrying the startup fingerprint: crreg.<fp>.<random>."""
    return f"{_NAME_PREFIX}.{STARTUP}.{uuid.uuid4().hex[:8]}"


def fingerprint_of(name: str) -> str | None:
    """The fingerprint in a ``worker_name()``; None for any other name."""
    parts = name.split(".")
    if len(parts) == 3 and parts[0] == _NAME_PREFIX:
        return parts[1]
    return None
