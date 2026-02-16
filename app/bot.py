"""Telegram bot (BotFather) — интерфейс для рассылки."""
from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram import MessageOriginChannel, MessageOriginUser, MessageOriginChat

from sqlalchemy import delete

from config import get_settings
from app.database import (
    get_session_factory,
    TelegramSession,
    MailingTask,
    SendLog,
    ErrorLog,
    get_target_chat_ids,
    set_target_chat_ids,
    get_forward_source,
    set_forward_source,
)
from app.telegram_client import (
    start_login,
    save_pending_login,
    complete_login_with_code,
    get_dialogs,
)
from app.task_runner import start_runner, _wake_worker, run_one_send_test


def _db():
    """Возвращает новую сессию БД (async context manager)."""
    factory = get_session_factory(get_settings().database_url)
    return factory()


# Состояние диалога по user_id (для /connect и /newtask)
_user_state: dict[int, dict[str, Any]] = {}


def _get_state(user_id: int) -> dict:
    if user_id not in _user_state:
        _user_state[user_id] = {}
    return _user_state[user_id]


def _clear_state(user_id: int):
    _user_state.pop(user_id, None)


# --- Handlers ---
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет. Я бот для рассылки из твоего Telegram-аккаунта.\n\n"
        "Команды:\n"
        "/connect — подключить аккаунт (API ID, API Hash, телефон)\n"
        "/sessions — список аккаунтов\n"
        "/deactivate N — отключить аккаунт N\n"
        "/tasks — список задач\n"
        "/newtask — создать задачу\n"
        "/edittask N — редактировать задачу N\n"
        "/task N — старт/пауза/удалить задачу N\n"
        "/dialogs N — диалоги аккаунта N (выбор чатов)\n"
        "/logs N — логи задачи N\n"
        "/errors — последние ошибки\n"
        "/cancel — отменить текущий ввод"
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else 0
    _clear_state(uid)
    await update.message.reply_text("Отменено.")


