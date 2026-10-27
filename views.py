"""Every user-visible message, rendered from the public read model.

Views return HTML for `parse_mode=HTML`. They are pure functions of the API
payload (plus the local row where settings are involved), so they can be
exercised without Telegram — `python3 bot.py --selftest` does exactly that.

House style: lead with the number people came for, keep a DM under ~15 lines,
and never invent a figure the API did not send.
"""

from __future__ import annotations

import time

import fmt

COMMANDS = [
    ("now", "live tables + what's next"),
    ("stats", "lifetime and this week"),
    ("lb", "leaderboard: week | 7d | all"),
    ("p", "player profile by nickname"),
    ("tourneys", "upcoming events"),
    ("tourney", "one event: blinds + payouts"),
    ("jackpot", "pool, sources, last winners"),
    ("price", "token, holders, burns"),
    ("stake", "staking APR and funding"),
    ("when", "when the room is actually alive"),
    ("alerts", "table push settings"),
    ("help", "this list"),
]

STAKES = {
    "nl2": "1¢/2¢",
    "nl10": "5¢/10¢",
    "nl50": "25¢/50¢",
    "nl200": "$1/$2",
    "nl500": "$2/$5",
    "nl1000": "$5/$10",
    "nl2000": "$10/$20",
}


TIER_ORDER = {"common": 1, "uncommon": 2, "rare": 3, "mythic": 4}


def _top_tier(badges: list[dict]) -> str:
    best = max(
        (badge.get("tier") for badge in badges if badge.get("tier")),
        key=lambda tier: TIER_ORDER.get(tier, 0),
        default="",
    )
    return str(best)


def stake_key(table: dict) -> str:
    return str(table.get("id") or "").split("-")[0]


def stake_label(table: dict) -> str:
    return STAKES.get(stake_key(table), fmt.short_stake(table))


def stake_label_for(key: str) -> str:
    return STAKES.get(key, key)


def help_text() -> str:
    lines = ["<b>Suited bot</b> — live room, numbers and token, in a DM.", ""]
    for name, blurb in COMMANDS:
        lines.append(f"/{name} — {blurb}")
    lines += [
        "",
        "Push alerts are opt-in per stake: /alerts add nl200",
        "Inline: type <code>@suited_club_bot scimmia</code> in any chat.",
        "Everything is read-only and comes from suited.club's public API.",
    ]
    return "\n".join(lines)


def welcome(name: str | None) -> str:
    who = f", {fmt.esc(name)}" if name else ""
    return (
        f"<b>Welcome{who}.</b>\n\n"
        "I watch the Suited room and the token and tell you when there is a "
        "game worth sitting in — plus the numbers on demand.\n\n"
        + help_text()
        + "\n\nStart with /now."
    )


def _table_rows(tables: list[dict]) -> list[str]:
    lines = []
    for table in tables:
        seated, seats = table.get("seated") or 0, table.get("maxSeats") or 0
        open_seats = table.get("openSeats")
        open_seats = seats - seated if open_seats is None else open_seats
        kind = "HU" if "HU" in str(table.get("name") or "") else "6-max"
        extra = f" · pot {fmt.usd(table.get('avgPot'))}" if table.get("avgPot") else ""
        lines.append(
            f"<b>{fmt.esc(stake_label(table))}</b> {kind} — {seated}/{seats} seated"
            f" · {open_seats} open{extra}"
        )
    return lines


SUITS = {"c": "♣", "d": "♦", "h": "♥", "s": "♠"}


def card(code: str) -> str:
    """`Tc` -> `10♣`. The API uses rank + lowercase suit."""
    if not code or len(code) < 2:
        return "?"
    rank, suit = code[:-1], code[-1].lower()
    return ("10" if rank.upper() == "T" else rank.upper()) + SUITS.get(suit, suit)


