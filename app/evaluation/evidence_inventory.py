"""Label-free accounting of raw telemetry and extracted evidence, not causal authority."""

from collections import Counter

from app.core.single_pass import FAULTS, family_for
from app.evaluation.single_pass_hypotheses import collect_evidence

REQUIREMENTS = {
    "cpu": "CPU quantity and workload/GC/retry contrasts",
    "mem": "memory quantity, allocation/GC/pressure and workload contrasts",
    "disk": "IO wait/latency or storage errors; IO volume alone is insufficient",
    "socket": "connection/FD capacity and failures; count alone is insufficient",
    "loss": "packet drop/retransmission evidence; errors/timeouts are indirect",
    "delay": "network versus local/downstream time; paired spans and path coverage",
}


def inventory_case(view, evidence):
    metric_facts = [f for f in evidence.facts if f.source == "metrics"]
    extracted = {f.data.get("metric") for f in metric_facts}
    columns = sorted(c for c in view.m.columns if c != "time")
    services = {}
    for service in evidence.services:
        families = {}
        for family in FAULTS:
            raw = [c for c in columns if family_for(c, service) == family]
            facts = [f for f in metric_facts if f.service == service and f.family == family]
            status = (
                "not_collected"
                if not raw
                else "not_extracted"
                if not facts
                else "insufficient_coverage"
                if any(f.data.get("coverage") != "complete" for f in facts)
                else "semantics_unverified"
                if any(
                    not f.data.get("semantic_source")
                    and (
                        f.data.get("unit", "unknown") == "unknown"
                        or f.data.get("metric_kind", "unknown") == "unknown"
                    )
                    for f in facts
                )
                else "available"
            )
            families[family] = {
                "status": status,
                "scope": "metrics_schema_only",
                "columns": raw,
                "refs": [f.ref for f in facts],
                "coverage": sorted({f.data.get("coverage", "unknown") for f in facts}),
                "semantic_sources": sorted(
                    {f.data["semantic_source"] for f in facts if f.data.get("semantic_source")}
                ),
            }
        services[service] = families
    sources = {}
    for source, frame in (("logs", view.l), ("traces", view.t)):
        coverage = [f.data for f in evidence.facts if f.source == source and f.family == "coverage"]
        # Only counts and status leave this module; no excerpts, status strings or paths.
        counts = {
            k: v for row in coverage for k, v in row.items() if type(v) is int or k == "status"
        }
        sources[source] = {
            "status": "present" if len(frame) else "not_available",
            "raw_rows": len(frame),
            **counts,
        }
    direct = {
        "disk": any(
            f.family == "disk"
            and any(term in f.data.get("metric", "").casefold() for term in ("wait", "latency"))
            for f in metric_facts
        ),
        "loss": any(f.family == "loss" for f in metric_facts),
        "socket": any(
            f.family == "socket" and f.data.get("resource_limit") is not None for f in metric_facts
        ),
    }
    return {
        "case_id": evidence.case_id,
        "window": evidence.window.model_dump(),
        "metric_column_count": len(columns),
        "unextracted_metric_columns": sorted(set(columns) - extracted),
        "sources": sources,
        "services": services,
        "log_event_facts": dict(
            Counter(
                f.family for f in evidence.facts if f.source == "logs" and f.family != "coverage"
            )
        ),
        "missing_direct_metric_fields": {k: not v for k, v in direct.items()},
        "requirements": REQUIREMENTS,
        "mechanism_confirmed": False,
        "limitation": "Schema absence is scoped to metrics; inventory does not grant diagnosis authority.",
    }


def audit_inventory(pilot, *, telemetry_profile="rcaeval-re2", progress=None):
    rows = []
    for case_id in sorted(pilot.ids):
        if progress:
            progress(case_id)
        view = pilot.open(case_id)
        evidence = collect_evidence(view, telemetry_profile=telemetry_profile)
        rows.append(inventory_case(view, evidence))
    return {"case_count": len(rows), "model_calls": 0, "cases": rows}
