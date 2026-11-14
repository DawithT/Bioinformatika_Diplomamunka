"""
tests/unit/test_rules.py

Unit tests for app/registry/rules_engine.py:
  - _negate_operator()
  - build_rules_from_registry()
  - Rule.condition_met()
  - apply()
  - RulesEngine

Rules are derived from the registry in memory at runtime — there is no
rules.yaml file any more.

Run with:
    python3 tests/unit/test_rules.py
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Any

# ── Path setup ────────────────────────────────────────────────────────────────
_PROJECT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT))

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField
from app.registry.rules_engine import (
    Rule,
    RulesEngine,
    _NOT_APPLICABLE,
    _negate_operator,
    apply,
    build_rules_from_registry,
)


# ── Test runner ───────────────────────────────────────────────────────────────

_passed: list[str] = []
_failed: list[str] = []


def run(name: str, fn):
    try:
        fn()
        _passed.append(name)
        print(f"  \u2713  {name}")
    except Exception:
        _failed.append(name)
        print(f"  \u2717  {name}")
        traceback.print_exc()


def assert_eq(actual, expected, msg=""):
    if actual != expected:
        raise AssertionError(
            f"{msg}\n  expected: {expected!r}\n  actual:   {actual!r}"
        )


def assert_true(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assertion failed")


def assert_false(cond, msg=""):
    if cond:
        raise AssertionError(msg or "expected False")


def assert_in(item, container, msg=""):
    if item not in container:
        raise AssertionError(f"{msg}\n  {item!r} not in {container!r}")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ef(
    key: str,
    value: Any,
    source: FieldSource = FieldSource.HARD,
    confidence: float = 0.95,
) -> ExtractedField:
    return ExtractedField(
        key=key, value=value, source=source,
        doc_type=DocType.DISCHARGE_SUMMARY,
        confidence=confidence, engine="test",
    )


def _fields(**kv) -> dict[str, ExtractedField]:
    return {k: _ef(k, v) for k, v in kv.items()}


class _FakeFD:
    """Minimal stand-in for app.registry.models.FieldDefinition."""

    def __init__(self, name, parent=None, op=None, val=None, timepoint="anam"):
        self.field_name = name
        self.condition_parent_field = parent
        self.condition_operator = op
        self.condition_value = val
        self.timepoint = timepoint

    @property
    def is_conditional(self):
        return self.condition_parent_field is not None


def _minimal_registry():
    """Build a tiny fake registry for rule-derivation tests."""
    return [
        _FakeFD("hepatitis_c_virus"),
        _FakeFD("hcv_statusz", "hepatitis_c_virus", "==", "Igen"),
        _FakeFD("hogyan_gyogyult", "hepatitis_c_virus", "==", "Igen"),
        _FakeFD("eredmenye"),
        _FakeFD("volt_e_sikeres_eradikacio", "eredmenye", "!=", "Negat\u00edv"),
        _FakeFD("majbiopszia_tortent_e"),
        _FakeFD("majbiopszia_idopontja", "majbiopszia_tortent_e", "==", "Igen"),
    ]


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Tests: _negate_operator
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def test_negate_eq_gives_neq():
    assert_eq(_negate_operator("=="), "!=")


def test_negate_neq_gives_eq():
    assert_eq(_negate_operator("!="), "==")


def test_negate_unknown_operator_unchanged():
    assert_eq(_negate_operator(">"), ">")
    assert_eq(_negate_operator("in"), "in")


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Tests: build_rules_from_registry
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def test_build_rules_groups_children_by_parent_op_value():
    """hcv_statusz and hogyan_gyogyult share the same parent rule \u2192 one rule entry."""
    rules = build_rules_from_registry(_minimal_registry())
    hcv_rule = next(r for r in rules if r.if_field == "hepatitis_c_virus")
    assert_eq(hcv_rule.operator, "!=")   # negated: registry "==" \u2192 engine "!="
    assert_eq(hcv_rule.value, "Igen")
    assert_in("hcv_statusz", hcv_rule.then_not_applicable)
    assert_in("hogyan_gyogyult", hcv_rule.then_not_applicable)


def test_build_rules_negates_neq_operator():
    """Registry '!=' (volt_e_sikeres_eradikacio) \u2192 engine '=='."""
    rules = build_rules_from_registry(_minimal_registry())
    eredmeny_rule = next(r for r in rules if r.if_field == "eredmenye")
    assert_eq(eredmeny_rule.operator, "==")   # negated: registry "!=" \u2192 engine "=="
    assert_eq(eredmeny_rule.value, "Negat\u00edv")
    assert_in("volt_e_sikeres_eradikacio", eredmeny_rule.then_not_applicable)


def test_build_rules_drops_unresolvable_parent():
    """Parent not in registry \u2192 rule silently dropped."""
    reg = [
        _FakeFD("some_field"),
        _FakeFD("orphan_child", "nonexistent_parent", "==", "Igen"),
    ]
    rules = build_rules_from_registry(reg)
    names = [r.if_field for r in rules]
    assert_false("nonexistent_parent" in names, "unresolvable parent must be dropped")


def test_build_rules_output_is_sorted():
    """Rules are sorted by (if_field, operator, value) for determinism."""
    rules = build_rules_from_registry(_minimal_registry())
    keys = [(r.if_field, r.operator, r.value) for r in rules]
    assert_eq(keys, sorted(keys), "rules must be sorted")


def test_build_rules_children_sorted():
    """Children tuple within each rule is sorted."""
    rules = build_rules_from_registry(_minimal_registry())
    hcv_rule = next(r for r in rules if r.if_field == "hepatitis_c_virus")
    children = list(hcv_rule.then_not_applicable)
    assert_eq(children, sorted(children))


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Tests: Rule.condition_met()
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def test_rule_neq_condition_met_when_value_differs():
    rule = Rule("parent", "!=", "Igen", ("child",))
    assert_true(rule.condition_met("Nem"), "Nem != Igen \u2192 condition met")


def test_rule_neq_condition_not_met_when_value_matches():
    rule = Rule("parent", "!=", "Igen", ("child",))
    assert_false(rule.condition_met("Igen"), "Igen == Igen \u2192 condition NOT met")


def test_rule_eq_condition_met_when_value_matches():
    rule = Rule("parent", "==", "Negat\u00edv", ("child",))
    assert_true(rule.condition_met("Negat\u00edv"))


def test_rule_eq_condition_not_met_when_value_differs():
    rule = Rule("parent", "==", "Negat\u00edv", ("child",))
    assert_false(rule.condition_met("Pozit\u00edv"))


def test_rule_condition_not_met_for_absent_parent():
    """None, Unknown, NA \u2192 treat as absent \u2192 don't propagate."""
    rule = Rule("parent", "!=", "Igen", ("child",))
    for absent in (None, "", "Unknown", "NA", "ERVENYTELEN", "Not applicable"):
        assert_false(
            rule.condition_met(absent),
            f"absent value {absent!r} must not trigger not-applicable"
        )


