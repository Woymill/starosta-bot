"""Телеграм-бот для старосты класса.

Запуск:
    export BOT_TOKEN="токен от @BotFather"
    export ADMIN_IDS="123456789"             # id АДМИНА бота (только ты). Старост назначай командой /addstarosta
    export TZ_NAME="Europe/Moscow"           # часовой пояс (необязательно)
    export REMIND_HOUR="19"                  # вечерние напоминания (необязательно)
    export DB_PATH="/data/class.db"          # путь к базе в ПОСТОЯННОМ хранилище (важно!)
    export BACKUP_HOUR="22"                  # резервная копия базы старосте (необязательно)
    export SHIFT="1"                         # смена: 1 или 2 (необязательно)
    python bot.py
"""
import asyncio
import html
import logging
import os
import re
import sqlite3
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
REMIND_HOUR = int(os.getenv("REMIND_HOUR", "19"))
BACKUP_HOUR = int(os.getenv("BACKUP_HOUR", "22"))
SHIFT = os.getenv("SHIFT", "1")  # 1 — первая смена (1, 4–11 классы), 2 — вторая (2–3 классы)

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# время начала уроков: 0 = пн ... 4 = пт
_TUE = ["8:00", "8:50", "9:45", "10:40", "11:35", "12:30", "13:20", "14:10"]
BELLS = {
    "1": {
        0: ["8:00", "8:40", "9:35", "10:30", "11:25", "12:20", "13:10", "14:00"],
        1: _TUE,
        2: _TUE,
        3: _TUE[:7],  # в четверг 7-й урок последний (РМГ)
        4: _TUE,
    },
    "2": {
        0: ["13:20", "14:00", "14:55", "15:45", "16:35"],
        1: ["13:20", "14:10", "15:05", "15:55", "16:45"],
        2: ["13:20", "14:10", "15:05", "15:55", "16:45"],
        3: ["13:20", "14:10", "15:05", "15:55", "16:45"],
        4: ["13:20", "14:10", "15:05", "15:55", "16:45"],
    },
}

# ---------------------------------------------------------------- оформление
LINE = "━━━━━━━━━━━━━━"
NUMS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def hdr(icon: str, title: str, sub: str = "") -> str:
    """Заголовок карточки: 🗓 <b>Понедельник</b> · 12.10 + линия."""
    return f"{icon} <b>{title}</b>" + (f" · {sub}" if sub else "") + f"\n{LINE}"


def num_emoji(n: int) -> str:
    return NUMS[n - 1] if 1 <= n <= len(NUMS) else f"{n}."


# ---------------------------------------------------------------- база данных
def _default_db_path() -> str:
    """Путь к базе. Раньше был относительный 'class.db' — он зависел от папки запуска
    и терялся при перезапуске/деплое. Теперь: DB_PATH из окружения -> /data (если есть
    постоянный диск) -> папка с bot.py (всегда один и тот же файл)."""
    env = os.getenv("DB_PATH")
    if env:
        return os.path.abspath(env)
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/class.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "class.db")


DB_PATH = _default_db_path()
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA synchronous=FULL")
logging.basicConfig(level=logging.INFO)
logging.info("База данных: %s", DB_PATH)
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, status TEXT DEFAULT 'approved', role TEXT DEFAULT 'student');
    CREATE TABLE IF NOT EXISTS homework(id INTEGER PRIMARY KEY AUTOINCREMENT, subject TEXT, task TEXT, due TEXT);
    CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT, text TEXT, kind TEXT DEFAULT 'event');
    CREATE TABLE IF NOT EXISTS schedule(day INTEGER PRIMARY KEY, lessons TEXT);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS changes(date TEXT, num INTEGER, subject TEXT, PRIMARY KEY(date, num));
    CREATE TABLE IF NOT EXISTS announcements(id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, file_id TEXT, text TEXT, by_role TEXT DEFAULT 'admin');
    CREATE TABLE IF NOT EXISTS reads(ann_id INTEGER, user_id INTEGER, PRIMARY KEY(ann_id, user_id));
    CREATE TABLE IF NOT EXISTS polls(id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT, options TEXT, closed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS votes(poll_id INTEGER, user_id INTEGER, opt INTEGER, name TEXT, PRIMARY KEY(poll_id, user_id));
    """
)
for _ddl in (  # для старых баз данных: новые колонки
    "ALTER TABLE events ADD COLUMN kind TEXT DEFAULT 'event'",
    "ALTER TABLE users ADD COLUMN status TEXT DEFAULT 'approved'",
    "ALTER TABLE users ADD COLUMN role TEXT DEFAULT 'student'",
    "ALTER TABLE announcements ADD COLUMN by_role TEXT DEFAULT 'admin'",
):
    try:
        db.execute(_ddl)
        db.commit()
    except sqlite3.OperationalError:
        pass

router = Router()

# В общих чатах бот только присылает оповещения и ни на какие команды не отвечает.
# Исключение: /setchat и /unsetchat (подключение чата админом).
GROUP_ALLOWED_CMDS = {"setchat", "unsetchat"}


async def group_filter(handler, event: Message, data):
    if event.chat.type != "private":
        t = event.text or event.caption or ""
        if t.startswith("/"):
            cmd = t.split()[0][1:].split("@")[0].lower()
            if cmd not in GROUP_ALLOWED_CMDS:
                return None
    return await handler(event, data)


router.message.outer_middleware(group_filter)


class Reg(StatesGroup):
    name = State()


class HW(StatesGroup):
    subject = State()
    task = State()
    due = State()


class TestW(StatesGroup):
    subject = State()
    due = State()
    topic = State()


class Ann(StatesGroup):
    content = State()
    confirm = State()


class PollW(StatesGroup):
    question = State()
    options = State()


class Repl(StatesGroup):
    day = State()
    num = State()
    subject = State()


class Week(StatesGroup):
    text = State()


class Reject(StatesGroup):
    reason = State()


# ---------------------------------------------------------------- утилиты
def esc(s: str) -> str:
    return html.escape(s or "")


def is_admin(m: Message) -> bool:
    return m.from_user.id in ADMINS


def today() -> date:
    return datetime.now(TZ).date()


def fmt(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%d.%m")


def norm(text: str) -> str:
    """Нижний регистр, без эмодзи и знаков: '🗓 Расписание!' -> 'расписание'."""
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch.isspace()).strip()


def parse_day(text: str) -> int | None:
    """Понимает: пн, понедельник, в понедельник, пятницу, среду и т.п."""
    t = text.strip().lower()
    if t.startswith("в "):
        t = t[2:].strip()
    for i, full in enumerate(DAYS_FULL):
        full = full.lower()
        if t == DAYS[i]:
            return i
        if len(t) >= 2 and (full.startswith(t) or t.startswith(full[:-1])):
            return i
    return None


def next_date(wd: int) -> date:
    """Ближайшая дата (сегодня или позже) с нужным днём недели."""
    return today() + timedelta(days=(wd - today().weekday()) % 7)


def parse_date(text: str) -> date | None:
    """ДД.ММ, ДД.ММ.ГГГГ, сегодня, завтра, послезавтра или день недели."""
    text = text.strip()
    for f in ("%d.%m.%Y", "%d.%m"):
        try:
            d = datetime.strptime(text, f).date()
        except ValueError:
            continue
        if f == "%d.%m":
            d = d.replace(year=today().year)
            if d < today() - timedelta(days=30):
                d = d.replace(year=d.year + 1)
        return d
    low = text.lower()
    if low.startswith("сегодня"):
        return today()
    if low.startswith("послезавтра"):
        return today() + timedelta(days=2)
    if low.startswith("завтра"):
        return today() + timedelta(days=1)
    wd = parse_day(low)
    return next_date(wd) if wd is not None else None


def split_args(command: CommandObject, parts: int) -> list[str] | None:
    """Делит аргументы по « | ». Возвращает None, если частей меньше нужного."""
    if not command.args:
        return None
    items = [p.strip() for p in command.args.split("|", parts - 1)]
    return items if len(items) == parts and all(items) else None


def ik(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    """Инлайн-кнопки: [[("Текст", "данные"), ...], ...]."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows]
    )


def get_setting(key: str) -> str | None:
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value))
    db.commit()


def del_setting(key: str):
    db.execute("DELETE FROM settings WHERE key=?", (key,))
    db.commit()


STUDENT_ROWS = [
    ["🗓 Расписание", "📌 События"],
]
ADMIN_ROWS = [
    ["➕ Добавить ДЗ", "📝 Контрольная"],
    ["📢 Объявление", "📊 Опрос"],
    ["🔄 Замена", "🗓 Неделя"],
    ["👥 Заявки"],
]
OWNER_ROWS = [["⚙️ Админ"]]
MENU_TEXTS = {norm(t) for row in STUDENT_ROWS + ADMIN_ROWS + OWNER_ROWS for t in row}


def _kb(rows: list[list[str]]) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows], resize_keyboard=True
    )


HELPER_ROWS = [["➕ Добавить ДЗ", "📝 Контрольная"], ["📢 Объявление"]]
KB = _kb(STUDENT_ROWS)
KB_HELPER = _kb(STUDENT_ROWS + HELPER_ROWS)
KB_STAROSTA = _kb(STUDENT_ROWS + ADMIN_ROWS)
KB_OWNER = _kb(STUDENT_ROWS + ADMIN_ROWS + OWNER_ROWS)

ROLE_TITLES = {"owner": "👑 Админ", "starosta": "⭐ Староста", "helper": "🛠 Помощник", "student": "🎓 Ученик"}
ROLE_ICON = {"starosta": "⭐", "helper": "🛠"}


