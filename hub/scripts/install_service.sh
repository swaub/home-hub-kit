#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
for unit in homehub.service homehub-backup.service homehub-backup.timer hub-services-backup.service hub-services-backup.timer duckdns-update.service duckdns-update.timer cert-renew.service cert-renew.timer notify-failure@.service; do
  sed "s|REPO_DIR|$REPO|g" "$REPO/scripts/$unit" > "$UNIT_DIR/$unit"
done
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now homehub
systemctl --user enable --now homehub-backup.timer
systemctl --user enable --now hub-services-backup.timer
if grep -q "^\[duckdns\]" "$HOME/.local/share/homehub/secrets.toml" 2>/dev/null; then systemctl --user enable --now duckdns-update.timer; fi
if [ -x "$HOME/.acme.sh/acme.sh" ]; then systemctl --user enable --now cert-renew.timer; fi
systemctl --user --no-pager status homehub | head -20
systemctl --user list-timers homehub-backup.timer --no-pager
echo
echo "Installed. Useful commands:"
echo "  systemctl --user status homehub        service state"
echo "  journalctl --user -u homehub -f        live log"
echo "  $REPO/.venv/bin/homehub status         assistant status"
echo "  $REPO/.venv/bin/homehub logs -f        the assistant's own log"
