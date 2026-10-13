# Production Remediation Harness Design

**Date:** 2026-10-13

**Status:** Approved design, pending implementation plan

**Scope:** Add a deterministic production-remediation control layer to the existing single-Agent AIOps workflow.

## 1. Intent and success criteria

The platform already diagnoses incidents through LangGraph, persists jobs and evidence in PostgreSQL, and runs durable work through Celery and RabbitMQ. The next capability is controlled production remediation: restart, scale out, and rollback.

The design is successful when:

- the language model can propose a remediation but can never execute a production write directly;
- every proposed action is typed, policy-checked, attributable, idempotent, auditable, and bounded;
- approval, execution, verification, and rollback follow an explicit state machine;
- the first release is safe to run in `dry_run` without infrastructure credentials;
- current diagnosis-only behavior remains unchanged when no remediation is proposed or remediation is disabled;
- the Harness can later accept a real Kubernetes adapter without changing its policy, state, API, or persistence contracts.

## 2. Constraints

- Keep the existing single Planner–Executor–Replanner Agent.
- Reuse LangGraph, PostgreSQL, RabbitMQ, Celery, authentication, CSRF protection, metrics, and the service catalog.
- Do not add another database, event bus, policy engine, or multi-Agent framework.
- Do not expose a public endpoint that creates arbitrary remediation actions.
- Do not accept shell commands, free-form URLs, or arbitrary tool names from the model.
- The first release includes only a `DryRunRemediationAdapter`; a real Kubernetes adapter is a separate follow-up.
- Stateful-service restarts, scale-in, deletes, database changes, and arbitrary commands are prohibited.

## 3. Architectural boundary

The model owns semantic reasoning. Deterministic application code owns authority.

```text
Alert or operator request
        |
Existing diagnosis Agent
        |  structured ActionIntent or no action
        v
RemediationService
        |
        +--> PolicyEngine: schema, service policy, risk, budget, cooldown, approval
        |
        +--> RemediationRepository: state, evidence snapshot, audit timestamps
        |
        +--> remediation.control Celery queue
                    |
                    v
             RemediationGateway
                    |
                    +--> Adapter.prepare()
                    +--> Adapter.execute()
                    +--> deterministic verification
                    +--> Adapter.rollback() at most once
```

No Agent node receives infrastructure credentials. Only the remediation worker process receives an adapter instance and, in a future release, narrowly scoped production credentials.

## 4. Components

### 4.1 `app/harness/contracts.py`

Defines closed Pydantic contracts and enums:

- `RemediationMode`: `off`, `dry_run`, `assisted`, `guarded_auto`;
- `ActionType`: `restart_instance`, `scale_out`, `rollback_revision`;
- `RiskLevel`: `r1`, `r2`, `r3`, `r4`;
- discriminated action intents:
  - `RestartInstanceIntent` — service and instance identifier;
  - `ScaleOutIntent` — service and a positive replica delta;
  - `RollbackRevisionIntent` — service and an allowlisted target revision;
- `PolicyDecision`: allow, require approval, or deny, with stable reason codes;
- `PreparedAction`: normalized target, pre-change snapshot, verification specification, and opaque rollback token;
- `ExecutionResult` and `VerificationResult`.

All models use `extra="forbid"`. Scale delta and string lengths are bounded. The contracts contain no command, script, URL, environment variable, or generic executable payload fields.

### 4.2 `app/harness/policy.py`

`PolicyEngine.evaluate()` is pure deterministic code. Its inputs are:

- validated intent;
- configured remediation mode;
- service catalog policy;
- immutable evidence snapshot;
- recent actions for the target service;
- current incident action count.

It returns a `PolicyDecision`; it never calls the model or infrastructure.

Required checks:

1. remediation mode permits proposals;
2. target service exists in the service catalog and remediation is enabled;
3. action appears in that service's allowlist;
4. service is explicitly marked stateless for restart;
5. required evidence types exist and are successful;
6. per-action bounds are satisfied;
7. service cooldown has elapsed;
8. no other write action for the service is active;
9. incident and global action budgets are available;
10. a rollback path can be prepared.

Any missing or unknown input fails closed. The Policy Engine cannot downgrade risk based on model-provided confidence.

### 4.3 `app/harness/gateway.py`

Defines `RemediationAdapter` as a narrow protocol:

```python
class RemediationAdapter(Protocol):
    async def prepare(self, intent: ActionIntent) -> PreparedAction: ...
    async def execute(self, prepared: PreparedAction) -> ExecutionResult: ...
    async def observe(self, prepared: PreparedAction) -> dict[str, Any]: ...
    async def rollback(self, prepared: PreparedAction) -> ExecutionResult: ...
```

`RemediationGateway` owns execution sequencing, timeouts, idempotency, post-action verification, and the one-attempt rollback rule. It does not make policy or approval decisions.

The first implementation supplies `DryRunRemediationAdapter`. It validates the same contracts and produces deterministic simulated results, but performs no production write.