def role_of(uid: int) -> str:
    """owner (из ADMIN_IDS) / starosta / helper / student."""
    if uid in ADMINS:
        return "owner"
    row = db.execute("SELECT role, status FROM users WHERE id=?", (uid,)).fetchone()
    if row and row["status"] == "approved" and row["role"] in ("starosta", "helper"):
        return row["role"]
    return "student"


def role_title(uid: int) -> str:
    return ROLE_TITLES[role_of(uid)]


def is_owner(uid: int) -> bool:
    return uid in ADMINS


def is_helper(uid: int) -> bool:
    return role_of(uid) == "helper"


def can_manage(uid: int) -> bool:
    """Админ или староста: всё, что связано с классом."""
    return role_of(uid) in ("owner", "starosta")


def can_edit(uid: int) -> bool:
    """Админ, староста или помощник: ДЗ, контрольные, объявления."""
    return role_of(uid) in ("owner", "starosta", "helper")


def kb_for(uid: int) -> ReplyKeyboardMarkup:
    return {"owner": KB_OWNER, "starosta": KB_STAROSTA, "helper": KB_HELPER}.get(role_of(uid), KB)


def kbm(m: Message):
    """Кнопки меню показываем только в личке, в общих чатах их не должно быть."""
    return kb_for(m.from_user.id) if m.chat.type == "private" else None


def manager_ids() -> list[int]:
    ids = set(ADMINS)
    ids.update(r["id"] for r in db.execute("SELECT id FROM users WHERE role='starosta' AND status='approved'"))
    return list(ids)


async def manager_only(m: Message) -> bool:
    """Только админ и староста."""
    if not can_manage(m.from_user.id):
        await m.answer("🔒 Эта команда только для старосты.")
        return False
    return True


async def owner_only(m: Message) -> bool:
    """Только админ бота."""
    if not is_owner(m.from_user.id):
        await m.answer("🔒 Эта команда только для администратора бота.")
        return False
    return True


async def staff_only(m: Message) -> bool:
    if not can_edit(m.from_user.id):
        await m.answer("🔒 Эта команда только для старосты и помощников.")
        return False
    return True


async def ensure_member(m: Message) -> bool:
    """Пускает только подтверждённых учеников (в группах — без проверки)."""
    if m.chat.type != "private" or m.from_user.id in ADMINS:
        return True
    row = db.execute("SELECT status FROM users WHERE id=?", (m.from_user.id,)).fetchone()
    if row and row["status"] == "approved":
        return True
    if row:
        await m.answer("⏳ Староста ещё не подтвердил твою заявку. Подожди немного.")
    else:
        await m.answer("Сначала нажми /start и напиши свою фамилию и имя 🙂")
    return False


async def interrupted(m: Message, state: FSMContext) -> bool:
    """Если посреди вопросов нажали кнопку меню или написали «отмена» — выходим."""
    t = norm(m.text or "")
    if t in ("отмена", "cancel") or t in MENU_TEXTS:
        await state.clear()
        await m.answer("Отменено. Выбери действие в меню 👇", reply_markup=kbm(m))
        return True
    return False


def member_ids() -> list[int]:
    return [r["id"] for r in db.execute("SELECT id FROM users WHERE status='approved'").fetchall()]


def chat_id() -> int | None:
    v = get_setting("chat_id")
    return int(v) if v else None


# ---------------------------------------------------------------- тексты: ДЗ, расписание, события
def hw_text(show_ids: bool = False) -> str:
    rows = db.execute(
        "SELECT * FROM homework WHERE due>=? ORDER BY due, subject", (today().isoformat(),)
    ).fetchall()
    title = hdr("📚", "Домашнее задание")
    if not rows:
        return f"{title}\n\n🎉 Домашки нет — отдыхаем!"
    lines, cur = [title], None
    for r in rows:
        if r["due"] != cur:
            cur = r["due"]
            d = date.fromisoformat(cur)
            lines.append(f"\n📅 <b>{DAYS_FULL[d.weekday()]}, {d.strftime('%d.%m')}</b>")
        tag = f" <i>#{r['id']}</i>" if show_ids else ""
        lines.append(f"▫️ <b>{esc(r['subject'])}</b> — {esc(r['task'])}{tag}")
    return "\n".join(lines)


def schedule_text(d: date, with_title: bool = True) -> str:
    """Расписание на дату: № урока — предмет — начало — ДЗ по этому уроку (+ замены)."""
    wd = d.weekday()
    title = hdr("🗓", DAYS_FULL[wd], d.strftime("%d.%m"))
    row = db.execute("SELECT lessons FROM schedule WHERE day=?", (wd,)).fetchone()
    if not row:
        out = (title + "\n\n" if with_title else "") + "😴 Расписание не задано."
        rest0 = db.execute("SELECT * FROM homework WHERE due=?", (d.isoformat(),)).fetchall()
        if rest0:
            out += "\n\n📚 <b>ДЗ на этот день</b>\n" + "\n".join(
                f"▫️ <b>{esc(h['subject'])}</b>\n     <code>ДЗ: {esc(h['task'])}</code>" for h in rest0
            )
        return out
    lessons = [x.strip() for x in row["lessons"].split(",") if x.strip()]
    changes = {
        r["num"]: r["subject"]
        for r in db.execute("SELECT num, subject FROM changes WHERE date=?", (d.isoformat(),)).fetchall()
    }
    while len(lessons) < max(changes, default=0):
        lessons.append("—")
    starts = BELLS.get(SHIFT, BELLS["1"]).get(wd, [])
    hws = db.execute("SELECT * FROM homework WHERE due=?", (d.isoformat(),)).fetchall()
    used: set[int] = set()
    lines = [title, ""] if with_title else []
    for i, name in enumerate(lessons):
        num = i + 1
        when = f" — {starts[i]}" if i < len(starts) else ""
        if changes.get(num) == "-":
            lines.append(f"{num}. <s>{esc(name)}</s> ❌ <i>отменён</i>{when}")
            continue
        shown = changes.get(num, name)
        extra = f" 🔄 <i>(было: {esc(name)})</i>" if num in changes else ""
        lines.append(f"{num}. <b>{esc(shown)}</b>{extra}{when}")
        a = shown.casefold()
        for h in hws:
            b = h["subject"].casefold().strip()
            if h["id"] not in used and b and (a == b or a in b or b in a):
                used.add(h["id"])
                lines.append(f"     <code>ДЗ: {esc(h['task'])}</code>")
                break
    rest = [h for h in hws if h["id"] not in used]
    if rest:
        lines.append("\n📚 <b>Ещё ДЗ на этот день</b>")
        lines += [f"▫️ <b>{esc(h['subject'])}</b>\n     <code>ДЗ: {esc(h['task'])}</code>" for h in rest]
    return "\n".join(lines)


def ev_label(r) -> str:
    return f"📝 <b>Контрольная:</b> {esc(r['text'])}" if r["kind"] == "test" else esc(r["text"])


def events_text(show_ids: bool = False) -> str:
    rows = db.execute(
        "SELECT * FROM events WHERE day>=? ORDER BY day", (today().isoformat(),)
    ).fetchall()
    title = hdr("📌", "События")
    if not rows:
        return f"{title}\n\n🌤 Ближайших событий нет."
    lines = [title, ""]
    for r in rows:
        d = date.fromisoformat(r["day"])
        tag = f" <i>#{r['id']}</i>" if show_ids else ""
        lines.append(f"▫️ <code>{d.strftime('%d.%m')}</code> {DAYS[d.weekday()]} — {ev_label(r)}{tag}")
    return "\n".join(lines)


def tomorrow_text(include_tests: bool = True) -> str:
    t = today() + timedelta(days=1)
    parts = ["🗓 <b>Уроки</b>\n" + schedule_text(t, with_title=False)]
    ev = db.execute("SELECT * FROM events WHERE day=?", (t.isoformat(),)).fetchall()
    if not include_tests:  # контрольные в рассылке идут отдельным напоминанием
        ev = [r for r in ev if r["kind"] != "test"]
    if ev:
        parts.append("📌 <b>События</b>\n" + "\n".join(f"▫️ {ev_label(r)}" for r in ev))
    title = hdr("🌙", "Завтра", f"{DAYS_FULL[t.weekday()]}, {t.strftime('%d.%m')}")
    return title + "\n\n" + "\n\n".join(parts)


# ---------------------------------------------------------------- регистрация (с подтверждением старосты)
@router.message(CommandStart())
async def start(m: Message, state: FSMContext, bot: Bot):
    if m.chat.type != "private":
        return
    uid = m.from_user.id
    row = db.execute("SELECT name, status FROM users WHERE id=?", (uid,)).fetchone()
    if row and row["status"] == "approved":
        if role_of(uid) != "student":
            await apply_commands(bot, uid)
        await m.answer(
            f"👋 <b>С возвращением, {esc(row['name'])}!</b>\n{role_title(uid)}\n\n"
            "Пользуйся кнопками внизу 👇 или напиши «помощь».",
            reply_markup=kb_for(uid),
        )
        return
    if row:
        await m.answer("⏳ <b>Заявка на рассмотрении</b>\nКак только староста подтвердит — я сразу напишу.")
        return
    await m.answer(
        "👋 <b>Привет! Я бот нашего класса.</b>\n\n"
        "📚 домашка · 🗓 расписание · 🔄 замены\n📝 контрольные · 📢 объявления\n\n"
        "✏️ Напиши свою <b>фамилию и имя</b>, и я тебя запомню."
    )
    await state.set_state(Reg.name)


