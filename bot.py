import asyncio
import json
import logging
import random
import re
from datetime import datetime, date, timedelta, UTC
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PhotoSize,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

try:
    # Best-effort: prefer library if available
    from telegramify_markdown import markdownify as tg_markdownify  # type: ignore
except Exception:  # pragma: no cover - fallback will be used
    tg_markdownify = None  # type: ignore


# ---------- Configuration ----------
try:
    import config  # type: ignore
except ImportError as e:  # pragma: no cover
    raise SystemExit(
        "config.py not found. Create config.py based on README instructions."
    ) from e


SETTINGS_FILE = "settings.json"
STATS_FILE = "stats.json"


# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("news-bot")


# ---------- Globals (in-memory state) ----------
settings: Dict[str, Any] = {}
stats: Dict[str, Any] = {}

# Keep processed and posted identifiers per day
processed_source_keys: set[str] = set()
posted_news_keys: set[str] = set()

pending_news: Dict[str, Dict[str, Any]] = {}

# Media group aggregation for auto channel posts: media_group_id -> list[Message]
media_groups_buffer: Dict[str, List[Message]] = {}
media_groups_tasks: Dict[str, asyncio.Task] = {}

# Media group aggregation for forwarded posts in manual mode
fwd_groups_buffer: Dict[str, List[Message]] = {}
fwd_groups_tasks: Dict[str, asyncio.Task] = {}

# Publication counters per day
published_today_count: int = 0
last_reset_date: date = date.today()


# ---------- Utilities ----------
MDV2_SPECIAL = "_[]()~`>#+-=|{}.!*"  # include * at the end for safety


def escape_markdown_v2(text: str) -> str:
    if not text:
        return ""
    escaped = []
    for ch in text:
        if ch in MDV2_SPECIAL:
            escaped.append("\\" + ch)
        else:
            escaped.append(ch)
    return "".join(escaped)


def mdv2(text: str) -> str:
    """Convert text to Telegram MarkdownV2-safe string.
    If telegramify-markdown is available, use it; otherwise fallback to local escape."""
    if tg_markdownify:
        try:
            # Use default behavior of telegramify_markdown which targets Telegram MarkdownV2
            return tg_markdownify(text)  # type: ignore[arg-type]
        except Exception:
            return escape_markdown_v2(text)
    return escape_markdown_v2(text)


def format_markdown_caption(title_raw: str, text_raw: str) -> str:
    """Build a single Markdown string and convert ONCE via telegramify_markdown to MarkdownV2.
    This prevents mismatched escaping between parts and avoids entity errors in Telegram.
    """
    title_md = (title_raw or "").strip()
    text_md = (text_raw or "").strip()
    if title_md and not re.search(r"[*_]", title_md):
        title_md = f"**{title_md}**"
    combined_md = (
        f"{title_md}\n\n{text_md}" if title_md and text_md else (title_md or text_md)
    )
    # markdownify already outputs text suitable for Telegram MarkdownV2
    return mdv2(combined_md)


async def _send_with_md_fallback(bot: Bot, chat_id: int, *, caption: str, photo_file_id: Optional[str] = None, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
    """Send message/photo with MarkdownV2; if Telegram can't parse entities, fallback to plain text."""
    try:
        if photo_file_id:
            await bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
        else:
            await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        msg = str(e).lower()
        if "can't parse entities" in msg or "parse entities" in msg:
            # Fallback: send without parse_mode (plain text)
            if photo_file_id:
                await bot.send_photo(chat_id=chat_id, photo=photo_file_id, caption=re.sub(r"\\([_\\[\\]\\(\\)~`>#+\-=|{}.!*])", r"\1", caption), reply_markup=reply_markup)
            else:
                await bot.send_message(chat_id=chat_id, text=re.sub(r"\\([_\\[\\]\\(\\)~`>#+\-=|{}.!*])", r"\1", caption), reply_markup=reply_markup)
        else:
            raise


def load_settings() -> Dict[str, Any]:
    default_settings: Dict[str, Any] = {
        "WHITELIST_SOURCE_CHANNEL_IDS": [],  # e.g. [-100123, -100456]
        "WHITELIST_TARGET_CHANNEL_IDS": [],
        # Deprecated timing fields kept for backward-compat but unused
        "mode": "interval",
        "daily_limit": 10,
        "min_interval": 30,
        "max_interval": 90,
        "auto_approve": True,
        # removed: old news_type/scheduler configs
        # New delay settings (minutes). If delay_max > delay_min, random in range
        "delay_min": 0,
        "delay_max": 0,
        # Heartbeat frequency for publishing queue (seconds)
        "heartbeat_seconds": 10,
    }
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            default_settings.update(data)
            logger.info("Settings loaded from settings.json")
            return default_settings
    except FileNotFoundError:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_settings, f, ensure_ascii=False, indent=2)
        logger.info("settings.json created with defaults")
        return default_settings
    except json.JSONDecodeError as e:
        logger.error("Invalid settings.json, using defaults: %s", e)
        return default_settings


def load_stats() -> Dict[str, Any]:
    default_stats = {"published_today": 0, "total_published": 0, "last_reset_date": date.today().isoformat()}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            default_stats.update(data)
            return default_stats
    except FileNotFoundError:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(default_stats, f, ensure_ascii=False, indent=2)
        return default_stats
    except json.JSONDecodeError as e:
        logger.error("Invalid stats.json, using defaults: %s", e)
        return default_stats


def save_settings() -> None:
    to_save = dict(settings)
    # Do not persist runtime-only counters
    for k in ["_runtime"]:
        to_save.pop(k, None)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    logger.info("Settings saved")


def save_stats() -> None:
    to_save = dict(stats)
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)
    logger.info("Stats saved")


def reset_daily_counters_if_needed() -> None:
    global published_today_count, last_reset_date
    today = date.today()
    if today != last_reset_date:
        published_today_count = 0
        last_reset_date = today
        posted_news_keys.clear()
        logger.info("Daily counters reset")
        # Persist stats reset
        stats["published_today"] = 0
        stats["last_reset_date"] = today.isoformat()
        save_stats()


def build_source_key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def choose_largest_photo(photos: List[PhotoSize]) -> Optional[str]:
    if not photos:
        return None
    # photos are sorted by size in Telegram: choose the last (largest)
    return photos[-1].file_id


def filter_by_news_type(_: str, __: str) -> bool:
    # Deprecated: always pass
    return True


def is_probable_ad(text: str) -> bool:
    """Heuristic detection of ads/sponsored posts.
    This is conservative: if triggered, we treat the post as advertisement and skip by default.
    """
    lower = (text or "").lower()
    ad_markers = [
        "партнерский материал",
        "партнёрский материал",
        "на правах рекламы",
        "спонсор",
        "спонсировано",
        "advertisement",
        "sponsored",
        "ad:\n",
        "ad:",
        "promo",
        "промо",
        "продвижение",
        "affiliate",
        "реферальная ссылка",
        "реферальный",
    ]
    if any(k in lower for k in ad_markers):
        return True
    # If too many URLs and little text
    urls = re.findall(r"https?://\S+", lower)
    words = re.findall(r"\w+", lower)
    if len(urls) >= 2 and len(words) < 30:
        return True
    return False


# ---------- SambaNova Integration ----------
def _extract_json_from_content(content: str) -> Optional[Dict[str, str]]:
    """Try multiple strategies to get JSON object with title/text from model content."""
    if not content:
        return None
    # 1) Try plain parse
    try:
        obj = json.loads(content)
        if isinstance(obj, dict):
            return {"title": str(obj.get("title", "")), "text": str(obj.get("text", ""))}
    except Exception:
        pass

    # 2) Try fenced blocks ```json ... ``` or ``` ... ```
    fence_patterns = [
        r"```json\s*([\s\S]*?)\s*```",
        r"```\s*([\s\S]*?)\s*```",
    ]
    for pat in fence_patterns:
        m = re.search(pat, content, re.IGNORECASE)
        if m:
            snippet = m.group(1).strip()
            try:
                obj = json.loads(snippet)
                if isinstance(obj, dict):
                    return {"title": str(obj.get("title", "")), "text": str(obj.get("text", ""))}
            except Exception:
                continue

    # 3) Find first {...} block
    start = content.find("{")
    end = content.rfind("}")
    if start != -1 and end != -1 and end > start:
        json_str = content[start : end + 1]
        try:
            obj = json.loads(json_str)
            if isinstance(obj, dict):
                return {"title": str(obj.get("title", "")), "text": str(obj.get("text", ""))}
        except Exception:
            return None
    return None


