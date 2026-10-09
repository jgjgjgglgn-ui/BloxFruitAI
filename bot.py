import os
import sqlite3
import logging
import asyncio
import json
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================================================
# BloxFruitAI
# Main bot foundation
# Tokens/API keys are intentionally loaded from .env
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_BOT_TOKEN_HERE")
OWNER_ID_RAW = os.getenv("OWNER_ID", "0")
DB_PATH = os.getenv("DB_PATH", "bloxfruitai.db")

try:
    OWNER_ID = int(OWNER_ID_RAW)
except ValueError:
    OWNER_ID = 0

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("BloxFruitAI")

# Per-user temporary UI/session state.
# Persistent important data belongs in SQLite.
user_sessions: dict[int, dict] = {}


# ============================================================
# DATABASE
# ============================================================

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            xp INTEGER NOT NULL DEFAULT 0,
            reputation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            chat_type TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            ai_enabled INTEGER NOT NULL DEFAULT 1,
            trade_enabled INTEGER NOT NULL DEFAULT 1,
            news_enabled INTEGER NOT NULL DEFAULT 1,
            marketplace_enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            chat_id INTEGER,
            give_items TEXT NOT NULL,
            want_items TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            expires_at TEXT
        );

        CREATE TABLE IF NOT EXISTS offers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL,
            from_user_id INTEGER NOT NULL,
            give_items TEXT NOT NULL,
            want_items TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        );
        """
    )

    conn.commit()
    conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def register_user(user) -> None:
    if not user:
        return
    conn = db()
    conn.execute(
        """
        INSERT INTO users(user_id, username, first_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            updated_at=excluded.updated_at
        """,
        (user.id, user.username or "", user.first_name or "", now(), now()),
    )
    conn.commit()
    conn.close()


def register_group(chat) -> None:
    if not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    conn = db()
    conn.execute(
        """
        INSERT INTO groups(chat_id, title, chat_type, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            chat_type=excluded.chat_type,
            updated_at=excluded.updated_at
        """,
        (chat.id, chat.title or "", chat.type, now(), now()),
    )
    conn.commit()
    conn.close()


def log_action(user_id: Optional[int], action: str, details: str = "") -> None:
    conn = db()
    conn.execute(
        "INSERT INTO audit_logs(user_id, action, details, created_at) VALUES (?, ?, ?, ?)",
        (user_id, action, details, now()),
    )
    conn.commit()
    conn.close()


# ============================================================
# HELPERS
# ============================================================

def is_owner(user_id: int) -> bool:
    return OWNER_ID != 0 and user_id == OWNER_ID


async def is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False

    if is_owner(user.id):
        return True

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return False

    try:
        member = await context.bot.get_chat_member(chat.id, user.id)
        return member.status in ("administrator", "creator")
    except Exception:
        logger.exception("Could not check group admin")
        return False


def group_settings(chat_id: int) -> dict:
    conn = db()
    row = conn.execute(
        "SELECT * FROM groups WHERE chat_id = ?", (chat_id,)
    ).fetchone()
    conn.close()

    if not row:
        return {
            "enabled": 1,
            "ai_enabled": 1,
            "trade_enabled": 1,
            "news_enabled": 1,
            "marketplace_enabled": 1,
        }

    return dict(row)


async def send_menu(update: Update, text: str) -> None:
    keyboard = [
        [
            InlineKeyboardButton("📊 Values", callback_data="menu_values"),
            InlineKeyboardButton("🔄 Trade", callback_data="menu_trade"),
        ],
        [
            InlineKeyboardButton("🤝 Marketplace", callback_data="menu_market"),
            InlineKeyboardButton("📰 News", callback_data="menu_news"),
        ],
        [
            InlineKeyboardButton("👤 Profile", callback_data="menu_profile"),
        ],
        [
            InlineKeyboardButton("❓ Help", callback_data="menu_help"),
        ],
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


# ============================================================
# PLACEHOLDER DATA SERVICES
# These are deliberately isolated so real APIs/scrapers can be
# connected later without rewriting handlers.
# ============================================================

async def _http_get(url: str, timeout: int = 12) -> str:
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Android) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130 Mobile Safari/537.36"
        },
    )
    with urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())
    def text(self):
        return re.sub(r"\s+", " ", unescape(" ".join(self.parts))).strip()


def _page_text(html: str) -> str:
    parser = _TextParser()
    parser.feed(html)
    return parser.text()


def _parse_value(raw: str):
    m = re.search(r"\b(\d+(?:\.\d+)?)([KMBT])\b", str(raw), re.I)
    if not m:
        return None
    return f"{m.group(1)}{m.group(2).upper()}"


def _format_live_number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "N/A"

    if value <= 0:
        return "N/A"

    for unit, divisor in (("T", 1e12), ("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if value >= divisor:
            result = value / divisor
            formatted = f"{result:.2f}".rstrip("0").rstrip(".")
            return f"{formatted}{unit}"

    return str(int(value))


def _slugify(name: str):
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", name.lower())).strip("-")


def _number_value(raw):
    m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMBT])?\s*", str(raw), re.I)
    if not m:
        return 0.0
    value = float(m.group(1))
    multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}.get((m.group(2) or "").upper(), 1)
    return value * multiplier

def _split_items(raw: str) -> list[str]:
    return [x.strip() for x in re.split(r"\s*(?:,|\+|&|\band\b)\s*", raw, flags=re.I) if x.strip()]


def _extract_live_item(text, query, category, permanent=False):
    metadata_start = text.find(r'\"metadata\"')
    if metadata_start == -1:
        metadata_start = text.find('"metadata"')
    if metadata_start == -1:
        return None

    metadata = text[metadata_start:metadata_start + 5000]

    def get_number(key):
        patterns = (
            rf'\\"{re.escape(key)}\\"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
            rf'"{re.escape(key)}"\s*:\s*([0-9]+(?:\.[0-9]+)?)',
        )
        for pattern in patterns:
            m = re.search(pattern, metadata, re.I)
            if m:
                return float(m.group(1))
        return None

    def get_string(key):
        patterns = (
            rf'\\"{re.escape(key)}\\"\s*:\s*\\"([^"]*)\\"',
            rf'"{re.escape(key)}"\s*:\s*"([^"]*)"',
        )
        for pattern in patterns:
            m = re.search(pattern, metadata, re.I)
            if m:
                return m.group(1).strip()
        return None

    value_key = "permValue" if permanent else "regValue"
    demand_key = "permDemand" if permanent else "regDemand"
    trend_key = "permTrend" if permanent else "regTrend"

    value = get_number(value_key)
    demand = get_number(demand_key)
    trend = get_string(trend_key)

    if value is None:
        return None

    updated_m = re.search(
        r'(?:\\"|")updatedAt(?:\\"|")\s*:\s*(?:\\"|")([^"]+)',
        text,
        re.I,
    )

    return {
        "name": query,
        "value": _format_live_number(value),
        "demand": demand if demand is not None else "N/A",
        "trend": trend or "Unknown",
        "updated_at": updated_m.group(1) if updated_m else "Unknown",
        "category": category,
        "permanent": permanent,
        "source": "https://bloxfruitsvalues.com/",
    }


async def get_live_value(query: str):
    query = query.strip()
    if not query:
        return None

    q = query.lower()
    permanent = bool(re.match(r"^(perm|permanent)\s+", q))

    clean_query = re.sub(
        r"^(perm|permanent)\s+",
        "",
        query,
        flags=re.I,
    ).strip()

    aliases = {
        "dragon fruit": "dragon",
        "leopard": "tiger",
    }

    candidate = aliases.get(clean_query.lower(), clean_query)
    slug = _slugify(candidate)

    categories = ("fruits", "gamepasses", "limiteds", "skins", "perm-fruits")

    for category in categories:
        url = f"https://bloxfruitsvalues.com/values/{category}/{slug}"

        try:
            html = await _http_get(url)
            result = _extract_live_item(
                html,
                candidate,
                category,
                permanent=permanent,
            )
            if result:
                return result
        except Exception as exc:
            logging.debug("Live value fetch failed: %s", exc)

    return None


async def translate_to_uz(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return text

    chunks = []
    remaining = text
    while remaining:
        if len(remaining) <= 450:
            chunks.append(remaining)
            break
        cut = remaining.rfind(" ", 0, 450)
        if cut < 100:
            cut = 450
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()

    translated_chunks = []
    for chunk in chunks:
        try:
            q = quote(chunk)
            url = "https://api.mymemory.translated.net/get?q=" + q + "&langpair=en|uz"
            raw = await _http_get(url, 15)
            data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
            translated = data.get("responseData", {}).get("translatedText", "").strip()
            translated_chunks.append(translated or chunk)
        except Exception as exc:
            logging.warning("Translation failed: %s", exc)
            translated_chunks.append(chunk)

    return unescape(" ".join(translated_chunks))

def _strip_html(value: str) -> str:
    value = re.sub(r"<script.*?</script>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<style.*?</style>", " ", value, flags=re.I | re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", unescape(value))).strip()


async def _article_image(url: str) -> Optional[str]:
    try:
        html = await _http_get(url, 10)
        for pattern in (
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image',
        ):
            m = re.search(pattern, html, re.I)
            if m:
                return urljoin(url, unescape(m.group(1)))
    except Exception:
        pass
    return None


async def search_latest_news(query: str = "Blox Fruits latest update") -> list[dict]:
    """Fetch fresh news from the official Gamer Robot blog."""
    try:
        raw = await _http_get("https://gamerrobot.com/blogs/news", 15)
        paths = list(dict.fromkeys(re.findall(r"/blogs/news/[A-Za-z0-9_-]+", raw)))
        paths = [x for x in paths if x != "/blogs/news/tagged"]
    except Exception as exc:
        logging.warning("GamerRobot news index failed: %s", exc)
        return []
    q = "" if query.lower() in {"blox fruits latest update", "blox fruits news"} else query.lower()
    items = []
    for path in paths[:12]:
        url = urljoin("https://gamerrobot.com", path)
        try:
            page = await _http_get(url, 15)
            tm = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
            title = _strip_html(unescape(tm.group(1))) if tm else path.rsplit("/", 1)[-1]
            title = re.sub(r"\s*[–-]\s*Blox Fruits\s*$", "", title, flags=re.I).strip()
            clean = _strip_html(page)
            if q and q not in title.lower() and q not in clean.lower():
                continue
            dm = re.search(r"<time[^>]*>(.*?)</time>", page, re.I | re.S)
            published_at = _strip_html(dm.group(1)) if dm else "N/A"
            im = re.search(r"<meta[^>]+property=[" + chr(34) + chr(39) + r"]og:image[" + chr(34) + chr(39) + r"][^>]+content=[" + chr(34) + chr(39) + r"]([^" + chr(34) + chr(39) + r"]+)", page, re.I)
            image = urljoin(url, unescape(im.group(1))) if im else None
            page_content = re.sub(r"<script\b[^>]*>.*?</script>", " ", page, flags=re.I | re.S)
            page_content = re.sub(r"<style\b[^>]*>.*?</style>", " ", page_content, flags=re.I | re.S)
            paragraphs = re.findall(r"<(?:p|h2|h3)[^>]*>(.*?)</(?:p|h2|h3)>", page_content, re.I | re.S)
            parts = [_strip_html(unescape(x)) for x in paragraphs]
            parts = [x for x in parts if x]
            items.append({"title": title, "summary": " ".join(parts[:8])[:900], "image": image, "source": "Gamer Robot", "url": url, "published_at": published_at})
        except Exception as exc:
            logging.warning("GamerRobot article failed %s: %s", url, exc)
    return items[:8]


async def calculate_trade(give_items: str, want_items: str) -> str:
    async def resolve(raw):
        results = []
        for item in _split_items(raw):
            r = await get_live_value(item)
            if r:
                results.append(r)
            else:
                results.append({"name": item, "value": None, "demand": "N/A"})
        return results

    give = await resolve(give_items)
    want = await resolve(want_items)
    unknown = [x["name"] for x in give + want if not x.get("value")]
    if unknown:
        return (
            "⚠️ Quyidagi item(lar) live manbadan topilmadi:\n"
            + "\n".join(f"• {x}" for x in unknown)
            + "\n\nNomini aniqroq yozing."
        )

    give_total = sum(_number_value(x["value"]) for x in give)
    want_total = sum(_number_value(x["value"]) for x in want)
    diff = want_total - give_total
    pct = (diff / give_total * 100) if give_total else 0
    if abs(pct) <= 5:
        verdict = "🟢 FAIR"
    elif pct > 0:
        verdict = "🔵 W (siz uchun)"
    else:
        verdict = "🔴 L (siz uchun)"

    def lines(items):
        return "\n".join(f"• {x['name']}: {x['value']} | Demand {x['demand']}/10" for x in items)

    return (
        "🔄 TRADE CALCULATOR\n\n"
        f"📤 SIZ BERASIZ:\n{lines(give)}\n"
        f"💰 Jami: {give_total:,.0f}\n\n"
        f"📥 SIZ OLASIZ:\n{lines(want)}\n"
        f"💰 Jami: {want_total:,.0f}\n\n"
        f"📊 Farq: {abs(diff):,.0f} ({abs(pct):.1f}%)\n"
        f"🏷️ Natija: {verdict}\n\n"
        "⚠️ Demand va real tradeability ham muhim. Robloxdagi transferni o'yinning o'zida bajarasiz."
    )


# ============================================================
# COMMANDS
# ============================================================

HELP_TEXT = """
🤖 BloxFruitAI — Yordam

📊 VALUES
/value <item> — item value
/values — value tizimi
/search <savol> — Blox Fruits ma'lumotini qidirish

🔄 TRADING
/trade — Trade Calculator
/offer — Offer yaratish
/trades — Marketplace
/profile — Profil

📰 NEWS
/news — Eng yangi Blox Fruits yangiliklari
/update — Eng so'nggi update

🤖 AI

👑 ADMIN
/admin — Owner/Group Admin Panel

ℹ️ Barcha asosiy funksiyalar / commandlar orqali ishlaydi.
Tugmalar orqali ham boshqarish mumkin.
"""


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    register_user(user)
    register_group(chat)

    text = (
        "🤖 BloxFruitAI'ga xush kelibsiz!\n\n"
        "🍎 Live Values\n"
        "🔄 Trade Calculator\n"
        "🤝 Offers & Marketplace\n"
        "📰 Live Blox Fruits News\n"
        "🧠 Blox Fruits AI\n\n"
        "Boshlash uchun /help ni bosing."
    )
    await send_menu(update, text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_user(update.effective_user)
    register_group(update.effective_chat)
    await update.effective_message.reply_text(HELP_TEXT)


async def value_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_user(update.effective_user)
    register_group(update.effective_chat)

    query = " ".join(context.args).strip()
    if not query:
        await update.effective_message.reply_text(
            "📊 Foydalanish:\n/value Dragon\n\n"
            "Item nomini yozing."
        )
        return

    await update.effective_message.reply_text(
        f"🔎 {query} uchun eng yangi value tekshirilmoqda..."
    )

    result = await get_live_value(query)
    if not result:
        await update.effective_message.reply_text(
            "⚠️ Hozircha live value manbasi ulanmagan.\n"
            "Eski yoki taxminiy value'ni current deb ko'rsatmayman."
        )
        return

    await update.effective_message.reply_text(
        f"📊 {result.get('name', query)}\n"
        f"💰 Value: {result.get('value', 'N/A')}\n"
        f"🔥 Demand: {result.get('demand', 'N/A')}\n"
        f"📈 Trend: {result.get('trend', 'N/A')}\n"
        f"🕐 Updated: {result.get('updated_at', 'N/A')}"
    )


async def values_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📊 Values bo'limi\n\n"
        "🍎 Fruits\n"
        "♾️ Perm Fruits\n"
        "🎟️ Gamepasses\n"
        "💎 Limiteds / Skins\n\n"
        "Live source ulanishi bilan qiymatlar avtomatik yangilanadi."
    )


async def trade_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    register_user(update.effective_user)
    register_group(update.effective_chat)

    if context.args:
        raw = " ".join(context.args)
        if "|" in raw:
            give, want = [x.strip() for x in raw.split("|", 1)]
            result = await calculate_trade(give, want)
            await update.effective_message.reply_text(result)
            return

    user_sessions[update.effective_user.id] = {
        "mode": "trade",
        "step": "give",
        "give": "",
        "want": "",
    }

    await update.effective_message.reply_text(
        "🔄 Trade yaratish\n\n"
        "1️⃣ Siz beradigan itemlarni yozing.\n"
        "Masalan: Dragon + Buddha\n\n"
        "Keyin sizdan olmoqchi bo'lgan itemlaringizni so'rayman.\n\n"
        "❌ Bekor qilish: /cancel"
    )


async def offer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)
    raw = " ".join(context.args).strip()

    if not raw or "|" not in raw:
        await update.effective_message.reply_text(
            "🤝 Offer yuborish\n\n"
            "Foydalanish:\n"
            "/offer <trade_id> | <siz berasiz> | <siz olasiz>\n\n"
            "Misol:\n"
            "/offer 12 | Dragon | Kitsune\n\n"
            "Ochiq trade'larni ko'rish: /trades"
        )
        return

    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 3:
        await update.effective_message.reply_text(
            "❌ Format noto'g'ri.\n"
            "/offer 12 | Dragon | Kitsune"
        )
        return

    try:
        trade_id = int(parts[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Trade ID raqam bo'lishi kerak.")
        return

    give_items, want_items = parts[1], parts[2]
    if not give_items or not want_items:
        await update.effective_message.reply_text("❌ Beradigan va oladigan itemlarni kiriting.")
        return

    conn = db()
    trade = conn.execute(
        "SELECT * FROM trades WHERE id=? AND status='open'", (trade_id,)
    ).fetchone()

    if not trade:
        conn.close()
        await update.effective_message.reply_text("❌ Bu trade mavjud emas yoki yopilgan.")
        return

    if trade["owner_id"] == user.id:
        conn.close()
        await update.effective_message.reply_text("❌ O'zingizning trade'ingizga offer yubora olmaysiz.")
        return

    existing = conn.execute(
        "SELECT id FROM offers WHERE trade_id=? AND from_user_id=? AND status='pending'",
        (trade_id, user.id),
    ).fetchone()

    if existing:
        conn.close()
        await update.effective_message.reply_text(
            f"⚠️ Sizda bu trade uchun #{existing['id']} pending offer bor."
        )
        return

    cur = conn.execute(
        """
        INSERT INTO offers(trade_id, from_user_id, give_items, want_items, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (trade_id, user.id, give_items, want_items, now()),
    )
    offer_id = cur.lastrowid
    conn.commit()
    conn.close()

    log_action(user.id, "offer_created", f"offer={offer_id},trade={trade_id}")

    try:
        await context.bot.send_message(
            chat_id=trade["owner_id"],
            text=(
                f"🤝 Yangi Trade Offer #{offer_id}\n\n"
                f"🔄 Trade #{trade_id}\n"
                f"👤 Yuboruvchi: @{user.username or user.first_name}\n\n"
                f"📤 U beradi: {give_items}\n"
                f"📥 U oladi: {want_items}\n\n"
                f"✅ Qabul qilish: /accept {offer_id}\n"
                f"🔄 Counter: /counter {offer_id} | itemlar | itemlar\n"
                f"❌ Rad etish: /decline {offer_id}"
            ),
        )
    except Exception:
        logger.exception("Could not notify trade owner")

    await update.effective_message.reply_text(
        f"✅ Offer #{offer_id} yuborildi!\n\n"
        f"🔄 Trade: #{trade_id}\n"
        f"📤 Siz berasiz: {give_items}\n"
        f"📥 Siz olasiz: {want_items}"
    )


async def accept_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)

    if len(context.args) != 1:
        await update.effective_message.reply_text("Foydalanish: /accept <offer_id>")
        return

    try:
        offer_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Offer ID raqam bo'lishi kerak.")
        return

    conn = db()
    row = conn.execute(
        """
        SELECT o.*, t.owner_id, t.status AS trade_status
        FROM offers o
        JOIN trades t ON t.id=o.trade_id
        WHERE o.id=?
        """,
        (offer_id,),
    ).fetchone()

    if not row:
        conn.close()
        await update.effective_message.reply_text("❌ Offer topilmadi.")
        return

    if row["owner_id"] != user.id:
        conn.close()
        await update.effective_message.reply_text("⛔ Bu offerni faqat trade egasi boshqara oladi.")
        return

    if row["status"] != "pending" or row["trade_status"] != "open":
        conn.close()
        await update.effective_message.reply_text("⚠️ Bu offer endi faol emas.")
        return

    conn.execute("UPDATE offers SET status='accepted' WHERE id=?", (offer_id,))
    conn.execute(
        "UPDATE offers SET status='declined' WHERE trade_id=? AND id<>? AND status='pending'",
        (row["trade_id"], offer_id),
    )
    conn.execute("UPDATE trades SET status='accepted' WHERE id=?", (row["trade_id"],))
    conn.commit()
    conn.close()

    log_action(user.id, "offer_accepted", f"offer={offer_id}")

    try:
        await context.bot.send_message(
            chat_id=row["from_user_id"],
            text=(
                f"✅ Offer #{offer_id} QABUL QILINDI!\n\n"
                f"📤 Siz berasiz: {row['give_items']}\n"
                f"📥 Siz olasiz: {row['want_items']}\n\n"
                "⚠️ Bot faqat kelishuvni boshqaradi. Roblox trade'ni o'yin ichida o'zingiz bajaring."
            ),
        )
    except Exception:
        logger.exception("Could not notify offer sender")

    await update.effective_message.reply_text(
        f"✅ Offer #{offer_id} qabul qilindi. Trade yopildi."
    )


async def decline_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)

    if len(context.args) != 1:
        await update.effective_message.reply_text("Foydalanish: /decline <offer_id>")
        return

    try:
        offer_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Offer ID raqam bo'lishi kerak.")
        return

    conn = db()
    row = conn.execute(
        """
        SELECT o.*, t.owner_id
        FROM offers o JOIN trades t ON t.id=o.trade_id
        WHERE o.id=?
        """,
        (offer_id,),
    ).fetchone()

    if not row:
        conn.close()
        await update.effective_message.reply_text("❌ Offer topilmadi.")
        return

    if row["owner_id"] != user.id and row["from_user_id"] != user.id:
        conn.close()
        await update.effective_message.reply_text("⛔ Siz bu offerni boshqara olmaysiz.")
        return

    if row["status"] != "pending":
        conn.close()
        await update.effective_message.reply_text("⚠️ Bu offer allaqachon yopilgan.")
        return

    conn.execute("UPDATE offers SET status='declined' WHERE id=?", (offer_id,))
    conn.commit()
    conn.close()

    log_action(user.id, "offer_declined", f"offer={offer_id}")
    await update.effective_message.reply_text(f"❌ Offer #{offer_id} rad etildi.")

    target = row["from_user_id"] if row["owner_id"] == user.id else row["owner_id"]
    try:
        await context.bot.send_message(
            chat_id=target,
            text=f"❌ Trade Offer #{offer_id} rad etildi."
        )
    except Exception:
        logger.exception("Could not notify declined offer")