@router.message(Command("cancel", "отмена", ignore_case=True))
async def cancel_cmd(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Отменено.", reply_markup=kbm(m))


@router.message(Command("id"))
async def my_id(m: Message):
    await m.answer(f"Твой Telegram id: <code>{m.from_user.id}</code>")


@router.message(Reg.name)
async def reg_name(m: Message, state: FSMContext, bot: Bot):
    name = (m.text or "").strip()[:60]
    if len(name) < 2:
        await m.answer("✏️ Напиши фамилию и имя текстом.")
        return
    uid = m.from_user.id
    auto = uid in ADMINS or not ADMINS
    db.execute(
        "INSERT OR REPLACE INTO users(id, name, status) VALUES(?,?,?)", (uid, name, "approved" if auto else "pending")
    )
    db.commit()
    await state.clear()
    if auto:
        await apply_commands(bot, uid)
        await m.answer(
            f"🎉 <b>Готово, {esc(name)}!</b>\n{role_title(uid)}\n\n"
            "Теперь ты получаешь расписание, замены и объявления.\nКнопки внизу 👇 — всё нужное под рукой.",
            reply_markup=kb_for(uid),
        )
        return
    await m.answer("📨 <b>Заявка отправлена старосте</b>\nКак только он подтвердит — я напишу 👍")
    uname = f"@{m.from_user.username}" if m.from_user.username else "без username"
    kb = ik([[("✅ Принять", f"reg:ok:{uid}"), ("❌ Отклонить", f"reg:no:{uid}")]])
    card = hdr("🆕", "Новая заявка") + f"\n\n👤 <b>{esc(name)}</b>\n🔗 {esc(uname)}\n🆔 <code>{uid}</code>"
    for a in manager_ids():
        try:
            await bot.send_message(a, card, reply_markup=kb)
        except Exception as e:
            logging.warning("Не удалось уведомить %s: %s", a, e)


@router.callback_query(F.data.startswith("reg:"))
async def reg_decision(cb: CallbackQuery, bot: Bot, state: FSMContext):
    if not can_manage(cb.from_user.id):
        await cb.answer("Только для старосты", show_alert=True)
        return
    _, action, uid_s = cb.data.split(":")
    uid = int(uid_s)
    row = db.execute("SELECT name, status FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row["status"] != "pending":
        await cb.answer("Эта заявка уже обработана")
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return
    if action == "ok":
        db.execute("UPDATE users SET status='approved' WHERE id=?", (uid,))
        db.commit()
        try:
            await bot.send_message(
                uid,
                "✅ <b>Староста подтвердил заявку!</b>\nТеперь ты получаешь расписание, ДЗ и объявления.\nКнопки внизу 👇",
                reply_markup=kb_for(uid),
            )
        except Exception as e:
            logging.warning("Не удалось уведомить %s: %s", uid, e)
        try:
            await cb.message.edit_text(f"✅ Принят: {esc(row['name'])}")
        except Exception:
            pass
        await cb.answer()
        await audit(cb.from_user, f"✅ <b>Принял заявку:</b> {esc(row['name'])}")
        return
    # отказ: спрашиваем причину, которую передадим ученику
    await state.clear()
    await state.set_state(Reject.reason)
    await state.update_data(uid=uid, chat=cb.message.chat.id, msg=cb.message.message_id)
    await cb.answer()
    await cb.message.answer(
        f"Напиши причину отказа для «{esc(row['name'])}» — я передам её ученику.\n"
        "Если причина не нужна — напиши «-». «отмена» — не отклонять."
    )


@router.message(Reject.reason)
async def reject_reason(m: Message, state: FSMContext, bot: Bot):
    if await interrupted(m, state) or not m.text:
        return
    data = await state.get_data()
    await state.clear()
    uid = data["uid"]
    row = db.execute("SELECT name, status FROM users WHERE id=?", (uid,)).fetchone()
    if not row or row["status"] != "pending":
        await m.answer("Эта заявка уже обработана.", reply_markup=kbm(m))
        return
    reason = "" if m.text.strip() in ("-", "—") else m.text.strip()[:300]
    db.execute("DELETE FROM users WHERE id=?", (uid,))
    db.commit()
    note = (
        "❌ <b>Староста не подтвердил заявку.</b>"
        + (f"\n\n💬 <b>Причина:</b> {esc(reason)}" if reason else "")
        + "\n\nЕсли это ошибка — нажми /start и напиши настоящие фамилию и имя."
    )
    try:
        await bot.send_message(uid, note)
    except Exception as e:
        logging.warning("Не удалось уведомить %s: %s", uid, e)
    try:
        await bot.edit_message_text(
            f"❌ Отклонён: {esc(row['name'])}" + (f"\nПричина: {esc(reason)}" if reason else ""),
            chat_id=data["chat"],
            message_id=data["msg"],
        )
    except Exception:
        pass
    await m.answer(
        "Отклонено, причина отправлена ученику." if reason else "Отклонено.", reply_markup=kbm(m)
    )
    await audit(m.from_user, f"❌ <b>Отклонил заявку:</b> {esc(row['name'])}" + (f"\nПричина: {clip(reason)}" if reason else ""))


def help_text(uid: int) -> str:
    role = role_of(uid)
    parts = [hdr("❓", "Справка", ROLE_TITLES[role])]
    parts.append(
        "<b>🎓 Для всех</b>\n"
        "▫️ <code>расписание</code> · <code>расписание завтра</code> · <code>расписание пт</code> — "
        "уроки и домашка по ним\n"
        "▫️ <code>события</code> — контрольные и мероприятия\n"
        "▫️ /me — мой профиль\n"
        "<i>Расписание и события есть на кнопках внизу. Остальное можно просто написать словами.</i>"
    )
    if role == "helper":
        parts.append(
            "<b>🛠 Помощник</b> — кнопки: ➕ ДЗ · 📝 Контрольная · 📢 Объявление\n"
            "▫️ /addhw Предмет | Задание | ДД.ММ\n"
            "▫️ /addtest Алгебра | 15.10 | Тема\n"
            "▫️ /announce Текст\n"
            "<i>Бот задаёт вопросы по шагам, «отмена» — выйти.</i>"
        )
    if role in ("starosta", "owner"):
        parts.append(
            "<b>⭐ Староста</b> — кнопки: ➕ ДЗ · 📝 Контрольная · 📢 Объявление · 📊 Опрос · 🔄 Замена · 🗓 Неделя · 👥 Заявки\n"
            "<i>Бот задаёт вопросы по шагам, «отмена» — выйти.</i>"
        )
        parts.append(
            "<b>📚 Учёба</b>\n"
            "▫️ /addhw Предмет | Задание | ДД.ММ · /hw — список с номерами · /delhw id\n"
            "▫️ /addtest Алгебра | 15.10 | Тема\n"
            "▫️ /addevent ДД.ММ | Описание · /delevent id\n"
            "▫️ /setweek (по строке на день: «Пн: РОВ, Математика»)\n"
            "▫️ /setschedule Пн | Математика, Русский\n"
            "▫️ /replace Пт | 3 | Физика («-» вместо предмета = урок отменён)\n"
            "▫️ /clearreplace Пт — убрать замены на день"
        )
        parts.append(
            "<b>📢 Связь</b>\n"
            "▫️ /announce Текст · /unread — кто не прочитал\n"
            "▫️ /pollresult · /pollclose"
        )
        parts.append(
            "<b>👥 Люди</b>\n"
            "▫️ /users · /deluser Иванов\n"
            "▫️ /addhelper Иванов · /delhelper Иванов · /helpers"
        )
    if role == "owner":
        parts.append(
            "<b>👑 Админ бота</b> — кнопка ⚙️ Админ\n"
            "▫️ /addstarosta Иванов · /delstarosta Иванов · /starostas\n"
            "▫️ /setchat (в общем чате класса) · /unsetchat\n"
            "▫️ /backup — копия базы"
        )
    return "\n\n".join(parts)


@router.message(Command("me"))
async def me(m: Message):
    uid = m.from_user.id
    row = db.execute("SELECT name, status FROM users WHERE id=?", (uid,)).fetchone()
    name = row["name"] if row else m.from_user.full_name
    status = ""
    if row and row["status"] != "approved":
        status = "\n⏳ Статус: ждёт подтверждения"
    await m.answer(
        hdr("👤", "Профиль") + f"\n\n<b>{esc(name)}</b>\n🏷 {role_title(uid)}{status}\n🆔 <code>{uid}</code>"
    )


@router.message(Command("help", "помощь", ignore_case=True))
async def help_cmd(m: Message):
    if not await ensure_member(m):
        return
    await m.answer(help_text(m.from_user.id), reply_markup=kbm(m))


# ---------------------------------------------------------------- команды для учеников
@router.message(Command("hw", "дз", "домашка", ignore_case=True))
async def hw(m: Message):
    if not await ensure_member(m):
        return
    if can_manage(m.from_user.id):  # староста: список ДЗ с номерами для /delhw
        await m.answer(hw_text(True))
    else:
        await m.answer("📚 Домашка теперь в расписании: нажми «🗓 Расписание» или напиши «расписание завтра».")


async def send_schedule(m: Message, arg: str):
    arg = (arg or "сегодня").strip().lower()
    if arg.startswith("сег"):
        d = today()
    elif arg.startswith("послезав"):
        d = today() + timedelta(days=2)
    elif arg.startswith("зав"):
        d = today() + timedelta(days=1)
    else:
        wd = parse_day(arg)
        d = next_date(wd) if wd is not None else None
    if d is None:
        await m.answer("Напиши: расписание сегодня | завтра | понедельник … воскресенье")
        return
    await m.answer(schedule_text(d))


@router.message(Command("schedule", "расписание", ignore_case=True))
async def schedule(m: Message, command: CommandObject):
    if await ensure_member(m):
        await send_schedule(m, command.args or "")


@router.message(Command("events", "события", ignore_case=True))
async def events(m: Message):
    if await ensure_member(m):
        await m.answer(events_text(can_manage(m.from_user.id)))


# ---------------------------------------------------------------- меню команд и профиль бота
BASE_CMDS = [
    ("schedule", "Расписание и ДЗ: сегодня, завтра, день недели"),
    ("events", "События и контрольные"), ("me", "Мой профиль"), ("help", "Помощь"),
]
HELPER_CMDS = [("addhw", "Добавить ДЗ"), ("addtest", "Добавить контрольную"), ("announce", "Отправить объявление")]
MANAGER_CMDS = [
    ("hw", "Список ДЗ с номерами"), ("setweek", "Расписание на неделю"), ("replace", "Замена урока"), ("unread", "Кто не прочитал объявление"),
    ("pollresult", "Итоги опроса"), ("users", "Список учеников"), ("helpers", "Помощники"),
]
OWNER_CMDS = [("backup", "Копия базы данных"), ("setchat", "Подключить общий чат"), ("starostas", "Старосты")]


def commands_for(uid: int) -> list[tuple[str, str]]:
    cmds = list(BASE_CMDS)
    if can_edit(uid):
        cmds += HELPER_CMDS
    if can_manage(uid):
        cmds += MANAGER_CMDS
    if is_owner(uid):
        cmds += OWNER_CMDS
    return cmds


async def apply_commands(bot: Bot, uid: int):
    """Личное меню команд по роли (кнопка «Меню» рядом с полем ввода)."""
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in commands_for(uid)],
            scope=BotCommandScopeChat(chat_id=uid),
        )
    except Exception as e:
        logging.info("Меню команд для %s не обновлено: %s", uid, e)


