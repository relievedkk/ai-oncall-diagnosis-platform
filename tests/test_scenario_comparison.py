"""Exercise real graph replay, honest measurement and experiment boundaries."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def test_replay_runs_graph_nodes_and_keeps_usage_and_human_scores_unknown():
    from app.evaluation.comparison import run_case

    result = asyncio.run(run_case("cpu_load"))
    nodes = {event.get("node") for event in result["trace"]}
    assert {"planner", "executor", "replanner"} <= nodes
    assert result["mode"] == "replay"
    assert result["semantic_accuracy_measured"] is False
    assert result["usage"]["tokens"] is None
    assert result["usage"]["cost"] is None
    assert result["usage"]["coverage"] == 0
    assert result["usage"]["model_calls"] > 0
    assert result["scoring"]["human_scores"]["root_cause_semantics"] is None
    assert result["scoring"]["review_status"] == "unreviewed"
    assert "合成" in result["report"]
    assert result["diagnosis_result"]["outcome"] == "insufficient_evidence"
    assert result["diagnosis_result"]["validation_issues"] == []
    assert len(result["diagnosis_result"]["observations"]) == 3
    assert all(
        observation["evidence_refs"] for observation in result["diagnosis_result"]["observations"]
    )
    assert all("known_cause" not in str(event) for event in result["trace"])


@pytest.mark.parametrize(
    "case_id,expected",
    [
        ("no_alerts", "no_active_alerts"),
        ("dependency_failure", "dependency_failed"),
        ("missing_logs", "insufficient_evidence"),
    ],
)
def test_replay_preserves_authoritative_failure_and_no_alert_outcomes(case_id, expected):
    from app.evaluation.comparison import run_case

    result = asyncio.run(run_case(case_id))
    assert result["diagnosis_result"]["outcome"] == expected


def test_baseline_and_agent_share_inputs_and_preserve_every_repeat():
    from app.evaluation.comparison import compare_cases

    result = asyncio.run(compare_cases(["cpu_load"], repeats=2))
    assert len(result["runs"]) == 4
    assert {run["strategy"] for run in result["runs"]} == {"agent", "baseline"}
    assert len({run["input_digest"] for run in result["runs"]}) == 1
    assert result["human_review_coverage"] == 0
    assert result["semantic_accuracy_measured"] is False
    assert result["strategies"]["agent"]["sample_count"] == 2
    assert result["strategies"]["baseline"]["sample_count"] == 2
    assert result["strategies"]["baseline"]["tokens"] is None
    assert (
        result["strategies"]["agent"]["model_calls"]
        > result["strategies"]["baseline"]["model_calls"]
    )
    for baseline in (run for run in result["runs"] if run["strategy"] == "baseline"):
        names = [event["tool"] for event in baseline["trace"] if event["kind"] == "tool"]
        assert names == ["query_service_metrics", "query_service_logs"]


def test_call_limit_is_authoritative_and_does_not_invent_tokens():
    from app.evaluation.comparison import run_case

    result = asyncio.run(run_case("cpu_load", max_calls=1))
    assert result["diagnosis_result"]["outcome"] == "budget_exhausted"
    assert result["usage"]["model_calls"] == 1
    assert result["usage"]["tokens"] is None


def test_baseline_preserves_fresh_refs_with_observations_and_review_rubric():
    from app.evaluation.comparison import run_case

    result = asyncio.run(run_case("cpu_load", strategy="baseline"))
    evidence = {item["evidence_ref"]: item for item in result["evidence"]}
    assert len(evidence) == 2
    assert all(not ref.startswith("legacy-") for ref in evidence)
    for observation in result["diagnosis_result"]["observations"]:
        assert set(observation["evidence_refs"]) <= evidence.keys()
    assert result["scoring"]["reviewer"] is None
    assert result["scoring"]["reviewed_at"] is None


def test_empty_log_query_is_not_cited_as_an_observation():
    from app.evaluation.comparison import run_case

    result = asyncio.run(run_case("missing_logs"))
    assert result["diagnosis_result"]["validation_issues"] == []
    assert result["scoring"]["rules"]["required_support_covered"] is False


def test_graph_step_budget_remains_authoritative_after_comparison_normalization(
    monkeypatch, tmp_path
):
    from app.evaluation import comparison

    scripts = json.loads(
        (comparison.SCENARIO_DIRECTORY / "scripts.json").read_text(encoding="utf-8")
    )
    scripts["cpu_load"]["query_sequence"] = ["query_service_metrics"] * 10
    (tmp_path / "scripts.json").write_text(json.dumps(scripts), encoding="utf-8")
    (tmp_path / "expectations.json").write_bytes(
        (comparison.SCENARIO_DIRECTORY / "expectations.json").read_bytes()
    )
    monkeypatch.setattr(comparison, "SCENARIO_DIRECTORY", tmp_path)
    result = asyncio.run(comparison.run_case("cpu_load", max_calls=80))
    assert result["diagnosis_result"]["outcome"] == "budget_exhausted"
    assert result["diagnosis_result"]["termination_reason"] == "max_steps_reached"


def test_graph_validation_warnings_survive_comparison_result(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from app.evaluation import comparison

    original = comparison._ReplayModel.with_structured_output

    def invalid_summary(self, schema, **kwargs):
        if schema.__name__ != "Response":
            return original(self, schema, **kwargs)

        async def invoke(value, config=None):
            self.meter.reserve("Response")
            return schema(
                response="synthetic",
                diagnosis_result={
                    "outcome": "diagnosed",
                    "summary": "bad reference",
                    "observations": [{"text": "unavailable", "evidence_refs": ["foreign-ref"]}],
                },
            )

        return RunnableLambda(invoke)

    monkeypatch.setattr(comparison._ReplayModel, "with_structured_output", invalid_summary)
    result = asyncio.run(comparison.run_case("cpu_load"))
    assert result["diagnosis_result"]["outcome"] == "insufficient_evidence"
    assert "Unknown evidence reference" in result["diagnosis_result"]["validation_issues"]
    assert result["scoring"]["rules"]["valid_references"] is False


def test_private_expectations_loaded_only_after_execution_and_never_in_prompts(
    monkeypatch, tmp_path
):
    from app.evaluation import comparison

    original_directory = comparison.SCENARIO_DIRECTORY
    for name in ("scripts", "expectations"):
        (tmp_path / f"{name}.json").write_bytes((original_directory / f"{name}.json").read_bytes())
    labels = json.loads((tmp_path / "expectations.json").read_text(encoding="utf-8"))
    labels["cpu_load"]["known_cause"] = "SECRET_EXPECTATION_CANARY_95731"
    (tmp_path / "expectations.json").write_text(json.dumps(labels), encoding="utf-8")
    monkeypatch.setattr(comparison, "SCENARIO_DIRECTORY", tmp_path)
    original_read = Path.read_text
    original_score = comparison._score
    execution_finished = False

    def guarded_read(path, *args, **kwargs):
        if path.name == "expectations.json":
            assert execution_finished, "Labels read before execution completed"
        return original_read(path, *args, **kwargs)

    def score(*args, **kwargs):
        nonlocal execution_finished
        execution_finished = True
        return original_score(*args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(comparison, "_score", score)
    result = asyncio.run(comparison.run_case("cpu_load"))
    assert execution_finished
    assert "SECRET_EXPECTATION_CANARY_95731" not in json.dumps(result)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_calls": 0},
        {"max_calls": 101},
        {"timeout_seconds": 0},
        {"timeout_seconds": 301},
        {"mode": "automatic"},
        {"strategy": "other"},
    ],
)
def test_run_rejects_unsafe_parameters_before_loading_models(kwargs):
    from app.evaluation.comparison import run_case

    with pytest.raises(ValueError):
        asyncio.run(run_case("cpu_load", **kwargs))


def test_comparison_rejects_excessive_repeats_and_total_budget():
    from app.evaluation.comparison import compare_cases

    for kwargs in ({"repeats": 0}, {"repeats": 11}, {"repeats": 10, "max_calls": 100}):
        with pytest.raises(ValueError):
            asyncio.run(compare_cases(**kwargs))


def test_replay_import_and_execution_need_no_network_keys_or_database(tmp_path):
    environment = dict(os.environ)
    for key in list(environment):
        if any(part in key.upper() for part in ("API_KEY", "SECRET", "DASHSCOPE", "DATABASE_URL")):
            environment.pop(key)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    script = """
