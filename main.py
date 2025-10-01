import asyncio
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, html
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    Message,
    InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, KeyboardButtonRequestUser,
    CallbackQuery, BotCommand
)
from aiogram.types import BotCommandScopeAllPrivateChats, BotCommandScopeAllGroupChats
from aiogram.enums import ChatType
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

BOT_TOKEN = "ВСТАВЬ_СВОЙ_ТОКЕН"
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

def add_task(assignee_tg_id: int | None, assignee_username: str | None, chat_id: int, text: str):
    db_execute(
        "INSERT INTO tasks (assignee_tg_id, assignee_username, chat_id, text, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (assignee_tg_id, assignee_username, chat_id, text.strip(), datetime.now(timezone.utc).isoformat()),
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

# ------------------ УТИЛИТЫ: удаление команды и ответы в ЛС ------------------
async def try_delete(message: Message):
    """Удаляем команду в группах (бот должен быть админом с правом Delete messages)."""
    try:
        if message.chat.type in {ChatType.GROUP, ChatType.SUPERGROUP}:
            await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except Exception:
        pass

async def dm(user_id: int, text: str, **kwargs) -> bool:
    """Отправка в личку; вернёт False, если у пользователя не открыт ЛС с ботом."""
    try:
        await bot.send_message(chat_id=user_id, text=text, **kwargs)
        return True
    except Exception:
        return False

async def reply_privately_or_hint(message: Message, text: str, **kwargs):
    """Пробуем ответить в личку; если нельзя — даём краткую подсказку в группе и удаляем её через 5 сек."""
    sent = await dm(message.from_user.id, text, **kwargs)
    if sent:
        return
    hint = await message.answer("Напиши мне в личку: открой профиль бота и нажми Start.")
    try:
        await asyncio.sleep(5)
        await hint.delete()
    except Exception:
        pass

# ------------------ ИНЛАЙН-КНОПКИ ДЛЯ /list ------------------
def tasks_keyboard(rows):
    buttons = [[InlineKeyboardButton(text=f"✅ Закрыть {r['id']}", callback_data=f"done:{r['id']}")] for r in rows]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ------------------ MENU BUTTON (нижняя панель) ------------------
PENDING_ASSIGN: dict[int, int] = {}  # кто -> кому назначаем (выбранный user_id)

def build_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Назначить пользователю", request_user=KeyboardButtonRequestUser(request_id=1))],
            [KeyboardButton(text="➕ Назначить себе"), KeyboardButton(text="📋 Мои задачи")],
            [KeyboardButton(text="✅ Закрыть задачу"), KeyboardButton(text="🌍 Часовой пояс")],
            [KeyboardButton(text="📅 Будни on/off"), KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False
    )

# ------------------ BOT COMMANDS (кнопка «Меню» у поля ввода) ------------------
PRIVATE_COMMANDS = [
    BotCommand(command="task",    description="Назначить себе"),
    BotCommand(command="list",    description="Показать мои задачи"),
    BotCommand(command="done",    description="Закрыть задачу по ID"),
    BotCommand(command="settz",   description="Установить часовой пояс"),
    BotCommand(command="weekdays",description="Будни on/off"),
    BotCommand(command="help",    description="Справка"),
]
GROUP_COMMANDS = [
    BotCommand(command="task",    description="Назначить себе"),
    BotCommand(command="list",    description="Показать мои задачи"),
    BotCommand(command="done",    description="Закрыть задачу по ID"),
    BotCommand(command="help",    description="Справка"),
]

async def setup_bot_commands(bot: Bot):
    await bot.set_my_commands(PRIVATE_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_COMMANDS, scope=BotCommandScopeAllGroupChats())

# ------------------ КОМАНДЫ ------------------
@dp.message(Command("start"))
async def cmd_start(message: Message):
    upsert_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "Привет! Кнопка «Меню» рядом с полем ввода показывает список команд.\n"
        "Ниже включил удобную панель для назначения задач.",
        reply_markup=build_menu_kb()
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await try_delete(message)
    text = (
        "📌 Как пользоваться:\n\n"
        "• 👤 Назначить пользователю — выбери человека и отправь текст задачи одним сообщением\n"
        "• /task <текст> — назначить себе\n"
        "• /list — список открытых задач (закрывай кнопками)\n"
        "• /done <id> — закрыть задачу по ID\n"
        "• /settz <IANA_TZ> — часовой пояс (например Europe/Moscow)\n"
        "• /weekdays on|off — напоминания только по будням\n\n"
        "⚠️ Назначение через @упоминание отключено — используем меню."
    )
    await reply_privately_or_hint(message, text)

@dp.message(Command("settz"))
async def cmd_settz(message: Message, command: CommandObject):
    await try_delete(message)
    upsert_user(message.from_user.id, message.from_user.username)

    tz = (command.args or "").strip()
    try:
        if not tz:
            raise ValueError("empty")
        ZoneInfo(tz)
        set_user_tz(message.from_user.id, tz)
        await reply_privately_or_hint(message, f"Часовой пояс обновлён на {html.quote(tz)}")
    except Exception:
        await reply_privately_or_hint(message, "Укажи корректный IANA TZ, например: Europe/Moscow")

@dp.message(Command("weekdays"))
async def cmd_weekdays(message: Message, command: CommandObject):
    await try_delete(message)
    upsert_user(message.from_user.id, message.from_user.username)

    arg = (command.args or "").strip().lower()
    if arg not in {"on", "off"}:
        current = "on" if get_weekdays_only(message.from_user.id) else "off"
        await reply_privately_or_hint(message, f"Сейчас: {current}. Используй: /weekdays on|off")
        return
    set_weekdays_only(message.from_user.id, arg == "on")
    await reply_privately_or_hint(
        message,
        "Напоминания по будням: включены ✅" if arg == "on" else "Выходные тоже включены ✅"
    )

@dp.message(Command("task"))
async def cmd_task(message: Message, command: CommandObject):
    await try_delete(message)
    upsert_user(message.from_user.id, message.from_user.username)

    text = (command.args or "").strip()
    if not text:
        await reply_privately_or_hint(message, "Напиши задачу: /task <текст>")
        return
    add_task(message.from_user.id, message.from_user.username, message.chat.id, text)
    await reply_privately_or_hint(message, "✅ Задача добавлена. Открой /list, чтобы посмотреть все.")

@dp.message(Command("list"))
async def cmd_list(message: Message):
    await try_delete(message)
    upsert_user(message.from_user.id, message.from_user.username)

    rows = list_tasks_for_user(message.from_user.id)
    if not rows:
        await reply_privately_or_hint(message, "У тебя нет открытых задач ✨")
        return

    lines = [f"{r['id']}. {html.quote(r['text'])}" for r in rows]
    text = "Твои задачи:\n" + "\n".join(lines)
    await reply_privately_or_hint(message, text, reply_markup=tasks_keyboard(rows))

@dp.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject):
    await try_delete(message)
    upsert_user(message.from_user.id, message.from_user.username)

    if not command.args or not command.args.isdigit():
        await reply_privately_or_hint(message, "Укажи ID задачи: /done <id>")
        return
    ok = mark_done(int(command.args), message.from_user.id)
    await reply_privately_or_hint(message, "Готово ✅" if ok else "Не получилось закрыть задачу")

