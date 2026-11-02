import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.diagnosis_result import DiagnosisResult
from app.core.diagnostic_evidence import (
    CandidateEvidence,
    CandidateHint,
    DiagnosisEvidencePolicy,
    EvidenceFact,
    EvidenceRequest,
    FollowupEvidenceBundle,
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


class DelayedModel(FakeModel):
    def __init__(self, outputs, delay):
        super().__init__(outputs)
        self.delay = delay
        self.cancelled = 0

    def with_structured_output(self, schema, **kwargs):
        self.schemas.append((schema, kwargs))
        owner = self

        class Bound:
            async def ainvoke(self, messages):
                owner.calls.append(messages)
                try:
                    await asyncio.sleep(owner.delay)
                except asyncio.CancelledError:
                    owner.cancelled += 1
                    raise
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
        facts=(
            EvidenceFact(
                evidence_ref=f"ev-{service}-cpu",
                evidence_type="structured_telemetry",
                service=service,
                mechanism="cpu",
                statement="structured CPU shifted",
                data={"value": 10},
            ),
        ),
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


def _followup(request=None, *, case_id="case-coordinator", services=None):
    request = request or EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )
    services = services or request.services
    return serialize_evidence_bundle(
        FollowupEvidenceBundle(
            case_id=case_id,
            request=request,
            candidates=tuple(_candidate(service) for service in services),
            limitations=("bounded follow-up",),
        ),
        max_bytes=16_384,
    )


def _tampered_followup(request, mutate):
    payload = _followup(request).payload
    mutate(payload)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    raw = text.encode("utf-8")
    return SerializedEvidenceBundle(
        text=text,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
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
        return _followup(value)

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
    assert result.followup_hash == _followup(request).sha256
    assert "ev-payments-cpu" in result.followup_evidence_refs
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
        followup_loader=lambda value: _followup(value),
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
    assert provider.failure_category == "provider_error"
    assert "secret" not in " ".join(provider.errors)
    assert loader.result.outcome == "dependency_failed"
    assert loader.termination_reason == "followup_loader_error"
    assert loader.failure_category == "followup_loader_error"


@pytest.mark.asyncio
async def test_model_timeout_has_structured_timeout_category():
    from app.agent.aiops.two_stage import run_single_stage

    result = await run_single_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(FakeModel([TimeoutError("provider details")])),
        policy=IdentityPolicy(),
        timeout_seconds=1,
    )

    assert result.call_count == 1
    assert result.termination_reason == "timeout"
    assert result.failure_category == "timeout"
    assert "provider details" not in " ".join(result.errors)


@pytest.mark.asyncio
async def test_strategy_deadline_cancels_a_genuinely_slow_model_call():
    from app.agent.aiops.two_stage import run_single_stage

    model = DelayedModel([_diagnosis()], delay=0.2)
    result = await run_single_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        policy=IdentityPolicy(),
        timeout_seconds=0.02,
    )

    assert result.failure_category == "timeout"
    assert model.cancelled == 1


@pytest.mark.asyncio
async def test_two_stage_uses_one_cumulative_strategy_deadline():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    request = EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )
    model = DelayedModel(
        [
            DiagnosisTurn(action="request_evidence", evidence_request=request),
            _diagnosis("too late"),
        ],
        delay=0.05,
    )

    async def loader(value):
        return _followup(value)

    started = time.perf_counter()
    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=loader,
        policy=IdentityPolicy(),
        timeout_seconds=0.08,
    )

    assert result.failure_category == "timeout"
    assert result.call_count == 2
    assert time.perf_counter() - started < 0.12


@pytest.mark.asyncio
async def test_sync_followup_loader_cannot_block_strategy_timeout():
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
            _diagnosis("must not be reached"),
        ]
    )

    def blocking_loader(value):
        time.sleep(0.2)
        return _followup(value)

    default_executor = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(default_executor)
    try:
        started = time.perf_counter()
        result = await run_two_stage(
            {"task": "diagnose"},
            _initial(),
            model_factory=_factory(model),
            followup_loader=blocking_loader,
            policy=IdentityPolicy(),
            timeout_seconds=0.03,
        )

        assert result.failure_category == "timeout"
        assert result.call_count == 1
        assert time.perf_counter() - started < 0.15
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), timeout=0.05) == "free"
    finally:
        default_executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_sync_policy_cannot_block_strategy_deadline():
    from app.agent.aiops.two_stage import run_single_stage

    class BlockingPolicy:
        def normalize(self, diagnosis, *, initial, followup=None):
            time.sleep(0.2)
            return diagnosis

    started = time.perf_counter()
    result = await run_single_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(FakeModel([_diagnosis()])),
        policy=BlockingPolicy(),
        timeout_seconds=0.03,
    )

    assert result.failure_category == "timeout"
    assert time.perf_counter() - started < 0.15


