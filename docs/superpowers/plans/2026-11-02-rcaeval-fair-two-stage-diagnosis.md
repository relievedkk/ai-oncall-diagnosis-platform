# RCAEval Fair Two-Stage Diagnosis Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed, lossy RCAEval Agent path with a fair bounded evidence contract shared by a single-stage baseline and a genuinely optional one-follow-up Agent.

**Architecture:** A deterministic adapter builds one immutable, canonical `InitialEvidenceBundle` per case before either strategy runs. Both strategies use that exact payload; the Agent may issue one validated `EvidenceRequest` and receive one bounded follow-up bundle before its final diagnosis. A bundle-derived policy validates service, mechanism, topology direction, and granular evidence references without consulting ground truth.

**Tech Stack:** Python 3.13, pandas, Pydantic, LangChain/OpenAI structured output, pytest, Ruff

**Spec:** `docs/superpowers/specs/2026-11-02-rcaeval-fair-two-stage-diagnosis-design.md`

## Global Constraints

- Do not add runtime dependencies.
- Do not increase `config.tool_result_max_bytes`; reserve at least 512 UTF-8 bytes for ToolMessage/JSON wrapping.
- Ground truth remains inaccessible until both strategies have produced predictions.
- No production or evaluation branch may classify by case ID, expected service, expected fault, or sealed-answer content.
- The 18 executed cases are development/regression data; the remaining 36 cases stay sealed and unrun.
- Production LangGraph defaults remain backward compatible; the new coordinator is an explicit path.
- No paid model rerun occurs until offline gates pass and the user approves a fresh usage estimate.
- Every product-code behavior change follows RED→GREEN TDD.

## Review Focus

- Unicode service/log content, JSON escaping, and ToolMessage wrapping must not push a bundle past the configured byte boundary; Task 1 tests the actual sanitizer boundary.
- A large service catalog with many common-mode anomalies must remain deterministic and bounded without silently dropping every non-disk mechanism; Task 3 tests selection stability and diversity.
- Missing/partial topology must not invent caller/callee direction or crash candidate construction; Task 3 tests absent and incomplete Trace edges.
- A malformed, repeated, or out-of-catalog follow-up request must not trigger a second tool/model loop; Task 4 tests each invalid request class.
- Existing non-evaluation diagnosis calls without a bundle policy must preserve their current normalization behavior; Task 5 runs explicit compatibility tests.

---

### Task 1: Versioned canonical bounded evidence contract

**Files:**
- Create: `app/core/diagnostic_evidence.py`
- Create: `tests/test_diagnostic_evidence.py`
- Modify: `app/core/evidence_packet.py`

**Interfaces:**
- Produces: `MechanismState = Literal["direct_signal", "possible_symptom", "ambiguous", "unavailable"]`.
- Produces: Pydantic models `EvidenceFact`, `MechanismProfile`, `CandidateEvidence`, `CandidateHint`, `InitialEvidenceBundle`, and `FollowupEvidenceBundle`, all `extra="forbid"`.
- Produces: `FollowupView` closed vocabulary and `EvidenceRequest(services, mechanisms, view, justification_refs)` with 1–2 distinct catalog services, at most one hinted non-initial service, and no free-form query/path field.
- Produces: immutable `SerializedEvidenceBundle(payload: dict, text: str, sha256: str, byte_length: int)`.
- Produces: `serialize_evidence_bundle(bundle, *, max_bytes: int, reserve_bytes: int = 512) -> SerializedEvidenceBundle`.
- Preserves: `pack_evidence(...)` for existing callers; it delegates semantic truncation primitives where possible without changing its public contract.

- [ ] **Step 1: Write failing contract and safety-boundary tests**

Add tests named `test_bundle_serialization_is_canonical_and_immutable`, `test_bundle_reserves_tool_message_overhead`, `test_bundle_compaction_preserves_identity_direct_signals_and_refs`, `test_bundle_records_every_omission`, and `test_unicode_and_json_escaping_stay_within_actual_sanitizer_boundary`. Assert stable bytes/hash for equivalent input order, `byte_length <= max_bytes - 512`, successful round-trip through `sanitize_tool_message(ToolMessage(...))`, and no `tool_result_truncated` envelope.

