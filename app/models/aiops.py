"""
AIOps 请求和响应模型
"""

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


class DiagnosisJobCreated(BaseModel):
    """Durable task identity, independent of its current subscription."""

    diagnosis_id: str
    events_url: str
