"""
tests/unit/test_ui.py

Step 15 tests: UI template rendering and JS API contract verification.

Run with:
    PYTHONPATH=/path/to/cirrhosis_registry_step15 python3 tests/unit/test_ui.py

Covers:
  - GET / returns 200 and renders index.html (contains key HTML landmarks)
  - index.html references the correct API endpoints (/upload, /status,
    /browse-folder) and shows where results were saved instead of offering
    a download
  - index.html includes Dropzone.js (vendored locally, no CDN)
  - Status JSON contains all fields the JS template expects
  - /health still returns JSON
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock


_ORIGINAL_CONFIG_MODULE: object = None


def _install_config_mock(tmp_path: Path) -> MagicMock:
    """Inject a Mock for the ``config`` module.  See ``_restore_config_module``."""
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
    for mod in ("structlog", "redis", "rq"):
        sys.modules.setdefault(mod, MagicMock())
    sys.modules.setdefault("app.extensions", MagicMock())
    sys.modules.setdefault("app.registry.loader", MagicMock())
    return mock_settings


def _restore_config_module() -> None:
    """Undo ``_install_config_mock`` so subsequent test files see the real
    ``config`` module.  Without this, the Mock leaks into other test files
    (e.g., tests/unit/test_soft_llm.py) and breaks ``MagicMock(spec=Settings)``."""
    global _ORIGINAL_CONFIG_MODULE
    if _ORIGINAL_CONFIG_MODULE is None:
        return
    if _ORIGINAL_CONFIG_MODULE is False:
        sys.modules.pop("config", None)
    else:
        sys.modules["config"] = _ORIGINAL_CONFIG_MODULE  # type: ignore[assignment]
    _ORIGINAL_CONFIG_MODULE = None


def _make_app(tmp_path: Path):
    _install_config_mock(tmp_path)
    from flask import Flask
    import importlib
    if "app.blueprints.main" in sys.modules:
        importlib.reload(sys.modules["app.blueprints.main"])
    from app.blueprints.main import bp
    # Find the templates/static folders relative to the project root
    project_root = Path(__file__).parent.parent.parent
    app = Flask(
        __name__,
        template_folder=str(project_root / "app" / "templates"),
        static_folder=str(project_root / "app" / "static"),
    )
    app.config["TESTING"] = True
    app.config["SECRET_KEY"] = "test"
    app.register_blueprint(bp)
    return app


class TestIndexRoute(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def test_root_returns_200(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)

    def test_root_returns_html(self):
        resp = self.client.get("/")
        self.assertIn(b"text/html", resp.content_type.encode())

    def test_page_contains_dropzone_script(self):
        resp = self.client.get("/")
        self.assertIn(b"dropzone", resp.data.lower())

    def test_dropzone_assets_served_locally(self):
        """Vendored Dropzone assets must be served from /static (no CDN)."""
        resp = self.client.get("/")
        self.assertNotIn(b"cdnjs.cloudflare.com", resp.data)
        js = self.client.get("/static/vendor/dropzone.min.js")
        self.assertEqual(js.status_code, 200)
        css = self.client.get("/static/vendor/dropzone.min.css")
        self.assertEqual(css.status_code, 200)

    def test_dropzone_param_name_is_function_form(self):
        """paramName must be a function.

        As a plain string, Dropzone + uploadMultiple renames the parts to
        ``files[0]``, ``files[1]``, ... and the upload silently arrives empty.
        """
        resp = self.client.get("/")
        self.assertIn(b"paramName: () => 'files'", resp.data)
        self.assertNotIn(b"paramName: 'files'", resp.data)

    def test_inline_script_is_valid_javascript(self):
        """The page's inline script must parse.

        All the upload logic lives in one inline <script>. A single syntax
        error anywhere in it stops the whole block from executing, so Dropzone
        is never constructed and the drop area silently becomes an inert div —
        the page looks fine but nothing can be uploaded. That is exactly how a
        stray newline inside a string literal shipped once.
        """
        import re
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if node is None:
            self.skipTest("node not installed — cannot syntax-check the inline script")

        html = self.client.get("/").data.decode("utf-8")
        # Drop HTML comments first — one of them discusses <script> tags.
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(blocks, "no inline <script> block found")

        with tempfile.TemporaryDirectory() as tmp:
            for i, block in enumerate(blocks):
                js = Path(tmp) / f"block{i}.js"
                js.write_text(block, encoding="utf-8")
                result = subprocess.run([node, "--check", str(js)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_upload_limits_cannot_diverge(self):
        """maxFiles and parallelUploads must come from the same constant.

        With uploadMultiple, Dropzone sends only the first `parallelUploads`
        queued files per request. If that number is below maxFiles, one case is
        split across two POSTs (two case_ids) or silently truncated.
        """
        resp = self.client.get("/")
        self.assertIn(b"parallelUploads: MAX_FILES_PER_CASE", resp.data)
        self.assertIn(b"maxFiles: MAX_FILES_PER_CASE", resp.data)

    def test_rejected_files_are_dropped_from_the_queue(self):
        """A file Dropzone refuses must not stay on screen as queued."""
        resp = self.client.get("/")
        self.assertIn(b"this.on('error'", resp.data)
        self.assertIn(b"removeChip(file.name)", resp.data)

    def test_header_icon_is_a_liver_not_a_heart(self):
        resp = self.client.get("/")
        self.assertNotIn("🫀".encode("utf-8"), resp.data)  # 🫀 anatomical heart
        self.assertIn("Máj".encode("utf-8"), resp.data)        # <title>Máj</title>

    def test_page_references_upload_endpoint(self):
        resp = self.client.get("/")
        self.assertIn(b"/upload", resp.data)

    def test_page_references_status_endpoint(self):
        resp = self.client.get("/")
        self.assertIn(b"/status/", resp.data)

    def test_page_offers_no_download_link(self):
        """Results are written to the user's own folder; a download would only
        make a second, unmanaged copy of patient data."""
        resp = self.client.get("/")
        self.assertNotIn(b"/download/", resp.data)

    def test_page_reports_where_results_were_saved(self):
        resp = self.client.get("/")
        self.assertIn(b"out_path", resp.data)
        self.assertIn(b"result-path", resp.data)

    def test_page_has_destination_controls(self):
        resp = self.client.get("/")
        for marker in (b'id="dest-dir"', b'id="btn-browse"', b'id="opt-zip"'):
            self.assertIn(marker, resp.data, f"{marker!r} missing from the page")

    def test_destination_field_is_prefilled_with_the_default_folder(self):
        """An empty box would force the user to type a path for the common case."""
        resp = self.client.get("/")
        expected = str((self.tmp / "out").resolve())
        self.assertIn(expected.encode(), resp.data)

    def test_page_has_file_input_area(self):
        resp = self.client.get("/")
        # Dropzone attaches to an element with id "dz-upload"
        self.assertIn(b"dz-upload", resp.data)

    def test_page_has_process_button(self):
        resp = self.client.get("/")
        self.assertIn(b"btn-process", resp.data)

    def test_llm_switch_is_sent_with_both_upload_paths(self):
        """The per-run LLM switch must reach POST /upload and /upload/unlock."""
        html = self.client.get("/").data
        self.assertIn(b'id="opt-llm"', html)
        self.assertIn(b"formData.append('use_llm'", html)
        self.assertIn(b"use_llm: llmToggle.checked", html)

    def test_llm_switch_is_not_remembered(self):
        """A forgotten "off" must not silently carry over to the next visit."""
        html = self.client.get("/").data
        self.assertNotIn(b"LLM_KEY", html)
        self.assertNotIn(b"llmToggle.checked = ", html)

    def test_config_panel_lists_llm_parameters(self):
        from unittest.mock import patch
        cfg = {
            "llm": {"enabled": True, "provider": "ollama", "model": "test-model:7b",
                    "base_url": "http://127.0.0.1:11434", "temperature": 0.0,
                    "max_tokens": 1000, "reasoning_effort": "none", "max_retries": 2,
                    "context_chars": 4000, "timeout_s": 300,
                    "prompts_sha256": "abcdef1234567890"},
            "cache": {"enabled": True},
            "registry": {"file": "Registry_base.xlsx", "sha256": "0123456789abcdef"},
        }
        with patch("app.services.run_config.describe_run", return_value=cfg):
            html = self.client.get("/").data.decode("utf-8")
        self.assertIn('id="cfg-panel"', html)
        for text in ("test-model:7b", "Hőmérséklet", ">0.0<", "1000 token",
                     "4000 karakter", "abcdef123456", "0123456789ab",
                     "csak az adott feladat idejére"):
            self.assertIn(text, html)
        self.assertNotIn("disabled>", html.split('id="opt-llm"')[1].split("</label>")[0])

    def test_config_panel_when_llm_disabled_on_server(self):
        from unittest.mock import patch
        cfg = {"llm": {"enabled": False}, "cache": {"enabled": False},
               "registry": {"file": "Registry_base.xlsx", "sha256": None}}
        with patch("app.services.run_config.describe_run", return_value=cfg):
            html = self.client.get("/").data.decode("utf-8")
        switch = html.split('id="opt-llm"')[1].split("</label>")[0]
        self.assertIn("disabled", switch)
        self.assertNotIn("checked", switch)
        self.assertIn("ki van kapcsolva", html)
        self.assertNotIn("Hőmérséklet", html)
        self.assertIn("nem olvasható", html)

    def test_health_still_returns_json(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data)
        self.assertEqual(data["status"], "ok")


class TestStatusAPIContract(unittest.TestCase):
    """
    Verify that /status returns all fields the JS template relies on:
    job_id, case_id, status, progress, step, error
    """
    def setUp(self):
        import tempfile
        self.tmp = Path(tempfile.mkdtemp())
        self.app = _make_app(self.tmp)
        self.client = self.app.test_client()

    def test_status_has_all_js_required_fields(self):
        resp = self.client.get("/status/anyjob")
        data = json.loads(resp.data)
        required = {"job_id", "case_id", "status", "progress", "step", "error"}
        missing = required - data.keys()
        self.assertFalse(missing, f"Missing fields in status response: {missing}")

    def test_status_progress_is_integer(self):
        resp = self.client.get("/status/anyjob")
        data = json.loads(resp.data)
        self.assertIsInstance(data["progress"], int)

    def test_status_job_id_matches(self):
        resp = self.client.get("/status/myjob123")
        data = json.loads(resp.data)
        self.assertEqual(data["job_id"], "myjob123")


class TestTemplateContent(unittest.TestCase):
    """Verify the template HTML structure without a live server."""

    def setUp(self):
        project_root = Path(__file__).parent.parent.parent
        self.template_path = project_root / "app" / "templates" / "index.html"

    def test_template_file_exists(self):
        self.assertTrue(self.template_path.exists(),
                        f"Template not found: {self.template_path}")

    def test_template_has_doctype(self):
        content = self.template_path.read_text(encoding="utf-8")
        self.assertIn("<!DOCTYPE html>", content)

    def test_template_has_meta_charset(self):
        content = self.template_path.read_text(encoding="utf-8")
        self.assertIn("charset", content.lower())

    def test_template_accepted_extensions_listed(self):
        content = self.template_path.read_text(encoding="utf-8")
        for ext in (".pdf", ".zip", ".docx"):
            self.assertIn(ext, content, f"Extension {ext} not found in template")

    def test_template_has_progress_bar_element(self):
        content = self.template_path.read_text(encoding="utf-8")
        self.assertIn("progress", content.lower())

    def test_template_has_error_display(self):
        content = self.template_path.read_text(encoding="utf-8")
        # The JS renders error-box elements
        self.assertIn("error", content.lower())

    def test_template_sends_the_destination_with_the_upload(self):
        content = self.template_path.read_text(encoding="utf-8")
        self.assertIn("formData.append('dest_dir'", content)
        self.assertIn("formData.append('make_zip'", content)

    def test_template_resends_the_destination_when_unlocking(self):
        """A password-protected case is enqueued by a second request, which
        would otherwise lose the folder the user picked."""
        content = self.template_path.read_text(encoding="utf-8")
        unlock_call = content.split("'/upload/unlock'")[1][:400]
        self.assertIn("dest_dir", unlock_call)
        self.assertIn("make_zip", unlock_call)

    def test_template_has_polling_interval(self):
        content = self.template_path.read_text(encoding="utf-8")
        self.assertIn("setInterval", content)

    def test_template_has_status_labels_for_all_job_statuses(self):
        content = self.template_path.read_text(encoding="utf-8")
        for status in ("QUEUED", "INGESTING", "ROUTING", "ANCHORING",
                       "EXTRACTING", "MERGING", "EXPORTING", "DONE", "FAILED"):
            self.assertIn(status, content,
                          f"Status '{status}' not handled in template JS")

    def test_template_has_hungarian_ui_text(self):
        """The UI should contain Hungarian labels (the clinical team's language)."""
        content = self.template_path.read_text(encoding="utf-8")
        # At least one Hungarian word must appear
        hungarian_words = ["Feltöltés", "Feldolgozás", "Eredmény", "Hiba"]
        found = any(w in content for w in hungarian_words)
        self.assertTrue(found, "No Hungarian UI text found in template")


def tearDownModule() -> None:
    """Restore the real ``config`` module so subsequent test files are not
    affected by the Mock that ``_install_config_mock`` injects."""
    _restore_config_module()


def run_all() -> bool:
    loader = unittest.TestLoader()
    suite  = unittest.TestSuite()
    for cls in (
        TestIndexRoute,
        TestStatusAPIContract,
        TestTemplateContent,
    ):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
