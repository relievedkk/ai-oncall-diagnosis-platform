# Windows Operational Safeguards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the seven approved Windows operational safeguards with honest offline/infrastructure verification boundaries.

**Architecture:** Keep native API/MCP and Docker Desktop dependencies. Add a stdlib-only `oncall_ops` package and PowerShell entrypoints; PostgreSQL owns shared model quota and durable task identity/events. Stable absolute state paths remain separate from immutable release directories.

**Tech Stack:** Python 3.11–3.13, PowerShell, PostgreSQL/SQLAlchemy/Alembic, Docker Compose, Prometheus, browser JavaScript/Node tests.

**Spec:** `docs/superpowers/specs/2026-10-24-windows-operational-safeguards-design.md` (approved).

## Global Constraints

- Work in the current approved checkout; preserve existing edits in static/app.js, static/index.html and static/styles.css.
- No cloud log queries, paid model calls, real notification delivery, production adapter, automatic database downgrade or restore over existing business volumes.
- Independent modules may be delegated under dispatching-parallel-agents with disjoint file ownership; coordinator integrates shared files and performs final review. All changes require RED→GREEN tests.
- Default restart delays 1,2,4,8,16,30 seconds; at most 5 restarts per service per 300 seconds.
- Probe failure/success thresholds 3/2. Shared model defaults: concurrency 4, calls/minute 60, input bytes/minute 1,048,576; scope by provider account. Effective lease bound, not guaranteed provider execution cancellation.
- Cold backup only after maintenance/drain and verified stopped writers; restore into new isolated resources only. No Docker available means real integration remains explicitly unverified.
- Do not create commits containing the user's unrelated changes; deliver reviewable working-tree changes, with progress/evidence recorded below.

## Review Focus

- Maintenance admission race: request admitted before maintenance must drain before snapshot; new mutation denied with 503.
- Windows child identity and duplicate supervisor: never kill foreign listeners or take ownership based on PID alone.
- Model lease renewal loss/cancellation: no further calls, no double release, stale release cannot free someone else's slot.
- Interrupted SSE frames and actor changes: advance cursors only on complete events, never leak/replay a prior account's task.
- Archives/release switching: reject traversal, links, tampering and source-volume aliases; data paths survive switching and rollback.

## Task 1: Supervisor and independent probes

**Files:** Create `oncall_ops/__init__.py`, `oncall_ops/common.py`, `oncall_ops/supervisor.py`, `oncall_ops/probe.py`, `tests/test_ops_supervisor.py`, `tests/test_ops_probe.py`.

**Interfaces:** `atomic_json(path: Path, data: dict)`, `read_env(path: Path) -> dict[str,str]`, `Supervisor(root: Path, state: Path, services: list[ServiceSpec], ...)` with run/stop and owned handles. `ServiceSpec(name, command, cwd, health_url=None, env=None)`. `probe.run_once(config: dict, state_dir: Path) -> dict` returns healthy/degraded checks and transition events. Stdlib only. Separate CLI is coordinator-owned.

- [ ] Write behavioral tests for owned-child death/restart, duplicate supervisor lock, foreign listener, stop-before-restart, failure window/backoff, dependency readiness not used as liveness.
- [ ] Run tests RED; implement finite loops and injected clocks/processes, plus at least one actual child-process restart test.
- [ ] Probe tests RED for failure/recovery thresholds, timeout, 401, event-only-on-transition, atomic state and secret-free errors; implement; run GREEN.
- [ ] Record exact commands/results in progress ledger, no unrelated file edits.

## Task 2: Shared quota and stable configuration

**Files:** Create `app/services/shared_model_quota.py`, `app/repositories/model_quota.py`, migration `20261024_0011_model_quota.py`, tests `test_shared_model_quota.py`, `test_shared_model_quota_postgres.py`; modify `app/config.py`, `app/db/models.py`, `app/core/llm_factory.py`, `app/core/model_protection.py`.

