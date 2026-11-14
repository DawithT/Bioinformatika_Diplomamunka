"""
tests/unit/test_worker.py

Guards the RQ worker's platform wiring.

The checks run in a subprocess on purpose: test_blueprints.py and test_ui.py
install ``MagicMock`` stubs for "rq"/"redis"/"structlog" in ``sys.modules``, so
importing the real ``rq.timeouts`` in-process fails with "'rq' is not a package"
depending on test order.  A clean interpreter also mirrors how the worker
actually starts.

Run with:
    py -3.13 -m pytest tests/unit/test_worker.py -q
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT), capture_output=True, text=True,
    )


class TestWindowsWorkerTimeout(unittest.TestCase):
    """The Windows worker must not arm a POSIX-only signal.

    RQ's default death penalty calls ``signal.signal(signal.SIGALRM, ...)``.
    Windows has no SIGALRM, so every job raised ``AttributeError`` before the
    job function was ever entered: RQ recorded the job as failed with no
    traceback, no job-state file was written, and the UI sat on "Ismeretlen"
    with an empty progress bar.  Nothing was ever processed on Windows.
    """

    def test_windows_worker_uses_timer_death_penalty(self):
        result = _run(
            "from rq.timeouts import TimerDeathPenalty\n"
            "from worker import WindowsSimpleWorker\n"
            "assert WindowsSimpleWorker.death_penalty_class is TimerDeathPenalty, \\\n"
            "    WindowsSimpleWorker.death_penalty_class\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_timer_death_penalty_arms_without_sigalrm(self):
        """Arming the chosen penalty must not touch signal.SIGALRM."""
        result = _run(
            "import signal\n"
            "from rq.timeouts import TimerDeathPenalty\n"
            "class Stub(BaseException): pass\n"
            "p = TimerDeathPenalty(60, Stub, job_id='probe')\n"
            "p.setup_death_penalty()\n"
            "p.cancel_death_penalty()\n"
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_worker_is_named_with_its_code_fingerprint(self):
        """The upload page tells outdated workers apart by this name."""
        source = (ROOT / "worker.py").read_text(encoding="utf-8")
        self.assertIn("name = code_version.worker_name()", source)
        self.assertIn("worker_cls(queues, name=name, connection=redis_conn)", source)

    def test_posix_still_uses_the_forking_worker(self):
        """The Windows branch must not change behaviour on Linux/macOS."""
        source = (ROOT / "worker.py").read_text(encoding="utf-8")
        self.assertIn(
            'worker_cls = WindowsSimpleWorker if sys.platform == "win32" else Worker',
            source,
        )


def run_all() -> bool:
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromTestCase(TestWindowsWorkerTimeout)
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
