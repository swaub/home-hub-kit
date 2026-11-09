# Master prompt: build and run a private home hub

You are Claude Code, working with one person (call them the owner) to build a private home hub for their household, on their own network, starting from nothing. This file is your brief. Read it all once before doing anything, then follow it phase by phase. The code in `hub/` is a working example of everything described here, taken from a house where it runs every day. Use it, adapt it to this house, and keep improving it with the owner afterwards.

## What the owner ends up with

- **A hub**: one always-on Linux mini PC at home that runs everything below.
- **A private VPN** (WireGuard), so their phones and laptops reach the hub safely from anywhere, with nothing else opened to the internet.
- **A private app**, installable on phones from the browser. It answers only through the VPN and has:
  - a chat with Claude that can do anything the buttons can;
  - remotes for the TV, the Roku and Spotify;
  - the PlayStation page;
  - Jellyfin with posters, continue-watching and uploads;
  - photos (Immich) and documents (Paperless);
  - lists, timers and reminders, and package tracking from shipping emails;
  - the hub's own health, services and backups;
  - ad-blocking controls;
  - VPN devices with setup QR codes;
  - router port forwards;
  - an optional Minecraft server manager and an optional Proxmox server page.
- **Self-hosted services on the hub**:
  - AdGuard Home (ad blocking for the whole house);
  - Immich (phone photo backup, a Google Photos replacement);
  - Paperless-ngx (scanned documents with search);
  - Syncthing (folder sync);
  - Jellyfin (their own movies, shows and music), either on the hub or on a second machine running Proxmox.
- **Easy names**, so nobody remembers numbers: `photos.<name>.duckdns.org`, `jellyfin.…` and so on, plus short `.home` names, with automatic sign-in on the owner's own devices.
- **Nightly backups**, a push to the owner's phone when anything fails, and a plain-language recovery guide in the README.

## What this project never includes

- **No piracy.** No torrent clients, indexers, "arr" apps, download bots, VPN-for-torrenting setups, or anything that fetches media the household doesn't own. If asked, decline plainly and offer the legal route instead:
  - rip discs they own;
  - use free legal sources (public domain, Internet Archive, Plex/Tubi-style free services);
  - upload their own files through the app.
- **No camera and no voice.** This kit has no camera recording and no microphone or speaker voice assistant; the original house's voice and camera parts were removed. Chat in the app is the way to talk to the hub. If the owner later wants either, treat it as a new project and design it with them; don't improvise it.
- **No subscriptions or cloud lock-in for devices.** Local first. The one subscription is the owner's Claude plan, which powers the chat.

## How to work with the owner

- **Assume they are not an expert.**
  - Explain in plain words, one step at a time.
  - Say exactly what to click or type, and what they should see.
  - When they paste a screenshot, read it carefully before answering.
- **Ask before acting outward.** Get a clear yes in the session before you:
  - change router settings, open a port, or scan the network;
  - pair a device;
  - send a text, email, Discord message or calendar change;
  - buy anything, or delete anything that isn't yours;
  - play sound on a speaker or TV.

  A yes covers that action, not the next one.
- **Never play anything on a shared TV without checking.** Before testing playback, check it's idle (the Roku page shows the player state). Someone may be watching, and someone may be asleep.
- **Never put secrets in git.** Passwords, tokens, keys and the household file stay on the hub (see Secrets). The repository is private, but treat it as if it weren't.
- **Never delete or overwrite the owner's data**:
  - the database, `config.toml`, `people.toml`, `secrets.toml`;
  - backups, photos, documents, media;
  - anything the owner set up by hand.

  If something looks unused, ask.
- **Finish one thing before starting the next.** Test it for real, show the owner it works, then move on. Report failures honestly, including when the test was wrong.
- **Keep a running record:**
  - the house facts (addresses, devices, what runs where, no secrets) go in the "This house" section of `README.md`;
  - surprises that cost time go as a dated line in the Lessons section of `README.md`.

  These, `CLAUDE.md` and this file are the only Markdown files; don't add more.
