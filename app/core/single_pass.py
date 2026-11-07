"""Case-scoped immutable contracts for one-request diagnosis."""

import hashlib
import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Fault = Literal["cpu", "mem", "disk", "socket", "loss", "delay"]
FAULTS = ("cpu", "mem", "disk", "socket", "loss", "delay")
REVISION = "single-pass-v5.1-r5-20261107"


def canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ObservationWindow(Frozen):
    start: int
    incident: int
    end: int

    @model_validator(mode="after")
    def valid_bounds(self):
        if not self.start <= self.incident <= self.end:
            raise ValueError("invalid_observation_window")
        return self


def make_window(view, *, incident=None) -> ObservationWindow:
    timestamp = view.incident if incident is None else incident
    if (
        isinstance(timestamp, bool)
        or not isinstance(timestamp, (int, float))
        or not math.isfinite(timestamp)
    ):
        raise ValueError("invalid_incident_time")
    timestamp = int(timestamp)
    return ObservationWindow(
        start=max(int(view.start), timestamp - 300),
        incident=timestamp,
        end=min(int(view.end), timestamp + 300),
    )


class Fact(Frozen):
    ref: str
    service: str
    source: Literal["metrics", "logs", "traces"]
    family: str
    data: dict[str, Any]


def fact_for(case_id, window, service, source, family, data) -> Fact:
    digest = hashlib.sha256(
        canonical([case_id, window.model_dump(), service, source, family, data]).encode()
    ).hexdigest()
    return Fact(ref="sp-" + digest[:24], service=service, source=source, family=family, data=data)


class EvidenceSet(Frozen):
    case_id: str
    window: ObservationWindow
    services: tuple[str, ...]
    facts: tuple[Fact, ...]
    quality: dict[str, Any] = Field(default_factory=dict)
    aligned_latency_quantiles: tuple[str, ...] = ()


class Hypothesis(Frozen):
    service: str
    fault: Fault
    supporting_refs: tuple[str, ...] = ()
    opposing_refs: tuple[str, ...] = ()
    contrast_refs: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    alternatives: tuple[str, ...] = ()
    eligible: bool = False
    candidate_supported: bool = False
    score: float = 0


FAMILY_PREFIXES = {
    "cpu": ("cpu", "throttl"),
    "mem": ("mem", "memory"),
    "disk": ("disk", "io_wait", "iowait", "storage_latency"),
    "socket": ("socket", "open_fds"),
    "loss": (
        "packet_loss",
        "packet_drop",
        "retransmit",
        "network_loss",
        "network_receive_drop",
        "network_transmit_drop",
        "tcp_retrans",
    ),
    "delay": ("latency", "delay"),
    "workload": ("workload", "request_rate"),
    "error": ("error", "failure_rate"),
}


def family_for(column, service):
    if not column.startswith(service + "_"):
        return None
    suffix = column[len(service) + 1 :].casefold().replace("-", "_")
    return next(
        (family for family, prefixes in FAMILY_PREFIXES.items() if suffix.startswith(prefixes)),
        None,
    )
