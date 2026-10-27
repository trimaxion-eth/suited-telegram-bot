#!/usr/bin/env python3
"""Suited DM bot — long-polls Telegram, answers from suited.club's public API.

No third-party packages: `urllib` for both sides, `sqlite3` for state. Long
polling (not a webhook) so the process needs no inbound port, no TLS and no
domain — it can sit next to the tracker on the box and be restarted by systemd.

Usage:
    python3 bot.py                 # run
    python3 bot.py --check         # verify the token, print the bot handle
    python3 bot.py --selftest      # render every view against the live API
    python3 bot.py --once          # one alert tick, then exit (debug)
"""

from __future__ import annotations

import fcntl
import html
import json
import os
import re
import signal
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import alerts as alerting
import fmt
import inline as inline_mode
import store as storage
import views
from suited import ApiError, Suited

HERE = Path(__file__).resolve().parent
API = "https://api.telegram.org"
MAX_LEN = 4000

COMMAND_MENU = [
    ("now", "live tables and what's next"),
    ("stats", "lifetime and this week"),
    ("lb", "leaderboard"),
    ("p", "player profile"),
    ("tourneys", "upcoming events"),
    ("tourney", "event detail"),
    ("jackpot", "jackpot pool and winners"),
    ("price", "token, holders, burns"),
    ("stake", "staking APR"),
    ("when", "when the room is alive"),
    ("alerts", "table push settings"),
    ("help", "all commands"),
]


