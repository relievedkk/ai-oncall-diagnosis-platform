"""Deterministic, authorized temporal refinement, verified against raw telemetry."""

import hashlib
import math

from app.core.diagnosis_result import DiagnosisResult
from app.core.diagnostic_evidence import (
    EvidenceFact,
    EvidenceRequest,
    FollowupEvidenceBundle,
    InitialEvidenceBundle,
    SerializedEvidenceBundle,
    canonical_json,
    serialize_evidence_bundle,
    validate_evidence_request,
)
from app.evaluation.rcaeval_evidence import build_followup_evidence


def plan_refinement(
    initial: InitialEvidenceBundle, diagnosis: DiagnosisResult
) -> EvidenceRequest | None:
    if diagnosis.diagnosis_complete:
        return None
    priority = [diagnosis.root_cause_service] + [
        c.service for c in diagnosis.candidate_comparisons if c.status != "rejected"
    ]
    candidates = sorted(
        initial.candidates,
        key=lambda c: (
            priority.index(c.service) if c.service in priority else len(priority),
            c.service,
        ),
    )
    chosen = [
        c
        for c in candidates
        if any(p.strong_change or p.coverage is not None for p in c.mechanism_profiles.values())
    ][:2]
    hinted = None
    if not diagnosis.root_cause_service:
        hinted = next(
            (
                h
                for h in initial.candidate_hints
                if h.evidence_refs and any(s.endswith(":direct_signal") for s in h.visible_signals)
            ),
            None,
        )
        if hinted:
            chosen = chosen[:1]
    mechanisms = sorted(
        {
            m
            for c in chosen
            for m, p in c.mechanism_profiles.items()
            if p.strong_change or p.coverage is not None
        }
    )
    refs = sorted({f.evidence_ref for c in chosen for f in c.facts})[:16]
    if hinted:
        mechanisms = sorted(
            set(mechanisms)
            | {
                signal.split(":", 1)[0]
                for signal in hinted.visible_signals
                if signal.endswith(":direct_signal")
            }
        )
        refs = sorted(set(refs[:12]) | set(hinted.evidence_refs[:4]))
    if not chosen or not mechanisms or not refs:
        return None
    request = EvidenceRequest(
        services=tuple(c.service for c in chosen) + ((hinted.service,) if hinted else ()),
        mechanisms=tuple(mechanisms),
        view="mechanism_details",
        justification_refs=tuple(refs),
    )
    validate_evidence_request(request, initial)
    return request


def _temporal_details(view, metric: str) -> dict:
    frame = view.m[["time", metric]].copy()
    frame[metric] = frame[metric].astype(float)
    frame = frame.loc[frame[metric].map(math.isfinite)].sort_values("time")
    duplicate_times = bool(frame.time.duplicated().any())
    frame = frame.drop_duplicates("time")
    before = frame.loc[frame.time.lt(view.incident), metric]
    after = frame.loc[frame.time.ge(view.incident)]
    summary = {
        "metric": metric,
        "before_samples": len(before),
        "after_samples": len(after),
        "onset_lower": None,
        "onset_upper": None,
        "persistence": 0.0,
        "sampling_gap": duplicate_times,
        "post_mean": (float(after[metric].mean()) if len(after) else None),
    }
    if len(before) < 3 or len(after) < 3:
        return summary
    median = float(before.median())
    threshold = max(6 * float((before - median).abs().median()), abs(median) * 0.5, 1e-9)
    cadence = float(frame.time.diff().dropna().median())
    summary["sampling_gap"] |= bool(after.time.diff().dropna().gt(max(cadence * 3, 1)).any())
    rows = list(after[["time", metric]].itertuples(index=False, name=None))
    abnormal = [abs(value - median) > threshold for _, value in rows]
    for i in range(len(rows) - 2):
        if all(abnormal[i : i + 3]) and rows[i + 2][0] - rows[i][0] <= max(3 * cadence, 1):
            normal_times = frame.loc[
                (frame.time < rows[i][0]) & (frame[metric].sub(median).abs() <= threshold), "time"
            ]
            if normal_times.empty:
                break
            summary.update(
                onset_lower=int(normal_times.max()),
                onset_upper=int(rows[i][0]),
                persistence=sum(abnormal[i:]) / len(abnormal[i:]),
            )
            break
    return summary