def _seat_lines(state: dict) -> list[str]:
    lines = []
    actor = state.get("to_act")
    for seat in state.get("seats") or []:
        marks = []
        if seat.get("sitting_out"):
            marks.append("out")
        if seat.get("all_in"):
            marks.append("all in")
        if seat.get("folded"):
            marks.append("folded")
        elif seat.get("last"):
            marks.append(str(seat["last"]))
        arrow = "▶ " if actor is not None and seat.get("idx") == actor else ""
        stack = fmt.usd(seat.get("stack"))
        tail = f" ({', '.join(marks)})" if marks else ""
        lines.append(f"{arrow}{fmt.esc(seat.get('name'))} — {stack}{tail}")
    return lines


def _live_detail(state: dict) -> list[str]:
    """The part of the table the public API cannot see."""
    lines = []
    if state.get("phase") in ("preflop", "flop", "turn", "river", "showdown"):
        head = f"hand #{int(state.get('hand_no') or 0):,} · {state.get('phase')}"
        if state.get("pot"):
            head += f" · pot {fmt.usd(state['pot'])}"
        lines.append(head)
        if state.get("board"):
            lines.append("board " + " ".join(card(c) for c in state["board"]))
    return lines


def now_view(api, live: dict[str, dict] | None = None) -> str:
    stats = api.stats()
    tables = api.tables()
    rows = [t for t in tables.get("tables", []) if int(t.get("seated") or 0) > 0]
    rows.sort(key=lambda t: -int(t.get("seated") or 0))
    seated = int(stats.get("seated") or 0)
    online = int(tables.get("playersOnline") or 0)
    head = f"🟢 <b>Live now</b> — {seated} seated · {online} online"
    if not rows:
        head = "😴 <b>No table running right now</b>"

    lines = [head, ""]
    for table in rows:
        label = stake_label(table)
        seats, capacity = int(table.get("seated") or 0), int(table.get("maxSeats") or 0)
        kind = "heads-up" if "HU" in str(table.get("name") or "") else "6-max"
        open_seats = table.get("openSeats")
        open_seats = capacity - seats if open_seats is None else open_seats
        watching = int(table.get("watching") or 0)
        meta = f"{seats}/{capacity} seated · {open_seats} open"
        if watching:
            meta += f" · {watching} watching"
        lines.append(f"<b>{fmt.esc(label)}</b> {kind} — {meta}")

        state = (live or {}).get(table.get("id"))
        if state and state.get("seats"):
            lines += ["  " + line for line in _seat_lines(state)]
            detail = _live_detail(state)
            if detail:
                lines += ["  " + line for line in detail]
        else:
            pot = _pot_line(table)
            if pot:
                lines.append("  " + pot)
        lines.append("")

    if not rows:
        means, peak, top = api.hour_profile()
        if means:
            lines.append(
                f"Peak hour is <b>{peak:02d}:00 UTC</b> ({top:.0f} hands/hr avg). "
                "The room usually wakes up 20:00–23:00 UTC."
            )

    events = api.next_events(limit=2)
    if events:
        for event in events:
            lines.append(
                f"🕒 <b>{fmt.esc(event.get('name'))}</b> {fmt.rel(event.get('startAt'))}"
                f" · {event.get('entrants') or 0}/{event.get('maxEntrants') or '?'}"
                f" · {fmt.usd(event.get('addedPrize'))} added"
            )
    if rows:
        lines += ["", "Sit in: https://suited.club"]
    return "\n".join(line for line in lines if line is not None).strip()


def _pot_line(table: dict) -> str:
    pot = table.get("avgPot")
    return f"avg pot {fmt.usd(pot)}" if pot else ""


def stats_view(api) -> str:
    stats = api.stats()
    hands = int(stats.get("handsDealt") or 0)
    week_hands = int(stats.get("handsThisWeek") or 0)
    lines = [
        "<b>Suited — the room</b>",
        f"Hands dealt <b>{hands:,}</b> · {week_hands:,} this week",
        f"Wagered <b>{fmt.usd_compact(stats.get('wagered'))}</b> lifetime"
        f" · {fmt.usd_compact(stats.get('wageredThisWeek'))} this week",
        f"Rake <b>{fmt.usd_compact(stats.get('rake'))}</b> lifetime",
        f"Players <b>{int(stats.get('players') or 0):,}</b>"
        f" · {int(stats.get('seated') or 0)} seated"
        f" · {int(stats.get('liveTables') or 0)} table live",
        f"In play <b>{fmt.usd(stats.get('inPlay'))}</b>",
    ]
    try:
        sources = api.jackpot_sources()
        if sources.get("total"):
            lines.append(
                f"Jackpot this week <b>{fmt.usd(sources.get('total'))}</b>"
                f" · {fmt.usd(sources.get('fromRollover'))} carried over"
            )
    except Exception:
        pass
    lines.append("")
    lines.append(
        "Effective rake "
        + fmt.pct(fmt.safe_div(stats.get("rake"), stats.get("wagered")) * 100, 2)
        + " of everything wagered."
    )
    return "\n".join(line for line in lines if line != "")


