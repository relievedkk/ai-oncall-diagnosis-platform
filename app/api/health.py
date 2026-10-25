"""Liveness and dependency-aware readiness endpoints."""

import asyncio
import os
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.db.session import database_manager
from app.services.queue_observer import queue_observer

router = APIRouter()


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Report that the API process is alive without probing dependencies."""
    result = {"status": "alive", "service": config.app_name, "version": config.app_version}
    if os.environ.get("ONCALL_OPERATIONS_RUN_ID"):
        result["operations_run_id"] = os.environ["ONCALL_OPERATIONS_RUN_ID"]
    return result


async def _readiness_response() -> JSONResponse:
    health_data: dict[str, Any] = {
        "service": config.app_name,
        "version": config.app_version,
    }

    try:
        milvus_healthy = await asyncio.wait_for(
            asyncio.to_thread(milvus_manager.health_check), config.readiness_timeout_seconds
        )
    except Exception as exc:
        logger.warning("Milvus 健康检查失败: {}", exc)
        milvus_healthy = False
    health_data["milvus"] = {
        "status": "connected" if milvus_healthy else "disconnected",
    }

    try:
        postgres_healthy = await asyncio.wait_for(
            database_manager.health_check(), config.readiness_timeout_seconds
        )
    except Exception as exc:
        logger.warning("PostgreSQL 就绪探针失败: {}", type(exc).__name__)
        postgres_healthy = False
    health_data["postgresql"] = {
        "status": "connected" if postgres_healthy else "disconnected",
    }

    rabbitmq_healthy = False
    queues: dict[str, Any] = {}
    try:
        queues = await asyncio.wait_for(
            queue_observer.rabbitmq_queues(), config.readiness_timeout_seconds
        )
        rabbitmq_healthy = True
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError) as exc:
        logger.warning("RabbitMQ 健康检查失败: {}", exc)
    health_data["rabbitmq"] = {
        "status": "authenticated" if rabbitmq_healthy else "disconnected",
        "queues": queues,
    }

    workers_healthy = all(
        queues.get(name, {}).get("consumers", 0) > 0
        for name in (config.celery_critical_queue, config.celery_default_queue)
    )
    health_data["diagnosis_workers"] = {"status": "available" if workers_healthy else "unavailable"}
    ready = milvus_healthy and postgres_healthy and rabbitmq_healthy and workers_healthy
    status_code = 200 if ready else 503
    health_data["status"] = "ready" if ready else "not_ready"
    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "message": "服务已就绪" if ready else "依赖服务未就绪",
            "data": health_data,
        },
    )


@router.get("/ready")
async def readiness() -> JSONResponse:
    """Probe PostgreSQL, Milvus and authenticated RabbitMQ management access."""
    return await _readiness_response()


@router.get("/health")
async def health_check() -> JSONResponse:
    """Backward-compatible alias for the readiness endpoint."""
    return await _readiness_response()
