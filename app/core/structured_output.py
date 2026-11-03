"""Value-free, bounded diagnostics for untrusted structured model outputs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError


class SchemaIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: Literal["missing_field", "extra_field", "invalid_enum", "wrong_type", "contract", "parse"]
    path: str


_SAFE_FIELDS = frozenset(
    "action services mechanisms view justification_refs schema_version outcome summary "
    "root_cause_service fault_type cause_evidence_refs diagnosis_complete candidate_comparisons "
    "affected_services observations hypotheses missing_evidence next_checks limitations "
    "validation_issues termination_reason description service supporting_refs opposing_refs "
    "status text evidence_refs".split()
)


def safe_schema_issues(error: BaseException) -> tuple[SchemaIssue, ...]:
    if isinstance(error, StructuredOutputError):
        return error.issues
    if not isinstance(error, ValidationError):
        return (SchemaIssue(code="parse", path="<output>"),)
    issues = []
    for item in error.errors(include_input=False, include_context=False, include_url=False)[:16]:
        kind = item["type"]
        code = {
            "missing": "missing_field",
            "extra_forbidden": "extra_field",
            "literal_error": "invalid_enum",
            "enum": "invalid_enum",
            "value_error": "contract",
        }.get(kind, "wrong_type")
        # Arbitrary extra keys and exception messages can themselves contain secrets.
        path = (
            ".".join(
                str(part)
                if isinstance(part, int) and 0 <= part < 128
                else part
                if isinstance(part, str) and part in _SAFE_FIELDS
                else "<extra>"
                for part in item.get("loc", ())[:8]
            )
            or "<output>"
        )
        issues.append(SchemaIssue(code=code, path=path))
    return tuple(issues) or (SchemaIssue(code="parse", path="<output>"),)


class StructuredOutputError(ValueError):
    def __init__(self, issues: tuple[SchemaIssue, ...]):
        self.issues = issues[:16]
        super().__init__("structured_output_invalid")