- [ ] **Step 2: Run Task 1 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_diagnostic_evidence.py -q -o addopts=""`

Expected: FAIL because `app.core.diagnostic_evidence` and its models do not exist.

- [ ] **Step 3: Implement the evidence models and canonical serializer**

Use canonical UTF-8 JSON (`sort_keys=True`, compact separators), copy-on-validation, deterministic semantic priority tiers, and explicit `omitted_paths`. Never return a successful payload missing identity, refs, mechanism states, topology direction, or limitations. Raise `EvidenceBudgetError` if the minimum contract cannot fit.

- [ ] **Step 4: Run Task 1 tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_diagnostic_evidence.py tests/test_evidence_packet.py -q -o addopts=""`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 1**

Commit: `feat(evals): add canonical bounded diagnosis evidence contract`

---

### Task 2: Mechanism states, temporal discrimination, and honest loss observability

**Files:**
- Create: `app/evaluation/rcaeval_evidence.py`
- Modify: `app/evaluation/rcaeval.py`
- Modify: `tests/test_rcaeval.py`

**Interfaces:**
- Consumes: Task 1 `MechanismProfile` and `EvidenceFact`.
- Produces: `build_mechanism_profiles(view: CaseView, service_name: str) -> dict[str, MechanismProfile]` for `cpu`, `mem`, `delay`, `disk`, `socket`, and `loss`.
- Produces: `classify_competing_mechanisms(profiles, *, common_mode, bucket_seconds=60) -> dict[str, MechanismProfile]`.
- `CaseView.mechanism_profiles()` delegates to these functions and continues returning JSON-serializable output to compatibility callers.

- [ ] **Step 1: Write failing synthetic mechanism tests**

Add `test_loss_requires_structured_network_signal`, `test_log_text_cannot_promote_loss_to_direct_signal`, `test_multiple_direct_signals_are_ambiguous_without_temporal_separation`, `test_earlier_persistent_unique_signal_can_disambiguate`, `test_delay_is_symptom_when_resource_signal_is_direct`, and `test_delay_requires_all_three_rebuttal_conditions_to_be_direct`. Include missing columns, zero baselines, NaN, common-mode disk shifts, and one-bucket onset boundaries.

- [ ] **Step 2: Run Task 2 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rcaeval.py -k "loss or mechanism or delay_is_symptom or disambiguate" -q -o addopts=""`

Expected: new assertions fail because current profiles have no loss state or competition policy.

- [ ] **Step 3: Implement mechanism extraction and competition policy**

Recognize loss only from structured metric/Trace field names. Use robust pre/post thresholds and minute-bucket onset already established by `CaseView`; never compare raw ratios across units as a final classifier. Encode the spec's temporal, common-mode, and propagation rules in profile status/reasons, not in prompt-only prose.

- [ ] **Step 4: Run Task 2 tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rcaeval.py -k "loss or mechanism or delay_is_symptom or disambiguate" -q -o addopts=""`

Expected: selected tests pass.

- [ ] **Step 5: Commit Task 2**

Commit: `feat(evals): classify direct mechanisms and propagation symptoms`

---

### Task 3: Diverse candidate selection and shared initial bundle

**Files:**
- Modify: `app/evaluation/rcaeval_evidence.py`
- Modify: `app/evaluation/rcaeval.py`
- Modify: `tests/test_rcaeval.py`
- Modify: `tests/test_diagnostic_evidence.py`

**Interfaces:**
- Consumes: Task 1 serializer and Task 2 mechanism profiles.
- Produces: `CandidateSelection(candidates: tuple[str, ...], hints: tuple[CandidateHint, ...], reasons: dict[str, tuple[str, ...]])`.
- Produces: `select_candidate_services(view: CaseView, *, max_candidates: int = 5) -> CandidateSelection`.
- Produces: `build_initial_evidence(view: CaseView, *, max_bytes: int) -> SerializedEvidenceBundle`.
- Produces: `build_followup_evidence(view: CaseView, request: EvidenceRequest, *, max_bytes: int) -> SerializedEvidenceBundle`; consume Task 1's core request model to avoid a dependency cycle with the coordinator.

