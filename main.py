"""ID Inactive & Deleted — Telegram group inactivity manager.

The bot uses the official Telegram Bot API. It tracks group message activity,
warns after a configurable period, then bans members who remain inactive after
the grace period. It does not claim to know users' Telegram-wide online status.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden, TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

APP_NAME = "ID Inactive & Deleted"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
# Default owner ID supplied for this project. Environment configuration can
# override this with one or more comma-separated owner IDs.
_DEFAULT_OWNER_IDS = "6195715245"
_OWNER_IDS_RAW = os.getenv("OWNER_IDS", "").strip() or _DEFAULT_OWNER_IDS
OWNER_IDS = {
    int(part.strip())
    for part in _OWNER_IDS_RAW.split(",")
    if part.strip().isdigit()
}
DB_PATH = Path(os.getenv("DB_PATH", "data/id_inactive_deleted.sqlite3")).expanduser()
ACTIVITY_DAYS = max(1, int(os.getenv("ACTIVITY_DAYS", "13")))
WARNING_HOURS = max(1, int(os.getenv("WARNING_HOURS", "24")))
CHECK_INTERVAL_MINUTES = max(5, int(os.getenv("CHECK_INTERVAL_MINUTES", "60")))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
BASE_DIR = Path(__file__).resolve().parent
LOGO_PATH = BASE_DIR / "assets" / "logo.png"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("id_inactive_deleted")

def build_welcome_text(activity_days: int, warning_hours: int) -> str:
    return (
        "<b>🛡️ Welcome to ID Inactive &amp; Deleted!</b>\n\n"
        "I'm <b>ID Inactive &amp; Deleted</b> — your smart group protection assistant.\n\n"
        "🔒 Keep your group secure and manage members efficiently with automatic inactivity protection.\n\n"
        "<b>⚙️ SETUP GUIDE</b>\n\n"
        "<b>➤ 1. ADD ME TO YOUR GROUP</b>\n"
        "Add me to your group as an administrator.\n\n"
        "<b>➤ 2. GRANT REQUIRED PERMISSIONS</b>\n"
        "Enable the ability to ban members. I must be an administrator to monitor activity and take action.\n\n"
        "<b>➤ 3. AUTOMATIC PROTECTION</b>\n"
        f"New groups start with a {activity_days}-day activity limit and a {warning_hours}-hour warning period. A group admin or approved Bot Admin can change that group's settings using /setinactive and /setwarning inside the group.\n\n"
        "<b>📌 IMPORTANT</b>\n"
        "I track activity <b>inside this group</b>, not a person's Telegram-wide online status. Commands are optional; change settings inside the specific group.\n\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "<b>🛡️ ID INACTIVE &amp; DELETED</b>\n"
        "🔐 Smart Management • Secure Groups"
    )


def now_ts() -> int:
    return int(time.time())


def display_name(user) -> str:
    """Format a clickable display name while safely escaping HTML."""
    name = html.escape((user.full_name or user.username or "Member").strip())
    return f'<a href="tg://user?id={user.id}">{name}</a>'


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    def _init_db(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    chat_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    enabled_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS members (
                    chat_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    last_activity INTEGER NOT NULL,
                    warning_sent_at INTEGER,
                    state TEXT NOT NULL DEFAULT 'member',
                    is_bot INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (chat_id, user_id)
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS idx_members_due ON members(state, last_activity)")
            # Recover any in-flight ban claims from a previous process crash.
            # The scanner will re-check Telegram membership before taking action.
            db.execute("UPDATE members SET state='member' WHERE state='banning'")
            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_access (
                    user_id INTEGER PRIMARY KEY,
                    role TEXT NOT NULL CHECK(role IN ('user','admin')),
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    approved_by INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS known_users (
                    user_id INTEGER PRIMARY KEY,
                    first_name TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    seen_at INTEGER NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
            db.execute(
                "INSERT OR IGNORE INTO bot_settings(key,value) VALUES('lockall','0')"
            )
            db.execute(
                "INSERT OR IGNORE INTO bot_settings(key,value) VALUES('activity_days',?)",
                (str(ACTIVITY_DAYS),),
            )
            db.execute(
                "INSERT OR IGNORE INTO bot_settings(key,value) VALUES('warning_hours',?)",
                (str(WARNING_HOURS),),
            )
            # Inactivity configuration is scoped per Telegram group, never global.
            db.execute("""
                CREATE TABLE IF NOT EXISTS group_settings (
                    chat_id INTEGER PRIMARY KEY,
                    activity_days INTEGER NOT NULL,
                    warning_hours INTEGER NOT NULL,
                    updated_by INTEGER,
                    updated_at INTEGER NOT NULL
                )
            """)
            # Existing groups receive neutral environment defaults once. Legacy global
            # activity settings are intentionally not copied into each group.
            db.execute(
                """INSERT OR IGNORE INTO group_settings
                   (chat_id,activity_days,warning_hours,updated_by,updated_at)
                   SELECT chat_id,?,?,NULL,? FROM groups""",
                (ACTIVITY_DAYS, WARNING_HOURS, now_ts()),
            )

    def ensure_group(self, chat_id: int, title: str) -> None:
        # A row in groups means the bot is an admin with the needed ban permission.
        with self._connect() as db:
            db.execute(
                "INSERT INTO groups(chat_id,title,enabled_at) VALUES(?,?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title",
                (chat_id, title or "Telegram group", now_ts()),
            )
            db.execute(
                """INSERT OR IGNORE INTO group_settings
                   (chat_id,activity_days,warning_hours,updated_by,updated_at)
                   VALUES(?,?,?,?,?)""",
                (chat_id, ACTIVITY_DAYS, WARNING_HOURS, None, now_ts()),
            )

    def has_group(self, chat_id: int) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM groups WHERE chat_id=?", (chat_id,)).fetchone() is not None

    def disable_group(self, chat_id: int) -> None:
        # Clear this group's tracking data when the bot is removed or loses its required role,
        # so re-adding it cannot trigger stale automatic bans.
        with self._connect() as db:
            db.execute("DELETE FROM groups WHERE chat_id=?", (chat_id,))
            db.execute("DELETE FROM members WHERE chat_id=?", (chat_id,))

    def track_activity(self, chat_id: int, user, activity_ts: Optional[int] = None) -> None:
        if not user or user.is_bot:
            return
        ts = activity_ts or now_ts()
        with self._connect() as db:
            db.execute(
                """INSERT INTO members(chat_id,user_id,first_name,username,last_activity,warning_sent_at,state,is_bot)
                   VALUES(?,?,?,?,?,NULL,'member',0)
                   ON CONFLICT(chat_id,user_id) DO UPDATE SET
                     first_name=excluded.first_name,
                     username=excluded.username,
                     last_activity=excluded.last_activity,
                     warning_sent_at=NULL,
                     state='member'""",
                (chat_id, user.id, user.first_name or user.full_name or "", user.username or "", ts),
            )

    def mark_left(self, chat_id: int, user_id: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE members SET state='left', warning_sent_at=NULL WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            )

    def due_members(self, chat_id: int) -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(db.execute(
                "SELECT * FROM members WHERE chat_id=? AND state='member' AND is_bot=0",
                (chat_id,),
            ).fetchall())

    def set_warning(self, chat_id: int, user_id: int, ts: int, expected_last_activity: int) -> bool:
        """Save a warning only if no new group message arrived while it was sent."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE members SET warning_sent_at=? "
                "WHERE chat_id=? AND user_id=? AND state='member' "
                "AND last_activity=? AND warning_sent_at IS NULL",
                (ts, chat_id, user_id, expected_last_activity),
            )
            return cur.rowcount == 1

    def claim_ban(
        self, chat_id: int, user_id: int, expected_last_activity: int, expected_warning_sent_at: int
    ) -> bool:
        """Atomically claim a due ban without overwriting a concurrent activity reset."""
        with self._connect() as db:
            cur = db.execute(
                "UPDATE members SET state='banning' "
                "WHERE chat_id=? AND user_id=? AND state='member' "
                "AND last_activity=? AND warning_sent_at=?",
                (chat_id, user_id, expected_last_activity, expected_warning_sent_at),
            )
            return cur.rowcount == 1

    def release_ban_claim(
        self, chat_id: int, user_id: int, expected_last_activity: int, expected_warning_sent_at: int
    ) -> None:
        """Roll back only this scanner's claim; never overwrite a newer user/admin event."""
        with self._connect() as db:
            db.execute(
                "UPDATE members SET state='member' "
                "WHERE chat_id=? AND user_id=? AND state='banning' "
                "AND last_activity=? AND warning_sent_at=?",
                (chat_id, user_id, expected_last_activity, expected_warning_sent_at),
            )

    def finish_ban_claim(
        self, chat_id: int, user_id: int, expected_last_activity: int, expected_warning_sent_at: int
    ) -> None:
        """Mark a successful ban only if no unban/rejoin/activity update superseded it."""
        with self._connect() as db:
            db.execute(
                "UPDATE members SET state='kicked' "
                "WHERE chat_id=? AND user_id=? AND state='banning' "
                "AND last_activity=? AND warning_sent_at=?",
                (chat_id, user_id, expected_last_activity, expected_warning_sent_at),
            )

    def mark_left_if_unchanged(self, chat_id: int, user_id: int, expected_last_activity: int) -> None:
        """Do not let a slow membership lookup overwrite a new join/message event."""
        with self._connect() as db:
            db.execute(
                "UPDATE members SET state='left', warning_sent_at=NULL "
                "WHERE chat_id=? AND user_id=? AND state='member' AND last_activity=?",
                (chat_id, user_id, expected_last_activity),
            )

    def get_member(self, chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as db:
            return db.execute(
                "SELECT * FROM members WHERE chat_id=? AND user_id=?",
                (chat_id, user_id),
            ).fetchone()

    def ensure_member_if_missing(self, chat_id: int, user) -> None:
        if not user or user.is_bot:
            return
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO members
                   (chat_id,user_id,first_name,username,last_activity,warning_sent_at,state,is_bot)
                   VALUES(?,?,?,?,?,NULL,'member',0)""",
                (chat_id, user.id, user.first_name or user.full_name or "", user.username or "", now_ts()),
            )

    def set_state(self, chat_id: int, user_id: int, state: str) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE members SET state=?, warning_sent_at=NULL WHERE chat_id=? AND user_id=?",
                (state, chat_id, user_id),
            )

    def group_ids(self) -> list[sqlite3.Row]:
        with self._connect() as db:
            return list(db.execute("SELECT chat_id,title FROM groups").fetchall())

    def remember_user(self, user) -> None:
        if not user or user.is_bot:
            return
        with self._connect() as db:
            db.execute(
                """INSERT INTO known_users(user_id,first_name,username,seen_at) VALUES(?,?,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name,
                   username=excluded.username, seen_at=excluded.seen_at""",
                (user.id, user.first_name or user.full_name or "", user.username or "", now_ts()),
            )

    def role_for(self, user_id: int) -> Optional[str]:
        if user_id in OWNER_IDS:
            return "owner"
        with self._connect() as db:
            row = db.execute("SELECT role FROM bot_access WHERE user_id=?", (user_id,)).fetchone()
            return str(row["role"]) if row else None

    def approve_user(self, user_id: int, role: str, approved_by: int) -> None:
        with self._connect() as db:
            known = db.execute(
                "SELECT first_name,username FROM known_users WHERE user_id=?", (user_id,)
            ).fetchone()
            first_name = str(known["first_name"]) if known else ""
            username = str(known["username"]) if known else ""
            db.execute(
                """INSERT INTO bot_access(user_id,role,first_name,username,approved_by,updated_at)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                   role=excluded.role, first_name=excluded.first_name, username=excluded.username,
                   approved_by=excluded.approved_by, updated_at=excluded.updated_at""",
                (user_id, role, first_name, username, approved_by, now_ts()),
            )

    def known_user_by_username(self, username: str) -> Optional[int]:
        cleaned = username.strip().lstrip("@").casefold()
        if not cleaned:
            return None
        with self._connect() as db:
            rows = db.execute(
                "SELECT user_id FROM known_users WHERE lower(username)=? ORDER BY seen_at DESC LIMIT 2",
                (cleaned,),
            ).fetchall()
            return int(rows[0]["user_id"]) if len(rows) == 1 else None

    def lockall_enabled(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT value FROM bot_settings WHERE key='lockall'").fetchone()
            return bool(row and row["value"] == "1")

    def set_lockall(self, enabled: bool) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO bot_settings(key,value) VALUES('lockall',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if enabled else "0",),
            )

    def setting_int(self, key: str, default: int) -> int:
        with self._connect() as db:
            row = db.execute("SELECT value FROM bot_settings WHERE key=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            log.warning("Invalid integer setting %s=%r; using default %s", key, row["value"], default)
            return default

    def set_setting_int(self, key: str, value: int) -> None:
        if key not in {"activity_days", "warning_hours"}:
            raise ValueError("Unsupported setting key")
        with self._connect() as db:
            db.execute(
                "INSERT INTO bot_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(int(value))),
            )

    def inactivity_settings(self) -> tuple[int, int]:
        return (
            self.setting_int("activity_days", ACTIVITY_DAYS),
            self.setting_int("warning_hours", WARNING_HOURS),
        )

    def _set_inactivity_setting_and_refresh_warnings(self, key: str, value: int) -> int:
        """Atomically save a setting and restart all persisted warning/ban claims."""
        if key not in {"activity_days", "warning_hours"}:
            raise ValueError("Unsupported setting key")
        ts = now_ts()
        with self._connect() as db:
            db.execute(
                "INSERT INTO bot_settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, str(int(value))),
            )
            cur = db.execute(
                "UPDATE members SET warning_sent_at=?, state='member' "
                "WHERE warning_sent_at IS NOT NULL AND state IN ('member','banning')",
                (ts,),
            )
            return cur.rowcount

    def set_inactivity_days(self, days: int) -> int:
        return self._set_inactivity_setting_and_refresh_warnings("activity_days", days)

    def set_warning_hours(self, hours: int) -> int:
        return self._set_inactivity_setting_and_refresh_warnings("warning_hours", hours)

    def group_inactivity_settings(self, chat_id: int) -> tuple[int, int]:
        """Return persisted settings for one group, creating defaults when absent."""
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO group_settings
                   (chat_id,activity_days,warning_hours,updated_by,updated_at)
                   VALUES(?,?,?,?,?)""",
                (chat_id, ACTIVITY_DAYS, WARNING_HOURS, None, now_ts()),
            )
            row = db.execute(
                "SELECT activity_days,warning_hours FROM group_settings WHERE chat_id=?",
                (chat_id,),
            ).fetchone()
        return int(row["activity_days"]), int(row["warning_hours"])

    def _set_group_setting_and_refresh_warnings(
        self, chat_id: int, key: str, value: int, updated_by: int
    ) -> int:
        if key not in {"activity_days", "warning_hours"}:
            raise ValueError("Unsupported group setting key")
        ts = now_ts()
        with self._connect() as db:
            db.execute(
                """INSERT OR IGNORE INTO group_settings
                   (chat_id,activity_days,warning_hours,updated_by,updated_at)
                   VALUES(?,?,?,?,?)""",
                (chat_id, ACTIVITY_DAYS, WARNING_HOURS, None, ts),
            )
            db.execute(
                f"UPDATE group_settings SET {key}=?, updated_by=?, updated_at=? WHERE chat_id=?",
                (int(value), updated_by, ts, chat_id),
            )
            # Only reset warnings in the group whose setting was changed. Other groups'
            # warning clocks and timer generations must remain completely untouched.
            cur = db.execute(
                "UPDATE members SET warning_sent_at=?, state='member' "
                "WHERE chat_id=? AND warning_sent_at IS NOT NULL "
                "AND state IN ('member','banning')",
                (ts, chat_id),
            )
            return cur.rowcount

    def set_group_inactivity_days(self, chat_id: int, days: int, updated_by: int) -> int:
        return self._set_group_setting_and_refresh_warnings(chat_id, "activity_days", days, updated_by)

    def set_group_warning_hours(self, chat_id: int, hours: int, updated_by: int) -> int:
        return self._set_group_setting_and_refresh_warnings(chat_id, "warning_hours", hours, updated_by)

    def clear_warning_if_unchanged(
        self, chat_id: int, user_id: int, expected_last_activity: int, expected_warning_sent_at: int
    ) -> None:
        """Remove an old warning if a raised threshold makes the member not yet due."""
        with self._connect() as db:
            db.execute(
                "UPDATE members SET warning_sent_at=NULL, state='member' "
                "WHERE chat_id=? AND user_id=? AND state='member' "
                "AND last_activity=? AND warning_sent_at=?",
                (chat_id, user_id, expected_last_activity, expected_warning_sent_at),
            )


def get_store(context: ContextTypes.DEFAULT_TYPE) -> Store:
    return context.application.bot_data["store"]


def group_add_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    if bot_username:
        url = (
            f"https://t.me/{bot_username}?startgroup=true"
            "&admin=restrict_members"
        )
        return InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add to Group", url=url)]])
    return InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add to Group", url="https://t.me/")]])


def is_private_chat(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == ChatType.PRIVATE)


def is_group_chat(update: Update) -> bool:
    return bool(
        update.effective_chat
        and update.effective_chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
    )


async def can_manage_current_group(
    update: Update, context: ContextTypes.DEFAULT_TYPE, store: Store
) -> bool:
    """Owner/Bot Admin may manage settings; regular users must admin THIS group."""
    chat = update.effective_chat
    actor = update.effective_user
    if not chat or not actor or not is_group_chat(update):
        return False
    if actor.id in OWNER_IDS or store.role_for(actor.id) == "admin":
        return True
    try:
        member = await context.bot.get_chat_member(chat.id, actor.id)
    except TelegramError as exc:
        log.info("Could not verify settings permission for %s in %s: %s", actor.id, chat.id, exc)
        return False
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}


