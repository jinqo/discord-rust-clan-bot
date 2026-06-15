# Discord Accept Bot

A feature-rich Discord bot designed for Rust clans that operate with two servers:

- **Main Server** — where active clan members play.
- **Tryout Server** — where new applicants are tested and processed.

The bot automates the entire recruitment pipeline: Steam verification, role-based acceptance, invite management, wipe coordination, and live server monitoring.

This project was built as part of preparation for the MEXT scholarship application. It demonstrates practical experience with asynchronous Python, Discord API development, third-party API integration, stateful bot architecture, and building production-grade tools used by a real gaming community.

## Key Features

### Recruitment & Verification
- `/check <steam>` — Comprehensive Steam + BattleMetrics profile analysis (bans, hours, friends network, comments scan for suspicious terms, risk scoring).
- Automatic Steam link detection in ticket channels (external ticket bot integration).
- `/ticketrefresh` — Re-run full verification on demand.
- `/linksteam` + `/clanpanel` — Steam linking system + public clan invite panel that posts clean `/clan invite <id>` messages to owners.

### Acceptance System
- `/accept @user` — Creates a **single-use, 24-hour** invite to the Main server.
- Ephemeral role picker (roles pulled live from the Main server).
- Automatic role assignment on join via `on_member_join`.
- Full invite-only mode (`/enableinviteonly`) with whitelist snapshot protection for existing members.
- Rich logging of who accepted whom and which invite was used.

### Wipe Coordination
- `/wipereact` — Manual posting of interactive wipe react messages.
- Recurring + one-off scheduling (`/schedulewipe`, `/setwipeschedule`).
- **Full timezone support** — specify time in the local region (e.g. 23:00 Europe/Amsterdam).
- Persistent interactive UI (buttons + modal) that survives bot restarts via `add_view`.
- Live-updating counts, decline reasons sent to staff channel.

### Live Server Monitoring
- `/live <bm link>` + `/setlivestatschannel` — Auto-updating BattleMetrics panel (refreshes every 60s).
- Integration of BM data into `/check` when servers are configured.

### Other
- Interactive recruiter `/guide` (paginated with buttons + DM export).
- Detailed logging of attempts to join without acceptance, VAC detections in tickets, etc.
- Robust data persistence with atomic writes and async locking.
- Owner bypass via `BOT_OWNER_ID` in `.env` (run privileged commands without server admin).

## Tech Stack

- **Python 3.11+** + `discord.py` (slash commands, UI components, persistent views, tasks)
- Async HTTP with `aiohttp` (Steam Web API + BattleMetrics API)
- `zoneinfo` for proper timezone handling in scheduling
- JSON-based persistence (with concurrency safety)
- Regex-based Steam profile comment scraping + keyword analysis

## Project Structure

```
discord-accept-bot/
├── bot.py              # Main bot logic, commands, events, views
├── steam.py            # Steam API wrapper (resolution, checks, comments)
├── battlemetrics.py    # BattleMetrics API wrapper
├── guide.py            # Interactive recruiter guide (paginated View)
├── store.py            # Persistent storage (InviteStore)
├── requirements.txt
├── start.ps1
└── data/
    └── pending.json    # All configuration and state
```

## Setup

### 1. Clone & Install
```bash
git clone https://github.com/yourusername/discord-accept-bot.git
cd discord-accept-bot
python -m venv .venv
source .venv/bin/activate   # or .\.venv\Scripts\Activate.ps1 on Windows
pip install -r requirements.txt
```

### 2. Environment Variables
Create a `.env` file:

```env
DISCORD_TOKEN=your_bot_token
STEAM_API_KEY=your_steam_key          # https://steamcommunity.com/dev/apikey
BATTLEMETRICS_TOKEN=your_bm_token     # https://www.battlemetrics.com/developers (optional but recommended)
BOT_OWNER_ID=your_discord_id          # Optional: lets you run admin commands without server perms
GUILD_IDS=123456789,987654321         # Optional: comma-separated guild IDs for fast command sync during development
```

### 3. Bot Permissions & Intents
- Invite the bot to **both** Main and Tryout servers.
- Required permissions on **Main server**: Manage Roles + Create Instant Invite.
- Enable **Server Members Intent** in the Discord Developer Portal.

### 4. Initial Configuration
In your **Main** server:
```
/main
```

In your **Tryout** server:
```
/tryout
```

## Important Commands

| Command                  | Description                                      | Permission     |
|--------------------------|--------------------------------------------------|----------------|
| `/accept @user`          | Accept player + role picker + 24h single-use invite | Admin / Owner |
| `/check <steam>`         | Full Steam + BattleMetrics analysis              | Anyone        |
| `/wipereact`             | Manually post interactive wipe react             | Admin         |
| `/schedulewipe`          | Add recurring wipe schedule with timezone        | Admin         |
| `/ticketrefresh`         | Re-verify the current Steam link in a ticket     | Ticket staff  |
| `/clanpanel`             | Post public clan invite panel                    | Admin         |
| `/live`                  | Start/update live BattleMetrics panel            | Admin         |
| `/status`                | Show current bot configuration (public)          | Anyone        |
| `/guide`                 | Interactive recruiter guide (with DM export)     | Anyone        |

Full list and detailed usage can be found by running commands in Discord or reading the source.

## Highlights & Technical Challenges Solved

- **Persistent UI after restart**: Wipe react buttons and modals continue working after bot restarts using `discord.py` persistent views + re-attachment in `on_ready`.
- **Timezone-aware scheduling**: Wipe times are defined in the local region (e.g. 23:00 Europe/Amsterdam) and correctly converted for posting and Discord timestamps.
- **Robust Steam verification**: Multi-page comment scraping, friend ban checking, risk scoring, and automatic detection in ticket channels.
- **Two-server architecture**: Clean separation between Tryout (recruitment) and Main (protected) servers with proper invite-only enforcement and logging.
- **Production considerations**: Atomic JSON persistence with async locking, graceful error handling on API calls, owner bypass system, and detailed staff logging.

## Future Ideas (for learning / portfolio)

- Full application system with modals + staff review tickets
- Player notes / internal database
- RCON integration
- More advanced BattleMetrics analytics
- Multi-language support (currently fully English)

## License

This project is made available for educational and portfolio purposes.

---

**Made with ❤️ for the Rust community.**

If you're reviewing this for a scholarship application (MEXT), thank you! This project represents real, ongoing development of a tool actually used by a gaming community. It showcases end-to-end system design, API integration, state management, and user experience considerations in a production Discord environment.

Feel free to reach out if you have any questions about the implementation.