#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cfg="$HOME/.config/standup-bot/config.env"
units="$HOME/.config/systemd/user"

mkdir -p "$(dirname "$cfg")" "$units"
if [[ ! -f "$cfg" ]]; then
  install -m 600 "$here/config.example.env" "$cfg"
  echo "Configuración creada en $cfg — añade TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID."
fi

# El .service se genera con la ruta real del clon (puede no estar en ~/Documents).
sed "s|%h/Documents/standup-bot|$here|" "$here/systemd/standup-bot.service" > "$units/standup-bot.service"
ln -sf "$here/systemd/standup-bot.timer" "$units/"
systemctl --user daemon-reload
systemctl --user enable --now standup-bot.timer

# Permite que los timers de usuario corran aunque no hayas iniciado sesión.
loginctl enable-linger "$USER"

systemctl --user list-timers standup-bot.timer