@pytest.mark.asyncio
@pytest.mark.parametrize("timeout_seconds", [float("nan"), float("inf")])
async def test_non_finite_strategy_timeout_is_rejected(timeout_seconds):
    from app.agent.aiops.two_stage import run_single_stage

    with pytest.raises(ValueError, match="positive finite"):
        await run_single_stage(
            {"task": "diagnose"},
            _initial(),
            model_factory=_factory(FakeModel([_diagnosis()])),
            policy=IdentityPolicy(),
            timeout_seconds=timeout_seconds,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidity", ["case_id", "request", "services"])
async def test_agent_rejects_followup_bundle_outside_authorized_request(invalidity):
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    request = EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )
    other_request = request.model_copy(update={"view": "logs"})

    def loader(_request):
        if invalidity == "case_id":
            return _followup(request, case_id="case-other")
        if invalidity == "request":
            return _followup(other_request)
        return _followup(request, services=("orders",))

    model = FakeModel(
        [
            DiagnosisTurn(action="request_evidence", evidence_request=request),
            _diagnosis("must not be reached"),
        ]
    )
    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=loader,
        policy=IdentityPolicy(),
    )

    assert result.call_count == 1
    assert result.termination_reason == "followup_loader_error"
    assert len(model.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalidity",
    [
        "profile_key",
        "blank_fact_service",
        "mechanismless_fact",
        "topology_fact",
        "topology_metadata",
        "unbound_profile_ref",
        "fact_without_profile",
        "duplicate_ref",
    ],
)
async def test_agent_rejects_internally_inconsistent_followup_evidence(invalidity):
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    request = EvidenceRequest(
        services=("payments",),
        mechanisms=("cpu",),
        view="metric_trends",
        justification_refs=("ev-payments-cpu",),
    )

    def mutate(payload):
        candidate = payload["candidates"][0]
        fact = candidate["facts"][0]
        if invalidity == "profile_key":
            candidate["mechanism_profiles"]["cpu"]["mechanism"] = "mem"
        elif invalidity == "blank_fact_service":
            fact["service"] = ""
        elif invalidity == "mechanismless_fact":
            fact["mechanism"] = None
        elif invalidity == "topology_fact":
            candidate["facts"].append(
                {
                    "evidence_ref": "topology-ref",
                    "evidence_type": "topology_edge",
                    "service": "payments",
                    "mechanism": None,
                    "statement": "payments -> orders",
                    "data": {"caller": "payments", "callee": "orders"},
                }
            )
        elif invalidity == "topology_metadata":
            candidate["outbound_dependencies"] = ["orders"]
        elif invalidity == "fact_without_profile":
            candidate["mechanism_profiles"] = {}
        elif invalidity == "duplicate_ref":
            candidate["facts"].append(dict(fact))
        else:
            candidate["mechanism_profiles"]["cpu"]["evidence_refs"].append("unbound-ref")

    model = FakeModel(
        [
            DiagnosisTurn(action="request_evidence", evidence_request=request),
            _diagnosis("must not be reached"),
        ]
    )
    result = await run_two_stage(
        {"task": "diagnose"},
        _initial(),
        model_factory=_factory(model),
        followup_loader=lambda _request: _tampered_followup(request, mutate),
        policy=IdentityPolicy(),
    )

    assert result.call_count == 1
    assert result.termination_reason == "followup_loader_error"
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_coordinator_applies_bundle_policy():
    from app.agent.aiops.two_stage import DiagnosisTurn, run_two_stage

    initial = _initial()
    diagnosis = DiagnosisResult(
        outcome="diagnosed",
        summary="orders CPU is directly supported",
        root_cause_service="orders",
        fault_type="cpu",
        cause_evidence_refs=["ev-orders-cpu"],
        observations=[{"text": "CPU shifted", "evidence_refs": ["ev-orders-cpu"]}],
        hypotheses=[
            {
                "description": "orders CPU saturation",
                "service": "orders",
                "fault_type": "cpu",
                "supporting_refs": ["ev-orders-cpu"],
                "status": "supported",
            }
        ],
    )
    result = await run_two_stage(
        {"task": "diagnose"},
        initial,
        model_factory=_factory(
            FakeModel([DiagnosisTurn(action="diagnose", diagnosis_result=diagnosis)])
        ),
        followup_loader=lambda _request: _followup(),
        policy=DiagnosisEvidencePolicy.from_bundles(initial),
    )

    assert result.result.validation_issues == []
    assert result.result.outcome == "diagnosed"
    assert result.result.root_cause_service == "orders"
    assert result.result.fault_type == "cpu"
