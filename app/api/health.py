"""Liveness and dependency-aware readiness endpoints."""

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
    return {"status": "alive", "service": config.app_name, "version": config.app_version}


async def _readiness_response() -> JSONResponse:
    health_data: dict[str, Any] = {
        "service": config.app_name,
        "version": config.app_version,
    }

    try:
        milvus_healthy = milvus_manager.health_check()
    except Exception as exc:
        logger.warning("Milvus 健康检查失败: {}", exc)
        milvus_healthy = False
    health_data["milvus"] = {
        "status": "connected" if milvus_healthy else "disconnected",
    }

    postgres_healthy = await database_manager.health_check()
    health_data["postgresql"] = {
        "status": "connected" if postgres_healthy else "disconnected",
    }

    rabbitmq_healthy = False
    queues: dict[str, Any] = {}
    try:
        queues = await queue_observer.rabbitmq_queues()
        rabbitmq_healthy = True
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.warning("RabbitMQ 健康检查失败: {}", exc)
    health_data["rabbitmq"] = {
        "status": "authenticated" if rabbitmq_healthy else "disconnected",
        "queues": queues,
    }

    ready = milvus_healthy and postgres_healthy and rabbitmq_healthy
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
