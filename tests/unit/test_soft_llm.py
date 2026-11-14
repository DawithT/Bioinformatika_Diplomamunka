"""
tests/unit/test_soft_llm.py

Tests for Step 10: LLM client abstraction + soft field extraction.

Covers:
  - build_user_prompt()         — prompt format, enum constraints, retry errors
  - extract_json_from_response()  — clean JSON, fenced, embedded
  - validate_enum_fields()     — valid / invalid enum values, special markers
  - DisabledClient              — always returns {}, no LLM call
  - OllamaClient / LlamaCppClient — constructed correctly (no network needed)
  - get_llm_client()            — ENABLE_LLM=false → Disabled
  - ConfigurationError          — unknown LLM_PROVIDER
  - PromptCache                 — get/set/miss, disabled cache
  - _select_context()           — page selection by keyword scoring
  - _fields_for_doc()           — source_policy filtering
  - SoftExtractor.extract()     — DisabledClient → all "Unknown"
  - SoftExtractor.extract()     — mock client → values mapped to ExtractedField
  - SoftExtractor               — missing LLM keys → "Unknown"
  - SoftExtractor               — cache hit avoids second LLM call

Run with:
    PYTHONPATH=. \\
        python3 tests/unit/test_soft_llm.py
"""
from __future__ import annotations

import json
import sys
import traceback
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

# ── Imports ───────────────────────────────────────────────────────────────────

