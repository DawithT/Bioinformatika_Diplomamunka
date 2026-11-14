"""
app/pipeline/preflight.py

Pre-run environment checks.

Before the extraction pipeline runs, ``check_environment()`` verifies that
all required and optional dependencies are available.  Each missing
dependency is reported with:

  - What it is
  - How to install it
  - What pipeline outputs will be missing without it

Severity levels
---------------
  CRITICAL  — pipeline cannot run at all (e.g. missing registry file)
  WARNING   — pipeline will run but some outputs will be degraded/missing
"""
from __future__ import annotations

import logging
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

Scope = Literal["web", "cli"]

log = logging.getLogger(__name__)


@dataclass
class PreflightIssue:
    """A single missing or misconfigured dependency."""
    component: str
    severity: str          # "critical" or "warning"
    message: str
    install_hint: str
    impact: str

    def to_dict(self) -> dict:
        return {
            "component": self.component,
            "severity": self.severity,
            "message": self.message,
            "install_hint": self.install_hint,
            "impact": self.impact,
        }


@dataclass
class PreflightResult:
    """Aggregated result of all environment checks."""
    issues: list[PreflightIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when there are no issues at all."""
        return len(self.issues) == 0

    @property
    def can_run(self) -> bool:
        """True when there are no critical issues (warnings are acceptable)."""
        return not any(i.severity == "critical" for i in self.issues)

    @property
    def warnings(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def critical(self) -> list[PreflightIssue]:
        return [i for i in self.issues if i.severity == "critical"]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "can_run": self.can_run,
            "issues": [i.to_dict() for i in self.issues],
        }


# ── Check functions ──────────────────────────────────────────────────────────

def _check_python_package(
    import_name: str,
    display_name: str,
    pip_name: str,
    impact: str,
) -> PreflightIssue | None:
    """Try to import a Python package; return an issue if it fails."""
    try:
        __import__(import_name)
        return None
    except ImportError:
        return PreflightIssue(
            component=display_name,
            severity="warning",
            message=f"Python package '{display_name}' is not installed.",
            install_hint=f"pip install {pip_name}",
            impact=impact,
        )


def _check_ghostscript() -> PreflightIssue | None:
    """Check that Ghostscript is available on the system PATH."""
    # On Windows: gswin64c or gswin32c; on Unix: gs
    if sys.platform == "win32":
        candidates = ["gswin64c", "gswin32c"]
    else:
        candidates = ["gs"]

    for name in candidates:
        if shutil.which(name):
            return None

    if sys.platform == "win32":
        hint = (
            "Download from https://www.ghostscript.com/releases/gsdnld.html\n"
            "The installer does NOT add Ghostscript to PATH automatically — "
            "manually add 'C:\\Program Files\\gs\\gs<version>\\bin' to your "
            "user or system PATH, then restart the terminal."
        )
    elif sys.platform == "darwin":
        hint = "brew install ghostscript"
    else:
        hint = "sudo apt install ghostscript   # or the equivalent for your distro"

    return PreflightIssue(
        component="Ghostscript",
        severity="warning",
        message="Ghostscript is not installed or not on PATH.",
        install_hint=hint,
        impact=(
            "PDF table extraction (Camelot) will not work. "
            "Lab values from standalone PDF lab reports will only be "
            "extracted via text parsing, not from grid tables."
        ),
    )


def _check_llm_server(use_llm: bool = True) -> PreflightIssue | None:
    """Check whether the configured LLM server is reachable."""
    from config import settings

    if not use_llm or not settings.enable_llm or settings.llm_provider == "disabled":
        return None  # LLM is intentionally disabled — not an issue

    import http.client
    import urllib.parse

    from app.services.llm_client import ConfigurationError, check_llm_endpoint

    # Refuse non-local endpoints before contacting them at all.
    try:
        check_llm_endpoint(
            settings.llm_base_url, settings.llm_model, settings.llm_allowed_hosts,
        )
    except ConfigurationError as exc:
        return PreflightIssue(
            component="LLM endpoint",
            severity="critical",
            message=str(exc),
            install_hint=(
                "Point LLM_BASE_URL at a local or institutional server and use a "
                "locally pulled model. Institutional servers on public IP ranges "
                "can be listed in LLM_ALLOWED_HOSTS."
            ),
            impact="Patient text would leave the institution — the run is blocked.",
        )

    parsed = urllib.parse.urlparse(settings.llm_base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(host, port, timeout=3)
        else:
            conn = http.client.HTTPConnection(host, port, timeout=3)
        conn.request("GET", "/")
        conn.getresponse()
        conn.close()
        return None
    except Exception:
        provider = settings.llm_provider
        base_url = settings.llm_base_url
        if provider == "ollama":
            hint = (
                f"Start Ollama: 'ollama serve'\n"
                f"Then pull the model: 'ollama pull {settings.llm_model}'\n"
                f"Expected at: {base_url}"
            )
        else:
            hint = (
                f"Start your llama.cpp server at {base_url}\n"
                f"Model: {settings.llm_model}"
            )
        return PreflightIssue(
            component=f"LLM server ({provider})",
            severity="warning",
            message=f"Cannot reach {provider} server at {base_url}.",
            install_hint=hint,
            impact=(
                "Soft LLM extraction will be skipped. Only hard-coded regex "
                "extraction will run. Some fields that require natural-language "
                "understanding will be missing from the output."
            ),
        )


def _check_redis() -> PreflightIssue | None:
    """
    Verify the Redis broker used by RQ is reachable.

    Web startup (``create_app``) already hard-fails when Redis is missing,
    but the preflight check duplicates that signal so operators running
    ``cli.py preflight`` or hitting the ``/preflight`` endpoint see the
    same information without having to trigger an upload.
    """
    from config import settings

    try:
        import redis  # imported here so preflight stays usable in stdlib-only contexts
    except ImportError:
        return PreflightIssue(
            component="redis-py",
            severity="warning",
            message="Python 'redis' package is not installed.",
            install_hint="pip install redis",
            impact=(
                "The web UI cannot enqueue jobs. The CLI pipeline still "
                "runs without Redis."
            ),
        )

    try:
        conn = redis.from_url(
            settings.redis_url, decode_responses=False, socket_connect_timeout=3,
        )
        conn.ping()
    except Exception as exc:
        if sys.platform == "win32":
            hint = (
                "Native Redis does not run on Windows — install one of:\n"
                "  Memurai (drop-in replacement):  https://www.memurai.com/get-memurai\n"
                "  WSL2:                           wsl --install, then 'sudo apt install redis-server'\n"
                "  Docker:                         docker run -d -p 6379:6379 --name redis redis:7\n"
                f"Configured URL: {settings.redis_url}"
            )
        else:
            hint = (
                "Start Redis with 'redis-server' "
                "(install via 'sudo apt install redis-server' or 'brew install redis').\n"
                f"Configured URL: {settings.redis_url}"
            )
        return PreflightIssue(
            component="Redis",
            severity="warning",
            message=f"Cannot reach Redis at {settings.redis_url!r}: {exc}",
            install_hint=hint,
            impact=(
                "The web UI cannot enqueue jobs — uploads will return 503. "
                "The CLI pipeline is unaffected."
            ),
        )
    return None


def _check_rq_worker() -> PreflightIssue | None:
    """
    Verify that at least one RQ worker is registered against the broker.

    Redis being reachable only proves the broker is up; jobs will still sit
    in QUEUED forever if no worker is listening on the ``default`` queue.
    """
    from config import settings

    try:
        import redis
        import rq  # noqa: F401 — used via app.services.workers
    except ImportError:
        return None  # covered by _check_redis

    try:
        conn = redis.from_url(
            settings.redis_url, decode_responses=False, socket_connect_timeout=3,
        )
        conn.ping()
    except Exception:
        return None  # Redis-down is already reported by _check_redis

    from app.services.workers import live_workers

    try:
        # A killed worker stays registered for up to an hour; skip those.
        workers = live_workers(conn)
    except Exception as exc:
        log.debug("preflight: could not enumerate RQ workers: %s", exc)
        return None

    listening = [w for w in workers if "default" in {q.name for q in w.queues}]
    if listening:
        return None

    if sys.platform == "win32":
        hint = (
            "Start the worker in a second terminal:\n"
            "  python worker.py\n"
            "On Windows the shipped entry point uses rq.SimpleWorker because "
            "rq.Worker relies on os.fork(), which does not exist on Windows. "
            "Do NOT start the worker with 'rq worker' — it bypasses this bootstrap."
        )
    else:
        hint = (
            "Start the worker in a second terminal:\n"
            "  python worker.py"
        )

    return PreflightIssue(
        component="RQ worker",
        severity="warning",
        message="Redis is reachable but no RQ worker is listening on the 'default' queue.",
        install_hint=hint,
        impact=(
            "Uploads will be accepted but jobs will stay in QUEUED forever. "
            "The CLI pipeline is unaffected."
        ),
    )


def _check_config_files() -> list[PreflightIssue]:
    """Check that required configuration files exist."""
    from config import settings

    issues: list[PreflightIssue] = []

    registry = settings.registry_path_obj
    if not registry.exists():
        issues.append(PreflightIssue(
            component="Registry_base.xlsx",
            severity="critical",
            message=f"Registry definition file not found: {registry}",
            install_hint=f"Place the Registry_base.xlsx file at: {registry.resolve()}",
            impact="The pipeline cannot run without the registry definition.",
        ))

    return issues


# ── Main entry point ─────────────────────────────────────────────────────────

def check_environment(scope: Scope = "web", use_llm: bool = True) -> PreflightResult:
    """
    Run all pre-flight checks and return the aggregated result.

    Parameters
    ----------
    scope : "web" | "cli"
        "web" (default) runs every check — the Flask app and RQ worker rely
        on Redis, so Redis reachability and at least one RQ worker are
        required.

        "cli" skips Redis and RQ-worker checks because ``cli.py`` runs the
        pipeline synchronously in-process and never touches the job queue.
        The redis-py package import check is skipped for the same reason.
    use_llm : bool
        False when the user turned the LLM off for this run — the LLM server
        is then not checked, since nothing will be sent to it.
    """
    result = PreflightResult()

    # ── Python packages ──────────────────────────────────────────────────────
    pkg_checks = [
        ("fitz", "PyMuPDF", "pymupdf", (
            "Primary PDF text extraction will not work. "
            "If pdfminer is available it will be used as fallback. "
            "ZIP archives with pre-extracted text pages are unaffected."
        )),
        ("pymupdf4llm", "pymupdf4llm", "pymupdf4llm", (
            "PDF markdown conversion will not work. "
            "pdfminer (plain text) will be used as fallback if available. "
            "ZIP archives with pre-extracted text pages are unaffected."
        )),
        ("pdfminer", "pdfminer.six", "pdfminer.six", (
            "PDF text fallback extraction will not work. "
            "If PyMuPDF is available it will be used as primary. "
            "ZIP archives with pre-extracted text pages are unaffected."
        )),
        ("camelot", "camelot-py", "camelot-py opencv-python-headless", (
            "PDF table extraction will not work. "
            "Lab values from standalone PDF lab reports will only be "
            "extracted via text parsing, not from grid tables."
        )),
        ("cv2", "OpenCV", "opencv-python-headless", (
            "Required by camelot-py for PDF table extraction. "
            "Table extraction from PDF lab reports will not work."
        )),
        ("docx", "python-docx", "python-docx", (
            "DOCX document ingestion will not work. "
            "Only PDF and ZIP files can be processed."
        )),
        ("pyzipper", "pyzipper", "pyzipper", (
            "Encrypted (AES-256) ZIP archives cannot be decrypted. "
            "Only unencrypted archives and standard ZipCrypto will work."
        )),
    ]

    for import_name, display_name, pip_name, impact in pkg_checks:
        issue = _check_python_package(import_name, display_name, pip_name, impact)
        if issue:
            result.issues.append(issue)

    # ── System tools ─────────────────────────────────────────────────────────
    gs = _check_ghostscript()
    if gs:
        result.issues.append(gs)

    # ── LLM server ───────────────────────────────────────────────────────────
    llm = _check_llm_server(use_llm)
    if llm:
        result.issues.append(llm)

    # ── Redis / RQ (web-UI only — CLI runs the pipeline in-process) ─────────
    if scope == "web":
        redis_issue = _check_redis()
        if redis_issue:
            result.issues.append(redis_issue)
        else:
            worker_issue = _check_rq_worker()
            if worker_issue:
                result.issues.append(worker_issue)

    # ── Config files ─────────────────────────────────────────────────────────
    result.issues.extend(_check_config_files())

    # Log summary
    if result.ok:
        log.info("preflight: all checks passed")
    else:
        for issue in result.issues:
            level = log.error if issue.severity == "critical" else log.warning
            level("preflight_%s: %s — %s", issue.severity, issue.component, issue.message)

    return result
