"""Versioned adaptive evaluation and model-free development evidence audits."""

import inspect
from collections import Counter

from app.agent.aiops.adaptive_diagnosis import run_adaptive
from app.agent.aiops.two_stage import run_single_stage
from app.core.diagnosis_result import DiagnosisResult
from app.core.diagnostic_evidence import (
    DiagnosisEvidencePolicy,
    InitialEvidenceBundle,
    SerializedEvidenceBundle,
)
from app.evaluation.rcaeval_evidence import build_initial_evidence
from app.evaluation.rcaeval_models import EvaluationDiagnosisResult
from app.evaluation.rcaeval_refinement import (
    build_refinement_evidence,
    plan_refinement,
    verify_refinement_evidence,
)
from app.evaluation.rcaeval_scoring import assess_run, summarize

PROMPT_REVISION = "stage-specific-20261103"
EVIDENCE_REVISION = "temporal-20261103"


async def run_protocol_v4_case(
    view, task, *, model_factory_for, truth_loader, max_bytes=16384, timeout_seconds=None
):
    initial = build_initial_evidence(view, max_bytes=max_bytes)
    parsed = InitialEvidenceBundle.model_validate(initial.payload)
    policy = DiagnosisEvidencePolicy.from_bundles(initial)

    def frozen():
        return SerializedEvidenceBundle(initial.text, initial.sha256, initial.byte_length)

    def loader(request):
        return build_refinement_evidence(view, request, initial=parsed, max_bytes=max_bytes)

    def verifier(request, bundle):
        return verify_refinement_evidence(
            view, request, bundle, initial=parsed, max_bytes=max_bytes
        )

    single = await run_single_stage(
        task,
        frozen(),
        model_factory=model_factory_for("single_stage"),
        policy=policy,
        timeout_seconds=timeout_seconds,
        diagnosis_schema=EvaluationDiagnosisResult,
    )
    adaptive = await run_adaptive(
        task,
        frozen(),
        model_factory=model_factory_for("agent"),
        policy=policy,
        followup_loader=loader,
        refinement_verifier=verifier,
        timeout_seconds=timeout_seconds,
    )
    # No truth is available to the planner, verifier, policy or result selector.
    truth = truth_loader(view.case_id)
    if inspect.isawaitable(truth):
        truth = await truth
    truth = {"root_cause_service": truth["root_cause_service"], "fault": truth["fault"]}
    runs = []
    for strategy, final, adaptive_result in (
        ("single_stage", single, None),
        ("agent", adaptive.final, adaptive),
    ):
        record = {
            "protocol_version": 4,
            "scoring_version": 4,
            "prompt_revision": PROMPT_REVISION,
            "evidence_revision": EVIDENCE_REVISION,
            "case_id": view.case_id,
            "strategy": strategy,
            "prediction": {
                "service": final.result.root_cause_service,
                "fault": final.result.fault_type,
            },
            "diagnosis_result": final.result.model_dump(mode="json"),
            "errors": list(final.errors),
            "failure_category": final.failure_category,
            "failure_detail_code": final.failure_detail_code,
            "schema_issues": [i.model_dump() for i in final.schema_issues],
            "termination_reason": final.termination_reason,
            "initial_hash": initial.sha256,
            "initial_bytes": initial.byte_length,
            "initial_parity": True,
            "initial_candidates": [c.service for c in parsed.candidates],
            "expanded_candidates": sorted(
                set(c.service for c in parsed.candidates)
                | (
                    set(adaptive_result.request.services)
                    if adaptive_result
                    and adaptive_result.verified_refinement_refs
                    and adaptive_result.request
                    else set()
                )
            ),
            "valid_evidence_refs": sorted(policy.valid_refs | set(final.followup_evidence_refs)),
            "usage": {
                "model_calls": adaptive_result.call_count if adaptive_result else single.call_count
            },
            "ground_truth": truth,
            "data_availability": {
                "traces": "present" if view.has_traces else "not_available",
                "logs": "present" if not view.l.empty else "not_available",
            },
            "final_result_source": "refined"
            if adaptive_result and adaptive_result.refinement_status == "refined"
            else "initial",
            "fallback_used": adaptive_result.fallback_used if adaptive_result else False,
            "refinement_status": adaptive_result.refinement_status
            if adaptive_result
            else "not_applicable",
            "evidence_request": adaptive_result.request.model_dump(mode="json")
            if adaptive_result and adaptive_result.request
            else None,
            "enrichment_failure_detail_code": adaptive_result.enrichment_failure_detail_code
            if adaptive_result
            else None,
            "enrichment_schema_issues": [
                i.model_dump() for i in adaptive_result.enrichment.schema_issues
            ]
            if adaptive_result and adaptive_result.enrichment
            else [],
            "selection_reason": adaptive_result.selection_reason
            if adaptive_result
            else "single_stage",
            "added_evidence_refs": list(adaptive_result.added_refs) if adaptive_result else [],
            "verified_refinement_refs": list(adaptive_result.verified_refinement_refs)
            if adaptive_result
            else [],
            "first_diagnosis_result": adaptive_result.first.result.model_dump(mode="json")
            if adaptive_result
            else final.result.model_dump(mode="json"),
        }
        record["scoring_v4"] = {**assess_run(record), "schema_version": 4}
        runs.append(record)
    return {"runs": runs}