async def sambanova_rewrite(session: aiohttp.ClientSession, text: str, news_type: str, *, _attempt: int = 1) -> Optional[Dict[str, str]]:
    """Call SambaNova Chat Completions API to rewrite text.

    Returns dict with keys: title, text — already formatted in MarkdownV2 style (we will escape anyway).
    """
    url = getattr(config, "SAMBANOVA_API_URL", "https://api.sambanova.ai/v1/chat/completions")
    api_key = getattr(config, "SAMBANOVA_API_KEY", None)
    model = getattr(config, "SAMBANOVA_MODEL", "DeepSeek-R1")

    if not api_key:
        logger.error("SambaNova API key not configured")
        return None

    system_prompt = (
        "Ты – редактор новостей для геймерского канала. Ты должен перефразировать текст,"
        " с юмором для геймеров и мягкой бранью, но без оскорблений."
        " Оформи в обычном Markdown (НЕ MarkdownV2): заголовок жирным (**...**).Не злоупоетребляй с курсивом"
        " Ответь СТРОГО валидным JSON БЕЗ какого-либо дополнительного текста, без Markdown-ограждений,"
        " без комментариев, без подсказок. Формат: {\"title\": string, \"text\": string}."
    )
    user_prompt = (
        f"Исходный текст:\n{text}\n\n"
        "Только JSON без лишнего текста. Если в исходной новости была ссылка — включи её в переписанный текст."
    )


    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "stream": False,
        "temperature": 0.2,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with session.post(url, headers=headers, json=payload, timeout=30) as resp:
            if resp.status == 403:
                logger.error("SambaNova returned 403. Region blocked? Use VPN or contact support.")
                return None
            if resp.status == 429:
                logger.error("SambaNova rate limited (429). Try again later.")
                return None
            if resp.status >= 400:
                txt = await resp.text()
                logger.error("SambaNova HTTP %s: %s", resp.status, txt)
                return None
            data = await resp.json()
    except aiohttp.ClientError as e:
        logger.error("SambaNova request failed: %s", e)
        return None
    except asyncio.TimeoutError:
        logger.error("SambaNova request timeout")
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error("Unexpected SambaNova response format: %s", e)
        return None

    parsed = _extract_json_from_content(content)
    if parsed is not None:
        return parsed

    # One retry with extra strict instruction
    if _attempt < 2:
        strict_messages = [
            {"role": "system", "content": system_prompt + " Верни только JSON без лишнего текста."},
            {"role": "user", "content": user_prompt},
        ]
        try:
            async with session.post(
                url, headers=headers, json={"model": model, "messages": strict_messages, "max_tokens": 500, "stream": False, "temperature": 0.2}, timeout=30
            ) as resp2:
                if resp2.status >= 400:
                    txt = await resp2.text()
                    logger.error("SambaNova retry HTTP %s: %s", resp2.status, txt)
                    return None
                data2 = await resp2.json()
                content2 = data2["choices"][0]["message"]["content"]
                parsed2 = _extract_json_from_content(content2)
                if parsed2 is not None:
                    return parsed2
        except Exception as e:
            logger.error("SambaNova retry failed: %s", e)

    logger.error("JSONDecodeError from SambaNova content: could not extract JSON. Content snippet: %s", (content[:200] + "...") if len(content) > 200 else content)
    return None


# ---------- Telegram Bot ----------
router = Router()


class AdminStates(StatesGroup):
    waiting_add_source = State()
    waiting_remove_source = State()
    waiting_add_target = State()
    waiting_remove_target = State()
    waiting_forwards = State()
    waiting_userbot_channel = State()  # Новое состояние
    waiting_userbot_bot_chat = State()  # Новое состояние
async def prepare_and_queue_item(
    bot: Bot,
    src_chat_id: int,
    src_message_id: int,
    source_key: str,
    text: str,
    media: List[Dict[str, Any]],
    notify: Optional[Message] = None,
) -> None:
    # Rewrite via AI
    logger.info("[QUEUE] Preparing item source_key=%s media=%s", source_key, [m.get("type") for m in media])
    async with aiohttp.ClientSession() as session:
        rewritten = await sambanova_rewrite(session, text, "all")
    if not rewritten:
        logger.error("[QUEUE] AI rewrite failed for source %s", source_key)
        if notify:
            await notify.answer("Не удалось обработать AI")
        return

    item_id = f"{source_key}"
    item = {
        "id": item_id,
        "source_key": source_key,
        "source_chat_id": src_chat_id,
        "source_message_id": src_message_id,
        "raw_text": text,
        "title": rewritten.get("title", ""),
        "text": rewritten.get("text", ""),
        "media": media,
        "created_at": datetime.now(UTC).isoformat(),
        "ready_at": (datetime.now(UTC) + timedelta(
            minutes=random.uniform(float(settings.get("delay_min", 0)), float(settings.get("delay_max", 0)))
        )).isoformat(),
    }
    pending_news[item_id] = item
    logger.info("[QUEUE] Prepared item id=%s ready_at=%s targets=%d", item_id, item["ready_at"], len(settings.get("WHITELIST_TARGET_CHANNEL_IDS", [])))
    if notify:
        await notify.answer("Новость подготовлена")

    if not settings.get("auto_approve", True):
        await send_admin_preview(bot, item)
    # If delay disabled (0-0), trigger immediate publish attempt
    if float(settings.get("delay_min", 0)) == 0 and float(settings.get("delay_max", 0)) == 0:
        logger.info("[QUEUE] Delay disabled, triggering immediate publish for %s", item_id)
        await publish_news_item(bot, item)


async def process_messages_as_one(bot: Bot, items: List[Message], source_chat_id: int) -> None:
    """Aggregate media-group messages from a channel into single queue item preserving order."""
    if not items:
        return
    items_sorted = sorted(items, key=lambda m: m.message_id)
    mgid = items_sorted[0].media_group_id or f"solo-{items_sorted[0].message_id}"
    # Determine text: first caption/text available
    text = ""
    for m in items_sorted:
        if m.caption:
            text = m.caption
            break
        if m.text:
            text = m.text
            break
    media: List[Dict[str, Any]] = []
    for m in items_sorted:
        if m.photo:
            media.append({"type": "photo", "file_id": choose_largest_photo(m.photo)})
        if getattr(m, "video", None):
            media.append({"type": "video", "file_id": m.video.file_id})
    # Mark all messages processed
    for m in items_sorted:
        processed_source_keys.add(build_source_key(source_chat_id, m.message_id))
    source_key = f"{source_chat_id}:mg:{mgid}"
    await prepare_and_queue_item(bot, source_chat_id, items_sorted[0].message_id, source_key, text, media)


async def process_forwarded_as_one(bot: Bot, items: List[Message], notify: Optional[Message]) -> None:
    if not items:
        return
    items_sorted = sorted(items, key=lambda m: m.message_id)
    # Extract origin
    src_id: Optional[int] = None
    for m in items_sorted:
        try:
            if getattr(m, "forward_origin", None) and getattr(m.forward_origin, "chat", None):
                src_id = m.forward_origin.chat.id
                break
            if getattr(m, "forward_from_chat", None):
                src_id = m.forward_from_chat.id
                break
        except Exception:
            pass
    if src_id is None:
        # Cannot determine origin; use admin id as pseudo-source
        src_id = notify.chat.id if notify else 0
    mgid = items_sorted[0].media_group_id or f"fwd-{items_sorted[0].message_id}"
    text = ""
    for m in items_sorted:
        if m.caption:
            text = m.caption
            break
        if m.text:
            text = m.text
            break
    media: List[Dict[str, Any]] = []
    for m in items_sorted:
        if m.photo:
            media.append({"type": "photo", "file_id": choose_largest_photo(m.photo)})
        if getattr(m, "video", None):
            media.append({"type": "video", "file_id": m.video.file_id})
    source_key = f"{src_id}:mg:{mgid}"
    await prepare_and_queue_item(bot, src_id, items_sorted[0].message_id, source_key, text, media, notify=notify)


# ---- Validation and fallback ----
_BAD_SNIPPETS = [
    "заголовок новости",
    "текст новости",
    "markdown",
    "json",
    "пример",
    "шаблон",
    "placeholder",
    "курсив",
    "жирн",
]


def _looks_bad(text: str) -> bool:
    low = (text or "").lower()
    return any(sn in low for sn in _BAD_SNIPPETS)


