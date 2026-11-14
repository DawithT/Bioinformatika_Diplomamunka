"""
app/pipeline/orchestrator.py

The RQ task function that runs the full pipeline for one case.

Job state is persisted as JSON in data/jobs/<job_id>.json so that:
  - The /status endpoint can poll it without Redis state inspection.
  - Tests can call process_case() directly without RQ.

Public API
----------
process_case(job_id, case_dir, out_dir=None, make_zip=False, use_llm=True,
             csv_format="intl")
    Entry point enqueued by POST /upload.  Safe to call directly in tests.
    *out_dir* overrides settings.output_folder_path — the destination folder
    the user picked in the UI.  *make_zip* additionally writes
    <case_id>_results.zip next to the case folder.  *use_llm=False* runs the
    case without the LLM even when the server has one configured.
    *csv_format="hu"* writes wide_table.csv for Excel with Hungarian
    regional settings (";" separator, "," decimals).

read_job_state(job_id)
    Read current state from disk.  Returns a default "Unknown" dict if the
    job file does not exist.

fail_interrupted(job_id)
    Mark a job whose worker died mid-run as FAILED and delete its documents.

prune_expired()
    Delete staged uploads older than a day and job-state files older than a
    week.  Runs at worker start and after every job.

State schema  (data/jobs/<job_id>.json)
---------------------------------------
{
    "job_id":   "...",
    "case_id":  "...",
    "status":   "QUEUED" | "INGESTING" | ... | "DONE" | "FAILED",
    "progress": 0-100,
    "step":     "human-readable current step label",
    "error":    null | "error message string",
    "out_path": null | "absolute path of the folder holding the result files",
    "zip_path": null | "absolute path of the ZIP archive, if one was requested"
}

out_path/zip_path appear only once a job reaches DONE — the UI shows them as
the "saved here" confirmation instead of offering a download.

Error isolation
---------------
A job is refused (FAILED) when the process runs code older than the files on
disk — a worker not restarted after a change (app/services/code_version.py).
A single failing document logs a warning and is skipped.
Only AnchorNotFoundError (from step3_anchor) halts the pipeline.
"""
from __future__ import annotations

import json
import logging
import shutil
import traceback
import zipfile
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger(__name__)


# ── Job state helpers ─────────────────────────────────────────────────────────

def _jobs_dir() -> Path:
    """Return the jobs directory, creating it if necessary."""
    from config import settings
    d = settings.jobs_folder_path
    d.mkdir(parents=True, exist_ok=True)
    return d


def _state_path(job_id: str) -> Path:
    return _jobs_dir() / f"{job_id}.json"


