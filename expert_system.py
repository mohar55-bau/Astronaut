"""AERO-MED ORION-IV explainable, rule-based expert system.

This module is educational/demo software. It does not perform medical diagnosis,
clinical triage, spacecraft flight control, or validated operational risk assessment.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

BASE_DIR = Path(__file__).resolve().parent
KNOWLEDGE_BASE_PATH = BASE_DIR / "knowledge_base.json"

SEVERITY_RANK = {"INFO": 1, "WARNING": 2, "CRITICAL": 3}
MISSING = object()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_knowledge_base() -> dict[str, Any]:
    """Read the JSON knowledge base on every request so rule edits take effect immediately."""
    try:
        payload = json.loads(KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Knowledge base not found: {KNOWLEDGE_BASE_PATH}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Knowledge base JSON is invalid: {exc}") from exc

    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        raise RuntimeError("Knowledge base must contain a 'rules' array.")

    seen_ids: set[str] = set()
    for rule in payload["rules"]:
        if not isinstance(rule, dict):
            raise RuntimeError("Every knowledge-base rule must be a JSON object.")
        rule_id = str(rule.get("id", "")).strip()
        if not rule_id or rule_id in seen_ids:
            raise RuntimeError(f"Rule IDs must be non-empty and unique: {rule_id!r}")
        seen_ids.add(rule_id)
        if rule.get("scope") not in {"environment", "crew", "sensor"}:
            raise RuntimeError(f"Rule {rule_id} has an unsupported scope.")
        if str(rule.get("severity", "")).upper() not in SEVERITY_RANK:
            raise RuntimeError(f"Rule {rule_id} has an unsupported severity.")
        if not isinstance(rule.get("conditions"), list) or not rule["conditions"]:
            raise RuntimeError(f"Rule {rule_id} must have at least one condition.")
    return payload


def _get_path(context: dict[str, Any], path: str) -> Any:
    current: Any = context
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return MISSING
    return current


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _condition_matches(context: dict[str, Any], condition: dict[str, Any]) -> tuple[bool, Any]:
    path = str(condition.get("fact", ""))
    operator = str(condition.get("operator", "eq")).lower()
    expected = condition.get("value")
    actual = _get_path(context, path)

    if operator == "exists":
        return (actual is not MISSING and actual is not None, actual)
    if actual is MISSING or actual is None:
        return False, actual

    if operator in {"gt", "gte", "lt", "lte"}:
        left, right = _as_number(actual), _as_number(expected)
        if left is None or right is None:
            return False, actual
        comparisons = {
            "gt": left > right,
            "gte": left >= right,
            "lt": left < right,
            "lte": left <= right,
        }
        return comparisons[operator], actual

    if operator == "eq":
        if isinstance(expected, bool):
            return actual is expected, actual
        if _as_number(actual) is not None and _as_number(expected) is not None:
            return _as_number(actual) == _as_number(expected), actual
        return str(actual).casefold() == str(expected).casefold(), actual

    if operator == "neq":
        if isinstance(expected, bool):
            return actual is not expected, actual
        return str(actual).casefold() != str(expected).casefold(), actual

    if operator in {"in", "not_in"}:
        if not isinstance(expected, list):
            return False, actual
        matched = any(str(actual).casefold() == str(item).casefold() for item in expected)
        return (matched if operator == "in" else not matched), actual

    raise RuntimeError(f"Unsupported rule operator: {operator}")


def _scope_contexts(state: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    scenario = state.get("scenario", "UNKNOWN")
    eclss = state.get("eclss") if isinstance(state.get("eclss"), dict) else {}
    crew = state.get("crew") if isinstance(state.get("crew"), list) else []

    environment_context = {"scenario": scenario, "eclss": eclss}
    crew_contexts: list[dict[str, Any]] = []
    sensor_contexts: list[dict[str, Any]] = []

    for member in crew:
        if not isinstance(member, dict):
            continue
        crew_contexts.append({"scenario": scenario, "eclss": eclss, "crew": member})
        nodes = member.get("nodes", [])
        if isinstance(nodes, list):
            for node in nodes:
                if isinstance(node, dict):
                    sensor_contexts.append(
                        {"scenario": scenario, "eclss": eclss, "crew": member, "sensor": node}
                    )

    return {
        "environment": [environment_context],
        "crew": crew_contexts,
        "sensor": sensor_contexts,
    }


def _format_actual(value: Any) -> str:
    if value is MISSING:
        return "not provided"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def build_input_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Store only relevant scalar facts, not large animated chart/history arrays."""
    eclss = state.get("eclss") if isinstance(state.get("eclss"), dict) else {}
    crew_snapshot: list[dict[str, Any]] = []
    for member in state.get("crew", []) if isinstance(state.get("crew"), list) else []:
        if not isinstance(member, dict):
            continue
        sensor_snapshot = []
        for node in member.get("nodes", []) if isinstance(member.get("nodes"), list) else []:
            if isinstance(node, dict):
                sensor_snapshot.append({k: node.get(k) for k in ("id", "battery", "rssi", "status") if k in node})
        crew_snapshot.append(
            {
                k: member.get(k)
                for k in ("id", "name", "status", "activity", "hr", "hrv", "spo2", "sys", "dia", "temp", "resp")
                if k in member
            }
            | {"nodes": sensor_snapshot}
        )
    return {
        "scenario": state.get("scenario", "UNKNOWN"),
        "eclss": {k: eclss.get(k) for k in ("pressure", "o2", "co2", "radFlux", "scrubberHigh", "shieldActive") if k in eclss},
        "crew": crew_snapshot,
        "data_source": "SIMULATED_DEMO_DATA",
    }


