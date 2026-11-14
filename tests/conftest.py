"""
tests/conftest.py — Shared pytest fixtures.
"""
from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

# Point tests at a test .env before anything else imports config
os.environ.setdefault("ENABLE_LLM", "false")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/1")  # DB 1 = test isolation
os.environ.setdefault("REGISTRY_PATH", "config/Registry_base.xlsx")

# Upload, output and job-state folders live in a temporary directory for the
# session, never under the real data/ folder — process_case() deletes staged
# uploads and prunes old job states there.  Set unconditionally, so a value
# exported in the shell cannot point the tests at real folders.
TEST_DATA_ROOT = tempfile.mkdtemp(prefix="crreg-tests-")
atexit.register(shutil.rmtree, TEST_DATA_ROOT, ignore_errors=True)
os.environ["UPLOAD_FOLDER"] = os.path.join(TEST_DATA_ROOT, "uploads")
os.environ["OUTPUT_FOLDER"] = os.path.join(TEST_DATA_ROOT, "out")
os.environ["JOBS_FOLDER"] = os.path.join(TEST_DATA_ROOT, "jobs")


@pytest.fixture(scope="session")
def app():
    """
    Create a Flask test app.
    Uses testing=True to skip Redis init (no live Redis required for unit tests).
    """
    from app import create_app
    return create_app(testing=True)


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()
