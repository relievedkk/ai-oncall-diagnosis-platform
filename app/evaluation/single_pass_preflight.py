"""Reusable zero-model audits bound to actual source and input file bytes."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

from app.core.single_pass import REVISION, canonical
from app.evaluation.evaluation_scope import DevelopmentScope
from app.evaluation.single_pass_hypotheses import collect_evidence, rank_hypotheses
from app.evaluation.single_pass_packet import Packet, build_packet
from app.evaluation.single_pass_runner import audit_development, source_snapshot


def input_snapshot(root, answers):
    root = Path(root).resolve(strict=True)
    entries = []
    files = sorted(p for p in root.rglob("*") if p.is_file())
    for path in files + [Path(answers).resolve(strict=True)]:
        if path in files and not path.resolve(strict=True).is_relative_to(root):
            raise ValueError("preflight_input_path_escape")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append(
            [str(path.relative_to(root)) if path in files else "<answers>", digest.hexdigest()]
        )
    return hashlib.sha256(canonical(entries).encode()).hexdigest()


def make_package(audit, context, telemetry_validation):
    return {
        "schema_version": 2,
        "context": context,
        "audit": audit,
        "audit_sha256": hashlib.sha256(canonical(audit).encode()).hexdigest(),
        "telemetry_validation": telemetry_validation,
        "telemetry_sha256": hashlib.sha256(canonical(telemetry_validation).encode()).hexdigest(),
    }


def load_package(path, context):
    package = json.loads(Path(path).read_text("utf-8"))
    if package.get("schema_version") != 2 or package.get("context") != context:
        raise ValueError("preflight_source_or_input_mismatch")
    for value, digest in (("audit", "audit_sha256"), ("telemetry_validation", "telemetry_sha256")):
        if hashlib.sha256(canonical(package[value]).encode()).hexdigest() != package.get(digest):
            raise ValueError("preflight_audit_hash_mismatch")
    return package


def cached_packets(validation, case_ids):
    """Decode only the exact, case-bound packets inside a source/input-verified package."""
    packets = {}
    try:
        for row in validation["cases"]:
            p = Packet(**row["packet"])
            if (
                not row["passed"]
                or p.case_id != row["case_id"]
                or p.case_id in packets
                or p.sha256 != row["packet_sha256"]
                or hashlib.sha256(p.text.encode()).hexdigest() != p.sha256
                or hashlib.sha256(canonical(row["packet"]).encode()).hexdigest()
                != row["packet_record_sha256"]
                or not 0 < p.transport_bytes <= 16384
            ):
                raise ValueError("cached_packet_invalid")
            # Validate lossless decoding before any provider can be created.
            payload = p.payload
            if (
                payload["revision"] != REVISION
                or dict(p.service_aliases) != {k: v["name"] for k, v in payload["services"].items()}
                or len(dict(p.service_aliases)) != len(p.service_aliases)
                or set(dict(p.ref_aliases)) != set(payload["facts"])
                or len(dict(p.ref_aliases)) != len(p.ref_aliases)
                or len({v for _, v in p.ref_aliases}) != len(p.ref_aliases)
            ):
                raise ValueError("cached_packet_alias_or_revision_mismatch")
            packets[p.case_id] = p
        if set(packets) != set(case_ids):
            raise ValueError("cached_packet_scope_mismatch")
    except (KeyError, TypeError, AttributeError):
        raise ValueError("cached_packet_invalid") from None
    return packets


def audit_telemetry(pilot, *, telemetry_profile, progress=None):
    rows = []
    for case_id in sorted(pilot.ids):
        if progress:
            progress(case_id)
        try:
            e = collect_evidence(pilot.open(case_id), telemetry_profile=telemetry_profile)
            packet = build_packet(e, rank_hypotheses(e))
            log = next(
                (f.data for f in e.facts if f.source == "logs" and f.family == "coverage"), {}
            )
            rows.append(
                {
                    "case_id": case_id,
                    "passed": True,
                    "transport_bytes": packet.transport_bytes,
                    "packet_sha256": packet.sha256,
                    "packet": asdict(packet),
                    "packet_record_sha256": hashlib.sha256(
                        canonical(asdict(packet)).encode()
                    ).hexdigest(),
                    "log_coverage": log,
                }
            )
        except Exception as exc:
            allowed = {
                "log_timestamps_unusable",
                "log_row_accounting_mismatch",
                "log_fact_extraction_empty",
                "evidence_budget_exceeded",
            }
            detail = (
                str(exc)
                if isinstance(exc, ValueError) and str(exc) in allowed
                else "telemetry_or_packet_invalid"
            )
            rows.append({"case_id": case_id, "passed": False, "detail": detail})
    return {
        "case_count": len(rows),
        "model_calls": 0,
        "passed": all(r["passed"] for r in rows),
        "cases": rows,
    }


def obtain_preflight(development, evaluation, *, telemetry_profile, cached=None):
    def context():
        return {
            "revision": REVISION,
            "source_sha256": source_snapshot(),
            "profile": telemetry_profile,
            "development_input_sha256": input_snapshot(development.root, development.answers_file),
            "input_sha256": input_snapshot(evaluation.root, evaluation.answers_file),
        }

    before = context()
    if cached is not None:
        return load_package(cached, before)
    audit = audit_development(
        development,
        scope=DevelopmentScope(development.answers_file),
        telemetry_profile=telemetry_profile,
        progress=lambda cid: print(f"Auditing {cid} (0 model calls)", flush=True),
    )
    validation = audit_telemetry(
        evaluation,
        telemetry_profile=telemetry_profile,
        progress=lambda cid: print(f"Validating {cid} (0 model calls)", flush=True),
    )
    if context() != before:
        raise ValueError("preflight_inputs_changed_during_audit")
    return make_package(audit, before, validation)
