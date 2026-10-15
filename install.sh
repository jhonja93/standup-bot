#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cfg="$HOME/.config/standup-bot/config.env"
units="$HOME/.config/systemd/user"

mkdir -p "$(dirname "$cfg")" "$units"
if [[ ! -f "$cfg" ]]; then
  install -m 600 "$here/config.example.env" "$cfg"
  echo "Config created at $cfg — add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID."
fi

# Generate the .service with the clone's real path (it may not live in ~/Documents).
sed "s|%h/Documents/standup-bot|$here|" "$here/systemd/standup-bot.service" > "$units/standup-bot.service"
ln -sf "$here/systemd/standup-bot.timer" "$units/"
systemctl --user daemon-reload
systemctl --user enable --now standup-bot.timer

# Let user timers run even when you're not logged in.
loginctl enable-linger "$USER"

systemctl --user list-timers standup-bot.timer
