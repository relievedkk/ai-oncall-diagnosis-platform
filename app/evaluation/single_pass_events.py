"""Local log-template time series; log content is data, never authority."""

import json
import math
import re

from app.core.data_safety import redact_sensitive
from app.core.single_pass import fact_for

_HIDDEN = re.compile(r"root_cause[_ :]|fault.?injection|chaos.mesh|stress.ng", re.I)
_EVENTS = (
    ("memory_allocation", r"outofmemory|oomkilled|out of memory|allocation failure"),
    ("gc", r"garbage collect|\bGC pause\b"),
    ("disk_error", r"\bEIO\b|I/O error|input/output error|read.only file system"),
    ("socket_limit", r"\bEMFILE\b|\bENFILE\b|too many open files|socket.*exhaust"),
    ("packet_loss", r"packet loss|packets? dropped|retransmi"),
    ("timeout", r"timeout|timed? out|deadline exceeded"),
    ("connection_error", r"connection (?:reset|refused)|ECONNRESET|ECONNREFUSED|broken pipe"),
    ("retry", r"\bretry|retrying"),
    ("error_event", r"\bERROR\b|\bFATAL\b|\bexception\b"),
)
_PATTERNS = [(name, re.compile(pattern, re.I)) for name, pattern in _EVENTS]
_CODES = {
    "ENOMEM": "memory_allocation",
    "EIO": "disk_error",
    "EMFILE": "socket_limit",
    "ENFILE": "socket_limit",
    "packet_drop": "packet_loss",
    "retransmit": "packet_loss",
    "oom": "memory_allocation",
    "oom_killed": "memory_allocation",
}


def _template(text):
    text = str(redact_sensitive(text))[:2000]
    text = re.sub(r"\b[0-9a-f]{8}(?:-[0-9a-f]+)+\b", "<id>", text, flags=re.I)
    text = re.sub(r"\b\d+(?:\.\d+)*\b", "<n>", text)
    return text[:240]


def log_facts(view, window):
    if view.l.empty:
        return (
            fact_for(view.case_id, window, "", "logs", "coverage", {"status": "not_available"}),
        )
    groups = {}
    counts = {
        "status": "present",
        "visible_rows": 0,
        "unmatched_services": 0,
        "excluded_control_rows": 0,
        "oversized_rows": 0,
    }
    frame = view.l.loc[view.l.timestamp.between(window.start, window.end)]
    containers = {}
    for timestamp, container, message in frame[
        ["timestamp", "container_name", "message"]
    ].itertuples(index=False, name=None):
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            continue
        counts["visible_rows"] += 1
        container = str(container)
        if container not in containers:
            parts = set(container.split("_")) | {container}
            matched = [s for s in view.services if s in parts]
            containers[container] = matched[0] if len(matched) == 1 else None
        service = containers[container]
        if service is None:
            counts["unmatched_services"] += 1
            continue
        text = str(message)
        if _HIDDEN.search(container) or _HIDDEN.search(text):
            counts["excluded_control_rows"] += 1
            continue
        if len(text) > 4096:
            counts["oversized_rows"] += 1
            text = text[:4096]
        specific, event = False, None
        if text.lstrip().startswith("{"):
            try:
                record = json.loads(text)
                if isinstance(record, dict):
                    for key in ("errno", "event_type", "event", "code"):
                        code = record.get(key)
                        if isinstance(code, str) and code in _CODES:
                            event, specific = _CODES[code], True
                            break
            except (ValueError, RecursionError):
                pass
        event = event or next(
            (name for name, pattern in _PATTERNS if pattern.search(text)), "template_shift"
        )
        template = _template(text)
        key = (service, event, specific, template)
        group = groups.setdefault(
            key,
            {
                "before_count": 0,
                "after_count": 0,
                "before_buckets": set(),
                "after_buckets": set(),
                "specific": False,
                "first_after": None,
            },
        )
        period = "before" if timestamp < window.incident else "after"
        group[period + "_count"] += 1
        group[period + "_buckets"].add(int((timestamp - window.incident) // 5))
        group["specific"] |= specific
        if period == "after":
            group["first_after"] = min(group["first_after"] or timestamp, timestamp)
    # Aggregate every recognized event before capping optional normal templates.
    # Separate structured and keyword observations so repetition cannot upgrade semantics.
    aggregated = {}
    for (service, event, _specific, template), group in groups.items():
        if event == "template_shift":
            aggregated[(service, event, template)] = group
            continue
        key = (service, event, "structured" if group["specific"] else "keyword")
        target = aggregated.setdefault(
            key,
            {
                "before_count": 0,
                "after_count": 0,
                "before_buckets": set(),
                "after_buckets": set(),
                "specific": group["specific"],
                "first_after": None,
                "templates": [],
            },
        )
        for period in ("before", "after"):
            target[period + "_count"] += group[period + "_count"]
            target[period + "_buckets"].update(group[period + "_buckets"])
        if group["first_after"] is not None:
            target["first_after"] = min(
                target["first_after"] or group["first_after"], group["first_after"]
            )
        target["templates"].append(template)
    facts = []
    for service in view.services:
        selected = [(key, g) for key, g in aggregated.items() if key[0] == service]
        selected.sort(
            key=lambda item: (
                not item[1]["specific"],
                item[0][1] == "template_shift",
                -len(item[1]["after_buckets"]),
                item[0][1],
                item[0][2],
            )
        )
        counts.setdefault("omitted_templates", 0)
        events = [item for item in selected if item[0][1] != "template_shift"]
        normal = [item for item in selected if item[0][1] == "template_shift"]
        counts["omitted_templates"] += max(0, len(normal) - 16)
        for (_, event, template), group in events + normal[:16]:
            template = min(group["templates"]) if "templates" in group else template
            data = {
                **{k: v for k, v in group.items() if k != "templates"},
                "before_buckets": len(group["before_buckets"]),
                "after_buckets": len(group["after_buckets"]),
                "template": template,
                "excerpt": template[:160],
                "untrusted_text": True,
                "template_count": len(group.get("templates", [template])),
                "request_normalization": "unavailable_without_request_semantics",
            }
            data["new_event"] = bool(data["after_count"] and not data["before_count"])
            facts.append(fact_for(view.case_id, window, service, "logs", event, data))
    facts.append(fact_for(view.case_id, window, "", "logs", "coverage", counts))
    return tuple(facts)
