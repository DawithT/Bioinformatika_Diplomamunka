"""
app/extensions.py — Shared extension instances.

Pattern: instantiate here (no app context needed), initialise in the
app factory (create_app).  Avoids circular imports.
"""
from __future__ import annotations

import logging
import sys

import redis
import structlog
from rq import Queue


# ── Redis connection (lazy — validated in create_app) ─────────────────────────
_redis_conn: redis.Redis | None = None
_rq_queue: Queue | None = None


def get_redis() -> redis.Redis:
    """Return the shared Redis connection, raising clearly if not initialised."""
    if _redis_conn is None:
        raise RuntimeError("Redis has not been initialised. Call init_extensions() first.")
    return _redis_conn


def get_queue() -> Queue:
    """Return the shared RQ job queue."""
    if _rq_queue is None:
        raise RuntimeError("RQ queue has not been initialised. Call init_extensions() first.")
    return _rq_queue


def init_extensions(redis_url: str) -> None:
    """
    Connect to Redis and create the RQ queue.
    Called once from create_app().
    Raises redis.ConnectionError immediately if Redis is unreachable —
    better to fail loudly at startup than silently at the first upload.
    """
    global _redis_conn, _rq_queue

    conn = redis.from_url(redis_url, decode_responses=False, socket_connect_timeout=3)
    conn.ping()  # raises redis.ConnectionError if Redis is not running
    _redis_conn = conn
    _rq_queue = Queue("default", connection=conn)


# ── Structlog configuration ───────────────────────────────────────────────────

def configure_logging(log_level: str = "INFO") -> None:
    """
    Set up structlog with:
    - Console: human-readable coloured output in development
    - Standard library logging bridge (so Flask/Werkzeug logs go through structlog)
    """
    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer(colors=True),
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers = [handler]
    root_logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))

    # Quiet down noisy libraries
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    logging.getLogger("rq.worker").setLevel(logging.INFO)
