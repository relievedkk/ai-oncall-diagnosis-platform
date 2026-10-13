# Production Remediation Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic, dry-run-first remediation Harness that lets the existing single AIOps Agent propose restart, scale-out, and rollback actions without giving the model production execution authority.

**Architecture:** The Replanner may emit one typed `ActionIntent`; a pure Policy Engine decides deny, approval, or automatic dry-run eligibility. A dedicated remediation service and repository own state, approval, idempotency, outbox enqueueing, execution events, verification, and rollback, while a narrow adapter protocol keeps production credentials outside the Agent and API processes.

**Tech Stack:** Python 3.11–3.13, Pydantic 2, FastAPI, SQLAlchemy async, PostgreSQL, Alembic, Celery 5, RabbitMQ quorum queues, LangGraph, pytest.

**Spec:** `docs/superpowers/specs/2026-10-13-production-remediation-harness-design.md`

## Global Constraints

- Keep the existing single Planner–Executor–Replanner Agent; do not add agents or a generic workflow engine.
- Reuse PostgreSQL, the existing transactional outbox, RabbitMQ, Celery, authentication, CSRF, metrics, and service catalog.
- `REMEDIATION_MODE` defaults to `dry_run`; the only runtime adapter in this plan is `dry_run` and it performs no production writes.
- Supported intents are exactly `restart_instance`, `scale_out`, and `rollback_revision`; all models use `extra="forbid"`.
- Never accept arbitrary commands, scripts, URLs, environment variables, tool names, scale-in, deletes, database changes, or stateful-service restarts.
- No public create, execute, retry, or rollback endpoint; machine API keys cannot approve actions.
- At most one active write action per service, at most two actions per diagnosis, default cooldown 900 seconds, and default approval expiry 900 seconds.
- Automatic scale-out is exactly one replica and requires a live current replica count of at least four; a real adapter must also prove the resulting count does not exceed the service maximum.
- Automatic rollback may only reverse the exact prepared action, may run once, and must fail closed when observation is missing or unknown.
- Current diagnosis-only behavior must remain unchanged when remediation is absent, disabled, denied, or cannot be persisted.

## Review Focus

- Malformed, oversized, or extra action fields must fail validation before persistence; pinned in Task 1 contract tests.
- Concurrent approval, rejection, and expiry must produce one legal terminal/approved outcome under a row lock; pinned in Task 4 service and PostgreSQL tests.
- Duplicate outbox or broker delivery must never invoke the adapter twice; pinned in Tasks 4 and 6 idempotency tests.
- Execution timeout plus missing observation must enter reconciliation and escalate without blind retry; pinned in Task 5 gateway tests.
- A normal report with no remediation must preserve the current event stream, job status, and report content; pinned in Task 7 regression tests.

---

### Task 1: Typed remediation contracts and configuration

**Files:**
- Create: `app/harness/__init__.py`
- Create: `app/harness/contracts.py`
- Modify: `app/config.py:11-162`
- Modify: `.env.example`
- Modify: `app/services/service_catalog.py:14-62`
- Modify: `config/service_catalog.json`
- Test: `tests/test_remediation_contracts.py`
- Test: `tests/test_service_catalog.py`

**Interfaces:**
- Consumes: Pydantic 2 and existing `Settings`/`ServiceCatalog` configuration patterns.
- Produces: `RemediationMode`, `ActionType`, `RiskLevel`, `ActionStatus`, `PolicyVerdict`, `RestartInstanceIntent`, `ScaleOutIntent`, `RollbackRevisionIntent`, `ActionIntent`, `parse_action_intent(payload)`, `PolicyDecision`, `PreparedAction`, `ExecutionResult`, `VerificationResult`, and `GatewayEvent`.

- [ ] **Step 1: Write failing contract tests**

Add tests named:

```python
def test_parse_restart_intent_rejects_extra_command_field(): ...
def test_scale_out_requires_positive_delta_and_caps_input_at_one(): ...
def test_rollback_requires_nonempty_revision(): ...
def test_action_payload_rejects_oversized_target_and_reason(): ...
def test_service_catalog_defaults_missing_remediation_to_disabled(): ...
def test_service_catalog_returns_explicit_remediation_policy(): ...
```