from app.domain.enums import DocType, FieldSource
from app.domain.models import PatientAnchor, RoutedDoc
from app.registry.models import FieldDefinition
from app.services.cache import PromptCache
from app.pipeline.step5_extract.prompts import (
    SYSTEM_PROMPT,
    build_user_prompt,
    extract_json_from_response,
    validate_enum_fields,
)
from app.services.llm_client import (
    ConfigurationError,
    DisabledClient,
    LlamaCppClient,
    OllamaClient,
    check_llm_endpoint,
    get_llm_client,
)
from app.pipeline.step5_extract.soft_llm import (
    SoftExtractor,
    _fields_for_doc,
    _score_page,
    _select_context,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

ANCHOR = PatientAnchor.create(
    name="Beteg Neve",
    dob=date(1960, 1, 1),
    taj="111-222-333",
    admission_start=datetime(2026, 1, 12, 4, 43),
    discharge_end=datetime(2026, 1, 20, 13, 32),
)


def _make_doc(text: str, doc_type: DocType = DocType.DISCHARGE_SUMMARY) -> RoutedDoc:
    return RoutedDoc(
        path=Path("test.txt"),
        doc_type=doc_type,
        pages=(text,),
        tables=(),
        meta={},
    )


def _make_fd(
    field_name: str,
    field_type: str = "open",
    allowed_values: tuple = (),
    source_policy: str = "DISCHARGE_PRIMARY",
    section_hint: str = "Anamnézis, jelen panaszok",
    list_storage_format: str | None = None,
    date_format_hint: str | None = None,
    date_granularity_allowed: str | None = None,
    routing: tuple = (),
) -> FieldDefinition:
    """Minimal FieldDefinition factory for tests."""
    return FieldDefinition(
        field_name=field_name,
        field_base=field_name,
        column_header=field_name.replace("_", " ").title(),
        form_id="A",
        timepoint="anam",
        field_type=field_type,
        value_type="enum" if field_type == "enum" else "text",
        allowed_values=allowed_values,
        enum_set=None,
        source_policy=source_policy,
        section_hint=section_hint,
        routing=routing,
        repeat_group=None,
        scope=None,
        canonical=False,
        dedupe_key=None,
        multiple_answers_allowed=False,
        missing_marker="NA",
        not_applicable_marker="ERVENYTELEN",
        condition_parent_field=None,
        condition_operator=None,
        condition_value=None,
        date_format_hint=date_format_hint,
        date_granularity_allowed=date_granularity_allowed,
        list_storage_format=list_storage_format,
        notes=None,
        unit=None,
    )


# ── Tests: build_user_prompt ──────────────────────────────────────────────────

class TestBuildUserPrompt:

    def test_contains_section_text(self):
        fd = _make_fd("demo")
        prompt = build_user_prompt("Some section text.", [fd])
        assert "Some section text." in prompt

    def test_contains_field_name(self):
        fd = _make_fd("ascites_grade")
        prompt = build_user_prompt("text", [fd])
        assert "ascites_grade" in prompt

    def test_enum_field_shows_allowed_values(self):
        fd = _make_fd("grade", field_type="enum", allowed_values=("Igen", "Nem"))
        prompt = build_user_prompt("text", [fd])
        assert '"Igen"' in prompt
        assert '"Nem"' in prompt

    def test_open_field_shows_string_type(self):
        fd = _make_fd("notes", field_type="open")
        prompt = build_user_prompt("text", [fd])
        assert "string" in prompt

    def test_retry_includes_validation_errors(self):
        fd = _make_fd("grade", field_type="enum", allowed_values=("Igen", "Nem"))
        prompt = build_user_prompt("text", [fd], validation_errors=["Field X invalid."])
        assert "Field X invalid." in prompt
        assert "javítsd" in prompt.lower() or "hiba" in prompt.lower()

    def test_no_retry_errors_when_none(self):
        fd = _make_fd("grade")
        prompt = build_user_prompt("text", [fd], validation_errors=None)
        assert "javítsd" not in prompt.lower()

    def test_section_text_capped_at_4000_chars(self):
        long_text = "x" * 6000
        fd = _make_fd("demo")
        prompt = build_user_prompt(long_text, [fd])
        # The prompt as a whole will be > 4000, but section text is capped
        assert "x" * 4001 not in prompt

    def test_multiple_fields_all_present(self):
        fields = [_make_fd(n) for n in ["field_a", "field_b", "field_c"]]
        prompt = build_user_prompt("text", fields)
        for name in ["field_a", "field_b", "field_c"]:
            assert name in prompt

    def test_column_header_present(self):
        fd = _make_fd("ascites_grade")
        prompt = build_user_prompt("text", [fd])
        assert "Ascites Grade" in prompt or "ascites_grade" in prompt.lower()

    def test_date_format_hint_shown(self):
        fd = _make_fd("diagnosis_date", date_format_hint="YYYY[-MM[-DD]]")
        prompt = build_user_prompt("text", [fd])
        assert "date" in prompt
        assert "YYYY[-MM[-DD]]" in prompt

    def test_list_enum_field_shows_array_hint(self):
        fd = _make_fd(
            "complications",
            field_type="enum",
            allowed_values=("Ascites", "HE", "SBP"),
            list_storage_format="json_string",
        )
        prompt = build_user_prompt("text", [fd])
        assert "TÖBB" in prompt or "tömb" in prompt
        assert '"Ascites"' in prompt
        assert '"HE"' in prompt


# ── Tests: extract_json_from_response ───────────────────────────────────────

class TestExtractJsonFromResponse:

    def test_clean_json(self):
        r = extract_json_from_response('{"field": "value"}')
        assert r == {"field": "value"}

    def test_json_in_markdown_fence(self):
        r = extract_json_from_response('```json\n{"a": "1"}\n```')
        assert r == {"a": "1"}

    def test_json_in_plain_fence(self):
        r = extract_json_from_response('```\n{"a": "1"}\n```')
        assert r == {"a": "1"}

    def test_json_with_preamble(self):
        text = 'Here is the result:\n{"field": "val"}\nThank you.'
        r = extract_json_from_response(text)
        # May or may not extract — acceptable to return {} if preamble confuses
        # but must not raise
        assert isinstance(r, dict)

    def test_empty_string_returns_empty_dict(self):
        assert extract_json_from_response("") == {}

    def test_whitespace_only_returns_empty_dict(self):
        assert extract_json_from_response("   ") == {}

    def test_invalid_json_returns_empty_dict(self):
        assert extract_json_from_response("not json at all") == {}

    def test_nested_values(self):
        r = extract_json_from_response('{"a": "x", "b": "y"}')
        assert r == {"a": "x", "b": "y"}


# ── Tests: validate_enum_fields ─────────────────────────────────────────────

class TestValidateEnumFields:

    def test_valid_enum_no_errors(self):
        fd = _make_fd("grade", "enum", allowed_values=("Igen", "Nem"))
        errors = validate_enum_fields({"grade": "Igen"}, [fd])
        assert errors == []

    def test_invalid_enum_returns_error(self):
        fd = _make_fd("grade", "enum", allowed_values=("Igen", "Nem"))
        errors = validate_enum_fields({"grade": "Maybe"}, [fd])
        assert len(errors) == 1
        assert "grade" in errors[0]
        assert "Maybe" in errors[0]

    def test_unknown_always_valid(self):
        fd = _make_fd("grade", "enum", allowed_values=("Igen", "Nem"))
        errors = validate_enum_fields({"grade": "Unknown"}, [fd])
        assert errors == []

    def test_not_applicable_always_valid(self):
        fd = _make_fd("grade", "enum", allowed_values=("Igen", "Nem"))
        errors = validate_enum_fields({"grade": "Not applicable"}, [fd])
        assert errors == []

    def test_uncertain_always_valid(self):
        fd = _make_fd("grade", "enum", allowed_values=("Igen", "Nem"))
        errors = validate_enum_fields({"grade": "Uncertain"}, [fd])
        assert errors == []

    def test_open_field_never_errors(self):
        fd = _make_fd("notes", "open")
        errors = validate_enum_fields({"notes": "anything at all"}, [fd])
        assert errors == []

    def test_multiple_errors(self):
        fields = [
            _make_fd("a", "enum", allowed_values=("X", "Y")),
            _make_fd("b", "enum", allowed_values=("P", "Q")),
        ]
        errors = validate_enum_fields({"a": "Z", "b": "R"}, fields)
        assert len(errors) == 2

    def test_extra_key_not_in_fields_ignored(self):
        fd = _make_fd("grade", "enum", allowed_values=("Igen", "Nem"))
        errors = validate_enum_fields({"grade": "Igen", "extra": "anything"}, [fd])
        assert errors == []

    def test_list_field_valid_values_no_error(self):
        fd = _make_fd(
            "complications", "enum",
            allowed_values=("Ascites", "HE", "SBP"),
            list_storage_format="json_string",
        )
        errors = validate_enum_fields(
            {"complications": '["Ascites", "HE"]'}, [fd],
        )
        assert errors == []

    def test_list_field_invalid_item_returns_error(self):
        fd = _make_fd(
            "complications", "enum",
            allowed_values=("Ascites", "HE", "SBP"),
            list_storage_format="json_string",
        )
        errors = validate_enum_fields(
            {"complications": '["Ascites", "INVALID"]'}, [fd],
        )
        assert len(errors) == 1
        assert "INVALID" in errors[0]

    def test_list_field_single_string_accepted(self):
        fd = _make_fd(
            "complications", "enum",
            allowed_values=("Ascites", "HE", "SBP"),
            list_storage_format="json_string",
        )
        errors = validate_enum_fields({"complications": "Ascites"}, [fd])
        assert errors == []

    def test_partial_date_year_only_accepted(self):
        fd = _make_fd(
            "diagnosis_date",
            date_format_hint="YYYY[-MM[-DD]]",
            date_granularity_allowed="year",
        )
        errors = validate_enum_fields({"diagnosis_date": "2019"}, [fd])
        assert errors == []

    def test_partial_date_year_month_accepted(self):
        fd = _make_fd(
            "diagnosis_date",
            date_format_hint="YYYY[-MM[-DD]]",
            date_granularity_allowed="year",
        )
        errors = validate_enum_fields({"diagnosis_date": "2019-06"}, [fd])
        assert errors == []

    def test_partial_date_full_date_accepted(self):
        fd = _make_fd(
            "diagnosis_date",
            date_format_hint="YYYY[-MM[-DD]]",
            date_granularity_allowed="year",
        )
        errors = validate_enum_fields({"diagnosis_date": "2019-06-15"}, [fd])
        assert errors == []

    def test_season_expression_normalised(self):
        fd = _make_fd(
            "diagnosis_date",
            date_format_hint="YYYY[-MM[-DD]]",
            date_granularity_allowed="year",
        )
        result = {"diagnosis_date": "2019 őszén"}
        errors = validate_enum_fields(result, [fd])
        assert errors == []
        assert result["diagnosis_date"] == "2019-10"

    def test_season_tavasz_normalised(self):
        fd = _make_fd(
            "diagnosis_date",
            date_format_hint="YYYY[-MM[-DD]]",
            date_granularity_allowed="year",
        )
        result = {"diagnosis_date": "2020 tavasz"}
        errors = validate_enum_fields(result, [fd])
        assert errors == []
        assert result["diagnosis_date"] == "2020-04"


# ── Tests: DisabledClient ─────────────────────────────────────────────────────

class TestDisabledClient:

    def test_returns_empty_json_string(self):
        client = DisabledClient()
        result = client.chat(SYSTEM_PROMPT, "user prompt text")
        assert result == "{}"

    def test_never_raises(self):
        client = DisabledClient()
        result = client.chat(SYSTEM_PROMPT, "")
        assert result == "{}"

    def test_parses_to_empty_dict(self):
        """Contract: client returns '{}' string — SoftExtractor parses and owns Unknown mapping."""
        client = DisabledClient()
        raw = client.chat(SYSTEM_PROMPT, "user prompt text")
        parsed = extract_json_from_response(raw)
        assert parsed == {}
        assert "Unknown" not in parsed.values()


# ── Tests: Client construction (no network) ───────────────────────────────────

class TestClientConstruction:

    def test_ollama_client_created(self):
        client = OllamaClient(base_url="http://localhost:11434")
        assert isinstance(client, OllamaClient)

    def test_llamacpp_client_created(self):
        client = LlamaCppClient(base_url="http://localhost:8000")
        assert isinstance(client, LlamaCppClient)

    def test_ollama_custom_base_url(self):
        client = OllamaClient(base_url="http://192.168.1.50:5000")
        assert client._host == "192.168.1.50"
        assert client._port == 5000

    def test_llamacpp_custom_model(self):
        client = LlamaCppClient(model="custom-model")
        assert client._model == "custom-model"

    def test_public_host_refused_at_construction(self):
        with pytest.raises(ConfigurationError):
            OllamaClient(base_url="http://8.8.8.8:11434")


# ── Tests: endpoint guard (no network beyond localhost resolution) ────────────

class TestPreflightLlmCheck:

    def test_skipped_when_user_switched_llm_off(self, monkeypatch):
        """No LLM for this run → the LLM server is neither checked nor needed."""
        from app.pipeline.preflight import _check_llm_server
        from config import settings
        monkeypatch.setattr(settings, "enable_llm", True)
        monkeypatch.setattr(settings, "llm_provider", "ollama")
        monkeypatch.setattr(settings, "llm_base_url", "http://8.8.8.8:11434")
        assert _check_llm_server(use_llm=False) is None
        assert _check_llm_server(use_llm=True).severity == "critical"


class TestCheckLlmEndpoint:
    _MODEL = "qwen2.5:7b-instruct-q4_K_M"

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
        "http://10.0.0.5:8000",
        "http://172.16.4.2:8000",
        "http://192.168.1.50:11434",
    ])
    def test_local_and_private_hosts_allowed(self, url):
        check_llm_endpoint(url, self._MODEL)

    @pytest.mark.parametrize("url", [
        "http://8.8.8.8:11434",
        "http://[2001:4860:4860::8888]:11434",
    ])
    def test_public_address_refused(self, url):
        with pytest.raises(ConfigurationError, match="public address"):
            check_llm_endpoint(url, self._MODEL)

    def test_unresolvable_host_refused(self):
        with pytest.raises(ConfigurationError, match="Cannot resolve"):
            check_llm_endpoint("http://llm.nonexistent.invalid:11434", self._MODEL)

    def test_allowlisted_host_accepted_without_lookup(self):
        check_llm_endpoint(
            "http://llm.hospital.invalid:11434", self._MODEL,
            allowed_hosts="other.host, LLM.Hospital.invalid",
        )

    def test_allowlist_does_not_cover_other_hosts(self):
        with pytest.raises(ConfigurationError):
            check_llm_endpoint("http://8.8.8.8", self._MODEL, allowed_hosts="llm.hospital.invalid")

    @pytest.mark.parametrize("model", ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud", "kimi:cloud"])
    def test_ollama_cloud_models_refused(self, model):
        with pytest.raises(ConfigurationError, match="cloud model"):
            check_llm_endpoint("http://127.0.0.1:11434", model)

    def test_missing_host_refused(self):
        with pytest.raises(ConfigurationError, match="no host"):
            check_llm_endpoint("11434", self._MODEL)


# ── Tests: get_llm_client factory ─────────────────────────────────────────────

class TestGetLlmClient:
    """
    Tests for get_llm_client().

    get_llm_client() now reads config.settings instead of os.environ directly,
    so tests patch the settings singleton with unittest.mock.patch.object.
    """

    def _patch_settings(self, **kwargs: Any):
        """
        Replace the `settings` name inside app.services.llm_client with a
        MagicMock carrying the given overrides.

        Patching the name where it is *used* (not the BaseSettings object
        itself) is the only reliable approach with pydantic v2, whose
        __setattr__ and slot descriptors intercept object.__setattr__ calls.

        The mock is intentionally created without ``spec=Settings`` because
        other test files (test_blueprints.py, test_ui.py) install a Mock
        as ``sys.modules["config"]`` and never restore it, which would make
        ``config.Settings`` itself a Mock by the time this test runs and
        ``MagicMock(spec=...)`` would then raise InvalidSpecError.
        """
        from unittest.mock import MagicMock, patch
        import app.services.llm_client as _llm_module

        mock = MagicMock()
        # Seed sensible defaults so only the overridden keys differ.
        mock.enable_llm = True
        mock.llm_provider = "ollama"
        mock.llm_base_url = "http://127.0.0.1:11434"
        mock.llm_model = "qwen2.5:7b-instruct-q4_K_M"
        mock.llm_allowed_hosts = ""
        for k, v in kwargs.items():
            setattr(mock, k, v)

        return patch.object(_llm_module, 'settings', mock)

    def test_enable_llm_false_returns_disabled(self):
        with self._patch_settings(enable_llm=False):
            client = get_llm_client()
        assert isinstance(client, DisabledClient)

    def test_provider_disabled_returns_disabled(self):
        with self._patch_settings(enable_llm=True, llm_provider="disabled"):
            client = get_llm_client()
        assert isinstance(client, DisabledClient)

    def test_provider_ollama_returns_ollama(self):
        with self._patch_settings(enable_llm=True, llm_provider="ollama"):
            client = get_llm_client()
        assert isinstance(client, OllamaClient)

    def test_provider_llamacpp_returns_llamacpp(self):
        with self._patch_settings(enable_llm=True, llm_provider="llamacpp"):
            client = get_llm_client()
        assert isinstance(client, LlamaCppClient)

    def test_unknown_provider_raises_configuration_error(self):
        """Safety-net branch: bypass pydantic by patching the raw attribute."""
        with self._patch_settings(enable_llm=True, llm_provider="unknown_xyz"):
            try:
                get_llm_client()
                assert False, "Should have raised ConfigurationError"
            except ConfigurationError as e:
                assert "unknown_xyz" in str(e)


# ── Tests: PromptCache ────────────────────────────────────────────────────────

class TestPromptCache:

    def test_miss_returns_none(self):
        cache = PromptCache(enabled=True)
        assert cache.get("never-stored-prompt") is None

    def test_set_then_get_returns_value(self):
        cache = PromptCache(enabled=True)
        cache.set("prompt text", "response text")
        assert cache.get("prompt text") == "response text"

    def test_different_prompts_independent(self):
        cache = PromptCache(enabled=True)
        cache.set("prompt A", "response A")
        cache.set("prompt B", "response B")
        assert cache.get("prompt A") == "response A"
        assert cache.get("prompt B") == "response B"

    def test_new_instance_starts_empty(self):
        """Job-scoped: nothing carries over from a previous job's cache."""
        PromptCache(enabled=True).set("prompt", "response")
        assert PromptCache(enabled=True).get("prompt") is None

    def test_scope_separates_entries(self):
        """Same prompt under another model / registry must miss."""
        cache_a = PromptCache(enabled=True, scope="model-a")
        cache_a.set("prompt", "response")
        assert cache_a.get("prompt") == "response"
        assert cache_a._key("prompt") != PromptCache(scope="model-b")._key("prompt")

    def test_nothing_written_to_disk(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        PromptCache(enabled=True).set("prompt", "response")
        assert list(tmp_path.iterdir()) == []

    def test_disabled_cache_always_misses(self):
        cache = PromptCache(enabled=False)
        cache.set("prompt", "response")
        assert cache.get("prompt") is None

    def test_disabled_cache_set_is_noop(self):
        cache = PromptCache(enabled=False)
        # Should not raise
        cache.set("key", "value")

    def test_enabled_property_true_when_enabled(self):
        cache = PromptCache(enabled=True)
        assert cache.enabled is True

    def test_enabled_property_false_when_disabled(self):
        cache = PromptCache(enabled=False)
        assert cache.enabled is False


# ── Tests: _select_context (page selection) ──────────────────────────────────

class TestSelectContext:

    def test_single_page_always_returned(self):
        doc = _make_doc("anamnézis text here")
        ctx = _select_context(doc, "Anamnézis, jelen panaszok")
        assert "anamnézis" in ctx.lower() or len(ctx) > 0

    def test_best_page_selected(self):
        doc = RoutedDoc(
            path=Path("t"),
            doc_type=DocType.DISCHARGE_SUMMARY,
            pages=("unrelated page about something else",
                   "anamnézis részletes leírása anamnestic data"),
            tables=(),
            meta={},
        )
        ctx = _select_context(doc, "Anamnézis, jelen panaszok")
        assert "anamnestic" in ctx or "anamnézis" in ctx.lower()

    def test_fallback_on_zero_scores(self):
        doc = _make_doc("page without any section keywords")
        ctx = _select_context(doc, "Anamnézis, jelen panaszok")
        # Fallback: should return something (full_text[:4000])
        assert len(ctx) > 0

    def test_empty_doc_returns_empty(self):
        doc = RoutedDoc(
            path=Path("t"),
            doc_type=DocType.DISCHARGE_SUMMARY,
            pages=(),
            tables=(),
            meta={},
        )
        ctx = _select_context(doc, "Anamnézis")
        assert ctx == ""

    def test_score_page_anamnezis_keyword(self):
        score = _score_page("Anamnézis részletes leírás", "Anamnézis, jelen panaszok")
        assert score > 0

    def test_score_page_no_keywords_zero(self):
        score = _score_page("random text without relevant words", "Anamnézis")
        assert score == 0


# ── Tests: _fields_for_doc ────────────────────────────────────────────────────

class TestFieldsForDoc:

    def test_discharge_only_included_for_discharge(self):
        fd = _make_fd("f1", source_policy="DISCHARGE_ONLY")
        result = _fields_for_doc([fd], DocType.DISCHARGE_SUMMARY)
        assert fd in result

    def test_discharge_only_excluded_for_non_discharge(self):
        fd = _make_fd("f1", source_policy="DISCHARGE_ONLY")
        result = _fields_for_doc([fd], DocType.LAB_REPORT)
        assert fd not in result

    def test_event_primary_included_for_discharge(self):
        """EXAM_DOC_PRIMARY fields are sent to the LLM for discharge docs too
        (the discharge summary provides cross-validation values)."""
        fd = _make_fd("f1", source_policy="EXAM_DOC_PRIMARY")
        result = _fields_for_doc([fd], DocType.DISCHARGE_SUMMARY)
        assert fd in result

    def test_event_primary_included_for_non_discharge_via_routing(self):
        # Registry routing now drives eligibility: a field must declare
        # (DocType.ENDOSCOPY,) in its routing tuple to be extracted from
        # endoscopy documents.
        fd = _make_fd(
            "f1", source_policy="EXAM_DOC_PRIMARY",
            section_hint="Epikrízis",
            routing=((DocType.ENDOSCOPY, None),),
        )
        result = _fields_for_doc([fd], DocType.ENDOSCOPY)
        assert fd in result

    def test_event_primary_included_for_all_routed_doc_types(self):
        # A field routed to a DocType is eligible for that DocType regardless
        # of section_hint.
        routed = [
            (DocType.ELASTOGRAPHY, None),
            (DocType.LAB_REPORT, None),
            (DocType.MICROBIOLOGY, None),
            (DocType.IMAGING, None),
            (DocType.HISTOLOGY, None),
        ]
        vizsg = _make_fd(
            "f1", source_policy="EXAM_DOC_PRIMARY",
            section_hint="Vizsgálatok eredményei",
            routing=tuple(routed),
        )
        for doc_type, _ in routed:
            assert vizsg in _fields_for_doc([vizsg], doc_type), (
                f"Vizsg missing for {doc_type}"
            )

    def test_field_excluded_from_doctype_not_in_routing(self):
        # A field routed only to ENDOSCOPY must NOT leak into other doc types.
        fd = _make_fd(
            "f1", source_policy="EXAM_DOC_PRIMARY",
            section_hint="Epikrízis",
            routing=((DocType.ENDOSCOPY, None),),
        )
        assert fd in _fields_for_doc([fd], DocType.ENDOSCOPY)
        assert fd not in _fields_for_doc([fd], DocType.IMAGING)
        assert fd not in _fields_for_doc([fd], DocType.ELASTOGRAPHY)

    def test_routing_subtype_matches(self):
        # A field routed to IMAGING:ct is eligible only when the document's
        # routing_subtype is "ct" — generic IMAGING documents are excluded.
        fd = _make_fd(
            "f1", source_policy="EXAM_DOC_PRIMARY",
            section_hint="Vizsgálatok eredményei",
            routing=((DocType.IMAGING, "ct"),),
        )
        # Subtype None → field declares a specific subtype, so generic fails.
        assert fd not in _fields_for_doc([fd], DocType.IMAGING)
        assert fd in _fields_for_doc([fd], DocType.IMAGING, routing_subtype="ct")
        assert fd not in _fields_for_doc([fd], DocType.IMAGING, routing_subtype="uh")

    def test_routing_wildcard_accepts_any_subtype(self):
        # A field routed to IMAGING with no subtype is eligible for every
        # IMAGING document regardless of subtype.
        fd = _make_fd(
            "f1", source_policy="EXAM_DOC_PRIMARY",
            section_hint="Vizsgálatok eredményei",
            routing=((DocType.IMAGING, None),),
        )
        assert fd in _fields_for_doc([fd], DocType.IMAGING)
        assert fd in _fields_for_doc([fd], DocType.IMAGING, routing_subtype="ct")
        assert fd in _fields_for_doc([fd], DocType.IMAGING, routing_subtype="uh")

    def test_discharge_primary_always_included_in_discharge(self):
        # DISCHARGE_SUMMARY always admits every non-DISCHARGE_ONLY field
        # regardless of routing — the discharge summary is the canonical source.
        fd = _make_fd(
            "f1", source_policy="DISCHARGE_PRIMARY",
            section_hint="Epikrízis",
            routing=(),
        )
        assert fd in _fields_for_doc([fd], DocType.DISCHARGE_SUMMARY)
        # Non-discharge docs without matching routing entry → excluded.
        assert fd not in _fields_for_doc([fd], DocType.HISTOLOGY)
        # Add HISTOLOGY to routing → now eligible.
        fd2 = _make_fd(
            "f2", source_policy="DISCHARGE_PRIMARY",
            section_hint="Epikrízis",
            routing=((DocType.HISTOLOGY, None),),
        )
        assert fd2 in _fields_for_doc([fd2], DocType.HISTOLOGY)

    def test_empty_list_returns_empty(self):
        assert _fields_for_doc([], DocType.DISCHARGE_SUMMARY) == []


# ── Tests: SoftExtractor with DisabledClient ─────────────────────────────────

class TestSoftExtractorDisabled:

    def setup_method(self):
        self.cache = PromptCache(enabled=False)
        self.client = DisabledClient()
        self.extractor = SoftExtractor(self.client, self.cache)

    def test_all_fields_return_unknown(self):
        fields = [_make_fd(n) for n in ["ascites", "hepatitis", "cirrhosis"]]
        doc = _make_doc("Anamnézis szöveg.", DocType.DISCHARGE_SUMMARY)
        results = self.extractor.extract(doc, fields, ANCHOR)
        keys = {f.key for f in results}
        assert keys == {"ascites", "hepatitis", "cirrhosis"}
        for ef in results:
            assert ef.value == "Unknown", f"Expected Unknown for {ef.key}, got {ef.value!r}"

    def test_source_is_soft(self):
        fields = [_make_fd("field_a")]
        doc = _make_doc("text")
        results = self.extractor.extract(doc, fields, ANCHOR)
        assert all(r.source == FieldSource.SOFT for r in results)

    def test_confidence_is_07(self):
        fields = [_make_fd("field_a")]
        doc = _make_doc("text")
        results = self.extractor.extract(doc, fields, ANCHOR)
        assert all(r.confidence == 0.7 for r in results)

    def test_engine_is_llm(self):
        fields = [_make_fd("field_a")]
        doc = _make_doc("text")
        results = self.extractor.extract(doc, fields, ANCHOR)
        assert all(r.engine == "llm" for r in results)

    def test_empty_registry_returns_empty(self):
        doc = _make_doc("text")
        results = self.extractor.extract(doc, [], ANCHOR)
        assert results == []

    def test_discharge_only_skipped_for_lab_report(self):
        fields = [_make_fd("f_dis", source_policy="DISCHARGE_ONLY")]
        doc = _make_doc("text", DocType.LAB_REPORT)
        results = self.extractor.extract(doc, fields, ANCHOR)
        assert results == []

    def test_event_primary_included_for_discharge_summary(self):
        """EXAM_DOC_PRIMARY fields are now extracted from discharge docs too
        (they provide cross-validation values for the merge step)."""
        fields = [_make_fd("f_event", source_policy="EXAM_DOC_PRIMARY")]
        doc = _make_doc("text", DocType.DISCHARGE_SUMMARY)
        results = self.extractor.extract(doc, fields, ANCHOR)
        # Should return Unknown (not empty) because EXAM_DOC_PRIMARY is now included
        assert len(results) == 1
        assert results[0].value == "Unknown"

    def test_doc_type_preserved_in_extracted_field(self):
        fields = [_make_fd(
            "field_a", source_policy="EXAM_DOC_PRIMARY",
            routing=((DocType.ELASTOGRAPHY, None),),
        )]
        doc = _make_doc("text", DocType.ELASTOGRAPHY)
        results = self.extractor.extract(doc, fields, ANCHOR)
        assert results, "field routed to ELASTOGRAPHY should produce a result"
        assert results[0].doc_type == DocType.ELASTOGRAPHY

    def test_cached_answer_not_used_when_llm_disabled(self):
        """An answer cached by an LLM run must not leak into a no-LLM run."""
        cache = PromptCache(enabled=True)
        fields = [_make_fd("field_a")]
        doc = _make_doc("Anamnézis szöveg a cache-szivárgás teszthez.")

        SoftExtractor(_MockClient({"field_a": "from_llm"}), cache).extract(
            doc, fields, ANCHOR,
        )
        results = SoftExtractor(DisabledClient(), cache).extract(doc, fields, ANCHOR)
        assert results[0].value == "Unknown"


# ── Tests: SoftExtractor with mock client (returns real values) ───────────────

class _MockClient:
    """Test double: returns a configurable JSON string from chat()."""
    def __init__(self, response_dict: dict, call_count_store: list | None = None):
        self._response = response_dict
        self._calls = call_count_store if call_count_store is not None else []

    def chat(self, system: str, user: str) -> str:
        self._calls.append({"system": system, "user": user})
        return json.dumps(self._response)


class TestSoftExtractorMockClient:

    def test_returned_value_used(self):
        calls = []
        client = _MockClient({"ascites": "Igen"}, calls)
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        fields = [_make_fd("ascites", field_type="enum", allowed_values=("Igen", "Nem"))]
        doc = _make_doc("Az ascites jelen van.")
        results = extractor.extract(doc, fields, ANCHOR)

        ef = next(r for r in results if r.key == "ascites")
        assert ef.value == "Igen"

    def test_missing_key_gets_no_data(self):
        """A real LLM answered but omitted 'hepatitis' → 'hepatitis' = No Data
        (the LLM examined the document and found nothing)."""
        client = _MockClient({"ascites": "Igen"})
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        fields = [
            _make_fd("ascites"),
            _make_fd("hepatitis"),
        ]
        doc = _make_doc("text")
        results = extractor.extract(doc, fields, ANCHOR)

        result_map = {r.key: r.value for r in results}
        assert result_map["ascites"] == "Igen"
        assert result_map["hepatitis"] == "No Data"

    def test_empty_string_value_becomes_no_data(self):
        client = _MockClient({"field_a": ""})
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        fields = [_make_fd("field_a")]
        doc = _make_doc("text")
        results = extractor.extract(doc, fields, ANCHOR)
        ef = results[0]
        assert ef.value == "No Data"

    def test_explicit_unknown_answer_becomes_no_data(self):
        """The LLM explicitly answering "Unknown" means it looked and found
        nothing → normalised to "No Data"."""
        client = _MockClient({"field_a": "Unknown"})
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        fields = [_make_fd("field_a")]
        doc = _make_doc("text")
        results = extractor.extract(doc, fields, ANCHOR)
        assert results[0].value == "No Data"

    def test_multiple_sections_each_called_once(self):
        calls = []
        client = _MockClient({"field_a": "val_a", "field_b": "val_b"}, calls)
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        # Two fields in DIFFERENT sections
        fields = [
            _make_fd("field_a", section_hint="Anamnézis, jelen panaszok"),
            _make_fd("field_b", section_hint="Felvételi státus"),
        ]
        doc = _make_doc("text with anamnézis and felvételi details")
        extractor.extract(doc, fields, ANCHOR)

        # Two sections → two LLM calls
        assert len(calls) == 2

    def test_same_section_fields_one_call(self):
        calls = []
        client = _MockClient({"field_a": "A", "field_b": "B"}, calls)
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        fields = [
            _make_fd("field_a", section_hint="Anamnézis, jelen panaszok"),
            _make_fd("field_b", section_hint="Anamnézis, jelen panaszok"),
        ]
        doc = _make_doc("text")
        extractor.extract(doc, fields, ANCHOR)

        assert len(calls) == 1

    def test_cache_hit_avoids_second_call(self):
        calls = []
        client = _MockClient({"field_a": "cached_val"}, calls)

        cache = PromptCache(enabled=True)
        extractor = SoftExtractor(client, cache)

        fields = [_make_fd("field_a")]
        doc = _make_doc("Anamnézis szöveg here for caching test.")

        # First call — populates cache
        results1 = extractor.extract(doc, fields, ANCHOR)
        calls_after_first = len(calls)

        # Second call with identical doc+fields — should hit cache
        results2 = extractor.extract(doc, fields, ANCHOR)
        assert len(calls) == calls_after_first, (
            "Second call should use cache, not call LLM again"
        )
        assert results1[0].value == results2[0].value


# ── Tests: SoftExtractor with real registry (integration) ────────────────────

class TestSoftExtractorWithRegistry:

    def test_disabled_client_all_fields_unknown_from_registry(self):
        """Load real registry; DisabledClient → all fields = Unknown."""
        try:
            from app.registry.loader import load_registry
            registry = load_registry(Path("/mnt/project/Registry_base.xlsx"))
        except Exception:
            print("  SKIP: real registry not available")
            return

        client = DisabledClient()
        cache = PromptCache(enabled=False)
        extractor = SoftExtractor(client, cache)

        # Use only first 20 fields to keep test fast
        sample = registry[:20]
        doc = _make_doc("Anamnézis szöveg.", DocType.DISCHARGE_SUMMARY)
        results = extractor.extract(doc, sample, ANCHOR)

        # DisabledClient: all fields relevant to DISCHARGE_SUMMARY → Unknown
        assert len(results) > 0
        for ef in results:
            assert ef.value == "Unknown", f"{ef.key}: {ef.value!r}"


# ── Standalone runner ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    all_classes = [
        TestBuildUserPrompt,
        TestExtractJsonFromResponse,
        TestValidateEnumFields,
        TestDisabledClient,
        TestClientConstruction,
        TestGetLlmClient,
        TestPromptCache,
        TestSelectContext,
        TestFieldsForDoc,
        TestSoftExtractorDisabled,
        TestSoftExtractorMockClient,
        TestSoftExtractorWithRegistry,
    ]

    passed = failed = 0
    for cls in all_classes:
        instance = cls()
        methods = sorted(m for m in dir(cls) if m.startswith("test_"))
        for method_name in methods:
            if hasattr(instance, "setup_method"):
                try:
                    instance.setup_method()
                except Exception as e:
                    print(f"  ERROR  setup_method for {cls.__name__}: {e}")
                    continue
            try:
                getattr(instance, method_name)()
                passed += 1
                print(f"  PASS   {cls.__name__}.{method_name}")
            except AssertionError as e:
                failed += 1
                print(f"  FAIL   {cls.__name__}.{method_name}: {e}")
            except Exception as e:
                failed += 1
                print(f"  ERROR  {cls.__name__}.{method_name}: {e}")
                traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"  PASSED:  {passed}/{passed + failed}")
    if failed:
        print(f"  FAILED:  {failed}")
        sys.exit(1)
    else:
        print("  All tests passed ✓")
