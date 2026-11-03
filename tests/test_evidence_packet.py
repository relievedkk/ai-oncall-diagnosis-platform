import json

import pytest

from app.core.data_safety import bounded_tool_result, json_bytes
from app.core.evidence_packet import pack_evidence


def test_packet_retains_refs_under_budget_and_deduplicates():
    rows = [
        {
            "evidence_ref": f"ref-{i}",
            "source": "metrics",
            "result": {
                "success": True,
                "data_points": [{"metric": "cpu", "value": 9}],
                "details": "x" * 15000,
            },
        }
        for i in range(4)
    ]
    packet = pack_evidence(rows, max_bytes=4096)
    assert len(json.dumps(packet, ensure_ascii=False).encode()) <= 4096
    assert {r["evidence_ref"] for r in packet["evidence"]} == {f"ref-{i}" for i in range(4)}
    assert packet["evidence"][1]["same_payload_as"] == "ref-0"
    assert packet["compacted"] is True


def test_packet_does_not_hide_truncation_or_invent_success():
    packet = pack_evidence(
        [
            {
                "evidence_ref": "bad",
                "source": "logs",
                "result": {"success": False, "error": "query_failed", "records": []},
            }
        ],
        max_bytes=2048,
    )
    assert packet["evidence"][0]["result"]["success"] is False


def test_packet_fits_actual_transport_spacing_and_retains_all_refs():
    rows = [
        {
            "evidence_ref": f"metric-{i}",
            "source": "metrics",
            "evidence_type": "metric",
            "result": {
                "success": True,
                "data_points": [
                    {
                        "timestamp": 1700000000 + j,
                        "metric": "cpu_usage",
                        "value": i + j / 10,
                        "service": f"service-{i}",
                        "label": "observed-sample",
                    }
                    for j in range(6)
                ],
            },
        }
        for i in range(20)
    ]
    packet = pack_evidence(rows, max_bytes=16384)
    transported = bounded_tool_result(packet, max_bytes=16384)
    assert transported.get("error") != "tool_result_truncated"
    assert json_bytes(transported) <= 16384
    assert {r["evidence_ref"] for r in transported["evidence"]} == {r["evidence_ref"] for r in rows}


def test_packet_accounts_for_wrapper_depth_before_transport():
    nested = {"value": 42}
    for _ in range(10):
        nested = {"details": nested}
    rows = [
        {
            "evidence_ref": "nested-ref",
            "source": "metrics",
            "result": {
                "success": True,
                "data_points": [{"timestamp": 100, "metric": "cpu", "value": 10}],
                "details": nested,
            },
        }
    ]
    packet = pack_evidence(rows, max_bytes=16384)
    transported = bounded_tool_result(packet, max_bytes=16384)
    assert transported.get("error") != "tool_result_truncated"
    assert transported["evidence"][0]["evidence_ref"] == "nested-ref"
    assert packet["compacted"] is True


def test_more_than_transport_row_limit_returns_explicit_failure_not_preview():
    rows = [
        {"evidence_ref": f"r{i}", "source": "metrics", "result": {"success": True}}
        for i in range(129)
    ]
    packet = pack_evidence(rows, max_bytes=16384)
    assert packet.get("error") == "evidence_packet_too_large"
    assert packet["success"] is False


def test_packet_keeps_both_window_endpoints_at_smallest_tier():
    packet = pack_evidence(
        [
            {
                "evidence_ref": "window-ref",
                "source": "metrics",
                "result": {
                    "success": True,
                    "window": [100, 200],
                    "noise": [{"value": index, "details": "x" * 500} for index in range(20)],
                },
            }
        ],
        max_bytes=700,
    )
    assert "error" not in packet
    assert packet["evidence"][0]["result"]["window"] == [100, 200]


def test_packet_keeps_first_and_last_time_sample_when_compacted():
    packet = pack_evidence(
        [
            {
                "evidence_ref": "trend-ref",
                "source": "metrics",
                "result": {
                    "success": True,
                    "minute_means": [
                        {"start": 100 + index * 60, "mean": index, "details": "x" * 300}
                        for index in range(6)
                    ],
                },
            }
        ],
        max_bytes=900,
    )
    samples = packet["evidence"][0]["result"]["minute_means"]
    assert samples[0]["start"] == 100
    assert samples[-1]["start"] == 400


@pytest.mark.asyncio
async def test_report_receives_bounded_packet_instead_of_truncation_envelope():
    from langchain_core.runnables import RunnableLambda

    from app.agent.aiops.replanner import Response, _generate_response
    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime

    captured = []

    async def respond(value):
        captured.extend(str(message.content) for message in value.to_messages())
        return Response(response="offline test")

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(respond)

    evidence = [
        {
            "evidence_ref": f"ref-{i}",
            "task_scope": "scope",
            "source": "metrics",
            "evidence_type": "metric",
            "result": {"success": True, "details": "x" * 15000},
        }
        for i in range(4)
    ]
    with diagnosis_runtime(DiagnosisRuntime(context="evaluation", compact_evidence_context=True)):
        await _generate_response(
            {"input": "offline", "task_scope": "scope", "evidence": evidence, "past_steps": []},
            Model(),
        )
    content = "\n".join(captured)
    assert "tool_result_truncated" not in content
    assert all(f"ref-{i}" in content for i in range(4))
    assert "same_payload_as" in content


@pytest.mark.asyncio
async def test_evidence_only_report_does_not_reinject_model_step_summary():
    from langchain_core.runnables import RunnableLambda

    from app.agent.aiops.replanner import Response, _generate_response
    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime

    captured = []

    async def respond(value):
        captured.extend(str(message.content) for message in value.to_messages())
        return Response(response="offline test")

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return RunnableLambda(respond)

    evidence = [
        {
            "evidence_ref": "raw-ref",
            "task_scope": "scope",
            "source": "metrics",
            "evidence_type": "metric",
            "result": {"success": True, "data_points": [{"value": 9}]},
        }
    ]
    with diagnosis_runtime(DiagnosisRuntime(context="evaluation", evidence_only_report=True)):
        await _generate_response(
            {
                "input": "offline",
                "task_scope": "scope",
                "evidence": evidence,
                "past_steps": [("step", "MODEL_SUMMARY_MARKER")],
            },
            Model(),
        )
    content = "\n".join(captured)
    assert "raw-ref" in content
    assert "MODEL_SUMMARY_MARKER" not in content
