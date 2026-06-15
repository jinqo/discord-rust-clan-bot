"""
BattleMetrics API helper for Rust clan.
Provides:
- Player lookup by SteamID64
- Server info (pop, status, etc.)
- Basic player details (last seen, identifiers)
- Integration with /check for live server status + BM profile info

Requires a BattleMetrics API token (personal access token from https://www.battlemetrics.com/developers)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("accept-bot.battlemetrics")

BM_API_BASE = "https://api.battlemetrics.com"

# Track servers for which we already warned about player list access (to avoid log spam)
_warned_servers_for_players: set[str] = set()


class BattleMetricsAPIError(Exception):
    """Base error for BattleMetrics API issues."""
    pass


class BattleMetricsChecker:
    def __init__(self, api_token: str, session: aiohttp.ClientSession | None = None):
        self.api_token = api_token.strip()
        self._own_session = session is None
        self.session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={
                "Authorization": f"Bearer {self.api_token}",
                "User-Agent": "RustClanAcceptBot/1.0",
                "Accept": "application/json",
            },
        )

    async def close(self):
        if self._own_session and self.session and not self.session.closed:
            await self.session.close()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        """Generic BattleMetrics API GET call."""
        url = f"{BM_API_BASE}{path}"
        params = params or {}

        async with self.session.get(url, params=params) as resp:
            if resp.status == 401:
                raise BattleMetricsAPIError("Invalid BattleMetrics token or insufficient permissions")
            if resp.status == 403:
                raise BattleMetricsAPIError("BattleMetrics access denied (check token/org permissions)")
            if resp.status == 429:
                raise BattleMetricsAPIError("BattleMetrics rate limit hit. Try again later.")
            if resp.status != 200:
                text = await resp.text()
                raise BattleMetricsAPIError(f"BattleMetrics API error {resp.status}: {text[:300]}")

            return await resp.json()

    # ------------------ PLAYER LOOKUP ------------------

    async def get_player_by_steamid(self, steamid64: str) -> Optional[dict]:
        """
        Search for a player by SteamID64.
        Returns the first matching player dict or None.
        """
        try:
            data = await self._get("/players", {
                "filter[steamID]": steamid64,
                "page[size]": 1,
            })
            players = data.get("data", [])
            return players[0] if players else None
        except BattleMetricsAPIError as e:
            logger.warning(f"BM get_player_by_steamid failed for {steamid64}: {e}")
            return None
        except Exception as e:
            logger.warning(f"BM player lookup error for {steamid64}: {e}")
            return None

    async def get_player(self, player_id: str) -> Optional[dict]:
        """Get detailed player info by BattleMetrics player ID."""
        try:
            data = await self._get(f"/players/{player_id}")
            return data.get("data")
        except BattleMetricsAPIError as e:
            logger.warning(f"BM get_player failed for {player_id}: {e}")
            return None
        except Exception as e:
            logger.warning(f"BM player details error for {player_id}: {e}")
            return None

    # ------------------ SERVER INFO ------------------

    async def get_server(self, server_id: str) -> Optional[dict]:
        """Get server details by BattleMetrics server ID."""
        try:
            data = await self._get(f"/servers/{server_id}")
            return data.get("data")
        except BattleMetricsAPIError as e:
            logger.warning(f"BM get_server failed for {server_id}: {e}")
            return None
        except Exception as e:
            logger.warning(f"BM server lookup error for {server_id}: {e}")
            return None

    async def get_server_players(self, server_id: str, limit: int = 50) -> list[dict] | None:
        """Get current players on a server.

        Returns None if the player list could not be retrieved (common permission issue).
        Requires the BATTLEMETRICS_TOKEN to have appropriate access to server player data.
        """
        try:
            data = await self._get(f"/servers/{server_id}/players", {
                "page[size]": min(limit, 100),
            })
            return data.get("data", [])
        except BattleMetricsAPIError as e:
            if server_id not in _warned_servers_for_players:
                error_str = str(e).lower()
                if "405" in error_str or "403" in error_str or "500" in error_str:
                    logger.warning(
                        f"BM get_server_players failed for {server_id}: "
                        f"BattleMetrics returned an error (often 405/500) when trying to list players. "
                        f"This almost always means your BATTLEMETRICS_TOKEN does not have permission to read "
                        f"the list of players on this server (the /servers/{server_id}/players endpoint). "
                        f"Playtime tracking, join detection for linked Steam users, and the /leaderboard command "
                        f"will NOT work for this server. "
                        f"Normal /live panels and aggregate server info will continue to work. "
                        f"Possible fixes: use a token with more scopes from battlemetrics.com/developers, "
                        f"or the server owner may need to adjust privacy/settings."
                    )
                else:
                    logger.warning(f"BM get_server_players failed for {server_id}: {e}")
                _warned_servers_for_players.add(server_id)
            return None
        except Exception as e:
            logger.warning(f"BM server players error for {server_id}: {e}")
            return None

    # ------------------ HELPER ------------------

    async def get_player_status_on_server(self, steamid64: str, server_id: str) -> dict:
        """
        Check if a Steam player is currently on the given BM server.
        Returns dict with:
          - on_server: bool
          - last_seen: str or None
          - player: raw player data or None
        """
        player = await self.get_player_by_steamid(steamid64)
        if not player:
            return {"on_server": False, "last_seen": None, "player": None}

        player_id = player.get("id")
        if not player_id:
            return {"on_server": False, "last_seen": None, "player": player}

        # Get more details
        full_player = await self.get_player(player_id)

        # Check if currently online on this server (basic heuristic via identifiers or server list)
        # BattleMetrics player data often includes "lastSeen" and server relationships.
        # For accurate "currently on server", the /servers/{id}/players endpoint is better,
        # but we can approximate from player data.

        on_server = False
        last_seen = None

        if full_player:
            attrs = full_player.get("attributes", {})
            last_seen = attrs.get("lastSeen")

            # Try to detect if on this server (BM sometimes exposes relationships)
            relationships = full_player.get("relationships", {})
            # This is approximate; for 100% accuracy use get_server_players + match steamid
            # We'll do a lightweight check here.

        # Fallback / more accurate: query current players on server and match
        try:
            current_players = await self.get_server_players(server_id, limit=100)
            if current_players is not None:
                for p in current_players:
                    identifiers = p.get("attributes", {}).get("identifiers", [])
                    for ident in identifiers:
                        if ident.get("type") == "steamID" and ident.get("identifier") == steamid64:
                            on_server = True
                            last_seen = p.get("attributes", {}).get("lastSeen") or last_seen
                            break
                    if on_server:
                        break
        except Exception:
            pass

        return {
            "on_server": on_server,
            "last_seen": last_seen,
            "player": full_player or player,
        }