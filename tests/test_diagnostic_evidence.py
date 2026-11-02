import json

import pytest
from langchain_core.messages import ToolMessage

from app.core.data_safety import sanitize_tool_message
from app.core.diagnostic_evidence import (
    CandidateEvidence,
    CandidateHint,
    EvidenceFact,
    InitialEvidenceBundle,
    MechanismProfile,
    serialize_evidence_bundle,
)


def _fact(ref: str, *, service: str = "订单服务", mechanism: str = "cpu") -> EvidenceFact:
    return EvidenceFact(
        evidence_ref=ref,
        evidence_type="metric",
        service=service,
        mechanism=mechanism,
        statement="故障后指标出现持续变化",
        data={"before": 1.0, "after": 8.0},
    )


def _candidate(
    service: str = "订单服务",
    *,
    noisy: bool = False,
    profile_order: tuple[str, ...] = ("cpu", "delay"),
) -> CandidateEvidence:
    profiles = {}
    for mechanism in profile_order:
        state = "direct_signal" if mechanism == "cpu" else "possible_symptom"
        profiles[mechanism] = MechanismProfile(
            mechanism=mechanism,
            state=state,
            evidence_refs=(f"ev-{service}-{mechanism}",),
            first_anomalous_time=120,
            reasons=("直接资源变化" if mechanism == "cpu" else "可能是传播症状",),
        )
    repeated = tuple(
        {"timestamp": index, "value": index, "note": "异常详情" * 20}
        for index in range(40 if noisy else 2)
    )
    return CandidateEvidence(
        service=service,
        selection_reasons=("direct:cpu",),
        inbound_callers=("frontend",),
        outbound_dependencies=("database",),
        mechanism_profiles=profiles,
        facts=(_fact(f"ev-{service}-cpu", service=service),),
        trends=repeated,
        traces=repeated,
        logs=tuple("错误日志" * 100 for _ in range(40 if noisy else 1)),
    )


def _bundle(*, noisy: bool = False, profile_order=("cpu", "delay")) -> InitialEvidenceBundle:
    return InitialEvidenceBundle(
        case_id="case-开发-001",
        services=("database", "frontend", "订单服务"),
        time_start=0,
        time_end=300,
        incident_time=100,
        candidates=(_candidate(noisy=noisy, profile_order=profile_order),),
        candidate_hints=(
            CandidateHint(
                service="database",
                visible_signals=("disk",),
                topology_neighbors=("订单服务",),
                evidence_refs=("ev-hint-database",),
            ),
        ),
        limitations=("没有部署、GC或故障注入标签。",),
    )


def test_bundle_serialization_is_canonical_and_immutable():
    first = _bundle(profile_order=("cpu", "delay"))
    second = _bundle(profile_order=("delay", "cpu"))

    encoded_first = serialize_evidence_bundle(first, max_bytes=16_384)
    encoded_second = serialize_evidence_bundle(second, max_bytes=16_384)

    assert encoded_first.text == encoded_second.text
    assert encoded_first.sha256 == encoded_second.sha256
    assert encoded_first.byte_length == len(encoded_first.text.encode("utf-8"))
    with pytest.raises((TypeError, AttributeError)):
        encoded_first.text = "mutated"
    payload_copy = encoded_first.payload
    payload_copy["case_id"] = "mutated"
    assert encoded_first.payload["case_id"] == "case-开发-001"
    assert json.loads(encoded_first.text)["case_id"] == "case-开发-001"


def test_bundle_reserves_tool_message_overhead():
    encoded = serialize_evidence_bundle(_bundle(noisy=True), max_bytes=4096)

    assert encoded.byte_length <= 4096 - 512
    safe = sanitize_tool_message(
        ToolMessage(content=encoded.text, tool_call_id="bundle-call", name="initial_evidence")
    )
    assert "tool_result_truncated" not in str(safe.content)
    assert json.loads(safe.content)["case_id"] == "case-开发-001"


def test_bundle_compaction_preserves_identity_direct_signals_and_refs():
    encoded = serialize_evidence_bundle(_bundle(noisy=True), max_bytes=4096)
    payload = json.loads(encoded.text)
    candidate = payload["candidates"][0]

    assert payload["case_id"] == "case-开发-001"
    assert payload["services"] == ["database", "frontend", "订单服务"]
    assert candidate["service"] == "订单服务"
    assert candidate["inbound_callers"] == ["frontend"]
    assert candidate["outbound_dependencies"] == ["database"]
    assert candidate["mechanism_profiles"]["cpu"]["state"] == "direct_signal"
    assert candidate["mechanism_profiles"]["cpu"]["evidence_refs"] == ["ev-订单服务-cpu"]
    assert candidate["facts"][0]["evidence_ref"] == "ev-订单服务-cpu"
    assert payload["limitations"]


def test_bundle_records_every_omission():
    payload = json.loads(serialize_evidence_bundle(_bundle(noisy=True), max_bytes=4096).text)

    omitted = set(payload["omitted_paths"])
    assert "candidates[0].logs" in omitted
    assert "candidates[0].traces" in omitted
    assert "candidates[0].trends" in omitted


def test_unicode_and_json_escaping_stay_within_actual_sanitizer_boundary():
    bundle = _bundle(noisy=True).model_copy(
        update={
            "limitations": ('含有引号 "、反斜线 \\、换行\n和多字节字符，不得破坏JSON边界。' * 20,)
        }
    )
    encoded = serialize_evidence_bundle(bundle, max_bytes=16_384)
    safe = sanitize_tool_message(
        ToolMessage(content=encoded.text, tool_call_id="unicode-call", name="initial_evidence")
    )

    assert encoded.byte_length <= 16_384 - 512
    assert "tool_result_truncated" not in str(safe.content)
    assert json.loads(safe.content)["limitations"]
