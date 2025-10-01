# bot.py
# Python 3.11+

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ForceReply,
    UserShared, ReplyKeyboardRemove,
    ChatMemberUpdated
)
)
from aiogram.enums import ChatType, ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# ========= CONFIG =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
TZ = pytz.timezone("Europe/Stockholm")
DATA_PATH = Path("tasks.json")

# ========= STORAGE =========

def load_db():
    if DATA_PATH.exists():
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}
    data.setdefault("users", {})
    data.setdefault("group_menu_messages", {})
    data.setdefault("groups", {})
    for gid, meta in list(data.get("groups", {}).items()):
        meta.setdefault("members", {})
    data.setdefault("user_prefs", {})
    return data


def save_db(db):
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)

# ========= BOT =========
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

db = load_db()
ASSIGN_STATE = {}

# ========= KEYBOARDS =========

def main_menu_kb_group() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Назначить задачу", callback_data="assign")
    kb.button(text="🔄 Обновить участников", callback_data="refresh_members")
    kb.button(text="📎 Ссылки", callback_data="links")
    kb.adjust(1)
    return kb.as_markup()


def main_menu_kb_private() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="📌 Назначить задачу", callback_data="assign")
    kb.button(text="🏷 Выбрать чат", callback_data="choose_chat")
    kb.button(text="🔄 Обновить участников", callback_data="refresh_members")
    kb.button(text="📎 Ссылки", callback_data="links")
    kb.adjust(1)
    return kb.as_markup()


def links_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for title, url in []:
        kb.row(InlineKeyboardButton(text=title, url=url))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="back_main"))
    return kb.as_markup()


def choose_chat_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    groups = db.get("groups", {})
    if not groups:
        kb.row(InlineKeyboardButton(text="(нет доступных чатов)", callback_data="noop"))
    else:
        for gid, meta in sorted(groups.items(), key=lambda x: (x[1].get("title") or str(x[0])).lower()):
            title = meta.get("title") or str(gid)
            kb.row(InlineKeyboardButton(text=title, callback_data=f"set_chat:{gid}"))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="back_main"))
    return kb.as_markup()

# ========= HELPERS =========

def assign_list_kb(chat_id: int) -> InlineKeyboardMarkup:
    members = db.get("groups", {}).get(str(chat_id), {}).get("members", {})
    items = [
        (int(uid), info.get("name", str(uid)))
        for uid, info in members.items()
        if not info.get("is_bot")
    ]
    items.sort(key=lambda x: x[1].lower())

    kb = InlineKeyboardBuilder()
    if items:
        for uid, name in items:
            kb.row(InlineKeyboardButton(text=name, callback_data=f"apick:{chat_id}:{uid}"))
    else:
        kb.row(InlineKeyboardButton(text="(нет участников)", callback_data="noop"))
    kb.row(InlineKeyboardButton(text="↩️ Назад", callback_data="back_main"))
    return kb.as_markup()


async def is_chat_member(chat_id: int, user_id: int) -> bool:
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
        return cm.status in {"creator", "administrator", "member"}
    except Exception:
        return False


async def is_admin(chat_id: int, user_id: int) -> bool:
    try:
        cm = await bot.get_chat_member(chat_id, user_id)
        return cm.status in {"creator", "administrator"}
    except Exception:
        return False


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


async def post_or_update_menu(chat_id: int):
    """Публикует или обновляет единое сообщение-меню в группе."""
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
    g = db.setdefault("groups", {}).setdefault(str(chat_id), {})
    g["title"] = sent.chat.title or str(chat_id)
    g.setdefault("members", {})
    save_db(db)


def post_cta_message_markup(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Я могу записывать тебе задачи", callback_data=f"iamhere:{chat_id}")
    ]])


async def post_cta_message(chat_id: int):
    """Публикует CTA для саморегистрации участников — без админских прав."""
    try:
        await bot.send_message(
            chat_id,
            "Я могу записывать тебе задачи. Нажми кнопку ниже, чтобы добавиться в список исполнителей этого чата и получать личные напоминания.",
            reply_markup=post_cta_message_markup(chat_id)
        )
    except Exception:
        pass

# ========= CALLBACKS =========

@dp.callback_query(F.data == "assign")
async def cb_assign(c: CallbackQuery):
    chat = c.message.chat
    target_chat_id = chat.id
    if chat.type == ChatType.PRIVATE:
        prefs = db.get("user_prefs", {}).get(str(c.from_user.id), {})
        target_chat_id = prefs.get("current_chat")
        if not target_chat_id:
            await c.answer("Сначала выбери рабочий чат", show_alert=True)
            await c.message.answer("<b>Меню задач</b>", reply_markup=main_menu_kb_private())
            return
    ASSIGN_STATE[c.from_user.id] = {"chat_id": int(target_chat_id), "assignee_id": None}
    await c.answer("Выбор исполнителя…")
    await bot.send_message(chat.id, "Выбери исполнителя:", reply_markup=assign_list_kb(int(target_chat_id)))


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


