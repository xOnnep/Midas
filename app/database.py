"""SQLite database and models."""
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import Column, Integer, BigInteger, String, Text, Boolean, DateTime, ForeignKey
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class TelegramSession(Base):
    """Saved Telegram session (one account)."""
    __tablename__ = "telegram_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), nullable=False, default="default")
    session_path = Column(String(512), nullable=False)  # path to .session file
    api_id = Column(Integer, nullable=False)
    api_hash = Column(String(64), nullable=False)
    phone = Column(String(32), nullable=True)
    user_id = Column(BigInteger, nullable=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    tasks = relationship("MailingTask", back_populates="session")


class MailingTask(Base):
    """One mailing task."""
    __tablename__ = "mailing_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("telegram_sessions.id"), nullable=False)
    name = Column(String(256), nullable=False)

    # JSON: list of chat IDs (int) or usernames (str)
    target_chat_ids = Column(Text, nullable=False, default="[]")

    # Message type: "text" | "html" | "markdown" | "forward" | "media"
    message_type = Column(String(32), nullable=False, default="text")
    # For text/html/markdown: message text
    message_text = Column(Text, nullable=True)
    # For forward: JSON {"chat_id": ..., "message_id": ...}
    forward_source = Column(Text, nullable=True)
    # For media: path or file_id (optional)
    media_path = Column(String(512), nullable=True)
    media_caption = Column(Text, nullable=True)

    interval_min_sec = Column(Integer, nullable=False, default=900)   # 20-900, 900 = 15 мин
    interval_max_sec = Column(Integer, nullable=False, default=900)
    daily_limit = Column(Integer, nullable=False, default=0)  # 0 = no limit
    total_limit = Column(Integer, nullable=False, default=0)  # 0 = no limit

    status = Column(String(32), nullable=False, default="paused")  # active | paused | completed | error
    error_message = Column(Text, nullable=True)
    sent_today = Column(Integer, default=0)
    sent_total = Column(Integer, default=0)
    last_sent_at = Column(DateTime, nullable=True)
    last_reset_at = Column(DateTime, nullable=True)  # last daily reset

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    session = relationship("TelegramSession", back_populates="tasks")
    logs = relationship(
        "SendLog", back_populates="task", order_by="SendLog.created_at",
        cascade="all, delete-orphan",
    )
    error_logs = relationship(
        "ErrorLog", back_populates="task",
        cascade="all, delete-orphan",
    )


class SendLog(Base):
    """Log of one send attempt."""
    __tablename__ = "send_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("mailing_tasks.id"), nullable=False)
    chat_id = Column(String(128), nullable=False)
    success = Column(Boolean, nullable=False)
    message = Column(Text, nullable=True)  # error message or "OK"
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("MailingTask", back_populates="logs")


class ErrorLog(Base):
    """Global error log (FloodWait, etc.)."""
    __tablename__ = "error_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(Integer, ForeignKey("mailing_tasks.id"), nullable=True)
    level = Column(String(16), nullable=False, default="error")
    message = Column(Text, nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("MailingTask", back_populates="error_logs")


# Helpers for JSON columns
def get_target_chat_ids(task: MailingTask) -> list:
    try:
        return json.loads(task.target_chat_ids or "[]")
    except Exception:
        return []


def set_target_chat_ids(task: MailingTask, ids: list):
    task.target_chat_ids = json.dumps(ids)


def get_forward_source(task: MailingTask) -> Optional[dict]:
    try:
        return json.loads(task.forward_source) if task.forward_source else None
    except Exception:
        return None


def set_forward_source(task: MailingTask, data: Optional[dict]):
    task.forward_source = json.dumps(data) if data else None


# Engine and session
_engine = None
_async_session = None


def get_engine(database_url: str):
    global _engine
    if _engine is None:
        _engine = create_async_engine(database_url, echo=False)
    return _engine


def get_session_factory(database_url: str):
    global _async_session
    if _async_session is None:
        engine = get_engine(database_url)
        _async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return _async_session


async def init_db(database_url: str):
    engine = get_engine(database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