**Interfaces:** Shared guard wraps async provider generation/streaming in `SafeChatOpenAI`, including bound/structured models; reserve/renew/release and shared cooldown use SQL transactions + DB clock. Public `SharedQuotaExceeded(retry_after)` and fail-closed infrastructure errors; no model credential in scope. Add config `shared_model_quota_enabled=False` compatibility, scope/capacity/minute budgets/lease/renew values, `oncall_data_root`, `oncall_config_root`, `upload_dir` property. Managed entrypoint enables shared quota; coordinator wires Compose/sample config.

- [ ] Test RED for transactional no-oversell, minute boundary, failed calls retain minute charge, expiry, stale/duplicate release, renewal failure/cancel and shared 429 cooling.
- [ ] Implement models/migration/repositories/service and actual model transport wrapping without conflicting with existing per-diagnosis budget or callbacks; run targeted GREEN.
- [ ] Add explicit PostgreSQL integration tests with independent connections and random test scopes; skip absent TEST_DATABASE_URL, never use implicit business DB.
- [ ] Test stable path configuration; configuration root defaults to existing cwd for backward compatibility, deployed absolute paths set by launcher; preserve SDK/schema accounting.

## Task 3: Durable task reconnection

**Files:** Modify `app/services/alert_diagnosis_service.py`, `app/repositories/diagnosis.py`, `app/api/aiops.py`, `app/models/aiops.py`, `static/app.js`; create `static/diagnosis-stream.js`, `tests/test_diagnosis_reconnect.py`, `tests/diagnosis_stream.test.cjs`. Do not modify app config, DB models or main; event_key uniqueness handles scoped idempotency, metadata in alert payload.

**Interfaces:** `POST /api/aiops/jobs` accepts AIOpsRequest plus `Idempotency-Key`, returns 202 `{diagnosis_id, events_url}`; keyed by authenticated principal and payload hash, mismatch=409. `GET /api/aiops/jobs/{id}/events?after_id=...` also accepts Last-Event-ID; new manual tasks restricted to creator, legacy manual streams admin-only, alert tasks existing team scope. `stream_job(diagnosis_id, after_id=0)` emits stable `_event_id` for transport (public JSON can use event_id). Terminal IDs stable and distinguish success/failure; use persisted evidence IDs without inventing ID collisions. Legacy POST stays compatible.

- [ ] Test RED for duplicate concurrent creation/one outbox, payload conflict, ownership, after cursor and completed replay.
- [ ] Implement protocol and repository support preserving retry/authority semantics; GREEN existing stream tests.
- [ ] Node tests RED for UTF-8 split chunks, multiline data/comments/heartbeats, disconnect mid-frame, dedup, retry, refresh and account switch. Implement robust shared parser/client; integrate existing AI Ops UI without overwriting prior user's UI changes. Load new script through index.html only by coordinator.

## Task 4: Maintenance, queue health, Windows launch integration

**Files:** Create `app/core/maintenance.py`, `app/api/operations.py`, `oncall_ops/cli.py`, `scripts/oncall-windows.ps1`, `scripts/install-oncall-logon-task.ps1`, `config/windows-operations.example.json`; modify `app/main.py`, `app/server.py`, `app/api/file.py`, `app/services/vector_index_service.py`, queue observer/metrics and repository metrics, `monitoring/alerts.yml`, `monitoring/alertmanager.yml`, `vector-database.yml`, `.env.example`, `pyproject.toml`, `.gitignore`, `static/index.html`, startup/stop bat entries as compatibility wrappers where safe.

**Interfaces:** Local owner-only control-file channel `state/control.json` run ID matched by API server and maintenance middleware; no public shutdown route. Middleware tracks full ASGI lifetime including stream, gates mutation based on file state before increment; status file exposes in-flight counters and active durable work counts. API server polls shutdown intent to set uvicorn.should_exit. Supervisor CLI commands start/stop/status/probe; native launcher sets absolute DATA_ROOT/CONFIG_ROOT and shared quota env for all components.