def has_bot_admin_access(store: Store, user_id: int) -> bool:
    return store.role_for(user_id) in {"owner", "admin"}


async def lockall_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_private_chat(update):
        # Bot-management commands must be completely silent outside the private bot chat.
        return
    store = get_store(context)
    store.remember_user(user)
    if not has_bot_admin_access(store, user.id):
        await message.reply_text("⛔ Aapko is command ki permission nahi hai.")
        return

    args = [arg.casefold() for arg in context.args]
    if len(args) > 1 or (args and args[0] not in {"on", "off"}):
        await message.reply_text("Use: /lockall, /lockall on, ya /lockall off")
        return

    if args:
        enabled = args[0] == "on"
    else:
        # Bare /lockall enables the restriction; /lockall off disables it.
        enabled = True
    store.set_lockall(enabled)
    if enabled:
        await message.reply_text(
            "🔒 <b>Lockall ENABLED</b>\n\n"
            "Ab sirf bot mein approved users bot ko groups mein add kar sakte hain. "
            "Unapproved user add karega toh bot permission warning dekar group leave karega.\n\n"
            "Disable karne ke liye /lockall off bhejein.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await message.reply_text(
            "🔓 <b>Lockall DISABLED</b>\n\n"
            "Ab koi bhi user bot ko group mein add kar sakta hai.",
            parse_mode=ParseMode.HTML,
        )


