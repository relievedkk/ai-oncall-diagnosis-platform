# P0 Security Merge Review Design

## Intent

Merge the independently reviewed local-security changes without shipping an online
credential-rotation path that can strand an existing installation after a crash or
leave native credential consumers stale.

## Approaches considered

- **Selected: safe partial merge.** Keep fresh-install credential generation,
  fail-closed managed startup, Compose hardening, ACL protection, and dependency
  upgrades. Remove the online rotation command and document that legacy default
  installations require a separately designed offline migration procedure.
- **Deferred: redesign rotation now.** A correct implementation needs an exclusive
  lock, protected durable journal, phase reconciliation, maintenance drain, restart
  of every credential consumer, crash injection, and live Docker tests. This is too
  large to hide inside a merge correction.
- **Rejected: merge as-is after stopping today's services.** Stopping the current
  machine does not make the command safe for future executions or other users.

## Goals

- A fresh local setup generates independent random infrastructure credentials and
  writes them only to a protected `.env`.
- Managed startup rejects missing, default, reused, placeholder, or trivially weak
  infrastructure credentials.
- Secret-bearing files are protected before secret bytes are written and are
  replaced atomically.
- The vulnerable dependency versions remain upgraded.
- Static-resource SRI values match the exact Git artifact bytes.
- No supported command claims to rotate live credentials until crash recovery and
  full-runtime restart are implemented and tested.

## Non-goals

- Live or offline migration of legacy volumes.
- K8s, SSO, multi-tenant RBAC, or broader P1/P2 hardening.
- Starting services again after the merge; the user requested a stopped review
  window.

## Verification

- Focused RED/GREEN tests for command removal, credential policy, protected-write
  ordering, and SRI verification.
- Project Python and frontend suites that can run without external cloud calls.
- Ruff, Bandit, dependency audit, Compose parsing, Git diff/secret scan, and final
  independent code review before local merge to `master`.