async def setup_profile(bot: Bot):
    """Описание бота, краткое описание и общее меню команд (для учеников)."""
    try:
        await bot.set_my_description(
            "Бот нашего класса 📚\n\nРасписание, домашка, замены, контрольные и объявления "
            "старосты — всё в одном месте.\n\nНажми «Начать» и напиши фамилию и имя."
        )
        await bot.set_my_short_description("Расписание, ДЗ, контрольные и объявления класса 📚")
        await bot.delete_my_commands()  # убираем общее меню (оно показывалось и в группах)
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in BASE_CMDS], scope=BotCommandScopeAllPrivateChats()
        )
        await bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    except Exception as e:
        logging.warning("Не удалось настроить профиль бота: %s", e)
    ids = set(ADMINS) | {
        r["id"] for r in db.execute("SELECT id FROM users WHERE role IN ('starosta','helper')").fetchall()
    }
    for uid in ids:
        await apply_commands(bot, uid)


# ---------------------------------------------------------------- рассылка
async def broadcast(bot: Bot, text: str) -> int:
    sent = 0
    for uid in member_ids():
        try:
            await bot.send_message(uid, text)
            sent += 1
        except Exception as e:  # пользователь заблокировал бота и т.п.
            logging.warning("Не доставлено %s: %s", uid, e)
        await asyncio.sleep(0.05)  # лимиты Telegram
    return sent


async def send_to_chat(bot: Bot, text: str) -> bool:
    cid = chat_id()
    if not cid:
        return False
    try:
        await bot.send_message(cid, text)
        return True
    except Exception as e:
        logging.warning("Не удалось отправить в общий чат: %s", e)
        return False


async def notify_admins(bot: Bot, text: str):
    for a in manager_ids():
        try:
            await bot.send_message(a, text)
        except Exception as e:
            logging.warning("Не доставлено админу %s: %s", a, e)


BOT: Bot | None = None  # заполняется в main(), нужен для тихих логов админу


def clip(s: str, n: int = 700) -> str:
    s = (s or "").strip()
    return esc(s[:n] + ("…" if len(s) > n else ""))


async def audit(user, text: str):
    """Тихий лог админу: кто из старост/помощников что сделал. Действия самого админа не логируются."""
    if BOT is None or is_owner(user.id):
        return
    row = db.execute("SELECT name FROM users WHERE id=?", (user.id,)).fetchone()
    name = row["name"] if row else user.full_name
    uname = f" · @{user.username}" if user.username else ""
    body = f"🕵️ {text}\n\n━━━━━━━━━━━━━━\n👤 <b>{esc(name)}</b> · {role_title(user.id)}{esc(uname)}"
    for a in ADMINS:
        try:
            await BOT.send_message(a, body)
        except Exception as e:
            logging.warning("Не доставлен лог админу %s: %s", a, e)


def repl_desc(num: int, subject: str) -> str:
    return f"{num} урок — отменён" if subject == "-" else f"{num} урок → {esc(subject)}"


# ---------------------------------------------------------------- команды старосты: ДЗ, события, контрольные
@router.message(Command("addhw"))
async def addhw(m: Message, command: CommandObject):
    if not await staff_only(m):
        return
    args = split_args(command, 3)
    d = parse_date(args[2]) if args else None
    if not args or not d:
        await m.answer("Формат: /addhw Математика | №123–125 | 12.10 (или «завтра», «пт»)")
        return
    db.execute("INSERT INTO homework(subject,task,due) VALUES(?,?,?)", (args[0], args[1], d.isoformat()))
    db.commit()
    await m.answer(f"✅ Домашка добавлена на {d.strftime('%d.%m')}.")
    await audit(m.from_user, f"➕ <b>Добавил ДЗ</b> на {d.strftime('%d.%m')}\n{esc(args[0])}: {clip(args[1])}")


@router.message(Command("delhw"))
async def delhw(m: Message, command: CommandObject):
    if not await manager_only(m):
        return
    if not (command.args or "").strip().isdigit():
        await m.answer("Формат: /delhw 5 (номер из списка /hw)")
        return
    row = db.execute("SELECT subject, task FROM homework WHERE id=?", (int(command.args),)).fetchone()
    db.execute("DELETE FROM homework WHERE id=?", (int(command.args),))
    db.commit()
    await m.answer("🗑 Удалено.")
    if row:
        await audit(m.from_user, f"🗑 <b>Удалил ДЗ</b>\n{esc(row['subject'])}: {clip(row['task'])}")


@router.message(Command("addevent"))
async def addevent(m: Message, command: CommandObject):
    if not await manager_only(m):
        return
    args = split_args(command, 2)
    d = parse_date(args[0]) if args else None
    if not args or not d:
        await m.answer("Формат: /addevent 15.10 | Экскурсия в музей")
        return
    db.execute("INSERT INTO events(day,text) VALUES(?,?)", (d.isoformat(), args[1]))
    db.commit()
    await m.answer("✅ Событие добавлено.")
    await audit(m.from_user, f"📌 <b>Добавил событие</b> на {d.strftime('%d.%m')}\n{clip(args[1])}")
    if BOT is not None:  # новое событие сразу уходит в общий чат
        await send_to_chat(BOT, hdr("📌", "Новое событие", d.strftime("%d.%m")) + f"\n\n{esc(args[1])}")


@router.message(Command("delevent"))
async def delevent(m: Message, command: CommandObject):
    if not await manager_only(m):
        return
    if not (command.args or "").strip().isdigit():
        await m.answer("Формат: /delevent 3")
        return
    row = db.execute("SELECT day, text FROM events WHERE id=?", (int(command.args),)).fetchone()
    db.execute("DELETE FROM events WHERE id=?", (int(command.args),))
    db.commit()
    await m.answer("🗑 Удалено.")
    if row:
        await audit(m.from_user, f"🗑 <b>Удалил событие</b> ({fmt(row['day'])})\n{clip(row['text'])}")


def save_test(subject: str, d: date, topic: str):
    text = subject + (f" — {topic}" if topic else "")
    db.execute("INSERT INTO events(day,text,kind) VALUES(?,?,'test')", (d.isoformat(), text))
    db.commit()


@router.message(Command("addtest"))
async def addtest(m: Message, command: CommandObject):
    if not await staff_only(m):
        return
    parts = [x.strip() for x in (command.args or "").split("|")]
    d = parse_date(parts[1]) if len(parts) >= 2 else None
    if len(parts) < 2 or not parts[0] or not d:
        await m.answer("Формат: /addtest Алгебра | 15.10 | Квадратные уравнения (тема по желанию)")
        return
    save_test(parts[0], d, parts[2] if len(parts) > 2 else "")
    await m.answer(f"✅ Контрольная добавлена на {d.strftime('%d.%m')}. Напомню за 3 дня и за 1 день.")
    await audit(
        m.from_user,
        f"📝 <b>Добавил контрольную</b> на {d.strftime('%d.%m')}\n{esc(parts[0])}" + (f" — {clip(parts[2])}" if len(parts) > 2 and parts[2] else ""),
    )


# ---------------------------------------------------------------- расписание: день, неделя, замены
@router.message(Command("setschedule"))
async def setschedule(m: Message, command: CommandObject):
    if not await manager_only(m):
        return
    args = split_args(command, 2)
    wd = parse_day(args[0]) if args else None
    if wd is None:
        await m.answer("Формат: /setschedule Пн | Математика, Русский, Физра")
        return
    db.execute("INSERT OR REPLACE INTO schedule VALUES(?,?)", (wd, args[1]))
    db.commit()
    await m.answer("✅ Расписание обновлено.\n\n" + schedule_text(next_date(wd)))
    await audit(m.from_user, f"🗓 <b>Изменил расписание</b> ({DAYS_FULL[wd]})\n{clip(args[1])}")


def apply_week(text: str) -> tuple[list[str], list[str]]:
    """Строки вида «Пн: РОВ, Математика»; «Сб: -» очищает день."""
    done, bad = [], []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        mt = re.match(r"^([^:|]+)[:|]\s*(.*)$", line)
        wd = parse_day(mt.group(1)) if mt else None
        if wd is None:
            bad.append(line)
            continue
        val = mt.group(2).strip()
        if val in ("", "-", "—"):
            db.execute("DELETE FROM schedule WHERE day=?", (wd,))
            done.append(f"{DAYS_FULL[wd]}: очищено")
        else:
            lessons = [x.strip() for x in val.split(",") if x.strip()]
            db.execute("INSERT OR REPLACE INTO schedule VALUES(?,?)", (wd, ", ".join(lessons)))
            done.append(f"{DAYS_FULL[wd]}: {len(lessons)} ур.")
    db.commit()
    return done, bad


