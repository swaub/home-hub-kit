#!/usr/bin/env bash
set -euo pipefail
SECRETS="${HOMEHUB_SECRETS:-$HOME/.local/share/homehub/secrets.toml}"
VPN_ADDRESS="${VPN_ADDRESS:-10.77.0.1}"
read -r token home app < <(python3 -c '
import sys, tomllib
table = tomllib.load(open(sys.argv[1], "rb")).get("duckdns") or {}
print(table.get("token", "-"), table.get("home", "-"), table.get("app", "-"))' "$SECRETS")
[ "$token" != "-" ] || { echo "no [duckdns] token in $SECRETS" >&2; exit 1; }
failed=0
update() {
  local answer
  answer="$(printf 'url = "https://www.duckdns.org/update?domains=%s&token=%s&ip=%s"\n' "$1" "$token" "$2" | curl -fsS --max-time 20 -K - || echo "no answer")"
  [ "$answer" = "OK" ] || { echo "DuckDNS refused $1: $answer" >&2; failed=1; }
}
[ "$home" = "-" ] || update "$home" ""
[ "$app" = "-" ] || update "$app" "$VPN_ADDRESS"
for name in $(python3 -c '
import glob, json, sys
for path in glob.glob(sys.argv[1] + "/*/server.json"):
    host = json.load(open(path)).get("host", "")
    if host.endswith(".duckdns.org"):
        print(host[:-len(".duckdns.org")])' "${MINECRAFT_ROOT:-/srv/minecraft}"); do
  update "$name" ""
done
exit "$failed"