Assert discriminated parsing, `extra="forbid"`, maximum service/instance/revision/reason lengths, `replicas_delta == 1`, and fail-closed service policy defaults.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/test_remediation_contracts.py tests/test_service_catalog.py -q`

Expected: FAIL because `app.harness.contracts` and remediation service-catalog fields do not exist.

- [ ] **Step 3: Implement the contracts**

In `app/harness/contracts.py`, define string enums and discriminated Pydantic models with exact action literals. Add:

```python
def parse_action_intent(payload: dict[str, Any]) -> ActionIntent
```

using a module-level `TypeAdapter(ActionIntent)`. `PreparedAction` carries `intent`, `pre_change_snapshot`, `verification_spec`, and an opaque `rollback_token`; no executable string fields are allowed.

- [ ] **Step 4: Add bounded settings and fail-closed catalog parsing**

Add settings: `remediation_mode="dry_run"`, `remediation_adapter="dry_run"`, `celery_remediation_queue="remediation.control"`, approval/cooldown defaults of 900 seconds, max two actions per diagnosis, global active limit five, and execution/verification timeouts. Extend `ServiceCatalog` with:

```python
def remediation_policy(self, service_name: str) -> dict[str, Any]
```

Return `{"enabled": False, "allowed_actions": []}` when configuration is absent or malformed. Configure `ai-oncall-agent` as enabled for dry-run, stateless, and bounded to the three known actions.

- [ ] **Step 5: Run focused tests and static checks**

Run: `uv run pytest tests/test_remediation_contracts.py tests/test_service_catalog.py -q`

Expected: PASS.

Run: `uv run ruff check app/harness app/config.py app/services/service_catalog.py tests/test_remediation_contracts.py tests/test_service_catalog.py`

Expected: PASS.

- [ ] **Step 6: Commit Task 1**

```bash
git add app/harness app/config.py .env.example app/services/service_catalog.py config/service_catalog.json tests/test_remediation_contracts.py tests/test_service_catalog.py
git commit -m "feat: add typed remediation contracts"
```

### Task 2: Deterministic Policy Engine

**Files:**
- Create: `app/harness/policy.py`
- Test: `tests/test_remediation_policy.py`

**Interfaces:**
- Consumes: Task 1 `ActionIntent`, `RemediationMode`, `PolicyDecision`, `PolicyVerdict`, and service remediation dictionaries.
- Produces: immutable `PolicyContext` and `PolicyEngine.evaluate(intent: ActionIntent, context: PolicyContext) -> PolicyDecision`.

- [ ] **Step 1: Write the failing policy matrix tests**

Add parameterized tests for all modes and actions, plus:

```python
def test_unknown_service_policy_is_denied(): ...
def test_restart_requires_stateless_service_and_alert_metric_evidence(): ...
def test_scale_out_requires_metric_evidence_and_catalog_capacity(): ...
def test_guarded_auto_scale_out_requires_live_replica_count_of_at_least_four(): ...
def test_new_rollback_always_requires_approval(): ...
def test_active_action_cooldown_and_diagnosis_budget_fail_closed(): ...
def test_model_confidence_cannot_lower_risk(): ...
```

Use a fixed UTC `now`. Require evidence types `alert` and `metric` for restart, `metric` for scale-out, and `metric` plus `log` for a newly proposed rollback.

- [ ] **Step 2: Run the policy tests and verify RED**

Run: `uv run pytest tests/test_remediation_policy.py -q`

Expected: FAIL because `PolicyEngine` does not exist.

- [ ] **Step 3: Implement pure policy evaluation**

Define `PolicyContext` with mode, service policy, bounded evidence tuple, optional live replica count, current time, last completed action time, service-active flag, global-active count, and diagnosis-action count. Implement stable reason codes such as `mode_off`, `service_disabled`, `action_not_allowed`, `stateful_target`, `missing_evidence`, `missing_target_state`, `cooldown_active`, `budget_exhausted`, `capacity_exceeded`, `approval_required`, and `policy_allowed`.

`dry_run` returns allow after all safety checks. `assisted` requires approval for all writes. `guarded_auto` allows eligible restart/scale-out and requires approval for rollback. Unknown data returns deny.

- [ ] **Step 4: Run policy tests and Ruff**

Run: `uv run pytest tests/test_remediation_policy.py -q`

Expected: PASS.

Run: `uv run ruff check app/harness/policy.py tests/test_remediation_policy.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/harness/policy.py tests/test_remediation_policy.py
git commit -m "feat: add deterministic remediation policy"
```

### Task 3: Remediation persistence and repository

**Files:**
- Modify: `app/db/models.py:77-226`
- Create: `app/repositories/remediation.py`
- Create: `migrations/versions/20261013_0005_remediation_harness.py`
- Test: `tests/test_remediation_repository.py`
- Test: `tests/test_remediation_persistence.py`

**Interfaces:**
- Consumes: Task 1 status, intent, policy, execution, and verification contracts; existing async session factory and `OutboxEvent` model.
- Produces: `RemediationAction` ORM model, `RemediationRepository` protocol, `InMemoryRemediationRepository`, `SQLAlchemyRemediationRepository`, and singleton `remediation_repository`.

- [ ] **Step 1: Write failing repository behavior tests**

Cover:

```python
async def test_create_is_idempotent_by_idempotency_key(): ...
async def test_list_filters_by_status_without_unbounded_payloads(): ...
async def test_compare_and_set_rejects_illegal_state_transition(): ...
async def test_claim_execution_only_succeeds_once(): ...
async def test_create_eligible_action_commits_outbox_atomically(): ...
```

The in-memory implementation must model the same idempotency and compare-and-set behavior as PostgreSQL.

- [ ] **Step 2: Run repository tests and verify RED**

Run: `uv run pytest tests/test_remediation_repository.py -q`

Expected: FAIL because the repository does not exist.

- [ ] **Step 3: Add the ORM model and migration**

Implement the single `remediation_actions` table from the spec. Store bounded JSON payloads; index `diagnosis_id`, `status`, `target_service`, and `created_at`; make `idempotency_key` unique. Reference `diagnosis_jobs.diagnosis_id` with restricted deletion so audit records cannot be cascade-deleted.

- [ ] **Step 4: Implement repository protocol and both implementations**

Required protocol methods:

```python
async def create(self, values: dict[str, Any], enqueue: bool) -> tuple[dict[str, Any], bool]: ...
async def get(self, action_id: str) -> dict[str, Any] | None: ...
async def list(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]: ...
async def compare_and_set(self, action_id: str, expected: set[str], **updates: Any) -> bool: ...
async def decide(self, action_id: str, decision: str, principal_id: str, now: datetime) -> str: ...
async def claim_execution(self, action_id: str, worker_name: str) -> bool: ...
async def count_active(self, target_service: str | None = None) -> int: ...
async def count_for_diagnosis(self, diagnosis_id: str) -> int: ...
async def last_completed_at(self, target_service: str) -> datetime | None: ...
```

Eligible enqueueing inserts an existing `OutboxEvent` with task `app.tasks.remediation.run_remediation`, queue `remediation.control`, and payload `{"args": [action_id], "task_id": action_id}` in the same transaction.

- [ ] **Step 5: Add PostgreSQL integration tests**

Test migration upgrade, unique idempotency, atomic outbox creation, and two concurrent decisions where only one succeeds. Mark these tests `integration` and skip only when the configured PostgreSQL URL is unavailable; CI supplies PostgreSQL.

- [ ] **Step 6: Run persistence verification**

Run: `uv run pytest tests/test_remediation_repository.py -q`

Expected: PASS.

With PostgreSQL available, run: `uv run alembic upgrade head && uv run pytest tests/test_remediation_persistence.py -q && uv run alembic check`

Expected: PASS and `No new upgrade operations detected`.

- [ ] **Step 7: Commit Task 3**

```bash
git add app/db/models.py app/repositories/remediation.py migrations/versions/20261013_0005_remediation_harness.py tests/test_remediation_repository.py tests/test_remediation_persistence.py
git commit -m "feat: persist remediation actions"
```

### Task 4: Remediation state machine and approval service

**Files:**
- Create: `app/services/remediation_service.py`
- Test: `tests/test_remediation_service.py`

**Interfaces:**
- Consumes: Task 2 `PolicyEngine`; Task 3 repository protocol; existing diagnosis evidence dictionaries and service catalog.
- Produces: `RemediationService.propose(...)`, `decide(...)`, `apply_gateway_event(...)`, `get_action(...)`, and `list_actions(...)`; singleton `remediation_service`.

- [ ] **Step 1: Write failing state-machine tests**

Cover:

```python
async def test_dry_run_proposal_is_enqueued_with_bounded_evidence_snapshot(): ...
async def test_assisted_proposal_waits_for_approval_without_outbox(): ...
async def test_denied_proposal_is_persisted_but_never_enqueued(): ...
async def test_approval_is_atomic_and_enqueues_once(): ...
async def test_concurrent_approve_reject_expire_has_one_outcome(): ...
async def test_expired_or_duplicate_decision_is_rejected(): ...
async def test_illegal_gateway_transition_is_rejected(): ...
```

Assert the evidence snapshot contains only evidence type, source, success state, timestamp, and SHA-256 hash/short summary—not raw unbounded logs.

- [ ] **Step 2: Run the service tests and verify RED**

Run: `uv run pytest tests/test_remediation_service.py -q`

Expected: FAIL because `RemediationService` does not exist.

- [ ] **Step 3: Implement proposal and decision orchestration**

Required signatures:

```python
async def propose(
    self,
    diagnosis_id: str,
    intent: ActionIntent,
    evidence: list[dict[str, Any]],
    requested_by: str = "agent",
) -> dict[str, Any]: ...

