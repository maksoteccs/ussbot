import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message, MessageEntity,
    InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand
)
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from aiogram.enums import ChatType, MessageEntityType
from aiogram.types import CallbackQuery
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

BOT_TOKEN = "8299026874:AAH0uKNWiiqGqi_YQl2SWDhm5qr6Z0Vrxvw"
DEFAULT_TZ = "Europe/Moscow"
DB_PATH = "bot.db"

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ------------------ ИНИЦИАЛИЗАЦИЯ БД ------------------
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

# ------------------ DB HELPERS ------------------
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

def set_user_tz(tg_id: int, tz: str):
    db_execute("UPDATE users SET tz=? WHERE tg_id=?", (tz, tg_id))

def get_user_tz(tg_id: int) -> str:
    row = db_fetchone("SELECT tz FROM users WHERE tg_id=?", (tg_id,))
    return row["tz"] if row and row["tz"] else DEFAULT_TZ

def set_weekdays_only(tg_id: int, value: bool):
    db_execute("UPDATE users SET weekdays_only=? WHERE tg_id=?", (1 if value else 0, tg_id))

def get_weekdays_only(tg_id: int) -> bool:
    row = db_fetchone("SELECT weekdays_only FROM users WHERE tg_id=?", (tg_id,))
    return bool(row["weekdays_only"]) if row else True

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

# ------------------ КЛАВИАТУРА ДЛЯ /list ------------------
def tasks_keyboard(rows):
    buttons = [
        [InlineKeyboardButton(text=f"✅ Закрыть {r['id']}", callback_data=f"done:{r['id']}")]
        for r in rows
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ------------------ МЕНЮ КОМАНД ------------------
PRIVATE_COMMANDS = [
    BotCommand(command="start",   description="Запуск и краткая справка"),
    BotCommand(command="task",    description="Назначить задачу"),
    BotCommand(command="list",    description="Показать мои задачи"),
    BotCommand(command="done",    description="Закрыть задачу по ID"),
    BotCommand(command="settz",   description="Установить часовой пояс"),
    BotCommand(command="weekdays",description="Напоминания по будням on|off"),
    BotCommand(command="help",    description="Справка по командам"),
]
GROUP_COMMANDS = [
    BotCommand(command="task",    description="Назначить задачу"),
    BotCommand(command="list",    description="Показать мои задачи"),
    BotCommand(command="done",    description="Закрыть задачу по ID"),
    BotCommand(command="help",    description="Справка по командам"),
]

async def setup_bot_commands(bot: Bot):
    await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

# ------------------ КОМАНДЫ ------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username)
    attach_username_tasks_to_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Я собираю задачи по @упоминанию и пришлю их тебе утром в 10:00.\n"
        "Команды: /task, /list, /done &lt;id&gt;, /settz, /weekdays, /help"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📌 Команды:\n\n"
        "▫️ /task <текст> — назначить задачу\n"
        "   • В личке — себе\n"
        "   • В группе — себе или другому (@username в сообщении)\n\n"
        "▫️ /list — показать мои открытые задачи (с кнопками закрытия)\n"
        "▫️ /done &lt;id&gt; — закрыть задачу по ID\n"
        "▫️ /settz <IANA_TZ> — установить часовой пояс (например Europe/Moscow)\n"
        "▫️ /weekdays on|off — включить/выключить напоминания только по будням\n"
        "▫️ /help — эта справка\n"
    )
    await message.answer(text)

@dp.message(Command("settz"))
async def cmd_settz(message: Message, command: CommandObject):
    upsert_user(message.from_user.id, message.from_user.username)
    tz = (command.args or "").strip()
    try:
        if not tz:
            raise ValueError("empty")
        ZoneInfo(tz)  # валидация
    except Exception:
        await message.answer("Укажи корректный IANA TZ, например: Europe/Moscow, Asia/Almaty, America/Los_Angeles")
        return
    set_user_tz(message.from_user.id, tz)
    await message.answer(f"Часовой пояс обновлён на {html.quote(tz)}")

