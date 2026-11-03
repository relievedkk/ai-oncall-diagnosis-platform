# RCAEval Adaptive v4 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved P0–P2 reliability, evidence and audit improvements with comprehensive boundary tests.

**Architecture:** Typed baseline diagnosis first; one deterministic authorized evidence refinement and at most one second diagnosis; preserve validated fallback with separate enrichment audit. Keep protocol v3 available.

**Tech Stack:** Existing Python, Pydantic v2, LangChain, pandas, pytest and Ruff; no new dependencies.

**Spec:** docs/superpowers/specs/2026-11-03-rcaeval-adaptive-v4-design.md

## Global Constraints

Use the design's constraints verbatim: no hidden retries; at most two model calls and one cumulative deadline; strict 16 KiB evidence/authorization; no raw responses/secrets; no sealed model runs; no merge/push; preserve existing edits; freeze results before truth.

## Review Focus

- Parse failures must retain observed usage without leaking arbitrary field names or exception values (Task 1).
- Baseline-missing telemetry is partial observation, not a diagnosed cause (Task 2).
- Refinement cannot clear ambiguity with a forged flag or an earlier onset within an overlapping interval (Task 3).
- A failed or less-supported second turn preserves the first valid result and records the attempt (Task 4).
- Mixed protocol versions, overwritten outputs, and premature truth access must be rejected or explicit (Task 5).

### Task 1: Typed model outputs and safe schema diagnostics

Files: create app/core/structured_output.py and app/evaluation/rcaeval_models.py; modify app/agent/aiops/two_stage.py and app/evaluation/rcaeval.py; add tests/test_rcaeval_models.py and extend tests/test_two_stage_diagnosis.py.

Interfaces: SchemaIssue(code, path), StructuredOutputError(issues), AuditedMeteredModel(model,meter), EvaluationDiagnosisResult, run_single_stage(..., diagnosis_schema=DiagnosisResult).

- [ ] Write tests rejecting invalid fault taxonomy at every level, checking safe issue paths and usage on parser failure; run pytest and observe RED.
- [ ] Implement typed outputs/wrapper; add schema_issues to coordinator failures, explicit stage instructions and task workflow cleanup.
- [ ] Run new and existing coordinator/scenario tests; expect PASS. Commit task-owned files.

### Task 2: Partial telemetry and mechanism-scoped trends

Files: modify app/core/diagnostic_evidence.py, app/evaluation/rcaeval_evidence.py, app/evaluation/rcaeval.py; add tests/test_rcaeval_refinement.py.

Interfaces: optional MechanismProfile.coverage, finite signal summaries, mechanism-scoped CaseView.metric_trends(..., mechanisms=None).

- [ ] Write RED tests for baseline missing, post missing, nonfinite, zero/constant and all six mechanism queries.
- [ ] Preserve partial sample statistics without eligibility; filter trends and keep exact boundary semantics.
- [ ] Run evidence and protocol tests, expect PASS, then commit.

### Task 3: Authorized temporal evidence and verified ambiguity resolution

Files: create app/evaluation/rcaeval_refinement.py; modify app/core/diagnostic_evidence.py and app/agent/aiops/two_stage.py; extend tests/test_rcaeval_refinement.py and tests/test_two_stage_diagnosis.py.

Interfaces: plan_refinement(initial, diagnosis) -> EvidenceRequest | None; build_refinement_evidence(view, request, initial, max_bytes) -> SerializedEvidenceBundle; independently checked temporal certificate, policy reconciliation.

- [ ] Write RED tests for new stable refs, service/mechanism bounds, temporal separation, ties/missing competitors/common mode and forged certificate.
- [ ] Implement deterministic planner, detailed finite temporal summaries and verified policy resolution; keep original conflicts rejected.
- [ ] Run focused evidence/coordinator suites, expect PASS, then commit.

### Task 4: Baseline-first adaptive coordinator

Files: create app/agent/aiops/adaptive_diagnosis.py; extend app/agent/aiops/two_stage.py only for reusable typed diagnosis invocation; add tests/test_adaptive_diagnosis.py.

Interfaces: AdaptiveResult(first, final, refinement_status, enrichment_failure, fallback_used, added_refs, call_count); run_adaptive(...).

- [ ] Write RED tests for one-call complete result, two-call enrichment, no novel evidence, all failure/fallback branches, cumulative timeout, cancellation and call cap.
- [ ] Implement immutable baseline fallback and auditable result selection using strict evidence policy.
- [ ] Run new/adjoining suites, expect PASS, then commit.

### Task 5: Versioned paired runner, scoring and development audit

Files: create app/evaluation/rcaeval_v4.py; modify app/evaluation/rcaeval.py and app/evaluation/rcaeval_scoring.py; add tests/test_rcaeval_v4.py.

Interfaces: run_protocol_v4_case, summarize_v4, audit_development; CLI --protocol-version {3,4} default 4.

- [ ] Write RED tests for truth ordering, explicit v4 audit, paired gains/losses, strata, isolated sealed answers and non-overwrite guard.
- [ ] Integrate audited wrapper, coordinator, versioned metrics and no-model development coverage/ceiling audit.
- [ ] Run protocol/scoring suites, expect PASS, then commit.

### Task 6: Whole-system verification and coverage report

Files: create docs/rcaeval-adaptive-v4-validation.md; fresh .runtime/rcaeval validation/audit outputs only.

- [ ] Run all task suites, full pytest, targeted Ruff/format and diff check. Expect no newly introduced failure.
- [ ] Run 54-case no-model validation and 18-case development audit; check 0 model calls and 36 sealed/unrun.
- [ ] Fresh whole-change reviewer; resolve important findings with RED→GREEN regressions.
- [ ] Record real test/coverage evidence and limits, commit documentation, retain branch for user review.