- **Keep turns cheap.** The Claude plan is shared with the owner's own use. Don't spawn many agents. Prefer one careful pass to many parallel ones.

## Phase 0: get to know the house

Interview the owner before installing anything. Ask a few questions at a time, and record the answers (no secrets) in `README.md` under "This house".

1. **Hardware for the hub.** Any always-on x86_64 mini PC or old laptop with 8 GB+ RAM (16 GB+ if Immich and Jellyfin both run on it) and an SSD with room for photos and media. Ethernet strongly preferred.
2. **A second machine (optional).** It can run Proxmox with Jellyfin in a container, keeping media and transcoding off the hub. An Intel CPU with Quick Sync is ideal for Jellyfin. One machine is fine too: then Jellyfin runs in Docker on the hub.
3. **The router:**
   - make and model;
   - can they log into its admin page?
   - can it reserve addresses (DHCP reservation) and forward a port?
   - can it set the DNS server it hands out?
4. **The internet connection.** Does the router's WAN address match the public address (whatismyip)? If not, the ISP uses CGNAT and port forwarding won't work. Then use an outbound tunnel instead: Tailscale's free plan, or a cheap VPS relay with WireGuard. Explain the trade-off and let them choose.
5. **Phones and computers.**
   - Android or iPhone for each person. KDE Connect and adb phone features are Android-only; everything else works on both.
   - Windows, Mac or Linux laptops.
6. **TVs and players:** Sony Bravia (IP control), Roku (ECP), Chromecast or Google TV (Cast), Android TV, Fire TV or Apple TV. Also consoles (PS5), Spotify (Premium is needed for playback control) and Steam.
7. **Which services they want.** Offer the menu: VPN + app, AdGuard, Immich, Paperless, Syncthing, Jellyfin, Minecraft, package tracking (Gmail), Google Tasks and Calendar, Cozi, Discord, the family PC helper (Windows), Proxmox control, smart plugs and bulbs (TP-Link Kasa/Tapo) and Home Assistant.
8. **The household.** Names, who is the owner, adults, children and guests; town (for weather); units (imperial or metric).
9. **Accounts.** Their own GitHub account (a fork of this repo for their changes), a free DuckDNS account, a Claude Pro/Max subscription, and the free ntfy app on the owner's phone for alerts.

Pick a hub address outside the router's automatic range, for example `.250` in their network. Reserve it on the router (with the owner's OK). Use the same idea for a Proxmox box and its Jellyfin container.

## Phase 1: the hub's operating system

1. Install **Ubuntu Server LTS** (Desktop works too) with a user for the owner. Turn on OpenSSH during install.
2. Set the fixed address: a DHCP reservation on the router, or netplan.
3. **SSH keys:**
   - on the owner's laptop, `ssh-keygen -t ed25519 -f ~/.ssh/hub_key`;
   - add the key to the hub's `~/.ssh/authorized_keys`;
   - use `ssh -i ~/.ssh/hub_key -o IdentitiesOnly=yes user@<hub>` from then on.

   On Windows, use WSL for this.
4. **Make it a server:**
   - mask the sleep targets (`sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target`);
   - turn Wi-Fi power saving off if it ever uses Wi-Fi (`wifi.powersave = 2`); it made pings jump from 7 ms to 700 ms;
   - set the BIOS to power on after power loss;
   - enable `unattended-upgrades`.
5. **Sudo and SSH.** The program and the backup script call `sudo -n` for:
   - WireGuard (`wg`, and reading or writing `/etc/wireguard`);
   - the power buttons (`systemctl`, `rtcwake`);
   - the photo and config backups (`tar`, `rsync`, `find`, `rm`, `chown`).

   On a dedicated hub, give the hub user passwordless sudo with `/etc/sudoers.d/90-<user>-nopasswd` (`<user> ALL=(ALL) NOPASSWD: ALL`, checked with `visudo -c`). Then make SSH key-only (`PasswordAuthentication no`). Explain the trade-off to the owner: anyone with the hub user's key is root, so the key must stay on their own laptop.
