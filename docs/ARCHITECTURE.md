# Architecture — CirrhosisRegistry

## 1. System Context

```
┌─────────────────────────────────────────────────────────────────────┐
│                        LOCAL MACHINE                                 │
│                                                                     │
│  ┌──────────┐    HTTP     ┌─────────────┐   enqueue   ┌──────────┐ │
│  │ Browser  │────────────▶│  Flask App  │────────────▶│  Redis   │ │
│  │(Dropzone)│◀────────────│  port 5000  │             │  queue   │ │
│  └──────────┘  JSON/SSE   └─────────────┘             └────┬─────┘ │
│                                                            │ dequeue│
│  ┌──────────┐             ┌──────────────────────────┐ ┌──▼───────┐ │
│  │  cli.py  │────────────▶│       File System        │ │ RQ Worker│ │
│  │(terminal)│  direct     │ data/uploads/  data/out/ │◀│(pipeline)│ │
│  └──────────┘  call       │ data/jobs/               │ └──┬───────┘ │
│                            └──────────────────────────┘    │         │
│                                                            │ HTTP    │
│  ┌──────────────────────┐   ┌──────────────────────────┐   │         │
│  │  Registry_base.xlsx  │   │  Local LLM Server        │◀──┘         │
│  │                      │   │  (Ollama / llama.cpp)    │             │
│  └──────────────────────┘   └──────────────────────────┘             │
└─────────────────────────────────────────────────────────────────────┘
```

**No data crosses the machine boundary at any point.**

Two entry points share the same pipeline orchestrator:

- **Web UI** (Flask + Redis + RQ): browser-based drag-and-drop upload, asynchronous background processing via RQ worker
- **CLI** (`cli.py`): direct synchronous call to `process_case()`, no Flask/Redis required

Both entry points run a **pre-flight environment check** (`app/pipeline/preflight.py`) before starting the pipeline, verifying that all required Python packages, system tools (Ghostscript), the LLM server, and configuration files are present. Critical issues block the run; warnings are shown with install instructions and the user can choose to continue with reduced capabilities.

---

## 2. Request / Response Flow

### 2a. Web UI flow

```
GET /preflight  ──▶  { ok, can_run, issues[] }
                     (manual/diagnostic use — the UI does not call it;
                      the same check runs inside POST /upload)

User uploads files
      │
      ▼
POST /upload  ──▶  validate extensions
                   validate dest_dir (absolute, exists, writable) — before saving
                   save to data/uploads/<case_id>/ (absolute path)
                   run pre-flight check
                   ├── critical issue → 400 { error, preflight }
                   ├── warnings → include in response { job_id, case_id, warnings[] }
                   ├── no encrypted ZIPs → write QUEUED state → enqueue job → Redis
                   │   ├── enqueue OK → 200 { job_id, case_id, warnings? }
                   │   └── enqueue failed (Redis down, RQ rejects) →
                   │       rmtree(case_dir); 503 { error, detail }
                   └── encrypted ZIP detected →
                       return { status: "password_required", case_id, protected_files }
                              │
POST /upload/unlock  ◀───────┘
    { case_id, password }
    decrypt + extract → enqueue pipeline job → Redis
    return { job_id, case_id }
      │
      ▼ (browser polls)
GET /status/<job_id>  ──▶  { status, step, progress, error?,
                             out_path?, zip_path? }   ← paths once DONE

(job completes — uploaded docs are deleted from data/uploads/)

The UI shows out_path as its completion message; there is no download step.
GET /download/<job_id> still exists for access from another machine.
```

**Destination, LLM switch and CSV format.** `POST /upload` carries four
optional form fields: `dest_dir` (absolute path), `make_zip`, `use_llm`
(`"false"` runs this case without the LLM; absent = on) and `csv_format`
(`"intl"` default, or `"hu"` — see the wide_table.csv entry in the export
section).  An unknown `csv_format` is a 400.  `use_llm` can only switch the LLM
off — `ENABLE_LLM=false` / `LLM_PROVIDER=disabled` cannot be overridden from
the browser — and with it off pre-flight skips the LLM-server check.
`_resolve_destination()` validates the folder `_resolve_destination()` validates the folder
*before* any file is staged — an absolute path to an existing, writable
directory, or `400` with a message for the user; empty falls back to
`settings.output_folder_path`. The path is deliberately unsandboxed: choosing
where results land is the point of the field, and `run.py` binds 127.0.0.1.
`POST /upload/unlock` takes the same three fields, re-sent by the browser, so
no per-case state is held between the two requests.

**Configuration panel.** `GET /` renders a read-only **Rendszerbeállítások**
panel from `app/services/run_config.describe_run()` — the same dict that
scopes the prompt cache and is written to `<case_id>_run_config.json` (§ 9c,
§ 12), so the panel, the cache and the output cannot disagree.  Nothing is
editable there: model, server and request parameters stay in `.env` / code.

**Folder picker.** A web page cannot obtain a real folder path — the File
System Access API is Chromium-only and returns a handle, not a path, and
`webkitdirectory` exposes only relative names. `POST /browse-folder` therefore
opens a native Tk dialog *on the machine running the server*, in a subprocess
of its own (Tk must own the main thread, which it can never do inside a Flask
request thread). Local requests only (`403` otherwise), and `503` wherever no
dialog is possible — no tkinter, headless host, Docker — where the UI falls
back to a typed path.

Both entry points (`/upload` and `/upload/unlock`) go through the same
`_enqueue()` helper, so both write the `QUEUED` job-state file before handing
the job to Redis — see § 15.

Password handling: the password is accepted as a request-local variable, used
only during `extract_zip()`, and never logged, stored in job metadata, or
persisted to disk.  `pyzipper` handles AES-256 + ZipCrypto; stdlib `zipfile`
is used as a fallback (ZipCrypto only).

### 2b. CLI flow

```
python cli.py file1.pdf file2.pdf [--password PWD] [--output-dir DIR]
      │
      ▼
  Validate inputs exist
  Pre-flight environment check
      ├── critical issue → abort with install instructions
      └── warnings only → show impacts, ask "Continue? [y/N]"
      │
      ▼
  Stage files → data/uploads/<case_id>/
  Handle ZIPs (decrypt if --password, expand bundles)
      │
      ▼
  process_case(job_id, case_dir)     ← same orchestrator as the web path
      │                                 runs synchronously in-process
      ▼
  Output folder → data/out/<case_id>/
```

The CLI calls `process_case()` directly in the current process.
No Flask application context, Redis, or RQ worker is needed.
The `--no-llm` flag sets `ENABLE_LLM=false` before importing config,
allowing hard-extraction-only runs on machines without an LLM server.

---

## 3. Eight-Step Pipeline

Each step is an independent, testable module. Steps are run sequentially by the orchestrator. Failures in individual document processing are isolated — one bad file does not abort the case.

```
┌──────────────────────────────────────────────────────────────────┐
│                    PIPELINE ORCHESTRATOR                          │
│                                                                  │
│  Pre-flight  CHECK    Verify packages, tools, LLM, config       │
│     │                                                            │
│  Step 1   INGEST      Load files → normalise to text + tables   │
│     │                                                            │
│  Step 2   ROUTE       Classify each doc → DocType enum          │
│     │                                                            │
│  Step 3   ANCHOR      Extract patient fingerprint + dates        │
│     │                 from DISCHARGE_SUMMARY                     │
│  Step 4   NORMALISE   Compute day-number for every event         │
│     │                 (called inline by parsers — no separate     │
│     │                 orchestrator step)                          │
│  Step 5   EXTRACT     Hard (lab/event) + Soft (LLM) extraction  │
│     │                                                            │
│  Step 6   MERGE       Discharge-First hierarchy + conflict log   │
│     │                                                            │
│  Step 7   RULES       Not-applicable propagation (from registry)│
│     │                                                            │
│  Step 8   EXPORT      wide.xlsx + labels.xlsx + wide.csv        │
│     │                 + 5 audit reports + run_config.json (9)   │
│     │                                                            │
│  Cleanup  SAFETY      shutil.rmtree(case_dir) in finally block  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. Domain Models

```python
@dataclass(frozen=True)
class CaseBundle:
    case_id: str
    files: tuple[Path, ...]    # tuple for immutability / hashability

@dataclass(frozen=True)
class RoutedDoc:
    path: Path
    doc_type: DocType           # DISCHARGE_SUMMARY | LAB_REPORT | ENDOSCOPY | …
    pages: tuple[str, ...]      # per-page text (used for section-aware prompting)
    tables: tuple[tuple, ...]   # extracted table grids (for lab parser)
    meta: dict[str, Any]        # e.g. {'date': '2026-01-19', 'lab_name': 'Bartók HQ'}

@dataclass
class ExtractedField:
    key: str                 # final column name, e.g. 'creatinine_d2'
    value: object            # str | float | int | None
    source: FieldSource      # HARD | HEURISTIC | SOFT
    doc_type: DocType
    confidence: float        # 0.0–1.0
    page: int | None
    engine: str | None       # 'regex' | 'camelot' | 'llm' | 'block_parser' | 'scores_parser' | …
    unit_seen: str | None    # unit string from the source document (None if absent/non-numeric)
    unit_ok: bool | None     # None = not evaluated; True = matches registry; False = mismatch

@dataclass(frozen=True)
class ConflictRecord:
    case_id: str
    field: str
    source_policy: str               # DISCHARGE_PRIMARY | EXAM_DOC_PRIMARY
    retained_value: Any              # the value that was kept
    retained_doc_type: DocType       # source of the retained value
    overridden_value: Any            # the value that was discarded
    overridden_doc_type: DocType     # source of the overridden value
    page: int | None

@dataclass(frozen=True)
class UnitMismatchRecord:
    case_id: str
    field: str
    expected_unit: str       # registry canonical unit
    seen_unit: str           # unit found in source document
    raw_value: str           # extracted value string (unconverted)
    doc_type: DocType
    page: int | None

