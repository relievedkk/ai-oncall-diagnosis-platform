"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Dict, Any
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用配置"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # 应用配置
    app_name: str = "SuperBizAgent"
    app_version: str = "1.0.0"
    debug: bool = False
    host: str = "0.0.0.0"
    port: int = 9900

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    dashscope_embedding_model: str = "text-embedding-v4"  # v4 支持多种维度（默认 1024）

    # Milvus 配置
    milvus_host: str = "localhost"
    milvus_port: int = 19530
    milvus_timeout: int = 10000  # 毫秒

    # RAG 配置
    rag_top_k: int = 3
    rag_model: str = "qwen-max"  # 使用快速响应模型，不带扩展思考

    # 文档分块配置
    chunk_max_size: int = 800
    chunk_overlap: int = 100

    # MCP 服务配置（transport: stdio | sse | streamable-http）
    # 腾讯云托管 MCP 的 URL 通常含 /sse/，需使用 sse；本地 FastMCP 使用 streamable-http
    mcp_cls_transport: str = "streamable-http"
    mcp_cls_url: str = "http://localhost:8003/mcp"
    mcp_monitor_transport: str = "streamable-http"
    mcp_monitor_url: str = "http://localhost:8004/mcp"

    # Prometheus
    prometheus_base_url: str = "http://127.0.0.1:9090"
    prometheus_request_timeout: float = 10.0

    # 安全配置
    # API Key 认证：为空且 auth_enabled=True 时拒绝所有受保护请求（防误配裸奔）
    api_key: str = ""
    auth_enabled: bool = True
    # 允许索引的根目录（逗号分隔），index_directory 仅允许访问这些目录的子树
    allowed_index_roots: str = "uploads,aiops-docs"

    # CORS 配置
    # 允许的跨域来源（逗号分隔）；生产应填具体域名（如 https://oncall.example.com）
    # 留空表示禁止所有跨域，仅同源可访问
    cors_allowed_origins: str = ""
    # 允许的方法（逗号分隔）；按最小权限只开实际需要的方法
    cors_allowed_methods: str = "GET,POST,DELETE"
    # 允许的请求头（逗号分隔）；按最小权限只开实际需要的头
    cors_allowed_headers: str = "Content-Type,X-API-Key,Accept"

    @property
    def cors_origin_list(self) -> list[str]:
        """解析允许的跨域来源列表。"""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def cors_method_list(self) -> list[str]:
        """解析允许的方法列表。"""
        return [m.strip() for m in self.cors_allowed_methods.split(",") if m.strip()]

    @property
    def cors_header_list(self) -> list[str]:
        """解析允许的请求头列表。"""
        return [h.strip() for h in self.cors_allowed_headers.split(",") if h.strip()]

    @property
    def allowed_index_root_list(self) -> list[str]:
        """解析允许的索引根目录列表。"""
        return [r.strip() for r in self.allowed_index_roots.split(",") if r.strip()]

    @property
    def mcp_servers(self) -> Dict[str, Dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            }
        }


# 全局配置实例
config = Settings()