async def counter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)
    raw = " ".join(context.args).strip()

    if "|" not in raw:
        await update.effective_message.reply_text(
            "Foydalanish:\n/counter <offer_id> | <siz berasiz> | <siz olasiz>\n\n"
            "Misol:\n/counter 15 | Buddha + Portal | Kitsune"
        )
        return

    parts = [x.strip() for x in raw.split("|")]
    if len(parts) != 3:
        await update.effective_message.reply_text("❌ Format noto'g'ri.")
        return

    try:
        offer_id = int(parts[0])
    except ValueError:
        await update.effective_message.reply_text("❌ Offer ID raqam bo'lishi kerak.")
        return

    give_items, want_items = parts[1], parts[2]
    if not give_items or not want_items:
        await update.effective_message.reply_text("❌ Itemlarni to'liq kiriting.")
        return

    conn = db()
    old = conn.execute(
        """
        SELECT o.*, t.owner_id, t.status AS trade_status
        FROM offers o JOIN trades t ON t.id=o.trade_id
        WHERE o.id=?
        """,
        (offer_id,),
    ).fetchone()

    if not old:
        conn.close()
        await update.effective_message.reply_text("❌ Offer topilmadi.")
        return

    if user.id not in (old["owner_id"], old["from_user_id"]):
        conn.close()
        await update.effective_message.reply_text("⛔ Siz bu offerda qatnashmagansiz.")
        return

    if old["status"] != "pending" or old["trade_status"] != "open":
        conn.close()
        await update.effective_message.reply_text("⚠️ Bu offer endi faol emas.")
        return

    conn.execute("UPDATE offers SET status='countered' WHERE id=?", (offer_id,))
    cur = conn.execute(
        """
        INSERT INTO offers(trade_id, from_user_id, give_items, want_items, status, created_at)
        VALUES (?, ?, ?, ?, 'pending', ?)
        """,
        (old["trade_id"], user.id, give_items, want_items, now()),
    )
    new_id = cur.lastrowid
    conn.commit()
    conn.close()

    target = old["from_user_id"] if user.id == old["owner_id"] else old["owner_id"]

    try:
        await context.bot.send_message(
            chat_id=target,
            text=(
                f"🔄 Counter Offer #{new_id}\n\n"
                f"📤 Beradi: {give_items}\n"
                f"📥 Oladi: {want_items}\n\n"
                f"✅ /accept {new_id}\n"
                f"❌ /decline {new_id}\n"
                f"🔄 /counter {new_id} | itemlar | itemlar"
            ),
        )
    except Exception:
        logger.exception("Could not notify counter target")

    await update.effective_message.reply_text(
        f"✅ Counter Offer #{new_id} yuborildi."
    )


