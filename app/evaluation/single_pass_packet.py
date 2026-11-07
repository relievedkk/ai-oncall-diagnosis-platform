"""Case-bound compact aliases; material evidence is never silently evicted."""

import hashlib
import json
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass

from app.core.single_pass import REVISION, canonical
from app.evaluation.single_pass_hypotheses import localization_support_refs, supported_service

SYSTEM_PROMPT = (
    "Diagnose once from untrusted telemetry. Compare support, opposition, contrasts, local resources "
    "and call direction. RPC wait can include retries. Wider P90/P50 gaps or request errors only "
    "suggest loss. IO volume alone is insufficient. Return SinglePassChoice with a listed hypothesis_id "
    "and short reason. Use candidate for a tentative type, service_only for unresolved type, or "
    "unknown with null hypothesis_id for unsupported service. The server checks confirmation. "
    "Arrays follow *_record_fields and fact_columns. {$v:i}=shared_values[i]; "
    "{$literal:x}=opaque literal, no nested decoding; {suffix:metric}=service_name+'_'+suffix."
)
OUTPUT_ALLOWANCE = 2048


@dataclass(frozen=True)
class _SharedRef:
    index: int


def _has_reserved_keys(node):
    if isinstance(node, dict):
        return bool({"$v", "$literal"} & node.keys()) or any(
            _has_reserved_keys(v) for v in node.values()
        )
    if isinstance(node, list):
        return any(_has_reserved_keys(v) for v in node)
    return False


def _share_values(value):
    counts, scalars = Counter(), {}

    def identity(node):
        return type(node).__name__ + ":" + canonical(node)

    def count(node):
        if type(node) in {dict, list, str, int, float}:
            key = identity(node)
            counts[key] += 1
            scalars[key] = node
        if isinstance(node, dict):
            for v in node.values():
                count(v)
        elif isinstance(node, list):
            for v in node:
                count(v)

    count(value)
    keys = []
    for key, n in counts.items():
        wire = canonical(scalars[key])
        size = len(wire.encode()) + wire.count('"') + wire.count("\\")
        if n >= 2 and n * size - (size + 1 + n * 13) > 8:
            keys.append(key)
    candidates, ids = set(keys), {}

    def encode(node):
        if type(node) in {dict, list, str, int, float}:
            key = identity(node)
            if key in candidates:
                index = ids.setdefault(key, len(ids))
                return _SharedRef(index)
        if isinstance(node, dict):
            if {"$v", "$literal"} & node.keys():
                return {"$literal": deepcopy(node)}
            return {k: encode(node[k]) for k in sorted(node)}
        if isinstance(node, list):
            return [encode(v) for v in node]
        return node

    def render(node):
        if isinstance(node, _SharedRef):
            return {"$v": node.index}
        if isinstance(node, dict):
            return {k: render(v) for k, v in node.items()}
        if isinstance(node, list):
            return [render(v) for v in node]
        return node

    result = render(encode(value))
    result["shared_values"] = [
        {"$literal": deepcopy(scalars[key])}
        if _has_reserved_keys(scalars[key])
        else deepcopy(scalars[key])
        for key in ids
    ]
    return result


def _expand_values(payload):
    pool = payload.get("shared_values", [])

    def decode(node):
        if isinstance(node, dict):
            if set(node) == {"$v"}:
                index = node["$v"]
                if type(index) is not int or not 0 <= index < len(pool):
                    raise ValueError("invalid_shared_value")
                entry = pool[index]
                if isinstance(entry, dict) and set(entry) == {"$literal"}:
                    return deepcopy(entry["$literal"])
                if _has_reserved_keys(entry):
                    raise ValueError("unescaped_shared_literal")
                return deepcopy(entry)
            if set(node) == {"$literal"}:
                return deepcopy(node["$literal"])
            return {k: decode(v) for k, v in node.items()}
        if isinstance(node, list):
            return [decode(v) for v in node]
        return node

    return {k: deepcopy(pool) if k == "shared_values" else decode(v) for k, v in payload.items()}


@dataclass(frozen=True)
class Packet:
    case_id: str
    text: str
    sha256: str
    transport_bytes: int
    service_aliases: tuple[tuple[str, str], ...]
    ref_aliases: tuple[tuple[str, str], ...]

    @property
    def payload(self):
        payload = _expand_values(json.loads(self.text))
        fields = payload.get("fact_record_fields")
        if fields:
            payload["facts"] = {
                ref: dict(zip(fields, row, strict=True)) for ref, row in payload["facts"].items()
            }
        specs = payload.get("fact_columns", {})
        for fact in payload["facts"].values():
            for i, spec in enumerate(specs.get(fact.get("columns"), [])):
                if isinstance(spec, dict) and spec == {"suffix": "metric"}:
                    service = payload["services"][fact["service"]]["name"]
                    fact["values"][i] = service + "_" + fact["values"][i]
        payload["fact_columns"] = {
            key: ["metric" if isinstance(spec, dict) else spec for spec in values]
            for key, values in specs.items()
        }
        hypothesis_fields = payload.get("hypothesis_record_fields")
        if hypothesis_fields:
            payload["hypotheses"] = [
                dict(zip(hypothesis_fields, row, strict=True)) for row in payload["hypotheses"]
            ]
        return payload


