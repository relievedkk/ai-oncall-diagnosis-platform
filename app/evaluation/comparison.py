"""Bounded synthetic experiments. Replay exercises the graph, never measures accuracy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from app.evaluation.scenarios import SCENARIO_DIRECTORY, load_case, load_cases

MAX_TOTAL_CALLS = 1200
MAX_TOTAL_SECONDS = 600


class ComparisonBudgetExceeded(RuntimeError):
    """Local experiment quota, independent of provider error messages."""


def _validate(*, strategy="agent", mode="replay", max_calls=24, timeout_seconds=60):
    if strategy not in {"agent", "baseline"} or mode not in {"replay", "live"}:
        raise ValueError("strategy must be agent/baseline; mode must be replay/live")
    if type(max_calls) is not int or not 1 <= max_calls <= 100:
        raise ValueError("max_calls must be between 1 and 100")
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 300
    ):
        raise ValueError("timeout_seconds must be finite and between 0 and 300")


class _Meter:
    def __init__(self, limit, trace):
        self.limit, self.trace = limit, trace
        self.calls = 0
        self.observed_usage = []
        self.observed_models = set()
        self.exhausted = False

    def reserve(self, kind):
        if self.calls >= self.limit:
            self.exhausted = True
            raise ComparisonBudgetExceeded("Experiment model-call limit reached")
        self.calls += 1
        event = {"kind": "model", "call": self.calls, "operation": kind}
        self.trace.append(event)
        return event

    def usage(self, value):
        usage = getattr(value, "usage_metadata", None)
        if not usage:
            metadata = getattr(value, "response_metadata", {}) or {}
            usage = metadata.get("token_usage")
        if isinstance(usage, dict) and isinstance(usage.get("total_tokens"), int):
            self.observed_usage.append(deepcopy(usage))

    def result(self, mode):
        observed = len(self.observed_usage)
        return {
            "model_calls": self.calls,
            "measurement": "scripted_replay" if mode == "replay" else "provider_observed",
            "tokens": (
                sum(item["total_tokens"] for item in self.observed_usage) if observed else None
            ),
            "cost": None,
            "observed_usage": self.observed_usage,
            "observed_calls": observed,
            "coverage": observed / self.calls if self.calls else 0,
            "tokens_complete": bool(self.calls and observed == self.calls),
        }


class _MeteredModel:
    """Meter both tool-bound and structured calls, preserving provider usage."""

    def __init__(self, model, meter):
        self.model, self.meter = model, meter
        identity = getattr(model, "model_name", None) or getattr(model, "model", None)
        if isinstance(identity, str):
            meter.observed_models.add(identity)

    def _runnable(self, target, operation, structured=False):
        from langchain_core.runnables import RunnableLambda

        async def invoke(value, config=None):
            event = self.meter.reserve(operation)
            try:
                result = await target.ainvoke(value, config=config)
            except asyncio.CancelledError:
                event["error"] = "CancelledError"
                raise
            except Exception as exc:
                event["error"] = type(exc).__name__
                raise
            if structured and isinstance(result, dict) and "raw" in result:
                self.meter.usage(result["raw"])
                if result.get("parsing_error"):
                    raise ValueError("Provider structured response could not be parsed")
                return result["parsed"]
            self.meter.usage(result)
            return result

        return RunnableLambda(invoke)

    def bind_tools(self, tools, **kwargs):
        return self._runnable(self.model.bind_tools(tools, **kwargs), "executor")

    def with_structured_output(self, schema, **kwargs):
        return self._runnable(
            self.model.with_structured_output(schema, include_raw=True, **kwargs),
            schema.__name__,
            structured=True,
        )


class _ReplayModel:
    """Only scripts queries; final response describes observed evidence conservatively."""

    def __init__(self, sequence, target_service, meter):
        self.sequence, self.target_service, self.meter = sequence, target_service, meter

    def bind_tools(self, tools, **kwargs):
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.runnables import RunnableLambda

        async def invoke(value, config=None):
            self.meter.reserve("executor")
            if isinstance(value[-1], ToolMessage):
                return AIMessage(content="合成工具观测：" + str(value[-1].content))
            task = str(value[-1].content)
            name = next((name for name in self.sequence if name in task), None)
            if name is None:
                raise ValueError("Replay step must name an allowlisted query")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": {"service_name": self.target_service},
                        "id": str(uuid4()),
                    }
                ],
            )

        return RunnableLambda(invoke)

    def with_structured_output(self, schema, **kwargs):
        from langchain_core.runnables import RunnableLambda

        async def invoke(value, config=None):
            self.meter.reserve(schema.__name__)
            if schema.__name__ == "Plan":
                return schema(steps=[f"使用 {name} 查询指定服务观测" for name in self.sequence])
            if schema.__name__ == "Act":
                return schema(action="continue")
            messages = value.to_messages() if hasattr(value, "to_messages") else value
            from app.core.evidence import valid_evidence

            evidence = []
            for message in messages:
                content = str(getattr(message, "content", ""))
                # The final production summarizer supplies its current task evidence,
                # including the actual refs. Scripts never manufacture identities.
                if "原始工具证据" in content:
                    start = content.find("[")
                    if start >= 0:
                        try:
                            decoded, _ = json.JSONDecoder().raw_decode(content[start:])
                            if isinstance(decoded, list):
                                evidence = decoded
                        except ValueError:
                            pass
            observations = [
                {
                    "text": "合成观测：" + str(item.get("result", ""))[:1200],
                    "evidence_refs": [item["evidence_ref"]],
                }
                for item in evidence
                if item.get("evidence_ref") and valid_evidence(item)
            ]
            diagnosis = {
                "schema_version": 1,
                "outcome": "insufficient_evidence",
                "summary": "合成回放仅复述工具观测，未验证根因语义。",
                "observations": observations,
                "hypotheses": [],
                "missing_evidence": ["需要人工结合观测确认因果关系"],
                "next_checks": ["核对同一时间与实例的指标、日志和变更记录"],
                "limitations": ["synthetic_fixture：合成输入；脚本回放不代表真实模型效果"],
            }
            return schema(response="合成回放观测，根因待人工复核。", diagnosis_result=diagnosis)

        return RunnableLambda(invoke)


def _live_model_factory(**kwargs):
    """Imported and called only after the caller explicitly selects live mode."""
    from app.core.llm_factory import llm_factory

    return llm_factory.create_chat_model(**kwargs)


def _fixture_tools(case, trace, captured):
    from langchain_core.tools import StructuredTool

    tools = []
    for name, fixture in case["tools"].items():

        def make_query(tool_name, tool_fixture):
            async def query(service_name: str = "checkout-api", query: str = "") -> dict:
                if service_name != case["target_service"]:
                    raise ValueError("Synthetic fixture is limited to its target service")
                result = deepcopy(tool_fixture)
                trace.append(
                    {
                        "kind": "tool",
                        "tool": tool_name,
                        "arguments": {"service_name": service_name, "query": query},
                        "result": result,
                    }
                )
                captured.append(
                    {
                        "evidence_ref": str(uuid4()),
                        "source": tool_name,
                        "evidence_type": (
                            "metric"
                            if "metrics" in tool_name
                            else (
                                "log"
                                if "logs" in tool_name
                                else "alert" if "alerts" in tool_name else "knowledge"
                            )
                        ),
                        "result": result,
                        "success": result.get("success") is True,
                        "service_name": service_name,
                        "provenance": "synthetic_fixture",
                    }
                )
                return result

            return query

        tools.append(
            StructuredTool.from_function(
                coroutine=make_query(name, fixture),
                name=name,
                description=f"Read synthetic {name} for the specified service only.",
            )
        )
    return tools


def _score(case_id, result, evidence):
    # Deliberately read labels AFTER all model/tool execution has finished.
    expected = json.loads((SCENARIO_DIRECTORY / "expectations.json").read_text(encoding="utf-8"))[
        case_id
    ]
    by_ref = {item.get("evidence_ref"): item for item in evidence}
    support = {
        ref for item in result.get("hypotheses", []) for ref in item.get("supporting_refs", [])
    }
    support.update(
        ref for item in result.get("observations", []) for ref in item.get("evidence_refs", [])
    )
    opposing = {
        ref for item in result.get("hypotheses", []) for ref in item.get("opposing_refs", [])
    }
    supported_types = {by_ref[ref].get("evidence_type") for ref in support if ref in by_ref}
    opposing_types = {by_ref[ref].get("evidence_type") for ref in opposing if ref in by_ref}
    checks = {
        "outcome_matches": result["outcome"] in expected["acceptable_outcomes"],
        "valid_references": not result.get("validation_issues"),
        "required_support_covered": set(expected["required_support_types"]) <= supported_types,
        "required_counterevidence_covered": set(expected["required_counterevidence_types"])
        <= opposing_types,
        "no_unsupported_diagnosis": not expected["forbid_diagnosed"]
        or result["outcome"] != "diagnosed",
    }
    return {
        "rules": checks,
        "rules_passed": sum(checks.values()),
        "rules_total": len(checks),
        "failures": [name for name, passed in checks.items() if not passed],
        "review_status": expected["review_status"],
        "reviewer": expected["reviewer"],
        "reviewed_at": expected["reviewed_at"],
        "human_scores": expected["human_scores"],
        "review_checklist": expected["review_checklist"],
        "source": "synthetic",
    }


async def run_case(
    case_id: str, *, strategy="agent", mode="replay", max_calls=24, timeout_seconds=60
) -> dict:
    """Execute one isolated graph/baseline against exactly the same input fixture."""
    _validate(strategy=strategy, mode=mode, max_calls=max_calls, timeout_seconds=timeout_seconds)
    case = load_case(case_id)
    # Heavy graph/provider imports stay behind the entrypoint and validation.
    from app.agent.aiops.runtime import DiagnosisRuntime, diagnosis_runtime
    from app.core.diagnosis_result import normalize_result, reference_evidence, render_result

    trace, captured, evidence, errors = [], [], [], []
    meter = _Meter(max_calls, trace)
    tools = _fixture_tools(case, trace, captured)
    sequence = json.loads((SCENARIO_DIRECTORY / "scripts.json").read_text(encoding="utf-8"))[
        case_id
    ]["query_sequence"]

    def factory(**kwargs):
        return (
            _ReplayModel(sequence, case["target_service"], meter)
            if mode == "replay"
            else _MeteredModel(_live_model_factory(**kwargs), meter)
        )

    async def no_knowledge(query):
        return ""

    scope = "synthetic-" + uuid4().hex
    runtime = DiagnosisRuntime(
        context="replay" if mode == "replay" else "evaluation",
        task_scope=scope,
        target_service=case["target_service"],
        tools=tools,
        model_factory=factory,
        knowledge_lookup=no_knowledge,
    )
    task = case["task"] + "\n服务目录：" + json.dumps(case["service_catalog"], ensure_ascii=False)
    final = {}
    started = time.perf_counter()
    authority, reason = None, ""

    async def execute():
        nonlocal final, evidence
        if strategy == "agent":
            from langgraph.checkpoint.memory import MemorySaver

            from app.services.aiops_service import AIOpsService

            service = AIOpsService()
            service.graph = service._build_graph(MemorySaver())
            async for event in service.execute(task, session_id=scope):
                event_type = event.get("type")
                node = (
                    "planner"
                    if event_type == "plan"
                    else (
                        "executor"
                        if event_type == "step_complete"
                        else (
                            "replanner"
                            if event_type == "report" or event.get("stage") == "replanner"
                            else None
                        )
                    )
                )
                trace.append({"kind": "graph", "node": node, "event": event})
                evidence.extend(event.get("tool_evidence", []))
                if event.get("diagnosis_result"):
                    final = event
                if event_type == "error":
                    errors.append(event.get("message", "workflow error"))
            checkpoint = await service.graph.aget_state({"configurable": {"thread_id": scope}})
            if checkpoint and checkpoint.values:
                evidence = list(checkpoint.values.get("evidence", evidence))
                final = {**final, **checkpoint.values}
        else:
            from app.agent.aiops.replanner import _generate_response

            steps = []
            for name in ("query_service_metrics", "query_service_logs"):
                tool = next(tool for tool in tools if tool.name == name)
                response = await tool.ainvoke({"service_name": case["target_service"]})
                steps.append((name, json.dumps(response, ensure_ascii=False)))
            evidence = reference_evidence(captured, scope)
            final = await _generate_response(
                {
                    "input": task,
                    "past_steps": steps,
                    "evidence": evidence,
                    "task_scope": scope,
                    "target_service": case["target_service"],
                },
                factory(temperature=0, streaming=False),
            )

    with diagnosis_runtime(runtime):
        try:
            await asyncio.wait_for(execute(), timeout=timeout_seconds)
        except TimeoutError:
            authority, reason = "budget_exhausted", "comparison_timeout"
            errors.append("Experiment time limit reached")
        except ComparisonBudgetExceeded:
            meter.exhausted = True
        except Exception as exc:
            authority, reason = "dependency_failed", "comparison_dependency_failed"
            errors.append(f"{type(exc).__name__}: {exc}")
        if meter.exhausted:
            authority, reason = "budget_exhausted", "comparison_model_call_limit"
        if not evidence:
            evidence = reference_evidence(captured, scope)
        if authority is None and final.get("authority_outcome") in {
            "budget_exhausted",
            "dependency_failed",
            "no_active_alerts",
        }:
            authority = final["authority_outcome"]
            reason = final.get("termination_reason", "")
        if authority is None and (errors or any(item.get("success") is False for item in captured)):
            authority, reason = "dependency_failed", "comparison_dependency_failed"
        raw = final.get("diagnosis_result")
        reason = reason or final.get("termination_reason", "")
        if authority is None and reason == "no_active_alerts":
            authority = "no_active_alerts"
        # These are trusted outputs of the shared production validator, not raw
        # model fields. Revalidating would discard its program-generated warnings
        # and mistake its authoritative terminal outcomes for model assertions.
        if isinstance(raw, dict) and authority is None:
            result = deepcopy(raw)
        else:
            result = normalize_result(
                raw,
                evidence,
                task_scope=scope,
                target_service=case["target_service"],
                context=runtime.context,
                authority_outcome=authority,
                termination_reason=reason,
            )
            if isinstance(raw, dict):
                result["validation_issues"] = list(
                    dict.fromkeys([*raw.get("validation_issues", []), *result["validation_issues"]])
                )[:32]
    output = {
        "run_id": scope,
        "case_id": case_id,
        "strategy": strategy,
        "mode": mode,
        "source": "synthetic",
        "semantic_accuracy_measured": False,
        "diagnosis_result": result,
        "report": render_result(result),
        "evidence": evidence,
        "trace": trace,
        "usage": meter.result(mode),
        "elapsed_seconds": time.perf_counter() - started,
        "errors": errors,
        "input_digest": hashlib.sha256(
            json.dumps(case, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "configuration": {
            "temperature": 0,
            "seed": None,
            "max_calls": max_calls,
            "timeout_seconds": timeout_seconds,
            "schema_version": 1,
            "model": "scripted_observations" if mode == "replay" else "configured_provider",
            "observed_models": sorted(meter.observed_models),
            "baseline_order": ["query_service_metrics", "query_service_logs"],
        },
        "limitations": [
            (
                "Replay call counts and elapsed time are local control-flow measurements."
                if mode == "replay"
                else "Live model evaluated only on synthetic tool observations."
            ),
            "Root-cause semantics and recommendation quality require human review.",
        ],
    }
    output["scoring"] = _score(case_id, result, evidence)
    return output


def _summary(runs, mode):
    strategies = {}
    for strategy in ("agent", "baseline"):
        selected = [run for run in runs if run["strategy"] == strategy]
        calls = sum(run["usage"]["model_calls"] for run in selected)
        observed = sum(run["usage"]["observed_calls"] for run in selected)
        strategies[strategy] = {
            "sample_count": len(selected),
            "model_calls": calls,
            "measurement": "scripted_replay" if mode == "replay" else "provider_observed",
            "elapsed_seconds": sum(run["elapsed_seconds"] for run in selected),
            "rules_passed": sum(run["scoring"]["rules_passed"] for run in selected),
            "rules_total": sum(run["scoring"]["rules_total"] for run in selected),
            "tokens": sum(run["usage"]["tokens"] or 0 for run in selected) if observed else None,
            "usage_coverage": observed / calls if calls else 0,
            "cost": None,
        }
    return {
        "mode": mode,
        "source": "synthetic",
        "semantic_accuracy_measured": False,
        "sample_count": len(runs),
        "case_count": len({run["case_id"] for run in runs}),
        "human_review_coverage": (
            sum(run["scoring"]["review_status"] == "reviewed" for run in runs) / len(runs)
            if runs
            else 0
        ),
        "rules_passed": sum(run["scoring"]["rules_passed"] for run in runs),
        "rules_total": sum(run["scoring"]["rules_total"] for run in runs),
        "runs": runs,
        "strategies": strategies,
        "limitations": [
            "Rule gates are not semantic root-cause accuracy.",
            "No claim that Agent outperforms baseline; all runs retained.",
        ],
    }


async def _compare(
    case_ids=None,
    *,
    mode="replay",
    repeats=1,
    max_calls=24,
    timeout_seconds=60,
    output_directory: Path | None = None,
):
    _validate(mode=mode, max_calls=max_calls, timeout_seconds=timeout_seconds)
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise ValueError("repeats must be between 1 and 10")
    selected = [case["case_id"] for case in load_cases()] if case_ids is None else list(case_ids)
    if not selected or len(selected) > 12 or len(set(selected)) != len(selected):
        raise ValueError("Select 1 to 12 distinct built-in cases")
    for case_id in selected:
        load_case(case_id)
    if len(selected) * 2 * repeats * max_calls > MAX_TOTAL_CALLS:
        raise ValueError("Comparison exceeds total model-call safety limit")
    runs = []
    deadline = time.perf_counter() + MAX_TOTAL_SECONDS
    for repeat in range(repeats):
        for case_id in selected:
            for strategy in ("agent", "baseline"):
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    return {
                        **_summary(runs, mode),
                        "termination_reason": "comparison_total_timeout",
                    }
                run = await run_case(
                    case_id,
                    strategy=strategy,
                    mode=mode,
                    max_calls=max_calls,
                    timeout_seconds=min(timeout_seconds, remaining),
                )
                run["repeat"] = repeat + 1
                runs.append(run)
                if output_directory is not None:
                    output_directory.mkdir(parents=True, exist_ok=True)
                    (
                        output_directory / f"{case_id}-{strategy}-{repeat + 1}-{run['run_id']}.json"
                    ).write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    return _summary(runs, mode)


async def compare_cases(
    case_ids=None, *, mode="replay", repeats=1, max_calls=24, timeout_seconds=60
) -> dict:
    """Run both strategies, bounded per call and across the entire experiment."""
    return await _compare(
        case_ids, mode=mode, repeats=repeats, max_calls=max_calls, timeout_seconds=timeout_seconds
    )
