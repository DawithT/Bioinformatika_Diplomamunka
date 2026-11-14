"""
cli.py — Command-line entry point for processing cases without the web UI.

Runs the full extraction pipeline synchronously on local files.
Does NOT require Flask, Redis, or RQ — only Python + the LLM server.

Usage
-----
    # Single case: one or more files (PDF, DOCX, ZIP)
    python cli.py path/to/discharge.pdf path/to/lab_report.pdf

    # A directory containing documents for one case
    python cli.py path/to/case_folder/

    # Encrypted ZIP — supply password
    python cli.py path/to/encrypted.zip --password mysecret

    # Custom output directory
    python cli.py docs/*.pdf --output-dir results/

    # Disable LLM (hard extraction only)
    python cli.py docs/*.pdf --no-llm
"""
from __future__ import annotations

import argparse
import shutil
import sys
import uuid
from pathlib import Path


def _configure_logging(verbose: bool = False) -> None:
    """Set up structlog for console output (no Flask dependency)."""
    import structlog
    from app.extensions import configure_logging

    configure_logging(log_level="DEBUG" if verbose else "INFO")
    structlog.get_logger(__name__).info("cli_started")


def _stage_files(input_paths: list[Path], case_dir: Path) -> list[Path]:
    """
    Copy input files into the case staging directory.

    If a single directory is given, copies its contents.
    Otherwise copies individual files.

    Returns the list of staged file paths.
    """
    case_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []

    for src in input_paths:
        if src.is_dir():
            for child in sorted(src.iterdir()):
                if child.is_file():
                    dst = case_dir / child.name
                    shutil.copy2(str(child), str(dst))
                    staged.append(dst)
        elif src.is_file():
            dst = case_dir / src.name
            shutil.copy2(str(src), str(dst))
            staged.append(dst)
        else:
            print(f"WARNING: skipping {src} (not a file or directory)", file=sys.stderr)

    return staged


