from app.core.diagnosis_result import normalize_result, render_result
from app.core.diagnostic_evidence import (
    CandidateEvidence,
    CandidateHint,
    DiagnosisEvidencePolicy,
    EvidenceFact,
    EvidenceRequest,
    FollowupEvidenceBundle,
    InitialEvidenceBundle,
    MechanismProfile,
    serialize_evidence_bundle,
)
from tests.test_diagnosis_result import REF, claim, metric


def typed_claim(service="checkout", fault="cpu"):
    raw = claim(root_cause_service="checkout", fault_type="cpu", cause_evidence_refs=[REF])
    raw["hypotheses"][0].update(service=service, fault_type=fault)
    return raw


def test_requested_complete_diagnosis_cannot_finish_without_typed_cause():
    result = normalize_result(
        {"outcome": "diagnosed"},
        [],
        task_scope="case",
        require_complete=True,
        termination_reason="sufficient_evidence",
    )
    assert result["outcome"] == "insufficient_evidence"
    assert result["diagnosis_complete"] is False
    assert result["termination_reason"] != "sufficient_evidence"
    assert result["fault_type"] == "unknown"


def test_catalog_rejects_multiple_services_before_completion():
    raw = claim(
        root_cause_service="checkout, currency", fault_type="delay", cause_evidence_refs=[REF]
    )
    result = normalize_result(
        raw,
        [metric()],
        task_scope="task-a",
        require_complete=True,
        service_catalog=("checkout", "currency"),
    )
    assert result["root_cause_service"] == ""
    assert result["diagnosis_complete"] is False
    assert result["outcome"] == "insufficient_evidence"


def test_comparison_required_does_not_accept_only_one_explanation():
    raw = claim(root_cause_service="checkout", fault_type="cpu", cause_evidence_refs=[REF])
    result = normalize_result(
        raw, [metric()], task_scope="task-a", require_complete=True, require_comparison=True
    )
    assert not result["diagnosis_complete"]
    assert result["outcome"] == "insufficient_evidence"


def test_complete_diagnosis_requires_valid_cause_references_and_is_rendered():
    raw = typed_claim()
    result = normalize_result(raw, [metric()], task_scope="task-a", require_complete=True)
    assert result["diagnosis_complete"] is True
    assert "checkout" in render_result(result)
    assert "cpu" in render_result(result)
    raw["cause_evidence_refs"] = ["fake"]
    rejected = normalize_result(raw, [metric()], task_scope="task-a", require_complete=True)
    assert rejected["diagnosis_complete"] is False
    assert rejected["outcome"] == "insufficient_evidence"


def test_strict_completion_rejects_supported_hypothesis_for_other_service():
    result = normalize_result(
        typed_claim(service="frontend"),
        [metric()],
        task_scope="task-a",
        require_complete=True,
    )
    assert result["diagnosis_complete"] is False
    assert result["outcome"] == "insufficient_evidence"


def test_strict_completion_rejects_supported_hypothesis_for_other_fault():
    result = normalize_result(
        typed_claim(fault="delay"),
        [metric()],
        task_scope="task-a",
        require_complete=True,
    )
    assert result["diagnosis_complete"] is False
    assert result["outcome"] == "insufficient_evidence"


def test_strict_completion_accepts_matching_typed_hypothesis():
    result = normalize_result(
        typed_claim(),
        [metric()],
        task_scope="task-a",
        require_complete=True,
    )
    assert result["diagnosis_complete"] is True
    assert result["hypotheses"][0]["service"] == "checkout"
    assert result["hypotheses"][0]["fault_type"] == "cpu"


def test_strict_completion_rejects_fault_outside_allowed_vocabulary():
    result = normalize_result(
        typed_claim(fault="cpu_mem_delay") | {"fault_type": "cpu_mem_delay"},
        [metric()],
        task_scope="task-a",
        require_complete=True,
        allowed_fault_types=("cpu", "mem", "delay", "unknown"),
    )
    assert result["fault_type"] == "unknown"
    assert result["diagnosis_complete"] is False
    assert result["outcome"] == "insufficient_evidence"
    assert "allowed vocabulary" in " ".join(result["validation_issues"])


