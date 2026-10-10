"""
AIOps 请求和响应模型
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.request import SESSION_ID_PATTERN


class AIOpsRequest(BaseModel):
    """AIOps 诊断请求"""

    session_id: str | None = Field(
        default="default",
        min_length=1,
        max_length=128,
        pattern=SESSION_ID_PATTERN,
        description="会话ID，用于追踪诊断历史",
    )

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "session_id": "session-123",
            },
        },
    )


class AlertInfo(BaseModel):
    """告警信息"""

    alertname: str
    severity: str
    instance: str
    duration: str
    description: str | None = None


class DiagnosisResponse(BaseModel):
    """诊断响应（非流式）"""

    code: int = 200
    message: str = "success"
    data: dict[str, Any]

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "code": 200,
                "message": "success",
                "data": {
                    "status": "completed",
                    "target_alert": {"alertname": "HighCPUUsage", "severity": "critical"},
                    "diagnosis": {
                        "root_cause": "数据库连接池耗尽",
                        "recommendations": ["扩容数据库连接池", "优化SQL查询"],
                    },
                },
            }
        },
    )