def _ordinal(place: int) -> str:
    if 10 <= place % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(place % 10, "th")
    return f"{place}{suffix}"


def leaderboard_view(api, period: str = "week") -> str:
    period = period if period in ("week", "7d", "all") else "week"
    board = api.leaderboard(period=period, limit=10)
    rows = board.get("rows") or []
    title = {"week": "This week", "7d": "Last 7 days", "all": "All time"}[period]
    if not rows:
        return f"<b>{title}</b>\nNo qualifying players (min {board.get('minHands') or 5} hands)."
    lines = [
        f"<b>{title} leaderboard</b> · pool {fmt.usd(board.get('pool'))}",
        f"{board.get('players') or 0} players · {int(board.get('hands') or 0):,} hands · "
        f"min {board.get('minHands') or 5} hands",
        "",
    ]
    for row in rows:
        net = float(row.get("net") or 0)
        mark = "🟩" if net > 0 else "🟥"
        lines.append(
            f"{row.get('rank'):>2}. {mark} <b>{fmt.esc(row.get('name'))}</b> "
            f"{'+' if net >= 0 else '-'}{fmt.usd(abs(net))}"
            f" · {float(row.get('netBb') or 0):+.0f} bb"
            f" · {int(row.get('hands') or 0)}h"
        )
    lines += ["", "/lb 7d · /lb all"]
    return "\n".join(lines)


def player_view(api, needle: str) -> str:
    profile = api.find_player(needle)
    if not profile:
        return (
            f"No player matching <b>{fmt.esc(needle)}</b> on the week, 7d or all-time board.\n"
            "Try /lb to see who is on it."
        )
    stats = profile.get("stats") or {}
    level = profile.get("level") or {}
    periods = stats.get("periods") or {}
    day = periods.get("day") or {}
    week = periods.get("week") or {}
    badges = profile.get("achievements") or []
    lines = [
        f"<b>{fmt.esc(profile.get('name'))}</b>"
        + (f" · {fmt.esc(level.get('title'))}" if level.get("title") else ""),
        f"Level {level.get('level') or '?'} · joined {fmt.esc(profile.get('joined') or '?')}"
        + (f" · rank #{stats.get('rank')}" if stats.get("rank") else ""),
        "",
        f"Lifetime: <b>{int(stats.get('hands') or 0):,} hands</b>"
        f" · {fmt.sign(stats.get('net'))}{fmt.usd(stats.get('net'))}"
        f" · {float(stats.get('bb100') or 0):+.1f} bb/100",
        f"Today: {int(day.get('hands') or 0)} hands"
        f" · {fmt.sign(day.get('net'))}{fmt.usd(day.get('net'))}",
        f"Week: {int(week.get('hands') or 0)} hands"
        f" · {fmt.sign(week.get('net'))}{fmt.usd(week.get('net'))}",
        f"Volume {fmt.usd_compact(stats.get('volume'))}"
        f" · best pot {fmt.usd(stats.get('best'))}",
        f"Badges {len(badges)}/{profile.get('achievementsTotal') or '?'}"
        + (f" · best {_top_tier(badges)}" if badges else ""),
        "",
        f"https://suited.club/player/{profile.get('id')}",
    ]
    return "\n".join(lines)


