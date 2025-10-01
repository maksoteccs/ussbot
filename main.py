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
# commands removed
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ForceReply,
    UserShared, ReplyKeyboardRemove,
    ChatMemberUpdated
)
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ForceReply,
    UserShared, ReplyKeyboardRemove
)
from aiogram.enums import ChatType, ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
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
#   "group_menu_messages": {"<chat_id>": <message_id>},
#   "groups": {"<chat_id>": {"title": str, "members": {"<uid>": {"name": str, "is_bot": bool}}}},
#   "user_prefs": {"<user_id>": {"current_chat": int | null}}
# }

def load_db():
    if DATA_PATH.exists():
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    # ensure keys
    data.setdefault("users", {})
    data.setdefault("group_menu_messages", {})
    data.setdefault("groups", {})
    # ensure members subdicts
    for gid, meta in list(data.get("groups", {}).items()):
        meta.setdefault("members", {})
    data.setdefault("user_prefs", {})
    return data


def save_db(db):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)



    data.setdefault("user_prefs", {})
    return data


def save_db(db):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)




def save_db(db):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)


# ========= BOT =========
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = load_db()

# In-memory state for ongoing assignment flows per user
ASSIGN_STATE = {}
# Structure ASSIGN_STATE[user_id] = {"chat_id": int, "assignee_id": int | None}


# ========= KEYBOARDS =========

def main_menu_kb_group() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Назначить задачу", callback_data="assign")
    kb.button(text="📎 Ссылки", callback_data="links")
    kb.adjust(1)
    return kb.as_markup()


def main_menu_kb_private() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Назначить задачу", callback_data="assign")
    kb.button(text="🏷 Выбрать чат", callback_data="choose_chat")
    kb.button(text="📎 Ссылки", callback_data="links")
    kb.adjust(1)
    return kb.as_markup()


def links_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for title, url in LINKS:
        kb.row(InlineKeyboardButton(text=title, url=url))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="back_main"))
    return kb.as_markup()


def assign_list_kb(chat_id: int, page: int = 0, page_size: int = 20) -> InlineKeyboardMarkup:
    members = db.get("groups", {}).get(str(chat_id), {}).get("members", {})
    # only real users, sorted by display name
    items = [
        (int(uid), info.get("name", str(uid)))
        for uid, info in members.items()
        if not info.get("is_bot")
    ]
    items.sort(key=lambda x: x[1].lower())
    total = len(items)
    start = page * page_size
    end = start + page_size
    page_items = items[start:end]

    kb = InlineKeyboardBuilder()
    if page_items:
        for uid, name in page_items:
            kb.row(InlineKeyboardButton(text=name, callback_data=f"apick:{chat_id}:{uid}:{page}"))
    else:
        kb.row(InlineKeyboardButton(text="(пока нет известных участников)", callback_data="noop"))

    # pagination row
    buttons = []
    if start > 0:
        buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"alist:{chat_id}:{max(page-1,0)}"))
    if end < total:
        buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"alist:{chat_id}:{page+1}"))
    if buttons:
        kb.row(*buttons)

    # back
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="back_main"))
    return kb.as_markup()


