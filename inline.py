"""Inline mode: `@suited_club_bot scimmia` in any chat, no group membership.

Inline results are a different medium from a DM: the text lands as a message the
user sends, so every result has to read correctly out of context and carry the
link. Titles and descriptions are plain text (Telegram rejects HTML there); only
`input_message_content` is HTML.

The query is deliberately forgiving: a nickname, a command name, or nothing.
Rate limits mean the answer must be cheap, so results come from the same cached
client the commands use and are capped at eight.
"""

from __future__ import annotations

import re

import fmt
import views

TAGS = re.compile(r"<[^>]+>")
FOOTER = "\n\n<b>Suited</b> · https://suited.club"


def plain(markup: str, limit: int = 120) -> str:
    text = TAGS.sub("", markup)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _article(result_id: str, title: str, description: str, body: str) -> dict:
    text = body if "https://suited.club" in body else body + FOOTER
    return {
        "type": "article",
        "id": result_id,
        "title": title,
        "description": description,
        "input_message_content": {
            "message_text": text[:4000],
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
        },
    }


def _player(api, needle: str) -> dict | None:
    profile = api.find_player(needle)
    if not profile:
        return None
    stats = profile.get("stats") or {}
    summary = (
        f"{int(stats.get('hands') or 0):,} hands · "
        f"{fmt.sign(stats.get('net'))}{fmt.usd(stats.get('net'))} · "
        f"{float(stats.get('bb100') or 0):+.1f} bb/100"
    )
    return _article(
        f"player:{profile.get('id')}",
        f"{profile.get('name')} · Suited profile",
        summary,
        views.player_view(api, str(profile.get("id"))),
    )


def build(api, query: str, live: dict[str, dict] | None = None) -> list[dict]:
    needle = (query or "").strip()
    if needle.startswith("/"):
        needle = needle[1:]
    if needle.lower().startswith("p "):
        needle = needle[2:].strip()
    results: list[dict] = []

    if not needle:
        now = views.now_view(api, live)
        week = views.leaderboard_view(api, "week")
        events = views.tournaments_view(api)
        price = views.price_view(api)
        return [
            _article("live", "Live now", plain(now), now),
            _article("board", "This week's leaderboard", plain(week, 90), week),
            _article("events", "Upcoming events", plain(events, 90), events),
            _article("token", "$SUITED", plain(price, 90), price),
        ]

    if len(needle) >= 3:
        # Short queries match half the roster on a substring; only search once
        # the person has typed something that could be a nickname.
        player = _player(api, needle)
        if player:
            results.append(player)

    lowered = needle.lower().lstrip("/")
    catalogue = [
        ("now", "Live now", lambda: views.now_view(api, live)),
        ("stats", "Room stats", lambda: views.stats_view(api)),
        ("lb", "Leaderboard", lambda: views.leaderboard_view(api, "week")),
        ("tourneys", "Upcoming events", lambda: views.tournaments_view(api)),
        ("jackpot", "Jackpot", lambda: views.jackpot_view(api)),
        ("price", "$SUITED price", lambda: views.price_view(api)),
        ("stake", "Staking", lambda: views.staking_view(api)),
        ("when", "When the room is alive", lambda: views.when_view(api)),
    ]
    for name, title, render in catalogue:
        if len(results) >= 8:
            break
        if lowered and not (lowered in name or name in lowered or lowered in title.lower()):
            continue
        body = render()
        results.append(_article(f"cmd:{name}", title, plain(body, 90), body))

    if not results:
        results.append(
            _article(
                "help",
                "No match — try a nickname",
                "Ask for a player, or one of: live, stats, board, events, jackpot, price",
                views.help_text(),
            )
        )
    return results[:8]
