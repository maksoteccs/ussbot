import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import Message, MessageEntity
from aiogram.enums import ChatType, MessageEntityType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

BOT_TOKEN = "8299026874:AAH0uKNWiiqGqi_YQl2SWDhm5qr6Z0Vrxvw"
DEFAULT_TZ = "Europe/Moscow"
DB_PATH = "bot.db"

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode="HTML"),
)
dp = Dispatcher()

# === Инициализация БД ===
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

# === DB helpers ===
def db_execute(query: str, params: tuple = ()) -> None:
    with closing(sqlite3.connect(DB_PATH)) as conn:
        c = conn.cursor()
        c.execute(query, params)
        conn.commit()

def db_fetchone(query: str, params: tuple = ()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchone()

def db_fetchall(query: str, params: tuple = ()):
    with closing(sqlite3.connect(DB_PATH)) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(query, params)
        return c.fetchall()

def upsert_user(tg_id: int, username: str | None):
    row = db_fetchone("SELECT tg_id FROM users WHERE tg_id=?", (tg_id,))
    if row:
        db_execute("UPDATE users SET username=? WHERE tg_id=?", (username, tg_id))
    else:
        db_execute(
            "INSERT INTO users (tg_id, username, tz, weekdays_only) VALUES (?, ?, ?, 1)",
            (tg_id, username, DEFAULT_TZ),
        )

def attach_username_tasks_to_user(tg_id: int, username: str | None):
    if not username:
        return
    db_execute(
        "UPDATE tasks SET assignee_tg_id=? "
        "WHERE assignee_tg_id IS NULL AND assignee_username=?",
        (tg_id, username),
    )

def add_task(assignee_tg_id: int | None, assignee_username: str | None, chat_id: int, text: str):
    db_execute(
        "INSERT INTO tasks (assignee_tg_id, assignee_username, chat_id, text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            assignee_tg_id,
            assignee_username,
            chat_id,
            text.strip(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )

def list_tasks_for_user(tg_id: int):
    return db_fetchall(
        "SELECT id, text FROM tasks WHERE is_done=0 AND assignee_tg_id=? ORDER BY id ASC",
        (tg_id,),
    )

def mark_done(task_id: int, tg_id: int) -> bool:
    row = db_fetchone("SELECT assignee_tg_id FROM tasks WHERE id=?", (task_id,))
    if not row:
        return False
    if row["assignee_tg_id"] and row["assignee_tg_id"] != tg_id:
        return False
    db_execute("UPDATE tasks SET is_done=1 WHERE id=?", (task_id,))
    return True

# === Команды ===
@dp.message(Command("start"))
async def cmd_start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username)
    attach_username_tasks_to_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Я собираю задачи по @упоминанию и пришлю их тебе утром в 10:00.\n"
        "Команды: /task, /list, /done &lt;id&gt;"
    )

@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject):
    # автопривязка username -> tg_id при любом обращении
    upsert_user(message.from_user.id, message.from_user.username)
    attach_username_tasks_to_user(message.from_user.id, message.from_user.username)

    text = (command.args or "").strip()
    if not text:
        await message.answer("Напиши задачу: /task Текст задачи")
        return
    add_task(message.from_user.id, message.from_user.username, message.chat.id, text)
    await message.answer("Задача добавлена ✅")

@dp.message(Command("list"))
async def cmd_list(message: Message):
    upsert_user(message.from_user.id, message.from_user.username)
    attach_username_tasks_to_user(message.from_user.id, message.from_user.username)

    rows = list_tasks_for_user(message.from_user.id)
    if not rows:
        await message.answer("У тебя нет открытых задач ✨")
        return
    lines = [f"{r['id']}. {r['text']}" for r in rows]
    await message.answer("Твои задачи:\n" + "\n".join(lines))

@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    upsert_user(message.from_user.id, message.from_user.username)
    attach_username_tasks_to_user(message.from_user.id, message.from_user.username)

    if not command.args or not command.args.isdigit():
        await message.answer("Укажи ID задачи: /done &lt;id&gt;")
        return
    ok = mark_done(int(command.args), message.from_user.id)
    await message.answer("Готово ✅" if ok else "Не получилось закрыть задачу")

# === Группы: добавление задач по упоминанию ===
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def on_group_message(message: Message):
    if not message.text:
        return

    entities: list[MessageEntity] = message.entities or []
    mentions = [
        e for e in entities
        if e.type in {MessageEntityType.MENTION, MessageEntityType.TEXT_MENTION}
    ]
    if not mentions:
        return

    text = message.text
    for e in mentions:
        if e.offset == 0:
            text = text[e.length:].strip()
    if not text:
        return

    for e in mentions:
        assignee_tg_id = None
        assignee_username = None
        if e.type == MessageEntityType.TEXT_MENTION and e.user:
            assignee_tg_id = e.user.id
            assignee_username = e.user.username
        else:
            assignee_username = message.text[e.offset + 1 : e.offset + e.length]
        add_task(assignee_tg_id, assignee_username, message.chat.id, text)

    await message.reply("Задача(и) добавлена(ы) ✅")

# === Ежедневные напоминания ===
async def send_daily_summaries():
    rows = db_fetchall("""
        SELECT DISTINCT u.tg_id
        FROM tasks t
        JOIN users u ON u.tg_id = t.assignee_tg_id
        WHERE t.is_done=0 AND t.assignee_tg_id IS NOT NULL
    """)
    for r in rows:
        tg_id = r["tg_id"]
        tasks = list_tasks_for_user(tg_id)
        if not tasks:
            continue
        lines = [f"{row['id']}. {row['text']}" for row in tasks]
        text = "Доброе утро! Твои задачи:\n" + "\n".join(lines)
        try:
            await bot.send_message(chat_id=tg_id, text=text)
        except Exception:
            pass

def schedule_jobs(scheduler: AsyncIOScheduler):
    scheduler.add_job(
        send_daily_summaries,
        CronTrigger(hour=10, minute=0, timezone=ZoneInfo(DEFAULT_TZ)),
        id="daily_summaries",
        replace_existing=True,
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
