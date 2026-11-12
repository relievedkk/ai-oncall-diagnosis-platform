"""响应数据模型

定义 API 响应的 Pydantic 模型
"""

from typing import Any

from pydantic import BaseModel, Field


class SessionInfoResponse(BaseModel):
    """会话信息响应"""

    session_id: str = Field(..., description="会话 ID")
    message_count: int = Field(..., description="消息数量")
    history: list[dict[str, str]] = Field(..., description="历史消息列表")


class ApiResponse(BaseModel):
    """通用 API 响应"""

    status: str = Field(..., description="状态")
    message: str = Field(..., description="消息")
    data: Any | None = Field(None, description="数据")
