# CLAUDE.md

This repository is a home hub kit. `MASTER-PROMPT.md` is your full brief: read it before any setup work, and follow its phases with the owner. `README.md` holds this house's facts, recovery guide and lessons. Keep them current, and keep secrets out of them. These three files and this one are the only Markdown files; put new documentation in one of them.

## Layout

- `hub/`: the `homehub` package (the whole program), `hub/tests/`, `hub/scripts/` (setup, systemd units, backups, DuckDNS, PC agent installer) and `hub/config/`. The config folder holds example files; the real `config.toml` and `people.toml` sit beside them and are gitignored.
- `proxmox/`: `hub-control` (the forced-command script for the hub's SSH key) and `hub-token.sh` (the narrow API token). Keep `hub-control` identical to `CONTROL_SCRIPT` in `hub/homehub/tools/proxmox.py`.
- On the hub the checkout is `~/home-hub-kit`, the venv is `hub/.venv`, data is `~/.local/share/homehub/` (`homehub.db`, `secrets.toml`, backups, sign-in tokens) and state is `~/.local/state/homehub/` (logs, `status.json`).

## Commands (from `hub/`)

```bash
.venv/bin/python -m pytest -q -m "not claude and not slow"   # fast suite
.venv/bin/python -m pytest -q                                 # everything, including real Claude calls
.venv/bin/homehub doctor
.venv/bin/homehub status | logs -f | backup [--list | --restore FILE]
.venv/bin/homehub notify "text"
.venv/bin/homehub vpn list | vpn add NAME --person ID | vpn remove NAME
.venv/bin/homehub people list | people import --file config/people.toml
.venv/bin/homehub test [stages] [--live] [--claude]           # one area by name
.venv/bin/homehub phone list | phone pair PERSON
.venv/bin/homehub spotify login | google-tasks login | packages login | ps5 pair
scripts/install_service.sh                                    # installs every user unit with real paths
```

## Deploying to the hub

1. From the laptop: `git push <user>@<hub>:home-hub-kit main:refs/heads/incoming`.
2. On the hub: `git merge --ff-only incoming && git branch -d incoming`.
3. If dependencies or units changed: `hub/.venv/bin/pip install -e hub` and `hub/scripts/install_service.sh`.
4. `systemctl --user restart homehub`, then `hub/.venv/bin/homehub status`.

## Rules (non-negotiable)

- No piracy tooling of any kind: no torrent clients, indexers, arr apps or download bots. Decline and offer legal routes.
- No comments or commented-out code. No placeholders, mock data, fake services or fake clocks; tests use real things and skip when a device or secret is absent.
- Never commit secrets. Logins and keys go in `~/.local/share/homehub/secrets.toml` on the hub.
- Never delete or overwrite the owner's data (database, `config.toml`, `people.toml`, `secrets.toml`, backups, photos, documents, media) or anything they set up by hand.
- Get the owner's explicit OK in the session before you:
  - change router settings or scan the network;
  - pair devices;
  - send messages, emails or calendar changes;
  - buy anything;
  - play sound.
- Before testing playback on a shared TV, check it's idle.
- Never set DNS in a phone's WireGuard tunnel.
- One feature at a time, fully tested on the real device before it gets a button. Report failures honestly.
- Commit as the owner's own GitHub identity. Push when a feature is finished and tested, or when the owner asks.
- Keep turns cheap; the Claude plan is the owner's. Don't spawn many agents.
