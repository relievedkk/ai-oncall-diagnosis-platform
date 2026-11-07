"""Same-trace paths and conservative exclusive interval approximations."""

import math
import re

from app.core.single_pass import fact_for

_RPC_METHOD = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+/[A-Za-z_]\w*")


def _quantile(values, fraction=0.9):
    if not values:
        return None
    ordered = sorted(values)
    position = fraction * (len(ordered) - 1)
    lower = int(position)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (
        position - lower
    )


def _union(intervals):
    total, end = 0.0, -math.inf
    for start, stop in sorted(intervals):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total


def _error(value, semantics):
    try:
        code = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(code):
        return None
    if semantics == "otel":
        return True if code == 2 else False if code == 1 else None
    if semantics == "http":
        return code >= 400 if 200 <= code <= 599 else None
    return None


def _rpc_wait_facts(case_id, window, groups):
    facts = []
    for (caller, callee, operation), group in sorted(groups.items()):
        data = {"callee": callee, "operation": operation[:120]}
        for period, traces in group.items():
            samples = list(traces.values())
            buckets = sorted({int((start - window.incident) // 30) for start, _, _ in samples})
            if len(samples) < 12 or len(buckets) < 3:
                break
            waits = [wait for _, wait, _ in samples]
            data.update(
                {
                    period + "_pairs": len(samples),
                    period + "_buckets": buckets,
                    period + "_wait_us": [_quantile(waits, q) for q in (0.1, 0.5, 0.9)],
                    period + "_handler_p90_us": _quantile([handler for _, _, handler in samples]),
                }
            )
        else:
            facts.append(fact_for(case_id, window, caller, "traces", "rpc_wait", data))
    return facts


def trace_facts(view, window, *, status_semantics=None):
    import pandas as pd

    if status_semantics not in {None, "otel", "http"}:
        raise ValueError("unknown_status_semantics")
    if view.t.empty:
        return (
            fact_for(view.case_id, window, "", "traces", "coverage", {"status": "not_available"}),
        )
    frame = view.t.copy()
    frame["startTimeMillis"] = pd.to_numeric(frame.startTimeMillis, errors="coerce")
    frame["duration"] = pd.to_numeric(frame.duration, errors="coerce")
    frame["_start"] = frame.startTimeMillis / 1000
    frame["_duration"] = frame.duration
    visible = frame._start.between(window.start, window.end)
    active_traces = set(frame.loc[visible, "traceID"].dropna().astype(str))
    # Malformed times in an otherwise visible trace are incomplete observations,
    # not proof that the remaining parent has no children. Known out-of-window
    # rows remain invisible and never contribute counts or comparisons.
    missing_start = ~frame._start.map(math.isfinite)
    frame = frame.loc[
        visible | (missing_start & frame.traceID.astype(str).isin(active_traces))
    ].copy()
    invalid_identity = frame[["traceID", "spanID"]].isna().any(axis=1)
    invalid_identity |= frame.traceID.astype(str).eq("") | frame.spanID.astype(str).eq("")
    frame["traceID"] = frame.traceID.astype(str)
    frame["spanID"] = frame.spanID.astype(str)
    # Detect ambiguous identities before any invalid-duration copy can disappear.
    duplicate = frame.duplicated(["traceID", "spanID"], keep=False)
    duplicate_count = int(duplicate.sum())
    finite = (
        frame._start.map(math.isfinite) & frame._duration.map(math.isfinite) & frame._duration.ge(0)
    )
    invalid_numeric = int((~finite).sum())
    incomplete_traces = set(frame.loc[~finite, "traceID"].astype(str))
    censored = int((finite & (frame._start + frame._duration / 1e6).gt(window.end)).sum())
    incomplete_traces.update(
        frame.loc[finite & (frame._start + frame._duration / 1e6).gt(window.end), "traceID"].astype(
            str
        )
    )
    incomplete_traces.update(frame.loc[duplicate, "traceID"].astype(str))
    incomplete_traces.update(frame.loc[invalid_identity, "traceID"].astype(str))
    invalid_identity_count = int(invalid_identity.sum())
    frame = frame.loc[
        finite
        & (frame._start + frame._duration / 1e6).le(window.end)
        & ~duplicate
        & ~invalid_identity
    ]
    spans, children, edges = {}, {}, {}
    for row in frame.itertuples(index=False):
        # Leading-underscore columns are renamed by pandas itertuples; use public fields.
        start, duration = float(row.startTimeMillis) / 1000, float(row.duration)
        service = str(row.serviceName)
        if service not in view.services:
            continue
        key = (str(row.traceID), str(row.spanID))
        spans[key] = {
            "service": service,
            "op": str(row.operationName) if pd.notna(row.operationName) else "",
            "parent": str(row.parentSpanID),
            "start": start,
            "end": start + duration / 1e6,
            "duration": duration,
            "status": _error(row.statusCode, status_semantics),
        }
    orphan, invalid_nested = 0, set()
    # Cycles (including equal-interval spans) must not invent call direction.
    visited, cyclic_traces = set(), set()
    for key in spans:
        path, node = set(), key
        while node in spans and node not in visited:
            if node in path:
                cyclic_traces.add(key[0])
                break
            path.add(node)
            node = (node[0], spans[node]["parent"])
        visited.update(path)
    incomplete_traces.update(cyclic_traces)
    remote_pairs = {}
    for key, span in spans.items():
        if key[0] in cyclic_traces:
            continue
        parent_key = (key[0], span["parent"])
        if parent_key not in spans or parent_key == key:
            orphan += int(span["parent"] not in {"", "None", "nan", "<NA>"})
            if span["parent"] not in {"", "None", "nan", "<NA>"}:
                incomplete_traces.add(key[0])
            continue
        parent = spans[parent_key]
        if not parent["start"] <= span["start"] <= span["end"] <= parent["end"]:
            invalid_nested.add(parent_key)
            incomplete_traces.add(key[0])
            continue
        children.setdefault(parent_key, []).append((span["start"], span["end"]))
        if parent["service"] != span["service"]:
            edge = edges.setdefault(
                (parent["service"], span["service"]), {"before": [], "after": []}
            )
            period = "before" if span["start"] < window.incident else "after"
            edge[period].append(span["duration"])
            operation = parent["op"].removeprefix("grpc.")
            if "/" in operation and operation == span["op"].removeprefix("grpc."):
                remote_pairs[parent_key] = (edge, period, key)
    # The format omits span kind. A sole nested remote span for the same qualified
    # RPC method is evidence of a client/server pair. Its wait is not self time.
    paired_clients, rpc_groups = set(), {}
    for key, (edge, child_period, child_key) in remote_pairs.items():
        if (
            len(children[key]) == 1
            and key not in invalid_nested
            and key[0] not in incomplete_traces
        ):
            parent = spans[key]
            period = "before" if parent["start"] < window.incident else "after"
            if period == child_period:
                edge.setdefault(period + "_client", []).append(parent["duration"])
                child = spans[child_key]
                method = parent["op"].removeprefix("grpc.").removeprefix("/")
                if _RPC_METHOD.fullmatch(method) and (
                    period == "after" or parent["end"] <= window.incident
                ):
                    wait = parent["duration"] - child["duration"]
                    if wait >= 0:
                        group = rpc_groups.setdefault(
                            (parent["service"], child["service"], parent["op"]),
                            {"before": {}, "after": {}},
                        )[period]
                        sample = (parent["start"], wait, child["duration"])
                        # A trace can contain retries or fan-out to the same method.
                        group[key[0]] = min(group.get(key[0], sample), sample)
            paired_clients.add(key)
    operations = {}
    for key, span in spans.items():
        group = operations.setdefault(
            (span["service"], span["op"][:120] or "unknown"),
            {
                "before": [],
                "after": [],
                "before_self": [],
                "after_self": [],
                "before_status": [],
                "after_status": [],
            },
        )
        period = "before" if span["start"] < window.incident else "after"
        group[period].append(span["duration"])
        if key in paired_clients:
            count = period + "_paired_remote_spans"
            group[count] = group.get(count, 0) + 1
        elif key not in invalid_nested and key[0] not in incomplete_traces:
            own = max(0, span["duration"] - _union(children.get(key, [])) * 1e6)
            group[period + "_self"].append(own)
        if span["status"] is not None:
            group[period + "_status"].append(span["status"])
    facts = []
    for (service, op), group in sorted(operations.items()):
        data = {"operation": op, "status_semantics": status_semantics or "unknown"}
        for period in ("before", "after"):
            data.update(
                {
                    period + "_spans": len(group[period]),
                    period + "_p90_us": _quantile(group[period]),
                    period + "_self_p90_us": _quantile(group[period + "_self"]),
                    period + "_self_spans": len(group[period + "_self"]),
                    period + "_error_count": sum(group[period + "_status"])
                    if group[period + "_status"]
                    else None,
                    period + "_known_status": len(group[period + "_status"]),
                }
            )
            count = period + "_paired_remote_spans"
            if count in group:
                data[count] = group[count]
        facts.append(fact_for(view.case_id, window, service, "traces", "operation", data))
    for (caller, callee), group in sorted(edges.items()):
        paired = {}
        if "before_client" in group or "after_client" in group:
            for period in ("before", "after"):
                values = group.get(period + "_client", [])
                paired[period + "_client_spans"] = len(values)
                paired[period + "_client_p90_us"] = _quantile(values)
        facts.append(
            fact_for(
                view.case_id,
                window,
                caller,
                "traces",
                "edge",
                {
                    "callee": callee,
                    "before_spans": len(group["before"]),
                    "after_spans": len(group["after"]),
                    "before_p90_us": _quantile(group["before"]),
                    "after_p90_us": _quantile(group["after"]),
                    **paired,
                },
            )
        )
    facts += _rpc_wait_facts(view.case_id, window, rpc_groups)
    facts.append(
        fact_for(
            view.case_id,
            window,
            "",
            "traces",
            "coverage",
            {
                "status": "present",
                "visible_valid_spans": len(spans),
                "duplicate_rows": duplicate_count,
                "invalid_numeric_rows": invalid_numeric,
                "invalid_identity_rows": invalid_identity_count,
                "incomplete_traces": len(incomplete_traces),
                "cyclic_traces": len(cyclic_traces),
                "orphan_spans": orphan,
                "invalid_nested_parents": len(invalid_nested),
                "censored_spans": censored,
                "parent_links": sum(len(v) for v in children.values()),
            },
        )
    )
    return tuple(facts)