@dataclass(frozen=True)
class AncillaryRecord:
    case_id: str
    filename: str
    routing_method: str
    failure_reason: str      # 'classified_ancillary' for AMBULANSLAP; routing reason for UNKNOWN
    best_score: int          # highest keyword-score the router achieved
    text_preview: str        # first 200 chars of document text
    doc_type: DocType        # AMBULANSLAP | UNKNOWN
    velemeny: str | None     # VÉLEMÉNY section text (AMBULANSLAP only)
    beavatkozasok: str | None  # BEAVATKOZÁSOK section text (AMBULANSLAP only)
    diagnozisok: tuple[dict, ...]  # [{"code": str|None, "description": str}, …] from DIAGNÓZISOK

@dataclass
class CaseResult:
    case_id: str
    fields: dict[str, ExtractedField]   # key → highest-confidence ExtractedField
    conflicts: list[ConflictRecord]
    mismatches: list[MismatchRecord]
    out_of_window: list[OutOfWindowRecord]
    unit_mismatches: list[UnitMismatchRecord]
    ancillary_docs: list[AncillaryRecord]
    warnings: list[str]
```

**Merge rule:** `set_field(fields, key, ef)` only replaces an existing value if `ef.source` has higher priority (`HARD > HEURISTIC > SOFT`), then by confidence as tiebreaker.

**Conflict resolution:** Depends on `source_policy` from the registry:
- `DISCHARGE_PRIMARY` → discharge summary value wins; ancillary value recorded in conflicts.xlsx
- `EXAM_DOC_PRIMARY` → exam document value wins; discharge value recorded in conflicts.xlsx

---

## 5. Step Detail: Ingest (Step 1)

**Input:** raw file path  
**Output:** `RoutedDoc` (text + tables)

```
file
 ├─ zipfile.is_zipfile()?
 │    └─ YES → extract TXT pages (sorted) + JPEG pages (for OCR fallback)
 │              assemble text = "\n\n--- PAGE N ---\n\n".join(pages)
 │
 ├─ .pdf extension?
 │    ├─ pymupdf4llm.to_markdown()          ← primary
 │    ├─ fallback: pdfminer text extract    ← if pymupdf fails
 │    └─ table extract: camelot lattice     ← separate pass for grids
 │
 └─ .docx extension?
      └─ python-docx paragraph + table extract
```

Every extraction failure is caught, logged with structlog, and produces a `RoutedDoc` with `pages=[]` + a warning. The pipeline continues.

**LAB_REPORT pdfminer re-ingest (post-routing):** pymupdf4llm's markdown-table reconstruction misreads rotated column headers in structured lab-report PDFs — e.g. `Fehérvérsejt` is emitted as vertical single-character cells `|tje<br>s<br>r<br>é<br>v<br>r<br>é<br>h<br>e<br>F|`, which breaks analyte-name matching. After Step 2 classifies a PDF as `LAB_REPORT`, the orchestrator calls `reingest_pdf_with_pdfminer(doc)` (exported from `step1_ingest.py`) which rebuilds the `RoutedDoc` using pdfminer's layout engine, preserving the original `doc_type`, tables, and meta but replacing the text pages. `meta["engine"]` flips to `"pdfminer"` and the old engine value is kept as `meta["engine_prev"]`. The routing classifier still runs on the pymupdf4llm output because the section-title keywords (`Laborvizsgálati eredmények`, `L E L E T`) are preserved even when the tables are fragmented — it's only the row-label column that's destroyed.

---

## 6. Step Detail: Route (Step 2)

Rule-based classifier. No LLM used here — it would be wasteful and non-deterministic for routing.

The classifier combines **definitive patterns** (short-circuit: first match wins) with **weighted keyword scoring** (fallback). Definitive patterns are used for titles that unambiguously identify a document type; the weighted scorer handles everything else. A minimum score threshold (`min_score = 2`) prevents false positives from incidental mentions in the weighted phase.

**Declarative rule set / engine split:** all rule *data* — keyword groups + weights, the two definitive tiers, the Ambulánslap title & modality-override patterns, filename hints, thresholds — lives in `app/pipeline/routing_rules.py` as a frozen `RoutingRuleSet` (`DEFAULT_RULES`), validated at import (bad regexes / empty keyword lists / invalid weights fail fast). `step2_route.py` is a pure engine that compiles and applies whatever rule set it is given, so a future rule source (JSON file, learned weights from an ML router) can replace `DEFAULT_RULES` without touching engine logic.

**Text normalisation:** the common Hungarian PDF mojibake `õ→ő` / `û→ű` (broken CP1250→Latin-1 text layers render `KEZELŐLAP` as `KEZELÕLAP`) is folded **once at ingest** (`app/pipeline/text_normalize.py`, applied to all pages and table cells in `step1_ingest`), so every downstream consumer — router, anchor regexes, lab/event parsers, ancillary section extraction, LLM context — sees clean text. The router folds again defensively because `classify()` also receives raw strings directly (tests, CLI probes). Note: `ftfy` was evaluated and rejected for this — õ/û are valid characters, so there is no detectable encoding error for it to repair (verified empirically with ftfy 6.3.1); the deterministic fold is lossless because the Hungarian alphabet contains no õ/û.

**Definitive patterns** (evaluated in tiers):

| Tier | Pattern | DocType | Notes |
|---|---|---|---|
| 1 (before AMBULANSLAP title) | `zárójelentés` | DISCHARGE_SUMMARY | unambiguous title — never appears on an outpatient sheet |
| 2 (after AMBULANSLAP title) | `felvétel dátuma`, `elbocsátás dátuma` | DISCHARGE_SUMMARY | header lines that *also* appear on some Ambuláns kezelőlap forms — must not steal a document carrying the outpatient title |
| 2 | `virológiai?` | VIROLOGY | disambiguates from LAB_REPORT — virology sub-reports often share the lab-report chassis |
| 2 | `laborvizsgálati eredmények` | LAB_REPORT | new-format lab report title |

**Soft-definitive (AMBULANSLAP title):** after the tier-1 definitive but **before** the tier-2 definitives and keyword scoring, the router checks for `Ambuláns kezelőlap` / `Ambulánslap` / `Ambuláns lap` (including letter-spaced variants like `A M B U L Á N S  K E Z E L Ő L A P`). If present *and* no strong modality-specific signal is also present (`fibroscan`, `tranziens elasztográfia`, `SWE`, `gastroscopia`, `colonoscopia`, `endoszkópos lelet/labor`, `patológiai osztály`, `biopszia típusa`, `hisztológiai diagnózis`, `mikrobiológiai laboratórium`, `ultrahang vizsgálat`, `hasi ultrahang`, `CT vizsgálat`, `MRI vizsgálat`, `virológia`, `laborvizsgálati eredmények`), the document is classified as AMBULANSLAP. Otherwise the router falls through so the modality-specific/definitive rule can win — this preserves correct routing for Fibroscan / endoscopy / imaging reports that are printed on an outpatient-visit header. When scoring runs for a document that *does* carry the title, AMBULANSLAP receives a **title bonus** (+4) so an incidental modality mention (e.g. `Kontroll hasi ultrahang javasolt` in the recommendation section) cannot outweigh the title, while a genuine modality report (many modality keywords) still wins. The scoring phase alone mis-classified documents like `bőr.pdf` (a dermatology visit that quotes "szövettani lelet" in its follow-up plan) as HISTOLOGY, which is why the soft-definitive exists.

**Keyword hygiene:** keyword lists are deduplicated case-insensitively at compile time (matching is `re.IGNORECASE`, so `Ambulánslap` + `ambulánslap` used to double-count every hit), and the generic section header `Vélemény:` was removed from the ENDOSCOPY keywords — it appears in nearly every Hungarian outpatient document and misrouted Ambuláns kezelőlap sheets to ENDOSCOPY.

**Weighted keyword scoring** (after definitive patterns). Each keyword hit adds the rule's weight to that DocType's score; the highest score wins if it reaches `min_score`. The authoritative lists are the `keyword_rules` of `DEFAULT_RULES` in `routing_rules.py` — the table below shows the weights and a few representative keywords per type, in rule order:

| DocType | Weight / hit | Representative keywords |
|---|:---:|---|
| DISCHARGE_SUMMARY | 3 | `ZÁRÓJELENTÉS`, `Felvétel dátuma`, `Elbocsátás dátuma`, `Záródiagnózis` |
| LAB_REPORT | 3 | `Laborvizsgálati eredmények`, `Laboratóriumi Medicina`, `L E L E T`, `Ref.tart`, `giga/L` |
| ELASTOGRAPHY | 2 | `FibroScan Expert`, `Tranziens elasztográfia`, `Shear wave`, `SWE`, `kPa`, `F0`–`F4` |
| ENDOSCOPY | 3 | `Gastroscopia`, `COLONOSCOPIA`, `Endoszkópos lelet`, `Premedicatio` |
| IMAGING | 2 | `Ultrahang vizsgálat`, `hasi ultrahang`, `CT vizsgálat`, `MRI vizsgálat`, `Röntgen` |
| MICROBIOLOGY | 3 | `Tenyésztés`, `Mikrobiológiai`, `Hemokultúra`, `Kórokozó` |
| VIROLOGY | 3 | `Virológiai`, `Szerológia`, `HBsAg`, `anti-HCV`, `HCV-RNS` |
| AMBULANSLAP | 3 | `Ambulánslap`, `Ambuláns kezelőlap`, `Járóbeteg` |
| HISTOLOGY | 3 | `Szövettani`, `Patológiai`, `Biopszia`, `Hisztológiai` |

**Old-format lab reports:** the PDFs produced by the older LIS export do not contain the word *Labor* in their title — they are headed `L E L E T` and carry `Laboratóriumi Medicina` as the site-of-measurement marker. Both tokens are included in the LAB_REPORT keyword list so these older reports still route correctly.

**Critical ordering:** ELASTOGRAPHY entries are evaluated before IMAGING entries. FibroScan / shear-wave reports contain ultrasound vocabulary, so on a score **tie** the rule listed first wins — IMAGING would take the tie if it came first.

**Subtype detection:** after the primary DocType is chosen, a second pass assigns a `routing_subtype` (e.g. `fibroscan` vs `shear_wave` for ELASTOGRAPHY, `ct`/`uh`/`mri`/`rtg` for IMAGING, `gastroscopy`/`colonoscopy` for ENDOSCOPY). Subtypes are stored in `RoutedDoc.meta["routing_subtype"]` and drive per-field eligibility via the registry `routing` column.

When the text layer is absent (e.g. fully rasterised scan), routing falls back to filename heuristics (case-insensitive substring match on the file stem). A filename containing `záró` or `zaro` — which includes `zárójelentés` / `zarojelentes` — is routed as `DISCHARGE_SUMMARY`.

---

## 7. Step Detail: Anchor (Step 3)

Extracts from DISCHARGE_SUMMARY:

```python
@dataclass
class PatientAnchor:
    name: str
    dob: date
    taj: str                         # TAJ szám
    admission_start: datetime
    discharge_end: datetime
    fingerprint: str                 # sha256(name + dob + taj)[:12]