def summarize_v4(runs):
    if any(r.get("protocol_version") != 4 for r in runs):
        raise ValueError("Cannot pool protocol versions")
    revisions = {(r.get("prompt_revision"), r.get("evidence_revision")) for r in runs}
    if len(revisions) > 1:
        raise ValueError("Cannot pool prompt/evidence revisions")
    metrics = summarize(runs)
    for strategy, item in metrics.items():
        selected = [r for r in runs if r["strategy"] == strategy]
        item.update(
            scoring_version=4,
            enrichment_failures=sum(
                bool(r.get("enrichment_failure_detail_code")) for r in selected
            ),
            fallback_runs=sum(bool(r.get("fallback_used")) for r in selected),
            refined_runs=sum(r.get("final_result_source") == "refined" for r in selected),
            tokens=sum((r.get("usage") or {}).get("tokens") or 0 for r in selected),
        )
    paired = {
        "new_service_hits": [],
        "lost_service_hits": [],
        "new_joint_hits": [],
        "lost_joint_hits": [],
    }
    indexed = {}
    for run in runs:
        key = (run["case_id"], run["strategy"])
        if key in indexed:
            raise ValueError("Duplicate case strategy record")
        indexed[key] = assess_run(run)
    for case_id in sorted({r["case_id"] for r in runs}):
        baseline, agent = indexed.get((case_id, "single_stage")), indexed.get((case_id, "agent"))
        if baseline is None or agent is None:
            continue
        for metric in ("service", "joint"):
            if agent[metric + "_correct"] and not baseline[metric + "_correct"]:
                paired["new_" + metric + "_hits"].append(case_id)
            if baseline[metric + "_correct"] and not agent[metric + "_correct"]:
                paired["lost_" + metric + "_hits"].append(case_id)
    strata = {}
    for field, get in (
        ("fault", lambda r: r["ground_truth"]["fault"]),
        ("traces", lambda r: r.get("data_availability", {}).get("traces", "unknown")),
    ):
        strata[field] = {
            value: summarize([r for r in runs if get(r) == value])
            for value in sorted({get(r) for r in runs})
        }
    return {"metrics": metrics, "paired": paired, "strata": strata}


def audit_development(pilot, *, max_bytes=16384):
    if not 1 <= len(pilot.ids) <= 18:
        raise ValueError(
            "Development audit accepts only the already-used development set (at most 18)"
        )
    rows = []
    for case_id in pilot.ids:
        view = pilot.open(case_id)
        initial = build_initial_evidence(view, max_bytes=max_bytes)
        parsed = InitialEvidenceBundle.model_validate(initial.payload)
        policy = DiagnosisEvidencePolicy.from_bundles(initial)
        request = plan_refinement(parsed, DiagnosisResult())
        followup = None
        resolutions = ()
        if request:
            followup = build_refinement_evidence(view, request, initial=parsed, max_bytes=max_bytes)
            resolutions = verify_refinement_evidence(
                view, request, followup, initial=parsed, max_bytes=max_bytes
            )
        refined_policy = DiagnosisEvidencePolicy.from_bundles(
            initial, followup, verified_resolutions=resolutions
        )
        truth = pilot.truth(case_id)
        root, fault = truth["root_cause_service"], truth["fault"]
        candidate = next((c for c in parsed.candidates if c.service == root), None)
        profile = candidate.mechanism_profiles.get(fault) if candidate else None
        rows.append(
            {
                "case_id": case_id,
                "fault": fault,
                "initial_candidate_recalled": candidate is not None,
                "truth_mechanism_state": profile.state if profile else "outside_initial_candidates",
                "coverage": profile.coverage.model_dump() if profile and profile.coverage else None,
                "initial_correct_mechanism_eligible": bool(
                    policy.mechanism_refs.get((root, fault))
                ),
                "refined_correct_mechanism_eligible": bool(
                    refined_policy.mechanism_refs.get((root, fault))
                )
                and root not in refined_policy.ambiguous_services,
                "verified_resolutions": len(resolutions),
                "new_mechanism_refs": len(refined_policy.valid_refs - policy.valid_refs),
                "initial_bytes": initial.byte_length,
                "followup_bytes": followup.byte_length if followup else None,
                "traces": "present" if view.has_traces else "not_available",
            }
        )
    return {
        "model_calls": 0,
        "cases": rows,
        "case_count": len(rows),
        "initial_candidate_recall": sum(r["initial_candidate_recalled"] for r in rows),
        "initial_eligible_correct_mechanisms": sum(
            r["initial_correct_mechanism_eligible"] for r in rows
        ),
        "refined_eligible_correct_mechanisms": sum(
            r["refined_correct_mechanism_eligible"] for r in rows
        ),
        "states": dict(Counter(r["truth_mechanism_state"] for r in rows)),
        "limitation": "Development evidence ceiling, not model accuracy; missing causal evidence remains unknown.",
    }
