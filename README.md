# Home Hub Kit

A private home hub you build with Claude Code. It gives you:

- a VPN, so your phone reaches home from anywhere;
- a private phone app with chat, TV and Roku remotes, Spotify, Jellyfin, photos, documents, lists, timers, package tracking and ad-blocking controls;
- Jellyfin, Immich, Paperless, Syncthing and AdGuard Home on your own hardware, with easy names and nightly backups.

It comes from a house where it runs every day, with the personal parts removed. Nothing in it downloads media you don't own, and it has no camera or voice parts.

## Getting started

1. You need:
   - an always-on mini PC or old laptop for the hub;
   - access to your router's settings;
   - a Claude Pro or Max subscription;
   - a free GitHub account and a free DuckDNS account.
2. Install Claude Code on your laptop (https://claude.com/claude-code). On Windows, install WSL first and use Claude Code inside it.
3. Fork or clone this repository, open a terminal in its folder and run `claude`.
4. Say: **"Read MASTER-PROMPT.md and help me set up my home hub from the start."**

Claude interviews you about your house first, then builds everything with you one step at a time. It asks before it changes your router, opens a port or touches any device.

## What's here

| Path | What it is |
|---|---|
| `MASTER-PROMPT.md` | Claude's full brief: every phase, rule and known pitfall |
| `CLAUDE.md` | The rules Claude Code loads by itself in this folder |
| `hub/` | The hub program (`homehub`), its tests, setup scripts, service files and example config |
| `proxmox/` | Optional: scripts that give the hub narrow, safe control of a Proxmox server |

## This house

Claude fills this in as you build: addresses, devices, what runs where, where each secret is kept (never the secrets themselves), and a plain-language recovery guide for a lost phone, a new router or a dead disk.

## Lessons

Claude adds a dated line here whenever something surprising costs time.
