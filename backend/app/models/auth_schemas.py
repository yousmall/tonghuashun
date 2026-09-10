"""用户认证与对话历史接口的数据契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class Credentials(BaseModel):
    """注册和登录共同使用的账号密码。"""

    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=8, max_length=128)

    @field_validator("username")
    @classmethod
    def normalize_username(cls, value: str) -> str:
        return value.strip().lower()


class UserSummary(BaseModel):
    id: int
    username: str
    created_at: datetime


class AuthResponse(BaseModel):
    access_token: str
    session_beacon_token: str
    token_type: str = "bearer"
    expires_at: datetime
    user: UserSummary


class ConversationSummary(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int
    last_message: str | None = None


class HistoryMessage(BaseModel):
    id: int
    role: str
    content: str
    payload: dict[str, Any] | None = None
    created_at: datetime


class ConversationDetail(BaseModel):
    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    messages: list[HistoryMessage] = Field(default_factory=list)
