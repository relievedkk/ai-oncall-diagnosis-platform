"""Deterministic quality gates for generated diagnosis reports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


def _normalize(value: Any) -> str:
    return " ".join(str(value).casefold().split())


@dataclass(frozen=True)
class DiagnosisCase:
    id: str
    alert_name: str
    root_cause_term_groups: list[list[str]]
    required_evidence_types: list[str]
    required_report_terms: list[str]
    forbidden_terms: list[str] = field(default_factory=list)
    minimum_score: float = 0.8

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> DiagnosisCase:
        return cls(
            id=str(value["id"]),
            alert_name=str(value["alert_name"]),
            root_cause_term_groups=[
                [str(term) for term in group] for group in value["root_cause_term_groups"]
            ],
            required_evidence_types=[str(item) for item in value["required_evidence_types"]],
            required_report_terms=[str(item) for item in value["required_report_terms"]],
            forbidden_terms=[str(item) for item in value.get("forbidden_terms", [])],
            minimum_score=float(value.get("minimum_score", 0.8)),
        )


@dataclass(frozen=True)
class DiagnosisEvaluation:
    case_id: str
    score: float
    passed: bool
    root_cause_score: float
    evidence_score: float
    report_completeness_score: float
    groundedness_score: float
    missing_evidence_types: list[str]
    missing_report_terms: list[str]
    forbidden_terms_found: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _coverage(required: list[str], observed: set[str]) -> tuple[float, list[str]]:
    if not required:
        return 1.0, []
    missing = [item for item in required if _normalize(item) not in observed]
    return (len(required) - len(missing)) / len(required), missing


def evaluate_diagnosis(
    case: DiagnosisCase,
    report: str,
    evidence: list[dict[str, Any]],
) -> DiagnosisEvaluation:
    """Score root cause, evidence coverage, completeness, and unsupported claims."""
    report_text = _normalize(report)
    root_cause_score = float(
        any(
            all(_normalize(term) in report_text for term in group)
            for group in case.root_cause_term_groups
        )
    )

    evidence_types = {
        _normalize(item.get("evidence_type", item.get("type", ""))) for item in evidence
    }
    evidence_score, missing_evidence = _coverage(
        case.required_evidence_types,
        evidence_types,
    )

    required_terms = {_normalize(term) for term in case.required_report_terms}
    present_terms = {term for term in required_terms if term in report_text}
    completeness_score, missing_report_terms = _coverage(
        case.required_report_terms,
        present_terms,
    )

    forbidden_found = [term for term in case.forbidden_terms if _normalize(term) in report_text]
    groundedness_score = 0.0 if forbidden_found else 1.0
    score = round(
        root_cause_score * 0.4
        + evidence_score * 0.3
        + completeness_score * 0.2
        + groundedness_score * 0.1,
        4,
    )
    passed = score >= case.minimum_score and root_cause_score == 1.0 and not forbidden_found
    return DiagnosisEvaluation(
        case_id=case.id,
        score=score,
        passed=passed,
        root_cause_score=root_cause_score,
        evidence_score=round(evidence_score, 4),
        report_completeness_score=round(completeness_score, 4),
        groundedness_score=groundedness_score,
        missing_evidence_types=missing_evidence,
        missing_report_terms=missing_report_terms,
        forbidden_terms_found=forbidden_found,
    )


def evaluate_suite(
    cases: list[DiagnosisCase],
    results: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    evaluations = [
        evaluate_diagnosis(
            case,
            str(results.get(case.id, {}).get("report", "")),
            list(results.get(case.id, {}).get("evidence", [])),
        )
        for case in cases
    ]
    passed_count = sum(item.passed for item in evaluations)
    return {
        "passed": passed_count == len(evaluations),
        "pass_rate": round(passed_count / len(evaluations), 4) if evaluations else 1.0,
        "average_score": (
            round(sum(item.score for item in evaluations) / len(evaluations), 4)
            if evaluations
            else 1.0
        ),
        "cases": [item.to_dict() for item in evaluations],
    }