def tournaments_view(api) -> str:
    events = api.tournaments()
    upcoming = sorted(
        [e for e in events if e.get("state") not in ("finished", "cancelled")],
        key=lambda e: e.get("startAt") or 0,
    )
    if not upcoming:
        return "<b>No events scheduled.</b>"
    lines = ["<b>Upcoming events</b>", ""]
    for event in upcoming[:8]:
        state = event.get("state") or ""
        dot = {"registering": "🟢", "running": "▶️", "scheduled": "⚪"}.get(state, "·")
        lines.append(
            f"{dot} <b>{fmt.esc(event.get('name'))}</b> — {fmt.rel(event.get('startAt'))}"
            f"\n    {fmt.usd(event.get('buyIn'))} buy-in"
            f" · {fmt.usd(event.get('addedPrize'))} added"
            f" · {event.get('entrants') or 0}/{event.get('maxEntrants') or '?'} in"
        )
    lines += ["", "/tourney Major — blinds, levels and payouts"]
    return "\n".join(lines)


def tournament_view(api, needle: str) -> str:
    events = api.tournaments()
    needle_l = needle.strip().lower()
    match = next((e for e in events if e.get("id") == needle_l), None)
    if not match:
        match = next((e for e in events if needle_l in str(e.get("name") or "").lower()), None)
    if not match:
        return f"No event matching <b>{fmt.esc(needle)}</b>. See /tourneys."
    detail = api.tournament(match["id"])
    state = str(detail.get("state") or "")
    heading = f"<b>{fmt.esc(detail.get('name'))}</b>"
    if state in ("running", "finished"):
        heading += f" · {state}"
    lines = [
        heading,
        f"Starts {fmt.utc(detail.get('startAt'))} ({fmt.rel(detail.get('startAt'))})",
        f"Buy-in {fmt.usd(detail.get('buyIn'))} + {fmt.usd(detail.get('fee'))} fee"
        f" · {fmt.usd(detail.get('addedPrize'))} added",
        f"Field {detail.get('entrants') or 0}/{detail.get('maxEntrants') or '?'}"
        f" · {detail.get('openSeats') or 0} seats open",
        f"Stack {int(detail.get('startingStack') or 0):,}"
        f" · {detail.get('levelMinutes') or '?'}-min levels",
        f"Pool now {fmt.usd(detail.get('prizePool') or detail.get('projectedPool'))}"
        f" · max {fmt.usd(detail.get('maxPool'))}",
    ]
    blinds = detail.get("blinds") or []
    if blinds:
        sample = ", ".join(
            f"{b.get('sb')}/{b.get('bb')}" + (f"/{b.get('ante')}" if b.get("ante") else "")
            for b in blinds[:4]
        )
        lines.append(f"Blinds {sample} …")
    payouts = detail.get("payouts") or []
    if payouts:
        lines.append(
            "Payouts "
            + " · ".join(f"{_ordinal(i + 1)} {p}%" for i, p in enumerate(payouts[:5]))
        )
    now_ms = time.time() * 1000
    open_at = detail.get("openAt") or 0
    if open_at > now_ms:
        lines.append(f"Registration opens {fmt.rel(open_at)}")
    else:
        lines.append("Register at https://suited.club")
    return "\n".join(lines)


def jackpot_view(api) -> str:
    sources = api.jackpot_sources()
    full = api.jackpot()
    lines = [
        "<b>Jackpot</b>",
        f"Pool <b>{fmt.usd(full.get('pool'))}</b>"
        f" · {full.get('entrants') or 0} entrants"
        f" · draws {fmt.rel(full.get('closesAt'))}",
        f"From rake {fmt.usd(sources.get('fromRake'))}"
        f" · creator {fmt.usd(sources.get('fromCreator'))}"
        f" · rollover {fmt.usd(sources.get('fromRollover'))}",
    ]
    winners = full.get("winners") or []
    if winners:
        latest = winners[0]
        lines += ["", f"<b>Last draw</b> · {fmt.esc(latest.get('week'))}"
                      f" · pool {fmt.usd(latest.get('pool'))}"]
        for place in (latest.get("places") or [])[:3]:
            pct = place.get("chance")
            lines.append(
                f"{place.get('place')}. <b>{fmt.esc(place.get('name'))}</b> "
                f"{fmt.usd(place.get('prize'))}"
                + (f" · {pct:.2f}% odds" if isinstance(pct, (int, float)) else "")
            )
    pots = full.get("biggestPots") or []
    if pots:
        lines += ["", "<b>Biggest pots</b>"]
        for pot in pots[:3]:
            lines.append(
                f"{fmt.usd(pot.get('pot'))} — {fmt.esc(pot.get('winner'))}"
                f" · {fmt.rel(pot.get('at'))}"
            )
    rules = full.get("rules") or {}
    if rules.get("prizeLadder"):
        lines += ["", "Split " + " / ".join(f"{p}%" for p in rules["prizeLadder"])]
    return "\n".join(lines)


