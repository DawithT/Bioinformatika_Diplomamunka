"""
tests/unit/test_run_config.py

Tests for app/services/run_config.py — the configuration description shared
by the GUI panel, the prompt-cache scope and <case_id>_run_config.json.
"""
from __future__ import annotations

from app.services import llm_client
from app.services.run_config import describe_run
from config import settings


class TestDescribeRun:

    def test_llm_on_lists_the_values_actually_sent(self):
        cfg = describe_run(True)
        llm = cfg["llm"]
        assert llm["enabled"] is True
        assert llm["temperature"] == llm_client.LLM_TEMPERATURE == 0.0
        assert llm["max_tokens"] == llm_client.LLM_MAX_TOKENS
        assert llm["timeout_s"] == llm_client.LLM_TIMEOUT_S
        assert llm["model"] == settings.llm_model
        assert llm["max_retries"] == settings.llm_max_retries
        assert len(llm["prompts_sha256"]) == 64

    def test_llm_off_lists_no_parameters_and_no_cache(self):
        cfg = describe_run(False)
        assert cfg["llm"] == {"enabled": False}
        assert cfg["cache"]["enabled"] is False

    def test_cache_follows_enable_llm_cache(self, monkeypatch):
        monkeypatch.setattr(settings, "enable_llm_cache", False)
        assert describe_run(True)["cache"]["enabled"] is False
        monkeypatch.setattr(settings, "enable_llm_cache", True)
        assert describe_run(True)["cache"]["enabled"] is True

    def test_registry_is_identified_by_hash(self):
        reg = describe_run(False)["registry"]
        assert reg["file"] == settings.registry_path_obj.name
        assert reg["sha256"] is not None and len(reg["sha256"]) == 64

    def test_model_change_changes_the_description(self, monkeypatch):
        """The cache scope is built from this dict, so a model change must show."""
        before = describe_run(True)
        monkeypatch.setattr(settings, "llm_model", "another-model:7b")
        assert describe_run(True) != before