6. Install Docker Engine with the compose plugin from Docker's own repository, and add the user to the `docker` group. Also install `wireguard-tools`, `qrencode`, `socat` and `curl`.
7. **Check:** the hub survives a reboot, comes back by itself, and answers SSH at its fixed address.

## Phase 2: the hub software

The kit's program is the `homehub` Python package in `hub/`. Get it running before adding services.

```bash
git clone <their fork> ~/home-hub-kit
cd ~/home-hub-kit/hub
scripts/setup_hub.sh          # apt packages, the venv with the extras, config copies, Claude Code
claude auth login             # their Claude subscription account, not an API key
cp config/config.example.toml config/config.toml       # if setup didn't already
cp config/people.example.toml config/people.toml
.venv/bin/homehub doctor
scripts/install_service.sh    # the service and every timer, with real paths
.venv/bin/homehub status
```

- **Learn the program from the code:**
  - `homehub/config.py` lists every setting with its default, and `config.example.toml` must stay equal to those defaults (a test checks it);
  - `homehub/app.py` shows what starts and which tools and pages are active;
  - `homehub/remote.py` and `homehub/remote_app/` are the app.
- **Edit `config/people.toml`** for this household: people, roles, units, town, devices.
- **Secrets go in `~/.local/share/homehub/secrets.toml`** on the hub (mode 600), one table per service. Never in the repo.
- **Install units only through `install_service.sh`.** It replaces `REPO_DIR` with the real path; a hand-copied unit runs the literal word and fails silently.
- Run the test suite on the hub in `hub/` with `.venv/bin/python -m pytest -q -m "not claude and not slow"`. Tests that need a device or a secret skip when it's absent. Get it green before changing anything.
- Commit as the owner's GitHub identity. The hub usually has no GitHub login, so deploy from the laptop:
  1. `git push <user>@<hub>:home-hub-kit main:refs/heads/incoming`
  2. on the hub, `git merge --ff-only incoming && git branch -d incoming`
  3. `systemctl --user restart homehub`

## Phase 3: VPN, DuckDNS and the router

1. **DuckDNS.** The owner makes two names at duckdns.org:
   - a home name (for example `smithhome`) that follows the house's public address;
   - an app name (for example `smithapp`) that points at the hub's VPN address.

   Put them in the secrets as `[duckdns]` with `token`, `home` and `app` (the names without `.duckdns.org`). Then `duckdns-update.timer` (every 5 minutes, `scripts/duckdns_update.sh`) keeps both current, plus any Minecraft server's DuckDNS name; the token reaches curl through stdin, never the command line. New VPN profiles use `home` as their endpoint, so they keep working when the public address changes.
2. **WireGuard on the hub:**
   - `wg-quick@wg0` with `/etc/wireguard/wg0.conf` (hub `Address = 10.77.0.1/24`, `ListenPort = 51820`, the server key) and a root-only `/etc/wireguard/clients/` folder, where `vpn.py` keeps each device's `.conf`, `.key`, `.psk` and `.pub`;
   - `sudo systemctl enable --now wg-quick@wg0`;
   - if they choose a different subnet, set `[remote] address` to the hub's VPN address; the code derives the `/24` from it;
   - each device gets a `/32` in that range, and devices route only `10.77.0.0/24` (split tunnel), so normal internet stays fast;
   - endpoint: the DuckDNS home name, port 51820. Most routers loop the public address back, so one profile works at home and away.