def _write_state(job_id: str, state: dict) -> None:
    """Atomically overwrite the job state file."""
    path = _state_path(job_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_job_state(job_id: str) -> dict:
    """
    Read persisted job state from disk.

    Returns a synthetic "Unknown" state if the file does not exist.
    """
    path = _state_path(job_id)
    if not path.exists():
        return {
            "job_id":   job_id,
            "case_id":  None,
            "status":   "Unknown",
            "progress": 0,
            "step":     "",
            "error":    None,
        }
    return json.loads(path.read_text(encoding="utf-8"))


def write_queued_state(job_id: str, case_id: str) -> None:
    """Record a job as QUEUED the moment it is enqueued.

    Without this the state file only appears once ``process_case`` starts, so
    anything that kills the job first (a worker that cannot run it, or no
    worker at all) left the UI polling a non-existent file and showing
    "Ismeretlen" with a dead progress bar forever.
    """
    _write_state(job_id, {
        "job_id":   job_id,
        "case_id":  case_id,
        "status":   "QUEUED",
        "progress": 0,
        "step":     "Várakozás a feldolgozóra…",
        "error":    None,
    })


def _update(job_id: str, state: dict, status: str, progress: int, step: str) -> dict:
    """Write a progress update and return the updated state dict."""
    state.update(status=status, progress=progress, step=step, error=None)
    _write_state(job_id, state)
    log.info("job_progress: job=%s status=%s progress=%d step=%s",
             job_id, status, progress, step)
    return state


def _fail(job_id: str, state: dict, message: str) -> None:
    """Mark the job as FAILED."""
    state.update(status="FAILED", progress=100, error=message)
    _write_state(job_id, state)
    log.error("job_failed: job=%s error=%s", job_id, message)


def _remove_case_dir(case_path: Path) -> None:
    """Delete one staged case directory. Best-effort."""
    if case_path.exists():
        try:
            shutil.rmtree(case_path)
            log.info("cleanup: removed case_dir %s", case_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("cleanup_failed: case_dir=%s error=%s", case_path, exc)


def fail_interrupted(job_id: str) -> None:
    """
    Fail a job whose worker died while running it, and delete its documents.

    ``process_case()``'s ``finally`` never ran for such a job, so the state
    file stayed at the last step forever and the uploaded documents stayed in
    the upload folder.  Called by ``app.services.workers.clean_dead_workers``.
    """
    from config import settings

    state = read_job_state(job_id)
    if state["status"] in ("DONE", "FAILED", "Unknown"):
        return
    _fail(job_id, state, "The worker stopped while processing this case "
                         "(closed or killed). Submit the case again.")
    if state.get("case_id"):
        _remove_case_dir(settings.upload_folder_path / state["case_id"])


# Staged documents nobody will process any more: a password-protected upload
# whose password was never given, a job whose worker died.  Job-state files
# are kept longer, for the status cards and for looking back at failures, but
# their error messages can carry document file names.
STAGED_UPLOAD_RETENTION_S = 24 * 3600
JOB_STATE_RETENTION_S = 7 * 24 * 3600


def prune_expired() -> None:
    """
    Delete staged case directories older than ``STAGED_UPLOAD_RETENTION_S``
    and job-state files older than ``JOB_STATE_RETENTION_S`` (by mtime).

    Runs at worker start and after every job.  A job still queued after a
    day finds its directory gone and fails with "No files found".
    """
    import time

    from config import settings

    now = time.time()
    uploads = settings.upload_folder_path
    if uploads.is_dir():
        for case_path in uploads.iterdir():
            if case_path.is_dir() and now - case_path.stat().st_mtime > STAGED_UPLOAD_RETENTION_S:
                _remove_case_dir(case_path)
    for path in _jobs_dir().iterdir():
        if path.suffix in (".json", ".tmp") and now - path.stat().st_mtime > JOB_STATE_RETENTION_S:
            try:
                path.unlink()
            except OSError as exc:
                log.warning("prune_failed: path=%s error=%s", path, exc)


# ── LLM client and prompt cache for one run ───────────────────────────────────

def _llm_for_run(use_llm: bool):
    """
    Return ``(client, cache, run_config)`` for one job.

    The LLM runs only when the server allows it and the user did not switch
    it off.  The cache is created per job, in memory only, so it is gone when
    the job ends; it is off whenever the LLM is, and its scope (model,
    request parameters, registry hash) keeps answers from being reused under
    a different configuration.
    """
    from app.services.cache import PromptCache
    from app.services.llm_client import DisabledClient, get_llm_client, llm_configured
    from app.services.run_config import describe_run

    llm_on = use_llm and llm_configured()
    run_config = describe_run(llm_on)
    cache = PromptCache(
        enabled=run_config["cache"]["enabled"],
        scope=json.dumps(run_config, sort_keys=True),
    )
    client = get_llm_client() if llm_on else DisabledClient()
    return client, cache, run_config


# ── Main orchestrator ─────────────────────────────────────────────────────────

def process_case(
    job_id: str,
    case_dir: str,
    out_dir: str | None = None,
    make_zip: bool = False,
    use_llm: bool = True,
    csv_format: str = "intl",
) -> None:
    """
    Run the full extraction pipeline for one case.

    Parameters
    ----------
    job_id : str
        Unique job identifier (also the output zip stem).
    case_dir : str
        Directory containing the uploaded documents for this case.
    out_dir : str | None
        Destination folder for the results.  ``None`` falls back to
        ``settings.output_folder_path``; the web UI passes the folder the
        user chose before starting the job.
    make_zip : bool
        Also write ``<case_id>_results.zip`` beside the case folder.
    use_llm : bool
        The user's per-run LLM switch.  It can only turn the LLM off: with
        ``ENABLE_LLM=false`` or ``LLM_PROVIDER=disabled`` the run is LLM-free
        regardless.  Without the LLM the prompt cache is off too.
    csv_format : str
        Layout of wide_table.csv: "intl" ("," separator, "." decimals) or
        "hu" (";" and "," decimals, for Excel with Hungarian settings).

    This function is designed to run inside an RQ worker but is also
    directly callable in tests by passing a pre-populated directory.
    """
    from app.domain.enums import DocType
    from app.domain.models import (
        AncillaryRecord,
        CaseResult,
        MismatchRecord,
        PatientAnchor,
        UnitMismatchRecord,
    )
    from app.pipeline.step5_extract.ancillary import (
        extract as extract_ancillary,
    )
    from app.pipeline.step5_extract.gender import extract_gender_field
    from app.pipeline.step1_ingest import ingest, reingest_pdf_with_pdfminer
    from app.pipeline.step2_route import route as route_doc
    from app.pipeline.step3_anchor import extract_anchor, AnchorNotFoundError
    from app.pipeline.step5_extract.soft_llm import SoftExtractor
    from app.pipeline.step6_merge import DocInput, merge
    from app.pipeline.step8_export import export
    from app.registry.loader import load_registry
    from app.registry.rules_engine import build_rules_from_registry, apply
    from app.services import code_version
    from config import settings

    case_path = Path(case_dir)
    case_id = case_path.name

    # Initialise job state
    state: dict = {
        "job_id":   job_id,
        "case_id":  case_id,
        "status":   "QUEUED",
        "progress": 0,
        "step":     "Queued",
        "error":    None,
    }
    _write_state(job_id, state)

    try:
        # A worker running code older than the files on disk would produce
        # results from the old code — refuse (inside try: uploads still go).
        if code_version.is_stale():
            _fail(job_id, state,
                  "The worker runs older code or settings than the files on "
                  "disk. Restart it (start.bat) and submit the case again.")
            return

        # ── Step 1: Ingest ────────────────────────────────────────────────────
        _update(job_id, state, "INGESTING", 10, "Ingesting documents\u2026")

        files = sorted(case_path.iterdir()) if case_path.is_dir() else []
        if not files:
            _fail(job_id, state, f"No files found in case directory: {case_dir}")
            return

        from app.domain.models import RoutedDoc
        raw_docs: list[RoutedDoc] = []
        for f in files:
            if f.is_file():
                try:
                    doc = ingest(f)
                    raw_docs.append(doc)
                except Exception as exc:
                    log.warning("ingest_failed: file=%s error=%s", f, exc)

        if not raw_docs:
            _fail(job_id, state, "All documents failed to ingest.")
            return

        # ── Step 2: Route ─────────────────────────────────────────────────────
        _update(job_id, state, "ROUTING", 20, "Classifying documents\u2026")

        routed_docs: list[RoutedDoc] = []
        ancillary_records: list[AncillaryRecord] = []

        for doc in raw_docs:
            try:
                routed = route_doc(doc)
                # LAB_REPORT PDFs: pymupdf4llm fragments rotated column labels
                # into markdown tables like `|tje<br>s<br>r<br>é<br>v…|`.
                # Re-ingest with pdfminer whose layout engine keeps labels intact.
                if (
                    routed.doc_type == DocType.LAB_REPORT
                    and routed.meta.get("engine") == "pymupdf4llm"
                ):
                    routed = reingest_pdf_with_pdfminer(routed)
                routed_docs.append(routed)

                # Audit record for every ancillary document:
                #   UNKNOWN      → unclassified (keeps the old triage fields)
                #   AMBULANSLAP  → classified-but-unrouted (adds section
                #                  extractions: VÉLEMÉNY / DIAGNÓZISOK /
                #                  BEAVATKOZÁSOK)
                if routed.doc_type in (DocType.UNKNOWN, DocType.AMBULANSLAP):
                    trace = routed.meta.get("routing_trace", {})
                    if routed.doc_type == DocType.AMBULANSLAP:
                        failure_reason = "classified_ancillary"
                    else:
                        failure_reason = trace.get("reason", "unknown")
                    ancillary_records.append(extract_ancillary(
                        doc=routed,
                        case_id=case_id,
                        routing_method=routed.meta.get(
                            "routing_method", "unclassified",
                        ),
                        failure_reason=failure_reason,
                        best_score=trace.get("best_score", 0),
                    ))
            except Exception as exc:
                log.warning("route_failed: file=%s error=%s", doc.path, exc)
                routed_docs.append(doc)  # keep with UNKNOWN

        # ── Step 3: Anchor ────────────────────────────────────────────────────
        _update(job_id, state, "ANCHORING", 30, "Extracting patient anchor\u2026")

        discharge_docs = [d for d in routed_docs if d.doc_type == DocType.DISCHARGE_SUMMARY]
        if not discharge_docs:
            _fail(job_id, state, "No DISCHARGE_SUMMARY found \u2014 pipeline halted.")
            return

        # Extract one PatientAnchor per discharge summary. Ward transfers are
        # documented as separate admissions, so a single hospitalisation may
        # produce multiple discharge summaries. They are ordered by
        # admission_start; the combined anchor spans the full episode.
        per_discharge_anchors: list[tuple[PatientAnchor, RoutedDoc]] = []
        for d in discharge_docs:
            try:
                per_discharge_anchors.append((extract_anchor(d), d))
            except AnchorNotFoundError as exc:
                log.warning(
                    "anchor: skipping discharge %s \u2014 %s", d.path.name, exc,
                )
        if not per_discharge_anchors:
            _fail(
                job_id, state,
                "AnchorNotFoundError: no discharge summary yielded a valid "
                "admission date.",
            )
            return

        per_discharge_anchors.sort(key=lambda pa: (pa[0].admission_start, pa[0].discharge_end))
        first_anchor = per_discharge_anchors[0][0]
        last_anchor  = per_discharge_anchors[-1][0]

        # Combined anchor: earliest admission \u2192 latest discharge.
        # Identity (name/dob/taj) comes from the first discharge; any
        # mismatches across discharges are recorded for audit so the pipeline
        # still delivers a result when one summary has a data-entry typo.
        anchor = PatientAnchor.create(
            name=first_anchor.name,
            dob=first_anchor.dob,
            taj=first_anchor.taj,
            admission_start=first_anchor.admission_start,
            discharge_end=last_anchor.discharge_end,
        )
        discharge_identity_mismatches: list[MismatchRecord] = []
        for a, d in per_discharge_anchors[1:]:
            candidate = PatientAnchor.make_fingerprint(a.name, a.dob, a.taj)
            if candidate != anchor.fingerprint:
                log.warning(
                    "anchor: discharge %s identity differs from first summary "
                    "(expected %s, got %s)",
                    d.path.name, anchor.fingerprint, candidate,
                )
                discharge_identity_mismatches.append(MismatchRecord(
                    case_id=case_id,
                    filename=d.path.name,
                    expected_fingerprint=anchor.fingerprint,
                    found_name=a.name,
                    found_taj=a.taj,
                ))
        log.info(
            "anchor: combined window %s \u2192 %s across %d discharge summary/summaries",
            anchor.admission_start.isoformat(),
            anchor.discharge_end.isoformat(),
            len(per_discharge_anchors),
        )

        # Map each discharge path \u2192 chronological index so field filtering
        # can apply the anam/adm \u2192 first, dis \u2192 last, inhosp \u2192 all rule.
        discharge_order: dict[Path, int] = {
            pa[1].path: idx for idx, pa in enumerate(per_discharge_anchors)
        }
        n_discharges = len(per_discharge_anchors)

        # ── Load shared resources ─────────────────────────────────────────────
        registry = load_registry(settings.registry_path_obj)
        rules    = build_rules_from_registry(registry)
        client, cache, run_config = _llm_for_run(use_llm)
        soft_extractor = SoftExtractor(client, cache)
        log.info("run_config: job=%s llm=%s cache=%s",
                 job_id, run_config["llm"]["enabled"], cache.enabled)

        # Registry field \u2192 timepoint map, used to enforce the multi-discharge
        # chronology rule.
        timepoint_map: dict[str, str] = {
            fd.field_name: fd.timepoint for fd in registry
        }

        # ── Step 5: Extract ───────────────────────────────────────────────────
        _update(job_id, state, "EXTRACTING", 60, "Extracting fields\u2026")

        n_threads = settings.hard_extraction_threads

        # Pre-compute anchor field keys so soft extraction can skip them.
        # Gender is deduced from the Hungarian name via the LLM (with the
        # '-n\u00e9' suffix rule enforced in the system prompt); absent or
        # ambiguous names fall through to the normal soft extractor.
        anchor_fields = _anchor_to_fields(anchor)
        gender_field = extract_gender_field(
            soft_extractor._client, cache, anchor.name,
        )
        if gender_field is not None:
            anchor_fields.append(gender_field)
        anchor_keys = {f.key for f in anchor_fields}

        # Phase 1: Hard extraction (parallelisable)
        hard_results: list[list | None] = _extract_all_hard(
            routed_docs, anchor, n_threads,
        )

        # Phase 2: Soft extraction + heuristic review (sequential — LLM-bound)
        doc_inputs: list[DocInput] = []
        for doc, hard_fields in zip(routed_docs, hard_results):
            if hard_fields is None:
                hard_fields = []

            # UNKNOWN documents: no parser covers them — skip soft extraction
            if doc.doc_type == DocType.UNKNOWN:
                log.info(
                    "extract: skipping soft extraction for UNKNOWN doc %s",
                    doc.path.name,
                )
                fields = hard_fields
            else:
                try:
                    # populated_keys is recomputed *inside* _extract_doc_soft
                    # after heuristic review, so LLM-rejected heuristic fields
                    # are re-opened for the soft LLM to fill.
                    fields = _extract_doc_soft(
                        doc, hard_fields, registry, soft_extractor, anchor,
                        anchor_keys=anchor_keys,
                    )
                except Exception as exc:
                    log.warning(
                        "soft_extraction_failed: file=%s doc_type=%s error=%s",
                        doc.path, doc.doc_type, exc,
                    )
                    fields = hard_fields

            # Multi-discharge chronology filter (DISCHARGE_SUMMARY only):
            #   anam / adm timepoints \u2192 kept only for the earliest summary;
            #   dis          timepoint \u2192 kept only for the latest summary;
            #   inhosp and untyped keys \u2192 kept for every summary.
            if (
                doc.doc_type == DocType.DISCHARGE_SUMMARY
                and n_discharges > 1
                and doc.path in discharge_order
            ):
                fields = _filter_discharge_by_timepoint(
                    fields,
                    idx=discharge_order[doc.path],
                    last_idx=n_discharges - 1,
                    timepoint_map=timepoint_map,
                )

            doc_inputs.append(DocInput(
                doc_type=doc.doc_type,
                fields=fields,
                doc_date=_doc_date(doc),
                doc_fingerprint=_doc_fingerprint(doc),
            ))

        if not doc_inputs:
            _fail(job_id, state, "All documents failed extraction.")
            return

        # ── Bridge: anchor identity → output fields ──────────────────────────
        # (anchor_fields already computed above for the populated-keys set)
        for di in doc_inputs:
            if di.doc_type == DocType.DISCHARGE_SUMMARY:
                di.fields.extend(anchor_fields)
                break

        # ── Step 6: Merge ─────────────────────────────────────────────────────
        _update(job_id, state, "MERGING", 80, "Merging fields\u2026")

        master_fields, conflicts, mismatches, out_of_window = merge(
            doc_inputs, anchor, case_id, registry=registry
        )

        # Surface identity mismatches detected across multiple discharge
        # summaries so downstream audit sheets show the affected filenames.
        if discharge_identity_mismatches:
            mismatches = list(mismatches) + discharge_identity_mismatches

        # ── Unit-mismatch audit ──────────────────────────────────────────────
        # Every ExtractedField carries the observed unit (unit_seen) and the
        # boolean comparison result (unit_ok). Collect the False entries into
        # a dedicated audit list; values are kept as-is (no auto-conversion).
        _registry_unit: dict[str, str | None] = {
            fd.field_name: fd.unit for fd in registry
        }
        unit_mismatches: list[UnitMismatchRecord] = []
        for key, ef in master_fields.items():
            if ef.unit_ok is False:
                unit_mismatches.append(UnitMismatchRecord(
                    case_id=case_id,
                    field=key,
                    expected_unit=_registry_unit.get(key) or "",
                    seen_unit=ef.unit_seen or "",
                    raw_value=str(ef.value),
                    doc_type=ef.doc_type,
                    page=ef.page,
                ))

        # ── Rules engine ──────────────────────────────────────────────────────
        master_fields = apply(master_fields, rules, registry=registry)

        # ── Build CaseResult ─────────────────────────────────────────────────
        result = CaseResult(
            case_id=case_id,
            fields=master_fields,
            conflicts=conflicts,
            mismatches=mismatches,
            out_of_window=out_of_window,
            unit_mismatches=unit_mismatches,
            ancillary_docs=ancillary_records,
        )

        # ── Step 8: Export ────────────────────────────────────────────────────
        _update(job_id, state, "EXPORTING", 90, "Exporting results\u2026")

        dest = Path(out_dir) if out_dir else settings.output_folder_path
        dest.mkdir(parents=True, exist_ok=True)
        export(result, dest, registry, run_config={
            "case_id": case_id,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            **run_config,
            "export": {"csv_format": csv_format},
        }, csv_format=csv_format)

        case_out = (dest / case_id).resolve()
        state["out_path"] = str(case_out)

        if make_zip:
            # The result files are already safely on disk at this point, so a
            # failing archive must not turn a successful run into FAILED —
            # report it beside the path instead.
            try:
                state["zip_path"] = str(_zip_case_output(case_out))
            except OSError as exc:
                state["zip_error"] = str(exc)
                log.warning("zip_failed: case=%s error=%s", case_id, exc)

        # ── Done ──────────────────────────────────────────────────────────────
        _update(job_id, state, "DONE", 100, "Done")
        log.info("job_done: job=%s case=%s", job_id, case_id)

    except Exception as exc:
        _fail(job_id, state, f"{type(exc).__name__}: {exc}")
        log.error("orchestrator_unhandled_error: job=%s\n%s",
                  job_id, traceback.format_exc())

    finally:
        # ── Cleanup: remove uploaded patient documents (data safety) ──────
        _remove_case_dir(Path(case_dir))
        prune_expired()


# ── Private helpers ────────────────────────────────────────────────────────────

def _zip_case_output(case_out: Path) -> Path:
    """
    Archive the exported files of one case into ``<case_id>_results.zip``.

    The archive is written *beside* the case folder, not inside it, so the
    folder keeps exactly the files step8_export produced.
    """
    zip_path = case_out.parent / f"{case_out.name}_results.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(case_out.iterdir()):
            if f.is_file():
                zf.write(f, arcname=f.name)
    log.info("zip_written: %s", zip_path)
    return zip_path


def _anchor_to_fields(anchor) -> list:
    """
    Convert PatientAnchor identity fields to ExtractedField objects.

    These 5 fields are reliably extracted by step3_anchor via regex but were
    never converted to ExtractedField objects for the output table.  This
    function bridges that gap.
    """
    from app.domain.enums import DocType, FieldSource
    from app.domain.models import ExtractedField

    fields: list[ExtractedField] = []

    if anchor.name and anchor.name != "UNKNOWN":
        fields.append(ExtractedField(
            key="a_beteg_neve",
            value=anchor.name,
            source=FieldSource.HARD,
            doc_type=DocType.DISCHARGE_SUMMARY,
            confidence=0.95,
            engine="anchor_regex",
        ))

    from datetime import date as _date
    if anchor.dob != _date(1900, 1, 1):
        fields.append(ExtractedField(
            key="szuletesi_ideje",
            value=anchor.dob.isoformat(),
            source=FieldSource.HARD,
            doc_type=DocType.DISCHARGE_SUMMARY,
            confidence=0.95,
            engine="anchor_regex",
        ))

    if anchor.taj and anchor.taj != "000000000":
        fields.append(ExtractedField(
            key="taj_szam",
            value=anchor.taj,
            source=FieldSource.HARD,
            doc_type=DocType.DISCHARGE_SUMMARY,
            confidence=0.99,
            engine="anchor_regex",
        ))

    fields.append(ExtractedField(
        key="korhazi_felvetel_ideje_adm",
        value=anchor.admission_start.strftime("%Y.%m.%d %H:%M"),
        source=FieldSource.HARD,
        doc_type=DocType.DISCHARGE_SUMMARY,
        confidence=0.99,
        engine="anchor_regex",
    ))

    fields.append(ExtractedField(
        key="elbocsajtas_datuma_dis",
        value=anchor.discharge_end.date().isoformat(),
        source=FieldSource.HARD,
        doc_type=DocType.DISCHARGE_SUMMARY,
        confidence=0.99,
        engine="anchor_regex",
    ))

    return fields


def _extract_all_hard(
    docs: list,
    anchor,
    n_threads: int,
) -> list[list | None]:
    """
    Run hard extraction on all documents.

    When *n_threads* == 1 (default), runs sequentially — identical output
    to the pre-threading code path.  When > 1, uses a ThreadPoolExecutor.

    Returns a list parallel to *docs*: each element is either a list of
    ExtractedField objects, or None if extraction failed for that document.
    """
    if n_threads <= 1:
        # Sequential path — guaranteed identical to pre-threading behaviour
        results: list[list | None] = []
        for doc in docs:
            try:
                results.append(_extract_doc_hard(doc, anchor))
            except Exception as exc:
                log.warning("hard_extraction_failed: file=%s error=%s", doc.path, exc)
                results.append(None)
        return results

    # Threaded path
    from concurrent.futures import ThreadPoolExecutor

    log.info("hard_extraction: using %d threads for %d documents", n_threads, len(docs))

    def _safe_extract(doc):
        try:
            return _extract_doc_hard(doc, anchor)
        except Exception as exc:
            log.warning("hard_extraction_failed: file=%s error=%s", doc.path, exc)
            return None

    with ThreadPoolExecutor(max_workers=n_threads) as executor:
        return list(executor.map(_safe_extract, docs))


def _extract_doc_hard(doc, anchor) -> list:
    """Extract hard (deterministic) fields from a single RoutedDoc."""
    from app.domain.enums import DocType
    from app.pipeline.step5_extract.hard_lab import TextLabParser, TableLabParser
    from app.pipeline.step5_extract._utils import strip_print_furniture
    from app.pipeline.step5_extract.hard_events import (
        EndoscopyParser, FibroscanParser, ImagingParser, MicrobiologyParser,
    )
    from app.pipeline.step5_extract.hard_scores import ScoresVitalsParser
    from app.pipeline.step5_extract.hard_structured import BNOParser, VirologyParser

    fields: list = []
    dt = doc.doc_type

    # Hard lab extraction (LAB_REPORT only)
    if dt == DocType.LAB_REPORT:
        try:
            fields.extend(TextLabParser().parse(strip_print_furniture(doc.pages), anchor))
        except Exception as exc:
            log.warning("hard_lab_text_failed: error=%s", exc)
        if doc.tables:
            sample_date = _parse_date_str(doc.meta.get("sample_date"))
            try:
                fields.extend(TableLabParser().parse(
                    [list(row) for row in doc.tables],
                    anchor,
                    sample_date=sample_date,
                ))
            except Exception as exc:
                log.warning("hard_lab_table_failed: error=%s", exc)

    # Hard event extraction — standalone event documents
    if dt == DocType.ENDOSCOPY:
        try:
            fields.extend(EndoscopyParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_endoscopy_failed: error=%s", exc)
    elif dt == DocType.ELASTOGRAPHY:
        # FibroscanParser currently handles fibroscan-formatted reports;
        # shear-wave reports fall through to heuristic/soft extraction until a
        # dedicated parser is added.
        try:
            fields.extend(FibroscanParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_elastography_failed: error=%s", exc)
    elif dt == DocType.IMAGING:
        try:
            fields.extend(ImagingParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_imaging_failed: error=%s", exc)
    elif dt == DocType.MICROBIOLOGY:
        try:
            fields.extend(MicrobiologyParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_microbiology_failed: error=%s", exc)

    # Hard event extraction — DISCHARGE_SUMMARY embeds event reports
    if dt == DocType.DISCHARGE_SUMMARY:
        for parser_cls, label in (
            (EndoscopyParser, "hard_endoscopy_in_discharge"),
            (FibroscanParser, "hard_fibroscan_in_discharge"),
            (ImagingParser, "hard_imaging_in_discharge"),
            (MicrobiologyParser, "hard_microbiology_in_discharge"),
        ):
            try:
                fields.extend(parser_cls().parse(doc, anchor))
            except Exception as exc:
                log.warning("%s_failed: error=%s", label, exc)

    # Hard lab extraction from DISCHARGE_SUMMARY (inline lab blocks)
    if dt == DocType.DISCHARGE_SUMMARY:
        try:
            fields.extend(TextLabParser().parse(
                strip_print_furniture(doc.pages), anchor,
                doc_type=DocType.DISCHARGE_SUMMARY,
            ))
        except Exception as exc:
            log.warning("hard_lab_in_discharge_failed: error=%s", exc)

    # Hard structured extraction — scores/vitals, BNO codes, virology
    if dt == DocType.DISCHARGE_SUMMARY:
        try:
            fields.extend(ScoresVitalsParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_scores_vitals_failed: error=%s", exc)
        try:
            fields.extend(BNOParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_bno_failed: error=%s", exc)
        try:
            fields.extend(VirologyParser().parse(doc, anchor))
        except Exception as exc:
            log.warning("hard_virology_failed: error=%s", exc)

    return fields


def _extract_doc_soft(
    doc, hard_fields, registry, soft_extractor, anchor,
    anchor_keys: set[str] | None = None,
) -> list:
    """Run heuristic LLM review and soft LLM extraction on a single document.

    populated_keys for the soft LLM is computed from the post-review field
    set: fields rejected by review_heuristic_fields are NOT in populated_keys,
    so the soft LLM gets a chance to provide a value for them.
    """
    from app.domain.enums import FieldSource

    fields = list(hard_fields)

    # LLM review of HEURISTIC fields
    heuristic_fields = [f for f in fields if f.source == FieldSource.HEURISTIC]
    if heuristic_fields:
        try:
            reviewed = soft_extractor.review_heuristic_fields(doc, heuristic_fields)
            reviewed_keys = {f.key for f in reviewed}
            original_keys = {f.key for f in heuristic_fields}
            rejected_keys = original_keys - reviewed_keys
            fields = [f for f in fields if f.source != FieldSource.HEURISTIC]
            fields.extend(reviewed)
            if rejected_keys:
                log.info("LLM review rejected %d fields: %s",
                         len(rejected_keys), rejected_keys)
        except Exception as exc:
            log.warning("llm_review_failed: error=%s — keeping originals", exc)

    # Soft LLM extraction — populated_keys excludes fields already covered
    # by higher-priority deterministic sources (hard, anchor, BNO, virology).
    # Computed AFTER heuristic review so rejected heuristics can be re-extracted.
    populated_keys = {f.key for f in fields} | (anchor_keys or set())
    try:
        fields.extend(soft_extractor.extract(
            doc, registry, anchor, populated_keys=populated_keys,
        ))
    except Exception as exc:
        log.warning("soft_extraction_failed: doc_type=%s error=%s", doc.doc_type, exc)

    return fields


def _doc_date(doc) -> "date | None":
    """Best-effort document date from meta."""
    for key in ("exam_date", "sample_date", "report_date", "doc_date"):
        raw = doc.meta.get(key)
        if raw:
            result = _parse_date_str(raw)
            if result:
                return result
    return None


def _parse_date_str(raw) -> "date | None":
    """Convert a meta date value (ISO string or date object) to date."""
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    from datetime import datetime
    for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(raw)[:10], fmt).date()
        except ValueError:
            continue
    return None


def _doc_fingerprint(doc) -> "str | None":
    """
    Return the patient fingerprint stored in doc.meta, if present.
    Discharge summaries are excluded — they are the identity source of truth.
    """
    from app.domain.enums import DocType
    if doc.doc_type == DocType.DISCHARGE_SUMMARY:
        return None
    return doc.meta.get("patient_fingerprint") or None


def _filter_discharge_by_timepoint(
    fields: list,
    idx: int,
    last_idx: int,
    timepoint_map: dict[str, str],
) -> list:
    """
    Apply the multi-discharge chronology rule to one discharge summary's fields.

    Rule (per user guidance):
      - timepoint ``anam`` / ``adm`` → only the first discharge contributes.
      - timepoint ``dis``            → only the last discharge contributes.
      - timepoint ``inhosp`` or unmapped keys → every discharge contributes.

    Anchor-bridge fields (added later in the orchestrator) carry timepoint
    ``adm``/``dis`` only for the combined anchor values, so they pass this
    filter untouched — they are attached after this step runs.
    """
    is_first = idx == 0
    is_last  = idx == last_idx
    result: list = []
    for ef in fields:
        tp = timepoint_map.get(ef.key)
        if tp in ("anam", "adm"):
            if is_first:
                result.append(ef)
        elif tp == "dis":
            if is_last:
                result.append(ef)
        else:
            result.append(ef)
    return result
