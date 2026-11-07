"""Transparent retrieval scores and separate, conservative diagnosis permissions."""

import math
from numbers import Real

from app.core.single_pass import FAULTS, EvidenceSet, Hypothesis, make_window
from app.evaluation.single_pass_events import log_facts
from app.evaluation.single_pass_metrics import aligned_latency_quantiles, metric_facts
from app.evaluation.single_pass_rpc_wait import endpoint_wait_contrast
from app.evaluation.single_pass_semantics import (
    PROFILE_SHA256,
    SEMANTICS_REVISION,
    published_cpu_usage,
    resolve_semantics,
)
from app.evaluation.single_pass_traces import trace_facts

EVENTS = {
    "cpu": {"cpu_throttling"},
    "mem": {"memory_allocation"},
    "disk": {"disk_error"},
    "socket": {"socket_limit"},
    "loss": {"packet_loss"},
    "delay": set(),
}
RESOURCE_FAMILIES = ("cpu", "mem", "disk", "socket")


def collect_evidence(
    view, *, incident=None, semantics=None, status_semantics=None, telemetry_profile=None
):
    window = make_window(view, incident=incident)
    resolved = resolve_semantics(view, telemetry_profile)
    if semantics is not None:
        resolved.update(semantics)
    facts = (
        metric_facts(view, window, semantics=resolved)
        + log_facts(view, window, timestamp_unit=getattr(view, "log_timestamp_unit", "seconds"))
        + trace_facts(view, window, status_semantics=status_semantics)
    )
    quality = {
        "metric_semantics": "explicit_only",
        "trace_status_semantics": status_semantics or "unknown",
        "missing_metric_baselines": sum(
            f.source == "metrics" and f.data.get("coverage") == "baseline_missing" for f in facts
        ),
    }
    if telemetry_profile not in {None, "unknown"}:
        quality.update(
            telemetry_profile=telemetry_profile,
            semantic_revision=SEMANTICS_REVISION,
            semantic_sha256=PROFILE_SHA256,
        )
    return EvidenceSet(
        case_id=view.case_id,
        window=window,
        services=tuple(sorted(view.services)),
        facts=facts,
        quality=quality,
        aligned_latency_quantiles=aligned_latency_quantiles(view, window),
    )


def _changed(f):
    return f.source == "metrics" and f.data.get("changed") is True


def _stable(f):
    return (
        f.source == "metrics"
        and f.data.get("coverage") == "complete"
        and not f.data.get("sampling_gap")
        and f.data.get("changed") is False
    )


def _specific(f):
    return (
        f.source == "logs"
        and f.data.get("specific") is True
        and f.data.get("new_event") is True
        and f.data.get("after_buckets", 0) >= 2
    )


def _corroborated_errno(f, changed):
    family = {
        "EIO": "disk",
        "EROFS": "disk",
        "EMFILE": "socket",
        "ENFILE": "socket",
        "ENOMEM": "mem",
    }.get(f.data.get("literal_errno"))
    event = {"disk": "disk_error", "socket": "socket_limit", "mem": "memory_allocation"}.get(family)
    return bool(
        f.source == "logs"
        and family
        and f.family == event
        and f.data.get("new_event")
        and f.data.get("after_buckets", 0) >= 2
        and any(x.family == family for x in changed)
    )


def _trace_delay(f, *, own=False):
    prefix = "self_" if own else ""
    before, after = (
        f.data.get("before_" + prefix + "p90_us"),
        f.data.get("after_" + prefix + "p90_us"),
    )
    return (
        f.source == "traces"
        and f.family == "operation"
        and min(
            f.data.get("before_" + prefix + "spans", 0), f.data.get("after_" + prefix + "spans", 0)
        )
        >= 5
        and before is not None
        and before > 0
        and after is not None
        and after > 2 * before
    )


def _edge_latency_ratio(f, *, client=False, zero_increase=False):
    prefix = "client_" if client else ""
    before, after = f.data.get(f"before_{prefix}p90_us"), f.data.get(f"after_{prefix}p90_us")
    if (
        min(f.data.get(f"before_{prefix}spans", 0), f.data.get(f"after_{prefix}spans", 0)) >= 5
        and before is not None
        and after is not None
    ):
        if before > 0:
            return after / before
        if before == after == 0:
            return 1.0
        if zero_increase and before == 0 < after:
            return math.inf
    return None


