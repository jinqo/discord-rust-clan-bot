"""
Discord Accept Bot (Main + Tryout servers)
------------------------------------------
/accept @user       → Role picker from MAIN server + 1-use 1-day invite to main
/main               → Set current guild as the MAIN clan server
/tryout             → Set current guild as the TRYOUT server
/check <steam>      → Full Steam check (bans, Rust hours, account age, friends, comments, risk flags)
/live <bm link>     → Start/update the live BattleMetrics stats panel (auto-refreshes every 60s in the set channel)
/setlivestatschannel #channel → Choose the channel where the live stats panel is posted + auto-updated
/setlogchannel #channel → Set channel for logs (attempted joins without /accept, successful /accepts, unauthorized command use, VAC bans detected in tickets)
/linksteam <steam>  → Link your Steam to Discord
/clanpanel → Posts the general clan invite panel to the set channel (pings owners, "Requirements: link steam", has "Request Team Invite" button that checks /linksteam and posts Rust "/clan invite <linked id>" text + ping)

Run with: python bot.py
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone, timedelta
import time
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

# Local Steam checker for Rust clan
from steam import SteamChecker, SteamAPIError
from battlemetrics import BattleMetricsChecker

# Guide command (moved to separate file to avoid spaghetti code)
from guide import guide as guide_cmd

# Store (InviteStore persistence logic extracted for maintainability)
from store import store, parse_bm_server_id

# UI Views + coupled helpers extracted to ui/views.py (first refactoring step).
# This removes ~400 lines of View/Modal + UI-only helpers from the main bot file.
# Imports make the names available everywhere in bot.py exactly as before (no call-site changes needed).
# bot instance is patched into the ui.views module after creation (see below) so that all
# original bare `bot` references inside the moved classes continue to work 100% unchanged.
from ui.views import (
    WipeReactView,
    DeclineReasonModal,
    RoleChoiceView,
    LiveServerView,
    RequestTeamInviteView,
    create_wipe_embed,
    build_bm_server_embed,
    create_main_invite,
    safe_send_dm,
)

# ============================================================
# CONFIG & PATHS
# ============================================================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_IDS = []
if os.getenv("GUILD_IDS"):
    GUILD_IDS = [int(g.strip()) for g in os.getenv("GUILD_IDS").split(",") if g.strip()]

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Bot owner ID - this user can use all admin commands (/main, /tryout, /accept, etc.)
# even without Administrator permission in the server.
BOT_OWNER_ID: int | None = None
owner_env = os.getenv("BOT_OWNER_ID")
if owner_env:
    try:
        BOT_OWNER_ID = int(owner_env.strip())
    except ValueError:
        print("WARNING: BOT_OWNER_ID in .env is not a valid number - owner bypass will be disabled")

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("accept-bot")
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)


# ============================================================
# CUSTOM CHECK: Bot owner OR Administrator permission
# Allows the person who owns the bot (BOT_OWNER_ID in .env) to run
# all privileged commands (/main, /tryout, /accept, /setdefaultrole, etc.)
# even if they don't have Administrator in the server.
# ============================================================
def owner_or_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        if BOT_OWNER_ID and interaction.user.id == BOT_OWNER_ID:
            return True
        if interaction.guild is None:
            return False
        return interaction.user.guild_permissions.administrator
    return app_commands.check(predicate)


# ============================================================
# PERSISTENT STORAGE
# Extracted in full to store.py (InviteStore class + store singleton + parse_bm_server_id).
# See store.py for the implementation. Import added at top; public API unchanged.
# ============================================================


# ============================================================
# BOT
# ============================================================
class AcceptBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True          # Needed for on_member_join + role assignment
        intents.message_content = False # We are slash-command only

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )

        self.steam: SteamChecker | None = None
        self.bm: BattleMetricsChecker | None = None

    async def setup_hook(self):
        # Initialize Steam checker (for /check command)
        steam_key = os.getenv("STEAM_API_KEY")
        if steam_key:
            self.steam = SteamChecker(steam_key)
            logger.info("Steam API checker initialized")
        else:
            logger.warning("STEAM_API_KEY not found in .env — /check command will be disabled")

        # Initialize BattleMetrics checker
        bm_token = os.getenv("BATTLEMETRICS_TOKEN")
        if bm_token:
            self.bm = BattleMetricsChecker(bm_token)
            logger.info("BattleMetrics API checker initialized")
        else:
            logger.warning("BATTLEMETRICS_TOKEN not found in .env — /check will not show live server info")

        # Load refactored command cogs (pilot step: admin config + wipe system).
        # This replaces the previous monolithic top-level @app_commands.command definitions
        # + manual bot.tree.add_command(...) registrations for these groups.
        # Cogs provide the same decorators, checks, and logic as methods.
        for cog_name in ["cogs.admin", "cogs.wipe"]:
            try:
                await self.load_extension(cog_name)
                logger.info(f"Loaded cog: {cog_name}")
            except Exception as e:
                logger.error(f"Failed to load {cog_name}: {e}")

        # Sync slash commands
        # - If you set GUILD_IDS in .env (dev), it only syncs to those guild(s) quickly.
        # - For production with Main + Tryout: leave GUILD_IDS empty so we do a global sync.
        #   Global sync can take up to 1 hour to appear in all servers the bot is member of.
        #   Tip: To force commands in both servers immediately, put BOTH guild IDs in GUILD_IDS.
        if GUILD_IDS:
            for gid in GUILD_IDS:
                guild = discord.Object(id=gid)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            logger.info(f"Commands synced to specific guilds: {GUILD_IDS} (fast)")
        else:
            await self.tree.sync()
            logger.info("Global slash commands synced. They may take up to 1 hour to show in all servers the bot is in.")

    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        logger.info(f"Connected to {len(self.guilds)} server(s)")
        activity = discord.Activity(type=discord.ActivityType.watching, name="/accept & /check")
        await self.change_presence(activity=activity)

        # Start wipe scheduler if not running
        if not self.wipe_scheduler.is_running():
            self.wipe_scheduler.start()
            logger.info("Wipe scheduler task started")

        # Start live stats auto-updater
        if not self.live_stats_updater.is_running():
            self.live_stats_updater.start()
            logger.info("Live stats updater task started")

        # Re-attach current wipe react view so buttons keep working after restart
        info = store.get_current_wipe_info()
        if info:
            try:
                view = WipeReactView(info["wipe_date"])
                self.add_view(view)
                logger.info(f"Re-attached wipe react view for {info['wipe_date']}")
            except Exception:
                pass

        # Re-attach live stats view
        active = store.get_active_live()
        if active:
            try:
                view = LiveServerView(active["server_id"])
                self.add_view(view)
                logger.info(f"Re-attached live stats view for server {active['server_id']}")
            except Exception:
                pass

    async def close(self):
        if self.steam:
            await self.steam.close()
        if self.bm:
            await self.bm.close()
        if hasattr(self, 'wipe_scheduler') and self.wipe_scheduler.is_running():
            self.wipe_scheduler.cancel()
        if hasattr(self, 'live_stats_updater') and self.live_stats_updater.is_running():
            self.live_stats_updater.cancel()
        await super().close()

    @tasks.loop(minutes=5)
    async def wipe_scheduler(self):
        """Automatically posts a wipe react message when the scheduled time arrives (recurring or specific)."""
        now_utc = datetime.now(timezone.utc)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        channel_id = store.get_wipe_channel_id()
        if not channel_id:
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"wipe_scheduler: could not fetch wipe channel {channel_id}: {e}")
                return

        # 1. Handle multiple recurring schedules (from /schedulewipe calls)
        # Time is interpreted in the schedule's timezone (e.g. Europe/Amsterdam for 11pm Amsterdam time)
        schedules = store.get_wipe_schedules()
        for sched in schedules:
            push_day = sched.get("push_day", sched.get("weekday", 0))
            tz_str = sched.get("timezone", "UTC")
            try:
                tz = ZoneInfo(tz_str)
            except Exception:
                tz = timezone.utc
            local_now = now_utc.astimezone(tz)
            if local_now.weekday() == push_day:
                scheduled_hour = sched.get("hour", 18)
                scheduled_minute = sched.get("minute", 0)
                if local_now.hour == scheduled_hour and abs(local_now.minute - scheduled_minute) <= 5:
                    wipe_date = local_now.strftime("%Y-%m-%d")
                    # Use schedule id + date as key to support multiple independent wipes
                    wipe_key = f"{sched['id']}_{wipe_date}"
                    current = store.get_current_wipe_info()
                    if current and current.get("wipe_date") == wipe_key:
                        continue  # already posted this week for this schedule
                    # Calculate target wipe time using this schedule's wipe_day (in the local tz)
                    wipe_day = sched.get("wipe_day", push_day)
                    base = local_now
                    days_ahead = wipe_day - base.weekday()
                    if days_ahead < 0 or (days_ahead == 0 and (base.hour > scheduled_hour or (base.hour == scheduled_hour and base.minute > scheduled_minute))):
                        days_ahead += 7
                    wipe_dt_local = base + timedelta(days=days_ahead)
                    wipe_dt_local = wipe_dt_local.replace(hour=scheduled_hour, minute=scheduled_minute, second=0, microsecond=0)
                    # Convert to UTC for Discord timestamp (target_dt)
                    wipe_dt = wipe_dt_local.astimezone(timezone.utc)
                    display = f"{days[wipe_day]} {wipe_dt_local.strftime('%d-%m-%y %H:%M')} ({tz_str})"
                    embed = create_wipe_embed(wipe_key, display_label=display, target_dt=wipe_dt)
                    # Use schedule title/desc if set
                    if sched.get("title"):
                        embed.title = sched["title"]
                    if sched.get("description"):
                        embed.description = sched["description"]
                    view = WipeReactView(wipe_key)
                    try:
                        msg = await channel.send(embed=embed, view=view)
                        store.set_current_wipe_message(channel.id, msg.id, wipe_key)
                        # update last_posted
                        sched["last_posted"] = wipe_date
                        store._save()
                        logger.info(f"Auto-posted wipe react for schedule {sched['id']} on {wipe_date} ({tz_str})")
                    except Exception as e:
                        logger.exception(f"Failed to auto-post for schedule {sched['id']}: {e}")

        # 2. Handle specific scheduled wipes (the new "choose when" feature)
        pending = store.get_pending_scheduled_wipes()
        for wipe in pending:
            try:
                wipe_dt = datetime.fromisoformat(wipe["push_datetime"])
                delta = (now_utc - wipe_dt).total_seconds()
                # Post if within ~5 min before or 10 min after the scheduled time (to catch the 5-min loop)
                if -300 < delta < 600 and not wipe.get("posted"):
                    wipe_id = wipe["id"]
                    title = wipe.get("title")
                    desc = wipe.get("description")

                    # Build wipe datetime from the wipe fields
                    try:
                        wipe_date_str = wipe["wipe_date"]
                        wipe_time_str = wipe["wipe_time"]
                        # Support both DD-MM-YY and YYYY-MM-DD for wipe_date
                        if len(wipe_date_str.split("-")[0]) == 2:
                            # DD-MM-YY or DD-MM-YYYY -> convert to YYYY-MM-DD for parsing
                            parts = wipe_date_str.split("-")
                            if len(parts[2]) == 2:
                                parts[2] = "20" + parts[2]
                            wipe_date_str = f"{parts[2]}-{parts[1]}-{parts[0]}"
                        full_wipe_dt_str = f"{wipe_date_str}T{wipe_time_str}:00"
                        actual_wipe_dt = datetime.fromisoformat(full_wipe_dt_str)
                    except Exception as e:
                        logger.warning(f"wipe_scheduler: bad scheduled wipe date/time parse for id={wipe.get('id')}: {e}")
                        actual_wipe_dt = wipe_dt  # fallback

                    # Use the wipe datetime for the timestamp in embed
                    display = f"{days[wipe.get('wipe_weekday', 4)]} {wipe['wipe_date']} {wipe['wipe_time']}"
                    embed = create_wipe_embed(wipe_id, display_label=display, target_dt=actual_wipe_dt)
                    if title:
                        embed.title = title
                    if desc:
                        embed.description = desc
                    view = WipeReactView(wipe_id)

                    try:
                        msg = await channel.send(embed=embed, view=view)
                        store.mark_scheduled_wipe_posted(wipe_id)
                        # Also set as current so buttons work nicely
                        store.set_current_wipe_message(channel.id, msg.id, wipe_id)
                        logger.info(f"Auto-posted scheduled wipe {wipe_id} at {wipe['push_datetime']}")
                    except Exception as e:
                        logger.exception(f"Failed to auto-post scheduled wipe {wipe_id}: {e}")
            except Exception as e:
                logger.warning(f"Bad scheduled wipe entry: {e}")

        # Occasional cleanup
        if now_utc.minute % 30 == 0:
            store.clear_old_scheduled_wipes()

    @tasks.loop(seconds=60)
    async def live_stats_updater(self):
        """Auto-updates the live server stats panel if one is active."""
        active = store.get_active_live()
        if not active or not self.bm:
            return

        channel_id = active.get("channel_id")
        message_id = active.get("message_id")
        server_id = active.get("server_id")

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as e:
                logger.warning(f"live_stats_updater: could not fetch live stats channel {channel_id}: {e}")
                store.clear_active_live()
                return

        try:
            server_data = await self.bm.get_server(server_id)
            if not server_data:
                return

            embed = build_bm_server_embed(server_data)
            view = LiveServerView(server_id)

            msg = await channel.fetch_message(message_id)
            await msg.edit(embed=embed, view=view)
        except Exception as e:
            logger.warning(f"Live stats update failed: {e}")
            # If message is gone, clear it
            if "NotFound" in str(e) or "Unknown Message" in str(e):
                store.clear_active_live()

    async def on_member_join(self, member: discord.Member):
        """Handle joins to the MAIN server.
        - If they came through /accept (pending), give role + welcome.
        - If invite-only mode is enabled and they are not whitelisted / pending, kick them.
        - Existing members (snapshot at enable time) and previously accepted users are never kicked.
        """
        guild = member.guild
        main_id = store.get_main_guild_id()

        # Only care about the main server
        if main_id is None or guild.id != main_id:
            return

        # Check if they have a pending acceptance
        role_id = store.get_pending_role(guild.id, member.id)
        pending_info = store.get_pending_info(guild.id, member.id)

        if role_id is not None:
            # They were accepted via the tryout system → let them in and give role
            role = guild.get_role(role_id)
            if role is None:
                logger.warning(f"Role {role_id} no longer exists in main guild")
                store.remove_pending(guild.id, member.id)
                return

            try:
                await member.add_roles(role, reason="Accepted via /accept command (main server)")
                store.remove_pending(guild.id, member.id)
                store.add_to_whitelist(member.id)  # protect them for future re-joins

                # Log the join with source invite info
                try:
                    creator = pending_info.get("created_by") if pending_info else None
                    code = pending_info.get("invite_code") if pending_info else None
                    log_embed = discord.Embed(
                        title="✅ User Joined Using /accept Invite",
                        color=0x2ECC71,
                        timestamp=datetime.now(timezone.utc)
                    )
                    log_embed.add_field(name="User", value=f"{member} (`{member.id}`)", inline=True)
                    if creator:
                        log_embed.add_field(name="Invite Created By", value=f"<@{creator}> (`{creator}`)", inline=True)
                    if code:
                        log_embed.add_field(name="Invite Code", value=code, inline=True)
                    log_embed.add_field(name="Role Assigned", value=role.name if role else "N/A", inline=True)
                    await bot.send_log(log_embed)

                    # Also append to file for invite join tracking
                    try:
                        with open("invite_joins.log", "a", encoding="utf-8") as f:
                            f.write(f"[{datetime.now(timezone.utc)}] {member} ({member.id}) joined using invite {code or 'unknown'} created by {creator or 'unknown'}\n")
                    except Exception as e:
                        logger.warning(f"on_member_join: failed to append invite_joins.log for {member.id}: {e}")
                except Exception as e:
                    logger.warning(f"on_member_join: failed to send join log embed for {member.id}: {e}")

                # Welcome DM
                try:
                    await member.send(
                        embed=discord.Embed(
                            title="Welcome! 🎉",
                            description=f"You have been accepted into **{guild.name}** and received the **{role.name}** role.",
                            color=discord.Color.green(),
                        )
                    )
                except discord.Forbidden:
                    pass

                logger.info(f"Assigned role {role.name} to {member} in main guild (via acceptance)")

            except discord.Forbidden:
                logger.error(f"Could not assign role {role.name} to {member}")
            except Exception as e:
                logger.exception(f"Error during role assignment: {e}")

            return

        # No pending acceptance
        if not store.is_require_acceptance():
            # Mode is off → open server, do nothing
            return

        # Invite-only mode is ON
        if member.bot:
            return  # never kick the bot or other bots

        whitelisted = store.get_whitelisted_members()
        if member.id in whitelisted:
            # Grandfathered member (was here when mode was enabled) or previously accepted
            return

        # Not whitelisted and no pending acceptance → kick them
        try:
            await member.kick(reason="This server is invite-only. You must be accepted from the tryout server first.")
            logger.info(f"Kicked {member} (ID {member.id}) from main server - not accepted via tryout")

            # Log to staff log channel
            embed = discord.Embed(
                title="🚫 Attempted Join Without /accept",
                description=f"**{member}** (`{member.id}`) tried to join **{guild.name}** without being accepted via `/accept`.",
                color=0xE74C3C,
                timestamp=datetime.now(timezone.utc)
            )
            embed.add_field(name="User", value=f"{member.mention} ({member})", inline=True)
            embed.add_field(name="Guild", value=guild.name, inline=True)
            await self.send_log(embed)

            # DM the user with clear instructions to go to the tryout server.
            # Enhanced: always resolve tryout name reliably (get + fetch fallback), use safe_send_dm for reliable delivery,
            # and keep the exact ticket format prominent. This explains the full tryout flow to applicants.
            try:
                tryout_id = store.get_tryout_guild_id()
                tryout_name = "the tryout server"
                if tryout_id:
                    tryout_guild = self.get_guild(tryout_id)
                    if tryout_guild is None:
                        try:
                            tryout_guild = await self.fetch_guild(tryout_id)
                        except Exception:
                            tryout_guild = None
                    if tryout_guild:
                        tryout_name = tryout_guild.name
                    else:
                        tryout_name = f"the tryout server (ID {tryout_id})"

                dm_embed = discord.Embed(
                    title="🚫 You were kicked from the main server",
                    description=(
                        f"**{guild.name}** is currently in invite-only / acceptance-required mode.\n\n"
                        f"**Please join {tryout_name} instead.**\n\n"
                        "**How to get accepted:**\n"
                        "1. Join the tryout server.\n"
                        "2. Open a ticket (the ticket bot will create one).\n"
                        "3. Post exactly this format in the ticket:\n"
                        "```\n"
                        "steam name -\n"
                        "steam link -\n"
                        "rust hours -\n"
                        "role in clan -\n"
                        "```\n"
                        "4. Staff will run `/check` on your Steam profile and review you.\n"
                        "5. If approved, a staff member will use `/accept @you` from the tryout server.\n"
                        "   You will receive a 1-use invite to the main server + your role.\n\n"
                        "Do **not** attempt to join the main server again until you have been properly accepted via `/accept`.\n"
                        "Repeated attempts may result in longer restrictions."
                    ),
                    color=discord.Color.red(),
                )
                await safe_send_dm(member, dm_embed)
            except Exception as e:
                # DM failure is non-fatal (user may have DMs disabled); we already kicked. Log context for diagnostics.
                logger.warning(f"on_member_join: DM after kick may have failed for {member.id} (non-fatal): {e}")

        except discord.Forbidden:
            logger.error(f"Failed to kick {member} (missing permissions)")
        except Exception as e:
            logger.exception(f"Error while trying to kick unauthorized joiner {member}: {e}")

    async def on_message(self, message: discord.Message):
        """Auto-detect Steam links posted in text channels (or threads) inside the tryout ticket category.
        When a new channel is created by the external ticket bot in the category (ID 1514646009781813270 or configured),
        any message containing a Steam link/ID will trigger a full check (same embed as /check, with all friends checked).
        Keeps track per channel to avoid duplicate auto-checks.
        """
        if message.author.bot or not message.guild:
            return

        ch = message.channel

        # Detect channels/threads inside the configured tryout ticket category
        # (user's external ticket bot creates text channels inside this category)
        cat_id = store.get_tryout_ticket_category_id() or 1514646009781813270  # fallback to provided category
        channel_cat_id = None
        if isinstance(ch, discord.TextChannel):
            channel_cat_id = ch.category_id
        elif isinstance(ch, discord.Thread):
            parent = ch.parent
            if isinstance(parent, discord.TextChannel):
                channel_cat_id = parent.category_id
        if channel_cat_id != cat_id:
            return

        # Support both text channels and threads created in the category
        thread_or_chan = ch

        # Quick filter for likely Steam links (performance)
        content = message.content or ""
        if "steamcommunity" not in content.lower() and "7656" not in content:
            return

        steamid = await self.steam.resolve_steam_id(content)
        if not steamid:
            return

        # --- Duplicate / update check ---
        # If this ticket already has a stored steamid:
        # - If same as current message's link → ignore (duplicate)
        # - If different → proceed (applicant posted a correction/new link); we will overwrite the stored one
        existing = store.get_tryout_ticket(getattr(thread_or_chan, "id", 0) or 0)
        if existing and existing.get("steamid64"):
            if existing.get("steamid64") == steamid:
                # Already processed this exact link for this ticket. Ignore to avoid spam.
                # User can still use /ticketrefresh to force re-check (e.g. after making profile public).
                return
            # Different link posted in the same ticket channel → treat as update and proceed
            # (will re-check and update the stored steamid64 below)

        # proceed with this steamid

        # Run the check (re-uses all the existing logic, hours fixes, etc.)
        try:
            data = await self.steam.check(steamid)
        except Exception as e:
            logger.warning(f"Auto ticket Steam check failed: {e}")
            return

        if data.get("error") or not data.get("resolved"):
            return

        # Log if VAC ban detected in a ticket (sus user)
        bans = data.get("bans") or {}
        if bans.get("VACBanned") or bans.get("NumberOfVACBans", 0) > 0:
            try:
                persona = data.get("persona", "Unknown")
                steamid64 = data.get("steamid64", "unknown")
                log_embed = discord.Embed(
                    title="🚨 VAC Banned User Detected in Tryout Ticket",
                    description="Suspicious user attempting to tryout.",
                    color=0xE74C3C,
                    timestamp=datetime.now(timezone.utc)
                )
                log_embed.add_field(name="User", value=f"{persona} (`{steamid64}`)", inline=True)
                log_embed.add_field(name="VAC Bans", value=str(bans.get("NumberOfVACBans", 0)), inline=True)
                if "channel" in dir() or "thread_or_chan" in dir():
                    ch_ref = thread_or_chan if 'thread_or_chan' in dir() else message.channel
                    log_embed.add_field(name="Channel", value=getattr(ch_ref, "mention", str(ch_ref.id)), inline=True)
                await self.send_log(log_embed)
            except Exception as e:
                logger.warning(f"on_message: failed to log VAC ban in ticket: {e}")

        if data.get("error") or not data.get("resolved"):
            return

        embed = create_steam_check_embed(data, content or str(steamid))

        # For hidden profiles in ticket context, add a short follow-up instruction after the /check-style embed
        verified = bool(data.get("rust", {}).get("has_data")) and (data.get("profile", {}).get("communityvisibilitystate", 0) == 3)

        # Post the verification message (exact same as /check)
        ticket_id = getattr(thread_or_chan, "id", 0)
        ticket_data = store.get_tryout_ticket(ticket_id) or {}
        old_msg_id = ticket_data.get("verification_msg_id")

        sent_msg = None
        try:
            if old_msg_id:
                try:
                    old = await thread_or_chan.fetch_message(old_msg_id)
                    await old.edit(embed=embed)
                    sent_msg = old
                except discord.NotFound:
                    sent_msg = await thread_or_chan.send(embed=embed)
            else:
                sent_msg = await thread_or_chan.send(embed=embed)
        except Exception as e:
            logger.warning(f"Could not post/edit ticket verification: {e}")
            return

        if sent_msg and ticket_id:
            store.set_tryout_ticket(
                ticket_id,
                owner_id=message.author.id,
                steamid64=steamid,
                verified_public=verified,
                verification_msg_id=sent_msg.id
            )

        if not verified:
            try:
                await thread_or_chan.send(
                    "⚠️ **Action Required**: Profile (or Game details) is hidden. Make it public and use `/ticketrefresh` (or post the link again). You will not be accepted until verified."
                )
            except Exception as e:
                logger.warning(f"on_message: failed to send hidden-profile warning in ticket {getattr(thread_or_chan, 'id', 'unknown')}: {e}")

        # Optional reaction
        try:
            await message.add_reaction("✅" if verified else "⚠️")
        except Exception as e:
            logger.warning(f"on_message: failed to add reaction in ticket channel: {e}")

    async def send_log(self, embed: discord.Embed):
        """Send an embed to the configured log channel (if set)."""
        log_id = store.get_log_channel_id()
        if not log_id:
            return
        channel = self.get_channel(log_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(log_id)
            except Exception:
                return
        if channel:
            try:
                await channel.send(embed=embed)
            except Exception as e:
                logger.warning(f"Failed to send log embed: {e}")


bot = AcceptBot()

# Patch the bot instance into the ui.views module so that all classes/helpers
# defined there that use the bare name `bot` (for .get_channel, .bm, send_log, etc.)
# continue to behave *exactly* as they did when the code lived inside bot.py.
# This is the minimal- delta way to extract while preserving persistent view re-attach,
# on_ready logic, all button handlers, modals, and every call site unchanged.
import ui.views as _ui_views
_ui_views.bot = bot


# ============================================================
# HELPER FUNCTIONS
# ============================================================
# create_main_invite, safe_send_dm, create_wipe_embed, build_bm_server_embed
# + all View/Modal classes (WipeReactView, DeclineReasonModal, RoleChoiceView,
# LiveServerView, RequestTeamInviteView) have been moved to ui/views.py
# and are imported at the top of this file.
# Only a thin re-export + bot patch is needed here for 100% behavioral compatibility.
# ============================================================


# ============================================================
# WIPE REACT UI + EMBED (moved to ui/views.py - see imports + bot patch)
# create_wipe_embed, WipeReactView, DeclineReasonModal, safe_send_dm, RoleChoiceView
# were here; now live in ui/views.py. All references via top-level import.
# ============================================================

def create_steam_check_embed(data: dict, steam_input: str) -> discord.Embed:
    """Builds the exact same embed as the /check command.
    Used by manual /check, auto ticket detection, and /ticketrefresh.
    """
    profile = data.get("profile") or {}
    steamid64 = data["steamid64"]

    bans = data.get("bans") or {}
    rust_hours = data.get("rust_hours", 0.0)
    rust_2w = data.get("rust_hours_2w", 0.0)
    flags = data.get("flags", [])
    risk_level = data.get("risk_level", "UNKNOWN")
    risk_color = data.get("risk_color", 0x808080)
    persona = data.get("persona", "Unknown")
    profile_url = data.get("profile_url", f"https://steamcommunity.com/profiles/{steamid64}")
    account_age = data.get("account_age_days")
    account_created = data.get("account_created")

    embed = discord.Embed(
        title=f"🔍 Steam Check — {persona}",
        url=profile_url,
        color=risk_color,
        timestamp=datetime.now(timezone.utc),
    )

    # Profile info
    embed.add_field(
        name="Profile",
        value=(
            f"**Name:** {persona}\n"
            f"**SteamID64:** `{steamid64}`\n"
            f"**Account age:** {account_age} days old ({account_created or 'unknown'})\n"
            f"[Open Profile]({profile_url})"
        ),
        inline=False,
    )

    # Bans section
    vac_banned = bans.get("VACBanned", False)
    vac_count = bans.get("NumberOfVACBans", 0)
    game_bans = bans.get("NumberOfGameBans", 0)
    days_last = bans.get("DaysSinceLastBan", 0)
    comm_banned = bans.get("CommunityBanned", False)

    ban_lines = []
    if vac_banned or vac_count > 0:
        ban_lines.append(f"⛔ **VAC bans:** {vac_count}" + (f" (last {days_last} days ago)" if days_last else ""))
    else:
        ban_lines.append("✅ No VAC bans")

    if game_bans > 0:
        ban_lines.append(f"⛔ **Game bans:** {game_bans}")
    else:
        ban_lines.append("✅ No game bans")

    if comm_banned:
        ban_lines.append("⛔ Community banned")

    embed.add_field(
        name="Bans",
        value="\n".join(ban_lines),
        inline=True,
    )

    # Rust hours (improved messaging for hidden game details / private profiles)
    rust_data = data.get("rust") or {}
    if rust_data.get("has_data"):
        rust_text = f"**Rust hours:** {rust_hours}h\n**Last 2 weeks:** {rust_2w}h"
    else:
        vis = (profile or {}).get("communityvisibilitystate", 0)
        if vis != 3:
            rust_text = "🔒 *Rust hours not visible (profile is private)*"
        else:
            rust_text = "🔒 *Rust hours not visible (game details hidden in profile privacy)*"

    embed.add_field(
        name="Rust (252490)",
        value=rust_text,
        inline=True,
    )

    # Friends Network
    fb = data.get("friends_bans") or {}
    if fb.get("success"):
        friends_text = (
            f"**Total friends:** {fb.get('total_friends', 0)}\n"
            f"**Checked:** {fb.get('checked', 0)}\n"
            f"**With VAC:** {fb.get('vac_banned', 0)}\n"
            f"**With game ban:** {fb.get('game_banned', 0)}"
        )
    else:
        fcount = (data.get("friends") or {}).get("count", 0)
        friends_text = f"🔒 Friends list private ({fcount} friends hidden)" if fcount else "🔒 Friends list private or not visible"
    embed.add_field(name="Friends Network", value=friends_text, inline=True)

    # Profile Comments
    com = data.get("comments") or {}
    if com.get("success"):
        checked = com.get('comment_count', 0)
        total = com.get('total_on_profile')
        if total and total > checked:
            count_str = f"{checked} (of {total} on profile)"
        else:
            count_str = str(checked)
        com_text = f"**Comments checked:** {count_str}\n**Suspicious:** {com.get('suspicious', 0)}"
        ex = com.get("examples") or []
        if ex:
            short = " | ".join(ex[:2])
            com_text += f"\nExamples: {short[:150]}"
    else:
        com_text = "🔒 Comments not visible (private or loading issue)"
    embed.add_field(name="Profile Comments", value=com_text, inline=True)

    # Risk / Flags
    if flags:
        flag_text = "\n".join(f"• {f}" for f in flags)
    else:
        flag_text = "✅ No suspicious flags detected"

    embed.add_field(
        name=f"🚩 Risk Level: {risk_level}",
        value=flag_text,
        inline=False,
    )

    # Footer with short "last checked" hint (embed timestamp also shows the time)
    checked_ts = int(datetime.now(timezone.utc).timestamp())
    embed.set_footer(text=f"Last checked: <t:{checked_ts}:R> • Input: {steam_input[:50]}")

    if data.get("avatar"):
        embed.set_thumbnail(url=data["avatar"])

    return embed


# ============================================================
# SLASH COMMANDS (all in English)
# ============================================================

# --- /main, /tryout, invite-only, set* config, setdefaultrole etc. moved to cogs/admin.py ---
# (Pilot refactoring step. Behavior 100% identical via Cog methods + same decorators + checks.)
# Remaining slash commands below are either public or not-yet-moved.

# --- Live Server Stats ---
# LiveServerView and build_bm_server_embed moved to ui/views.py (imported at top).
# The live_cmd below (and live_stats_updater in class) continue to use the imported names.
@app_commands.guild_only()
@app_commands.command(name="live", description="Start or update the live server stats panel (posted in the configured live channel).")
@app_commands.describe(server="BattleMetrics server URL or ID (e.g. https://www.battlemetrics.com/servers/rust/21763974)")
async def live_cmd(interaction: discord.Interaction, server: str):
    if not bot.bm:
        await interaction.response.send_message("BattleMetrics is not configured (missing token).", ephemeral=True)
        return

    server_id = parse_bm_server_id(server)
    if not server_id:
        await interaction.response.send_message("Could not parse a valid BattleMetrics server ID from the input.", ephemeral=True)
        return

    channel_id = store.get_live_stats_channel_id()
    if not channel_id:
        await interaction.response.send_message("No live stats channel set. Use /setlivestatschannel first.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    if not channel:
        await interaction.response.send_message("Configured live stats channel not found.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    try:
        server_data = await bot.bm.get_server(server_id)
        if not server_data:
            await interaction.followup.send("Server not found on BattleMetrics.", ephemeral=True)
            return

        embed = build_bm_server_embed(server_data)
        view = LiveServerView(server_id)

        # Post or update the live panel in the selected channel
        active = store.get_active_live()
        if active and active.get("channel_id") == channel_id:
            try:
                msg = await channel.fetch_message(active["message_id"])
                await msg.edit(embed=embed, view=view)
                store.set_active_live(server_id, channel_id, active["message_id"])
                await interaction.followup.send(f"✅ Live stats panel updated in {channel.mention}.", ephemeral=True)
                return
            except:
                pass  # message gone, post new

        msg = await channel.send(embed=embed, view=view)
        store.set_active_live(server_id, channel_id, msg.id)
        await interaction.followup.send(f"✅ Live stats panel posted in {channel.mention} (auto-updating).", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"Error: {e}", ephemeral=True)


# --- Steam Linking ---
@app_commands.guild_only()
@app_commands.command(name="linksteam", description="Link your Steam account to your Discord for easier checks and clan features.")
@app_commands.describe(steam="Your Steam profile URL, ID, or vanity name")
async def linksteam_cmd(interaction: discord.Interaction, steam: str):
    if not bot.steam:
        await interaction.response.send_message("Steam API is not configured.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    steam64 = await bot.steam.resolve_steam_id(steam)
    if not steam64:
        await interaction.followup.send("Could not resolve that Steam identifier.", ephemeral=True)
        return

    store.link_steam(interaction.user.id, steam64)
    await interaction.followup.send(f"✅ Linked your Steam64: `{steam64}`. Future checks can use your linked account.", ephemeral=True)


# /clan group so the command shows as /clan invite
@app_commands.guild_only()
@owner_or_admin_check()
@app_commands.command(name="clanpanel", description="Post the clan invite panel to the set channel (pings owners). No parameters.")
async def clanpanel_cmd(interaction: discord.Interaction):
    channel_id = store.get_clan_invite_channel_id()
    owner_role_id = store.get_clan_owner_role_id()

    if not channel_id:
        await interaction.response.send_message("Clan invite channel not set. Use /setclaninvitechannel first.", ephemeral=True)
        return

    channel = interaction.guild.get_channel(channel_id)
    if not channel:
        await interaction.response.send_message("Configured clan invite channel not found.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # General clan panel - no specific ID, as per user
    embed = discord.Embed(
        title="Clan Invite",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Requirements", value="link steam", inline=False)

    embed.add_field(name="Initiated by", value=interaction.user.mention, inline=False)

    # Ping the owners (role mention only; never @here)
    if owner_role_id:
        owner_role = interaction.guild.get_role(owner_role_id)
        ping_content = owner_role.mention if owner_role else ""
    else:
        ping_content = ""

    # Button with "Request Team Invite"
    # Supports multiple requests (different users) + 30s per-user cooldown on the action.
    # (RequestTeamInviteView class definition moved to ui/views.py and imported at top level.
    # Using the top-level imported class keeps behavior identical; the class is no longer nested.)
    view = RequestTeamInviteView(interaction.user, ping_content)

    # This is the message/panel that will be sent to the channel
    # Only include content (for role ping) if there is an owner role configured
    send_kwargs = {"embed": embed, "view": view}
    if ping_content:
        send_kwargs["content"] = ping_content
    await channel.send(**send_kwargs)

    await interaction.followup.send(f"Clan panel sent to {channel.mention}.", ephemeral=True)


# NOTE: /guide lives in guide.py (no spaghetti). All English. Uses 6 small embeds to prevent 6000-char errors.
# Registered via: from guide import guide as guide_cmd  +  bot.tree.add_command(guide_cmd)  later.

# setclaninvitechannel / setclanownerrole / setlogchannel / settryoutticketcategory
# (and several other /set* + main/tryout/inviteonly) moved to cogs/admin.py


@app_commands.command(name="ticketrefresh", description="Re-verify Steam link in this ticket channel.")
async def ticketrefresh_cmd(interaction: discord.Interaction):
    """Force a full Steam check for the link found in the current ticket channel.
    Works in ticket channels (name prefix 'ticket') inside the Tryout server.
    Scans recent messages for the Steam link if not already recorded.
    """
    if interaction.guild is None:
        await interaction.response.send_message("Must be used inside the Tryout server.", ephemeral=True)
        return

    tryout_id = store.get_tryout_guild_id()
    if tryout_id is None or interaction.guild.id != tryout_id:
        await interaction.response.send_message("This command can only be used in the configured Tryout server.", ephemeral=True)
        return

    ch = interaction.channel
    # Must be a "ticket" channel (your ticket bot's channels)
    chan_name = getattr(ch, "name", "") or ""
    is_ticket = chan_name.lower().startswith("ticket")
    if not is_ticket:
        parent = getattr(ch, "parent", None)
        if parent:
            is_ticket = getattr(parent, "name", "").lower().startswith("ticket")
    if not is_ticket:
        await interaction.response.send_message("This command only works inside ticket channels (those whose name starts with 'ticket').", ephemeral=True)
        return

    # Get existing record (may be empty)
    ticket = store.get_tryout_ticket(ch.id) or {}

    # Find the Steam link: always scan recent messages to get the *current* one the applicant posted
    # (prefer a message that matches the expected ticket format "steam name -", "steam link -", etc.)
    # This fixes cases where an older/wrong Steam ID was stored or appears in other messages in the channel.
    # Fallback to stored only if no link found in recent history.
    steamid = None
    async for msg in ch.history(limit=100):
        if msg.author.bot:
            continue
        sid = await bot.steam.resolve_steam_id(msg.content or "")
        if sid:
            cl = (msg.content or "").lower()
            if any(x in cl for x in ["steam name -", "steam link -", "rust hours -", "role in clan -"]):
                steamid = sid
                break  # newest matching the format
            if steamid is None:
                steamid = sid  # remember first (newest) any as fallback
    if not steamid:
        steamid = ticket.get("steamid64")

    if not steamid:
        await interaction.response.send_message("No Steam link found in the recent messages of this channel. Ask the applicant to post the format with their Steam profile link.", ephemeral=True)
        return

    # Owner / staff check (if we have a prior owner recorded)
    if ticket.get("owner_id"):
        is_owner = ticket.get("owner_id") == interaction.user.id
        if not is_owner and not (interaction.user.guild_permissions.administrator or interaction.user.id == BOT_OWNER_ID):
            await interaction.response.send_message("Only the original ticket owner or staff can use /ticketrefresh.", ephemeral=True)
            return

    await interaction.response.defer(thinking=True)

    try:
        data = await bot.steam.check(steamid)
    except Exception as e:
        await interaction.followup.send(f"Check failed: {e}", ephemeral=True)
        return

    if data.get("error") or not data.get("resolved"):
        await interaction.followup.send("Could not resolve or check the Steam profile.", ephemeral=True)
        return

    # Log VAC ban if present (for /ticketrefresh in ticket)
    bans = data.get("bans") or {}
    if bans.get("VACBanned") or bans.get("NumberOfVACBans", 0) > 0:
        try:
            persona = data.get("persona", "Unknown")
            steamid64 = data.get("steamid64", "unknown")
            log_embed = discord.Embed(
                title="🚨 VAC Banned User Detected in Tryout Ticket (via /ticketrefresh)",
                description="Suspicious user attempting to tryout.",
                color=0xE74C3C,
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="User", value=f"{persona} (`{steamid64}`)", inline=True)
            log_embed.add_field(name="VAC Bans", value=str(bans.get("NumberOfVACBans", 0)), inline=True)
            log_embed.add_field(name="Channel", value=ch.mention if hasattr(ch, "mention") else str(getattr(ch, "id", "unknown")), inline=True)
            await bot.send_log(log_embed)
        except Exception:
            pass

    # Use the exact same embed as /check
    embed = create_steam_check_embed(data, str(steamid))

    # For hidden profiles, add the instruction after the /check-style embed
    hidden = not (data.get("rust", {}).get("has_data") and data.get("profile", {}).get("communityvisibilitystate", 0) == 3)

    # Send the verification (exact same as manual /check)
    old_msg_id = ticket.get("verification_msg_id")
    sent_msg = None
    try:
        if old_msg_id:
            try:
                old = await ch.fetch_message(old_msg_id)
                await old.edit(embed=embed)
                sent_msg = old
            except discord.NotFound:
                sent_msg = await ch.send(embed=embed)
        else:
            sent_msg = await ch.send(embed=embed)
    except Exception as e:
        await interaction.followup.send(f"Could not post refresh: {e}", ephemeral=True)
        return

    if sent_msg:
        owner = ticket.get("owner_id") or interaction.user.id
        store.set_tryout_ticket(
            ch.id,
            owner_id=owner,
            steamid64=steamid,
            verified_public=not hidden,
            verification_msg_id=sent_msg.id
        )

    if hidden:
        try:
            await ch.send(
                "⚠️ **Action Required**: Profile (or Game details) is hidden. Make it public and use `/ticketrefresh` (or post the link again). You will not be accepted until verified."
            )
        except:
            pass

    await interaction.followup.send("✅ Full Steam check (identical to /check) completed and posted in the channel.", ephemeral=True)


# All wipe commands (/setwipechannel, /setreasonchannel, /setwipeschedule, /clearwipeschedule,
# /schedulewipe, /listwipes, /removewipe, /testwipereact, /wipereact) moved to cogs/wipe.py
# (Pilot cog #2). Uses the already-extracted ui/views.py helpers (WipeReactView, create_wipe_embed).
# TIMEZONE/HOUR/MINUTE_CHOICES also moved into that cog module.

# --- /accept (now always targets the MAIN server + role choice UI) ---

@app_commands.guild_only()
@owner_or_admin_check()
@app_commands.command(
    name="accept",
    description="Send a 1-day 1-use invite to the Main server and pick a role from Main."
)
@app_commands.describe(user="The user you want to accept (they will receive a DM with the invite)")
async def accept_cmd(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)

    # /accept is only allowed in the Tryout server
    tryout_id = store.get_tryout_guild_id()
    if interaction.guild is None or tryout_id is None or interaction.guild.id != tryout_id:
        try:
            log_embed = discord.Embed(
                title="🚫 Unauthorized /accept Attempt",
                description=f"**{interaction.user}** (`{interaction.user.id}`) tried to use `/accept` outside the Tryout server.",
                color=0xE74C3C,
                timestamp=datetime.now(timezone.utc)
            )
            await bot.send_log(log_embed)
        except:
            pass
        await interaction.followup.send(
            "❌ /accept can only be used in the **Tryout** server.\n"
            "Go to the Tryout server and run `/tryout` there first if it is not set.",
            ephemeral=True,
        )
        return

    main_guild = store.get_main_guild(bot)
    if main_guild is None:
        await interaction.followup.send(
            "❌ No main server configured yet.\n"
            "Go to your **main clan server** and run `/main` (as administrator).",
            ephemeral=True,
        )
        return

    # Verify that the user has completed a public Steam check in a tryout ticket
    ticket_info = store.get_tryout_ticket_for_user(user.id)
    verified_public = False
    if ticket_info:
        _, tdata = ticket_info
        verified_public = bool(tdata.get("verified_public"))

    if not verified_public:
        await interaction.followup.send(
            f"⚠️ **Warning**: {user.mention} does **not** have a verified public Steam profile from a tryout ticket.\n"
            "They must post the exact format with a **public** Steam link (Game details visible) and run `/ticketrefresh` successfully (no hidden warning) before acceptance.\n"
            "Proceed only if you have manually confirmed their info.",
            ephemeral=True
        )

    # If the user is already in the main server, we can still let them pick a role to assign right now.
    if main_guild.get_member(user.id):
        # Show role selector anyway so they can assign a role immediately
        view = RoleChoiceView(main_guild, user, interaction.user)
        await interaction.followup.send(
            f"**{user}** is already in the main server.\nSelect a role to assign right now (or use default):",
            view=view,
            ephemeral=True,
        )
        return

    # Normal flow: show role selector from MAIN guild
    view = RoleChoiceView(main_guild, user, interaction.user)
    await interaction.followup.send(
        f"Select the role **{user}** should receive on the **main server** when they join.\n"
        f"Invite will be a **single-use, 24-hour** link to **{main_guild.name}**.",
        view=view,
        ephemeral=True,
    )


# --- /setdefaultrole moved to cogs/admin.py (along with main/tryout + most other set* commands) ---

# --- /pending (shows pending for the main server) ---

@app_commands.guild_only()
@owner_or_admin_check()
@app_commands.command(name="pending", description="View users who still have an open invite to the MAIN server.")
async def pending_cmd(interaction: discord.Interaction):
    main_guild = store.get_main_guild(bot)
    if main_guild is None:
        await interaction.response.send_message(
            "❌ Main server is not configured. Use `/main` first.",
            ephemeral=True,
        )
        return

    pending = store.get_all_pending(main_guild.id)
    default_role_id = store.get_default_role(main_guild.id)
    default_role = main_guild.get_role(default_role_id) if default_role_id else None

    if not pending:
        embed = discord.Embed(
            title="📭 No pending invites",
            description="There are currently no open invites to the main server.",
            color=discord.Color.green(),
        )
        if default_role:
            embed.add_field(name="Default role", value=default_role.mention, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    lines = []
    for uid_str, role_id in list(pending.items())[:15]:
        user_mention = f"<@{uid_str}>"
        role_mention = ""
        if role_id:
            r = main_guild.get_role(role_id)
            role_mention = f" → {r.mention}" if r else f" → (role {role_id} not found)"
        else:
            role_mention = " → (no role)"
        lines.append(f"• {user_mention}{role_mention}")

    embed = discord.Embed(
        title=f"📋 Pending invites to main server ({len(pending)})",
        description="\n".join(lines),
        color=discord.Color.orange(),
    )
    if default_role:
        embed.add_field(name="Current default role", value=default_role.mention, inline=False)
    if len(pending) > 15:
        embed.set_footer(text=f"Showing first 15 of {len(pending)}")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# --- /clearpending ---

@app_commands.guild_only()
@owner_or_admin_check()
@app_commands.command(name="clearpending", description="Clear all (or one specific) pending invites to the main server.")
@app_commands.describe(user="Optional: only remove this user from the pending list")
async def clearpending_cmd(interaction: discord.Interaction, user: Optional[discord.User] = None):
    main_guild = store.get_main_guild(bot)
    if main_guild is None:
        await interaction.response.send_message("❌ Main server not configured.", ephemeral=True)
        return

    if user:
        store.clear_specific(main_guild.id, user.id)
        await interaction.response.send_message(
            f"✅ Pending invite for **{user}** removed (main server).",
            ephemeral=True,
        )
    else:
        store.clear_pending(main_guild.id)
        await interaction.response.send_message(
            "✅ All pending invites for the main server have been cleared.",
            ephemeral=True,
        )


# --- /status (simple public command, no special perms required) ---
# Shows non-sensitive bot config and runtime status for applicants and staff.
# Everyone can use it (in any server the bot is in, or DMs).

@app_commands.command(
    name="status",
    description="Show bot status: invite-only mode, server names, live panel, pending count, and wipe schedule info."
)
async def status_cmd(interaction: discord.Interaction):
    """Public status command. No owner/admin check — available to everyone."""
    # require_acceptance
    require_acc = store.is_require_acceptance()

    # Main / Tryout guild names (resolve via cache; fall back gracefully)
    main_guild = store.get_main_guild(bot)
    main_name = main_guild.name if main_guild else "Not configured"

    tryout_id = store.get_tryout_guild_id()
    tryout_name = "Not configured"
    if tryout_id:
        tg = bot.get_guild(tryout_id)
        if tg:
            tryout_name = tg.name
        else:
            tryout_name = f"Configured (ID {tryout_id}, name not cached)"

    # Live panel status (non-sensitive)
    active = store.get_active_live()
    if active:
        live_status = f"Active (BM server `{active.get('server_id', 'unknown')}` in <#{active.get('channel_id', 'unknown')}>)"
    else:
        live_status = "Inactive (no live panel currently posted)"

    # Number of pending (for main)
    main_id = store.get_main_guild_id()
    pending_count = 0
    if main_id:
        pending = store.get_all_pending(main_id)
        pending_count = len(pending)

    # Last wipe schedule info (non-sensitive: only days/times/titles, no responses or user data)
    wipe_lines = []
    legacy = store.get_wipe_schedule()
    if legacy:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        pday = legacy.get("push_day", legacy.get("weekday", 0))
        wday = legacy.get("wipe_day", pday)
        tz = legacy.get("timezone", "UTC")
        wipe_lines.append(
            f"Legacy: push {days[pday]} / wipe {days[wday]} at {legacy.get('hour', 18):02d}:{legacy.get('minute', 0):02d} ({tz})"
        )
        if legacy.get("title"):
            wipe_lines.append(f"  Title: {legacy['title']}")

    multi = store.get_wipe_schedules()
    if multi:
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for s in multi[:5]:  # limit output
            pday = s.get("push_day", 0)
            wday = s.get("wipe_day", pday)
            tz = s.get("timezone", "UTC")
            line = f"• push every {days[pday]} / wipe {days[wday]} at {s.get('hour', 18):02d}:{s.get('minute', 0):02d} ({tz})"
            if s.get("title"):
                line += f" — {s['title']}"
            wipe_lines.append(line)
        if len(multi) > 5:
            wipe_lines.append(f"(+{len(multi) - 5} more schedules)")

    if not wipe_lines:
        wipe_info = "No wipe schedules configured."
    else:
        wipe_info = "\n".join(wipe_lines)

    embed = discord.Embed(
        title="📊 Accept Bot Status",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="Invite-Only Mode (require_acceptance)",
        value="✅ Enabled (unauthorized joins are kicked)" if require_acc else "❌ Disabled (open joins allowed)",
        inline=False,
    )
    embed.add_field(name="Main Server", value=main_name, inline=True)
    embed.add_field(name="Tryout Server", value=tryout_name, inline=True)
    embed.add_field(name="Live Panel", value=live_status, inline=False)
    embed.add_field(name="Pending Invites (to main)", value=str(pending_count), inline=True)
    embed.add_field(
        name="Wipe Schedule Info",
        value=wipe_info[:1024] if len(wipe_info) > 1024 else wipe_info,
        inline=False,
    )
    embed.set_footer(text="Public non-sensitive status • Data from current config and cache")

    await interaction.response.send_message(embed=embed)


# ============================================================
# /check - Steam checker (English)
# ============================================================
# Available to everyone (no admin/owner restriction).

@app_commands.command(
    name="check",
    description="Check a Steam profile for bans, Rust hours, account age and risk flags."
)
@app_commands.describe(
    steam_id="SteamID64, full profile link, /id/ vanity, or STEAM_0: format"
)
async def check_cmd(interaction: discord.Interaction, steam_id: str):
    await interaction.response.defer(ephemeral=True)

    if not bot.steam:
        await interaction.followup.send(
            "❌ Steam API key is not configured. Set `STEAM_API_KEY` in your `.env` file.",
            ephemeral=True,
        )
        return

    try:
        data = await bot.steam.check(steam_id)
    except SteamAPIError as e:
        await interaction.followup.send(f"❌ Steam API error: {e}", ephemeral=True)
        return
    except Exception as e:
        logger.exception("Unexpected error in /check")
        await interaction.followup.send(f"❌ Unexpected error: {e}", ephemeral=True)
        return

    if data.get("error"):
        await interaction.followup.send(f"❌ {data['error']}", ephemeral=True)
        return

    if not data.get("resolved"):
        await interaction.followup.send("❌ Could not resolve a valid Steam ID.", ephemeral=True)
        return

    embed = create_steam_check_embed(data, steam_id)
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================
# REGISTER COMMANDS & RUN
# ============================================================
# Cogs-loaded commands (admin + wipe) are auto-registered by discord.py when the
# cog is loaded in setup_hook via load_extension. No manual add_command needed.
# The list below only contains commands that have NOT yet been moved to cogs.

bot.tree.add_command(live_cmd)
bot.tree.add_command(linksteam_cmd)
bot.tree.add_command(clanpanel_cmd)
bot.tree.add_command(guide_cmd)
bot.tree.add_command(ticketrefresh_cmd)
bot.tree.add_command(accept_cmd)
bot.tree.add_command(pending_cmd)
bot.tree.add_command(clearpending_cmd)
bot.tree.add_command(check_cmd)
bot.tree.add_command(status_cmd)


# Error handler for checks
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        try:
            log_embed = discord.Embed(
                title="🚫 Unauthorized Command Use",
                description=f"**{interaction.user}** (`{interaction.user.id}`) tried to use a privileged command without permissions.",
                color=0xE74C3C,
                timestamp=datetime.now(timezone.utc)
            )
            if interaction.command:
                log_embed.add_field(name="Command", value=f"/{interaction.command.name}", inline=True)
            if interaction.guild:
                log_embed.add_field(name="Guild", value=interaction.guild.name, inline=True)
            await bot.send_log(log_embed)
        except:
            pass
        await interaction.response.send_message(
            "❌ You need Administrator permissions (or be the bot owner) to use this command.",
            ephemeral=True,
        )
    elif isinstance(error, app_commands.NoPrivateMessage):
        await interaction.response.send_message("This command only works inside servers.", ephemeral=True)
    else:
        logger.exception(f"Command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message("An unexpected error occurred.", ephemeral=True)
        else:
            await interaction.followup.send("An unexpected error occurred.", ephemeral=True)


@bot.event
async def on_invite_delete(invite: discord.Invite):
    """Log to file + discord log channel when one of our tracked /accept invites is deleted.
    Records who deleted it (via audit logs) and the original creator.
    """
    if invite is None or invite.guild is None:
        return

    code = invite.code

    # Find if this code belongs to one of our pending accepts
    matched = None
    for gid_str, gdata in store.data.get("guilds", {}).items():
        for uid_str, pval in gdata.get("pending", {}).items():
            pinfo = pval if isinstance(pval, dict) else {"role_id": pval}
            if pinfo.get("invite_code") == code:
                matched = (gid_str, uid_str, pinfo)
                break
        if matched:
            break

    if not matched:
        return

    gid_str, uid_str, pinfo = matched
    creator = pinfo.get("created_by")

    # Try to determine who performed the deletion
    deleter = None
    try:
        async for entry in invite.guild.audit_logs(
            limit=5, action=discord.AuditLogAction.invite_delete
        ):
            target = entry.target
            if target and getattr(target, "code", None) == code:
                deleter = entry.user
                break
    except Exception as e:
        logger.warning(f"Failed to read audit logs for invite delete {code}: {e}")

    # Log to dedicated file
    log_line = (
        f"[{datetime.now(timezone.utc)}] "
        f"INVITE_DELETED code={code} for_user={uid_str} created_by={creator or 'unknown'} "
        f"guild={gid_str} deleted_by={str(deleter) if deleter else 'unknown'} "
        f"({getattr(deleter, 'id', None) if deleter else 'unknown'})"
    )
    try:
        with open("invite_deletions.log", "a", encoding="utf-8") as f:
            f.write(log_line + "\n")
    except Exception as e:
        logger.error(f"Failed to append to invite_deletions.log: {e}")

    # Also send to the configured log channel
    try:
        embed = discord.Embed(
            title="🗑️ Tracked Invite Deleted (from /accept)",
            color=0xFF6600,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Invite Code", value=code, inline=True)
        embed.add_field(name="For User", value=f"<@{uid_str}> (`{uid_str}`)", inline=True)
        if creator:
            embed.add_field(name="Originally Created By", value=f"<@{creator}> (`{creator}`)", inline=True)
        if deleter:
            embed.add_field(name="Deleted By", value=f"{deleter} (`{deleter.id}`)", inline=True)
        embed.add_field(name="Guild", value=invite.guild.name, inline=False)
        if invite.channel:
            embed.add_field(name="Channel", value=invite.channel.mention, inline=True)
        await bot.send_log(embed)
    except Exception as e:
        logger.warning(f"Failed to send invite deletion log to channel: {e}")


if __name__ == "__main__":
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN is missing in .env")
        sys.exit(1)

    logger.info("Starting Accept Bot...")
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
