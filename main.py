"""
Telegram Task Bot — aiogram 3.7+ + APScheduler + sqlite3 (async via asyncio.to_thread)

Поведение:
- Бот НИЧЕГО не пишет в общий чат.
- Назначение задачи — только в группе по реплаю: /assign <текст>
  → команда удаляется в группе; ЛС уходят:
     • исполнителю — новая задача,
     • инициатору — подтверждение (или подсказка, если у исполнителя закрыты ЛС).
- /menu /mytasks /done /start, вызванные в группе, удаляются и ответ уходит в ЛС инициатору.
- Ежедневные напоминания по будням в 10:00 (Europe/Stockholm).

ENV (.env рядом с main.py):
BOT_TOKEN=8299026874:AAH0uKNWiiqGqi_YQl2SWDhm5qr6Z0Vrxvw.
TZ=Europe/Stockholm

Зависимости:
aiogram>=3.7.0
APScheduler==3.10.4
python-dotenv==1.0.1
pytz==2024.1
"""

import asyncio
import html
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List, Tuple

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------- Config ----------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TZ = os.getenv("TZ", "Europe/Stockholm")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Put it in .env")

logger = logging.getLogger("ussbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# ---------- Helpers ----------
def quote_html(text: str) -> str:
    return html.escape(text, quote=True)

# ---------- Data layer (sqlite3 sync -> run in thread) ----------
DB_PATH = "tasks.db"

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def _init_db_sync():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                assigner_id INTEGER NOT NULL,
                assignee_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL,
                due_date TEXT,
                is_done INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()

async def init_db():
    await asyncio.to_thread(_init_db_sync)

def _add_task_sync(chat_id: int, assigner_id: int, assignee_id: int, text: str, due_date: Optional[str]) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO tasks (chat_id, assigner_id, assignee_id, text, created_at, due_date, is_done) "
            "VALUES (?,?,?,?,?,?,0)",
            (chat_id, assigner_id, assignee_id, text.strip(), datetime.utcnow().isoformat(), due_date),
        )
        conn.commit()
        return cur.lastrowid

async def add_task(chat_id: int, assigner_id: int, assignee_id: int, text: str, due_date: Optional[str] = None) -> int:
    return await asyncio.to_thread(_add_task_sync, chat_id, assigner_id, assignee_id, text, due_date)

def _list_tasks_for_assignee_sync(assignee_id: int, only_open: bool) -> List[Tuple]:
    q = "SELECT id, chat_id, text, created_at, due_date, is_done FROM tasks WHERE assignee_id=?"
    params = [assignee_id]
    if only_open:
        q += " AND is_done=0"
    q += " ORDER BY id DESC"
    with _connect() as conn:
        cur = conn.execute(q, params)
        return cur.fetchall()

async def list_tasks_for_assignee(assignee_id: int, only_open: bool = True) -> List[Tuple]:
    return await asyncio.to_thread(_list_tasks_for_assignee_sync, assignee_id, only_open)