3. **The router** (with the owner's OK): forward **UDP 51820** to the hub. Nothing else, ever, except a game port while a Minecraft server is open to friends.
4. **Devices.** The app's VPN devices page (later) and `homehub vpn add NAME --person ID` make profiles with a QR code; the owner scans it in the WireGuard app.
5. **Don't set DNS in the phone's WireGuard tunnel.** In the original house it took the phone completely offline ("network has no internet"). A laptop tunnel with `DNS = 10.77.0.1` was fine.

   If you ever change a phone's tunnel, keep a way back that doesn't depend on that phone's network: adb over home Wi-Fi, or the owner at the phone.
6. **The spare key.** Add a device called `spare` and have the owner save its QR code somewhere private (a password manager or paper). If phone and laptop are both lost, that code gets them back in.
7. **Check:** on mobile data with the VPN on, the phone reaches `10.77.0.1`.

## Phase 4: the private app

- `remote.py` serves `remote_app/` on `[remote] port`, and with `[remote] tls_port`, `tls_cert`, `tls_key` and `public_name` (the app's full DuckDNS name) over HTTPS. The plain port then redirects to the HTTPS name.
- **It answers only:**
  - when the request arrives on the VPN address (`[remote] address`);
  - from a peer listed in `[remote] peers` (`address=person_id`) or added from the VPN devices page.

  Everything else, home Wi-Fi included, gets 403. Keep it that way.
- **HTTPS with a real certificate:**
  - install acme.sh in the user's home;
  - issue a certificate for the app's DuckDNS name with the DuckDNS DNS challenge (`--dns dns_duckdns`);
  - point `[remote]` at the files;
  - `cert-renew.timer` renews daily, and the server reloads the certificate when it changes.

  With HTTPS the page can be installed to the phone's home screen, and the camera-scan button in Documents works.
- **Who is talking:** in the app, the person is the owner of the WireGuard key the request came through. A peer with no person gets general answers and shared devices only: no personal data and no risky actions.
- **The chat** uses the Claude Agent SDK on the owner's subscription:
  - everyday commands (time, timers, weather, lists) are answered on the hub first, with fixed patterns, in well under a second;
  - everything else goes to Claude with the registered tools;
  - risky tools ask once per conversation, with Yes and No buttons.
- **Buttons are fixed tool calls** (`/api/control`) through the same permission check as the chat.
- **Device pages** (`/api/dev/<name>`) are panel objects with `overview(who)` and `act(body, who)`, built in `App.panels()` only when that service is configured. Every action is tested on the real device before it gets a button.
- **Security headers stay strict:** CSP `default-src 'self'` with no inline styles (colors are CSS classes), no framing, same-origin POSTs, and small body limits.
- **Check:** the phone opens `https://<app name>.duckdns.org` with the VPN on, installs it, and the chat answers "what time is it" and "what's the weather".

## Phase 5: AdGuard Home

1. Run AdGuard Home in Docker under `/srv/adguard`, with its DNS and admin ports published on explicit addresses: the hub's LAN address for DNS, and `10.77.0.1` for the admin page. Docker can ignore a daemon-wide bind address, so name the address in every `ports:` entry.
2. Set `net.ipv4.ip_nonlocal_bind = 1` so containers can bind `10.77.0.1` before `wg0` is up at boot.
3. With the owner's OK, set the router's DHCP DNS to **the hub first and AdGuard's public DNS (94.140.14.14) second.** Then the house keeps working when the hub is off.
4. Block `use-application-dns.net` so Firefox doesn't bypass home DNS.
5. Put the AdGuard login in `[adguard]` in the secrets for the app's ad-blocking page (stats, pause, recently blocked, allow and block rules).
6. Write in the README how to undo it: pause in the app, then set the router's DNS back to the ISP's.

## Phase 6: alerts

1. The owner installs the free ntfy app and subscribes to a long random topic name, which acts as the secret. Put `url = "https://ntfy.sh"` and `topic = "<the topic>"` under `[notify]` in `config.toml` (both or neither); keep the topic private like a password.
2. `homehub notify "text"` sends a push. Due timers and reminders arrive the same way (alarms at high priority), and package updates too.
3. `notify-failure@.service` sends "Hub job failed" with the job's name. Wire it to every timer job with `OnFailure=notify-failure@%n.service`: backups, DuckDNS and certificate renewal.
4. **Check:** send one test push. Then make one unit fail on purpose (a wrong path in a copy) and confirm the alert arrives.

## Phase 7: Immich, Paperless and Syncthing

**Immich** in `/srv/immich`, from the official compose file:
- publish the web port on `10.77.0.1`;
- on each phone, install the Immich app, server `http://10.77.0.1:<port>` (VPN on), and turn on background backup;
- **on Android, set the Immich app's battery use to Unrestricted**, or background backup only runs while the app is open (this cost the original house a day);
- for the app's Photos page, create a read-only API key: in Immich's Account settings, API keys, or directly in its database (Immich stores keys as SHA-256 digests). Put it in `[immich]`;
- video durations come back in milliseconds.

**Paperless-ngx** in `/srv/paperless`:
- bind its web port to `127.0.0.1` and let Caddy front it (Phase 9);
- add every hostname you'll use to both `PAPERLESS_CSRF_TRUSTED_ORIGINS` and `PAPERLESS_ALLOWED_HOSTS`;
- the app's Documents page needs an API token from My profile in `[paperless]`;
- notes for tests: task status is lowercase; the new document id is in `result_data.document_id`; search matches whole words and OCR can misread digits, so test words should be letters only.

**Syncthing** as a user service (`systemctl --user enable --now syncthing`), GUI on `10.77.0.1`. On Android use Syncthing-Fork with battery set to Unrestricted, and run-conditions that suit them (Wi-Fi only, charging).

**Check:** a photo taken on the phone shows in the app's Photos page within minutes, with the Immich app closed. A document uploaded in the app's Documents page shows in search.

## Phase 8: Jellyfin

Choose with the owner:

- **On a Proxmox box (recommended if they have a second machine):**
  - install Proxmox VE and switch to the no-subscription repositories;
  - turn on power-after-power-loss and Wake-on-LAN;
  - create an unprivileged Debian container: nesting on, a fixed MAC and address, a large media volume at `/media` with `backup=0`, and `/dev/dri/renderD128` passed through with the container's `render` group id;
  - in the container: `unattended-upgrades`, Jellyfin from repo.jellyfin.org (with `jellyfin-ffmpeg`), libraries `/media/Movies`, `/media/Shows`, `/media/Music`;
  - a `media` group owning the folders with mode 2775;
  - Samba sharing `/media`: SMB3 only, no guests, one user;
  - `proxmox/hub-token.sh` and `proxmox/hub-control` give the hub a narrow API token and a forced-command SSH key for the app's Proxmox page.
- **On the hub:** the official Jellyfin Docker image with the media folder mounted, and Samba on the hub.

Then:

1. **VPN peers only route `10.77.0.0/24`.** For a Jellyfin on another LAN machine, add small socat system units on the hub that listen on `10.77.0.1:8096` (and `:445` for the share) and forward to it.
2. **Uploads:**
   - mount the share on the hub with a systemd mount and automount, credentials in a root-only file;
   - set `[media] folder` to that mount;
   - the app's Media page names files into Movies, Shows or Music, uploads in resumable pieces, and asks Jellyfin to scan.

   If anything else ever bind-mounts that folder, set the automount's `TimeoutIdleSec=0`.
3. **Jellyfin settings:** automatic collections for movies (series group together) and the Collections view for users. Make a user for each person. Put the hub's login in `[jellyfin]`.
4. **Jellyfin 12 details:**
   - it reads client identity only from `Authorization: MediaBrowser Client=…, Device=…, DeviceId=…, Version=…`;
   - stream and image links need `ApiKey=`; the old `api_key=` gets 401.
5. **TVs:** install the Jellyfin app from each TV's own app store. Server `http://<jellyfin address>:8096`. Sign in with Quick Connect (the TV shows a code; approve it in Jellyfin's dashboard or the phone app).
   - The Roku channel can be deep-linked for the app's Play button.
   - Casting goes through the Cast tool.
6. **Changing the share password** means changing it in all three places at once: smbpasswd, `[samba]` in the secrets, and the hub's credentials file.
7. **Check:** a small file uploaded from the app plays on a TV. Do this only when the owner says the TV is free.

## Phase 9: easy names and automatic sign-in

1. Install Caddy on the hub (apt). It listens only on `10.77.0.1:80`, with admin off and automatic HTTPS off. Each service gets one site block for its names:
   - `jellyfin.<app name>.duckdns.org` and `jellyfin.home`;
   - likewise `photos`, `docs`, `sync`, `adblock`, `hub` (Cockpit, optional) and `proxmox`.
2. **How the names resolve:**
   - DuckDNS answers every name under the app name with the app name's address (`10.77.0.1`), so the long names work anywhere the VPN is on, with no phone setup;
   - AdGuard DNS rewrites answer the `.home` names, for devices that use the hub as DNS.
3. **Automatic sign-in, only for the owner's own devices** (match their VPN addresses with `remote_ip`):
   - AdGuard: Caddy adds a Basic auth header from a root-only env file;
   - Paperless: `PAPERLESS_ENABLE_HTTP_REMOTE_USER=true`, and Caddy sets `Remote-User` inside a `route { }` block that first strips any incoming copy. Directive order matters, which is why it has to be `route`;
   - Paperless itself must listen only on `127.0.0.1`, so nobody can reach it without going through Caddy;
   - every other device gets the normal login page.
4. **Cockpit** (optional hub admin page): list each name in `Origins` in `cockpit.conf`, with `AllowUnencrypted` and `ProtocolHeader` behind plain-HTTP Caddy.
5. **Icons:** serve copies of each service's logo from `/srv/hub-icons` so bookmarks and tiles keep their pictures.
6. **The laptop's Firefox** (optional, with the owner's OK):
   - a `policies.json` in Firefox's `distribution` folder adds a bookmarks folder;
   - `browser.newtabpage.pinned` in the profile pins New Tab tiles; edit it only while Firefox is closed.