def _handle_zips(case_dir: Path, staged: list[Path], password: str | None) -> None:
    """
    Handle ZIP archives in the staged files:
    - Decrypt encrypted ZIPs if a password is provided
    - Expand unprotected document-bundle ZIPs
    """
    from app.pipeline.zip_handler import extract_zip, is_document_bundle, is_encrypted_zip

    encrypted = [p for p in staged if p.suffix.lower() == ".zip" and is_encrypted_zip(p)]

    if encrypted and not password:
        names = ", ".join(p.name for p in encrypted)
        print(
            f"ERROR: Encrypted ZIP(s) detected: {names}\n"
            f"Supply the password with --password <pwd>",
            file=sys.stderr,
        )
        sys.exit(1)

    # Decrypt
    for zip_path in encrypted:
        try:
            extract_zip(zip_path, case_dir, password=password)
            zip_path.unlink()
            print(f"  Decrypted: {zip_path.name}")
        except RuntimeError as exc:
            print(f"ERROR: Failed to decrypt {zip_path.name}: {exc}", file=sys.stderr)
            sys.exit(1)

    # Expand unprotected document-bundle ZIPs
    remaining = [p for p in case_dir.iterdir() if p.is_file()]
    for path in remaining:
        if path.suffix.lower() != ".zip" or not path.exists():
            continue
        if is_document_bundle(path):
            try:
                extract_zip(path, case_dir)
                path.unlink()
                print(f"  Expanded bundle: {path.name}")
            except Exception as exc:
                print(f"WARNING: Failed to expand {path.name}: {exc}", file=sys.stderr)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="CirrhosisRegistry - process clinical documents from the command line.",
        epilog=(
            "Examples:\n"
            "  python cli.py zarojelentes.pdf labor.pdf\n"
            "  python cli.py case_folder/\n"
            "  python cli.py encrypted.zip --password secret\n"
            "  python cli.py *.pdf --output-dir results/ --no-llm\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Document files (PDF, DOCX, ZIP) or a single directory containing them",
    )
    parser.add_argument(
        "--password",
        default=None,
        help="Password for encrypted ZIP archives",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Output directory for results (default: data/out/)",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Disable LLM soft-field extraction (hard extraction only)",
    )
    parser.add_argument(
        "--threads", "-t",
        type=int,
        default=None,
        help="Number of threads for hard extraction (default: from .env or 1)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug-level logging",
    )

    args = parser.parse_args()

    # ── Validate inputs exist ────────────────────────────────────────────────
    for p in args.inputs:
        if not p.exists():
            print(f"ERROR: {p} does not exist", file=sys.stderr)
            sys.exit(1)

    # ── Override LLM setting before importing config ─────────────────────────
    if args.no_llm:
        import os
        os.environ["ENABLE_LLM"] = "false"

    # ── Configure logging (imports structlog + app.extensions) ───────────────
    _configure_logging(verbose=args.verbose)

    # ── Import pipeline components (after logging is set up) ─────────────────
    from config import settings
    from app.pipeline.orchestrator import process_case

    # ── Override output dir if specified ──────────────────────────────────────
    if args.output_dir:
        settings.output_folder = str(args.output_dir)
        args.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Override thread count if specified ────────────────────────────────────
    if args.threads is not None:
        settings.hard_extraction_threads = max(1, min(args.threads, 16))

    # ── Pre-flight environment check ─────────────────────────────────────────
    from app.pipeline.preflight import check_environment

    preflight = check_environment(scope="cli")

    if not preflight.ok:
        if preflight.critical:
            print("\n CRITICAL — cannot proceed:", file=sys.stderr)
            for issue in preflight.critical:
                print(f"\n  [{issue.component}]", file=sys.stderr)
                print(f"    {issue.message}", file=sys.stderr)
                print(f"    Install: {issue.install_hint}", file=sys.stderr)
                print(f"    Impact:  {issue.impact}", file=sys.stderr)

        if preflight.warnings:
            print("\n WARNINGS — pipeline will run with reduced capabilities:", file=sys.stderr)
            for issue in preflight.warnings:
                print(f"\n  [{issue.component}]", file=sys.stderr)
                print(f"    {issue.message}", file=sys.stderr)
                print(f"    Install: {issue.install_hint}", file=sys.stderr)
                print(f"    Impact:  {issue.impact}", file=sys.stderr)

        if not preflight.can_run:
            print("\nCannot start — fix the critical issues above.", file=sys.stderr)
            sys.exit(1)

        # Warnings only — ask user whether to continue
        print()
        try:
            answer = input("Continue with reduced capabilities? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.", file=sys.stderr)
            sys.exit(0)

    # ── Stage files into a case directory ────────────────────────────────────
    case_id = uuid.uuid4().hex[:8]
    case_dir = settings.upload_folder_path / case_id
    job_id = uuid.uuid4().hex

    print(f"Case ID:  {case_id}")
    print(f"Job ID:   {job_id}")
    print("Staging files...")

    staged = _stage_files(args.inputs, case_dir)
    if not staged:
        print("ERROR: No files to process.", file=sys.stderr)
        sys.exit(1)

    print(f"  {len(staged)} file(s) staged")

    # ── Handle ZIPs ──────────────────────────────────────────────────────────
    _handle_zips(case_dir, staged, args.password)

    # ── Run the pipeline ─────────────────────────────────────────────────────
    out_dir = settings.output_folder_path
    out_dir.mkdir(parents=True, exist_ok=True)

    print("Running pipeline...")
    process_case(job_id, str(case_dir))

    # ── Check result ─────────────────────────────────────────────────────────
    from app.pipeline.orchestrator import read_job_state

    state = read_job_state(job_id)
    status = state.get("status", "Unknown")

    if status == "DONE":
        case_out = out_dir / state.get("case_id", job_id)
        if case_out.exists():
            print(f"\nDone! Output: {case_out.resolve()}")
        else:
            print(f"\nDone! Check output directory: {out_dir.resolve()}")
    else:
        error = state.get("error", "Unknown error")
        print(f"\nPipeline failed at step: {state.get('step', '?')}", file=sys.stderr)
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
