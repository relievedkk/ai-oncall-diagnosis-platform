# P0 Local Security Hardening Implementation Plan

> Merge-review update: the online rotation task was removed because it was not crash-recoverable and did not restart every credential consumer. See `2026-11-01-p0-security-merge-review.md` for the safe merge delta.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the confirmed local P0 secret-boundary, default-credential, and vulnerable-dependency risks without losing existing data.

**Architecture:** Pure configuration helpers generate and validate independent credentials; an explicit Windows/local rotation command coordinates live services and atomically updates `.env`. Startup and Compose fail closed when unsafe credentials remain.

**Tech Stack:** Python 3.13, pytest, Docker Compose, PostgreSQL, RabbitMQ, MinIO/Milvus, uv.

**Spec:** `docs/superpowers/specs/2026-10-31-p0-local-security-hardening-design.md`

## Global Constraints

- Preserve all existing Docker volumes and application data.
- Never print or persist plaintext credentials outside the protected `.env`.
- `pypdf>=6.21.4`, `authlib>=1.9.1`, `joserfc>=1.7.6`.
- Windows `.env` ACL is protected and limited to current user, SYSTEM, and Administrators; POSIX mode is `0600`.

## Review Focus

- Existing default credentials with populated volumes must not be silently replaced before server-side rotation.
- Partial rotation failure must not leave a new `.env` pointing at old service credentials.
- Derived database and broker URLs must encode and match generated credentials.
- Missing Compose credentials must fail before a service launches.
- Tests and diagnostics must never echo generated or existing secret values.

---

### Task 1: Credential generation, validation, and ACL

**Files:**
- Modify: `app/local_env.py`
- Modify: `tests/test_harden_local_env.py`

- [ ] Write failing behavior tests for generated infrastructure credentials, synchronized URLs, weak-admin removal, and owner-only file permissions.
- [ ] Run the focused tests and confirm expected failures.
- [ ] Implement minimal pure helpers plus ACL hardening.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Fail-closed startup and Compose configuration

**Files:**
- Modify: `oncall_ops/cli.py`
- Modify: `vector-database.yml`
- Modify: `.env.example`
- Modify: `start-windows.bat`
- Test: `tests/test_operations_cli.py`
- Test: `tests/test_deployment_configuration.py`

- [ ] Add failing tests for unsafe/missing infrastructure credentials.
- [ ] Run them to establish RED.
- [ ] Enforce validation, remove Compose credential fallbacks, and pass MinIO credentials to Milvus.
- [ ] Run focused tests to GREEN.

### Task 3: Data-preserving local credential rotation

**Files:**
- Create: `oncall_ops/local_security.py`
- Create: `tests/test_local_security.py`
- Modify: `oncall_ops/cli.py`
- Modify: `scripts/oncall-windows.ps1`

- [ ] Write failing orchestration tests for ordering, rollback, secret-free errors, and health verification.
- [ ] Run them to establish RED.
- [ ] Implement the explicit rotate-local-credentials operation.
- [ ] Run focused tests to GREEN.
- [ ] Execute the operation against the current local stack and verify service health.

### Task 4: Dependency security floors

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`

- [ ] Add the exact dependency floors and regenerate the lock file.
- [ ] Sync the environment and run upload/index regression tests.
- [ ] Run `pip-audit --local` and confirm the three known vulnerable versions are absent.

### Task 5: Final verification

- [ ] Run Python tests available in the local environment, frontend Node tests, Ruff/Bandit, Compose validation, ACL assertions, and live health checks.
- [ ] Inspect `git diff` for secrets and unintended changes.
- [ ] Report exact pass/fail counts and any test that could not complete.