async def approve_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    actor = update.effective_user
    if not message or not actor:
        return
    if not is_private_chat(update):
        # Bot-management commands must be completely silent outside the private bot chat.
        return

    store = get_store(context)
    store.remember_user(actor)
    if not has_bot_admin_access(store, actor.id):
        await message.reply_text("⛔ Aapko is command ki permission nahi hai.")
        return
    if len(context.args) != 1:
        await message.reply_text(
            "Use: <code>/approve USER_ID</code> ya <code>/approve @username</code>\n\n"
            "@username se approval ke liye target user ko pehle bot par /start karna hoga ya kisi aise group mein "
            "message bhejna hoga jahan bot usse dekh sake. Numeric user ID seedha use kar sakte hain.",
            parse_mode=ParseMode.HTML,
        )
        return

    target_arg = context.args[0].strip()
    target_id: Optional[int] = None
    if target_arg.lstrip("+").isdigit():
        target_id = int(target_arg)
    elif target_arg.startswith("@"):
        target_id = store.known_user_by_username(target_arg)
        if target_id is None:
            await message.reply_text(
                "❓ Is username ko main abhi identify nahi kar pa raha. User ID use karein, "
                "ya target user se pehle bot par /start karwayein.",
            )
            return
    else:
        await message.reply_text("User ID ya @username dijiye. Example: <code>/approve 123456789</code>", parse_mode=ParseMode.HTML)
        return

    if target_id <= 0:
        await message.reply_text("❌ Valid positive Telegram user ID dijiye.")
        return
    if target_id == actor.id:
        await message.reply_text("ℹ️ Aap apne aap ko approve nahi kar sakte.")
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Sirf Bot Use Karne Dein", callback_data=f"approve:user:{target_id}")],
        [InlineKeyboardButton("👑 Bot Admin Banayein", callback_data=f"approve:admin:{target_id}")],
    ])
    await message.reply_text(
        f"<b>Approve User</b>\n\nTarget Telegram ID: <code>{target_id}</code>\n\n"
        "Choose permission:",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
    )


