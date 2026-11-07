"""Strict single-request result contract, independent policy and local reporting."""

import asyncio
import hashlib
from typing import Literal

from pydantic import Field, ValidationError

from app.core.data_safety import redact_sensitive
from app.core.single_pass import REVISION, Frozen, canonical
from app.core.structured_output import safe_schema_issues
from app.evaluation.single_pass_storage import PersistenceError


class SinglePassChoice(Frozen):
    hypothesis_id: str | None = Field(
        default=None, description="Exact supplied hypothesis id, or null for unknown"
    )
    decision: Literal["candidate", "service_only", "unknown"]
    reason: str = Field(
        default="",
        description="Optional brief rationale, preferably at most120 characters; not authoritative evidence",
    )


class SinglePassDecision(Frozen):
    """Internal/server-bound decision; the model only emits SinglePassChoice."""

    packet_id: str = Field(min_length=1, max_length=64)
    service_id: str | None = Field(default=None, max_length=16)
    fault: Literal["cpu", "mem", "disk", "socket", "loss", "delay", "unknown"]
    supporting_refs: list[str] = Field(default_factory=list)
    opposing_refs: list[str] = Field(default_factory=list)
    contrast_refs: list[str] = Field(default_factory=list)
    excluded_hypotheses: list[str] = Field(default_factory=list, max_length=5)
    reason: str = Field(default="", max_length=400)
    gaps: list[str] = Field(default_factory=list, max_length=6)


def validate_decision(packet, decision):
    payload = packet.payload
    if decision.packet_id != payload["packet_id"]:
        raise ValueError("cross_case_packet_id")
    refs = set(decision.supporting_refs + decision.opposing_refs + decision.contrast_refs)
    if not refs <= payload["facts"].keys():
        raise ValueError("unknown_evidence_reference")
    if not set(decision.excluded_hypotheses) <= {h["id"] for h in payload["hypotheses"]}:
        raise ValueError("unknown_excluded_hypothesis")
    if decision.service_id is not None and decision.service_id not in payload["services"]:
        raise ValueError("unknown_service_alias")
    if decision.service_id is None and decision.fault != "unknown":
        raise ValueError("type_requires_service")
    for ref in decision.supporting_refs:
        if payload["facts"][ref]["service"] != decision.service_id:
            raise ValueError("cross_service_mechanism_reference")
    service = dict(packet.service_aliases).get(decision.service_id)
    supported = (
        decision.service_id is not None
        and payload["services"][decision.service_id]["independently_supported"]
    )
    fault = decision.fault
    gaps = [str(redact_sensitive(g))[:160] for g in decision.gaps]
    if fault != "unknown":
        hypothesis = next(
            (
                h
                for h in payload["hypotheses"]
                if h["service"] == decision.service_id and h["fault"] == fault
            ),
            None,
        )
        if hypothesis is None:
            raise ValueError("unselected_hypothesis")
        if (
            not set(hypothesis["support"]) <= set(decision.supporting_refs)
            or not set(hypothesis["opposition"]) <= set(decision.opposing_refs)
            or not set(hypothesis["contrast"]) <= set(decision.contrast_refs)
        ):
            raise ValueError("missing_material_evidence")
        if not hypothesis["eligible"]:
            fault = "unknown"
            gaps = sorted(set(gaps + hypothesis["gaps"]))
    if not supported:
        service, fault = None, "unknown"
        gaps.append("service_localization_not_supported")
    authoritative_refs = dict(packet.ref_aliases)
    return {
        "case_id": packet.case_id,
        "packet_sha256": packet.sha256,
        "service": service,
        "fault": fault,
        "status": "supported" if service and fault != "unknown" else "insufficient_evidence",
        "supporting_refs": [authoritative_refs[r] for r in decision.supporting_refs],
        "opposing_refs": [authoritative_refs[r] for r in decision.opposing_refs],
        "contrast_refs": [authoritative_refs[r] for r in decision.contrast_refs],
        "excluded_hypotheses": decision.excluded_hypotheses,
        "reason": str(redact_sensitive(decision.reason)),
        "gaps": sorted(set(gaps)),
    }


