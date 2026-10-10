"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import aiops, alerts, auth, chat, debug, file, health, metrics
from app.config import config
from app.core.auth import ApiKeyMiddleware, validate_security_configuration
from app.core.checkpoint import checkpoint_manager
from app.core.metrics import prometheus_http_middleware
from app.core.milvus_client import milvus_manager
from app.core.security import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.db.session import database_manager
from app.services.alert_diagnosis_service import alert_diagnosis_service
from app.services.auth_service import auth_service
from app.services.queue_observer import queue_observer


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    validate_security_configuration()
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    if config.expose_api_docs:
        logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    else:
        logger.info("📚 API 文档默认关闭（EXPOSE_API_DOCS=false）")

    logger.info("🔌 正在连接 PostgreSQL...")
    await database_manager.initialize()
    await auth_service.ensure_bootstrap_user()
    logger.info("✅ Web 登录账户已就绪")
    await checkpoint_manager.initialize()
    logger.info("✅ LangGraph 检查点存储已就绪")

    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")

    await queue_observer.start()

    logger.info("=" * 60)

    yield

    # 关闭时执行
    await alert_diagnosis_service.shutdown()
    await queue_observer.stop()
    await checkpoint_manager.close()
    logger.info("🔌 正在关闭 Milvus 连接...")
    milvus_manager.close()
    await database_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="融合 RAG、MCP 与可观测数据的 AI OnCall 诊断平台",
    lifespan=lifespan,
    docs_url="/docs" if config.expose_api_docs else None,
    redoc_url="/redoc" if config.expose_api_docs else None,
    openapi_url="/openapi.json" if config.expose_api_docs else None,
)

# Record real request rate, status, and latency for Prometheus alerting.
app.middleware("http")(prometheus_http_middleware)

# 配置中间件
# 注册顺序：后注册者先执行（Starlette 栈式）。CORS 最外层处理预检，ApiKey 次之校验受保护路由
app.add_middleware(ApiKeyMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=config.trusted_host_list,
)

# CORS 来源策略：
#   - 配置了 CORS_ALLOWED_ORIGINS -> 只允许这些来源（生产推荐）
#   - 未配置 + DEBUG=true           -> 回退 ["*"] 仅用于本机调试
#   - 未配置 + 非调试                -> 空列表（默认拒绝所有跨域，仅同源可访问）
_cors_origins = config.cors_origin_list
if not _cors_origins and config.debug:
    _cors_origins = ["*"]
    logger.warning(
        "[CORS] 未配置 CORS_ALLOWED_ORIGINS 且 DEBUG=true，临时开放所有来源供调试，生产环境必须显式配置"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=config.cors_method_list,
    allow_headers=config.cors_header_list,
)
app.add_middleware(SecurityHeadersMiddleware)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(metrics.router, tags=["监控指标"])
app.include_router(auth.router, prefix="/api", tags=["账号认证"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])
app.include_router(alerts.router, prefix="/api", tags=["Alertmanager"])
app.include_router(debug.router, prefix="/api", tags=["本地故障注入"])

# 挂载静态文件
static_dir = "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/")
async def root():
    """返回首页"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {
        "message": f"Welcome to {config.app_name} API",
        "version": config.app_version,
        **({"docs": "/docs"} if config.expose_api_docs else {}),
    }


@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    """Serve the operator login page, or return to the app if already signed in."""
    token = request.cookies.get(config.session_cookie_name, "")
    if config.web_login_enabled and await auth_service.get_principal(token):
        return RedirectResponse(url="/", status_code=303)
    login_path = os.path.join(static_dir, "login.html")
    return FileResponse(login_path)


if __name__ == "__main__":
    from app.server import main

    main()