def validate_rewrite(candidate: Dict[str, str], source: str) -> bool:
    title = (candidate.get("title") or "").strip()
    body = (candidate.get("text") or "").strip()
    if len(title) < 3 or len(title) > 160:
        return False
    if len(body) < 20:
        return False
    if _looks_bad(title) or _looks_bad(body):
        return False
    # Basic overlap: require minimal lexical overlap to avoid generic placeholders
    src_tokens = {t for t in re.findall(r"[\w\-]{6,}", source.lower())}
    out_tokens = {t for t in re.findall(r"[\w\-]{6,}", body.lower())}
    if src_tokens and out_tokens and len(src_tokens.intersection(out_tokens)) == 0:
        out_title_tokens = {t for t in re.findall(r"[\w\-]{6,}", title.lower())}
        if len(src_tokens.intersection(out_title_tokens)) == 0:
            return False
    return True


def fallback_rewrite_from_source(source: str) -> Dict[str, str]:
    text = (source or "").strip()
    no_urls = re.sub(r"https?://\S+", "", text).strip()
    sentences = re.split(r"(?<=[.!?…])\s+", no_urls)
    title = sentences[0][:80].strip(" -—:;,.\n\t") if sentences else no_urls[:80]
    if len(title) < 10 and len(no_urls) > 10:
        title = no_urls[:80]
    body = re.sub(r"\s+", " ", text)[:800]
    tail = "\n\n_Короче, будет жарко — держим руку на пульсе._"
    return {"title": title, "text": body + tail}


def admin_only(message: Message) -> bool:
    return message.from_user and message.from_user.id == getattr(config, "ADMIN_USER_ID", 0)


def build_main_menu() -> InlineKeyboardMarkup:
    """Строит главное меню с красивым оформлением"""
    kb = InlineKeyboardBuilder()
    
    # Основные настройки
    kb.button(text="⚙️ Настройки", callback_data="submenu_settings")
    kb.button(text="📡 Каналы", callback_data="submenu_channels")
    
    # Управление и мониторинг
    kb.button(text="📊 Статистика", callback_data="stats")
    kb.button(text="🔄 Обновить", callback_data="refresh")
    
    kb.adjust(2)
    return kb.as_markup()


def build_settings_menu() -> InlineKeyboardMarkup:
    """Строит меню настроек"""
    kb = InlineKeyboardBuilder()
    
    # Основные настройки
    auto_status = "🟢 Вкл" if settings.get("auto_approve", True) else "🔴 Выкл"
    delay_status = "🟢 Вкл" if (settings.get("delay_min", 0) or settings.get("delay_max", 0)) else "🔴 Выкл"
    
    kb.button(text=f"✅ Автоутверждение: {auto_status}", callback_data="toggle_auto")
    kb.button(text=f"⏰ Задержка публикации: {delay_status}", callback_data="delay_menu")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="back_main")
    
    kb.adjust(1)
    return kb.as_markup()


def build_channels_menu() -> InlineKeyboardMarkup:
    """Строит меню управления каналами"""
    kb = InlineKeyboardBuilder()
    
    # Управление источниками
    kb.button(text="📥 Источники новостей", callback_data="submenu_sources")
    kb.button(text="📤 Целевые каналы", callback_data="submenu_targets")
    
    # UserBot интеграция
    kb.button(text="🤖 UserBot каналы", callback_data="userbot_channels")
    
    # Ручной парсинг
    kb.button(text="📝 Ручной парсинг", callback_data="manual_parse")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="back_main")
    
    kb.adjust(2)
    return kb.as_markup()


def build_sources_menu() -> InlineKeyboardMarkup:
    """Строит меню управления источниками"""
    kb = InlineKeyboardBuilder()
    
    # Управление источниками
    kb.button(text="➕ Добавить источник", callback_data="add_source")
    kb.button(text="➖ Убрать источник", callback_data="remove_source")
    
    # Просмотр
    kb.button(text="👁️ Показать источники", callback_data="show_sources")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="submenu_channels")
    
    kb.adjust(2)
    return kb.as_markup()


def build_targets_menu() -> InlineKeyboardMarkup:
    """Строит меню управления целевыми каналами"""
    kb = InlineKeyboardBuilder()
    
    # Управление целями
    kb.button(text="➕ Добавить цель", callback_data="add_target")
    kb.button(text="➖ Убрать цель", callback_data="remove_target")
    
    # Просмотр
    kb.button(text="👁️ Показать цели", callback_data="show_targets")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="submenu_channels")
    
    kb.adjust(2)
    return kb.as_markup()


def build_delay_menu() -> InlineKeyboardMarkup:
    """Строит меню настройки задержки"""
    kb = InlineKeyboardBuilder()
    
    # Текущие настройки
    current_min = settings.get('delay_min', 0)
    current_max = settings.get('delay_max', 0)
    kb.button(text=f"⏱️ Текущая задержка: {current_min}-{current_max} мин", callback_data="noop")
    
    # Быстрые пресеты
    kb.button(text="🚫 Без задержки", callback_data="set_delay:0-0")
    kb.button(text="⚡ Быстро (2-5 мин)", callback_data="set_delay:2-5")
    kb.button(text="🐌 Средне (5-10 мин)", callback_data="set_delay:5-10")
    kb.button(text="🐌 Медленно (10-15 мин)", callback_data="set_delay:10-15")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="submenu_settings")
    
    kb.adjust(2)
    return kb.as_markup()


def build_userbot_channels_menu() -> InlineKeyboardMarkup:
    """Строит меню управления каналами UserBot"""
    kb = InlineKeyboardBuilder()
    
    # Управление каналами
    kb.button(text="➕ Добавить канал", callback_data="userbot_add_channel")
    kb.button(text="➖ Убрать канал", callback_data="userbot_remove_channel")
    kb.button(text="📋 Список каналов", callback_data="userbot_list_channels")
    
    # Настройки UserBot
    kb.button(text="🔧 Настройки UserBot", callback_data="userbot_settings")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="submenu_channels")
    
    kb.adjust(2)
    return kb.as_markup()


def build_userbot_settings_menu() -> InlineKeyboardMarkup:
    """Строит меню настроек UserBot"""
    kb = InlineKeyboardBuilder()
    
    # Настройки
    kb.button(text="🤖 Установить чат бота", callback_data="userbot_set_bot")
    kb.button(text="📊 Статус UserBot", callback_data="userbot_status")
    
    # Навигация
    kb.button(text="⬅️ Назад", callback_data="userbot_channels")
    
    kb.adjust(1)
    return kb.as_markup()


async def show_channels_overview(message: Message, bot: Bot) -> None:
    sources = settings.get("WHITELIST_SOURCE_CHANNEL_IDS", [])
    targets = settings.get("WHITELIST_TARGET_CHANNEL_IDS", [])
    text = (
        "Каналы:\n"
        f"Источники ({len(sources)}): {', '.join(map(str, sources)) or '—'}\n"
        f"Цели ({len(targets)}): {', '.join(map(str, targets)) or '—'}\n\n"
        "Можно добавлять ID (например, -100123...) или @username — бот попробует разрешить в ID."
    )
    await message.answer(mdv2(text), parse_mode="MarkdownV2")


async def resolve_chat_id(bot: Bot, raw: str) -> Optional[int]:
    raw = raw.strip()
    if not raw:
        return None
    # Numeric ID
    try:
        return int(raw)
    except ValueError:
        pass
    # Try resolve via username or invite link
    try:
        chat = await bot.get_chat(raw)
        return chat.id
    except Exception:
        return None


async def send_admin_menu(message: Message) -> None:
    text = (
        "🤖 **Панель управления ботом**\n\n"
        f"✅ **Автоутверждение:** {'🟢 Включено' if settings.get('auto_approve', True) else '🔴 Отключено'}\n"
        f"📥 **Источники:** {len(settings.get('WHITELIST_SOURCE_CHANNEL_IDS', []) )}\n"
        f"📤 **Целевые каналы:** {len(settings.get('WHITELIST_TARGET_CHANNEL_IDS', []))}\n\n"
        "Выберите нужный раздел в меню ниже 👇"
    )
    await message.answer(mdv2(text), reply_markup=build_main_menu(), parse_mode="MarkdownV2")


# ---------- Handlers ----------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    if not admin_only(message):
        await message.answer("Доступ запрещён.")
        return
    await send_admin_menu(message)


@router.message(Command("set_min"))
async def cmd_set_min(message: Message) -> None:
    if not admin_only(message):
        return
    await message.answer("Настройки интервалов удалены")


