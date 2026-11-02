import hashlib

import pytest

from app.core.diagnosis_result import DiagnosisResult
from app.core.diagnostic_evidence import (
    CandidateEvidence,
    CandidateHint,
    EvidenceRequest,
    InitialEvidenceBundle,
    MechanismProfile,
    SerializedEvidenceBundle,
    serialize_evidence_bundle,
)


class FakeModel:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.schemas = []

    def with_structured_output(self, schema, **kwargs):
        self.schemas.append((schema, kwargs))
        owner = self

        class Bound:
            async def ainvoke(self, messages):
                owner.calls.append(messages)
                output = owner.outputs.pop(0)
                if isinstance(output, BaseException):
                    raise output
                return output

        return Bound()


class IdentityPolicy:
    def __init__(self, replacement=None):
        self.calls = []
        self.replacement = replacement

    def normalize(self, diagnosis, *, initial, followup=None):
        self.calls.append((diagnosis, initial, followup))
        return self.replacement or DiagnosisResult.model_validate(diagnosis)


def _candidate(service="orders"):
    profile = MechanismProfile(
        mechanism="cpu",
        state="direct_signal",
        evidence_refs=(f"ev-{service}-cpu",),
        first_anomalous_time=120,
        reasons=("structured CPU shifted",),
        available=True,
        strong_change=True,
        strongest_metric=f"{service}_cpu",
        ratio=10,
        interpretation="direct_resource_signal",
        fault_eligible=True,
    )
    return CandidateEvidence(
        service=service,
        selection_reasons=("direct:cpu",),
        mechanism_profiles={"cpu": profile},
    )


def _initial():
    bundle = InitialEvidenceBundle(
        case_id="case-coordinator",
        services=("catalog-only", "orders", "payments"),
        time_start=0,
        time_end=300,
        incident_time=120,
        candidates=(_candidate(),),
        candidate_hints=(
            CandidateHint(
                service="payments",
                visible_signals=("cpu:direct_signal",),
                evidence_refs=("ev-payments-cpu",),
            ),
        ),
        limitations=("evaluation evidence only",),
    )
    return serialize_evidence_bundle(bundle, max_bytes=16_384)


def _followup():
    text = '{"schema_version":3,"case_id":"case-coordinator","followup":true}'
    return SerializedEvidenceBundle(
        text=text,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
        byte_length=len(text.encode()),
    )


def _diagnosis(summary="done"):
    return DiagnosisResult(outcome="insufficient_evidence", summary=summary)


def _factory(model):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return model

    create.calls = calls
    return create


@pytest.mark.asyncio
async def test_single_stage_uses_exact_initial_payload_once():
    from app.agent.aiops.two_stage import run_single_stage

    initial = _initial()
    model = FakeModel([_diagnosis()])
    policy = IdentityPolicy()

    result = await run_single_stage(
        {"task": "diagnose"},
        initial,
        model_factory=_factory(model),
        policy=policy,
    )

    assert result.call_count == 1
    assert result.initial_hash == initial.sha256
    assert len(model.calls) == 1
    combined = "\n".join(message.content for message in model.calls[0])
    assert combined.count(initial.text) == 1
    assert model.schemas[0][0] is DiagnosisResult
    assert result.result.summary == "done"


@pytest.mark.asyncio
async def test_agent_direct_diagnosis_uses_one_call():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    initial = _initial()
    turn = DiagnosisTurn(action="diagnose", diagnosis_result=_diagnosis("direct"))
    model = FakeModel([turn])
    loader_calls = []

    async def loader(request):
        loader_calls.append(request)
        return _followup()

    result = await run_two_stage(
        {"task": "diagnose"},
        initial,
        model_factory=_factory(model),
        followup_loader=loader,
        policy=IdentityPolicy(),
    )

    assert result.call_count == 1
    assert result.result.summary == "direct"
    assert result.termination_reason == "direct_diagnosis"
    assert loader_calls == []