async def trades_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = db()
    rows = conn.execute(
        """
        SELECT id, owner_id, give_items, want_items, created_at
        FROM trades
        WHERE status='open'
        ORDER BY id DESC
        LIMIT 10
        """
    ).fetchall()
    conn.close()

    if not rows:
        await update.effective_message.reply_text(
            "🛒 Hozircha ochiq trade'lar yo'q."
        )
        return

    lines = ["🛒 Ochiq Trade'lar:\n"]
    for row in rows:
        lines.append(
            f"#{row['id']} | 📤 {row['give_items']} → 📥 {row['want_items']}"
        )

    await update.effective_message.reply_text("\n".join(lines))


async def profile_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    register_user(user)

    conn = db()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id=?", (user.id,)
    ).fetchone()
    trade_count = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE owner_id=?", (user.id,)
    ).fetchone()[0]
    conn.close()

    await update.effective_message.reply_text(
        "👤 Profil\n\n"
        f"🆔 ID: {user.id}\n"
        f"👤 Username: @{user.username}" if user.username else
        f"👤 Profil\n\n🆔 ID: {user.id}"
    )

    # Send stats separately so conditional formatting stays simple.
    await update.effective_message.reply_text(
        f"⭐ XP: {row['xp']}\n"
        f"🏆 Reputation: {row['reputation']}\n"
        f"🔄 Created trades: {trade_count}"
    )