def render_report(result):
    return (
        f"服务：{result['service'] or 'unknown'}；故障类型：{result['fault']}。\n"
        f"判断：{result['reason']}\n缺口：{', '.join(result['gaps']) or '未报告'}"
    )


def validate_choice(packet, choice):
    payload = packet.payload
    if choice.decision == "unknown":
        if choice.hypothesis_id is not None:
            raise ValueError("unknown_choice_has_hypothesis")
        h = None
    else:
        h = next((h for h in payload["hypotheses"] if h["id"] == choice.hypothesis_id), None)
        if h is None:
            raise ValueError("unknown_hypothesis_id")
    reason = str(redact_sensitive(choice.reason))[:400]
    decision = SinglePassDecision(
        packet_id=payload["packet_id"],
        service_id=h["service"] if h else None,
        fault=h["fault"] if h and choice.decision == "candidate" else "unknown",
        supporting_refs=h["support"] if h else [],
        opposing_refs=h["opposition"] if h else [],
        contrast_refs=h["contrast"] if h else [],
        reason=reason,
    )
    result = validate_decision(packet, decision)
    proposal_service = payload["services"][h["service"]]["name"] if h else None
    proposed_type = h["fault"] if h and choice.decision == "candidate" else "unknown"
    tentative = bool(
        h
        and h.get("candidate_supported", h["eligible"])
        and result["service"]
        and choice.decision == "candidate"
    )
    reasons = list(h["gaps"]) if h and proposed_type != "unknown" and not h["eligible"] else []
    if h and not result["service"]:
        reasons.append("service_localization_not_supported")
    if choice.decision == "service_only":
        reasons.append("model_service_only")
    if choice.decision == "unknown":
        reasons.append("model_abstained")
    result.update(
        proposal={
            "hypothesis_id": h["id"] if h else None,
            "decision": choice.decision,
            "service": proposal_service,
            "fault": proposed_type,
        },
        candidate_service=result["service"] if tentative else None,
        candidate_fault=proposed_type if tentative else "unknown",
        confirmed=result["status"] == "supported",
        service_certainty="supported_localization" if result["service"] else "unknown",
        downgrade_reasons=sorted(set(reasons)),
        evidence_binding_origin="server_bound_candidate",
    )
    if tentative and not result["confirmed"]:
        result["status"] = "candidate"
    return result


def configuration_fingerprint(packet, config):
    from app.evaluation.single_pass_packet import SYSTEM_PROMPT

    value = [REVISION, packet.sha256, config, SYSTEM_PROMPT, SinglePassChoice.model_json_schema()]
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _usage(raw):
    metadata = getattr(raw, "usage_metadata", None) or {}
    result = {}
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        value = metadata.get(key)
        result[key] = (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )
    for key in ("input_token_details", "output_token_details"):
        if isinstance(metadata.get(key), dict):
            result[key] = {
                k: v
                for k, v in metadata[key].items()
                if k in {"cache_read", "cache_creation", "audio", "reasoning"}
                and isinstance(v, int)
                and not isinstance(v, bool)
                and v >= 0
            }
    result["observed"] = result.get("total_tokens") is not None
    return result