@pytest.mark.asyncio
async def test_agent_valid_followup_uses_exactly_two_calls():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    initial = _initial()
    request = EvidenceRequest(
        services=("orders", "payments"),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )
    model = FakeModel(
        [
            DiagnosisTurn(action="request_evidence", evidence_request=request),
            _diagnosis("after followup"),
        ]
    )
    loader_calls = []

    async def loader(value):
        loader_calls.append(value)
        return _followup()

    result = await run_two_stage(
        {"task": "diagnose"},
        initial,
        model_factory=_factory(model),
        followup_loader=loader,
        policy=IdentityPolicy(),
    )

    assert result.call_count == 2
    assert len(model.calls) == 2
    assert [schema[0] for schema in model.schemas] == [DiagnosisTurn, DiagnosisResult]
    assert loader_calls == [request]
    assert result.request == request
    assert result.followup_hash == _followup().sha256
    assert result.result.summary == "after followup"


@pytest.mark.asyncio
async def test_agent_cannot_request_second_followup():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    request = EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )
    model = FakeModel(
        [
            DiagnosisTurn(action="request_evidence", evidence_request=request),
            DiagnosisTurn(action="request_evidence", evidence_request=request),
        ]
    )

    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=lambda _request: _followup(),
        policy=IdentityPolicy(),
    )

    assert result.call_count == 2
    assert result.result.outcome == "insufficient_evidence"
    assert result.termination_reason == "invalid_second_turn"


@pytest.mark.asyncio
@pytest.mark.parametrize("service", ["outside-catalog", "catalog-only"])
async def test_out_of_catalog_or_unhinted_request_is_rejected_without_loader_call(service):
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    request = EvidenceRequest(
        services=(service,),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )
    model = FakeModel([DiagnosisTurn(action="request_evidence", evidence_request=request)])
    loader_calls = []

    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=lambda value: loader_calls.append(value),
        policy=IdentityPolicy(),
    )

    assert result.call_count == 1
    assert result.termination_reason == "invalid_evidence_request"
    assert loader_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "services",
    [
        ["orders", "orders"],
        ["orders", "payments", "catalog-only"],
    ],
)
async def test_duplicate_service_and_oversized_request_are_rejected(services):
    from app.agent.aiops.two_stage import run_two_stage

    raw_turn = {
        "action": "request_evidence",
        "diagnosis_result": None,
        "evidence_request": {
            "services": services,
            "mechanisms": ["cpu"],
            "view": "metric_trends",
            "justification_refs": ["ev-payments-cpu"],
        },
    }
    model = FakeModel([raw_turn])

    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=lambda _request: _followup(),
        policy=IdentityPolicy(),
    )

    assert result.call_count == 1
    assert result.termination_reason == "invalid_evidence_request"


@pytest.mark.asyncio
async def test_failed_direct_diagnosis_does_not_auto_retry():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    model = FakeModel(
        [DiagnosisTurn(action="diagnose", diagnosis_result=_diagnosis("model conclusion"))]
    )
    rejected = DiagnosisResult(outcome="insufficient_evidence", summary="policy rejected")

    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=lambda _request: _followup(),
        policy=IdentityPolicy(replacement=rejected),
    )

    assert result.call_count == 1
    assert result.result.summary == "policy rejected"
    assert result.termination_reason == "direct_diagnosis"


@pytest.mark.asyncio
async def test_provider_or_loader_failure_has_distinct_outcome():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_single_stage, run_two_stage

    provider = await run_single_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(FakeModel([RuntimeError("provider token=secret")])),
        policy=IdentityPolicy(),
    )
    request = EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )

    async def broken_loader(_request):
        raise OSError("trace store unavailable")

    loader = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(
            FakeModel([DiagnosisTurn(action="request_evidence", evidence_request=request)])
        ),
        followup_loader=broken_loader,
        policy=IdentityPolicy(),
    )

    assert provider.result.outcome == "dependency_failed"
    assert provider.termination_reason == "provider_error"
    assert "secret" not in " ".join(provider.errors)
    assert loader.result.outcome == "dependency_failed"
    assert loader.termination_reason == "followup_loader_error"
