"""请求数据模型

定义 API 请求的 Pydantic 模型
"""

from pydantic import BaseModel, ConfigDict, Field

SESSION_ID_PATTERN = r"^[A-Za-z0-9_.:-]+$"


class ChatRequest(BaseModel):
    """对话请求"""

    id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=SESSION_ID_PATTERN,
        description="会话 ID",
        alias="Id",
    )
    question: str = Field(
        ..., min_length=1, max_length=10_000, description="用户问题", alias="Question"
    )

    model_config = ConfigDict(
        populate_by_name=True,
        extra="forbid",
        json_schema_extra={
            "example": {
                "Id": "session-123",
                "Question": "什么是向量数据库？",
            },
        },
    )


class ClearRequest(BaseModel):
    """清空会话请求"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        pattern=SESSION_ID_PATTERN,
        description="会话 ID",
        alias="sessionId",
    )