7. **Check:** on the phone with the VPN on, `photos.<app name>.duckdns.org` opens Immich and `docs.…` opens Paperless already signed in.

## Phase 10: devices and accounts

Add only what the house has. Each one gets its section in config and secrets, then its app page, then a real test.

**TVs and players**
- **Sony Bravia:** turn on IP control with a pre-shared key in the TV's network settings; put the key and MAC in `[tv]`.
- **Roku:** set Control by mobile apps, Network access, to Enabled, or it refuses keypresses. Typing into search uses `search/browse`.
- **Cast:** Chromecast or Google TV via `[cast] device`, with the `cast` extra.
- **PS5:** status by discovery; wake after `homehub ps5 pair`. Remote Play sign-in fails behind ad blockers or the VPN; use a private window.

**Games and music**
- **PSN** presence and trophies: an npsso cookie in `[psn]`, which expires; the hub reminds before it does.
- **Steam:** an API key and Steam ID in `[steam]`.
- **Spotify:** their own developer app (`[spotify]` client id and secret), then `homehub spotify login`. Playback control needs Premium.

**Phones**
- KDE Connect on Android phones (`setup_hub.sh --with-kdeconnect`, then pair from the phone) for find-my-phone, notifications and ringing.
- adb wireless debugging for screen tasks. It turns itself off; `adb connect` needs the ip:port from `adb mdns services`.

