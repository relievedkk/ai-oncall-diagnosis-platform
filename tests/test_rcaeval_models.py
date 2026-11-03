import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from pydantic import ValidationError

from app.core.diagnosis_result import DiagnosisResult
from app.evaluation.comparison import _Meter


@pytest.mark.parametrize("field", ["root", "hypotheses", "candidate_comparisons"])
@pytest.mark.parametrize("fault", ["resource_contention", "memory", "cpu,disk"])
def test_evaluation_rejects_out_of_taxonomy_faults(field, fault):
    from app.evaluation.rcaeval_models import EvaluationDiagnosisResult

    payload = (
        {"fault_type": fault}
        if field == "root"
        else {field: [{"description": "observed", "fault_type": fault}]}
    )
    with pytest.raises(ValidationError):
        EvaluationDiagnosisResult.model_validate(payload)
    # The reusable production report remains capable of non-evaluation faults.
    DiagnosisResult.model_validate(payload)


@pytest.mark.parametrize("fault", ["cpu", "mem", "delay", "disk", "loss", "socket", "unknown"])
def test_evaluation_accepts_supported_faults(fault):
    from app.evaluation.rcaeval_models import EvaluationDiagnosisResult

    result = EvaluationDiagnosisResult(fault_type=fault)
    assert result.fault_type == fault


def _bad_output():
    from app.evaluation.rcaeval_models import EvaluationDiagnosisResult

    try:
        EvaluationDiagnosisResult.model_validate(
            {
                "fault_type": "secret-value",
                "password_sk_private": "secret-value",
            }
        )
    except ValidationError as error:
        return error
    raise AssertionError("invalid fixture accepted")


def test_schema_issues_do_not_leak_inputs_messages_or_extra_field_names():
    from app.core.structured_output import safe_schema_issues

    issues = safe_schema_issues(_bad_output())
    encoded = json.dumps([issue.model_dump() for issue in issues])
    assert "secret-value" not in encoded
    assert "password_sk_private" not in encoded
    assert {(i.code, i.path) for i in issues} == {
        ("invalid_enum", "fault_type"),
        ("extra_field", "<extra>"),
    }


def test_unstructured_parse_exception_is_reduced_to_fixed_code():
    from app.core.structured_output import safe_schema_issues

    issues = safe_schema_issues(ValueError("secret raw provider output"))
    assert [(i.code, i.path) for i in issues] == [("parse", "<output>")]


@pytest.mark.asyncio
async def test_audited_parser_failure_retains_usage_and_safe_cause():
    from app.core.structured_output import StructuredOutputError
    from app.evaluation.rcaeval_models import AuditedMeteredModel, EvaluationDiagnosisResult

    class Provider:
        def with_structured_output(self, schema, **kwargs):
            assert schema is EvaluationDiagnosisResult
            assert kwargs["include_raw"] is True
            return RunnableLambda(
                lambda _: {
                    "raw": AIMessage(
                        content="secret raw response",
                        usage_metadata={
                            "input_tokens": 10,
                            "output_tokens": 5,
                            "total_tokens": 15,
                        },
                    ),
                    "parsed": None,
                    "parsing_error": _bad_output(),
                }
            )

    meter = _Meter(2, [])
    wrapped = AuditedMeteredModel(Provider(), meter)
    with pytest.raises(StructuredOutputError) as caught:
        await wrapped.with_structured_output(EvaluationDiagnosisResult).ainvoke("task")
    assert meter.result("live")["tokens"] == 15
    assert meter.result("live")["tokens_complete"] is True
    assert caught.value.issues[0].code == "invalid_enum"
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_coordinator_classifies_safe_parser_error_as_model_output():
    from app.agent.aiops.two_stage import run_single_stage
    from app.core.structured_output import StructuredOutputError, safe_schema_issues
    from tests.test_two_stage_diagnosis import FakeModel, IdentityPolicy, _factory, _initial

    model = FakeModel([StructuredOutputError(safe_schema_issues(_bad_output()))])
    result = await run_single_stage(
        {}, _initial(), model_factory=_factory(model), policy=IdentityPolicy()
    )
    assert result.failure_category == "invalid_model_output"
    assert result.failure_detail_code == "diagnosis_output_invalid"
    assert result.schema_issues[0].code == "invalid_enum"
    assert result.call_count == 1
    assert "secret" not in result.model_dump_json()
