# Diagnosis Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans; independent contract, evaluation and demo tasks may use superpowers:dispatching-parallel-agents with exclusive file ownership. Steps use checkbox syntax.

**Goal:** Deliver validated structured results, 12 synthetic cases, an honest baseline comparison and an offline replay demo.

**Architecture:** Extend the current graph/events/storage without replacing Markdown or queue states. Pure report contracts are shared by production and evaluation. Context-local runtime injection supplies tools/models for evaluation without altering production singletons.

**Tech Stack:** Existing Python/Pydantic/LangGraph/PostgreSQL, browser JavaScript and standard-library loopback demo server.

**Spec:** `docs/superpowers/specs/2026-10-26-diagnosis-quality-and-replay-design.md` (approved in chat).

## Global Constraints

- `schema_version=1`; outcomes `diagnosed`, `insufficient_evidence`, `budget_exhausted`, `dependency_failed`, `no_active_alerts`, `legacy_unverified`.
- Runtime context is `production`, `replay`, or `evaluation`, set by code rather than model output.
- Existing ownership, authentication, redaction, retry, SSE and Dry Run boundaries remain intact.
- No paid live experiment, production deployment or real remediation as part of acceptance.
- Synthetic expectations never enter model/tool inputs; unreviewed cases do not count as human-verified accuracy.

## Review Focus

- Cross-task/stale evidence references cannot support a diagnosis (Task 1).
- A replay/evaluation context cannot submit remediation (Task 2).
- Budget/timeout failure remains authoritative even if the model report is invalid (Task 2).
- Reconnection after report event loss retains structured outcome (Task 3).
- Demo works without keys and rejects remote binding and arbitrary file routes (Task 5).

## Task 1: Pure diagnosis contracts

**Files:** create `app/core/diagnosis_result.py`, `tests/test_diagnosis_result.py`.

**Interfaces:** `reference_evidence(evidence: list[dict], task_scope: str) -> list[dict]`; `normalize_result(raw: dict | None, evidence: list[dict], *, task_scope: str, target_service: str = '', context: str = 'production', termination_reason: str = '', authority_outcome: str | None = None) -> dict`; `render_result(result: dict) -> str`. `DiagnosisResult` is the bounded Pydantic schema. Raw claims use `text/evidence_refs`; hypotheses use `description/supporting_refs/opposing_refs/status`.

- [x] Write adversarial behavior tests, run RED, implement contracts and deterministic Markdown, run GREEN.
- [x] Cover missing/foreign/conflicting/failed/demo/knowledge-only refs, synthetic context isolation, legacy input, authoritative failures and safe rendering.

## Task 2: Runtime injection and graph integration

**Files:** create `app/agent/aiops/runtime.py`; modify Planner/Executor/Replanner/state and `app/services/aiops_service.py`, `app/services/diagnosis_runner.py`; add runtime/result pipeline tests.

**Interfaces:** `DiagnosisRuntime(context='production', task_scope='', target_service='', tools=None, model_factory=None)` and `diagnosis_runtime(runtime)` context manager; `current_runtime()`, `async resolve_tools(default_loader)` and `create_model(**kwargs)` preserve existing production lookup when unset. Evaluation may explicitly inject knowledge lookup; no global model/tool monkeypatches. Live comparison runs a fresh `AIOpsService` with in-memory saver under this runtime.

- [x] Write/run RED tests for no-key import, isolated concurrent runtimes, structured events, budget partials and no remediation in non-production.
- [x] Lazy-load production-only tools, attach fresh refs to evidence, reset new state on fresh runs, normalize final results and propagate them on all report/complete/error paths.
- [x] Keep deterministic no-alert output; use typed budget exceptions and propagate original failure/retry behavior.
- [x] Run graph/ownership/budget regressions GREEN.

## Task 3: Durable and browser compatibility

**Files:** modify report model, both repositories, stream service and `static/diagnosis-stream.js`; create migration `20261026_0012`; add SQL and Node behavior tests.

**Interfaces:** report JSON column `structured_result`; public/event key `diagnosis_result`; retain `report/response` strings.

- [x] Write/run RED tests for SQL persistence, stream terminal reconstruction and browser outcome persistence across reconnection.
- [x] Implement nullable migration, serialization and event propagation without expanding list evidence queries.
- [x] Add result labels to stream report rendering; legacy is explicitly unverified; maintain account isolation.
- [x] Verify migrations/rollback on disposable PostgreSQL and affected regressions GREEN.

## Task 4: Cases and evaluation comparison

**Files:** create `evals/scenarios/v1/{inputs,expectations,scripts}.json`, `app/evaluation/scenarios.py`, `app/evaluation/comparison.py`, `app/evaluation/compare_cli.py`, tests; preserve old evaluation CLI.

**Interfaces:** `load_cases() -> list[dict]` returns inputs only; `load_case(case_id: str) -> dict`; `async run_case(case_id: str, *, strategy='agent', mode='replay', max_calls=24, timeout_seconds=60) -> dict`; `async compare_cases(case_ids=None, *, mode='replay', repeats=1, max_calls=24, timeout_seconds=60) -> dict`. Outputs include `diagnosis_result`, `report`, `trace`, calls/usage coverage, run mode and explicit semantic limitations. Review scoring is loaded only after execution. Runner uses Task 2 runtime injection and Task 1 contracts.

- [x] RED tests: 12 diverse cases, expectation canaries absent from runtime inputs, honest NULL human scores/usage, failure outcomes, bounded runs, same input fixture for both strategies.
- [x] Implement replay scripted model through actual graph, live opt-in through real graph and same final summarizer as fixed metrics/log baseline; save per-run artifacts and review rubric.
- [x] Prove no-key/no-provider/no-DB replay in subprocess; verify live transport injection with a controlled model and run GREEN.

## Task 5: Offline demo and documentation

**Files:** create `app/demo/{__init__,__main__,server}.py`, static demo assets, tests and usage docs; update README.

**Interfaces:** loopback-only `build_server(port=9911)` serving `/`, `/api/cases`, `/api/replay?case_id=...&strategy=...`; uses Task 4 replay only. Browser renders data with textContent; no arbitrary paths/URLs or live mode through HTTP.

- [x] RED tests for route traversal rejection, non-loopback binding, no secrets, and actual replay HTTP result.
- [x] Implement bounded replay-only server and readable case/trace/result UI; document installation, mode limits, CLI and stopping.
- [x] Smoke-test no-env subprocess and visually inspect demo; run GREEN.

## Final acceptance

- [x] Fresh whole-change review and address actionable defects with regression tests.
- [x] Full Python with disposable migrated PostgreSQL, Node, Ruff/Bandit, audit and Compose checks.
- [x] Preserve old CLI, no-key clean-checkout demo, replay comparison artifacts; do not run paid live experiments.
- [x] Record exact counts, skipped integrations, remaining limits and commits; deliver work in primary checkout without losing user changes. Remote push only if separately authorized for this feature.