- [ ] Maintenance race/drain/status tests RED; implement fail-closed channel, state files and graceful shutdown.
- [ ] Queue metric tests RED for oldest queued/retrying age and collection timestamp; implement. Promtool rules for depth critical/default, age, stale/absent. Operational alert label routes to independent receiver; webhook also ignores operational-loop alerts defensively.
- [ ] Wire CLI/PS entrypoint and optional logon registration; scripts accept explicit Python executable and Docker command overrides; no automatic installer/real scheduled-task registration during tests. Include managed-stop semantics and fail-fast preflight.
- [ ] Add long-lived Compose restart policy, immutable worker image variable, shared quota env, stable data/config settings. New streams script loaded before app.js.

## Task 5: Cold backup and isolated restore drill

**Files:** Create `oncall_ops/backup.py`, `oncall_ops/docker.py`, `oncall_ops/drill.py`, `tests/test_ops_backup.py`, `tests/test_ops_restore.py`, `tests/test_ops_drill_integration.py`; extend ops CLI.

**Interfaces:** `DockerRunner` executes argument arrays, captures no secrets, finite timeouts, discovers inspect mounts/images/hostname. `create_backup(root, state, destination, docker, ...) -> Path` drain via Task 4, stop exact original services and dependency writers, archive volumes using existing image helper (no required remote pull), archive uploads/config, write hashed manifest last, restart original running set in finally. `restore_isolated(backup: Path, target_root: Path, docker, ...) -> dict` validates and creates random owner-labelled network/volumes/containers, preserves Rabbit node identity, runs no workers. Every generated container/volume/name retained in ownership journal for safe cleanup.

- [ ] Test RED for stopped-writer precondition, stop/drain failures, partial archive no manifest, restart in finally (including double errors), missing volumes, target/source alias.
- [ ] Implement streaming archive hash checks and bounded safe extraction; reject symlink/hardlink/traversal/duplicate entries and nonempty targets. No recursive delete outside verified owned temp roots.
- [ ] Add real drill command creating isolated fixture DB/Rabbit/Milvus/uploads, seed deterministic synthetic task/report/checkpoint/version/queue/vector, cold-backup/restore then compare fingerprints and sample vector query; use installed project dependencies through its chosen Python. Integration test explicitly skipped without Docker; output records missing infrastructure rather than pass.

## Task 6: Immutable release and rollback

**Files:** Create `oncall_ops/releases.py`, `tests/test_ops_releases.py`, extend CLI and Windows scripts.

**Interfaces:** `package_release(root, releases_root, version, ...) -> Path` allowlisted source snapshot, SHA256 manifest, preserve current working-tree app UI. `activate_release(..., health_check, maintenance, docker) -> dict` atomic active/previous pointer, versioned venv and immutable worker image ID; injected adapters for tests. Validate schema declaration and migration head before switching; no downgrade. Stable absolute data/config env, versioned code/static paths.

- [ ] Tests RED for failed smoke rollback, source and worker version coherence, corrupt packages, incompatible schema, stable data paths and interruption journal.
- [ ] Implement preflight/prepare/activate/smoke/rollback finite state, retain previous pointers and images; assert real adapters invoke complete flow rather than only update JSON. Never delete active/previous release or unmanaged paths.
- [ ] Exercise two synthetic no-model local releases with deliberate bad health and verify previous service returns; Docker-specific Worker rollback explicitly integration-gated.

## Task 7: Final verification and review

**Files:** Update README and `docs/windows-operations.md`, write `docs/windows-operations-verification-20261024.md`; progress ledger `.superpowers/sdd/2026-10-24-windows-operational-safeguards/progress.md`.

- [ ] Run all targeted tests, complete Python suite, Node tests, compile checks, relevant Ruff/Bandit, PowerShell parser validation, git diff --check. Run promtool/Docker only if available; report missing tools separately.
- [ ] Independent reviewer checks full change against approved spec, especially unsafe mutation/restore, async quota lifecycle, authorization/reconnect and real CLI wiring. Fix material findings with regression tests.
- [ ] Deliver entrypoint commands, links to runbook and evidence, tested counts, unverified infrastructure conditions; do not claim production HA or successful real restore without evidence.
