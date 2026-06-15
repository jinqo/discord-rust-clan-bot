import discord
from discord import app_commands, ui
from datetime import datetime, timezone


class GuideView(ui.View):
    """Interactive paginator for the recruiter guide.
    Supports Previous/Next, direct section select, DM copy, and close.
    """

    def __init__(self, embeds: list[discord.Embed]):
        super().__init__(timeout=300)  # 5 minutes idle timeout
        self.embeds = embeds
        self.index = 0

        # Create the section jump select and add it (we control its row)
        self._add_part_select()

        # Initial button states (prev/next decorators will be present after super)
        self.update_nav_buttons()

    def _add_part_select(self):
        options = []
        for i, emb in enumerate(self.embeds):
            label = f"Part {i + 1}"
            # Derive a short description from the embed title
            raw_title = emb.title or f"Section {i + 1}"
            desc = raw_title.replace("📋 Recruiter / Tryout Guide - ", "").strip()
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(i),
                    description=desc[:100] if desc else f"Section {i + 1}"
                )
            )

        select = ui.Select(
            placeholder="Jump directly to a section…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="guide_part_select",
            row=1  # Place on its own row
        )
        select.callback = self._on_part_select
        self.add_item(select)
        self.part_select = select

    def _get_current_embed(self) -> discord.Embed:
        """Return a copy of the current embed with page footer."""
        emb = self.embeds[self.index].copy()
        emb.set_footer(
            text=f"📖 Recruiter Guide • Page {self.index + 1} of {len(self.embeds)} • Use controls below"
        )
        return emb

    def update_nav_buttons(self):
        """Enable/disable prev/next based on current position."""
        for child in self.children:
            if isinstance(child, ui.Button):
                if child.custom_id == "guide_prev":
                    child.disabled = (self.index == 0)
                elif child.custom_id == "guide_next":
                    child.disabled = (self.index == len(self.embeds) - 1)

    async def on_timeout(self) -> None:
        # Disable everything when the view times out
        for child in self.children:
            child.disabled = True
        # We can't edit the message here without a reference, but interactions will be disabled.

    # --- Navigation buttons (row 0) ---
    @ui.button(emoji="◀️", style=discord.ButtonStyle.secondary, custom_id="guide_prev", row=0)
    async def prev_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.index > 0:
            self.index -= 1
        self.update_nav_buttons()
        embed = self._get_current_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(emoji="▶️", style=discord.ButtonStyle.secondary, custom_id="guide_next", row=0)
    async def next_button(self, interaction: discord.Interaction, button: ui.Button):
        if self.index < len(self.embeds) - 1:
            self.index += 1
        self.update_nav_buttons()
        embed = self._get_current_embed()
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_part_select(self, interaction: discord.Interaction):
        """Callback for the section selector."""
        try:
            if interaction.data and "values" in interaction.data:
                self.index = int(interaction.data["values"][0])
            self.update_nav_buttons()
            embed = self._get_current_embed()
            await interaction.response.edit_message(embed=embed, view=self)
        except Exception:
            await interaction.response.send_message("Failed to change section.", ephemeral=True)

    # --- Action buttons (row 2) ---
    @ui.button(emoji="📩", label="Send to DM", style=discord.ButtonStyle.primary, custom_id="guide_dm", row=2)
    async def dm_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            await interaction.user.send(
                content="📋 **Full Recruiter / Tryout Guide** — keep this for reference",
                embeds=self.embeds
            )
            await interaction.response.send_message(
                "✅ Full guide sent to your DMs!", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ Could not DM you. Enable DMs from server members and try again.",
                ephemeral=True
            )
        except Exception as exc:
            await interaction.response.send_message(
                f"❌ Failed to send DM: {exc}", ephemeral=True
            )

    @ui.button(emoji="❌", label="Close", style=discord.ButtonStyle.danger, custom_id="guide_close", row=2)
    async def close_button(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(view=None)
        self.stop()


@app_commands.command(
    name="guide",
    description="Recruiter guide: how to review applicants, test on UKN/aim, spot cheats, and all useful commands."
)
async def guide(interaction: discord.Interaction):
    embeds = build_guide_embeds()
    if not embeds:
        await interaction.response.send_message("Guide content is empty.", ephemeral=True)
        return

    view = GuideView(embeds)
    first_embed = view._get_current_embed()

    await interaction.response.send_message(
        content=(
            "📖 **Recruiter / Tryout Guide** — Use the arrow buttons or dropdown to navigate the sections. "
            "Click **📩 Send to DM** to get a personal copy."
        ),
        embed=first_embed,
        view=view
    )

def build_guide_embeds():
    embeds = []
    
    # Embed 1: Intro + Ticket + Reviewing
    e1 = discord.Embed(
        title="📋 Recruiter / Tryout Guide - Part 1",
        description="**For all recruiters in the Tryout server.**\nFollow this process when reviewing new applicants.\n\nEverything is in English for clarity.",
        color=discord.Color.blurple(),
        timestamp=datetime.now(timezone.utc)
    )
    e1.add_field(
        name="1. New Ticket Process",
        value="""• Ticket channels created in the configured category.
• Applicant posts:
```
steam name -
steam link -
rust hours -
role in clan -
```
• Bot auto-detects Steam link and posts full /check (no BM).""",
        inline=False
    )
    e1.add_field(
        name="2. Reviewing the Check (/check output)",
        value="""Key things:
• Risk (CLEAN / LOW-MED preferred)
• Real Rust hours visible (not 0/hidden)
• Bans: VAC/game ban = instant deny
• New account + high hours = sus
• Many banned friends = red flag
• Cheat mentions in comments = bad

If private/hidden: tell them to unhide Profile+Game details, then `/ticketrefresh`.""",
        inline=False
    )
    embeds.append(e1)

    # Embed 2: Role Manuals
    e2 = discord.Embed(
        title="📋 Recruiter / Tryout Guide - Part 2: Role Manuals",
        color=discord.Color.blurple()
    )
    e2.add_field(
        name="3. Role Manuals & Requirements (based on 'role in clan')",
        value="""**PvP / Monument Runners** (same reqs):
• Strong combat, movement, game sense.
• UKN + in-game fights must be solid.
• Consistent PvP at monuments/raids.

**Builders**:
• **Must show builds** (screenshots/video/live demo required).
• Quality, smart defense, efficiency, resource use.
• Explain design choices.

**Electricians**:
• Electrical focus (doors, traps, power, turrets).
• Show complex wiring (screenshots or demo).
• Usually pairs with building.""",
        inline=False
    )
    embeds.append(e2)

    # Embed 3: Testing & Cheats
    e3 = discord.Embed(
        title="📋 Recruiter / Tryout Guide - Part 3: Testing & Cheats",
        color=discord.Color.blurple()
    )
    e3.add_field(
        name="4. Testing Process (UKN / Combat / Build Demo)",
        value="""**PvP / Monument:**
1. UKN IP/invite.
2. Warm-up + drills (track/flick/move+aim).
3. Watch live or request clips.
4. Multiple opponents.

**Builders:** Require build proof (screenshots/video or live on build server). Ask for specific build on demand.

**Electricians:** Builders + complex wiring/electrical demo + explanation of systems.""",
        inline=False
    )
    e3.add_field(
        name="5. Spotting Aimbots & Flat Rage (UKN)",
        value="""**Aimbot signs:**
• Pixel-perfect tracking (smoke/walls/jump, no wobble)
• 100% head flicks (incl. 180s), god sprays while strafing
• Pre-aims every peek perfectly
• Never whiffs or has off games

**Flat Rage signs:**
• Fly/speed/spinbot
• Consistent wall/floor kills
• Hold W + mouse1 wins every fight instantly
• Unreal reactions""",
        inline=False
    )
    embeds.append(e3)

    # Embed 4: Good vs Sus
    e4 = discord.Embed(
        title="📋 Recruiter / Tryout Guide - Part 4: Good vs Sus",
        color=discord.Color.blurple()
    )
    e4.add_field(
        name="6. Good vs Sus Examples",
        value="""**UKN Performance**
Good: 70-85% tracking, realistic sprays, small errors on fast moves.
Sus: 95-100% perfect even jumping/strafing, zero recoil long range.

**Movement + Aim**
Good: Aim suffers during complex movement (human focus limit).
Sus: Insane movement + laser aim with zero delay.

**Consistency**
Good: Good + bad rounds, tires, whiffs.
Sus: Robotic same god-level for hours.

**Reaction**
Good: Cooperative, shows clips, wants feedback.
Sus: Defensive, refuses drills, toxic/excuses.

**Steam (/check)**
Good: Hours match claims, normal history, open profile.
Sus: "9k hours" but check shows hidden/0, new acct + banned friends.""",
        inline=False
    )
    embeds.append(e4)

    # Embed 5: Commands + Red Flags
    e5 = discord.Embed(
        title="📋 Recruiter / Tryout Guide - Part 5: Commands & Red Flags",
        color=discord.Color.blurple()
    )
    e5.add_field(
        name="7. Important Commands for Recruiters",
        value="""`/check <steam link or ID>` — Full Steam check (bans, hours, risk, friends)
`/ticketrefresh` — Re-run full /check in current ticket (for hidden profiles)
`/accept @user` — Accept to main (pick role). Tryout server only.
`/pending` — List players waiting for main server accept
`/settryoutticketcategory`, `/setlogchannel` — Admin config

Run commands inside ticket channels.""",
        inline=False
    )
    e5.add_field(
        name="8. Red Flags & Sus People",
        value="""**Steam /check red flags:**
• Hidden/private hours after request to unhide
• Claims 5-9k but /check shows near 0 or hidden
• Brand new account + insane hour claims
• Any VAC/recent game ban (instant deny)
• Many VAC/game-banned friends
• Young account + "pro" claims

**UKN / in-game red flags:**
• Perfect tracking/flicks/no recoil (see spotting section)
• Rage signs: fly/spin/wall every peek
• Refuses UKN or specific drills, gets toxic/defensive
• Exact same robotic god-mode for hours""",
        inline=False
    )
    embeds.append(e5)

    # Embed 6: Observe + Decision
    e6 = discord.Embed(
        title="📋 Recruiter / Tryout Guide - Part 6: Observation & Decision",
        color=discord.Color.blurple()
    )
    e6.add_field(
        name="9. How to Observe + Good Questions to Ask",
        value="""**Watch properly:** Observe movement + aim together, not just crosshair.
Good players show human error (overflick, whiff, adjust).
Cheaters look robotic: perfect every time.

**Key questions:**
• Actual Rust playtime? (cross-check /check)
• Usual servers + previous clan experience (proof)?
• Best recent 1v3/1v4 clip?
• Why this clan?

Angry / evasive / no clips = red flag.""",
        inline=False
    )
    e6.add_field(
        name="10. Final Decision",
        value="""**Accept if:** Clean /check + role-specific demo (UKN PvP / builds shown for Builders / electrical for Electricians) + honest + cooperative.

**Deny / careful:** Sus Steam, refuses role tests (build/electrical/UKN), toxic, evasive, or robotic UKN.

When unsure: ask other staff first.

`/accept @user` only in Tryout server. Bot auto-logs VACs, bad accepts, unauthorized use, and failed joins.""",
        inline=False
    )
    embeds.append(e6)

    return embeds
