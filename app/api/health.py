"""健康检查接口"""

import asyncio
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

from app.config import config
from app.core.milvus_client import milvus_manager
from app.db.session import database_manager

router = APIRouter()


@router.get("/health")
async def health_check():

    """健康检查接口
    检查服务状态和数据库连接状态

    Returns:
        JSONResponse: 健康检查结果
    """
    # 检查服务基本状态
    health_data: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "service": config.app_name,
        "version": config.app_version,
        "status": "healthy"
    }

    # 检查 Milvus 连接状态
    try:
        milvus_healthy = milvus_manager.health_check()
        milvus_status: str = "connected" if milvus_healthy else "disconnected"
        milvus_message: str = "Milvus 连接正常" if milvus_healthy else "Milvus 连接异常"
        health_data["milvus"] = {
            "status": milvus_status,
            "message": milvus_message
        }
    except Exception as e:
        logger.warning(f"Milvus 健康检查失败: {e}")
        health_data["milvus"] = {
            "status": "error",
            "message": f"Milvus 检查失败: {str(e)}"
        }

    postgres_healthy = await database_manager.health_check()
    health_data["postgresql"] = {
        "status": "connected" if postgres_healthy else "disconnected",
        "message": "PostgreSQL 连接正常" if postgres_healthy else "PostgreSQL 连接异常",
    }

    broker_url = urlparse(config.celery_broker_url)
    rabbitmq_healthy = False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(broker_url.hostname or "127.0.0.1", broker_url.port or 5672),
            timeout=2,
        )
        writer.close()
        await writer.wait_closed()
        rabbitmq_healthy = True
    except (OSError, TimeoutError):
        pass
    health_data["rabbitmq"] = {
        "status": "connected" if rabbitmq_healthy else "disconnected",
        "message": "RabbitMQ 连接正常" if rabbitmq_healthy else "RabbitMQ 连接异常",
    }

    # 判断整体健康状态
    overall_status = "healthy"
    status_code = 200

    # 如果 Milvus 不可用，服务不可用
    if (
        health_data["milvus"]["status"] != "connected"
        or health_data["postgresql"]["status"] != "connected"
        or health_data["rabbitmq"]["status"] != "connected"
    ):
        overall_status = "unhealthy"
        status_code = 503
        health_data["error"] = "依赖服务不可用"

    health_data["status"] = overall_status

    return JSONResponse(
        status_code=status_code,
        content={
            "code": status_code,
            "message": "服务运行正常" if overall_status == "healthy" else "服务不可用",
            "data": health_data
        }
    )