def _stable_edge(f):
    ratio = _edge_latency_ratio(f)
    client_ratio = _edge_latency_ratio(f, client=True)
    return (
        ratio is not None
        and ratio <= 1.5
        and (
            "before_client_spans" not in f.data or client_ratio is not None and client_ratio <= 1.5
        )
    )


def _downstream_contrast(edges, unstable_edges):
    if not unstable_edges:
        return edges
    slow = [
        f
        for f in unstable_edges
        if max(
            _edge_latency_ratio(f, zero_increase=True) or 0,
            _edge_latency_ratio(f, client=True, zero_increase=True) or 0,
        )
        > 1.5
    ]
    # Keep the measured counterexamples and one example of incomplete coverage.
    missing = next((f for f in unstable_edges if f not in slow), None)
    return slow + ([missing] if missing is not None else [])


def _localized_path(evidence, service, facts, edges, *, require_contrast=False):
    metric = next(
        (
            f
            for f in facts
            if _changed(f) and f.family == "delay" and f.data.get("direction") == "up"
        ),
        None,
    )
    if metric is None or not all(_stable_edge(f) for f in edges):
        return ()
    incoming = [
        f
        for f in evidence.facts
        if f.source == "traces"
        and f.family == "edge"
        and f.service != service
        and f.data.get("callee") == service
    ]
    slow_handler = next((f for f in incoming if (_edge_latency_ratio(f) or 0) > 2), None)
    if slow_handler is not None and (edges or not require_contrast):
        return (metric, slow_handler, *edges)
    # Independent callers can localize a shared endpoint even when its handlers
    # run normally. One slow client-side RPC alone cannot localize that endpoint.
    slow_callers = {}
    for f in incoming:
        handler_ratio = _edge_latency_ratio(f)
        if handler_ratio is not None and handler_ratio <= 1.5:
            if (_edge_latency_ratio(f, client=True) or 0) > 2:
                slow_callers.setdefault(f.service, f)
    if len(slow_callers) >= 2:
        return (metric, *tuple(slow_callers.values())[:2], *edges)
    return ()


def _request_failure(f):
    if f.source != "metrics" or f.family != "error" or f.data.get("sampling_gap"):
        return False
    if _changed(f):
        return f.data.get("direction") == "up"
    return (
        f.data.get("coverage") == "complete"
        and f.data.get("before_median") == 0
        and f.data.get("onset_upper") is not None
        and any(v is not None and v > 0 for v in f.data.get("scales", {}).values())
    )


def _resource_surge(f, facts, *, peer_floor=False):
    """Bound positive fold changes for retrieval without inferring a unit or a limit."""
    before, after = f.data.get("before_median"), f.data.get("after_median")
    if after is None or after <= 0 or f.data.get("sampling_gap"):
        return 0.0
    if not (_changed(f) or f.data.get("coverage") == "baseline_missing"):
        return 0.0
    if before is None or before == 0 or peer_floor:
        # Use the measured peer envelope where the local baseline is missing or
        # unusually low. The peer scale affects retrieval only.
        metric = f.data.get("metric", "")
        if not isinstance(metric, str) or not metric.startswith(f.service + "_"):
            return 0.0
        suffix = metric[len(f.service) + 1 :]
        peer_max = max(
            (
                max(peer.data.get("before_median") or 0, peer.data.get("after_median") or 0)
                for peer in facts
                if peer.service != f.service
                and peer.family == f.family
                and peer.data.get("coverage") == "complete"
                and not peer.data.get("sampling_gap")
                and peer.data.get("metric") == f"{peer.service}_{suffix}"
                and peer.data.get("unit") == f.data.get("unit")
                and peer.data.get("metric_kind") == f.data.get("metric_kind")
            ),
            default=0,
        )
        before = max(before or 0, peer_max)
    if not 0 < before < after:
        return 0.0
    return min(5.0, (math.log2(after) - math.log2(before)) / 3)


def _metric_log_fold(f):
    before, after = f.data.get("before_median"), f.data.get("after_median")
    if before is None or after is None or before <= 0 or after <= 0:
        return None
    return math.log2(after) - math.log2(before)


