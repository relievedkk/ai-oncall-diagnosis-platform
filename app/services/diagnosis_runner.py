"""Shared diagnosis execution logic used by Celery and inline tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack, aclosing
from typing import Any
from uuid import uuid4

from loguru import logger

from app.config import config
from app.core.checkpoint import diagnosis_checkpoint_scope
from app.core.evidence import decode_evidence_result, valid_evidence
from app.core.metrics import AIOPS_ACTIVE, AIOPS_DIAGNOSES
from app.harness.contracts import parse_action_intent
from app.models.alerts import AlertmanagerAlert
from app.repositories.diagnosis import ClaimResult, DiagnosisLeaseLostError, DiagnosisRepository
from app.services.aiops_service import aiops_service
from app.services.remediation_service import RemediationService, remediation_service
from app.services.service_catalog import service_catalog


class DiagnosisBusyError(RuntimeError):
    """Another worker currently owns a valid execution lease."""


class DiagnosisNotFoundError(RuntimeError):
    """The broker message references a missing diagnosis."""


def build_alert_task(alert: AlertmanagerAlert, service_name: str, service: dict[str, Any]) -> str:
    payload = {
        "alertname": alert.labels.get("alertname", "unknown"),
        "severity": alert.labels.get("severity", "unknown"),
        "service": service_name,
        "instance": alert.labels.get("instance", ""),
        "starts_at": alert.starts_at,
        "summary": alert.annotations.get("summary", ""),
        "description": alert.annotations.get("description", ""),
        "prometheus_job": service.get("prometheus_job", service_name),
        "metrics": service.get("metrics", {}),
        "cls": service.get("cls", {}),
        "owner": service.get("owner", "unknown"),
    }
    return (
        "对下列 Alertmanager firing 告警执行真实诊断。\n"
        f"告警与服务映射：{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "要求：\n"
        "1. 先用 Prometheus 告警工具确认告警仍在 firing。\n"
        "2. 只查询该服务及告警时间窗口的真实指标和日志；优先使用上述 metrics 和 CLS 映射。"
        "日志使用 query_service_logs(service_name, start_time, end_time)，由服务目录确定 CLS 主题，"
        "不要猜测 TopicId 或使用示例值。\n"
        "3. 每个结论都要写明证据来源、查询时间范围和关键数值。\n"
        "4. 工具无法获取数据时必须明确写明证据缺失，禁止编造。\n"
        "5. 输出 Markdown 报告，包含告警摘要、指标证据、日志证据、根因判断、处理建议和恢复验证项。"
    )


class DiagnosisRunner:
    def __init__(
        self,
        repository: DiagnosisRepository,
        remediation: RemediationService | None = None,
    ) -> None:
        self.repository = repository
        self.remediation = remediation or remediation_service

    async def run(
        self, diagnosis_id: str, celery_task_id: str, worker_name: str,
        *, execution_token: str | None = None,
    ) -> str:
        execution_token = execution_token or uuid4().hex
        claim: ClaimResult = await self.repository.claim_job(
            diagnosis_id, celery_task_id, worker_name, execution_token=execution_token
        )
        if claim == "completed":
            return "already_completed"
        if claim == "busy":
            raise DiagnosisBusyError(f"diagnosis {diagnosis_id} has an active lease")
        if claim == "missing":
            raise DiagnosisNotFoundError(f"diagnosis {diagnosis_id} does not exist")

        job = await self.repository.get_job(diagnosis_id)
        if job is None:  # pragma: no cover - guarded by the claim transaction
            raise DiagnosisNotFoundError(diagnosis_id)

        trigger = str(job.get("trigger", "unknown"))
        AIOPS_DIAGNOSES.labels(trigger=trigger, status="started").inc()
        AIOPS_ACTIVE.labels(trigger=trigger).inc()
        lease_lost = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(
                diagnosis_id, celery_task_id, execution_token, asyncio.current_task(), lease_lost
            ),
            name=f"diagnosis-heartbeat-{diagnosis_id}",
        )
        error_event_seen = False
        timeout_seconds = min(
            (limit for limit in (
                config.celery_task_soft_time_limit, config.celery_task_time_limit
            ) if limit and limit > 0),
            default=None,
        )
        deadline = asyncio.timeout(timeout_seconds)
        try:
            async with deadline, AsyncExitStack() as checkpoint_scope:
                checkpoint_scope.enter_context(
                    diagnosis_checkpoint_scope(self.repository, diagnosis_id, execution_token)
                )
                session_id = f"aiops:{diagnosis_id}"
                resume = (
                    job["attempt_count"] > 1
                    and await aiops_service.has_pending_execution(session_id)
                )
                resume_kwargs = {"resume": True} if resume else {}
                if trigger == "manual":
                    events = aiops_service.diagnose(session_id=session_id, **resume_kwargs)
                else:
                    alert = AlertmanagerAlert.model_validate(job["alert"])
                    service_name, service = service_catalog.resolve(alert.labels)
                    prompt = build_alert_task(alert, service_name, service)
                    events = aiops_service.execute(prompt, session_id=session_id, **resume_kwargs)

                events = await checkpoint_scope.enter_async_context(aclosing(events))
                async for event in events:
                    durable_event = {
                        **event,
                        "diagnosis_id": diagnosis_id,
                        "attempt": job["attempt_count"],
                        "worker": worker_name,
                    }
                    await self.repository.record_event(
                        diagnosis_id, durable_event, execution_token=execution_token
                    )
                    if event.get("type") == "remediation_proposed":
                        await self._submit_remediation(diagnosis_id, event, execution_token)
                    if event.get("type") == "error":
                        error_event_seen = True
                        raise RuntimeError(str(event.get("message", "diagnosis failed")))

                # A completed job must never leave planned steps behind. The report event
                # normally carries precise skip reasons; this is the final invariant guard.
                await self.repository.skip_remaining_steps(
                    diagnosis_id, "诊断工作流已结束，该步骤未执行", execution_token=execution_token
                )
                await self.repository.update_job(
                    diagnosis_id, status="completed", error=None, execution_token=execution_token
                )
                AIOPS_DIAGNOSES.labels(trigger=trigger, status="completed").inc()
                return "completed"
        except asyncio.CancelledError:
            if lease_lost.is_set():
                raise DiagnosisLeaseLostError("diagnosis heartbeat could not renew ownership") from None
            await self.repository.record_event(
                diagnosis_id,
                {
                    "type": "error",
                    "stage": "interrupted",
                    "message": "diagnosis worker was cancelled",
                    "diagnosis_id": diagnosis_id,
                },
                execution_token=execution_token,
            )
            await self.repository.update_job(
                diagnosis_id, status="interrupted", error="diagnosis worker was cancelled",
                execution_token=execution_token,
            )
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="interrupted").inc()
            raise
        except DiagnosisLeaseLostError:
            raise
        except Exception as exc:
            error_message = (
                f"diagnosis deadline exceeded ({timeout_seconds:g}s)"
                if deadline.expired() else str(exc)
            )
            if not error_event_seen:
                await self.repository.record_event(
                    diagnosis_id,
                    {
                        "type": "error",
                        "stage": "timeout" if deadline.expired() else "exception",
                        "message": error_message,
                        "diagnosis_id": diagnosis_id,
                    },
                    execution_token=execution_token,
                )
            await self.repository.update_job(
                diagnosis_id, status="failed", error=error_message, execution_token=execution_token
            )
            AIOPS_DIAGNOSES.labels(trigger=trigger, status="failed").inc()
            logger.exception("Diagnosis execution failed: {}", diagnosis_id)
            if deadline.expired():
                raise TimeoutError(error_message) from exc
            raise
        finally:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
            AIOPS_ACTIVE.labels(trigger=trigger).dec()

    async def _submit_remediation(
        self,
        diagnosis_id: str,
        event: dict[str, Any],
        execution_token: str,
    ) -> None:
        try:
            intent = parse_action_intent(event.get("remediation"))
            job = await self.repository.get_job(diagnosis_id)
            evidence = _remediation_evidence(job or {})
            await self.remediation.propose(
                diagnosis_id,
                intent,
                evidence,
                requested_by="agent",
                execution_token=execution_token,
            )
        except DiagnosisLeaseLostError:
            raise
        except Exception as exc:
            await self.repository.record_event(
                diagnosis_id,
                {
                    "type": "harness_error",
                    "stage": "remediation_proposal",
                    "message": f"Remediation proposal was not accepted: {str(exc)[:256]}",
                    "diagnosis_id": diagnosis_id,
                },
                execution_token=execution_token,
            )

    async def _heartbeat_loop(
        self, diagnosis_id: str, celery_task_id: str, execution_token: str,
        owner_task: asyncio.Task | None, lease_lost: asyncio.Event,
    ) -> None:
        while True:
            await asyncio.sleep(config.diagnosis_heartbeat_seconds)
            try:
                renewed = await self.repository.heartbeat(
                    diagnosis_id, celery_task_id, execution_token=execution_token
                )
            except Exception:
                renewed = False
            if not renewed:
                lease_lost.set()
                if owner_task is not None:
                    owner_task.cancel()
                return


def _remediation_evidence(job: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    for item in job.get("evidence", []):
        payload = item.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        result = payload.get("result")
        # Decode the complete item so wrapper-level errors and demo provenance
        # survive alongside the inner observation payload.
        parsed_result = _decode_evidence_result(item)
        success = valid_evidence(item)
        normalized.append(
            {
                "evidence_type": str(item.get("evidence_type", "unknown")),
                "source": str(item.get("source", "unknown")),
                "success": success,
                "collected_at": item.get("collected_at"),
                "summary": str(result or payload.get("message", ""))[:256],
                "payload": parsed_result,
            }
        )
    return normalized


def _decode_evidence_result(value: Any) -> dict[str, Any] | None:
    """Decode MCP text blocks without treating arbitrary/error text as data."""
    return decode_evidence_result(value)
