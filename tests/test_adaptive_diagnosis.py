import asyncio

import pytest

from app.core.diagnostic_evidence import (
    EvidenceFact,
    EvidenceRequest,
    FollowupEvidenceBundle,
    serialize_evidence_bundle,
)
from app.evaluation.rcaeval_models import EvaluationDiagnosisResult
from tests.test_two_stage_diagnosis import FakeModel, IdentityPolicy, _candidate, _factory, _initial


def request():
    return EvidenceRequest(
        services=("orders",),
        mechanisms=("cpu",),
        view="mechanism_details",
        justification_refs=("ev-orders-cpu",),
    )


def followup(*, novel=True):
    candidate = _candidate()
    if novel:
        fact = EvidenceFact(
            evidence_ref="ev-detail",
            evidence_type="structured_telemetry",
            service="orders",
            mechanism="cpu",
            statement="temporal observation",
            data={"temporal": {"after_samples": 10}},
        )
        profile = candidate.mechanism_profiles["cpu"].model_copy(
            update={
                "evidence_refs": ("ev-orders-cpu", "ev-detail"),
            }
        )
        candidate = candidate.model_copy(
            update={"facts": (*candidate.facts, fact), "mechanism_profiles": {"cpu": profile}}
        )
    return serialize_evidence_bundle(
        FollowupEvidenceBundle(
            case_id="case-coordinator",
            request=request(),
            candidates=(candidate,),
            limitations=("bounded",),
        ),
        max_bytes=16384,
    )


def complete(service="orders", ref="ev-detail"):
    return EvaluationDiagnosisResult(
        outcome="diagnosed",
        root_cause_service=service,
        fault_type="cpu",
        diagnosis_complete=True,
        cause_evidence_refs=[ref],
    )


async def execute(outputs, *, loader=None, verifier=None, timeout=1, planner=None):
    from app.agent.aiops.adaptive_diagnosis import run_adaptive

    model = FakeModel(outputs)
    result = await run_adaptive(
        {},
        _initial(),
        model_factory=_factory(model),
        policy=IdentityPolicy(),
        followup_loader=loader or (lambda _: followup()),
        refinement_verifier=verifier or (lambda *_: ()),
        planner=planner or (lambda *_: request()),
        timeout_seconds=timeout,
    )
    return result, model


@pytest.mark.asyncio
async def test_complete_baseline_ends_after_one_call():
    result, model = await execute([complete(ref="ev-orders-cpu")])
    assert result.call_count == 1 and len(model.calls) == 1
    assert result.refinement_status == "skipped_complete"
    assert result.final.result.root_cause_service == "orders"


@pytest.mark.asyncio
async def test_novel_verified_refinement_can_complete_diagnosis():
    result, model = await execute([EvaluationDiagnosisResult(), complete()])
    assert result.call_count == 2 and len(model.calls) == 2
    assert result.refinement_status == "refined"
    assert result.final.result.diagnosis_complete
    assert result.added_refs == ("ev-detail",)
    assert result.fallback_used is False


@pytest.mark.asyncio
@pytest.mark.parametrize("second", [RuntimeError("provider secret"), {"fault_type": "invented"}])
async def test_second_turn_failure_preserves_valid_first_result(second):
    first = EvaluationDiagnosisResult(summary="validated baseline", root_cause_service="orders")
    result, _ = await execute([first, second])
    assert result.final.result.summary == "validated baseline"
    assert result.fallback_used
    assert result.enrichment_failure_detail_code is not None
    assert result.call_count == 2
    assert "provider secret" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_loader_failure_preserves_baseline_and_records_attempt():
    def fail(_):
        raise RuntimeError("private telemetry")

    result, model = await execute([EvaluationDiagnosisResult(summary="first")], loader=fail)
    assert result.final.result.summary == "first"
    assert result.call_count == 1 and len(model.calls) == 1
    assert result.enrichment_failure_detail_code == "followup_loader_failed"
    assert result.fallback_used and "private telemetry" not in result.model_dump_json()


@pytest.mark.asyncio
async def test_unverified_certificate_cannot_replace_baseline():
    def fail(*_):
        raise ValueError("forged")

    result, model = await execute([EvaluationDiagnosisResult()], verifier=fail)
    assert result.refinement_status == "failed" and len(model.calls) == 1
    assert result.enrichment_failure_detail_code == "refinement_verification_failed"


@pytest.mark.asyncio
async def test_duplicate_followup_skips_second_call():
    result, model = await execute(
        [EvaluationDiagnosisResult()], loader=lambda _: followup(novel=False)
    )
    assert result.refinement_status == "no_new_evidence"
    assert result.call_count == 1 and len(model.calls) == 1


@pytest.mark.asyncio
async def test_partial_second_result_does_not_erase_supported_first_service():
    first = EvaluationDiagnosisResult(root_cause_service="orders", summary="first")
    result, _ = await execute([first, EvaluationDiagnosisResult(summary="second")])
    assert result.final.result.root_cause_service == "orders"
    assert result.selection_reason == "retained_first_service"


@pytest.mark.asyncio
async def test_first_failure_has_no_valid_fallback():
    result, model = await execute([RuntimeError("private")])
    assert result.refinement_status == "first_failed"
    assert result.final.failure_category == "provider_error"
    assert result.fallback_used is False and len(model.calls) == 1


@pytest.mark.asyncio
async def test_refinement_uses_remaining_deadline_and_cancels_async_loader():
    cancelled = []

    async def slow(_):
        try:
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    result, _ = await execute([EvaluationDiagnosisResult()], loader=slow, timeout=0.03)
    assert result.enrichment_failure_detail_code == "strategy_timeout"
    assert result.call_count == 1 and cancelled == [True]


@pytest.mark.asyncio
async def test_unauthorized_query_never_executes_loader():
    loaded = []
    bad = request().model_copy(update={"services": ("outside",)})
    result, _ = await execute(
        [EvaluationDiagnosisResult()], planner=lambda *_: bad, loader=lambda _: loaded.append(True)
    )
    assert loaded == []
    assert result.enrichment_failure_detail_code == "request_service_outside_catalog"
