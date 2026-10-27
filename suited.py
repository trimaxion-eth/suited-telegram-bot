"""Keyless client for the suited.club public API.

Only `/api/public/*` is used here (plus the two un-namespaced reads that have no
public mirror: `/api/token` and `/api/token/price`). The gateway serves those
with `cache-control: public, max-age=10`, so a 30 s poll loop is well inside
what it expects; this client caches for 20 s on top of that so a single command
that touches four endpoints still costs four requests, not eight.

Nothing here formats or decides — see `fmt.py` and `views.py`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://suited.club"
UA = "suited-telegram-bot/1.0"


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str, path: str):
        super().__init__(f"{path} -> {status}: {body[:200]}")
        self.status = status
        self.body = body
        self.path = path


class Suited:
    """Cached, retrying GET client for one base URL."""

    def __init__(self, base: str = BASE, ttl: float = 20.0, timeout: float = 15.0):
        self.base = base.rstrip("/")
        self.ttl = ttl
        self.timeout = timeout
        self._cache: dict[str, tuple[float, object]] = {}

    # -- transport ---------------------------------------------------------

    def get(self, path: str, params: dict | None = None, ttl: float | None = None):
        url = self.base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        now = time.monotonic()
        hit = self._cache.get(url)
        if hit and hit[0] > now:
            return hit[1]

        last: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(url, headers={"user-agent": UA})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                data = json.loads(body)
                self._cache[url] = (now + (self.ttl if ttl is None else ttl), data)
                return data
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")
                if error.code in (400, 401, 403, 404):
                    raise ApiError(error.code, detail, path) from error
                last = ApiError(error.code, detail, path)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                last = error
            time.sleep(0.4 * (attempt + 1))
        raise ApiError(0, str(last), path)

    # -- public read model -------------------------------------------------

    def status(self):
        return self.get("/api/public/status")

    def stats(self):
        return self.get("/api/public/stats")

    def tables(self):
        return self.get("/api/public/tables")

    def history(self):
        return self.get("/api/public/stats/history")

    def leaderboard(self, period: str = "week", limit: int = 10, stake: str | None = None):
        return self.get(
            "/api/public/leaderboard",
            {"period": period, "limit": limit, "stake": stake},
        )

    def player(self, player_id: str):
        return self.get("/api/public/player", {"id": player_id})

    def achievements_player(self, name: str):
        return self.get("/api/public/achievements/player", {"name": name})

    def tournaments(self):
        return self.get("/api/public/tournaments").get("tournaments", [])

    def tournament(self, event_id: str):
        return self.get(f"/api/tournaments/{event_id}")

    def results(self, limit: int = 5):
        return self.get(
            "/api/public/tournaments/results", {"limit": limit}
        ).get("results", [])

    def jackpot(self):
        return self.get("/api/public/jackpot", ttl=60)

    def jackpot_sources(self):
        return self.get("/api/public/jackpot/sources")

    def staking(self):
        return self.get("/api/public/staking", ttl=300)

    def token(self):
        return self.get("/api/token", ttl=60)

    def token_price(self, span: str = "7d"):
        return self.get("/api/token/price", {"range": span}, ttl=300)

    # -- derived views -----------------------------------------------------

    def live_tables(self):
        """Tables with at least one player, busiest first."""
        rows = [
            table
            for table in self.tables().get("tables", [])
            if int(table.get("seated") or 0) > 0
        ]
        return sorted(rows, key=lambda t: -int(t.get("seated") or 0))

    def next_events(self, within_ms: int = 7 * 24 * 3600_000, limit: int = 3):
        now = time.time() * 1000
        upcoming = [
            event
            for event in self.tournaments()
            if (event.get("startAt") or 0) > now
            and (event.get("startAt") or 0) <= now + within_ms
            and event.get("state") not in ("finished", "cancelled")
        ]
        return sorted(upcoming, key=lambda e: e.get("startAt") or 0)[:limit]

    def hour_profile(self):
        """Average hands per hour-of-day (UTC), plus the peak bucket.

        `.hours.hands` is one row per day, 24 columns; averaging the columns
        gives the room's actual rhythm, which is what `/when` is for.
        """
        hours = (self.history().get("hours") or {}).get("hands") or []
        rows = [row for row in hours if isinstance(row, list) and len(row) == 24]
        if not rows:
            return [], 0, 0.0
        means = [sum(row[i] for row in rows) / len(rows) for i in range(24)]
        peak = max(range(24), key=lambda i: means[i])
        return means, peak, means[peak]

    def find_player(self, needle: str):
        """Resolve a nickname to a public id.

        The player route wants a 12-hex id, but people know each other by
        nickname, so search the three leaderboard windows before giving up.
        """
        needle = needle.strip().lstrip("@")
        if len(needle) == 12 and all(c in "0123456789abcdef" for c in needle.lower()):
            return self.player(needle.lower())
        lowered = needle.lower()
        for period in ("week", "7d", "all"):
            try:
                rows = self.leaderboard(period=period, limit=100).get("rows", [])
            except ApiError:
                continue
            for row in rows:
                name = str(row.get("name") or "").lower()
                if name == lowered:
                    return self.player(row["id"])
        for period in ("week", "7d", "all"):
            try:
                rows = self.leaderboard(period=period, limit=100).get("rows", [])
            except ApiError:
                continue
            for row in rows:
                if lowered in str(row.get("name") or "").lower():
                    return self.player(row["id"])
        return None
