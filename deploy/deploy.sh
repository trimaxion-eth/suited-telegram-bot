#!/usr/bin/env bash
# Deploy the Suited Telegram bot to a Linux host over ssh.
#
#   HOST=user@server ./deploy/deploy.sh          # code + unit + restart
#   HOST=user@server ./deploy/deploy.sh --logs   # ... then follow the journal
#
# Nothing here is host-specific: the target user must have a systemd **user**
# session with lingering enabled (`loginctl enable-linger <user>`), which is
# what keeps the bot alive after logout and across reboots.
#
# First run seeds the host with the local secrets and state:
#   ~/apps/suited-telegram-bot/.env                (0600, never in git)
#   ~/apps/suited-telegram-bot/data/spectator.key  (0600)
#   ~/apps/suited-telegram-bot/data/bot.sqlite     (subscribers, idempotency)
# After that, rsync never touches them again (they are excluded).
set -euo pipefail

HOST="${HOST:?usage: HOST=user@server $0 [--logs]}"
REMOTE="${REMOTE:-apps/suited-telegram-bot}"
LOCAL="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="suited-telegram-bot"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

if git -C "$LOCAL" ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "refusing to deploy: .env is tracked by git" >&2
  exit 1
fi

say "1/6 code -> $HOST:~/$REMOTE"
rsync -az --delete \
  --exclude '.env' \
  --exclude 'data/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.log' \
  --exclude '*.sqlite' \
  -e ssh "$LOCAL/" "$HOST:~/$REMOTE/"

ssh "$HOST" "mkdir -p ~/$REMOTE/data"

say "2/6 first-run secrets + state"
if ! ssh "$HOST" "test -f ~/$REMOTE/.env"; then
  test -f "$LOCAL/.env" || { echo "missing $LOCAL/.env — cp .env.example .env"; exit 1; }
  rsync -az --chmod=600 -e ssh "$LOCAL/.env" "$HOST:~/$REMOTE/.env"
  for f in spectator.key spectator.json bot.sqlite; do
    if [ -f "$LOCAL/data/$f" ]; then
      rsync -az --chmod=600 -e ssh "$LOCAL/data/$f" "$HOST:~/$REMOTE/data/$f"
    fi
  done
  echo "seeded .env + data (kept at 0600)"
else
  echo "already present — left alone"
fi

say "3/6 systemd unit"
rsync -az -e ssh "$LOCAL/deploy/$SERVICE.service" "$HOST:~/apps/$SERVICE.service.tmp"
ssh "$HOST" "install -D -m 644 ~/apps/$SERVICE.service.tmp ~/.config/systemd/user/$SERVICE.service && rm -f ~/apps/$SERVICE.service.tmp && systemctl --user daemon-reload"

ssh "$HOST" "cp ~/$REMOTE/deploy/DEPLOY.md ~/$REMOTE/DEPLOY.md"

say "4/6 venv + coincurve (spectator signing)"
ssh "$HOST" "cd ~/$REMOTE && { test -x .venv/bin/python || python3 -m venv .venv; } && .venv/bin/pip install --quiet --upgrade pip >/dev/null && .venv/bin/pip install --quiet -r requirements.txt"

say "5/6 restart"
ssh "$HOST" "systemctl --user enable --now $SERVICE >/dev/null && systemctl --user restart $SERVICE"

say "6/6 health"
sleep 4
ssh "$HOST" "systemctl --user is-active $SERVICE"
ssh "$HOST" "cd ~/$REMOTE && .venv/bin/python bot.py --check"
ssh "$HOST" "journalctl --user -u $SERVICE -n 6 --no-pager -o cat"

if [ "${1:-}" = "--logs" ]; then
  say "following journal (ctrl-c to stop)"
  ssh -t "$HOST" "journalctl --user -u $SERVICE -f -o cat"
fi
