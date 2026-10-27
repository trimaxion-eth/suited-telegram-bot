"""The push side: a 30 s tick that turns API deltas into a handful of DMs.

Design rules learned from the room's actual shape (one live table, four players
seated, ten on the weekly board):

* **Sparse and earned.** A ping about a table only exists while the table is
  live, so there is nothing to say most of the day — that is fine. The bot's
  standing value lives in the commands and the daily recap.
* **Per-stake, not global.** Someone playing $1/$2 never wants to hear about
  1¢/2¢. Alerts carry a stake and every subscriber filters on it.
* **Idempotent.** Each alert key is written to `sent` (per user). A restart at
  22:05 does not re-announce the 22:00 event.
* **Primed, never replayed.** The first tick after a fresh deploy marks what is
  already true as sent without sending it.

Keys are coarse on purpose (`table:nl200-…:4:48213` buckets by seated count and
a two-hour window) so a table that fills, empties and refills does not produce
three messages for the same evening.
"""

from __future__ import annotations

import time

import fmt

DAY = 86400


class Alerter:
    def __init__(self, api, store, notify, log=print):
        self.api = api
        self.store = store
        self.notify = notify
        self.log = log

    # -- public ------------------------------------------------------------

    def tick(self) -> int:
        now = int(time.time())
        primed = self.store.get("primed") == "1"
        try:
            alerts = self._collect(now)
        except Exception as error:  # a flaky gateway must not kill the loop
            self.log(f"alert collect failed: {error}")
            return 0

        if not primed:
            for alert in alerts:
                self.store.mark_sent(self._global_key(alert))
            self.store.set("primed", "1")
            self.log(f"primed {len(alerts)} existing alert(s); none sent")
            return 0

        sent = 0
        for alert in alerts:
            if self.store.was_sent(self._global_key(alert)):
                continue
            self.store.mark_sent(self._global_key(alert))
            sent += self._fan_out(alert, now)
        return sent

    # -- collection --------------------------------------------------------

    def _collect(self, now: int) -> list[dict]:
        alerts: list[dict] = []
        alerts += self._tables(now)
        alerts += self._events(now)
        alerts += self._results()
        alerts += self._biggest_pot()
        alerts += self._jackpot_draw()
        alerts += self._milestones()
        return alerts

    def _tables(self, now: int) -> list[dict]:
        out = []
        for table in self.api.live_tables():
            seated = int(table.get("seated") or 0)
            seats = int(table.get("maxSeats") or 0)
            key = table.get("id") or "?"
            stake = str(key).split("-")[0]
            window = now // 7200  # two-hour bucket
            event = {
                "key": f"table:{key}:{seated}:{window}",
                "kind": "table",
                "stake": stake,
                "seated": seated,
                "text": self._table_text(table, seated, seats),
            }
            out.append(event)
        return out

    @staticmethod
    def _table_text(table: dict, seated: int, seats: int) -> str:
        label = table.get("name") or "table"
        kind = "heads-up" if "HU" in str(label) else "6-max"
        stake = str(label).split(" · ")[0]
        open_seats = seats - seated
        pot = table.get("avgPot")
        line = (
            f"🪑 <b>{stake}</b> {kind} is running — "
            f"{seated}/{seats} seated, {open_seats} open"
        )
        if pot:
            line += f"\nAverage pot {fmt.usd(pot)}"
        return line + "\nhttps://suited.club"

    def _events(self, now: int) -> list[dict]:
        out = []
        for event in self.api.tournaments():
            start = int(event.get("startAt") or 0)
            if not start:
                continue
            minutes = (start / 1000 - now) / 60
            entrants = int(event.get("entrants") or 0)
            if entrants < 3:
                continue  # a countdown to an empty field is not an alert
            for mark in (60, 15):
                if mark - 5 <= minutes <= mark + 1:
                    out.append(
                        {
                            "key": f"event:{event.get('id')}:{mark}",
                            "kind": "event",
                            "stake": None,
                            "text": (
                                f"⏰ <b>{fmt.esc(event.get('name'))}</b> starts in "
                                f"{mark} minutes\n"
                                f"{entrants}/{event.get('maxEntrants') or '?'} registered"
                                f" · {fmt.usd(event.get('addedPrize'))} added\n"
                                "https://suited.club"
                            ),
                        }
                    )
        return out

    def _results(self) -> list[dict]:
        out = []
        try:
            results = self.api.results(limit=1)
        except Exception:
            return out
        for event in results:
            places = event.get("places") or []
            if not places:
                continue
            top = "\n".join(
                f"{place.get('place')}. {fmt.esc(place.get('name'))} "
                f"{fmt.usd(place.get('payout'))}"
                for place in places[:3]
            )
            out.append(
                {
                    "key": f"result:{event.get('id')}",
                    "kind": "result",
                    "stake": None,
                    "text": (
                        f"🏆 <b>{fmt.esc(event.get('name'))}</b> finished — "
                        f"{event.get('entrants')} players, "
                        f"{fmt.usd(event.get('prizePool'))} pool\n{top}"
                    ),
                }
            )
        return out

    def _biggest_pot(self) -> list[dict]:
        out = []
        try:
            pots = (self.api.jackpot().get("biggestPots") or [])[:1]
        except Exception:
            return out
        for pot in pots:
            out.append(
                {
                    "key": f"pot:{pot.get('at')}",
                    "kind": "pot",
                    "stake": None,
                    "text": (
                        f"💥 <b>Biggest pot of the day</b> — {fmt.usd(pot.get('pot'))}\n"
                        f"{fmt.esc(pot.get('winner'))} took it down"
                    ),
                }
            )
        return out

    def _jackpot_draw(self) -> list[dict]:
        out = []
        try:
            full = self.api.jackpot()
        except Exception:
            return out
        winners = full.get("winners") or []
        for draw in winners[:1]:
            places = draw.get("places") or []
            if not places:
                continue
            top = "\n".join(
                f"{p.get('place')}. {fmt.esc(p.get('name'))} {fmt.usd(p.get('prize'))}"
                for p in places[:3]
            )
            out.append(
                {
                    "key": f"draw:{draw.get('week')}",
                    "kind": "draw",
                    "stake": None,
                    "text": (
                        f"🎟 <b>Jackpot drawn</b> · {fmt.esc(draw.get('week'))} — "
                        f"{fmt.usd(draw.get('pool'))} pool\n{top}"
                    ),
                }
            )
        return out

    def _milestones(self) -> list[dict]:
        """Round-number fun facts, at most one a day, only on the way up."""
        out = []
        try:
            stats = self.api.stats()
        except Exception:
            return out
        rake = float(stats.get("rake") or 0) / 1e6
        bucket = int(rake // 1000)
        if bucket < 1:
            return out
        previous = self.store.get("rake_bucket")
        self.store.set("rake_bucket", str(bucket))
        if previous is None or int(previous) == bucket:
            return out
        return [
            {
                "key": f"rake:{bucket}",
                "kind": "milestone",
                "stake": None,
                "text": f"📈 <b>${bucket * 1000:,} of lifetime rake</b> taken by the room.",
            }
        ]

    # -- delivery ----------------------------------------------------------

    @staticmethod
    def _global_key(alert: dict) -> str:
        return f"g:{alert['key']}"

    def _fan_out(self, alert: dict, now: int) -> int:
        sent = 0
        for user in self.store.users(only_alerts=True):
            if not self._wants(user, alert):
                continue
            if self._quiet(user, now):
                continue
            if now - int(user["last_push"] or 0) < 300:  # 5 min between pings
                continue
            key = f"{user['chat_id']}:{alert['key']}"
            if self.store.was_sent(key):
                continue
            if self.notify(user["chat_id"], alert["text"], alert["kind"]):
                self.store.mark_sent(key)
                self.store.mark_pushed(user["chat_id"])
                sent += 1
        return sent

    def _wants(self, user, alert: dict) -> bool:
        if alert["kind"] != "table":
            return True
        for sub in self.store.subscriptions(user["chat_id"]):
            if sub["stake"] == alert["stake"] and alert["seated"] >= sub["min_seated"]:
                return True
        return False

    @staticmethod
    def _quiet(user, now: int) -> bool:
        hour = time.gmtime(now).tm_hour
        start, end = int(user["quiet_from"]), int(user["quiet_to"])
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end
