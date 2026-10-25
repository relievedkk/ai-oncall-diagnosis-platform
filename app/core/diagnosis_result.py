"""Pure, bounded diagnosis reports and task-local evidence validation.

An accepted reference proves provenance and usable data, not causality. Runtime
ownership, context and termination remain the responsibility of the caller.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
from copy import deepcopy
from typing import Annotated, Literal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from app.core.evidence import decode_evidence_result, evidence_response_layers, valid_evidence
from oncall_logging.redaction import make_redactor

Outcome = Literal[
    "diagnosed",
    "insufficient_evidence",
    "budget_exhausted",
    "dependency_failed",
    "no_active_alerts",
    "legacy_unverified",
]
Text = Annotated[str, Field(max_length=2000)]
Ref = Annotated[str, Field(max_length=160)]
References = Annotated[list[Ref], Field(max_length=32)]
TextList = Annotated[list[Text], Field(max_length=32)]
_OUTCOMES = {
    "diagnosed",
    "insufficient_evidence",
    "budget_exhausted",
    "dependency_failed",
    "no_active_alerts",
    "legacy_unverified",
}
_AUTHORITY = {"budget_exhausted", "dependency_failed", "no_active_alerts"}
_SECRET_NAME = re.compile(r"password|secret|api.?key|token|authorization|cookie", re.I)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.S | re.I,
)


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Text
    evidence_refs: References = Field(default_factory=list)


class Hypothesis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: Text
    supporting_refs: References = Field(default_factory=list)
    opposing_refs: References = Field(default_factory=list)
    status: Literal["supported", "unverified", "rejected"] = "unverified"


class DiagnosisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    outcome: Outcome = "insufficient_evidence"
    summary: Text = ""
    observations: list[Observation] = Field(default_factory=list, max_length=32)
    hypotheses: list[Hypothesis] = Field(default_factory=list, max_length=16)
    missing_evidence: TextList = Field(default_factory=list)
    next_checks: TextList = Field(default_factory=list)
    limitations: TextList = Field(default_factory=list)
    validation_issues: TextList = Field(default_factory=list)
    termination_reason: Annotated[str, Field(max_length=500)] = ""


def reference_evidence(evidence: list[dict], task_scope: str) -> list[dict]:
    """Copy evidence, retaining checkpoint refs and deriving scoped legacy IDs.

    New observations should receive uuid4 at collection time. This helper only
    supplies deterministic compatibility IDs for checkpoints predating that path.
    An existing foreign task scope is deliberately never overwritten.
    """
    result = []
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            continue
        copied = deepcopy(item)
        if not copied.get("evidence_ref"):
            encoded = json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            copied["evidence_ref"] = "legacy-" + str(
                uuid5(NAMESPACE_URL, f"oncall:{task_scope}:{index}:{digest}")
            )
        copied.setdefault("task_scope", task_scope)
        result.append(copied)
    return result


def _redact(value: str) -> str:
    # Reuse the shared boundary when loaded, without importing configuration or
    # requiring provider credentials in the standalone replay process.
    boundary = sys.modules.get("app.core.data_safety")
    if boundary is not None and hasattr(boundary, "redact_sensitive"):
        return boundary.redact_sensitive(value)
    secrets = [value for key, value in os.environ.items() if _SECRET_NAME.search(key)]
    value = _PRIVATE_KEY.sub("[REDACTED]", value)
    value = re.sub(r"(?im)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]+", r"\1[REDACTED]", value)
    return make_redactor(secrets)(value)


def _scopes(item: dict) -> set[str]:
    services = set()

    def collect(value, depth=0):
        if depth >= 16:
            return
        if isinstance(value, dict):
            for key in ("service", "service_name", "target_service"):
                if isinstance(value.get(key), str) and value[key].strip():
                    services.add(value[key].strip())
            for nested in value.values():
                if isinstance(nested, dict | list):
                    collect(nested, depth + 1)
        elif isinstance(value, list):
            for nested in value:
                collect(nested, depth + 1)

    for layer in evidence_response_layers(item) or [item]:
        collect(layer)
    return services


def _availability(item: dict, context: str, target_service: str) -> tuple[bool, bool, str]:
    layers = evidence_response_layers(item) or [item]
    markers = []
    for layer in layers:
        for key in ("provenance", "source", "data_source", "data_status"):
            value = layer.get(key)
            if isinstance(value, str):
                markers.append(value.casefold())
            elif isinstance(value, dict):
                markers.extend(str(v).casefold() for v in value.values())
    if any(marker in {"demo", "example", "sample", "fallback_demo"} for marker in markers):
        return False, False, "demo/example evidence"
    synthetic = "synthetic_fixture" in markers
    if synthetic and context == "production":
        return False, False, "synthetic_fixture unavailable in production"
    if target_service and any(service != target_service for service in _scopes(item)):
        return False, False, "evidence belongs to another service"
    payload = decode_evidence_result(item)
    kind = str(item.get("evidence_type", item.get("type", ""))).casefold().strip()
    if kind == "knowledge":
        usable = bool(payload and payload.get("success") is True and item.get("source"))
        return usable, False, "knowledge response failed" if not usable else ""
    usable = valid_evidence(item)
    return (
        usable,
        usable,
        "evidence is failed, demo, or lacks usable observations" if not usable else "",
    )


def normalize_result(
    raw: dict | None,
    evidence: list[dict],
    *,
    task_scope: str,
    target_service: str = "",
    context: str = "production",
    termination_reason: str = "",
    authority_outcome: str | None = None,
) -> dict:
    """Validate model claims against this task's evidence and executor facts."""
    if context not in {"production", "replay", "evaluation"}:
        raise ValueError("context must be production, replay, or evaluation")
    if authority_outcome is not None and authority_outcome not in _AUTHORITY:
        raise ValueError("authority_outcome must be an executor terminal outcome")
    issues: list[str] = []

    def issue(message: str):
        if message not in issues and len(issues) < 32:
            issues.append(_redact(message)[:2000])

    def text(value, limit=2000):
        if not isinstance(value, str):
            issue("Non-string text discarded")
            return ""
        cleaned = _redact(value)
        if len(cleaned) > limit:
            issue("Report text truncated to contract limit")
        return cleaned[:limit]

    def items(value, limit=32):
        if not isinstance(value, list):
            issue("Non-list report field discarded")
            return []
        if len(value) > limit:
            issue("Report list truncated to contract limit")
        return value[:limit]

    source = raw if isinstance(raw, dict) else {}
    outcome = (
        source.get("outcome", "insufficient_evidence") if raw is not None else "legacy_unverified"
    )
    if not isinstance(outcome, str) or outcome not in _OUTCOMES:
        issue("Unknown report outcome")
        outcome = "insufficient_evidence"
    if outcome in _AUTHORITY and not authority_outcome:
        issue("Model output cannot establish executor terminal facts")
        outcome = "insufficient_evidence"
    table = {}
    conflicts = set()
    for item in reference_evidence(evidence, task_scope):
        ref = item.get("evidence_ref")
        if not isinstance(ref, str) or not ref or len(ref) > 160:
            issue("Invalid evidence identity")
            continue
        if ref in table and table[ref] != item:
            conflicts.add(ref)
        table[ref] = item
    available = {}
    observed = set()
    synthetic_used = False
    for ref, item in table.items():
        if ref in conflicts:
            available[ref] = (False, "conflicting duplicate evidence identity")
        elif item.get("task_scope") != task_scope:
            available[ref] = (False, "foreign task evidence")
        else:
            usable, observation, reason = _availability(item, context, target_service)
            available[ref] = usable, reason
            if observation:
                observed.add(ref)

    def refs(value):
        nonlocal synthetic_used
        valid = []
        for ref in items(value):
            if not isinstance(ref, str) or ref not in available:
                issue("Unknown evidence reference")
                continue
            usable, reason = available[ref]
            if not usable:
                issue(f"Invalid evidence reference {ref}: {reason}")
                continue
            if ref not in valid:
                valid.append(ref)
                if "synthetic_fixture" in json.dumps(table[ref], ensure_ascii=False, default=str):
                    synthetic_used = True
        return valid

    observations = []
    for item in items(source.get("observations", [])):
        if not isinstance(item, dict):
            issue("Malformed observation discarded")
            continue
        observations.append(
            {
                "text": text(item.get("text", "")),
                "evidence_refs": refs(item.get("evidence_refs", [])),
            }
        )
    hypotheses = []
    supported_observation = False
    for item in items(source.get("hypotheses", []), 16):
        if not isinstance(item, dict):
            issue("Malformed hypothesis discarded")
            continue
        supporting = refs(item.get("supporting_refs", []))
        opposing = refs(item.get("opposing_refs", []))
        status = item.get("status", "unverified")
        if not isinstance(status, str) or status not in {"supported", "unverified", "rejected"}:
            issue("Unknown hypothesis status")
            status = "unverified"
        if status == "supported" and not supporting:
            status = "unverified"
        description = text(item.get("description", ""))
        if status == "supported" and description.strip() and observed.intersection(supporting):
            supported_observation = True
        hypotheses.append(
            {
                "description": description,
                "supporting_refs": supporting,
                "opposing_refs": opposing,
                "status": status,
            }
        )
    result = {
        "schema_version": 1,
        "outcome": outcome,
        "summary": text(source.get("summary", "")),
        "observations": observations,
        "hypotheses": hypotheses,
        **{
            key: [text(value) for value in items(source.get(key, []))]
            for key in ("missing_evidence", "next_checks", "limitations")
        },
        "termination_reason": text(termination_reason, 500),
    }
    if synthetic_used or any("synthetic_fixture" in reason for _, reason in available.values()):
        result["limitations"] = (
            ["来源：synthetic_fixture（合成实验数据，非真实业务验证）"] + result["limitations"]
        )[:32]
    if outcome == "diagnosed" and not supported_observation:
        issue("Diagnosis lacks a supported hypothesis citing usable observations")
    if outcome == "diagnosed" and issues:
        result["outcome"] = "insufficient_evidence"
    if authority_outcome:
        result["outcome"] = authority_outcome
    result["validation_issues"] = issues
    return DiagnosisResult.model_validate(result).model_dump()


