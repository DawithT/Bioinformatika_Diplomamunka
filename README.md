# 🏥 CirrhosisRegistry — Local Clinical Data Extraction System

> **Privacy-first, offline-capable pipeline** that transforms heterogeneous Hungarian medical documents (discharge summaries, lab reports, endoscopy/fibroscan/imaging findings, microbiology cultures) into a structured, research-grade wide-table dataset — without any data ever leaving the local machine.

---

## Overview

Clinical data collection for liver cirrhosis research is bottlenecked by manual transcription from heterogeneous source documents. This application automates that process by:

1. Accepting a **case document bundle** (discharge summary + any combination of ancillary reports)
2. Routing each document to the correct extraction pipeline (hard/deterministic for labs and events; LLM-assisted for narrative fields)
3. Merging all sources according to a **Discharge-First hierarchy of truth**
4. Exporting a validated, annotated wide-format dataset

All processing runs **locally (CPU-only)**. No patient data is transmitted to any external service.

---

## Key Features

| Feature | Detail |
|---|---|
| **Privacy-first** | 100% local inference; no cloud API calls required |
| **Discharge-First merge** | Zárójelentés is the master record for narrative / scoring fields; ancillary docs enrich or verify, never overwrite silently |
| **Lab-report-authoritative** | When discharge and lab report disagree on a lab value, the lab report wins (the discharge summary usually just quotes the lab) — policy promoted at merge time |
| **Multi-discharge chronology** | Ward transfers produce multiple discharge summaries — orchestrator orders them by admission date, builds a combined anchor (first admission → last discharge), and routes fields by timepoint: `anam`/`adm` from the first summary, `dis` from the last, `inhosp` from all |
| **LLM-based gender deduction** | Patient gender inferred from the Hungarian name using the `-né` suffix rule and first-name patterns. Skipped entirely in runs without the LLM |
| **Dynamic lab columns** | `creatinine_d1`, `creatinine_d2_2`, … generated deterministically from admission anchor |
| **Event-based imaging/endoscopy** | Multiple studies per admission captured as separate columns with date + result |
| **Registry-driven fields** | All soft fields are defined in `Registry_base.xlsx` — no hardcoded schema |
| **Uncertainty-aware** | Cells are `Unknown` / `Not applicable` / `Uncertain` — never silently empty |
| **Nine output artifacts** | Wide table (xlsx + csv + label sheet) · Conflict report · Patient mismatch report · Out-of-window history/follow-up · Unit mismatch audit · Ancillary/unclassified document audit · Run configuration (`run_config.json`) |
| **Pre-flight checks** | Verifies Python packages, Ghostscript, LLM server, and config files before each run, with Windows-specific install hints (Memurai/WSL/Docker for Redis, PATH tips for Ghostscript). The web UI additionally checks Redis reachability and that at least one RQ worker is attached to the `default` queue; the CLI skips those since it runs the pipeline in-process |
| **Honest job status** | If the RQ queue cannot accept a job (Redis down, worker crashed) the upload returns `503` with the failure reason and cleans up staged files — never a fake `200 {job_id}`. A run blocked by the pre-flight check (`400`) cleans them up too |
| **No outdated worker** | A worker that was not restarted after a code or `.env` change refuses jobs instead of processing them with the old code, and the upload page warns about it (and about a missing worker) — see "Outdated worker" below |
| **Killed workers and leftovers** | A worker killed mid-job (window closed, crash) is recognised by its PID; the next worker start unregisters it, fails the interrupted case and deletes its documents. Staged uploads older than 24 h and job-state files older than 7 days are deleted automatically |
| **Registry routing validation** | Unknown `DocType` tokens in the `routing` column of `Registry_base.xlsx` are caught at startup instead of silently disabling a field |
| **Job-scoped prompt cache** | LLM answers are cached in memory for one job only and discarded with it — nothing derived from patient text is written to disk, and a re-run (or benchmark run) never reuses an earlier answer. Runs without the LLM never read the cache |
| **Visible configuration** | The UI shows the configuration a run will use (LLM on/off, model, server, temperature, max tokens, retries, cache mode, registry hash) in a read-only **Rendszerbeállítások** panel, and every result folder records it in `<case_id>_run_config.json`. The LLM can be switched off for a single run from the UI |
| **Chosen destination** | Before starting a run the user picks the folder the results are written to (typed, or via **Tallózás…** which opens a native folder dialog on the machine running the server), plus an optional ZIP archive. The job card then reports the exact path — no download step, so patient data is never copied into an unmanaged `Downloads` folder |
| **Data safety** | Uploaded patient documents are automatically deleted after processing. The output files are **not anonymised** — the wide table contains name, date of birth and TAJ — and are written unencrypted, so save them to an access-controlled location |
| **Pluggable document format** | Handles true PDFs, DOCX, and ZIP-wrapped scan bundles (JPEG + TXT) |
| **Password-protected ZIPs** | Encrypted ZIP detection + two-phase unlock flow; password never persisted |
| **Three ways to run** | Web UI (drag-and-drop, background queue), headless `cli.py` for servers without a GUI, or Docker Compose for dev/prod parity — see [Run the Application](#run-the-application) |
| **One-command startup** | `python start.py` (or double-click `start.bat` on Windows) brings up Redis, the RQ worker and Flask together and opens the browser, replacing the three-terminal startup |

---

## Architecture at a Glance

```
Browser (Dropzone UI)
        │  multipart upload
        ▼
Flask App  ──── /upload ──▶  Pre-flight check ──▶  RQ Worker (Redis)
                                                         │
                                    ┌───────────────────┼────────────────────┐
                                    ▼                    ▼                    ▼
                             Step 1-3              Step 4-5            Step 6-7
                          Ingest + Route        Normalise +          Merge + Rules
                          + Anchor              Extract (hard+soft) (provenance-aware)
                                                     │
                                                     ▼
                                                Step 8: Export → <destination>/<case_id>/
    wide.xlsx · labels.xlsx · wide.csv · conflicts.xlsx · mismatch.xlsx · history.xlsx
                                     · unit_mismatches.xlsx · ancillary_docs.xlsx
                                                     │
                                                     ▼
                                           Cleanup: delete uploaded docs
```

Full architecture detail: see [ARCHITECTURE.md](./docs/ARCHITECTURE.md)

---

## Project Structure

```
cirrhosis_registry/
├── app/
│   ├── __init__.py               # App factory
│   ├── blueprints/
│   │   └── main.py               # Upload, status, folder-picker endpoints + Dropzone UI (GET /)
│   ├── domain/
│   │   ├── models.py             # CaseBundle, RoutedDoc, ExtractedField, CaseResult
│   │   └── enums.py              # DocType, FieldSource, Timepoint, JobStatus
│   ├── pipeline/
│   │   ├── orchestrator.py       # Pipeline runner (steps 1-8) + job state + data cleanup
│   │   ├── preflight.py          # Pre-flight environment check (packages, tools, LLM, config)
│   │   ├── step1_ingest.py       # Multi-format document loader (PDF/DOCX/ZIP)
│   │   ├── text_normalize.py     # Hungarian mojibake folding (õ→ő, û→ű) applied at ingest
│   │   ├── routing_rules.py      # Declarative router rule set (keywords, weights, definitives)
│   │   ├── step2_route.py        # Document classifier engine (applies routing_rules)
│   │   ├── step3_anchor.py       # Patient fingerprint + admission anchor
│   │   ├── step4_normalize.py    # Timestamp → day-number conversion
│   │   ├── step5_extract/
│   │   │   ├── hard_lab.py       # Deterministic lab parser (regex + Camelot table)
│   │   │   ├── hard_events.py    # Endoscopy / fibroscan / imaging / microbiology block parser
│   │   │   ├── hard_scores.py    # Clinical scores (Child-Pugh, MELD, Maddrey, CLIF-C) + vital signs
│   │   │   ├── hard_structured.py # BNO/ICD-10 diagnosis codes + virology serology (HEURISTIC)
│   │   │   ├── ancillary.py      # AMBULANSLAP / UNKNOWN document audit record builder
│   │   │   ├── units.py          # Unit normalisation, mismatch detection, magnitude conversion
│   │   │   ├── gender.py         # LLM-based Hungarian-name gender deduction (-né rule + first name)
│   │   │   ├── _utils.py         # Shared helpers for the extractors
│   │   │   ├── soft_llm.py       # LLM-assisted soft-field extractor (orchestration)
│   │   │   └── prompts.py        # Prompt construction, JSON parsing, enum validation
│   │   ├── step6_merge.py        # Hierarchy-of-truth merge + conflict detection
│   │   ├── zip_handler.py        # Encrypted ZIP detection, document-bundle extraction
│   │   └── step8_export.py       # Wide table + labels + CSV + 5 audit reports → folder
│   ├── registry/
│   │   ├── loader.py             # Loads + validates Registry_base.xlsx
│   │   ├── models.py             # FieldDefinition dataclass
│   │   └── rules_engine.py       # Derives + applies not-applicable rules from registry (Step 7)
│   ├── services/
│   │   ├── llm_client.py         # HTTP transport for local LLM (Ollama/llama.cpp/disabled)
│   │   ├── cache.py              # Per-job in-memory prompt cache
│   │   ├── run_config.py         # Run configuration: GUI panel, cache scope, run_config.json
│   │   ├── code_version.py       # Code fingerprint: refuse jobs on an outdated worker
│   │   └── workers.py            # RQ worker registry: dead/outdated workers, page warnings
│   ├── extensions.py             # Redis connection + RQ queue initialisation
│   ├── templates/
│   │   └── index.html            # Single-page Dropzone UI (all UI logic is inline here)
│   └── static/
│       └── vendor/               # Dropzone.js + CSS, vendored (no CDN — works offline)
├── data/
│   ├── uploads/                  # Transient upload staging
│   ├── jobs/                     # Per-job state JSON files
│   └── out/                      # Final exports (per job_id)
├── config/
│   └── Registry_base.xlsx        # Field registry (source of truth for soft fields)
├── tests/
│   ├── conftest.py               # Data folders in a temp dir per run — tests never touch data/
│   ├── unit/                     # Parser unit tests (lab, event, day-calc)
│   ├── integration/              # Pipeline integration tests
│   └── fixtures/                 # Anonymised sample documents (ZIP-wrapped PDFs)
├── docs/
│   └── ARCHITECTURE.md
├── config.py                     # Pydantic Settings (env-driven)
├── .env.example                  # Template for .env — copy and edit
├── cli.py                        # Command-line entry point (no Flask/Redis required)
├── worker.py                     # RQ worker entry point
├── run.py                        # Flask dev server
├── start.py                      # One-command launcher: Redis + worker + Flask + browser
├── start.bat                     # Windows double-click wrapper around start.py
├── Dockerfile                    # Image for web + worker (Option C)
├── docker-compose.yml            # Redis + web + worker (+ optional Ollama profile)
├── pytest.ini                    # Test discovery configuration
└── requirements.txt
```

---

## Setup

### Prerequisites

- Python 3.11+
- Redis (local, for job queue — web UI only):
  - Linux: `sudo apt install redis-server` · macOS: `brew install redis`
  - Windows has no native Redis build. Install **Memurai** (Redis-compatible, runs as an always-on Windows service) from an **elevated** terminal — the MSI needs administrator rights and fails with exit code 1603 without them:
    `winget install Memurai.MemuraiDeveloper`
    Alternatives: WSL2 (`wsl --install`, then install redis-server inside it) or Docker Desktop (Option C below).
- A local LLM server — **Ollama** (recommended) or **llama.cpp server**
- **Ghostscript** (for PDF table extraction via Camelot):
  - Windows: download from https://www.ghostscript.com/releases/gsdnld.html and add `bin/` to PATH
  - Linux: `sudo apt install ghostscript`
  - macOS: `brew install ghostscript`

> **Not sure what's installed?** Both the CLI and web UI run a pre-flight environment check before each pipeline run. Missing dependencies are reported with install instructions and their impact on output quality. You can choose to continue without them.
>
> The check is scoped to the entry point: the CLI skips Redis and RQ-worker checks (it runs the pipeline in-process), while the web UI runs them since uploads require a working job queue. Install hints adapt to your OS — Windows users are pointed at Memurai / WSL / Docker for Redis and reminded that the Ghostscript installer does not add `bin/` to PATH automatically.

### Installation

```bash
git clone https://github.com/your-org/cirrhosis-registry
cd cirrhosis-registry
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # edit LLM_BASE_URL and REDIS_URL if non-default
```

### Choosing and Starting the LLM

> **Recommended model: `Qwen2.5-7B-Instruct` (Q4_K_M quantisation)**
>
> This is the best fit for this project. Reasons:
> - **Best-in-class multilingual performance at 7B** — Qwen 2.5 significantly outperforms Mistral-7B and Llama-3.1-8B on non-English languages including Hungarian
> - **Strong instruction-following** — reliable structured JSON output, fewer hallucinations on medical terminology
> - **16 GB RAM budget** — Q4_K_M quantisation uses ~4.5 GB VRAM/RAM, leaving ample headroom for the Flask process and OS
> - **128k context window** — handles long discharge summaries without truncation
>
> Mistral-7B-Instruct and Llama-3.1-8B-Instruct are viable fallbacks, but their Hungarian comprehension is measurably weaker.

```bash
# Install Ollama (recommended runner — handles quantisation automatically)
# macOS/Linux:
curl -fsSL https://ollama.com/install.sh | sh

# Pull the recommended model (~4.5 GB download)
ollama pull qwen2.5:7b-instruct-q4_K_M

# Start the server (runs at http://localhost:11434 by default)
ollama serve
```

Set in `.env`:
```dotenv
LLM_PROVIDER=ollama
LLM_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=qwen2.5:7b-instruct-q4_K_M
```

### Run the Application

There are two ways to use the application: the **web UI** (browser-based) or the **command-line interface**.

#### Option A: Web UI (Flask + Redis)

Requires Redis for the background job queue.

**One command (recommended):**

```bash
python start.py          # Windows: or just double-click start.bat
```

`start.py` starts Redis (only if it is not already running and a `redis-server`
binary is on PATH — on Windows Redis is normally an always-on service such as
Memurai), then the RQ worker, then Flask, and opens the browser. Ctrl+C stops
everything it started. It refuses to start if something is already serving port
5000, because Windows will happily let a second server bind a port that is
already listening and then serve half the requests from the stale process.

`start.bat` picks the interpreter that actually has the dependencies installed —
useful where the default `python` is a different version from the one the
project was installed into.

**Manually, in separate terminals:**

```bash
# Terminal 1 — Flask app
python run.py

# Terminal 2 — RQ worker
python worker.py
```

Navigate to `http://127.0.0.1:5000`

> **Offline-capable UI:** the browser UI has no CDN dependencies — Dropzone.js is vendored locally in `app/static/vendor/` and served by Flask, so the UI renders identically on Linux and Windows and on machines without internet access. (Earlier versions loaded Dropzone/Bootstrap from cdnjs with invalid SRI integrity hashes, which made browsers block the scripts and left the upload page non-functional.)

> **Upload field name:** the browser posts every file under the multipart field `files`. Dropzone's `paramName` is configured as a **function** (`paramName: () => 'files'`) rather than the plain string `'files'` — with `uploadMultiple` enabled, a string `paramName` makes Dropzone rename the parts to `files[0]`, `files[1]`, ... which `request.files.getlist("files")` does not see, so every upload failed with *"No files provided."* while the file chips were visibly queued. `POST /upload` now accepts both spellings, so a stale cached page still works.

> **Why not `flask run` / `rq worker`?** `flask run` requires `FLASK_APP=run.py` in the environment, and the plain `rq worker` CLI bypasses the `create_app()` bootstrap (registry loading, structlog configuration) that `worker.py` performs. On Windows, `worker.py` additionally selects `rq.SimpleWorker` because `rq.Worker.work()` relies on `os.fork()`, which does not exist on Windows — using the plain `rq worker` CLI there will crash the first time a job arrives.

> **Outdated worker:** a running worker keeps the code it imported at start. After an edit, `git pull` or branch switch it would quietly produce results from the old code (Flask reloads itself, the worker does not), and a worker left open in another terminal keeps taking jobs from the same queue. `app/services/code_version.py` fingerprints `app/**/*.py`, `config.py`, `worker.py` and `.env`; the worker carries its startup fingerprint in its RQ name (`crreg.<fingerprint>.<random>`). When the files on disk no longer match, `process_case()` marks the job FAILED with *"The worker runs older code or settings than the files on disk…"* (the uploaded documents are still deleted), and the upload page shows a warning naming the worker's PID. Restart with `start.bat` / `python start.py` and reload the page. `Registry_base.xlsx` is not part of the fingerprint — every job reloads it.

> **Killed worker:** a worker that is closed with the window's X, crashes or is terminated never unregisters itself, and on Windows (`SimpleWorker`) RQ keeps a worker that dies mid-job listed for up to an hour (job timeout + 60 s). `app/services/workers.py` checks the PID of every registration made on this host: dead ones are ignored by the upload-page warning and the pre-flight check, and the next worker start unregisters them, marks the case each was running as FAILED (*"The worker stopped while processing this case…"*) and deletes its staged documents.

> **Automatic cleanup:** at worker start and after every job, staged case folders in `data/uploads/` older than 24 hours (a password-protected upload whose password was never given, a case whose worker died) and job-state files in `data/jobs/` older than 7 days are deleted (`prune_expired()` in `orchestrator.py`). A password-protected ZIP is deleted as soon as it is unlocked; its extracted documents go with the case folder after the run.

> **Windows job timeouts:** `worker.py` uses `WindowsSimpleWorker`, which sets `death_penalty_class = TimerDeathPenalty`. RQ's default death penalty arms `signal.SIGALRM`, which does not exist on Windows, so *every* job used to die with `AttributeError: module 'signal' has no attribute 'SIGALRM'` raised before the job function was entered. RQ recorded the job as failed with no traceback, the pipeline never ran, and the UI showed the case as "Ismeretlen" with an empty progress bar forever. Linux and macOS are unaffected and still use the forking `rq.Worker`.

> **Documents per case:** the UI caps one case at 100 files (`MAX_FILES_PER_CASE` in `index.html`), which feeds **both** Dropzone's `maxFiles` and `parallelUploads`. They must never diverge: with `uploadMultiple`, Dropzone only sends the first `parallelUploads` queued files per request, so a lower `parallelUploads` splits one patient across two `case_id`s — or drops the surplus silently once `successmultiple` clears the queue. Files Dropzone refuses are removed from the on-screen queue and reported in a single summary alert, so a rejected document can never sit there looking like it was submitted.

#### Option B: Command Line (no Flask/Redis required)

Process files directly from the terminal. Only Python + the LLM server are needed.

```bash
# One or more document files
python cli.py zarojelentes.pdf labor.pdf endoszkopia.pdf

# A directory containing all documents for one case
python cli.py path/to/case_folder/

# Encrypted ZIP archive
python cli.py encrypted_bundle.zip --password mysecret

# Custom output directory + disable LLM (hard extraction only)
python cli.py docs/*.pdf --output-dir results/ --no-llm

# Parallel hard extraction (overrides HARD_EXTRACTION_THREADS; 1–16)
python cli.py case_folder/ --threads 4

# Verbose logging
python cli.py zarojelentes.pdf -v
```

Full option list: `inputs` (one or more files, or a single directory) plus
`--password`, `--output-dir`, `--no-llm`, `--threads/-t`, `--verbose/-v`.

The output folder is written to `data/out/<case_id>/` by default (override with `--output-dir`). The web UI asks for the destination per run instead — see [Where the results go](#where-the-results-go). Run `python cli.py --help` for all options.

#### Option C: Docker Compose (dev/prod parity)

One command starts Redis + Flask (gunicorn) + RQ worker in Linux containers on any host OS:

```bash
docker compose up --build
# with containerised Ollama instead of a host install:
LLM_BASE_URL=http://ollama:11434 docker compose --profile llm up --build
```

Under Compose the worker always runs the forking `rq.Worker` (Linux container), so Windows development exercises the identical code path as Linux production — the bare-metal `SimpleWorker` fallback in `worker.py` exists only for running natively on Windows without Docker. Patient data stays in the bind-mounted `./data` directory. *(The compose files are newly added and not yet build-verified on a Docker host.)*

---

## Configuration

All settings are controlled via `.env` (no secrets — all local):

```dotenv
# LLM — local or institutional servers only; public cloud APIs are refused
LLM_PROVIDER=ollama                          # ollama | llamacpp | disabled (CI/no-LLM)
LLM_BASE_URL=http://127.0.0.1:11434
LLM_ALLOWED_HOSTS=                           # institutional hosts on public IPs, comma-separated
LLM_MODEL=qwen2.5:7b-instruct-q4_K_M        # recommended; see Setup
LLM_CONTEXT_LIMIT=24000                      # NOT YET WIRED UP — see note below
ENABLE_LLM=true                              # false = no run may use the LLM (the UI switch cannot override it)
LLM_MAX_RETRIES=2                            # retry attempts on invalid LLM JSON (0–5)
LLM_REASONING_EFFORT=none                    # reasoning budget hint: high | medium | low | none
ENABLE_LLM_CACHE=true                        # per-job in-memory cache; false e.g. for benchmark runs

# Job queue
REDIS_URL=redis://localhost:6379/0

# Paths
UPLOAD_FOLDER=data/uploads
OUTPUT_FOLDER=data/out
JOBS_FOLDER=data/jobs
REGISTRY_PATH=config/Registry_base.xlsx

# Flask
SECRET_KEY=change-me-local-only               # session signing key; local use only
FLASK_ENV=development                         # development | production | testing

# Upload
MAX_UPLOAD_MB=200                            # maximum total upload size in megabytes

# Extraction
HARD_EXTRACTION_THREADS=1    # parallel threads for hard extraction (1–16)
```

> **`LLM_CONTEXT_LIMIT` is read from `.env` but not yet used.** The prompt context budget is the hardcoded `_MAX_CONTEXT_CHARS = 4000` in `app/pipeline/step5_extract/soft_llm.py` (characters, not tokens), so raising `LLM_CONTEXT_LIMIT` will not send more text to the model.

> **LLM endpoint guard.** Before any LLM client is created — and in the pre-flight check, before the server is even contacted — `LLM_BASE_URL` is verified: its host must be loopback or a private-network address (or resolve only to such addresses, e.g. Docker's `ollama`, an internal DNS name). An institutional server on a public IP range must be listed explicitly in `LLM_ALLOWED_HOSTS`. Ollama `-cloud` model tags are refused, since Ollama runs those on ollama.com. A refused endpoint blocks the run as a critical pre-flight issue.

> **Fixed LLM request parameters.** Temperature `0.0`, `max_tokens` 1000, a 300 s timeout per call, thinking off and JSON-object responses are set in code (`LLM_TEMPERATURE`, `LLM_MAX_TOKENS`, `LLM_TIMEOUT_S` in `app/services/llm_client.py`), not in `.env`: temperature 0 is what makes the same document give the same answer. The UI panel and `run_config.json` show these values as sent.

> **Prompt cache.** One cache per job, held in memory and discarded when the job ends. Its key covers the full prompt text (system prompt included) plus the model, request parameters and registry hash, so even within a job an answer is only reused under an identical configuration. A run without the LLM — `ENABLE_LLM=false`, `LLM_PROVIDER=disabled`, `--no-llm`, or the UI switch — neither reads nor writes it. `CACHE_DIR` and the old on-disk `data/cache` folder are no longer used; delete any leftover `data/cache` contents, which hold extracted answers from earlier runs.

Separately, `MAX_UPLOAD_MB` caps the **total request size** server-side. The browser additionally caps one case at `MAX_FILES_PER_CASE = 100` files (defined in `app/templates/index.html`).

---

## Data Dictionary

Fields in the wide output table follow a strict naming convention:

| Pattern | Example | Meaning |
|---|---|---|
| `{field_base}` | `alkohol_abuzus` | Anamnesis field (no suffix) |
| `{field_base}_adm` | `ascites_grade_adm` | Measured at admission |
| `{field_base}_inhosp` | `uj_aki_inhosp` | Occurred during hospitalisation |
| `{field_base}_dis` | `ascites_mennyisege_elbocsajtaskor_dis` | Status at discharge |
| `{analyte}_d{N}` | `creatinine_d1` | Lab value on day N |
| `{analyte}_d{N}_{n}` | `creatinine_d2_2` | Second draw of analyte on day N |
| `endo_{type}_d{N}_{n}_date` | `endo_gastro_d1_1_date` | Endoscopy event date |
| `fibro_d{N}_{n}_E_kpa` | `fibro_d3_1_E_kpa` | Fibroscan stiffness |
| `{modality}_d{N}_{n}_result` | `uh_d2_1_result` | Imaging finding text |

**Canonical analytes recognised by the hard lab parser** (`app/pipeline/step5_extract/hard_lab.py` — `LAB_MAPPING` + `CATEGORICAL_LAB_MAPPING`):

- Chemistry: `sodium`, `potassium`, `chloride`, `urea` (KN/Karbamid-nitrogén collapsed here — both mmol/L in Hungarian labs), `creatinine`, `gfr_epi`, `bilirubin_total` / `_direct` / `_indirect`, `crp`, `albumin`, `total_protein`, `ast` (GOT), `alt` (GPT), `ggt`, `alp`, `ldh`, `amylase`, `lipase`, `glucose`, `triglyceride`, `cholesterol` / `hdl` / `ldl` / `non_hdl`, `uric_acid`, `ammonia`, `lactate`, `cpk`, `vitamin_b12`, `ferritin`, `folate`, `phosphate`, `calcium_ionized` / `_total` / `_normalized`, `magnesium`, `cholinesterase`, `hba1c` / `hba1c_ifcc`, `iron`, `transferrin` / `_saturation`, `soluble_transferrin_receptor`, `procalcitonin`, `troponin`, `tsh`
- Coagulation: `inr`, `pt_sec` (PI), `aptt_sec` (APTI), `tt_sec` (TI), `fibrinogen`, `d_dimer` — **mixing studies are intentionally excluded** (`APTI keverék`, `PI keverő plazma`, `TI keverék` are distinct clinical values that would corrupt the base-test column)
- CBC: `wbc`, `rbc`, `hemoglobin`, `hematocrit`, `mcv`, `mch`, `mchc`, `rdw_sd`, `rdw_pct`, `platelet`, `mpv`, `pct`, `pdw`, `plcr`, {`neutrophil`,`lymphocyte`,`monocyte`,`eosinophil`,`basophil`}×{`_pct`,`_abs`}, `nrbc_pct` / `nrbc_abs`
- Ascites fluid analysis: `ascites_total_cell_count`, `ascites_pmn_abs` (critical for SBP diagnosis), `ascites_pmn_pct`, `ascites_mn_abs`, `ascites_mn_pct` — kept separate from whole-blood canonicals. Ascites chemistry that the discharge summary writes as a note on the serum analyte (`Glükóz: lásd megjAsc: Glükóz: 7,4 mmol/L`) becomes `ascites_<analyte>` (`ascites_glucose`, `ascites_total_protein`, `ascites_albumin`, `ascites_ldh`, `ascites_amylase`, `ascites_lipase`, `ascites_triglyceride`, `ascites_wbc`, `ascites_cea`), never the serum column
- Serum total immunoglobulins: `serum_iga`, `serum_igg`, `serum_igm` (g/L, for autoimmune liver disease workup — distinct from viral-serology sub-class antibodies)
- Tumour markers: `cea`, `afp`, `ca_19_9`
- Urine dipstick chemistry: `urine_protein`, `urine_glucose`, `urine_bilirubin`, `urine_specific_gravity`, `urine_ph`, `urine_wbc`, `urine_blood`, `urine_ketones`, `urine_nitrite`, `urine_urobilinogen` — urine-sediment microscopy (`Viz. ül. *`) is intentionally excluded (qualitative / semi-quantitative; handled by soft-LLM path)
- **Categorical (binary) lab** — GI bleeding flag `fecal_occult_blood`, encoded as `1.0` (Pozitív / Pos / +) or `0.0` (Negatív / Neg / -) in the same `{analyte}_d{day}` column machinery — the numeric `0`/`1` is what downstream stats consumers (R / SPSS / STATA) expect.
- **Censored results** (`Ferritin: >2000.0`, `hTNI: < 10`) are kept as reported, as text: `ferritin_d1 = ">2000.0"`, `troponin_dm1 = "<10"`. Such a column is then text for that case; every other lab value is a number.
- **Print footers are ignored.** The per-page footer (`<name> - Esetszám: …`, `Nyomtatva: … Oldal n/m`) and the page-start `.` are removed before the lab and exam parsers read a document, so a page break inside a lab line or report loses nothing and the print timestamp is never taken as an exam date.
- **Viral serology is not a lab column.** Virology results are registry questions (`virologia_*_inhosp`, and the anamnesis etiology fields), so the lab parser skips virology blocks.

**Special cell values (all soft fields):**

| Value | Meaning |
|---|---|
| `Unknown` | Field was never successfully examined — the LLM was disabled or its call failed, or no extractor (regex or LLM) covered the field |
| `No Data` | The LLM examined the relevant document text and found no evidence for the field |
| `Not applicable` | Logically excluded by parent field answer (rules derived from registry) |
| `Uncertain` | Evidence found but ambiguous — requires manual review |

The `Unknown` / `No Data` distinction lets reviewers tell "nothing looked at this field" (`Unknown` — worth a manual check) apart from "the model looked and the document genuinely does not mention it" (`No Data`).

### Parent → Child Propagation

Many registry fields are conditional children of a parent field (e.g. `diabetes_tipusa` is a child of the diabetes-presence field). Propagation applies to all registry timepoints, including anamnesis:

| Parent timepoint | Parent value | Child value |
|---|---|---|
| `anam` / `adm` / `inhosp` | `Unknown` / `No Data` (not mentioned anywhere) | `Not applicable` — the dependent child fields cannot carry meaningful data when the parent condition/exam is unreported |
| `anam` / `adm` / `inhosp` | A concrete answer that excludes the child (e.g. "No gastroscopy") | `Not applicable` |
| any | Concrete answer that includes the child | child is extracted normally |

This is implemented by `rules_engine.apply()` in [app/registry/rules_engine.py](app/registry/rules_engine.py). The registry column driving the hierarchy is `condition_parent_field` (with `condition_operator` / `condition_value`). `condition_value` must be spelled exactly like one of the parent's allowed values (the Enum_sets label — e.g. `Egyéb`, not `egyéb` or `Egyéb (megnevezni)`). For a multiple-answer parent, `==` means "the answers include this value".

---

## Day Numbering Convention

- Admission date = **day 1**
- Day before admission = **day -1** (day 0 is never used)
- Formula: `day = delta_days + 1` where `delta ≥ 0`, else `day = delta`
- Lab columns are named dynamically by their exact day (`creatinine_dm1`, `creatinine_d1`, …); labs are not part of the registry `_adm` / `_inhosp` / `_dis` fields
- Day-keyed fields (labs and exam records) from an **ancillary** document that fall more than 48 h before admission or after discharge are out-of-window: they are not merged and are listed in `history_followup.xlsx`. The same document's registry answers still merge — e.g. a post-discharge ambulatory sheet that repeats the in-hospital FibroScan adds no `fibro_d10_*` columns, but its "FibroScan történt" answer still counts. Day-keyed fields of the discharge summary are never dropped.

---

## Document Types Supported

| Type constant | Examples |
|---|---|
| `DISCHARGE_SUMMARY` | Zárójelentés |
| `LAB_REPORT` | New format: `Laborvizsgálati eredmények` (definitive title). Old format: `L E L E T` title without the word `Labor`, or `Laboratóriumi Medicina` as site-of-measurement marker. LAB_REPORT PDFs are re-ingested with pdfminer after routing because pymupdf4llm's markdown-table reconstruction fragments rotated column labels (e.g. `Fehérvérsejt` → `|tje<br>s<br>r<br>é<br>v…`). |
| `ENDOSCOPY` | Gasztroszkópia, Kolonoszkópia |
| `ELASTOGRAPHY` | FibroScan, 2D/p-SWE shear-wave (subtypes `fibroscan`, `shear_wave`) |
| `IMAGING` | Hasi UH, Hasi CT, Mellkas RTG (subtypes `uh`, `ct`, `mri`, `rtg`) |
| `MICROBIOLOGY` | Tenyésztési lelet |
| `VIROLOGY` | Hepatitis szerológia, HCV/HBV PCR — recognised via `Virológia` / `Virológiai` (definitive pattern, prevents misrouting as LAB_REPORT) |
| `HISTOLOGY` | Szövettan, Biopszia (soft-only extraction — no hard parser) |
| `AMBULANSLAP` | Outpatient visit note (`Ambulánslap` / `Ambuláns kezelőlap`). Routed via a **soft-definitive** rule: the title wins unless a strong modality-specific signal (FibroScan, Gastroscopia, Colonoscopia, Patológiai Osztály, Ultrahang vizsgálat, CT/MRI vizsgálat…) is also present — in which case classification falls through to weighted scoring, where AMBULANSLAP receives a title bonus so *incidental* modality mentions (e.g. "Kontroll hasi ultrahang javasolt" in the recommendation) do not outweigh the title, while genuine modality reports (many modality keywords) still win. Robustness details: (1) the title check runs *before* the `Felvétel dátuma` / `Elbocsátás dátuma` discharge markers, so outpatient sheets carrying those header lines are not misrouted; (2) Latin-1 mojibake in broken PDF text layers (`KEZELÕLAP` with `õ`) and letter-spaced titles (`A M B U L Á N S  K E Z E L Ő L A P`) are normalised before matching; (3) the generic section header `Vélemény:` is **not** an endoscopy keyword (it appears in nearly every Hungarian outpatient document). |

---

## Output Files

Each completed job produces an output folder named after the case, inside the destination the user chose in the UI (`data/out/` by default, or `--output-dir` from the CLI) — so `<destination>/<case_id>/` containing:

| File | Contents |
|---|---|
| `{case_id}_wide_table.xlsx` | One row per patient case; all soft + hard fields as columns |
| `{case_id}_wide_table_labels.xlsx` | Two columns: `variable_name` \| `label` — for SPSS label import |
| `{case_id}_wide_table.csv` | UTF-8 with BOM. Default layout: `,` separator and `.` decimals (STATA / SPSS / R). With **CSV formátum: magyar Excel**: `;` separator and `,` decimals — what Excel with Hungarian regional settings expects (the default file there shows `6.4` as 4 June). The xlsx files are the same either way: numbers are number cells, and no cell is ever written as a formula (text starting with `=` is stored as text; a formula-like value used to make Excel report the file as damaged). |
| `{case_id}_conflicts.xlsx` | Fields where discharge summary and an ancillary doc disagree. **Fuzzy-equivalent values are not reported as conflicts**: case/whitespace variants, the same quantity in convertible units (`1 g` vs `1000 mg`, `1.5 g/dL` vs `15 g/L`) or with the unit omitted on one side (`75` vs `75.0 kPa`), one value being a long (≥20 char) prefix of the other (context truncation / quoted exam text), and near-identical long strings (≥92 % similarity — OCR noise). **Always strict conflicts**: date-like values, numerically different quantities, equal numerals under non-convertible units (`75 kPa` vs `75 mmHg`), and values whose clinical semantic markers differ (antonyms like *benignus/malignus*, *bal/jobb*, or a negation on only one side — these disable the fuzzy shortcuts). |
| `{case_id}_mismatch.xlsx` | Documents whose patient fingerprint does not match the master |
| `{case_id}_history_followup.xlsx` | Day-keyed fields of ancillary documents dated outside the admission window (one row per document and date, listing the dropped columns) |
| `{case_id}_unit_mismatches.xlsx` | Numeric fields whose extracted unit differed from the registry canonical unit; no auto-conversion — flagged for manual reconciliation. Columns: `case_id`, `field`, `expected_unit`, `seen_unit`, `raw_value`, `doc_type`, `page` |
| `{case_id}_run_config.json` | The configuration this run actually used: `llm.enabled` and, when on, provider, model, server URL, temperature, max tokens, reasoning effort, retries, context budget, timeout and a hash of the system prompts; whether the cache was on; registry file name and SHA-256; the CSV layout (`export.csv_format`); `generated_at` timestamp. No credentials |
| `{case_id}_ancillary_docs.xlsx` | Audit sheet for documents not extracted into the wide table: `UNKNOWN` (unclassified) and `AMBULANSLAP` (outpatient visits). For `AMBULANSLAP` docs, three narrative sections are additionally extracted: `VÉLEMÉNY` (opinion), `BEAVATKOZÁSOK` (procedures), and `DIAGNÓZISOK` (BNO codes as a JSON array). Columns: `case_id`, `filename`, `doc_type`, `routing_method`, `failure_reason`, `best_score`, `text_preview`, `velemeny`, `beavatkozasok`, `diagnoses_json` |

### Where the results go

The destination is chosen per run in the web UI, above the **Feldolgozás indítása** button:

- **Eredmények mentési helye** — the destination folder. Pre-filled with `OUTPUT_FOLDER` (default `data/out`), remembered in the browser between runs. Type an absolute path, or press **Tallózás…** to open a native folder dialog. Paths copied from Explorer with *Copy as path* (wrapped in quotes) are accepted as-is.
- **ZIP archívum is készüljön a mappába** — when ticked, `<case_id>_results.zip` is written *beside* the case folder, so the folder keeps exactly the nine exported files.
- **LLM használata ennél a feldolgozásnál** — ticked by default; untick to run this one case without the LLM (hard parsers only, LLM fields come out `Unknown`). It can only switch the LLM *off*: when `.env` disables the LLM, the box is greyed out. It is deliberately not remembered between page loads.
- **CSV formátum** — `nemzetközi` (default; `,` separator, `.` decimals, for R / STATA / SPSS) or `magyar Excel` (`;` separator, `,` decimals). Remembered in the browser; recorded in `run_config.json` under `export.csv_format`.
- **Rendszerbeállítások** — collapsible read-only panel with the configuration the run will use. Settings are changed in `.env`, not in the UI.

A run therefore leaves either:

```
<destination>/<case_id>/           ← the 9 files
<destination>/<case_id>_results.zip  ← only when the ZIP option is ticked
```

The destination is validated *before* any file is saved: it must be an absolute path to an existing, writable directory, otherwise the upload is rejected with `400` and nothing is staged. When the app runs in Docker the path must be one *inside* the container (`./data` is bind-mounted to `/app/data`), and the **Tallózás…** button is unavailable there — type the path instead.

Once a job reaches `DONE` the job card scrolls into view and shows the full path with an **Útvonal másolása** (copy path) button.

---

## Web Endpoints

| Method | Path | Description |
|---|---|---|
| GET | `/` | Single-page Dropzone upload UI, including the read-only configuration panel |
| GET | `/health` | Health check |
| GET | `/preflight` | Pre-flight environment check (JSON report of missing dependencies). For manual/diagnostic use — the UI does not call it; `POST /upload` runs the same check itself |
| POST | `/upload` | Upload documents; runs pre-flight check; returns `{job_id, case_id}` (+ `warnings[]` if any). Optional form fields: `dest_dir` (absolute destination folder), `make_zip` (`"true"`/`"false"`), `use_llm` (`"false"` runs this case without the LLM; absent = on) and `csv_format` (`"intl"` default / `"hu"`) |
| POST | `/upload/unlock` | Supply password for encrypted ZIP, then enqueue. Takes `dest_dir`/`make_zip`/`use_llm`/`csv_format` too — the browser re-sends them, so no per-case state is held on the server between the two requests |
| POST | `/browse-folder` | Open a native folder-picker on the machine running the server; returns `{path}` (`""` if cancelled). `403` for non-local requests, `503` where no dialog is possible (no tkinter, headless host, Docker) — the UI then falls back to typing the path |
| GET | `/status/<job_id>` | Poll job progress. On `DONE` the payload also carries `out_path` (and `zip_path` when a ZIP was requested) |
| GET | `/download/<job_id>` | Download result files as ZIP (created on-the-fly). **Not used by the UI** — results are written straight to the chosen folder. Kept for the case where the app is reached from another machine |

---

## Password-Protected ZIP Uploads

The upload endpoint handles encrypted ZIP archives with a two-phase flow:

1. **POST /upload** — files are saved; if any ZIP is encrypted, the response returns `{"status": "password_required", "case_id": "...", "protected_files": [...]}`
2. **POST /upload/unlock** — the UI sends `{"case_id": "...", "password": "..."}` to decrypt and extract the archive, then enqueue processing

**Security properties:**
- The password is a local variable in the request handler; it is never logged, stored in job metadata, or persisted to disk
- ZIP-slip protection: member filenames are flattened to basename; path traversal sequences are rejected
- Uses `pyzipper` for AES-256 + ZipCrypto support; falls back to stdlib `zipfile` (ZipCrypto only)

**Current limitations:**
- Single password per upload (all encrypted ZIPs in one upload must share the same password)
- No recursive ZIP extraction (nested ZIPs inside extracted archives are not handled)
- Synchronous unlock/extraction (large encrypted ZIPs block the request thread)

---

## Registry-Driven Design

The application's soft-field schema is entirely driven by `config/Registry_base.xlsx`:

- **Adding a new field**: add a row to the Excel — no code changes required
- **Changing enum values**: update the `Enum_sets` sheet
- **Changing hierarchy rules**: edit the `condition_parent_field` / `condition_operator` / `condition_value` columns in `Registry_base.xlsx`. Rules are derived from the registry in memory at pipeline start — no regeneration step.
- The field registry is loaded and validated at startup; a schema mismatch raises a clear error
- The `routing` column is validated strictly at startup: every pipe-separated token must resolve to a known `DocType`. Typos like `LAB_REPORRT` or stale aliases now surface as hard errors instead of silently making the field ineligible for all ancillary documents
- Soft warnings (an unresolved `condition_parent_field` reference, or a `condition_value` that is not one of the parent's allowed values) do not block startup; affected wide-table cells are flagged with a `REGISTRY_WARNING:` prefix in the export so non-coding users can locate the offending row
- The answer options the application uses come from the **Enum_sets** labels. The Main sheet's `allowed_values` column is a duplicate kept for readability; a startup warning reports any field whose column differs from its Enum_sets labels, or whose `enum_set` does not exist (the only case where the column is used instead)

---

## Technology Stack

| Layer | Choice | Rationale |
|---|---|---|
| Web framework | Flask 3 + App Factory + Blueprints | Lightweight, well-understood |
| Job queue | RQ + Redis | Persistent across restarts; survives dev server reload |
| LLM runner | Ollama (recommended) | One-command install; handles quantisation; runs fully locally |
| LLM model | Qwen2.5-7B-Instruct Q4_K_M | Best multilingual (Hungarian) at 7B; fits 16 GB RAM |
| LLM client | stdlib http.client, no vendor SDK | Raw HTTP to `LLM_BASE_URL` using the standard `/v1/chat/completions` request format, which Ollama and llama.cpp both serve locally |
| LLM validation | Manual JSON parse + enum check | extract_json_from_response() + validate_enum_fields(); retry with error feedback |
| Config validation | Pydantic v2 (pydantic-settings) | Typed settings from .env; validated at startup |
| PDF parsing | PyMuPDF → pdfminer fallback | Best-in-class text/table extraction |
| Table extraction | Camelot (lattice) | Structured lab table grids |
| Excel I/O | openpyxl | Wide table + registry loading |
| Output format | xlsx (wide, flat) | Compatible with Excel, R (readxl), SPSS, STATA |
| Logging | structlog | Structured, correlatable log lines |
| Testing | pytest | Unit + integration |

---

## Assumptions & Constraints

> These are confirmed design constraints, not defaults to adjust:

- **Deployment:** single local machine, one user at a time — no authentication, no multi-tenancy
- **Privacy:** no data ever leaves the machine (or the institution's own servers); public cloud LLM APIs are strictly prohibited
- **LLM:** local only — Ollama serving `qwen2.5:7b-instruct-q4_K_M` on `localhost`; llama.cpp is a drop-in alternative via `LLM_PROVIDER=llamacpp`
- **Hardware:** 16 GB RAM; the recommended model uses ~4.5 GB, leaving ~11 GB for the OS and application
- **Language:** Hungarian medical documents; all parsing patterns, keyword routing, and LLM prompts are Hungarian-aware
- **Output consumers:** Excel for manual review; R (`readxl`), SPSS, STATA for statistical analysis — column names are kept `snake_case`, ≤ 64 characters, no special characters, to satisfy STATA's variable name limits
- **Registry:** `Registry_base.xlsx` is the current baseline — the schema is intentionally extensible without code changes

---

## License

Internal research use only. Not for clinical decision-making.
