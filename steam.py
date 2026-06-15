"""
Steam API helper for Rust clan checks.
Provides:
- Steam ID resolution (vanity, profiles, STEAM_ format, raw 64)
- Player summaries (account age, visibility, name, avatar)
- VAC / Game / Community bans
- Rust (252490) playtime (total + last 2 weeks)
- Basic "sus" / risk heuristics tailored for Rust recruiting

Caching & resilience:
- Simple in-memory TTL cache (dict + timestamp) for expensive calls:
  player summaries, bans, rust hours (180s), comments & friend lists (300s).
- Internal _get wrapped with 2-attempt retry + small sleep on transient errors.
- Public invalidate(steamid64) for /ticketrefresh and force re-checks.
- Slightly improved logging to distinguish private profiles vs API errors.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("accept-bot.steam")

RUST_APP_ID = 252490
STEAM_API_BASE = "https://api.steampowered.com"

# Regex helpers
STEAM64_RE = re.compile(r"7656\d{13}")
STEAM2_RE = re.compile(r"STEAM_(\d):(\d):(\d+)", re.IGNORECASE)
PROFILE_URL_RE = re.compile(r"steamcommunity\.com/profiles/(\d+)", re.IGNORECASE)
VANITY_URL_RE = re.compile(r"steamcommunity\.com/id/([a-zA-Z0-9_-]+)", re.IGNORECASE)


class SteamAPIError(Exception):
    """Base error for Steam API issues."""
    pass


class SteamChecker:
    def __init__(self, api_key: str, session: aiohttp.ClientSession | None = None):
        self.api_key = api_key.strip()
        self._own_session = session is None
        self.session = session or aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            headers={"User-Agent": "RustClanAcceptBot/1.0"},
        )
        self._cache: dict[str, dict[str, Any]] = {}  # in-memory TTL cache: key -> {"value": ..., "expires": ts}

    async def close(self):
        if self._own_session and self.session and not self.session.closed:
            await self.session.close()

    def _cache_key(self, prefix: str, steamid64: str) -> str:
        """Internal cache key helper."""
        return f"{prefix}:{steamid64}"

    def _get_cached(self, key: str, ttl: int) -> Optional[Any]:
        """Return cached value if still valid within TTL (seconds)."""
        entry = self._cache.get(key)
        if entry and time.time() < entry.get("expires", 0):
            return entry["value"]
        return None

    def _set_cache(self, key: str, value: Any, ttl: int) -> None:
        """Store value in cache with expiration timestamp."""
        self._cache[key] = {
            "value": value,
            "expires": time.time() + ttl,
        }

    def invalidate(self, steamid64: str) -> None:
        """
        Public method to clear cached data for a specific SteamID64.
        Intended to be called from /ticketrefresh endpoints or force-check flows
        to ensure fresh data on demand.
        """
        if not steamid64:
            return
        prefixes = ["summary", "bans", "rust", "friends", "comments"]
        removed = 0
        for p in prefixes:
            key = self._cache_key(p, steamid64)
            if key in self._cache:
                del self._cache[key]
                removed += 1
        # Also purge any lingering keys that might contain the id (defensive)
        for k in list(self._cache.keys()):
            if steamid64 in k:
                self._cache.pop(k, None)
                removed += 1
        if removed:
            logger.debug(f"Cache invalidated for {steamid64} ({removed} entries)")
        else:
            logger.debug(f"Invalidate called for {steamid64} (no cache entries)")

    async def _get(self, iface: str, method: str, version: str, params: dict[str, Any]) -> dict:
        """
        Generic Steam API GET call.
        Includes small retry (2 total attempts) with brief sleep on transient
        network / server errors. Does not retry hard errors like 403/429.
        """
        url = f"{STEAM_API_BASE}/{iface}/{method}/{version}/"
        params = {**params, "key": self.api_key, "format": "json"}

        last_err: Exception | None = None
        for attempt in range(2):  # attempt 0 and 1 (2 attempts total)
            try:
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 403:
                        raise SteamAPIError("Invalid Steam API key or access denied")
                    if resp.status == 429:
                        raise SteamAPIError("Steam API rate limit hit. Try again later.")
                    if 500 <= resp.status < 600:
                        # Transient server error - retry on first attempt
                        text = await resp.text()
                        last_err = SteamAPIError(f"Steam API error {resp.status}: {text[:200]}")
                        if attempt == 0:
                            await asyncio.sleep(0.5)
                            continue
                        raise last_err
                    if resp.status != 200:
                        text = await resp.text()
                        raise SteamAPIError(f"Steam API error {resp.status}: {text[:200]}")

                    data = await resp.json()
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                last_err = e
                if attempt == 0:
                    await asyncio.sleep(0.35)
                    continue
                raise
            except SteamAPIError:
                # Re-raise immediately for auth/rate/known errors; no retry
                raise

        # If we exhausted retries
        if last_err:
            if isinstance(last_err, SteamAPIError):
                raise last_err
            raise SteamAPIError(f"Steam API transient error after retries: {last_err}")
        raise SteamAPIError("Steam API unknown error after retries")

    # ------------------ ID RESOLUTION ------------------

    async def resolve_steam_id(self, user_input: str) -> Optional[str]:
        """
        Turn almost any Steam identifier into a SteamID64 string.
        Supports:
        - 7656119xxxxxxxxxx
        - https://steamcommunity.com/profiles/7656...
        - https://steamcommunity.com/id/vanityname
        - STEAM_0:1:12345678
        - raw vanity name (last resort)
        Prefers explicit profile/vanity URLs (hardened with length/validation).
        """
        s = (user_input or "").strip()

        # Prefer explicit links (more reliable when message mixes old IDs + new link)
        # Full profile URL
        m = PROFILE_URL_RE.search(s)
        if m:
            sid = m.group(1)
            if len(sid) == 17 and sid.startswith("7656"):
                return sid

        # Vanity URL → resolve
        m = VANITY_URL_RE.search(s)
        if m:
            vanity = m.group(1)
            return await self._resolve_vanity(vanity)

        # Direct SteamID64 (raw) - require full 17 digit match
        m = STEAM64_RE.search(s)
        if m:
            sid = m.group(0)
            if len(sid) == 17:
                return sid

        # STEAM_0:x:xxxxxxxx format (hardened)
        m = STEAM2_RE.search(s)
        if m:
            try:
                y = int(m.group(2))
                z = int(m.group(3))
                if 0 <= y <= 1 and z > 0:
                    steamid64 = z * 2 + y + 76561197960265728
                    if 17 == len(str(steamid64)) and str(steamid64).startswith("7656"):
                        return str(steamid64)
            except (ValueError, OverflowError):
                pass

        # Last resort: treat whole input as vanity (no /id/)
        if re.match(r"^[a-zA-Z0-9_-]{2,32}$", s):
            return await self._resolve_vanity(s)

        return None

    async def _resolve_vanity(self, vanity: str) -> Optional[str]:
        try:
            data = await self._get(
                "ISteamUser", "ResolveVanityURL", "v0001",
                {"vanityurl": vanity}
            )
            response = data.get("response", {})
            if response.get("success") == 1:
                return str(response.get("steamid"))
            logger.warning(f"Could not resolve vanity '{vanity}': {response}")
            return None
        except Exception as e:
            logger.warning(f"Vanity resolve failed for '{vanity}': {e}")
            return None

    # ------------------ DATA FETCHING ------------------

    async def get_player_summary(self, steamid64: str) -> Optional[dict]:
        """Returns the player summary dict or None. Uses 180s TTL cache."""
        key = self._cache_key("summary", steamid64)
        cached = self._get_cached(key, 180)
        if cached is not None:
            return cached
        try:
            data = await self._get(
                "ISteamUser", "GetPlayerSummaries", "v0002",
                {"steamids": steamid64}
            )
            players = data.get("response", {}).get("players", [])
            result = players[0] if players else None
            self._set_cache(key, result, 180)
            return result
        except Exception as e:
            logger.warning(f"GetPlayerSummaries failed: {e}")
            return None

    async def get_player_bans(self, steamid64: str) -> Optional[dict]:
        """Returns the bans dict (VACBanned, NumberOfVACBans, etc.). Uses 180s TTL cache."""
        key = self._cache_key("bans", steamid64)
        cached = self._get_cached(key, 180)
        if cached is not None:
            return cached
        try:
            data = await self._get(
                "ISteamUser", "GetPlayerBans", "v1",
                {"steamids": steamid64}
            )
            players = data.get("players", [])
            result = players[0] if players else None
            self._set_cache(key, result, 180)
            return result
        except Exception as e:
            logger.warning(f"GetPlayerBans failed: {e}")
            return None

    async def get_rust_hours(self, steamid64: str) -> dict:
        """
        Returns dict with:
          - has_data: bool (False if profile private or no Rust)
          - hours: float (total)
          - hours_2weeks: float
        Uses 180s TTL cache.
        """
        key = self._cache_key("rust", steamid64)
        cached = self._get_cached(key, 180)
        if cached is not None:
            return cached
        try:
            data = await self._get(
                "IPlayerService", "GetOwnedGames", "v0001",
                {
                    "steamid": steamid64,
                    "include_appinfo": "true",
                    "include_played_free_games": "true",
                },
            )
            games = data.get("response", {}).get("games", []) or []
            for game in games:
                if game.get("appid") == RUST_APP_ID:
                    mins = game.get("playtime_forever", 0) or 0
                    mins_2w = game.get("playtime_2weeks", 0) or 0
                    result = {
                        "has_data": True,
                        "hours": round(mins / 60, 1),
                        "hours_2weeks": round(mins_2w / 60, 1),
                    }
                    self._set_cache(key, result, 180)
                    return result
            # No Rust in library (or never played)
            result = {"has_data": False, "hours": 0.0, "hours_2weeks": 0.0}
            self._set_cache(key, result, 180)
            return result
        except Exception as e:
            logger.warning(f"GetOwnedGames failed: {e}")
            return {"has_data": False, "hours": 0.0, "hours_2weeks": 0.0}

    # ------------------ FRIENDS & COMMENTS (extra intel) ------------------

    async def get_friend_list(self, steamid64: str) -> dict:
        """Returns friends list info. Only works if friends list is public. Uses 300s TTL cache."""
        key = self._cache_key("friends", steamid64)
        cached = self._get_cached(key, 300)
        if cached is not None:
            return cached
        try:
            data = await self._get(
                "ISteamUser", "GetFriendList", "v1",
                {"steamid": steamid64, "relationship": "friend"}
            )
            friends = data.get("friendslist", {}).get("friends", []) or []
            steamids = [str(f["steamid"]) for f in friends]
            result = {"success": True, "friends": steamids, "count": len(steamids)}
            self._set_cache(key, result, 300)
            return result
        except Exception as e:
            logger.warning(f"GetFriendList failed (private or error): {e}")
            return {"success": False, "friends": [], "count": 0}

    async def get_friends_bans_summary(self, steamid64: str, max_check: Optional[int] = None) -> dict:
        """Check bans among the player's friends (if visible).
        Set max_check=None (default) to check *all* friends ("check alles").
        Chunks ban API calls (Steam recommends <=100 IDs per request).
        """
        fl = await self.get_friend_list(steamid64)
        if not fl["success"] or not fl["friends"]:
            return {
                "success": False,
                "total_friends": fl["count"],
                "checked": 0,
                "vac_banned": 0,
                "game_banned": 0
            }

        friend_ids = fl["friends"]
        if isinstance(max_check, int) and max_check > 0:
            friend_ids = friend_ids[:max_check]

        vac = 0
        game = 0
        checked = 0
        try:
            CHUNK_SIZE = 100
            for i in range(0, len(friend_ids), CHUNK_SIZE):
                chunk = friend_ids[i : i + CHUNK_SIZE]
                ids_str = ",".join(chunk)
                data = await self._get(
                    "ISteamUser", "GetPlayerBans", "v1",
                    {"steamids": ids_str}
                )
                players = data.get("players", []) or []
                checked += len(players)
                vac += sum(1 for p in players if p.get("VACBanned", False))
                game += sum(1 for p in players if p.get("NumberOfGameBans", 0) > 0)

            return {
                "success": True,
                "total_friends": fl["count"],
                "checked": checked,
                "vac_banned": vac,
                "game_banned": game
            }
        except Exception as e:
            logger.warning(f"Friends bans check failed: {e}")
            return {
                "success": False,
                "total_friends": fl["count"],
                "checked": 0,
                "vac_banned": 0,
                "game_banned": 0
            }

    async def get_comments_summary(self, steamid64: str, max_pages: int = 10) -> dict:
        """
        Scrape public profile comments across multiple pages to get (nearly) all comments.
        Steam's default view only shows ~6 comments; we now paginate (?p=1, ?p=2, ...) to check more/all.
        Scans for hacker/cheater/cheat/hack/ban/scam etc. keywords.
        Uses 300s TTL cache. Logging distinguishes private profile cases from other errors.
        """
        key = self._cache_key("comments", steamid64)
        cached = self._get_cached(key, 300)
        if cached is not None:
            return cached

        base_url = f"https://steamcommunity.com/profiles/{steamid64}/comments/"
        bad_keywords = ["cheat", "hack", "vac", "ban", "scam", "hacker", "cheater", "aimbot", "wall", "grief", "toxic", "report", "trash", "noob", "ruin", "script", "esp", "spin", "rage", "macro"]

        all_raw: list[str] = []

        try:
            total_on_profile = None
            first_status_bad = False

            for page_num in range(1, max_pages + 1):
                url = f"{base_url}?p={page_num}"
                async with self.session.get(url, timeout=aiohttp.ClientTimeout(total=12)) as resp:
                    if resp.status != 200:
                        if page_num == 1:
                            first_status_bad = True
                            if resp.status in (403, 401):
                                logger.info(f"Comments scrape: access denied/private profile or comments restricted for {steamid64} (status {resp.status})")
                            else:
                                logger.warning(f"Comments scrape: first page non-200 ({resp.status}) for {steamid64}")
                        break

                    html = await resp.text(errors="ignore")

                    # Detect private profile hints for better logging (non-breaking)
                    if page_num == 1 and ("private" in html.lower() or "this profile is" in html.lower()):
                        logger.info(f"Comments scrape: profile appears private (comments may be limited) for {steamid64}")

                    # Try to parse total comment count from the first page
                    if total_on_profile is None:
                        total_match = re.search(r'(?:of|Showing)\s*[\d,]+\s*-\s*[\d,]+\s*of\s*([\d,]+)', html, re.IGNORECASE)
                        if total_match:
                            total_on_profile = int(total_match.group(1).replace(",", ""))
                        else:
                            # fallback rough count
                            count_match = re.search(r'(\d+)\s+comments?', html, re.IGNORECASE)
                            if count_match:
                                total_on_profile = int(count_match.group(1).replace(",", ""))

                    # Extract comment texts
                    pattern = r'(?:commentthread_comment_text|comment_text)[^>]*>(.*?)</div>'
                    raw_matches = re.findall(pattern, html, re.IGNORECASE | re.DOTALL)

                    page_comments = 0
                    for raw in raw_matches:
                        text = re.sub(r'<[^>]+>', ' ', raw)
                        text = re.sub(r'\s+', ' ', text).strip()
                        if len(text) > 8:
                            all_raw.append(text[:300])
                            page_comments += 1

                    if page_comments == 0:
                        break  # no more comments on this page

            # Deduplicate
            seen = set()
            unique_comments: list[str] = []
            for c in all_raw:
                if c not in seen:
                    seen.add(c)
                    unique_comments.append(c)

            # Scan for suspicious keywords
            suspicious = []
            for text in unique_comments:
                lower = text.lower()
                if any(kw in lower for kw in bad_keywords):
                    suspicious.append(text)

            result = {
                "success": True,
                "comment_count": len(unique_comments),
                "total_on_profile": total_on_profile,
                "suspicious": len(suspicious),
                "examples": suspicious[:3]
            }
            # Even 0-count results (no comments or hidden) are cached to avoid repeat expensive scrapes
            self._set_cache(key, result, 300)
            return result

        except Exception as e:
            err_msg = str(e)
            if "private" in err_msg.lower() or "403" in err_msg or "401" in err_msg:
                logger.info(f"Comments scrape: likely private profile or restricted access for {steamid64}: {err_msg}")
            else:
                logger.warning(f"Comments scrape failed (API/network error): {err_msg}")
            fail_result = {"success": False, "comment_count": 0, "suspicious": 0, "examples": []}
            # Cache failure briefly? For simplicity do not cache transient fails here (will retry on next check)
            return fail_result

    # ------------------ MAIN CHECK ------------------

    async def check(self, user_input: str) -> dict:
        """
        High level check. Returns a rich dict ready for Discord embed.
        """
        result: dict[str, Any] = {
            "input": user_input,
            "steamid64": None,
            "resolved": False,
            "profile": None,
            "bans": None,
            "rust": {"has_data": False, "hours": 0.0, "hours_2weeks": 0.0},
            "flags": [],
            "risk_level": "unknown",
            "risk_color": 0x808080,
            "error": None,
        }

        steamid = await self.resolve_steam_id(user_input)
        if not steamid:
            result["error"] = "Could not recognize Steam ID. Use a profile link, SteamID64, or vanity name."
            return result

        result["steamid64"] = steamid
        result["resolved"] = True

        # Fetch in parallel where possible (API calls)
        summary, bans, rust, friend_list, friends_bans, comments = await asyncio.gather(
            self.get_player_summary(steamid),
            self.get_player_bans(steamid),
            self.get_rust_hours(steamid),
            self.get_friend_list(steamid),
            self.get_friends_bans_summary(steamid),
            self.get_comments_summary(steamid),
            return_exceptions=True,
        )

        if isinstance(summary, Exception):
            summary = None
        if isinstance(bans, Exception):
            bans = None
        if isinstance(rust, Exception):
            rust = {"has_data": False, "hours": 0.0, "hours_2weeks": 0.0}
        if isinstance(friend_list, Exception):
            friend_list = {"success": False, "friends": [], "count": 0}
        if isinstance(friends_bans, Exception):
            friends_bans = {"success": False, "total_friends": 0, "checked": 0, "vac_banned": 0, "game_banned": 0}
        if isinstance(comments, Exception):
            comments = {"success": False, "comment_count": 0, "suspicious": 0, "examples": []}

        result["profile"] = summary
        result["bans"] = bans
        result["rust"] = rust
        result["friends"] = friend_list
        result["friends_bans"] = friends_bans
        result["comments"] = comments

        # Build flags + risk assessment
        self._analyze(result)

        return result

    def _analyze(self, result: dict):
        profile = result.get("profile") or {}
        bans = result.get("bans") or {}
        rust = result.get("rust") or {}
        friends = result.get("friends") or {}
        friends_bans = result.get("friends_bans") or {}
        comments = result.get("comments") or {}

        flags = []
        risk_score = 0

        # --- Profile visibility & basic info ---
        visibility = profile.get("communityvisibilitystate", 0)  # 1=private, 3=public
        is_private = visibility != 3
        if is_private:
            flags.append("🔒 Profile is private (hours and games not visible)")
            risk_score += 25

        # Account age
        timecreated = profile.get("timecreated")
        account_age_days = None
        if timecreated:
            created_dt = datetime.fromtimestamp(timecreated, tz=timezone.utc)
            account_age_days = (datetime.now(timezone.utc) - created_dt).days
            result["account_age_days"] = account_age_days
            result["account_created"] = created_dt.strftime("%Y-%m-%d")

            if account_age_days < 30:
                flags.append(f"🆕 New Steam account ({account_age_days} days old)")
                risk_score += 40
            elif account_age_days < 90:
                flags.append(f"🆕 Relatively new account ({account_age_days} days)")
                risk_score += 15

        # --- Bans ---
        has_vac = bans.get("VACBanned", False)
        vac_count = bans.get("NumberOfVACBans", 0)
        game_bans = bans.get("NumberOfGameBans", 0)
        days_since_last = bans.get("DaysSinceLastBan", 0)
        community_banned = bans.get("CommunityBanned", False)
        economy_ban = bans.get("EconomyBan", "none")

        if has_vac or vac_count > 0:
            flags.append(f"⛔ VAC ban(s): {vac_count}")
            risk_score += 60
            if days_since_last and days_since_last < 365:
                flags.append(f"⚠️ Last VAC ban was {days_since_last} days ago")
                risk_score += 30
            elif days_since_last and days_since_last < 1825:  # 5 years
                risk_score += 10

        if game_bans and game_bans > 0:
            flags.append(f"⛔ Game ban(s): {game_bans}")
            risk_score += 50

        if community_banned:
            flags.append("⛔ Community banned")
            risk_score += 35

        if economy_ban and economy_ban != "none":
            flags.append(f"⛔ Trade/Economy ban: {economy_ban}")
            risk_score += 25

        # --- Rust specific ---
        rust_hours = rust.get("hours", 0.0) or 0.0
        rust_2w = rust.get("hours_2weeks", 0.0) or 0.0
        has_rust_data = rust.get("has_data", False)

        result["rust_hours"] = rust_hours
        result["rust_hours_2w"] = rust_2w

        if not has_rust_data and not is_private:
            flags.append("🎮 Never played Rust (or game details hidden)")
            risk_score += 20

        if has_rust_data:
            if rust_hours < 20:
                flags.append(f"⏱️ Very few Rust hours ({rust_hours}h)")
                risk_score += 30
            elif rust_hours < 80:
                flags.append(f"⏱️ Low Rust experience ({rust_hours}h)")
                risk_score += 12
            elif rust_hours > 2000:
                flags.append(f"🏆 Very experienced Rust player ({int(rust_hours)}h)")

            if rust_2w > 30:
                flags.append(f"🔥 Very active recently ({rust_2w}h in last 2 weeks)")

        # --- Friends network (new) ---
        if friends_bans.get("success"):
            f_vac = friends_bans.get("vac_banned", 0)
            f_game = friends_bans.get("game_banned", 0)
            f_total = friends_bans.get("total_friends", 0)
            f_checked = friends_bans.get("checked", 0)

            if f_vac > 0:
                flags.append(f"👥 {f_vac} of {f_checked} checked friends have VAC bans")
                risk_score += min(30, f_vac * 6)
            if f_game > 0:
                flags.append(f"👥 {f_game} of {f_checked} checked friends have game bans")
                risk_score += min(25, f_game * 8)

            if f_total > 0 and f_checked > 0 and (f_vac + f_game) / f_checked > 0.3:
                flags.append("👥 High percentage of banned friends (suspicious network)")
                risk_score += 20

        elif friends.get("count", 0) > 0:
            # Friends visible but we couldn't check bans
            pass
        else:
            flags.append("🔒 Friends list is private")
            risk_score += 10

        # --- Profile comments ---
        if comments.get("success"):
            c_count = comments.get("comment_count", 0)
            c_sus = comments.get("suspicious", 0)
            total = comments.get("total_on_profile")
            if c_sus > 0:
                flags.append(f"💬 {c_sus} suspicious comment(s) found on profile")
                risk_score += 15 + (c_sus * 5)
            elif total and total > 100:
                flags.append(f"💬 Very active profile ({total}+ comments)")
        else:
            flags.append("🔒 Comments not visible")

        # --- Final risk level ---
        if risk_score >= 80:
            level = "HIGH RISK"
            color = 0xE74C3C  # red
        elif risk_score >= 45:
            level = "CAUTION"
            color = 0xF1C40F  # yellow
        elif risk_score >= 20:
            level = "LOW-MEDIUM"
            color = 0xF39C12
        else:
            level = "CLEAN"
            color = 0x2ECC71  # green

        result["flags"] = flags
        result["risk_level"] = level
        result["risk_score"] = risk_score
        result["risk_color"] = color

        # Persona + links
        result["persona"] = profile.get("personaname", "Unknown")
        result["avatar"] = profile.get("avatarfull") or profile.get("avatarmedium")
        result["profile_url"] = profile.get("profileurl", f"https://steamcommunity.com/profiles/{result['steamid64']}")