@dp.callback_query(F.data == "refresh_members")
async def cb_refresh_members(c: CallbackQuery):
    chat = c.message.chat
    if chat.type == ChatType.PRIVATE:
        await c.answer("Нажмите эту кнопку в нужном групповом чате", show_alert=True)
        return
    if not await is_admin(chat.id, c.from_user.id):
        await c.answer("Только админы чата могут обновлять список участников", show_alert=True)
        return
    added = 0
    try:
        admins = await bot.get_chat_administrators(chat.id)
        g = db.setdefault("groups", {}).setdefault(str(chat.id), {"title": chat.title or str(chat.id), "members": {}})
        for adm in admins:
            u = adm.user
            if u.is_bot:
                continue
            if str(u.id) not in g["members"]:
                g["members"][str(u.id)] = {"name": u.full_name, "is_bot": u.is_bot}
                added += 1
        save_db(db)
    except Exception:
        pass

    # Постим CTA, чтобы молчуны добавили себя сами
    await post_cta_message(chat.id)
    await c.answer(f"Обновлено админов: +{added}. Отправил сообщение с кнопкой для участников.", show_alert=True)


@dp.callback_query(F.data.startswith("iamhere:"))
async def cb_iamhere(c: CallbackQuery):
    chat_id = int(c.data.split(":", 1)[1])
    if not await is_chat_member(chat_id, c.from_user.id):
        await c.answer("Вы не состоите в этом чате.", show_alert=True)
        return
    g = db.setdefault("groups", {}).setdefault(str(chat_id), {"title": str(chat_id), "members": {}})
    g.setdefault("members", {})[str(c.from_user.id)] = {"name": c.from_user.full_name, "is_bot": c.from_user.is_bot}
    save_db(db)
    await c.answer("Готово: вы добавлены в список исполнителей.", show_alert=True)


@dp.callback_query(F.data == "back_main")
async def cb_back(c: CallbackQuery):
    if c.message.chat.type == ChatType.PRIVATE:
        await c.message.edit_text("<b>Меню задач</b>", reply_markup=main_menu_kb_private())
    else:
        await c.message.edit_text("<b>Меню задач</b> — назначайте задачи через кнопки ниже.", reply_markup=main_menu_kb_group())
    await c.answer()


@dp.message(F.reply_to_message, F.reply_to_message.text.contains("Напиши текст задачи"))
async def on_task_text(m: Message):
    state = ASSIGN_STATE.get(m.from_user.id)
    if not state or not state.get("assignee_id"):
        return
    assignee_id = state["assignee_id"]
    chat_id = state["chat_id"]
    text = m.text.strip()
    add_task(assignee_id, text, m.from_user.id, chat_id)
    try:
        await bot.send_message(assignee_id, f"🆕 Новая задача: {text}")
    except Exception:
        pass
    try:
        await bot.send_message(m.from_user.id, f"✅ Задача назначена пользователю {assignee_id}: {text}")
    except Exception:
        pass
    ASSIGN_STATE.pop(m.from_user.id, None)

# ========= REMINDERS =========

async def send_daily_reminders():
    now = datetime.now(TZ)
    if now.weekday() >= 5:
        return
    for uid in list(db.get("users", {}).keys()):
        tasks = get_active_tasks(int(uid))
        if not tasks:
            continue
        text = "🗓 <b>Ежедневное напоминание</b>\n" + "\n".join(f"{i+1}. {t['text']}" for i, t in enumerate(tasks))
        try:
            await bot.send_message(int(uid), text)
        except Exception:
            pass


async def scheduler_runner():
    sched = AsyncIOScheduler(timezone=str(TZ))
    trigger = CronTrigger(day_of_week="mon-fri", hour=10, minute=0)
    sched.add_job(send_daily_reminders, trigger)
    sched.start()

# ========= STARTUP =========

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


@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    """Когда бота добавили в чат — публикуем меню и CTA для саморегистрации."""
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    try:
        new_status = event.new_chat_member.status
    except Exception:
        new_status = None
    if str(new_status) in {"administrator", "member"}:
        # Сохраняем заголовок чата
        db.setdefault("groups", {}).setdefault(str(chat.id), {}).update({"title": chat.title or str(chat.id)})
        db["groups"][str(chat.id)].setdefault("members", {})
        save_db(db)
        # Публикуем меню и CTA
        await post_or_update_menu(chat.id)
        await post_cta_message(chat.id)

@dp.message(F.chat.type == ChatType.PRIVATE)
async def on_private(m: Message):
    await m.answer("<b>Меню задач</b>", reply_markup=main_menu_kb_private())

async def main():
    asyncio.create_task(scheduler_runner())
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped")
