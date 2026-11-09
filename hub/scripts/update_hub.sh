#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
BEFORE="$(git rev-parse --short HEAD)"
git pull --ff-only
.venv/bin/pip install -e . --quiet
if systemctl --user is-enabled homehub >/dev/null 2>&1; then
  systemctl --user restart homehub
fi
echo "updated from $BEFORE to $(git rev-parse --short HEAD); roll back with: git checkout $BEFORE && systemctl --user restart homehub"
.venv/bin/homehub status || true
