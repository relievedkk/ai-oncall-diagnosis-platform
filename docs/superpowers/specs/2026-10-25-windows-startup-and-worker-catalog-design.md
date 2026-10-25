# Windows Startup And Worker Catalog Design

## Intent

Make the supported Windows local deployment start reliably on a cold CLS MCP proxy and ensure every worker loads the release-matched service catalog. Success means the standard background start reports ready without repeated short probes cancelling CLS initialization, and worker containers no longer depend on Docker Desktop sharing a Windows host directory.

## Scope and constraints

- Keep ordinary health checks strict; only managed startup receives a longer MCP protocol budget.
- Tolerate only transient Windows sharing violations while atomically publishing runtime JSON; persistent permission failures must still surface.
- A failed periodic `supervisor.json` snapshot must be retried on the next tick instead of terminating owned services; control writes and final stop publication remain strict.
- Keep the existing 120-second total startup deadline and ownership checks.
- Use a parent-generated 128-bit run identity, rather than launcher PID equality, as the cross-process ownership proof on Windows.
- Launch the background Supervisor in a detached process group that can outlive the invoking PowerShell/Codex job.
- Treat the virtual-environment launcher as an intermediary: its exit is not failure while the bounded run-id readiness window remains.
- Keep worker releases immutable and rollback-compatible.
- Do not introduce a mutable Docker volume, a host bind mount, or a runtime copy step for the catalog.
- Do not change secrets, business data, or database volumes.

## Approaches considered

1. **Selected: detached run-id-owned Supervisor, startup-only probe budget, bounded atomic-file retry, plus image-owned catalog.** The parent generates and injects a 128-bit run-id and starts a detached process group that is allowed to leave the invoking Windows job. The start loop uses a 15-second timeout, retries transient `PermissionError` failures around atomic replace, and points workers at `/app/config/service_catalog.json`.
2. Seed a named Docker volume from the host before workers start. This preserves runtime mutability but adds ownership, cleanup, seed ordering, and rollback consistency risks.
3. Keep the Windows bind mount and retry or document Docker Desktop file sharing. This does not remove the observed failure mode and can continue hiding the valid image file with an empty directory.

## Verification design

- A CLI regression test simulates a cold MCP probe that succeeds only when startup grants at least 10 seconds; background `_start` must return ready.
- The same CLI test uses deliberately different launcher and Supervisor PIDs and requires matching the injected run-id end to end.
- An atomic-state regression test injects two transient sharing violations and requires the complete JSON publication to succeed on the third attempt.
- A rendered Compose test supplies a Windows host config root and verifies workers use the absolute image catalog path with no `/app-config/config` mount.
- Targeted tests, the full Python and Node suites, then a real managed Windows start validate the result. Live acceptance includes non-empty worker catalog entries and healthy API/MCP endpoints.