def test_rule_unknown_operator_returns_false():
    rule = Rule("parent", ">=", "5", ("child",))
    assert_false(rule.condition_met("3"), "unknown operator \u2192 conservative: False")


def test_rule_list_parent_containing_value_keeps_child():
    """Multiple-answer parent: the answer is in the list \u2192 child stays applicable."""
    rule = Rule("he_kezeles", "!=", "Lakt\u00fal\u00f3z", ("laktuloz_dozis",))
    assert_false(rule.condition_met('["Lakt\u00fal\u00f3z", "Rifaximin"]'))


def test_rule_list_parent_without_value_marks_child():
    rule = Rule("he_kezeles", "!=", "Lakt\u00fal\u00f3z", ("laktuloz_dozis",))
    assert_true(rule.condition_met('["Rifaximin"]'))


def test_rule_list_parent_eq_operator_tests_membership():
    rule = Rule("parent", "==", "Nincs", ("child",))
    assert_true(rule.condition_met('["Nincs"]'))
    assert_false(rule.condition_met('["Hasi f\u00e1jdalom", "H\u00e1ny\u00e1s"]'))


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Tests: apply()
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def test_apply_sets_child_not_applicable_when_condition_met():
    """hepatitis_c_virus = 'Nem' \u2192 hcv_statusz = Not applicable."""
    rules = [Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz", "hogyan_gyogyult"))]
    fields = _fields(hepatitis_c_virus="Nem", hcv_statusz="Unknown")
    result = apply(fields, rules)
    assert_eq(result["hcv_statusz"].value, _NOT_APPLICABLE)
    assert_eq(result["hogyan_gyogyult"].value, _NOT_APPLICABLE)


def test_apply_leaves_child_unchanged_when_condition_not_met():
    """hepatitis_c_virus = 'Igen' \u2192 hcv_statusz left as-is."""
    rules = [Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz",))]
    fields = _fields(hepatitis_c_virus="Igen", hcv_statusz="Stabil")
    result = apply(fields, rules)
    assert_eq(result["hcv_statusz"].value, "Stabil", "child must not be changed")


def test_apply_missing_parent_rule_silently_skipped():
    """Parent not in fields \u2192 rule skipped, no error."""
    rules = [Rule("missing_parent", "!=", "Igen", ("child_field",))]
    fields = _fields(child_field="some_value")
    result = apply(fields, rules)
    assert_eq(result["child_field"].value, "some_value")


def test_apply_child_not_in_fields_is_injected():
    """Child field absent from extraction \u2192 engine creates it as Not applicable."""
    rules = [Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz",))]
    fields = _fields(hepatitis_c_virus="Nem")  # hcv_statusz never extracted
    result = apply(fields, rules)
    assert_true("hcv_statusz" in result)
    assert_eq(result["hcv_statusz"].value, _NOT_APPLICABLE)
    assert_eq(result["hcv_statusz"].engine, "rules_engine")


def test_apply_already_not_applicable_is_idempotent():
    """Running apply twice produces the same result."""
    rules = [Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz",))]
    fields = _fields(hepatitis_c_virus="Nem", hcv_statusz="Unknown")
    result1 = apply(fields, rules)
    result2 = apply(result1, rules)
    assert_eq(result2["hcv_statusz"].value, _NOT_APPLICABLE)


def test_apply_does_not_mutate_input():
    """The original fields dict is not modified."""
    rules = [Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz",))]
    fields = _fields(hepatitis_c_virus="Nem", hcv_statusz="Something")
    original_value = fields["hcv_statusz"].value
    apply(fields, rules)
    assert_eq(fields["hcv_statusz"].value, original_value, "input must not be mutated")


def test_apply_multiple_rules_all_applied():
    """Multiple rules applied in sequence."""
    rules = [
        Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz",)),
        Rule("majbiopszia_tortent_e", "!=", "Igen", ("majbiopszia_idopontja",)),
    ]
    fields = _fields(
        hepatitis_c_virus="Nem",
        hcv_statusz="Unknown",
        majbiopszia_tortent_e="Nem",
        majbiopszia_idopontja="Unknown",
    )
    result = apply(fields, rules)
    assert_eq(result["hcv_statusz"].value, _NOT_APPLICABLE)
    assert_eq(result["majbiopszia_idopontja"].value, _NOT_APPLICABLE)


def test_apply_sets_engine_provenance_correctly():
    """Fields set by the engine have correct source/engine metadata."""
    rules = [Rule("parent", "!=", "Igen", ("child",))]
    fields = _fields(parent="Nem")
    result = apply(fields, rules)
    ef = result["child"]
    assert_eq(ef.engine, "rules_engine")
    assert_eq(ef.confidence, 1.0)


def test_apply_absent_parent_value_does_not_trigger():
    """Parent field exists but has Unknown value \u2192 rule skipped when no
    registry (timepoint info) is available."""
    rules = [Rule("hepatitis_c_virus", "!=", "Igen", ("hcv_statusz",))]
    fields = _fields(hepatitis_c_virus="Unknown", hcv_statusz="Some value")
    result = apply(fields, rules)
    assert_eq(result["hcv_statusz"].value, "Some value", "Unknown parent must not trigger")


def test_apply_adm_absent_parent_propagates():
    """Parent is Unknown but timepoint=adm \u2192 child becomes Not applicable."""
    reg = [
        _FakeFD("ascites_adm", timepoint="adm"),
        _FakeFD("ascites_mennyiseg_adm", "ascites_adm", "==", "Igen", timepoint="adm"),
    ]
    rules = build_rules_from_registry(reg)
    fields = _fields(ascites_adm="Unknown", ascites_mennyiseg_adm="Unknown")
    result = apply(fields, rules, registry=reg)
    assert_eq(result["ascites_mennyiseg_adm"].value, _NOT_APPLICABLE)


def test_apply_anam_absent_parent_propagates():
    """Parent is Unknown with timepoint=anam \u2192 child becomes Not applicable
    (parent-child indications apply to anamnesis fields as well)."""
    reg = [
        _FakeFD("korabbi_ascites", timepoint="anam"),
        _FakeFD("korabbi_ascites_datuma", "korabbi_ascites", "==", "Igen",
                timepoint="anam"),
    ]
    rules = build_rules_from_registry(reg)
    fields = _fields(korabbi_ascites="Unknown", korabbi_ascites_datuma="Unknown")
    result = apply(fields, rules, registry=reg)
    assert_eq(result["korabbi_ascites_datuma"].value, _NOT_APPLICABLE)


def test_apply_no_data_parent_propagates_like_unknown():
    """'No Data' (LLM examined, found nothing) behaves like Unknown for
    absent-parent propagation."""
    reg = [
        _FakeFD("ascites_adm", timepoint="adm"),
        _FakeFD("ascites_mennyiseg_adm", "ascites_adm", "==", "Igen", timepoint="adm"),
    ]
    rules = build_rules_from_registry(reg)
    fields = _fields(ascites_adm="No Data", ascites_mennyiseg_adm="No Data")
    result = apply(fields, rules, registry=reg)
    assert_eq(result["ascites_mennyiseg_adm"].value, _NOT_APPLICABLE)


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Tests: RulesEngine class
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def test_rules_engine_applies_rules():
    """RulesEngine.apply() wraps apply() correctly."""
    engine = RulesEngine(_minimal_registry())
    assert_true(engine.rule_count > 0)
    fields = _fields(hepatitis_c_virus="Nem", hcv_statusz="Something")
    result = engine.apply(fields)
    assert_eq(result["hcv_statusz"].value, _NOT_APPLICABLE)


def test_rules_engine_exposes_rule_count():
    engine = RulesEngine(_minimal_registry())
    # _minimal_registry has 3 distinct (parent, op, value) groups
    assert_eq(engine.rule_count, 3)


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Tests: integration with real registry (if available)
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

def _load_real_registry():
    """Load the real registry, or return None if openpyxl/registry are unavailable."""
    try:
        from app.registry.loader import load_registry
    except ImportError:
        return None
    registry_path = _PROJECT / "config" / "Registry_base.xlsx"
    if not registry_path.exists():
        return None
    try:
        return load_registry(registry_path)
    except Exception:
        return None


def test_real_registry_builds_rules():
    """Rules derived from the real registry are non-empty and well-formed."""
    reg = _load_real_registry()
    if reg is None:
        print("    (skipped \u2014 real registry not available)")
        return
    rules = build_rules_from_registry(reg)
    assert_true(len(rules) > 0, "real registry must produce at least one rule")
    for r in rules:
        assert_true(r.if_field, "if_field must be non-empty")
        assert_true(r.operator in ("==", "!="), f"unexpected operator: {r.operator!r}")
        assert_true(len(r.then_not_applicable) > 0, "each rule must have at least one child")


def test_real_registry_hepatitis_c_rule_correct():
    """The HCV rule from the real registry must propagate Not applicable correctly."""
    reg = _load_real_registry()
    if reg is None:
        print("    (skipped \u2014 real registry not available)")
        return
    rules = build_rules_from_registry(reg)

    hcv_rules = [r for r in rules if r.if_field == "hepatitis_c_virus"]
    if not hcv_rules:
        print("    (skipped \u2014 HCV rule not present in real registry)")
        return

    fields = _fields(hepatitis_c_virus="Nem", hcv_statusz="Stabil")
    result = apply(fields, rules)
    assert_eq(result.get("hcv_statusz").value if "hcv_statusz" in result else None,
              _NOT_APPLICABLE)

    fields2 = _fields(hepatitis_c_virus="Igen", hcv_statusz="Stabil")
    result2 = apply(fields2, rules)
    assert_eq(result2["hcv_statusz"].value, "Stabil")


# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550
# Runner
# \u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550

if __name__ == "__main__":
    tests = [
        # negate_operator
        ("negate: == \u2192 !=", test_negate_eq_gives_neq),
        ("negate: != \u2192 ==", test_negate_neq_gives_eq),
        ("negate: unknown unchanged", test_negate_unknown_operator_unchanged),
        # build_rules_from_registry
        ("build_rules: groups children by parent/op/value",
         test_build_rules_groups_children_by_parent_op_value),
        ("build_rules: negates != operator",
         test_build_rules_negates_neq_operator),
        ("build_rules: drops unresolvable parent",
         test_build_rules_drops_unresolvable_parent),
        ("build_rules: output sorted",
         test_build_rules_output_is_sorted),
        ("build_rules: children sorted",
         test_build_rules_children_sorted),
        # Rule.condition_met
        ("condition_met: != met when differs",
         test_rule_neq_condition_met_when_value_differs),
        ("condition_met: != not met when matches",
         test_rule_neq_condition_not_met_when_value_matches),
        ("condition_met: == met when matches",
         test_rule_eq_condition_met_when_value_matches),
        ("condition_met: == not met when differs",
         test_rule_eq_condition_not_met_when_value_differs),
        ("condition_met: absent parent values \u2192 False",
         test_rule_condition_not_met_for_absent_parent),
        ("condition_met: unknown operator \u2192 False",
         test_rule_unknown_operator_returns_false),
        ("condition_met: list parent containing value → False",
         test_rule_list_parent_containing_value_keeps_child),
        ("condition_met: list parent without value → True",
         test_rule_list_parent_without_value_marks_child),
        ("condition_met: list parent == tests membership",
         test_rule_list_parent_eq_operator_tests_membership),
        # apply
        ("apply: sets child not-applicable when condition met",
         test_apply_sets_child_not_applicable_when_condition_met),
        ("apply: leaves child unchanged when condition not met",
         test_apply_leaves_child_unchanged_when_condition_not_met),
        ("apply: missing parent silently skipped",
         test_apply_missing_parent_rule_silently_skipped),
        ("apply: absent child injected as not-applicable",
         test_apply_child_not_in_fields_is_injected),
        ("apply: idempotent",
         test_apply_already_not_applicable_is_idempotent),
        ("apply: does not mutate input",
         test_apply_does_not_mutate_input),
        ("apply: multiple rules applied",
         test_apply_multiple_rules_all_applied),
        ("apply: engine provenance metadata correct",
         test_apply_sets_engine_provenance_correctly),
        ("apply: Unknown parent does not trigger (no registry)",
         test_apply_absent_parent_value_does_not_trigger),
        ("apply: Unknown parent propagates (adm timepoint)",
         test_apply_adm_absent_parent_propagates),
        ("apply: Unknown parent propagates (anam timepoint)",
         test_apply_anam_absent_parent_propagates),
        ("apply: No Data parent propagates like Unknown",
         test_apply_no_data_parent_propagates_like_unknown),
        # RulesEngine
        ("RulesEngine: apply works", test_rules_engine_applies_rules),
        ("RulesEngine: rule_count correct", test_rules_engine_exposes_rule_count),
        # Integration
        ("integration: real registry builds rules",
         test_real_registry_builds_rules),
        ("integration: real HCV rule applies correctly",
         test_real_registry_hepatitis_c_rule_correct),
    ]

    print(f"\n{'\u2500' * 60}")
    print("  test_rules.py \u2014 Rules Engine")
    print(f"{'\u2500' * 60}")
    for name, fn in tests:
        run(name, fn)

    print(f"\n{'\u2500' * 60}")
    total = len(_passed) + len(_failed)
    print(f"  {len(_passed)}/{total} passed", end="")
    if _failed:
        print(f"  |  FAILED: {', '.join(_failed)}")
    else:
        print("  \u2014 all passed \u2713")
    print(f"{'\u2500' * 60}\n")
    sys.exit(0 if not _failed else 1)