def _slow_client_branches(edges):
    branches = {}
    for f in edges:
        callee = f.data.get("callee")
        handler_ratio = _edge_latency_ratio(f)
        if callee and handler_ratio is not None and handler_ratio <= 1.5:
            if (_edge_latency_ratio(f, client=True) or 0) > 2:
                branches.setdefault(callee, f)
    return tuple(branches.values())


def _response_shape(evidence, service, facts, upward):
    if service not in evidence.aligned_latency_quantiles:
        return (), 0.0, 0.0, ()
    quantiles = {
        f.data.get("metric"): f
        for f in facts
        if f.family == "delay"
        and f.source == "metrics"
        and f.data.get("coverage") == "complete"
        and not f.data.get("sampling_gap")
        and f.data.get("metric_kind") != "counter_rate"
    }
    median, tail = (quantiles.get(f"{service}_latency-{p}") for p in (50, 90))
    if median is None or tail is None or not _changed(tail):
        return (), 0.0, 0.0, ()
    mid_fold, tail_fold = _metric_log_fold(median), _metric_log_fold(tail)
    onset = tail.data.get("onset_upper")
    if mid_fold is None or tail_fold is None or tail_fold <= 0 or onset is None:
        return (), 0.0, 0.0, ()
    unit = median.data.get("unit")
    if (
        unit not in (None, "unknown")
        and unit == tail.data.get("unit")
        and any(
            median.data[f"{period}_median"] > tail.data[f"{period}_median"]
            for period in ("before", "after")
        )
    ):
        return (), 0.0, 0.0, ()
    if any(f.data.get("onset_lower") is None or f.data["onset_lower"] <= onset for f in upward):
        return (), 0.0, 0.0, ()
    trace_edges = [f for f in evidence.facts if f.source == "traces" and f.family == "edge"]
    incoming = [f for f in trace_edges if f.data.get("callee") == service and f.service != service]
    callers = {
        f.service for f in incoming if _edge_latency_ratio(f, zero_increase=True) is not None
    }
    if len(callers) == 1:
        # Several client-side delays at one shared caller are a competing
        # explanation for a tail observed on a single downstream service.
        siblings = [
            f for f in trace_edges if f.service in callers and f.data.get("callee") != service
        ]
        if len(_slow_client_branches(siblings)) >= 2:
            return (), 0.0, 0.0, ()
    slow_callers = {}
    for f in incoming:
        if max(_edge_latency_ratio(f) or 0, _edge_latency_ratio(f, client=True) or 0) > 2:
            slow_callers.setdefault(f.service, f)
    return (
        (median, tail),
        tail_fold - max(0.0, mid_fold),
        min(mid_fold, tail_fold) if _changed(median) else 0.0,
        tuple(slow_callers.values())[:2],
    )


def _later_downstream_tail(evidence, tail, edges):
    """Observed response timing helps rank a service ahead of a later symptom."""
    onset = tail.data["onset_upper"]
    slow_peers = {f.data.get("callee") for f in edges if (_edge_latency_ratio(f) or 0) > 2}
    return next(
        (
            f
            for f in evidence.facts
            if f.service in slow_peers
            and f.data.get("metric") == f"{f.service}_latency-90"
            and _changed(f)
            and f.data.get("direction") == "up"
            and f.data.get("onset_lower") is not None
            and f.data["onset_lower"] >= onset + 15
        ),
        None,
    )


