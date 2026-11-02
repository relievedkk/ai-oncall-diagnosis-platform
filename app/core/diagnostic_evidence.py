"""Versioned, bounded evidence contracts for auditable diagnosis workflows."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

MechanismState: TypeAlias = Literal["direct_signal", "possible_symptom", "ambiguous", "unavailable"]
FaultMechanism: TypeAlias = Literal["cpu", "mem", "delay", "disk", "loss", "socket"]
FollowupView: TypeAlias = Literal[
    "mechanism_details", "metric_trends", "logs", "traces", "topology"
]


class EvidenceBudgetError(ValueError):
    """The minimum authoritative evidence contract cannot fit its byte budget."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceFact(_FrozenModel):
    evidence_ref: str = Field(min_length=1, max_length=160)
    evidence_type: str = Field(min_length=1, max_length=64)
    service: str = Field(default="", max_length=256)
    mechanism: FaultMechanism | None = None
    statement: str = Field(min_length=1, max_length=2000)
    data: dict[str, Any] = Field(default_factory=dict)


class MechanismProfile(_FrozenModel):
    mechanism: FaultMechanism
    state: MechanismState
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    first_anomalous_time: int | None = None
    reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    available: bool = False
    strong_change: bool = False
    strongest_metric: str = Field(default="", max_length=256)
    ratio: float | None = None
    post_start_mean: float | None = None
    post_end_mean: float | None = None
    post_pattern: str = Field(default="unavailable", max_length=64)
    interpretation: str = Field(default="unavailable", max_length=128)
    fault_eligible: bool = False
    competing_mechanisms: tuple[FaultMechanism, ...] = Field(default_factory=tuple, max_length=6)


class CandidateEvidence(_FrozenModel):
    service: str = Field(min_length=1, max_length=256)
    selection_reasons: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    inbound_callers: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    outbound_dependencies: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    mechanism_profiles: dict[FaultMechanism, MechanismProfile]
    facts: tuple[EvidenceFact, ...] = Field(default_factory=tuple, max_length=64)
    trends: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=128)
    traces: tuple[dict[str, Any], ...] = Field(default_factory=tuple, max_length=128)
    logs: tuple[str, ...] = Field(default_factory=tuple, max_length=128)

    @model_validator(mode="after")
    def validate_profile_keys(self):
        if any(key != profile.mechanism for key, profile in self.mechanism_profiles.items()):
            raise ValueError("Mechanism profile keys must match profile.mechanism")
        return self


class CandidateHint(_FrozenModel):
    service: str = Field(min_length=1, max_length=256)
    visible_signals: tuple[str, ...] = Field(default_factory=tuple, max_length=16)
    topology_neighbors: tuple[str, ...] = Field(default_factory=tuple, max_length=64)
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple, max_length=32)


class EvidenceRequest(_FrozenModel):
    services: tuple[str, ...] = Field(min_length=1, max_length=2)
    mechanisms: tuple[FaultMechanism, ...] = Field(min_length=1, max_length=6)
    view: FollowupView
    justification_refs: tuple[str, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def validate_distinct_values(self):
        if len(set(self.services)) != len(self.services):
            raise ValueError("Evidence request services must be distinct")
        if len(set(self.mechanisms)) != len(self.mechanisms):
            raise ValueError("Evidence request mechanisms must be distinct")
        return self


class InitialEvidenceBundle(_FrozenModel):
    schema_version: Literal[3] = 3
    case_id: str = Field(min_length=1, max_length=160)
    services: tuple[str, ...] = Field(min_length=1, max_length=256)
    time_start: int
    time_end: int
    incident_time: int
    candidates: tuple[CandidateEvidence, ...] = Field(min_length=1, max_length=5)
    candidate_hints: tuple[CandidateHint, ...] = Field(default_factory=tuple, max_length=256)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=32)
    omitted_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=256)


class FollowupEvidenceBundle(_FrozenModel):
    schema_version: Literal[3] = 3
    case_id: str = Field(min_length=1, max_length=160)
    request: EvidenceRequest
    candidates: tuple[CandidateEvidence, ...] = Field(min_length=1, max_length=2)
    limitations: tuple[str, ...] = Field(min_length=1, max_length=32)
    omitted_paths: tuple[str, ...] = Field(default_factory=tuple, max_length=256)


@dataclass(frozen=True)
class SerializedEvidenceBundle:
    text: str
    sha256: str
    byte_length: int

    @property
    def payload(self) -> dict[str, Any]:
        """Return a detached copy so callers cannot mutate shared evidence."""
        return json.loads(self.text)


