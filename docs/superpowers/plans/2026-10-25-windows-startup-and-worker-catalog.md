# Windows Startup And Worker Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Windows managed cold startup reliable and make worker service-catalog loading independent of Windows bind mounts.

**Architecture:** Start the Windows background Supervisor as a detached, breakaway process group and identify it with a parent-generated 128-bit run-id echoed by the Supervisor and health endpoints. Preserve the 120-second deadline, give startup a 15-second per-MCP protocol budget, retry transient Windows sharing violations, and use the service catalog baked into the immutable worker image.

**Tech Stack:** Python 3.13, pytest, Docker Compose, FastMCP, PowerShell/Windows Docker Desktop

**Spec:** `docs/superpowers/specs/2026-10-25-windows-startup-and-worker-catalog-design.md`

## Global Constraints

- Normal `native_ready` calls retain their 3-second default.
- Managed startup uses a validated 32-character lowercase hexadecimal run-id and retains the 120-second total deadline.
- Windows background creation includes detached, new-process-group, and breakaway-from-job flags.
- Runtime JSON publication retries only `PermissionError` with a bounded delay.
- Worker catalogs must come from `/app/config/service_catalog.json` in the immutable worker image.
- No existing user changes are reverted or committed implicitly.

## Review Focus

- A permanently failing MCP probe must still time out and request shutdown only for the owned supervisor.
- A foreign supervisor status must never be accepted or stopped.
- A Windows launcher PID that differs from the actual Supervisor PID must still succeed when the injected run-id matches.
- The Supervisor must remain live after the invoking start command exits.
- An exited virtual-environment launcher must not fail startup before a matching Supervisor run-id can publish readiness.
- Monitor MCP readiness must remain required after the longer startup probe budget is introduced.
- All four catalog consumers must avoid the Windows config bind target.
- Release restart/rollback must continue using the recorded immutable worker image without rebuilding.
- Persistent ACL or filesystem permission failures must still abort state publication.
- Periodic observational state publication may skip one tick on `PermissionError`; control and final-stop writes remain strict.

---

### Task 1: Startup-only cold MCP readiness budget

**Files:**
- Modify: `tests/test_operations_cli.py`
- Modify: `oncall_ops/cli.py`

**Interfaces:**
- Consumes: `native_ready(status, environment, api_url, *, timeout=...)`
- Produces: `_start(...)` invokes readiness with a 15-second timeout while preserving all ownership and deadline logic.

- [x] Write `test_background_start_allows_cold_mcp_protocol_initialization` so a simulated protocol becomes ready only with at least 10 seconds.
- [x] Run that test and confirm it fails because `_start` uses the 3-second default.
- [x] Pass `timeout=15` only from `_start`.
- [x] Generate a run-id in `_start`, inject it through `ONCALL_SUPERVISOR_RUN_ID`, and require it in status and `native_ready`; allow `Supervisor` to accept only a validated injected run-id.
- [x] Launch the Windows background Supervisor with `DETACHED_PROCESS`, `CREATE_NEW_PROCESS_GROUP`, and `CREATE_BREAKAWAY_FROM_JOB`.
- [x] Treat launcher exit as real Supervisor failure; enforce one absolute 120-second deadline across all readiness operations and reject late success.
- [x] Run the new test and the existing startup safety tests; expect all pass.

### Task 2: Immutable worker service catalog

**Files:**
- Modify: `tests/test_deployment_configuration.py`
- Modify: `vector-database.yml`

**Interfaces:**
- Consumes: `/app/config/service_catalog.json` copied by `Dockerfile.worker`.
- Produces: rendered Compose services with absolute `SERVICE_CATALOG_PATH` and no `/app-config/config` volume target.

- [x] Write `test_workers_use_release_catalog_without_windows_config_bind_mount` against rendered Compose behavior.
- [x] Run it and confirm it fails on the relative path and bind mounts.
- [x] Set both worker environment anchors to `/app/config/service_catalog.json` and remove the four config bind mounts.
- [x] Run deployment-configuration tests; expect all pass.

### Task 2A: Windows atomic state publication

**Files:**
- Modify: `tests/test_ops_supervisor.py`
- Modify: `oncall_ops/common.py`

**Interfaces:**
- Consumes: `atomic_json(path, data)` used by supervisor and control-plane state files.
- Produces: the same atomic publication contract with bounded retry for transient `PermissionError` from `os.replace`.

- [x] Write `test_atomic_json_retries_transient_windows_sharing_violation` with two injected failures followed by a real replace.
- [x] Run it and confirm the unmodified helper raises `PermissionError`.
- [x] Add bounded exponential retry around only the replace operation.
- [x] Make periodic Supervisor status publication tolerate `PermissionError` until the next tick, while initial and final stop publication remain strict.
- [x] Run supervisor/common tests and confirm all pass.

### Task 3: Integrated verification

**Files:**
- No production files added.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: test and live-run evidence for handoff.

- [x] Run the complete Python suite against an isolated PostgreSQL test database.
- [x] Run the Node test suite.
- [x] Start the standard Windows managed deployment from stopped state and require a zero exit code.
- [x] Verify API readiness, both MCP endpoints, worker catalog entries, container health, and zero immediate supervisor restarts.