async def decide(
    self,
    action_id: str,
    decision: Literal["approve", "reject"],
    principal_id: str,
    now: datetime | None = None,
) -> dict[str, Any]: ...

async def apply_gateway_event(self, action_id: str, event: GatewayEvent) -> dict[str, Any]: ...
```

Use an explicit legal-transition mapping. The service computes repository counts/cooldown inputs, calls Policy once, persists the immutable decision, and never re-asks the model.

- [ ] **Step 4: Run service and policy tests**

Run: `uv run pytest tests/test_remediation_service.py tests/test_remediation_policy.py tests/test_remediation_repository.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 4**

```bash
git add app/services/remediation_service.py tests/test_remediation_service.py
git commit -m "feat: add remediation state machine"
```

### Task 5: Dry-run execution gateway, verification, and rollback

**Files:**
- Create: `app/harness/gateway.py`
- Test: `tests/test_remediation_gateway.py`

**Interfaces:**
- Consumes: Task 1 adapter/result/event contracts.
- Produces: `RemediationAdapter` protocol, `DryRunRemediationAdapter`, and `RemediationGateway.run(intent: ActionIntent) -> AsyncIterator[GatewayEvent]`.

- [ ] **Step 1: Write failing gateway sequence tests**

Cover exact emitted status sequences:

