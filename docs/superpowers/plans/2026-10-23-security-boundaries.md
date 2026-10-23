# Eight security and lifecycle repairs

Spec: user accepted all eight findings in `D:/项目/智能OnCall Agent系统/代码风险复查-2026-10-23.md` and the preceding prioritized repair table.

## Global constraints

- Preserve the existing Python/FastAPI/LangGraph application and browser UI. Use the current checkout on repair branch `fix/security-boundaries-20261023` so existing local runtime scripts continue to address the correct files.
- Do not query real cloud logs, invoke paid models, disclose secrets, reset data, or rewrite existing task IDs. Schema updates must have a migration.
- Follow TDD: run meaningful failing regression tests before implementation; retain coverage for existing behavior. No Git commits or remote pushes by implementers.
- Work independently in owned files; coordinate interfaces through the ledger. Root owns configuration, model data boundary, runner integration and final validation.
- Treat CLS as a read-only catalog-scoped service. Keep the catalog-backed local log wrapper; never expose raw cloud inventory/callback tools to agents.

## Review focus

1. Legacy task identities and Alertmanager's UTC zero endsAt must not break incoming valid notifications.
2. A batch with one invalid alert must not partially enqueue before validation; stale resolves must not close newer periods or other labels.
3. Model call and input budgets must persist across retries; secrets must not enter provider payloads or streamed/persisted tool results.
4. A cancelled login must not release its CPU capacity while the password worker is still running; stale browser tabs must stop rendering and sending as an old identity.
5. Pagination must be bounded and stable; stream readers must drain pending evidence before a terminal event.

## Task A: diagnosis identity, lifecycle, bounded reads and durable budget counters

Owned files: `app/models/alerts.py`, `app/repositories/diagnosis.py`, `app/services/alert_diagnosis_service.py`, `app/api/alerts.py`, `app/db/models.py`, new migration after `20261023_0009`, and dedicated diagnosis tests.

Interfaces produced:
- Canonical aware UTC timestamp helpers; validate startsAt/endsAt and cap fingerprint/event-key inputs before batch effects. Preserve RFC3339 representation compatibility, zero firing endsAt, legacy identifiers and equivalent-time dedupe.
- `list_jobs(*, limit=50, offset=0)` returns bounded summaries; API accepts bounded pagination and keeps top-level `jobs`.
- `get_job_stream(diagnosis_id, *, after_id=0, limit=100)` returns small job state plus bounded incremental evidence; stream drains before completion. Existing `get_job` remains for worker/detail use.
- `reserve_model_budget(diagnosis_id, *, execution_token, input_bytes, max_calls, max_input_bytes)` atomically returns bool, retaining counters across retries and rejecting stale owners; add `model_calls` and `model_input_bytes` columns with zero defaults in a new migration and in-memory equivalents.

Steps: write/run failing regressions for stale/label-collision resolves, invalid timestamp batch, equivalent/legacy timestamp identity; implement; test pagination/incremental drain and durable counters; implement and run relevant existing diagnosis suites. No edits to runner or shared config.

## Task B: bounded asynchronous password work and account-change invalidation

Owned files: `app/services/auth_service.py`, `app/api/auth.py`, `static/app.js`, `static/login.js`, auth/frontend regression tests.

Interfaces produced: bounded password worker capacity, busy exception mapped to 503/429, asynchronous `auth_service.shutdown()` callable from lifespan. Proposed defaults: 2 CPU workers and at most 8 queued computations. Capacity remains reserved until underlying work completes even if request cancelled.

Browser: broadcast logout/login identity changes; invalidate stale instances, clear previous-user cache on logout, check identity before cached fallbacks after asynchronous history loads. Preserve per-user storage and ordinary history reloads. Do not transmit cache contents to the server.

Steps: failing event-loop/cancellation/busy tests and stale-tab/history-fallback tests; implement; run auth/frontend suites and Node tests.

## Task C: catalog-scoped CLS gateway and agent tool allowlist

Owned files: `app/agent/mcp_client.py`, `mcp_servers/cls_gateway.py`, dedicated tool policy/gateway tests and existing tests that exercise their contract. Root owns executor/replanner/RAG service files.

Interfaces: preserve `load_mcp_tools_safe` signature but expose only approved Monitor tools to agents; raw CLS tools remain private to the catalog-backed local wrapper. Gateway lists/calls only `SearchLog` with catalog-approved Region/TopicId, exact server-generated service/error query, bounded From/To and Limit; reject multi-topic/extra bypass parameters and unsupported tools. Existing gateway token auth remains. No cloud access in tests.

Steps: failing tests for raw tool binding, noncatalog topics, callback inventory, query broadening, timestamp/limit/extra-parameter bypass and authenticated valid query; implement; run gateway, MCP and observability tests.

## Task D (root): deterministic data boundary and persistent task budgets

Owned files: `app/core/data_safety.py` (new), `app/core/llm_factory.py`, `app/core/model_protection.py`, `app/config.py`, `app/services/diagnosis_runner.py`, `app/agent/aiops/executor.py`, `app/agent/aiops/replanner.py`, `app/services/conversation_memory.py`, `app/tools/service_observability.py`, and integration/docs/runtime files.

Defaults: provider request input 64 KiB; individual tool/evidence result 16 KiB; each diagnosis at most 24 model calls and 256 KiB cumulative input (durable counters via Task A). All configurable with bounded fields. Redact structured secret keys and recognizable free-text credential assignments/Bearer/private-key data; preserve service IDs, timestamps and valid metric values. Oversized evidence carries explicit truncation metadata; oversized provider input is rejected before network access. Provider boundary remains enforced with tool-bound and structured-output models.

Steps: failing redaction/payload/budget tests using fake transport/model; implement; wire diagnostic budget context through runner and reserve via async observer; sanitize bounded evidence before persistence/stream and model calls. Add lifespan shutdown for password pool. Integrate Task A/B/C and run full suite, Ruff, Bandit, JS tests and isolated real PostgreSQL/Milvus checks as appropriate. Update local containers/native processes only after checks pass; do not send cloud queries.

## Completion

Run independent code review over the complete diff and address actionable findings. Record exact test results and remaining limitations in a repair document. Keep user's checkout and runtime usable; do not claim all security risks globally eliminated.
