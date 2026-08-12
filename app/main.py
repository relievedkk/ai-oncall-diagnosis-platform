"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os

from app.config import config
from loguru import logger
from app.api import chat, health, file, aiops, metrics
from app.core.milvus_client import milvus_manager
from app.core.auth import ApiKeyMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    
    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")
    
    logger.info("=" * 60)
    
    yield
    
    # 关闭时执行
    logger.info("🔌 正在关闭 Milvus 连接...")
    milvus_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="基于 LangChain 的智能oncall运维系统",
    lifespan=lifespan
)

# 配置中间件
# 注册顺序：后注册者先执行（Starlette 栈式）。CORS 最外层处理预检，ApiKey 次之校验受保护路由
app.add_middleware(ApiKeyMiddleware)

# CORS 来源策略：
#   - 配置了 CORS_ALLOWED_ORIGINS -> 只允许这些来源（生产推荐）
#   - 未配置 + DEBUG=true           -> 回退 ["*"] 仅用于本机调试
#   - 未配置 + 非调试                -> 空列表（默认拒绝所有跨域，仅同源可访问）
_cors_origins = config.cors_origin_list
if not _cors_origins and config.debug:
    _cors_origins = ["*"]
    logger.warning("[CORS] 未配置 CORS_ALLOWED_ORIGINS 且 DEBUG=true，临时开放所有来源供调试，生产环境必须显式配置")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=config.cors_method_list,
    allow_headers=config.cors_header_list,
)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(metrics.router, tags=["监控指标"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维"])

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
        "docs": "/docs"
    }


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
        log_level="info"
    )
