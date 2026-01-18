import asyncio
import logging
import secrets
import string
import os
import re
from datetime import datetime, timedelta
from typing import Optional, Union

import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    BotCommand, ContentType
)
from aiogram.filters import Command, CommandStart, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

# === КОНФИГУРАЦИЯ ===
# !!! Введите свой реальный BOT_TOKEN !!!
BOT_TOKEN = "8562053785:AAG0TGlwwIY_dly-Wo7CzTf2F0GmR6A46Wg"
PROVIDER_TOKEN = ""
USE_PAYMENTS = False
DB_NAME = "bot.db"
# !!! Введите свой реальный username для Супер-Админа !!!
SUPER_ADMIN_USERNAME = "fenixkeeper"
# !!! ID/USERNAME Супер-Админа для ссылки на раскрытие !!!
SUPER_ADMIN_ID_FOR_LINK = "fenixkeeper"

# Новые параметры лимитов
DAILY_MESSAGE_LIMIT = 5  # Базовый лимит
SPECIAL_MESSAGE_LIMIT = 20  # Лимит для статуса "Особый"

SUPPORTED_CONTENT_TYPES = [
    ContentType.TEXT, ContentType.PHOTO, ContentType.VIDEO,
    ContentType.VOICE, ContentType.AUDIO, ContentType.ANIMATION, ContentType.STICKER
]

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

default_properties = DefaultBotProperties(parse_mode=ParseMode.HTML)
bot = Bot(token=BOT_TOKEN, default=default_properties)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)


