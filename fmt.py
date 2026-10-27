"""Formatting helpers shared by every view.

The API speaks three units: micro-USDG (divide by 1e6), wei (divide by 1e18,
for $SUITED) and milliseconds. Nothing else in this package divides anything —
the split keeps the arithmetic in one place where it can be checked.

Telegram is sent HTML (`parse_mode=HTML`), so every string that came from the
API has to pass through `esc()`: player names are user-chosen and will happily
carry an `&` or a `<`.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

MICRO = 1_000_000
WEI = 10**18


def esc(text: object) -> str:
    """Escape for Telegram's HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _f(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


def usd(micro: object, dp: int = 2) -> str:
    """micro-USDG -> `$1,234.56`."""
    value = _f(micro) / MICRO
    if dp == 0:
        return f"${value:,.0f}"
    return f"${value:,.{dp}f}"


def usd_compact(micro: object) -> str:
    """micro-USDG -> `$2.55M` style, for headline counters."""
    value = _f(micro) / MICRO
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(value) >= cut:
            return f"${value / cut:,.2f}{suffix}"
    return f"${value:,.2f}"


def suited(wei: object) -> str:
    """wei -> $SUITED amount with a sane number of decimals."""
    value = _f(wei) / WEI
    if abs(value) >= 1e6:
        return f"{value / 1e6:,.2f}M"
    if abs(value) >= 1e3:
        return f"{value / 1e3:,.2f}k"
    return f"{value:,.0f}"


def price(e18: object) -> str:
    """Token price in USD, already 1e18-scaled by the API (`price.e18`)."""
    value = _f(e18) / WEI
    if value == 0:
        return "—"
    if value < 0.01:
        return f"${value:.8f}".rstrip("0").rstrip(".")
    return f"${value:,.4f}"


def compact(value: object) -> str:
    number = _f(value)
    for cut, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if abs(number) >= cut:
            return f"{number / cut:,.1f}{suffix}"
    return f"{number:,.0f}"


def bps(value: object, dp: int = 2) -> str:
    """Basis points -> `+1.77%`."""
    pct = _f(value) / 100
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.{dp}f}%"


def dt(ms: object) -> datetime:
    return datetime.fromtimestamp(_f(ms) / 1000, tz=timezone.utc)


def utc(ms: object, pattern: str = "%d %b %H:%M") -> str:
    return dt(ms).strftime(pattern) + " UTC"


def rel(ms: object, now: float | None = None) -> str:
    """`in 2h 14m` / `4m ago` — the form the DM wants, not a timestamp."""
    if not ms:
        return "—"
    now = now if now is not None else datetime.now(tz=timezone.utc).timestamp()
    delta = _f(ms) / 1000 - now
    future = delta >= 0
    delta = abs(delta)
    if delta < 60:
        body = f"{int(delta)}s"
    elif delta < 3600:
        body = f"{int(delta // 60)}m"
    elif delta < 86400:
        hours, minutes = divmod(int(delta // 60), 60)
        body = f"{hours}h" + (f" {minutes}m" if minutes and hours < 6 else "")
    else:
        days, rest = divmod(int(delta // 3600), 24)
        body = f"{days}d" + (f" {rest}h" if rest and days < 7 else "")
    return f"in {body}" if future else f"{body} ago"


def bar(value: object, peak: object, width: int = 12) -> str:
    peak = _f(peak) or 1.0
    filled = max(0, min(width, int(round(_f(value) / peak * width))))
    return "█" * filled + "·" * (width - filled)


def short_stake(table: dict) -> str:
    """`$1/$2 · 1` -> `$1/$2` (drops the table-set index the API appends)."""
    name = str(table.get("name") or "").split(" · ")[0]
    return name or "table"


def sign(value_micro: object) -> str:
    return "+" if _f(value_micro) >= 0 else ""


def pct(value: object, dp: int = 0) -> str:
    return f"{_f(value):.{dp}f}%"


def safe_div(a: object, b: object) -> float:
    b = _f(b)
    return _f(a) / b if b else 0.0


def round_up(number: float, step: float) -> float:
    return math.ceil(number / step) * step