WEEK_HINT = (
    "Пришли расписание одним сообщением, по строке на день:\n\n"
    "Пн: РОВ, Математика, Русский\nВт: Физика, Алгебра, Английский\n\n"
    "Предметы через запятую, номер урока = порядок. Чтобы очистить день: «Сб: -»."
)


def week_reply(text: str) -> str:
    done, bad = apply_week(text)
    out = ""
    if done:
        out += "✅ Сохранено:\n" + "\n".join(done)
    if bad:
        out += ("\n\n" if out else "") + "⚠️ Не понял эти строки:\n" + "\n".join(esc(b) for b in bad)
    return out or "Ничего не распознано. " + WEEK_HINT


@router.message(Command("setweek"))
async def setweek(m: Message, command: CommandObject, state: FSMContext):
    if not await manager_only(m):
        return
    if not command.args:
        await state.set_state(Week.text)
        await m.answer(WEEK_HINT)
        return
    await m.answer(week_reply(command.args))
    await audit(m.from_user, f"🗓 <b>Изменил расписание недели</b>\n{clip(command.args)}")


def apply_replacement(d: date, num: int, subject: str) -> str | None:
    row = db.execute("SELECT lessons FROM schedule WHERE day=?", (d.weekday(),)).fetchone()
    n = len([x for x in row["lessons"].split(",") if x.strip()]) if row else 0
    if num < 1 or num > n + 1:
        return f"В этот день уроков: {n}. Укажи номер от 1 до {n + 1}."
    db.execute("INSERT OR REPLACE INTO changes VALUES(?,?,?)", (d.isoformat(), num, subject))
    db.commit()
    return None


async def announce_replacement(bot: Bot, d: date, num: int, subject: str) -> tuple[int, bool]:
    row = db.execute("SELECT lessons FROM schedule WHERE day=?", (d.weekday(),)).fetchone()
    lessons = [x.strip() for x in row["lessons"].split(",") if x.strip()] if row else []
    old = lessons[num - 1] if num <= len(lessons) else "—"
    title = hdr("🔄", "Изменение в расписании", f"{DAYS_FULL[d.weekday()]}, {d.strftime('%d.%m')}")
    body = (
        f"{num_emoji(num)} <s>{esc(old)}</s> ❌ <b>урок отменён</b>"
        if subject == "-"
        else f"{num_emoji(num)} <s>{esc(old)}</s> ➜ <b>{esc(subject)}</b>"
    )
    text = f"{title}\n\n{body}"
    return await broadcast(bot, text), await send_to_chat(bot, text)


@router.message(Command("replace"))
async def replace_cmd(m: Message, command: CommandObject, bot: Bot):
    if not await manager_only(m):
        return
    args = split_args(command, 3)
    d = parse_date(args[0]) if args else None
    if not args or not d or not args[1].isdigit():
        await m.answer("Формат: /replace Пт | 3 | Физика (вместо предмета «-» — урок отменён)")
        return
    err = apply_replacement(d, int(args[1]), args[2])
    if err:
        await m.answer(err)
        return
    sent, in_chat = await announce_replacement(bot, d, int(args[1]), args[2])
    await m.answer(f"✅ Замена сохранена и разослана: {sent}" + (" + общий чат" if in_chat else ""))
    await audit(m.from_user, f"🔄 <b>Сделал замену</b> на {d.strftime('%d.%m')}\n{repl_desc(int(args[1]), args[2])}")


@router.message(Command("clearreplace"))
async def clearreplace(m: Message, command: CommandObject):
    if not await manager_only(m):
        return
    d = parse_date(command.args or "")
    if not d:
        await m.answer("Формат: /clearreplace Пт (или дата)")
        return
    db.execute("DELETE FROM changes WHERE date=?", (d.isoformat(),))
    db.commit()
    await m.answer(f"🗑 Замены на {d.strftime('%d.%m')} убраны.")
    await audit(m.from_user, f"🗑 <b>Убрал замены</b> на {d.strftime('%d.%m')}")


# ---------------------------------------------------------------- объявления (текст / фото / файл), «прочитал»
def ann_caption(text: str, reminder: bool = False, by: str = "admin") -> str:
    who = "помощника" if by == "helper" else "старосты"
    title = hdr("🔔", "Напоминание", f"объявление от {who}") if reminder else hdr("📢", "Объявление", f"от {who}")
    return title + (f"\n\n<blockquote>{esc(text)}</blockquote>" if text else "")


async def send_ann(bot: Bot, chat: int, kind: str, file_id: str | None, text: str, markup=None, reminder=False, by="admin"):
    cap = ann_caption(text, reminder, by)
    if kind == "photo":
        await bot.send_photo(chat, file_id, caption=cap, reply_markup=markup)
    elif kind == "document":
        await bot.send_document(chat, file_id, caption=cap, reply_markup=markup)
    else:
        await bot.send_message(chat, cap, reply_markup=markup)


async def deliver_ann(bot: Bot, ann_id: int, kind: str, file_id, text: str, only_ids=None, reminder=False, by="admin"):
    ids = only_ids if only_ids is not None else member_ids()
    kb = ik([[("✅ Прочитал", f"read:{ann_id}")]])
    sent = 0
    for uid in ids:
        try:
            await send_ann(bot, uid, kind, file_id, text, kb, reminder, by)
            sent += 1
        except Exception as e:
            logging.warning("Не доставлено %s: %s", uid, e)
        await asyncio.sleep(0.05)
    in_chat = False
    cid = chat_id()
    if only_ids is None and cid:
        try:
            await send_ann(bot, cid, kind, file_id, text, None, reminder, by)
            in_chat = True
        except Exception as e:
            logging.warning("Не удалось отправить в общий чат: %s", e)
    return sent, in_chat


def ann_by(uid: int) -> str:
    return "admin" if can_manage(uid) else "helper"


def new_announcement(kind: str, file_id, text: str, by: str = "admin") -> int:
    cur = db.execute(
        "INSERT INTO announcements(kind,file_id,text,by_role) VALUES(?,?,?,?)", (kind, file_id, text, by)
    )
    db.commit()
    return cur.lastrowid


@router.message(Command("announce"))
async def announce(m: Message, command: CommandObject, bot: Bot, state: FSMContext):
    if not await staff_only(m):
        return
    if not command.args:
        await state.set_state(Ann.content)
        await m.answer("Пришли объявление: текст, фото (можно с подписью) или файл. «отмена» — выйти.")
        return
    by = ann_by(m.from_user.id)
    ann_id = new_announcement("text", None, command.args, by)
    sent, in_chat = await deliver_ann(bot, ann_id, "text", None, command.args, by=by)
    await m.answer(f"Отправлено ученикам: {sent}" + (" и в общий чат ✅" if in_chat else ""))
    await audit(m.from_user, f"📢 <b>Отправил объявление</b> (получили: {sent})\n<blockquote>{clip(command.args, 1500)}</blockquote>")


@router.callback_query(F.data.startswith("read:"))
async def mark_read(cb: CallbackQuery):
    ann_id = int(cb.data.split(":")[1])
    db.execute("INSERT OR IGNORE INTO reads VALUES(?,?)", (ann_id, cb.from_user.id))
    db.commit()
    await cb.answer("Отметил ✅")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


def unread_info(ann_id: int):
    read = {r["user_id"] for r in db.execute("SELECT user_id FROM reads WHERE ann_id=?", (ann_id,)).fetchall()}
    users = db.execute("SELECT id, name FROM users WHERE status='approved' ORDER BY name").fetchall()
    return [u for u in users if u["id"] not in read], len(users)


@router.message(Command("unread"))
async def unread(m: Message):
    if not await manager_only(m):
        return
    ann = db.execute("SELECT * FROM announcements ORDER BY id DESC LIMIT 1").fetchone()
    if not ann:
        await m.answer("Объявлений ещё не было.")
        return
    not_read, total = unread_info(ann["id"])
    head = f"📢 Объявление #{ann['id']}: прочитали {total - len(not_read)} из {total}."
    if not not_read:
        await m.answer(head + "\nВсе прочитали ✅")
        return
    await m.answer(
        head + "\n\nНе отметились:\n" + "\n".join(f"• {esc(u['name'])}" for u in not_read),
        reply_markup=ik([[("🔔 Напомнить им", f"nudge:{ann['id']}")]]),
    )


@router.callback_query(F.data.startswith("nudge:"))
async def nudge(cb: CallbackQuery, bot: Bot):
    if not can_manage(cb.from_user.id):
        await cb.answer("Только для старосты", show_alert=True)
        return
    ann_id = int(cb.data.split(":")[1])
    ann = db.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not ann:
        await cb.answer("Объявление не найдено", show_alert=True)
        return
    await cb.answer("Отправляю напоминание…")
    not_read, _ = unread_info(ann_id)
    sent, _ = await deliver_ann(
        bot, ann_id, ann["kind"], ann["file_id"], ann["text"],
        only_ids=[u["id"] for u in not_read], reminder=True, by=ann["by_role"] or "admin",
    )
    await cb.message.answer(f"🔔 Напомнил: {sent}")
    await audit(cb.from_user, f"🔔 <b>Напомнил о непрочитанном объявлении</b> #{ann_id} (получили: {sent})")


# ---------------------------------------------------------------- опросы с подсчётом голосов
def poll_options(poll) -> list[str]:
    return poll["options"].split("\n")


