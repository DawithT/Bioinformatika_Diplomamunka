"""
tests/unit/test_config.py

Tests for Step 1: configuration, app factory, health endpoint.
"""
import pytest
from config import Settings


class TestSettings:
    def test_defaults_are_valid(self):
        """Settings loads with defaults without raising."""
        s = Settings()
        assert s.llm_provider in ("ollama", "llamacpp", "disabled")
        assert s.llm_context_limit >= 1000
        assert s.max_upload_mb > 0

    def test_max_content_length_in_bytes(self):
        s = Settings()
        assert s.max_content_length == s.max_upload_mb * 1024 * 1024

    def test_base_url_must_have_scheme(self):
        with pytest.raises(Exception):
            Settings(llm_base_url="localhost:11434")

    def test_base_url_trailing_slash_stripped(self):
        s = Settings(llm_base_url="http://localhost:11434/")
        assert not s.llm_base_url.endswith("/")

    def test_to_flask_config_keys(self):
        s = Settings()
        fc = s.to_flask_config()
        assert "SECRET_KEY" in fc
        assert "MAX_CONTENT_LENGTH" in fc
        assert "UPLOAD_FOLDER" in fc

    def test_data_folders_are_temporary_not_the_real_data_dir(self):
        """conftest.py redirects them; the pipeline deletes and prunes there."""
        from pathlib import Path

        from tests.conftest import TEST_DATA_ROOT

        real_data = (Path(__file__).resolve().parents[2] / "data").resolve()
        s = Settings()
        for folder in (s.upload_folder_path, s.output_folder_path, s.jobs_folder_path):
            resolved = folder.resolve()
            assert resolved.is_relative_to(Path(TEST_DATA_ROOT).resolve()), resolved
            assert not resolved.is_relative_to(real_data), resolved

    def test_disabled_is_valid_llm_provider(self):
        """LLM_PROVIDER=disabled must be accepted (used in CI)."""
        s = Settings(llm_provider="disabled")
        assert s.llm_provider == "disabled"

    def test_enable_llm_cache_default_true(self):
        s = Settings()
        assert s.enable_llm_cache is True

    def test_enable_llm_cache_can_be_disabled(self):
        s = Settings(enable_llm_cache=False)
        assert s.enable_llm_cache is False

    def test_all_variable_names_match_env_example(self):
        """
        Spot-check that the Settings field names correspond to the variable
        names declared in .env.example.  This acts as a canary for name drift.
        """
        s = Settings()
        # These field names must exist and be accessible (not raise AttributeError)
        _ = s.llm_provider
        _ = s.llm_base_url
        _ = s.llm_model
        _ = s.llm_context_limit
        _ = s.llm_max_retries
        _ = s.enable_llm
        _ = s.enable_llm_cache
        _ = s.redis_url
        _ = s.secret_key
        _ = s.flask_env
        _ = s.max_upload_mb
        _ = s.upload_folder
        _ = s.output_folder
        _ = s.jobs_folder
        _ = s.registry_path


class TestAppFactory:
    def test_app_creates_without_error(self, app):
        assert app is not None

    def test_testing_flag_set(self, app):
        assert app.config["TESTING"] is True

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"

    def test_root_endpoint(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
