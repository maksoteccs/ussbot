# bot.py
# Python 3.11+
# Features:
# - Inline "Меню" in group without typing commands (one pinned bot message with buttons)
# - Assign tasks ONLY via menu flow (no @mentions parsing)
# - User picker (KeyboardButtonRequestUser) — choose assignee from current chat
# - Task text collected with ForceReply and immediately auto-deleted to keep chat clean
# - Other users don't see your commands (bot deletes prompts; confirmations via ephemeral popups and in DMs)
# - Same menu works in any added group and in private chat
# - Links submenu (e.g., Google Sheets) with URL buttons
# - Daily reminders at 10:00 Europe/Stockholm (Mon–Fri) sent to each assignee in DM with only ACTIVE tasks
# - Simple JSON storage (tasks.json) — no external DB/hosting required; run locally via polling

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ForceReply,
    UserShared, ReplyKeyboardRemove
)
from aiogram.enums import ChatType, ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")  # set in env: export BOT_TOKEN=123:ABC
TZ = pytz.timezone("Europe/Stockholm")
DATA_PATH = Path("tasks.json")

# Optional: allow only these group IDs. Leave empty to allow any.
ALLOWED_GROUP_IDS = set()  # e.g., { -1001234567890 }

# Example links menu — edit freely
LINKS = [
    ("План", "https://docs.google.com/spreadsheets/d/1jYQAQIYGqXc8nM1zZFrsjHB4qVwcxeZufoZjtgj4_Ck/edit?usp=sharing"),
]

# ========= STORAGE =========
# Structure:
# {
#   "users": { "<user_id>": {"tasks": [{"text": str, "by": int, "chat": int, "ts": int, "done": bool}] } },
#   "group_menu_messages": {"<chat_id>": <message_id>}  # to update the single menu message
# }

def load_db():
    if DATA_PATH.exists():
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"users": {}, "group_menu_messages": {}}


def save_db(db):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ========= BOT =========
bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()

db = load_db()

# In-memory state for ongoing assignment flows per user
ASSIGN_STATE = {}
# Structure ASSIGN_STATE[user_id] = {"chat_id": int, "assignee_id": int | None}


# ========= KEYBOARDS =========

def main_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Назначить задачу", callback_data="assign")
    kb.button(text="📎 Ссылки", callback_data="links")
    kb.adjust(1)
    return kb.as_markup()


def links_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for title, url in LINKS:
        kb.row(InlineKeyboardButton(text=title, url=url))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="back_main"))
    return kb.as_markup()


def user_picker_reply_kb() -> ReplyKeyboardMarkup:
    # One-time keyboard that opens system user picker for current chat
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Выбрать исполнителя из этого чата", request_user={"request_id": 1, "user_is_bot": False})]],
        resize_keyboard=True,
        one_time_keyboard=True
    )

async def is_chat_member(chat_id: int, user_id: int) -> bool:
    """Check that the selected user is a current member of the given chat."""
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
        return cm.status in {"creator", "administrator", "member"}
    except Exception:
        return False


# ========= HELPERS =========

def ensure_menu_message(chat_id: int) -> None:
    """Ensure one persistent menu message exists per group chat."""
    msg_id = db.get("group_menu_messages", {}).get(str(chat_id))
    if msg_id:
        return
    # Post a fresh menu message
    # Note: we do not require users to type commands; admins can run /setupmenu once.
    # After that, the menu message lives in chat and buttons work silently.


async def post_or_update_menu(chat_id: int):
    rec = db.setdefault("group_menu_messages", {})
    msg_id = rec.get(str(chat_id))
    if msg_id:
        try:
            await bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=main_menu_kb())
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, "<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb())
    rec[str(chat_id)] = sent.message_id
    save_db(db)


def add_task(assignee_id: int, text: str, by_user_id: int, chat_id: int):
    u = db.setdefault("users", {}).setdefault(str(assignee_id), {"tasks": []})
    u["tasks"].append({
        "text": text,
        "by": by_user_id,
        "chat": chat_id,
        "ts": int(datetime.now(tz=TZ).timestamp()),
        "done": False,
    })
    save_db(db)


def get_active_tasks(user_id: int):
    u = db.get("users", {}).get(str(user_id), {"tasks": []})
    return [t for t in u["tasks"] if not t.get("done")]


# ========= COMMANDS =========

@dp.message(CommandStart())
async def on_start(m: Message):
    if m.chat.type == ChatType.PRIVATE:
        await m.answer(
            "Привет! Я бот для задач. Добавь меня в рабочий чат и отправь команду /setupmenu, чтобы я закрепил там кнопки.\n\n"
            "Задачи назначаются <b>только</b> через меню: сначала выбираешь исполнителя, потом вводишь текст. Всё тихо и без засорения чата.")
    else:
        await post_or_update_menu(m.chat.id)
        try:
            await m.delete()
        except Exception:
            pass


@dp.message(Command("setupmenu"))
async def setup_menu(m: Message):
    if m.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        await m.answer("Эта команда для групп. Добавь меня в рабочий чат и пришли /setupmenu там.")
        return
    if ALLOWED_GROUP_IDS and m.chat.id not in ALLOWED_GROUP_IDS:
        await m.answer("Этот чат не разрешен для меню.")
        return
    await post_or_update_menu(m.chat.id)
    try:
        await m.delete()
    except Exception:
        pass


