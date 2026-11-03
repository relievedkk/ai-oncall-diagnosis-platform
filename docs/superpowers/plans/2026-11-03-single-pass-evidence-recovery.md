# Single-pass evidence recovery implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implement inline, with tests first and one final independent review.

**Goal:** Execute the user's approved sequence: verify CPU semantics, explain all18 failures, repair retrieval/packet losses and rerun local feasibility before any paid experiment.

**Architecture:** A curated, source-backed telemetry profile feeds the existing extractor without truth. A post-freeze evaluator builds explicit failure attributions. Label-free retrieval and lossless serialization recover avoidable losses, while diagnosis eligibility and persistent request limits remain independent.

**Tech Stack:** Existing Python, pandas, Pydantic, SQLite and pytest; no new dependencies.

**Spec:** docs/superpowers/specs/2026-11-03-single-pass-diagnosis-design.md; approved follow-up sequence in conversation.

## Global Constraints

- No real provider requests or remaining36 answer access in this implementation. Preserve all original dirty changes, current branch, and recorded baseline.
- Fixed +/-300s observations and +/-30/60s local perturbations, <=5重点hypotheses,16KiB hard transport, no material support/opposition loss.
- Never infer percentage, CPU capacity, raw counter/gauge semantics, network loss or trace success from names or values alone. Explicitly distinguish published quantity semantics from verified physical units/query definitions.
- Source investigation pins references/revisions; known CPU usage can support relative-change interpretation without inventing saturation. Generic/unsupported inputs retain unknown.
- All candidate/permission/features freeze without labels. Labels only score known18 and generate audit explanations afterwards. No case-ID answer rules or threshold tuning against heldout36.
- Existing one-attempt journal, no retries and paid gate17/18 twice+18/18 packet+human-reviewed hash remain enforced.

## Review Focus

- Curated semantic profiles must not bless arbitrary cpu-like counters or invent unit/collection facts; profile-off and unsupported metrics stay unknown (Task1).
- Metadata must reach base, perturbation, validation and live evidence uniformly, and participate in packet/config identity (Task1).
- Failure attribution must separate observed extraction/selection gaps from unproven raw-data absence, keeping truth outside feature generation (Task2).
- Retrieval must not promote symptom keywords into permission or restore recall by enumerating all combinations; partial baseline observations remain explicit (Task3).
- Lossless packet changes must preserve exact values/missingness/all material refs, cross-case binding, provider boundary and concurrency/resume behavior (Task3/4).

### Task1: Verified semantic profile and CPU tracing

Files: new app/evaluation/single_pass_semantics.py; modify single_pass_metrics.py, single_pass_hypotheses.py, single_pass_runner.py; new tests/test_single_pass_semantics.py.
Interfaces: resolve_semantics(view,profile), collect_evidence(...,telemetry_profile=None), audit_development(...,telemetry_profile=None); CLI --telemetry-profile.

- [x] Investigate official sources and existing3CPU inventories; record what is proved and remains unknown.
- [x] RED tests: published CPU-usage quantity reaches eligibility via relative comparison; no percentage/counter inference; arbitrary/counter-like columns excluded; profile switch reaches all5 extraction paths and changes packet identity.
- [x] Implement curated source-backed quantity profile, preserve unknown kind/unit unless explicitly documented; maintain existing known-unit path.
- [x] Run semantic/extractor/runner regression (PASS); document3CPU before/after; commit.

### Task2: Reviewable18-case failure attribution

Files: new app/evaluation/single_pass_attribution.py; modify single_pass_runner.py; new tests/test_single_pass_attribution.py.
Interfaces: attribute_failure(evidence,hypotheses,packet_error,truth), write_attribution_report(audit,path).

- [x] RED tests: independent metadata/coverage/competition/retrieval/packet axes; absent extracted feature cannot assert raw-data absence; truth available only post-freeze.
- [x] Add per-case source/semantics inventory, exact cited refs/gaps, specific follow-up actions and aggregate table; preserve failures in all denominators.
- [x] Focused tests PASS; generate18 attribution from approved developer artifact/current frozen evidence; commit.

### Task3: Label-free retrieval and lossless budget repair

Files: modify single_pass_hypotheses.py and single_pass_packet.py; extend tests/test_single_pass_hypotheses.py and tests/test_single_pass_packet.py.
Interfaces: same rank_hypotheses/build_packet public contracts; optional versioned transport decoding internal to Packet.payload.

- [x] Investigate all5 misses and3 overflows using frozen developer evidence; state reproducible cause before each change.
- [x] RED tests for each general correction: weak/missing-baseline facts, genuine alternative hypotheses, service/mechanism diversity with temporal context as available; no new mechanism permissions from ranking.
- [x] RED dense fact fixture: exact support/opposition/contrast identity and full precision survive compression or0-call failure. Prefer structural field/value sharing over deletion.
- [x] Implement only evidence-backed corrections, run all single-pass/SDK/journal regressions PASS; commit.

### Task4: Frozen no-fee verification, review and report

Files: docs/single-pass-evidence-recovery.md; extend docs/single-pass-validation.md and protocol revision/config if needed.

- [x] Run all18 raw-source audits with ablations and4perturbations using frozen profiles; report top5, eligible true/competing pairs, packet counts, time stability separately.
- [x] Full pytest, targeted Ruff/Bandit, one fresh independent whole-range reviewer. Fix material findings with RED/GREEN and full green suite.
- [x] Write actual before/after, source proofs/unknowns and remaining data requirements; paid run blocked unless prerequisites and review satisfied; remaining36 untouched.
- [x] Commit only task files; preserve branch and progress records (previous deletion auto-review rejected). No merge/push.