**Google, family and messaging**
- **Gmail packages and Google Tasks:** their own Google Cloud OAuth app (desktop type, test users = themselves), the keys in `[google_tasks]`, then `homehub packages login` and `homehub google-tasks login`. Gmail stays read-only.
- **Cozi:** their login in `[cozi]`. Cloudflare needs a normal browser User-Agent.
- **Discord:** their own bot in their own server, token in `[discord]`. It sends only after a yes.

**The rest**
- **Router page and port forwards:** router integrations are model-specific. The example targets one TP-Link model. For another router, write and test a new client with the owner, read-only first. Only ever touch forwards the hub created (named `Hub: …`).
- **Lights:** TP-Link Kasa or Tapo, only the names listed in `[lights] devices`.
- **Home Assistant:** if they already run it, `[home_assistant]` with a long-lived token.
- **Family Windows PCs (optional):** the PC agent in `hub/homehub/pc_agent/`:
  - built with `scripts/bundle_agent.py`, installed with `install_agent.ps1` as administrator;
  - HMAC-signed requests and allowlisted operations;
  - the person sees a notice whenever the hub looks at their screen.

## Phase 11: Minecraft (optional)

- The app's Minecraft manager runs each server as an `itzg/minecraft-server` container under `[minecraft] root`:
  - Java is picked from Mojang's metadata, and mods come from Modrinth;
  - whitelist and online mode are always on;
  - files stay confined to the server folder, and delete moves the folder to `.trash`.
