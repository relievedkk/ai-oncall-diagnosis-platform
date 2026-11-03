"""Label-free mechanism evidence for RCAEval recordings."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass

from app.core.data_safety import redact_sensitive
from app.core.diagnostic_evidence import (
    CandidateEvidence,
    CandidateHint,
    EvidenceBudgetError,
    EvidenceFact,
    EvidenceRequest,
    FaultMechanism,
    FollowupEvidenceBundle,
    InitialEvidenceBundle,
    MechanismProfile,
    SerializedEvidenceBundle,
    TelemetryCoverage,
    serialize_evidence_bundle,
    validate_evidence_request,
)

_PREFIXES: dict[FaultMechanism, tuple[str, ...]] = {
    "cpu": ("cpu",),
    "mem": ("mem", "memory"),
    "delay": ("latency", "delay"),
    "disk": ("diskio", "disk_io", "disk"),
    "socket": ("socket",),
    "loss": (
        "packet_loss",
        "packetloss",
        "network_loss",
        "drop_rate",
        "packet_drop",
        "retransmit",
        "network_error",
    ),
}
_RESOURCE_MECHANISMS: tuple[FaultMechanism, ...] = (
    "cpu",
    "mem",
    "disk",
    "socket",
    "loss",
)


def _matches_mechanism(metric_suffix: str, prefixes: Iterable[str]) -> bool:
    normalized = metric_suffix.casefold().replace("-", "_")
    return any(normalized.startswith(prefix) for prefix in prefixes)


def _evidence_ref(case_id: str, service: str, mechanism: str, metric: str) -> str:
    digest = hashlib.sha256(f"{case_id}\0{service}\0{mechanism}\0{metric}".encode()).hexdigest()[
        :20
    ]
    return f"rca-{mechanism}-{digest}"


def _summarize_signal(metric: str, before, after_frame, value_column: str) -> dict | None:
    try:
        before = before.astype(float).dropna()
        before = before[before.map(math.isfinite)]
        after_frame = after_frame.copy()
        after_frame[value_column] = after_frame[value_column].astype(float)
        after_frame = after_frame.dropna(subset=[value_column])
        after_frame = after_frame.loc[after_frame[value_column].map(math.isfinite)]
    except (TypeError, ValueError):
        return None
    if before.empty and after_frame.empty:
        return None
    if before.empty or after_frame.empty:
        values = after_frame[value_column]
        split = max(1, len(values) // 3)
        return {
            "metric": metric,
            "ratio": None,
            "first_anomalous_time": None,
            "post_start_mean": float(values.iloc[:split].mean()) if len(values) else None,
            "post_end_mean": float(values.iloc[-split:].mean()) if len(values) else None,
            "post_pattern": "partial_observation",
            "strong_change": False,
            "strength": 0.0,
            "coverage": {
                "status": "baseline_missing" if before.empty else "post_missing",
                "before_samples": len(before),
                "after_samples": len(values),
            },
        }
    pre = float(before.mean())
    post = float(after_frame[value_column].mean())
    median = float(before.median())
    mad = float((before - median).abs().median())
    threshold = max(6 * mad, abs(median) * 0.5, 1e-9)
    ratio = post / pre if pre else None
    if ratio is not None and (not math.isfinite(ratio) or ratio <= 0):
        ratio = None
    deviation = abs(post - median) / threshold
    log_change = abs(math.log(ratio)) if ratio else 0.0
    strength = max(log_change, math.log1p(deviation))
    first_anomaly = next(
        (
            int(timestamp)
            for timestamp, value in after_frame[[after_frame.columns[0], value_column]].itertuples(
                index=False, name=None
            )
            if abs(float(value) - median) > threshold
        ),
        None,
    )
    values = after_frame[value_column].astype(float)
    split = max(1, len(values) // 3)
    early = float(values.iloc[:split].mean())
    late = float(values.iloc[-split:].mean())
    strong = bool(first_anomaly is not None and (log_change >= math.log(2) or deviation > 2))
    if not strong:
        pattern = "stable"
    elif post < pre:
        pattern = "decrease"
    elif len(values) >= 3 and late > max(abs(early) * 1.5, early + threshold):
        pattern = "growth"
    else:
        pattern = "sustained_shift"
    return {
        "metric": metric,
        "ratio": ratio,
        "first_anomalous_time": first_anomaly,
        "post_start_mean": early,
        "post_end_mean": late,
        "post_pattern": pattern,
        "strong_change": strong,
        "strength": strength,
    }


def _structured_trace_signals(view, service_name: str, mechanism: FaultMechanism) -> list[dict]:
    if mechanism != "loss" or view.t.empty:
        return []
    required = {"_epoch", "serviceName"}
    if not required.issubset(view.t.columns):
        return []
    frame = view.t.loc[view.t.serviceName.astype(str).eq(service_name)]
    signals = []
    for column in frame.columns:
        if column in required or not _matches_mechanism(column, _PREFIXES["loss"]):
            continue
        before = frame.loc[frame._epoch.lt(view.incident), column]
        after = frame.loc[frame._epoch.ge(view.incident), ["_epoch", column]]
        signal = _summarize_signal(f"trace.{column}", before, after, column)
        if signal is not None:
            signals.append(signal)
    return signals


def _profile_from_metrics(view, service_name: str, mechanism: FaultMechanism) -> MechanismProfile:
    prefixes = _PREFIXES[mechanism]
    columns = [
        column
        for column in view.m.columns
        if column.startswith(service_name + "_")
        and _matches_mechanism(column[len(service_name) + 1 :], prefixes)
    ]
    signals = []
    for column in columns:
        before = view.m.loc[view.m.time.lt(view.incident), column].dropna()
        after_frame = view.m.loc[view.m.time.ge(view.incident), ["time", column]].dropna()
        signal = _summarize_signal(column, before, after_frame, column)
        if signal is not None:
            signals.append(signal)
    signals.extend(_structured_trace_signals(view, service_name, mechanism))
    signals.sort(key=lambda item: (-item["strength"], item["metric"]))
    strongest = signals[0] if signals else None
    available = bool(strongest)
    strong_change = bool(strongest and strongest["strong_change"])
    state = "direct_signal" if strong_change else ("ambiguous" if available else "unavailable")
    metric = strongest["metric"] if strongest else ""
    evidence_refs = (
        (_evidence_ref(view.case_id, service_name, mechanism, metric),) if strong_change else ()
    )
    coverage = (
        strongest.get("coverage")
        if strongest
        else (
            {"status": "no_finite_samples", "before_samples": 0, "after_samples": 0}
            if columns
            else None
        )
    )
    if coverage:
        interpretation = coverage["status"]
        reasons = (
            "Partial telemetry: "
            + coverage["status"]
            + "; observed values do not establish an incident change.",
        )
    elif not available:
        interpretation = "unavailable"
        reasons = ("No structured telemetry is available for this mechanism.",)
    elif not strong_change:
        interpretation = "no_strong_change"
        reasons = ("Structured telemetry is available but has no strong incident-time change.",)
    else:
        interpretation = (
            "direct_resource_signal" if mechanism != "delay" else "possible_direct_delay"
        )
        reasons = ("A structured mechanism-specific signal changed strongly after the incident.",)
    return MechanismProfile(
        mechanism=mechanism,
        state=state,
        evidence_refs=evidence_refs,
        first_anomalous_time=strongest["first_anomalous_time"] if strongest else None,
        reasons=reasons,
        available=available,
        strong_change=strong_change,
        strongest_metric=metric,
        ratio=strongest["ratio"] if strongest else None,
        post_start_mean=strongest["post_start_mean"] if strongest else None,
        post_end_mean=strongest["post_end_mean"] if strongest else None,
        post_pattern=strongest["post_pattern"] if strongest else "unavailable",
        interpretation=interpretation,
        coverage=TelemetryCoverage.model_validate(coverage) if coverage else None,
    )


def classify_competing_mechanisms(
    profiles: Mapping[str, MechanismProfile],
    *,
    common_mode: Set[str],
    bucket_seconds: int = 60,
    delay_precedes_callers: bool = False,
    downstream_direct_signal: bool = False,
) -> dict[str, MechanismProfile]:
    """Separate observed direct facts from uniquely fault-eligible mechanisms."""
    if (
        isinstance(bucket_seconds, bool)
        or not isinstance(bucket_seconds, int)
        or bucket_seconds <= 0
    ):
        raise ValueError("bucket_seconds must be a positive integer")
    result = {name: profile.model_copy(deep=True) for name, profile in profiles.items()}
    direct_resources = [
        name
        for name in _RESOURCE_MECHANISMS
        if name in result and result[name].state == "direct_signal"
    ]
    candidate_specific = [name for name in direct_resources if name not in common_mode]
    eligible: set[str] = set()
    if len(candidate_specific) == 1:
        eligible.add(candidate_specific[0])
    elif len(candidate_specific) > 1:
        ordered = sorted(
            candidate_specific,
            key=lambda name: (
                result[name].first_anomalous_time
                if result[name].first_anomalous_time is not None
                else math.inf,
                name,
            ),
        )
        first, second = result[ordered[0]], result[ordered[1]]
        if (
            first.first_anomalous_time is not None
            and second.first_anomalous_time is not None
            and second.first_anomalous_time - first.first_anomalous_time >= bucket_seconds
            and first.post_pattern in {"sustained_shift", "growth"}
        ):
            eligible.add(ordered[0])

    for name in direct_resources:
        competitors = tuple(sorted(other for other in direct_resources if other != name))
        reasons = list(result[name].reasons)
        if name in common_mode:
            reasons.append(
                "The signal is common-mode across peer services and is not root-specific."
            )
        elif name not in eligible and len(candidate_specific) > 1:
            reasons.append("Competing direct mechanisms are not temporally distinguishable.")
        result[name] = result[name].model_copy(
            update={
                "fault_eligible": name in eligible,
                "competing_mechanisms": competitors,
                "reasons": tuple(reasons),
            }
        )

    delay = result.get("delay")
    if delay and delay.strong_change:
        if direct_resources:
            delay_supported = (
                set(direct_resources).issubset(common_mode)
                and delay_precedes_callers
                and not downstream_direct_signal
            )
            result["delay"] = delay.model_copy(
                update={
                    "state": "direct_signal" if delay_supported else "possible_symptom",
                    "fault_eligible": delay_supported,
                    "interpretation": (
                        "possible_direct_delay"
                        if delay_supported
                        else "may_be_symptom_with_resource_anomaly"
                    ),
                    "competing_mechanisms": tuple(sorted(direct_resources)),
                    "reasons": delay.reasons
                    + (
                        (
                            "Resource signals were rebutted as common-mode and topology supports direct delay.",
                        )
                        if delay_supported
                        else (
                            "A direct resource signal can explain the observed latency as a symptom.",
                        )
                    ),
                }
            )
        else:
            result["delay"] = delay.model_copy(update={"fault_eligible": True})
    return result


def _raw_mechanism_profiles(view, service_name: str) -> dict[str, MechanismProfile]:
    view._window(service_name, None, None, 20)
    return {
        mechanism: _profile_from_metrics(view, service_name, mechanism) for mechanism in _PREFIXES
    }


def build_mechanism_profiles(view, service_name: str) -> dict[str, MechanismProfile]:
    """Build six label-free profiles from structured recorded telemetry only."""
    return classify_competing_mechanisms(
        _raw_mechanism_profiles(view, service_name), common_mode=set()
    )


@dataclass(frozen=True)
class CandidateSelection:
    candidates: tuple[str, ...]
    hints: tuple[CandidateHint, ...]
    reasons: dict[str, tuple[str, ...]]


def _signal_score(profile: MechanismProfile) -> float:
    if profile.ratio is not None and profile.ratio > 0:
        return abs(math.log(profile.ratio))
    values = [
        abs(value)
        for value in (profile.post_start_mean, profile.post_end_mean)
        if value is not None and math.isfinite(value)
    ]
    return max(values, default=0.0)


def _common_mode_mechanisms(
    profiles_by_service: Mapping[str, Mapping[str, MechanismProfile]],
    *,
    bucket_seconds: int = 60,
) -> set[str]:
    common = set()
    for mechanism in _RESOURCE_MECHANISMS:
        observed = [
            profiles[mechanism]
            for profiles in profiles_by_service.values()
            if profiles[mechanism].state == "direct_signal"
        ]
        onset = [
            profile.first_anomalous_time
            for profile in observed
            if profile.first_anomalous_time is not None
        ]
        if (
            len(observed) >= 3
            and len(observed) * 2 > len(profiles_by_service)
            and len(onset) == len(observed)
            and max(onset) - min(onset) <= bucket_seconds
        ):
            common.add(mechanism)
    return common


def _topology_context(view) -> tuple[dict, dict[str, set[str]], dict[str, set[str]]]:
    topology = view.call_edges(limit=50)
    inbound = {service: set() for service in view.services}
    outbound = {service: set() for service in view.services}
    if topology.get("available"):
        for edge in topology.get("edges", []):
            caller = edge["caller"]
            callee = edge["callee"]
            if caller in outbound and callee in inbound:
                outbound[caller].add(callee)
                inbound[callee].add(caller)
    return topology, inbound, outbound


def _catalog_profiles(view) -> tuple[dict[str, dict[str, MechanismProfile]], set[str]]:
    raw = {service: _raw_mechanism_profiles(view, service) for service in view.services}
    common = _common_mode_mechanisms(raw)
    classified = {
        service: classify_competing_mechanisms(profiles, common_mode=common)
        for service, profiles in raw.items()
    }
    return classified, common


def _topology_ref(case_id: str, caller: str, callee: str) -> str:
    digest = hashlib.sha256(f"{case_id}\0{caller}\0{callee}".encode()).hexdigest()[:20]
    return f"rca-topology-{digest}"


def _hint_for_service(
    view,
    service: str,
    profiles: Mapping[str, MechanismProfile],
    inbound: Mapping[str, set[str]],
    outbound: Mapping[str, set[str]],
    common_mode: set[str],
) -> CandidateHint | None:
    visible = []
    refs = set()
    for mechanism, profile in profiles.items():
        if not profile.strong_change:
            continue
        suffix = ":common_mode" if mechanism in common_mode else f":{profile.state}"
        visible.append(mechanism + suffix)
        refs.update(profile.evidence_refs)
    neighbors = sorted(inbound[service] | outbound[service])
    for caller in inbound[service]:
        refs.add(_topology_ref(view.case_id, caller, service))
    for callee in outbound[service]:
        refs.add(_topology_ref(view.case_id, service, callee))
    if not visible and not neighbors:
        return None
    return CandidateHint(
        service=service,
        visible_signals=tuple(sorted(visible)),
        topology_neighbors=tuple(neighbors),
        evidence_refs=tuple(sorted(refs)),
    )


def select_candidate_services(
    view,
    *,
    max_candidates: int = 5,
    _catalog=None,
    _topology=None,
) -> CandidateSelection:
    """Select a bounded, mechanism-diverse candidate pool without answer labels."""
    if isinstance(max_candidates, bool) or not isinstance(max_candidates, int):
        raise ValueError("max_candidates must be an integer")
    if not 1 <= max_candidates <= 5:
        raise ValueError("max_candidates must be 1..5")
    profiles_by_service, common_mode = _catalog or _catalog_profiles(view)
    topology, inbound, outbound = _topology or _topology_context(view)
    selected: set[str] = set()
    reasons: dict[str, set[str]] = {}

    def choose(service: str, reason: str) -> None:
        if len(selected) >= max_candidates and service not in selected:
            return
        selected.add(service)
        reasons.setdefault(service, set()).add(reason)

    for mechanism in _RESOURCE_MECHANISMS:
        if mechanism in common_mode:
            continue
        eligible = [
            service
            for service, profiles in profiles_by_service.items()
            if profiles[mechanism].state == "direct_signal"
        ]
        if eligible:
            strongest = max(
                eligible,
                key=lambda service: (
                    _signal_score(profiles_by_service[service][mechanism]),
                    service,
                ),
            )
            choose(strongest, f"direct:{mechanism}")

    delay_only = []
    for service, profiles in profiles_by_service.items():
        delay = profiles["delay"]
        unique_resource = any(
            profiles[mechanism].state == "direct_signal" and mechanism not in common_mode
            for mechanism in _RESOURCE_MECHANISMS
        )
        if delay.strong_change and not unique_resource:
            delay_only.append(service)
    if delay_only and len(selected) < max_candidates:
        strongest = max(
            delay_only,
            key=lambda service: (_signal_score(profiles_by_service[service]["delay"]), service),
        )
        choose(strongest, "delay-symptom-candidate")

    for callee in sorted(view.services):
        if len(selected) >= max_candidates:
            break
        has_direct = any(
            profiles_by_service[callee][mechanism].state == "direct_signal"
            and mechanism not in common_mode
            for mechanism in _RESOURCE_MECHANISMS
        )
        abnormal_callers = [
            caller
            for caller in inbound[callee]
            if profiles_by_service[caller]["delay"].strong_change
        ]
        if has_direct and abnormal_callers:
            choose(callee, "downstream-direct-evidence")

    ranked_fallbacks = sorted(
        view.services,
        key=lambda service: (
            -max(
                (
                    _signal_score(profile)
                    for profile in profiles_by_service[service].values()
                    if profile.strong_change
                ),
                default=0.0,
            ),
            service,
        ),
    )
    for service in ranked_fallbacks:
        if len(selected) >= max_candidates:
            break
        if any(profile.strong_change for profile in profiles_by_service[service].values()):
            choose(service, "bounded-anomaly-fallback")
    if not selected and view.services:
        choose(sorted(view.services)[0], "catalog-fallback-no-strong-signal")

    hints = []
    for service in sorted(set(view.services) - selected):
        hint = _hint_for_service(
            view,
            service,
            profiles_by_service[service],
            inbound,
            outbound,
            common_mode,
        )
        if hint is not None:
            hints.append(hint)
    hints.sort(
        key=lambda hint: (
            all(signal.endswith(":common_mode") for signal in hint.visible_signals),
            -len(hint.visible_signals),
            -len(hint.evidence_refs),
            hint.service,
        )
    )
    return CandidateSelection(
        candidates=tuple(sorted(selected)),
        hints=tuple(hints),
        reasons={service: tuple(sorted(values)) for service, values in sorted(reasons.items())},
    )


def _candidate_evidence(
    view,
    service: str,
    *,
    reasons: tuple[str, ...],
    profiles: Mapping[str, MechanismProfile],
    inbound: Mapping[str, set[str]],
    outbound: Mapping[str, set[str]],
    include: str = "all",
    mechanisms: tuple[FaultMechanism, ...] | None = None,
) -> CandidateEvidence:
    profiles = {
        mechanism: profile
        for mechanism, profile in profiles.items()
        if mechanisms is None or mechanism in mechanisms
    }
    include_topology = include in {"all", "topology", "traces"}
    facts = []
    for mechanism, profile in sorted(profiles.items()):
        if not profile.strong_change:
            continue
        for evidence_ref in profile.evidence_refs:
            facts.append(
                EvidenceFact(
                    evidence_ref=evidence_ref,
                    evidence_type="structured_telemetry",
                    service=service,
                    mechanism=mechanism,
                    statement=(
                        f"{profile.strongest_metric} changed after the incident; "
                        f"state={profile.state}, interpretation={profile.interpretation}."
                    ),
                    data={
                        "metric": profile.strongest_metric,
                        "ratio": profile.ratio,
                        "first_anomalous_time": profile.first_anomalous_time,
                        "post_pattern": profile.post_pattern,
                    },
                )
            )
    if include_topology:
        for caller in sorted(inbound[service]):
            facts.append(
                EvidenceFact(
                    evidence_ref=_topology_ref(view.case_id, caller, service),
                    evidence_type="topology_edge",
                    service=service,
                    statement=f"Observed call direction: {caller} -> {service} (caller -> callee).",
                    data={"caller": caller, "callee": service},
                )
            )
        for callee in sorted(outbound[service]):
            facts.append(
                EvidenceFact(
                    evidence_ref=_topology_ref(view.case_id, service, callee),
                    evidence_type="topology_edge",
                    service=service,
                    statement=f"Observed call direction: {service} -> {callee} (caller -> callee).",
                    data={"caller": service, "callee": callee},
                )
            )

    trends = (
        tuple(view.metric_trends(service, limit=6, mechanisms=mechanisms))
        if include in {"all", "metric_trends", "mechanism_details"}
        else ()
    )
    trace_data = view.traces(service, limit=6)
    traces = (
        tuple(trace_data.get("data_points", [])) if include in {"all", "traces", "topology"} else ()
    )
    log_records = view.logs(service, limit=3).get("records", [])
    logs = (
        tuple(
            str(
                redact_sensitive(
                    f"{record['timestamp']} {record['container_name']}: {record['message']}"
                )
            )
            for record in log_records
        )
        if include in {"all", "logs"}
        else ()
    )
    return CandidateEvidence(
        service=service,
        selection_reasons=reasons,
        inbound_callers=tuple(sorted(inbound[service])) if include_topology else (),
        outbound_dependencies=tuple(sorted(outbound[service])) if include_topology else (),
        mechanism_profiles=dict(profiles),
        facts=tuple(facts),
        trends=trends,
        traces=traces,
        logs=logs,
    )


def _bundle_context(view):
    profiles_by_service, _common_mode = _catalog_profiles(view)
    topology, inbound, outbound = _topology_context(view)
    return profiles_by_service, topology, inbound, outbound


def build_initial_evidence(view, *, max_bytes: int) -> SerializedEvidenceBundle:
    catalog = _catalog_profiles(view)
    topology_context = _topology_context(view)
    profiles_by_service, _common_mode = catalog
    topology, inbound, outbound = topology_context
    limitations = [
        "Anomaly strength and onset are heuristics, not root-cause labels.",
        "Resource limits, GC, restarts, deployment history and injection labels are unavailable.",
        "Observed caller -> callee edges do not prove propagation or root cause.",
    ]
    if view.l.empty:
        limitations.append("Log telemetry unavailable; absence cannot exclude a fault.")
    if not topology.get("available"):
        limitations.append(
            f"Topology unavailable: {topology.get('reason', 'no reliable call edges')}."
        )
    full_selection = select_candidate_services(
        view,
        max_candidates=5,
        _catalog=catalog,
        _topology=topology_context,
    )
    last_error = None
    for max_candidates in range(5, 0, -1):
        selection = select_candidate_services(
            view,
            max_candidates=max_candidates,
            _catalog=catalog,
            _topology=topology_context,
        )
        removed_candidate_details = sorted(
            set(full_selection.candidates) - set(selection.candidates)
        )
        attempt_limitations = list(limitations)
        omitted_paths = []
        if removed_candidate_details:
            attempt_limitations.append(
                "Byte budget omitted candidate detail for: "
                + ", ".join(removed_candidate_details)
                + "; catalog-backed hints remain where available."
            )
            omitted_paths = [
                f"candidate_budget.services[{service}].detailed_evidence"
                for service in removed_candidate_details
            ]
        bundle = InitialEvidenceBundle(
            case_id=view.case_id,
            services=tuple(sorted(view.services)),
            time_start=int(view.start),
            time_end=int(view.end),
            incident_time=int(view.incident),
            candidates=tuple(
                _candidate_evidence(
                    view,
                    service,
                    reasons=selection.reasons[service],
                    profiles=profiles_by_service[service],
                    inbound=inbound,
                    outbound=outbound,
                )
                for service in selection.candidates
            ),
            candidate_hints=selection.hints,
            limitations=tuple(attempt_limitations),
            omitted_paths=tuple(omitted_paths),
        )
        try:
            return serialize_evidence_bundle(bundle, max_bytes=max_bytes)
        except EvidenceBudgetError as exc:
            last_error = exc
    raise last_error or EvidenceBudgetError("No candidate evidence fits the configured budget")


def build_followup_evidence(
    view,
    request: EvidenceRequest,
    *,
    initial: InitialEvidenceBundle,
    max_bytes: int,
) -> SerializedEvidenceBundle:
    validate_evidence_request(request, initial)
    selected = {candidate.service: candidate for candidate in initial.candidates}
    profiles_by_service, topology, inbound, outbound = _bundle_context(view)

    limitations = [
        "This is one bounded follow-up view; absence outside the requested view is not exculpatory.",
        "Observed caller -> callee edges do not prove propagation or root cause.",
    ]
    if not topology.get("available"):
        limitations.append(
            f"Topology unavailable: {topology.get('reason', 'no reliable call edges')}."
        )
    candidates = []
    seen_fact_refs = set()
    for service in sorted(request.services):
        candidate = _candidate_evidence(
            view,
            service,
            reasons=(
                selected[service].selection_reasons
                if service in selected
                else ("hint-authorized-followup",)
            ),
            profiles=profiles_by_service[service],
            inbound=inbound,
            outbound=outbound,
            include=request.view,
            mechanisms=request.mechanisms,
        )
        unique_facts = []
        for fact in candidate.facts:
            if fact.evidence_ref in seen_fact_refs:
                continue
            seen_fact_refs.add(fact.evidence_ref)
            unique_facts.append(fact)
        candidates.append(candidate.model_copy(update={"facts": tuple(unique_facts)}))
    bundle = FollowupEvidenceBundle(
        case_id=view.case_id,
        request=request,
        candidates=tuple(candidates),
        limitations=tuple(limitations),
    )
    return serialize_evidence_bundle(bundle, max_bytes=max_bytes)