### 4.4 `app/services/remediation_service.py`

This is the only state-machine entry point. Responsibilities:

- accept internal proposals from the diagnosis runner;
- freeze the minimum evidence snapshot used by Policy;
- persist proposals and decisions;
- accept authenticated operator approval or rejection;
- enqueue eligible actions through a transactional outbox event;
- apply worker execution events through legal state transitions only;
- expose read models to the API.

The service rejects duplicate decisions, expired approvals, stale actions, and illegal state transitions.

### 4.5 `app/repositories/remediation.py`

Provides a protocol, an in-memory test implementation, and a SQLAlchemy implementation. It is separate from the existing large diagnosis repository.

The SQL implementation uses row locks for approval and state transitions, and a unique idempotency key to prevent duplicate execution.

## 5. Agent integration

The final response contract gains one optional `remediation` field containing one typed `ActionIntent`. The default is `None`.

The response prompt may propose an action only when:

- collected evidence supports the action;
- the target service can be resolved through the service catalog;
- the report clearly distinguishes observed facts from hypotheses.

The Replanner only proposes. It does not decide approval, risk, eligibility, or execution.

`AIOpsService` emits a `remediation_proposed` event when the optional intent exists. `DiagnosisRunner` records the event first, then submits it to `RemediationService`. Failure to create a remediation record does not rewrite a completed diagnosis as successful remediation; it records a separate Harness error event.

## 6. Risk and autonomy policy

### 6.1 Modes

| Mode | Behavior |
|---|---|
| `off` | Diagnosis only; discard remediation proposals with a recorded disabled reason. |
| `dry_run` | Evaluate and audit the complete flow; adapter never writes. This is the default. |
| `assisted` | Every production write requires an authenticated, unexpired approval. |
| `guarded_auto` | Only allowlisted R1/R2 actions may bypass approval when every precondition passes. |

There is no unrestricted `full_auto` mode.

### 6.2 Action policy

| Risk | Action | Default decision |
|---|---|---|
| R0 | Metrics, logs, and knowledge reads | Existing automatic behavior |
| R1 | Restart one instance of an explicitly stateless service | Auto only in `guarded_auto`; otherwise approval |
| R2 | Scale out by exactly one replica without exceeding service maximum | Auto only in `guarded_auto`, with a live current replica count of at least four; otherwise approval |
| R3 | Roll back to a configured last-known-good revision | Always approval for a newly proposed rollback |
| R4 | Scale-in, delete, database mutation, stateful restart, arbitrary command | Always deny |

An automatic rollback is allowed without a second approval only when it reverses the exact action just executed by the Harness and the rollback token was prepared before execution. A rollback is attempted at most once.

### 6.3 Budgets and circuit breakers

- one active write action per service;
- at most two write actions per diagnosis;
- configurable global active-action limit;
- service-specific cooldown, default 15 minutes;
- approval expiry, default 15 minutes;
- execution and verification deadlines;
- global kill switch through `REMEDIATION_MODE=off`;
- missing verification data is failure, never success;
- an unknown execution outcome is reconciled through `observe()` before retry or rollback.

## 7. Service catalog additions

Each service may define:

```json
{
  "remediation": {
    "enabled": true,
    "stateless": true,
    "allowed_actions": ["restart_instance", "scale_out", "rollback_revision"],
    "max_replicas": 10,
    "cooldown_seconds": 900,
    "last_known_good_revisions": ["revision-id"]
  }
}
```

Absent configuration means remediation is denied for that service. The current `ai-oncall-agent` entry is enabled only for `dry_run` during this implementation.

## 8. State machine

Legal states:

- `proposed`;
- `awaiting_approval`;
- `approved`;
- `executing`;
- `reconciling`;
- `verifying`;
- `succeeded`;
- `rolling_back`;
- `rolled_back`;
- `failed`;
- `rejected`;
- `expired`;
- `denied`.

Primary transitions:

```text
proposed -> denied
proposed -> awaiting_approval -> approved -> executing
proposed -> executing                         # policy-approved automatic or dry run
executing -> verifying -> succeeded
executing -> reconciling -> verifying        # timeout but observe confirms execution
reconciling -> rolling_back | failed          # observe finds regression or remains unknown
executing -> failed                           # no safe rollback path
executing -> rolling_back -> rolled_back
verifying -> rolling_back -> rolled_back
rolling_back -> failed                        # rollback failure; circuit breaker opens
awaiting_approval -> rejected | expired
```

Terminal states cannot transition. Approval uses a row lock and verifies current status and expiry in one transaction.

## 9. Persistence

Add one `remediation_actions` table through Alembic. Required columns:

- `id` primary key;
- `diagnosis_id` indexed foreign key with restricted deletion so remediation audit records cannot be cascade-deleted;
- `idempotency_key` unique;
- `action_type`, `target_service`, `risk_level`, `status` indexed strings;
- `intent_payload` JSON;
- `evidence_snapshot` JSON containing bounded summaries and hashes, not duplicated raw logs;
- `policy_decision` and `policy_reason`;
- `prepared_payload`, `result_payload`, and `verification_payload` JSON;
- `requested_by`, `approved_by`;
- `approval_expires_at`, `created_at`, `updated_at`, `approved_at`, `started_at`, `completed_at`;
- `error` bounded text.

