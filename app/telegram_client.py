"""Telegram client wrapper: auth, session, dialogs, send/forward."""
import asyncio
import logging
import random
from pathlib import Path
from typing import Optional, List, Any

logger = logging.getLogger(__name__)

# Слова/фразы, при наличии которых сообщение не пересылается (регистр не важен)
FORWARD_BLOCKLIST = ("шерлок", "sherlock", "бот шерлок", "sherlock bot")

from telethon import TelegramClient
from telethon.sessions import SQLiteSession
from telethon.tl.types import Channel, Chat, User
from telethon.errors import FloodWaitError, RPCError

from config import get_settings
from app.database import TelegramSession as DbSession, get_forward_source, get_target_chat_ids


def _session_path(session_dir: Path, name: str) -> Path:
    return session_dir / name.replace(" ", "_").lower()


async def create_client_for_session(db_session: DbSession) -> Optional[TelegramClient]:
    """Create Telethon client for a saved session."""
    settings = get_settings()
    path = Path(db_session.session_path)
    if not path.is_absolute():
        path = (settings.sessions_dir.resolve() / path.name).with_suffix("")
    if not path.suffix:
        path = path.with_suffix(".session")
    client = TelegramClient(
        str(path),
        db_session.api_id,
        db_session.api_hash,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        return client
    except Exception:
        await client.disconnect()
        raise


async def start_login(api_id: int, api_hash: str, phone: str, name: str) -> dict:
    """
    Start login flow. Returns dict with:
    - success, requires_code, session_id, message, client (optional, only if already logged in)
    """
    settings = get_settings()
    settings.sessions_dir.mkdir(parents=True, exist_ok=True)
    path = _session_path(settings.sessions_dir, name)
    client = TelegramClient(str(path), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        sent = await client.send_code_request(phone)
        return {
            "success": True,
            "requires_code": True,
            "session_id": 0,  # no DB row yet
            "message": "Код отправлен в Telegram",
            "phone": phone,
            "phone_code_hash": sent.phone_code_hash,
            "client": client,
            "path": str(path),
            "api_id": api_id,
            "api_hash": api_hash,
            "name": name,
        }
    me = await client.get_me()
    return {
        "success": True,
        "requires_code": False,
        "message": f"Уже авторизован: {me.phone}",
        "client": client,
        "path": str(path),
        "api_id": api_id,
        "api_hash": api_hash,
        "name": name,
        "user_id": me.id,
        "phone": me.phone,
    }


# In-memory store for pending logins (phone_code_hash etc.)
_pending_logins: dict = {}  # session_key -> dict from start_login


def _pending_key(phone: str, name: str) -> str:
    return f"{phone}:{name}"


def save_pending_login(phone: str, name: str, data: dict):
    _pending_logins[_pending_key(phone, name)] = data


def get_pending_login(phone: str, name: str) -> Optional[dict]:
    return _pending_logins.get(_pending_key(phone, name))


def pop_pending_login(phone: str, name: str) -> Optional[dict]:
    return _pending_logins.pop(_pending_key(phone, name), None)


async def complete_login_with_code(
    phone: str, name: str, code: str, db_session_id: Optional[int] = None
) -> dict:
    """Complete login using code; optionally save to DB with db_session_id (create new row)."""
    pending = pop_pending_login(phone, name)
    if not pending or not pending.get("requires_code"):
        return {"success": False, "message": "Сессия не найдена или код уже введён"}
    client: TelegramClient = pending["client"]
    try:
        await client.sign_in(phone, code, phone_code_hash=pending["phone_code_hash"])
    except Exception as e:
        return {"success": False, "message": str(e)}
    me = await client.get_me()
    from app.database import get_session_factory, TelegramSession
    from sqlalchemy import select

    session_factory = get_session_factory(get_settings().database_url)
    async with session_factory() as session:
        row = TelegramSession(
            name=pending["name"],
            session_path=pending["path"],
            api_id=pending["api_id"],
            api_hash=pending["api_hash"],
            phone=me.phone,
            user_id=me.id,
            is_active=True,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    await client.disconnect()
    return {
        "success": True,
        "message": "Авторизация успешна",
        "session_id": row.id,
        "user_id": me.id,
        "phone": me.phone,
    }


async def get_dialogs(db_session: DbSession) -> List[dict]:
    """Get list of chats/channels for selection."""
    client = await create_client_for_session(db_session)
    if not client:
        return []
    try:
        result = []
        async for d in client.iter_dialogs():
            e = d.entity
            if isinstance(e, Channel):
                tid = e.id if e.megagroup else f"-100{e.id}"
                result.append({
                    "id": str(tid),
                    "title": d.name or (e.title if hasattr(e, "title") else str(e.id)),
                    "type": "channel" if not e.megagroup else "chat",
                })
            elif isinstance(e, Chat):
                result.append({"id": str(-e.id), "title": d.name or e.title, "type": "chat"})
            elif isinstance(e, User):
                result.append({"id": str(e.id), "title": d.name or (e.first_name or "") + " " + (e.last_name or ""), "type": "user"})
        return result
    finally:
        await client.disconnect()


def _normalize_chat_id(chat_id: Any):
    if isinstance(chat_id, str) and chat_id.lstrip("-").isdigit():
        return int(chat_id)
    return chat_id


async def _resolve_peer(client: TelegramClient, chat_id: Any):
    """Resolve chat_id to entity (Telethon needs entity for channels)."""
    chat_id = _normalize_chat_id(chat_id)
    try:
        return await client.get_input_entity(chat_id)
    except ValueError as e:
        if "Could not find the input entity" in str(e):
            logger.debug("Не найден entity для %s: %s", chat_id, e)
        else:
            logger.error("Не удалось найти чат/юзера %s: %s", chat_id, e)
        raise
    except Exception as e:
        logger.error("Не удалось найти чат/юзера %s: %s", chat_id, e)
        raise


async def send_or_forward_one(
    client: TelegramClient,
    task: "MailingTask",
    chat_id: Any,
) -> tuple[bool, str]:
    """
    Send or forward one message to chat_id. Returns (success, message).
    Uses forward when message_type == "forward" to preserve premium emoji.
    """
    from app.database import get_forward_source, get_target_chat_ids, MailingTask

    msg_type = task.message_type

    try:
        peer = await _resolve_peer(client, chat_id)
        if msg_type == "forward":
            src = get_forward_source(task)
            if not src:
                return False, "forward_source not set"
            try:
                from_peer = await _resolve_peer(client, src["chat_id"])
            except ValueError as e:
                if "Could not find the input entity" in str(e):
                    logger.warning("Источник пересылки недоступен (chat_id=%s)", src["chat_id"])
                    return False, "Аккаунт не видит источник сообщения. Зайди в этот чат/канал с аккаунта рассылки или пересоздай задачу."
                raise
            msg_id = int(src["message_id"])
            msgs = await client.get_messages(from_peer, ids=msg_id)
            if not msgs:
                return False, "Исходное сообщение не найдено"
            msg = msgs[0] if isinstance(msgs, list) else msgs
            grouped_id = getattr(msg, "grouped_id", None)

            def _collect_text(m):
                return (getattr(m, "text", None) or "") + " " + (getattr(m, "message", None) or "")

            if grouped_id:
                group = []
                async for m in client.iter_messages(from_peer, min_id=msg_id - 25, max_id=msg_id + 25):
                    if getattr(m, "grouped_id", None) == grouped_id:
                        group.append(m)
                group.sort(key=lambda m: m.id)
                message_ids = [m.id for m in group]
                text_to_check = " ".join(_collect_text(m) for m in group)
                logger.info("Пересылаю альбом (%s сообщ.) из %s в %s", len(message_ids), src["chat_id"], chat_id)
            else:
                message_ids = [msg_id]
                text_to_check = _collect_text(msg)
                logger.info("Пересылаю сообщение %s из %s в %s", msg_id, src["chat_id"], chat_id)

            # Не пересылаем сообщения с запрещёнными словами
            text_lower = text_to_check.lower()
            for phrase in FORWARD_BLOCKLIST:
                if phrase.lower() in text_lower:
                    logger.warning("Пропуск пересылки: в сообщении найдено запрещённое слово («%s»)", phrase)
                    return False, "Сообщение не пересылается: содержимое в блок-листе."

            await client.forward_messages(peer, message_ids, from_peer)
            return True, "OK"
        elif msg_type in ("text", "html", "markdown"):
            text = (task.message_text or "").strip()
            if not text:
                return False, "message_text пустой"
            if msg_type == "html":
                await client.send_message(peer, text, parse_mode="html")
            elif msg_type == "markdown":
                await client.send_message(peer, text, parse_mode="md")
            else:
                await client.send_message(peer, text)
            return True, "OK"
        elif msg_type == "media":
            path = task.media_path
            caption = task.media_caption or ""
            if path and Path(path).exists():
                await client.send_file(peer, path, caption=caption)
            else:
                return False, "media file not found"
            return True, "OK"
        else:
            return False, f"Unknown message_type: {msg_type}"
    except FloodWaitError as e:
        raise e
    except (ValueError, RPCError) as e:
        err = str(e)
        if "Could not find the input entity" in err:
            logger.warning("Источник пересылки недоступен для аккаунта (задача → %s)", chat_id)
            return False, "Аккаунт не видит источник сообщения. Зайди в этот чат/канал с аккаунта рассылки или пересоздай задачу."
        logger.error("Ошибка при отправке в %s: %s", chat_id, e)
        return False, err
    except Exception as e:
        err_msg = str(e)
        if "Could not find the input entity" in err_msg:
            logger.warning("Источник пересылки недоступен (задача → %s)", chat_id)
            return False, "Аккаунт не видит источник сообщения. Зайди в этот чат/канал с аккаунта рассылки или пересоздай задачу."
        logger.exception("Ошибка отправки в %s: %s", chat_id, e)
        return False, err_msg


# Type hint for MailingTask
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from app.database import MailingTask
