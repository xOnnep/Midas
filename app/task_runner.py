"""Background task runner: queue, random delay, FloodWait, daily limit."""
import asyncio
import logging
import random
from datetime import datetime, date
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import FloodWaitError

from config import get_settings
from app.database import (
    get_session_factory,
    MailingTask,
    TelegramSession,
    SendLog,
    ErrorLog,
    get_target_chat_ids,
)
from app.telegram_client import create_client_for_session, send_or_forward_one

logger = logging.getLogger(__name__)

_running = False
_tasks_event: Optional[asyncio.Event] = None


def _need_reset_daily(task: MailingTask) -> bool:
    if not task.last_reset_at:
        return True
    return date.today() > task.last_reset_at.date()


async def _reset_daily_if_needed(session: AsyncSession, task: MailingTask):
    if not _need_reset_daily(task):
        return
    task.sent_today = 0
    task.last_reset_at = datetime.utcnow()
    await session.commit()


async def _log_error(session_factory, task_id: Optional[int], message: str, details: str = None):
    async with session_factory() as session:
        log = ErrorLog(task_id=task_id, level="error", message=message, details=details)
        session.add(log)
        await session.commit()


async def _run_one_task(session_factory, task_id: int):
    async with session_factory() as db:
        r = await db.get(MailingTask, task_id)
        if not r or r.status != "active":
            return
        task = r
        ts = await db.get(TelegramSession, task.session_id)
        if not ts or not ts.is_active:
            task.status = "error"
            task.error_message = "Сессия не найдена или отключена"
            await db.commit()
            return
    client = await create_client_for_session(ts)
    if not client:
        async with session_factory() as db:
            t = await db.get(MailingTask, task_id)
            if t:
                t.status = "error"
                t.error_message = "Не удалось подключиться к аккаунту"
                await db.commit()
        return
    try:
        async with session_factory() as db:
            task = await db.get(MailingTask, task_id)
            if not task or task.status != "active":
                return
            await _reset_daily_if_needed(db, task)
            if task.daily_limit and task.sent_today >= task.daily_limit:
                return
            if task.total_limit and task.sent_total >= task.total_limit:
                task.status = "completed"
                await db.commit()
                return
            chat_ids = get_target_chat_ids(task)
            if not chat_ids:
                logger.warning("Task %s: нет чатов в target_chat_ids", task_id)
                return
            chat_id = random.choice(chat_ids)
        logger.info("Рассылка: задача %s → %s (тип: %s)", task_id, chat_id, task.message_type)
        success, msg = False, ""
        try:
            success, msg = await send_or_forward_one(client, task, chat_id)
        except FloodWaitError as e:
            logger.warning("Task %s FloodWait %s sec", task_id, e.seconds)
            await _log_error(session_factory, task_id, "FloodWait", str(e.seconds))
            await asyncio.sleep(e.seconds)
            success, msg = await send_or_forward_one(client, task, chat_id)
        except Exception as e:
            err_str = str(e)
            if "Could not find the input entity" in err_str:
                logger.warning("Задача %s: источник пересылки недоступен для аккаунта", task_id)
            else:
                logger.exception("Task %s send error: %s", task_id, e)
            success, msg = False, err_str
        if success:
            logger.info("Отправлено: задача %s → %s", task_id, chat_id)
        else:
            logger.error("Задача %s не отправила в %s: %s", task_id, chat_id, msg)
        async with session_factory() as db:
            task = await db.get(MailingTask, task_id)
            if not task:
                return
            log = SendLog(task_id=task_id, chat_id=str(chat_id), success=success, message=msg)
            db.add(log)
            if success:
                task.sent_today = (task.sent_today or 0) + 1
                task.sent_total = (task.sent_total or 0) + 1
                task.last_sent_at = datetime.utcnow()
                if task.total_limit and task.sent_total >= task.total_limit:
                    task.status = "completed"
                if task.daily_limit and task.sent_today >= task.daily_limit:
                    pass
            else:
                task.error_message = msg
                await _log_error(session_factory, task_id, "Отправка не удалась", msg)
            await db.commit()
    finally:
        await client.disconnect()


async def run_one_send_test(task_id: int) -> tuple[bool, str]:
    """
    Одна попытка отправки по задаче. Возвращает (успех, сообщение).
    Для команды /task N test — сразу видно, почему не отправляет.
    """
    settings = get_settings()
    session_factory = get_session_factory(settings.database_url)
    async with session_factory() as db:
        task = await db.get(MailingTask, task_id)
        if not task:
            return False, "Задача не найдена"
        ts = await db.get(TelegramSession, task.session_id)
        if not ts or not ts.is_active:
            return False, "Сессия не найдена или отключена"
        chat_ids = get_target_chat_ids(task)
        if not chat_ids:
            return False, "Нет чатов в рассылке"
    client = await create_client_for_session(ts)
    if not client:
        return False, "Не удалось подключиться к аккаунту (проверь сессию)"
    try:
        chat_id = chat_ids[0]
        success, msg = await send_or_forward_one(client, task, chat_id)
        return success, msg
    except FloodWaitError as e:
        return False, f"FloodWait: подожди {e.seconds} сек"
    except Exception as e:
        return False, str(e)
    finally:
        await client.disconnect()


def _wake_worker():
    """Разбудить воркер (например, после запуска задачи) — первое сообщение уйдёт сразу."""
    if _tasks_event:
        _tasks_event.set()


async def _worker(session_factory):
    global _running
    settings = get_settings()
    while _running:
        async with session_factory() as db:
            # Берём задачу, которую дольше всего не отправляли (или ещё ни разу) — все активные задачи по очереди
            r = await db.execute(
                select(MailingTask)
                .where(MailingTask.status == "active")
                .order_by(MailingTask.last_sent_at.asc(), MailingTask.id.asc())
                .limit(1)
            )
            task = r.scalar_one_or_none()
            if not task:
                _tasks_event.clear()
                try:
                    await asyncio.wait_for(_tasks_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
                continue
            task_id = task.id
        logger.info("Воркер взял задачу %s", task_id)
        await _run_one_task(session_factory, task_id)
        async with session_factory() as db:
            t = await db.get(MailingTask, task_id)
            if not t:
                continue
            interval_min = max(20, (t.interval_min_sec if t else 900))
            interval_max = max(interval_min, (t.interval_max_sec if t else 900))
            delay = random.uniform(interval_min, interval_max)
        # Ждём интервал, но можно прервать (wake) при старте новой задачи — тогда первое сообщение уйдёт сразу
        _tasks_event.clear()
        try:
            await asyncio.wait_for(_tasks_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass


async def start_runner():
    global _running, _tasks_event
    if _running:
        return
    _running = True
    _tasks_event = asyncio.Event()
    settings = get_settings()
    session_factory = get_session_factory(settings.database_url)
    asyncio.create_task(_worker(session_factory))


def stop_runner():
    global _running
    _running = False
