# === Импорты и настройки ===
import asyncio
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, ChatType, MessageEntityType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

BOT_TOKEN = "8299026874:AAH0uKNWiiqGqi_YQl2SWDhm5qr6Z0Vrxvw"
DEFAULT_TZ = "Europe/Moscow"
DB_PATH = "bot.db"

bot = Bot(token=BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# === База данных ===
with closing(sqlite3.connect(DB_PATH)) as conn:
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_id INTEGER UNIQUE,
            username TEXT,
            tz TEXT,
            weekdays_only INTEGER DEFAULT 1
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            assignee_tg_id INTEGER,
            assignee_username TEXT,
            chat_id INTEGER,
            text TEXT,
            is_done INTEGER DEFAULT 0,
            created_at TEXT
        )
    """)
    conn.commit()

# === DB-хелперы ===
def db_exec(query, params=()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()
        return c

def upsert_user(tg_id, username):
    db_exec(
        """INSERT INTO users (tg_id, username, tz) VALUES (?, ?, ?)
           ON CONFLICT(tg_id) DO UPDATE SET username=excluded.username""",
        (tg_id, username, DEFAULT_TZ)
    )

def add_task(assignee_tg_id, assignee_username, chat_id, text):
    db_exec(
        "INSERT INTO tasks (assignee_tg_id, assignee_username, chat_id, text, created_at) VALUES (?, ?, ?, ?, ?)",
        (assignee_tg_id, assignee_username, chat_id, text.strip(), datetime.now(timezone.utc).isoformat())
    )

def list_tasks_for_user(tg_id):
    return db_exec(
        "SELECT id, text FROM tasks WHERE is_done=0 AND assignee_tg_id=? ORDER BY id ASC",
        (tg_id,)
    ).fetchall()

def mark_done(task_id, tg_id):
    row = db_exec("SELECT assignee_tg_id FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not row: return False
    if row["assignee_tg_id"] and row["assignee_tg_id"] != tg_id: return False
    db_exec("UPDATE tasks SET is_done=1 WHERE id=?", (task_id,))
    return True

# === Команды ===
@dp.message(Command("start"))
async def cmd_start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username)
    await message.answer("Привет! Я собираю задачи по @упоминанию и пришлю их тебе утром в 10:00.")

@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        await message.answer("Напиши задачу: /task Текст задачи")
        return
    add_task(message.from_user.id, message.from_user.username, message.chat.id, text)
    await message.answer("Задача добавлена ✅")

@dp.message(Command("list"))
async def cmd_list(message: Message):
    rows = list_tasks_for_user(message.from_user.id)
    if not rows:
        await message.answer("У тебя нет открытых задач ✨")
        return
    lines = [f"{r['id']}. {r['text']}" for r in rows]
    await message.answer("Твои задачи:\n" + "\n".join(lines))

@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    if not command.args or not command.args.isdigit():
        await message.answer("Укажи ID задачи: /done 1")
        return
    ok = mark_done(int(command.args), message.from_user.id)
    await message.answer("Готово ✅" if ok else "Не получилось закрыть задачу")

# === Обработчик сообщений в группах (по упоминанию) ===
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_group_message(message: Message):
    if not message.text: return
    mentions = [e for e in (message.entities or []) if e.type in {MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION}]
    if not mentions: return

    text = message.text
    for e in mentions:
        if e.offset == 0:
            text = text[e.length:].strip()
    if not text: return

    for e in mentions:
        assignee_tg_id = None
        assignee_username = None
        if e.type == MessageEntityType.TEXT_MENTION and e.user:
            assignee_tg_id = e.user.id
            assignee_username = e.user.username
        else:
            assignee_username = message.text[e.offset+1:e.offset+e.length]
        add_task(assignee_tg_id, assignee_username, message.chat.id, text)

    await message.reply("Задача(и) добавлена(ы) ✅")

# === Ежедневные напоминания ===
async def send_daily_summaries():
    rows = db_exec("""
        SELECT DISTINCT u.tg_id FROM tasks t
        JOIN users u ON u.tg_id = t.assignee_tg_id
        WHERE t.is_done=0
    """).fetchall()
    for r in rows:
        tg_id = r["tg_id"]
        tasks = list_tasks_for_user(tg_id)
        if not tasks: continue
        lines = [f"{row['id']}. {row['text']}" for row in tasks]
        text = "Доброе утро! Твои задачи:\n" + "\n".join(lines)
        try:
            await bot.send_message(chat_id=tg_id, text=text)
        except: pass

def schedule_jobs(scheduler: AsyncIOScheduler):
    scheduler.add_job(
        send_daily_summaries,
        CronTrigger(hour=10, minute=0, timezone=ZoneInfo(DEFAULT_TZ)),
        id="daily_summaries",
        replace_existing=True
    )

# === Запуск ===
async def main():
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(DEFAULT_TZ))
    schedule_jobs(scheduler)
    scheduler.start()
    print("Бот работает...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