```

All other documents are checked against `fingerprint`. Mismatch → document is skipped, recorded in `mismatch.xlsx`.

**Parsing strategy:** deterministic regex on known header patterns in Zárójelentés (e.g. `Felvétel dátuma: YYYY.MM.DD HH:MM`, Hungarian dot-date + 24-hour time). Supports both old format (`Beteg neve: ...`) and new format (`Név: ...`) for patient name extraction. No LLM.

### Multi-discharge chronology

A single admission can produce **multiple discharge summaries** when the patient is transferred between wards — each ward issues its own Zárójelentés, with the discharge date of the earlier summary aligning with the admission date of the next one.

The orchestrator handles this as follows:

1. Extract a `PatientAnchor` from **every** discharge summary, skipping any that lack a parseable anchor (warning logged).
2. Sort the per-discharge anchors by `(admission_start, discharge_end)` — natural ward-transfer order.
3. Build a **combined anchor** via `PatientAnchor.create(...)` using the first summary's admission info and the last summary's discharge info:
   - `name`, `dob`, `taj`, `admission_start` ← first summary
   - `discharge_end` ← last summary
   - `fingerprint` ← recomputed from the combined fields
4. Record a `MismatchRecord` for any summary whose `name`/`dob`/`taj` disagree with the first summary (identity drift detection across transfers).
5. Build a `discharge_order: dict[Path, int]` that maps every parsed discharge to its chronological index.

### Timepoint-based field filter for multi-discharge

When `n_discharges > 1`, fields extracted from each summary are filtered by registry `timepoint` so the wide table reflects the correct moment:

| timepoint | Kept from … |
|---|---|
| `anam` | First discharge only |
| `adm` | First discharge only |
| `inhosp` | All discharges |
| `dis` | Last discharge only |

Fields whose key cannot be resolved to a timepoint (dynamic lab / event keys) are kept from every summary — they are already day-indexed and will be deduplicated downstream.

### Gender deduction

Gender (`a_beteg_neme`) is deduced from the anchor name by a small **LLM-based module** (`app/pipeline/step5_extract/gender.py`, cache key `GENDER_V1::{name}`). The system prompt encodes the Hungarian `-né` wife-of suffix rule plus first-name patterns and emits `Férfi` / `Nő` / `Unknown`. A successful verdict is added as a HARD `ExtractedField` (confidence 0.90, engine `gender_llm`); `Unknown` / disabled-client / placeholder-name cases fall through to the normal soft-extraction pathway so the value can still come from narrative text.

> **Routing gotcha:** Zárójelentés embeds full endoscopy result sections in its body text. The text classifier alone can score it as ENDOSCOPY. The router guards against this with a Tier-1 definitive pattern: `zárójelentés` in the document text assigns `DocType.DISCHARGE_SUMMARY` before keyword scoring runs, ensuring the anchor is always extracted from the correct document. The filename (`záró` / `zaro`) is only consulted when the text layer yields no classification.

---

## 8. Step Detail: Day Normalisation (Step 4)

```python
def compute_day(event_date: date, admission_start: date) -> int:
    delta = (event_date - admission_start).days
    return delta + 1 if delta >= 0 else delta
    # admission day = 1, day before = -1, day 0 never occurs
```

Lab values are never mapped to registry fields: they always go to dynamically named columns carrying their exact day (`creatinine_dm1` for the day before admission, `creatinine_d1`, …). The registry `_adm` / `_inhosp` / `_dis` fields hold clinical findings (microbiology, virology, histology, etc.), not lab values.

**±48 h window:** `classify_window()` uses it only to decide whether data from an *ancillary document* belongs to this admission. A date more than 48 h before admission is `PRE_ADMISSION`, one after discharge is `POST_DISCHARGE`. At merge, the day-keyed fields (labs, exam records) of an ancillary document that fall outside the window are dropped and listed in `history_followup.xlsx`; its registry fields still merge (see "Out-of-window fields" in the merge section). The window is fixed (`_ADMISSION_WINDOW_DAYS` in `step4_normalize.py`).

---

## 9. Step Detail: Extraction (Step 5)

### 9a. Hard Lab Extraction

Two sub-parsers run in sequence; results are merged:

**TextLabParser** — regex on inline lab strings in Zárójelentés and standalone lab reports:
```
"GLUC: 6.4; NA: 132; CL: 96; K: 3.9; KREA: 108; ..."
```

The TextLabParser handles two formats:
1. **Inline block format** (Zárójelentés pages 3-5) — lab header lines like `LMI Központi Laboratórium (KIZKEMIA) 2026.01.13 09:21 (Mintavétel: 2026.01.13 08:00)` followed by semicolon-separated analyte values. Uses the `Mintavétel` date when present; falls back to the header completion date. An empty `(Mintavétel: )` is consumed with the header (it used to stay attached to, and lose, the first analyte).
2. **Plain-text table format** (standalone lab reports) — `Minta dátuma:` header provides the sample date; `Vizsgálat Eredmény` marks the table start. Date fallback chain: `Minta dátuma` → `Mintavisszaigazolás` → `Megjelenés ideje`.

Both old-format (date-first: `2018.10.03 07:26 - Hematológia`) and new-format (`LMI Központi Laboratórium … 2026.01.13 09:21`) Zárójelentés headers are supported.

The orchestrator runs `TextLabParser` on **both** `LAB_REPORT` and `DISCHARGE_SUMMARY` documents, so inline lab blocks embedded in discharge summaries are extracted alongside standalone lab reports.  It passes the text through `strip_print_furniture()` (`_utils.py`): the per-page footer (`<name> - Esetszám: …`, `Nyomtatva: … Oldal n/m`) and pymupdf4llm's page-start `.` are removed, so a page break inside a lab line no longer splits an analyte's name from its value.

**Values:** `parse_lab_value()` — a plain number becomes a float; a censored result keeps its qualifier and is stored as text as reported (`>2000.0`, `< 10` → `"<10"`), in all three paths (inline, plain table, Camelot).

**TableLabParser** — Camelot lattice on structured lab PDF tables:
```
| Fehérvérsejt szám | 5.87 | 4.50-10.80 | giga/L |
```

Both normalise analyte names through `LAB_MAPPING` (Hungarian → canonical code). Output: `{analyte_code}_d{day}` keys, with `_2`, `_3` suffixes for multiple draws on the same day. `LAB_MAPPING` covers Hungarian primary names, their Latin/English synonyms, and the short abbreviations that appear on structured lab-report grids (e.g. `KREA`, `FVS`, `HGB`, `HCT`, `PLT`, `CHOL`, `TG`, `AP`, `ALB`, `NEU%`/`NEU#`, `LYM%`/`LYM#` and so on) — sourced from the on-site analyte vocabulary in `cross_check/Lab values.txt`.

**Collapsing policy** (single canonical where clinically identical in Hungarian labs):
- `Urea` and `Karbamid-nitrogén` / `KN` → `urea` (both reported in mmol/L; do **not** convert to US-standard BUN mg/dL)
- `GOT` → `ast`, `GPT` → `alt` (old German clinical names)

**Separation policy** (distinct canonicals to preserve clinical meaning):
- Ascites fluid cells (`TC-BF#`, `PMN#`, `PMN%`, `MN#`, `MN%`) live under `ascites_*` canonicals, **never** folded into whole-blood differential — absolute PMN count is the sole diagnostic criterion for Spontaneous Bacterial Peritonitis in cirrhosis
- Ascites chemistry written as a note on the serum analyte (`Glükóz: lásd megjAsc: Glükóz: 7,4`, `Carcino-embrionális antigén: Lásd megj.Ascites CEA: 0,2`) is matched by `_ASCITES_NOTE_RE` and stored as `ascites_<canonical of the outer name>` — never in the serum column
- Bare `IgA` / `IgG` / `IgM` (measured in g/L) map to `serum_iga` / `serum_igg` / `serum_igm` for autoimmune-liver-disease workup, kept distinct from viral-serology sub-class antibodies like `anti-CMV IgG`

**Intentional exclusions** (skipped even when present in the document):
- Coagulation mixing studies (`APTI keverék`, `PI keverő plazma`, `TI keverék` / `Protrombin idő keverék` / `Trombin idő keverék`) — a distinct lab value that would silently corrupt the base-test column
- Urine-sediment microscopy (`Viz. ül. *` — amorphous, bacteria, crystals, cylinders, epithelial cells) — qualitative / semi-quantitative; the soft-LLM path handles these
- `20-as Polibrén TI` — niche specialty test

**Categorical (binary) analytes** — `CATEGORICAL_LAB_MAPPING` + `CATEGORICAL_VALUE_MAP`:

Fecal occult blood is reported qualitatively (Pozitív / Negatív / POS / NEG / + / -) but flags variceal-bleeding risk. The inline block parser tries `_INLINE_ANALYTE_RE` (strict numeric) first, then falls back to `_INLINE_LOOSE_VALUE_RE` + `CATEGORICAL_LAB_MAPPING` + `CATEGORICAL_VALUE_MAP` which encodes the raw value as `1.0` (positive) or `0.0` (negative) and stores it through the same `{canonical}_d{day}` column machinery. Covered canonical: `fecal_occult_blood`.