def _policy_candidate(service, mechanism, ref, *, delay_symptom=False, ambiguous=False):
    profiles = {
        mechanism: MechanismProfile(
            mechanism=mechanism,
            state="direct_signal",
            evidence_refs=(ref,),
            strong_change=True,
            strongest_metric=f"{service}_{mechanism}",
            fault_eligible=not ambiguous,
        )
    }
    if delay_symptom:
        profiles["delay"] = MechanismProfile(
            mechanism="delay",
            state="possible_symptom",
            evidence_refs=("delay-ref",),
            strong_change=True,
            strongest_metric=f"{service}_latency",
        )
    if ambiguous:
        profiles["mem"] = MechanismProfile(
            mechanism="mem",
            state="direct_signal",
            evidence_refs=("mem-ref",),
            strong_change=True,
            strongest_metric=f"{service}_mem",
            fault_eligible=False,
        )
    facts = [
        EvidenceFact(
            evidence_ref=ref,
            evidence_type="structured_telemetry",
            service=service,
            mechanism=mechanism,
            statement="direct structured signal",
        )
    ]
    if delay_symptom:
        facts.append(
            EvidenceFact(
                evidence_ref="delay-ref",
                evidence_type="structured_telemetry",
                service=service,
                mechanism="delay",
                statement="delay may be propagated",
            )
        )
    return CandidateEvidence(service=service, mechanism_profiles=profiles, facts=tuple(facts))


def _policy(ambiguous=False):
    checkout = _policy_candidate("checkout", "cpu", REF, delay_symptom=True, ambiguous=ambiguous)
    checkout = checkout.model_copy(
        update={
            "outbound_dependencies": ("database",),
            "facts": checkout.facts
            + (
                EvidenceFact(
                    evidence_ref="edge-ref",
                    evidence_type="topology_edge",
                    service="checkout",
                    statement="checkout -> database",
                    data={"caller": "checkout", "callee": "database"},
                ),
            ),
        }
    )
    initial = serialize_evidence_bundle(
        InitialEvidenceBundle(
            case_id="task-a",
            services=("checkout", "database", "payments"),
            time_start=0,
            time_end=300,
            incident_time=120,
            candidates=(checkout, _policy_candidate("database", "cpu", "db-ref")),
            candidate_hints=(
                CandidateHint(
                    service="payments",
                    visible_signals=("cpu:direct_signal",),
                    evidence_refs=("pay-ref",),
                ),
            ),
            limitations=("test bundle",),
        ),
        max_bytes=16_384,
    )
    return initial, DiagnosisEvidencePolicy.from_bundles(initial)


def _policy_evidence():
    return [
        metric(),
        metric(evidence_ref="delay-ref"),
        metric(evidence_ref="edge-ref"),
        metric(evidence_ref="db-ref", service_name="database"),
        metric(evidence_ref="mem-ref"),
        metric(evidence_ref="pay-ref", service_name="payments"),
    ]


def test_policy_rejects_unknown_or_unexpanded_root():
    _, policy = _policy()
    raw = typed_claim(service="payments") | {
        "root_cause_service": "payments",
        "cause_evidence_refs": [REF],
    }
    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)
    assert result["root_cause_service"] == ""
    assert result["outcome"] == "insufficient_evidence"


def test_policy_rejects_fault_without_matching_service_ref():
    _, policy = _policy()
    raw = typed_claim(fault="mem") | {"fault_type": "mem"}
    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)
    assert result["fault_type"] == "unknown"
    assert result["outcome"] == "insufficient_evidence"