# === КЛАСС РАБОТЫ С БД ===
class Database:
    def __init__(self, db_name):
        self.db_name = db_name

    async def create_tables(self):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    is_admin BOOLEAN DEFAULT 0,
                    is_super_admin BOOLEAN DEFAULT 0,
                    is_special BOOLEAN DEFAULT 0,
                    sub_expiry TEXT,
                    blocked_bot BOOLEAN DEFAULT 0,
                    banned BOOLEAN DEFAULT 0,
                    reg_date TEXT,
                    messages_sent_today INTEGER DEFAULT 0,
                    last_message_date TEXT NULL           
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS recipients (
                    user_id INTEGER PRIMARY KEY,
                    code TEXT UNIQUE,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    msg_id TEXT PRIMARY KEY,
                    from_user_id INTEGER,
                    to_user_id INTEGER,
                    content_type TEXT,
                    content_text TEXT, 
                    file_id TEXT,
                    caption TEXT,
                    revealed BOOLEAN DEFAULT 0,
                    sent_at TEXT,
                    scheduled_time TEXT NULL, 
                    tg_message_id INTEGER
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    channel_id INTEGER PRIMARY KEY,
                    title TEXT,
                    invite_link TEXT
                )
            """)
            await db.commit()

            # Добавляем новые колонки, если их нет
            try:
                await db.execute("ALTER TABLE messages ADD COLUMN scheduled_time TEXT NULL")
            except aiosqlite.OperationalError:
                pass

            try:
                await db.execute("ALTER TABLE users ADD COLUMN messages_sent_today INTEGER DEFAULT 0")
                await db.execute("ALTER TABLE users ADD COLUMN last_message_date TEXT NULL")
            except aiosqlite.OperationalError:
                pass

            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_special BOOLEAN DEFAULT 0")
            except aiosqlite.OperationalError:
                pass

            await db.commit()

    async def add_user(self, user_id, username, full_name):
        async with aiosqlite.connect(self.db_name) as db:
            now = datetime.now().isoformat()
            current_username = username if username else ""

            is_target = current_username and current_username.lower() == SUPER_ADMIN_USERNAME.lower()
            is_super = 1 if is_target else 0
            is_admin = 1 if is_target else 0

            await db.execute("""
                INSERT INTO users (user_id, username, full_name, reg_date, is_super_admin, is_admin)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET 
                    username=excluded.username, 
                    full_name=excluded.full_name,
                    is_super_admin = MAX(users.is_super_admin, excluded.is_super_admin), 
                    is_admin = MAX(users.is_admin, excluded.is_admin)
            """, (user_id, username, full_name, now, is_super, is_admin))
            await db.commit()

    async def get_user(self, user_id):
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def set_special_status(self, user_id, status: bool):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE users SET is_special = ? WHERE user_id = ?", (1 if status else 0, user_id))
            await db.commit()

    async def set_boss_subscription(self, user_id, days: int):
        async with aiosqlite.connect(self.db_name) as db:
            expiry = (datetime.now() + timedelta(days=days)).isoformat()
            await db.execute("UPDATE users SET sub_expiry = ? WHERE user_id = ?", (expiry, user_id))
            await db.commit()

    async def increment_message_count(self, user_id):
        async with aiosqlite.connect(self.db_name) as db:
            today_date = datetime.now().strftime("%Y-%m-%d")

            user_data = await self.get_user(user_id)
            last_date = user_data.get('last_message_date')

            if last_date != today_date:
                new_count = 1
                await db.execute("UPDATE users SET messages_sent_today = 1, last_message_date = ? WHERE user_id = ?",
                                 (today_date, user_id))
            else:
                new_count = user_data.get('messages_sent_today', 0) + 1
                await db.execute("UPDATE users SET messages_sent_today = messages_sent_today + 1 WHERE user_id = ?",
                                 (user_id,))

            await db.commit()
            return new_count

    async def get_recipient_by_code(self, code):
        async with aiosqlite.connect(self.db_name) as db:
            async with db.execute("SELECT user_id FROM recipients WHERE code = ?", (code,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def create_recipient_box(self, user_id):
        existing_code = await self.get_user_code(user_id)
        if existing_code:
            return existing_code

        while True:
            code = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
            async with aiosqlite.connect(self.db_name) as db:
                try:
                    await db.execute("INSERT INTO recipients (user_id, code) VALUES (?, ?)", (user_id, code))
                    await db.commit()
                    return code
                except aiosqlite.IntegrityError:
                    continue

    async def get_user_code(self, user_id):
        async with aiosqlite.connect(self.db_name) as db:
            async with db.execute("SELECT code FROM recipients WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                return row[0] if row else None

    async def save_message(self, msg_data):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("""
                INSERT INTO messages (msg_id, from_user_id, to_user_id, content_type, content_text, file_id, caption, sent_at, tg_message_id, scheduled_time)
                VALUES (:msg_id, :from_user_id, :to_user_id, :content_type, :content_text, :file_id, :caption, :sent_at, :tg_message_id, :scheduled_time)
            """, msg_data)
            await db.commit()

    async def get_messages_for_sending(self):
        """Получает запланированные сообщения, время которых наступило."""
        now = datetime.now().isoformat()
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                    "SELECT * FROM messages WHERE scheduled_time NOT NULL AND scheduled_time <= ? AND tg_message_id = 0",
                    (now,)) as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def update_message_tg_id(self, msg_id, tg_message_id):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE messages SET tg_message_id = ? WHERE msg_id = ?", (tg_message_id, msg_id))
            await db.commit()

    async def get_message(self, msg_id):
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM messages WHERE msg_id = ?", (msg_id,)) as cursor:
                row = await cursor.fetchone()
                return dict(row) if row else None

    async def update_message_revealed(self, msg_id):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE messages SET revealed = 1 WHERE msg_id = ?", (msg_id,))
            await db.commit()

    async def get_all_users(self):
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def get_stats(self):
        async with aiosqlite.connect(self.db_name) as db:
            async with db.execute("SELECT COUNT(*) FROM users") as c1:
                uc = (await c1.fetchone())[0]
            async with db.execute("SELECT COUNT(*) FROM messages") as c2:
                mc = (await c2.fetchone())[0]
            return uc, mc

    async def add_channel(self, channel_id, title, invite_link):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("INSERT OR REPLACE INTO channels (channel_id, title, invite_link) VALUES (?, ?, ?)",
                             (channel_id, title, invite_link))
            await db.commit()

    async def get_channels(self):
        async with aiosqlite.connect(self.db_name) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM channels") as cursor:
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]

    async def delete_channel(self, channel_id):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
            await db.commit()

    async def set_ban_status(self, user_id, status: bool):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE users SET banned = ? WHERE user_id = ?", (1 if status else 0, user_id))
            await db.commit()

    async def set_admin_status(self, user_id, status: bool):
        async with aiosqlite.connect(self.db_name) as db:
            await db.execute("UPDATE users SET is_admin = ? WHERE user_id = ?", (1 if status else 0, user_id))
            await db.commit()


db = Database(DB_NAME)


# === СОСТОЯНИЯ FSM ===
class SendingFlow(StatesGroup):
    choosing_template = State()
    writing_custom = State()
    sending_to = State()


class AdminFlow(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_channel_link = State()
    waiting_for_ban_id = State()
    waiting_for_admin_id = State()
    waiting_for_special_id = State()
    waiting_for_boss_id = State()


class TimeSendingFlow(StatesGroup):
    choosing_template = State()
    writing_custom = State()
    sending_to = State()
    waiting_for_time = State()


# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===
TEMPLATES = {
    "tpl_confession": "Хочу признаться… ",
    "tpl_compliment": "Ты настолько… ",
    "tpl_question": "Мне интересно… ",
    "tpl_hate": "Меня бесит, что ты... "
}


def get_sender_display(msg_data: dict, user_data: dict) -> str:
    username = user_data.get('username')
    first_name = user_data.get('full_name') or "Аноним"
    user_id = user_data.get('user_id')
    if username: return f"@{username}"
    return f'<a href="tg://user?id={user_id}">{first_name}</a>'


def is_boss_active(expiry_str: Optional[str]) -> bool:
    """Проверяет, активна ли подписка Босса."""
    if not expiry_str: return False
    try:
        expiry = datetime.fromisoformat(expiry_str)
        return expiry > datetime.now()
    except ValueError:
        return False


# Функция для определения лимита пользователя
def get_user_limit(user_db: dict) -> float:
    """Возвращает максимальное количество сообщений в день для пользователя."""
    if user_db.get('is_admin') or is_boss_active(user_db.get('sub_expiry')):
        return float('inf')  # Неограничено для Админов и Боссов
    if user_db.get('is_special'):
        return SPECIAL_MESSAGE_LIMIT  # Увеличенный лимит для Особых
    return DAILY_MESSAGE_LIMIT  # Базовый лимит


def get_message_kb(msg_id: str, revealed: bool) -> Optional[InlineKeyboardMarkup]:
    if revealed:
        return None

    ready_text = f"Здравствуйте, хочу раскрыть отправителя сообщения с ID {msg_id}, как открыть эту функцию?"
    link = f"https://t.me/{SUPER_ADMIN_ID_FOR_LINK}?start=reveal_{msg_id}&text={ready_text}"

    buttons = [
        [InlineKeyboardButton(text="🔓 Раскрыть", callback_data=f"reveal_{msg_id}")],
        [InlineKeyboardButton(text="💬 Связаться с админом", url=link)]
    ]

    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_template_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💌 Признание", callback_data="tpl_confession")],
        [InlineKeyboardButton(text="✨ Комплимент", callback_data="tpl_compliment")],
        [InlineKeyboardButton(text="🤔 Вопрос", callback_data="tpl_question")],
        [InlineKeyboardButton(text="🤬 Хейт", callback_data="tpl_hate")],
        [InlineKeyboardButton(text="✏️ Свое сообщение", callback_data="tpl_custom")],
        [InlineKeyboardButton(text="🔙 Отмена", callback_data="cancel")]
    ])


async def check_subscription(user_id: int) -> bool:
    channels = await db.get_channels()
    if not channels: return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(ch['channel_id'], user_id)
            if member.status not in ['member', 'administrator', 'creator']: return False
        except:
            continue
    return True


async def get_subs_kb():
    channels = await db.get_channels()
    buttons = []
    for ch in channels:
        buttons.append([InlineKeyboardButton(text=f"➡️ {ch['title']}", url=ch['invite_link'])])
    buttons.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subs")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# === ФУНКЦИЯ ОТПРАВКИ СООБЩЕНИЯ (для Send и Scheduler) ===
async def send_message_to_recipient(msg_db_data: dict, recipient_id: int, message_to_answer: Message = None):
    content_type = msg_db_data['content_type']

    try:
        sent_msg = None
        header = "📨 <b>Вам новое анонимное сообщение!</b>\n\n"

        kb = get_message_kb(msg_db_data['msg_id'], False)

        if content_type == ContentType.TEXT:
            sent_msg = await bot.send_message(recipient_id, header + msg_db_data['content_text'], reply_markup=kb)

        elif content_type == ContentType.STICKER:
            await bot.send_message(recipient_id, header)
            sent_msg = await bot.send_sticker(recipient_id, msg_db_data['file_id'], reply_markup=kb)

        elif msg_db_data['file_id']:
            method = getattr(bot, f"send_{content_type}", None)
            if method is None:
                if message_to_answer:
                    await message_to_answer.answer("⚠️ Неподдерживаемый медиа-тип.")
                return False

            final_caption = header + (msg_db_data['caption'] or "")

            if content_type in [ContentType.PHOTO, ContentType.VIDEO, ContentType.AUDIO, ContentType.ANIMATION,
                                ContentType.VOICE]:
                sent_msg = await method(recipient_id, msg_db_data['file_id'], caption=final_caption, reply_markup=kb)
            else:
                if message_to_answer:
                    await message_to_answer.answer("⚠️ Неподдерживаемый медиа-тип.")
                return False

        if sent_msg:
            await db.update_message_tg_id(msg_db_data['msg_id'], sent_msg.message_id)
            return True
        return False

    except TelegramForbiddenError:
        if message_to_answer:
            await message_to_answer.answer("⚠️ Пользователь заблокировал бота.")
        return False
    except Exception as e:
        logging.error(f"Err sending: {e}")
        if message_to_answer:
            await message_to_answer.answer("⚠️ Ошибка отправки.")
        return False


# --- HANDLERS ---

## 1. Start Command & Subscription Check
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user = message.from_user
    await db.add_user(user.id, user.username, user.full_name)
    user_db = await db.get_user(user.id)

    if user_db and user_db.get('banned'):
        return await message.answer("🚫 Вы заблокированы в боте.")

    await state.clear()
    if not await check_subscription(user.id):
        return await message.answer("⚠️ Для использования бота подпишитесь на каналы:",
                                    reply_markup=await get_subs_kb())

    # Получаем личную ссылку пользователя
    code = await db.get_user_code(user.id) or await db.create_recipient_box(user.id)
    me = await bot.get_me()
    my_link = f"https://t.me/{me.username}?start={code}"

    # Deep Link Logic
    args = message.text.split()
    start_payload = args[1] if len(args) > 1 else None

    if start_payload and not start_payload.startswith("reveal_"):
        recipient_id = await db.get_recipient_by_code(start_payload)

        # --- Дополнительная фича: Показ личной ссылки при переходе по чужой ---

        await message.answer(
            f"👋 Привет, {user.first_name}!\n"
            f"🔗 <b>Твоя личная ссылка для анонимных сообщений:</b>\n"
            f"<code>{my_link}</code>\n\n"
            f"<i>Можешь делиться ею с друзьями!</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={my_link}")],
                [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile")],
            ]),
            disable_web_page_preview=True
        )

        # ---------------------------------------------------------------------

        if not recipient_id: return await message.answer("❌ Неверная ссылка. Ящик не найден.")
        if recipient_id == user.id: return await message.answer("❌ Вы не можете отправить сообщение самому себе!")

        await state.update_data(target_code=start_payload, target_id=recipient_id)

        # Проверка лимита перед началом отправки
        user_db = await db.get_user(user.id)
        current_limit = get_user_limit(user_db)

        if current_limit != float('inf') and user_db.get('messages_sent_today', 0) >= current_limit:
            return await message.answer(
                f"❌ Вы превысили лимит в {current_limit} анонимных сообщений в день. Попробуйте завтра или получите новый статус.")

        await message.answer(f"🎯 Вы пишете анонимно пользователю (код: {start_payload}**).\nВыберите шаблон:",
                             reply_markup=get_template_kb())
        await state.set_state(SendingFlow.choosing_template)
        return

    # Normal Start Logic
    await message.answer(
        f"👋 Привет, {user.first_name}!\n🔗 <b>Твоя ссылка для анонимных сообщений:</b>\n<code>{my_link}</code>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📤 Поделиться ссылкой", url=f"https://t.me/share/url?url={my_link}")],
            [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile")],
            [InlineKeyboardButton(text="❓ Помощь / FAQ", callback_data="my_help")]
        ]),
        disable_web_page_preview=True
    )


@router.callback_query(F.data == "check_subs")
async def check_subs_btn(callback: CallbackQuery):
    if await check_subscription(callback.from_user.id):
        await callback.message.delete()
        await callback.message.answer("🎉 Спасибо! Нажмите /start")
    else:
        await callback.answer("❌ Вы не подписались на все каналы!", show_alert=True)


## 2. Help/FAQ Handler
@router.message(Command("help"))
@router.callback_query(F.data == "my_help")
async def cmd_help(event: Union[Message, CallbackQuery]):
    text = (
        "❓ <b>Помощь и Ответы на вопросы</b>\n\n"
        "1. Как отправить анонимное сообщение?\n"
        "   — Используйте команду /send или отправьте личную ссылку пользователя, которому хотите написать. Ссылку можно взять в разделе /profile.\n\n"
        "2. Статусы и Бонусы:\n"
        f"   — 👤 Пользователь: Лимит {DAILY_MESSAGE_LIMIT} сообщений в день.\n"
        f"   — ✨ Особый: Лимит {SPECIAL_MESSAGE_LIMIT} сообщений в день.\n"
        "   — 😎 Босс/⭐️ Админ: Безлимит, доступ к планировщику (`/send_time`) и бесплатному раскрытию отправителей.\n\n"
        "3. Как раскрыть отправителя?\n"
        "   — Нажмите кнопку 'Раскрыть' под сообщением. Если у вас статус **Босс/Админ**, раскрытие произойдет немедленно. В противном случае, вам нужно связаться с администрацией.\n"
        "4. Как запланировать сообщение?\n"
        "   — Используйте команду /send_time (только для Боссов/Админов).\n\n"
        "5. Как проверить лимит?\n"
        "   — Используйте команду /limit."
    )
    if isinstance(event, Message):
        await event.answer(text)
    else:
        await event.message.edit_text(text)
        await event.answer()


## 3. Limit Check
@router.message(Command("limit"))
async def cmd_limit(message: Message):
    user_db = await db.get_user(message.from_user.id)

    if user_db:
        current_limit = get_user_limit(user_db)

        if current_limit == float('inf'):
            status_text = "✅ У вас нет ограничений на отправку сообщений! (Статус: Босс/Админ)"
        else:
            sent = user_db.get('messages_sent_today', 0)
            remaining = int(current_limit) - sent

            if remaining > 0:
                status_text = f"✉️ Ваш лимит на сегодня:\n" \
                              f"Отправлено: {sent}\n" \
                              f"Осталось: {remaining} из {int(current_limit)}."
            else:
                status_text = f"❌ Лимит исчерпан. Вы отправили {int(current_limit)} сообщений сегодня. Попробуйте завтра или получите новый статус."
    else:
        status_text = "❌ Профиль не найден. Нажмите /start."

    await message.answer(status_text)


## 4. Sending Flow (Immediate)
@router.message(Command("send"))
async def cmd_send(message: Message, state: FSMContext):
    if not await check_subscription(message.from_user.id):
        return await message.answer("⚠️ Подпишитесь на каналы!", reply_markup=await get_subs_kb())

    user_db = await db.get_user(message.from_user.id)
    current_limit = get_user_limit(user_db)

    if current_limit != float('inf') and user_db.get('messages_sent_today', 0) >= current_limit:
        return await message.answer(
            f"❌ Вы превысили лимит в {int(current_limit)} анонимных сообщений в день. Попробуйте завтра или получите новый статус.")

    await state.clear()
    await message.answer("🎯 Выберите шаблон сообщения:", reply_markup=get_template_kb())
    await state.set_state(SendingFlow.choosing_template)


@router.callback_query(SendingFlow.choosing_template)
async def tpl_chosen(callback: CallbackQuery, state: FSMContext):
    code = callback.data
    if code == "cancel":
        await state.clear()
        return await callback.message.edit_text("❌ Отменено.")

    prefix = TEMPLATES.get(code, "")

    if code == "tpl_custom":
        await callback.message.edit_text("✏️ Напишите ваше сообщение:")
    else:
        await callback.message.edit_text(f"✍️ Допишите сообщение:\n\n<i>{prefix}...</i>")

    await state.update_data(prefix=prefix)
    await state.set_state(SendingFlow.writing_custom)
    await callback.answer()


@router.message(SendingFlow.writing_custom)
async def receive_content(message: Message, state: FSMContext):
    if message.content_type not in SUPPORTED_CONTENT_TYPES:
        return await message.answer("❌ Этот тип файлов не поддерживается.")

    data = await state.get_data()
    prefix = data.get("prefix", "")

    content_text = ""
    file_id = None
    caption = ""

    if message.text: content_text = prefix + message.text
    if message.caption:
        caption = prefix + message.caption
    elif prefix and not message.text:
        caption = prefix

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.animation:
        file_id = message.animation.file_id
    elif message.sticker:
        file_id = message.sticker.file_id

    await state.update_data(
        content_type=message.content_type,
        content_text=content_text,
        file_id=file_id,
        caption=caption,
    )

    if data.get("target_id"):
        await finalize_sending_immediate(message, state)
    else:
        await message.answer("📬 Введите <b>код получателя</b> (или ссылку):")
        await state.set_state(SendingFlow.sending_to)


@router.message(SendingFlow.sending_to)
async def process_code(message: Message, state: FSMContext):
    text = message.text.strip()
    code = text.split("start=")[-1] if "start=" in text else text

    recipient_id = await db.get_recipient_by_code(code)

    if not recipient_id:
        return await message.answer("❌ Код не найден. Попробуйте снова.")

    if recipient_id == message.from_user.id:
        return await message.answer("❌ Нельзя отправлять себе.")

    await state.update_data(target_id=recipient_id)
    await finalize_sending_immediate(message, state)


async def finalize_sending_immediate(message: Message, state: FSMContext):
    data = await state.get_data()
    recipient_id = data['target_id']
    msg_id = secrets.token_hex(8)
    now = datetime.now().isoformat()

    msg_db_data = {
        "msg_id": msg_id,
        "from_user_id": message.from_user.id,
        "to_user_id": recipient_id,
        "content_type": data['content_type'],
        "content_text": data.get("content_text"),
        "file_id": data.get("file_id"),
        "caption": data.get("caption"),
        "sent_at": now,
        "scheduled_time": None,
        "tg_message_id": 0
    }

    await db.save_message(msg_db_data)

    await db.increment_message_count(message.from_user.id)

    success = await send_message_to_recipient(msg_db_data, recipient_id, message)
    if success:
        await message.answer("✅ Сообщение успешно отправлено!")

    await state.clear()


## 5. Scheduled Sending Flow
@router.message(Command("send_time"))
async def cmd_send_time(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    # Только Админ и Босс имеют доступ к планировщику
    is_admin_or_boss = user_db.get('is_admin', False) or is_boss_active(user_db.get('sub_expiry'))

    if not user_db or not is_admin_or_boss:
        return await message.answer("❌ Для запланированной отправки требуется статус <b>😎 Босс</b> или ⭐️ Админ.")

    await state.clear()
    await message.answer("🎯 Выберите шаблон сообщения для запланированной отправки:", reply_markup=get_template_kb())
    await state.set_state(TimeSendingFlow.choosing_template)


@router.callback_query(TimeSendingFlow.choosing_template)
async def tpl_chosen_time(callback: CallbackQuery, state: FSMContext):
    await tpl_chosen(callback, state)
    await state.set_state(TimeSendingFlow.writing_custom)


@router.message(TimeSendingFlow.writing_custom)
async def receive_content_time(message: Message, state: FSMContext):
    if message.content_type not in SUPPORTED_CONTENT_TYPES:
        return await message.answer("❌ Этот тип файлов не поддерживается.")

    data = await state.get_data()
    prefix = data.get("prefix", "")
    content_text = ""
    file_id = None
    caption = ""

    if message.text: content_text = prefix + message.text
    if message.caption:
        caption = prefix + message.caption
    elif prefix and not message.text:
        caption = prefix

    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.video:
        file_id = message.video.file_id
    elif message.voice:
        file_id = message.voice.file_id
    elif message.audio:
        file_id = message.audio.file_id
    elif message.animation:
        file_id = message.animation.file_id
    elif message.sticker:
        file_id = message.sticker.file_id

    await state.update_data(
        content_type=message.content_type,
        content_text=content_text,
        file_id=file_id,
        caption=caption
    )

    await message.answer("📬 Введите <b>код получателя</b> (или ссылку):")
    await state.set_state(TimeSendingFlow.sending_to)


@router.message(TimeSendingFlow.sending_to)
async def process_code_time(message: Message, state: FSMContext):
    text = message.text.strip()
    code = text.split("start=")[-1] if "start=" in text else text

    recipient_id = await db.get_recipient_by_code(code)

    if not recipient_id:
        return await message.answer("❌ Код не найден. Попробуйте снова.")

    if recipient_id == message.from_user.id:
        return await message.answer("❌ Нельзя отправлять себе.")

    await state.update_data(target_id=recipient_id)
    await message.answer("⏰ Введите время отправки в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (например, 20.12.2025 15:30):")
    await state.set_state(TimeSendingFlow.waiting_for_time)


@router.message(TimeSendingFlow.waiting_for_time)
async def process_time_input(message: Message, state: FSMContext):
    try:
        schedule_dt = datetime.strptime(message.text.strip(), "%d.%m.%Y %H:%M")
        if schedule_dt <= datetime.now():
            return await message.answer("❌ Время должно быть в будущем.")

        schedule_iso = schedule_dt.isoformat()
    except ValueError:
        return await message.answer("❌ Неверный формат даты/времени. Попробуйте: <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>")

    data = await state.get_data()
    msg_id = secrets.token_hex(8)
    now = datetime.now().isoformat()

    msg_db_data = {
        "msg_id": msg_id,
        "from_user_id": message.from_user.id,
        "to_user_id": data['target_id'],
        "content_type": data['content_type'],
        "content_text": data.get("content_text"),
        "file_id": data.get("file_id"),
        "caption": data.get("caption"),
        "sent_at": now,
        "scheduled_time": schedule_iso,
        "tg_message_id": 0
    }

    await db.save_message(msg_db_data)

    await message.answer(f"✅ Сообщение запланировано на <b>{message.text.strip()}</b>.")
    await state.clear()


## 6. Reveal Handler (Callback Query)
@router.callback_query(F.data.startswith("reveal_"))
async def reveal_handler(callback: CallbackQuery):
    msg_id = callback.data.split("_")[1]
    msg = await db.get_message(msg_id)
    if not msg: return await callback.answer("Ошибка: сообщение не найдено", show_alert=True)

    user_who_clicked = await db.get_user(callback.from_user.id)
    # Админы/Супер-Админы и Боссы могут раскрывать
    is_privileged = user_who_clicked.get('is_admin', False) or is_boss_active(user_who_clicked.get('sub_expiry'))

    if msg['revealed']:
        sender = await db.get_user(msg['from_user_id'])
        return await callback.answer(f"Отправитель: {get_sender_display(msg, sender)}", show_alert=True)

    # Логика: Если пользователь Привилегирован -> Раскрываем
    if is_privileged:
        await perform_reveal(callback.message.chat.id, msg)
        await callback.answer("Успешно раскрыто!", show_alert=True)
        return

    # Логика: Если не Привилегирован -> Просим связаться
    await callback.answer(
        "❌ У вас нет прав на бесплатное раскрытие. Пожалуйста, используйте кнопку 'Связаться с админом' для обсуждения условий.",
        show_alert=True)


## 7. Reveal Handler (Command for Admins)
@router.message(Command("reveal"))
async def cmd_reveal_by_id(message: Message, command: CommandObject):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_admin'):
        return await message.answer("❌ Команда доступна только Администраторам.")

    if not command.args:
        return await message.answer("Введите ID сообщения для раскрытия. Формат: `/reveal [ID_сообщения]`")

    msg_id = command.args.strip()
    msg = await db.get_message(msg_id)

    if not msg:
        return await message.answer(f"❌ Сообщение с ID **{msg_id}** не найдено.")

    if msg['revealed']:
        return await message.answer(f"⚠️ Сообщение с ID **{msg_id}** уже раскрыто.")

    success = await perform_reveal(message.chat.id, msg, is_command=True)

    if success:
        await message.answer(f"✅ Сообщение с ID **{msg_id}** успешно раскрыто.")
    else:
        await message.answer(
            f"⚠️ Ошибка при раскрытии сообщения с ID **{msg_id}**. Возможно, оригинальное сообщение TG удалено.")


async def perform_reveal(chat_id: int, msg: dict, is_command: bool = False):
    """Обновляет статус раскрытия в БД и редактирует сообщение TG."""

    await db.update_message_revealed(msg['msg_id'])
    sender = await db.get_user(msg['from_user_id'])
    display_text = f"🕵️‍♂️ <b>Отправитель раскрыт:</b> {get_sender_display(msg, sender)}"

    if is_command:
        return bool(msg['tg_message_id'])

    try:
        if msg['content_type'] == ContentType.TEXT:
            new_text = f"{display_text}\n\n{msg['content_text']}"
            await bot.edit_message_text(new_text, chat_id=chat_id, message_id=msg['tg_message_id'], reply_markup=None)

        elif msg['content_type'] == ContentType.STICKER:
            await bot.send_message(chat_id, display_text, reply_to_message_id=msg['tg_message_id'])
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=msg['tg_message_id'], reply_markup=None)

        elif msg['file_id']:
            new_caption = f"{display_text}\n\n{msg['caption'] or ''}"
            await bot.edit_message_caption(caption=new_caption, chat_id=chat_id, message_id=msg['tg_message_id'],
                                           reply_markup=None)

        return True

    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            logging.error(f"Error during message edit: {e}")
        return True
    except Exception as e:
        logging.error(f"Error during message edit: {e}")
        return False


## 8. Profile
@router.callback_query(F.data == "my_profile")
@router.message(Command("profile"))
async def my_profile(event: Union[Message, CallbackQuery]):
    user = event.from_user
    await db.add_user(user.id, user.username, user.full_name)
    code = await db.get_user_code(user.id)
    user_db = await db.get_user(user.id)

    if not user_db:
        return await (event.message.answer if isinstance(event, CallbackQuery) else event.answer)(
            "❌ Ошибка: данные профиля не найдены.")

    status = "👤 Пользователь"
    bonus_info = f"Лимит: {DAILY_MESSAGE_LIMIT} сообщений в день."

    if user_db.get('is_super_admin'):
        status = "👑 Супер-Админ"
        bonus_info = "Безлимитная отправка, планировщик и бесплатное раскрытие отправителей."
    elif is_boss_active(user_db.get('sub_expiry')):
        date_end = datetime.fromisoformat(user_db['sub_expiry']).strftime('%d.%m.%Y')
        status = f"😎 Босс (до {date_end})"
        bonus_info = "Безлимитная отправка, планировщик и бесплатное раскрытие отправителей."
    elif user_db.get('is_admin'):
        status = "⭐️ Админ"
        bonus_info = "Безлимитная отправка, планировщик и бесплатное раскрытие отправителей."
    elif user_db.get('is_special'):
        status = "✨ Особый"
        bonus_info = f"Увеличенный лимит: {SPECIAL_MESSAGE_LIMIT} сообщений в день."

    text = (
        f"👤 <b>Ваш профиль:</b>\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"🔰 Статус: {status}\n"
        f"🎁 Бонусы: {bonus_info}"
    )

    if isinstance(event, Message):
        await event.answer(text)
    else:
        await event.message.edit_text(text)


## 9. Admin Panel & Status Management
@router.message(Command("admin"))
async def admin_panel(message: Message):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_admin'):
        return await message.answer("❌ Команда не найдена.")

    stats = await db.get_stats()

    kb_rows = [
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🔨 Бан по ID", callback_data="adm_ban")],
        [InlineKeyboardButton(text="➕ Канал (по ссылке)", callback_data="adm_add_chan")],
        [InlineKeyboardButton(text="➖ Канал", callback_data="adm_del_chan")]
    ]

    if user_db.get('is_super_admin'):
        kb_rows.append([InlineKeyboardButton(text="⭐️ Назначить Админа", callback_data="adm_give_admin")])
        kb_rows.append([InlineKeyboardButton(text="✨ Выдать 'Особый'", callback_data="adm_give_special")])
        kb_rows.append([InlineKeyboardButton(text="😎 Выдать 'Босс' (30 дн)", callback_data="adm_give_boss")])

    await message.answer(
        f"👑 <b>Админ-панель</b>\n\n"
        f"👥 Пользователей: {stats[0]}\n"
        f"✉️ Сообщений: {stats[1]}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )


@router.callback_query(F.data == "adm_broadcast")
async def start_broadcast(callback: CallbackQuery, state: FSMContext):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await callback.answer("Нет доступа.")

    await callback.message.answer("Введите сообщение для рассылки (или перешлите его):")
    await state.set_state(AdminFlow.waiting_for_broadcast)
    await callback.answer()


@router.message(AdminFlow.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await message.answer("Нет доступа.")

    users = await db.get_all_users()
    count = 0
    await message.answer(f"Начинаю рассылку на {len(users)} чел...")
    for u in users:
        try:
            await bot.copy_message(u['user_id'], message.chat.id, message.message_id)
            count += 1
            await asyncio.sleep(0.05)
        except:
            pass
    await message.answer(f"✅ Рассылка завершена. Доставлено: {count}")
    await state.clear()


@router.callback_query(F.data == "adm_ban")
async def ban_user_start(callback: CallbackQuery, state: FSMContext):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await callback.answer("Нет доступа.")

    await callback.message.answer("Введите ID пользователя для бана:")
    await state.set_state(AdminFlow.waiting_for_ban_id)
    await callback.answer()


@router.message(AdminFlow.waiting_for_ban_id)
async def process_ban(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await message.answer("Нет доступа.")

    try:
        uid = int(message.text)
        await db.set_ban_status(uid, True)
        await message.answer(f"✅ Пользователь {uid} забанен.")
    except:
        await message.answer("❌ Некорректный ID")
    await state.clear()


# --- Status Management Handlers ---

@router.callback_query(F.data == "adm_give_admin")
async def ask_admin(callback: CallbackQuery, state: FSMContext):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_super_admin'): return await callback.answer("Нет доступа.")

    await callback.message.answer("Введите ID и статус (1 - назначить, 0 - снять) через пробел для Админа:")
    await state.set_state(AdminFlow.waiting_for_admin_id)
    await callback.answer()


@router.message(AdminFlow.waiting_for_admin_id)
async def process_admin_status(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_super_admin'): return await message.answer("Нет доступа.")

    try:
        parts = message.text.split()
        if len(parts) < 2: raise ValueError
        uid = int(parts[0])
        status = True if int(parts[1]) == 1 else False

        await db.set_admin_status(uid, status)
        action = "назначен" if status else "снят"
        await message.answer(f"✅ Пользователь {uid} {action} Админом.")

    except:
        await message.answer("❌ Некорректный формат. Используйте 'ID 1' или 'ID 0'.")

    await state.clear()


@router.callback_query(F.data == "adm_give_special")
async def ask_special(callback: CallbackQuery, state: FSMContext):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_super_admin'): return await callback.answer("Нет доступа.")

    await callback.message.answer("Введите ID пользователя для статуса 'Особый':")
    await state.set_state(AdminFlow.waiting_for_special_id)
    await callback.answer()


@router.message(AdminFlow.waiting_for_special_id)
async def give_special(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_super_admin'): return await message.answer("Нет доступа.")

    try:
        uid = int(message.text)
        await db.set_special_status(uid, True)
        await message.answer(f"✅ Пользователь {uid} теперь Особый (лимит {SPECIAL_MESSAGE_LIMIT} сообщений).")
    except:
        await message.answer("❌ Ошибка ID.")
    await state.clear()


@router.callback_query(F.data == "adm_give_boss")
async def ask_boss(callback: CallbackQuery, state: FSMContext):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_super_admin'): return await callback.answer("Нет доступа.")

    await callback.message.answer("Введите ID для подписки 'Босс' (выдам на 30 дней):")
    await state.set_state(AdminFlow.waiting_for_boss_id)
    await callback.answer()


@router.message(AdminFlow.waiting_for_boss_id)
async def give_boss(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_super_admin'): return await message.answer("Нет доступа.")

    try:
        uid = int(message.text)
        await db.set_boss_subscription(uid, 30)
        await message.answer(f"✅ Пользователь {uid} теперь Босс на 30 дней (безлимит + планировщик).")
    except:
        await message.answer("❌ Ошибка ID.")
    await state.clear()


# --- Channel Management Handlers ---
@router.callback_query(F.data == "adm_add_chan")
async def add_chan_start(callback: CallbackQuery, state: FSMContext):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await callback.answer("Нет доступа.")

    await callback.message.answer(
        "🔗 Введите ссылку-приглашение (t.me/+AbCdEf...) или @username канала.\n\n⚠️ Бот должен быть там администратором.")
    await state.set_state(AdminFlow.waiting_for_channel_link)
    await callback.answer()


@router.message(AdminFlow.waiting_for_channel_link)
async def process_add_chan(message: Message, state: FSMContext):
    user_db = await db.get_user(message.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await message.answer("Нет доступа.")

    input_text = message.text.strip()

    match = re.search(r'(?:t\.me\/|\/joinchat\/)([\w\-\+]+)', input_text)
    lookup_identifier = match.group(1) if match else input_text

    await message.answer("Проверяю канал...")

    try:
        chat = await bot.get_chat(lookup_identifier)
        channel_id = chat.id
        title = chat.title or "Канал без названия"

        if chat.type not in ['channel', 'supergroup']:
            await message.answer("❌ Идентификатор не соответствует каналу или супергруппе.")
            await state.clear()
            return

        try:
            final_invite_link = await bot.export_chat_invite_link(channel_id)
        except Exception:
            final_invite_link = input_text
            await message.answer("⚠️ Бот не имеет прав администратора для создания ссылки. Использую вашу ссылку.")

        await db.add_channel(channel_id, title, final_invite_link)
        await message.answer(f"✅ Канал {title} добавлен в список обязательных подписок.")

    except TelegramBadRequest:
        await message.answer(
            f"❌ Канал не найден. Убедитесь, что вы отправили правильную ссылку или @username, и что бот добавлен в канал.")
    except Exception as e:
        await message.answer(f"❌ Произошла ошибка: {e}")

    await state.clear()


@router.callback_query(F.data == "adm_del_chan")
async def del_chan_list(callback: CallbackQuery):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await callback.answer("Нет доступа.")

    chans = await db.get_channels()
    if not chans:
        return await callback.message.answer("Список каналов пуст.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"❌ {c['title']}", callback_data=f"delch_{c['channel_id']}")] for c in chans
    ])
    await callback.message.answer("Нажмите, чтобы удалить:", reply_markup=kb)


@router.callback_query(F.data.startswith("delch_"))
async def process_del_chan(callback: CallbackQuery):
    user_db = await db.get_user(callback.from_user.id)
    if not user_db or not user_db.get('is_admin'): return await callback.answer("Нет доступа.")

    cid = int(callback.data.split("_")[1])
    await db.delete_channel(cid)
    await callback.answer("Удалено!")
    await callback.message.delete()


# --- Background Scheduler ---
async def scheduler_task(sleep_time=10):
    """Фоновая задача для отправки запланированных сообщений."""
    while True:
        await asyncio.sleep(sleep_time)
        try:
            messages_to_send = await db.get_messages_for_sending()

            for msg in messages_to_send:
                recipient_id = msg['to_user_id']

                success = await send_message_to_recipient(msg, recipient_id)

                if success:
                    logging.info(f"Scheduled message {msg['msg_id']} sent to {recipient_id}.")
                else:
                    logging.warning(f"Failed to send scheduled message {msg['msg_id']} to {recipient_id}.")

        except Exception as e:
            logging.error(f"Scheduler error: {e}")


# --- Main Run ---
async def main():
    await db.create_tables()

    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="send", description="Отправить сообщение"),
        BotCommand(command="send_time", description="Запланировать отправку (Boss/Admin only)"),
        BotCommand(command="limit", description="Проверить лимит сообщений"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="reveal", description="Раскрыть по ID (Admin only)"),
        BotCommand(command="help", description="Помощь / FAQ"),
    ])

    asyncio.create_task(scheduler_task())

    logging.info("Бот запущен!")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped")