"""
worker.py — RQ worker entry point.

Run in a separate terminal:
    python worker.py

The worker imports create_app so that:
- structlog is configured
- the field registry is loaded and cached
- all pipeline modules are importable in the worker process

The worker's RQ name carries the fingerprint of the code it started with
(app/services/code_version.py): jobs are refused once the files on disk
differ, and the upload page warns about such a worker.  At start it also
unregisters killed workers of this host (app/services/workers.py) and prunes
expired staged uploads and job-state files.
"""
from __future__ import annotations

import sys

import structlog
from rq import SimpleWorker, Worker
from rq.timeouts import TimerDeathPenalty

log = structlog.get_logger(__name__)


class WindowsSimpleWorker(SimpleWorker):
    """SimpleWorker that enforces job timeouts with a thread timer.

    RQ's default death penalty arms ``signal.SIGALRM``, which does not exist on
    Windows, so every job died with ``AttributeError: module 'signal' has no
    attribute 'SIGALRM'`` raised *before* the job function was ever called.
    RQ recorded the job as failed with no traceback, and because the pipeline
    never started, no job-state file was written and the UI showed the case as
    "Ismeretlen" forever.
    """

    death_penalty_class = TimerDeathPenalty


def main() -> None:
    """
    Start the RQ worker process.

    Bootstraps the Flask application context so that config, the field registry,
    and structlog are all available inside pipeline job functions that run in this
    worker process.  Blocks until interrupted (SIGINT / SIGTERM).
    """
    # First: fingerprint the code this worker starts with, so process_case()
    # can refuse jobs once the files on disk have moved on (code_version.py).
    from app.services import code_version

    # Bootstrap app context so config, registry, and logging are all available
    # inside job functions that run in this process.
    from app import create_app
    from app.extensions import get_redis

    app = create_app()

    with app.app_context():
        redis_conn = get_redis()
        queues = ["default"]

        # Windows has no os.fork(), which rq.Worker.work() relies on to
        # spawn its work-horse subprocess. SimpleWorker runs jobs in the
        # same process — slightly less isolation, but it actually works.
        # It also needs a non-signal death penalty; see WindowsSimpleWorker.
        #
        # Dev/prod parity note: this fallback exists ONLY for bare-metal
        # Windows development. The supported parity path is
        # `docker compose up` (see docker-compose.yml), where the worker
        # always runs in a Linux container and therefore always uses the
        # forking rq.Worker — identical to production on every host OS.
        worker_cls = WindowsSimpleWorker if sys.platform == "win32" else Worker

        # Housekeeping before taking jobs: forget workers of this host that
        # were killed (and fail the case each was running), then drop expired
        # staged uploads and job-state files.
        from app.pipeline.orchestrator import prune_expired
        from app.services.workers import clean_dead_workers

        clean_dead_workers(redis_conn)
        prune_expired()

        name = code_version.worker_name()
        log.info("worker_starting", queues=queues, worker_cls=worker_cls.__name__,
                 name=name)

        worker = worker_cls(queues, name=name, connection=redis_conn)
        try:
            worker.work(with_scheduler=True)
        except KeyboardInterrupt:
            log.info("worker_stopped")
            sys.exit(0)


if __name__ == "__main__":
    main()
