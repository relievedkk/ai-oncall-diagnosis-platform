"""配置管理模块

使用 Pydantic Settings 实现类型安全的配置管理
"""

from typing import Any

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
    app_name: str = "AI 智能运维与 OnCall 诊断平台"
    app_version: str = "1.2.1"
    debug: bool = False
    host: str = "127.0.0.1"
    port: int = 9900
    expose_api_docs: bool = False

    # PostgreSQL：持久化诊断任务、步骤、证据和报告
    database_url: str = "postgresql+asyncpg://oncall:oncall@127.0.0.1:5432/oncall"
    database_auto_create: bool = True
    database_connect_retries: int = 15
    database_connect_retry_seconds: float = 2.0
    database_pool_size: int = 10
    database_max_overflow: int = 20

    # Celery / RabbitMQ 独立诊断队列
    celery_broker_url: str = "amqp://oncall:oncall@127.0.0.1:5672//"
    celery_default_queue: str = "diagnosis.default"
    celery_critical_queue: str = "diagnosis.critical"
    celery_dead_letter_queue: str = "diagnosis.dead-letter"
    celery_task_soft_time_limit: int = 1740
    celery_task_time_limit: int = 1800
    celery_task_max_retries: int = 5
    diagnosis_lease_seconds: int = 90
    diagnosis_heartbeat_seconds: int = 30
    outbox_poll_interval_seconds: float = 1.0
    rabbitmq_management_url: str = "http://127.0.0.1:15672"
    rabbitmq_default_user: str = "oncall"
    rabbitmq_default_pass: str = "oncall"
    queue_observer_interval_seconds: float = 15.0

    # LangGraph durable checkpoints. The explicit URL uses psycopg syntax;
    # when empty it is derived from DATABASE_URL.
    langgraph_checkpoint_enabled: bool = True
    langgraph_checkpoint_database_url: str = ""

    # DashScope 配置
    dashscope_api_key: str = ""  # 默认空字符串，实际使用需从环境变量加载
    dashscope_model: str = "qwen-max"
    llm_request_timeout: float = 120.0
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

    # Alertmanager / service catalog
    alertmanager_webhook_token: str = ""
    service_catalog_path: str = "config/service_catalog.json"

    # Local end-to-end diagnostics only. Keep disabled outside an isolated test environment.
    fault_injection_enabled: bool = False

    # 安全配置
    # API Key 认证：为空且 auth_enabled=True 时拒绝所有受保护请求（防误配裸奔）
    api_key: str = ""
    auth_enabled: bool = True
    trusted_hosts: str = "localhost,127.0.0.1,host.docker.internal,testserver"
    max_request_body_bytes: int = 12 * 1024 * 1024
    max_upload_bytes: int = 10 * 1024 * 1024
    max_archive_uncompressed_bytes: int = 50 * 1024 * 1024
    rate_limit_enabled: bool = True
    rate_limit_default_per_minute: int = 120
    rate_limit_expensive_per_minute: int = 20
    rate_limit_upload_per_minute: int = 5
    rate_limit_login_per_minute: int = 5
    # Human browser authentication. API keys remain available for service calls.
    web_login_enabled: bool = False
    admin_username: str = "admin"
    admin_password: str = ""
    allow_weak_admin_password: bool = False
    session_cookie_name: str = "oncall_session"
    csrf_cookie_name: str = "oncall_csrf"
    session_ttl_hours: int = 8
    session_remember_days: int = 7
    session_cookie_secure: bool = False
    login_max_failures: int = 5
    login_lock_minutes: int = 15
    # 允许索引的根目录（逗号分隔），index_directory 仅允许访问这些目录的子树
    allowed_index_roots: str = "uploads,aiops-docs"

    # CORS 配置
    # 允许的跨域来源（逗号分隔）；生产应填具体域名（如 https://oncall.example.com）
    # 留空表示禁止所有跨域，仅同源可访问
    cors_allowed_origins: str = ""
    # 允许的方法（逗号分隔）；按最小权限只开实际需要的方法
    cors_allowed_methods: str = "GET,POST,DELETE"
    # 允许的请求头（逗号分隔）；按最小权限只开实际需要的头
    cors_allowed_headers: str = "Content-Type,X-API-Key,X-CSRF-Token,Accept"

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
    def trusted_host_list(self) -> list[str]:
        """解析允许的 Host 请求头列表。"""
        return [h.strip() for h in self.trusted_hosts.split(",") if h.strip()]

    @property
    def checkpoint_database_url(self) -> str:
        """Return a psycopg-compatible URL for LangGraph checkpoints."""
        if self.langgraph_checkpoint_database_url:
            return self.langgraph_checkpoint_database_url
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """获取完整的 MCP 服务器配置"""
        return {
            "cls": {
                "transport": self.mcp_cls_transport,
                "url": self.mcp_cls_url,
            },
            "monitor": {
                "transport": self.mcp_monitor_transport,
                "url": self.mcp_monitor_url,
            },
        }


# 全局配置实例
config = Settings()
