#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
WITH_KDECONNECT=0
for arg in "$@"; do
  case "$arg" in
    --with-kdeconnect) WITH_KDECONNECT=1 ;;
    *) echo "unknown option $arg (use --with-kdeconnect to install the phone bridge)"; exit 2 ;;
  esac
done
if [ "$(uname -s)" != "Linux" ]; then
  echo "setup_hub.sh is for the Linux hub; it needs Debian or Ubuntu"; exit 2
fi
if command -v apt-get >/dev/null; then
  PACKAGES="python3-venv python3-dev build-essential curl git rsync avahi-daemon libnss-mdns"
  [ "$WITH_KDECONNECT" = 1 ] && PACKAGES="$PACKAGES kdeconnect"
  sudo apt-get update
  sudo apt-get install -y $PACKAGES
fi
if [ -d /etc/NetworkManager/conf.d ]; then
  printf "[connection]\nwifi.powersave = 2\n" | sudo tee /etc/NetworkManager/conf.d/homehub-wifi-powersave-off.conf >/dev/null
  sudo systemctl reload NetworkManager
fi
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e ".[kdeconnect,router,cast,ps5,psn,lights]"
[ -f config/config.toml ] || cp config/config.example.toml config/config.toml
[ -f config/people.toml ] || cp config/people.example.toml config/people.toml
if ! command -v claude >/dev/null; then
  curl -fsSL https://claude.ai/install.sh | bash
fi
echo
echo "Setup done. Next:"
echo "  1. claude auth login                   (pick the Claude subscription account, not an API key)"
echo "  2. edit config/config.toml and config/people.toml for this house"
echo "  3. .venv/bin/homehub doctor            everything should be OK or SKIP"
echo "  4. .venv/bin/homehub run               then open the private app over the VPN"
echo "  5. scripts/install_service.sh          start on boot with the nightly backup timer"
