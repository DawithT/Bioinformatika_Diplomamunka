"""
tests/unit/test_workers.py

Worker registry (app/services/workers.py) and the housekeeping that goes
with it in the orchestrator: failing a job whose worker died, and pruning
expired staged uploads and job-state files.

Run with:
    py -3.13 -m pytest tests/unit/test_workers.py -q
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import code_version, workers


def _dead_pid() -> int:
    """PID of a process that has already exited."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _worker(name="crreg.000000000000.aaaa0000", pid=None, host=None, job=None):
    return types.SimpleNamespace(
        name=name,
        pid=os.getpid() if pid is None else pid,
        hostname=socket.gethostname() if host is None else host,
        register_death=MagicMock(),
        get_current_job_id=MagicMock(return_value=job),
    )


def _fake_rq(found=None, error: Exception | None = None):
    """A stand-in ``rq`` module whose Worker.all returns *found*."""
    rq = types.ModuleType("rq")
    rq.Worker = MagicMock()
    if error is not None:
        rq.Worker.all.side_effect = error
    else:
        rq.Worker.all.return_value = found or []
    return rq


def _fake_config(root: Path):
    """A stand-in ``config`` module with its folders under *root*."""
    cfg = types.ModuleType("config")
    cfg.settings = types.SimpleNamespace(
        upload_folder_path=root / "uploads",
        jobs_folder_path=root / "jobs",
    )
    (root / "uploads").mkdir()
    (root / "jobs").mkdir()
    return cfg


class TestIsDead(unittest.TestCase):

    def test_running_process_is_alive(self):
        self.assertFalse(workers.is_dead(_worker(pid=os.getpid())))

    def test_exited_process_is_dead(self):
        self.assertTrue(workers.is_dead(_worker(pid=_dead_pid())))

    def test_other_host_cannot_be_checked_and_counts_as_alive(self):
        self.assertFalse(workers.is_dead(_worker(pid=_dead_pid(), host="elsewhere")))


class TestWorkerProblems(unittest.TestCase):

    def _problems(self, **kw):
        with patch.dict(sys.modules, {"rq": _fake_rq(**kw)}):
            return workers.worker_problems(connection=object())

    def test_worker_on_current_code_is_fine(self):
        good = _worker(name=f"crreg.{code_version.current()}.abcd1234")
        self.assertEqual(self._problems(found=[good]), [])

    def test_no_worker_is_reported(self):
        problems = self._problems(found=[])
        self.assertEqual(len(problems), 1)
        self.assertIn("Nem fut feldolgozó", problems[0])

    def test_only_dead_workers_count_as_no_worker(self):
        """A killed worker stays registered for up to an hour."""
        dead = _worker(name=f"crreg.{code_version.current()}.dead0000", pid=_dead_pid())
        problems = self._problems(found=[dead])
        self.assertEqual(len(problems), 1)
        self.assertIn("Nem fut feldolgozó", problems[0])

    def test_outdated_worker_is_reported_with_pid(self):
        good = _worker(name=f"crreg.{code_version.current()}.aaaa0000")
        old = _worker(name="crreg.000000000000.bbbb1111")
        problems = self._problems(found=[good, old])
        self.assertEqual(len(problems), 1)
        self.assertIn("crreg.000000000000.bbbb1111", problems[0])
        self.assertIn(str(os.getpid()), problems[0])

    def test_worker_without_fingerprint_counts_as_outdated(self):
        """e.g. a plain `rq worker` with a uuid name."""
        problems = self._problems(found=[_worker(name="9f3d61e624214ae1a1cddbaafd0f1f07")])
        self.assertEqual(len(problems), 1)

    def test_redis_error_is_reported_not_raised(self):
        problems = self._problems(error=ConnectionError("refused"))
        self.assertEqual(len(problems), 1)
        self.assertIn("refused", problems[0])


