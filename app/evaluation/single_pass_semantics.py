"""Curated dataset semantics: documented quantity is not a guessed physical unit."""

import hashlib

from app.core.single_pass import canonical

SEMANTICS_REVISION = "rcaeval-re2-published-20261103"
CPU_SOURCE = "rcaeval-paper-3.2-cpu-usage"
MEM_SOURCE = "rcaeval-2f703059-convert-mem-mb"
LATENCY_SOURCE = "rcaeval-owner-comment-5870300859"
SOURCES = {
    CPU_SOURCE: {
        "url": "https://arxiv.org/html/2412.17015v1#S3.SS2",
        "scope": "Published *_cpu resource-usage indicator; physical units/query unspecified. Relative comparison only.",
    },
    MEM_SOURCE: {
        "url": "https://github.com/phamquiluan/RCAEval/blob/2f703059960517b024c504c817771fa85294aa09/RCAEval/io/time_series.py",
        "scope": "convert_mem_mb divides *_mem by1e6; retain original bytes, exact underlying memory statistic unspecified.",
    },
    LATENCY_SOURCE: {
        "url": "https://github.com/phamquiluan/RCAEval/issues/80#issuecomment-5870300859",
        "scope": "Only ts-ui-dashboard_latency-90: seconds, trailing60s quantile,1s evaluation,15s scrape; downstream included.",
    },
}
PROFILE_SHA256 = hashlib.sha256(canonical([SEMANTICS_REVISION, SOURCES]).encode()).hexdigest()


def resolve_semantics(view, profile=None):
    if profile in {None, "unknown"}:
        return {}
    if profile != "rcaeval-re2":
        raise ValueError("unknown_telemetry_profile")
    resolved = {}
    for service in view.services:
        for suffix, quantity, source in (
            ("cpu", "cpu_usage", CPU_SOURCE),
            ("mem", "memory_usage", MEM_SOURCE),
        ):
            column = service + "_" + suffix
            if column in view.m.columns:
                resolved[column] = {
                    "kind": "unknown",
                    "unit": "bytes" if suffix == "mem" else "unknown",
                    "quantity": quantity,
                    "semantic_source": source,
                    "interpretation": "relative_change_only",
                }
    column = "ts-ui-dashboard_latency-90"
    if column in view.m.columns:
        resolved[column] = {
            "kind": "gauge",
            "unit": "seconds",
            "quantity": "service_response_latency",
            "semantic_source": LATENCY_SOURCE,
            "interpretation": "downstream_inclusive",
            "lookback_seconds": 60,
            "evaluation_step_seconds": 1,
            "scrape_seconds": 15,
        }
    return resolved


def published_cpu_usage(fact):
    return (
        fact.data.get("quantity") == "cpu_usage"
        and fact.data.get("semantic_source") == CPU_SOURCE
        and fact.data.get("interpretation") == "relative_change_only"
    )
