"""
cogs/wipe.py

Wipe react system commands for the Accept Bot.

Pilot cog #2: moves all the wipe-related top-level command functions
(/wipereact, /testwipereact, /schedulewipe, /listwipes, /removewipe,
/setwipeschedule, /clearwipeschedule, plus the supporting setwipechannel
and setreasonchannel) out of bot.py.

These commands (and the scheduler in the bot class) make heavy use of the
UI elements that were extracted in the previous step:
- create_wipe_embed
- WipeReactView

All behavior identical:
- Uses the imported UI helpers (no duplication).
- Exact same parsing, validation, store interactions, ephemeral messages.
- @owner_or_admin_check and guild_only preserved.
- Timezone/choice handling identical (moved the relevant CHOICES here).
- In wipereact/scheduler paths, uses self.bot where bare `bot` was referenced before.

The legacy /setwipeschedule + /clearwipeschedule are kept (for backward compat)
alongside the more powerful /schedulewipe family.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from store import store

# UI elements extracted in previous refactoring step (ui/views.py).
# Imported here exactly as they were imported at top of bot.py.
from ui.views import WipeReactView, create_wipe_embed

import logging

logger = logging.getLogger("accept-bot")

# Import the shared owner/admin check factory (relative + fallback, same pattern as admin cog).
try:
    from ..bot import owner_or_admin_check
except Exception:
    def owner_or_admin_check():
        async def predicate(interaction: discord.Interaction) -> bool:
            if interaction.guild is None:
                return False
            return interaction.user.guild_permissions.administrator
        return app_commands.check(predicate)


# ============================================================
# CHOICES (moved here from bot.py top level because they belong exclusively
# to the wipe commands. Discord limits + used only by wipereact + schedules.)
# Using clean IANA names. Kept identical to previous definition.
# ============================================================
TIMEZONE_CHOICES = [
    app_commands.Choice(name="UTC", value="UTC"),
    app_commands.Choice(name="Europe/Amsterdam", value="Europe/Amsterdam"),
    app_commands.Choice(name="Europe/London", value="Europe/London"),
    app_commands.Choice(name="Europe/Berlin", value="Europe/Berlin"),
    app_commands.Choice(name="Europe/Paris", value="Europe/Paris"),
    app_commands.Choice(name="America/New_York", value="America/New_York"),
    app_commands.Choice(name="America/Chicago", value="America/Chicago"),
    app_commands.Choice(name="America/Los_Angeles", value="America/Los_Angeles"),
    app_commands.Choice(name="Australia/Sydney", value="Australia/Sydney"),
]

HOUR_CHOICES = [app_commands.Choice(name=str(h), value=h) for h in range(24)]
MINUTE_CHOICES = [app_commands.Choice(name=f"{m:02d}", value=m) for m in range(0, 60, 5)]  # 0,5,10,...,55


class WipeCog(commands.Cog):
    """Wipe scheduling, manual posting, and configuration commands."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- Wipe channel config (kept with wipe features) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setwipechannel",
        description="Set the channel where automatic wipe react messages will be posted."
    )
    @app_commands.describe(channel="The channel for wipe reacts")
    async def setwipechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_wipe_channel(channel.id)
        await interaction.response.send_message(
            f"✅ Wipe react messages will now be posted in {channel.mention}.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setreasonchannel",
        description="Set the channel where decline reasons for wipe reacts are sent."
    )
    @app_commands.describe(channel="The channel to receive decline reasons")
    async def setreasonchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_reason_channel(channel.id)
        await interaction.response.send_message(
            f"✅ Decline reasons will now be sent to {channel.mention}.",
            ephemeral=True,
        )

    # --- Legacy single wipe schedule (still supported) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setwipeschedule",
        description="Set recurring wipe day and time for automatic wipe react posts. Time is in the specified timezone."
    )
    @app_commands.describe(
        weekday="Day of the week the wipe happens",
        time="Time in 24h format e.g. 18:00 (in the timezone below)",
        timezone="Timezone for the time (select from list)",
        title="Title for the wipe react embed (optional, overrides default)",
        description="Description text for the wipe react embed (optional)"
    )
    @app_commands.choices(weekday=[
        app_commands.Choice(name="Monday", value=0),
        app_commands.Choice(name="Tuesday", value=1),
        app_commands.Choice(name="Wednesday", value=2),
        app_commands.Choice(name="Thursday", value=3),
        app_commands.Choice(name="Friday", value=4),
        app_commands.Choice(name="Saturday", value=5),
        app_commands.Choice(name="Sunday", value=6),
    ])
    @app_commands.choices(timezone=TIMEZONE_CHOICES)
    async def setwipeschedule(
        self,
        interaction: discord.Interaction,
        weekday: int,
        time: str,
        timezone: Optional[str] = "UTC",
        title: Optional[str] = None,
        description: Optional[str] = None
    ):
        try:
            # Robust parsing: strip whitespace
            cleaned_time = time.strip().replace(" ", "")
            parts = cleaned_time.split(":")
            if len(parts) != 2:
                raise ValueError
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError

            # Normalize timezone if user pastes friendly name with (CET/CEST) etc.
            tz_input = (timezone or "UTC").strip()
            if "(" in tz_input:
                tz_input = tz_input.split("(", 1)[0].strip()
            if tz_input:
                ZoneInfo(tz_input)
            timezone = tz_input
        except Exception:
            await interaction.response.send_message(
                "❌ Invalid time (HH:MM). Timezone must be selected from the list.",
                ephemeral=True
            )
            return

        store.set_wipe_schedule(weekday, hour, minute, title, description, timezone=timezone)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        tz = timezone or "UTC"
        msg = (
            f"✅ Wipe schedule set for every **{days[weekday]}** at **{hour:02d}:{minute:02d}** ({tz}).\n"
            "The bot will automatically post a wipe react message at that time."
        )
        if title or description:
            msg += "\nCustom title/description saved."
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="clearwipeschedule",
        description="Remove the automatic wipe schedule."
    )
    async def clearwipeschedule(self, interaction: discord.Interaction):
        if "wipe_schedule" in store.data["config"]:
            del store.data["config"]["wipe_schedule"]
            store._save()
        await interaction.response.send_message(
            "✅ Wipe schedule cleared. No more automatic wipe reacts.",
            ephemeral=True
        )

    # --- Modern multi-schedule wipe support ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="schedulewipe",
        description="Set recurring wipe schedule (push/ wipe days + time in timezone)."
    )
    @app_commands.describe(
        push_day="The weekday the react message will be posted every week (the 'push day')",
        wipe_day="The weekday the actual server wipe will happen on",
        wipe_time="The time the wipe happens (HH:MM) in the timezone below — this is also when the react is posted on the push day every week",
        timezone="Timezone for the wipe_time (select from list)",
        title="Custom title for the wipe react embed (optional)",
        description="Custom description for the wipe react embed (optional)"
    )
    @app_commands.choices(push_day=[
        app_commands.Choice(name="Monday", value=0),
        app_commands.Choice(name="Tuesday", value=1),
        app_commands.Choice(name="Wednesday", value=2),
        app_commands.Choice(name="Thursday", value=3),
        app_commands.Choice(name="Friday", value=4),
        app_commands.Choice(name="Saturday", value=5),
        app_commands.Choice(name="Sunday", value=6),
    ])
    @app_commands.choices(wipe_day=[
        app_commands.Choice(name="Monday", value=0),
        app_commands.Choice(name="Tuesday", value=1),
        app_commands.Choice(name="Wednesday", value=2),
        app_commands.Choice(name="Thursday", value=3),
        app_commands.Choice(name="Friday", value=4),
        app_commands.Choice(name="Saturday", value=5),
        app_commands.Choice(name="Sunday", value=6),
    ])
    @app_commands.choices(timezone=TIMEZONE_CHOICES)
    async def schedulewipe(
        self,
        interaction: discord.Interaction,
        push_day: int,
        wipe_day: int,
        wipe_time: str,
        timezone: Optional[str] = "UTC",
        title: Optional[str] = None,
        description: Optional[str] = None
    ):
        try:
            # Robust parsing: strip whitespace, support "23:00" or " 23 : 00 "
            cleaned_time = wipe_time.strip().replace(" ", "")
            parts = cleaned_time.split(":")
            if len(parts) != 2:
                raise ValueError
            hour = int(parts[0])
            minute = int(parts[1])
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError

            # Normalize timezone if user pastes a friendly name like "Europe/Amsterdam (CET/CEST)"
            tz_input = (timezone or "UTC").strip()
            if "(" in tz_input:
                tz_input = tz_input.split("(", 1)[0].strip()
            if tz_input:
                ZoneInfo(tz_input)
            # Use the cleaned tz_input for storage/display
            timezone = tz_input
        except Exception:
            await interaction.response.send_message(
                "❌ Invalid wipe_time (use HH:MM). Timezone must be selected from the list.",
                ephemeral=True
            )
            return

        # Add this as a new schedule entry.
        # Run the command multiple times (with different push_day / wipe_day / time) to have multiple wipe reacts.
        wipe_id = store.add_wipe_schedule(push_day, wipe_day, hour, minute, title, description, timezone=timezone)
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        tz = timezone or "UTC"
        await interaction.response.send_message(
            f"✅ Added wipe react schedule (ID: {wipe_id}):\n"
            f"• Posted every **{days[push_day]}** at **{hour:02d}:{minute:02d}** ({tz}) (push day)\n"
            f"• Message will show wipe on **{days[wipe_day]}** at that time\n\n"
            "Run /schedulewipe again with different params to add more wipes. Use /listwipes to see all.",
            ephemeral=True
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="listwipes",
        description="List all scheduled (specific and recurring) wipe reacts."
    )
    async def listwipes(self, interaction: discord.Interaction):
        lines = []
        # Multiple scheduled wipe reacts (added via /schedulewipe - supports as many as you want)
        schedules = store.get_wipe_schedules()
        if schedules:
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            lines.append("**Wipe reacts (multiple supported):**")
            for s in schedules:
                pday = days[s.get('push_day', 0)]
                wday = days[s.get('wipe_day', 0)]
                tz = s.get('timezone', 'UTC')
                lines.append(
                    f"- ID {s['id']}: push every {pday} at {s['hour']:02d}:{s['minute']:02d} ({tz}), wipe on {wday} at that time"
                )
                if s.get("title"):
                    lines.append(f"  Title: {s['title']}")

        # Legacy one-off specific wipes (if any)
        scheduled = store.get_scheduled_wipes()
        pending = [w for w in scheduled if not w.get("posted")]
        if pending:
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            lines.append("**One-off scheduled wipes:**")
            for w in pending[:10]:
                wd = days[w.get('wipe_weekday', 4)]
                lines.append(f"- Push: {w['push_datetime']} | Wipe: {wd} {w['wipe_date']} {w['wipe_time']} (ID: {w['id']})")

        if not lines:
            lines.append("No wipe schedules set. Use /schedulewipe (you can run it multiple times for different wipes).")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="removewipe",
        description="Remove a specific scheduled wipe by its ID (from /listwipes)."
    )
    @app_commands.describe(wipe_id="The ID of the scheduled wipe to remove")
    async def removewipe(self, interaction: discord.Interaction, wipe_id: str):
        scheduled = store.get_scheduled_wipes()
        new_list = [w for w in scheduled if w["id"] != wipe_id]
        if len(new_list) == len(scheduled):
            await interaction.response.send_message(
                "❌ No wipe found with that ID (or it was already posted/removed).",
                ephemeral=True
            )
            return
        store.data["config"]["scheduled_wipes"] = new_list
        store._save()
        await interaction.response.send_message(
            f"✅ Removed scheduled wipe with ID `{wipe_id}`.",
            ephemeral=True
        )

    # --- Manual / test wipe react posting (uses the extracted UI) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="testwipereact",
        description="Post a test wipe react in this channel so you can preview the embed and test the buttons."
    )
    async def testwipereact(self, interaction: discord.Interaction):
        schedule = store.get_wipe_schedule() or {}
        now = datetime.now()
        # Unique key every time so counts always start fresh for each test
        test_key = f"test_{int(now.timestamp())}"
        # Nice label for display (the timestamp logic will show the scheduled time anyway)
        display_label = now.strftime("%A, %B %d, %Y") + " (TEST)"

        embed = create_wipe_embed(test_key, display_label)
        view = WipeReactView(test_key, display_label)

        await interaction.response.send_message(
            "**This is a TEST wipe react.**\n"
            "Buttons work for preview, but responses won't affect real data or the current active wipe.\n"
            "Decline reasons (if any) will still go to the reason channel for testing.",
            embed=embed,
            view=view
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="wipereact",
        description="Manually post wipe react with buttons. Supports time + timezone."
    )
    @app_commands.describe(
        label="Display date/label for this wipe react (e.g. Friday 13 June)",
        hour="Hour of the wipe time (0-23)",
        minute="Minute of the wipe time (every 5 min)",
        tz="Timezone for the wipe time (select from list)",
        title="Optional custom title for the embed",
        description="Optional custom description for the embed"
    )
    @app_commands.choices(
        hour=HOUR_CHOICES,
        minute=MINUTE_CHOICES,
        tz=TIMEZONE_CHOICES,
    )
    async def wipereact(
        self,
        interaction: discord.Interaction,
        label: Optional[str] = None,
        hour: Optional[int] = None,
        minute: Optional[int] = None,
        tz: Optional[str] = "UTC",
        title: Optional[str] = None,
        description: Optional[str] = None
    ):
        """Manually post a wipe react (real, counts towards responses). Sets it as the current active wipe so buttons/UI survive restarts.
        If hour + minute + tz provided, uses that for the display timestamp (e.g. 23:00 Europe/Amsterdam = 11pm Amsterdam time).
        """
        now = datetime.now()
        # Unique but human readable key for this manual wipe
        wipe_key = f"manual_{now.strftime('%Y-%m-%d_%H%M')}"
        display_label = label or now.strftime("%A, %B %d, %Y")

        # Normalize and validate tz (support pasting "Europe/Amsterdam (CET/CEST)" etc.)
        tz_input = (tz or "UTC").strip()
        if "(" in tz_input:
            tz_input = tz_input.split("(", 1)[0].strip()
        if tz:
            try:
                ZoneInfo(tz_input)
            except Exception:
                await interaction.response.send_message(
                    "❌ Invalid timezone. Please select from the dropdown list (e.g. Europe/Amsterdam).",
                    ephemeral=True
                )
                return

        target_dt = None
        if hour is not None and minute is not None:
            try:
                tz_obj = ZoneInfo(tz_input)
                now_local = datetime.now(tz_obj)
                target_local = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
                target_dt = target_local.astimezone(timezone.utc)
                if not label:
                    display_label = f"{now_local.strftime('%A, %B %d, %Y')} {hour:02d}:{minute:02d} ({tz_input})"
            except Exception:
                await interaction.response.send_message(
                    "❌ Could not compute time for selected hour/minute/timezone.",
                    ephemeral=True
                )
                return

        embed = create_wipe_embed(wipe_key, display_label, target_dt=target_dt)
        if title:
            embed.title = title
        if description:
            embed.description = description

        view = WipeReactView(wipe_key, display_label)

        # Prefer configured wipe channel if set, otherwise use the channel the command was used in
        ch = interaction.channel
        wipe_ch_id = store.get_wipe_channel_id()
        if wipe_ch_id and interaction.guild:
            configured = interaction.guild.get_channel(wipe_ch_id)
            if configured:
                ch = configured

        try:
            msg = await ch.send(
                "🗓️ **Wipe React** (manual)",
                embed=embed,
                view=view
            )
            target_ts = int(target_dt.timestamp()) if target_dt else None
            store.set_current_wipe_message(ch.id, msg.id, wipe_key, target_ts=target_ts)
            await interaction.response.send_message(
                f"✅ Manual wipe react posted in {ch.mention}.\nKey: `{wipe_key}` (responses are real; UI will work after restart).",
                ephemeral=True
            )
            logger.info(f"Manual /wipereact posted by {interaction.user} (key={wipe_key}) in channel {ch.id}")
        except Exception as e:
            await interaction.response.send_message(f"❌ Failed to post wipe react: {e}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(WipeCog(bot))
