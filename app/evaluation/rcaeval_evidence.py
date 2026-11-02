"""Label-free mechanism evidence for RCAEval recordings."""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Mapping, Set

from app.core.diagnostic_evidence import FaultMechanism, MechanismProfile

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
        after_frame = after_frame.copy()
        after_frame[value_column] = after_frame[value_column].astype(float)
        after_frame = after_frame.dropna(subset=[value_column])
    except (TypeError, ValueError):
        return None
    if before.empty or after_frame.empty:
        return None
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
        and _matches_mechanism(column.split("_", 1)[1], prefixes)
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
    if not available:
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


def build_mechanism_profiles(view, service_name: str) -> dict[str, MechanismProfile]:
    """Build six label-free profiles from structured recorded telemetry only."""
    view._window(service_name, None, None, 20)
    profiles = {
        mechanism: _profile_from_metrics(view, service_name, mechanism) for mechanism in _PREFIXES
    }
    return classify_competing_mechanisms(profiles, common_mode=set())
