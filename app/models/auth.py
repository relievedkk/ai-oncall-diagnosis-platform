"""Request and response models for browser authentication."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, SecretStr

USERNAME_PATTERN = r"^[A-Za-z0-9_.@-]+$"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64, pattern=USERNAME_PATTERN)
    password: SecretStr = Field(min_length=1, max_length=128)
    remember: bool = False


class UserInfo(BaseModel):
    id: int
    username: str
    display_name: str
    role: str


class LoginResponse(BaseModel):
    user: UserInfo
    expires_at: datetime