def _winner(candidate, details: dict[str, dict]) -> str | None:
    competitors = {
        m
        for m, p in candidate.mechanism_profiles.items()
        if m != "delay" and p.state == "direct_signal"
    }
    if len(competitors) < 2 or not competitors.issubset(details):
        return None
    if any(
        "common-mode" in reason
        for m in competitors
        for reason in candidate.mechanism_profiles[m].reasons
    ):
        return None
    if any(details[m]["onset_lower"] is None or details[m]["sampling_gap"] for m in competitors):
        return None
    for mechanism in sorted(competitors):
        item = details[mechanism]
        if item["persistence"] >= 0.8 and all(
            item["onset_upper"] < details[other]["onset_lower"]
            for other in competitors
            if other != mechanism
        ):
            return mechanism
    return None


def build_refinement_evidence(
    view, request: EvidenceRequest, *, initial: InitialEvidenceBundle, max_bytes: int
) -> SerializedEvidenceBundle:
    validate_evidence_request(request, initial)
    base = FollowupEvidenceBundle.model_validate(
        build_followup_evidence(view, request, initial=initial, max_bytes=max_bytes).payload
    )
    originals = {c.service: c for c in initial.candidates}
    candidates = []
    for candidate in base.candidates:
        details = {
            m: _temporal_details(view, p.strongest_metric)
            for m, p in candidate.mechanism_profiles.items()
            if p.strongest_metric and p.strongest_metric in view.m.columns
        }
        winner = _winner(originals.get(candidate.service, candidate), details)
        profiles = dict(candidate.mechanism_profiles)
        facts = list(candidate.facts)
        for mechanism, detail in sorted(details.items()):
            data = {"temporal": detail, "resolves_competition": mechanism == winner}
            digest = hashlib.sha256(
                canonical_json([view.case_id, candidate.service, mechanism, data]).encode()
            ).hexdigest()[:24]
            ref = "rca-detail-" + digest
            facts.append(
                EvidenceFact(
                    evidence_ref=ref,
                    evidence_type="structured_telemetry",
                    service=candidate.service,
                    mechanism=mechanism,
                    statement="Finite temporal detail; temporal priority alone does not prove causality.",
                    data=data,
                )
            )
            profiles[mechanism] = profiles[mechanism].model_copy(
                update={
                    "evidence_refs": (*profiles[mechanism].evidence_refs, ref),
                    "fault_eligible": (mechanism == winner)
                    if winner
                    else profiles[mechanism].fault_eligible,
                }
            )
        candidates.append(
            candidate.model_copy(
                update={
                    "facts": tuple(facts),
                    "mechanism_profiles": profiles,
                }
            )
        )
    return serialize_evidence_bundle(
        base.model_copy(update={"candidates": tuple(candidates)}), max_bytes=max_bytes
    )


def verify_refinement_evidence(
    view,
    request: EvidenceRequest,
    bundle: SerializedEvidenceBundle,
    *,
    initial: InitialEvidenceBundle,
    max_bytes: int,
) -> tuple:
    """Trusted runner recomputes from raw data; the replaceable loader cannot self-certify."""
    expected = build_refinement_evidence(view, request, initial=initial, max_bytes=max_bytes)
    if expected.sha256 != bundle.sha256 or expected.text != bundle.text:
        raise ValueError("refinement_raw_verification_failed")
    return tuple(
        (c["service"], fact["mechanism"], fact["evidence_ref"])
        for c in expected.payload["candidates"]
        for fact in c["facts"]
        if fact.get("data", {}).get("resolves_competition") is True
    )


def refinement_has_novel_observations(
    initial: SerializedEvidenceBundle, followup: SerializedEvidenceBundle
) -> bool:
    old = {c["service"]: c for c in initial.payload["candidates"]}
    for candidate in followup.payload["candidates"]:
        prior = old.get(candidate["service"])
        if prior is None:
            return bool(candidate["facts"])

        def observations(trends):
            return {
                (t.get("metric"), b.get("start"), b.get("mean"), b.get("samples"))
                for t in trends
                for b in t.get("minute_means", [])
                if isinstance(b.get("mean"), (int, float))
                and math.isfinite(b["mean"])
                and b.get("samples", 0) > 0
            }

        if observations(candidate.get("trends", [])) - observations(prior.get("trends", [])):
            return True
        old_temporal = {canonical_json(f.get("data", {}).get("temporal")) for f in prior["facts"]}
        for fact in candidate["facts"]:
            detail = fact.get("data", {}).get("temporal")
            if (
                detail
                and canonical_json(detail) not in old_temporal
                and (
                    detail.get("before_samples", 0) >= 3
                    and detail.get("after_samples", 0) >= 3
                    and detail.get("onset_lower") is not None
                    and detail.get("onset_upper") is not None
                    and detail.get("persistence", 0) > 0
                )
            ):
                return True
    return False