async def approve_choice_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    actor = update.effective_user
    if not query or not actor:
        return
    if not query.message or query.message.chat.type != ChatType.PRIVATE:
        await query.answer("Ye option sirf bot ki private chat mein use ho sakta hai.", show_alert=True)
        return

    store = get_store(context)
    if not has_bot_admin_access(store, actor.id):
        await query.answer("Aapko approval manage karne ki permission nahi hai.", show_alert=True)
        return

    try:
        _prefix, role, raw_target_id = str(query.data).split(":", 2)
        target_id = int(raw_target_id)
    except (ValueError, AttributeError):
        await query.answer()
        await query.edit_message_text("❌ Invalid approval request.")
        return

    if role not in {"user", "admin"} or target_id <= 0:
        await query.answer()
        await query.edit_message_text("❌ Invalid approval role or user ID.")
        return
    if target_id == actor.id:
        await query.answer()
        await query.edit_message_text("ℹ️ Aap apne aap ko approve nahi kar sakte.")
        return
    if role == "admin" and actor.id not in OWNER_IDS:
        await query.answer("Naya Bot Admin sirf bot owner bana sakta hai.", show_alert=True)
        return

    await query.answer()
    store.approve_user(target_id, role, actor.id)
    role_label = "Bot Admin" if role == "admin" else "Bot Use Only"
    await query.edit_message_text(
        "✅ <b>Approval saved</b>\n\n"
        f"User ID: <code>{target_id}</code>\n"
        f"Permission: <b>{role_label}</b>\n\n"
        "Lockall ON hone par approved users bot ko groups mein add/use kar sakte hain. "
        "Bot Admin /lockall aur /approve manage kar sakta hai.",
        parse_mode=ParseMode.HTML,
    )