- [ ] **Step 1: Write failing candidate/topology tests**

Add `test_candidate_selection_is_mechanism_diverse_not_top_ratio_only`, `test_common_mode_disk_does_not_fill_candidate_budget`, `test_candidates_are_serialized_by_stable_service_name`, `test_topology_exposes_caller_to_callee_without_reverse_inference`, `test_missing_topology_is_explicitly_unavailable`, `test_hint_expansion_is_bounded_to_catalog_evidence`, and offline development regressions asserting case-032 direction/resources, case-041 initial-or-hinted `ts-train-service`, and case-054 CPU-direct/delay-symptom semantics. Tests may read the 18-case development labels only in assertions after bundle construction; production selection code may not receive them.

- [ ] **Step 2: Run Task 3 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rcaeval.py tests/test_diagnostic_evidence.py -k "candidate or topology or case_032 or case_041 or case_054 or hint" -q -o addopts=""`

Expected: new tests fail because current selection is anomaly Top-3 and has no shared bundle/hints.

- [ ] **Step 3: Implement deterministic diverse selection and bundle builders**

Select from per-mechanism direct signals, delay-only signals, downstream services with direct evidence, and bounded fallbacks. Keep selection reasons and non-selected catalog hints, then serialize through Task 1. Follow-up accepts initial candidates plus at most one catalog-and-hint service and one allowed view.

- [ ] **Step 4: Run Task 3 tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rcaeval.py tests/test_diagnostic_evidence.py -k "candidate or topology or case_032 or case_041 or case_054 or hint" -q -o addopts=""`

Expected: selected tests pass and development fixtures fit the actual tool boundary.

- [ ] **Step 5: Commit Task 3**

Commit: `feat(evals): build diverse shared RCA evidence bundles`

---

### Task 4: Single-stage baseline and one-follow-up Agent coordinator

**Files:**
- Create: `app/agent/aiops/two_stage.py`
- Create: `tests/test_two_stage_diagnosis.py`
- Modify: `app/agent/aiops/runtime.py`

**Interfaces:**
- Consumes: `SerializedEvidenceBundle`, `EvidenceRequest`, and a model factory compatible with the existing metered model wrapper.
- Produces: `DiagnosisTurn(action: Literal["diagnose", "request_evidence"], diagnosis_result: DiagnosisResult | None, evidence_request: EvidenceRequest | None)` with exactly-one validation.
- Produces: `run_single_stage(task: dict, initial: SerializedEvidenceBundle, *, model_factory, policy) -> CoordinatorResult`.
- Produces: `run_two_stage(task: dict, initial: SerializedEvidenceBundle, *, model_factory, followup_loader, policy) -> CoordinatorResult`.
- `CoordinatorResult` records result, errors, call count, initial hash, optional request, optional follow-up hash, and termination reason.
- Both coordinators use the same diagnosis instructions, `DiagnosisResult` contract, and normalization policy; the only strategy difference is whether a valid first-turn `EvidenceRequest` is permitted.

- [ ] **Step 1: Write failing coordinator state-machine tests with fake models**

Add `test_single_stage_uses_exact_initial_payload_once`, `test_agent_direct_diagnosis_uses_one_call`, `test_agent_valid_followup_uses_exactly_two_calls`, `test_agent_cannot_request_second_followup`, `test_out_of_catalog_or_unhinted_request_is_rejected_without_loader_call`, `test_duplicate_service_and_oversized_request_are_rejected`, `test_failed_direct_diagnosis_does_not_auto_retry`, and `test_provider_or_loader_failure_has_distinct_outcome`. Assert no intermediate natural-language summary call exists.

- [ ] **Step 2: Run Task 4 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_two_stage_diagnosis.py -q -o addopts=""`

Expected: FAIL because the coordinator does not exist.

- [ ] **Step 3: Implement the bounded two-state coordinator**

Use structured output only. Freeze/copy initial payload before each strategy and construct the shared diagnosis prompt through one helper used by both paths. Permit a second invocation only after a valid first-turn request; the second-turn schema accepts diagnosis only. Preserve model/provider errors without exposing secrets.

- [ ] **Step 4: Run Task 4 tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_two_stage_diagnosis.py -q -o addopts=""`