@router.message(Command("set_max"))
async def cmd_set_max(message: Message) -> None:
    if not admin_only(message):
        return
    await message.answer("Настройки интервалов удалены")


@router.message(Command("set_limit"))
async def cmd_set_limit(message: Message) -> None:
    if not admin_only(message):
        return
    await message.answer("Лимиты по дню удалены")


@router.callback_query(F.data == "toggle_auto")
async def cb_toggle_auto(call: CallbackQuery) -> None:
    await call.answer("Переключаю…", cache_time=1)
    settings["auto_approve"] = not settings.get("auto_approve", True)
    save_settings()
    try:
        await call.message.edit_reply_markup(reply_markup=build_settings_menu())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "switch_mode")
async def cb_switch_mode(call: CallbackQuery) -> None:
    await call.answer("Режимы удалены", cache_time=1)


@router.callback_query(F.data == "submenu_type")
async def cb_submenu_type(call: CallbackQuery) -> None:
    await call.answer("Типы новостей удалены", cache_time=1)


@router.callback_query(F.data.startswith("set_type:"))
async def cb_set_type(call: CallbackQuery) -> None:
    await call.answer("Типы новостей удалены", cache_time=1)


@router.callback_query(F.data == "submenu_interval")
async def cb_submenu_interval(call: CallbackQuery) -> None:
    await call.answer("Интервалы отключены", cache_time=1)


@router.callback_query(F.data.startswith("set_min:"))
async def cb_set_min(call: CallbackQuery) -> None:
    await call.answer("Интервалы отключены", cache_time=1)


@router.callback_query(F.data.startswith("set_max:"))
async def cb_set_max(call: CallbackQuery) -> None:
    await call.answer("Интервалы отключены", cache_time=1)


@router.callback_query(F.data == "submenu_limit")
async def cb_submenu_limit(call: CallbackQuery) -> None:
    await call.answer("Лимит/день отключён", cache_time=1)


@router.callback_query(F.data.startswith("set_limit:"))
async def cb_set_limit_cb(call: CallbackQuery) -> None:
    await call.answer("Лимиты отключены", cache_time=1)


@router.callback_query(F.data == "back_main")
async def cb_back_main(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_main_menu())


@router.callback_query(F.data == "refresh")
async def cb_refresh(call: CallbackQuery) -> None:
    await call.answer("Обновлено", cache_time=1)
    try:
        await call.message.edit_reply_markup(reply_markup=build_main_menu())
    except TelegramBadRequest:
        pass


@router.callback_query(F.data == "stats")
async def cb_stats(call: CallbackQuery) -> None:
    await call.answer("📊 Статистика загружена", cache_time=1)
    text = (
        "📊 **Статистика бота**\n\n"
        f"📅 **Сегодня:** {stats.get('published_today', 0)} публикаций\n"
        f"📈 **Всего:** {stats.get('total_published', 0)} публикаций\n"
        f"⏳ **В очереди:** {len(pending_news)} новостей\n"
        f"📥 **Источники:** {len(settings.get('WHITELIST_SOURCE_CHANNEL_IDS', []))} каналов\n"
        f"📤 **Целевые каналы:** {len(settings.get('WHITELIST_TARGET_CHANNEL_IDS', []))} каналов"
    )
    await call.message.answer(mdv2(text), parse_mode="MarkdownV2")


@router.callback_query(F.data == "submenu_settings")
async def cb_submenu_settings(call: CallbackQuery) -> None:
    if call.from_user.id != getattr(config, "ADMIN_USER_ID", 0):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_settings_menu())


@router.callback_query(F.data == "submenu_channels")
async def cb_submenu_channels(call: CallbackQuery, bot: Bot) -> None:
    if call.from_user.id != getattr(config, "ADMIN_USER_ID", 0):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer()
    await show_channels_overview(call.message, bot)
    await call.message.edit_reply_markup(reply_markup=build_channels_menu())


@router.callback_query(F.data == "submenu_sources")
async def cb_submenu_sources(call: CallbackQuery) -> None:
    if call.from_user.id != getattr(config, "ADMIN_USER_ID", 0):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_sources_menu())


@router.callback_query(F.data == "submenu_targets")
async def cb_submenu_targets(call: CallbackQuery) -> None:
    if call.from_user.id != getattr(config, "ADMIN_USER_ID", 0):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_targets_menu())


@router.callback_query(F.data == "show_sources")
async def cb_show_sources(call: CallbackQuery) -> None:
    if call.from_user.id != getattr(config, "ADMIN_USER_ID", 0):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer()
    sources = settings.get("WHITELIST_SOURCE_CHANNEL_IDS", [])
    if sources:
        text = f"📥 **Источники новостей ({len(sources)}):**\n" + "\n".join([f"• `{source}`" for source in sources])
    else:
        text = "📥 **Источники новостей:**\nНет добавленных источников"
    
    await call.message.answer(mdv2(text), parse_mode="MarkdownV2")
    await call.message.edit_reply_markup(reply_markup=build_sources_menu())


@router.callback_query(F.data == "show_targets")
async def cb_show_targets(call: CallbackQuery) -> None:
    if call.from_user.id != getattr(config, "ADMIN_USER_ID", 0):
        await call.answer("Доступ запрещён", show_alert=True)
        return
    await call.answer()
    targets = settings.get("WHITELIST_TARGET_CHANNEL_IDS", [])
    if targets:
        text = f"📤 **Целевые каналы ({len(targets)}):**\n" + "\n".join([f"• `{target}`" for target in targets])
    else:
        text = "📤 **Целевые каналы:**\nНет добавленных целей"
    
    await call.message.answer(mdv2(text), parse_mode="MarkdownV2")
    await call.message.edit_reply_markup(reply_markup=build_targets_menu())


@router.callback_query(F.data == "noop")
async def cb_noop(call: CallbackQuery) -> None:
    """Обработчик для информационных кнопок без действия"""
    await call.answer()


@router.callback_query(F.data == "delay_menu")
async def cb_delay_menu(call: CallbackQuery) -> None:
    await call.answer()
    await call.message.edit_reply_markup(reply_markup=build_delay_menu())


@router.callback_query(F.data.startswith("set_delay:"))
async def cb_set_delay(call: CallbackQuery) -> None:
    await call.answer("Задержка обновлена", cache_time=1)
    _, rng = call.data.split(":", 1)
    try:
        a, b = rng.split("-", 1)
        settings["delay_min"] = float(a)
        settings["delay_max"] = float(b)
        save_settings()
    except Exception:
        pass
    await call.message.edit_reply_markup(reply_markup=build_delay_menu())


@router.callback_query(F.data == "toggle_delay")
async def cb_toggle_delay(call: CallbackQuery) -> None:
    await call.answer("Переключаю…", cache_time=1)
    if float(settings.get("delay_min", 0)) == 0 and float(settings.get("delay_max", 0)) == 0:
        settings["delay_min"] = 5
        settings["delay_max"] = 5
    else:
        settings["delay_min"] = 0
        settings["delay_max"] = 0
    save_settings()
    await call.message.edit_reply_markup(reply_markup=build_settings_menu())


@router.callback_query(F.data == "add_source")
async def cb_add_source(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.waiting_add_source)
    await call.message.answer(
        mdv2("📥 **Добавление источника новостей**\n\n"
        "Отправьте ID канала или @username\n\n"
        "**Примеры:**\n"
        "• `-1001234567890` \\(ID канала\\)\n"
        "• `@channel\\_name` \\(username канала\\)\n\n"
        "⚠️ **Важно:** Бот должен быть добавлен в этот канал\\!"),
        parse_mode="MarkdownV2"
    )


@router.callback_query(F.data == "remove_source")
async def cb_remove_source(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.waiting_remove_source)
    await call.message.answer(
        mdv2("➖ **Удаление источника новостей**\n\n"
        "Отправьте ID канала для удаления\n\n"
        "**Пример:** `-1001234567890`"),
        parse_mode="MarkdownV2"
    )


@router.callback_query(F.data == "add_target")
async def cb_add_target(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.waiting_add_target)
    await call.message.answer(
        mdv2("📤 **Добавление целевого канала**\n\n"
        "Отправьте ID канала или @username\n\n"
        "**Примеры:**\n"
        "• `-1001234567890` \\(ID канала\\)\n"
        "• `@channel\\_name` \\(username канала\\)\n\n"
        "⚠️ **Важно:** Бот должен быть администратором этого канала\\!"),
        parse_mode="MarkdownV2"
    )