```python
async def test_dry_run_success_emits_executing_verifying_succeeded(): ...
async def test_verification_failure_rolls_back_exactly_once(): ...
async def test_rollback_failure_emits_failed_and_never_retries(): ...
async def test_prepare_failure_executes_nothing(): ...
async def test_execution_timeout_observed_success_reconciles_to_verification(): ...
async def test_execution_timeout_missing_observation_fails_without_blind_retry(): ...
```

Use small fake adapters with call counters; assert `execute` and `rollback` maximum call counts of one.

- [ ] **Step 2: Run gateway tests and verify RED**

Run: `uv run pytest tests/test_remediation_gateway.py -q`

Expected: FAIL because the gateway does not exist.

- [ ] **Step 3: Implement the adapter protocol and gateway**

Use `asyncio.timeout()` with configured deadlines. `run()` yields stable events for `executing`, optional `reconciling`, `verifying`, `rolling_back`, and one terminal status. An empty/unknown observation cannot produce success.

`DryRunRemediationAdapter` produces explicit `dry_run: true` result and observation payloads; no subprocess, Docker, Kubernetes, HTTP mutation, or cloud SDK call is permitted.

- [ ] **Step 4: Run gateway tests and Ruff**

Run: `uv run pytest tests/test_remediation_gateway.py -q`

