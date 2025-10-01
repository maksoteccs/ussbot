"""
Telegram Task Bot — fresh start

Assumptions preserved from our previous setup:
- Language: Python 3.10+
- Framework: aiogram 2.x (2.25.1 recommended)
- Scheduler: APScheduler 3.x (3.10+)
- Timezone: Europe/Stockholm
- Daily reminders: 10:00 on weekdays, DM to each assignee
- Commands should not clutter group chats: the bot deletes trigger messages when possible
- Assignment via command/menu (no free-form @-mentions in the task text)

Quick start:
1) Create .env next to this file with:
   BOT_TOKEN=123456:ABC...
   TZ=Europe/Stockholm
   SHEETS_LINK_PLAN=https://docs.google.com/spreadsheets/d/1jYQAQIYGqXc8nM1zZFrsjHB4qVwcxeZufoZjtgj4_Ck/edit?usp=sharing

2) pip install -r requirements.txt

   # requirements.txt
   aiogram==2.25.1
   APScheduler==3.10.4
   python-dotenv==1.0.1
   aiosqlite==0.20.0
   pytz==2024.1

3) Run: python ussbot_main.py

Notes:
- Add the bot to your group and promote it with the right to delete messages (to auto-clean /commands)
- Use /menu or the Telegram menu button to navigate features
- To assign a task in a group, reply to the assignee's message and send: /assign <task text>
  (No need to write @username inside the task; the assignee is inferred from the reply.)
- The same menu works in groups and in private chat.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, time
from typing import Optional, List, Tuple

import aiosqlite
import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from aiogram import Bot, Dispatcher, types
from aiogram.types import (
    BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from aiogram.utils import executor
from dotenv import load_dotenv

# -------------------------------------------------
# Config
# -------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TZ = os.getenv("TZ", "Europe/Stockholm")
SHEETS_LINK_PLAN = os.getenv("SHEETS_LINK_PLAN", "https://docs.google.com/")

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN is not set. Put it in .env")

logger = logging.getLogger("ussbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")

# -------------------------------------------------
# Data layer (SQLite)
# -------------------------------------------------
DB_PATH = "tasks.db"

CREATE_TABLE_SQL = """
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

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(CREATE_TABLE_SQL)
        await db.commit()

async def add_task(chat_id: int, assigner_id: int, assignee_id: int, text: str, due_date: Optional[str] = None) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO tasks (chat_id, assigner_id, assignee_id, text, created_at, due_date, is_done) VALUES (?,?,?,?,?,?,0)",
            (chat_id, assigner_id, assignee_id, text.strip(), datetime.utcnow().isoformat(), due_date),
        )
        await db.commit()
        return cur.lastrowid

async def list_tasks_for_assignee(assignee_id: int, only_open: bool = True) -> List[Tuple]:
    q = "SELECT id, chat_id, text, created_at, due_date, is_done FROM tasks WHERE assignee_id=?"
    if only_open:
        q += " AND is_done=0"
    q += " ORDER BY id DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, (assignee_id,))
        return await cur.fetchall()

