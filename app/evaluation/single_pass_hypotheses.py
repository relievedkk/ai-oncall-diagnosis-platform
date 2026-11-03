"""Transparent retrieval scores and separate, conservative diagnosis permissions."""

from app.core.single_pass import FAULTS, EvidenceSet, Hypothesis, make_window
from app.evaluation.single_pass_events import log_facts
from app.evaluation.single_pass_metrics import metric_facts
from app.evaluation.single_pass_traces import trace_facts

EVENTS = {
    "cpu": {"cpu_throttling"},
    "mem": {"memory_allocation"},
    "disk": {"disk_error"},
    "socket": {"socket_limit"},
    "loss": {"packet_loss"},
    "delay": set(),
}


def collect_evidence(view, *, incident=None, semantics=None, status_semantics=None):
    window = make_window(view, incident=incident)
    facts = (
        metric_facts(view, window, semantics=semantics)
        + log_facts(view, window)
        + trace_facts(view, window, status_semantics=status_semantics)
    )
    quality = {
        "metric_semantics": "explicit_only",
        "trace_status_semantics": status_semantics or "unknown",
        "missing_metric_baselines": sum(
            f.source == "metrics" and f.data.get("coverage") == "baseline_missing" for f in facts
        ),
    }
    return EvidenceSet(
        case_id=view.case_id,
        window=window,
        services=tuple(sorted(view.services)),
        facts=facts,
        quality=quality,
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


def _trace_delay(f, *, own=False):
    prefix = "self_" if own else ""
    before, after = (
        f.data.get("before_" + prefix + "p90_us"),
        f.data.get("after_" + prefix + "p90_us"),
    )
    return (
        f.source == "traces"
        and f.family == "operation"
        and min(f.data.get("before_spans", 0), f.data.get("after_spans", 0)) >= 5
        and before is not None
        and before > 0
        and after is not None
        and after > 2 * before
    )


def _for_service(evidence, service):
    facts = [f for f in evidence.facts if f.service == service]
    changed = [f for f in facts if _changed(f)]
    stable = [f for f in facts if _stable(f)]
    events = [f for f in facts if _specific(f)]
    latency = [f for f in changed if f.family in {"delay", "error"}]
    latency += [f for f in facts if _trace_delay(f)]
    hypotheses = []
    for fault in FAULTS:
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
        mechanism = bool(direct)
        if fault == "cpu":
            mechanism |= any(
                f.data.get("unit") not in {None, "unknown"}
                and f.data.get("metric_kind") != "unknown"
                and f.data.get("direction") == "up"
                for f in own
            )
            if any(
                f.family in {"gc", "retry"} and f.data.get("after_buckets", 0) >= 2 for f in facts
            ):
                alternatives.append("gc_or_retry_cpu_effect")
        elif fault == "mem":
            mechanism |= any(
                f.data.get("post_shape") == "growth" and f.data.get("direction") == "up"
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
            mechanism |= any(f.data.get("direction") == "up" for f in own)
        elif fault == "delay":
            support += [f for f in facts if _trace_delay(f, own=True)]
            resource_coverage = {
                f.family for f in stable if f.family in {"cpu", "mem", "disk", "socket", "loss"}
            }
            edges = [f for f in facts if f.source == "traces" and f.family == "edge"]
            edges_stable = all(
                f.data.get("before_spans", 0) >= 5
                and f.data.get("after_spans", 0) >= 5
                and f.data.get("before_p90_us") is not None
                and f.data.get("after_p90_us") is not None
                and f.data["after_p90_us"] <= 1.5 * f.data["before_p90_us"]
                for f in edges
            )
            contrast += edges
            # Complete resource contrasts are required; absent network telemetry is a gap.
            mechanism = (
                bool(support)
                and resource_coverage == {"cpu", "mem", "disk", "socket", "loss"}
                and edges_stable
            )
            if not edges_stable:
                alternatives.append("downstream_latency")
        unresolved = bool(
            (competitors or load or "gc_or_retry_cpu_effect" in alternatives) and not direct
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
        if own:
            score += 0.5 * max(float(f.data.get("persistence", 0)) for f in own)
            times = [
                f.data.get("onset_upper") for f in own if f.data.get("onset_upper") is not None
            ]
            if times:
                score += 0.5 / (1 + min(abs(t - evidence.window.incident) for t in times) / 30)
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
                score=score,
            )
        )
    return hypotheses


def rank_hypotheses(evidence, *, limit=5):
    if not 1 <= limit <= 30:
        raise ValueError("invalid_hypothesis_limit")
    all_hypotheses = [h for service in evidence.services for h in _for_service(evidence, service)]
    return tuple(sorted(all_hypotheses, key=lambda h: (-h.score, h.service, h.fault))[:limit])


def supported_service(evidence, service):
    if service not in evidence.services:
        return False
    if any(h.eligible for h in _for_service(evidence, service)):
        return True
    facts = [f for f in evidence.facts if f.service == service]
    # Partial localization can retain a service-specific event with independent response evidence.
    return any(_specific(f) for f in facts) and any(
        (_changed(f) and f.family in {"delay", "error"}) or _trace_delay(f) for f in facts
    )
