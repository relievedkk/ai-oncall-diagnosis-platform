import asyncio

import pytest

from app.harness.contracts import (
    ExecutionResult,
    PreparedAction,
    RestartInstanceIntent,
)
from app.harness.gateway import DryRunRemediationAdapter, RemediationGateway


def restart_intent():
    return RestartInstanceIntent(
        action_type="restart_instance",
        service_name="orders",
        instance_id="orders-1",
        reason="The instance is unhealthy.",
    )


class FakeAdapter:
    def __init__(
        self,
        *,
        observation=None,
        rollback_success=True,
        prepare_error=None,
        execute_delay=0,
    ):
        self.observation = {"known": True, "success": True} if observation is None else observation
        self.rollback_success = rollback_success
        self.prepare_error = prepare_error
        self.execute_delay = execute_delay
        self.prepare_calls = 0
        self.execute_calls = 0
        self.observe_calls = 0
        self.rollback_calls = 0

    async def prepare(self, intent):
        self.prepare_calls += 1
        if self.prepare_error is not None:
            raise self.prepare_error
        return PreparedAction(
            intent=intent,
            pre_change_snapshot={"known": True},
            verification_spec={"kind": "generic"},
            rollback_token="opaque-test-token",
        )

    async def execute(self, prepared):
        self.execute_calls += 1
        if self.execute_delay:
            await asyncio.sleep(self.execute_delay)
        return ExecutionResult(success=True, details={"applied": True})

    async def observe(self, prepared):
        self.observe_calls += 1
        return self.observation

    async def rollback(self, prepared):
        self.rollback_calls += 1
        return ExecutionResult(
            success=self.rollback_success,
            details={"rolled_back": self.rollback_success},
        )


async def collect(gateway):
    return [event async for event in gateway.run(restart_intent())]


def statuses(events):
    return [event.status.value for event in events]


@pytest.mark.asyncio
async def test_dry_run_success_emits_executing_verifying_succeeded():
    adapter = DryRunRemediationAdapter()

    events = await collect(RemediationGateway(adapter))

    assert statuses(events) == ["executing", "verifying", "succeeded"]
    assert events[-1].result.details["dry_run"] is True
    assert events[-1].verification.observation["dry_run"] is True


@pytest.mark.asyncio
async def test_verification_failure_rolls_back_exactly_once():
    adapter = FakeAdapter(observation={"known": True, "success": False})

    events = await collect(RemediationGateway(adapter))

    assert statuses(events) == [
        "executing",
        "verifying",
        "rolling_back",
        "rolled_back",
    ]
    assert adapter.execute_calls == 1
    assert adapter.rollback_calls == 1


@pytest.mark.asyncio
async def test_rollback_failure_emits_failed_and_never_retries():
    adapter = FakeAdapter(
        observation={"known": True, "success": False},
        rollback_success=False,
    )

    events = await collect(RemediationGateway(adapter))

    assert statuses(events) == [
        "executing",
        "verifying",
        "rolling_back",
        "failed",
    ]
    assert adapter.execute_calls == 1
    assert adapter.rollback_calls == 1


@pytest.mark.asyncio
async def test_prepare_failure_executes_nothing():
    adapter = FakeAdapter(prepare_error=RuntimeError("prepare refused"))

    events = await collect(RemediationGateway(adapter))

    assert statuses(events) == ["failed"]
    assert events[0].error == "prepare failed: prepare refused"
    assert adapter.execute_calls == 0
    assert adapter.rollback_calls == 0


@pytest.mark.asyncio
async def test_execution_timeout_observed_success_reconciles_to_verification():
    adapter = FakeAdapter(execute_delay=0.05)

    events = await collect(
        RemediationGateway(
            adapter,
            execution_timeout_seconds=0.01,
            verification_timeout_seconds=0.1,
        )
    )

    assert statuses(events) == [
        "executing",
        "reconciling",
        "verifying",
        "succeeded",
    ]
    assert adapter.execute_calls == 1
    assert adapter.observe_calls == 1
    assert adapter.rollback_calls == 0


@pytest.mark.asyncio
async def test_execution_timeout_missing_observation_fails_without_blind_retry():
    adapter = FakeAdapter(execute_delay=0.05, observation={})

    events = await collect(
        RemediationGateway(
            adapter,
            execution_timeout_seconds=0.01,
            verification_timeout_seconds=0.1,
        )
    )

    assert statuses(events) == ["executing", "reconciling", "failed"]
    assert events[-1].error == "execution outcome remains unknown"
    assert events[-1].requires_manual_review is True
    assert adapter.execute_calls == 1
    assert adapter.observe_calls == 1
    assert adapter.rollback_calls == 0


@pytest.mark.asyncio
async def test_successful_execution_with_missing_observation_reconciles_without_rollback():
    adapter = FakeAdapter(observation={})

    events = await collect(RemediationGateway(adapter))

    assert statuses(events) == ["executing", "reconciling", "failed"]
    assert events[-1].error == "execution outcome remains unknown"
    assert events[-1].requires_manual_review is True
    assert adapter.execute_calls == 1
    assert adapter.observe_calls == 1
    assert adapter.rollback_calls == 0


@pytest.mark.asyncio
async def test_reconcile_observes_prepared_action_without_reexecuting():
    adapter = FakeAdapter(observation={"known": True, "success": True})
    prepared = await adapter.prepare(restart_intent())
    gateway = RemediationGateway(adapter)

    events = [event async for event in gateway.reconcile(prepared)]

    assert statuses(events) == ["verifying", "succeeded"]
    assert adapter.execute_calls == 0
    assert adapter.observe_calls == 1


@pytest.mark.asyncio
async def test_reconcile_unknown_observation_requires_manual_review_without_reexecution():
    adapter = FakeAdapter(observation={"known": False})
    prepared = await adapter.prepare(restart_intent())

    events = [event async for event in RemediationGateway(adapter).reconcile(prepared)]

    assert statuses(events) == ["failed"]
    assert events[-1].requires_manual_review is True
    assert events[-1].error == "execution outcome remains unknown"
    assert adapter.execute_calls == adapter.rollback_calls == 0


@pytest.mark.asyncio
async def test_timeout_confirmed_failed_execution_cannot_succeed_via_passing_guards():
    class FailedOperationAdapter(FakeAdapter):
        async def prepare(self, intent):
            prepared = await super().prepare(intent)
            return prepared.model_copy(update={"verification_spec": {"kind": "restart_instance"}})

    adapter = FailedOperationAdapter(
        execute_delay=0.05,
        observation={
            "known": True,
            "success": False,
            "replacement_observed": True,
            "guards_passed": True,
        },
    )

    events = await collect(RemediationGateway(adapter, execution_timeout_seconds=0.01))

    assert statuses(events) == ["executing", "reconciling", "failed"]
    assert events[-1].result.success is False
    assert adapter.rollback_calls == 0


@pytest.mark.asyncio
async def test_prepare_for_another_target_is_rejected_before_execution():
    class WrongTargetAdapter(FakeAdapter):
        async def prepare(self, intent):
            return await super().prepare(intent.model_copy(update={"service_name": "payments"}))

    adapter = WrongTargetAdapter()
    events = await collect(RemediationGateway(adapter))

    assert statuses(events) == ["failed"]
    assert adapter.execute_calls == 0
