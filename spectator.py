"""Read-only spectator session for suited.club.

Why this exists: the public API is aggregate-only — it will tell you four
people are seated at `$1/$2` but not who, for how much, or what just happened.
The table socket does carry that, and it treats an unseated connection as a
spectator (`everything on the socket reaches spectators too` in the shipped
client). The only thing it wants first is a signed-in identity.

This module mints that identity and caches the token until its 12-hour TTL
runs out. It is off by default (`SPECTATOR=1` in `.env` turns it on) because it
is the one part of the bot that leans on an interface the team never documented
as an integration surface: if it ever stops working, the bot keeps running on
the public API alone.

    python3 spectator.py --login     # sign in, print who we are and the lobby
    python3 spectator.py --keygen    # create the identity (already done)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

import wallet
import wss

HERE = Path(__file__).resolve().parent
UA = "suited-telegram-bot/1.0 (spectator)"
TOKEN_SKEW = 300  # re-auth five minutes before the 12h TTL actually lapses


class SpectatorError(RuntimeError):
    pass


class Spectator:
    def __init__(self, base: str, key_path: Path, cache_path: Path, log=print):
        self.base = base.rstrip("/")
        self.key_path = key_path
        self.cache_path = cache_path
        self.log = log
        self.token: str | None = None
        self.address: str | None = None
        self.expires_at = 0.0
        self._live: dict[str, tuple[float, dict]] = {}
        self._load_cache()

    # -- token cache -------------------------------------------------------

    def _load_cache(self) -> None:
        if not self.cache_path.exists():
            return
        try:
            data = json.loads(self.cache_path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if data.get("expires_at", 0) - TOKEN_SKEW > time.time():
            self.token = data.get("token")
            self.address = data.get("address")
            self.expires_at = float(data.get("expires_at") or 0)

    def _save_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"token": self.token, "address": self.address, "expires_at": self.expires_at}
        )
        with open(
            os.open(self.cache_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w"
        ) as handle:
            handle.write(payload)

    # -- transport ---------------------------------------------------------

    def _open(self, path: str, data: dict | None = None, method: str | None = None,
              auth: bool = True, timeout: float = 15.0):
        url = self.base + path
        body = json.dumps(data).encode() if data is not None else None
        headers = {"user-agent": UA}
        if body is not None:
            headers["content-type"] = "application/json"
        if auth and self.token:
            headers["authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(
            url, data=body, headers=headers, method=method or ("POST" if body else "GET")
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", "replace")
            raise SpectatorError(f"{path} -> {error.code}: {detail[:200]}") from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise SpectatorError(f"{path}: {error}") from error

    # -- auth --------------------------------------------------------------

    def login(self, force: bool = False) -> str:
        if self.token and not force and self.expires_at - TOKEN_SKEW > time.time():
            return self.token
        key, address = wallet.load(self.key_path)
        challenge = self._open("/api/auth/challenge", {"pubkey": address}, auth=False)
        signature = wallet.personal_sign(key, challenge["message"])
        verified = self._open(
            "/api/auth/verify",
            {
                "pubkey": address,
                "nonce": challenge["nonce"],
                "signature": signature,
            },
            auth=False,
        )
        self.token = verified.get("token")
        self.address = verified.get("pubkey") or address
        # the gateway does not return a TTL; the client treats it as 12 h
        self.expires_at = time.time() + 12 * 3600
        self._save_cache()
        self.log(f"spectator signed in as {self.address}")
        return self.token

    def ticket(self) -> str:
        self.login()
        return self._open("/api/ws-ticket", {}).get("ticket", "")

    # -- reads -------------------------------------------------------------

    def me(self):
        self.login()
        return self._open("/api/me")

    def lobby(self):
        self.login()
        return self._open("/api/lobby")

    def table_state(self, table_id: str):
        """The same read the lobby uses, narrowed to one table."""
        lobby = self.lobby()
        for table in lobby.get("tables") or []:
            if table.get("id") == table_id:
                return table
        return None

    # -- live table state (the reason this identity exists) ----------------

    def live_state(self, table_id: str, ttl: float = 8.0, timeout: float = 6.0) -> dict:
        """Open the table socket as a spectator, take the first `sync`, close.

        A socket per call rather than a standing connection: `/now` is answered
        on demand, the whole exchange is ~200 ms, and a long-lived socket would
        need its own reconnect logic for no extra information. Results are
        cached for `ttl` seconds so a burst of commands costs one connection.

        Raises SpectatorError when the socket refuses or never syncs — callers
        must fall back to the public seated counts rather than fail the command.
        """
        now = time.time()
        hit = self._live.get(table_id)
        if hit and hit[0] > now:
            return hit[1]

        ticket = self.ticket()
        ws_base = self.base.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/ws?ticket={ticket}&table={table_id}"
        deadline = now + timeout
        try:
            with wss.WebSocket(url, timeout=timeout, user_agent=UA) as socket:
                socket.send_text(json.dumps({"t": "hello", "tableId": table_id}))
                while time.time() < deadline:
                    raw = socket.recv_text(max(0.5, deadline - time.time()))
                    if raw is None:
                        break
                    try:
                        frame = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if frame.get("t") == "sync":
                        state = self._normalise(table_id, frame)
                        self._live[table_id] = (time.time() + ttl, state)
                        return state
        except (OSError, wss.WSError) as error:
            raise SpectatorError(f"socket {table_id}: {error}") from error
        raise SpectatorError(f"socket {table_id}: no sync frame")

    @staticmethod
    def _normalise(table_id: str, frame: dict) -> dict:
        state = frame.get("state") or {}
        seats = []
        for seat in state.get("seats") or []:
            if seat.get("empty"):
                continue
            seats.append(
                {
                    "idx": seat.get("idx"),
                    "name": seat.get("name") or "?",
                    "stack": int(seat.get("stack") or 0),
                    "sitting_out": bool(seat.get("sittingOut")),
                    "all_in": bool(seat.get("allIn")),
                    "last": seat.get("lastAction"),
                    "folded": bool(seat.get("folded")),
                }
            )
        board = [card for card in (state.get("board") or []) if card and card != "??"]
        return {
            "table_id": table_id,
            "hand_no": state.get("handNo"),
            "phase": state.get("phase"),
            "street": state.get("street"),
            "pot": int(state.get("potTotal") or state.get("pot") or 0),
            "board": board,
            "to_act": state.get("toAct"),
            "paused": bool(state.get("paused")),
            "seats": seats,
            "as_of": time.time(),
        }


def _defaults():
    env = {}
    env_file = HERE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                env[key.strip()] = value.strip()
    base = env.get("SUITED_BASE") or "https://suited.club"
    key_path = Path(env.get("SPECTATOR_KEY_FILE") or HERE / "data" / "spectator.key")
    cache_path = Path(env.get("SPECTATOR_CACHE") or HERE / "data" / "spectator.json")
    return base, key_path, cache_path


def main(argv: list[str]) -> int:
    base, key_path, cache_path = _defaults()
    if "--keygen" in argv:
        if key_path.exists() and "--force" not in argv:
            print(f"key already exists at {key_path}")
            return 1
        hexkey, address = wallet.generate(key_path)
        print(f"created {key_path} -> {address}")
        print("add this to .env: SPECTATOR_KEY_FILE=data/spectator.key")
        return 0

    spectator = Spectator(base, key_path, cache_path, log=print)

    if "--table" in argv:
        table_id = argv[argv.index("--table") + 1]
        state = spectator.live_state(table_id)
        print(json.dumps(state, indent=1))
        return 0

    if "--login" in argv:
        me = spectator.me()
        print(f"address   {spectator.address}")
        print(f"public id {me.get('publicId')} · nickname {me.get('nickname')!r}")
        print(f"balance   {me.get('balance')} micro-USDG")
        lobby = spectator.lobby()
        live = [t for t in lobby.get("tables") or [] if (t.get("seated") or 0) > 0]
        print(f"lobby     {len(lobby.get('tables') or [])} tables, "
              f"{lobby.get('playersOnline')} online, {len(live)} live")
        for table in live:
            print(f"  {table.get('name')} seated={table.get('seated')} id={table.get('id')}")
        print(f"ticket    {'ok' if spectator.ticket() else 'empty'}")
        return 0

    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(__import__("sys").argv[1:]))
