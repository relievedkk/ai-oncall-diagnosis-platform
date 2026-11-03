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
            data={
                "temporal": {
                    "before_samples": 5,
                    "after_samples": 10,
                    "onset_lower": 119,
                    "onset_upper": 120,
                    "persistence": 1.0,
                }
            },
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


@pytest.mark.asyncio
async def test_deadline_after_second_turn_retains_two_call_audit(monkeypatch):
    import app.agent.aiops.adaptive_diagnosis as adaptive

    class Deadline:
        def __init__(self, _):
            self.checks = 0

        def remaining(self):
            self.checks += 1
            if self.checks == 3:
                raise TimeoutError
            return 3

        async def wait(self, value):
            return await value

    monkeypatch.setattr(adaptive, "_StrategyDeadline", Deadline)
    result, model = await execute([EvaluationDiagnosisResult(), complete()])
    assert len(model.calls) == 2
    assert result.call_count == 2
    assert result.enrichment is not None
    assert result.added_refs == ("ev-detail",)
    assert result.enrichment_failure_detail_code == "strategy_timeout"


@pytest.mark.asyncio
async def test_cancellation_during_verification_propagates():
    started = asyncio.Event()
    cancelled = []

    async def verify(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    task = asyncio.create_task(execute([EvaluationDiagnosisResult()], verifier=verify))
    await asyncio.wait_for(started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("slow_turn", [1, 2])
async def test_policy_timeout_retains_exact_attempt_accounting(slow_turn):
    from app.agent.aiops.adaptive_diagnosis import run_adaptive

    class Policy:
        calls = 0

        async def normalize(self, diagnosis, **_):
            self.calls += 1
            if self.calls == slow_turn:
                await asyncio.sleep(1)
            return diagnosis

    model = FakeModel([EvaluationDiagnosisResult(), complete()])
    result = await run_adaptive(
        {},
        _initial(),
        model_factory=_factory(model),
        policy=Policy(),
        followup_loader=lambda _: followup(),
        refinement_verifier=lambda *_: (),
        planner=lambda *_: request(),
        timeout_seconds=0.08,
    )
    assert result.call_count == slow_turn
    if slow_turn == 1:
        assert result.final.failure_detail_code == "strategy_timeout" and not result.fallback_used
    else:
        assert result.enrichment_failure_detail_code == "strategy_timeout" and result.fallback_used


@pytest.mark.asyncio
async def test_two_turns_share_one_deadline():
    from app.agent.aiops.adaptive_diagnosis import run_adaptive
    from tests.test_two_stage_diagnosis import DelayedModel

    model = DelayedModel([EvaluationDiagnosisResult(), complete()], delay=0.07)
    result = await run_adaptive(
        {},
        _initial(),
        model_factory=_factory(model),
        policy=IdentityPolicy(),
        followup_loader=lambda _: followup(),
        refinement_verifier=lambda *_: (),
        planner=lambda *_: request(),
        timeout_seconds=0.12,
    )
    assert result.call_count == 2
    assert result.enrichment_failure_detail_code == "strategy_timeout"
    assert result.fallback_used


@pytest.mark.asyncio
async def test_invalid_initial_json_stops_before_any_provider_call():
    from app.agent.aiops.adaptive_diagnosis import run_adaptive
    from app.core.diagnostic_evidence import SerializedEvidenceBundle

    model = FakeModel([])
    result = await run_adaptive(
        {},
        SerializedEvidenceBundle("not json", "x" * 64, 8),
        model_factory=_factory(model),
        policy=IdentityPolicy(),
        followup_loader=lambda _: None,
    )
    assert result.call_count == 0 and model.calls == []
    assert result.final.failure_detail_code == "initial_bundle_invalid"


@pytest.mark.asyncio
async def test_no_authorized_query_ends_with_valid_initial_result():
    result, model = await execute(
        [EvaluationDiagnosisResult(summary="no new query")], planner=lambda *_: None
    )
    assert result.refinement_status == "no_query" and result.call_count == 1
    assert result.final.result.summary == "no new query" and len(model.calls) == 1


@pytest.mark.asyncio
async def test_no_verifier_cannot_promote_new_cause():
    from app.agent.aiops.adaptive_diagnosis import run_adaptive

    model = FakeModel([EvaluationDiagnosisResult(), complete()])
    result = await run_adaptive(
        {},
        _initial(),
        model_factory=_factory(model),
        policy=IdentityPolicy(),
        followup_loader=lambda _: followup(),
        planner=lambda *_: request(),
    )
    assert result.final.result.root_cause_service == ""
    assert result.refinement_status == "rejected" and result.call_count == 2


@pytest.mark.asyncio
async def test_sync_verifier_returning_awaitable_is_awaited():
    async def certificate():
        return ()

    result, _ = await execute(
        [EvaluationDiagnosisResult(), complete()], verifier=lambda *_: certificate()
    )
    assert result.refinement_status == "refined"


@pytest.mark.asyncio
async def test_new_supported_service_can_improve_partial_diagnosis():
    partial = EvaluationDiagnosisResult(
        root_cause_service="orders", cause_evidence_refs=["ev-detail"]
    )
    result, _ = await execute([EvaluationDiagnosisResult(), partial])
    assert result.selection_reason == "new_supported_service"
    assert result.final.result.root_cause_service == "orders"


@pytest.mark.asyncio
async def test_wrong_followup_case_preserves_initial_result():
    from app.core.diagnostic_evidence import FollowupEvidenceBundle, serialize_evidence_bundle

    payload = followup().payload
    payload["case_id"] = "other-case"
    wrong = serialize_evidence_bundle(
        FollowupEvidenceBundle.model_validate(payload), max_bytes=16384
    )
    result, _ = await execute([EvaluationDiagnosisResult()], loader=lambda _: wrong)
    assert result.enrichment_failure_detail_code == "followup_request_mismatch"
    assert result.call_count == 1 and result.fallback_used
