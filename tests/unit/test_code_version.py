"""
tests/unit/test_code_version.py

The code fingerprint that keeps an outdated RQ worker from processing cases
(app/services/code_version.py).

Run with:
    py -3.13 -m pytest tests/unit/test_code_version.py -q
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from app.services import code_version


class TestFingerprint(unittest.TestCase):
    """Fingerprint a throw-away project tree instead of the real one."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "app" / "pipeline").mkdir(parents=True)
        (self.root / "app" / "pipeline" / "step.py").write_text("x = 1\n")
        (self.root / "config.py").write_text("A = 1\n")
        self._saved = (code_version._ROOT, code_version.STARTUP)
        code_version._ROOT = self.root
        code_version.STARTUP = code_version.current()

    def tearDown(self):
        code_version._ROOT, code_version.STARTUP = self._saved

    def test_unchanged_files_are_not_stale(self):
        self.assertFalse(code_version.is_stale())

    def test_edited_module_is_stale(self):
        (self.root / "app" / "pipeline" / "step.py").write_text("x = 2\n")
        self.assertTrue(code_version.is_stale())

    def test_new_module_is_stale(self):
        (self.root / "app" / "new.py").write_text("")
        self.assertTrue(code_version.is_stale())

    def test_env_settings_count(self):
        """Settings are read once at start, so a new .env means a stale worker."""
        (self.root / ".env").write_text("LLM_MODEL=other\n")
        self.assertTrue(code_version.is_stale())

    def test_worker_name_carries_startup_fingerprint(self):
        name = code_version.worker_name()
        self.assertEqual(code_version.fingerprint_of(name), code_version.STARTUP)

    def test_foreign_name_has_no_fingerprint(self):
        self.assertIsNone(code_version.fingerprint_of("9f3d61e624214ae1a1cddbaafd0f1f07"))

    def test_non_python_files_do_not_count(self):
        """The registry is reloaded by every job; data files are not code."""
        (self.root / "app" / "notes.txt").write_text("hello")
        self.assertFalse(code_version.is_stale())


def run_all() -> bool:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestFingerprint,):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