Expected: all coordinator tests pass.

- [ ] **Step 5: Commit Task 4**

Commit: `feat(agent): add bounded two-stage diagnosis coordinator`

---

### Task 5: Bundle-derived causal consistency policy

**Files:**
- Modify: `app/core/diagnostic_evidence.py`
- Modify: `app/core/diagnosis_result.py`
- Modify: `app/agent/aiops/two_stage.py`
- Modify: `tests/test_diagnosis_completeness.py`
- Modify: `tests/test_two_stage_diagnosis.py`

**Interfaces:**
- Produces: immutable `DiagnosisEvidencePolicy.from_bundles(initial, followup=None)` containing allowed services, valid refs, per-service mechanism support, delay blockers, and directed edges.
- Extends: `normalize_result(..., evidence_policy: DiagnosisEvidencePolicy | None = None)`; `None` preserves current production behavior.
- Consumes: Task 4 coordinator normalizes every model diagnosis through this policy before returning it.

- [ ] **Step 1: Write failing policy tests**

Add `test_policy_rejects_unknown_or_unexpanded_root`, `test_policy_rejects_fault_without_matching_service_ref`, `test_policy_downgrades_delay_when_direct_resource_blocks_it`, `test_policy_rejects_reverse_dependency_causality`, `test_policy_allows_unknown_for_ambiguous_direct_signals`, `test_valid_followup_expands_allowed_root`, and `test_no_policy_preserves_legacy_normalization`.

- [ ] **Step 2: Run Task 5 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_diagnosis_completeness.py tests/test_two_stage_diagnosis.py -k "policy or reverse_dependency or legacy" -q -o addopts=""`

Expected: new tests fail because normalization has no bundle policy.

- [ ] **Step 3: Implement policy extraction and normalization gates**

Validate granular refs and directed facts. A gate may reject or downgrade to `insufficient_evidence`/`unknown`; it must never choose a service/fault on the model's behalf. Keep existing `reference_evidence` validation and production compatibility.

- [ ] **Step 4: Run Task 5 tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_diagnosis_completeness.py tests/test_two_stage_diagnosis.py -q -o addopts=""`

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 5**

Commit: `feat(agent): enforce bundle-derived causal diagnosis policy`

---

### Task 6: Fair protocol-v3 runner and decomposed scoring

**Files:**
- Modify: `app/evaluation/rcaeval.py`
- Modify: `app/evaluation/rcaeval_scoring.py`
- Modify: `tests/test_rcaeval.py`
- Modify: `tests/test_rcaeval_scoring.py`

**Interfaces:**
- Consumes: Tasks 3–5 shared bundle, coordinator, and policy.
- Replaces new experiment strategy names with `single_stage` and `agent`; CLI accepts legacy `baseline` only as an explicit alias recorded in output metadata.
- Produces: protocol/scoring version 3 run records with initial hash/bytes, parity result, follow-up metadata, execution category, usage, abstention, candidate lists, and post-score candidate recall.
- Produces: summary metrics for end-to-end accuracy, execution-conditioned accuracy, initial/post-follow-up candidate recall, safe unknown rate, failures by category, and model usage.

- [ ] **Step 1: Write failing fair-runner and scoring tests**

Add `test_runner_builds_initial_bundle_once_and_shares_exact_bytes`, `test_truth_is_loaded_only_after_both_predictions`, `test_protocol_v3_separates_execution_failure_from_safe_unknown`, `test_protocol_v3_reports_conditional_accuracy_and_candidate_recall`, `test_old_summary_remains_rescorable_without_v3_fields`, and `test_legacy_baseline_alias_is_explicit_not_silent`.

