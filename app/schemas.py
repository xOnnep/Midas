"""Pydantic schemas for API."""
from datetime import datetime
from typing import Optional, List, Any

from pydantic import BaseModel, Field


class TelegramConnectRequest(BaseModel):
    api_id: int
    api_hash: str
    phone: str
    name: str = "default"


class TelegramConnectResponse(BaseModel):
    success: bool
    message: str
    session_id: Optional[int] = None
    requires_code: bool = False


class TelegramCodeRequest(BaseModel):
    phone: str
    name: str = "default"
    code: str


class SessionInfo(BaseModel):
    id: int
    name: str
    phone: Optional[str] = None
    user_id: Optional[int] = None
    is_active: bool
    created_at: datetime


class DialogItem(BaseModel):
    id: str
    title: str
    type: str


class TaskCreate(BaseModel):
    session_id: int
    name: str = "Новая рассылка"
    target_chat_ids: List[Any] = Field(default_factory=list)
    message_type: str = "text"
    message_text: Optional[str] = None
    forward_source: Optional[dict] = None
    media_path: Optional[str] = None
    media_caption: Optional[str] = None
    interval_min_sec: int = Field(60, ge=20, le=900)
    interval_max_sec: int = Field(120, ge=20, le=900)
    daily_limit: int = Field(0, ge=0)
    total_limit: int = Field(0, ge=0)


class TaskUpdate(BaseModel):
    name: Optional[str] = None
    target_chat_ids: Optional[List[Any]] = None
    message_type: Optional[str] = None
    message_text: Optional[str] = None
    forward_source: Optional[dict] = None
    media_path: Optional[str] = None
    media_caption: Optional[str] = None
    interval_min_sec: Optional[int] = Field(None, ge=20, le=900)
    interval_max_sec: Optional[int] = Field(None, ge=20, le=900)
    daily_limit: Optional[int] = Field(None, ge=0)
    total_limit: Optional[int] = Field(None, ge=0)


class TaskStatus(BaseModel):
    id: int
    name: str
    session_id: int
    status: str
    sent_today: int
    sent_total: int
    daily_limit: int
    total_limit: int
    last_sent_at: Optional[datetime] = None
    error_message: Optional[str] = None
    created_at: datetime


class TaskDetail(TaskStatus):
    target_chat_ids: List[Any]
    message_type: str
    message_text: Optional[str] = None
    forward_source: Optional[dict] = None
    media_path: Optional[str] = None
    media_caption: Optional[str] = None
    interval_min_sec: int
    interval_max_sec: int


class SendLogItem(BaseModel):
    id: int
    task_id: int
    chat_id: str
    success: bool
    message: Optional[str] = None
    created_at: datetime


class ErrorLogItem(BaseModel):
    id: int
    task_id: Optional[int] = None
    level: str
    message: str
    details: Optional[str] = None
    created_at: datetime