# ------------------ INLINE-КНОПКИ «ЗАКРЫТЬ» (в ЛС) ------------------
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

# ------------------ НАЗНАЧЕНИЕ ЧЕРЕЗ ПИКЕР ПОЛЬЗОВАТЕЛЯ ------------------
@dp.message(F.user_shared)
async def on_user_shared(message: Message):
    assignee_id = message.user_shared.user_id
    PENDING_ASSIGN[message.from_user.id] = assignee_id

    upsert_user(message.from_user.id, message.from_user.username)
    upsert_user(assignee_id, None)

    await message.answer("✍️ Напиши текст задачи для выбранного пользователя одним сообщением.")

@dp.message(F.text == "➕ Назначить себе")
async def menu_task_self(message: Message):
    # Эта кнопка только подсказывает синтаксис
    await message.answer("Напиши задачу для себя так: /task <текст задачи>")

@dp.message(F.text == "📋 Мои задачи")
async def menu_list_btn(message: Message):
    await cmd_list(message)

@dp.message(F.text == "✅ Закрыть задачу")
async def menu_done_btn(message: Message):
    await message.answer("Закрыть: /done <id> или кнопками в списке")

@dp.message(F.text == "ℹ️ Помощь")
async def menu_help_btn(message: Message):
    await cmd_help(message)

@dp.message(F.text == "🌍 Часовой пояс")
async def menu_tz_btn(message: Message):
    await message.answer("Установить часовой пояс: /settz <IANA_TZ> (например Europe/Moscow)")

@dp.message(F.text == "📅 Будни on/off")
async def menu_weekdays_btn(message: Message):
    await message.answer("Включить/выключить напоминания только по будням: /weekdays on|off")

# Следующее текстовое сообщение после выбора пользователя — текст задачи
@dp.message(F.text)
async def on_any_text(message: Message):
    assignee_id = PENDING_ASSIGN.pop(message.from_user.id, None)
    if assignee_id is None:
        return  # не в режиме назначения через пикер

    text = (message.text or "").strip()
    if not text:
        await message.answer("Текст задачи пустой. Напиши сообщение с задачей.")
        return

    add_task(assignee_tg_id=assignee_id, assignee_username=None, chat_id=message.chat.id, text=text)
    await message.answer("✅ Задача назначена выбранному пользователю.\nОткрой /list чтобы увидеть свои задачи.")

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
        if weekdays_only and datetime.now(ZoneInfo(tz)).weekday() >= 5:  # 5,6 = Сб,Вс
            continue

        tasks = list_tasks_for_user(tg_id)
        if not tasks:
            continue

        lines = [f"{row['id']}. {html.quote(row['text'])}" for row in tasks]
        now_local = datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")
        text = (
            f"Доброе утро! ({now_local} {tz})\n"
            "Твои актуальные задачи:\n" + "\n".join(lines) +
            "\n\nЗакрывай выполненные кнопками в /list или командой /done <id>"
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