# --- Connect (пошагово: api_id -> api_hash -> phone -> code) ---
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = _get_state(uid)
    s["flow"] = "connect"
    s["step"] = "api_id"
    _user_state[uid] = s
    await update.message.reply_text(
        "Подключение аккаунта. Получи API ID и API Hash на https://my.telegram.org/apps\n"
        "Пришли мне **API ID** (только число):"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    uid = update.effective_user.id
    state = _get_state(uid)
    flow = state.get("flow")
    if not flow:
        return
    # Для message_content и edit_content принимаем и пересланные сообщения (без текста)
    text = (update.message.text or update.message.caption or "").strip()
    accept_no_text = (
        (flow == "newtask" and state.get("step") == "message_content")
        or (flow == "edittask" and state.get("step") == "edit_content")
    )
    if not text and not accept_no_text:
        return

    if flow == "connect":
        step = state.get("step")
        if step == "api_id":
            try:
                state["api_id"] = int(text)
                state["step"] = "api_hash"
                await update.message.reply_text("Теперь пришли **API Hash**:")
            except ValueError:
                await update.message.reply_text("Нужно число. Пришли API ID:")
            return
        if step == "api_hash":
            state["api_hash"] = text
            state["step"] = "phone"
            await update.message.reply_text("Пришли **номер телефона** (например +79001234567):")
            return
        if step == "phone":
            state["phone"] = text
            state["name"] = state.get("name") or "default"
            api_id = state["api_id"]
            api_hash = state["api_hash"]
            phone = state["phone"]
            name = state["name"]
            try:
                result = await start_login(api_id, api_hash, phone, name)
            except Exception as e:
                await update.message.reply_text(f"Ошибка: {e}")
                _clear_state(uid)
                return
            if not result.get("success"):
                await update.message.reply_text(result.get("message", "Ошибка"))
                _clear_state(uid)
                return
            if result.get("requires_code"):
                save_pending_login(phone, name, result)
                state["step"] = "code"
                await update.message.reply_text("Код отправлен в Telegram. Пришли **код** из приложения:")
            else:
                from app.database import TelegramSession
                async with _db() as db:
                    row = TelegramSession(
                        name=result["name"], session_path=result["path"],
                        api_id=result["api_id"], api_hash=result["api_hash"],
                        phone=result.get("phone"), user_id=result.get("user_id"), is_active=True,
                    )
                    db.add(row)
                    await db.commit()
                    await db.refresh(row)
                if result.get("client"):
                    await result["client"].disconnect()
                _clear_state(uid)
                await update.message.reply_text(f"Аккаунт подключён. Session ID: {row.id}")
            return
        if step == "code":
            code = text
            phone = state.get("phone")
            name = state.get("name", "default")
            result = await complete_login_with_code(phone, name, code)
            _clear_state(uid)
            if result.get("success"):
                await update.message.reply_text(f"Готово. Session ID: {result.get('session_id')}")
            else:
                await update.message.reply_text(result.get("message", "Ошибка"))
            return

    if flow == "newtask":
        step = state.get("step")
        if step == "session_id":
            try:
                state["session_id"] = int(text)
                state["step"] = "name"
                await update.message.reply_text("Название задачи:")
            except ValueError:
                await update.message.reply_text("Пришли число (ID сессии):")
            return
        if step == "name":
            state["name"] = text
            state["step"] = "message_content"
            await update.message.reply_text(
                "Пришли или перешли сообщение для рассылки: напиши текст или перешли пост/сообщение (из канала, чата или от пользователя)."
            )
            return
        if step == "message_content":
            msg = update.message
            # Пересланное сообщение (канал, пользователь или чат) — рассылаем как forward
            origin = getattr(msg, "forward_origin", None)
            if isinstance(origin, (MessageOriginChannel, MessageOriginUser, MessageOriginChat)):
                state["message_type"] = "forward"
                if isinstance(origin, MessageOriginChannel):
                    state["forward_chat_id"] = origin.chat.id
                elif isinstance(origin, MessageOriginUser):
                    state["forward_chat_id"] = origin.sender_user.id
                else:
                    state["forward_chat_id"] = origin.sender_chat.id
                state["forward_message_id"] = origin.message_id
                state["step"] = "target_chats"
                await update.message.reply_text("Пересланное сообщение принято — будет пересылаться как forward. Чаты: ID или @username через запятую:")
                return
            if getattr(msg, "forward_origin", None) is not None:
                await update.message.reply_text(
                    "Пересланное сообщение от анонимного админа нельзя использовать. Перешли пост из канала/чата/от пользователя или напиши текст."
                )
                return
            # Текст (написанное сообщение)
            if text:
                state["message_type"] = "text"
                state["message_text"] = text
                state["step"] = "target_chats"
                await update.message.reply_text("Текст принят. Чаты: ID или @username через запятую:")
                return
            await update.message.reply_text(
                "Нужен текст или пересланное сообщение. Напиши текст или перешли пост/сообщение сюда."
            )
            return
        if step == "target_chats":
            ids = [x.strip() for x in text.split(",") if x.strip()]
            result = []
            for x in ids:
                if x.lstrip("-").isdigit():
                    result.append(int(x))
                else:
                    # @username или channel → всегда с @ для Telethon
                    result.append(x if x.startswith("@") else f"@{x}")
            state["target_chat_ids"] = result
            state["step"] = "interval"
            await update.message.reply_text("Интервал в секундах (20–900). Раз в 15 мин: 900 900")
            return
        if step == "interval":
            parts = text.split()
            # Одно число: 15 = 15 мин (900 сек), 20–900 = интервал в секундах
            if len(parts) == 1 and text.strip().isdigit():
                n = int(text.strip())
                if n == 15:
                    state["interval_min_sec"] = 900
                    state["interval_max_sec"] = 900
                    state["step"] = "limits"
                    await update.message.reply_text("Интервал: раз в 15 мин. Лимиты: лимит в сутки и всего (0 = без лимита), например: 200 0")
                    return
                if 20 <= n <= 900:
                    state["interval_min_sec"] = n
                    state["interval_max_sec"] = n
                    state["step"] = "limits"
                    await update.message.reply_text(f"Интервал: раз в {n} сек. Лимиты: лимит в сутки и всего (0 = без лимита), например: 200 0")
                    return
            if len(parts) >= 2:
                try:
                    a, b = int(parts[0]), int(parts[1])
                    if 20 <= a <= 900 and 20 <= b <= 900:
                        state["interval_min_sec"] = min(a, b)
                        state["interval_max_sec"] = max(a, b)
                        state["step"] = "limits"
                        await update.message.reply_text("Лимиты: лимит в сутки и всего (0 = без лимита), например: 200 0")
                    else:
                        await update.message.reply_text("Числа от 20 до 900")
                except ValueError:
                    await update.message.reply_text("Два числа через пробел")
            else:
                await update.message.reply_text("Напиши 15 (раз в 15 мин) или два числа: мин_сек макс_сек")
            return
        if step == "limits":
            parts = text.split()
            if len(parts) >= 2:
                try:
                    state["daily_limit"] = int(parts[0])
                    state["total_limit"] = int(parts[1])
                    # создаём задачу
                    async with _db() as db:
                        task = MailingTask(
                            session_id=state["session_id"],
                            name=state["name"],
                            message_type=state.get("message_type", "text"),
                            message_text=state.get("message_text"),
                            media_path=state.get("media_path"),
                            media_caption=state.get("media_caption"),
                            interval_min_sec=state.get("interval_min_sec", 900),
                            interval_max_sec=state.get("interval_max_sec", 900),
                            daily_limit=state.get("daily_limit", 0),
                            total_limit=state.get("total_limit", 0),
                            status="paused",
                        )
                        set_target_chat_ids(task, state["target_chat_ids"])
                        if state.get("forward_chat_id") is not None:
                            set_forward_source(task, {"chat_id": state["forward_chat_id"], "message_id": state["forward_message_id"]})
                            task.message_type = "forward"
                        db.add(task)
                        await db.commit()
                        await db.refresh(task)
                    _clear_state(uid)
                    await update.message.reply_text(f"Задача создана. ID: {task.id}. Запуск: /task {task.id} start")
                except ValueError:
                    await update.message.reply_text("Два числа: daily_limit total_limit")
            else:
                await update.message.reply_text("Нужны два числа (0 = без лимита)")
            return

    if flow == "edittask":
        step = state.get("step")
        task_id = state.get("task_id")
        if not task_id:
            _clear_state(uid)
            return
        if step == "choice":
            c = text.strip()
            if c == "0":
                _clear_state(uid)
                await update.message.reply_text("Редактирование завершено.")
                return
            if c == "1":
                state["step"] = "edit_name"
                await update.message.reply_text("Новое название задачи:")
                return
            if c == "2":
                state["step"] = "edit_chats"
                await update.message.reply_text("Чаты: ID или @username через запятую:")
                return
            if c == "3":
                state["step"] = "edit_interval"
                await update.message.reply_text("Интервал: 15 (раз в 15 мин) или два числа мин макс (20–900):")
                return
            if c == "4":
                state["step"] = "edit_limits"
                await update.message.reply_text("Лимиты: лимит в сутки и всего (0 = без лимита), например: 200 0")
                return
            if c == "5":
                state["step"] = "edit_content"
                await update.message.reply_text("Пришли новый текст или перешли пост из канала — это будет отправляться в рассылке.")
                return
            await update.message.reply_text("Введи 1–5 или 0 (готово).")
            return
        if step == "edit_name":
            async with _db() as db:
                task = await db.get(MailingTask, task_id)
                if not task:
                    await update.message.reply_text("Задача не найдена.")
                    _clear_state(uid)
                    return
                task.name = text[:256]
                await db.commit()
            state["step"] = "choice"
            await _send_edit_menu(update, task_id)
            return
        if step == "edit_chats":
            ids = [x.strip() for x in text.split(",") if x.strip()]
            result = []
            for x in ids:
                if x.lstrip("-").isdigit():
                    result.append(int(x))
                else:
                    result.append(x if x.startswith("@") else f"@{x}")
            async with _db() as db:
                task = await db.get(MailingTask, task_id)
                if not task:
                    await update.message.reply_text("Задача не найдена.")
                    _clear_state(uid)
                    return
                set_target_chat_ids(task, result)
                await db.commit()
            state["step"] = "choice"
            await _send_edit_menu(update, task_id)
            return
        if step == "edit_interval":
            parts = text.split()
            if len(parts) == 1 and text.strip().isdigit():
                n = int(text.strip())
                if n == 15:
                    interval_min, interval_max = 900, 900
                elif 20 <= n <= 900:
                    interval_min, interval_max = n, n
                else:
                    await update.message.reply_text("Число от 20 до 900 (или 15 для 15 мин)")
                    return
            elif len(parts) >= 2:
                try:
                    a, b = int(parts[0]), int(parts[1])
                    if not (20 <= a <= 900 and 20 <= b <= 900):
                        await update.message.reply_text("Числа от 20 до 900")
                        return
                    interval_min, interval_max = min(a, b), max(a, b)
                except ValueError:
                    await update.message.reply_text("Два числа через пробел или одно: 15 (мин), 20–900 (сек)")
                    return
            else:
                await update.message.reply_text("Напиши 15 (15 мин), одно число 20–900 (сек) или два числа: мин макс")
                return
            async with _db() as db:
                task = await db.get(MailingTask, task_id)
                if not task:
                    await update.message.reply_text("Задача не найдена.")
                    _clear_state(uid)
                    return
                task.interval_min_sec = interval_min
                task.interval_max_sec = interval_max
                await db.commit()
            state["step"] = "choice"
            await update.message.reply_text(f"Интервал: {interval_min}–{interval_max} сек.")
            await _send_edit_menu(update, task_id)
            return
        if step == "edit_content":
            msg = update.message
            origin = getattr(msg, "forward_origin", None)
            if origin is not None and not isinstance(origin, (MessageOriginChannel, MessageOriginUser, MessageOriginChat)):
                await update.message.reply_text(
                    "Пересланное сообщение от анонимного админа нельзя использовать. Перешли пост из канала/чата/от пользователя или напиши текст."
                )
                return
            if isinstance(origin, (MessageOriginChannel, MessageOriginUser, MessageOriginChat)):
                if isinstance(origin, MessageOriginChannel):
                    chat_id = origin.chat.id
                elif isinstance(origin, MessageOriginUser):
                    chat_id = origin.sender_user.id
                else:
                    chat_id = origin.sender_chat.id
                async with _db() as db:
                    task = await db.get(MailingTask, task_id)
                    if not task:
                        await update.message.reply_text("Задача не найдена.")
                        _clear_state(uid)
                        return
                    task.message_type = "forward"
                    task.message_text = None
                    set_forward_source(task, {"chat_id": chat_id, "message_id": origin.message_id})
                    await db.commit()
                await update.message.reply_text("Теперь задача будет пересылать это пересланное сообщение.")
            elif text:
                async with _db() as db:
                    task = await db.get(MailingTask, task_id)
                    if not task:
                        await update.message.reply_text("Задача не найдена.")
                        _clear_state(uid)
                        return
                    task.message_type = "text"
                    task.message_text = text
                    task.forward_source = None
                    await db.commit()
                await update.message.reply_text("Теперь задача будет отправлять этот текст.")
            else:
                await update.message.reply_text("Пришли текст или перешли пост/сообщение.")
                return
            state["step"] = "choice"
            await _send_edit_menu(update, task_id)
            return
        if step == "edit_limits":
            parts = text.split()
            if len(parts) >= 2:
                try:
                    daily_limit = int(parts[0])
                    total_limit = int(parts[1])
                except ValueError:
                    await update.message.reply_text("Два числа: daily_limit total_limit")
                    return
            else:
                await update.message.reply_text("Нужны два числа (0 = без лимита)")
                return
            async with _db() as db:
                task = await db.get(MailingTask, task_id)
                if not task:
                    await update.message.reply_text("Задача не найдена.")
                    _clear_state(uid)
                    return
                task.daily_limit = daily_limit
                task.total_limit = total_limit
                await db.commit()
            state["step"] = "choice"
            await _send_edit_menu(update, task_id)
            return


async def _send_edit_menu(update: Update, task_id: int):
    """Показать меню редактирования задачи."""
    async with _db() as db:
        task = await db.get(MailingTask, task_id)
        if not task:
            await update.message.reply_text("Задача не найдена.")
            return
        chats = get_target_chat_ids(task)
        chats_preview = ", ".join(str(c) for c in chats[:3])
        if len(chats) > 3:
            chats_preview += f" … (+{len(chats) - 3})"
        interval = f"{task.interval_min_sec or 900}–{task.interval_max_sec or 900} сек"
        if task.message_type == "forward":
            src = get_forward_source(task)
            sends = f"пересланный пост (прем-эмодзи)" if src else "пост (не задан)"
        else:
            txt = (task.message_text or "")[:50]
            sends = f"текст: {txt}…" if len((task.message_text or "")) > 50 else f"текст: {txt or '—'}"
        menu = (
            f"Задача {task_id}: {task.name}\n"
            f"Отправляет: {sends}\n"
            f"Чаты: {chats_preview or '—'}\n"
            f"Интервал: {interval} | Лимиты: {task.daily_limit or 0}/{task.total_limit or 0}\n\n"
            "Что изменить? 1 — название, 2 — чаты, 3 — интервал, 4 — лимиты, 5 — что отправлять, 0 — готово"
        )
    await update.message.reply_text(menu)


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from sqlalchemy import select
    async with _db() as db:
        r = await db.execute(select(TelegramSession).order_by(TelegramSession.id))
        rows = r.scalars().all()
    if not rows:
        await update.message.reply_text("Нет аккаунтов. /connect чтобы добавить.")
        return
    lines = []
    for x in rows:
        st = "активен" if x.is_active else "выкл"
        lines.append(f"ID {x.id}: {x.name} | {x.phone or '—'} | {st}")
    await update.message.reply_text("\n".join(lines))


async def cmd_deactivate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Использование: /deactivate N")
        return
    try:
        sid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("N — число (ID сессии)")
        return
    async with _db() as db:
        row = await db.get(TelegramSession, sid)
        if not row:
            await update.message.reply_text("Сессия не найдена.")
            return
        row.is_active = False
        await db.commit()
    await update.message.reply_text(f"Сессия {sid} отключена.")


async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from sqlalchemy import select
    async with _db() as db:
        r = await db.execute(select(MailingTask).order_by(MailingTask.id.desc()))
        rows = r.scalars().all()
    if not rows:
        await update.message.reply_text("Нет задач. /newtask чтобы создать.")
        return
    lines = []
    for t in rows:
        lines.append(f"ID {t.id}: {t.name} | {t.status} | отправлено: {t.sent_today or 0}/{t.daily_limit or '∞'} сегодня, {t.sent_total or 0}/{t.total_limit or '∞'} всего")
    await update.message.reply_text("\n".join(lines))


async def cmd_newtask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    s = _get_state(uid)
    s["flow"] = "newtask"
    s["step"] = "session_id"
    await update.message.reply_text("ID сессии (аккаунта) для рассылки:")


async def cmd_edittask(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("Использование: /edittask N (ID задачи)")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("N — число (ID задачи из /tasks)")
        return
    async with _db() as db:
        task = await db.get(MailingTask, task_id)
        if not task:
            await update.message.reply_text("Задача не найдена.")
            return
    uid = update.effective_user.id
    s = _get_state(uid)
    s["flow"] = "edittask"
    s["step"] = "choice"
    s["task_id"] = task_id
    await _send_edit_menu(update, task_id)


async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 2:
        await update.message.reply_text("Использование: /task N start | pause | delete | status | test")
        return
    try:
        task_id = int(context.args[0])
        action = context.args[1].lower()
    except (ValueError, IndexError):
        await update.message.reply_text("Пример: /task 1 start")
        return
    if action not in ("start", "pause", "delete", "status", "test"):
        await update.message.reply_text("Действие: start, pause, delete, status или test")
        return
    async with _db() as db:
        task = await db.get(MailingTask, task_id)
        if not task:
            await update.message.reply_text("Задача не найдена.")
            return
        if action == "test":
            chats = get_target_chat_ids(task)
            await update.message.reply_text("Пробую отправить один раз…")
            success, msg = await run_one_send_test(task_id)
            if success:
                await update.message.reply_text(f"✅ Тест: сообщение отправлено в {chats[0]}")
            else:
                await update.message.reply_text(f"❌ Тест: {msg}")
            return
        if action == "status":
            chats = get_target_chat_ids(task)
            err = (task.error_message or "—")[:200]
            last = task.last_sent_at.strftime("%H:%M %d.%m") if task.last_sent_at else "никогда"
            chats_preview = ", ".join(str(c) for c in chats[:3]) if chats else "—"
            msg = (
                f"Задача {task_id}: {task.name}\n"
                f"Статус: {task.status}\n"
                f"Отправлено: всего {task.sent_total or 0}, сегодня {task.sent_today or 0}. Последняя: {last}\n"
                f"Чатов: {len(chats)} ({chats_preview})\n"
                f"Тип: {task.message_type}\n"
                f"Ошибка: {err}"
            )
            if task.status == "active" and (task.sent_total or 0) == 0 and (task.error_message or "").strip() == "":
                msg += "\n\n💡 Если сообщения не приходят — воркер мог ещё не взять эту задачу (при нескольких активных теперь очередь чередуется). Через минуту снова /task N status или смотри /errors."
            await update.message.reply_text(msg)
            return
        if action == "start":
            task.status = "active"
            task.error_message = None
            await db.commit()
            await start_runner()
            _wake_worker()
            chats = get_target_chat_ids(task)
            chats_preview = ", ".join(str(c) for c in chats[:5])
            if len(chats) > 5:
                chats_preview += f" … (+{len(chats) - 5})"
            interval = f"{task.interval_min_sec or 900}–{task.interval_max_sec or 900} сек"
            await update.message.reply_text(
                f"Задача {task_id} запущена.\n"
                f"Чаты: {chats_preview or '—'}\n"
                f"Интервал: {interval}.\n"
                f"Первое сообщение — в течение минуты. Логи: /logs {task_id}, ошибки: /errors"
            )
        elif action == "pause":
            task.status = "paused"
            await db.commit()
            await update.message.reply_text(f"Задача {task_id} на паузе.")
        elif action == "delete":
            await db.execute(delete(SendLog).where(SendLog.task_id == task_id))
            await db.execute(delete(ErrorLog).where(ErrorLog.task_id == task_id))
            await db.delete(task)
            await db.commit()
            await update.message.reply_text(f"Задача {task_id} удалена.")


async def cmd_dialogs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /dialogs N (ID сессии)")
        return
    try:
        sid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("N — число")
        return
    async with _db() as db:
        row = await db.get(TelegramSession, sid)
        if not row:
            await update.message.reply_text("Сессия не найдена.")
            return
    msg = await update.message.reply_text("Загрузка диалогов...")
    items = await get_dialogs(row)
    if not items:
        await msg.edit_text("Не удалось загрузить или пусто.")
        return
    lines = [f"{d['id']} — {d['title']} ({d['type']})" for d in items[:50]]
    await msg.edit_text("Чаты (id — название):\n" + "\n".join(lines) + ("\n..." if len(items) > 50 else ""))


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Использование: /logs N (ID задачи)")
        return
    try:
        task_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("N — число")
        return
    from sqlalchemy import select
    async with _db() as db:
        r = await db.execute(
            select(SendLog).where(SendLog.task_id == task_id).order_by(SendLog.created_at.desc()).limit(30)
        )
        rows = r.scalars().all()
    if not rows:
        await update.message.reply_text("Логов нет.")
        return
    lines = [f"{l.created_at} | {l.chat_id} | {'OK' if l.success else l.message}" for l in rows]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text)


async def cmd_errors(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from sqlalchemy import select
    async with _db() as db:
        r = await db.execute(select(ErrorLog).order_by(ErrorLog.created_at.desc()).limit(20))
        rows = r.scalars().all()
    if not rows:
        await update.message.reply_text("Ошибок нет.")
        return
    lines = [f"{e.created_at} | {e.message} | {e.details or ''}" for e in rows]
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."
    await update.message.reply_text(text)


def build_app(token: str) -> Application:
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("deactivate", cmd_deactivate))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("newtask", cmd_newtask))
    app.add_handler(CommandHandler("edittask", cmd_edittask))
    app.add_handler(CommandHandler("task", cmd_task))
    app.add_handler(CommandHandler("dialogs", cmd_dialogs))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("errors", cmd_errors))
    app.add_handler(MessageHandler(
    (filters.TEXT | filters.CAPTION | filters.FORWARDED) & ~filters.COMMAND,
    handle_message,
))
    return app