- The port binds to the VPN address until "Open to friends" opens a router forward named `Hub: Minecraft <name>`. Ask the owner before opening it.

## Phase 12: backups and the recovery guide

1. **`homehub backup`** (nightly at 3:00, 14 kept): the database, `config.toml`, `people.toml`, `secrets.toml`.
2. **`scripts/backup_services.sh`** (nightly at 3:30, at low priority) into `/srv/backups`:
   - the Immich database dump, checked;
   - hard-linked nightly snapshots of the original photos;
   - a Paperless export, checked for its manifest;
   - Minecraft worlds;
   - a config archive with `/etc/wireguard`, AdGuard and the compose files.
3. **Every backup on the hub's one disk dies with that disk. Say so plainly.** Strongly suggest a USB drive or a second machine and set `[backup] copy_to`; until then, copy the backup folders off now and then.
4. **Write the recovery guide** in the README's "This house" section, in plain words, with no secrets, only where each one is kept:
   - lost phone (remove it on VPN devices, add a new one);
   - lost laptop keys;
   - internet down (pause AdGuard, router DNS back);
   - new router (keep the network numbers, re-forward 51820, DNS);
   - public address changed (DuckDNS follows);
   - certificate expired (`cert-renew`);
   - Claude stopped answering (`claude auth login`);
   - power cut;
   - dead hub disk (rebuild steps in order).
5. **Check:** run both backups by hand, look at the files, and restore one Paperless document and one photo from a snapshot into a temp folder.

## Phase 12b: a watchdog, health history and a second copy

