"""Bound model context as a whole. Original evidence remains the validation authority.

Callers must redact before packing and apply their normal transport boundary.
"""

import hashlib

from app.core.data_safety import bounded_tool_result, json_bytes
from app.core.diagnostic_evidence import canonical_json
from app.core.evidence import decode_evidence_result


def pack_evidence(evidence, *, max_bytes=16384):
    if type(max_bytes) is not int or max_bytes < 512:
        raise ValueError("max_bytes must be an integer >= 512")

    def encode(value):
        return canonical_json(value)

    def selected_indices(length, limit, *, keep_endpoints=False):
        if length <= limit:
            return list(range(length))
        if keep_endpoints:
            limit = max(2, limit)
            return sorted({round(index * (length - 1) / (limit - 1)) for index in range(limit)})
        return list(range(limit))

    for count, chars in ((6, 600), (3, 300), (1, 120)):
        omitted = []

        def compact(value, path="", depth=0, *, count=count, chars=chars, omitted=omitted):
            # The final boundary counts packet -> evidence -> record -> result
            # wrappers, too. Stop before its absolute depth-12 cutoff.
            if depth >= 8:
                omitted.append(path)
                return "[omitted: nesting limit]"
            if isinstance(value, str) and len(value) > chars:
                omitted.append(path)
                return value[:chars] + " [excerpt; truncated]"
            if isinstance(value, list):
                # Preserve protocol pairs and both ends of time series. Keeping only
                # the first item would erase the before/after meaning of the data.
                if path.endswith(".window") and len(value) <= 2:
                    indices = list(range(len(value)))
                elif path.endswith(".minute_means"):
                    indices = selected_indices(len(value), count, keep_endpoints=True)
                else:
                    limit = 3 if path.endswith(".candidates") else count
                    indices = selected_indices(len(value), limit)
                if len(indices) < len(value):
                    omitted.append(path)
                return [compact(value[index], path + f"[{index}]", depth + 1) for index in indices]
            if isinstance(value, dict):
                if len(value) > 128:
                    omitted.append(path)
                return {
                    k: compact(v, path + "." + str(k), depth + 1)
                    for k, v in list(value.items())[:128]
                }
            return value

        records, seen = [], {}
        for row in evidence:
            payload = decode_evidence_result(row.get("result"))
            if payload is None:
                payload = row.get("result")
            digest = hashlib.sha256(encode([row.get("source"), payload]).encode()).hexdigest()
            record = {k: row[k] for k in ("evidence_ref", "source", "evidence_type") if k in row}
            if digest in seen:
                record["same_payload_as"] = seen[digest]
            else:
                seen[digest] = row.get("evidence_ref")
                record["result"] = compact(payload, str(row.get("evidence_ref", "unknown")))
            records.append(record)
        packet = {
            "evidence": records,
            "compacted": bool(omitted) or len(seen) < len(records),
            "omitted_paths": omitted[:8],
            "limitations": "Excerpts are incomplete. same_payload_as shares content, not a new observation. "
            "Do not infer absence or causality from omitted content; originals are retained.",
        }
        if json_bytes(packet) <= max_bytes:
            transported = bounded_tool_result(packet, max_bytes=max_bytes)
            if transported.get("error") != "tool_result_truncated":
                return transported
    return {
        "success": False,
        "error": "evidence_packet_too_large",
        "evidence": [],
        "message": "Cannot fit evidence identifiers and excerpts; reduce query scope.",
    }
