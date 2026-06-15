"""
ui/views.py
Extracted discord.ui.View / Modal classes + tightly coupled helpers from bot.py.

This is the first extraction step toward a clean modular structure (cogs + ui).
All original behavior is preserved 100%:
- Persistent views (timeout=None) for wipe reacts and live stats.
- on_ready re-attach logic continues to work via the same constructors.
- All button/modal flows, decline reasons, role choice, clan panel, live refresh.
- References to global `bot` (injected at runtime), `store`, `logger`, and helper functions.
- Embed builders used by the UIs.

Usage from bot.py (or future cogs):
    from ui.views import (
        WipeReactView, DeclineReasonModal, RoleChoiceView,
        LiveServerView, RequestTeamInviteView,
        create_wipe_embed, build_bm_server_embed,
        create_main_invite, safe_send_dm,
    )

    # After bot = AcceptBot():
    import ui.views as ui_views
    ui_views.bot = bot

Future: views can be enhanced to accept bot explicitly in __init__ for even cleaner DI.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import discord

# Local persistent store (already extracted; singleton API unchanged)
from store import store


logger = logging.getLogger("accept-bot")


# ============================================================
# RUNTIME BOT INJECTION (avoids circular import while keeping
# all original bare `bot` references inside moved code identical)
# ============================================================
# bot.py will do:
#   bot = AcceptBot()
#   import ui.views as _v
#   _v.bot = bot
# This makes every `bot.xxx` inside the views/helpers resolve correctly
# at runtime, exactly as they did when defined inside bot.py.
bot: Optional[object] = None


# ============================================================
# HELPERS EXTRACTED BECAUSE THEY ARE ONLY USED BY (OR TOGETHER WITH) THE VIEWS
# (create_steam_check_embed stays in bot.py as it is also used by /check + ticket logic)
# ============================================================

async def create_main_invite(guild_id: int, inviter: discord.Member) -> Optional[str]:
    """Create a 1-use, 1-day invite to the main server.
    We fetch a fresh Guild object to avoid any cache or object reference issues.
    If a specific channel was set with /main, the invite will be created for that channel.
    """
    try:
        # Always get a fresh, proper Guild object
        guild = bot.get_guild(guild_id)
        if guild is None:
            guild = await bot.fetch_guild(guild_id)

        if guild is None:
            logger.error(f"Could not retrieve main guild with ID {guild_id} to create invite")
            return None

        # Prefer the channel where /main was originally run
        channel = None
        main_channel_id = store.get_main_channel_id()
        if main_channel_id:
            channel = guild.get_channel(main_channel_id)
            if channel is None:
                try:
                    channel = await guild.fetch_channel(main_channel_id)
                except Exception:
                    channel = None

        if channel and hasattr(channel, "create_invite"):
            # Create invite specifically for this channel
            invite = await channel.create_invite(
                max_age=86400,   # 1 day
                max_uses=1,
                unique=True,
                reason=f"Invite sent via /accept by {inviter} (targeting specific channel)",
            )
        else:
            # Fallback to guild-level invite (Discord will pick a suitable channel)
            invite = await guild.create_invite(
                max_age=86400,   # 1 day
                max_uses=1,
                unique=True,
                reason=f"Invite sent via /accept by {inviter}",
            )

        return invite.url
    except discord.Forbidden:
        return None
    except Exception as e:
        logger.exception(f"Failed to create invite: {e}")
        return None


async def safe_send_dm(user: discord.User | discord.Member, embed: discord.Embed) -> bool:
    """Try to DM the user. Returns True on success."""
    try:
        await user.send(embed=embed)
        return True
    except discord.Forbidden:
        return False
    except Exception as e:
        logger.warning(f"Failed to DM {user}: {e}")
        return False


# ============================================================
# WIPE REACT UI + EMBED
# ============================================================

def create_wipe_embed(wipe_key: str, display_label: str = None, target_dt: Optional[datetime] = None) -> discord.Embed:
    schedule = store.get_wipe_schedule() or {}
    title = schedule.get("title", "🗓️ Wipe React")
    if display_label and "(TEST)" in str(display_label):
        title = f"{title} {display_label}"
    desc = schedule.get("description", "React below if you're playing this wipe!")

    responses = store.get_wipe_responses(wipe_key)
    accept_ids = responses.get("accepts", [])
    late_ids = responses.get("lates", [])
    decline_dict = responses.get("declines", {})
    decline_ids = [int(k) for k in decline_dict.keys()]

    accepts = len(accept_ids)
    lates = len(late_ids)
    declines = len(decline_dict)

    def _format_mentions(ids: list[int]) -> str:
        if not ids:
            return "None yet"
        # Limit to keep embed under 1024 chars per field
        mentions = [f"<@{uid}>" for uid in ids[:12]]
        text = " ".join(mentions)
        if len(ids) > 12:
            text += f" +{len(ids) - 12} more"
        return text

    # Compute Discord timestamp
    # If target_dt is provided (for specific scheduled wipes or manual with tz), use it exactly.
    # Otherwise fall back to schedule time.
    # For manual /wipereact with custom timezone, target_ts may be stored in current_wipe_info.
    timestamp_display = wipe_key
    try:
        if not target_dt:
            current = store.get_current_wipe_info()
            if current and current.get("target_ts"):
                try:
                    target_dt = datetime.fromtimestamp(current["target_ts"], tz=timezone.utc)
                except:
                    pass
        if target_dt:
            unix_ts = int(target_dt.timestamp())
            timestamp_display = f"<t:{unix_ts}:F>"
        elif schedule:
            base_dt = datetime.now()
            hour = schedule.get("hour", 18)
            minute = schedule.get("minute", 0)
            scheduled_dt = base_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            unix_ts = int(scheduled_dt.timestamp())
            timestamp_display = f"<t:{unix_ts}:F>"
        else:
            timestamp_display = "Time not scheduled"
    except Exception:
        pass

    embed = discord.Embed(
        title=title,
        description=desc,
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )

    embed.add_field(name="Wipe Time", value=timestamp_display, inline=False)

    embed.add_field(
        name=f"✅ Accept ({accepts})",
        value=_format_mentions(accept_ids),
        inline=False
    )
    embed.add_field(
        name=f"⏰ Late ({lates})",
        value=_format_mentions(late_ids),
        inline=False
    )
    embed.add_field(
        name=f"❌ Decline ({declines})",
        value=_format_mentions(decline_ids),
        inline=False
    )

    embed.set_footer(text="Click buttons below • Decline requires a reason (sent to staff) • One choice per person")
    return embed


class WipeReactView(discord.ui.View):
    def __init__(self, wipe_key: str, display_label: str = None):
        super().__init__(timeout=None)
        self.wipe_key = wipe_key
        self.display_label = display_label or wipe_key

    @discord.ui.button(label="✅ Accept", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        store.set_user_response(self.wipe_key, "accept", interaction.user.id)
        embed = create_wipe_embed(self.wipe_key, self.display_label)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⏰ Late", style=discord.ButtonStyle.primary)
    async def late(self, interaction: discord.Interaction, button: discord.ui.Button):
        store.set_user_response(self.wipe_key, "late", interaction.user.id)
        embed = create_wipe_embed(self.wipe_key, self.display_label)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="❌ Decline", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DeclineReasonModal(self.wipe_key))


class DeclineReasonModal(discord.ui.Modal):
    # Define TextInput at class level (recommended pattern in discord.py for reliable modal component registration)
    reason = discord.ui.TextInput(
        label="Why can't you play the wipe?",
        placeholder="e.g. Work, no time, holiday, etc.",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=500
    )

    def __init__(self, wipe_key: str):
        # Short, clean custom_id (the previous auto-generated or long ones were causing the 1-40 length error)
        cleaned = wipe_key.replace(" ", "_").replace("(", "").replace(")", "").replace("-", "_")[:50]
        super().__init__(title="Decline Reason", custom_id=f"decline_modal_{cleaned}")
        self.wipe_key = wipe_key
        # Set custom_id on the TextInput after super() — this is the reliable way
        self.reason.custom_id = f"reason_input_{cleaned}"

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason.value.strip()
        store.set_user_response(self.wipe_key, "decline", interaction.user.id, reason)

        # Send to configured reason channel
        ch_id = store.get_reason_channel_id()
        if ch_id:
            ch = bot.get_channel(ch_id)
            if ch is None:
                try:
                    ch = await bot.fetch_channel(ch_id)
                except:
                    ch = None
            if ch:
                await ch.send(
                    f"**{interaction.user.mention}** declined the wipe on **{self.wipe_key}**:\n> {reason}"
                )

        # Refresh the wipe react message with updated counts (only affects the active real wipe)
        info = store.get_current_wipe_info()
        if info and info.get("wipe_date") == self.wipe_key:
            try:
                ch = bot.get_channel(info["channel_id"])
                if ch:
                    msg = await ch.fetch_message(info["message_id"])
                    embed = create_wipe_embed(self.wipe_key)
                    await msg.edit(embed=embed)
            except Exception as e:
                logger.warning(f"Failed to refresh wipe message after decline: {e}")

        await interaction.response.send_message("✅ Your reason has been recorded. Thank you!", ephemeral=True)


# ============================================================
# UI: Role selector for /accept (pulls roles from the MAIN server)
# ============================================================
class RoleChoiceView(discord.ui.View):
    """Ephemeral select menu so staff can choose a role from the main server."""

    def __init__(self, main_guild: discord.Guild, target_user: discord.User, invoker: discord.Member):
        super().__init__(timeout=180.0)
        self.main_guild = main_guild
        self.target_user = target_user
        self.invoker = invoker
        self.selected_role: Optional[discord.Role] = None

        # Collect assignable roles from MAIN guild (bot must be able to assign them)
        assignable = [
            r for r in main_guild.roles
            if not r.is_default()
            and not r.managed
            and r < main_guild.me.top_role
        ]
        assignable.sort(key=lambda r: r.position, reverse=True)

        options: list[discord.SelectOption] = []
        for role in assignable[:25]:
            options.append(
                discord.SelectOption(
                    label=role.name[:100],
                    value=str(role.id),
                )
            )

        if options:
            select = discord.ui.Select(
                placeholder="Choose a role the user will receive on the MAIN server...",
                min_values=1,
                max_values=1,
                options=options,
            )
            select.callback = self._on_role_selected
            self.add_item(select)
        else:
            # Fallback button if no roles are assignable
            btn = discord.ui.Button(label="No assignable roles - use default (if set)", style=discord.ButtonStyle.secondary)
            btn.callback = self._use_default
            self.add_item(btn)

    async def _on_role_selected(self, interaction: discord.Interaction):
        if not self.children:
            await interaction.response.send_message("No roles available.", ephemeral=True)
            return

        select = self.children[0]
        try:
            role_id = int(select.values[0])
        except Exception:
            await interaction.response.send_message("Invalid selection.", ephemeral=True)
            return

        self.selected_role = self.main_guild.get_role(role_id)
        if self.selected_role is None:
            await interaction.response.send_message("Role no longer exists.", ephemeral=True)
            return

        # Disable the menu
        for child in self.children:
            if isinstance(child, (discord.ui.Select, discord.ui.Button)):
                child.disabled = True

        await interaction.response.edit_message(
            content=f"**Selected role:** {self.selected_role.mention}\nCreating invite to the main server...",
            view=self
        )

        await self._complete_accept(interaction)

    async def _use_default(self, interaction: discord.Interaction):
        for child in self.children:
            if isinstance(child, (discord.ui.Select, discord.ui.Button)):
                child.disabled = True

        self.selected_role = None
        await interaction.response.edit_message(
            content="**No specific role** — will use the server's default role (if configured).\nCreating invite...",
            view=self
        )
        await self._complete_accept(interaction)

    async def _complete_accept(self, interaction: discord.Interaction):
        """Create 1-day 1-use invite to MAIN, DM the user, and store pending."""
        main_guild = self.main_guild
        target = self.target_user
        invoker = self.invoker

        # Already in main server? Just assign role directly.
        existing = main_guild.get_member(target.id)
        if existing:
            if self.selected_role:
                try:
                    await existing.add_roles(self.selected_role, reason=f"Direct role assignment via /accept by {invoker}")
                except Exception:
                    pass

            # Make sure they are protected under invite-only mode
            store.add_to_whitelist(target.id)

            await interaction.followup.send(
                f"ℹ️ **{target}** is already in the main server. Role assigned if possible.",
                ephemeral=True
            )
            return

        # Create invite (1 use, 1 day) to the MAIN server
        invite_url = await create_main_invite(main_guild.id, invoker)
        if not invite_url:
            await interaction.followup.send(
                "❌ Failed to create an invite. The bot needs the **Create Instant Invite** permission in the **main** server.",
                ephemeral=True,
            )
            return

        role_text = (
            f"You will automatically receive the **{self.selected_role.name}** role when you join."
            if self.selected_role
            else "No specific role was chosen (default role may be assigned if configured)."
        )

        dm_embed = discord.Embed(
            title=f"Invitation to {main_guild.name}",
            description=(
                f"Hi {target.mention},\n\n"
                f"You have been accepted to the main server!\n\n"
                f"{role_text}\n\n"
                f"**Join here (single-use, expires in 24 hours):**\n{invite_url}\n\n"
                f"*This invite can only be used once.*"
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc),
        )
        if invoker.display_avatar:
            dm_embed.set_footer(text=f"Accepted by {invoker}", icon_url=invoker.display_avatar.url)

        dm_sent = await safe_send_dm(target, dm_embed)

        # Always store pending under the MAIN guild ID
        role_id_to_store = self.selected_role.id if self.selected_role else None
        invite_code = None
        if invite_url:
            # Extract code from URL like https://discord.gg/abc123
            try:
                invite_code = invite_url.rstrip("/").split("/")[-1]
            except Exception:
                invite_code = None
        store.add_pending(
            main_guild.id,
            target.id,
            role_id_to_store,
            invite_code=invite_code,
            created_by=invoker.id
        )
        store.add_to_whitelist(target.id)  # Once accepted via /accept, they are allowed to join (even if they rejoin later)

        # Log successful /accept
        try:
            role_name = self.selected_role.name if self.selected_role else "None (default may apply)"
            log_embed = discord.Embed(
                title="✅ /accept Used Successfully",
                color=0x2ECC71,
                timestamp=datetime.now(timezone.utc)
            )
            log_embed.add_field(name="Accepted User", value=f"{target} (`{target.id}`)", inline=True)
            log_embed.add_field(name="Accepted By", value=f"{invoker} (`{invoker.id}`)", inline=True)
            log_embed.add_field(name="Role on Join", value=role_name, inline=True)
            log_embed.add_field(name="Invite Link", value=invite_url or "N/A", inline=False)
            await bot.send_log(log_embed)
        except Exception:
            pass

        if dm_sent:
            await interaction.followup.send(
                f"✅ Invite successfully sent to **{target}** via DM.\n"
                f"Role on join: {self.selected_role.mention if self.selected_role else 'None (default may apply)'}",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Could not DM **{target}** (they may have DMs disabled).\n"
                f"Manual invite link:\n{invite_url}\n"
                f"Role on join: {self.selected_role.mention if self.selected_role else 'None'}",
                ephemeral=True,
            )


# ============================================================
# LIVE SERVER STATS VIEW + EMBED BUILDER
# ============================================================

class LiveServerView(discord.ui.View):
    def __init__(self, server_id: str):
        super().__init__(timeout=None)
        self.server_id = server_id

    @discord.ui.button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not bot.bm:
            await interaction.response.send_message("BattleMetrics not configured.", ephemeral=True)
            return
        try:
            server = await bot.bm.get_server(self.server_id)
            if not server:
                await interaction.response.send_message("Server not found.", ephemeral=True)
                return
            embed = build_bm_server_embed(server)
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception as e:
            await interaction.response.send_message(f"Error refreshing: {e}", ephemeral=True)


def build_bm_server_embed(server: dict) -> discord.Embed:
    attrs = server.get("attributes", {})
    name = attrs.get("name", "Unknown Server")
    status = attrs.get("status", "unknown")
    players = attrs.get("players", 0)
    max_players = attrs.get("maxPlayers", 0)
    rank = attrs.get("rank", "?")
    country = attrs.get("country", "??")
    details = attrs.get("details", {})

    embed = discord.Embed(
        title=f"📊 {name}",
        url=f"https://www.battlemetrics.com/servers/rust/{server.get('id')}",
        color=discord.Color.green() if status == "online" else discord.Color.red(),
    )

    embed.add_field(name="Status", value=status.upper(), inline=True)
    embed.add_field(name="Players", value=f"{players}/{max_players}", inline=True)
    embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    embed.add_field(name="Country", value=country, inline=True)

    # Rust specific details if available
    if "rust_last_wipe" in details:
        last_wipe = details["rust_last_wipe"]
        embed.add_field(name="Last Wipe", value=last_wipe, inline=True)
    if "rust_next_wipe" in details:
        next_wipe = details.get("rust_next_wipe", "Unknown")
        embed.add_field(name="Next Wipe", value=next_wipe, inline=True)

    embed.set_footer(text="Data from BattleMetrics • Click refresh to update")
    return embed


# ============================================================
# REQUEST TEAM INVITE VIEW (was nested inside /clanpanel; now top-level for reusability)
# ============================================================

class RequestTeamInviteView(discord.ui.View):
    """Button view for the clan invite panel. Supports multiple requests with per-user cooldown."""
    def __init__(self, inviter: discord.Member, ping_content: str):
        super().__init__(timeout=None)
        self.inviter = inviter
        self.ping_content = ping_content
        self._cooldowns: dict[int, float] = {}  # user.id -> last successful request time
        self.COOLDOWN_SECONDS = 30.0

    @discord.ui.button(label="Request Team Invite", style=discord.ButtonStyle.primary)
    async def request_team(self, btn_interaction: discord.Interaction, button: discord.ui.Button):
        linked = store.get_linked_steam(btn_interaction.user.id)
        if not linked:
            await btn_interaction.response.send_message("you need to /linksteam", ephemeral=True)
            return

        # 30s cooldown per user for the clan invite request (prevents spam pings to owners)
        now = time.time()
        last = self._cooldowns.get(btn_interaction.user.id, 0.0)
        if now - last < self.COOLDOWN_SECONDS:
            remaining = int(self.COOLDOWN_SECONDS - (now - last)) + 1
            await btn_interaction.response.send_message(
                f"⏳ Please wait {remaining}s before requesting another clan invite.",
                ephemeral=True
            )
            return

        # Record cooldown *only* on successful request post
        self._cooldowns[btn_interaction.user.id] = now

        # Post exactly the Rust /clan invite text + owner tag (no @here, no "Requested by", no extras).
        # content= for the ping (if owner role set), embed for the clean "/clan invite <id>" text.
        req_content = self.ping_content if self.ping_content else None
        req_embed = discord.Embed(
            description=f"/clan invite {linked}",
            color=discord.Color(0x3498db)
        )
        await btn_interaction.response.send_message(content=req_content, embed=req_embed, ephemeral=False)
        # Do NOT disable the button — allows multiple users to request from the same panel.
