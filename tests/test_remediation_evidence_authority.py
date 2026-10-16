"""Proposal authority requires fresh observations of the requested target."""

import copy
import json
from datetime import timedelta

import pytest
from test_remediation_service import NOW, build_service, restart_intent

from app.config import config
from app.harness.contracts import RemediationMode, ScaleOutIntent


def observations(*, count=None, now=NOW, instance="orders-1"):
    collected = (now - timedelta(seconds=30)).isoformat()
    metric = {
        "success": True,
        "service_name": "orders",
        "instance_id": instance,
        "metric": "up",
        "data_points": [{"timestamp": collected, "value": 0}],
    }
    if count is not None:
        metric["live_replica_count"] = count
    return [
        {
            "evidence_type": "alert", "source": "query_prometheus_alerts",
            "success": True, "collected_at": collected,
            "payload": {"success": True, "alerts": [{
                "state": "firing", "active_at": "2000-01-01T00:00:00Z",
                "labels": {"service": "orders", "instance": instance},
            }]},
        },
        {
            "evidence_type": "metric", "source": "query_service_metric",
            "success": True, "collected_at": collected,
            "query": {"service_name": "orders", "instance_id": instance},
            "payload": metric,
        },
    ]


def scale_intent():
    return ScaleOutIntent(action_type="scale_out", service_name="orders", reason="Saturation")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "stale_collection", "future_collection", "missing_collection", "naive_collection",
    "stale_sample", "future_sample", "other_service", "missing_service",
    "other_instance", "missing_instance", "query_conflict", "alert_label_conflict",
    "failed_inner", "demo_wrapper", "forged_outer_success",
])
async def test_invalid_evidence_cannot_authorize_restart(tmp_path, failure):
    service, repository = build_service(tmp_path)
    items = observations()
    alert, metric = items
    if failure.endswith("collection"):
        for item in items:
            if failure == "missing_collection":
                item.pop("collected_at")
            else:
                item["collected_at"] = {
                    "stale_collection": "2000-01-01T00:00:00Z",
                    "future_collection": (NOW + timedelta(seconds=61)).isoformat(),
                    "naive_collection": "2026-10-13T11:59:30",
                }[failure]
    elif failure in {"stale_sample", "future_sample"}:
        metric["payload"]["data_points"][0]["timestamp"] = (
            "2000-01-01T00:00:00Z" if failure == "stale_sample"
            else (NOW + timedelta(seconds=61)).isoformat()
        )
    elif failure == "other_service":
        metric["payload"]["service_name"] = "another-service"
        metric["query"]["service_name"] = "another-service"
    elif failure == "missing_service":
        metric["payload"].pop("service_name")
        metric["query"].pop("service_name")
    elif failure == "other_instance":
        items = observations(instance="orders-2")
    elif failure == "missing_instance":
        metric["payload"].pop("instance_id")
        metric["query"].pop("instance_id")
    elif failure == "query_conflict":
        metric["query"]["service_name"] = "another-service"
    elif failure == "alert_label_conflict":
        alert["payload"]["alerts"][0]["labels"]["job"] = "another-service"
    elif failure == "failed_inner":
        metric["payload"]["success"] = False
    elif failure == "demo_wrapper":
        metric["payload"] = {"is_demo": True, "result": metric["payload"]}
    else:
        metric["payload"].pop("success")
    action = await service.propose("diagnosis-bad", restart_intent(), items)
    assert action["status"] == "denied"
    assert action["policy_reason"] == "missing_evidence"
    assert repository.outbox_events == []
    assert len(action["evidence_snapshot"]) == 2
    assert any(not item["success"] for item in action["evidence_snapshot"])


@pytest.mark.asyncio
async def test_fresh_matching_actual_tool_wrappers_authorize_restart(tmp_path):
    service, _ = build_service(tmp_path)
    items = observations()
    for item in items:
        item["payload"] = {"query": item.get("query", {}), "result": [
            {"type": "text", "text": json.dumps(item["payload"])},
        ]}
    action = await service.propose("diagnosis-valid", restart_intent(), items)
    assert action["status"] == "proposed"
    assert all(item["success"] for item in action["evidence_snapshot"])


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind", ["failed", "stale", "other_service", "demo", "future"])
async def test_invalid_replica_state_cannot_override_valid_state(tmp_path, bad_kind):
    service, _ = build_service(tmp_path, RemediationMode.GUARDED_AUTO)
    valid = observations(count=9)[1]
    invalid = copy.deepcopy(valid)
    invalid["payload"]["live_replica_count"] = 2
    if bad_kind == "failed":
        invalid["success"] = False
    elif bad_kind == "stale":
        invalid["collected_at"] = "2000-01-01T00:00:00Z"
    elif bad_kind == "other_service":
        invalid["payload"]["service_name"] = "another-service"
        invalid["query"]["service_name"] = "another-service"
    elif bad_kind == "demo":
        invalid["payload"]["is_demo"] = True
    else:
        invalid["payload"]["data_points"][0]["timestamp"] = (NOW + timedelta(days=1)).isoformat()
    action = await service.propose("diagnosis-scale", scale_intent(), [invalid, valid])
    assert action["status"] == "proposed"
    assert action["policy_decision"] == "allow"
    assert [row["success"] for row in action["evidence_snapshot"]] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_conflicting_valid_replica_counts_fail_closed_independent_of_order(tmp_path, reverse):
    service, repository = build_service(tmp_path)
    items = [observations(count=2)[1], observations(count=9)[1]]
    action = await service.propose("diagnosis-conflict", scale_intent(), items[::-1] if reverse else items)
    assert action["status"] == "denied"
    assert action["policy_reason"] == "missing_target_state"
    assert repository.outbox_events == []


@pytest.mark.asyncio
async def test_replica_count_from_nonmetric_evidence_is_not_live_state(tmp_path):
    service, _ = build_service(tmp_path)
    items = observations()
    items[0]["payload"]["live_replica_count"] = 2
    action = await service.propose("diagnosis-wrong-state-type", scale_intent(), items)
    assert action["status"] == "denied"
    assert action["policy_reason"] == "missing_target_state"


@pytest.mark.asyncio
async def test_configured_evidence_age_and_clock_skew_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "remediation_evidence_max_age_seconds", 10, raising=False)
    monkeypatch.setattr(config, "remediation_evidence_clock_skew_seconds", 0, raising=False)
    service, _ = build_service(tmp_path)
    action = await service.propose("diagnosis-age", restart_intent(), observations())
    assert action["status"] == "denied"


@pytest.mark.asyncio
@pytest.mark.parametrize("log_target,age,expected", [("orders", 30, "proposed"), ("other", 30, "denied"), ("orders", 700, "denied")])
async def test_rollback_requires_fresh_target_logs(tmp_path, log_target, age, expected):
    from app.harness.contracts import RollbackRevisionIntent

    service, repository = build_service(tmp_path)
    items = observations()
    items.append({"evidence_type": "log", "source": "cls", "success": True,
        "collected_at": NOW.isoformat(), "payload": {"success": True, "records": [{
            "service_name": log_target, "message": "deployment failure",
            "timestamp": (NOW - timedelta(seconds=age)).isoformat(),
        }]}})
    intent = RollbackRevisionIntent(action_type="rollback_revision", service_name="orders",
        revision="stable-42", reason="deployment failure")
    action = await service.propose("rollback-log-test", intent, items)
    assert action["status"] == expected
    assert len(repository.outbox_events) == (1 if expected == "proposed" else 0)

