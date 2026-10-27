# Suited Telegram bot

A DM bot for the Suited poker room on Robinhood Chain. It answers questions
from `suited.club`'s public API and pushes the few things worth interrupting
someone for — a table actually filling, an event about to start, a jackpot
drawn.

Live as **[@suited_club_bot](https://t.me/suited_club_bot)**.

```
python3 bot.py --check       # token valid? prints the handle
python3 bot.py --selftest    # render every view against the live API
python3 bot.py --once        # one alert tick, then exit
python3 bot.py               # run (long poll + 30 s alert loop)
```

## Why it is shaped this way

The room is small: on a normal evening four people are seated at one table and
ten appear on the weekly board. A bot that only paged "a table is live" would
be silent for most of the day and muted within a week.

So the bot has three jobs, in this order:

1. **Answer.** Commands that make it worth opening: current tables, leaderboard,
   a player profile, the tournament calendar, jackpot, token, staking, and
   *when the room is actually alive* (the hourly heatmap — peak is 20:00–23:00
   UTC at ~270 hands/hr).
2. **Recap.** One daily message at 09:00 UTC with yesterday's hands, rake,
   biggest pot and the last event's top three. This is what keeps the DM warm.
3. **Alert.** Per-stake table pings, event countdowns at T-60/T-15 (only if
   three or more registered), results, biggest pot, jackpot draw.

## Data sources

| Source | What it gives | Key needed |
|---|---|---|
| `/api/public/*` | stats, tables, leaderboard, players, tournaments, jackpot, staking, achievements | no |
| `/api/token`, `/api/token/price` | price, market cap, holders, burns | no |
| spectator session (`spectator.py`) | a signed-in read identity: `/api/me`, `/api/lobby`, `ws-ticket` | local key, generated (not in the repo) |

Everything is read-only. The bot never signs a transaction, holds no funds and
cannot move anything.

### The spectator identity

The public namespace reports *four people are seated*, not *who*. The table
socket carries seats, names, stacks, board and actions, and the shipped client
says the socket reaches spectators — a connection with no seat gets a normal
`sync` with `heroIdx: null`. `spectator.py` mints that identity: EIP-4361
challenge → `personal_sign` (EIP-191) → bearer token, cached for its 12-hour
TTL.

It is **off by default** and deliberately isolated:

- the key is a fresh, unfunded address in `data/spectator.key` (mode 0600);
- only `login`, `me`, `lobby` and `ticket` exist — no writes, no signing of
  anything but the sign-in message;
- if the team ever closes this path, set `SPECTATOR=0` and the bot keeps
  running on the public API alone.

Verified 2026-10-26: sign-in, `/api/me`, `/api/lobby` and `ws-ticket` all work,
and a spectator socket on `nl200-…` returns a full `sync` (`seats[]`, `handNo`,
`board`, `pot`, `toAct`, `clock`).

## Commands

| Command | Answer |
|---|---|
| `/now` | live tables with **seat names, stacks and the current hand** (`SPECTATOR=1`), otherwise seated/open counts, plus next two events |
| `/stats` | hands, wagered, rake, players, effective rake |
| `/lb [week\|7d\|all]` | top 10 with net, bb and hands |
| `/p <nickname\|id>` | level, lifetime/day/week P&L, bb/100, best badges |
| `/tourneys` | upcoming events with added prize and field |
| `/tourney <name>` | one event: stacks, levels, blinds, payouts, seats |
| `/jackpot` | pool and provenance, last winners, biggest pots |
| `/price` | $SUITED price, 24h/7d, mcap, holders, staked, burnt |
| `/stake` | staking APR by lock tier and funding |
| `/when` | hourly activity heatmap and the best window |
| `/alerts` | push settings, per-stake follows, quiet hours |
| `/id` | chat id (needed when the channel is wired up) |

`/alerts` subcommands: `on`/`off`, `add nl200 3`, `del nl200`,
`quiet 2 9` (UTC hours), `digest on`/`off`.

## Inline mode

Enabled in BotFather (`/setinline`). Type `@suited_club_bot <nickname>` — or
just `@suited_club_bot` — in any chat and pick a result; the text lands as the
user's message, so it works in groups the bot has never joined.

| Query | Results |
|---|---|
| empty | Live now · this week's board · upcoming events · $SUITED |
| `scimmia`, `p scimmia`, `/p scimmia` | that player's profile card |
| `stats`, `lb`, `now`, `jackpot`, `price`, `stake`, `when`, `tourneys` | that view |

Results are plain articles (no `web_app`), capped at eight, and cached by
Telegram for 30 s. The bot refreshes its inline capability from `getMe` every
ten minutes, so enabling inline mode in BotFather does not need a restart.

## Live seat data

`SPECTATOR=1` adds a thin socket client (`wss.py`) and `Spectator.live_state()`:
open `wss://suited.club/ws?ticket=…&table=…`, send `hello`, take the first
`sync`, close. That is where seat names, stacks, the board, the pot and whose
turn it is come from — the public API only counts heads.

- One connection per live table (max three), cached 8 s per table and refreshed
  in the background every poll so `/now` and inline queries read a snapshot.
- If the socket refuses, times out or the identity cannot sign in, `/now` falls
  back to public counts. It is never a failed command.
- Opponents' hole cards arrive as `??`; they are never rendered.

## State

`data/bot.sqlite` — users, per-stake subscriptions, quiet hours, and a `sent`
table that makes every push idempotent. Alerts are **primed** on first run:
whatever is already true is marked sent, so a deploy never replays a burst.

`data/bot.log`, `data/bot.sqlite`, `data/spectator.*` and `.env` are all
gitignored, and CI fails if one of them is ever added.

## Repository

| Path | What |
|---|---|
| `bot.py` | poll loop, routing, inline handler, scheduler, `--check` / `--selftest` / `--once` |
| `suited.py` | cached client for the public API plus derived reads (live tables, next events, hourly profile, name→id) |
| `views.py` | every message, rendered as Telegram HTML |
| `alerts.py` | push triggers, priming, per-stake fan-out, quiet hours |
| `store.py` | sqlite: users, subscriptions, idempotent send keys |
| `inline.py` | inline-query results |
| `spectator.py`, `wallet.py`, `wss.py` | read-only session, EIP-191 signing, a minimal WebSocket client |
| `deploy/` | systemd unit, deploy script, deploy doc |
| `scripts/check_secrets.py` | fails on a token, key, bearer, public IP or home path |

**Not in the repository, by design:** `.env` (the bot token), `data/bot.sqlite`
(chat ids and usernames), `data/spectator.key` (a private key) and
`data/spectator.json` (a live bearer token). `.env.example` documents the shape
without the values; `python3 spectator.py --keygen` creates a fresh identity.

Before pushing:

```bash
python3 scripts/check_secrets.py --all
```

## Running it

```bash
cp .env.example .env             # paste the BotFather token; SPECTATOR=1 for seat names
pip install -r requirements.txt  # optional — only spectator mode needs it
python3 bot.py --check
python3 bot.py
```

To run it permanently on a Linux host, `deploy/deploy.sh` installs the systemd
**user** unit and restarts it:

```bash
HOST=user@server ./deploy/deploy.sh
```

It is long polling, so the host needs no inbound port, no TLS and no domain.
`deploy/DEPLOY.md` has the details (state, secrets, rotation, verification).

## Adding the channel

The bot currently ignores non-private chats on purpose. When the channel is
ready: post once in the channel, run `/id` in it, then add the id to `.env` as
`CHANNEL_ID`; the scheduled posts and alerts gain a second destination. Nothing
in the command path changes.

## Next

- A "hand of the hour" post built from the socket's `history[]` frames.
- "I'm in" RSVP buttons so the bot can *create* the table instead of reporting
  it — the one feature that attacks the empty-lobby problem rather than
  narrating it.
- A Telegram Mini App reusing these same reads.