def price_view(api) -> str:
    token = api.token()
    price = token.get("price") or {}
    holders = token.get("holders") or {}
    staked = token.get("staked") or {}
    supply = token.get("supply") or {}
    burn = token.get("burn") or {}
    totals = burn.get("totals") or {}
    change24 = price.get("change24hBps")
    lines = [
        f"<b>$SUITED</b> {fmt.price(price.get('e18'))}",
        f"24h {(fmt.bps(change24) if change24 is not None else '—')}"
        f" · 7d {(fmt.bps(price.get('change7dBps')) if price.get('change7dBps') is not None else '—')}",
        f"Market cap <b>{fmt.usd_compact(token.get('marketCapMicro'))}</b>",
        f"Holders {int(holders.get('now') or 0):,}"
        f" ({fmt.bps(holders.get('changeBps'))} w/w)",
        f"Staked {fmt.suited(staked.get('total'))}"
        f" · {int(staked.get('positions') or 0)} positions",
        f"Burnt {fmt.suited(totals.get('burnt'))}"
        f" · next burn {fmt.rel(burn.get('nextRun'))}",
    ]
    supply_total = float(supply.get("total") or 0)
    if supply_total:
        lines.append(
            f"Supply {fmt.suited(supply_total)}"
            f" · {fmt.pct(float(totals.get('burnt') or 0) / supply_total * 100, 2)} burnt"
        )
    return "\n".join(lines)


def staking_view(api) -> str:
    data = api.staking()
    state = data.get("state") or {}
    trailing = (data.get("apr") or {}).get("trailing") or {}
    current = (data.get("apr") or {}).get("current") or {}
    funded = data.get("funded") or {}
    tiers = trailing.get("perTier") or []
    durations = {
        tier.get("boostBps"): int(tier.get("duration") or 0) // 86400
        for tier in (data.get("terms") or {}).get("tiers") or []
    }
    lines = [
        "<b>Staking</b>" + ("" if data.get("live") else " (read model stale)"),
        f"Staked <b>{fmt.suited(state.get('totalStaked'))}</b>"
        f" · weight {fmt.suited(state.get('activeWeight'))}",
        f"Funded {fmt.usd(funded.get('rake'))} from rake"
        f" + {fmt.usd(funded.get('fees'))} from fees",
        "",
        f"<b>APR</b> (trailing {trailing.get('days') or '?'}d)",
    ]
    for tier in tiers:
        lock = durations.get(tier.get("boostBps"))
        label = f"{lock}d lock" if lock else f"{tier.get('boostBps', 0) / 100:.0f}% boost"
        lines.append(f"· {label} — {fmt.pct(tier.get('pct'), 1)}")
    if current.get("perTier"):
        lines.append(
            "Current: " + " · ".join(f"{fmt.pct(t.get('pct'), 1)}" for t in current["perTier"])
        )
    lines += [
        "",
        f"Reward period ends {fmt.utc(int(state.get('periodFinish') or 0) * 1000)}",
        "Lock or claim at https://suited.club/staking",
    ]
    return "\n".join(lines)


