"""
app/registry/rules_engine.py

Step 7 of the pipeline — Rules Engine.

Derives not-applicable rules directly from the registry's condition_* columns
(Registry_base.xlsx) at runtime and propagates "Not applicable" to child fields
whose parent condition is not met. No YAML file, no separate generator step.

Rule semantics (from registry)
------------------------------
Each FieldDefinition with a non-None condition_parent_field encodes:

    "This field is applicable ONLY when:
         condition_parent_field  condition_operator  condition_value"

The NOT-APPLICABLE trigger is the negation:

    registry '=='  →  engine '!='   (parent differs from value → child N/A)
    registry '!='  →  engine '=='   (parent equals value → child N/A)
    other operator →  kept as-is

Public API
----------
build_rules_from_registry(registry)
    Pure function: list[FieldDefinition] → list[Rule].

apply(fields, rules, registry=None)
    Apply rules to a merged field dict; return an updated copy.

RulesEngine(registry)
    Stateful wrapper: build rules once, apply them repeatedly.

Design notes
------------
- Pure function interface (`apply`) for testability.
- Idempotent: applying twice produces the same result.
- Not-applicable marker is always the string "Not applicable" — consistent
  with the ExtractedField pipeline convention.
- When a child field is not yet in `fields` (i.e. was never extracted),
  the engine still sets it to "Not applicable" so the wide table is complete.
- ExtractedField provenance for injected Not-applicable fields:
    source=SOFT, confidence=1.0, engine="rules_engine", doc_type=UNKNOWN
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

from app.domain.enums import DocType, FieldSource
from app.domain.models import ExtractedField

if TYPE_CHECKING:
    from app.registry.models import FieldDefinition

log = logging.getLogger(__name__)

# Sentinel value written into fields set by the rules engine
_NOT_APPLICABLE = "Not applicable"

# Values treated as "absent" — the parent field has no meaningful data.
# "No Data" is the LLM-examined-but-found-nothing marker (see soft_llm).
_ABSENT_VALUES: frozenset = frozenset(
    {None, "", "Unknown", "No Data", "NA", "ERVENYTELEN", "Not applicable",
     "Uncertain"}
)

# Source metadata for engine-injected fields
_ENGINE_SOURCE = FieldSource.SOFT
_ENGINE_CONFIDENCE = 1.0
_ENGINE_NAME = "rules_engine"

# Registry-operator → engine-operator negation table
_NEGATION: dict[str, str] = {
    "==": "!=",
    "!=": "==",
}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Rule:
    """
    A single not-applicable rule.

    if_field  [operator]  value  →  set then_not_applicable to "Not applicable"

    Operators supported:
        "!="  — parent value is not equal to value  (most common)
        "=="  — parent value equals value
    For multiple-answer parents (JSON array values) both test membership.
    """
    if_field: str
    operator: str        # "!=" | "=="
    value: str
    then_not_applicable: tuple[str, ...]

    def condition_met(self, parent_value: Any) -> bool:
        """
        Return True if the NOT-APPLICABLE condition is satisfied.

        None / "Unknown" / "NA" on the parent side → condition NOT met
        (treat absent parent as "we don't know yet" → don't propagate).

        A multiple-answer parent arrives as a JSON array string
        ('["Laktulóz", "Rifaximin"]'); it is compared by membership, so
        "==" means "contains" and "!=" means "does not contain".
        """
        if parent_value in _ABSENT_VALUES:
            return False

        pv = str(parent_value).strip()
        v = str(self.value).strip()

        if pv.startswith("["):
            try:
                items = json.loads(pv)
            except ValueError:
                items = None
            if isinstance(items, list):
                present = v in {str(i).strip() for i in items}
                if self.operator == "!=":
                    return not present
                if self.operator == "==":
                    return present

        if self.operator == "!=":
            return pv != v
        if self.operator == "==":
            return pv == v
        # Unknown operator — conservative: don't propagate
        log.warning("Unknown rule operator %r — rule skipped", self.operator)
        return False


# ── Rule derivation from registry ─────────────────────────────────────────────

def _negate_operator(op: str) -> str:
    """
    Return the logical negation of *op*.

    The registry encodes the APPLICABLE condition; the engine encodes the
    NOT-APPLICABLE trigger, which is the negation. Unknown operators are
    returned unchanged (conservative: no negation).
    """
    return _NEGATION.get(op, op)


def build_rules_from_registry(
    registry: "list[FieldDefinition]",
) -> list[Rule]:
    """
    Derive not-applicable rules from a loaded registry.

    Groups children that share the same (parent, operator, value) into a
    single Rule. Rules whose parent is not in the registry are silently
    dropped (the registry loader already warns about these).

    Output is sorted deterministically by (if_field, operator, value) so
    that repeated builds produce identical rule lists.
    """
    all_names: set[str] = {fd.field_name for fd in registry}

    groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)

    for fd in registry:
        if not fd.is_conditional:
            continue
        parent = fd.condition_parent_field
        op = fd.condition_operator or "=="
        val = fd.condition_value or ""

        if parent not in all_names:
            continue

        engine_op = _negate_operator(op)
        groups[(parent, engine_op, val)].append(fd.field_name)

    rules: list[Rule] = []
    for (parent, op, val), children in sorted(groups.items()):
        rules.append(
            Rule(
                if_field=parent,
                operator=op,
                value=val,
                then_not_applicable=tuple(sorted(children)),
            )
        )
    return rules


# ── Application ───────────────────────────────────────────────────────────────

def apply(
    fields: dict[str, ExtractedField],
    rules: list[Rule],
    registry: "list[FieldDefinition] | None" = None,
) -> dict[str, ExtractedField]:
    """
    Apply all rules to the merged field dict and return an updated copy.

    Parameters
    ----------
    fields : dict[str, ExtractedField]
        Merged fields from step6_merge.  Not mutated.
    rules : list[Rule]
        Rules from build_rules_from_registry().
    registry : list[FieldDefinition] | None
        Optional registry for timepoint-aware unknown-parent propagation.
        When provided, absent parent values in anam/adm/inhosp fields
        propagate "Not applicable" to children (missing data means the
        condition/examination was not present or not performed).

    Returns
    -------
    dict[str, ExtractedField]
        A new dict with not-applicable fields injected or overridden.
    """
    result = dict(fields)  # shallow copy — ExtractedFields are not mutated

    _timepoints: dict[str, str] = {}
    if registry:
        for fd in registry:
            _timepoints[fd.field_name] = fd.timepoint

    applied_count = 0

    for rule in rules:
        parent_ef = result.get(rule.if_field)
        parent_value = parent_ef.value if parent_ef is not None else None

        should_apply = rule.condition_met(parent_value)

        # Unknown parent → propagate Not applicable for anam/adm/inhosp fields.
        # Rationale: if there is no information about whether a condition or
        # examination was present/performed, the dependent child fields cannot
        # carry meaningful data either, so they are marked "Not applicable".
        # (anam was originally excluded on the argument that absence of
        # historical information does not prove absence of the event; per
        # user guidance the parent-child indications now apply to anamnesis
        # fields as well.)
        if not should_apply and parent_value in _ABSENT_VALUES:
            tp = _timepoints.get(rule.if_field)
            if tp in ("anam", "adm", "inhosp"):
                should_apply = True

        if not should_apply:
            continue

        for child_key in rule.then_not_applicable:
            existing = result.get(child_key)
            if existing is not None and existing.value == _NOT_APPLICABLE:
                continue  # already not applicable — idempotent

            result[child_key] = ExtractedField(
                key=child_key,
                value=_NOT_APPLICABLE,
                source=_ENGINE_SOURCE,
                doc_type=DocType.UNKNOWN,
                confidence=_ENGINE_CONFIDENCE,
                page=None,
                engine=_ENGINE_NAME,
            )
            applied_count += 1
            log.debug(
                "Rules engine: %r → Not applicable (because %r %s %r = %r)",
                child_key, rule.if_field, rule.operator, rule.value, parent_value,
            )

    if applied_count:
        log.info("Rules engine applied %d not-applicable annotations", applied_count)

    return result


# ── Convenience class ─────────────────────────────────────────────────────────

class RulesEngine:
    """
    Stateful wrapper: builds rules once from the registry, applies them
    repeatedly. Preferred for production use where the same rules are
    applied to many CaseResult objects in a worker process.
    """

    def __init__(self, registry: "list[FieldDefinition]") -> None:
        self._registry = registry
        self._rules = build_rules_from_registry(registry)
        log.info("RulesEngine initialised with %d rules", len(self._rules))

    @property
    def rules(self) -> list[Rule]:
        """Return a copy of the derived rule list (safe for external inspection)."""
        return list(self._rules)

    @property
    def rule_count(self) -> int:
        """Return the number of not-applicable rules currently loaded."""
        return len(self._rules)

    def apply(self, fields: dict[str, ExtractedField]) -> dict[str, ExtractedField]:
        """Apply rules to *fields* and return an updated copy."""
        return apply(fields, self._rules, self._registry)
