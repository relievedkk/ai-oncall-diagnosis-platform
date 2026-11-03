"""Baseline-first diagnosis with one optional, verified evidence refinement."""

import inspect
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.agent.aiops.two_stage import (
    CoordinatorResult,
    FollowupContractError,
    _failure,
    _load_followup,
    _refs,
    _run_blocking,
    _run_diagnosis_turn,
    _StrategyDeadline,
)
from app.core.diagnostic_evidence import (
    DiagnosisEvidencePolicy,
    EvidenceContractError,
    EvidenceRequest,
    InitialEvidenceBundle,
    SerializedEvidenceBundle,
    validate_evidence_request,
)
from app.evaluation.rcaeval_models import EvaluationDiagnosisResult
from app.evaluation.rcaeval_refinement import plan_refinement, refinement_has_novel_observations


class AdaptiveResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    first: CoordinatorResult
    final: CoordinatorResult
    enrichment: CoordinatorResult | None = None
    request: EvidenceRequest | None = None
    refinement_status: Literal[
        "skipped_complete",
        "first_failed",
        "no_query",
        "no_new_evidence",
        "refined",
        "rejected",
        "failed",
    ]
    enrichment_failure_detail_code: str | None = None
    fallback_used: bool = False
    selection_reason: Literal[
        "first_complete",
        "first_failure",
        "evidence_unavailable",
        "refined_diagnosis",
        "new_supported_service",
        "retained_first_service",
        "unsupported_second",
    ]
    added_refs: tuple[str, ...] = ()
    verified_refinement_refs: tuple[str, ...] = ()
    call_count: int = Field(ge=0, le=2)


async def run_adaptive(
    task: dict,
    initial: SerializedEvidenceBundle,
    *,
    model_factory,
    policy,
    followup_loader,
    refinement_verifier=None,
    planner=plan_refinement,
    timeout_seconds: float | None = None,
) -> AdaptiveResult:
    deadline = _StrategyDeadline(timeout_seconds)
    try:
        parsed = InitialEvidenceBundle.model_validate(initial.payload)
    except Exception as exc:
        first = _failure(
            initial,
            reason="invalid_initial_bundle",
            detail_code="initial_bundle_invalid",
            call_count=0,
            error=exc,
            outcome="insufficient_evidence",
        )
        return AdaptiveResult(
            first=first,
            final=first,
            refinement_status="first_failed",
            selection_reason="first_failure",
            call_count=0,
        )
    first = await _run_diagnosis_turn(
        task,
        initial,
        model_factory=model_factory,
        policy=policy,
        timeout_seconds=deadline.remaining(),
        diagnosis_schema=EvaluationDiagnosisResult,
    )

    def retain(
        status,
        *,
        failure=None,
        request=None,
        enrichment=None,
        reason="evidence_unavailable",
        added=(),
        verified=(),
    ):
        return AdaptiveResult(
            first=first,
            final=first,
            enrichment=enrichment,
            request=request,
            refinement_status=status,
            enrichment_failure_detail_code=failure,
            fallback_used=failure is not None,
            selection_reason=reason,
            added_refs=added,
            verified_refinement_refs=verified,
            call_count=first.call_count + (enrichment.call_count if enrichment else 0),
        )

    if first.failure_category:
        return retain("first_failed", reason="first_failure")
    if first.result.diagnosis_complete:
        return retain("skipped_complete", reason="first_complete")
    request = None
    try:
        request = await deadline.wait(_run_blocking(planner, parsed, first.result))
        if request is None:
            return retain("no_query")
        # Never accept an unvalidated model_copy or arbitrary planner object.
        request = EvidenceRequest.model_validate(request.model_dump())
        validate_evidence_request(request, parsed)
        followup = await _load_followup(
            followup_loader, request, initial_payload=initial.payload, deadline=deadline
        )
        added = tuple(sorted(_refs(followup.payload) - _refs(initial.payload)))
        novel = await deadline.wait(
            _run_blocking(refinement_has_novel_observations, initial, followup)
        )
        if not novel:
            return retain("no_new_evidence", request=request)
        resolutions = ()
        verified = ()
        if refinement_verifier is not None:
            try:
                if inspect.iscoroutinefunction(refinement_verifier):
                    resolutions = await deadline.wait(refinement_verifier(request, followup))
                else:
                    resolutions = await deadline.wait(
                        _run_blocking(refinement_verifier, request, followup)
                    )
                verified = added
            except TimeoutError:
                raise
            except Exception:
                return retain("failed", failure="refinement_verification_failed", request=request)
        refined_policy = (
            DiagnosisEvidencePolicy.from_bundles(
                initial, followup, verified_resolutions=resolutions
            )
            if isinstance(policy, DiagnosisEvidencePolicy)
            else policy
        )
        second = await _run_diagnosis_turn(
            task,
            initial,
            model_factory=model_factory,
            policy=refined_policy,
            request=request,
            followup=followup,
            timeout_seconds=deadline.remaining(),
            diagnosis_schema=EvaluationDiagnosisResult,
        )
        if second.failure_category:
            return retain(
                "failed",
                failure=second.failure_detail_code,
                request=request,
                enrichment=second,
                added=added,
                verified=verified,
            )
        deadline.remaining()
        novel_cause = set(second.result.cause_evidence_refs).intersection(verified)
        if second.result.diagnosis_complete and novel_cause:
            reason = "refined_diagnosis"
        elif first.result.root_cause_service:
            return retain(
                "rejected",
                request=request,
                enrichment=second,
                reason="retained_first_service",
                added=added,
                verified=verified,
            )
        elif second.result.root_cause_service and novel_cause:
            reason = "new_supported_service"
        else:
            return retain(
                "rejected",
                request=request,
                enrichment=second,
                reason="unsupported_second",
                added=added,
                verified=verified,
            )
        return AdaptiveResult(
            first=first,
            final=second,
            enrichment=second,
            request=request,
            refinement_status="refined",
            selection_reason=reason,
            added_refs=added,
            verified_refinement_refs=verified,
            call_count=first.call_count + second.call_count,
        )
    except TimeoutError:
        return retain("failed", failure="strategy_timeout", request=request)
    except EvidenceContractError as exc:
        return retain("failed", failure=exc.code, request=request)
    except FollowupContractError as exc:
        return retain("failed", failure=exc.code, request=request)
    except Exception:
        return retain("failed", failure="followup_loader_failed", request=request)