- [ ] **Step 2: Run Task 6 tests and verify RED**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rcaeval.py tests/test_rcaeval_scoring.py -k "protocol_v3 or shares_exact or truth_is_loaded or legacy_baseline" -q -o addopts=""`

Expected: new tests fail because the current runner builds evidence separately and reports scoring version 2.

- [ ] **Step 3: Integrate the shared bundle and protocol-v3 scorer**

Build/freeze once per case, run both strategies from copies of the same canonical text, then load truth and score. Preserve historical fields where harmless, add explicit versioning, and never merge old/new baseline numbers as one experiment.

- [ ] **Step 4: Run Task 6 tests and verify GREEN**

Run: `.\.venv\Scripts\python.exe -m pytest tests/test_rcaeval.py tests/test_rcaeval_scoring.py -q -o addopts=""`

Expected: all runner/scoring tests pass.

- [ ] **Step 5: Commit Task 6**

Commit: `feat(evals): add fair protocol-v3 RCA comparison`

---

### Task 7: Offline corpus gate, documentation, and full verification

**Files:**
- Create: `docs/rcaeval-two-stage-offline-validation.md`
- Modify: `docs/rcaeval-heldout-18-v1-results.md`
- Test: all files above plus the existing project suite

**Interfaces:**
- Consumes: completed protocol-v3 implementation.
- Produces: a no-model validation report for all 54 task directories and a paid-rerun estimate for the 18-case development set.

- [ ] **Step 1: Add failing/offline corpus gate coverage**

Add a test or validation command that builds all 54 initial bundles, passes their exact serialized text through `sanitize_tool_message`, asserts no truncation/failure envelope, checks determinism over two builds, and confirms the 36 unrun case IDs are never passed to a model coordinator. Add aggregate assertions for 18-case development initial/hinted candidate recall without exposing labels to bundle construction.

- [ ] **Step 2: Run the offline corpus gate before documentation**

Run: `.\.venv\Scripts\python.exe -m app.evaluation.rcaeval --data-dir .runtime\rcaeval\heldout-54-v1\tasks --answers-file .runtime\rcaeval\heldout-54-v1-sealed\answers.parquet --mode validate --output-dir .runtime\rcaeval\two-stage-offline-validation`

Expected: zero model calls, 54/54 readable, 54/54 bundles deterministic and sanitizer-safe, with candidate metrics calculated only in the validation/scoring phase.

- [ ] **Step 3: Write the offline validation and migration report**

Document protocol changes, byte-size distribution, candidate recall, unavailable mechanisms, compatibility behavior, known limitations, and an estimated 18-case paid rerun call/token range. State that the remaining36 are still sealed/unrun.

- [ ] **Step 4: Run focused, full, static, and diff verification**

Run focused:
`.\.venv\Scripts\python.exe -m pytest tests/test_diagnostic_evidence.py tests/test_evidence_packet.py tests/test_diagnosis_completeness.py tests/test_two_stage_diagnosis.py tests/test_rcaeval.py tests/test_rcaeval_scoring.py -q -o addopts=""`

Run full:
`.\.venv\Scripts\python.exe -m pytest -q -o addopts=""`

Run static:
`.\.venv\Scripts\python.exe -m ruff check app/core/diagnostic_evidence.py app/core/evidence_packet.py app/core/diagnosis_result.py app/agent/aiops/two_stage.py app/evaluation/rcaeval_evidence.py app/evaluation/rcaeval.py app/evaluation/rcaeval_scoring.py tests/test_diagnostic_evidence.py tests/test_two_stage_diagnosis.py tests/test_rcaeval.py tests/test_rcaeval_scoring.py`

Run format check:
`.\.venv\Scripts\python.exe -m ruff format --check app/core/diagnostic_evidence.py app/core/evidence_packet.py app/core/diagnosis_result.py app/agent/aiops/two_stage.py app/evaluation/rcaeval_evidence.py app/evaluation/rcaeval.py app/evaluation/rcaeval_scoring.py tests/test_diagnostic_evidence.py tests/test_two_stage_diagnosis.py tests/test_rcaeval.py tests/test_rcaeval_scoring.py`

Run diff check: `git diff --check`

Expected: all tests pass, Ruff reports no violations or format drift, and diff check is empty.

- [ ] **Step 5: Commit Task 7**

Commit: `docs(evals): record two-stage offline validation`

Paid execution is not part of this task. Stop after reporting the fresh estimate and request approval before any live 18-case rerun. Do not run the remaining36.