class TestCleanDeadWorkers(unittest.TestCase):

    def test_dead_worker_is_unregistered_and_its_job_failed(self):
        alive = _worker(job=b"livejob")
        dead = _worker(pid=_dead_pid(), job=b"deadjob")
        with patch.dict(sys.modules, {"rq": _fake_rq(found=[alive, dead])}), \
             patch("app.pipeline.orchestrator.fail_interrupted") as mock_fail:
            workers.clean_dead_workers(connection=object())
        dead.register_death.assert_called_once()
        alive.register_death.assert_not_called()
        mock_fail.assert_called_once_with("deadjob")

    def test_idle_dead_worker_fails_no_job(self):
        dead = _worker(pid=_dead_pid(), job=None)
        with patch.dict(sys.modules, {"rq": _fake_rq(found=[dead])}), \
             patch("app.pipeline.orchestrator.fail_interrupted") as mock_fail:
            workers.clean_dead_workers(connection=object())
        dead.register_death.assert_called_once()
        mock_fail.assert_not_called()


class _WithFolders(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self._cfg = patch.dict(sys.modules, {"config": _fake_config(self.root)})
        self._cfg.start()
        from app.pipeline import orchestrator
        self.orch = orchestrator

    def tearDown(self):
        self._cfg.stop()

    def _state(self, job_id, **fields):
        path = self.root / "jobs" / f"{job_id}.json"
        path.write_text(json.dumps({"job_id": job_id, "progress": 60,
                                    "step": "x", "error": None, **fields}))
        return path

    @staticmethod
    def _age(path: Path, seconds: float):
        t = time.time() - seconds
        os.utime(path, (t, t))


class TestFailInterrupted(_WithFolders):

    def test_running_job_fails_and_its_documents_go(self):
        case = self.root / "uploads" / "case01"
        case.mkdir()
        (case / "doc.pdf").write_bytes(b"x")
        self._state("job1", case_id="case01", status="EXTRACTING")
        self.orch.fail_interrupted("job1")
        state = self.orch.read_job_state("job1")
        self.assertEqual(state["status"], "FAILED")
        self.assertIn("worker stopped", state["error"])
        self.assertFalse(case.exists())

    def test_finished_job_is_left_alone(self):
        self._state("job2", case_id="case02", status="DONE")
        self.orch.fail_interrupted("job2")
        self.assertEqual(self.orch.read_job_state("job2")["status"], "DONE")

    def test_unknown_job_writes_nothing(self):
        self.orch.fail_interrupted("nojob")
        self.assertEqual(list((self.root / "jobs").iterdir()), [])


class TestPruneExpired(_WithFolders):

    def test_old_staged_upload_goes_fresh_one_stays(self):
        old = self.root / "uploads" / "abandoned"
        old.mkdir()
        (old / "locked.zip").write_bytes(b"PK")
        self._age(old, self.orch.STAGED_UPLOAD_RETENTION_S + 60)
        fresh = self.root / "uploads" / "waiting"
        fresh.mkdir()
        self.orch.prune_expired()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_old_job_state_goes_fresh_one_stays(self):
        old = self._state("old", case_id="c", status="FAILED")
        tmp = self.root / "jobs" / "half.tmp"
        tmp.write_text("{")
        for p in (old, tmp):
            self._age(p, self.orch.JOB_STATE_RETENTION_S + 60)
        fresh = self._state("fresh", case_id="c", status="DONE")
        self.orch.prune_expired()
        self.assertFalse(old.exists())
        self.assertFalse(tmp.exists())
        self.assertTrue(fresh.exists())

    def test_job_state_outlives_the_upload_retention(self):
        state = self._state("recent", case_id="c", status="DONE")
        self._age(state, self.orch.STAGED_UPLOAD_RETENTION_S + 60)
        self.orch.prune_expired()
        self.assertTrue(state.exists())


def run_all() -> bool:
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for cls in (TestIsDead, TestWorkerProblems, TestCleanDeadWorkers,
                TestFailInterrupted, TestPruneExpired):
        suite.addTests(loader.loadTestsFromTestCase(cls))
    return unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_all() else 1)