def test_policy_downgrades_delay_when_direct_resource_blocks_it():
    _, policy = _policy()
    raw = typed_claim(fault="delay") | {
        "fault_type": "delay",
        "cause_evidence_refs": ["delay-ref"],
    }
    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)
    assert result["fault_type"] == "unknown"
    assert "possible symptom" in " ".join(result["validation_issues"])


def test_policy_rejects_reverse_dependency_causality():
    _, policy = _policy()
    raw = typed_claim(fault="unknown") | {
        "fault_type": "unknown",
        "cause_evidence_refs": ["edge-ref"],
    }
    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)
    assert result["root_cause_service"] == ""
    assert "caller -> callee" in " ".join(result["validation_issues"])


def test_policy_allows_unknown_for_ambiguous_direct_signals():
    _, policy = _policy(ambiguous=True)
    raw = typed_claim(fault="unknown") | {"fault_type": "unknown"}
    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)
    assert result["root_cause_service"] == "checkout"
    assert result["fault_type"] == "unknown"


def test_policy_rejects_typed_fault_from_ineligible_direct_signal():
    checkout = _policy_candidate("checkout", "cpu", REF)
    mem = MechanismProfile(
        mechanism="mem",
        state="direct_signal",
        evidence_refs=("mem-ref",),
        strong_change=True,
        strongest_metric="checkout_mem",
        fault_eligible=False,
    )
    checkout = checkout.model_copy(
        update={
            "mechanism_profiles": {**checkout.mechanism_profiles, "mem": mem},
            "facts": checkout.facts
            + (
                EvidenceFact(
                    evidence_ref="mem-ref",
                    evidence_type="structured_telemetry",
                    service="checkout",
                    mechanism="mem",
                    statement="memory shifted but lost temporal competition",
                ),
            ),
        }
    )
    initial = serialize_evidence_bundle(
        InitialEvidenceBundle(
            case_id="task-a",
            services=("checkout",),
            time_start=0,
            time_end=300,
            incident_time=120,
            candidates=(checkout,),
            limitations=("test bundle",),
        ),
        max_bytes=16_384,
    )
    policy = DiagnosisEvidencePolicy.from_bundles(initial)
    raw = typed_claim(fault="mem") | {
        "fault_type": "mem",
        "cause_evidence_refs": ["mem-ref"],
    }
    raw["hypotheses"][0]["supporting_refs"] = ["mem-ref"]

    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)

    assert result["fault_type"] == "unknown"
    assert result["outcome"] == "insufficient_evidence"


def test_policy_rejects_unknown_service_without_competing_ambiguity():
    _, policy = _policy()
    raw = typed_claim(fault="unknown") | {"fault_type": "unknown"}

    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)

    assert result["root_cause_service"] == ""
    assert result["outcome"] == "insufficient_evidence"


def test_valid_followup_expands_allowed_root():
    initial, _ = _policy()
    request = EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="mechanism_details",
        justification_refs=("pay-ref",),
    )
    followup = serialize_evidence_bundle(
        FollowupEvidenceBundle(
            case_id="task-a",
            request=request,
            candidates=(_policy_candidate("payments", "cpu", "pay-ref"),),
            limitations=("bounded followup",),
        ),
        max_bytes=16_384,
    )
    policy = DiagnosisEvidencePolicy.from_bundles(initial, followup)
    raw = typed_claim(service="payments") | {
        "root_cause_service": "payments",
        "cause_evidence_refs": ["pay-ref"],
    }
    raw["hypotheses"][0]["supporting_refs"] = ["pay-ref"]
    result = normalize_result(raw, _policy_evidence(), task_scope="task-a", evidence_policy=policy)
    assert result["root_cause_service"] == "payments"
    assert result["fault_type"] == "cpu"


def test_no_policy_preserves_legacy_normalization():
    raw = typed_claim()
    assert normalize_result(raw, [metric()], task_scope="task-a") == normalize_result(
        raw, [metric()], task_scope="task-a", evidence_policy=None
    )