def prefer_ipv4() -> None:
    """Try IPv4 addresses before IPv6 ones, process-wide.

    The Hetzner box has IPv6 but api.telegram.org's AAAA is blackholed from it:
    `curl -6` hangs, `curl -4` connects in 28 ms. `create_connection` walks
    getaddrinfo in order and has no happy-eyeballs, so without this every new
    connection stalls until the socket timeout — a 70 s stall at start-up and
    the same on each reconnect.
    """
    if str(os.environ.get("IPV4_FIRST") or "1").lower() in ("0", "false", "no"):
        return
    original = socket.getaddrinfo

    def ordered(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        infos = original(host, port, family, type, proto, flags)
        return sorted(infos, key=lambda info: 0 if info[0] == socket.AF_INET else 1)

    socket.getaddrinfo = ordered  # type: ignore[assignment]
    log("resolver: IPv4 first")


def log(message: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
    print(f"{stamp} {message}", flush=True)


def load_env(path: Path) -> dict[str, str]:
    """Read KEY=VALUE lines. Real environment wins over the file."""
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    for key in list(values):
        if os.environ.get(key):
            values[key] = os.environ[key]
    return values


def live_reader(config: dict):
    """Build the spectator reader, or None when it is off or unusable.

    Imported lazily on purpose: spectator mode needs `coincurve`, and a bot
    that cannot read seat names should still answer every other command.
    """
    if str(config.get("SPECTATOR") or "0").lower() not in ("1", "true", "yes", "on"):
        return None
    try:
        import spectator as spectator_module
    except (ImportError, SystemExit) as error:
        log(f"spectator disabled: {error}")
        return None
    key_path = Path(config.get("SPECTATOR_KEY_FILE") or HERE / "data" / "spectator.key")
    cache_path = Path(config.get("SPECTATOR_CACHE") or HERE / "data" / "spectator.json")
    if not key_path.exists():
        log(f"spectator disabled: no key at {key_path}")
        return None
    base = config.get("SUITED_BASE") or "https://suited.club"
    try:
        reader = spectator_module.Spectator(base, key_path, cache_path, log=log)
        reader.login()
    except Exception as error:
        log(f"spectator disabled: sign-in failed ({error})")
        return None
    return reader


class Telegram:
    """The two calls this bot needs, plus 429/HTML handling."""

    def __init__(self, token: str, timeout: float = 20.0):
        self.token = token
        self.timeout = timeout

    def call(self, method: str, read_timeout: float | None = None, **params):
        url = f"{API}/bot{self.token}/{method}"
        body = json.dumps(params).encode()
        request = urllib.request.Request(
            url, data=body, headers={"content-type": "application/json"}
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(
                    request, timeout=read_timeout or self.timeout
                ) as response:
                    payload = json.loads(response.read())
                if not payload.get("ok"):
                    raise RuntimeError(payload.get("description") or "telegram error")
                return payload.get("result")
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", "replace")
                try:
                    parsed = json.loads(detail)
                except json.JSONDecodeError:
                    parsed = {}
                if error.code == 429:
                    wait = int((parsed.get("parameters") or {}).get("retry_after") or 3)
                    log(f"rate limited by telegram, waiting {wait}s")
                    time.sleep(wait + 1)
                    continue
                if error.code >= 500 and attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise RuntimeError(f"{method} {error.code}: {detail[:200]}") from error
            except (urllib.error.URLError, TimeoutError):
                if attempt < 2:
                    time.sleep(1 + attempt)
                    continue
                raise
        raise RuntimeError(f"{method}: gave up")

    def send(self, chat_id: int, text: str, keyboard: list | None = None, preview: bool = True):
        for body in self._split(text):
            params = {
                "chat_id": chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": not preview,
            }
            if keyboard:
                params["reply_markup"] = {"inline_keyboard": keyboard}
                keyboard = None  # only attach to the first chunk
            try:
                self.call("sendMessage", **params)
            except RuntimeError as error:
                if "can't parse entities" not in str(error).lower():
                    raise
                params.pop("parse_mode", None)
                params["text"] = html.unescape(re.sub(r"</?[a-z][^>]*>", "", body))
                self.call("sendMessage", **params)
        return True

    def typing(self, chat_id: int) -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass

    @staticmethod
    def _split(text: str, limit: int = MAX_LEN) -> list[str]:
        if len(text) <= limit:
            return [text]
        chunks, current = [], []
        size = 0
        for line in text.split("\n"):
            if size + len(line) + 1 > limit and current:
                chunks.append("\n".join(current))
                current, size = [], 0
            current.append(line)
            size += len(line) + 1
        if current:
            chunks.append("\n".join(current))
        return chunks

    def set_commands(self, commands: list[tuple[str, str]] = COMMAND_MENU) -> None:
        self.call(
            "setMyCommands",
            commands=[{"command": name, "description": blurb} for name, blurb in commands],
        )


class Bot:
    def __init__(self, api: Suited, store: storage.Store, tg: Telegram, config: dict,
                 inline_ok: bool = False):
        self.api = api
        self.store = store
        self.tg = tg
        self.config = config
        self.alerter = alerting.Alerter(api, store, self._notify, log)
        self.spectator = live_reader(config)
        self._snapshot: dict[str, dict] = {}
        self._snapshot_at = 0.0
        self.inline_ok = inline_ok
        self.running = True

    # -- delivery ----------------------------------------------------------

    def _notify(self, chat_id: int, text: str, kind: str) -> bool:
        try:
            self.tg.send(chat_id, text, preview=False)
            log(f"push {kind} -> {chat_id}")
            return True
        except Exception as error:
            log(f"push {kind} -> {chat_id} failed: {error}")
            return False

    # -- routing -----------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        if "inline_query" in update:
            return self._handle_inline(update["inline_query"])
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        message = update.get("message") or update.get("edited_message")
        if not message:
            return
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return  # DM only for now; channel wiring lands separately
        chat_id = chat.get("id")
        if not chat_id:
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        user = message.get("from") or {}
        self.store.touch_user(chat_id, user.get("username"), user.get("first_name"))

        command, _, argument = text[1:].partition(" ")
        command = command.split("@")[0].lower()
        argument = argument.strip()
        log(f"/{command} from {chat_id}" + (f": {argument}" if argument else ""))

        try:
            self.tg.typing(chat_id)
            reply, keyboard = self.route(command, argument, chat_id, user)
        except ApiError as error:
            log(f"/{command} api error: {error}")
            reply, keyboard = (
                "suited.club did not answer that one just now. Try again in a moment.",
                None,
            )
        except Exception as error:  # never let one bad command kill the poll loop
            log(f"/{command} failed: {error!r}")
            reply, keyboard = "Something broke on my side. Try again in a moment.", None

        if reply:
            self.tg.send(chat_id, reply, keyboard=keyboard, preview=False)

    def _handle_inline(self, query: dict) -> None:
        text = query.get("query") or ""
        user = query.get("from") or {}
        try:
            results = inline_mode.build(self.api, text, self._live_states())
        except Exception as error:
            log(f"inline {text!r} failed: {error!r}")
            results = []
        log(f"inline {text!r} -> {len(results)} from {user.get('id')}")
        try:
            self.tg.call(
                "answerInlineQuery",
                inline_query_id=query.get("id"),
                results=results,
                cache_time=30,
                is_personal=False,
            )
        except Exception as error:
            log(f"answerInlineQuery failed: {error}")

    def _handle_callback(self, query: dict) -> None:
        query_id = query.get("id")
        data = query.get("data") or ""
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=query_id)
        except Exception:
            pass
        if not chat_id or not data.startswith("cmd:"):
            return
        command = data[4:]
        user = query.get("from") or {}
        self.store.touch_user(chat_id, user.get("username"), user.get("first_name"))
        try:
            reply, keyboard = self.route(command, "", chat_id, user)
        except Exception as error:
            log(f"callback {command} failed: {error!r}")
            return
        if reply:
            self.tg.send(chat_id, reply, keyboard=keyboard, preview=False)

    def route(self, command: str, argument: str, chat_id: int, user: dict):
        api = self.api
        if command in ("start", "help"):
            if command == "start":
                self.store.touch_user(chat_id, user.get("username"), user.get("first_name"))
                reply = views.welcome(user.get("first_name"))
            else:
                reply = views.help_text()
            return reply, self.quick_keyboard()

        if command == "now":
            return views.now_view(api, self._live_states()), self.quick_keyboard()
        if command == "stats":
            return views.stats_view(api), None
        if command == "lb":
            return views.leaderboard_view(api, argument or "week"), None
        if command in ("p", "player"):
            if not argument:
                return (
                    "Usage: /p &lt;nickname&gt; — e.g. /p scimmia\n"
                    "/lb shows who is on the board this week."
                ), None
            return views.player_view(api, argument), None
        if command in ("tourneys", "tournaments"):
            return views.tournaments_view(api), None
        if command == "tourney":
            if not argument:
                return "Usage: /tourney &lt;name&gt; — e.g. /tourney Major", None
            return views.tournament_view(api, argument), None
        if command == "jackpot":
            return views.jackpot_view(api), None
        if command == "price":
            return views.price_view(api), None
        if command in ("stake", "staking"):
            return views.staking_view(api), None
        if command == "when":
            return views.when_view(api), None
        if command == "alerts":
            return self._alerts(chat_id, argument), None
        if command == "id":
            return f"chat id <code>{chat_id}</code>", None
        return views.help_text(), self.quick_keyboard()

    def _refresh_live(self) -> dict[str, dict] | None:
        """One socket per live table, capped at three, refreshed on a timer.

        Kept warm by the heartbeat thread so `/now` and inline queries read a
        snapshot instead of each paying for a handshake — inline queries fire
        while someone types, and a socket per keystroke would be rude.
        """
        if not self.spectator:
            return None
        states: dict[str, dict] = {}
        for table in self.api.live_tables()[:3]:
            try:
                states[table["id"]] = self.spectator.live_state(table["id"], ttl=25)
            except Exception as error:
                log(f"live state {table.get('id')}: {error}")
        self._snapshot = states
        self._snapshot_at = time.time()
        return states or None

    def _live_states(self) -> dict[str, dict] | None:
        """Snapshot if it is warm, otherwise a synchronous refresh."""
        if not self.spectator:
            return None
        if self._snapshot and time.time() - self._snapshot_at < 45:
            return self._snapshot
        return self._refresh_live()

    def _alerts(self, chat_id: int, argument: str):
        self.store.touch_user(chat_id, None, None)
        parts = argument.split()
        if not parts:
            return views.alerts_view(self.store, chat_id)
        action = parts[0].lower()
        if action in ("on", "off"):
            self.store.set_flag(chat_id, "alerts", 1 if action == "on" else 0)
            note = "Table pushes on." if action == "on" else "Table pushes off."
            if action == "on" and not self.store.subscriptions(chat_id):
                note += (
                    "\nYou have no stakes followed yet — add one:"
                    "\n<code>/alerts add nl200 3</code>"
                    "\nStakes: " + ", ".join(views.STAKES)
                )
            return note + "\n\n" + views.alerts_view(self.store, chat_id)
        if action == "digest":
            on = len(parts) > 1 and parts[1].lower() in ("on", "1", "yes")
            self.store.set_flag(chat_id, "digest", 1 if on else 0)
            return f"Daily recap {'on' if on else 'off'}.\n\n" + views.alerts_view(
                self.store, chat_id
            )
        if action == "add":
            if len(parts) < 2 or parts[1] not in views.STAKES:
                return (
                    "Which stake? One of: " + ", ".join(views.STAKES)
                    + "\n<code>/alerts add nl200 3</code>"
                )
            minimum = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 2
            self.store.subscribe(chat_id, parts[1], minimum)
            return (
                f"Following <b>{views.STAKES[parts[1]]}</b> from {minimum} seated.\n\n"
                + views.alerts_view(self.store, chat_id)
            )
        if action in ("del", "remove"):
            if len(parts) > 1:
                self.store.unsubscribe(chat_id, parts[1])
            return "Updated.\n\n" + views.alerts_view(self.store, chat_id)
        if action == "quiet":
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                self.store.set_quiet(chat_id, int(parts[1]), int(parts[2]))
                return (
                    f"Quiet {int(parts[1]):02d}:00–{int(parts[2]):02d}:00 UTC.\n\n"
                    + views.alerts_view(self.store, chat_id)
                )
            return "Usage: <code>/alerts quiet 2 9</code> (UTC hours)"
        return views.alerts_view(self.store, chat_id)

    def quick_keyboard(self):
        def button(label, command):
            return {"text": label, "callback_data": f"cmd:{command}"}

        rows = [
            [button("Live now", "now"), button("Stats", "stats")],
            [button("Leaderboard", "lb"), button("Events", "tourneys")],
            [button("Price", "price"), button("When", "when")],
        ]
        if self.inline_ok:
            rows.append([{"text": "Share…", "switch_inline_query_current_chat": ""}])
        return rows

    # -- loops -------------------------------------------------------------

    def poll_forever(self) -> None:
        offset = int(self.store.get("offset") or 0)
        while self.running:
            try:
                updates = self.tg.call(
                    "getUpdates",
                    read_timeout=70,
                    offset=offset,
                    timeout=50,
                    allowed_updates=["message", "callback_query", "inline_query"],
                )
            except Exception as error:
                log(f"getUpdates failed: {error}")
                time.sleep(5)
                continue
            for update in updates or []:
                offset = max(offset, int(update["update_id"]) + 1)
                try:
                    self.handle_update(update)
                except Exception as error:
                    log(f"update {update.get('update_id')} failed: {error!r}")
            self.store.set("offset", str(offset))

    def heartbeat_forever(self) -> None:
        last_digest = ""
        last_probe = 0.0
        while self.running:
            try:
                self.alerter.tick()
            except Exception as error:
                log(f"alert tick failed: {error!r}")
            try:
                last_digest = self._maybe_digest(last_digest)
            except Exception as error:
                log(f"digest failed: {error!r}")
            if self.spectator:
                try:
                    self._refresh_live()
                except Exception as error:
                    log(f"live refresh failed: {error!r}")
            # Inline mode is a BotFather switch, not something we can set. Pick
            # it up within ten minutes instead of requiring a restart.
            if time.time() - last_probe > 600:
                last_probe = time.time()
                try:
                    me = self.tg.call("getMe") or {}
                    inline_now = bool(me.get("supports_inline_queries"))
                    if inline_now != self.inline_ok:
                        self.inline_ok = inline_now
                        log(f"inline mode {'enabled' if inline_now else 'disabled'}")
                except Exception:
                    pass
            time.sleep(int(self.config.get("POLL_SECONDS") or 30))

    def _maybe_digest(self, last: str) -> str:
        now = time.gmtime()
        hour = int(self.config.get("DIGEST_HOUR") or 9)
        if hour != now.tm_hour:
            return last
        today = time.strftime("%Y-%m-%d", now)
        if last == today or self.store.get("digest_day") == today:
            return today
        text = views.digest_view(self.api)
        delivered = 0
        for user in self.store.users():
            if not user["digest"]:
                continue
            if self._notify(user["chat_id"], text, "digest"):
                delivered += 1
        self.store.set("digest_day", today)
        self.store.prune()
        log(f"daily digest {today} -> {delivered} chat(s)")
        return today

    def stop(self, *_args) -> None:
        self.running = False
        log("stopping")
        # The poll sits inside a 50 s long-poll; do not make a deploy wait for
        # it. Give the loop three seconds to unwind, then leave regardless —
        # the sqlite writes are already committed per call.
        import threading

        def _exit() -> None:
            time.sleep(3)
            os._exit(0)

        threading.Thread(target=_exit, name="exit", daemon=True).start()


ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "tg-spoiler", "blockquote"}


def html_problems(markup: str) -> list[str]:
    """Telegram rejects the whole message on one stray `<`, so check every view.

    Only the tags the bot actually uses are allowed; anything else is a bug in
    a format string rather than something to send and hope for.
    """
    from html.parser import HTMLParser

    class Check(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=False)
            self.stack: list[str] = []
            self.problems: list[str] = []

        def handle_starttag(self, tag, attrs):
            if tag not in ALLOWED_TAGS:
                self.problems.append(f"unknown <{tag}> at {self.getpos()}")
            self.stack.append(tag)

        def handle_endtag(self, tag):
            if not self.stack or self.stack[-1] != tag:
                self.problems.append(f"unbalanced </{tag}> at {self.getpos()}")
            else:
                self.stack.pop()

    checker = Check()
    checker.feed(markup)
    return checker.problems + [f"unclosed <{tag}>" for tag in checker.stack]


def selftest(api: Suited, store: storage.Store, config: dict | None = None) -> int:
    """Render every view and print it, so formatting is testable without Telegram."""
    live: dict[str, dict] = {}
    spectator = live_reader(config or {})
    if spectator:
        for table in api.live_tables()[:3]:
            try:
                live[table["id"]] = spectator.live_state(table["id"])
            except Exception as error:
                print(f"(socket {table.get('id')} failed: {error})")
    checks = [
        ("help", lambda: views.help_text()),
        ("now", lambda: views.now_view(api, live)),
        ("stats", lambda: views.stats_view(api)),
        ("lb week", lambda: views.leaderboard_view(api, "week")),
        ("lb all", lambda: views.leaderboard_view(api, "all")),
        ("player", lambda: views.player_view(api, "scimmia")),
        ("player miss", lambda: views.player_view(api, "zzz-not-a-player")),
        ("tourneys", lambda: views.tournaments_view(api)),
        ("tourney", lambda: views.tournament_view(api, "Major")),
        ("jackpot", lambda: views.jackpot_view(api)),
        ("price", lambda: views.price_view(api)),
        ("stake", lambda: views.staking_view(api)),
        ("when", lambda: views.when_view(api)),
        ("alerts", lambda: views.alerts_view(store, 424242)),
        ("digest", lambda: views.digest_view(api)),
        ("inline empty", lambda: "\n".join(
            f"{r['title']} :: {r['description']}" for r in inline_mode.build(api, "", live)
        )),
        ("inline player", lambda: "\n".join(
            f"{r['title']} :: {r['description']}" for r in inline_mode.build(api, "scimmia", live)
        )),
    ]
    ok = True
    for name, render in checks:
        try:
            body = render()
        except Exception as error:
            ok = False
            print(f"\n### {name} FAILED: {error!r}")
            continue
        problems = html_problems(body)
        if problems:
            ok = False
            print(f"\n### {name} HTML INVALID: {problems}")
        longest = max((len(line) for line in body.split("\n")), default=0)
        print(f"\n### {name} ({len(body)} chars, longest line {longest})")
        print(body)
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    prefer_ipv4()
    config = load_env(HERE / ".env")
    token = config.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    base = config.get("SUITED_BASE") or "https://suited.club"
    api = Suited(base=base)

    state_path = Path(config.get("STATE_PATH") or HERE / "data" / "bot.sqlite")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    store = storage.Store(state_path)

    if "--selftest" in argv:
        return selftest(api, store, config)
    if not token:
        print("TELEGRAM_BOT_TOKEN missing — cp .env.example .env", file=sys.stderr)
        return 2

    tg = Telegram(token)
    if "--check" in argv:
        me = tg.call("getMe")
        print(f"token ok: @{me.get('username')} ({me.get('first_name')})")
        return 0

    if "--once" in argv:
        bot = Bot(api, store, tg, config)
        print(f"alerts sent: {bot.alerter.tick()}")
        return 0

    lock_path = state_path.with_suffix(".lock")
    lock = open(lock_path, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("another instance is running", file=sys.stderr)
        return 1

    me = tg.call("getMe") or {}
    bot = Bot(api, store, tg, config, inline_ok=bool(me.get("supports_inline_queries")))
    try:
        tg.set_commands()
    except Exception as error:
        log(f"setMyCommands failed: {error}")
    log(
        f"bot up as @{me.get('username')} ({state_path})"
        f" · inline={'on' if bot.inline_ok else 'off'}"
        f" · spectator={'on' if bot.spectator else 'off'}"
    )

    signal.signal(signal.SIGTERM, bot.stop)
    signal.signal(signal.SIGINT, bot.stop)

    import threading

    heartbeat = threading.Thread(target=bot.heartbeat_forever, name="alerts", daemon=True)
    heartbeat.start()
    try:
        bot.poll_forever()
    finally:
        bot.running = False
        store.close()
        lock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