async def news_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "📰 Eng yangi Blox Fruits yangiliklari tekshirilmoqda..."
    )

    items = await search_latest_news()
    if not items:
        await update.effective_message.reply_text(
            "⚠️ Hozircha live news engine ulanmagan.\n"
            "Eski news'ni yangi deb ko'rsatmayman."
        )
        return

    for item in items[:5]:
        title_uz = await translate_to_uz(item.get("title", "Blox Fruits Update"))
        summary_uz = await translate_to_uz(item.get("summary", ""))
        text = (
            f"🆕 {title_uz}\n\n"
            f"{summary_uz}\n\n"
            f"🕐 {item.get('published_at', 'N/A')}\n"
            f"📰 {item.get('source', 'N/A')}\n"
            f"🔗 {item.get('url', '')}"
        )
        image = item.get("image")
        try:
            if image:
                await update.effective_message.reply_photo(photo=image, caption=text[:1024])
            else:
                await update.effective_message.reply_text(text)
        except Exception:
            await update.effective_message.reply_text(text)


async def fetch_latest_update() -> dict | None:
    """Fetch the newest numbered update from the Blox Fruits Fandom API."""
    api = "https://blox-fruits.fandom.com/api.php"
    try:
        list_url = api + "?action=query&list=allpages&apprefix=Updates/&aplimit=100&format=json"
        raw = await _http_get(list_url, 15)
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        pages = data.get("query", {}).get("allpages", [])
        numbers = []
        for page in pages:
            m = re.search(r"^Updates/(\d+)$", page.get("title", ""))
            if m:
                numbers.append(int(m.group(1)))
        if not numbers:
            return None

        latest = max(numbers)
        title = f"Updates/{latest}"
        rev_url = api + "?action=query&prop=revisions&rvprop=content&rvslots=main&titles=" + quote(title) + "&format=json"
        raw_rev = await _http_get(rev_url, 15)
        rev_data = json.loads(raw_rev.decode("utf-8") if isinstance(raw_rev, bytes) else raw_rev)
        pages_data = rev_data.get("query", {}).get("pages", {})
        page = next(iter(pages_data.values()), {})
        revisions = page.get("revisions", [])
        if not revisions:
            return None
        content = revisions[0].get("slots", {}).get("main", {}).get("*", "")

        def field(name: str) -> str:
            m = re.search(r"\|\s*" + re.escape(name) + r"\s*=\s*([^\n|]+)", content, re.I)
            return _strip_html(m.group(1)).strip() if m else ""

        name = field("name") or f"Update {latest}"
        release = field("release") or "N/A"
        desc = field("desc") or ""

        lines = []
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("{|") or line.startswith("|}") or line.startswith("|-"):
                continue
            if line.startswith("|") and "=" in line:
                continue
            if line.startswith("*"):
                line = re.sub(r"^\*+\s*", "• ", line)
                line = re.sub(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", lambda m: m.group(2) or m.group(1), line)
                line = re.sub(r"\{\{[^{}]+\}\}", "", line)
                line = re.sub(r"\[\d+\]", "", line)
                line = re.sub(r"\s+", " ", line).strip()
                if line:
                    lines.append(line)

        summary = "\n".join(lines[:12])
        url = f"https://blox-fruits.fandom.com/wiki/{quote(title.replace(' ', '_'))}"
        return {"number": latest, "name": name, "release": release, "desc": desc, "summary": summary, "url": url}
    except Exception as exc:
        logging.warning("Fandom update fetch failed: %s", exc)
        return None


async def update_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("🔄 Eng yangi Blox Fruits update tekshirilmoqda...")
    item = await fetch_latest_update()
    if not item:
        await update.effective_message.reply_text("⚠️ Fandom Wiki'dan update ma'lumotini olib bo'lmadi.")
        return
    name_uz = await translate_to_uz(item["name"])
    release_uz = await translate_to_uz(item["release"])
    desc_uz = await translate_to_uz(item["desc"])
    summary_uz = await translate_to_uz(item["summary"])
    text = (
        f"🆕 Blox Fruits Update #{item['number']}\n"
        f"📌 {name_uz}\n"
        f"📅 {release_uz}\n\n"
        f"{desc_uz}\n\n"
        f"{summary_uz}\n\n"
        f"🔗 Fandom Wiki: {item['url']}"
    )
    await update.effective_message.reply_text(text[:4096])


async def search_wiki_item(query: str) -> Optional[dict]:
    api = "https://blox-fruits.fandom.com/api.php"
    search_url = api + "?action=query&list=search&srsearch=" + quote(query) + "&srlimit=5&format=json"

    try:
        data = json.loads(await _http_get(search_url, 15))
        results = data.get("query", {}).get("search", [])
        if not results:
            return None

        title = next(
            (x.get("title", "") for x in results
             if x.get("title", "").lower() == query.lower()),
            results[0].get("title", ""),
        )
        if not title:
            return None

        page_url = "https://blox-fruits.fandom.com/wiki/" + quote(title.replace(" ", "_"))
        content_url = (
            api + "?action=query&prop=revisions&rvprop=content"
            "&rvslots=main&titles=" + quote(title) + "&format=json"
        )

        raw = json.loads(await _http_get(content_url, 15))
        pages = raw.get("query", {}).get("pages", {})
        page = next(iter(pages.values()), {})
        revisions = page.get("revisions", [])

        if not revisions:
            return {"title": title, "url": page_url}

        content = revisions[0].get("slots", {}).get("main", {}).get("*", "")

        def clean(value: str) -> str:
            value = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
            value = re.sub(r"<gallery.*?>.*?</gallery>", " ", value, flags=re.S | re.I)
            value = re.sub(r"<[^>]+>", " ", value)
            value = re.sub(r"\{\{[^{}]*\}\}", " ", value)
            value = re.sub(r"\[\[([^|\]]+)\|([^\]]+)\]\]", r"\2", value)
            value = re.sub(r"\[\[([^\]]+)\]\]", r"\1", value)
            value = re.sub(r"\[https?://[^ ]+ ([^\]]+)\]", r"\1", value)
            value = re.sub(r"'''?", "", value)
            value = re.sub(r"^\s*[-*#;:]+\s*", "", value, flags=re.M)
            return re.sub(r"\s+", " ", unescape(value)).strip()

        def field(name: str) -> str:
            pattern = r"^\s*\|\s*" + re.escape(name) + r"\s*=\s*(.*?)\s*$"
            match = re.search(pattern, content, flags=re.I | re.M)
            return clean(match.group(1)) if match else ""

        def section(name: str) -> str:
            pattern = r"^==+\s*" + re.escape(name) + r"\s*==+\s*$"
            match = re.search(pattern, content, flags=re.I | re.M)
            if not match:
                return ""

            rest = content[match.end():]
            next_section = re.search(
                r"^==+\s*[^=].*?\s*==+\s*$",
                rest,
                flags=re.M,
            )
            text = rest[:next_section.start()] if next_section else rest
            return clean(text)

        buffs = field("buffs") or section("Buffs")
        money = field("money")
        robux = field("robux")
        price = field("price") or field("cost")
        if not price and (money or robux):
            parts = []
            if money:
                parts.append(f"{money} Beli")
            if robux:
                parts.append(f"{robux} Robux")
            price = " / ".join(parts)

        overview = section("Overview")
        if not overview:
            intro = content.split("==", 1)[0]
            intro = re.sub(r"^\s*\|.*$", " ", intro, flags=re.M)
            overview = clean(intro)
        if re.fullmatch(r"Pros\s*=\s*\|?\s*-\s*\|?\s*Cons\s*=\s*", overview, flags=re.I):
            overview = ""

        return {
            "title": title,
            "url": page_url,
            "rarity": field("rarity"),
            "type": field("type"),
            "sea": field("sea"),
            "location": field("location"),
            "obtain": field("obtain"),
            "source": field("source"),
            "price": price,
            "buffs": buffs,
            "obtainment": section("Obtainment"),
            "requirements": section("Requirements"),
            "overview": overview,
            "description": section("Description"),
        }

    except Exception as exc:
        logging.warning("Wiki search failed: %s", exc)
        return None


async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(context.args).strip()

    if not query:
        await update.effective_message.reply_text(
            "🔎 Foydalanish:\n/search Meva_nomi_yoki_item"
        )
        return

    await update.effective_message.reply_text(
        f"🔎 {query} haqida ma’lumot qidirilmoqda..."
    )

    item = await search_wiki_item(query)

    if not item:
        await update.effective_message.reply_text(
            f"❌ Wiki’da “{query}” bo‘yicha ma’lumot topilmadi."
        )
        return

    lines = [f"📖 {item.get('title', query)}"]

    fields = (
        ("rarity", "⭐ Noyobligi"),
        ("type", "🏷 Turi"),
        ("sea", "🌊 Dengiz"),
        ("location", "📍 Joylashuvi"),
        ("price", "💰 Narxi"),
        ("obtain", "🎯 Olish usuli"),
        ("source", "👤 Manba"),
        ("buffs", "⚡ Xususiyatlari"),
    )

    for key, label in fields:
        value = item.get(key, "")
        if value:
            lines.append(f"{label}: {value}")

    if item.get("obtainment"):
        lines.append(f"\n📌 Olish usuli:\n{item['obtainment']}")

    if item.get("requirements"):
        lines.append(f"\n📋 Kerakli shartlar:\n{item['requirements']}")

    info = item.get("overview") or item.get("description")
    if info:
        lines.append(f"\nℹ️ Qo‘shimcha ma’lumot:\n{info}")

    text = "\n".join(lines)
    text = await translate_to_uz(text)
    text += f"\n\n🔗 Batafsil manba (Wiki): {item['url']}"

    await update.effective_message.reply_text(text[:4000])


async def stock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        page = await _http_get("https://bloxfruitswiki.org/wiki/stock", timeout=20)
        lines = ["🛒 BLOX FRUITS — IKKALA STOCK"]
        for title in ("Current Stock", "Current Mirage Stock"):
            heading = re.search(
                r"<h[1-3]\b[^>]*>\s*" + re.escape(title) + r"\s*</h[1-3]>",
                page, re.I
            )
            if not heading:
                lines.extend(["", f"❌ {title}: topilmadi"])
                continue

            rest = page[heading.end():]
            nxt = re.search(r"<h[1-3]\b[^>]*>", rest, re.I)
            block = rest[:nxt.start()] if nxt else rest
            reset = re.search(r"\((\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\)", block)
            lines.extend(["", f"📦 {title}", f"🔄 Reset: {reset.group(1) if reset else 'Noma’lum'}"])

            cards = re.findall(
                r'<div style="display:flex;flex-direction:column;align-items:center;width:118px;.*?</div>',
                block, re.I | re.S
            )
            count = 0
            for card in cards:
                name_match = re.search(r'<a\b[^>]*\btitle="([^"]+)"', card, re.I)
                anchors = re.findall(r"<a\b[^>]*>(.*?)</a>", card, re.I | re.S)
                if not name_match or len(anchors) < 2:
                    continue
                name = unescape(name_match.group(1)).strip()
                display = re.sub(r"<[^>]+>", "", anchors[1])
                display = unescape(display).strip()
                rarity_match = re.search(r"<span[^>]*>(.*?)</span>", card, re.I | re.S)
                rarity = unescape(re.sub(r"<[^>]+>", "", rarity_match.group(1))).strip() if rarity_match else "Noma’lum"
                money_match = re.search(r'__money\.webp[^>]*>\s*([\d,]+)', card, re.I | re.S)
                robux_match = re.search(r'__robux\.webp[^>]*>\s*([\d,]+)', card, re.I | re.S)
                lines.append(
                    f"🍎 {name if name else display}\n"
                    f"   {rarity}\n"
                    f"   💰 {money_match.group(1) if money_match else '?'} Beli"
                    f" | 💎 {robux_match.group(1) if robux_match else '?'} Robux"
                )
                count += 1
            if count == 0:
                lines.append("❌ Mevalarni o‘qib bo‘lmadi.")

        await update.effective_message.reply_text("\n".join(lines)[:4000])
    except Exception as e:
        logging.exception("Stock error")
        await update.effective_message.reply_text(
            f"❌ Stockni olishda xatolik: {type(e).__name__}"
        )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_sessions.pop(update.effective_user.id, None)
    await update.effective_message.reply_text("❌ Joriy amal bekor qilindi.")


# ============================================================
# ADMIN PANEL
# ============================================================

def owner_panel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("👥 Groups", callback_data="owner_groups"),
                InlineKeyboardButton("📊 Stats", callback_data="owner_stats"),
            ],
            [
                InlineKeyboardButton("🔄 Value Sync", callback_data="owner_sync"),
                InlineKeyboardButton("📰 News", callback_data="owner_news"),
            ],
            [
                InlineKeyboardButton("🛡️ Logs", callback_data="owner_logs"),
            ],
            [
                InlineKeyboardButton("⚙️ Settings", callback_data="owner_settings"),
            ],
        ]
    )


