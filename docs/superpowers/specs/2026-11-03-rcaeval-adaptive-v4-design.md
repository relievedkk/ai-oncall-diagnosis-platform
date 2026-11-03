# RCAEval adaptive protocol v4

## Intent and accepted approach

The user approved all P0–P2 improvements after the code/result analysis and asked for comprehensive detection and test coverage. Implement the recommended baseline-first workflow in the existing feature checkout. Coverage means every supported failure/data condition has an explicit outcome and regression test; it does not mean fabricating a diagnosis when causal evidence is absent.

Alternatives considered: (1) repair only the v3 route prompts, which leaves an unnecessary model gate and unchanged evidence; (2) baseline-first diagnosis with deterministic, authorized refinement, selected here; (3) replace the model, which cannot remove the current evidence ceiling. Keep v3 available and label v4 results separately.

## Constraints

- No hidden retries, new dependencies, merge, push, or sealed 36-case model runs.
- At most two model calls per strategy and one follow-up; all share one cumulative deadline.
- Preserve the 16 KiB serialized/transport evidence boundary and frozen initial parity.
- Preserve strict extra-field, service, reference, mechanism, and view authorization.
- Never persist raw model responses, arbitrary exception messages, secret field names, or telemetry values in schema errors.
- Preserve unrelated worktree edits. Stage explicit task-owned files only.
- Ground truth loads after both results freeze; development audits may inspect only the already-used 18 labels.
- No claim of 100% accuracy: missing evidence, ambiguity, execution failure, and valid abstention remain distinct.

## Model boundary

Add an evaluation-specific diagnosis schema with the closed seven-value fault taxonomy at root and hypothesis/comparison levels. Use separate route and diagnosis instructions, remove stale tool workflows from model-visible v3/v4 tasks, and describe route invariants explicitly. Keep the general production DiagnosisResult flexible.

An audited model wrapper preserves provider usage on parse failure and raises a safe typed exception with bounded schema issue codes and whitelisted field paths. Never serialize exception input/context/message. Both coordinator exception paths persist these summaries as invalid_model_output, not provider_error.

## Evidence completeness and targeted refinement

Missing baseline retains finite post observations, sample counts, and a baseline_missing reason; missing post and nonfinite-only inputs have explicit coverage states. These do not become eligible causes. All six mechanisms are represented, including unavailable ones. Distinguish telemetry absence from lack of a measurable incident change.

Follow-up trends must match requested services and mechanisms. A deterministic refinement builder adds stable, new references for detailed temporal observations, not reuses a ref for changed data. It summarizes all finite observations, records onset uncertainty and persistence, and includes bounded excerpts near incident/onset. It cannot invent loss evidence from log keywords.

For competing direct resource mechanisms, temporal refinement may resolve ambiguity only when every competitor has sufficient baseline/post samples, the proposed mechanism has sustained change, its onset interval strictly precedes all competitors, and it is not common-mode. Ties, gaps, missing competitors, and conflicting facts preserve unknown. Policy may supersede initial ambiguity only using an explicit, internally verified refinement certificate bound to the service and source refs; a boolean fault_eligible alone cannot clear ambiguity.

## Adaptive coordinator

Call 1 is a typed diagnosis on the identical frozen initial bundle used by single_stage. A valid complete diagnosis ends immediately. Otherwise, a deterministic planner examines candidate comparisons, evidence coverage, omitted details, and available views, and produces at most one authorized EvidenceRequest. No extra model route call.

Only attempt call 2 when the follow-up has added useful, nonempty evidence. Apply the same strict policy to the second diagnosis. Freeze the first valid diagnosis before refinement. If loading, authorization, timeout, parse, or policy execution fails, preserve it with an explicit degraded status and an enrichment failure record. Never accept invalid partial output. If call 1 fails there is no valid fallback and the strategy fails. Cancellation propagates.

Second-turn abstention must not erase an already-supported first-turn service. Record selection/rejection reasons. A complete second diagnosis needs validated cause refs; an unsupported result cannot replace the first. Evaluation never consults truth when deciding.

## Runner and audit

Add an explicit --protocol-version {3,4}, default 4, while retaining scoring-v3 historical reading. New records carry protocol_version=4, final result source, fallback_used, refinement status/failure, schema issues, request, added refs and coverage summaries. Report exact call/token usage, paired gains/losses, failures versus abstentions, and strata by fault and trace availability. Reject overwriting an existing result set before any model calls.

Expose a no-model audit of the 18 development cases: candidate recall, correct-mechanism availability/eligibility ceiling, baseline gaps, and query novelty. It must not open sealed 36-case answers. Model evaluation is a separate step after offline gates.

## Verification

Tests cover six faults plus unknown, invalid enums/extra/missing fields, arbitrary secret error paths, no traces/logs, missing baseline/post, NaN/Inf, constant/zero baselines, competing/tied/reversed onset, unauthorized request/view, duplicate/conflicting refs, empty/no-new-evidence follow-up, loader/policy/provider errors, timeout in each stage, cumulative deadline, cancellation, fallback provenance, strict call cap, usage completeness and truth-load ordering. Run focused suites and full pytest, targeted Ruff/format, 54-case zero-model validation, and development coverage audit. Publish a coverage matrix with explicit limitations instead of equating passing tests with diagnostic accuracy.

## Builder-check resolutions

The runner injects a trusted verifier that rebuilds refinement independently from its raw CaseView and compares the canonical hash. The replaceable loader cannot certify its own output. Policy accepts ambiguity resolution only with refs supplied by this verifier through an in-memory trusted argument; serialized flags/certificates alone never grant authority. Without a verifier, refinement can add observations but cannot clear ambiguity.

V3 preserves its entry point and result-reader compatibility. Shared prompt/evidence bug fixes are recorded as explicit prompt_revision/evidence_revision on every newly created record; historical v3 records are not rewritten and must not be pooled with revised runs.

Result selection: a complete first result stops. A partial second result never replaces a first result with an already-supported service. A complete second result may replace a partial first result only with valid cause refs including a verified novel observation; a changed service is recorded explicitly. If both are partial and the first has no service, accept the second only when it adds a supported service; otherwise retain first. Fallback/error provenance is independent of final execution status.

Semantic novelty requires restored data omitted from the initial bundle, a newly observed authorized service, partial-coverage detail unavailable initially, or a verified resolution of competition; renaming a ref without changed observations is not novelty. Planner, loader, raw verification, policy and selection share the strategy deadline; blocking work uses the bounded executor.