import asyncio, json, socket, sys
import app
loop = asyncio.new_event_loop()
def forbidden(*args, **kwargs):
    raise AssertionError('Network is forbidden during replay')
# Construct Windows asyncio's internal loopback socketpair before forbidding
# all further connections, including database and loopback MCP connections.
socket.socket.connect = forbidden
socket.create_connection = forbidden
from app.evaluation.scenarios import load_cases
assert 'app.core.llm_factory' not in sys.modules
from app.evaluation.comparison import run_case
result = loop.run_until_complete(run_case('cpu_load'))
loop.close()
assert 'app.tools' not in sys.modules
assert 'app.main' not in sys.modules
mcp_module = sys.modules.get('app.agent.mcp_client')
assert mcp_module is None or mcp_module._mcp_client is None
print(json.dumps({'outcome':result['diagnosis_result']['outcome']}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=40,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["outcome"] == "insufficient_evidence"


def test_live_transport_substitute_uses_same_graph_and_records_real_usage(monkeypatch):
    from langchain_core.messages import AIMessage
    from langchain_core.runnables import RunnableLambda

    from app.evaluation import comparison

    class ControlledProvider:
        model_name = "controlled-transport"

        def __init__(self):
            self.replay = comparison._ReplayModel(
                ["query_service_metrics", "query_service_logs"],
                "checkout-api",
                comparison._Meter(100, []),
            )

        def bind_tools(self, tools, **kwargs):
            async def invoke(value, config=None):
                message = await self.replay.bind_tools(tools).ainvoke(value)
                return message.model_copy(
                    update={
                        "usage_metadata": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}
                    }
                )

            return RunnableLambda(invoke)

        def with_structured_output(self, schema, **kwargs):
            assert kwargs.pop("include_raw") is True

            async def invoke(value, config=None):
                parsed = await self.replay.with_structured_output(schema, **kwargs).ainvoke(value)
                if schema.__name__ == "Response":
                    diagnosis = parsed.diagnosis_result.model_dump()
                    refs = [
                        ref
                        for observation in diagnosis["observations"]
                        for ref in observation["evidence_refs"]
                    ]
                    diagnosis.update(
                        outcome="diagnosed",
                        hypotheses=[
                            {
                                "description": "Observed synthetic load hypothesis",
                                "supporting_refs": refs,
                                "opposing_refs": [],
                                "status": "supported",
                            }
                        ],
                    )
                    parsed = schema(
                        response="controlled observation hypothesis", diagnosis_result=diagnosis
                    )
                return {
                    "raw": AIMessage(
                        content="",
                        usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
                    ),
                    "parsed": parsed,
                    "parsing_error": None,
                }

            return RunnableLambda(invoke)

    monkeypatch.setattr(comparison, "_live_model_factory", lambda **kwargs: ControlledProvider())
    result = asyncio.run(comparison.compare_cases(["cpu_load"], mode="live"))
    for run in result["runs"]:
        assert run["mode"] == "live"
        assert run["usage"]["coverage"] == 1
        assert run["usage"]["tokens"] == 5 * run["usage"]["model_calls"]
        assert run["usage"]["cost"] is None
        assert run["configuration"]["observed_models"] == ["controlled-transport"]
        assert run["diagnosis_result"]["outcome"] == "diagnosed"
        assert run["diagnosis_result"]["validation_issues"] == []


def test_live_timeout_cancels_transport_and_preserves_budget_result(monkeypatch):
    from langchain_core.runnables import RunnableLambda

    from app.evaluation import comparison

    class BlockingProvider:
        def with_structured_output(self, schema, **kwargs):
            async def invoke(value, config=None):
                await asyncio.sleep(10)

            return RunnableLambda(invoke)

    monkeypatch.setattr(comparison, "_live_model_factory", lambda **kwargs: BlockingProvider())
    result = asyncio.run(
        comparison.run_case("cpu_load", strategy="baseline", mode="live", timeout_seconds=0.2)
    )
    assert result["diagnosis_result"]["outcome"] == "budget_exhausted"
    assert result["diagnosis_result"]["termination_reason"] == "comparison_timeout"
    assert any(item.get("error") == "CancelledError" for item in result["trace"])