**Viral serology is deliberately not collected as day-named lab columns.** Virology results are registry questions: the in-hospital fields `virologia_*_inhosp` are filled by the LLM (discharge summary results section and standalone `VIROLOGY` documents), and the anamnesis etiology fields by `VirologyParser` (§ 9g). A virology block inside a discharge summary is therefore skipped by the lab parser.

### 9b. Hard Event Extraction

Block-based deterministic parser per event type (`app/pipeline/step5_extract/hard_events.py`):

| Event type | Trigger pattern | Extracted fields | Column prefix |
|---|---|---|---|
| Endoscopy (gastro/colono) | `Endoszkópos lelet:` | date, result block, opinion block | `endo_gastro`, `endo_colono` |
| Fibroscan | `FibroScan Expert :` or `{date} Fibroscan` | date, E_kpa, CAP_dbm, IQR_pct, steatosis_grade, fibrosis_stage | `fibro` |
| Imaging UH/CT/RTG/MRI | `Ultrahang`, `CT`, `Röntgen`, `MR`/`MRI`/`MRT` headers | date, result text block | `uh`, `ct`, `rtg`, `mri` |
| Microbiology | `Bakterológia` / `MIKROBIOL` header lines, or lines starting with `Tenyésztés` (a result sentence such as `Gomba tenyésztése negatív.` is not a header) | date, sample_type, pathogen, multiresistant | `micro` |

Events that appear multiple times per admission get index suffixes: `endo_gastro_d1_1_*`, `endo_gastro_d3_2_*`.

**Text and dates in the discharge-summary layout** (pymupdf4llm markdown):
- All four parsers read `strip_print_furniture(doc.pages)`. The footer's print timestamp (`UD MED - 2026.03.31 22:33`) used to be the "last date + time before the block" whenever a report started after a page break, dating a 2026-01-13 gastroscopy to day 79.
- Result and opinion text stop at the next section heading also in its markdown form (`## **GAD5 - …**`, `## **2026.01.16 Fibroscan**`) — `_SECTION_HEADING`; before, an ultrasound result ran on into the gastroscopy. The wrapped tail of an imaging header (`## **részlelet) Leletező orvos: …**`) is dropped, and `Leletező orvos: …` (the reporting physician) is never part of stored text. The length cap is 4000 characters, only as a guard.
- Microbiology dates: a labelled `Minta dátuma:` / `Felvétel dátuma:` before the header, else the date on the header line itself, else the nearest date before it. The header's own date must beat the backward search, which in a discharge summary lands in the previous section (the echo report).

**DISCHARGE_SUMMARY embedded extraction:** the orchestrator runs all four event parsers against discharge summaries in addition to the document-type-specific parser for standalone event documents. Each parser returns `[]` if no relevant block is found.

**HISTOLOGY** documents are routed correctly but have no hard parser — soft-only LLM extraction handles them.

### 9c. Soft Field Extraction (LLM)

**Registry-driven**: fields are grouped by `section_hint` from `Registry_base.xlsx`. Each group is sent as one focused prompt — not the entire document at once.

**Source-policy + routing filter:** Before prompting, fields are filtered by `source_policy` and by the registry-driven `routing` column:
- `DISCHARGE_ONLY` → extracted only from DISCHARGE_SUMMARY documents
- `EXAM_DOC_PRIMARY` → extracted from DISCHARGE_SUMMARY and from any exam doc whose (DocType, subtype) matches the field's `routing` entry (exam value wins on conflict)
- `DISCHARGE_PRIMARY` → extracted from DISCHARGE_SUMMARY and from any exam doc whose (DocType, subtype) matches the field's `routing` entry (discharge value wins on conflict)

Routing entries take the form `TYPE` or `TYPE:subtype` (pipe-joined for multi-doc fields, e.g. `DISCHARGE_SUMMARY|IMAGING:ct|IMAGING:mri`). Eligibility is resolved by `FieldDefinition.is_eligible_for(doc_type, routing_subtype)` — no hard-coded routing maps in code.

**Layering:**
- `llm_client.py` — transport only: `chat(system, user) -> str` via stdlib `http.client`
- `prompts.py` — prompt construction (`build_user_prompt`), JSON extraction (`extract_json_from_response`), enum/date validation (`validate_enum_fields`)
- `soft_llm.py` — orchestration: section grouping, page selection, cache, retry loop, field mapping
- `app/services/cache.py` — per-job in-memory prompt cache

```
┌─────────────────────────────────────────────────────┐
│  System prompt (fixed)                               │
│  ─ Hepatology assistant, Hungarian documents         │
│  ─ Return ONLY valid JSON                            │
│  ─ Use Unknown / Not applicable / Uncertain          │
│  ─ Season normalisation (tavasz→04, nyár→07, etc.)   │
│                                                      │
│  User prompt (per section group)                     │
│  ─ Relevant document section text (page selection)   │
│  ─ Fields to extract (from registry, this group)     │
│  ─ Enum constraints, date format hints, list flags   │
└─────────────────────────────────────────────────────┘
          │
          ▼  llm_client.chat() — raw HTTP POST
    Raw JSON string from LLM
          │
          ▼  extract_json_from_response() — manual JSON parse
          ▼  validate_enum_fields() — enum + partial-date + list validation
    { field_name: value | "No Data" | "Not applicable" | "Uncertain" }
          │
          ▼  PromptCache (in memory, this job only)
    reused only for an identical prompt under the same configuration
```

**Missing-value markers (`_map_to_fields`):** when a real LLM answered the section call, any field it omitted, returned empty, or explicitly answered `"Unknown"` for is stored as **`"No Data"`** (the model examined the text and found nothing). When the LLM never examined the document — `DisabledClient`, or the call failed / stayed unparseable after all retries — the marker is **`"Unknown"`**. This lets reviewers distinguish "nothing looked at this field" from "the document genuinely does not mention it".

**Prompt cache — per job, in memory.** `_llm_for_run()` in the orchestrator creates one `PromptCache` per job; it lives only as long as that `process_case()` call, so cached answers are discarded together with the uploaded documents and nothing derived from patient text is written to disk. Keys are sha256 of a *scope* plus the full prompt text (the system prompt included). The scope is `describe_run()` serialised — model, provider, server, temperature, max tokens, reasoning effort, retries, context budget, prompt hash, registry SHA-256 — so an answer is only ever reused under an identical configuration. `ENABLE_LLM_CACHE=false` switches it off (e.g. benchmark runs). Within a job a hit only happens when the same prompt recurs, e.g. two documents with identical text.

**No cache without the LLM.** When the run has no LLM (`ENABLE_LLM=false`, `LLM_PROVIDER=disabled`, `--no-llm`, or the UI switch), the cache is created disabled, and `SoftExtractor._extract_section()` additionally refuses to read it whenever its client is a `DisabledClient`. Before this, the cache was global and on disk (`data/cache`), and a no-LLM run silently returned answers from an earlier LLM run — e.g. a gender the documents never state.

**Retry logic:** on validation failure (invalid enum value, unparseable JSON), the error messages are appended to the next prompt as correction feedback. The number of retries is `LLM_MAX_RETRIES` (default 2, allowed 0–5).

**HISTOLOGY documents** have no hard parser — they fall through to soft-only LLM extraction, which is the intended design.

### 9d. Gender Deduction (HARD)

`app/pipeline/step5_extract/gender.py` produces a HARD `ExtractedField` for `a_beteg_neme` by asking the LLM to classify the Hungarian patient name. The answer is cached for the job under the normalised name (`GENDER_V1::{name}`); the call — and the cache lookup — is skipped when the client is a `DisabledClient` or the name is a placeholder (`UNKNOWN`, `xxx`, empty). The result — `Férfi`, `Nő`, or `None` — is added to `anchor_fields` before the per-document soft extraction begins. When the LLM can't decide, the field falls through to the soft extractor so it can still be read from the narrative.

### 9e. Multi-Discharge Timepoint Filter

When multiple discharge summaries are present (ward transfers), the orchestrator calls `_filter_discharge_by_timepoint(fields, idx, last_idx, timepoint_map)` on each summary's field list before handing it to the merge step. Fields with timepoint `anam` or `adm` are kept only when `idx == 0`; `dis` fields are kept only when `idx == last_idx`; `inhosp` (and any field whose key cannot be mapped to a timepoint) passes through unchanged. This ensures the combined anchor's admission-status fields come from the first summary and the discharge-status fields from the last summary, without duplicating values on transfer boundaries.

### 9f. Hard Scores & Vitals Extraction (HEURISTIC)

`app/pipeline/step5_extract/hard_scores.py` — `ScoresVitalsParser`

Extracts clinical scoring values and vital signs from discharge summaries. Source: `HEURISTIC`, confidence 0.85, engine `scores_parser`.

**Scores extracted:**

| Score | Example patterns |
|---|---|
| Child-Pugh class / points | `Child: C/11 pont`, `Child C stádiumú` |
| MELD / MELD-Na | `MELD-Na: 25 pont` |
| Maddrey discriminant function | `Maddrey: 37.6` |
| CLIF-C ACLF | `CLIF-C: 54` |

**Vital signs extracted:** systolic/diastolic BP, pulse rate, height, weight, SpO2, temperature — from both abbreviated (`RR: 112/91`, `P: 102/min`, `TM: 189`) and full-word forms (`Vérnyomás: 120/80 Hgmm`).

**Anamnézis exclusion:** any score/vital found inside the Anamnézis section (historical data) is dropped — the section boundary is detected by the same `_ANAM_END_RE` pattern used by `hard_structured.py`. This prevents historical scores (prior admissions) from overwriting admission-time registry fields.

### 9g. Hard Structured Extraction: BNO Codes + Virology (HEURISTIC)

`app/pipeline/step5_extract/hard_structured.py` — `BNOParser` + `VirologyParser`

Both parsers are source `HEURISTIC`, confidence 0.90.

**BNOParser** (`engine="bno_parser"`) — extracts BNO/ICD-10 diagnosis codes from the `Diagnózisok` / `BNO kód` table and maps known prefixes to registry field values (etiology, comorbidities, cirrhosis type, etc.). The table can appear anywhere in the document (old format: beginning; new format: end).

Diagnosis `Típus` column drives timepoint gating:

| Típus | Meaning | Target timepoint |
|---|---|---|
| 0, 1, 3 | Admitting / principal | `_adm` fields |
| 2, 4 | Transfer / complication | `_inhosp` fields |
| 5 | Comorbidity | etiology / anamnesis (any) |
| 6–8 | Death / autopsy | `_dis` fields |

Death / autopsy codes: 6 = immediate cause of death (clinical), 7 = underlying disease (pathological), 8 = immediate cause of death (pathological). The source system can also emit 9 (meaning unknown), `B` (underlying cause of death, clinical) and `C` (nosocomial complication); these are not mapped, and the row regex only reads a digit, so `B` / `C` rows are not parsed.

**VirologyParser** (`engine="virology_parser"`) — runs on discharge summaries only. Finds virology blocks (`MBV Mikrob. Virológiai Labor`, `Virológiai`, `Szerológia` headers), reads HBsAg / anti-HBc / anti-HCV / HCV RNS and interprets them into the **anamnesis etiology** fields: `hepatitis_b_virus_etiologia`, `hbv_statusz`, `hepatitis_c_virus_etiologia`, `hcv_statusz`. It does not fill the in-hospital virology fields (`virologia_*_inhosp`) — those come from the LLM extractor, which reads discharge summaries and standalone `VIROLOGY` documents according to the fields' registry `routing`.

Neither parser restricts itself to (or excludes) the Anamnézis section; they search the whole document text.

### 9h. Ancillary Document Handling

`app/pipeline/step5_extract/ancillary.py` — called during Step 2 (routing) for every `UNKNOWN` and `AMBULANSLAP` document.

Documents that cannot be routed to the extraction pipeline are not silently dropped. Instead, an `AncillaryRecord` is built and ultimately written to `ancillary_docs.xlsx` so reviewers can confirm nothing load-bearing was missed.

For `AMBULANSLAP` documents, three sections are extracted with regex:
- `VÉLEMÉNY` / `Összefoglaló` — narrative clinical opinion (raw text, truncated at 2000 chars)
- `DIAGNÓZISOK` — structured BNO codes + descriptions (JSON array); falls back to line-split raw text if no BNO patterns match
- `BEAVATKOZÁSOK` — narrative list of procedures (raw text, truncated at 2000 chars)

For `UNKNOWN` documents, the same extractors run best-effort; typically only the `text_preview` (first 200 chars) and routing-triage fields are populated.

### 9i. Unit Normalisation and Mismatch Detection

`app/pipeline/step5_extract/units.py`

Each `ExtractedField` carries two provenance fields: `unit_seen` (the unit string actually present in the source document) and `unit_ok` (tri-state comparison result). At the end of Step 6 (merge), the orchestrator compares `unit_seen` against the registry `unit` column for every field that has `unit_ok == False`.

**Normalisation before comparison:** µ/μ → `u`, degree sign dropped, whitespace collapsed, lowercased — so `µmol/L` and `umol/L` are treated as equivalent. `g/L` vs `g/dL` is a genuine mismatch (factor-of-10 magnitude difference) and is NOT normalised away.

**Policy: flag only, no conversion.** The raw value is kept in the wide table unchanged; the mismatch row in `unit_mismatches.xlsx` provides `expected_unit`, `seen_unit`, and `raw_value` for manual reconciliation.

A missing or blank `unit_seen` is treated as equivalent (the source document omitted the unit, which is common for structured lab grids that only print the unit in the header row).

---

## 10. Step Detail: Merge (Step 6)

```python
# Public API
master_fields, conflicts, mismatches, out_of_window = merge(
    docs: list[DocInput],   # DocInput wraps (doc_type, fields, doc_date, doc_fingerprint)
    anchor: PatientAnchor,
    case_id: str,
    registry: list[FieldDefinition],  # for source_policy lookup
)
```

Merge processes discharge documents first, then ancillary documents. For each field:

```
1. key not yet in master_fields → set (enrich)
2. values agree (or one side absent) → set (verify/upgrade source priority)
3. values disagree:
   a. source_policy == DISCHARGE_PRIMARY → keep discharge value, log conflict
   b. source_policy == EXAM_DOC_PRIMARY  → keep exam document value, log conflict
```

**LAB_REPORT promotion:** when an incoming field comes from a LAB_REPORT document and the registry policy is `DISCHARGE_PRIMARY`, the policy is promoted to `EXAM_DOC_PRIMARY` at merge time. This reflects the clinical reality that lab reports are the authoritative source for lab values — the discharge summary usually just quotes the lab. The promotion is data-driven on `ef.doc_type`, so we don't need to enumerate every analyte prefix in `_default_policy()`. `DISCHARGE_ONLY` fields are untouched (they never reach the merge loop from a lab report because the soft extractor filters them out upstream).

**Force-override on EXAM_DOC_PRIMARY:** the EXAM_DOC_PRIMARY branch assigns the new value directly (`master_fields[ef.key] = ef`) instead of going through `set_field()`. Policy has decided the winner, and `set_field`'s `is_better_than` priority check would keep the discharge value when both sides share the same source tier and confidence (e.g. both HARD/0.95, a common case for labs).

**Fingerprint mismatch:** ancillary documents carrying a `doc_fingerprint` that
differs from `anchor.fingerprint` are skipped entirely — all their fields are
discarded and logged as `MismatchRecord`.

**Out-of-window fields:** a day-keyed field (`{analyte}_d{day}[_{n}]`,
`{prefix}_d{day}_{n}_{field}`) of an ancillary document whose day is
`PRE_ADMISSION` or `POST_DISCHARGE` is not merged.  One `OutOfWindowRecord`
per (document, date) lists the dropped keys.  The document's registry fields
still merge: a post-discharge ambulatory sheet repeating the in-hospital
FibroScan adds no `fibro_d10_*` columns but still answers the FibroScan
question.  Discharge-summary fields are never dropped.

**Out-of-window documents:** a `DocInput` with a `doc_date` classified as
`PRE_ADMISSION` or `POST_DISCHARGE` is skipped entirely and logged as
`OutOfWindowRecord`.  The orchestrator does not set `doc_date` (no document
type carries one reliable date — `_doc_date()` reads metadata nothing fills),
so in the pipeline only the field-level rule applies.

Conflict records go to `conflicts.xlsx`.  The wide table stays clean.

**Absent values:** `None`, `""`, `"Unknown"`, `"No Data"`, `"NA"`, `"ERVENYTELEN"`, `"Not applicable"`, `"Uncertain"` are treated as absent — filling an absent field is *enrichment*, not a conflict.

**Fuzzy conflict filtering (`_values_conflict`):** values that differ only superficially are *not* reported as conflicts. After normalisation (strip, collapse whitespace, casefold), the following pairs are treated as agreement:

- the same physical quantity under **convertible units** (`1 g` vs `1000 mg`; `1.5 g/dL` vs `15 g/L`; `84 µmol/L` vs `0.084 mmol/L`) — magnitude conversion via `units.unit_to_base()`, which covers metric-prefixed mass / volume / amount / enzyme units and their `X/Y` compounds;
- the same leading number with the unit omitted on one side (`75` vs `75.0 kPa`; `1` vs `1.0`) — discharge summaries quote lab values without units;
- one value being a long (≥ 20 char) prefix of the other — context truncation, or a discharge summary quoting the exam text and appending further notes;
- near-identical long strings (≥ 12 chars, `difflib.SequenceMatcher` ratio ≥ 0.92) — OCR / punctuation noise.

Clinical-safety guards keep meaningful differences strict (conservative-conflict principle: a false conflict costs one reviewer glance; a false merge silently corrupts research data):

- **date-like values** (`YYYY-MM-DD` / `YYYY.MM.DD` shapes) are compared exactly;
- values whose **numbers differ** are always conflicts regardless of textual similarity (`75.0 kPa` vs `35`; `1 g` vs `500 mg` after conversion);
- equal numerals under **non-convertible units** are conflicts (`75 kPa` vs `75 mmHg`, `2 mg` vs `2 mmol`) — unknown units are never guessed;
- the **semantic guard** (`_SEMANTIC_MARKERS`): if the two values raise different clinical marker sets — antonym pairs like *benignus/malignus*, *bal/jobb*, *emelkedett/csökkent*, *pozitív/negatív*, *kompenzált/dekompenzált*, *akut/krónikus*, or a negation particle (*nem*, *nincs*, *sine*…) present on only one side — the prefix/similarity shortcuts are disabled and the pair is reported as a conflict. Long Hungarian findings differing only in one antonym can otherwise exceed the 0.92 similarity threshold.

Lexicon governance: additions to `_SEMANTIC_MARKERS` and `units._SIMPLE_UNIT_BASE` require a motivating test case; removals require a labeled evaluation.

---

## 11. Step Detail: Rules Engine (Step 7)

Not-applicable rules are derived directly from the registry's `condition_parent_field`, `condition_operator`, `condition_value` columns at pipeline start — in memory, every run. There is no YAML file to keep in sync.

The registry encodes the condition under which a child field IS applicable; `rules_engine.build_rules_from_registry()` stores the **negation** so that the engine fires when the child becomes NOT applicable. Concretely:

- Registry `condition_operator == "=="` → engine `operator: "!="`
- Registry `condition_operator == "!="` → engine `operator: "=="`
- Other operators are kept as-is and the engine refuses to fire them (a soft warning is logged).

Example: `hcv_statusz` has `condition_parent_field=hepatitis_c_virus`, `condition_operator="=="`, `condition_value="Igen"`. The derived rule fires when `hepatitis_c_virus != "Igen"`, setting `hcv_statusz` to `Not applicable`.

Applied after merge. Any field listed under `then_not_applicable` is set to `"Not applicable"` if the condition holds. This is idempotent and re-runnable.

**Comparison.** Values are compared exactly (after trimming), so `condition_value` must be spelled exactly like one of the parent's allowed values (its Enum_sets label) — `Egyéb` does not match `Egyéb (megnevezni)` or `egyéb`. A multiple-answer parent arrives as a JSON array string (`'["Laktulóz", "Rifaximin"]'`) and is compared by membership: registry `==` means "the list contains the value", `!=` means "it does not".