def when_view(api) -> str:
    means, peak, top = api.hour_profile()
    if not means:
        return "No hourly history yet."
    good = [i for i in range(24) if means[i] >= top * 0.75]
    # the contiguous stretch around the peak is the recommendation
    start = end = peak
    while (start - 1) % 24 in good and start - 1 >= 0:
        start -= 1
    while (end + 1) % 24 in good and end + 1 < 24:
        end += 1
    now_hour = time.gmtime().tm_hour
    seated = int(api.stats().get("seated") or 0)
    lines = [
        "<b>When the room is alive</b> (avg hands/hr by UTC hour)",
        "",
    ]
    for hour in range(24):
        mark = "◀" if hour == peak else ("·" if hour in good else " ")
        lines.append(
            f"{hour:02d} {fmt.bar(means[hour], top, 10)} {means[hour]:4.0f} {mark}"
        )
    lines += [
        "",
        f"<b>Best window: {start:02d}:00–{(end + 1) % 24:02d}:00 UTC</b>"
        f" ({top:.0f} hands/hr at peak)",
        f"Right now it is {now_hour:02d}:00 UTC with {seated} seated",
        "" + ("In the good window." if now_hour in good else "Outside the usual window — expect it quiet."),
    ]
    return "\n".join(lines)


def alerts_view(store, chat_id: int, api=None) -> str:
    user = store.user(chat_id)
    subs = store.subscriptions(chat_id)
    lines = ["<b>Alerts</b>"]
    if user:
        lines.append(
            f"Table pushes: {'on' if user['alerts'] else 'off'}"
            f" · daily recap: {'on' if user['digest'] else 'off'}"
        )
        lines.append(f"Quiet {user['quiet_from']:02d}:00–{user['quiet_to']:02d}:00 UTC")
    lines.append("")
    if subs:
        for sub in subs:
            lines.append(
                f"· {fmt.esc(stake_label_for(sub['stake']))}"
                f" — ping at {sub['min_seated']}+ seated"
            )
    else:
        lines.append("No stakes followed yet.")
    lines += [
        "",
        "<b>Commands</b>",
        "/alerts on | off — table pushes",
        "/alerts add nl200 3 — follow $1/$2 from 3 seated",
        "/alerts del nl200",
        "/alerts quiet 2 9 — silence 02:00–09:00 UTC",
        "/alerts digest on | off — the 09:00 recap",
        "",
        "Stakes: " + ", ".join(f"{k} ({v})" for k, v in STAKES.items()),
    ]
    return "\n".join(lines)


def digest_view(api, day: str | None = None) -> str:
    """Yesterday in one message. Day index -2 because -1 is today, partial."""
    history = api.history()
    days = history.get("days") or []
    if len(days) < 2:
        return "Not enough history yet."
    index = len(days) - 2
    if day and day in days:
        index = days.index(day)
    cash = history.get("cash") or {}
    hands = cash.get("hands") or []
    wagered = cash.get("wagered") or []
    rake = cash.get("rake") or []
    pots = cash.get("biggestPot") or []
    players = history.get("players") or {}
    new = players.get("new") or []
    active = players.get("active") or []

    def at(series, i, default=0):
        return series[i] if 0 <= i < len(series) else default

    label = days[index]
    stats = api.stats()
    lines = [
        f"<b>Suited — {label}</b>",
        f"Hands <b>{int(at(hands, index)):,}</b>"
        f" · wagered {fmt.usd(at(wagered, index))}"
        f" · rake {fmt.usd(at(rake, index))}",
        f"Players {int(at(active, index))} active"
        f" · {int(at(new, index))} new"
        f" · {int(stats.get('seated') or 0)} seated now",
        f"Biggest pot {fmt.usd(at(pots, index))}",
    ]
    events = api.results(limit=1)
    if events:
        event = events[0]
        places = event.get("places") or []
        if places:
            lines += ["", f"<b>{fmt.esc(event.get('name'))}</b>"
                          f" · pool {fmt.usd(event.get('prizePool'))}"
                          f" · {event.get('entrants')} players"]
            for place in places[:3]:
                lines.append(
                    f"{place.get('place')}. {fmt.esc(place.get('name'))} "
                    f"{fmt.usd(place.get('payout'))}"
                )
    upcoming = api.next_events(limit=1)
    if upcoming:
        event = upcoming[0]
        lines += [
            "",
            f"Next: <b>{fmt.esc(event.get('name'))}</b> {fmt.rel(event.get('startAt'))}"
            f" · {fmt.usd(event.get('addedPrize'))} added",
        ]
    lines += ["", "/now · /stats · /tourneys"]
    return "\n".join(lines)