def _markdown(value: str) -> str:
    value = html.escape(_redact(value), quote=True)
    value = re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)
    return " ".join(value.splitlines())


def render_result(result: dict) -> str:
    """Render only bounded contract fields as deterministic, inert Markdown."""
    report = DiagnosisResult.model_validate(result)
    labels = {
        "diagnosed": "诊断结论（待人工复核）",
        "insufficient_evidence": "证据不足",
        "budget_exhausted": "诊断预算耗尽",
        "dependency_failed": "诊断依赖失败",
        "no_active_alerts": "当前范围无活跃告警",
        "legacy_unverified": "历史报告，未进行结构化校验",
    }
    lines = [f"## {labels[report.outcome]}", "", _markdown(report.summary)]
    if report.termination_reason:
        lines += ["", f"终止原因：{_markdown(report.termination_reason)}"]
    if report.observations:
        lines += ["", "### 观测事实"]
        for item in report.observations:
            references = ", ".join(_markdown(ref) for ref in item.evidence_refs)
            lines.append(
                f"- {_markdown(item.text)}"
                + (f" （证据：{references}）" if references else " （无有效证据引用）")
            )
    if report.hypotheses:
        lines += ["", "### 候选原因"]
        for item in report.hypotheses:
            lines.append(f"- {_markdown(item.description)} [{item.status}]")
            if item.supporting_refs:
                lines.append(
                    "  - 支持：" + ", ".join(_markdown(ref) for ref in item.supporting_refs)
                )
            if item.opposing_refs:
                lines.append("  - 反证：" + ", ".join(_markdown(ref) for ref in item.opposing_refs))
    for key, label in (
        ("missing_evidence", "缺失证据"),
        ("next_checks", "后续验证"),
        ("limitations", "范围与限制"),
        ("validation_issues", "引用校验问题"),
    ):
        values = getattr(report, key)
        if values:
            lines += ["", f"### {label}", *(f"- {_markdown(value)}" for value in values)]
    return "\n".join(lines).strip() + "\n"
