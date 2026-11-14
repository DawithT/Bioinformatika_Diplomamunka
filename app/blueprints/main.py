"""
app/blueprints/main.py

Web layer for CirrhosisRegistry.

Endpoints
---------
GET  /                      — serve the single-page UI (with the read-only
                              configuration panel)
GET  /health                — health check
POST /upload                — receive documents, create case, enqueue processing job
POST /upload/unlock         — supply a password for an encrypted ZIP, then enqueue
POST /browse-folder         — open a native folder picker on the server's desktop
GET  /status/<job_id>       — poll job state
GET  /download/<job_id>     — download completed result ZIP (not used by the UI)

Design notes
------------
- Job state is stored in data/jobs/<job_id>.json (file-based) so the
  /status endpoint works without direct Redis inspection.  The orchestrator
  writes progress updates to that file; RQ is used only for async execution.
- Allowed upload extensions: .pdf, .zip, .docx  (case-insensitive)
- Each upload creates a unique case_id (short hex) and job_id (full hex UUID).
  Files are saved to data/uploads/<case_id>/.
- ZIP handling at upload time:
    - Encrypted ZIPs: detected and deferred until the user provides a
      password via POST /upload/unlock.
    - Unencrypted document-bundle ZIPs (containing .pdf/.docx): extracted
      in-place so each document enters the pipeline individually.
    - Page-archive ZIPs (only .txt files): left untouched for
      step1_ingest._ingest_zip().
- ZIP passwords are never logged, stored in job metadata, or persisted
  beyond the single request that needs them.
- Result destination: the upload carries an optional "dest_dir" (absolute
  path) and "make_zip" flag.  Results land in <dest_dir>/<case_id>/ and the
  UI shows that path as its completion message, so no download step is
  needed.  An empty dest_dir falls back to settings.output_folder_path.
- LLM per run: the upload carries an optional "use_llm" flag (absent = on).
  It can only switch the LLM off for that run; whether the server allows the
  LLM at all is decided by .env (ENABLE_LLM / LLM_PROVIDER).
- CSV format per run: optional "csv_format" — "intl" (default; "," and ".")
  or "hu" (";" and "," decimals, for Excel with Hungarian regional settings).
"""
from __future__ import annotations

import logging
import uuid
from pathlib import Path

from flask import Blueprint, jsonify, render_template, request, send_file

log = logging.getLogger(__name__)

bp = Blueprint("main", __name__)

# Permitted upload extensions (lower-case, with leading dot)
_ALLOWED_EXT = {".pdf", ".zip", ".docx"}


# ── Utilities ─────────────────────────────────────────────────────────────────

def _allowed(filename: str) -> bool:
    """Return True if the file extension is permitted."""
    return Path(filename).suffix.lower() in _ALLOWED_EXT


def _uploaded_files() -> list:
    """Collect the uploaded file parts, whatever spelling the client used.

    Dropzone in ``uploadMultiple`` mode names the parts ``files[0]``,
    ``files[1]``, ... instead of repeating a plain ``files`` field, so accept
    both rather than silently seeing an empty upload.
    """
    collected = []
    for key, parts in request.files.lists():
        if key == "files" or key.startswith("files["):
            collected.extend(parts)
    return collected


def _upload_dir() -> Path:
    from config import settings
    return settings.upload_folder_path


def _output_dir() -> Path:
    from config import settings
    return settings.output_folder_path


def _is_writable(path: Path) -> bool:
    """Probe with a real file — os.access() is unreliable for directories on Windows."""
    import tempfile

    try:
        with tempfile.TemporaryFile(dir=path):
            return True
    except OSError:
        return False


def _resolve_destination(raw: str) -> Path:
    """
    Validate the destination folder the user chose for the result files.

    Returns the resolved path, or the configured default when *raw* is empty.
    Raises ``ValueError`` carrying a message meant for the user.

    The path is deliberately not restricted to a sandbox: choosing where the
    results land is the entire point of the field, and the server is a
    single-user localhost app (run.py binds 127.0.0.1).  Under Docker it must
    be a path *inside* the container — ./data is bind-mounted to /app/data.
    """
    cleaned = (raw or "").strip().strip('"')
    if not cleaned:
        return _output_dir()

    path = Path(cleaned)
    if not path.is_absolute():
        raise ValueError(
            "A mentési hely legyen teljes útvonal "
            "(pl. C:\\Kutatas\\Eredmenyek vagy /home/user/eredmenyek)."
        )

    path = path.resolve()
    if not path.exists():
        raise ValueError(f"A megadott mappa nem létezik: {path}")
    if not path.is_dir():
        raise ValueError(f"A megadott útvonal nem mappa: {path}")
    if not _is_writable(path):
        raise ValueError(f"A megadott mappa nem írható: {path}")
    return path


