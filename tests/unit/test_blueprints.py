"""
tests/unit/test_blueprints.py

Standalone tests for Step 14: Flask blueprints & orchestrator.

Run with:
    PYTHONPATH=/path/to/cirrhosis_registry_step14 python3 tests/unit/test_blueprints.py

Tests use Flask's test client.  Redis/RQ and pydantic-based config are mocked
so no live services and no pydantic installation needed.
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


_ORIGINAL_CONFIG_MODULE: object = None


def _install_config_mock(tmp_path: Path) -> MagicMock:
    """
    Inject a lightweight mock for the `config` module so tests run without
    pydantic.  Must be called before any import that transitively touches config.

    The previous ``sys.modules["config"]`` (if any) is preserved in the
    module-level ``_ORIGINAL_CONFIG_MODULE`` so it can be restored later by
    ``_restore_config_module``; otherwise, the Mock would leak into any test
    file that runs after this one (e.g., tests/unit/test_soft_llm.py).
    """
    global _ORIGINAL_CONFIG_MODULE
    if _ORIGINAL_CONFIG_MODULE is None:
        _ORIGINAL_CONFIG_MODULE = sys.modules.get("config", False)

    upload_dir = tmp_path / "uploads"
    output_dir = tmp_path / "out"
    jobs_dir   = tmp_path / "jobs"
    for d in (upload_dir, output_dir, jobs_dir):
        d.mkdir(parents=True, exist_ok=True)

    mock_settings = MagicMock()
    mock_settings.upload_folder_path = upload_dir
    mock_settings.output_folder_path = output_dir
    mock_settings.jobs_folder_path   = jobs_dir
    mock_settings.registry_path_obj  = Path("config/Registry_base.xlsx")
    mock_settings.flask_env          = "testing"
    mock_settings.enable_llm         = False
    mock_settings.llm_provider       = "ollama"
    mock_settings.to_flask_config.return_value = {
        "SECRET_KEY": "test-secret",
        "MAX_CONTENT_LENGTH": 200 * 1024 * 1024,
        "UPLOAD_FOLDER": str(upload_dir),
        "OUTPUT_FOLDER": str(output_dir),
        "JOBS_FOLDER":   str(jobs_dir),
    }

    mock_config = MagicMock()
    mock_config.settings = mock_settings
    sys.modules["config"] = mock_config
    sys.modules.setdefault("pydantic", MagicMock())
    sys.modules.setdefault("pydantic_settings", MagicMock())
    return mock_settings


def _restore_config_module() -> None:
    """
    Undo ``_install_config_mock`` so subsequent test files see the real
    ``config`` module.  Safe to call multiple times.
    """
    global _ORIGINAL_CONFIG_MODULE
    if _ORIGINAL_CONFIG_MODULE is None:
        return
    if _ORIGINAL_CONFIG_MODULE is False:
        sys.modules.pop("config", None)
    else:
        sys.modules["config"] = _ORIGINAL_CONFIG_MODULE  # type: ignore[assignment]
    _ORIGINAL_CONFIG_MODULE = None


def _make_app(tmp_path: Path):
    """
    Create a minimal Flask app with the main blueprint registered.
    Bypasses create_app() to avoid the pydantic/Redis dependencies.
    """
    _install_config_mock(tmp_path)

    from flask import Flask
    # Mock heavy deps before importing the blueprint
    for mod in ("structlog", "redis", "rq"):
        sys.modules.setdefault(mod, MagicMock())
    sys.modules.setdefault("app.extensions", MagicMock())
    sys.modules.setdefault("app.registry.loader", MagicMock())

    import importlib
    if "app.blueprints.main" in sys.modules:
        importlib.reload(sys.modules["app.blueprints.main"])

    from app.blueprints.main import bp

    # Point Flask at the real templates directory
    project_root = Path(__file__).parent.parent.parent
    app = Flask(__name__,
                template_folder=str(project_root / "app" / "templates"))
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    app.register_blueprint(bp)
    return app


# ─────────────────────────────────────────────────────────────────────────────

class TestHealthEndpoints(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def test_root_returns_ok(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        # Root now serves the HTML UI (not JSON)
        self.assertIn(b"<!DOCTYPE html>", resp.data)

    def test_root_shows_worker_warnings(self):
        with patch("app.services.workers.worker_problems",
                   return_value=["Nem fut feldolgozó (RQ worker)"]):
            resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('class="worker-warn"', resp.get_data(as_text=True))
        self.assertIn("Nem fut feldolgozó", resp.get_data(as_text=True))

    def test_root_without_worker_warnings_has_no_banner(self):
        with patch("app.services.workers.worker_problems", return_value=[]):
            resp = self.client.get("/")
        self.assertNotIn('class="worker-warn"', resp.get_data(as_text=True))

    def test_health_returns_ok(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "ok")


class TestUploadExtensionValidation(unittest.TestCase):
    """POST /upload should reject unsupported extensions with 400."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def _upload(self, filename: str, content: bytes = b"dummy"):
        return self.client.post(
            "/upload",
            data={"files": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
        )

    def test_no_files_returns_400(self):
        resp = self.client.post("/upload", data={}, content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_invalid_extension_txt_returns_400(self):
        resp = self._upload("report.txt")
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("Unsupported", data["error"])

    def test_invalid_extension_exe_returns_400(self):
        self.assertEqual(self._upload("evil.exe").status_code, 400)

    def test_invalid_extension_csv_returns_400(self):
        self.assertEqual(self._upload("data.csv").status_code, 400)

    @patch("app.extensions.get_queue")
    def test_valid_pdf_returns_200(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self._upload("report.pdf", b"%PDF dummy")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("job_id", data)
        self.assertIn("case_id", data)

    @patch("app.extensions.get_queue")
    def test_valid_zip_returns_200(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._upload("bundle.zip", b"PK\x03\x04").status_code, 200)

    @patch("app.extensions.get_queue")
    def test_valid_docx_returns_200(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._upload("letter.docx", b"PK\x03\x04").status_code, 200)

    @patch("app.extensions.get_queue")
    def test_case_insensitive_extension(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._upload("report.PDF", b"%PDF dummy").status_code, 200)

    @patch("app.extensions.get_queue")
    def test_upload_creates_unique_ids(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        r1 = json.loads(self._upload("a.pdf").data)
        r2 = json.loads(self._upload("b.pdf").data)
        self.assertNotEqual(r1["job_id"], r2["job_id"])
        self.assertNotEqual(r1["case_id"], r2["case_id"])

    @patch("app.extensions.get_queue")
    def test_file_saved_to_case_dir(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self.client.post(
            "/upload",
            data={"files": (io.BytesIO(b"content"), "myfile.pdf")},
            content_type="multipart/form-data",
        )
        data = json.loads(resp.data)
        case_id = data["case_id"]
        case_dir = self.tmp / "uploads" / case_id
        self.assertTrue(case_dir.exists(), f"Expected dir {case_dir}")
        files = list(case_dir.iterdir())
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].name, "myfile.pdf")

    @patch("app.extensions.get_queue")
    def test_enqueue_called_with_job_id(self, mock_get_queue):
        mock_queue = MagicMock()
        mock_get_queue.return_value = mock_queue
        resp = self._upload("x.pdf")
        data = json.loads(resp.data)
        job_id = data["job_id"]
        mock_queue.enqueue.assert_called_once()
        # job_id kwarg must match
        call_kwargs = mock_queue.enqueue.call_args[1]
        self.assertEqual(call_kwargs.get("job_id"), job_id)


class TestUploadFieldNaming(unittest.TestCase):
    """POST /upload must read the field names a real browser actually sends.

    Dropzone with ``uploadMultiple`` posts ``files[0]``, ``files[1]``, ...
    rather than a repeated ``files`` field.  The endpoint previously read only
    ``files`` and answered every browser upload with "No files provided."
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    @patch("app.extensions.get_queue")
    def test_indexed_field_name_accepted(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self.client.post(
            "/upload",
            data={"files[0]": (io.BytesIO(b"%PDF dummy"), "report.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    @patch("app.extensions.get_queue")
    def test_multiple_indexed_files_all_saved(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self.client.post(
            "/upload",
            data={
                "files[0]": (io.BytesIO(b"%PDF a"), "a.pdf"),
                "files[1]": (io.BytesIO(b"%PDF b"), "b.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        case_id = json.loads(resp.data)["case_id"]
        saved = {p.name for p in (self.tmp / "uploads" / case_id).iterdir()}
        self.assertEqual(saved, {"a.pdf", "b.pdf"})

    @patch("app.extensions.get_queue")
    def test_plain_field_name_still_accepted(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self.client.post(
            "/upload",
            data={"files": (io.BytesIO(b"%PDF dummy"), "report.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_unrelated_field_name_still_rejected(self):
        resp = self.client.post(
            "/upload",
            data={"attachment": (io.BytesIO(b"%PDF dummy"), "report.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)


class TestUploadCaseIntegrity(unittest.TestCase):
    """A case must arrive whole, and must never look processed when it is not."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    @patch("app.extensions.get_queue")
    def test_25_files_land_in_one_case(self, mock_get_queue):
        """Dropzone's parallelUploads used to cap a request at 20 files.

        The surplus was either posted as a second case (splitting one patient
        across two case_ids) or dropped when successmultiple cleared the queue.
        Either way documents went missing silently, so assert the whole set.
        """
        mock_get_queue.return_value = MagicMock()
        data = {
            f"files[{i}]": (io.BytesIO(b"%PDF dummy"), f"doc_{i:02d}.pdf")
            for i in range(25)
        }
        resp = self.client.post("/upload", data=data,
                                content_type="multipart/form-data")
        self.assertEqual(resp.status_code, 200, resp.data)
        case_id = json.loads(resp.data)["case_id"]
        saved = sorted(p.name for p in (self.tmp / "uploads" / case_id).iterdir())
        self.assertEqual(saved, [f"doc_{i:02d}.pdf" for i in range(25)])

    @patch("app.extensions.get_queue")
    def test_state_is_queued_immediately_after_upload(self, mock_get_queue):
        """The job-state file must exist before the worker touches the job.

        It used to be written only once process_case started, so a job that
        died first (or had no worker) left the UI on "Ismeretlen" forever with
        a dead progress bar.
        """
        mock_get_queue.return_value = MagicMock()
        resp = self.client.post(
            "/upload",
            data={"files": (io.BytesIO(b"%PDF dummy"), "a.pdf")},
            content_type="multipart/form-data",
        )
        job_id = json.loads(resp.data)["job_id"]

        state = json.loads((self.tmp / "jobs" / f"{job_id}.json").read_text(encoding="utf-8"))
        self.assertEqual(state["status"], "QUEUED")
        self.assertEqual(state["progress"], 0)

        status = json.loads(self.client.get(f"/status/{job_id}").data)
        self.assertEqual(status["status"], "QUEUED")

    @patch("app.pipeline.preflight.check_environment")
    @patch("app.extensions.get_queue")
    def test_blocked_preflight_removes_staged_documents(self, mock_get_queue, mock_pf):
        """A run refused by the preflight check used to leave the patient
        documents in uploads/<case_id>/ for good."""
        mock_pf.return_value = MagicMock(can_run=False, to_dict=lambda: {})
        resp = self.client.post(
            "/upload",
            data={"files": (io.BytesIO(b"%PDF dummy"), "a.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(list((self.tmp / "uploads").iterdir()), [])
        mock_get_queue.return_value.enqueue.assert_not_called()


class TestDestinationFolder(unittest.TestCase):
    """The user picks where results land; the worker must receive that choice."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def _post(self, **fields):
        data = {"files": (io.BytesIO(b"%PDF dummy"), "a.pdf")}
        data.update(fields)
        return self.client.post("/upload", data=data,
                                content_type="multipart/form-data")

    @staticmethod
    def _enqueue_args(mock_get_queue):
        """(job_id, case_dir, out_dir, make_zip) as handed to RQ."""
        return mock_get_queue.return_value.enqueue.call_args[0][1:5]

    @staticmethod
    def _use_llm(mock_get_queue):
        """The per-run use_llm flag as handed to RQ."""
        return mock_get_queue.return_value.enqueue.call_args[0][5]

    @patch("app.extensions.get_queue")
    def test_llm_flag_defaults_to_on(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._post().status_code, 200)
        self.assertTrue(self._use_llm(mock_get_queue))

    @patch("app.extensions.get_queue")
    def test_llm_can_be_switched_off_for_one_run(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._post(use_llm="false").status_code, 200)
        self.assertFalse(self._use_llm(mock_get_queue))

    @staticmethod
    def _csv_format(mock_get_queue):
        """The per-run csv_format as handed to RQ."""
        return mock_get_queue.return_value.enqueue.call_args[0][6]

    @patch("app.extensions.get_queue")
    def test_csv_format_defaults_to_intl(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._post().status_code, 200)
        self.assertEqual(self._csv_format(mock_get_queue), "intl")

    @patch("app.extensions.get_queue")
    def test_hungarian_csv_format_reaches_the_worker(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._post(csv_format="hu").status_code, 200)
        self.assertEqual(self._csv_format(mock_get_queue), "hu")

    @patch("app.extensions.get_queue")
    def test_unknown_csv_format_is_rejected(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self._post(csv_format="xls")
        self.assertEqual(resp.status_code, 400)
        mock_get_queue.return_value.enqueue.assert_not_called()

    @patch("app.extensions.get_queue")
    def test_blank_destination_falls_back_to_the_configured_default(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        self.assertEqual(self._post().status_code, 200)
        _, _, out_dir, make_zip = self._enqueue_args(mock_get_queue)
        self.assertEqual(Path(out_dir), self.tmp / "out")
        self.assertFalse(make_zip)

    @patch("app.extensions.get_queue")
    def test_chosen_destination_and_zip_flag_reach_the_worker(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        dest = self.tmp / "elsewhere"
        dest.mkdir()

        resp = self._post(dest_dir=str(dest), make_zip="true")
        self.assertEqual(resp.status_code, 200, resp.data)

        _, _, out_dir, make_zip = self._enqueue_args(mock_get_queue)
        self.assertEqual(Path(out_dir), dest.resolve())
        self.assertTrue(make_zip)

    @patch("app.extensions.get_queue")
    def test_relative_destination_is_rejected(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self._post(dest_dir="eredmenyek")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("teljes útvonal", json.loads(resp.data)["error"])

    @patch("app.extensions.get_queue")
    def test_nonexistent_destination_is_rejected(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        resp = self._post(dest_dir=str(self.tmp / "no_such_folder"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nem létezik", json.loads(resp.data)["error"])

    @patch("app.extensions.get_queue")
    def test_a_file_is_rejected_as_a_destination(self, mock_get_queue):
        mock_get_queue.return_value = MagicMock()
        not_a_dir = self.tmp / "notes.txt"
        not_a_dir.write_text("x", encoding="utf-8")
        resp = self._post(dest_dir=str(not_a_dir))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("nem mappa", json.loads(resp.data)["error"])

    @patch("app.extensions.get_queue")
    def test_a_bad_destination_stages_no_files(self, mock_get_queue):
        """Validate before writing: a typo must not leave uploads behind."""
        mock_get_queue.return_value = MagicMock()
        self._post(dest_dir="relative/path")
        self.assertEqual(list((self.tmp / "uploads").iterdir()), [])
        mock_get_queue.return_value.enqueue.assert_not_called()

    @patch("app.extensions.get_queue")
    def test_quoted_windows_path_is_accepted(self, mock_get_queue):
        """Explorer's "Copy as path" wraps the path in quotes."""
        mock_get_queue.return_value = MagicMock()
        dest = self.tmp / "quoted"
        dest.mkdir()
        resp = self._post(dest_dir=f'"{dest}"')
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(Path(self._enqueue_args(mock_get_queue)[2]), dest.resolve())

    def test_unlock_validates_the_destination_too(self):
        """The password round-trip re-sends dest_dir; it must be checked there."""
        resp = self.client.post("/upload/unlock", json={
            "case_id": "0123abcd",
            "password": "irrelevant",
            "dest_dir": "relative/path",
        })
        self.assertEqual(resp.status_code, 400)
        self.assertIn("teljes útvonal", json.loads(resp.data)["error"])


class TestBrowseFolderEndpoint(unittest.TestCase):
    """POST /browse-folder — server-side native dialog, local requests only."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def test_remote_requests_cannot_open_a_dialog_on_the_host(self):
        resp = self.client.post("/browse-folder", json={},
                                environ_overrides={"REMOTE_ADDR": "10.0.0.5"})
        self.assertEqual(resp.status_code, 403)

    def test_returns_the_chosen_path(self):
        chosen = str(self.tmp)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=chosen + "\n", stderr="")
            resp = self.client.post("/browse-folder", json={"current": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.data)["path"], chosen)

    def test_missing_dialog_degrades_to_503_not_500(self):
        """No tkinter / headless host: the UI falls back to typing a path."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no display")
            resp = self.client.post("/browse-folder", json={})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("error", json.loads(resp.data))


class TestResultArchive(unittest.TestCase):
    """The optional ZIP sits beside the case folder, not inside it."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _install_config_mock(self.tmp)

    def _case_folder(self) -> Path:
        case_out = self.tmp / "out" / "abc12345"
        case_out.mkdir(parents=True)
        for name in ("a_wide_table.csv", "b_conflicts.xlsx", "c_mismatch.xlsx"):
            (case_out / name).write_text("data", encoding="utf-8")
        return case_out

    def test_archive_contains_every_exported_file(self):
        import zipfile

        from app.pipeline.orchestrator import _zip_case_output

        case_out = self._case_folder()
        zip_path = _zip_case_output(case_out)

        self.assertEqual(zip_path, case_out.parent / "abc12345_results.zip")
        with zipfile.ZipFile(zip_path) as zf:
            self.assertEqual(
                sorted(zf.namelist()),
                ["a_wide_table.csv", "b_conflicts.xlsx", "c_mismatch.xlsx"],
            )

    def test_case_folder_keeps_only_the_exported_files(self):
        from app.pipeline.orchestrator import _zip_case_output

        case_out = self._case_folder()
        _zip_case_output(case_out)
        self.assertEqual(len(list(case_out.iterdir())), 3)


class TestStatusEndpoint(unittest.TestCase):
    """GET /status/<job_id> — always 200; Unknown for non-existent jobs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def test_unknown_job_id_returns_unknown(self):
        resp = self.client.get("/status/nonexistentjobid12345")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "Unknown")
        self.assertEqual(data["job_id"], "nonexistentjobid12345")

    def test_status_response_has_required_fields(self):
        resp = self.client.get("/status/anyjobid")
        data = json.loads(resp.data)
        for key in ("job_id", "status", "progress", "step", "error"):
            self.assertIn(key, data, f"Missing field: {key}")

    def test_status_reflects_written_state(self):
        """Writing a job JSON file must be reflected by /status."""
        jobs_dir = self.tmp / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        fake = {
            "job_id": "testjob001", "case_id": "abc12345",
            "status": "EXTRACTING", "progress": 60,
            "step": "Extracting fields\u2026", "error": None,
        }
        (jobs_dir / "testjob001.json").write_text(json.dumps(fake), encoding="utf-8")
        resp = self.client.get("/status/testjob001")
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "EXTRACTING")
        self.assertEqual(data["progress"], 60)
        self.assertEqual(data["case_id"], "abc12345")


class TestDownloadEndpoint(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def _write_state(self, job_id: str, status: str, case_id: str = "test1234"):
        jobs_dir = self.tmp / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        state = {
            "job_id": job_id, "case_id": case_id,
            "status": status, "progress": 100 if status == "DONE" else 50,
            "step": "", "error": None,
        }
        (jobs_dir / f"{job_id}.json").write_text(json.dumps(state), encoding="utf-8")

    def test_in_progress_returns_202(self):
        self._write_state("job_prog", "EXTRACTING")
        self.assertEqual(self.client.get("/download/job_prog").status_code, 202)

    def test_failed_returns_404(self):
        jobs_dir = self.tmp / "jobs"
        jobs_dir.mkdir(parents=True, exist_ok=True)
        state = {"job_id": "job_fail", "case_id": "x", "status": "FAILED",
                 "progress": 100, "step": "", "error": "boom"}
        (jobs_dir / "job_fail.json").write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(self.client.get("/download/job_fail").status_code, 404)

    def test_done_without_output_returns_404(self):
        self._write_state("job_nzip", "DONE", "nocase")
        self.assertEqual(self.client.get("/download/job_nzip").status_code, 404)

    def test_done_with_output_serves_zip(self):
        self._write_state("job_done", "DONE", "mycase1")
        out_dir = self.tmp / "out"
        case_dir = out_dir / "mycase1"
        case_dir.mkdir(parents=True, exist_ok=True)
        (case_dir / "mycase1_wide_table.csv").write_text("hello", encoding="utf-8")
        resp = self.client.get("/download/job_done")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/zip", resp.content_type)

    def test_unknown_job_returns_202(self):
        self.assertEqual(self.client.get("/download/completely_unknown_job").status_code, 202)


class TestJobStateHelpers(unittest.TestCase):
    """Unit tests for orchestrator state helpers (no Flask needed)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _install_config_mock(self.tmp)

    def test_read_nonexistent_returns_unknown(self):
        from app.pipeline.orchestrator import read_job_state
        state = read_job_state("does_not_exist_xyz")
        self.assertEqual(state["status"], "Unknown")
        self.assertEqual(state["job_id"], "does_not_exist_xyz")
        self.assertIsNone(state["error"])

    def test_write_then_read_roundtrip(self):
        from app.pipeline.orchestrator import read_job_state, _write_state
        written = {
            "job_id": "rt001", "case_id": "cafe1234",
            "status": "MERGING", "progress": 80,
            "step": "Merging fields\u2026", "error": None,
        }
        _write_state("rt001", written)
        recovered = read_job_state("rt001")
        self.assertEqual(recovered["status"], "MERGING")
        self.assertEqual(recovered["progress"], 80)
        self.assertEqual(recovered["case_id"], "cafe1234")


class TestOrchestratorEdgeCases(unittest.TestCase):
    """Smoke-test the orchestrator without RQ (direct calls)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _install_config_mock(self.tmp)

    def test_empty_case_dir_fails_gracefully(self):
        from app.pipeline.orchestrator import process_case, read_job_state
        empty_dir = self.tmp / "uploads" / "emptycase"
        empty_dir.mkdir()
        process_case("jobempty001", str(empty_dir))
        state = read_job_state("jobempty001")
        self.assertEqual(state["status"], "FAILED")
        self.assertIsNotNone(state["error"])

    def test_stale_worker_refuses_job_and_removes_uploads(self):
        """A worker whose code is older than the disk must not process a case."""
        from app.pipeline.orchestrator import process_case, read_job_state
        case_dir = self.tmp / "uploads" / "stalecase"
        case_dir.mkdir()
        (case_dir / "doc.pdf").write_bytes(b"%PDF-1.4 dummy")
        with patch("app.services.code_version.is_stale", return_value=True), \
             patch("app.pipeline.step1_ingest.ingest") as mock_ingest:
            process_case("jobstale003", str(case_dir))
        state = read_job_state("jobstale003")
        self.assertEqual(state["status"], "FAILED")
        self.assertIn("Restart", state["error"])
        mock_ingest.assert_not_called()
        self.assertFalse(case_dir.exists())

    def test_nonexistent_dir_fails_gracefully(self):
        from app.pipeline.orchestrator import process_case, read_job_state
        process_case("jobnodir002", str(self.tmp / "uploads" / "does_not_exist"))
        state = read_job_state("jobnodir002")
        self.assertEqual(state["status"], "FAILED")


class TestUploadZipHandling(unittest.TestCase):
    """Upload endpoint behaviour when ZIP files are involved."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def _upload(self, filename: str, content: bytes = b"dummy"):
        return self.client.post(
            "/upload",
            data={"files": (io.BytesIO(content), filename)},
            content_type="multipart/form-data",
        )

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=True)
    @patch("app.extensions.get_queue")
    def test_encrypted_zip_returns_password_required(self, mock_get_queue, mock_enc):
        mock_get_queue.return_value = MagicMock()
        resp = self._upload("bundle.zip", b"PK\x03\x04 fake zip")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "password_required")
        self.assertIn("case_id", data)
        self.assertIn("protected_files", data)
        self.assertIn("bundle.zip", data["protected_files"])
        # Job must NOT be enqueued
        mock_get_queue.return_value.enqueue.assert_not_called()

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=False)
    @patch("app.pipeline.zip_handler.is_document_bundle", return_value=False)
    @patch("app.extensions.get_queue")
    def test_page_archive_zip_left_untouched(self, mock_get_queue, mock_bundle, mock_enc):
        """A page-archive ZIP (only .txt) is left for the ingest pipeline."""
        mock_get_queue.return_value = MagicMock()
        resp = self._upload("pages.zip", b"PK\x03\x04 page archive")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("job_id", data)
        # extract_zip should NOT have been called (is_document_bundle returned False)

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=False)
    @patch("app.pipeline.zip_handler.is_document_bundle", return_value=True)
    @patch("app.pipeline.zip_handler.extract_zip", return_value=[])
    @patch("app.extensions.get_queue")
    def test_unencrypted_bundle_extracted_and_enqueued(
        self, mock_get_queue, mock_extract, mock_bundle, mock_enc,
    ):
        mock_get_queue.return_value = MagicMock()
        resp = self._upload("bundle.zip", b"PK\x03\x04 document bundle")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("job_id", data)
        mock_extract.assert_called_once()
        mock_get_queue.return_value.enqueue.assert_called_once()

    @patch("app.extensions.get_queue")
    def test_plain_pdf_unchanged_behaviour(self, mock_get_queue):
        """Non-ZIP uploads skip all ZIP detection and work as before."""
        mock_get_queue.return_value = MagicMock()
        resp = self._upload("report.pdf", b"%PDF dummy")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("job_id", data)
        self.assertIn("case_id", data)

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=True)
    @patch("app.extensions.get_queue")
    def test_mixed_upload_pdf_and_encrypted_zip(self, mock_get_queue, mock_enc):
        """Uploading a PDF alongside an encrypted ZIP → password_required."""
        mock_get_queue.return_value = MagicMock()
        resp = self.client.post("/upload", data={
            "files": [
                (io.BytesIO(b"%PDF"), "report.pdf"),
                (io.BytesIO(b"PK\x03\x04"), "bundle.zip"),
            ],
        }, content_type="multipart/form-data")
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "password_required")
        # Only the .zip should be in protected_files
        self.assertEqual(data["protected_files"], ["bundle.zip"])


class TestUnlockEndpoint(unittest.TestCase):
    """POST /upload/unlock — password submission for encrypted ZIPs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def _setup_case(self, case_id: str) -> Path:
        """Create a fake case directory with a placeholder ZIP."""
        case_dir = self.tmp / "uploads" / case_id
        case_dir.mkdir(parents=True)
        (case_dir / "bundle.zip").write_bytes(b"PK fake encrypted zip")
        return case_dir

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=True)
    @patch("app.pipeline.zip_handler.extract_zip", return_value=[])
    @patch("app.extensions.get_queue")
    def test_correct_password_returns_job_id(
        self, mock_get_queue, mock_extract, mock_enc,
    ):
        mock_get_queue.return_value = MagicMock()
        self._setup_case("ab12cd34")

        resp = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34",
            "password": "secret123",
        })
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertIn("job_id", data)
        self.assertEqual(data["case_id"], "ab12cd34")
        mock_get_queue.return_value.enqueue.assert_called_once()

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=True)
    @patch("app.pipeline.zip_handler.extract_zip",
           side_effect=RuntimeError("Hibás jelszó."))
    def test_wrong_password_returns_400(self, mock_extract, mock_enc):
        self._setup_case("ab12cd34")

        resp = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34",
            "password": "wrong",
        })
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    def test_missing_case_id_returns_400(self):
        resp = self.client.post("/upload/unlock", json={
            "password": "secret",
        })
        self.assertEqual(resp.status_code, 400)

    def test_missing_password_returns_400(self):
        self._setup_case("ab12cd34")
        resp = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34",
        })
        self.assertEqual(resp.status_code, 400)

    def test_invalid_case_id_format_returns_400(self):
        resp = self.client.post("/upload/unlock", json={
            "case_id": "not-hex!!",
            "password": "secret",
        })
        self.assertEqual(resp.status_code, 400)

    def test_nonexistent_case_returns_404(self):
        resp = self.client.post("/upload/unlock", json={
            "case_id": "deadbeef",
            "password": "secret",
        })
        self.assertEqual(resp.status_code, 404)

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=False)
    def test_no_encrypted_zips_returns_400(self, mock_enc):
        """Case exists but has no encrypted ZIPs."""
        self._setup_case("ab12cd34")
        resp = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34",
            "password": "secret",
        })
        self.assertEqual(resp.status_code, 400)
        data = json.loads(resp.data)
        self.assertIn("error", data)

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=True)
    @patch("app.pipeline.zip_handler.extract_zip",
           side_effect=RuntimeError("Hibás jelszó."))
    def test_retry_after_wrong_password(self, mock_extract, mock_enc):
        """Second attempt with wrong password also returns 400."""
        self._setup_case("ab12cd34")

        # First attempt
        resp1 = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34", "password": "wrong1",
        })
        self.assertEqual(resp1.status_code, 400)

        # Second attempt
        resp2 = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34", "password": "wrong2",
        })
        self.assertEqual(resp2.status_code, 400)

    @patch("app.pipeline.zip_handler.is_encrypted_zip", return_value=True)
    @patch("app.pipeline.zip_handler.extract_zip", return_value=[])
    @patch("app.extensions.get_queue")
    def test_password_not_in_response(self, mock_get_queue, mock_extract, mock_enc):
        """The password must never appear in the API response."""
        mock_get_queue.return_value = MagicMock()
        self._setup_case("ab12cd34")

        resp = self.client.post("/upload/unlock", json={
            "case_id": "ab12cd34",
            "password": "supersecret",
        })
        self.assertNotIn(b"supersecret", resp.data)


# ─────────────────────────────────────────────────────────────────────────────

def tearDownModule() -> None:
    """Restore the real ``config`` module so subsequent test files are not
    affected by the Mock that ``_install_config_mock`` injects."""
    _restore_config_module()


def run_all() -> bool:
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in (
        TestHealthEndpoints,
        TestUploadExtensionValidation,
        TestUploadFieldNaming,
        TestUploadCaseIntegrity,
        TestDestinationFolder,
        TestBrowseFolderEndpoint,
        TestResultArchive,
        TestStatusEndpoint,
        TestDownloadEndpoint,
        TestJobStateHelpers,
        TestOrchestratorEdgeCases,
        TestUploadZipHandling,
        TestUnlockEndpoint,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
