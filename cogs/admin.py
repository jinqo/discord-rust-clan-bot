"""
cogs/admin.py

Admin / config / setup commands for the Accept Bot.

Pilot cog for the refactoring: moves the top-level command functions for
server configuration (/main, /tryout), invite-only mode, most /set* commands
(setbm*, setlivestatschannel, setclan*, setlogchannel, settryoutticketcategory,
setdefaultrole, etc.) out of bot.py.

All behavior is preserved 100%:
- Decorators (@app_commands.guild_only, @owner_or_admin_check)
- Ephemeral responses, error messages, store calls exactly as before.
- Uses self.bot instead of the old global bare `bot` inside functions.
- owner_or_admin_check imported via relative (with defensive fallback).

Relevant CHOICES (TIMEZONE etc.) live in the wipe cog (they are wipe-specific).
"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from store import store, parse_bm_server_id

# Import the shared owner/admin check factory.
# This mirrors the pattern used in sibling bots (clan-bot, osint-bot).
# Relative import "from ..bot" works when the cog is loaded as an extension.
try:
    from ..bot import owner_or_admin_check
except Exception:
    # Defensive fallback (should never be reached in normal load)
    def owner_or_admin_check():
        async def predicate(interaction: discord.Interaction) -> bool:
            # Fallback: at least require admin in guild
            if interaction.guild is None:
                return False
            return interaction.user.guild_permissions.administrator
        return app_commands.check(predicate)


class AdminCog(commands.Cog):
    """Configuration and setup commands (owner or admin protected)."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # --- /main and /tryout (configure the two servers the bot lives in) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="main",
        description="Set the current server as the MAIN clan server (invites and roles are handled here)."
    )
    async def main(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        channel_id = interaction.channel.id if interaction.channel else None
        store.set_main_guild(interaction.guild.id, channel_id)
        channel_mention = interaction.channel.mention if interaction.channel else "a default channel"
        await interaction.response.send_message(
            f"✅ **{interaction.guild.name}** is now configured as the **Main** server.\n"
            f"Invites created with /accept will be created for {channel_mention} (if possible).\n"
            "Roles will be assigned from this server.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="tryout",
        description="Set the current server as the TRYOUT server (where you usually run /accept from)."
    )
    async def tryout(self, interaction: discord.Interaction):
        if interaction.guild is None:
            return
        store.set_tryout_guild(interaction.guild.id)
        await interaction.response.send_message(
            f"✅ **{interaction.guild.name}** is now configured as the **Tryout** server.\n"
            "Staff will typically use /accept here to send people to the main server.",
            ephemeral=True,
        )

    # --- Invite-only mode (nobody can join main without being accepted via tryout) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="enableinviteonly",
        description="Enable invite-only: only accepted users (via /accept) can join. Current members protected."
    )
    async def enableinviteonly(self, interaction: discord.Interaction):
        main_guild = store.get_main_guild(self.bot)
        if main_guild is None:
            await interaction.response.send_message(
                "❌ Main server is not configured. Use `/main` in the main server first.",
                ephemeral=True,
            )
            return

        # Snapshot current members so they won't be kicked later
        store.snapshot_current_members(main_guild)
        store.set_require_acceptance(True)

        await interaction.response.send_message(
            f"✅ **Invite-only mode enabled** on **{main_guild.name}**.\n\n"
            "• From now on, anyone who joins the main server without being accepted via `/accept` from the tryout server will be automatically kicked.\n"
            "• All members who were already in the server when this was enabled are protected (they will not be kicked).\n"
            "• People accepted through the normal /accept flow will be allowed to join and are added to the protected list.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="disableinviteonly",
        description="Disable invite-only mode on the MAIN server. Anyone will be able to join freely again."
    )
    async def disableinviteonly(self, interaction: discord.Interaction):
        main_guild = store.get_main_guild(self.bot)
        guild_name = main_guild.name if main_guild else "the main server"
        store.set_require_acceptance(False)

        await interaction.response.send_message(
            f"✅ **Invite-only mode disabled** on {guild_name}.\n"
            "Anyone can now join the main server freely.",
            ephemeral=True,
        )

    # --- BattleMetrics server links (used by /check + live panel) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setbmserver",
        description="Link your main BattleMetrics server ID so /check can show live server status."
    )
    @app_commands.describe(server_id="BattleMetrics server ID (from the URL: battlemetrics.com/servers/rust/XXXXX)")
    async def setbmserver(self, interaction: discord.Interaction, server_id: str):
        parsed = parse_bm_server_id(server_id)
        if not parsed:
            await interaction.response.send_message(
                "Could not parse a valid BattleMetrics server ID from the input (URL or numeric ID expected).",
                ephemeral=True
            )
            return
        store.set_bm_server(parsed)
        await interaction.response.send_message(
            f"✅ BattleMetrics main server `{parsed}` linked.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setbmaimserver",
        description="Link your BattleMetrics Aim Training server so /check shows playtime on aim servers."
    )
    @app_commands.describe(server_id="BattleMetrics server ID for aim training")
    async def setbmaimserver(self, interaction: discord.Interaction, server_id: str):
        parsed = parse_bm_server_id(server_id)
        if not parsed:
            await interaction.response.send_message(
                "Could not parse a valid BattleMetrics server ID from the input (URL or numeric ID expected).",
                ephemeral=True
            )
            return
        store.set_bm_aim_server(parsed)
        await interaction.response.send_message(
            f"✅ BattleMetrics Aim Training server `{parsed}` linked.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setbmbuildingserver",
        description="Link your BattleMetrics Building server so /check shows playtime on building servers."
    )
    @app_commands.describe(server_id="BattleMetrics server ID for building practice")
    async def setbmbuildingserver(self, interaction: discord.Interaction, server_id: str):
        parsed = parse_bm_server_id(server_id)
        if not parsed:
            await interaction.response.send_message(
                "Could not parse a valid BattleMetrics server ID from the input (URL or numeric ID expected).",
                ephemeral=True
            )
            return
        store.set_bm_building_server(parsed)
        await interaction.response.send_message(
            f"✅ BattleMetrics Building server `{parsed}` linked.",
            ephemeral=True,
        )

    # --- Live Server Stats channel ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setlivestatschannel",
        description="Set the channel where the live server stats panel will be posted and auto-updated."
    )
    @app_commands.describe(channel="The channel for the live BattleMetrics stats")
    async def setlivestatschannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_live_stats_channel(channel.id)
        await interaction.response.send_message(
            f"✅ Live server stats will now be posted and auto-updated in {channel.mention}.",
            ephemeral=True,
        )

    # --- Set clan channels/roles ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setclaninvitechannel",
        description="Set the channel where clan invite panels will be posted."
    )
    @app_commands.describe(channel="Channel for clan invite panels")
    async def setclaninvitechannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_clan_invite_channel(channel.id)
        await interaction.response.send_message(
            f"✅ Clan invite panels will now be posted in {channel.mention}.",
            ephemeral=True
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setclanownerrole",
        description="Set the role that gets pinged for clan invite decisions."
    )
    @app_commands.describe(role="Owner role to ping on invite panels")
    async def setclanownerrole(self, interaction: discord.Interaction, role: discord.Role):
        store.set_clan_owner_role(role.id)
        await interaction.response.send_message(
            f"✅ Clan owners role set to {role.mention}. They will be tagged on invite panels.",
            ephemeral=True
        )

    # --- Logging & ticket monitoring ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setlogchannel",
        description="Set the channel for bot logs (joins, accepts, unauthorized use, VAC in tickets)."
    )
    @app_commands.describe(channel="The text channel for bot logs")
    async def setlogchannel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        store.set_log_channel(channel.id)
        await interaction.response.send_message(
            f"✅ Bot logs will now be sent to {channel.mention}.",
            ephemeral=True
        )

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="settryoutticketcategory",
        description="Set the category where your external ticket bot creates new text channels for tryout applicants."
    )
    @app_commands.describe(category="The category containing the ticket channels")
    async def settryoutticketcategory(self, interaction: discord.Interaction, category: discord.CategoryChannel):
        store.set_tryout_ticket_category(category.id)
        await interaction.response.send_message(
            f"✅ Tryout ticket channels in category {category.name} (`{category.id}`) will now be monitored for Steam links.",
            ephemeral=True
        )

    # --- /setdefaultrole (operates on the main server) ---

    @app_commands.guild_only()
    @owner_or_admin_check()
    @app_commands.command(
        name="setdefaultrole",
        description="Set the default role that users receive on the MAIN server when no role is chosen in /accept."
    )
    @app_commands.describe(role="The role to assign by default on the main server")
    async def setdefaultrole(self, interaction: discord.Interaction, role: discord.Role):
        main_guild = store.get_main_guild(self.bot)
        if main_guild is None:
            await interaction.response.send_message(
                "❌ Main server is not configured. Use `/main` in the main clan server first.",
                ephemeral=True,
            )
            return

        # Make sure the role actually belongs to the main guild
        if role.guild.id != main_guild.id:
            await interaction.response.send_message(
                "❌ The selected role must be from the **main** server.",
                ephemeral=True,
            )
            return

        if role >= main_guild.me.top_role:
            await interaction.response.send_message(
                f"❌ I cannot manage the role **{role.name}** (it is higher than my highest role in the main server).",
                ephemeral=True,
            )
            return

        store.set_default_role(main_guild.id, role.id)

        await interaction.response.send_message(
            f"✅ Default role on the **main server** set to **{role.name}**.\n"
            "Users accepted without choosing a role will receive this role when they join.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AdminCog(bot))