def _mark_done_sync(task_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("UPDATE tasks SET is_done=1 WHERE id=?", (task_id,))
        conn.commit()
        return cur.rowcount > 0

async def mark_done(task_id: int) -> bool:
    return await asyncio.to_thread(_mark_done_sync, task_id)

def _distinct_open_assignees_sync() -> List[int]:
    with _connect() as conn:
        cur = conn.execute("SELECT DISTINCT assignee_id FROM tasks WHERE is_done=0")
        return [row[0] for row in cur.fetchall()]

async def distinct_open_assignees() -> List[int]:
    return await asyncio.to_thread(_distinct_open_assignees_sync)

# ---------- Bot / Dispatcher / Router (aiogram 3.7+) ----------
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ---------- Utilities ----------
@dataclass
class Ctx:
    tz: pytz.BaseTzInfo

ctx = Ctx(tz=pytz.timezone(TZ))

async def safe_delete(message: Message):
    try:
        await message.delete()
    except Exception:
        pass  # not admin / can't delete

def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Назначить задачу", callback_data="menu_assign")
    kb.button(text="📋 Мои задачи", callback_data="menu_mytasks")
    kb.adjust(2)
    return kb.as_markup()

async def send_menu_dm(user_id: int):
    text = (
        "<b>Меню</b>\n\n"
        "• Назначение задач выполняется <b>в группе</b> по реплаю: ответьте на сообщение сотрудника и отправьте\n"
        "  <code>/assign текст задачи</code>. Команда в группе будет удалена; в общий чат бот ничего не пишет.\n\n"
        "• В личке можно смотреть свои задачи и помечать их выполненными.\n"
    )
    await bot.send_message(user_id, text, reply_markup=menu_kb(), disable_web_page_preview=True)

# ---------- Handlers ----------
@router.message(Command("start"))
async def cmd_start(message: Message):
    await init_db()
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_delete(message)
        try:
            await bot.send_message(
                message.from_user.id,
                "Привет! Я помогаю назначать задачи в группах и напоминать сотрудникам по будням в 10:00 (Europe/Stockholm)."
                "\nВ общий чат я ничего писать не буду — всё уходит в ЛС."
            )
            await send_menu_dm(message.from_user.id)
        except Exception:
            pass
        return

    await message.answer(
        "Привет! Я помогаю назначать задачи в группах и напоминать сотрудникам по будням в 10:00 (Europe/Stockholm)."
        "\nВ общий чат я ничего писать не буду — всё уходит в ЛС."
    )
    await send_menu_dm(message.from_user.id)

@router.message(Command("menu"))
async def cmd_menu(message: Message):
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_delete(message)
        try:
            await send_menu_dm(message.from_user.id)
        except Exception:
            pass
        return
    await send_menu_dm(message.chat.id)

@router.message(Command("assign"))
async def cmd_assign(message: Message, command: CommandObject):
    # Только в группах по реплаю. В общий чат не пишем.
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await message.answer("Назначение задачи доступно в группах по реплаю. Откройте меню в ЛС: /menu")
        return

    # Удаляем команду в группе
    await safe_delete(message)

    if not message.reply_to_message or not message.reply_to_message.from_user:
        try:
            await bot.send_message(
                message.from_user.id,
                "Пожалуйста, ответьте <b>реплаем</b> на сообщение сотрудника и отправьте: /assign <текст задачи>."
            )
        except Exception:
            pass
        return

    assignee = message.reply_to_message.from_user
    assignee_id = assignee.id
    assigner_id = message.from_user.id
    task_text = (command.args or "").strip()

    if not task_text:
        try:
            await bot.send_message(assigner_id, "Добавьте текст задачи: /assign <текст задачи>")
        except Exception:
            pass
        return

    task_id = await add_task(
        chat_id=message.chat.id,
        assigner_id=assigner_id,
        assignee_id=assignee_id,
        text=task_text
    )

    # Исполнителю — новая задача в ЛС
    try:
        await bot.send_message(
            assignee_id,
            f"🆕 Вам назначена задача <b>#{task_id}</b>:\n— {quote_html(task_text)}"
        )
    except Exception:
        # Если исполнитель не открыл бота — сообщим инициатору
        try:
            await bot.send_message(
                assigner_id,
                (
                    f"Задача #{task_id} создана, но не удалось отправить ЛС исполнителю.\n"
                    f"Попросите <a href=\"tg://user?id={assignee_id}\">{quote_html(assignee.full_name)}</a> сначала написать мне."
                ),
                disable_web_page_preview=True
            )
        except Exception:
            pass
        return

    # Инициатору — подтверждение в ЛС
    try:
        await bot.send_message(
            assigner_id,
            f"✅ Задача #{task_id} назначена пользователю {quote_html(assignee.full_name)} и отправлена ему в ЛС."
        )
    except Exception:
        pass

@router.message(Command("mytasks"))
async def cmd_mytasks(message: Message):
    # В группе — удаляем и шлём список задач в ЛС
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_delete(message)
        uid = message.from_user.id
    else:
        uid = message.from_user.id

    tasks = await list_tasks_for_assignee(uid, only_open=True)
    if not tasks:
        try:
            await bot.send_message(uid, "У вас нет открытых задач ✨")
        except Exception:
            pass
        return

    lines = ["<b>Ваши открытые задачи:</b>"]
    for (tid, chat_id, text, created_at, due, is_done) in tasks:
        lines.append(f"#{tid}: {quote_html(text)}")
    lines.append("\nЧтобы закрыть: /done <id>")
    try:
        await bot.send_message(uid, "\n".join(lines))
    except Exception:
        pass

@router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    # В группе — удаляем команду и работаем через ЛС
    if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await safe_delete(message)
        uid = message.from_user.id
        args = (command.args or "").strip()
        try:
            if not args:
                await bot.send_message(uid, "Укажите ID задачи: /done <id>")
                return
        except Exception:
            return
    else:
        uid = message.from_user.id
        args = (command.args or "").strip()

    if not args.isdigit():
        try:
            await bot.send_message(uid, "Укажите ID задачи: /done <id>")
        except Exception:
            pass
        return

    ok = await mark_done(int(args))
    try:
        if ok:
            await bot.send_message(uid, "Готово! Задача закрыта ✅")
        else:
            await bot.send_message(uid, "Не нашёл такую задачу или она уже закрыта.")
    except Exception:
        pass

# ---------- Callback handlers ----------
@router.callback_query(F.data == "menu_assign")
async def cb_menu_assign(call: CallbackQuery):
    await call.answer()
    txt = (
        "В группе: ответьте реплаем на сообщение сотрудника и отправьте "
        "<code>/assign текст задачи</code>.\n"
        "Команда в группе будет удалена. Бот в общий чат не пишет."
    )
    try:
        await call.message.edit_text(txt, reply_markup=menu_kb(), disable_web_page_preview=True)
    except Exception:
        pass

@router.callback_query(F.data == "menu_mytasks")
async def cb_menu_mytasks(call: CallbackQuery):
    await call.answer()
    tasks = await list_tasks_for_assignee(call.from_user.id, only_open=True)
    if not tasks:
        try:
            await call.message.edit_text("У вас нет открытых задач ✨", reply_markup=menu_kb())
        except Exception:
            pass
        return
    lines = ["<b>Ваши открытые задачи:</b>"]
    for (tid, chat_id, text, created_at, due, is_done) in tasks:
        lines.append(f"#{tid}: {quote_html(text)}")
    lines.append("\nЧтобы закрыть: /done <id>")
    try:
        await call.message.edit_text("\n".join(lines), reply_markup=menu_kb())
    except Exception:
        pass

# ---------- Scheduler: daily reminders @ 10:00 Europe/Stockholm (Mon–Fri) ----------
scheduler: Optional[AsyncIOScheduler] = None

async def send_daily_reminders():
    assignees = await distinct_open_assignees()
    if not assignees:
        return

    for uid in assignees:
        tasks = await list_tasks_for_assignee(uid, only_open=True)
        if not tasks:
            continue
        lines = ["🔔 <b>Ежедневное напоминание</b>", "Ваши открытые задачи:"]
        for (tid, chat_id, text, created_at, due, is_done) in tasks:
            lines.append(f"#{tid}: {quote_html(text)}")
        lines.append("\nЗакрыть задачу: /done <id>")
        try:
            await bot.send_message(uid, "\n".join(lines))
        except Exception:
            pass

async def setup_commands():
    commands = [
        BotCommand(command="menu", description="Открыть меню"),
        BotCommand(command="assign", description="Назначить задачу (в группе по реплаю)"),
        BotCommand(command="mytasks", description="Мои задачи"),
        BotCommand(command="done", description="Закрыть задачу по ID"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())

async def on_startup():
    await init_db()
    await setup_commands()

    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler(timezone=ctx.tz)
        trigger = CronTrigger(day_of_week="mon-fri", hour=10, minute=0, timezone=ctx.tz)
        scheduler.add_job(lambda: asyncio.create_task(send_daily_reminders()), trigger)
        scheduler.start()
        logger.info("Scheduler started for 10:00 %s on weekdays", TZ)

async def on_shutdown():
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)

dp.startup.register(on_startup)
dp.shutdown.register(on_shutdown)

# ---------- Entrypoint ----------
async def main():
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
