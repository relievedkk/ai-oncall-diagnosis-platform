"""Evaluation taxonomy and metering that never discards safe parse diagnostics."""

import asyncio
from typing import Literal

from langchain_core.runnables import RunnableLambda
from pydantic import Field

from app.core.diagnosis_result import DiagnosisResult, Hypothesis
from app.core.structured_output import StructuredOutputError, safe_schema_issues
from app.evaluation.comparison import _MeteredModel

EvaluationFault = Literal["cpu", "mem", "delay", "disk", "loss", "socket", "unknown"]


class EvaluationHypothesis(Hypothesis):
    fault_type: EvaluationFault = "unknown"


class EvaluationDiagnosisResult(DiagnosisResult):
    """Choose a single supported mechanism or unknown; never invent a fault label."""

    fault_type: EvaluationFault = "unknown"
    hypotheses: list[EvaluationHypothesis] = Field(default_factory=list, max_length=16)
    candidate_comparisons: list[EvaluationHypothesis] = Field(default_factory=list, max_length=8)


class AuditedMeteredModel(_MeteredModel):
    def with_structured_output(self, schema, **kwargs):
        target = self.model.with_structured_output(schema, include_raw=True, **kwargs)

        async def invoke(value, config=None):
            event = self.meter.reserve(schema.__name__)
            try:
                result = await target.ainvoke(value, config=config)
                if isinstance(result, dict) and "raw" in result:
                    self.meter.usage(result["raw"])
                    if result.get("parsing_error") is not None:
                        issues = safe_schema_issues(result["parsing_error"])
                        event["schema_issues"] = [issue.model_dump() for issue in issues]
                        raise StructuredOutputError(issues)
                    return result.get("parsed")
                self.meter.usage(result)
                return result
            except asyncio.CancelledError:
                event["error"] = "CancelledError"
                raise
            except Exception as exc:
                event["error"] = type(exc).__name__
                raise

        return RunnableLambda(invoke)