Expected: PASS.

Run: `uv run ruff check app/harness/gateway.py tests/test_remediation_gateway.py`

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add app/harness/gateway.py tests/test_remediation_gateway.py
git commit -m "feat: add dry run remediation gateway"
```

### Task 6: Transactional dispatch and isolated Celery worker

**Files:**
- Create: `app/tasks/remediation.py`
- Modify: `app/celery_app.py:1-61`
- Modify: `app/workers/outbox_publisher.py:18-44`
- Modify: `app/workers/queue_bootstrap.py`
- Modify: `app/services/queue_observer.py:24-122`
- Modify: `app/core/metrics.py`
- Modify: `vector-database.yml:1-160`
- Test: `tests/test_remediation_queue.py`
- Test: `tests/test_queue_observer.py`

**Interfaces:**
- Consumes: Task 3 generic outbox payload; Task 4 service; Task 5 gateway.
- Produces: Celery task `app.tasks.remediation.run_remediation`, durable queue `remediation.control`, and an isolated worker with prefetch/concurrency one.

- [ ] **Step 1: Write failing dispatch and idempotency tests**

Cover:

```python
async def test_outbox_publisher_sends_generic_args_and_task_id(): ...
def test_remediation_queue_is_durable_quorum_queue(): ...
def test_duplicate_task_delivery_claims_and_runs_adapter_once(): ...
def test_worker_applies_each_gateway_event_in_order(): ...
async def test_queue_observer_includes_remediation_queue(): ...
```

- [ ] **Step 2: Run queue tests and verify RED**

Run: `uv run pytest tests/test_remediation_queue.py tests/test_queue_observer.py -q`

Expected: FAIL because the queue/task and generic publisher behavior do not exist.

- [ ] **Step 3: Generalize outbox publishing without breaking diagnosis events**

Read `payload["args"]` and `payload["task_id"]` when present; otherwise retain the current diagnosis fallback. Route diagnosis queues exactly as before and remediation events to `remediation.control`.

- [ ] **Step 4: Implement the Celery task and worker configuration**

The bound task claims the action once, iterates gateway events, and applies each through `RemediationService`. Missing/already-terminal actions return without execution. Include both task modules in Celery.

Add `remediation.control` as a durable quorum queue and a Compose worker using `--pool=solo`, `--concurrency=1`, `--prefetch-multiplier=1`, and only that queue. Reuse the existing image, broker, database, and migration dependency.

- [ ] **Step 5: Add queue metrics and operational observation**

Track remediation queue ready/unacknowledged/consumer metrics through the existing queue observer. Add action status counters/gauges without high-cardinality action IDs.

- [ ] **Step 6: Run queue and Compose verification**

Run: `uv run pytest tests/test_remediation_queue.py tests/test_queue_observer.py -q`

Expected: PASS.

Run: `docker compose -f vector-database.yml config --quiet`

Expected: exit 0.

- [ ] **Step 7: Commit Task 6**

```bash
git add app/tasks/remediation.py app/celery_app.py app/workers/outbox_publisher.py app/workers/queue_bootstrap.py app/services/queue_observer.py app/core/metrics.py vector-database.yml tests/test_remediation_queue.py tests/test_queue_observer.py
git commit -m "feat: dispatch remediation through isolated queue"
```

### Task 7: Agent proposal integration without behavior regression

**Files:**
- Modify: `app/agent/aiops/state.py:10-33`
- Modify: `app/agent/aiops/replanner.py:23-375`
- Modify: `app/services/aiops_service.py:89-331`
- Modify: `app/services/diagnosis_runner.py:57-148`
- Modify: `app/services/alert_diagnosis_service.py:35-66`
- Test: `tests/test_remediation_agent_integration.py`
- Test: `tests/test_aiops_no_alerts.py`
- Test: `tests/test_alert_diagnosis_service.py`

**Interfaces:**
- Consumes: Task 1 `ActionIntent` and parser; Task 4 `remediation_service.propose()`.
- Produces: optional `remediation` in the final structured response/state and persisted `remediation_proposed` event.

- [ ] **Step 1: Write failing Agent integration and no-regression tests**

Cover:

```python
def test_report_event_without_remediation_is_byte_for_byte_compatible(): ...
def test_report_event_contains_one_valid_typed_remediation_summary(): ...
def test_invalid_model_remediation_is_dropped_and_recorded_as_harness_error(): ...
async def test_runner_records_report_before_submitting_remediation(): ...
async def test_runner_remediation_failure_does_not_change_completed_diagnosis(): ...
async def test_no_alert_report_never_proposes_remediation(): ...
```

- [ ] **Step 2: Run integration tests and verify RED**

Run: `uv run pytest tests/test_remediation_agent_integration.py tests/test_aiops_no_alerts.py tests/test_alert_diagnosis_service.py -q`

Expected: FAIL because the response/state has no remediation contract.

- [ ] **Step 3: Extend structured response and state**

Add optional `remediation` to `Response` and `PlanExecuteState`. The response prompt says to omit it unless evidence supports a catalog-resolvable action, and never proposes remediation in the deterministic no-alert branch. Preserve existing report output when the field is absent.

- [ ] **Step 4: Emit and persist proposals safely**

Include a bounded remediation summary in the report event. After recording that report, `DiagnosisRunner` loads persisted evidence and calls `remediation_service.propose()`. Persist a separate Harness error event on proposal failure; do not mark the diagnosis failed or completed remediation.

Ensure browser SSE never exposes raw evidence snapshots or adapter payloads.

- [ ] **Step 5: Run Agent and diagnosis regression tests**

Run: `uv run pytest tests/test_remediation_agent_integration.py tests/test_aiops_no_alerts.py tests/test_alert_diagnosis_service.py -q`

Expected: PASS.

- [ ] **Step 6: Commit Task 7**

```bash
git add app/agent/aiops/state.py app/agent/aiops/replanner.py app/services/aiops_service.py app/services/diagnosis_runner.py app/services/alert_diagnosis_service.py tests/test_remediation_agent_integration.py tests/test_aiops_no_alerts.py tests/test_alert_diagnosis_service.py
git commit -m "feat: let diagnoses propose controlled remediation"
```

### Task 8: Authenticated remediation query and decision API

**Files:**
- Create: `app/models/remediation.py`
- Create: `app/api/remediation.py`
- Modify: `app/api/__init__.py`
- Modify: `app/main.py:120-127`
- Test: `tests/test_remediation_api.py`

**Interfaces:**
- Consumes: Task 4 read/decision service; existing `request.state.auth_kind`, session principal, and CSRF middleware.
- Produces: `GET /api/remediations`, `GET /api/remediations/{action_id}`, and `POST /api/remediations/{action_id}/decision`.

- [ ] **Step 1: Write failing API tests**

Cover:

```python
def test_list_and_detail_require_authentication(): ...
def test_machine_api_key_cannot_approve(): ...
def test_session_decision_requires_csrf(): ...
def test_approve_reject_expired_missing_and_conflict_responses(): ...
def test_api_response_omits_raw_evidence_and_adapter_secrets(): ...
def test_list_limit_is_bounded_and_status_is_validated(): ...
```

- [ ] **Step 2: Run API tests and verify RED**

Run: `uv run pytest tests/test_remediation_api.py -q`

Expected: FAIL because routes/models do not exist.

- [ ] **Step 3: Implement request/response models and routes**

Use `DecisionRequest(decision: Literal["approve", "reject"])`; bound list limit to 1–100. On the decision route, require `request.state.auth_kind == "session"` and a principal; machine API key requests return 403 even though middleware authenticated them. Map missing to 404, expired to 410, and state conflict to 409.

- [ ] **Step 4: Register routes and run API/security tests**

Run: `uv run pytest tests/test_remediation_api.py tests/test_auth.py tests/test_security_hardening.py -q`

Expected: PASS.

- [ ] **Step 5: Commit Task 8**

```bash
git add app/models/remediation.py app/api/remediation.py app/api/__init__.py app/main.py tests/test_remediation_api.py
git commit -m "feat: add remediation approval API"
```

### Task 9: Operational hardening, documentation, and full verification

**Files:**
- Modify: `app/core/auth.py:126-146`
- Modify: `monitoring/alerts.yml`
- Modify: `README.md`
- Modify: `.github/workflows/ci.yml`
- Test: `tests/test_security_hardening.py`
- Test: `tests/test_remediation_operational.py`

**Interfaces:**
- Consumes: all earlier task behavior and metrics.
- Produces: fail-closed runtime configuration, operator documentation, remediation alerts, and CI gates.

- [ ] **Step 1: Write failing operational configuration tests**

Cover:

```python
def test_unknown_remediation_adapter_fails_startup_validation(): ...
def test_dry_run_adapter_is_the_only_supported_runtime_adapter(): ...
def test_remediation_alert_rules_cover_failure_rollback_and_circuit_breaker(): ...
```

- [ ] **Step 2: Run operational tests and verify RED**

Run: `uv run pytest tests/test_remediation_operational.py tests/test_security_hardening.py -q`

Expected: FAIL until configuration validation and alerts exist.

- [ ] **Step 3: Add fail-closed configuration and alerts**

Reject any `REMEDIATION_ADAPTER` other than `dry_run` in this release. Add Prometheus alerts for failed remediation, failed rollback/circuit breaker, stale awaiting approval, and remediation queue without a consumer. Do not alert on each normal dry-run proposal.

- [ ] **Step 4: Document the exact safety boundary**

Update README configuration, architecture, API, queue, status, and troubleshooting sections. State prominently that this release executes only Dry Run and does not contain a production Kubernetes adapter or credentials. Document mode semantics and the three public API route shapes.

- [ ] **Step 5: Strengthen CI for new code without folding in unrelated cleanup**

Run full Ruff on `app/harness`, `app/repositories/remediation.py`, `app/services/remediation_service.py`, `app/tasks/remediation.py`, `app/api/remediation.py`, and new tests. Keep the pre-existing full-project CLS lint debt out of this feature commit, but do not add new exclusions.

- [ ] **Step 6: Run complete fresh verification**

Run:

```bash
uv run pytest -q
uv run ruff check app/harness app/repositories/remediation.py app/services/remediation_service.py app/tasks/remediation.py app/api/remediation.py tests/test_remediation_*.py
uv run bandit -q -r app mcp_servers
uv run pip-audit --progress-spinner off
uv run alembic upgrade head
uv run alembic check
docker compose -f vector-database.yml config --quiet
docker compose -f monitoring.yml config --quiet
```

Expected: all tests and targeted lint pass; Bandit and dependency audit report no findings; Alembic reports no new upgrade operations; both Compose files validate. If PostgreSQL or Docker is unavailable locally, report those checks as unverified rather than claiming success, while CI must run them.

- [ ] **Step 7: Verify the repository diff and commit Task 9**

Run: `git diff --check && git status --short`

Expected: no whitespace errors; only intended Task 9 files plus this approved plan if not previously committed.

```bash
git add app/core/auth.py monitoring/alerts.yml README.md .github/workflows/ci.yml tests/test_security_hardening.py tests/test_remediation_operational.py docs/superpowers/plans/2026-10-13-production-remediation-harness.md
git commit -m "docs: operationalize remediation harness"
```

### Final review gate

- [ ] Review the complete branch against every success criterion and non-goal in the design spec.
- [ ] Confirm no code path imports subprocess or accepts an executable string for remediation.
- [ ] Confirm no production adapter or credential was introduced.
- [ ] Confirm diagnosis-only behavior remains covered by passing regression tests.
- [ ] Request a whole-branch code review, address findings, and rerun the complete verification commands before declaring completion.