def poll_text(poll_id: int) -> str:
    poll = db.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()
    counts = Counter(r["opt"] for r in db.execute("SELECT opt FROM votes WHERE poll_id=?", (poll_id,)).fetchall())
    total = sum(counts.values())
    lines = [hdr("📊", "Опрос", "🔒 закрыт" if poll["closed"] else ""), f"\n❓ <b>{esc(poll['question'])}</b>\n"]
    for i, o in enumerate(poll_options(poll)):
        n = counts.get(i, 0)
        pct = round(100 * n / total) if total else 0
        filled = round(pct / 10)
        lines.append(f"{esc(o)}\n<code>{'▰' * filled}{'▱' * (10 - filled)}</code> <b>{pct}%</b> · {n}")
    lines.append(f"\n👥 Проголосовали: <b>{total}</b>")
    return "\n".join(lines)


def poll_kb(poll_id: int) -> InlineKeyboardMarkup:
    poll = db.execute("SELECT * FROM polls WHERE id=?", (poll_id,)).fetchone()
    return ik([[(o, f"poll:{poll_id}:{i}")] for i, o in enumerate(poll_options(poll))])


async def send_poll(bot: Bot, question: str, options: list[str]) -> tuple[int, bool]:
    cur = db.execute("INSERT INTO polls(question, options) VALUES(?,?)", (question, "\n".join(options)))
    db.commit()
    pid = cur.lastrowid
    text, kb = poll_text(pid), poll_kb(pid)
    sent = 0
    for uid in member_ids():
        try:
            await bot.send_message(uid, text, reply_markup=kb)
            sent += 1
        except Exception as e:
            logging.warning("Не доставлено %s: %s", uid, e)
        await asyncio.sleep(0.05)
    in_chat = False
    cid = chat_id()
    if cid:
        try:
            await bot.send_message(cid, text, reply_markup=kb)
            in_chat = True
        except Exception as e:
            logging.warning("Не удалось отправить опрос в чат: %s", e)
    return sent, in_chat


@router.callback_query(F.data.startswith("poll:"))
async def vote(cb: CallbackQuery):
    _, pid_s, idx_s = cb.data.split(":")
    pid, idx = int(pid_s), int(idx_s)
    poll = db.execute("SELECT * FROM polls WHERE id=?", (pid,)).fetchone()
    if not poll:
        await cb.answer("Опрос не найден", show_alert=True)
        return
    if poll["closed"]:
        await cb.answer("Опрос закрыт", show_alert=True)
        return
    u = db.execute("SELECT name FROM users WHERE id=?", (cb.from_user.id,)).fetchone()
    name = u["name"] if u else cb.from_user.full_name
    db.execute("INSERT OR REPLACE INTO votes VALUES(?,?,?,?)", (pid, cb.from_user.id, idx, name))
    db.commit()
    await cb.answer("Голос учтён ✅")
    try:
        await cb.message.edit_text(poll_text(pid), reply_markup=poll_kb(pid))
    except Exception:
        pass


@router.message(Command("pollresult"))
async def pollresult(m: Message):
    if not await manager_only(m):
        return
    poll = db.execute("SELECT * FROM polls ORDER BY id DESC LIMIT 1").fetchone()
    if not poll:
        await m.answer("Опросов ещё не было.")
        return
    votes = db.execute("SELECT opt, name FROM votes WHERE poll_id=? ORDER BY name", (poll["id"],)).fetchall()
    lines = [poll_text(poll["id"]), "\n<b>Кто как проголосовал</b>"]
    for i, o in enumerate(poll_options(poll)):
        names = ", ".join(esc(v["name"]) for v in votes if v["opt"] == i) or "—"
        lines.append(f"{esc(o)}: {names}")
    voted = {v["name"] for v in votes}
    missing = [u["name"] for u in db.execute("SELECT name FROM users WHERE status='approved'").fetchall() if u["name"] not in voted]
    if missing:
        lines.append("\n<b>Не голосовали:</b> " + ", ".join(esc(x) for x in missing))
    await m.answer("\n".join(lines))


@router.message(Command("pollclose"))
async def pollclose(m: Message):
    if not await manager_only(m):
        return
    poll = db.execute("SELECT id, question FROM polls ORDER BY id DESC LIMIT 1").fetchone()
    if not poll:
        await m.answer("Опросов ещё не было.")
        return
    db.execute("UPDATE polls SET closed=1 WHERE id=?", (poll["id"],))
    db.commit()
    await m.answer("🔒 Опрос закрыт. Итоги: /pollresult")
    await audit(m.from_user, f"🔒 <b>Закрыл опрос</b>\n{clip(poll['question'])}")


# ---------------------------------------------------------------- общий чат, ученики, резервная копия
@router.message(Command("setchat"))
async def setchat(m: Message):
    if not await owner_only(m):
        return
    if m.chat.type == "private":
        await m.answer(
            "Добавь меня в общий чат класса и напиши там /setchat "
            "(лучше так: /setchat@имя_бота, чтобы я точно услышал)."
        )
        return
    set_setting("chat_id", str(m.chat.id))
    await m.answer("✅ Чат подключён. Сюда будут приходить объявления, опросы, замены и напоминания о контрольных.")


@router.message(Command("unsetchat"))
async def unsetchat(m: Message):
    if not await owner_only(m):
        return
    del_setting("chat_id")
    await m.answer("Общий чат отключён.")


def find_users(q: str):
    rows = db.execute("SELECT id, name, status, role FROM users").fetchall()
    if q.isdigit():
        return [r for r in rows if r["id"] == int(q)]
    return [r for r in rows if q.casefold() in r["name"].casefold()]


async def _pick_user(m: Message, q: str):
    found = find_users(q) if q else []
    if not found:
        await m.answer("Такого ученика нет. Список: /users")
    elif len(found) > 1:
        await m.answer(
            "Нашлось несколько, укажи id:\n"
            + "\n".join(f"{esc(r['name'])} — <code>{r['id']}</code>" for r in found)
        )
    else:
        return found[0]
    return None


@router.message(Command("addhelper"))
async def addhelper(m: Message, command: CommandObject, bot: Bot):
    if not await manager_only(m):
        return
    r = await _pick_user(m, (command.args or "").strip())
    if not r:
        return
    if r["id"] in ADMINS:
        await m.answer("Это админ бота — ему и так доступно всё.")
    elif r["role"] == "starosta":
        await m.answer("Он староста — ему и так доступно всё по классу.")
    elif r["status"] != "approved":
        await m.answer("Сначала подтверди его заявку (👥 Заявки).")
    elif r["role"] == "helper":
        await m.answer("Он уже помощник.")
    else:
        db.execute("UPDATE users SET role='helper' WHERE id=?", (r["id"],))
        db.commit()
        try:
            await bot.send_message(
                r["id"],
                "🛠 Староста назначил тебя помощником! Теперь тебе доступны кнопки внизу: "
                "➕ Добавить ДЗ, 📝 Контрольная, 📢 Объявление.",
                reply_markup=KB_HELPER,
            )
        except Exception as e:
            logging.warning("Не удалось уведомить %s: %s", r["id"], e)
        await apply_commands(bot, r["id"])
        await m.answer(f"🛠 {esc(r['name'])} теперь помощник.")
        await audit(m.from_user, f"🛠 <b>Назначил помощника:</b> {esc(r['name'])}")


@router.message(Command("delhelper"))
async def delhelper(m: Message, command: CommandObject, bot: Bot):
    if not await manager_only(m):
        return
    r = await _pick_user(m, (command.args or "").strip())
    if not r:
        return
    if r["role"] != "helper":
        await m.answer("Он и так не помощник.")
        return
    db.execute("UPDATE users SET role='student' WHERE id=?", (r["id"],))
    db.commit()
    try:
        await bot.send_message(r["id"], "Роль помощника снята.", reply_markup=KB)
    except Exception as e:
        logging.warning("Не удалось уведомить %s: %s", r["id"], e)
    await apply_commands(bot, r["id"])
    await m.answer(f"Роль помощника снята: {esc(r['name'])}")
    await audit(m.from_user, f"🛠 <b>Снял роль помощника:</b> {esc(r['name'])}")


@router.message(Command("helpers"))
async def helpers(m: Message):
    if not await manager_only(m):
        return
    rows = db.execute("SELECT id, name FROM users WHERE role='helper' ORDER BY name").fetchall()
    await m.answer(
        "🛠 Помощники:\n" + "\n".join(f"{esc(r['name'])} — <code>{r['id']}</code>" for r in rows)
        if rows else "Помощников пока нет. Назначить: /addhelper Иванов"
    )


@router.message(Command("addstarosta"))
async def addstarosta(m: Message, command: CommandObject, bot: Bot):
    if not await owner_only(m):
        return
    r = await _pick_user(m, (command.args or "").strip())
    if not r:
        return
    if r["id"] in ADMINS:
        await m.answer("Это админ бота.")
    elif r["status"] != "approved":
        await m.answer("Сначала подтверди его заявку (👥 Заявки).")
    elif r["role"] == "starosta":
        await m.answer("Он уже староста.")
    else:
        db.execute("UPDATE users SET role='starosta' WHERE id=?", (r["id"],))
        db.commit()
        try:
            await bot.send_message(
                r["id"],
                "⭐ <b>Тебя назначили старостой!</b>\nТеперь тебе доступно всё по классу: расписание, ДЗ, объявления, "
                "опросы, заявки. Список: «помощь».",
                reply_markup=KB_STAROSTA,
            )
        except Exception as e:
            logging.warning("Не удалось уведомить %s: %s", r["id"], e)
        await apply_commands(bot, r["id"])
        await m.answer(f"⭐ {esc(r['name'])} теперь староста.")