def group_panel_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🤖 AI", callback_data=f"grp_ai:{chat_id}"),
                InlineKeyboardButton("🔄 Trade", callback_data=f"grp_trade:{chat_id}"),
            ],
            [
                InlineKeyboardButton("📰 News", callback_data=f"grp_news:{chat_id}"),
                InlineKeyboardButton("🛒 Market", callback_data=f"grp_market:{chat_id}"),
            ],
            [
                InlineKeyboardButton("🔌 Bot ON/OFF", callback_data=f"grp_bot:{chat_id}"),
            ],
            [
                InlineKeyboardButton("⬅️ Back", callback_data="admin_back"),
            ],
        ]
    )


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat

    register_user(user)
    register_group(chat)

    if is_owner(user.id):
        await update.effective_message.reply_text(
            "👑 BloxFruitAI — Owner Panel\n\n"
            "Bu panel orqali barcha guruhlar va global tizimlarni boshqarasiz.",
            reply_markup=owner_panel_keyboard(),
        )
        return

    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        if not await is_group_admin(update, context):
            await update.effective_message.reply_text(
                "⛔ Bu panel faqat guruh adminlari uchun."
            )
            return

        await update.effective_message.reply_text(
            f"👮 {chat.title} — Group Admin Panel",
            reply_markup=group_panel_keyboard(chat.id),
        )
        return

    await update.effective_message.reply_text(
        "⛔ Private chatda /admin faqat bot owneri uchun."
    )