@dp.message(Command("weekdays"))
async def cmd_weekdays(message: Message, command: CommandObject):
    upsert_user(message.from_user.id, message.from_user.username)
    arg = (command.args or "").strip().lower()
    if arg not in {"on", "off"}:
        current = "on" if get_weekdays_only(message.from_user.id) else "off"
        await message.answer(f"Сейчас: {current}. Используй: /weekdays on|off")
        return
    set_weekdays_only(message.from_user.id, arg == "on")
    await message.answer("Напоминания по будням: включены ✅" if arg == "on" else "Будни-ограничение выключено — слать и в выходные ✅")

@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject):
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

    lines = [f"{r['id']}. {html.quote(r['text'])}" for r in rows]
    text = "Твои задачи:\n" + "\n".join(lines)
    await message.answer(text, reply_markup=tasks_keyboard(rows))

@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    upsert_user(message.from_user.id, message.from_user.username)
    attach_username_tasks_to_user(message.from_user.id, message.from_user.username)

    if not command.args or not command.args.isdigit():
        await message.answer("Укажи ID задачи: /done &lt;id&gt;")
        return
    ok = mark_done(int(command.args), message.from_user.id)
    await message.answer("Готово ✅" if ok else "Не получилось закрыть задачу")

# ------------------ INLINE-КНОПКИ «ЗАКРЫТЬ» ------------------
@dp.callback_query(F.data.startswith("done:"))
async def on_done_click(callback: CallbackQuery):
    task_id_str = callback.data.split(":", 1)[1]
    if not task_id_str.isdigit():
        await callback.answer("Некорректный ID", show_alert=False)
        return

    ok = mark_done(int(task_id_str), callback.from_user.id)
    if not ok:
        await callback.answer("Не получилось (не найдена или не твоя)", show_alert=False)
        return

    rows = list_tasks_for_user(callback.from_user.id)
    if rows:
        lines = [f"{r['id']}. {html.quote(r['text'])}" for r in rows]
        text = "Твои задачи:\n" + "\n".join(lines)
        await callback.message.edit_text(text, reply_markup=tasks_keyboard(rows))
    else:
        await callback.message.edit_text("Все задачи закрыты 🎉")
    await callback.answer("Закрыто ✅", show_alert=False)

# ------------------ ГРУППЫ: НАЗНАЧЕНИЕ ПО УПОМИНАНИЮ ------------------
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

# ------------------ НАПОМИНАНИЯ ------------------
async def send_daily_summaries():
    rows = db_fetchall("""
        SELECT DISTINCT u.tg_id, COALESCE(u.tz, ?) AS tz, COALESCE(u.weekdays_only, 1) AS weekdays_only
        FROM tasks t
        JOIN users u ON u.tg_id = t.assignee_tg_id
        WHERE t.is_done=0 AND t.assignee_tg_id IS NOT NULL
    """, (DEFAULT_TZ,))
    for r in rows:
        tg_id = r["tg_id"]
        tz = r["tz"]
        weekdays_only = bool(r["weekdays_only"])
        if weekdays_only:
            wd = datetime.now(ZoneInfo(tz)).weekday()  # 0=Mon .. 6=Sun
            if wd >= 5:  # Sat/Sun
                continue

        tasks = list_tasks_for_user(tg_id)
        if not tasks:
            continue

        lines = [f"{row['id']}. {html.quote(row['text'])}" for row in tasks]
        now_local = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
        text = (
            f"Доброе утро! ({now_local} {tz})\n"
            "Твои актуальные задачи:\n" + "\n".join(lines) +
            "\n\nЗакрывай выполненные кнопками в /list или командой /done &lt;id&gt;"
        )
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

# ------------------ ЗАПУСК ------------------
async def main():
    scheduler = AsyncIOScheduler(timezone=ZoneInfo(DEFAULT_TZ))
    schedule_jobs(scheduler)
    scheduler.start()

    await setup_bot_commands(bot)

    print("Бот работает...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
