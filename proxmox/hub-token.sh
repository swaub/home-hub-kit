#!/usr/bin/env bash
set -euo pipefail
pveum role add HubControl --privs "Datastore.Audit,Sys.Audit,Sys.Modify,Sys.PowerMgmt,VM.Audit,VM.PowerMgmt" 2>/dev/null || pveum role modify HubControl --privs "Datastore.Audit,Sys.Audit,Sys.Modify,Sys.PowerMgmt,VM.Audit,VM.PowerMgmt"
pveum user add hub@pve --comment "Home Hub" 2>/dev/null || true
pveum acl modify / --users hub@pve --roles HubControl
pveum user token remove hub@pve app 2>/dev/null || true
pveum user token add hub@pve app --privsep 1 --output-format json
pveum acl modify / --tokens 'hub@pve!app' --roles HubControl
echo "Put the token id and value above into [proxmox] in the hub's secrets.toml; it is shown only this once."
