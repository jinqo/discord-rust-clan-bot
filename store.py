"""
Persistent InviteStore extracted from bot.py (refactoring for separation of concerns).

This module was created by extracting the entire InviteStore class (~500 lines),
the top-level `store = InviteStore(PENDING_FILE)`, and the related `parse_bm_server_id`
helper from the original bot.py.

All behavior is preserved exactly:
- asyncio.Lock for concurrent safety
- Atomic writes via .tmp + os.replace()
- Full public + internal API (get_*, set_*, _*, data shape with config/guilds/tryout_tickets/...)
- All logger calls, datetime handling, etc.

Import in bot.py with: from store import store, parse_bm_server_id
The global `store` singleton continues to work for all existing call sites.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional


# ============================================================
# CONFIG & PATHS (moved from bot.py; only used for the store)
# ============================================================
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

PENDING_FILE = DATA_DIR / "pending.json"


# Use the same logger name so that logs from the store integrate with the
# configuration performed in bot.py (level, format, handlers).
logger = logging.getLogger("accept-bot")


# ============================================================
# PERSISTENT STORAGE (JSON)
# ============================================================
class InviteStore:
    """
    Persistent storage for:
    - Main / Tryout guild configuration (cross-server setup)
    - Per-guild default roles and pending invites (we primarily use the main guild's entry)
    """

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.data: dict = {"config": {}, "guilds": {}}
        self._lock = asyncio.Lock()  # Protects concurrent reads/writes from multiple commands, events, tasks
        self._load()

    def _load(self):
        """Synchronous load at startup. Protected against corrupt file."""
        if self.filepath.exists():
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                if "guilds" not in self.data:
                    self.data["guilds"] = {}
                if "config" not in self.data:
                    self.data["config"] = {}
            except Exception as e:
                logger.error(f"Failed to load pending.json: {e}")
                self.data = {"config": {}, "guilds": {}}
        else:
            self._save()

    async def _async_save(self):
        """Async + atomic + locked save. Never blocks the event loop and prevents concurrent overwrites."""
        async with self._lock:
            def _atomic_write():
                tmp_path = self.filepath.with_suffix(self.filepath.suffix + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.filepath)  # atomic on POSIX + Windows

            await asyncio.to_thread(_atomic_write)

    def _save(self):
        """Safe save from any context (sync API kept for minimal caller changes).
        In async context it uses proper async I/O + lock.
        """
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._async_save())
        except RuntimeError:
            # Fallback during startup / no loop
            try:
                tmp_path = self.filepath.with_suffix(self.filepath.suffix + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.filepath)
            except Exception as e:
                logger.error(f"Failed to save pending.json (sync fallback): {e}")

    def _get_guild(self, guild_id: int) -> dict:
        gid = str(guild_id)
        if gid not in self.data["guilds"]:
            self.data["guilds"][gid] = {"default_role": None, "pending": {}}
        return self.data["guilds"][gid]

    # --- Server configuration (Main / Tryout) ---
    def get_main_guild_id(self) -> Optional[int]:
        val = self.data["config"].get("main_guild_id")
        return int(val) if val else None

    def set_main_guild(self, guild_id: int, channel_id: Optional[int] = None):
        self.data["config"]["main_guild_id"] = guild_id
        if channel_id:
            self.data["config"]["main_channel_id"] = channel_id
        self._save()
        logger.info(f"Main guild set to {guild_id} (channel: {channel_id})")

    def get_tryout_guild_id(self) -> Optional[int]:
        val = self.data["config"].get("tryout_guild_id")
        return int(val) if val else None

    def get_main_channel_id(self) -> Optional[int]:
        val = self.data["config"].get("main_channel_id")
        return int(val) if val else None

    # --- BattleMetrics integration ---
    def set_bm_server(self, server_id: str):
        parsed = parse_bm_server_id(server_id) or str(server_id).strip()
        self.data["config"]["bm_server_id"] = parsed
        self._save()
        logger.info(f"BattleMetrics server set to {parsed}")

    def get_bm_server_id(self) -> Optional[str]:
        val = self.data["config"].get("bm_server_id")
        return str(val) if val else None

    def set_bm_aim_server(self, server_id: str):
        parsed = parse_bm_server_id(server_id) or str(server_id).strip()
        self.data["config"]["bm_aim_server_id"] = parsed
        self._save()
        logger.info(f"BattleMetrics Aim Training server set to {parsed}")

    def get_bm_aim_server_id(self) -> Optional[str]:
        val = self.data["config"].get("bm_aim_server_id")
        return str(val) if val else None

    def set_bm_building_server(self, server_id: str):
        parsed = parse_bm_server_id(server_id) or str(server_id).strip()
        self.data["config"]["bm_building_server_id"] = parsed
        self._save()
        logger.info(f"BattleMetrics Building server set to {parsed}")

    def get_bm_building_server_id(self) -> Optional[str]:
        val = self.data["config"].get("bm_building_server_id")
        return str(val) if val else None

    # --- Live Server Stats ---
    def set_live_stats_channel(self, channel_id: int):
        self.data["config"]["live_stats_channel_id"] = channel_id
        self._save()
        logger.info(f"Live stats channel set to {channel_id}")

    def get_live_stats_channel_id(self) -> Optional[int]:
        val = self.data["config"].get("live_stats_channel_id")
        return int(val) if val else None

    def set_active_live(self, server_id: str, channel_id: int, message_id: int):
        self.data["config"]["active_live"] = {
            "server_id": server_id,
            "channel_id": channel_id,
            "message_id": message_id
        }
        self._save()

    def get_active_live(self) -> Optional[dict]:
        return self.data["config"].get("active_live")

    def clear_active_live(self):
        if "active_live" in self.data["config"]:
            del self.data["config"]["active_live"]
            self._save()

    # --- Clan & Linking Config ---
    def set_clan_invite_channel(self, channel_id: int):
        self.data["config"]["clan_invite_channel_id"] = channel_id
        self._save()

    def get_clan_invite_channel_id(self) -> Optional[int]:
        val = self.data["config"].get("clan_invite_channel_id")
        return int(val) if val else None

    def set_clan_owner_role(self, role_id: int):
        self.data["config"]["clan_owner_role_id"] = role_id
        self._save()

    def get_clan_owner_role_id(self) -> Optional[int]:
        val = self.data["config"].get("clan_owner_role_id")
        return int(val) if val else None

    # --- Logging channel ---
    def set_log_channel(self, channel_id: int):
        self.data["config"]["log_channel_id"] = channel_id
        self._save()
        logger.info(f"Log channel set to {channel_id}")

    def get_log_channel_id(self) -> Optional[int]:
        val = self.data["config"].get("log_channel_id")
        return int(val) if val else None

    # --- Tryout ticket category (for auto Steam checks in ticket channels created by external ticket bot) ---
    def set_tryout_ticket_category(self, category_id: int):
        self.data["config"]["tryout_ticket_category_id"] = category_id
        self._save()
        logger.info(f"Tryout ticket category set to {category_id}")

    def get_tryout_ticket_category_id(self) -> Optional[int]:
        val = self.data["config"].get("tryout_ticket_category_id")
        return int(val) if val else None

    def set_tryout_ticket(self, thread_id: int, owner_id: int, steamid64: Optional[str] = None, verified_public: bool = False, verification_msg_id: Optional[int] = None):
        if "tryout_tickets" not in self.data["config"]:
            self.data["config"]["tryout_tickets"] = {}
        self.data["config"]["tryout_tickets"][str(thread_id)] = {
            "owner_id": owner_id,
            "steamid64": steamid64,
            "verified_public": verified_public,
            "verification_msg_id": verification_msg_id,
            "updated": int(datetime.now(timezone.utc).timestamp())
        }
        self._save()

    def get_tryout_ticket(self, thread_id: int) -> Optional[dict]:
        tickets = self.data["config"].get("tryout_tickets", {})
        data = tickets.get(str(thread_id))
        return data

    def update_tryout_ticket(self, thread_id: int, **kwargs):
        tickets = self.data["config"].get("tryout_tickets", {})
        key = str(thread_id)
        if key not in tickets:
            return
        tickets[key].update(kwargs)
        if "updated" not in kwargs:
            tickets[key]["updated"] = int(datetime.now(timezone.utc).timestamp())
        self._save()

    def get_tryout_ticket_for_user(self, user_id: int) -> Optional[tuple[int, dict]]:
        """Return (thread_id, ticket_data) for the user's current open ticket if any."""
        tickets = self.data["config"].get("tryout_tickets", {})
        for tid_str, data in tickets.items():
            if data.get("owner_id") == user_id:
                return int(tid_str), data
        return None

    def link_steam(self, discord_id: int, steam64: str):
        if "steam_links" not in self.data["config"]:
            self.data["config"]["steam_links"] = {}
        self.data["config"]["steam_links"][str(discord_id)] = steam64
        self._save()

    def get_linked_steam(self, discord_id: int) -> Optional[str]:
        links = self.data["config"].get("steam_links", {})
        return links.get(str(discord_id))

    # --- Invite-only / acceptance required mode for main server ---
    def is_require_acceptance(self) -> bool:
        return bool(self.data["config"].get("require_acceptance", False))

    def set_require_acceptance(self, enabled: bool):
        self.data["config"]["require_acceptance"] = bool(enabled)
        self._save()
        logger.info(f"Require acceptance mode set to {enabled}")

    def get_whitelisted_members(self) -> set[int]:
        members = self.data["config"].get("whitelisted_members", [])
        return {int(m) for m in members}

    def add_to_whitelist(self, user_id: int):
        current = set(self.get_whitelisted_members())
        current.add(int(user_id))
        self.data["config"]["whitelisted_members"] = list(current)
        self._save()

    def snapshot_current_members(self, guild: discord.Guild):
        """Call this when enabling invite-only mode to protect existing members."""
        current = {m.id for m in guild.members}
        existing = self.get_whitelisted_members()
        merged = existing | current
        self.data["config"]["whitelisted_members"] = list(merged)
        self._save()
        logger.info(f"Whitelisted snapshot taken for main guild. Total protected: {len(merged)}")

    def set_tryout_guild(self, guild_id: int):
        self.data["config"]["tryout_guild_id"] = guild_id
        self._save()
        logger.info(f"Tryout guild set to {guild_id}")

    def get_main_guild(self, bot: commands.Bot) -> Optional[discord.Guild]:
        mid = self.get_main_guild_id()
        return bot.get_guild(mid) if mid else None

    # --- Default role (stored under main guild) ---
    def get_default_role(self, guild_id: int) -> Optional[int]:
        return self._get_guild(guild_id).get("default_role")

    def set_default_role(self, guild_id: int, role_id: Optional[int]):
        g = self._get_guild(guild_id)
        g["default_role"] = role_id
        self._save()
        logger.info(f"Default role for guild {guild_id} set to {role_id}")

    # --- Pending invites (we store under the main guild id) ---
    # Now stores rich info: role_id, invite_code, created_by (who did /accept), created_at
    def add_pending(self, guild_id: int, user_id: int, role_id: Optional[int] = None, invite_code: Optional[str] = None, created_by: Optional[int] = None):
        g = self._get_guild(guild_id)
        entry = {
            "role_id": role_id,
            "invite_code": invite_code,
            "created_by": created_by,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        g["pending"][str(user_id)] = entry
        self._save()
        logger.info(f"Pending added: guild={guild_id} user={user_id} role={role_id} code={invite_code} by={created_by}")

    def get_pending_role(self, guild_id: int, user_id: int) -> Optional[int]:
        g = self._get_guild(guild_id)
        val = g["pending"].get(str(user_id))
        if isinstance(val, dict):
            return val.get("role_id")
        return val  # old format compat

    def get_pending_info(self, guild_id: int, user_id: int) -> Optional[dict]:
        """Return full pending entry (with invite_code, created_by etc) or None."""
        g = self._get_guild(guild_id)
        val = g["pending"].get(str(user_id))
        if isinstance(val, dict):
            return val
        elif val is not None:
            return {"role_id": val}
        return None

    def remove_pending(self, guild_id: int, user_id: int):
        g = self._get_guild(guild_id)
        if str(user_id) in g["pending"]:
            del g["pending"][str(user_id)]
            self._save()
            logger.info(f"Pending removed: guild={guild_id} user={user_id}")

    def get_all_pending(self, guild_id: int) -> dict[str, Optional[int]]:
        g = self._get_guild(guild_id)
        result = {}
        for uid, val in g.get("pending", {}).items():
            if isinstance(val, dict):
                result[uid] = val.get("role_id")
            else:
                result[uid] = val
        return result

    def clear_pending(self, guild_id: int):
        g = self._get_guild(guild_id)
        g["pending"] = {}
        self._save()
        logger.info(f"All pending cleared for guild {guild_id}")

    def clear_specific(self, guild_id: int, user_id: int):
        self.remove_pending(guild_id, user_id)

    # --- Wipe React System ---
    def set_wipe_channel(self, channel_id: int):
        self.data["config"]["wipe_channel_id"] = channel_id
        self._save()
        logger.info(f"Wipe channel set to {channel_id}")

    def get_wipe_channel_id(self) -> Optional[int]:
        val = self.data["config"].get("wipe_channel_id")
        return int(val) if val else None

    def set_reason_channel(self, channel_id: int):
        self.data["config"]["reason_channel_id"] = channel_id
        self._save()
        logger.info(f"Reason channel set to {channel_id}")

    def get_reason_channel_id(self) -> Optional[int]:
        val = self.data["config"].get("reason_channel_id")
        return int(val) if val else None

    def add_wipe_schedule(self, push_day: int, wipe_day: int, hour: int, minute: int, title: Optional[str] = None, description: Optional[str] = None, timezone: str = "UTC"):
        if "wipe_schedules" not in self.data["config"]:
            self.data["config"]["wipe_schedules"] = []
        sched = {
            "id": str(len(self.data["config"]["wipe_schedules"]) + 1),
            "push_day": push_day,
            "wipe_day": wipe_day,
            "hour": hour,
            "minute": minute,
            "timezone": timezone or "UTC",
            "title": title or "🗓️ Wipe React",
            "description": description or "React below if you're playing this wipe!\n\n✅ Accept - I'm playing\n⏰ Late - I'll be late\n❌ Decline - Can't make it (please give reason)",
            "last_posted": None
        }
        self.data["config"]["wipe_schedules"].append(sched)
        self._save()
        logger.info(f"Wipe schedule added: id={sched['id']} push_day={push_day} wipe_day={wipe_day} {hour:02d}:{minute:02d} tz={timezone}")
        return sched["id"]

    def get_wipe_schedules(self) -> list:
        return self.data["config"].get("wipe_schedules", [])

    def set_wipe_schedule(self, push_day: int, wipe_day: int, hour: int, minute: int, title: Optional[str] = None, description: Optional[str] = None, timezone: str = "UTC"):
        # Keep for backward compatibility or single recurring
        self.data["config"]["wipe_schedule"] = {
            "push_day": push_day,
            "wipe_day": wipe_day,
            "hour": hour,
            "minute": minute,
            "timezone": timezone or "UTC",
            "title": title or "🗓️ Wipe React",
            "description": description or "React below if you're playing this wipe!\n\n✅ Accept - I'm playing\n⏰ Late - I'll be late\n❌ Decline - Can't make it (please give reason)"
        }
        self._save()
        logger.info(f"Wipe schedule set: push_day={push_day} wipe_day={wipe_day} {hour:02d}:{minute:02d} tz={timezone}")

    def get_wipe_schedule(self) -> Optional[dict]:
        return self.data["config"].get("wipe_schedule")

    def get_current_wipe_info(self) -> Optional[dict]:
        return self.data["config"].get("current_wipe_message")

    def set_current_wipe_message(self, channel_id: int, message_id: int, wipe_date: str, target_ts: Optional[int] = None):
        data = {
            "channel_id": channel_id,
            "message_id": message_id,
            "wipe_date": wipe_date
        }
        if target_ts:
            data["target_ts"] = target_ts
        self.data["config"]["current_wipe_message"] = data
        self._save()

    def get_wipe_responses(self, wipe_date: str) -> dict:
        responses = self.data["config"].get("wipe_responses", {})
        return responses.get(wipe_date, {"accepts": [], "lates": [], "declines": {}})

    def add_wipe_response(self, wipe_date: str, response_type: str, user_id: int, reason: Optional[str] = None):
        if "wipe_responses" not in self.data["config"]:
            self.data["config"]["wipe_responses"] = {}
        responses = self.data["config"]["wipe_responses"]
        if wipe_date not in responses:
            responses[wipe_date] = {"accepts": [], "lates": [], "declines": {}}
        entry = responses[wipe_date]
        if response_type == "accept":
            if user_id not in entry["accepts"]:
                entry["accepts"].append(user_id)
        elif response_type == "late":
            if user_id not in entry["lates"]:
                entry["lates"].append(user_id)
        elif response_type == "decline":
            entry["declines"][str(user_id)] = reason or "No reason provided"
        self._save()

    def set_user_response(self, wipe_date: str, response_type: str, user_id: int, reason: Optional[str] = None):
        """Set a user's response, ensuring they only have ONE choice (accept, late or decline)."""
        if "wipe_responses" not in self.data["config"]:
            self.data["config"]["wipe_responses"] = {}
        responses = self.data["config"]["wipe_responses"]
        if wipe_date not in responses:
            responses[wipe_date] = {"accepts": [], "lates": [], "declines": {}}
        entry = responses[wipe_date]

        uid = int(user_id)
        uid_str = str(uid)

        # Remove user from all previous choices
        if uid in entry.get("accepts", []):
            entry["accepts"].remove(uid)
        if uid in entry.get("lates", []):
            entry["lates"].remove(uid)
        if uid_str in entry.get("declines", {}):
            del entry["declines"][uid_str]

        # Add to the new choice
        if response_type == "accept":
            if uid not in entry["accepts"]:
                entry["accepts"].append(uid)
        elif response_type == "late":
            if uid not in entry["lates"]:
                entry["lates"].append(uid)
        elif response_type == "decline":
            entry["declines"][uid_str] = reason or "No reason provided"

        self._save()

    # --- Specific scheduled wipes (for choosing exact push times) ---
    def get_scheduled_wipes(self) -> list:
        return self.data["config"].get("scheduled_wipes", [])

    def add_scheduled_wipe(self, push_dt_str: str, wipe_weekday: int, wipe_date: str, wipe_time: str, title: Optional[str] = None, description: Optional[str] = None):
        """push_dt_str is when to post the react (ISO).
        wipe_weekday: 0-6 (Mon-Sun), wipe_date: 'DD-MM-YY' or 'YYYY-MM-DD', wipe_time: 'HH:MM'
        """
        if "scheduled_wipes" not in self.data["config"]:
            self.data["config"]["scheduled_wipes"] = []
        wipe_id = f"sw_{int(datetime.now().timestamp())}"
        days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        default_title = f"Wipe React - {days[wipe_weekday]} {wipe_date} {wipe_time}"
        wipe = {
            "id": wipe_id,
            "push_datetime": push_dt_str,
            "wipe_weekday": wipe_weekday,
            "wipe_date": wipe_date,
            "wipe_time": wipe_time,
            "title": title or default_title,
            "description": description or "React below if you're playing this wipe!\n\n✅ Accept - I'm playing\n⏰ Late - I'll be late\n❌ Decline - Can't make it (reason required)",
            "posted": False
        }
        self.data["config"]["scheduled_wipes"].append(wipe)
        self._save()
        return wipe_id

    def mark_scheduled_wipe_posted(self, wipe_id: str):
        for w in self.data["config"].get("scheduled_wipes", []):
            if w["id"] == wipe_id:
                w["posted"] = True
                break
        self._save()

    def get_pending_scheduled_wipes(self) -> list:
        now = datetime.now()
        pending = []
        for w in self.data["config"].get("scheduled_wipes", []):
            if not w.get("posted"):
                try:
                    w_dt = datetime.fromisoformat(w["push_datetime"])
                    if w_dt <= now:
                        pending.append(w)
                except:
                    pass
        return pending

    def clear_old_scheduled_wipes(self):
        # Optional cleanup
        now = datetime.now()
        to_keep = []
        for w in self.data["config"].get("scheduled_wipes", []):
            try:
                w_dt = datetime.fromisoformat(w["push_datetime"])
                if w_dt > now - timedelta(days=7):  # keep recent
                    to_keep.append(w)
            except:
                to_keep.append(w)
        self.data["config"]["scheduled_wipes"] = to_keep
        self._save()


# parse_bm_server_id is a directly related helper (used by InviteStore BM methods
# internally, and by command handlers in bot.py for /setbm* and /live).
# Moved here so the store is self-contained for BM ID normalization.
def parse_bm_server_id(input_str: str) -> Optional[str]:
    """Extract server ID from BattleMetrics URL or return as-is if numeric."""
    s = input_str.strip()
    if s.isdigit():
        return s
    # Try to extract from URL
    import re
    match = re.search(r'/servers/(?:rust/)?(\d+)', s)
    if match:
        return match.group(1)
    return None


store = InviteStore(PENDING_FILE)
