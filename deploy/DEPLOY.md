# Telegram bot — deploy

Deploy doc for `@suited_club_bot`. No host, address or user name in this file:
the target is whatever box you point `HOST` at, and any operator-specific
notes belong outside the repository.

| | |
|---|---|
| **Source** | this repository |
| **Target** | any Linux host with systemd user lingering, reachable over ssh |
| **Path** | `~/apps/suited-telegram-bot` |
| **Service** | systemd **user** unit `suited-telegram-bot.service` |
| **Port** | **none** — long polling via `getUpdates` |
| **Reverse proxy** | not involved (nothing to route) |
| **Logs** | `journalctl --user -u suited-telegram-bot` |
| **State** | `~/apps/suited-telegram-bot/data/bot.sqlite` |

It is a worker, not a web app: no container, no inbound port, no TLS, no DNS.
`Restart=always` plus journald covers what a container would add.

## Deploy

```bash
cp .env.example .env             # first time only; paste the BotFather token
HOST=user@server ./deploy/deploy.sh
HOST=user@server ./deploy/deploy.sh --logs
```

The script:

1. rsyncs the code with `--delete`, excluding `.env`, `data/`, `.venv/`,
   `__pycache__/`, `*.log`, `*.sqlite`;
2. on **first run only**, copies `.env` (0600), `data/spectator.key`,
   `data/spectator.json` and `data/bot.sqlite` from this checkout, so the bot
   keeps its subscriber list and its spectator identity; later runs leave them
   untouched;
3. installs `deploy/suited-telegram-bot.service` to
   `~/.config/systemd/user/` and reloads systemd;
4. creates `.venv` if missing and installs `requirements.txt` (only
   `coincurve`, for signing the spectator sign-in);
5. `systemctl --user enable --now` then `restart`;
6. checks `is-active`, runs `bot.py --check` (validates the token with `getMe`)
   and prints the last six journal lines.

Prerequisite on the host: `python3-venv` (many distros ship it separately), and
`loginctl enable-linger <user>` so the unit survives logout.

## Operate

```bash
systemctl --user status suited-telegram-bot
systemctl --user restart suited-telegram-bot
journalctl --user -u suited-telegram-bot -f
journalctl --user -u suited-telegram-bot --since "1 hour ago"
```

`bot.py` answers SIGTERM with a 3 s watchdog, so restarts are quick and
`TimeoutStopSec=15` is ample.

## Secrets

`.env` — mode `600`:

```
TELEGRAM_BOT_TOKEN=…      # @BotFather; rotate with /revoke there
SPECTATOR=1
SPECTATOR_KEY_FILE=data/spectator.key
SPECTATOR_CACHE=data/spectator.json
```

`data/spectator.key` (0600) is an **unfunded** EVM key used only to sign the
EIP-4361 message that opens the read-only spectator session. It holds no money
and the bot never signs a transaction. Rotating it means deleting the key and
the cache, then restarting — the address changes and the old session dies.

Nothing here is tracked by git: `.gitignore` covers `.env`, `data/`, sqlite,
logs and venv. `scripts/check_secrets.py` fails the build if one slips in.

## One instance only

Telegram allows several processes to call `getUpdates`, but they steal each
other's updates and offsets. Run exactly one poller:

```bash
systemctl --user stop suited-telegram-bot   # on the host
pgrep -f '^python3 bot.py'                  # locally — should print nothing
```

The sqlite file carries the update offset, the subscriber list and the
idempotency keys, so moving between hosts keeps the state coherent.

## Verify

```bash
ssh user@server 'cd ~/apps/suited-telegram-bot && .venv/bin/python bot.py --check'
ssh user@server 'cd ~/apps/suited-telegram-bot && .venv/bin/python spectator.py --login'
ssh user@server 'cd ~/apps/suited-telegram-bot && .venv/bin/python bot.py --selftest'
```

Then in Telegram: `/now` (seat names when a table is live), `@suited_club_bot
scimmia` (inline), `/alerts`.

## Reverse proxy / DNS

None. The bot opens outbound TLS connections to `api.telegram.org` and
`suited.club`; nothing dials in.