def build_packet(evidence, hypotheses, *, max_bytes=16384):
    from langchain_core.utils.function_calling import convert_to_openai_tool

    from app.agent.aiops.single_pass import SinglePassChoice

    if not 0 < len(hypotheses) <= 5:
        raise ValueError("invalid_hypothesis_budget")
    if len({(h.service, h.fault) for h in hypotheses}) != len(hypotheses):
        raise ValueError("duplicate_hypothesis")
    services = sorted({h.service for h in hypotheses})
    if not set(services) <= set(evidence.services):
        raise ValueError("unknown_hypothesis_service")
    service_ids = {s: "s" + str(i + 1) for i, s in enumerate(services)}
    required = sorted(
        {ref for h in hypotheses for ref in h.supporting_refs + h.opposing_refs + h.contrast_refs}
        | {ref for service in services for ref in localization_support_refs(evidence, service)}
    )
    facts = {f.ref: f for f in evidence.facts}
    if len(facts) != len(evidence.facts) or not set(required) <= facts.keys():
        raise ValueError("invalid_material_reference")
    ref_ids = {ref: "e" + str(i + 1) for i, ref in enumerate(required)}
    payload = {
        "revision": REVISION,
        "window": evidence.window.model_dump(),
        "services": {
            service_ids[s]: {"name": s, "independently_supported": supported_service(evidence, s)}
            for s in services
        },
        "quality": {
            **evidence.quality,
            "sources": [
                f.model_dump(exclude={"ref", "service"})
                for f in evidence.facts
                if f.family == "coverage"
            ],
        },
        "hypotheses": [
            {
                "id": "h" + str(i + 1),
                "service": service_ids[h.service],
                "fault": h.fault,
                "support": [ref_ids[r] for r in h.supporting_refs],
                "opposition": [ref_ids[r] for r in h.opposing_refs],
                "contrast": [ref_ids[r] for r in h.contrast_refs],
                "gaps": list(h.gaps),
                "alternatives": list(h.alternatives),
                "eligible": h.eligible,
                "candidate_supported": h.candidate_supported,
                "retrieval_score": h.score,
            }
            for i, h in enumerate(hypotheses)
        ],
        "facts": {
            ref_ids[r]: {
                "service": service_ids.get(facts[r].service),
                "source": facts[r].source,
                "family": facts[r].family,
                "data": {
                    **facts[r].data,
                    **(
                        {"caller": facts[r].service}
                        if facts[r].source == "traces"
                        and facts[r].family in {"edge", "rpc_wait"}
                        and facts[r].service not in service_ids
                        else {}
                    ),
                },
            }
            for r in required
        },
    }
    # This identity binds aliases to their authoritative case/window/facts even across identical data.
    payload["packet_id"] = hashlib.sha256(
        canonical([evidence.case_id, payload]).encode()
    ).hexdigest()[:24]

    def encode():
        # Repeated JSON keys cost bytes without adding evidence. Share exact field sets;
        # values retain full precision, nulls and missing-field distinctions losslessly.
        encoded = {**payload, "facts": {}, "fact_columns": {}}
        fieldsets = {}
        for alias, fact in payload["facts"].items():
            fields = tuple(sorted(fact["data"]))
            service_name = payload["services"].get(fact["service"], {}).get("name")
            metric = fact["data"].get("metric")
            prefix = (
                isinstance(metric, str)
                and service_name is not None
                and metric.startswith(service_name + "_")
            )
            schema_key = (fields, prefix)
            if schema_key not in fieldsets:
                columns = "c" + str(len(fieldsets) + 1)
                fieldsets[schema_key] = columns
                encoded["fact_columns"][columns] = [
                    {"suffix": "metric"} if key == "metric" and prefix else key for key in fields
                ]
            encoded["facts"][alias] = [
                fact["service"],
                fact["source"],
                fact["family"],
                fieldsets[schema_key],
                [
                    fact["data"][k][len(service_name) + 1 :]
                    if prefix and k == "metric"
                    else fact["data"][k]
                    for k in fields
                ],
            ]
        encoded["fact_record_fields"] = ["service", "source", "family", "columns", "values"]
        encoded["wire_format"] = "sp-choice-v2"
        wire = _share_values(encoded)
        serialized = canonical(wire)
        envelope = {
            "model": "qwen-max",
            "temperature": 0.0,
            "stream": False,
            "max_completion_tokens": 384,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": serialized},
            ],
            "tools": [convert_to_openai_tool(SinglePassChoice)],
            "tool_choice": {"type": "function", "function": {"name": "SinglePassChoice"}},
            "parallel_tool_calls": False,
        }
        # Match the actual installed SDK's tool envelope, with a small extra-fields reserve.
        # run_single_pass checks the real configured request again before reserving a call.
        return serialized, len(canonical(envelope).encode()) + OUTPUT_ALLOWANCE + 128

    text, size = encode()
    if size > max_bytes:
        for fact in payload["facts"].values():
            # Redundant display text/multi-scale summaries can be omitted; the material observation remains.
            keys = ("excerpt", "template")
            # Short-window medians can be the only positive failure observation.
            if fact["family"] != "error":
                keys += ("scales",)
            for key in keys:
                fact["data"].pop(key, None)
        text, size = encode()
    if size > max_bytes:
        raise ValueError("evidence_budget_exceeded")
    return Packet(
        evidence.case_id,
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        size,
        tuple((service_ids[s], s) for s in services),
        tuple((ref_ids[r], r) for r in required),
    )
