# Single-pass diagnosis implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Execute task-by-task, tests before implementation.

**Goal:** Implement the approved local evidence processing and one-request diagnosis design, including durable attempt limits and model-free feasibility gates.

**Architecture:** New, isolated extraction/ranking/packet modules feed a single structured model call. SQLite journals bind experiment+case+configuration before invocation. Protocol v5 becomes the default evaluation entry; explicit v3/v4 remain available.

**Tech Stack:** Existing Python/Pydantic/pandas/SQLite/LangChain/pytest. No new dependencies.

**Spec:** docs/superpowers/specs/2026-11-03-single-pass-diagnosis-design.md

## Global Constraints

- At most one real request per experiment/case, no retry/repair/summary calls; unknown reservations block resend after restart.
- Fixed visible incident +/-300 seconds, clipped to available data; timestamps/units/missingness explicit.
- 16 KiB hard packet+transport boundary; ~5000 total tokens soft target, not a claimed measurement.
- No paid calls or sealed36 answer access during implementation. Candidate generation never receives truth.
- Only task files staged; preserve existing user changes. No merge/push.

## Review Focus

- Nested/parallel child spans and duplicate IDs must not invent self-time or direction (Task 2).
- A large anomaly or log keyword alone must not grant mechanism eligibility (Task 3).
- Compression must preserve all selected supporting and opposing evidence or fail before a model call (Task 4).
- Concurrent processes, restart, timeout/cancellation, malformed results and changed configs must never reset an attempt (Task 5).
- Candidate recall and evidence sufficiency are separate; development-only scope is checked before any labels load (Task 6).

### Task 1: Scoped finite metric evidence
Files: app/core/single_pass.py; app/evaluation/single_pass_metrics.py; tests/test_single_pass_metrics.py.
Interfaces: ObservationWindow, Fact, EvidenceSet, Hypothesis; metric_facts(view, window, semantics=None).
- [ ] RED: fixed window/no-future leakage, zero/missing baseline, nonfinite values, sustained onset, workload/error, explicit counters/reset.
- [ ] Implement finite robust summaries, coverage and stable case/window refs; do not infer unknown units.
- [ ] Focused tests PASS; commit task files.

### Task 2: Local log events and trace paths
Files: app/evaluation/single_pass_events.py; app/evaluation/single_pass_traces.py; tests/test_single_pass_sources.py.
Interfaces: log_facts(view, window), trace_facts(view, window, status_semantics=None).
- [ ] RED: deduplicated event buckets, windowing/service identity, untrusted/injection logs, absent sources; same-trace parent join, parallel interval union, orphan/duplicate/missing status.
- [ ] Implement template/event counts and bounded redacted representatives; trace coverage and trustworthy path/self-time approximations.
- [ ] Source and metric suites PASS; commit.

### Task 3: Hypothesis contracts and local ranking
Files: app/evaluation/single_pass_hypotheses.py; tests/test_single_pass_hypotheses.py.
Interfaces: collect_evidence(view, incident=None), rank_hypotheses(evidence), supported_service(evidence, service).
- [ ] RED: six mechanism positive/negative cases, workload-driven symptoms, direct versus keyword observations, competition and partial service localization.
- [ ] Implement deterministic score plus independent eligibility and competing explanations; stable source refs, no truth input.
- [ ] Evidence suites PASS; commit.

### Task 4: Compact packet and strict one-shot output
Files: app/evaluation/single_pass_packet.py; app/agent/aiops/single_pass.py; tests/test_single_pass_packet.py.
Interfaces: build_packet(evidence, hypotheses, max_bytes=16384), SinglePassDecision, validate_decision(packet, decision), render_report.
- [ ] RED: required support/opposition preservation, boundary overflow, aliases and cross-service refs, unknown/partial decisions, no forced winner.
- [ ] Implement aliases, compact comparisons, strict short output and deterministic report; optional excerpts trimmed before material evidence.
- [ ] Packet/extraction tests PASS; commit.

### Task 5: Durable one-attempt coordinator
Files: app/evaluation/single_pass_journal.py; app/agent/aiops/single_pass.py; tests/test_single_pass_execution.py.
Interfaces: AttemptJournal.reserve/load/complete; run_single_pass(packet, journal, experiment_id, config, model_factory).
- [ ] RED: concurrency/restart, config mismatch, cached result, parser/provider error, cancellation/timeout, no second call and observed usage.
- [ ] Persist reservation before invocation, never reset, cache validated result, safe error summaries and isolated evaluation runtime.
- [ ] Coordinator and model-boundary suites PASS; commit.

### Task 6: Single-only CLI, development audit and full validation
Files: app/evaluation/single_pass_runner.py; app/evaluation/rcaeval.py; tests/test_single_pass_runner.py; docs/single-pass-validation.md.
Interfaces: scoped audit/report + --mode validate/live; existing RCAEval defaults route to protocol5, explicit older protocols unchanged.
- [ ] RED: no labels in extraction, gate17/18 candidate+discrimination separately, ablations/time perturbations, one strategy only, overwrite/resume and missing deps.
- [ ] Implement v5-only records, exact model/packet/schema/window revisions, workload/missing-source strata, coverage/feasibility flags and live gate.
- [ ] Run focused/full pytest, targeted Ruff, no-model 18-case scoped audit; independent final reviewer and fix important findings RED→GREEN.
- [ ] Commit verification report with actual results and limitations. Stop before paid runs when gates are unmet.
