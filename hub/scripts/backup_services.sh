#!/usr/bin/env bash
set -euo pipefail
DEST="${SERVICES_BACKUP_DIR:-/srv/backups}"
KEEP="${SERVICES_BACKUP_KEEP:-14}"
IMMICH_DB="${IMMICH_DB_CONTAINER:-immich_postgres}"
PAPERLESS="${PAPERLESS_CONTAINER:-paperless-webserver-1}"
PAPERLESS_EXPORT="${PAPERLESS_EXPORT_DIR:-/srv/paperless/export}"
IMMICH_LIBRARY="${IMMICH_LIBRARY_DIR:-/srv/immich/library}"
MINECRAFT_ROOT="${MINECRAFT_ROOT:-/srv/minecraft}"
DELETED_WORLD_DAYS="${DELETED_WORLD_DAYS:-7}"
PHOTO_KEEP="${PHOTO_KEEP:-14}"
WORLD_KEEP="${WORLD_KEEP:-7}"
stamp="$(date +%Y%m%d-%H%M%S)"
made=()

prune() {
  ls -1t "$1"/$2 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f
}

if docker inspect "$IMMICH_DB" >/dev/null 2>&1; then
  mkdir -p "$DEST/immich"
  part="$DEST/immich/.immich-db-$stamp.sql.gz.part"
  docker exec "$IMMICH_DB" pg_dumpall --clean --if-exists --username=postgres | gzip -6 > "$part"
  gzip -t "$part"
  if ! zgrep -q "^CREATE DATABASE immich" "$part" || ! zgrep -q -E "^CREATE TABLE public\.\"?assets?\"? " "$part"; then
    rm -f "$part"
    echo "Immich dump is missing the immich database or its assets table" >&2
    exit 1
  fi
  mv "$part" "$DEST/immich/immich-db-$stamp.sql.gz"
  prune "$DEST/immich" "immich-db-*.sql.gz"
  made+=("immich $(du -h "$DEST/immich/immich-db-$stamp.sql.gz" | cut -f1)")
fi

if docker inspect "$PAPERLESS" >/dev/null 2>&1; then
  mkdir -p "$DEST/paperless"
  docker exec "$PAPERLESS" document_exporter ../export --zip --zip-name "paperless-$stamp" >/dev/null
  zip="$PAPERLESS_EXPORT/paperless-$stamp.zip"
  python3 -c "import sys, zipfile; z = zipfile.ZipFile(sys.argv[1]); assert z.testzip() is None and 'manifest.json' in z.namelist()" "$zip"
  mv "$zip" "$DEST/paperless/"
  prune "$DEST/paperless" "paperless-*.zip"
  made+=("paperless $(du -h "$DEST/paperless/paperless-$stamp.zip" | cut -f1)")
fi

CONFIG_PATHS=(etc/wireguard srv/adguard/compose.yml srv/adguard/conf srv/isponsorblocktv/compose.yml srv/isponsorblocktv/pair.py
  srv/isponsorblocktv/data srv/immich/.env srv/immich/docker-compose.yml srv/immich/docker-compose.override.yml
  srv/paperless/docker-compose.yml srv/paperless/docker-compose.env)
present=()
for path in "${CONFIG_PATHS[@]}"; do
  sudo -n test -e "/$path" && present+=("$path")
done
if [ "${#present[@]}" -gt 0 ]; then
  mkdir -p "$DEST/config"
  chmod 700 "$DEST/config"
  part="$DEST/config/.config-$stamp.tar.gz.part"
  sudo -n tar -czf "$part" -C / "${present[@]}"
  sudo -n chown "$(id -u):$(id -g)" "$part"
  chmod 600 "$part"
  tar -tzf "$part" >/dev/null
  mv "$part" "$DEST/config/config-$stamp.tar.gz"
  prune "$DEST/config" "config-*.tar.gz"
  made+=("config $(du -h "$DEST/config/config-$stamp.tar.gz" | cut -f1)")
fi
if sudo -n test -d "$IMMICH_LIBRARY"; then
  photos="$DEST/immich-photos"
  sudo -n mkdir -p "$photos"
  latest="$(sudo -n find "$photos" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | tail -n 1)"
  sudo -n rsync -a --delete ${latest:+--link-dest="$latest"} --exclude thumbs/ --exclude encoded-video/ --exclude backups/ \
    "$IMMICH_LIBRARY/" "$photos/.$stamp.part/"
  sudo -n mv "$photos/.$stamp.part" "$photos/$stamp"
  sudo -n find "$photos" -mindepth 1 -maxdepth 1 -type d -name '20*' | sort | head -n -"$PHOTO_KEEP" | xargs -r sudo -n rm -rf --
  made+=("photos $(sudo -n find "$photos/$stamp" -type f | wc -l) files")
fi
if [ -d "$MINECRAFT_ROOT" ]; then
  mkdir -p "$DEST/minecraft"
  for server in "$MINECRAFT_ROOT"/*/; do
    slug="$(basename "$server")"
    container="mc-$slug"
    running=0
    if docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null | grep -q true; then
      running=1
      docker exec "$container" rcon-cli save-off >/dev/null
      docker exec "$container" rcon-cli save-all flush >/dev/null
    fi
    part="$DEST/minecraft/.$slug-$stamp.tar.gz.part"
    status=0
    sudo -n tar -czf "$part" -C "$MINECRAFT_ROOT" "$slug" || status=$?
    [ "$running" = 1 ] && docker exec "$container" rcon-cli save-on >/dev/null
    [ "$status" = 0 ] || { sudo -n rm -f "$part"; echo "Minecraft $slug archive failed" >&2; exit 1; }
    sudo -n chown "$(id -u):$(id -g)" "$part"
    tar -tzf "$part" >/dev/null
    mv "$part" "$DEST/minecraft/$slug-$stamp.tar.gz"
    ls -1t "$DEST/minecraft/$slug-"*.tar.gz 2>/dev/null | tail -n +"$((WORLD_KEEP + 1))" | xargs -r rm -f
    made+=("minecraft $slug $(du -h "$DEST/minecraft/$slug-$stamp.tar.gz" | cut -f1)")
  done
  find "$DEST/minecraft" -maxdepth 1 -name '*.tar.gz' -mtime +"$DELETED_WORLD_DAYS" -print0 | while IFS= read -r -d '' archive; do
    slug="$(basename "$archive" | sed -E 's/-[0-9]{8}-[0-9]{6}\.tar\.gz$//')"
    [ -d "$MINECRAFT_ROOT/$slug" ] || rm -f "$archive"
  done
fi
echo "safety copies $stamp: ${made[*]:-nothing to copy}"
