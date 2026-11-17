import asyncio, io, logging, os, re, time
import datetime as dt
from html import escape

import aiohttp
import aiosqlite
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, BusinessConnection, BusinessMessagesDeleted,
                           CallbackQuery, FSInputFile, InlineKeyboardButton, LabeledPrice,
                           Message, PreCheckoutQuery)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageDraw, ImageFont

try:
    from dotenv import load_dotenv
    load_dotenv()  # читает .env рядом с bot.py
except ImportError:
    pass

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
CRYPTO = os.getenv("CRYPTO_TOKEN", "")          # токен @CryptoBot -> Crypto Pay
PUBLIC_URL = (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL", "")).rstrip("/")  # Render задаёт сам
PORT = int(os.getenv("PORT", "8080"))
DB = os.getenv("DB_PATH", "bot.db")
OFF = int(os.getenv("TZ_OFFSET", "3"))          # часовой пояс (часы от UTC)
KEEP_DAYS = int(os.getenv("RETENTION_DAYS", "30"))  # срок хранения архива

r = Router()
cooldown: dict = {}


class S(StatesGroup):
    search = State(); ar_key = State(); ar_text = State(); set_val = State(); bc = State()


# ───────────────────────── БД ─────────────────────────
async def q(sql, args=(), one=False, commit=False):
    async with aiosqlite.connect(DB) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(sql, args)
        if commit:
            await db.commit()
            return cur.lastrowid
        rows = await cur.fetchall()
        return (rows[0] if rows else None) if one else rows


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, username TEXT,
            joined INTEGER, save_media INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS conns(id TEXT PRIMARY KEY, user_id INTEGER, enabled INTEGER);
        CREATE TABLE IF NOT EXISTS messages(owner INTEGER, chat_id INTEGER, msg_id INTEGER,
            from_id INTEGER, name TEXT, username TEXT, text TEXT, mtype TEXT, fid TEXT, date INTEGER,
            PRIMARY KEY(owner, chat_id, msg_id));
        CREATE INDEX IF NOT EXISTS ix_msg ON messages(owner, chat_id, date);
        CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER,
            kind TEXT, key TEXT, text TEXT, active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS donations(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INTEGER,
            amount REAL, cur TEXT, method TEXT, ext TEXT UNIQUE, date INTEGER);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        """)
        await db.commit()


async def get_set(k, default=""):
    row = await q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return row["v"] if row else default


async def put_set(k, v):
    await q("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v), commit=True)


async def add_user(u):
    await q("INSERT INTO users(id,name,username,joined) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username",
            (u.id, u.full_name, u.username, int(time.time())), commit=True)


async def add_donation(uid, amount, cur, method, ext):
    await q("INSERT OR IGNORE INTO donations(uid,amount,cur,method,ext,date) VALUES(?,?,?,?,?,?)",
            (uid, amount, cur, method, ext, int(time.time())), commit=True)


# ───────────────────────── Утилиты ─────────────────────────
def kb(*rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, url=d) if d.startswith("http")
                else InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


BACK = [("⬅️ Меню", "menu")]


def menu(uid):
    rows = [[("🗂 Архив", "arch:0"), ("🤖 Автоответчик", "ar")],
            [("👤 Профиль", "prof"), ("💎 Донат", "don")], [("❓ Помощь", "help")]]
    if uid in ADMINS:
        rows.append([("🛠 Админпанель", "adm")])
    return kb(*rows)


async def send_cover(msg: Message, fid, ctype, text, markup):
    fn = {"animation": msg.answer_animation, "video": msg.answer_video}.get(ctype, msg.answer_photo)
    await fn(fid, caption=text, reply_markup=markup)


async def show(c: CallbackQuery, text, markup, cover=True):
    """Экран меню. Если задана обложка — текст идёт подписью под ней (лимит 1024 символа)."""
    msg = c.message
    fid, ctype = await get_set("cover"), await get_set("cover_type", "photo")
    has_media = bool(msg.photo or msg.animation or msg.video)
    try:
        if cover and fid and len(text) <= 1000:
            if has_media:
                await msg.edit_caption(caption=text, reply_markup=markup)
            else:
                await msg.delete()
                await send_cover(msg, fid, ctype, text, markup)
        elif has_media:
            await msg.delete()
            await msg.answer(text, reply_markup=markup)
        else:
            await msg.edit_text(text, reply_markup=markup)
    except Exception as e:
        if "not modified" not in str(e):
            try:
                await msg.delete()
            except Exception:
                pass
            await msg.answer(text, reply_markup=markup)
    await c.answer()


def who(name, username):
    return escape(name or "—") + (f"(@{username})" if username else "")


def cut(s, n=3800):
    return s if len(s) <= n else s[:n] + "…"


def media_of(m: Message):
    if m.photo:
        return "photo", m.photo[-1].file_id
    for t in ("video", "video_note", "voice", "audio", "document", "animation", "sticker"):
        o = getattr(m, t)
        if o:
            return t, o.file_id
    return None, None


SEND = {"photo": "send_photo", "video": "send_video", "video_note": "send_video_note", "voice": "send_voice",
        "audio": "send_audio", "document": "send_document", "animation": "send_animation", "sticker": "send_sticker"}


EXT = {"photo": "jpg", "video": "mp4", "video_note": "mp4", "voice": "ogg", "audio": "mp3",
       "document": "bin", "animation": "mp4", "sticker": "webp"}


async def send_media(bot, chat, t, fid, cap):
    fn = getattr(bot, SEND[t])
    nocap = t in ("video_note", "sticker")
    if nocap:
        await bot.send_message(chat, cap)

    async def go(f):
        await (fn(chat, f) if nocap else fn(chat, f, caption=cap))
    try:
        await go(fid)
    except TelegramBadRequest:  # напр. SelfDestructingPhoto / FILE_REFERENCE_EXPIRED
        buf = await bot.download(fid)  # скачиваем и загружаем заново как обычный файл
        await go(BufferedInputFile(buf.read(), f"media.{EXT[t]}"))


async def owner_of(bot, conn_id):
    row = await q("SELECT user_id FROM conns WHERE id=?", (conn_id,), one=True)
    if row:
        return row["user_id"]
    bc = await bot.get_business_connection(conn_id)
    await q("INSERT OR REPLACE INTO conns VALUES(?,?,?)", (conn_id, bc.user.id, int(bc.is_enabled)), commit=True)
    return bc.user.id


# ───────────────────────── Business ─────────────────────────
@r.business_connection()
async def on_conn(bc: BusinessConnection, bot: Bot):
    await q("INSERT OR REPLACE INTO conns VALUES(?,?,?)", (bc.id, bc.user.id, int(bc.is_enabled)), commit=True)
    await add_user(bc.user)
    txt = "✅ Бот подключён к вашему аккаунту. Перехват работает 24/7." if bc.is_enabled \
        else "⏸ Бот отключён от аккаунта."
    await bot.send_message(bc.user_chat_id, txt)


def in_window(key):
    a, b = map(int, key.split("-"))
    h = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=OFF)).hour
    return a <= h < b if a <= b else (h >= a or h < b)


@r.business_message()
async def on_bm(m: Message, bot: Bot):
    owner = await owner_of(bot, m.business_connection_id)
    u = m.from_user
    mtype, fid = media_of(m)
    text = m.text or m.caption or ""
    await q("INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?)",
            (owner, m.chat.id, m.message_id, u.id, u.full_name, u.username, text, mtype, fid,
             int(m.date.timestamp())), commit=True)
    rp = m.reply_to_message
    if rp and u.id == owner:  # ответ на (одноразовое) медиа -> сохраняем его в ЛС
        rt, rf = media_of(rp)
        if rt in ("photo", "video", "video_note", "voice"):
            src = rp.from_user
            try:
                await send_media(bot, owner, rt, rf, f"💾 Медиа от {who(src.full_name, src.username) if src else '—'}")
            except Exception as e:
                logging.warning("reply save: %s", e)
    if u.id == owner:
        if text.strip().lower() == ".stats":
            await send_stats(bot, owner, m.chat.id, m.chat.full_name or "chat")
        return
    # одноразовые/любые медиа -> в личку владельцу
    me = await q("SELECT save_media FROM users WHERE id=?", (owner,), one=True)
    if mtype in ("photo", "video", "video_note", "voice") and (me and me["save_media"]):
        try:
            await send_media(bot, owner, mtype, fid, f"💾 Медиа от {who(u.full_name, u.username)}")
        except Exception as e:
            logging.warning("save media: %s", e)
    # автоответы
    for ru in await q("SELECT * FROM rules WHERE owner=? AND active=1", (owner,)):
        hit = (ru["kind"] == "t" and ru["key"].lower() in text.lower()) or \
              (ru["kind"] == "s" and in_window(ru["key"]))
        if not hit:
            continue
        ck = (owner, m.chat.id, ru["id"])
        if time.time() - cooldown.get(ck, 0) < (21600 if ru["kind"] == "s" else 60):
            continue
        cooldown[ck] = time.time()
        await bot.send_message(m.chat.id, ru["text"], business_connection_id=m.business_connection_id)
        break


@r.edited_business_message()
async def on_edit(m: Message, bot: Bot):
    owner = await owner_of(bot, m.business_connection_id)
    old = await q("SELECT * FROM messages WHERE owner=? AND chat_id=? AND msg_id=?",
                  (owner, m.chat.id, m.message_id), one=True)
    new = m.text or m.caption or ""
    mtype, fid = media_of(m)
    await q("UPDATE messages SET text=?, fid=COALESCE(?,fid) WHERE owner=? AND chat_id=? AND msg_id=?",
            (new, fid, owner, m.chat.id, m.message_id), commit=True)
    if m.from_user.id == owner or not old or old["text"] == new:
        return
    await bot.send_message(owner, f"<b>Сообщение отредактировано</b>\nОт кого: {who(m.from_user.full_name, m.from_user.username)}"
                                  f"\n\n<b>Было:</b>\n{escape(cut(old['text'] or '—', 1800))}"
                                  f"\n\n<b>Стало:</b>\n{escape(cut(new or '—', 1800))}")


@r.deleted_business_messages()
async def on_del(ev: BusinessMessagesDeleted, bot: Bot):
    owner = await owner_of(bot, ev.business_connection_id)
    for mid in ev.message_ids:
        row = await q("SELECT * FROM messages WHERE owner=? AND chat_id=? AND msg_id=?",
                      (owner, ev.chat.id, mid), one=True)
        if not row or row["from_id"] == owner:
            continue
        cap = f"<b>Удалено сообщение!</b>\nОт кого: {who(row['name'], row['username'])}" \
              f"\n\n<b>Текст сообщения:</b>\n{escape(cut(row['text'] or '—', 900))}"
        try:
            if row["mtype"] and row["fid"]:
                await send_media(bot, owner, row["mtype"], row["fid"], cap)
            else:
                await bot.send_message(owner, cap)
        except Exception as e:
            logging.warning("del notify: %s", e)


# ───────────────────────── .stats ─────────────────────────
FONT_PATHS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/system/fonts/Roboto-Regular.ttf",
              "/system/fonts/NotoSans-Regular.ttf", "/system/fonts/DroidSans.ttf", "DejaVuSans.ttf"]
FONT_OK = any(os.path.exists(p) for p in FONT_PATHS)


def T(ru, en):
    return ru if FONT_OK else en


def font(size):
    for p in FONT_PATHS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


async def send_stats(bot, owner, chat_id, name):
    if not FONT_OK:
        name = name.encode('ascii', 'ignore').decode() or 'chat'
    since = int(time.time()) - 8 * 86400
    rows = await q("SELECT from_id, date FROM messages WHERE owner=? AND chat_id=? AND date>=?",
                   (owner, chat_id, since))
    today = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=OFF)).date()
    days = [today - dt.timedelta(days=i) for i in range(6, -1, -1)]
    mine = {d: 0 for d in days}; theirs = {d: 0 for d in days}
    for x in rows:
        d = dt.datetime.fromtimestamp(x["date"] + OFF * 3600, dt.timezone.utc).date()
        if d in mine:
            (mine if x["from_id"] == owner else theirs)[d] += 1
    tm, tt = sum(mine.values()), sum(theirs.values())
    W, H = 900, 560
    img = Image.new("RGB", (W, H), (22, 24, 33)); d = ImageDraw.Draw(img)
    d.text((40, 30), f"{T('Переписка за 7 дней', 'Chat, 7 days')}: {name}", font=font(34), fill=(255, 255, 255))
    d.text((40, 90), f"{T('Вы', 'You')}: {tm}   {T('Собеседник', 'Them')}: {tt}   {T('Всего', 'Total')}: {tm + tt}", font=font(26), fill=(160, 170, 200))
    top = max(max(mine.values()), max(theirs.values()), 1)
    base, ch = 480, 300
    for i, day in enumerate(days):
        x = 60 + i * 120
        for j, (data, col) in enumerate(((mine, (82, 148, 255)), (theirs, (255, 170, 70)))):
            h = int(ch * data[day] / top)
            d.rounded_rectangle((x + j * 44, base - h, x + j * 44 + 38, base), 6, fill=col)
            d.text((x + j * 44 + 4, base - h - 26), str(data[day]), font=font(20), fill=(230, 230, 230))
        d.text((x + 8, base + 12), day.strftime("%d.%m"), font=font(20), fill=(150, 155, 175))
    d.rectangle((40, 140, 58, 158), fill=(82, 148, 255)); d.text((66, 136), T("Вы", "You"), font=font(20), fill=(220, 220, 220))
    d.rectangle((140, 140, 158, 158), fill=(255, 170, 70)); d.text((166, 136), T("Собеседник", "Them"), font=font(20), fill=(220, 220, 220))
    buf = io.BytesIO(); img.save(buf, "PNG")
    await bot.send_photo(owner, BufferedInputFile(buf.getvalue(), "stats.png"), caption=f"📊 Статистика: {escape(name)}")


# ───────────────────────── Старт / меню ─────────────────────────
@r.message(CommandStart())
async def start(m: Message):
    await add_user(m.from_user)
    welcome = await get_set("welcome", "👋 <b>Привет!</b>\nЯ сохраняю удалённые и изменённые сообщения, "
                                       "медиа, веду архив переписок и отвечаю за вас.\n\nПодключение: "
                                       "Настройки → Telegram для бизнеса → Чат-боты.")
    cover = await get_set("cover")
    if cover:
        await send_cover(m, cover, await get_set("cover_type", "photo"), welcome, menu(m.from_user.id))
    else:
        await m.answer(welcome, reply_markup=menu(m.from_user.id))


@r.callback_query(F.data == "menu")
async def cb_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "📍 <b>Главное меню</b>", menu(c.from_user.id))


@r.callback_query(F.data == "help")
async def cb_help(c: CallbackQuery):
    await show(c, "❓ <b>Помощь</b>\n\n1. Настройки Telegram → Telegram для бизнеса → Чат-боты → укажите этого бота "
                  "и дайте доступ к чатам.\n2. Удалённые/изменённые сообщения собеседников придут сюда.\n"
                  "3. Одноразовое фото/видео/кружок/голосовое: ответьте на него (до просмотра), бот пришлёт копию в ЛС.\n"
                  "4. В любом чате напишите <code>.stats</code> — пришлю картинку со статистикой за 7 дней.\n"
                  "5. Автоответы настраиваются в разделе «Автоответчик».\n\n"
                  "⚠️ Telegram не всегда передаёт боту одноразовые медиа — сохраняется то, что бот получил.",
                  kb(BACK))


# ───────────────────────── Профиль ─────────────────────────
@r.callback_query(F.data == "prof")
async def cb_prof(c: CallbackQuery):
    uid = c.from_user.id
    u = await q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    n = (await q("SELECT COUNT(*) n FROM messages WHERE owner=?", (uid,), one=True))["n"]
    con = await q("SELECT 1 FROM conns WHERE user_id=? AND enabled=1", (uid,), one=True)
    don = await q("SELECT cur, SUM(amount) s FROM donations WHERE uid=? GROUP BY cur", (uid,))
    dtxt = ", ".join(f"{x['s']:g} {x['cur']}" for x in don) or "—"
    sm = (u["save_media"] if u else 1)
    await show(c, f"👤 <b>Профиль</b>\nID: <code>{uid}</code>\nБизнес-подключение: {'✅' if con else '❌'}\n"
                  f"Сообщений в архиве: {n}\nВаши донаты: {dtxt}",
               kb([(f"💾 Все медиа в ЛС: {'вкл' if sm else 'выкл'}", "pm")], [("🗑 Очистить архив", "pc")], BACK))


@r.callback_query(F.data == "pm")
async def cb_pm(c: CallbackQuery):
    await q("UPDATE users SET save_media=1-save_media WHERE id=?", (c.from_user.id,), commit=True)
    await cb_prof(c)


@r.callback_query(F.data == "pc")
async def cb_pc(c: CallbackQuery):
    await show(c, "Удалить весь ваш архив? Это необратимо.", kb([("✅ Да", "pc2"), ("Отмена", "prof")]))


@r.callback_query(F.data == "pc2")
async def cb_pc2(c: CallbackQuery):
    await q("DELETE FROM messages WHERE owner=?", (c.from_user.id,), commit=True)
    await cb_prof(c)


# ───────────────────────── Архив ─────────────────────────
@r.callback_query(F.data.startswith("arch:"))
async def cb_arch(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid, p = c.from_user.id, int(c.data.split(":")[1])
    rows = await q("SELECT chat_id, COUNT(*) n, MAX(date) d, MAX(CASE WHEN from_id!=owner THEN name END) nm "
                   "FROM messages WHERE owner=? GROUP BY chat_id ORDER BY d DESC LIMIT 8 OFFSET ?", (uid, p * 8))
    btns = [[(f"{x['nm'] or x['chat_id']} · {x['n']}", f"ac:{x['chat_id']}:0")] for x in rows]
    nav = []
    if p: nav.append(("◀️", f"arch:{p - 1}"))
    if len(rows) == 8: nav.append(("▶️", f"arch:{p + 1}"))
    await show(c, "🗂 <b>Архив переписок</b>" + ("" if rows else "\n\nПока пусто."),
               kb(*btns, nav or [("🔄", f"arch:{p}")], [("🔎 Поиск", "as")], BACK))


@r.callback_query(F.data.startswith("ac:"))
async def cb_chat(c: CallbackQuery):
    _, chat, p = c.data.split(":"); chat, p = int(chat), int(p)
    rows = await q("SELECT * FROM messages WHERE owner=? AND chat_id=? ORDER BY date DESC LIMIT 10 OFFSET ?",
                   (c.from_user.id, chat, p * 10))
    lines = [f"<i>{fmt_t(x['date'])}</i> <b>{'Вы' if x['from_id'] == x['owner'] else escape(x['name'] or '?')}</b>: "
             f"{escape(cut(x['text'], 200)) or '[' + str(x['mtype']) + ']'}" for x in reversed(rows)]
    nav = []
    if p: nav.append(("◀️ Новее", f"ac:{chat}:{p - 1}"))
    if len(rows) == 10: nav.append(("Старше ▶️", f"ac:{chat}:{p + 1}"))
    await show(c, cut("\n".join(lines) or "Пусто"), kb(nav or [("🔄", f"ac:{chat}:{p}")], [("⬅️ К архиву", "arch:0")]), cover=False)


def fmt_t(ts):
    return dt.datetime.fromtimestamp(ts + OFF * 3600, dt.timezone.utc).strftime("%d.%m %H:%M")


@r.callback_query(F.data == "as")
async def cb_as(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.search)
    await show(c, "🔎 Пришлите текст, имя или @username для поиска по архиву.", kb([("Отмена", "arch:0")]))


@r.message(S.search)
async def do_search(m: Message, state: FSMContext):
    await state.clear()
    t = f"%{(m.text or '').lstrip('@')}%"
    rows = await q("SELECT * FROM messages WHERE owner=? AND (text LIKE ? OR name LIKE ? OR username LIKE ?) "
                   "ORDER BY date DESC LIMIT 15", (m.from_user.id, t, t, t))
    lines = [f"<i>{fmt_t(x['date'])}</i> <b>{'Вы' if x['from_id'] == x['owner'] else escape(x['name'] or '?')}</b>: "
             f"{escape(cut(x['text'], 160)) or '[' + str(x['mtype']) + ']'}" for x in rows]
    await m.answer(cut("🔎 <b>Результаты</b>\n\n" + ("\n".join(lines) or "Ничего не найдено.")),
                   reply_markup=kb([("🔎 Ещё поиск", "as")], BACK))


# ───────────────────────── Автоответчик ─────────────────────────
@r.callback_query(F.data == "ar")
async def cb_ar(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rules = await q("SELECT * FROM rules WHERE owner=?", (c.from_user.id,))
    rows = [[(f"{'✅' if x['active'] else '⏸'} {'⏰' if x['kind'] == 's' else '💬'} {x['key']}", f"at:{x['id']}"),
             ("🗑", f"ad:{x['id']}")] for x in rules]
    await show(c, "🤖 <b>Автоответчик</b>\n💬 триггер — ответ на слово в сообщении\n⏰ расписание — ответ в "
                  "заданные часы (например 18-9: вечером и ночью)",
               kb(*rows, [("➕ Триггер", "aa:t"), ("➕ Расписание", "aa:s")], BACK))


@r.callback_query(F.data.startswith(("at:", "ad:")))
async def cb_rule(c: CallbackQuery, state: FSMContext):
    act, rid = c.data.split(":")
    if act == "at":
        await q("UPDATE rules SET active=1-active WHERE id=? AND owner=?", (rid, c.from_user.id), commit=True)
    else:
        await q("DELETE FROM rules WHERE id=? AND owner=?", (rid, c.from_user.id), commit=True)
    await cb_ar(c, state)


@r.callback_query(F.data.startswith("aa:"))
async def cb_add(c: CallbackQuery, state: FSMContext):
    kind = c.data.split(":")[1]
    await state.set_state(S.ar_key); await state.update_data(kind=kind)
    await show(c, "Пришлите ключевое слово/фразу:" if kind == "t" else
               f"Пришлите окно в формате <code>18-9</code> (часы, ваше время UTC+{OFF}):", kb([("Отмена", "ar")]))


@r.message(S.ar_key)
async def ar_key(m: Message, state: FSMContext):
    kind = (await state.get_data())["kind"]
    key = (m.text or "").strip()
    if kind == "s" and not re.fullmatch(r"\d{1,2}-\d{1,2}", key):
        return await m.answer("Формат: <code>18-9</code>")
    await state.update_data(key=key); await state.set_state(S.ar_text)
    await m.answer("Теперь пришлите текст ответа:")


@r.message(S.ar_text)
async def ar_text(m: Message, state: FSMContext):
    d = await state.get_data(); await state.clear()
    await q("INSERT INTO rules(owner,kind,key,text) VALUES(?,?,?,?)",
            (m.from_user.id, d["kind"], d["key"], m.text or ""), commit=True)
    await m.answer("✅ Правило добавлено.", reply_markup=kb([("🤖 К автоответчику", "ar")], BACK))


# ───────────────────────── Донаты ─────────────────────────
@r.callback_query(F.data == "don")
async def cb_don(c: CallbackQuery):
    await show(c, "💎 <b>Поддержать проект</b>\nБот бесплатный, донаты идут на сервер и развитие. Спасибо! 🙏",
               kb([("⭐ Stars", "ds")], [("💵 USDT TRC20", "dx:usdt"), ("💎 TON", "dx:ton")],
                  [("🤖 CryptoBot", "dc")], BACK))


@r.callback_query(F.data == "ds")
async def cb_ds(c: CallbackQuery):
    await show(c, "⭐ Выберите сумму:", kb([(f"{n} ⭐", f"st:{n}") for n in (50, 100, 250, 500)], [("⬅️", "don")]))


@r.callback_query(F.data.startswith("st:"))
async def cb_st(c: CallbackQuery, bot: Bot):
    n = int(c.data.split(":")[1])
    await bot.send_invoice(c.from_user.id, "Поддержка проекта", f"Донат {n} ⭐", f"don:{c.from_user.id}",
                           "XTR", [LabeledPrice(label="Донат", amount=n)], provider_token="")
    await c.answer()


@r.pre_checkout_query()
async def pre(p: PreCheckoutQuery):
    await p.answer(True)


@r.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    sp = m.successful_payment
    await add_donation(m.from_user.id, sp.total_amount, "XTR", "stars", sp.telegram_payment_charge_id)
    await m.answer("💛 Спасибо за поддержку!")
    await notify_admins(bot, f"💰 Донат {sp.total_amount} ⭐ от {m.from_user.full_name} (<code>{m.from_user.id}</code>)")


@r.callback_query(F.data.startswith("dx:"))
async def cb_dx(c: CallbackQuery):
    key = c.data.split(":")[1]
    addr = await get_set(key)
    title = "USDT (TRC20)" if key == "usdt" else "TON"
    txt = f"{title}\nАдрес:\n<code>{escape(addr)}</code>" if addr else "Реквизиты пока не заданы."
    await show(c, f"💵 <b>{txt}</b>\n\nПосле перевода нажмите «Я оплатил» — админ проверит." if addr else txt,
               kb([("✅ Я оплатил", f"pd:{key}")], [("⬅️", "don")]))


@r.callback_query(F.data.startswith("pd:"))
async def cb_pd(c: CallbackQuery, bot: Bot):
    key = c.data.split(":")[1]
    await notify_admins(bot, f"🔔 {c.from_user.full_name} (<code>{c.from_user.id}</code>) сообщает об оплате {key.upper()}.\n"
                             f"Записать: <code>/don {c.from_user.id} СУММА {key.upper()}</code>")
    await c.answer("Отправлено админу, спасибо!", show_alert=True)


async def crypto(method, **p):
    async with aiohttp.ClientSession() as s:
        async with s.post(f"https://pay.crypt.bot/api/{method}", json=p,
                          headers={"Crypto-Pay-API-Token": CRYPTO}) as resp:
            return (await resp.json())["result"]


@r.callback_query(F.data == "dc")
async def cb_dc(c: CallbackQuery):
    if not CRYPTO:
        return await c.answer("CryptoBot пока не настроен", show_alert=True)
    await show(c, "🤖 CryptoBot — выберите сумму (USDT):", kb([(f"{n}$", f"cb:{n}") for n in (1, 5, 10, 25)], [("⬅️", "don")]))


@r.callback_query(F.data.startswith("cb:"))
async def cb_cb(c: CallbackQuery):
    n = c.data.split(":")[1]
    inv = await crypto("createInvoice", asset="USDT", amount=n, description="Поддержка проекта")
    await show(c, f"Счёт на {n} USDT создан. Оплатите и нажмите «Проверить».",
               kb([("💳 Оплатить", inv.get("bot_invoice_url") or inv["pay_url"])],
                  [("🔄 Проверить", f"cc:{inv['invoice_id']}")], [("⬅️", "don")]))


@r.callback_query(F.data.startswith("cc:"))
async def cb_cc(c: CallbackQuery, bot: Bot):
    iid = c.data.split(":")[1]
    res = await crypto("getInvoices", invoice_ids=iid)
    items = res.get("items", []) if isinstance(res, dict) else res
    if items and items[0]["status"] == "paid":
        it = items[0]
        await add_donation(c.from_user.id, float(it["amount"]), it["asset"], "cryptobot", f"cb{iid}")
        await show(c, "💛 Оплата получена, спасибо!", kb(BACK))
        await notify_admins(bot, f"💰 CryptoBot {it['amount']} {it['asset']} от {c.from_user.full_name} (<code>{c.from_user.id}</code>)")
    else:
        await c.answer("Оплата пока не найдена", show_alert=True)


async def notify_admins(bot, text):
    for a in ADMINS:
        try:
            await bot.send_message(a, text)
        except Exception:
            pass


@r.message(Command("don"), F.from_user.id.in_(ADMINS))
async def cmd_don(m: Message):
    try:
        _, uid, amount, cur = m.text.split()
        await add_donation(int(uid), float(amount), cur.upper(), "manual", f"m{time.time()}")
        await m.answer("✅ Записано")
    except Exception:
        await m.answer("Формат: /don USER_ID СУММА USDT")


# ───────────────────────── Админ-панель ─────────────────────────
A = F.from_user.id.in_(ADMINS)


@r.callback_query(F.data == "adm", A)
async def cb_adm(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "🛠 <b>Админпанель</b>", kb([("📊 Статистика", "adm_s"), ("📤 Экспорт БД", "adm_e")],
                                              [("⚙️ Управление", "adm_m"), ("📣 Рассылка", "adm_b")], BACK))


@r.callback_query(F.data == "adm_s", A)
async def adm_s(c: CallbackQuery):
    n = lambda s: q(s, one=True)
    users = (await n("SELECT COUNT(*) n FROM users"))["n"]
    conn = (await n("SELECT COUNT(DISTINCT user_id) n FROM conns WHERE enabled=1"))["n"]
    msgs = (await n("SELECT COUNT(*) n FROM messages"))["n"]
    donors = (await n("SELECT COUNT(DISTINCT uid) n FROM donations"))["n"]
    sums = await q("SELECT cur, SUM(amount) s FROM donations GROUP BY cur")
    await show(c, f"📊 <b>Статистика</b>\nПользователей: {users}\nС бизнес-подключением: {conn}\n"
                  f"Сообщений в архиве: {msgs}\nДонатеров: {donors}\nСумма донатов за всё время:\n"
                  + ("\n".join(f"• {x['s']:g} {x['cur']}" for x in sums) or "• —"), kb([("⬅️", "adm")]))


@r.callback_query(F.data == "adm_e", A)
async def adm_e(c: CallbackQuery, bot: Bot):
    await bot.send_document(c.from_user.id, FSInputFile(DB, "bot_backup.db"), caption="📤 Экспорт базы данных")
    await c.answer()


SETTINGS = {"welcome": "Приветствие", "cover": "Обложка (фото/GIF)", "usdt": "Адрес USDT TRC20", "ton": "Адрес TON"}


@r.callback_query(F.data == "adm_m", A)
async def adm_m(c: CallbackQuery):
    await show(c, "⚙️ <b>Управление ботом</b>\nВыберите, что изменить:",
               kb(*[[(v, f"adm_k:{k}")] for k, v in SETTINGS.items()], [("⬅️", "adm")]))


@r.callback_query(F.data.startswith("adm_k:"), A)
async def adm_k(c: CallbackQuery, state: FSMContext):
    k = c.data.split(":")[1]
    await state.set_state(S.set_val); await state.update_data(k=k)
    cur = await get_set(k)
    await show(c, f"<b>{SETTINGS[k]}</b>\nТекущее: {'(фото)' if k == 'cover' and cur else escape(cur) or '—'}\n\n"
                  f"Пришлите новое значение{' (фото или GIF)' if k == 'cover' else ''}. "
                  f"Для сброса отправьте <code>-</code>", kb([("Отмена", "adm_m")]))


@r.message(S.set_val, A)
async def adm_set(m: Message, state: FSMContext):
    k = (await state.get_data())["k"]
    if k == "cover":
        if m.photo:
            val, ct = m.photo[-1].file_id, "photo"
        elif m.animation:
            val, ct = m.animation.file_id, "animation"
        elif m.video:
            val, ct = m.video.file_id, "video"
        else:
            val, ct = ("" if (m.text or "").strip() == "-" else None), "photo"
        if val is not None:
            await put_set("cover_type", ct)
    else:
        val = "" if (m.text or "").strip() == "-" else (m.html_text if k == "welcome" else (m.text or "").strip())
    if val is None:
        return await m.answer("Нужно прислать фото или GIF.")
    await state.clear(); await put_set(k, val)
    await m.answer("✅ Сохранено", reply_markup=kb([("⚙️ Управление", "adm_m")]))


@r.callback_query(F.data == "adm_b", A)
async def adm_b(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.bc)
    await show(c, "📣 Пришлите сообщение для рассылки (текст/фото/видео — что угодно).", kb([("Отмена", "adm")]))


@r.message(S.bc, A)
async def do_bc(m: Message, state: FSMContext, bot: Bot):
    await state.clear()
    ok = bad = 0
    for u in await q("SELECT id FROM users"):
        try:
            await m.copy_to(u["id"]); ok += 1
        except Exception:
            bad += 1
        await asyncio.sleep(0.05)
    await m.answer(f"📣 Готово: доставлено {ok}, ошибок {bad}", reply_markup=kb([("⬅️", "adm")]))


# ───────────────────────── Сервер и keep-alive ─────────────────────────
async def keepalive():
    n = 0
    while True:
        await asyncio.sleep(300)  # каждые 5 минут
        n += 1
        logging.info("keep-alive tick")
        if PUBLIC_URL:
            try:
                async with aiohttp.ClientSession() as s:
                    await s.get(PUBLIC_URL + "/health", timeout=aiohttp.ClientTimeout(total=15))
            except Exception as e:
                logging.warning("ping failed: %s", e)
        if n % 288 == 0:  # раз в сутки чистим старый архив
            await q("DELETE FROM messages WHERE date<?", (int(time.time()) - KEEP_DAYS * 86400,), commit=True)


async def main():
    await init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(); dp.include_router(r)
    app = web.Application()
    ok = lambda _: web.Response(text="ok")
    app.router.add_get("/", ok); app.router.add_get("/health", ok)
    runner = web.AppRunner(app); await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    asyncio.create_task(keepalive())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