@router.callback_query(F.data == "remove_target")
async def cb_remove_target(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.waiting_remove_target)
    await call.message.answer(
        mdv2("➖ **Удаление целевого канала**\n\n"
        "Отправьте ID канала для удаления\n\n"
        "**Пример:** `-1001234567890`"),
        parse_mode="MarkdownV2"
    )


@router.message(AdminStates.waiting_add_source)
async def on_add_source(message: Message, bot: Bot, state: FSMContext) -> None:
    if not admin_only(message):
        return
    chat_id = await resolve_chat_id(bot, message.text or "")
    if chat_id is None:
        await message.answer(
            mdv2("❌ **Ошибка определения канала**\n\n"
            "Не удалось определить ID канала\\.\n\n"
            "**Проверьте:**\n"
            "• Правильность написания username\n"
            "• Существование канала\n"
            "• Доступность канала для бота\n\n"
            "Попробуйте ещё раз\\."),
            parse_mode="MarkdownV2"
        )
        return
    arr = settings.setdefault("WHITELIST_SOURCE_CHANNEL_IDS", [])
    if chat_id not in arr:
        arr.append(chat_id)
        save_settings()
    await message.answer(
        mdv2(f"✅ **Источник добавлен**\n\n"
        f"Канал `{chat_id}` успешно добавлен в список источников новостей."),
        parse_mode="MarkdownV2"
    )
    await state.clear()


@router.message(AdminStates.waiting_remove_source)
async def on_remove_source(message: Message, state: FSMContext) -> None:
    if not admin_only(message):
        return
    try:
        chat_id = int(message.text.strip())
    except Exception:
        await message.answer(
            mdv2("❌ **Неверный формат ID**\n\n"
            "ID канала должен быть числом.\n\n"
            "**Пример:** `-1001234567890`"),
            parse_mode="MarkdownV2"
        )
        return
    arr = settings.setdefault("WHITELIST_SOURCE_CHANNEL_IDS", [])
    if chat_id in arr:
        arr.remove(chat_id)
        save_settings()
        await message.answer(
            mdv2(f"✅ **Источник удалён**\n\n"
            f"Канал `{chat_id}` успешно удалён из списка источников."),
            parse_mode="MarkdownV2"
        )
    else:
        await message.answer(
            mdv2("❌ **Источник не найден**\n\n"
            f"Канал `{chat_id}` не найден в списке источников."),
            parse_mode="MarkdownV2"
        )
    await state.clear()


@router.message(AdminStates.waiting_add_target)
async def on_add_target(message: Message, bot: Bot, state: FSMContext) -> None:
    if not admin_only(message):
        return
    chat_id = await resolve_chat_id(bot, message.text or "")
    if chat_id is None:
        await message.answer(
            mdv2("❌ **Ошибка определения канала**\n\n"
            "Не удалось определить ID канала\\.\n\n"
            "**Проверьте:**\n"
            "• Правильность написания username\n"
            "• Существование канала\n"
            "• Доступность канала для бота\n\n"
            "Попробуйте ещё раз\\."),
            parse_mode="MarkdownV2"
        )
        return
    arr = settings.setdefault("WHITELIST_TARGET_CHANNEL_IDS", [])
    if chat_id not in arr:
        arr.append(chat_id)
        save_settings()
    await message.answer(
        mdv2(f"✅ **Целевой канал добавлен**\n\n"
        f"Канал `{chat_id}` успешно добавлен в список целей для публикации."),
        parse_mode="MarkdownV2"
    )
    await state.clear()


@router.message(AdminStates.waiting_remove_target)
async def on_remove_target(message: Message, state: FSMContext) -> None:
    if not admin_only(message):
        return
    try:
        chat_id = int(message.text.strip())
    except Exception:
        await message.answer(
            mdv2("❌ **Неверный формат ID**\n\n"
            "ID канала должен быть числом.\n\n"
            "**Пример:** `-1001234567890`"),
            parse_mode="MarkdownV2"
        )
        return
    arr = settings.setdefault("WHITELIST_TARGET_CHANNEL_IDS", [])
    if chat_id in arr:
        arr.remove(chat_id)
        save_settings()
        await message.answer(
            mdv2(f"✅ **Целевой канал удалён**\n\n"
            f"Канал `{chat_id}` успешно удалён из списка целей."),
            parse_mode="MarkdownV2"
        )
    else:
        await message.answer(
            mdv2("❌ **Целевой канал не найден**\n\n"
            f"Канал `{chat_id}` не найден в списке целей."),
            parse_mode="MarkdownV2"
        )
    await state.clear()


@router.callback_query(F.data == "manual_parse")
async def cb_manual_parse(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.set_state(AdminStates.waiting_forwards)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Готово", callback_data="finish_forward")]]
    )
    await call.message.answer(
        mdv2("📝 **Режим ручного парсинга**\n\n"
        "Перешлите сюда последние посты из исходных каналов\n\n"
        "**Что делает бот:**\n"
        "• 📸 Извлекает фото и видео\n"
        "• 🤖 Обрабатывает текст через AI\n"
        "• 📤 Отправляет на публикацию/утверждение\n\n"
        "Когда закончите, нажмите кнопку 'Готово' 👇"),
        reply_markup=kb,
        parse_mode="MarkdownV2"
    )