# ============================================================
# CALLBACKS
# ============================================================

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user = update.effective_user
    data = query.data or ""

    # General menu
    if data == "menu_help":
        await query.edit_message_text(HELP_TEXT)
        return

    if data == "menu_values":
        await query.edit_message_text(
            "📊 Values\n\n"
            "/value <item> — aniq item value\n"
            "/values — value bo'limi"
        )
        return

    if data == "menu_trade":
        await query.edit_message_text(
            "🔄 Trade\n\n"
            "/trade — bosqichma-bosqich trade\n"
            "/trade Dragon + Buddha | Kitsune — tezkor hisob"
        )
        return

    if data == "menu_market":
        await query.edit_message_text(
            "🛒 Marketplace\n\n/trades — ochiq trade'lar\n/offer — offer tizimi"
        )
        return

    if data == "menu_news":
        await query.edit_message_text(
            "📰 News\n\n/news — eng yangi yangiliklar\n/update — update"
        )
        return


    if data == "menu_profile":
        await profile_command(update, context)
        return

    # Owner
    if data == "admin_back":
        await query.edit_message_text(
            "👑 BloxFruitAI — Owner Panel",
            reply_markup=owner_panel_keyboard(),
        )
        return

    if data.startswith("owner_"):
        if not is_owner(user.id):
            await query.edit_message_text("⛔ Ruxsat yo'q.")
            return

        if data == "owner_groups":
            conn = db()
            rows = conn.execute(
                "SELECT chat_id, title, chat_type, enabled FROM groups ORDER BY title"
            ).fetchall()
            conn.close()

            if not rows:
                await query.edit_message_text(
                    "👥 Hali hech qanday guruh ro'yxatdan o'tmagan.",
                    reply_markup=owner_panel_keyboard(),
                )
                return

            buttons = []
            for row in rows[:50]:
                state = "🟢" if row["enabled"] else "🔴"
                title = row["title"] or str(row["chat_id"])
                buttons.append(
                    [InlineKeyboardButton(
                        f"{state} {title[:35]}",
                        callback_data=f"owner_group:{row['chat_id']}"
                    )]
                )
            buttons.append(
                [InlineKeyboardButton("⬅️ Back", callback_data="admin_back")]
            )
            await query.edit_message_text(
                "👥 Guruhlar:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
            return

        if data == "owner_stats":
            conn = db()
            users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            groups = conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]
            trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            offers = conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0]
            conn.close()

            await query.edit_message_text(
                "📊 Global Statistics\n\n"
                f"👤 Users: {users}\n"
                f"👥 Groups: {groups}\n"
                f"🔄 Trades: {trades}\n"
                f"🤝 Offers: {offers}",
                reply_markup=owner_panel_keyboard(),
            )
            return

        if data == "owner_sync":
            await query.edit_message_text(
                "🔄 Value Sync\n\n"
                "Live value source adapter hali ulanmagan.\n"
                "Adapter ulangach shu tugma orqali sync ishga tushadi.",
                reply_markup=owner_panel_keyboard(),
            )
            return

        if data == "owner_news":
            await query.edit_message_text(
                "📰 News Control\n\n"
                "Live news/search adapter hali ulanmagan.\n"
                "Ulangach news freshness va image pipeline shu yerda boshqariladi.",
                reply_markup=owner_panel_keyboard(),
            )
            return


        if data == "owner_logs":
            conn = db()
            rows = conn.execute(
                "SELECT action, details, created_at FROM audit_logs ORDER BY id DESC LIMIT 10"
            ).fetchall()
            conn.close()

            if not rows:
                body = "🛡️ Hali loglar yo'q."
            else:
                body = "🛡️ Oxirgi loglar:\n\n" + "\n".join(
                    f"• {r['created_at']} — {r['action']} {r['details'] or ''}"
                    for r in rows
                )

            await query.edit_message_text(
                body, reply_markup=owner_panel_keyboard()
            )
            return

        if data == "owner_settings":
            await query.edit_message_text(
                "⚙️ Global Settings\n\n"
                "Bu bo'limga maintenance, broadcast, freshness policy "
                "va boshqa global sozlamalar ulanadi.",
                reply_markup=owner_panel_keyboard(),
            )
            return

        if data.startswith("owner_group:"):
            try:
                chat_id = int(data.split(":", 1)[1])
            except ValueError:
                await query.edit_message_text("❌ Noto'g'ri group ID.")
                return

            conn = db()
            row = conn.execute(
                "SELECT * FROM groups WHERE chat_id=?", (chat_id,)
            ).fetchone()
            conn.close()

            if not row:
                await query.edit_message_text("❌ Guruh topilmadi.")
                return

            await query.edit_message_text(
                f"👥 {row['title']}\n\n"
                f"Bot: {'🟢 ON' if row['enabled'] else '🔴 OFF'}\n"
                f"AI: {'🟢 ON' if row['ai_enabled'] else '🔴 OFF'}\n"
                f"Trade: {'🟢 ON' if row['trade_enabled'] else '🔴 OFF'}\n"
                f"News: {'🟢 ON' if row['news_enabled'] else '🔴 OFF'}\n"
                f"Market: {'🟢 ON' if row['marketplace_enabled'] else '🔴 OFF'}",
                reply_markup=group_panel_keyboard(chat_id),
            )
            return

    # Group panel
    if data.startswith("grp_"):
        if not is_owner(user.id):
            # For group admins, validate against current chat.
            if not await is_group_admin(update, context):
                await query.edit_message_text("⛔ Ruxsat yo'q.")
                return

        parts = data.split(":")
        action = parts[0]
        try:
            chat_id = int(parts[1])
        except (IndexError, ValueError):
            await query.edit_message_text("❌ Noto'g'ri group ID.")
            return

        field_map = {
            "grp_ai": "ai_enabled",
            "grp_trade": "trade_enabled",
            "grp_news": "news_enabled",
            "grp_market": "marketplace_enabled",
            "grp_bot": "enabled",
        }

        if action in field_map:
            field = field_map[action]
            conn = db()
            row = conn.execute(
                f"SELECT {field} FROM groups WHERE chat_id=?", (chat_id,)
            ).fetchone()

            if not row:
                conn.close()
                await query.edit_message_text("❌ Guruh topilmadi.")
                return

            new_value = 0 if row[field] else 1
            conn.execute(
                f"UPDATE groups SET {field}=?, updated_at=? WHERE chat_id=?",
                (new_value, now(), chat_id),
            )
            conn.commit()
            conn.close()

            await query.edit_message_text(
                f"⚙️ Sozlama yangilandi: {field} = "
                f"{'ON 🟢' if new_value else 'OFF 🔴'}",
                reply_markup=group_panel_keyboard(chat_id),
            )
            return

        await query.edit_message_text(
            "⚙️ Group Panel",
            reply_markup=group_panel_keyboard(chat_id),
        )
        return


