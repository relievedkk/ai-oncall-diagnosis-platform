"""Shared evidence decoding and observation checks for scoring and remediation."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Any


def _unusable_status(value: Any) -> bool:
    return value is not None and (
        not isinstance(value, str) or value in {"no_data", "no_traffic", "query_failed"}
    )


def evidence_response_layers(value: Any) -> list[dict[str, Any]] | None:
    """Expose bounded response wrappers so authority checks retain provenance."""
    layers = []
    for _ in range(16):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return None
            continue
        if isinstance(value, list):
            if len(value) != 1 or not isinstance(value[0], dict):
                return None
            value = value[0]
            continue
        if not isinstance(value, dict):
            return None
        layers.append(value)
        if value.get("type") == "text":
            value = value.get("text")
            continue
        for key in ("result", "payload", "content"):
            if key in value:
                value = value[key]
                break
        else:
            return layers
    return None


def decode_evidence_result(value: Any) -> dict[str, Any] | None:
    """Unwrap one response, preserving every layer's failure/demo markers."""
    layers = evidence_response_layers(value)
    if not layers:
        return None
    markers: dict[str, Any] = {}
    succeeded = False
    rejected = False
    for layer in layers:
        succeeded = succeeded or layer.get("success") is True
        rejected = rejected or layer.get("success") is False
        for key in ("error", "Error", "isError", "is_demo", "demo"):
            if layer.get(key):
                markers[key] = layer[key]
                rejected = True
        if _unusable_status(layer.get("data_status")):
            markers["data_status"] = layer["data_status"]
            rejected = True
    return {**layers[-1], **markers, "success": succeeded and not rejected}


def _timestamp_present(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value) and value >= 0
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def successful_observation(evidence_type: str, payload: Any) -> bool:
    """Require explicit success and actual observations for the evidence type."""
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return False
    if any(payload.get(key) for key in ("error", "Error", "isError", "is_demo", "demo")):
        return False
    if _unusable_status(payload.get("data_status")):
        return False
    if evidence_type == "metric":
        points = payload.get("data_points")
        if not isinstance(points, list) or not points:
            return False
        try:
            return all(
                isinstance(point, dict)
                and _timestamp_present(point.get("timestamp"))
                and not isinstance(point.get("value"), bool)
                and math.isfinite(float(point["value"]))
                for point in points
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
    if evidence_type == "log":
        records = payload.get("records", payload.get("logs"))
        if not isinstance(records, list) or not records:
            return False
        for record in records:
            if not isinstance(record, dict):
                return False
            raw = record.get("LogJson", record)
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    return False
            if not isinstance(raw, dict) or not isinstance(raw.get("message"), str):
                return False
            if not raw["message"].strip():
                return False
        return True
    if evidence_type == "alert":
        alerts = payload.get("alerts")
        return (
            isinstance(alerts, list)
            and bool(alerts)
            and all(isinstance(alert, dict) and bool(alert.get("labels")) for alert in alerts)
        )
    return False


def valid_evidence(item: Any) -> bool:
    """Require attribution and the same usable observations at every gate."""
    if not isinstance(item, dict) or not isinstance(item.get("source"), str):
        return False
    if not item["source"].strip():
        return False
    evidence_type = str(item.get("evidence_type", item.get("type", ""))).casefold().strip()
    return successful_observation(evidence_type, decode_evidence_result(item))