def choose_chat_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    groups = db.get("groups", {})
    if not groups:
        kb.row(InlineKeyboardButton(text="(нет доступных чатов)", callback_data="noop"))
    else:
        for gid, meta in sorted(groups.items(), key=lambda x: x[1].get("title", "")):
            title = meta.get("title", str(gid))
            kb.row(InlineKeyboardButton(text=title, callback_data=f"set_chat:{gid}"))
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
            await bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=main_menu_kb_group())
            return
        except Exception:
            pass
    sent = await bot.send_message(chat_id, "<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb_group())
    rec[str(chat_id)] = sent.message_id
    # also store group title and ensure members dict
    g = db.setdefault("groups", {}).setdefault(str(chat_id), {})
    g["title"] = sent.chat.title or str(chat_id)
    g.setdefault("members", {})
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



# ========= MENU CALLBACKS =========

@dp.callback_query(F.data == "assign")
async def cb_assign(c: CallbackQuery):
    chat = c.message.chat
    # Determine target chat: in private — from user prefs; in group — current chat
    target_chat_id = chat.id
    if chat.type == ChatType.PRIVATE:
        prefs = db.get("user_prefs", {}).get(str(c.from_user.id), {})
        target_chat_id = prefs.get("current_chat")
        if not target_chat_id:
            await c.answer("Сначала выбери рабочий чат", show_alert=True)
            await c.message.edit_text("Выбери рабочий чат:", reply_markup=choose_chat_kb())
            return
    # Start assignment flow bound to target chat
    ASSIGN_STATE[c.from_user.id] = {"chat_id": int(target_chat_id), "assignee_id": None}
    await c.answer("Выбор исполнителя…", show_alert=False)
    await bot.send_message(chat.id,
        f"{c.from_user.full_name}, выбери исполнителя из этого чата ⤵️",
        reply_markup=assign_list_kb(int(target_chat_id), page=0))


@dp.callback_query(F.data == "links")
async def cb_links(c: CallbackQuery):
    await c.message.edit_text("Полезные ссылки:", reply_markup=links_kb())
    await c.answer()


@dp.callback_query(F.data == "choose_chat")
async def cb_choose_chat(c: CallbackQuery):
    await c.message.edit_text("Выбери рабочий чат:", reply_markup=choose_chat_kb())
    await c.answer()


@dp.callback_query(F.data.startswith("set_chat:"))
async def cb_set_chat(c: CallbackQuery):
    chat_id = c.data.split(":", 1)[1]
    db.setdefault("user_prefs", {}).setdefault(str(c.from_user.id), {})["current_chat"] = int(chat_id)
    save_db(db)
    title = db.get("groups", {}).get(str(chat_id), {}).get("title", str(chat_id))
    await c.message.edit_text(f"<b>Меню задач</b> — выбран чат: <i>{title}</i>", reply_markup=main_menu_kb_private())
    await c.answer("Чат выбран")
async def cb_links(c: CallbackQuery):
    await c.message.edit_text("Полезные ссылки:", reply_markup=links_kb())
    await c.answer()


@dp.callback_query(F.data == "back_main")
async def cb_back(c: CallbackQuery):
    if c.message.chat.type == ChatType.PRIVATE:
        await c.message.edit_text("<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb_private())
    else:
        await c.message.edit_text("<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb_group())
    await c.answer()


# ========= USER PICKER HANDLER =========
# Works when someone presses the reply keyboard button with request_user

# (fallback via user_shared is no longer primary; kept for rare cases)
@dp.message(F.user_shared)
async def on_user_shared(m: Message):
    shared: UserShared = m.user_shared
    state = ASSIGN_STATE.get(m.from_user.id)
    if not state:
        await m.answer("Выбор исполнителя вне контекста меню.", reply_markup=ReplyKeyboardRemove())
        try:
            await m.delete()
        except Exception:
            pass
        return

    assignee_id = shared.user_id
    chat_id = state["chat_id"]

    if not await is_chat_member(chat_id, assignee_id):
        try:
            await bot.send_message(m.from_user.id, "❗ Нельзя назначать задачи пользователям, которых нет в этом чате. Выбери участника из списка.")
        except Exception:
            pass
        try:
            await m.delete()
        except Exception:
            pass
        return

    state["assignee_id"] = assignee_id
    prompt = await m.answer(
        "Напиши текст задачи (это сообщение будет скрыто)",
        reply_markup=ForceReply(selective=True)
    )
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

# Track membership changes to build the per-chat roster
@dp.chat_member()
async def on_chat_member(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    user = event.from_user
    g = db.setdefault("groups", {}).setdefault(str(chat.id), {"title": chat.title or str(chat.id), "members": {}})
    members = g.setdefault("members", {})
    status = event.new_chat_member.status if event.new_chat_member else None
    if str(status) in {"kicked", "left"}:
        members.pop(str(user.id), None)
    else:
        members[str(user.id)] = {"name": user.full_name, "is_bot": user.is_bot}
    save_db(db)

# When bot is added to a group/supergroup, auto-post menu (no commands needed)
@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        try:
            new_status = event.new_chat_member.status
        except Exception:
            new_status = None
        if str(new_status) in {"administrator", "member"}:
            await post_or_update_menu(chat.id)
            # Save group title for private chat picker
            db.setdefault("groups", {}).setdefault(str(chat.id), {}).update({"title": chat.title or str(chat.id)})
            db["groups"][str(chat.id)].setdefault("members", {})
            save_db(db)

# Also learn members on any group message
@dp.message((F.chat.type == ChatType.GROUP) | (F.chat.type == ChatType.SUPERGROUP))
async def on_group_message_learn(m: Message):
    g = db.setdefault("groups", {}).setdefault(str(m.chat.id), {"title": m.chat.title or str(m.chat.id), "members": {}})
    g.setdefault("members", {})[str(m.from_user.id)] = {"name": m.from_user.full_name, "is_bot": m.from_user.is_bot}
    save_db(db)

# Private chat: show menu on any message and keep the dialog clean
@dp.message(F.chat.type == ChatType.PRIVATE)
async def on_private_any(m: Message):
    await m.answer("<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb_private())
    try:
        await m.delete()
    except Exception:
        pass

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var")
    await on_startup()
    asyncio.create_task(scheduler_runner())
    await dp.start_polling(bot)

# Private chat: show menu on any message and keep the dialog clean
@dp.message(F.chat.type == ChatType.PRIVATE)
async def on_private_any(m: Message):
    await m.answer("<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb_private())
    try:
        await m.delete()
    except Exception:
        pass

async def main():
    if not BOT_TOKEN:
        raise SystemExit("Please set BOT_TOKEN env var")
    await on_startup()
    asyncio.create_task(scheduler_runner())
    await dp.start_polling(bot)
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