# ============================================================
# TEXT / TRADE SESSION
# ============================================================

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.effective_message:
        return

    register_user(user)
    register_group(update.effective_chat)

    session = user_sessions.get(user.id)
    if not session:
        return

    if session.get("mode") != "trade":
        return

    text = update.effective_message.text.strip()

    if session["step"] == "give":
        session["give"] = text
        session["step"] = "want"
        await update.effective_message.reply_text(
            "2️⃣ Endi siz olmoqchi bo'lgan itemlarni yozing.\n"
            "Masalan: Kitsune + 2x Money"
        )
        return

    if session["step"] == "want":
        session["want"] = text
        give = session["give"]
        want = session["want"]
        user_sessions.pop(user.id, None)

        result = await calculate_trade(give, want)
        await update.effective_message.reply_text(result)
        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled bot error", exc_info=context.error)

    try:
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text(
                "⚠️ Texnik xatolik yuz berdi.\n"
                "Bot ishlashda davom etadi. Keyinroq qayta urinib ko'ring."
            )
    except Exception:
        logger.exception("Could not send error message")


# ============================================================
# POST INIT
# ============================================================

async def post_init(application: Application) -> None:
    commands = [
        BotCommand("start", "Botni boshlash"),
        BotCommand("help", "Barcha yordam"),
        BotCommand("value", "Item value"),
        BotCommand("values", "Values bo'limi"),
        BotCommand("trade", "Trade Calculator"),
        BotCommand("offer", "Offer"),
        BotCommand("trades", "Marketplace"),
        BotCommand("profile", "Profil"),
        BotCommand("news", "Eng yangi news"),
        BotCommand("update", "Eng yangi update"),
        BotCommand("search", "Live qidiruv"),
        BotCommand("stock", "Current Fruit Stock"),
        BotCommand("ai", "Blox Fruits AI"),
        BotCommand("admin", "Admin Panel"),
        BotCommand("cancel", "Joriy amalni bekor qilish"),
    ]
    await application.bot.set_my_commands(commands)
    logger.info("BloxFruitAI bot commands configured.")


# ============================================================
# MAIN
# ============================================================

def main() -> None:
    if BOT_TOKEN == "PUT_BOT_TOKEN_HERE" or not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN .env fayliga qo'yilmagan. "
            "Masalan: BOT_TOKEN=123456:ABC..."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("value", value_command))
    application.add_handler(CommandHandler("values", values_command))
    application.add_handler(CommandHandler("trade", trade_command))
    application.add_handler(CommandHandler("offer", offer_command))
    application.add_handler(CommandHandler("accept", accept_command))
    application.add_handler(CommandHandler("decline", decline_command))
    application.add_handler(CommandHandler("counter", counter_command))
    application.add_handler(CommandHandler("trades", trades_command))
    application.add_handler(CommandHandler("profile", profile_command))
    application.add_handler(CommandHandler("news", news_command))
    application.add_handler(CommandHandler("update", update_command))
    application.add_handler(CommandHandler("search", search_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("stock", stock_command))

    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )

    application.add_error_handler(error_handler)

    logger.info("🚀 BloxFruitAI starting...")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