Execution enqueueing uses the existing transactional outbox mechanism. The implementation extends outbox payload handling without adding a second outbox table.

## 10. Queue isolation

Add one durable quorum queue, `remediation.control`, to the existing RabbitMQ exchange and one Celery worker consuming only that queue. The worker uses prefetch 1 and concurrency 1 initially.

This adds no new infrastructure. Isolation prevents long diagnosis work from delaying approved production actions and prevents remediation concurrency from inheriting diagnosis-worker scaling.

The Dry Run worker has no infrastructure credentials. A future real adapter worker receives a dedicated least-privilege credential; API and diagnosis workers do not.

## 11. API

Expose three authenticated route shapes:

- `GET /api/remediations` — filterable list, primarily awaiting approval and history;
- `GET /api/remediations/{action_id}` — intent, policy, bounded evidence summary, state, result, and verification;
- `POST /api/remediations/{action_id}/decision` — body `{ "decision": "approve" | "reject" }`.

The decision endpoint requires the existing browser session plus CSRF token, or a future dedicated operator credential. Machine API keys cannot approve production actions in the first release.

No public create, execute, retry, or rollback endpoint is added.

## 12. Verification

Verification is deterministic and action-specific:

- restart: target instance replacement is observed and service availability/error-rate guard does not regress;
- scale out: desired and available replica counts reach the prepared target and error/latency guards do not regress;
- rollback: observed revision equals the approved revision and availability/error/latency guards recover.

The first Dry Run adapter returns deterministic simulated observations so state transitions can be tested. These simulations are clearly labeled and are never presented as production verification.

## 13. Error handling

- Policy failure: persist `denied` with a stable reason; do not enqueue.
- Approval expiry: atomically set `expired`; require a new proposal.
- Adapter preparation failure: set `failed`; nothing was executed.
- Execution timeout: move to `reconciling`, call `observe()`, and resolve to verification, rollback, or human escalation without blind retry.
- Verification failure: attempt only the prepared rollback, once.
- Rollback failure: set `failed`, increment a circuit-breaker metric, disable automatic actions for the target service until operator intervention.
- Persistence failure: never execute an unrecorded action.
- Duplicate broker delivery: idempotency key and locked state transition produce no second write.

## 14. Observability and audit

Add metrics for:

- proposals and policy decisions by action/risk/reason;
- awaiting approvals and expired approvals;
- execution, verification, rollback, and failure totals;
- active actions and action duration;
- global or per-service circuit-breaker state.

Logs include action ID, diagnosis ID, target service, state transition, worker, and reason code. They do not include credentials or unbounded raw evidence.

## 15. Testing strategy

### Unit tests

- discriminated intent parsing and rejection of extra/dangerous fields;
- complete mode × action × risk policy matrix;
- missing service config, missing evidence, cooldown, budget, and concurrency denial;
- legal and illegal state transitions;
- approval, duplicate decision, expiry, and rejection;
- idempotent duplicate execution;
- execution success, verification failure, rollback success, rollback failure, timeout reconciliation;
- report generation with and without a remediation proposal.

### API tests

- authentication and CSRF enforcement;
- machine API key cannot approve;
- list/detail output excludes raw credentials and unbounded evidence;
- approved, rejected, expired, missing, and conflict responses.

### Persistence and queue tests

- Alembic upgrade and check against PostgreSQL;
- unique idempotency key and row-locked approval behavior;
- outbox event is committed atomically with an eligible action;
- duplicate broker delivery does not repeat adapter execution;
- remediation queue and worker Compose configuration validates.

### Regression verification

- all existing tests pass;
- full new Harness test suite passes;
- Ruff, Bandit, dependency audit, migration check, and Compose validation run;
- diagnosis-only flows produce unchanged reports and statuses when remediation is absent or disabled.

## 16. Rollout

1. Ship contracts, persistence, Policy, state machine, APIs, queue, metrics, and Dry Run adapter with `REMEDIATION_MODE=dry_run`.
2. Observe proposals and false-positive/false-negative policy decisions using saved traces and tests.
3. Implement a Kubernetes adapter as a separate approved design and plan, initially in `assisted` mode.
4. Enable `guarded_auto` only for selected stateless services and only for R1 restart and bounded R2 scale-out.
5. Keep newly proposed R3 rollback approval-gated. Automatically rollback only the exact Harness action that just failed verification.

## 17. Explicit non-goals

- multi-Agent specialist orchestration;
- generic workflow or policy language;
- arbitrary shell or code execution;
- automatic scale-in or resource deletion;
- stateful workload or database remediation;
- full RBAC administration UI;
- production Kubernetes credentials or adapter in this implementation;
- claiming that Dry Run results prove real production execution safety.