def _wants_zip(value) -> bool:
    """Interpret the make_zip flag from form data or JSON."""
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _wants_llm(value) -> bool:
    """Interpret the use_llm flag; absent means use the LLM if configured."""
    return True if value is None else _wants_zip(value)


def _csv_format(value) -> str:
    """Validate the csv_format field; absent means the default "intl"."""
    from app.pipeline.step8_export import CSV_FORMATS

    fmt = str(value or "intl").strip().lower()
    if fmt not in CSV_FORMATS:
        raise ValueError(f"Ismeretlen CSV formátum: {value}")
    return fmt


class EnqueueError(RuntimeError):
    """Raised when the RQ queue rejects or cannot receive a job."""


def _enqueue(
    job_id: str,
    case_id: str,
    case_dir: Path,
    out_dir: Path,
    make_zip: bool,
    use_llm: bool = True,
    csv_format: str = "intl",
) -> None:
    """
    Enqueue the RQ processing job. Raises ``EnqueueError`` on failure so
    the caller can clean up staged files and return a truthful HTTP status
    instead of a fake-success ``200 {job_id}``.
    """
    from app.extensions import get_queue
    from app.pipeline.orchestrator import process_case, write_queued_state

    try:
        queue = get_queue()
        write_queued_state(job_id, case_id)
        queue.enqueue(
            process_case,
            job_id,
            str(case_dir),
            str(out_dir),
            make_zip,
            use_llm,
            csv_format,
            job_id=job_id,
            job_timeout=3600,
        )
        log.info("job_enqueued", extra={"job_id": job_id, "case_id": case_id})
    except Exception as exc:
        log.error("enqueue_failed", extra={"job_id": job_id, "error": str(exc)})
        raise EnqueueError(str(exc)) from exc


def _cleanup_case_dir(case_dir: Path) -> None:
    """Remove a staged case directory after a failed enqueue. Best-effort."""
    import shutil

    try:
        if case_dir.exists():
            shutil.rmtree(case_dir)
    except OSError as exc:
        log.warning("cleanup_failed: dir=%s error=%s", case_dir, exc)


def _expand_document_bundles(case_dir: Path, files: list[Path]) -> None:
    """
    Extract unprotected document-bundle ZIPs in *case_dir* and delete
    the original ZIPs after successful extraction.

    Page-archive ZIPs (containing only .txt files) are left untouched
    for step1_ingest._ingest_zip().
    """
    from app.pipeline.zip_handler import extract_zip, is_document_bundle

    for path in files:
        if path.suffix.lower() != ".zip" or not path.exists():
            continue
        if is_document_bundle(path):
            try:
                extract_zip(path, case_dir)
                path.unlink()
            except Exception as exc:
                log.warning(
                    "bundle_extract_failed: file=%s error=%s", path.name, exc,
                )


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.route("/")
def index():
    """Serve the single-page upload UI."""
    from app.extensions import get_redis
    from app.services.llm_client import llm_configured
    from app.services.run_config import describe_run
    from app.services.workers import worker_problems

    try:
        problems = worker_problems(get_redis())
    except RuntimeError:
        problems = []  # create_app(testing=True) has no Redis to ask

    return render_template(
        "index.html",
        default_dest=str(_output_dir().resolve()),
        run_config=describe_run(llm_configured()),
        worker_problems=problems,
    )


@bp.route("/health")
def health():
    """Kubernetes-style health probe."""
    return jsonify({"status": "ok"})


@bp.route("/preflight")
def preflight():
    """
    Run environment checks and return the results.

    Response 200
    ------------
    {
        "ok":      true|false,
        "can_run": true|false,
        "issues":  [{"component": "...", "severity": "...", ...}, ...]
    }

    The UI should call this before (or at) upload time and show any issues
    to the user so they can decide whether to proceed.
    """
    from app.pipeline.preflight import check_environment

    result = check_environment()
    return jsonify(result.to_dict()), 200