async def run_single_pass(
    packet, *, journal, experiment_id, config, model_factory, resume=False, timeout_seconds=180
):
    """Reserve before dispatch. Resume reuses success, never retries uncertain attempts."""
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime
    from app.evaluation.single_pass_packet import OUTPUT_ALLOWANCE, SYSTEM_PROMPT

    if not 0 < timeout_seconds <= 300:
        raise ValueError("invalid_timeout")
    fingerprint = configuration_fingerprint(packet, config)
    row = journal.load(experiment_id, packet.case_id)
    if row:
        if row["fingerprint"] != fingerprint:
            raise ValueError("configuration_mismatch")
        if resume and row["state"] == "reserved":
            row = journal.recover(experiment_id, packet.case_id, fingerprint)
        if row["state"] != "complete":
            raise ValueError("attempt_already_reserved")
        if not resume:
            raise ValueError("explicit_resume_required")
        return {**journal.cached(row), "reused": True, "new_model_calls": 0}
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=packet.text)]
    with diagnosis_runtime(DiagnosisRuntime(context="evaluation")):
        model = model_factory()
        if getattr(model, "max_retries", None) != 0:
            raise ValueError("hidden_retries_forbidden")
        # Check the actual provider envelope as well as the conservative packet estimate.
        if hasattr(model, "_get_request_payload"):
            from langchain_core.utils.function_calling import convert_to_openai_tool

            transport = model._get_request_payload(
                messages,
                tools=[convert_to_openai_tool(SinglePassChoice)],
                tool_choice={"type": "function", "function": {"name": "SinglePassChoice"}},
                parallel_tool_calls=False,
            )
            if len(canonical(transport).encode()) + OUTPUT_ALLOWANCE > 16384:
                raise ValueError("evidence_budget_exceeded")
        target = model.with_structured_output(
            SinglePassChoice, include_raw=True, method="function_calling"
        )
        token = journal.reserve(experiment_id, packet.case_id, fingerprint)
        usage, finish_reason = _usage(None), None
        failure_code = "provider_error"
        failure_detail_code = "provider_transport_failed"
        issues = []
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await target.ainvoke(messages)
            failure_code = "invalid_structured_output"
            failure_detail_code = "raw_usage_missing"
            if not isinstance(response, dict) or "raw" not in response:
                raise ValueError("raw_usage_missing")
            raw = response["raw"]
            usage = _usage(raw)
            finish_reason = (getattr(raw, "response_metadata", None) or {}).get("finish_reason")
            finish_reason = (
                finish_reason
                if finish_reason in {"stop", "tool_calls", "length", "content_filter"}
                else None
            )
            if response.get("parsing_error") is not None or response.get("parsed") is None:
                issues = [
                    i.model_dump()
                    for i in safe_schema_issues(response.get("parsing_error") or ValueError())
                ]
                failure_detail_code = "schema_parse_failed"
                raise ValueError("schema_parse_failed")
            failure_detail_code = "schema_parse_failed"
            parsed = SinglePassChoice.model_validate(response["parsed"])
            if len(canonical(parsed.model_dump()).encode()) > OUTPUT_ALLOWANCE:
                failure_detail_code = "result_budget_exceeded"
                raise ValueError("result_budget_exceeded")
            if finish_reason in {"length", "content_filter"}:
                failure_detail_code = "output_incomplete"
                raise ValueError("output_incomplete")
            failure_code = "invalid_evidence_binding"
            failure_detail_code = "choice_binding_invalid"
            result = validate_choice(packet, parsed)
            result.update(
                model_calls=1,
                new_model_calls=1,
                usage=usage,
                finish_reason=finish_reason,
                reused=False,
                configuration_sha256=fingerprint,
                report=render_report(result),
            )
        except asyncio.CancelledError:
            try:
                journal.finish(
                    experiment_id,
                    packet.case_id,
                    token,
                    {
                        "status": "error",
                        "error": "cancelled_unknown",
                        "case_id": packet.case_id,
                        "packet_sha256": packet.sha256,
                        "model_calls": 1,
                        "new_model_calls": 1,
                        "usage": usage,
                        "configuration_sha256": fingerprint,
                    },
                )
            except PersistenceError as exc:
                raise asyncio.CancelledError() from exc
            raise
        except Exception as exc:
            if isinstance(exc, ValidationError):
                issues = [i.model_dump() for i in safe_schema_issues(exc)]
            if isinstance(exc, ValueError) and str(exc) in {
                "unknown_hypothesis_id",
                "unknown_choice_has_hypothesis",
            }:
                failure_detail_code = str(exc)
            result = {
                "case_id": packet.case_id,
                "status": "error",
                "error": "timeout_unknown" if isinstance(exc, TimeoutError) else failure_code,
                "failure_detail_code": "timeout_unknown"
                if isinstance(exc, TimeoutError)
                else failure_detail_code,
                "schema_issues": issues,
                "model_calls": 1,
                "new_model_calls": 1,
                "usage": usage,
                "finish_reason": finish_reason,
                "configuration_sha256": fingerprint,
                "reused": False,
            }
        result["packet_sha256"] = packet.sha256
        journal.finish(experiment_id, packet.case_id, token, result)
        return result