### Parent-child propagation semantics

The engine treats an **absent parent** (parent value in `_ABSENT_VALUES`, which includes `Unknown` and `No Data`) as follows:

| Parent timepoint | Parent absent | Child outcome | Rationale |
|---|---|---|---|
| `anam` / `adm` / `inhosp` | yes | `Not applicable` | When there is no information about whether the parent condition/exam was present or performed, the dependent child fields cannot carry meaningful data — marking dozens of child sub-fields as `Unknown` would flood the export without adding reviewable information. |
| any timepoint | parent has a concrete value that excludes the child | `Not applicable` | Standard conditional-field logic (e.g. `hepatitis_c_virus != "Igen"` → `hcv_statusz` is `Not applicable`). |

Historical note: `anam` parents were originally excluded from absent-parent propagation (absence of a historical mention does not prove absence of the event); per user guidance (2026-07) the parent-child indications now apply to anamnesis fields as well. When `apply()` is called *without* a registry (no timepoint information), absent parents never propagate.

---

## 12. Step Detail: Export (Step 8)

**wide_table.xlsx** (`{case_id}_wide_table.xlsx`)
- One sheet; one row per processed case
- **Column naming — R / SPSS / STATA compatible:**
  - `snake_case` only — no spaces, no special characters
  - Maximum 60 characters (STATA's hard limit is 32 for older versions, 64 for modern; we target 60 as a safe common denominator)
  - Negative day numbers encoded as `_dmN` (e.g. `creatinine_dm1` for day -1) to avoid the leading `-`
  - If truncation would create a duplicate, append `_{hash4}` suffix
- Column order: canonical fields (name, dob, taj) → anam → `_adm` → `_inhosp` → `_dis` → lab columns sorted by `(day, analyte, index)` → event columns
- Cell colour coding: yellow fill = `Uncertain` (manual review flag — Excel only)
- Frozen header row; auto-filter enabled; auto-fit column widths (capped at 40 chars)
- JSON array strings (list fields) and partial dates (YYYY, YYYY-MM) are kept as strings to prevent Excel auto-conversion

**wide_table_labels.xlsx** (`{case_id}_wide_table_labels.xlsx`) — `variable_name` | `label` for SPSS label import

**wide_table.csv** (`{case_id}_wide_table.csv`) — UTF-8 with BOM; same columns as xlsx.  Layout per run (`csv_format`, `CSV_FORMATS` in `step8_export.py`):
- `intl` (default) — `,` separator, `.` decimals; direct import into STATA / SPSS / R.
- `hu` — `;` separator, `,` decimals for numbers and for text that is just a decimal number (`"98.9"` from the scores parser); other text unchanged.  This is what Excel with Hungarian regional settings reads; there the `intl` file opens as one column, and imported with `,` as separator it turns `6.4` into 4 June.

**No formulas in any workbook:** `_save_workbook()` stores every cell that openpyxl took for a formula (a string starting with `=`) as text.  Text such as `= 5 mm` is not a valid formula, and Excel refuses to open such a file without repairing it.

**conflicts.xlsx** (`{case_id}_conflicts.xlsx`) — `case_id`, `field`, `source_policy`, `retained_value`, `retained_doc_type`, `overridden_value`, `overridden_doc_type`, `page`

**mismatch.xlsx** (`{case_id}_mismatch.xlsx`) — `case_id`, `filename`, `expected_fingerprint`, `found_name`, `found_taj`

**history_followup.xlsx** (`{case_id}_history_followup.xlsx`) — `case_id`, `filename`, `doc_type`, `event_date`, `days_from_admission`, `window_type`, `summary` (for dropped day-keyed fields, the summary lists the keys)

**unit_mismatches.xlsx** (`{case_id}_unit_mismatches.xlsx`) — one row per field where `ExtractedField.unit_ok == False`. Columns: `case_id`, `field`, `expected_unit`, `seen_unit`, `raw_value`, `doc_type`, `page`. No auto-conversion is performed; the raw value is kept in the wide table for manual reconciliation.

**run_config.json** (`{case_id}_run_config.json`) — the configuration this run actually used, from `describe_run()`: `llm.enabled` and, when on, `provider`, `model`, `base_url`, `temperature`, `max_tokens`, `reasoning_effort`, `max_retries`, `context_chars`, `timeout_s`, `prompts_sha256` (all system prompts); `cache.enabled`; `registry.file` and `registry.sha256`; plus `case_id`, `generated_at` and `export.csv_format`. No credentials. It makes a no-LLM result distinguishable from an LLM result after the fact, and lets benchmark runs be compared on identical settings.

**ancillary_docs.xlsx** (`{case_id}_ancillary_docs.xlsx`) — one row per `UNKNOWN` or `AMBULANSLAP` document. Columns: `case_id`, `filename`, `doc_type`, `routing_method`, `failure_reason`, `best_score`, `text_preview`, `velemeny`, `beavatkozasok`, `diagnoses_json`. For `AMBULANSLAP` documents `velemeny`/`beavatkozasok` contain the raw section text and `diagnoses_json` is a JSON array of `{code, description}` objects; for `UNKNOWN` documents these three columns are `null`.

All nine files are written to a case subfolder `<destination>/<case_id>/`, where `<destination>` is the folder the user picked for the run (`settings.output_folder_path` — `data/out` — when none was given, and always for the CLI).

A persistent ZIP is written only on request: with the UI's ZIP option ticked, `_zip_case_output()` writes `<case_id>_results.zip` **beside** the case folder, not inside it, so the folder keeps exactly the nine exported files. The case subfolder is kept even when a ZIP is produced — a destination reused across 30 cases would otherwise become 270 loose files in one directory.

`GET /download/<job_id>` still creates a ZIP on-the-fly in memory from the case folder, but the UI no longer links to it.

---

## 13. LLM Client Abstraction

### Model Selection

**Confirmed: `qwen2.5:7b-instruct-q4_K_M` via Ollama**

| Model | Hungarian quality | RAM (Q4_K_M) | Context | Verdict |
|---|---|---|---|---|
| **Qwen2.5-7B-Instruct** | ★★★★★ | ~4.5 GB | 128k | **Recommended** |
| Llama-3.1-8B-Instruct | ★★★☆☆ | ~5.0 GB | 128k | Acceptable fallback |
| Mistral-7B-Instruct | ★★☆☆☆ | ~4.1 GB | 32k | Weaker on Hungarian |

Qwen 2.5 is trained on significantly more multilingual data than the Meta and Mistral 7B models and consistently outperforms them on non-English structured extraction tasks. For Hungarian medical text, this difference is meaningful.

### Client Interface

The LLM client is a transport-only layer (`app/services/llm_client.py`), swappable via `LLM_PROVIDER`:

```python
# All three clients implement this single method:
def chat(self, system: str, user: str) -> str:
    """Single chat completion call. Returns the raw assistant message string."""
```

Implementations:
- `OllamaClient` — local Ollama server on `http://localhost:11434` (default)
- `LlamaCppClient` — llama-cpp-python server on `http://localhost:8000`
- `DisabledClient` — returns `"{}"` (empty JSON) for all calls (CI/testing without LLM)

All HTTP communication uses stdlib `http.client` + `json`; no vendor SDK is installed. Requests go only to `LLM_BASE_URL` (`POST /v1/chat/completions`). That path is the de-facto standard chat-completion request format, which Ollama and llama.cpp both serve locally — using the format does not involve any external service. `LLM_BASE_URL` must point to the local machine or an institutional server; public cloud LLM APIs are prohibited.

**Endpoint guard (`check_llm_endpoint`).** Called from `_BaseHTTPClient.__init__`, so no Ollama / llama.cpp client can be built for a disallowed endpoint, and from the pre-flight check before the server is probed. Rules, in order:

1. An `LLM_MODEL` whose tag ends in `cloud` (`gpt-oss:120b-cloud`, `…:cloud`) is refused — Ollama forwards such models to ollama.com even when the server is local.
2. A host listed in `LLM_ALLOWED_HOSTS` (comma-separated, case-insensitive) is accepted without lookup — for institutional servers on public IP ranges.
3. Otherwise the host (IP literal, or every address it resolves to) must be non-global: loopback, private (10/8, 172.16/12, 192.168/16, fc00::/7), link-local. A public address, or a name that cannot be resolved, is refused.

Violations raise `ConfigurationError`; the job fails with that message and pre-flight reports it as **critical**.

Selected via `LLM_PROVIDER` env var (`ollama` | `llamacpp` | `disabled`). Switching providers requires **only a `.env` change** — no code changes. `LLM_PROVIDER=disabled` is equivalent to `ENABLE_LLM=false` and is the recommended setting for CI environments without an LLM server. `llm_configured()` combines the two; a single run can additionally be switched off from the UI (`use_llm`).

**Request parameters** are fixed in code, not `.env`: `LLM_TEMPERATURE = 0.0` (same document → same answer), `LLM_MAX_TOKENS = 1000`, `LLM_TIMEOUT_S = 300`, `think: false`, `response_format: json_object`, no streaming. `LLM_REASONING_EFFORT` and `LLM_MAX_RETRIES` come from `.env`. All of them are reported by `describe_run()`.

Prompt construction, JSON parsing, and field validation are handled by `app/pipeline/step5_extract/prompts.py` — the client never sees FieldDefinition objects or parsed results.

### Context Window Strategy

Qwen2.5-7B supports 128k tokens, but we use a **section-by-section** prompting strategy regardless, for three reasons:
1. Precision — focused context reduces hallucination
2. Debuggability — easy to identify which section produced a wrong value
3. Retries — a failed validation re-asks only the affected section, not the whole document

Each prompt targets one `section_hint` group (e.g., all `Felvételi státus` fields in one call), keeping prompt size well under 4k tokens.

---

## 14. Registry Loader

Loaded once at app startup by `app/registry/loader.py` (cached in memory):

```python
@dataclass(frozen=True)
class FieldDefinition:
    # Identity
    field_name: str           # final column name (with suffix)
    field_base: str           # base name (from registry)
    column_header: str        # human-readable label (for SPSS)
    form_id: str              # A | B1 | Bmid | Bdis
    timepoint: str            # anam | adm | inhosp | dis

    # Type & values
    field_type: str           # 'enum' | 'open'
    value_type: str           # 'enum' | 'text' | 'number' | 'date'
    allowed_values: tuple[str, ...]  # from Enum_sets sheet; empty for open fields
    enum_set: str | None

    # Extraction policy
    source_policy: str        # DISCHARGE_ONLY | DISCHARGE_PRIMARY | EXAM_DOC_PRIMARY
    section_hint: str         # discharge-section scope (pipe-joined for multi-section fields)
    routing: tuple[tuple[DocType, str | None], ...]  # (DocType, subtype|None) pairs driving eligibility
    repeat_group: str | None  # e.g. 'endoscopy_event' (not yet used at runtime)
    scope: str | None         # 'soft_scalar' | 'list_scalar'

    # Identity / deduplication
    canonical: bool           # True = patient identity field (name, dob, taj)
    dedupe_key: str | None    # e.g. 'patient.name', 'patient.dob'
    multiple_answers_allowed: bool

    # Date handling
    date_format_hint: str | None         # e.g. "YYYY[-MM[-DD]]"
    date_granularity_allowed: str | None # "year" = partial dates accepted

    # Multi-value fields
    list_storage_format: str | None      # "json_string" = JSON array cell value

    # Conditional applicability
    condition_parent_field: str | None
    condition_operator: str | None       # "==" or "!=" (340 == / 18 != in current registry); blank is treated as "=="
    condition_value: str | None

    # Markers (from registry; pipeline uses "Unknown" / "Not applicable" at runtime)
    missing_marker: str       # registry value: 'Unknown'
    not_applicable_marker: str # registry value: 'ERVENYTELEN'

    # Extras
    notes: str | None         # free-text annotation from registry (not used at runtime)
    unit: str | None          # canonical unit for numeric fields (e.g. 'mmol/L', 'g/L')
                              # compared against ExtractedField.unit_seen at merge time
```

Key derived properties: `is_enum`, `is_list_field`, `allows_partial_date`, `is_conditional`.

**Hard validation at load time** (blocks startup):
- No duplicate `field_name` values
- No spaces in `field_name`
- Valid `timepoint` values (anam, adm, inhosp, dis)
- Valid `source_policy` values (DISCHARGE_ONLY, DISCHARGE_PRIMARY, EXAM_DOC_PRIMARY)
- Enum fields must have non-empty `allowed_values`

**Soft validation** (logged as warnings, does not block):
- `condition_parent_field` values that reference non-existent `field_name` — the rules engine silently skips these rules
- `condition_value` that is not one of the (enum) parent's allowed values — such a rule never matches a real answer, so the child is always set to `Not applicable`
- Both cases are also flagged in the exported wide table with a `REGISTRY_WARNING:` prefix (`unresolved_parent_warnings()`)
- `_check_allowed_values_column()`: a field whose Main `allowed_values` column holds different values from its Enum_sets labels, or whose `enum_set` does not exist. The application reads allowed values from Enum_sets (`_parse_allowed_values`: enum_set lookup first, the column only as fallback), so the column is a duplicate that must not drift — a drift between the two forms (`Egyéb` vs `Egyéb (megnevezni)`) is what once broke 57 conditions

**Routing column validation** (hard, introduced alongside the registry-as-source-of-truth change): every pipe-separated token in the `routing` column must resolve to a known `DocType`. `parse_routing()` itself stays tolerant (unknown tokens are silently skipped at runtime so the app is forward-compatible with newer registry files), but `_validate_routing_tokens()` in the loader raises `RegistryValidationError` at startup for any typo or stale alias. Without this check, a single misspelled token (`LAB_REPORRT`) would silently make the affected field ineligible for every ancillary document with no log signal.

A hard validation failure raises `RegistryValidationError` — the app will not start with a broken registry.

**Registry stats (current):** 558 fields total. Source policies: 427 DISCHARGE_ONLY, 107 EXAM_DOC_PRIMARY, 24 DISCHARGE_PRIMARY. 358 fields are conditional (have a `condition_parent_field`). The `routing` column declares per-field eligibility as a pipe-joined list of `DocType` or `DocType:subtype` entries (e.g. `DISCHARGE_SUMMARY|IMAGING:ct`).

---

## 15. Job State Machine

```
QUEUED → INGESTING → ROUTING → ANCHORING → EXTRACTING → MERGING → EXPORTING → DONE
                                    │                                         │
                                    ▼                                         ▼
                               FAILED (anchor not found)                FAILED (export error)

                          (finally: shutil.rmtree(case_dir) — data safety cleanup)
```

State is persisted as a JSON file at `data/jobs/<job_id>.json` (not in Redis):

```json
{
  "job_id":   "abc123",
  "case_id":  "case_full",
  "status":   "EXTRACTING",
  "step":     "Extracting fields…",
  "progress": 60,
  "error":    null
}
```

On `DONE` two more keys are present, and they are what the UI shows instead of
a download link:

```json
{
  "status":   "DONE",
  "out_path": "D:\\Kutatas\\a1b2c3d4",
  "zip_path": "D:\\Kutatas\\a1b2c3d4_results.zip"
}
```

`zip_path` appears only when the ZIP option was ticked. If the archive cannot
be written the job still ends `DONE` — the nine files are already safely on
disk by then — and `zip_error` carries the reason, which the card shows beside
the path. Failing the whole run over a packaging step would misreport a
successful extraction.

**Why JSON file, not Redis?** The job state file outlives the Redis job entry (RQ cleans up finished jobs). The `/status/<job_id>` endpoint reads the file directly — no Redis call needed for status polling. This also makes the orchestrator testable without a running Redis instance.

**The file is written at enqueue time,** by `write_queued_state()` in `POST /upload` — not by `process_case()`. It used to appear only once the pipeline started, so any job that died before that (no worker running, or a worker that could not execute it) left `/status` returning the synthetic `"Unknown"` state forever: the card showed "Ismeretlen" with a dead progress bar and no error, and the only way out was closing the page.

**A dead job is reported, not waited on.** A state file stuck at `QUEUED` cannot distinguish "waiting for a worker" from "the worker gave up" — the pipeline never ran, so it never wrote `FAILED`. When the on-disk status is `QUEUED` or `Unknown`, `/status/<job_id>` therefore asks RQ via `_rq_failure()`; if RQ has the job marked failed, the last line of its traceback is surfaced as a `FAILED` state. Redis stays out of the normal polling path — this lookup only happens while a job has yet to report progress of its own.

The browser polls `/status/<job_id>` every 1.5 s. The `progress` integer (0–100) and `step` label drive the UI progress bar directly.

---

## 16. Key Design Decisions

| Decision | Choice | Alternative considered | Reason |
|---|---|---|---|
| Job queue | RQ + Redis | Flask-Executor (threads) | RQ persists across restarts; isolates job state; easier debugging |
| LLM prompting | Section-by-section (registry section_hint) | Single full-document prompt | Reduces context window usage; improves precision; easier to debug |
| LLM transport | stdlib http.client, no vendor SDK | instructor + Pydantic | Simpler, zero external dependency for LLM calls |
| LLM validation | Manual JSON parse + enum check | Pydantic model validation | Registry-driven fields can't be expressed as static Pydantic models |
| Schema definition | Registry_base.xlsx (runtime-loaded) | Hardcoded Pydantic models | Registry changes don't require code changes |
| Merge strategy | Source-policy-aware, confidence-ranked | Last-write-wins | EXAM_DOC_PRIMARY lets exam docs win; DISCHARGE_PRIMARY preserves discharge data |
| Conflict handling | Separate report, source-policy-aware | Merge with flag in wide table | Keeps the wide table clean for statistical analysis |
| Day numbering | delta+1 (≥0), delta (<0), no day0 | calendar-based grouping | Deterministic, matches clinical convention |
| Not-applicable logic | Derived from registry in memory each run | Hardcoded in extraction | Maintainable without code changes; no YAML to keep in sync |
| Prompt cache | Per-job, in memory | Global on-disk cache (used until 2026-10) | Patient-derived answers never outlive the job or touch disk; re-runs and benchmarks are never served stale answers |
| Encrypted ZIP handling | Two-phase upload (detect → unlock) | Inline password prompt | Clean REST flow; password never persisted |

---

## 17. Password-Protected ZIP Handling

**Upload wire format:** files arrive as `multipart/form-data`. The handler
accepts both the plain `files` field and Dropzone's `uploadMultiple` spelling
`files[0]`, `files[1]`, ... (`_uploaded_files()` in `app/blueprints/main.py`).
The UI pins the name by passing `paramName` as a function; as a plain string
Dropzone would index it and the server would see an empty upload.

```
POST /upload
  ├── unencrypted files → save + enqueue → { job_id, case_id }
  └── encrypted ZIP detected →
      { status: "password_required", case_id, protected_files }
            │
POST /upload/unlock  { case_id, password }
  → decrypt + extract → enqueue → { job_id, case_id }
```

**Detection:** `is_encrypted_zip()` checks the general purpose bit flag (0x1) on each ZIP entry, which is set for both ZipCrypto and AES-256 encryption.

**Extraction:** `extract_zip()` uses `pyzipper` (AES-256 + ZipCrypto support), falling back to stdlib `zipfile` (ZipCrypto only). ZIP-slip protection flattens member names to basenames and rejects `..` sequences.

**Document-bundle ZIPs** (containing .pdf/.docx) are detected by `is_document_bundle()` and expanded at upload time. Page-archive ZIPs (only .txt files) are left intact for `step1_ingest._ingest_zip()`.

**Security:** password is a local variable in the `/upload/unlock` handler — it is used only during `extract_zip()` and is never logged, stored in job metadata, or persisted to disk.

**Current limitations:**
- Single password per upload (all encrypted ZIPs must share the same password)
- No recursive ZIP extraction
- Synchronous unlock/extraction (large ZIPs block the request thread)

---

## 18. Pre-flight Environment Check

Before the pipeline starts, `app/pipeline/preflight.py` verifies that all required and optional dependencies are available. The check runs at both entry points:

- **CLI:** calls `check_environment(scope="cli")` before file staging; shows categorised issues with install instructions, blocks on critical errors, prompts "Continue with reduced capabilities? [y/N]" on warnings only.
- **Web UI:** `GET /preflight` and `POST /upload` call `check_environment()` (default `scope="web"`). The upload endpoint returns 400 on critical failures and includes warnings in the 200 response.

### Scoped checks

`check_environment(scope)` runs a different subset of checks depending on the caller:

| Check | `web` | `cli` | Why |
|---|:---:|:---:|---|
| Python packages (PyMuPDF, pymupdf4llm, pdfminer.six, camelot-py, OpenCV, python-docx, pyzipper) | ✅ | ✅ | Used by both pipelines |
| Ghostscript on PATH | ✅ | ✅ | Required by Camelot for table extraction |
| LLM endpoint is local / institutional (**critical**) | ✅ | ✅ | `check_llm_endpoint` — runs before the reachability probe, so a cloud host is never contacted |
| LLM server reachable | ✅ | ✅ | Soft extraction needs the configured Ollama / llama.cpp endpoint. Skipped (with the endpoint check above) when the LLM is off in `.env` or switched off for this upload — `check_environment(use_llm=False)` |
| Registry_base.xlsx present (**critical**) | ✅ | ✅ | Pipeline cannot run without it |
| `redis-py` installed | ✅ | ❌ | CLI never imports it |
| Redis URL reachable | ✅ | ❌ | CLI does not enqueue; it runs `process_case()` directly |
| RQ worker attached to `default` queue | ✅ | ❌ | No queue involved on the CLI path |

Each issue includes: what is missing, how to install it, and what outputs will be degraded or missing without it. The result is structured as `PreflightResult` with `ok` (no issues), `can_run` (no critical issues), and categorised `issues[]`.

### OS-specific install hints

Install hints adapt to `sys.platform`:

- **Redis (`_check_redis`):** on Windows the hint lists only the three viable paths — Memurai, WSL2, or Docker — with concrete one-liners, because native `redis-server.exe` is not supported. On Linux/macOS the hint is the standard package-manager command.
- **Ghostscript (`_check_ghostscript`):** on Windows the hint warns that the installer does not append `gs\bin` to PATH automatically and gives the exact folder to add.
- **RQ worker (`_check_rq_worker`):** on Windows the hint explains that `worker.py` must be started with `python worker.py` rather than the plain `rq worker` CLI, because `rq.Worker.work()` relies on `os.fork()` which does not exist on Windows. The shipped entry point selects `rq.SimpleWorker` for this reason.

### RQ worker liveness

`_check_rq_worker()` runs only when Redis is reachable (otherwise the Redis warning already tells the user what to do). It enumerates `rq.Worker.all(connection=conn)` and flags a warning when no worker is listening on the `default` queue — otherwise uploads would be accepted but jobs would sit in `QUEUED` forever. This catches the common footgun of "Redis started, worker forgotten."

### Outdated worker

A worker keeps running the code it imported at start; Flask's reloader restarts the web process after an edit, the worker it does not. `app/services/code_version.py`:

- `STARTUP` — sha256 (first 12 hex) of `app/**/*.py`, `config.py`, `worker.py` and `.env`, taken when the module is first imported. `worker.main()` imports it before anything else. `current()` hashes the same files now; `is_stale()` compares the two.
- `worker_name()` — the worker registers as `crreg.<STARTUP>.<random>`, so the fingerprint is visible in `rq info` and to the web app.
- `process_case()` checks `is_stale()` first, inside its `try`: a stale worker writes FAILED with a restart message, and the `finally` still deletes the uploaded documents. In the CLI or a test the module is first imported by `process_case()` itself, so the check is a no-op there.
- `worker_problems(conn)` (`app/services/workers.py`) — Hungarian warnings rendered at the top of the upload page (`GET /`): no live worker, or a live worker whose fingerprint differs from `current()` (a worker without one, e.g. plain `rq worker`, counts as outdated). Checked at page load; an app without Redis (`create_app(testing=True)`) shows no warning.

### Killed worker

A worker that is killed (console window closed, crash, `TerminateProcess`) never calls `register_death()`. RQ drops the registration only when its heartbeat key expires, and `SimpleWorker` sets that key to the job timeout + 60 s (3660 s here) when a job starts and sends no heartbeat during the job, so a worker killed mid-job stays listed for up to an hour; a shorter `worker_ttl` would not change that. `app/services/workers.py` therefore checks liveness directly:

- `is_dead(w)` — the registration was made on this host (`hostname`) and its `pid` is no longer running (Windows: `OpenProcess` / `GetExitCodeProcess`; POSIX: `kill(pid, 0)`). A worker on another host (another container) cannot be checked and counts as alive.
- `live_workers(conn)` — used by the upload-page warning and by `_check_rq_worker()` in the pre-flight, so a dead registration is neither "a running worker" nor "an outdated worker".
- `clean_dead_workers(conn)` — run by `worker.main()` before it takes jobs: unregisters each dead worker and passes the job it was running (`current_job`) to `orchestrator.fail_interrupted()`, which writes FAILED (unless the state is already final) and deletes `data/uploads/<case_id>/` — `process_case()`'s `finally` never ran for that job.

The field registry is not fingerprinted: every job reloads `Registry_base.xlsx` (`orchestrator.py`), so a registry edit needs no restart.

### Redis as a preflight warning (not critical)

`create_app()` itself still hard-fails at startup when Redis is unreachable — the Flask process cannot meaningfully serve the web UI without a queue. The preflight check reports Redis as a **warning** rather than critical so that CLI users (who do not need Redis) are not blocked. When the web upload endpoint later tries to enqueue a job and Redis has since gone away, `_enqueue()` raises `EnqueueError`, the staged case directory is cleaned up, and the response is `503` with the underlying reason — never a fake `200 {job_id}`.

---

## 19. Data Safety

**Upload cleanup:** The orchestrator wraps the entire pipeline in a `try/finally` block. In the `finally` clause, `shutil.rmtree(case_dir)` deletes all uploaded patient documents from `data/uploads/<case_id>/` regardless of whether the pipeline succeeded or failed. This ensures no unprotected patient data persists after processing. Before a job exists, `POST /upload` removes the staged directory itself when the job cannot start: enqueue failure (`503`) or a critical pre-flight issue (`400`; earlier the documents stayed in `data/uploads/`). A password-protected upload stays staged until `POST /upload/unlock` succeeds, so a wrong password can be retried; each ZIP is deleted as soon as it is extracted.

**Retention (`prune_expired()` in `orchestrator.py`):** at worker start and after every job, staged case directories in `data/uploads/` older than 24 h (`STAGED_UPLOAD_RETENTION_S`: an upload whose password was never given, a case whose worker died) and job-state files (`*.json`, leftover `*.tmp`) in `data/jobs/` older than 7 days (`JOB_STATE_RETENTION_S`) are deleted, by mtime. Job states are kept longer for the status cards and for looking back at failures, but their error messages can carry document file names. A job still queued after a day finds its directory gone and fails with "No files found".

**No answer cache on disk:** LLM answers are cached only in memory for the running job (§ 9c) and are gone when `process_case()` returns, together with the uploaded documents. Earlier versions kept a global cache in `data/cache` that outlived every job; delete any leftover contents there — they are extracted answers from earlier runs.

**Tests never touch `data/`:** because `process_case()` deletes staged uploads and prunes job states, `tests/conftest.py` sets `UPLOAD_FOLDER`, `OUTPUT_FOLDER` and `JOBS_FOLDER` to a temporary directory (`crreg-tests-*`) for each test run and deletes it at exit. They are set unconditionally, so values exported in the shell cannot redirect the tests to the real folders; `test_config.py` checks this. Earlier versions used `data/test_uploads`, `data/test_out` and `data/test_jobs`; those empty folders can be deleted.

**Output lifecycle:** Extracted results are written to `<destination>/<case_id>/` — the folder the user chose for the run, `data/out/` by default — and stay there. No patient documents (PDFs, ZIPs) are stored in the output folder, only the wide table and the audit reports. These are **not anonymised**: the wide table carries the patient's name, date of birth and TAJ number, and the files are written unencrypted. Choose a destination that is itself access-controlled (e.g. an encrypted or permission-restricted drive).

**Why the UI does not offer a download.** The server writes the results to the user's own disk, so a download would only copy identified clinical data a second time into an unmanaged `Downloads` folder — typically synced and rarely cleaned, and now needing separate deletion. Even under Docker the output reaches the host directly (`./data:/app/data` is a bind mount), so the download was never the only way out. The job card reports the path instead.

---

## 20. Known Technical Debt

| Item | Status | Impact |
|---|---|---|
| `repeat_group` column loaded but unused at merge time | Deferred | Multi-occurrence event columns work via key identity; explicit repeat-group dedup is a future PR |
| `missing_marker` / `not_applicable_marker` in FieldDefinition | Registry data only | Pipeline uses hardcoded "Unknown" / "Not applicable" strings; registry values are loaded but never consumed at runtime |
| Unresolved `condition_parent_field` / mismatched `condition_value` cells | Soft warning + per-cell flag | All 358 conditional fields in the current registry resolve to an existing parent, and every `condition_value` is one of the parent's allowed values. (The "other" and "culture positive" options now use the short labels `Egyéb` / `Kitenyészett` everywhere — Enum_sets labels, the Main `allowed_values` column and the conditions; the redundant `(megnevezni)` / `(kórokozó megnevezése)` suffixes were removed from 25 Enum_sets labels.) If a future edit breaks either, the loader logs a soft warning at startup AND `step8_export` prefixes the affected wide-table cells with `REGISTRY_WARNING:` so non-coding users can locate the offending row in `Registry_base.xlsx`. |
| `LLM_CONTEXT_LIMIT` setting | Declared, never read | `config.Settings.llm_context_limit` validates and is exposed on `settings`, but nothing consults it. The prompt budget is the hardcoded `_MAX_CONTEXT_CHARS = 4000` in `step5_extract/soft_llm.py` — and that is a **character** count, not tokens. Tuning the env var has no effect. |
