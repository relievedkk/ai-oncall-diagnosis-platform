# P0 Security Merge Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the reviewed P0 branch safe to merge while deferring unsafe online credential rotation.

**Architecture:** Fresh provisioning and managed-start validation remain the supported path. A single shared credential policy is used by provisioning and startup, protected writes use a pre-hardened temporary file plus atomic replace, and static SRI is verified from artifact bytes.

**Tech Stack:** Python 3.13, pytest, PowerShell/Windows ACLs, Docker Compose, Node test runner.

**Spec:** `docs/superpowers/specs/2026-11-01-p0-security-merge-review-design.md`

## Global Constraints

- Preserve Docker volumes and application data.
- Never print or persist plaintext credentials outside the protected `.env`.
- Do not ship an online rotation command without durable recovery semantics.
- Keep `pypdf>=6.21.4`, `authlib>=1.9.1`, and `joserfc>=1.7.6`.

## Review Focus

- A repeated-character or reused secret must not pass startup validation.
- An ACL failure must occur before new secret bytes reach a temporary or final file.
- The unsupported rotation command must be rejected without mutating state.
- SRI must be derived from Git artifact bytes, not newline-converted working-tree assumptions.
- Managed operations must protect `.env` before any read, including a separate ConfigRoot.
- Raw Compose URL passwords must be restricted to URI-safe ASCII.
- Fresh provisioning must still synchronize encoded database and broker URLs.

---

### Task 1: Remove unsafe online rotation surface

**Files:**
- Modify: `tests/test_local_security.py`
- Modify: `oncall_ops/cli.py`
- Modify: `scripts/oncall-windows.ps1`
- Modify: `README.md`
- Modify: `docs/api-and-security.md`
- Delete: `oncall_ops/local_security.py`

- [ ] Add a CLI behavior test that `rotate-credentials` is rejected and run it RED.
- [ ] Remove the parser/dispatch/wrapper/documentation surface and obsolete rotation tests/module.
- [ ] Run focused operational CLI and startup tests GREEN.

### Task 2: Centralize strong credential policy

**Files:**
- Modify: `tests/test_harden_local_env.py`
- Modify: `tests/test_operations_cli.py`
- Modify: `app/local_env.py`
- Modify: `oncall_ops/cli.py`

- [ ] Add tests for repeated-character secrets and reuse across API/infrastructure credentials; run RED.
- [ ] Implement one policy helper and use it in provisioning and managed startup.
- [ ] Run both focused suites GREEN.

### Task 3: Protect before writing secrets

**Files:**
- Modify: `tests/test_harden_local_env.py`
- Modify: `app/local_env.py`

- [ ] Add an ordering/failure test proving ACL protection precedes secret writes; run RED.
- [ ] Replace direct writes with a protected temporary file, fsync, atomic replace, and final ACL verification.
- [ ] Run the focused suite GREEN and inspect the actual Windows DACL.

### Task 4: Verify static resource integrity

**Files:**
- Create: `tests/static_integrity.test.cjs`
- Modify: `static/index.html`

- [ ] Add a Node test that hashes every local integrity-bearing asset and run it RED on the mismatched CSS.
- [ ] Correct the CSS SRI value and run all Node tests GREEN.

### Task 5: Final branch verification and merge

**Files:**
- Review all changed files.

- [ ] Run Python suites, Ruff/Bandit, dependency audit, Compose config, Node tests, and a secret scan.
- [ ] Request a fresh whole-branch review and resolve Critical/Important findings.
- [ ] Merge `codex/p0-local-security` into `master` locally, rerun verification on the merged tree, and leave services stopped.