def run_expert_system(state: dict[str, Any], source: str = "saved_state") -> dict[str, Any]:
    """Run a forward-chaining, IF/THEN rule evaluation and return explainable findings."""
    knowledge_base = load_knowledge_base()
    rules = [rule for rule in knowledge_base["rules"] if rule.get("enabled", True)]
    contexts = _scope_contexts(state)
    findings: list[dict[str, Any]] = []
    evaluated = 0

    for rule in rules:
        scope = rule["scope"]
        for context in contexts[scope]:
            evaluated += 1
            matched_conditions: list[dict[str, Any]] = []
            all_match = True
            for condition in rule["conditions"]:
                matched, actual = _condition_matches(context, condition)
                if not matched:
                    all_match = False
                    break
                matched_conditions.append(
                    {
                        "fact": condition["fact"],
                        "operator": condition["operator"],
                        "expected": condition.get("value"),
                        "actual": None if actual is MISSING else actual,
                        "label": condition.get("label", condition["fact"]),
                    }
                )
            if not all_match:
                continue

            if scope == "environment":
                target = "ECLSS"
                target_name = "Environmental Control and Life Support"
            elif scope == "crew":
                member = context["crew"]
                target = str(member.get("id", "UNKNOWN"))
                target_name = str(member.get("name") or target)
            else:
                member = context["crew"]
                sensor = context["sensor"]
                target = f"{member.get('id', 'UNKNOWN')} / {sensor.get('id', 'SENSOR')}"
                target_name = f"{member.get('name') or member.get('id', 'Crew')} — {sensor.get('id', 'Sensor')}"

            explanation_parts = [
                f"{item['label']}: actual value {_format_actual(item['actual'])}; "
                f"condition {item['operator']} {item['expected']}"
                for item in matched_conditions
            ]
            findings.append(
                {
                    "rule_id": rule["id"],
                    "rule_name": rule["name"],
                    "category": rule.get("category", "GENERAL"),
                    "severity": str(rule["severity"]).upper(),
                    "target": target,
                    "target_name": target_name,
                    "message": rule["message"],
                    "recommendation": rule["recommendation"],
                    "explanation": rule.get("rationale", "Rule conditions matched."),
                    "matched_conditions": matched_conditions,
                    "reasoning_trace": explanation_parts,
                    "priority": int(rule.get("priority", 100)),
                }
            )

    findings.sort(
        key=lambda item: (
            -SEVERITY_RANK.get(item["severity"], 0),
            item["priority"],
            item["target"],
            item["rule_id"],
        )
    )

    critical_count = sum(item["severity"] == "CRITICAL" for item in findings)
    warning_count = sum(item["severity"] == "WARNING" for item in findings)
    info_count = sum(item["severity"] == "INFO" for item in findings)
    if critical_count:
        overall_status = "CRITICAL"
        summary = "One or more critical demonstration rules were triggered. Review the findings."
    elif warning_count:
        overall_status = "WARNING"
        summary = "One or more warning demonstration rules were triggered. Review the findings."
    elif findings:
        overall_status = "INFO"
        summary = "Informational rules were triggered; no warning or critical rule matched."
    else:
        overall_status = "NOMINAL"
        summary = "No enabled expert-system rules matched the supplied demonstration state."

    return {
        "run_id": str(uuid4()),
        "generated_at": utc_now(),
        "source": source,
        "engine": "ORION-IV Rule-Based Inference Engine",
        "inference_method": "Data-driven IF-THEN rule evaluation over current mission facts",
        "scenario": state.get("scenario", "UNKNOWN"),
        "overall_status": overall_status,
        "summary": summary,
        "counts": {
            "findings": len(findings),
            "critical": critical_count,
            "warning": warning_count,
            "info": info_count,
            "rules_evaluated": evaluated,
            "rules_matched": len({item["rule_id"] for item in findings}),
        },
        "findings": findings,
        "simulation_only": True,
        "disclaimer": (
            "SIMULATED DEMONSTRATION ONLY. These configurable rules and thresholds are not validated "
            "medical criteria, NASA procedures, or spacecraft operational limits. Do not use for diagnosis "
            "or real-world operational decisions."
        ),
    }


def get_rule_catalog() -> dict[str, Any]:
    knowledge_base = load_knowledge_base()
    items = []
    for rule in knowledge_base["rules"]:
        items.append(
            {
                "id": rule["id"],
                "name": rule["name"],
                "category": rule.get("category", "GENERAL"),
                "scope": rule["scope"],
                "severity": rule["severity"],
                "enabled": bool(rule.get("enabled", True)),
                "description": rule.get("description", ""),
                "conditions": rule["conditions"],
                "recommendation": rule["recommendation"],
                "rationale": rule.get("rationale", ""),
            }
        )
    return {
        "name": knowledge_base.get("name", "ORION-IV Knowledge Base"),
        "version": knowledge_base.get("version", "1.0"),
        "description": knowledge_base.get("description", ""),
        "items": items,
        "count": len(items),
        "enabled_count": sum(item["enabled"] for item in items),
    }