async def inactive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    # These commands are intentionally silent in private chat; they are group-only.
    if not message or not actor or not chat or not is_group_chat(update):
        return
    store = get_store(context)
    if not await can_manage_current_group(update, context, store):
        await message.reply_text("⛔ Is GC ke admin, approved Bot Admin, ya bot owner hi inactivity settings badal sakte hain.")
        return
    days, hours = store.group_inactivity_settings(chat.id)
    title = html.escape(getattr(chat, "title", "") or "This group")
    await message.reply_text(
        f"⚙️ <b>Inactivity settings — {title}</b>\n\n"
        f"• Activity limit: <b>{days} days</b>\n"
        f"• Warning grace period: <b>{hours} hours</b>\n\n"
        "Sirf is group ki settings change hongi.\n\n"
        "Commands:\n"
        "<code>/setinactive 20</code> — is group ki inactivity limit 20 din karein\n"
        "<code>/setwarning 48</code> — is group mein warning ke baad 48 ghante dein\n"
        "<code>/inactive</code> — is group ki settings dekhein",
        parse_mode=ParseMode.HTML,
    )


async def setinactive_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    if not message or not actor or not chat or not is_group_chat(update):
        return
    store = get_store(context)
    if not await can_manage_current_group(update, context, store):
        await message.reply_text("⛔ Is GC ke admin, approved Bot Admin, ya bot owner hi inactivity settings badal sakte hain.")
        return
    if len(context.args) != 1:
        await message.reply_text(
            "Use: <code>/setinactive DAYS</code>\nExample: <code>/setinactive 20</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        days = int(context.args[0])
    except ValueError:
        days = 0
    if not 1 <= days <= 365:
        await message.reply_text("❌ Days 1 se 365 ke beech number hona chahiye.")
        return
    old_days, hours = store.group_inactivity_settings(chat.id)
    reset_count = store.set_group_inactivity_days(chat.id, days, actor.id)
    await message.reply_text(
        "✅ <b>Is group ki inactivity limit update ho gayi</b>\n\n"
        f"Old limit: {old_days} days\nNew limit: <b>{days} days</b>\n"
        f"Warning period: {hours} hours\n\n"
        f"{reset_count} existing warning(s) ko isi group mein fresh grace period mila. "
        "Doosre groups ki settings nahi badli.",
        parse_mode=ParseMode.HTML,
    )


async def setwarning_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    actor = update.effective_user
    chat = update.effective_chat
    if not message or not actor or not chat or not is_group_chat(update):
        return
    store = get_store(context)
    if not await can_manage_current_group(update, context, store):
        await message.reply_text("⛔ Is GC ke admin, approved Bot Admin, ya bot owner hi inactivity settings badal sakte hain.")
        return
    if len(context.args) != 1:
        await message.reply_text(
            "Use: <code>/setwarning HOURS</code>\nExample: <code>/setwarning 48</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        hours = int(context.args[0])
    except ValueError:
        hours = 0
    if not 1 <= hours <= 720:
        await message.reply_text("❌ Hours 1 se 720 ke beech number hona chahiye.")
        return
    days, old_hours = store.group_inactivity_settings(chat.id)
    reset_count = store.set_group_warning_hours(chat.id, hours, actor.id)
    await message.reply_text(
        "✅ <b>Is group ka warning period update ho gaya</b>\n\n"
        f"Old period: {old_hours} hours\nNew period: <b>{hours} hours</b>\n"
        f"Activity limit: {days} days\n\n"
        f"{reset_count} existing warning(s) ko isi group mein ab se naya full grace period milega. "
        "Doosre groups ki settings nahi badli.",
        parse_mode=ParseMode.HTML,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or chat.type != ChatType.PRIVATE:
        # /start in a group/channel is intentionally ignored: no reply and no side effects.
        return

    store = get_store(context)
    if user and not user.is_bot:
        store.remember_user(user)

    # These are defaults for the private welcome only. Actual moderation settings are per-group.
    welcome_text = build_welcome_text(ACTIVITY_DAYS, WARNING_HOURS)
    bot_user = await context.bot.get_me()
    keyboard = group_add_keyboard(bot_user.username or "")
    try:
        if LOGO_PATH.exists():
            with LOGO_PATH.open("rb") as photo:
                await message.reply_photo(
                    photo=photo,
                    caption=welcome_text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
        else:
            await message.reply_text(
                welcome_text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=True,
            )
    except BadRequest as exc:
        # Some logo revisions/caption lengths may be rejected by Telegram; text fallback keeps /start usable.
        log.warning("Welcome photo failed, sending text instead: %s", exc)
        await message.reply_text(
            welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )


async def track_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    store = get_store(context)
    if not store.has_group(chat.id):
        return

    # Treat any ordinary user message (text, media, sticker, etc.) as activity.
    if user and not user.is_bot:
        store.remember_user(user)
        store.track_activity(chat.id, user)

    # Track people added using a Telegram service message. They start with a fresh clock.
    for joined_user in (message.new_chat_members or []):
        if not joined_user.is_bot:
            store.remember_user(joined_user)
            store.track_activity(chat.id, joined_user, activity_ts=now_ts())


async def chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.chat_member
    if not event or event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    store = get_store(context)
    chat = event.chat
    if not store.has_group(chat.id):
        return
    changed_user = event.new_chat_member.user
    store.remember_user(changed_user)
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    if changed_user.is_bot:
        return

    if new_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
        store.mark_left(chat.id, changed_user.id)
    elif new_status in {
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.OWNER,
        ChatMemberStatus.RESTRICTED,
    }:
        active_statuses = {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        }
        is_new_or_reinstated_member = (
            old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
            and new_status in active_statuses
        )
        is_former_admin_becoming_regular = (
            old_status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}
            and new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}
        )
        if is_new_or_reinstated_member or is_former_admin_becoming_regular:
            # Clears stale warnings/state and starts the full inactivity period again.
            store.track_activity(chat.id, changed_user, activity_ts=now_ts())
            log.info(
                "Fresh inactivity timer started for %s in %s after membership transition %s -> %s",
                changed_user.id, chat.id, old_status, new_status,
            )
        else:
            store.ensure_member_if_missing(chat.id, changed_user)


async def my_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    event = update.my_chat_member
    if not event or event.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    chat = event.chat
    store = get_store(context)
    old_status = event.old_chat_member.status
    new_status = event.new_chat_member.status

    # When lockall is enabled, reject an initial group addition unless the inviter
    # was explicitly approved. An approved "Bot Use Only" user may add/use the bot,
    # but cannot administer bot access or lockall; only owners/bot-admins can do that.
    initial_add = (
        old_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}
        and new_status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.RESTRICTED}
    ) or (
        old_status == ChatMemberStatus.MEMBER
        and new_status == ChatMemberStatus.ADMINISTRATOR
        and not store.has_group(chat.id)
    )
    if initial_add and store.lockall_enabled():
        inviter = getattr(event, "from_user", None)
        inviter_role = store.role_for(inviter.id) if inviter and not inviter.is_bot else None
        if inviter_role not in {"owner", "admin", "user"}:
            try:
                await context.bot.send_message(
                    chat.id,
                    "⛔ <b>Aapke paas permission nahi hai!</b>\n\n"
                    "ID Inactive &amp; Deleted ka group-add access restricted hai. "
                    "Bot owner se approval lein.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError as exc:
                log.info("Unable to send lockall denial in %s: %s", chat.id, exc)
            try:
                await context.bot.leave_chat(chat.id)
            except TelegramError as exc:
                log.warning("Unable to leave unauthorized group %s: %s", chat.id, exc)
            store.disable_group(chat.id)
            log.info("Rejected unapproved addition to group %s by %s", chat.id, getattr(inviter, "id", None))
            return

    if new_status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.MEMBER}:
        store.disable_group(chat.id)
        return

    if new_status == ChatMemberStatus.ADMINISTRATOR:
        can_ban = bool(getattr(event.new_chat_member, "can_restrict_members", False))
        if not can_ban:
            store.disable_group(chat.id)
            try:
                await context.bot.send_message(
                    chat.id,
                    "⚠️ <b>ID Inactive &amp; Deleted is not active yet.</b>\n\n"
                    "Please enable <b>Ban Users / Restrict Members</b> in my administrator permissions. "
                    "After you grant it, protection will start automatically.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError as exc:
                log.info("Unable to post permission note in %s: %s", chat.id, exc)
            return

        store.ensure_group(chat.id, chat.title or "Telegram group")
        if old_status != ChatMemberStatus.ADMINISTRATOR:
            activity_days, warning_hours = store.group_inactivity_settings(chat.id)
            try:
                await context.bot.send_message(
                    chat.id,
                    "🛡️ <b>ID Inactive &amp; Deleted activated</b>\n\n"
                    f"I track messages in this group. After {activity_days} days without a message, a member receives a warning. "
                    f"If they remain inactive for another {warning_hours} hours, I will ban them.\n\n"
                    "No setup commands are needed.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError as exc:
                log.info("Unable to post activation note in %s: %s", chat.id, exc)


async def scan_group(app: Application, chat_id: int, title: str) -> None:
    store: Store = app.bot_data["store"]
    activity_days, warning_hours = store.group_inactivity_settings(chat_id)
    activity_limit = activity_days * 24 * 60 * 60
    warning_period = warning_hours * 60 * 60

    for row in store.due_members(chat_id):
        uid = int(row["user_id"])
        scan_last_activity = int(row["last_activity"])
        scan_warned_at = int(row["warning_sent_at"]) if row["warning_sent_at"] is not None else None

        if now_ts() - scan_last_activity < activity_limit:
            # If the inactivity limit was raised after a warning, remove that stale
            # warning so the member gets a fresh warning when the new threshold is met.
            if scan_warned_at is not None:
                store.clear_warning_if_unchanged(chat_id, uid, scan_last_activity, scan_warned_at)
            continue

        # Ask Telegram for current membership before deciding to warn/ban.
        try:
            current = await app.bot.get_chat_member(chat_id, uid)
        except Forbidden as exc:
            log.warning("Cannot inspect members in group %s; check admin permissions: %s", chat_id, exc)
            return
        except BadRequest as exc:
            # A slow lookup must never overwrite a newer message, join, or unban event.
            if "user not found" in str(exc).lower() or "participant_id_invalid" in str(exc).lower():
                store.mark_left_if_unchanged(chat_id, uid, scan_last_activity)
            else:
                log.info("Skipping member %s in %s: %s", uid, chat_id, exc)
            continue
        except TelegramError as exc:
            log.warning("Temporary Telegram API error for member %s in %s: %s", uid, chat_id, exc)
            continue

        # Re-read after the network call. If activity or membership handling changed
        # the row while Telegram was responding, this scan must not use its stale data.
        fresh = store.get_member(chat_id, uid)
        if not fresh or fresh["state"] != "member":
            continue
        fresh_last_activity = int(fresh["last_activity"])
        if fresh_last_activity != scan_last_activity:
            continue
        # A command may have changed either threshold while the Telegram membership
        # lookup was in flight. Use the latest persisted configuration for this decision.
        activity_days, warning_hours = store.group_inactivity_settings(chat_id)
        activity_limit = activity_days * 24 * 60 * 60
        warning_period = warning_hours * 60 * 60
        now = now_ts()
        if now - fresh_last_activity < activity_limit:
            continue
        fresh_warned_at = int(fresh["warning_sent_at"]) if fresh["warning_sent_at"] is not None else None

        status = current.status
        if status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
            # Conditional update avoids a stale membership response resetting a fresh join.
            store.mark_left_if_unchanged(chat_id, uid, fresh_last_activity)
            continue
        if status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            # Group admins/owners are exempt; don't mutate state based on a stale scan.
            continue

        name = html.escape((current.user.full_name or current.user.username or "Member").strip())
        if current.user.username:
            mention = f"@{html.escape(current.user.username)}"
        else:
            mention = f'<a href="tg://user?id={uid}">{name}</a>'

        if fresh_warned_at is None:
            warning_text = (
                "⚠️ <b>INACTIVITY WARNING</b>\n\n"
                f"👤 Member: {mention}\n\n"
                f"You have not sent a message in this group for {activity_days} days.\n\n"
                f"⏳ You have {warning_hours} hours to send a message here.\n"
                "If you remain inactive until the deadline, you will be banned.\n\n"
                "📝 <b>Reason:</b> Inactive for too long\n"
                "🛡️ ID Inactive &amp; Deleted"
            )
            try:
                await app.bot.send_message(
                    chat_id, warning_text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
                )
                # Save the warning only if no message arrived while send_message awaited Telegram.
                store.set_warning(chat_id, uid, now_ts(), expected_last_activity=fresh_last_activity)
            except TelegramError as exc:
                log.warning("Unable to warn %s in %s: %s", uid, chat_id, exc)
            continue

        if now < fresh_warned_at + warning_period:
            continue

        # Claim this exact activity/warning generation atomically. A new message or
        # reinstatement resets the row to 'member', causing this claim to fail or expire.
        if not store.claim_ban(chat_id, uid, fresh_last_activity, fresh_warned_at):
            continue

        claimed = store.get_member(chat_id, uid)
        if (
            not claimed
            or claimed["state"] != "banning"
            or int(claimed["last_activity"]) != fresh_last_activity
            or claimed["warning_sent_at"] is None
            or int(claimed["warning_sent_at"]) != fresh_warned_at
            or now_ts() - int(claimed["last_activity"]) < activity_limit
            or now_ts() < int(claimed["warning_sent_at"]) + warning_period
        ):
            store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            continue

        # Re-check Telegram membership after claiming and before making the ban API
        # call. This catches an unban/leave/rejoin that happened during the earlier lookup.
        try:
            pre_ban_member = await app.bot.get_chat_member(chat_id, uid)
        except TelegramError as exc:
            store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            log.warning("Pre-ban membership recheck failed for %s in %s: %s", uid, chat_id, exc)
            continue

        pre_ban_row = store.get_member(chat_id, uid)
        if (
            not pre_ban_row
            or pre_ban_row["state"] != "banning"
            or int(pre_ban_row["last_activity"]) != fresh_last_activity
            or pre_ban_row["warning_sent_at"] is None
            or int(pre_ban_row["warning_sent_at"]) != fresh_warned_at
        ):
            store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            continue

        if pre_ban_member.status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
            if pre_ban_member.status == ChatMemberStatus.BANNED:
                store.finish_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            else:
                store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
                store.mark_left_if_unchanged(chat_id, uid, fresh_last_activity)
            continue
        if pre_ban_member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            continue

        # Settings may have changed while this scan awaited Telegram; re-read the
        # thresholds before making a destructive API call.
        latest_days, latest_hours = store.group_inactivity_settings(chat_id)
        latest_now = now_ts()
        if (
            latest_now - fresh_last_activity < latest_days * 24 * 60 * 60
            or latest_now < fresh_warned_at + latest_hours * 60 * 60
        ):
            store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            continue

        # Crucial race fix: do not unconditionally write 'kicked' after Telegram's
        # ban request. If an admin unbans/rejoins during the request, chat_member_update
        # records the fresh baseline and finish_ban_claim will leave it untouched.
        try:
            await app.bot.ban_chat_member(chat_id=chat_id, user_id=uid, revoke_messages=False)
            store.finish_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            log.info("Ban request succeeded for inactive member %s from %s", uid, chat_id)
        except TelegramError as exc:
            store.release_ban_claim(chat_id, uid, fresh_last_activity, fresh_warned_at)
            log.error("Could not ban inactive member %s from %s: %s", uid, chat_id, exc)
            continue

        # If an admin quickly unbanned the member while the API call was in flight,
        # avoid posting a misleading ban notice and ensure a currently present member
        # receives a fresh baseline. This never writes a stale 'kicked' state.
        still_banned = True
        try:
            after_ban = await app.bot.get_chat_member(chat_id, uid)
            still_banned = after_ban.status == ChatMemberStatus.BANNED
            if not still_banned and after_ban.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}:
                # Current Telegram membership wins over a stale DB state left by the
                # just-finished API request. Starting a fresh clock is safe and idempotent.
                store.track_activity(chat_id, after_ban.user, activity_ts=now_ts())
        except TelegramError as exc:
            # The ban API succeeded. A follow-up lookup can fail transiently; do not
            # change database state based on an unavailable answer.
            log.info("Post-ban membership check failed for %s in %s: %s", uid, chat_id, exc)

        if still_banned:
            try:
                await app.bot.send_message(
                    chat_id,
                    "🚫 <b>MEMBER BANNED</b>\n\n"
                    f"👤 Member: {mention}\n"
                    "📝 <b>Reason:</b> Inactive for too long\n"
                    f"⏱️ No group message for {activity_days} days, followed by a {warning_hours}-hour warning period.\n\n"
                    "🛡️ ID Inactive &amp; Deleted",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
            except TelegramError as exc:
                log.warning("Ban succeeded but announcement failed for %s in %s: %s", uid, chat_id, exc)


async def inactivity_loop(app: Application) -> None:
    while True:
        try:
            groups = app.bot_data["store"].group_ids()
            for group in groups:
                await scan_group(app, int(group["chat_id"]), str(group["title"] or "Telegram group"))
                # Avoid a tight burst of API calls across many groups.
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Unexpected error during inactivity scan")
        await asyncio.sleep(CHECK_INTERVAL_MINUTES * 60)


async def post_init(app: Application) -> None:
    app.bot_data["store"] = Store(DB_PATH)
    bot_user = await app.bot.get_me()
    log.info("Starting %s as @%s", APP_NAME, bot_user.username)
    app.create_task(inactivity_loop(app), name="inactivity-scan-loop")


async def post_shutdown(app: Application) -> None:
    log.info("%s stopped", APP_NAME)


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN is missing. Copy .env.example to .env and paste your token from @BotFather."
        )

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("lockall", lockall_command))
    app.add_handler(CommandHandler("approve", approve_command))
    app.add_handler(CommandHandler("inactive", inactive_command))
    app.add_handler(CommandHandler("setinactive", setinactive_command))
    app.add_handler(CommandHandler("setwarning", setwarning_command))
    app.add_handler(CallbackQueryHandler(approve_choice_callback, pattern=r"^approve:(user|admin):\d+$"))
    app.add_handler(ChatMemberHandler(chat_member_update, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(ChatMemberHandler(my_chat_member_update, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS, track_group_message))
    app.run_polling(
        allowed_updates=["message", "my_chat_member", "chat_member"],
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