async def mark_done(task_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("UPDATE tasks SET is_done=1 WHERE id=?", (task_id,))
        await db.commit()
        return cur.rowcount > 0

async def list_open_tasks_in_chat(chat_id: int) -> List[Tuple]:
    q = "SELECT id, assignee_id, text FROM tasks WHERE chat_id=? AND is_done=0 ORDER BY id DESC"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(q, (chat_id,))
        return await cur.fetchall()

# -------------------------------------------------
# Bot setup
# -------------------------------------------------
bot = Bot(token=BOT_TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

# -------------------------------------------------
# Utilities
# -------------------------------------------------
@dataclass
class Ctx:
    tz: pytz.BaseTzInfo

ctx = Ctx(tz=pytz.timezone(TZ))

async def safe_delete(message: types.Message):
    """Try to delete user's command to keep chat clean."""
    try:
        await message.delete()
    except Exception:
        # Not admin or can't delete here — ignore
        pass

# -------------------------------------------------
# Menus
# -------------------------------------------------

def menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("➕ Назначить задачу", callback_data="menu_assign"),
        InlineKeyboardButton("📋 Мои задачи", callback_data="menu_mytasks"),
    )
    return kb

async def send_menu_dm(user_id: int):
    text = (
        "<b>Меню</b>

"
        "• Назначение задач выполняется В ГРУППЕ по реплаю: ответьте на сообщение сотрудника и отправьте
"
        "  <code>/assign текст задачи</code>. Команда в группе будет удалена, бот ничего не пишет в общий чат.

"
        "• В личке можно смотреть свои задачи и помечать их выполненными.
"
    )
    await bot.send_message(user_id, text, reply_markup=menu_kb(), disable_web_page_preview=True)

# -------------------------------------------------
# Commands
# -------------------------------------------------
@dp.message_handler(commands=["start"]) 
async def cmd_start(message: types.Message):
    await init_db()
    await message.answer(
        "Привет! Я помогу назначать задачи в группах и напоминать сотрудникам каждый будний день в 10:00 (Europe/Stockholm).

"
        "Добавь меня в рабочий чат с правом удаления сообщений. В общий чат я ничего писать не буду — всё улетает в ЛС.")
    await send_menu_dm(message.from_user.id)

@dp.message_handler(commands=["menu"]) 
async def cmd_menu(message: types.Message):
    # В группах ничего не пишем — удаляем команду и шлём меню в ЛС инициатору
    if message.chat.type in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
        await send_menu_dm(message.from_user.id)
        await safe_delete(message)
        return
    # В личке — показываем меню тут
    await send_menu_dm(message.chat.id)

@dp.message_handler(commands=["assign"]) 
async def cmd_assign(message: types.Message):
    # Назначение допускается только в группах по реплаю. Бот не пишет в общий чат.
    if message.chat.type not in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
        await message.answer("Назначение задачи доступно в группах по реплаю. Откройте меню в ЛС: /menu")
        return

    if not message.reply_to_message or not message.reply_to_message.from_user:
        # Ничего в общий чат не пишем — просто удалим команду и сообщим инициатору в ЛС
        await safe_delete(message)
        try:
            await bot.send_message(message.from_user.id, "Ответьте реплаем на сообщение сотрудника и отправьте: /assign <текст задачи>.")
        except Exception:
            pass
        return

    assignee = message.reply_to_message.from_user
    assignee_id = assignee.id
    assigner_id = message.from_user.id
    task_text = message.get_args().strip()
    if not task_text:
        await safe_delete(message)
        try:
            await bot.send_message(assigner_id, "Добавьте текст задачи: /assign <текст задачи>")
        except Exception:
            pass
        return

    task_id = await add_task(chat_id=message.chat.id, assigner_id=assigner_id, assignee_id=assignee_id, text=task_text)

    # Никаких сообщений в общий чат. Удаляем команду, шлём ЛС исполнителю и инициатору.
    await safe_delete(message)

    # Исполнителю — новая задача
    try:
        await bot.send_message(assignee_id, f"🆕 Вам назначена задача <b>#{task_id}</b>:
— {types.utils.html.quote_html(task_text)}")
    except Exception:
        # Если исполнитель не открыл бота — сообщим инициатору
        try:
            await bot.send_message(assigner_id, (
                f"Задача #{task_id} создана, но я не смог отправить ЛС исполнителю.
"
                f"Попросите <a href=\"tg://user?id={assignee_id}\">его</a> сначала написать мне в чат."
            ), disable_web_page_preview=True)
        except Exception:
            pass
        return

    # Инициатору — подтверждение в ЛС
    try:
        await bot.send_message(assigner_id, f"✅ Задача #{task_id} назначена пользователю {assignee.full_name} и отправлена ему в ЛС.")
    except Exception:
        pass

@dp.message_handler(commands=["mytasks"]) 
async def cmd_mytasks(message: types.Message):
    tasks = await list_tasks_for_assignee(message.from_user.id, only_open=True)
    if not tasks:
        await message.answer("У вас нет открытых задач ✨")
        return

    lines = ["<b>Ваши открытые задачи:</b>"]
    for (tid, chat_id, text, created_at, due, is_done) in tasks:
        lines.append(f"#{tid}: {types.utils.html.quote_html(text)}")
    lines.append("\nЧтобы закрыть: /done <id>")
    await message.answer("\n".join(lines))

@dp.message_handler(commands=["done"]) 
async def cmd_done(message: types.Message):
    args = message.get_args().strip()
    if not args.isdigit():
        await message.answer("Укажите ID задачи: /done <id>")
        return
    ok = await mark_done(int(args))
    if ok:
        await message.answer("Готово! Задача закрыта ✅")
    else:
        await message.answer("Не нашёл такую задачу или она уже закрыта.")

# -------------------------------------------------
# Callbacks (menu buttons)
# -------------------------------------------------
@dp.callback_query_handler(lambda c: c.data == "menu_assign")
async def cb_menu_assign(call: types.CallbackQuery):
    await call.answer()
    if call.message.chat.type in (types.ChatType.GROUP, types.ChatType.SUPERGROUP):
        txt = (
            "В группе: ответьте на сообщение сотрудника и отправьте <code>/assign текст задачи</code>.\n"
            "Команда будет удалена, чтобы не засорять чат."
        )
    else:
        txt = (
            "Назначение задач выполняется в рабочих группах по реплаю.\n"
            "Здесь вы можете смотреть свои задачи: /mytasks"
        )
    await call.message.edit_text(txt, reply_markup=menu_kb(), disable_web_page_preview=True)

@dp.callback_query_handler(lambda c: c.data == "menu_mytasks")
async def cb_menu_mytasks(call: types.CallbackQuery):
    await call.answer()
    tasks = await list_tasks_for_assignee(call.from_user.id, only_open=True)
    if not tasks:
        await call.message.edit_text("У вас нет открытых задач ✨", reply_markup=menu_kb())
        return
    lines = ["<b>Ваши открытые задачи:</b>"]
    for (tid, chat_id, text, created_at, due, is_done) in tasks:
        lines.append(f"#{tid}: {types.utils.html.quote_html(text)}")
    lines.append("\nЧтобы закрыть: /done <id>")
    await call.message.edit_text("\n".join(lines), reply_markup=menu_kb())

# -------------------------------------------------
# Scheduler: daily reminders @ 10:00 Europe/Stockholm (Mon-Fri)
# -------------------------------------------------
async def send_daily_reminders():
    # We gather all assignees who have open tasks and send a grouped DM
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT DISTINCT assignee_id FROM tasks WHERE is_done=0")
        assignees = [row[0] for row in await cur.fetchall()]
    if not assignees:
        return

    for uid in assignees:
        tasks = await list_tasks_for_assignee(uid, only_open=True)
        if not tasks:
            continue
        lines = ["🔔 <b>Ежедневное напоминание</b>", "Ваши открытые задачи:"]
        for (tid, chat_id, text, created_at, due, is_done) in tasks:
            lines.append(f"#{tid}: {types.utils.html.quote_html(text)}")
        lines.append("\nЗакрыть задачу: /done <id>")
        try:
            await bot.send_message(uid, "\n".join(lines))
        except Exception:
            pass

scheduler: Optional[AsyncIOScheduler] = None

async def on_startup(dispatcher: Dispatcher):
    await init_db()

    # Set menu commands for private and group chats
    commands = [
        BotCommand("menu", "Открыть меню"),
        BotCommand("assign", "Назначить задачу (в группе по реплаю)"),
        BotCommand("mytasks", "Мои задачи"),
        BotCommand("done", "Закрыть задачу по ID"),
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())

    global scheduler
    if scheduler is None:
        scheduler = AsyncIOScheduler(timezone=TZ)
        # 10:00 Mon-Fri
        trigger = CronTrigger(day_of_week="mon-fri", hour=10, minute=0, timezone=TZ)
        scheduler.add_job(lambda: asyncio.create_task(send_daily_reminders()), trigger)
        scheduler.start()
        logger.info("Scheduler started for 10:00 %s on weekdays", TZ)

async def on_shutdown(dispatcher: Dispatcher):
    global scheduler
    if scheduler:
        scheduler.shutdown(wait=False)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup, on_shutdown=on_shutdown)