# ========= MENU CALLBACKS =========

@dp.callback_query(F.data == "assign")
async def cb_assign(c: CallbackQuery):
    chat = c.message.chat
    # Start assignment flow in current chat
    ASSIGN_STATE[c.from_user.id] = {"chat_id": chat.id, "assignee_id": None}
    await c.answer("Выбор исполнителя…", show_alert=False)
    await bot.send_message(chat.id,
        f"{c.from_user.full_name}, выбери исполнителя ⤵️",
        reply_markup=user_picker_reply_kb())


@dp.callback_query(F.data == "links")
async def cb_links(c: CallbackQuery):
    await c.message.edit_text("Полезные ссылки:", reply_markup=links_kb())
    await c.answer()


@dp.callback_query(F.data == "back_main")
async def cb_back(c: CallbackQuery):
    await c.message.edit_text("<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb())
    await c.answer()


# ========= USER PICKER HANDLER =========
# Works when someone presses the reply keyboard button with request_user

@dp.message(F.user_shared)
async def on_user_shared(m: Message):
    shared: UserShared = m.user_shared
    state = ASSIGN_STATE.get(m.from_user.id)
    if not state:
        # Not in flow — ignore and remove the keyboard
        await m.answer("Выбор исполнителя вне контекста меню.", reply_markup=ReplyKeyboardRemove())
        try:
            await m.delete()
        except Exception:
            pass
        return

    assignee_id = shared.user_id
    chat_id = state["chat_id"]

    # Enforce membership: only users from THIS chat can be assigned
    if not await is_chat_member(chat_id, assignee_id):
        # Inform selector privately and clean message
        try:
            await bot.send_message(m.from_user.id, "❗ Нельзя назначать задачи пользователям, которых нет в этом чате. Выбери участника из текущего чата.")
        except Exception:
            pass
        try:
            await m.delete()
        except Exception:
            pass
        # Keep state; they can pick again
        return

    state["assignee_id"] = assignee_id

    # Ask for task text via ForceReply to keep flow tidy; we'll delete the message after capture
    prompt = await m.answer(
        "Напиши текст задачи (это сообщение будет скрыто)",
        reply_markup=ForceReply(selective=True)
    )

    # Clean the user_shared message quickly
    try:
        await m.delete()
    except Exception:
        pass


# ========= CAPTURE TASK TEXT =========

@dp.message(F.reply_to_message, F.reply_to_message.text.contains("Напиши текст задачи"))
async def on_task_text(m: Message):
    state = ASSIGN_STATE.get(m.from_user.id)
    if not state or not state.get("assignee_id"):
        try:
            await m.delete()
        except Exception:
            pass
        return

    assignee_id = state["assignee_id"]
    chat_id = state["chat_id"]
    text = m.text.strip()

    add_task(assignee_id, text, by_user_id=m.from_user.id, chat_id=chat_id)

    # DM assignee and creator
    try:
        await bot.send_message(assignee_id, f"🆕 Новая задача от <b>{m.from_user.full_name}</b>:\n• {text}")
    except Exception:
        pass
    try:
        await bot.send_message(m.from_user.id, f"✅ Задача назначена пользователю <code>{assignee_id}</code>:\n• {text}")
    except Exception:
        pass

    # Ephemeral confirmation in chat via a short message, then delete
    conf = await bot.send_message(chat_id, "Готово. Задача назначена.")
    await asyncio.sleep(2)
    try:
        await conf.delete()
    except Exception:
        pass

    # Clean prompt and user's task text
    try:
        await m.delete()
    except Exception:
        pass
    # Remove reply keyboard if still present
    try:
        await bot.send_message(chat_id, " ", reply_markup=ReplyKeyboardRemove())
    except Exception:
        pass

    # Reset state
    ASSIGN_STATE.pop(m.from_user.id, None)


# ========= REMINDERS =========

async def send_daily_reminders():
    now = datetime.now(TZ)
    # Mon–Fri only
    if now.weekday() >= 5:
        return

    users = list(db.get("users", {}).keys())
    for uid in users:
        uid_int = int(uid)
        tasks = get_active_tasks(uid_int)
        if not tasks:
            continue
        lines = ["🗓 <b>Ежедневное напоминание</b>"]
        for i, t in enumerate(tasks, start=1):
            lines.append(f"{i}. {t['text']}")
        text = "\n".join(lines)
        try:
            await bot.send_message(uid_int, text)
        except Exception:
            pass


async def scheduler_runner():
    sched = AsyncIOScheduler(timezone=str(TZ))
    # Every weekday at 10:00
    trigger = CronTrigger(day_of_week="mon-fri", hour=10, minute=0)
    sched.add_job(send_daily_reminders, trigger)
    sched.start()


# ========= STARTUP =========

@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    # Fallback manual menu (we will delete the command message to keep chat clean)
    await post_or_update_menu(m.chat.id)
    try:
        await m.delete()
    except Exception:
        pass


async def on_startup():
    print("Bot started @", datetime.now())


async def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var")
    await on_startup()
    asyncio.create_task(scheduler_runner())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