@router.message(Command("delstarosta"))
async def delstarosta(m: Message, command: CommandObject, bot: Bot):
    if not await owner_only(m):
        return
    r = await _pick_user(m, (command.args or "").strip())
    if not r:
        return
    if r["role"] != "starosta":
        await m.answer("Он не староста.")
        return
    db.execute("UPDATE users SET role='student' WHERE id=?", (r["id"],))
    db.commit()
    try:
        await bot.send_message(r["id"], "Роль старосты снята.", reply_markup=KB)
    except Exception as e:
        logging.warning("Не удалось уведомить %s: %s", r["id"], e)
    await apply_commands(bot, r["id"])
    await m.answer(f"Роль старосты снята: {esc(r['name'])}")


@router.message(Command("starostas"))
async def starostas(m: Message):
    if not await owner_only(m):
        return
    await m.answer(starostas_text())


def starostas_text() -> str:
    rows = db.execute("SELECT id, name FROM users WHERE role='starosta' ORDER BY name").fetchall()
    if not rows:
        return hdr("⭐", "Старосты") + "\n\nПока никого. Назначить: /addstarosta Иванов"
    return hdr("⭐", "Старосты") + "\n\n" + "\n".join(f"▫️ {esc(r['name'])} — <code>{r['id']}</code>" for r in rows)


@router.message(F.chat.type == "private", F.text == "⚙️ Админ")
async def admin_panel(m: Message):
    if not await owner_only(m):
        return
    total = db.execute("SELECT COUNT(*) c FROM users WHERE status='approved'").fetchone()["c"]
    pending = db.execute("SELECT COUNT(*) c FROM users WHERE status='pending'").fetchone()["c"]
    n_st = db.execute("SELECT COUNT(*) c FROM users WHERE role='starosta'").fetchone()["c"]
    n_hp = db.execute("SELECT COUNT(*) c FROM users WHERE role='helper'").fetchone()["c"]
    chat = "подключён ✅" if chat_id() else "не подключён ❌ (/setchat в чате)"
    await m.answer(
        hdr("⚙️", "Панель админа")
        + f"\n\n👥 Учеников: <b>{total}</b> · заявок: <b>{pending}</b>\n⭐ Старост: <b>{n_st}</b> · 🛠 помощников: <b>{n_hp}</b>"
        + f"\n💬 Общий чат: {chat}\n🕗 Смена: <b>{SHIFT}</b>",
        reply_markup=ik([[("💾 Копия базы", "adm:backup"), ("⭐ Старосты", "adm:starostas")]]),
    )


@router.callback_query(F.data.startswith("adm:"))
async def admin_cb(cb: CallbackQuery, bot: Bot):
    if not is_owner(cb.from_user.id):
        await cb.answer("Только для админа", show_alert=True)
        return
    await cb.answer()
    if cb.data == "adm:backup":
        await send_backup(bot, [cb.from_user.id])
    elif cb.data == "adm:starostas":
        await cb.message.answer(starostas_text())


@router.message(Command("users"))
async def users(m: Message):
    if not await manager_only(m):
        return
    rows = db.execute("SELECT id, name, status, role FROM users ORDER BY name").fetchall()
    await m.answer(
        hdr("👥", "Ученики", str(len(rows))) + "\n\n"
        + "\n".join(
            f"{'✅' if r['status'] == 'approved' else '⏳'}{ROLE_ICON.get(r['role'], '')} "
            f"{esc(r['name'])} — <code>{r['id']}</code>"
            for r in rows
        )
        + "\n\n⭐ староста · 🛠 помощник"
    )


@router.message(Command("deluser"))
async def deluser(m: Message, command: CommandObject):
    if not await manager_only(m):
        return
    q = (command.args or "").strip()
    if not q:
        await m.answer("Формат: /deluser Иванов Иван (или id из списка /users)")
        return
    rows = db.execute("SELECT id, name FROM users").fetchall()
    if q.isdigit():
        found = [r for r in rows if r["id"] == int(q)]
    else:
        found = [r for r in rows if q.casefold() in r["name"].casefold()]
    if not found:
        await m.answer("Такого пользователя нет. Список: /users")
    elif len(found) > 1:
        await m.answer(
            "Нашлось несколько, удали по id:\n"
            + "\n".join(f"{esc(r['name'])} — <code>{r['id']}</code>" for r in found)
        )
    else:
        target = found[0]["id"]
        if (target in ADMINS or role_of(target) == "starosta") and not is_owner(m.from_user.id):
            await m.answer("🔒 Старосту или админа может удалить только админ бота.")
            return
        db.execute("DELETE FROM users WHERE id=?", (target,))
        db.commit()
        await m.answer(f"🗑 Удалён: {esc(found[0]['name'])}")
        await audit(m.from_user, f"🗑 <b>Удалил ученика:</b> {esc(found[0]['name'])}")


def make_backup() -> str:
    path = os.path.join(tempfile.gettempdir(), "backup_tmp.db")
    if os.path.exists(path):
        os.remove(path)
    dst = sqlite3.connect(path)
    with dst:
        db.backup(dst)
    dst.close()
    return path


async def send_backup(bot: Bot, to: set[int] | list[int] | None = None):
    path = make_backup()
    name = f"class_{today().isoformat()}.db"
    for a in (to or ADMINS):
        try:
            await bot.send_document(a, FSInputFile(path, filename=name), caption="💾 Резервная копия базы класса")
        except Exception as e:
            logging.warning("Не удалось отправить копию %s: %s", a, e)


@router.message(Command("backup"))
async def backup_cmd(m: Message, bot: Bot):
    if not await owner_only(m):
        return
    await send_backup(bot, [m.from_user.id])


@router.message(F.chat.type == "private", F.document, F.caption.startswith("/restore"))
async def restore_cmd(m: Message, bot: Bot):
    """Восстановление базы: админ присылает файл .db с подписью /restore."""
    if not is_owner(m.from_user.id):
        return
    if m.document.file_size and m.document.file_size > 20 * 1024 * 1024:
        await m.answer("❌ Файл слишком большой.")
        return
    tmp = os.path.join(tempfile.gettempdir(), "restore_upload.db")
    await bot.download(m.document, destination=tmp)
    try:
        src = sqlite3.connect(tmp)
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"users", "homework", "schedule"} <= tables:
            src.close()
            await m.answer("❌ Это не база класса (нет нужных таблиц). Ничего не изменено.")
            return
        await send_backup(bot, [m.from_user.id])  # страховка: копия текущей базы перед заменой
        with db:
            src.backup(db)
        src.close()
        await m.answer("✅ База восстановлена из файла.")
    except Exception as e:
        logging.exception("restore failed")
        await m.answer(f"❌ Не удалось восстановить: {esc(str(e))}")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


# ---------------------------------------------------------------- пошаговые мастера (кнопки старосты)
@router.message(F.chat.type == "private", F.text == "➕ Добавить ДЗ")
async def hw_wizard(m: Message, state: FSMContext):
    if not await staff_only(m):
        return
    await state.clear()
    await state.set_state(HW.subject)
    await m.answer("Какой предмет? (так же, как в расписании). «отмена» — выйти.")