def _resource_rank_adjustments(evidence, facts, upward, response_onset):
    by_family = {family: [f for f in upward if f.family == family] for family in RESOURCE_FAMILIES}
    memory = max(
        (
            _metric_log_fold(f) or 0
            for f in by_family["mem"]
            if f.data.get("quantity") == "memory_usage" and f.data.get("unit") == "bytes"
        ),
        default=0,
    )
    sockets = max((_metric_log_fold(f) or 0 for f in by_family["socket"]), default=0)
    weak = max((_metric_log_fold(f) or 0 for f in facts if _weak_socket(f)), default=0)
    adjustments = {}
    if memory >= 1 or sockets >= math.log2(1.5) or weak > 0 or by_family["disk"]:
        adjustments["cpu"] = -0.75 * max(
            (_resource_surge(f, evidence.facts) for f in by_family["cpu"]), default=0
        )
    if not by_family["cpu"] and any(
        f.family == "cpu" and _changed(f) and f.data.get("direction") == "down" for f in facts
    ):
        adjustments["cpu"] = -1.5
    if sockets >= math.log2(1.5) or weak > 0:
        adjustments["socket"] = max(min(3.0, 2.8 * sockets), min(1.25, 2.8 * weak))
    if memory >= 1:
        disk = by_family["disk"] + [f for f in facts if f.family == "disk" and _post_only_metric(f)]
        pressure = max(
            (_resource_surge(f, evidence.facts, peer_floor=True) for f in disk), default=0
        )
        # Disk activity and memory growth can be related. A large peer-relative
        # disk excursion should keep its priority over generic co-movement.
        discount = max(0, 1 - (pressure / 4) ** 3)
        adjustments["mem"] = min(3.0, 1.3 * memory) * discount
        adjustments["socket"] = adjustments.get("socket", 0) * discount
        if by_family["disk"]:
            adjustments["disk"] = pressure - max(
                _resource_surge(f, evidence.facts) for f in by_family["disk"]
            )
        elif pressure > 3:
            adjustments["disk"] = pressure - 3
    if response_onset is not None:
        for family, shifts in by_family.items():
            if shifts and all(
                f.data.get("onset_lower") is not None
                and response_onset + 15 <= f.data["onset_lower"]
                for f in shifts
            ):
                adjustments[family] = -1.5
    return adjustments


def _post_only_metric(f):
    return (
        f.source == "metrics"
        and f.data.get("coverage") == "baseline_missing"
        and f.data.get("after_samples", 0) >= 3
        and f.data.get("after_median") is not None
    )


def _temporal_score(facts, incident):
    if not facts:
        return 0.0
    score = 0.5 * max(float(f.data.get("persistence", 0)) for f in facts)
    times = [f.data.get("onset_upper") for f in facts if f.data.get("onset_upper") is not None]
    if times:
        score += 0.5 / (1 + min(abs(t - incident) for t in times) / 30)
    return score


def _weak_socket(f):
    return (
        f.source == "metrics"
        and f.family == "socket"
        and not f.data.get("changed")
        and f.data.get("coverage") == "complete"
        and not f.data.get("sampling_gap")
        and f.data.get("onset_upper") is not None
        and f.data.get("persistence", 0) >= 0.25
        and f.data.get("before_median") is not None
        and f.data["before_median"] > 0
        and f.data.get("after_median", 0) >= 1.25 * f.data["before_median"]
    )


def localization_support_refs(evidence, service):
    """Localize a possible fault service independently of full mechanism confirmation."""
    facts = [f for f in evidence.facts if f.service == service]
    shifts = [f for f in facts if _changed(f) and f.family in FAULTS and f.family != "delay"]
    responses = [
        f for f in facts if (_changed(f) and f.family in {"delay", "error"}) or _trace_delay(f)
    ]
    load = [f for f in facts if _stable(f) and f.family == "workload"]
    specific = [f for f in facts if _specific(f)]
    if specific and responses:
        return (specific[0].ref, responses[0].ref)
    if shifts and responses and load:
        return (shifts[0].ref, responses[0].ref, load[0].ref)
    # A localized resource shift plus stable demand and same-family other-service control
    # supports localization, not a particular fault mechanism or a mutation.
    if shifts and load:
        controls = [
            f
            for f in evidence.facts
            if f.service != service and _stable(f) and f.family == shifts[0].family
        ]
        if controls:
            return (shifts[0].ref, load[0].ref, controls[0].ref)
    own_path = [f for f in facts if _trace_delay(f, own=True)]
    if own_path and load:
        return (own_path[0].ref, load[0].ref)
    if load:
        edges = [f for f in facts if f.source == "traces" and f.family == "edge"]
        path = _localized_path(evidence, service, facts, edges, require_contrast=True)
        if path:
            return (load[0].ref, *(f.ref for f in path))
    return ()


def _for_service(evidence, service):
    facts = [f for f in evidence.facts if f.service == service]
    changed = [f for f in facts if _changed(f)]
    stable = [f for f in facts if _stable(f)]
    events = [f for f in facts if _specific(f) or _corroborated_errno(f, changed)]
    latency = [f for f in changed if f.family in {"delay", "error"}]
    latency += [f for f in facts if _trace_delay(f)]
    edges = [f for f in facts if f.source == "traces" and f.family == "edge"]
    path = _localized_path(evidence, service, facts, edges)
    failures = [f for f in facts if _request_failure(f)]
    upward = [
        f for f in changed if f.family in RESOURCE_FAMILIES and f.data.get("direction") == "up"
    ]
    response_pair, tail_growth, broad_growth, slow_callers = _response_shape(
        evidence, service, facts, upward
    )
    resource_adjustments = _resource_rank_adjustments(
        evidence, facts, upward, response_pair[1].data["onset_upper"] if response_pair else None
    )
    hypotheses = []
    for fault in FAULTS:
        rpc_wait = ()
        own = [f for f in changed if f.family == fault]
        direct = [f for f in events if f.family in EVENTS[fault]]
        competitors = [
            f
            for f in changed
            if f.family in {"cpu", "mem", "disk", "socket", "loss"} and f.family != fault
        ]
        # A structured mechanism-specific event can discriminate simultaneous resource changes.
        opposing = competitors + [f for f in events if f.family not in EVENTS[fault]]
        load = [f for f in changed if f.family == "workload"]
        opposing += load
        stable_load = [f for f in stable if f.family == "workload"]
        contrast = stable_load + [f for f in stable if f.family in FAULTS and f.family != fault]
        support = own + direct
        gaps, alternatives = [], []
        partial = [f for f in facts if f.family == fault and _post_only_metric(f)]
        weak = [f for f in facts if fault == "socket" and _weak_socket(f)]
        network_ambiguous = bool(
            latency
            and stable_load
            and not events
            and not any(f.family in {"cpu", "mem", "disk", "socket", "loss"} for f in changed)
            and not any(f.family == "loss" and _stable(f) for f in facts)
        )
        support += partial + weak
        if partial:
            gaps.append("missing_baseline_for_mechanism")
        if partial or weak:
            gaps.append("retrieval_only_observation")
        if fault in {"loss", "delay"} and network_ambiguous:
            alternatives.append("network_delay_or_loss_unresolved")
            if fault == "loss" and not own and not direct:
                support += latency
                gaps.append("retrieval_only_observation")
        failure_candidate = fault == "loss" and bool(failures and stable_load)
        tail_candidate = fault == "loss" and tail_growth >= 1
        if failure_candidate:
            support += failures
            gaps.append("request_errors_are_indirect")
            alternatives.append("request_failure_cause_unresolved")
        if tail_candidate:
            support += response_pair
            gaps.append("latency_tail_is_indirect")
            alternatives.append("selective_slowness_or_retries")
        if failure_candidate or tail_candidate:
            opposing += [f for f in stable if f.family == "loss"]
        if fault in {"loss", "delay"} and path:
            contrast += path
        mechanism = bool(direct)
        if fault == "cpu":
            mechanism |= any(
                (
                    published_cpu_usage(f)
                    or (
                        f.data.get("unit") not in {None, "unknown"}
                        and f.data.get("metric_kind") != "unknown"
                    )
                )
                and f.data.get("direction") == "up"
                for f in own
            )
            if any(
                f.family in {"gc", "retry"}
                and f.data.get("new_event")
                and f.data.get("after_buckets", 0) >= 2
                for f in facts
            ):
                alternatives.append("gc_or_retry_cpu_effect")
            connection_pressure = [f for f in facts if _weak_socket(f)]
            if connection_pressure and not direct:
                opposing += connection_pressure
                alternatives.append("connection_pressure_may_drive_cpu")
        elif fault == "mem":
            mechanism |= any(
                (
                    f.data.get("post_shape") == "growth"
                    or (
                        f.data.get("post_shape") == "sustained"
                        and f.data.get("quantity") == "memory_usage"
                        and f.data.get("unit") == "bytes"
                    )
                )
                and f.data.get("direction") == "up"
                for f in own
            )
            alternatives.append("cache_or_allocation_growth")
        elif fault == "disk":
            mechanism |= any(
                f.data.get("metric_kind") != "unknown"
                and f.data.get("unit") not in {None, "unknown"}
                and ("wait" in f.data.get("metric", "") or "latency" in f.data.get("metric", ""))
                and f.data.get("direction") == "up"
                for f in own
            )
            if any(f.data.get("direction") == "down" for f in own):
                alternatives.append("reduced_business_io")
        elif fault == "loss":
            mechanism |= any(
                f.data.get("direction") == "up"
                and f.data.get("metric_kind") in {"gauge", "counter_rate"}
                for f in own
            )
        elif fault == "socket":
            capacity = [
                f
                for f in own
                if f.data.get("metric_kind") == "gauge"
                and f.data.get("unit") == "count"
                and isinstance(f.data.get("resource_limit"), Real)
                and not isinstance(f.data.get("resource_limit"), bool)
                and math.isfinite(f.data["resource_limit"])
                and f.data["resource_limit"] > 0
                and f.data.get("after_median") is not None
                and f.data["after_median"] >= 0.9 * f.data["resource_limit"]
            ]
            connection_failures = [
                f
                for f in facts
                if f.source == "logs"
                and f.family == "connection_error"
                and f.data.get("new_event")
                and f.data.get("after_buckets", 0) >= 2
            ]
            if capacity and connection_failures:
                mechanism = True
                support += connection_failures
        elif fault == "delay":
            own_trace_latency = [f for f in facts if _trace_delay(f, own=True)]
            support += own_trace_latency
            resource_coverage = {
                f.family for f in stable if f.family in {"cpu", "mem", "disk", "socket", "loss"}
            }
            unstable_edges = [f for f in edges if not _stable_edge(f)]
            edges_stable = not unstable_edges
            contrast += _downstream_contrast(edges, unstable_edges)
            complete_own_path = any(
                min(f.data.get("before_self_spans", 0), f.data.get("after_self_spans", 0)) >= 5
                and f.data.get("before_self_spans") == f.data.get("before_spans")
                and f.data.get("after_self_spans") == f.data.get("after_spans")
                for f in own_trace_latency
            )
            # Direct exclusive-time and observed stable downstream paths can distinguish
            # own-service delay without requiring a nonexistent packet-loss series.
            traced_local_delay = bool(
                own
                and complete_own_path
                and edges
                and edges_stable
                and {"cpu", "mem"} <= resource_coverage
            )
            if (
                broad_growth >= 1
                and tail_growth < 1
                and {"cpu", "mem"} <= resource_coverage
                and not failures
                and any(
                    (fold := _metric_log_fold(f)) is not None and abs(fold) <= math.log2(1.25)
                    for f in stable_load
                )
            ):
                rpc_wait = endpoint_wait_contrast(evidence, service)
                contrast += rpc_wait
            mechanism = (
                bool(own or own_trace_latency)
                and resource_coverage == {"cpu", "mem", "disk", "socket", "loss"}
                and edges_stable
            ) or traced_local_delay
            if rpc_wait:
                gaps.append("rpc_wait_cause_unresolved")
                alternatives.append("packet_retransmission_or_path_delay")
            if traced_local_delay and "loss" not in resource_coverage:
                gaps.append("network_contrast_not_observed")
            if not edges_stable:
                alternatives.append("downstream_latency")
        unresolved = bool(
            (
                competitors
                or load
                or "gc_or_retry_cpu_effect" in alternatives
                or "connection_pressure_may_drive_cpu" in alternatives
            )
            and not direct
        )
        unresolved |= any(f.family not in EVENTS[fault] for f in events)
        if not mechanism:
            gaps.append("missing_mechanism_specific_observation")
        if not stable_load:
            gaps.append("missing_stable_workload_contrast")
        if not latency:
            gaps.append("missing_independent_response_contrast")
        if unresolved:
            gaps.append("unresolved_competing_explanation")
        eligible = bool(mechanism and stable_load and latency and contrast and not unresolved)
        # Scores retrieve hypotheses; they are deliberately not probabilities or permission.
        score = 5 * len(direct) + 2 * bool(own) + bool(latency) + bool(stable_load) + 4 * eligible
        score -= 0.5 * bool(load)
        # Bounded within-signal timing/shape features break retrieval ties without comparing raw units.
        score += _temporal_score(own, evidence.window.incident)
        score += 0.8 * bool(partial) + 1.5 * bool(weak)
        if fault in RESOURCE_FAMILIES:
            score += max((_resource_surge(f, evidence.facts) for f in own + partial), default=0)
            if not eligible and not direct:
                score += resource_adjustments.get(fault, 0)
        score += 1.5 * bool(path)
        if fault == "loss" and network_ambiguous and not own and not direct:
            score += 2 + _temporal_score(latency, evidence.window.incident) - 0.05
        if failure_candidate:
            score += 2.5 if not network_ambiguous else 1.0
        if tail_candidate:
            score += min(4.0, tail_growth) + 1.25 * (len(slow_callers) >= 2)
            if any(f.data.get("direction") == "down" for f in load):
                score += 1.0
            if not upward and any(
                f.family == "cpu" and f.data.get("direction") == "down" for f in changed
            ):
                score += 1.5
            if len(slow_callers) >= 2:
                contrast += slow_callers
        if fault == "delay" and broad_growth >= 1 and tail_growth < 1:
            branches = _slow_client_branches(edges)
            score += min(2.25, broad_growth) + 1.5 * (len(branches) >= 2)
            if len(branches) >= 2:
                contrast += branches[:2]
            if len(slow_callers) >= 2:
                score += 1.25
                contrast += slow_callers
                later_tail = _later_downstream_tail(evidence, response_pair[1], edges)
                if later_tail is not None:
                    score += 1.25
                    contrast.append(later_tail)
            if rpc_wait:
                score += 1.0
        hypotheses.append(
            Hypothesis(
                service=service,
                fault=fault,
                supporting_refs=tuple(dict.fromkeys(f.ref for f in support)),
                opposing_refs=tuple(dict.fromkeys(f.ref for f in opposing)),
                contrast_refs=tuple(
                    dict.fromkeys(f.ref for f in contrast + latency if f not in support)
                ),
                alternatives=tuple(
                    sorted(
                        set(
                            alternatives
                            + [f.family for f in competitors]
                            + (["workload_change"] if load else [])
                        )
                    )
                ),
                gaps=tuple(gaps),
                eligible=eligible,
                candidate_supported=bool(
                    support
                    and (
                        own
                        or direct
                        or partial
                        or weak
                        or failure_candidate
                        or tail_candidate
                        or fault == "delay"
                        and latency
                    )
                ),
                score=score,
            )
        )
    return hypotheses


def rank_hypotheses(evidence, *, limit=5):
    if not 1 <= limit <= 30:
        raise ValueError("invalid_hypothesis_limit")
    all_hypotheses = [h for service in evidence.services for h in _for_service(evidence, service)]
    # Frequency within each measured family is a retrieval contrast, not a causal veto.
    observed, shifted = {}, {}
    for f in evidence.facts:
        if f.source != "metrics" or f.family not in FAULTS:
            continue
        if f.data.get("coverage") == "complete":
            observed.setdefault(f.family, set()).add(f.service)
        if _changed(f):
            shifted.setdefault(f.family, set()).add(f.service)
    revised = []
    for h in all_hypotheses:
        population = observed.get(h.fault, set())
        changed_services = shifted.get(h.fault, set())
        bonus = (
            (1 - len(changed_services) / len(population))
            if population and h.service in changed_services
            else 0
        )
        revised.append(h.model_copy(update={"score": h.score + bonus}))
    ordered = sorted(revised, key=lambda h: (-h.score, h.service, h.fault))
    if limit > 5:
        return tuple(ordered[:limit])  # complete local permission catalog for the evaluator
    # An empty candidate can anchor an abstention when no type has evidence.
    # Otherwise leave those slots for candidates with observed support.
    ordered = [h for h in ordered if h.supporting_refs] or ordered[:1]
    chosen, seen = [], set()
    # Reserve up to three slots for different services before adding type alternatives.
    services = set()
    for h in ordered:
        if h.service not in services:
            chosen.append(h)
            seen.add((h.service, h.fault))
            services.add(h.service)
            if len(chosen) == min(3, limit):
                break
    for h in ordered:
        if len(chosen) == limit:
            break
        if h.service not in services:
            continue
        if (h.service, h.fault) in seen:
            continue
        chosen.append(h)
        seen.add((h.service, h.fault))
        if len(chosen) == limit:
            break
    return tuple(chosen)


def supported_service(evidence, service):
    if service not in evidence.services:
        return False
    if localization_support_refs(evidence, service):
        return True
    if any(h.eligible for h in _for_service(evidence, service)):
        return True
    facts = [f for f in evidence.facts if f.service == service]
    # Partial localization can retain a service-specific event with independent response evidence.
    return any(_specific(f) for f in facts) and any(
        (_changed(f) and f.family in {"delay", "error"}) or _trace_delay(f) for f in facts
    )
