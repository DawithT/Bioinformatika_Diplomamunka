"""
app/services/workers.py

The RQ worker registry, with dead registrations told apart from live ones.

A worker that is killed (console window closed, crash, TerminateProcess)
never unregisters itself.  RQ drops its entry only when the heartbeat key
expires, and a SimpleWorker (Windows) sets that key to the job timeout + 60 s
when a job starts and sends no heartbeat during the job, so a worker killed
mid-job stays listed for up to an hour.  Shortening the TTL would not help
for that reason.  Instead a registration made on this host is checked
against its PID: if that process is gone, the worker is dead.

worker_problems(conn)
    Hungarian warnings for the upload page: no live worker, or a live worker
    running code older than the files on disk (app.services.code_version).
is_dead(worker)
    True when *worker* registered from this host and its process is gone.
    A worker on another host (e.g. another container) cannot be checked and
    counts as alive.
clean_dead_workers(conn)
    Called once at worker start: unregisters dead workers and fails the job
    each one was running, which also deletes that job's staged documents.
"""
from __future__ import annotations

import logging
import os
import socket
import sys

from app.services import code_version

log = logging.getLogger(__name__)


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5  # access denied: it exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_dead(worker) -> bool:
    """True when *worker* registered from this host and its process is gone."""
    if worker.hostname != socket.gethostname() or not worker.pid:
        return False
    return not _pid_alive(int(worker.pid))


def live_workers(connection) -> list:
    """Registered workers, without the dead registrations of this host."""
    from rq import Worker

    return [w for w in Worker.all(connection=connection) if not is_dead(w)]


def clean_dead_workers(connection) -> None:
    """Unregister dead workers of this host and fail the job each was running."""
    from rq import Worker

    from app.pipeline.orchestrator import fail_interrupted

    for w in Worker.all(connection=connection):
        if not is_dead(w):
            continue
        job_id = w.get_current_job_id()
        w.register_death()
        log.warning("dead_worker_unregistered: name=%s pid=%s job=%s",
                    w.name, w.pid, job_id)
        if job_id:
            fail_interrupted(job_id.decode() if isinstance(job_id, bytes) else job_id)


def worker_problems(connection) -> list[str]:
    """
    Return warnings (Hungarian, for the upload page) about the RQ workers.

    Empty when at least one live worker is registered and every live worker
    runs the code on disk.  A worker started some other way (``rq worker``)
    carries no fingerprint and counts as outdated.
    """
    try:
        workers = live_workers(connection)
    except Exception as exc:  # noqa: BLE001 — Redis down, etc.
        return [f"A feldolgozók állapota nem kérdezhető le: {exc}"]

    if not workers:
        return ["Nem fut feldolgozó (RQ worker): a feltöltött esetek a sorban "
                "maradnak. Indítsd el a start.bat-tal (vagy: python worker.py)."]

    on_disk = code_version.current()
    problems = []
    for w in workers:
        if code_version.fingerprint_of(w.name) != on_disk:
            problems.append(
                f"A(z) {w.name} feldolgozó (PID {w.pid}, {w.hostname}) régebbi "
                "kóddal vagy beállításokkal fut, mint ami a lemezen van, ezért "
                "nem dolgoz fel eseteket. Állítsd le, és indítsd újra a start.bat-tal."
            )
    return problems