@router.message(HW.subject)
async def hw_subject(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    await state.update_data(subject=m.text.strip())
    await state.set_state(HW.task)
    await m.answer("Что задано?")


@router.message(HW.task)
async def hw_task(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    await state.update_data(task=m.text.strip())
    await state.set_state(HW.due)
    await m.answer("На какой урок? Напиши дату (12.10), «завтра» или день недели («пт»).")


@router.message(HW.due)
async def hw_due(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    d = parse_date(m.text)
    if not d:
        await m.answer("Не понял дату. Примеры: 12.10, завтра, пт.")
        return
    data = await state.get_data()
    db.execute(
        "INSERT INTO homework(subject,task,due) VALUES(?,?,?)", (data["subject"], data["task"], d.isoformat())
    )
    db.commit()
    await state.clear()
    await m.answer(f"✅ Добавлено: {esc(data['subject'])} на {d.strftime('%d.%m')}.", reply_markup=kbm(m))
    await audit(m.from_user, f"➕ <b>Добавил ДЗ</b> на {d.strftime('%d.%m')}\n{esc(data['subject'])}: {clip(data['task'])}")


@router.message(F.chat.type == "private", F.text == "📝 Контрольная")
async def test_wizard(m: Message, state: FSMContext):
    if not await staff_only(m):
        return
    await state.clear()
    await state.set_state(TestW.subject)
    await m.answer("По какому предмету контрольная? «отмена» — выйти.")


@router.message(TestW.subject)
async def test_subject(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    await state.update_data(subject=m.text.strip())
    await state.set_state(TestW.due)
    await m.answer("Когда? Дата (15.10), «завтра» или день недели.")


@router.message(TestW.due)
async def test_due(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    d = parse_date(m.text)
    if not d:
        await m.answer("Не понял дату. Примеры: 15.10, завтра, пт.")
        return
    await state.update_data(due=d.isoformat())
    await state.set_state(TestW.topic)
    await m.answer("Тема? Если не нужна — напиши «-».")


@router.message(TestW.topic)
async def test_topic(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    data = await state.get_data()
    topic = "" if m.text.strip() in ("-", "—") else m.text.strip()
    d = date.fromisoformat(data["due"])
    save_test(data["subject"], d, topic)
    await state.clear()
    await m.answer(
        f"✅ Контрольная по {esc(data['subject'])} на {d.strftime('%d.%m')} добавлена. Напомню за 3 дня и за 1 день.",
        reply_markup=kbm(m),
    )
    await audit(
        m.from_user,
        f"📝 <b>Добавил контрольную</b> на {d.strftime('%d.%m')}\n{esc(data['subject'])}" + (f" — {clip(topic)}" if topic else ""),
    )


@router.message(F.chat.type == "private", F.text == "📢 Объявление")
async def ann_wizard(m: Message, state: FSMContext):
    if not await staff_only(m):
        return
    await state.clear()
    await state.set_state(Ann.content)
    await m.answer("Пришли объявление: текст, фото (можно с подписью) или файл. «отмена» — выйти.")


@router.message(Ann.content)
async def ann_content(m: Message, state: FSMContext, bot: Bot):
    if await interrupted(m, state):
        return
    if m.photo:
        kind, file_id, text = "photo", m.photo[-1].file_id, m.caption or ""
    elif m.document:
        kind, file_id, text = "document", m.document.file_id, m.caption or ""
    elif m.text:
        kind, file_id, text = "text", None, m.text.strip()
    else:
        await m.answer("Пришли текст, фото или файл.")
        return
    if len(ann_caption(text)) > (4000 if kind == "text" else 1000):
        await m.answer("Слишком длинно. Сократи текст.")
        return
    await state.update_data(kind=kind, file_id=file_id, text=text)
    await state.set_state(Ann.confirm)
    await m.answer("Так увидят ученики 👇")
    await send_ann(
        bot, m.chat.id, kind, file_id, text,
        ik([[("✅ Отправить всем", "ann:send"), ("❌ Отмена", "ann:cancel")]]), by=ann_by(m.from_user.id),
    )


@router.callback_query(F.data == "ann:send")
async def ann_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not can_edit(cb.from_user.id):
        await cb.answer("Только для старосты и помощников", show_alert=True)
        return
    data = await state.get_data()
    if not data.get("kind"):
        await cb.answer("Черновик устарел. Начни заново.", show_alert=True)
        return
    await cb.answer("Отправляю…")
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    by = ann_by(cb.from_user.id)
    ann_id = new_announcement(data["kind"], data.get("file_id"), data.get("text", ""), by)
    sent, in_chat = await deliver_ann(bot, ann_id, data["kind"], data.get("file_id"), data.get("text", ""), by=by)
    await cb.message.answer(
        f"✅ Отправлено ученикам: {sent}" + (" и в общий чат" if in_chat else "") + "\nКто прочитал: /unread"
    )
    kind_ru = {"photo": "с фото", "document": "с файлом"}.get(data["kind"], "")
    await audit(
        cb.from_user,
        f"📢 <b>Отправил объявление</b> {kind_ru} (получили: {sent})\n<blockquote>{clip(data.get('text', ''), 1500) or '—'}</blockquote>",
    )


@router.callback_query(F.data == "ann:cancel")
async def ann_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("Отменено")


@router.message(F.chat.type == "private", F.text == "📊 Опрос")
async def poll_wizard(m: Message, state: FSMContext):
    if not await manager_only(m):
        return
    await state.clear()
    await state.set_state(PollW.question)
    await m.answer("Вопрос для опроса? «отмена» — выйти.")


@router.message(PollW.question)
async def poll_question(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    await state.update_data(question=m.text.strip())
    await state.set_state(PollW.options)
    await m.answer("Варианты ответов — каждый с новой строки (от 2 до 6):")


@router.message(PollW.options)
async def poll_opts(m: Message, state: FSMContext, bot: Bot):
    if await interrupted(m, state) or not m.text:
        return
    opts = [x.strip() for x in m.text.splitlines() if x.strip()]
    if not 2 <= len(opts) <= 6 or any(len(o) > 50 for o in opts):
        await m.answer("Нужно от 2 до 6 вариантов, каждый не длиннее 50 символов. Попробуй ещё раз.")
        return
    data = await state.get_data()
    await state.clear()
    sent, in_chat = await send_poll(bot, data["question"], opts)
    await m.answer(
        f"✅ Опрос отправлен: {sent}" + (" + общий чат" if in_chat else "") + "\nИтоги с именами: /pollresult",
        reply_markup=kbm(m),
    )
    await audit(m.from_user, f"📊 <b>Отправил опрос</b> (получили: {sent})\n{clip(data['question'])}\n" + "\n".join(f"▫️ {esc(o)}" for o in opts))


@router.message(F.chat.type == "private", F.text == "🔄 Замена")
async def repl_wizard(m: Message, state: FSMContext):
    if not await manager_only(m):
        return
    await state.clear()
    await state.set_state(Repl.day)
    await m.answer("На какой день замена? Дата, «завтра» или день недели. «отмена» — выйти.")


@router.message(Repl.day)
async def repl_day(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    d = parse_date(m.text)
    if not d:
        await m.answer("Не понял дату. Примеры: 16.10, завтра, пт.")
        return
    await state.update_data(day=d.isoformat())
    await state.set_state(Repl.num)
    await m.answer("Какой по счёту урок? (число)")


@router.message(Repl.num)
async def repl_num(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    if not m.text.strip().isdigit():
        await m.answer("Нужно число, например 3.")
        return
    await state.update_data(num=int(m.text.strip()))
    await state.set_state(Repl.subject)
    await m.answer("Какой предмет будет вместо? Если урок отменён — напиши «-».")


@router.message(Repl.subject)
async def repl_subject(m: Message, state: FSMContext, bot: Bot):
    if await interrupted(m, state) or not m.text:
        return
    data = await state.get_data()
    d, num, subject = date.fromisoformat(data["day"]), data["num"], m.text.strip()
    err = apply_replacement(d, num, subject)
    if err:
        await state.clear()
        await m.answer(err + "\nНачни заново: 🔄 Замена.", reply_markup=kbm(m))
        return
    await state.clear()
    sent, in_chat = await announce_replacement(bot, d, num, subject)
    await m.answer(
        f"✅ Замена сохранена и разослана: {sent}" + (" + общий чат" if in_chat else ""),
        reply_markup=kbm(m),
    )
    await audit(m.from_user, f"🔄 <b>Сделал замену</b> на {d.strftime('%d.%m')}\n{repl_desc(num, subject)}")


@router.message(F.chat.type == "private", F.text == "🗓 Неделя")
async def week_wizard(m: Message, state: FSMContext):
    if not await manager_only(m):
        return
    await state.clear()
    await state.set_state(Week.text)
    await m.answer(WEEK_HINT + "\n\n«отмена» — выйти.")


@router.message(Week.text)
async def week_text(m: Message, state: FSMContext):
    if await interrupted(m, state) or not m.text:
        return
    await state.clear()
    await m.answer(week_reply(m.text), reply_markup=kbm(m))
    await audit(m.from_user, f"🗓 <b>Изменил расписание недели</b>\n{clip(m.text)}")


@router.message(F.chat.type == "private", F.text == "👥 Заявки")
async def pending_list(m: Message):
    if not await manager_only(m):
        return
    rows = db.execute("SELECT id, name FROM users WHERE status='pending' ORDER BY name").fetchall()
    if not rows:
        await m.answer("Новых заявок нет ✅")
        return
    for r in rows:
        await m.answer(
            f"🆕 <b>{esc(r['name'])}</b> (id <code>{r['id']}</code>)",
            reply_markup=ik([[("✅ Принять", f"reg:ok:{r['id']}"), ("❌ Отклонить", f"reg:no:{r['id']}")]]),
        )


# ---------------------------------------------------------------- слова и кнопки (без слэша)
WORDS = {
    "домашка": hw, "дз": hw, "домашнее задание": hw, "домашние задания": hw, "домашнее": hw,
    "события": events, "контрольные": events,
    "помощь": help_cmd, "команды": help_cmd, "меню": help_cmd,
}


@router.message(F.text)
async def words(m: Message):
    if m.chat.type != "private" or m.text.startswith("/"):
        return
    t = norm(m.text)
    if t.startswith("расписание"):
        if await ensure_member(m):
            await send_schedule(m, t[len("расписание"):])
    elif t in WORDS:
        await WORDS[t](m)


# ---------------------------------------------------------------- фоновые задачи
async def test_reminders(bot: Bot):
    """Контрольные: напоминание за 3 дня и за 1 день."""
    for days, when in ((3, "через 3 дня"), (1, "завтра")):
        day = (today() + timedelta(days=days)).isoformat()
        for r in db.execute("SELECT * FROM events WHERE day=? AND kind='test'", (day,)).fetchall():
            text = (
                hdr("⚠️", f"Контрольная {when}", fmt(r["day"]))
                + f"\n\n📝 {esc(r['text'])}\n\nГотовимся! 📚"
            )
            await broadcast(bot, text)
            await send_to_chat(bot, text)


async def job_evening(bot: Bot, now: datetime):
    await test_reminders(bot)
    if now.weekday() != 5:  # в субботу вечернюю сводку не шлём
        await broadcast(bot, tomorrow_text(include_tests=False))


async def job_backup(bot: Bot, now: datetime):
    await send_backup(bot)


async def scheduler(bot: Bot):
    done: dict[str, date] = {}
    jobs = (
        ("evening", REMIND_HOUR, job_evening),
        ("backup", BACKUP_HOUR, job_backup),
    )
    while True:
        now = datetime.now(TZ)
        for name, hour, job in jobs:
            if now.hour == hour and done.get(name) != now.date():
                done[name] = now.date()
                try:
                    await job(bot, now)
                except Exception:
                    logging.exception("Ошибка в задаче %s", name)
        await asyncio.sleep(30)


async def warn_if_empty(bot: Bot):
    """Если база пустая (например, диск не сохранился) — сообщаем админу, как восстановить."""
    n = db.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
    if n:
        return
    for a in ADMINS:
        try:
            await bot.send_message(
                a,
                "⚠️ <b>База данных пустая.</b>\n"
                f"Файл: <code>{esc(DB_PATH)}</code>\n\n"
                "Если это не первый запуск — пришли мне последнюю копию (.db) с подписью "
                "<code>/restore</code>, и всё вернётся.\n"
                "Чтобы база больше не пропадала, укажи DB_PATH на постоянный диск.",
            )
        except Exception as e:
            logging.warning("Не удалось предупредить админа %s: %s", a, e)


async def main():
    global BOT
    bot = BOT = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    await setup_profile(bot)
    await warn_if_empty(bot)
    asyncio.create_task(scheduler(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