@bp.route("/upload", methods=["POST"])
def upload():
    """
    Accept one or more document files, enqueue a processing job.

    Request
    -------
    multipart/form-data  — field name: "files" (one or more files).
                           Dropzone's "files[0]", "files[1]", ... spelling is
                           accepted too.
                         — "dest_dir"  (optional) absolute destination folder
                           for the results; empty = configured default.
                         — "make_zip"  (optional) "true" to also write
                           <case_id>_results.zip beside the case folder.
                         — "use_llm"   (optional) "false" to run this case
                           without the LLM; absent = use it if configured.
                         — "csv_format" (optional) "intl" (default) or "hu".

    Response 200
    ------------
    Normal:            {"job_id": "<uuid_hex>", "case_id": "<short_hex>"}
    Password required: {"status": "password_required",
                        "case_id": "<short_hex>",
                        "protected_files": ["<name>", ...]}

    Response 400
    ------------
    {"error": "<reason>"}  — no files, wrong extension, etc.
    """
    uploaded = _uploaded_files()
    if not uploaded:
        return jsonify({"error": "No files provided."}), 400

    # Validate all extensions before saving anything
    bad = [f.filename for f in uploaded if f.filename and not _allowed(f.filename)]
    if bad:
        return jsonify({
            "error": f"Unsupported file type(s): {', '.join(bad)}. "
                     f"Allowed: .pdf, .zip, .docx"
        }), 400

    # Validate the destination before a single byte is written, so a bad path
    # cannot leave staged uploads behind.
    try:
        dest_dir = _resolve_destination(request.form.get("dest_dir", ""))
        csv_format = _csv_format(request.form.get("csv_format"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    make_zip = _wants_zip(request.form.get("make_zip"))
    use_llm = _wants_llm(request.form.get("use_llm"))

    # Generate case_id and save files
    case_id = uuid.uuid4().hex[:8]
    case_dir = (_upload_dir() / case_id).resolve()
    case_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []
    for fobj in uploaded:
        if fobj.filename:
            safe_name = Path(fobj.filename).name  # strip any path traversal
            dest = case_dir / safe_name
            fobj.save(str(dest))
            saved_files.append(dest)
            log.info("file_saved", extra={"case_id": case_id, "file": safe_name})

    # ── ZIP handling ─────────────────────────────────────────────────────────
    from app.pipeline.zip_handler import is_encrypted_zip

    protected = [
        p for p in saved_files
        if p.suffix.lower() == ".zip" and is_encrypted_zip(p)
    ]

    if protected:
        # Don't enqueue yet — the UI must collect the password first.
        return jsonify({
            "status": "password_required",
            "case_id": case_id,
            "protected_files": [p.name for p in protected],
        }), 200

    # Extract unprotected document-bundle ZIPs (page-archive ZIPs stay)
    _expand_document_bundles(case_dir, saved_files)

    # ── Pre-flight check ────────────────────────────────────────────────────
    from app.pipeline.preflight import check_environment

    pf = check_environment(use_llm=use_llm)
    if not pf.can_run:
        # Critical issue — cannot proceed; the staged documents must not stay
        _cleanup_case_dir(case_dir)
        return jsonify({
            "error": "Environment check failed — cannot start pipeline.",
            "preflight": pf.to_dict(),
        }), 400

    # ── Enqueue ──────────────────────────────────────────────────────────────
    job_id = uuid.uuid4().hex
    try:
        _enqueue(job_id, case_id, case_dir, dest_dir, make_zip, use_llm, csv_format)
    except EnqueueError as exc:
        _cleanup_case_dir(case_dir)
        return jsonify({
            "error": "A feldolgozó sor nem elérhető. Próbálja újra később.",
            "detail": str(exc),
        }), 503

    response: dict = {"job_id": job_id, "case_id": case_id}
    if pf.warnings:
        response["warnings"] = [w.to_dict() for w in pf.warnings]
    return jsonify(response), 200


@bp.route("/upload/unlock", methods=["POST"])
def unlock():
    """
    Supply a password for encrypted ZIP(s) previously uploaded via POST /upload.

    After successful decryption the contents are extracted into the case
    directory and a processing job is enqueued.

    Request (JSON)
    --------------
    {"case_id": "<short_hex>", "password": "<string>",
     "dest_dir": "<absolute path>", "make_zip": true|false,
     "use_llm": true|false, "csv_format": "intl"|"hu"}

    dest_dir/make_zip/use_llm/csv_format are re-sent by the UI because the user chose them before
    the first POST /upload; keeping them client-side avoids holding per-case
    state on the server between the two requests.

    Response 200
    ------------
    {"job_id": "<uuid_hex>", "case_id": "<short_hex>"}

    Response 400
    ------------
    {"error": "<reason>"}  — wrong password, missing fields, no encrypted ZIPs

    Response 404
    ------------
    {"error": "<reason>"}  — case directory not found

    Security
    --------
    The password is used only within this request to unlock the archive.
    It is never logged, stored in job metadata, or persisted to disk.
    """
    data = request.get_json(silent=True) or {}
    case_id = str(data.get("case_id", "")).strip()
    password = str(data.get("password", ""))

    # ── Input validation ─────────────────────────────────────────────────────
    if not case_id:
        return jsonify({"error": "Hiányzó eset azonosító."}), 400
    if not password:
        return jsonify({"error": "Hiányzó jelszó."}), 400
    if len(case_id) != 8 or not all(c in "0123456789abcdef" for c in case_id):
        return jsonify({"error": "Érvénytelen eset azonosító."}), 400

    try:
        dest_dir = _resolve_destination(str(data.get("dest_dir", "")))
        csv_format = _csv_format(data.get("csv_format"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    make_zip = _wants_zip(data.get("make_zip"))
    use_llm = _wants_llm(data.get("use_llm"))

    case_dir = (_upload_dir() / case_id).resolve()
    if not case_dir.exists() or not case_dir.is_dir():
        return jsonify({"error": "Eset nem található."}), 404

    # ── Find encrypted ZIPs ──────────────────────────────────────────────────
    from app.pipeline.zip_handler import extract_zip, is_encrypted_zip

    encrypted_zips = [
        p for p in case_dir.iterdir()
        if p.is_file() and p.suffix.lower() == ".zip" and is_encrypted_zip(p)
    ]

    if not encrypted_zips:
        return jsonify({"error": "Nem található titkosított archívum."}), 400

    # ── Unlock & extract ─────────────────────────────────────────────────────
    for zip_path in encrypted_zips:
        try:
            extract_zip(zip_path, case_dir, password=password)
            zip_path.unlink()
        except RuntimeError as exc:
            # Do not expose internal details — use the message from zip_handler
            return jsonify({"error": str(exc)}), 400

    # Expand any remaining unencrypted document-bundle ZIPs
    remaining = [p for p in case_dir.iterdir() if p.is_file()]
    _expand_document_bundles(case_dir, remaining)

    # ── Enqueue ──────────────────────────────────────────────────────────────
    job_id = uuid.uuid4().hex
    try:
        _enqueue(job_id, case_id, case_dir, dest_dir, make_zip, use_llm, csv_format)
    except EnqueueError as exc:
        _cleanup_case_dir(case_dir)
        return jsonify({
            "error": "A feldolgozó sor nem elérhető. Próbálja újra később.",
            "detail": str(exc),
        }), 503
    return jsonify({"job_id": job_id, "case_id": case_id}), 200


# Runs in a subprocess of its own: Tk is not thread-safe and must own the main
# thread, which it can never do inside a Flask request thread.
_FOLDER_PICKER = """
import sys
import tkinter as tk
from tkinter import filedialog

root = tk.Tk()
root.withdraw()
root.attributes("-topmost", True)
chosen = filedialog.askdirectory(
    title="Válassza ki az eredmények mentési helyét",
    initialdir=sys.argv[1] or None,
    mustexist=True,
)
root.destroy()
sys.stdout.write(chosen or "")
"""


@bp.route("/browse-folder", methods=["POST"])
def browse_folder():
    """
    Open a native folder-picker dialog on the machine running the server.

    A web page cannot obtain a real filesystem path for a folder — the File
    System Access API is Chromium-only (Firefox has no equivalent) and yields
    a handle rather than a path, and ``webkitdirectory`` exposes only relative
    names.  Since this app normally runs on the user's own desktop, the dialog
    is opened server-side instead.

    Request (JSON, optional)
    ------------------------
    {"current": "<path to open the dialog at>"}

    Response 200 — {"path": "<chosen folder>"}   ("" when the user cancelled)
    Response 403 — the request did not come from this machine
    Response 503 — no usable dialog here (no tkinter, headless host, Docker);
                   the UI falls back to typing the path.
    """
    import importlib.util
    import subprocess
    import sys

    if request.remote_addr not in ("127.0.0.1", "::1"):
        return jsonify({
            "error": "A mappaválasztó csak azon a gépen használható, "
                     "ahol az alkalmazás fut."
        }), 403

    if importlib.util.find_spec("tkinter") is None:
        return jsonify({
            "error": "A mappaválasztó nem érhető el ezen a gépen "
                     "(hiányzik a tkinter). Írja be az útvonalat kézzel."
        }), 503

    data = request.get_json(silent=True) or {}
    initial = str(data.get("current") or "")

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _FOLDER_PICKER, initial],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.warning("folder_picker_unavailable: %s", exc)
        return jsonify({
            "error": "A mappaválasztó nem válaszolt. Írja be az útvonalat kézzel."
        }), 503

    if proc.returncode != 0:
        log.warning("folder_picker_failed: %s", (proc.stderr or "").strip()[-300:])
        return jsonify({
            "error": "A mappaválasztó nem indítható ezen a gépen. "
                     "Írja be az útvonalat kézzel."
        }), 503

    return jsonify({"path": proc.stdout.strip()}), 200


def _rq_failure(job_id: str) -> str | None:
    """Return the failure reason if RQ has failed this job, else None."""
    try:
        from rq.job import Job

        from app.extensions import get_redis

        job = Job.fetch(job_id, connection=get_redis())
    except Exception:
        # Job expired out of Redis, or Redis is unreachable — nothing to add.
        return None

    if job.get_status(refresh=False) != "failed":
        return None

    exc = (job.exc_info or "").strip()
    detail = exc.splitlines()[-1] if exc else "A feldolgozó nem tudta futtatni a feladatot."
    log.error("job_failed_in_rq", extra={"job_id": job_id, "detail": detail})
    return detail


@bp.route("/status/<job_id>")
def status(job_id: str):
    """
    Return the current processing state for a job.

    Response 200 (always — never 404 for unknown job_id)
    ------------
    {
        "job_id":   "...",
        "case_id":  "..." | null,
        "status":   "QUEUED" | "INGESTING" | ... | "DONE" | "FAILED" | "Unknown",
        "progress": 0–100,
        "step":     "human-readable label",
        "error":    null | "error message"
    }
    """
    from app.pipeline.orchestrator import read_job_state
    state = read_job_state(job_id)

    # The pipeline writes progress to disk, but a job can die before it runs
    # (no worker, or a worker that cannot execute it). RQ knows; the state file
    # does not. Ask RQ rather than leave the UI spinning on a stale QUEUED.
    if state.get("status") in ("QUEUED", "Unknown"):
        failure = _rq_failure(job_id)
        if failure is not None:
            state.update(status="FAILED", progress=100, error=failure)

    return jsonify(state), 200


@bp.route("/download/<job_id>")
def download(job_id: str):
    """
    Serve the result files for a completed job as a ZIP (created on-the-fly).

    The UI no longer links here: results are written straight to the folder the
    user chose and the job card shows that path.  The endpoint is kept for the
    case where the app is reached from another machine, where the output folder
    is not the viewer's own disk.

    Response 200 — ZIP file (application/zip)
    Response 404 — job failed or file missing
    Response 202 — job still in progress
    """
    import io
    import zipfile

    from app.pipeline.orchestrator import read_job_state

    state = read_job_state(job_id)
    job_status = state.get("status", "Unknown")

    if job_status == "FAILED":
        return jsonify({"error": "Job failed.", "detail": state.get("error")}), 404

    if job_status != "DONE":
        return jsonify({
            "error": "Job not yet complete.",
            "status": job_status,
            "progress": state.get("progress", 0),
        }), 202

    case_id = state.get("case_id", job_id)
    case_dir = _output_dir() / case_id
    if not case_dir.exists() or not case_dir.is_dir():
        return jsonify({"error": "Result files not found."}), 404

    # Create ZIP in memory from the case output folder
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(case_dir.iterdir()):
            if f.is_file():
                zf.write(f, arcname=f.name)
    buf.seek(0)

    return send_file(
        buf,
        mimetype="application/zip",
        as_attachment=True,
        download_name=f"{case_id}_results.zip",
    )