@dataclass(frozen=True)
class DiagnosisEvidencePolicy:
    allowed_services: frozenset[str]
    valid_refs: frozenset[str]
    observed_mechanism_refs: Any
    mechanism_refs: Any
    delay_blockers: frozenset[str]
    ambiguous_services: frozenset[str]
    directed_edges: frozenset[tuple[str, str]]
    topology_refs: Any

    @classmethod
    def from_bundles(
        cls,
        initial: SerializedEvidenceBundle,
        followup: SerializedEvidenceBundle | None = None,
    ) -> DiagnosisEvidencePolicy:
        payloads = [initial.payload] + ([followup.payload] if followup is not None else [])
        allowed = set()
        valid_refs = set()
        observed_mechanism_refs: dict[tuple[str, str], set[str]] = {}
        mechanism_refs: dict[tuple[str, str], set[str]] = {}
        delay_blockers = set()
        ambiguous = set()
        edges = set()
        topology_refs: dict[str, tuple[str, str]] = {}

        def collect_refs(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "evidence_ref" and isinstance(child, str):
                        valid_refs.add(child)
                    elif key == "evidence_refs" and isinstance(child, list):
                        valid_refs.update(ref for ref in child if isinstance(ref, str))
                    else:
                        collect_refs(child)
            elif isinstance(value, list):
                for child in value:
                    collect_refs(child)

        for payload in payloads:
            collect_refs(payload)
            for candidate in payload.get("candidates", []):
                service = candidate.get("service")
                if not isinstance(service, str):
                    continue
                allowed.add(service)
                profiles = candidate.get("mechanism_profiles", {})
                direct_resources = []
                for mechanism, profile in profiles.items():
                    if not isinstance(profile, dict):
                        continue
                    refs = frozenset(
                        ref for ref in profile.get("evidence_refs", []) if isinstance(ref, str)
                    )
                    if profile.get("state") == "direct_signal" and refs:
                        observed_mechanism_refs.setdefault((service, mechanism), set()).update(refs)
                        if profile.get("fault_eligible") is True:
                            mechanism_refs.setdefault((service, mechanism), set()).update(refs)
                        if mechanism != "delay":
                            direct_resources.append(mechanism)
                    if mechanism == "delay" and profile.get("state") == "possible_symptom":
                        delay_blockers.add(service)
                if direct_resources and profiles.get("delay", {}).get("strong_change"):
                    delay_blockers.add(service)
                if len(direct_resources) > 1 and not any(
                    profiles[name].get("fault_eligible") for name in direct_resources
                ):
                    ambiguous.add(service)
                for fact in candidate.get("facts", []):
                    if not isinstance(fact, dict) or fact.get("evidence_type") != "topology_edge":
                        continue
                    data = fact.get("data", {})
                    caller, callee = data.get("caller"), data.get("callee")
                    ref = fact.get("evidence_ref")
                    if all(isinstance(value, str) and value for value in (caller, callee, ref)):
                        edges.add((caller, callee))
                        topology_refs[ref] = (caller, callee)
        frozen_observed_refs = MappingProxyType(
            {key: frozenset(value) for key, value in observed_mechanism_refs.items()}
        )
        frozen_refs = MappingProxyType(
            {key: frozenset(value) for key, value in mechanism_refs.items()}
        )
        return cls(
            allowed_services=frozenset(allowed),
            valid_refs=frozenset(valid_refs),
            observed_mechanism_refs=frozen_observed_refs,
            mechanism_refs=frozen_refs,
            delay_blockers=frozenset(delay_blockers),
            ambiguous_services=frozenset(ambiguous),
            directed_edges=frozenset(edges),
            topology_refs=MappingProxyType(dict(topology_refs)),
        )


def canonical_json(value: Any) -> str:
    """Return stable UTF-8 JSON text independent of mapping insertion order."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _transport_bytes(text: str) -> int:
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def _bounded_text(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + " [excerpt; truncated]"


def _append_omission(payload: dict[str, Any], path: str) -> None:
    omitted = payload.setdefault("omitted_paths", [])
    if path not in omitted:
        omitted.append(path)


def _drop_low_priority_collections(payload: dict[str, Any]) -> None:
    for index, candidate in enumerate(payload.get("candidates", [])):
        for field in ("logs", "traces", "trends"):
            if candidate.get(field):
                candidate[field] = []
                _append_omission(payload, f"candidates[{index}].{field}")


def _compact_text_and_optional_data(payload: dict[str, Any]) -> None:
    for index, limitation in enumerate(payload.get("limitations", [])):
        compacted = _bounded_text(str(limitation), 240)
        if compacted != limitation:
            payload["limitations"][index] = compacted
            _append_omission(payload, f"limitations[{index}]")
    for index, candidate in enumerate(payload.get("candidates", [])):
        reasons = candidate.get("selection_reasons", [])
        for reason_index, reason in enumerate(reasons):
            compacted = _bounded_text(str(reason), 160)
            if compacted != reason:
                reasons[reason_index] = compacted
                _append_omission(payload, f"candidates[{index}].selection_reasons[{reason_index}]")
        for fact_index, fact in enumerate(candidate.get("facts", [])):
            statement = fact.get("statement", "")
            compacted = _bounded_text(str(statement), 240)
            if compacted != statement:
                fact["statement"] = compacted
                _append_omission(payload, f"candidates[{index}].facts[{fact_index}].statement")
            data = fact.get("data", {})
            if len(data) > 12:
                fact["data"] = dict(list(data.items())[:12])
                _append_omission(payload, f"candidates[{index}].facts[{fact_index}].data")
        for mechanism, profile in candidate.get("mechanism_profiles", {}).items():
            for reason_index, reason in enumerate(profile.get("reasons", [])):
                compacted = _bounded_text(str(reason), 160)
                if compacted != reason:
                    profile["reasons"][reason_index] = compacted
                    _append_omission(
                        payload,
                        f"candidates[{index}].mechanism_profiles.{mechanism}.reasons[{reason_index}]",
                    )
    for index, hint in enumerate(payload.get("candidate_hints", [])):
        if len(hint.get("topology_neighbors", [])) > 8:
            hint["topology_neighbors"] = hint["topology_neighbors"][:8]
            _append_omission(payload, f"candidate_hints[{index}].topology_neighbors")


def _compact_candidate_hints(payload: dict[str, Any]) -> None:
    hints = payload.get("candidate_hints", [])
    for index, hint in enumerate(hints):
        for field, limit in (
            ("visible_signals", 4),
            ("topology_neighbors", 4),
            ("evidence_refs", 4),
        ):
            if len(hint.get(field, [])) > limit:
                hint[field] = hint[field][:limit]
                _append_omission(payload, f"candidate_hints[{index}].{field}")
    if len(hints) > 12:
        payload["candidate_hints"] = hints[:12]
        _append_omission(payload, "candidate_hints[12:]")


def serialize_evidence_bundle(
    bundle: InitialEvidenceBundle | FollowupEvidenceBundle,
    *,
    max_bytes: int,
    reserve_bytes: int = 512,
) -> SerializedEvidenceBundle:
    """Serialize an authoritative bundle before it reaches a ToolMessage.

    Identity, granular references, mechanism states, topology direction and at
    least one limitation are never discarded. Optional excerpts are removed in
    deterministic semantic tiers and every removal is recorded.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1024:
        raise ValueError("max_bytes must be an integer of at least 1024")
    if (
        isinstance(reserve_bytes, bool)
        or not isinstance(reserve_bytes, int)
        or reserve_bytes < 0
        or max_bytes - reserve_bytes < 512
    ):
        raise ValueError("reserve_bytes leaves less than 512 bytes for evidence")

    payload = deepcopy(bundle.model_dump(mode="json"))
    payload["omitted_paths"] = sorted(set(payload.get("omitted_paths", [])))
    content_budget = max_bytes - reserve_bytes

    def encoded() -> str:
        payload["omitted_paths"] = sorted(set(payload.get("omitted_paths", [])))
        return canonical_json(payload)

    def fits(text: str) -> bool:
        return len(text.encode("utf-8")) <= content_budget and _transport_bytes(text) <= max_bytes

    text = encoded()
    if not fits(text):
        _drop_low_priority_collections(payload)
        text = encoded()
    if not fits(text):
        _compact_text_and_optional_data(payload)
        text = encoded()
    if not fits(text):
        _compact_candidate_hints(payload)
        text = encoded()
    while not fits(text) and len(payload.get("candidate_hints", [])) > 2:
        hints = payload["candidate_hints"]
        keep = max(2, len(hints) // 2)
        payload["candidate_hints"] = hints[:keep]
        _append_omission(payload, f"candidate_hints[{keep}:]")
        text = encoded()
    if not fits(text):
        raise EvidenceBudgetError(
            "Minimum evidence identity, references, mechanism states and topology exceed budget"
        )

    raw = text.encode("utf-8")
    return SerializedEvidenceBundle(
        text=text,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_length=len(raw),
    )