@router.callback_query(F.data == "finish_forward")
async def cb_finish_forward(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer("✅ Режим завершён", cache_time=1)
    await state.clear()
    await call.message.answer(
        mdv2("✅ **Ручной парсинг завершён**\n\n"
        "Все пересланные посты обработаны и добавлены в очередь на публикацию."),
        parse_mode="MarkdownV2"
    )


@router.message(AdminStates.waiting_forwards)
async def on_forwarded_news(message: Message, bot: Bot) -> None:
    if not admin_only(message):
        return
    # Extract forward origin
    source_chat_id: Optional[int] = None
    source_message_id: Optional[int] = None
    try:
        # aiogram v3: forward_origin.*
        if getattr(message, "forward_origin", None):
            origin = message.forward_origin
            # MessageOriginChannel has .chat and .message_id
            origin_chat = getattr(origin, "chat", None)
            if origin_chat:
                source_chat_id = getattr(origin_chat, "id", None)
            source_message_id = getattr(origin, "message_id", None)
    except Exception:
        pass
    if source_chat_id is None:
        # fallback legacy fields
        if getattr(message, "forward_from_chat", None):
            source_chat_id = message.forward_from_chat.id
            source_message_id = getattr(message, "forward_from_message_id", None)

    if source_chat_id is None:
        await message.answer(
            mdv2("❌ **Ошибка обработки**\n\n"
            "Это не пересланный пост канала. Перешлите пост из канала-источника."),
            parse_mode="MarkdownV2"
        )
        return

    # If sources whitelist is non-empty, enforce it
    sources = settings.get("WHITELIST_SOURCE_CHANNEL_IDS", [])
    if sources and source_chat_id not in sources:
        await message.answer(
            mdv2(f"❌ **Канал не в списке источников**\n\n"
            f"Канал `{source_chat_id}` не добавлен в список источников.\n"
            f"Добавьте его в настройках каналов."),
            parse_mode="MarkdownV2"
        )
        return

    text = message.caption if message.caption else (message.text or "")
    if is_probable_ad(text):
        await message.answer(
            mdv2("⚠️ **Рекламный пост обнаружен**\n\n"
            "Пост похож на рекламу и будет пропущен."),
            parse_mode="MarkdownV2"
        )
        return
    # If forwarded album, buffer and flush as one
    if message.media_group_id:
        mgid = f"fwd:{message.media_group_id}:{source_chat_id}"
        logger.info("[FWD] buffer media_group_id=%s msg=%s", mgid, message.message_id)
        fwd_groups_buffer.setdefault(mgid, []).append(message)
        if mgid not in fwd_groups_tasks:
            async def flush():
                await asyncio.sleep(1.0)
                items = fwd_groups_buffer.pop(mgid, [])
                fwd_groups_tasks.pop(mgid, None)
                logger.info("[FWD] flush media_group_id=%s count=%d", mgid, len(items))
                await process_forwarded_as_one(bot, items, notify=message)
            fwd_groups_tasks[mgid] = asyncio.create_task(flush())
        return

    # Single forwarded message (photo/video/text)
    media: List[Dict[str, Any]] = []
    if message.photo:
        media.append({"type": "photo", "file_id": choose_largest_photo(message.photo)})
    if getattr(message, "video", None):
        media.append({"type": "video", "file_id": message.video.file_id})

    source_key = build_source_key(source_chat_id, int(source_message_id or 0))
    await prepare_and_queue_item(bot, source_chat_id, int(source_message_id or 0), source_key, text, media, notify=message)


@router.callback_query(F.data == "publish_random")
async def cb_publish_random(call: CallbackQuery, bot: Bot) -> None:
    await call.answer("Функция удалена", cache_time=1)


# Approval flow callbacks: approve:, rework:, generate:
@router.callback_query(F.data.startswith("approve:"))
async def cb_approve(call: CallbackQuery, bot: Bot) -> None:
    await call.answer("Утверждено", cache_time=1)
    _, news_id = call.data.split(":", 1)
    item = pending_news.get(news_id)
    if not item:
        await call.message.answer("Элемент не найден")
        return
    await publish_news_item(bot, item)


@router.callback_query(F.data.startswith("rework:"))
async def cb_rework(call: CallbackQuery, bot: Bot) -> None:
    await call.answer("Переделываю…", cache_time=1)
    _, news_id = call.data.split(":", 1)
    item = pending_news.get(news_id)
    if not item:
        await call.message.answer("Элемент не найден")
        return
    # Reprocess with AI
    async with aiohttp.ClientSession() as session:
        rewritten = await sambanova_rewrite(session, item.get("raw_text", ""), "all")
    if not rewritten:
        await call.message.answer("Не удалось переделать (AI)")
        return
    item["title"] = rewritten.get("title", item.get("title", ""))
    item["text"] = rewritten.get("text", item.get("text", ""))
    await send_admin_preview(bot, item, replace_message=call.message)


@router.callback_query(F.data.startswith("generate:"))
async def cb_generate(call: CallbackQuery, bot: Bot) -> None:
    await call.answer("Генерирую новое…", cache_time=1)
    _, news_id = call.data.split(":", 1)
    item = pending_news.get(news_id)
    if not item:
        await call.message.answer("Элемент не найден")
        return
    # Generate a fresh variant based on same raw text
    async with aiohttp.ClientSession() as session:
        rewritten = await sambanova_rewrite(session, item.get("raw_text", ""), "all")
    if not rewritten:
        await call.message.answer("Не удалось сгенерировать (AI)")
        return
    # New ID
    new_id = f"gen-{datetime.utcnow().timestamp()}"
    new_item = dict(item)
    new_item.update({"id": new_id, "title": rewritten.get("title", ""), "text": rewritten.get("text", "")})
    pending_news[new_id] = new_item
    await send_admin_preview(bot, new_item, replace_message=call.message)


# ---------- Source channel listener ----------
@router.channel_post(F.chat.type == ChatType.CHANNEL)
async def on_channel_post(message: Message, bot: Bot) -> None:
    chat_id = message.chat.id
    if chat_id not in settings.get("WHITELIST_SOURCE_CHANNEL_IDS", []):
        return

    source_key = build_source_key(chat_id, message.message_id)
    if source_key in processed_source_keys:
        return

    text = message.caption if message.caption else (message.text or "")
    # news_type filter removed

    # Collect media: photos, videos, albums
    media: List[Dict[str, Any]] = []
    if message.media_group_id:
        # Buffer album messages briefly to send as a single media group
        mgid = message.media_group_id
        logger.info("[AUTO] buffer media_group_id=%s msg=%s", mgid, message.message_id)
        media_groups_buffer.setdefault(mgid, []).append(message)
        if mgid not in media_groups_tasks:
            async def flush():
                await asyncio.sleep(1.0)  # wait for group to complete
                items = media_groups_buffer.pop(mgid, [])
                media_groups_tasks.pop(mgid, None)
                logger.info("[AUTO] flush media_group_id=%s count=%d", mgid, len(items))
                await process_messages_as_one(bot, items, source_chat_id=chat_id)
            media_groups_tasks[mgid] = asyncio.create_task(flush())
        return
    else:
        if message.photo:
            media.append({"type": "photo", "file_id": choose_largest_photo(message.photo)})
        if getattr(message, "video", None):
            media.append({"type": "video", "file_id": message.video.file_id})

    processed_source_keys.add(source_key)

    await prepare_and_queue_item(bot, chat_id, message.message_id, source_key, text, media)


def _ensure_valid_output(raw_text: str, title: str, text: str) -> Tuple[str, str]:
    if validate_rewrite({"title": title, "text": text}, raw_text):
        return title, text
    fb = fallback_rewrite_from_source(raw_text)
    return fb["title"], fb["text"]


async def send_admin_preview(bot: Bot, item: Dict[str, Any], replace_message: Optional[Message] = None) -> None:
    admin_id = getattr(config, "ADMIN_USER_ID", 0)
    title_raw = item.get("title", "")
    text_raw = item.get("text", "")
    title_raw, text_raw = _ensure_valid_output(item.get("raw_text", ""), title_raw, text_raw)
    caption = format_markdown_caption(title_raw, text_raw)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Утвердить", callback_data=f"approve:{item['id']}"),
                InlineKeyboardButton(text="Переделать", callback_data=f"rework:{item['id']}")
            ],
            [InlineKeyboardButton(text="Сгенерировать новое", callback_data=f"generate:{item['id']}")],
        ]
    )
    try:
        media = item.get("media")
        if media:
            # For preview, send first media with caption
            first = media[0]
            if first["type"] == "photo":
                await _send_with_md_fallback(bot, admin_id, caption=caption, photo_file_id=first["file_id"], reply_markup=kb)
            elif first["type"] == "video":
                try:
                    await bot.send_video(admin_id, first["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
                except TelegramBadRequest:
                    await bot.send_video(admin_id, first["file_id"], caption=strip_markdown_to_plain(caption), reply_markup=kb)
        else:
            if replace_message:
                await replace_message.edit_text(text=caption, parse_mode=ParseMode.MARKDOWN_V2, reply_markup=kb)
            else:
                await _send_with_md_fallback(bot, admin_id, caption=caption, reply_markup=kb)
    except TelegramBadRequest as e:
        logger.error("Failed to send admin preview: %s", e)


async def publish_news_item(bot: Bot, item: Dict[str, Any]) -> None:
    global published_today_count
    reset_daily_counters_if_needed()

    source_key = item.get("source_key")
    if source_key and source_key in posted_news_keys:
        logger.info("Duplicate publication skipped: %s", source_key)
        pending_news.pop(item["id"], None)
        return

    # Modes/limits deprecated
    title_raw = item.get("title", "")
    text_raw = item.get("text", "")
    title_raw, text_raw = _ensure_valid_output(item.get("raw_text", ""), title_raw, text_raw)

    caption = format_markdown_caption(title_raw, text_raw)

    targets = settings.get("WHITELIST_TARGET_CHANNEL_IDS", [])
    if not targets:
        logger.error("No target channels configured")
        return

    for chat_id in targets:
        try:
            media = item.get("media")
            if media:
                if len(media) > 1:
                    from aiogram.types import InputMediaPhoto, InputMediaVideo
                    group = []
                    for idx, m in enumerate(media):
                        if m["type"] == "photo":
                            group.append(InputMediaPhoto(media=m["file_id"], caption=caption if idx == 0 else None, parse_mode=ParseMode.MARKDOWN_V2 if idx == 0 else None))
                        elif m["type"] == "video":
                            group.append(InputMediaVideo(media=m["file_id"], caption=caption if idx == 0 else None, parse_mode=ParseMode.MARKDOWN_V2 if idx == 0 else None))
                    try:
                        await bot.send_media_group(chat_id, media=group)
                    except TelegramBadRequest:
                        plain = strip_markdown_to_plain(caption)
                        group_plain = []
                        for idx, m in enumerate(media):
                            if m["type"] == "photo":
                                group_plain.append(InputMediaPhoto(media=m["file_id"], caption=plain if idx == 0 else None))
                            else:
                                group_plain.append(InputMediaVideo(media=m["file_id"], caption=plain if idx == 0 else None))
                        await bot.send_media_group(chat_id, media=group_plain)
                else:
                    m = media[0]
                    if m["type"] == "photo":
                        await _send_with_md_fallback(bot, chat_id, caption=caption, photo_file_id=m["file_id"]) 
                    else:
                        try:
                            await bot.send_video(chat_id, m["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
                        except TelegramBadRequest:
                            await bot.send_video(chat_id, m["file_id"], caption=strip_markdown_to_plain(caption))
            else:
                await _send_with_md_fallback(bot, chat_id, caption=caption)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
            # One retry
            try:
                media = item.get("media")
                if media and len(media) == 1:
                    m = media[0]
                    if m["type"] == "photo":
                        await _send_with_md_fallback(bot, chat_id, caption=caption, photo_file_id=m["file_id"]) 
                    else:
                        await bot.send_video(chat_id, m["file_id"], caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
                elif media and len(media) > 1:
                    from aiogram.types import InputMediaPhoto, InputMediaVideo
                    group = []
                    for idx, m in enumerate(media):
                        if m["type"] == "photo":
                            group.append(InputMediaPhoto(media=m["file_id"], caption=caption if idx == 0 else None, parse_mode=ParseMode.MARKDOWN_V2 if idx == 0 else None))
                        else:
                            group.append(InputMediaVideo(media=m["file_id"], caption=caption if idx == 0 else None, parse_mode=ParseMode.MARKDOWN_V2 if idx == 0 else None))
                    await bot.send_media_group(chat_id, media=group)
                else:
                    await _send_with_md_fallback(bot, chat_id, caption=caption)
            except TelegramBadRequest:
                plain = strip_markdown_to_plain(caption)
                if media and len(media) == 1:
                    m = media[0]
                    if m["type"] == "photo":
                        await bot.send_photo(chat_id, m["file_id"], caption=plain)
                    else:
                        await bot.send_video(chat_id, m["file_id"], caption=plain)
                elif media and len(media) > 1:
                    from aiogram.types import InputMediaPhoto, InputMediaVideo
                    group_plain = []
                    for idx, m in enumerate(media):
                        if m["type"] == "photo":
                            group_plain.append(InputMediaPhoto(media=m["file_id"], caption=plain if idx == 0 else None))
                        else:
                            group_plain.append(InputMediaVideo(media=m["file_id"], caption=plain if idx == 0 else None))
                    await bot.send_media_group(chat_id, media=group_plain)
                else:
                    await bot.send_message(chat_id, plain)
        except TelegramBadRequest as e:
            # Suppress duplicate logs if parse entities already handled inside fallback
            if "can't parse entities" in str(e).lower():
                continue
            logger.error("Failed to publish to %s: %s", chat_id, e)
            continue

    if source_key:
        posted_news_keys.add(source_key)
    pending_news.pop(item["id"], None)
    published_today_count += 1
    stats["published_today"] = published_today_count
    stats["total_published"] = int(stats.get("total_published", 0)) + 1
    save_stats()
    logger.info("News published (today=%s)", published_today_count)


# ---------- Scheduler ----------
scheduler: Optional[AsyncIOScheduler] = None


def schedule_jobs(dp: Dispatcher, bot: Bot) -> None:
    global scheduler
    if scheduler:
        return
    scheduler = AsyncIOScheduler()

    # Heartbeat job: enforce interval/daily limit policy
    async def heartbeat_job():
        try:
            reset_daily_counters_if_needed()
            if not pending_news:
                return
            now = datetime.now(UTC)
            # Publish any items whose ready_at passed
            due_ids = [iid for iid, it in list(pending_news.items()) if datetime.fromisoformat(it.get("ready_at", now.isoformat())) <= now]
            logger.info("[HB] pending=%d due=%d", len(pending_news), len(due_ids))
            if not due_ids:
                return
            # Publish in insertion order of due_ids
            for iid in due_ids:
                if iid in pending_news:
                    logger.info("[HB] publishing item=%s", iid)
                    await publish_news_item(bot, pending_news[iid])
        except Exception as e:
            logger.error("Scheduler job error: %s", e)

    # Initial schedule
    # Run heartbeat frequently to catch ready_at promptly
    scheduler.add_job(heartbeat_job, id="heartbeat", trigger="interval", seconds=int(settings.get("heartbeat_seconds", 10)), replace_existing=True)
    scheduler.start()
    logger.info("Scheduler started")


# ---------- UserBot Integration Functions ----------
async def get_userbot_settings() -> dict:
    """Получает настройки UserBot"""
    try:
        with open('userbot_settings.json', 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return {"source_channels": [], "bot_chat_id": None}

async def save_userbot_settings(settings: dict):
    """Сохраняет настройки UserBot"""
    with open('userbot_settings.json', 'w', encoding='utf-8') as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)

async def resolve_channel_id(channel_input: str, bot_instance: Bot) -> Optional[int]:
    """Разрешает канал по username или ID"""
    try:
        if channel_input.startswith('@'):
            # Username - получаем через get_chat
            chat = await bot_instance.get_chat(channel_input)
            return chat.id
        elif channel_input.startswith('-100'):
            # Channel ID
            return int(channel_input)
        else:
            # Попробуем как ID
            return int(channel_input)
    except Exception as e:
        logger.error(f"Ошибка разрешения канала '{channel_input}': {e}")
        return None


# ---------- UserBot Callback Handlers ----------
@router.callback_query(F.data == "userbot_channels")
async def cb_userbot_channels(call: CallbackQuery, bot: Bot) -> None:
    """Показывает меню управления каналами UserBot"""
    await call.answer()
    await call.message.edit_text(
        mdv2("📡 **Управление каналами UserBot**\n\n"
        "Здесь вы можете управлять каналами, которые UserBot будет мониторить автоматически\\."),
        reply_markup=build_userbot_channels_menu(),
        parse_mode="MarkdownV2"
    )

@router.callback_query(F.data == "userbot_add_channel")
async def cb_userbot_add_channel(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Запрашивает канал для добавления в UserBot"""
    await call.answer()
    
    # Сохраняем состояние для ожидания ввода канала
    await state.set_state(AdminStates.waiting_userbot_channel)
    
    await call.message.edit_text(
        mdv2("🤖 **Добавление канала в UserBot**\n\n"
        "Отправьте username канала или его ID\n\n"
        "**Примеры:**\n"
        "• `@channel\\_name` \\(username канала\\)\n"
        "• `\\-1001234567890` \\(ID канала\\)\n\n"
        "💡 **UserBot** может мониторить каналы, где обычный бот не может быть участником\n\n"
        "UserBot автоматически начнет мониторить этот канал и пересылать все новые посты."),
        reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="userbot_channels").as_markup(),
        parse_mode="MarkdownV2"
    )

@router.callback_query(F.data == "userbot_remove_channel")
async def cb_userbot_remove_channel(call: CallbackQuery, bot: Bot) -> None:
    """Показывает список каналов для удаления"""
    await call.answer()
    
    settings = await get_userbot_settings()
    channels = settings.get("source_channels", [])
    
    if not channels:
        await call.message.edit_text(
            mdv2("📋 **Список каналов UserBot пуст**\n\n"
            "Сначала добавьте каналы для мониторинга."),
            reply_markup=build_userbot_channels_menu(),
            parse_mode="MarkdownV2"
        )
        return
    
    # Создаем кнопки для каждого канала
    kb = InlineKeyboardBuilder()
    for channel_id in channels:
        try:
            chat = await bot.get_chat(channel_id)
            title = chat.title or f"Канал {channel_id}"
            kb.button(text=f"❌ {title[:30]}", callback_data=f"userbot_del_channel:{channel_id}")
        except Exception:
            kb.button(text=f"❌ ID: {channel_id}", callback_data=f"userbot_del_channel:{channel_id}")
    
    kb.button(text="⬅ Назад", callback_data="userbot_channels")
    kb.adjust(1)
    
    await call.message.edit_text(
        mdv2("➖ **Удаление канала из UserBot**\n\n"
        "Выберите канал, который хотите убрать из мониторинга:"),
        reply_markup=kb.as_markup(),
        parse_mode="MarkdownV2"
    )

@router.callback_query(F.data.startswith("userbot_del_channel:"))
async def cb_userbot_delete_channel(call: CallbackQuery, bot: Bot) -> None:
    """Удаляет канал из UserBot"""
    await call.answer()
    
    _, channel_id = call.data.split(":", 1)
    channel_id = int(channel_id)
    
    settings = await get_userbot_settings()
    if channel_id in settings.get("source_channels", []):
        settings["source_channels"].remove(channel_id)
        await save_userbot_settings(settings)
        
        await call.message.edit_text(
            mdv2(f"✅ **Канал {channel_id} удален из UserBot**\n\n"
            "UserBot больше не будет мониторить этот канал."),
            reply_markup=build_userbot_channels_menu(),
            parse_mode="MarkdownV2"
        )
    else:
        await call.message.edit_text(
            mdv2(f"❌ **Канал {channel_id} не найден в списке мониторинга.**"),
            reply_markup=build_userbot_channels_menu(),
            parse_mode="MarkdownV2"
        )

@router.callback_query(F.data == "userbot_list_channels")
async def cb_userbot_list_channels(call: CallbackQuery, bot: Bot) -> None:
    """Показывает список мониторимых каналов UserBot"""
    await call.answer()
    
    settings = await get_userbot_settings()
    channels = settings.get("source_channels", [])
    
    if not channels:
        await call.message.edit_text(
            mdv2("📋 **Список каналов UserBot пуст**\n\n"
            "Добавьте каналы для автоматического мониторинга."),
            reply_markup=build_userbot_channels_menu(),
            parse_mode="MarkdownV2"
        )
        return
    
    # Формируем список каналов
    channels_text = "📋 **Каналы под мониторингом UserBot:**\n\n"
    for i, channel_id in enumerate(channels, 1):
        try:
            chat = await bot.get_chat(channel_id)
            title = chat.title or f"Канал {channel_id}"
            username = f" \\(@{chat.username}\\)" if chat.username else ""
            channels_text += f"{i}\\. **{title}**{username}\n   `ID: {channel_id}`\n\n"
        except Exception:
            channels_text += f"{i}\\. **ID: {channel_id}** \\(ошибка получения\\)\n\n"
    
    channels_text += f"\n**Всего каналов:** {len(channels)}"
    
    await call.message.edit_text(
        mdv2(channels_text),
        reply_markup=build_userbot_channels_menu(),
        parse_mode="MarkdownV2"
    )

@router.callback_query(F.data == "userbot_settings")
async def cb_userbot_settings(call: CallbackQuery, bot: Bot) -> None:
    """Показывает настройки UserBot"""
    await call.answer()
    
    await call.message.edit_text(
        mdv2("🔧 **Настройки UserBot**\n\n"
        "Управление параметрами автоматического мониторинга каналов."),
        reply_markup=build_userbot_settings_menu(),
        parse_mode="MarkdownV2"
    )

@router.callback_query(F.data == "userbot_set_bot")
async def cb_userbot_set_bot(call: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    """Запрашивает чат бота для UserBot"""
    await call.answer()
    
    # Сохраняем состояние для ожидания ввода чата бота
    await state.set_state(AdminStates.waiting_userbot_bot_chat)
    
    await call.message.edit_text(
        mdv2("🤖 **Установка чата бота для UserBot**\n\n"
        "Отправьте username вашего бота или его ID\n\n"
        "**Примеры:**\n"
        "• `@your\\_bot` \\(username бота\\)\n"
        "• `123456789` \\(ID бота\\)\n\n"
        "UserBot будет пересылать все посты из мониторимых каналов в этот чат."),
        reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="userbot_settings").as_markup(),
        parse_mode="MarkdownV2"
    )

@router.callback_query(F.data == "userbot_status")
async def cb_userbot_status(call: CallbackQuery, bot: Bot) -> None:
    """Показывает статус UserBot"""
    await call.answer()
    
    settings = await get_userbot_settings()
    channels_count = len(settings.get("source_channels", []))
    bot_chat_id = settings.get("bot_chat_id")
    
    status_text = "📊 **Статус UserBot**\n\n"
    status_text += f"📡 **Мониторимых каналов:** {channels_count}\n"
    
    if bot_chat_id:
        try:
            chat = await bot.get_chat(bot_chat_id)
            chat_name = chat.title or chat.username or f"ID: {bot_chat_id}"
            status_text += f"🤖 **Чат бота:** {chat_name}\n"
        except Exception:
            status_text += f"🤖 **Чат бота:** ID {bot_chat_id}\n"
    else:
        status_text += "🤖 **Чат бота:** Не установлен\n"
    
    # Проверяем файл сессии
    import os
    if os.path.exists('userbot_session.session'):
        status_text += "✅ **Сессия UserBot:** Активна\n"
    else:
        status_text += "❌ **Сессия UserBot:** Не найдена\n"
    
    status_text += "\n💡 **Для запуска UserBot используйте:** `python userbot\\.py`"
    
    await call.message.edit_text(
        mdv2(status_text),
        reply_markup=build_userbot_settings_menu(),
        parse_mode="MarkdownV2"
    )

@router.message(AdminStates.waiting_userbot_channel)
async def on_userbot_channel_input(message: Message, bot: Bot, state: FSMContext) -> None:
    """Обрабатывает ввод канала для UserBot"""
    if not admin_only(message):
        return
    
    channel_input = message.text.strip()
    
    # Разрешаем канал
    channel_id = await resolve_channel_id(channel_input, bot)
    if not channel_id:
            await message.answer(
        mdv2("❌ **Не удалось разрешить канал**\n\n"
        "Проверьте формат:\n"
        "• `@channel\\_name` \\(username канала\\)\n"
        "• `\\-1001234567890` \\(ID канала\\)"),
        reply_markup=build_userbot_channels_menu(),
        parse_mode="MarkdownV2"
    )
        await state.clear()
        return
    
    # Проверяем, не добавлен ли уже
    settings = await get_userbot_settings()
    if channel_id in settings.get("source_channels", []):
            await message.answer(
        mdv2(f"ℹ️ **Канал уже добавлен в UserBot**\n\n"
        f"ID: `{channel_id}`"),
        reply_markup=build_userbot_channels_menu(),
        parse_mode="MarkdownV2"
    )
        await state.clear()
        return
    
    # Добавляем канал
    if "source_channels" not in settings:
        settings["source_channels"] = []
    settings["source_channels"].append(channel_id)
    await save_userbot_settings(settings)
    
    # Получаем информацию о канале
    try:
        chat = await bot.get_chat(channel_id)
        title = chat.title or f"Канал {channel_id}"
        username = f" \\(@{chat.username}\\)" if chat.username else ""
        channel_info = f"{title}{username}"
    except Exception:
        channel_info = f"ID: {channel_id}"
    
    await message.answer(
        mdv2(f"✅ **Канал добавлен в UserBot\\!**\n\n"
        f"📡 **Канал:** {channel_info}\n"
        f"🆔 **ID:** `{channel_id}`\n\n"
        f"UserBot автоматически начнет мониторить этот канал и пересылать все новые посты\\."),
        reply_markup=build_userbot_channels_menu(),
        parse_mode="MarkdownV2"
    )
    
    await state.clear()

@router.message(AdminStates.waiting_userbot_bot_chat)
async def on_userbot_bot_chat_input(message: Message, bot: Bot, state: FSMContext) -> None:
    """Обрабатывает ввод чата бота для UserBot"""
    if not admin_only(message):
        return
    
    chat_input = message.text.strip()
    
    try:
        if chat_input.startswith('@'):
            # Username бота
            chat = await bot.get_chat(chat_input)
        else:
            # ID чата
            chat = await bot.get_chat(int(chat_input))
        
        bot_chat_id = chat.id
        
        # Сохраняем настройку
        settings = await get_userbot_settings()
        settings["bot_chat_id"] = bot_chat_id
        await save_userbot_settings(settings)
        
        chat_name = chat.title or chat.username or f"ID: {bot_chat_id}"
        
        await message.answer(
            mdv2(f"✅ **Чат бота установлен для UserBot\\!**\n\n"
            f"🤖 **Чат:** {chat_name}\n"
            f"🆔 **ID:** `{bot_chat_id}`\n\n"
            f"Теперь UserBot будет пересылать все посты из мониторимых каналов в этот чат\\."),
            reply_markup=build_userbot_settings_menu(),
            parse_mode="MarkdownV2"
        )
        
    except Exception as e:
        await message.answer(
            f"❌ Ошибка установки чата бота: {e}\n\n"
                    "Проверьте формат:\n"
        "• @your\\_bot \\- username бота\n"
        "• 123456789 \\- ID чата",
            reply_markup=build_userbot_settings_menu()
        )
    
    await state.clear()


async def main() -> None:
    global settings, stats
    settings = load_settings()
    stats = load_stats()

    bot = Bot(token=getattr(config, "TELEGRAM_BOT_TOKEN", ""))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    # Startup message disabled per request

    # Start scheduler after bot initialized
    schedule_jobs(dp, bot)

    # Run polling
    await dp.start_polling(bot, allowed_updates=["message", "channel_post", "callback_query"])  # noqa: E501


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")