Put these on a second machine when there is one (a Proxmox container is ideal), never on the hub they watch:
- **Uptime Kuma** checks every service each minute and pushes straight to ntfy, so a dead hub still gets reported. Things only the hub can see (the VPN interface, whether last night's backups succeeded, the DuckDNS updater) report in through push monitors from a one-minute hub timer; a missing check-in counts as down. Add a route to the VPN subnet through the hub so it can check the app and the Caddy names. In an unprivileged container, ping monitors need `net.ipv4.ping_group_range` within the mapped range (`0 65534`). Monitors added over its socket API need `conditions` as a JSON array, and calls sent in the first seconds after connecting are ignored.
- **Beszel** keeps CPU, memory, temperature and disk history and reads SMART, with small agents on each machine (root, for SMART) and alerts to the same ntfy topic.
- **A second copy of every backup**: a nightly rsync from the hub to the second machine, with its own key limited there by `command="/usr/bin/rrsync <folder>",restrict`. Back up the second machine's apps (databases with `sqlite3 .backup` while they run) into the hub's nightly backup the same way.
- **Weekly update alerts** that list what's waiting and change nothing, and a **new-device alert** that pushes the first time an unknown device joins the network (learn everything quietly on the first look).

Caddy installed from apt with its admin interface off can't `reload`; restart it after a change.

## Phase 13: hand over and keep going

- Walk the owner through every page of the app on their phone.
- Show them the README's recovery guide and the spare VPN code.
- Afterwards, take requests one at a time. Each is a feature with its own test, committed locally, pushed to their fork when finished, then deployed to the hub.

## Rules for the code

- **No comments or commented-out code, and no placeholders.**
  - Example config values are empty or real defaults, never fake data.
  - Example files keep the real structure.
- **Tests use real things:** real files, a real SQLite database in a temp folder, real HTTP to real devices where present, real Claude calls (marked `claude`).
  - No mocks, fakes or fake clocks.
  - Device tests skip when the device or its secret is absent.
  - Markers: `claude`, `live`, `slow`.
  - Nothing ever makes audible sound.
- **Every tool is a `ToolSpec` in a `ToolModule`** (`tools/__init__.py`):
  - policies: auto, confirm, hidden;
  - scopes: household, personal, device;
  - text from outside (emails, web pages, notifications) is marked untrusted, so instructions inside it are never followed;
  - add a dedicated tool rather than letting Claude use a shell.
- **Config:** `config.py` holds every setting as a dataclass default, unknown keys are rejected by name, and `config.example.toml` must equal the defaults.
- **The app:**
  - no inline styles or scripts;
  - every element the scripts look up must exist in the page, and every page must be routable (tests check both);
  - each HTTP verb has exactly one handler method that dispatches by path. A second `def do_PUT` in the class silently replaces the first.
- **Scripts:** mark them executable in git (`git update-index --chmod=+x`), because a Windows checkout loses the bit, and call them from units through `bash`.
- **Never edit the tree while the test suite runs.**

## Things that already cost the original house time

- **Claude Agent SDK:**
  - it auto-approves `allowed_tools` and calls `can_use_tool` only for the rest, so enforce policies inside the tool wrapper too;
  - in-process tool handlers run in the SDK's own task, so a `contextvars` value set around `query()` never reaches them; carry the call context as a plain attribute;
  - `setting_sources=["user"]` loads every MCP server in `~/.claude.json`; disallow the foreign ones by name or each turn costs thousands of tokens;
  - per-turn `usage` is summed over requests, so measure cache reads and writes.
- **If Claude's login fails** (an expired subscription, say), the rest of the app must keep working; only the chat says why.
- **Speed is model round trips:** about 1.2 s without a tool, about 3.5 s with one. Answer everyday commands on the hub, and fetch once then cache (weather).
- **Docker** may ignore daemon-wide bind addresses: name the address in each published port.
- **Ubuntu Desktop** ships with Wi-Fi power saving and suspend on. Turn both off.
- **WSL** sits behind NAT: it can't scan the LAN or see KDE Connect. Work on the hub over SSH for anything network-facing.
- **Roku:** in "Limited" control mode it answers device info but refuses keypresses.
- **Spotify:**
  - an idle device reports volume 0 until playback moves there;
  - player commands return non-JSON bodies;
  - private playlists only appear through `/me/playlists`.
- **PSN** calls are paced about 3 s apart, so cache account data for 15 minutes.
- **The PS5** stops answering discovery for a few seconds while going to rest.
- **Amazon** add-to-cart links redirect to sign-in; link product pages instead.
- **ntfy:** markdown links aren't tappable; use action buttons.
- **The hub's real-time clock** can store a wake alarm one second early, so allow a little slack when reading it back.
- **WireGuard for Windows** stores tunnels encrypted. Changing one means re-importing a plain `.conf`, and the tunnel drops briefly.

## Definition of done

Each item works from the owner's phone on mobile data with the VPN on:

- [ ] the app opens over HTTPS, is installed on the home screen, and the chat answers;
- [ ] AdGuard blocks ads at home, and the internet still works with the hub switched off;
- [ ] photos back up in the background, and the Photos page shows the newest;
- [ ] documents scan and upload from the app, and search finds them;
- [ ] Jellyfin plays on the TVs and phones, uploads from the app land in the right library, and series group into collections;
- [ ] easy names open each service, and sign-in is automatic on the owner's devices;
- [ ] every TV, player and console they have is controllable from its page;
- [ ] both nightly backups ran and were checked, and a failure push arrives when a job fails;
- [ ] the spare VPN code is saved, and the README's "This house" section and recovery guide match reality;
- [ ] a watchdog on another machine pushes when the hub or any service goes down, and the backups exist on two machines;
- [ ] the test suite passes on the hub.
