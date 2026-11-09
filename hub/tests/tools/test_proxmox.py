import asyncio
import os
import subprocess

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.network import check_port
from homehub.tools.proxmox import CONTROL_SCRIPT, GUEST_ACTIONS, WEB_PORT, Proxmox, ProxmoxError, build_module, describe, load_secrets, parse_status, took
from homehub.tools.wake_on_lan import Waker
from tests.tools.test_wake_on_lan import WANT, Listener


def config(tmp_path, **proxmox):
    return parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "proxmox": proxmox})


def control(command, tmp_path):
    script = tmp_path / "hub-control"
    script.write_text(CONTROL_SCRIPT)
    return subprocess.run(["sh", str(script)], capture_output=True, text=True, env={**os.environ, "SSH_ORIGINAL_COMMAND": command})


def test_status_lines_and_the_overview_read_into_a_sentence():
    values = parse_status("uptime=93784\nload=0.07\ncpu_temp=46000\nnoise\n")
    assert values == {"uptime": 93784, "load": 0.07, "cpu_temp": 46000}
    assert took(241) == "4 min" and took(7300) == "2 h 1 min" and took(3 * 86400) == "3 days 0 h"
    view = {"on": True, "node": {"uptime": 93784, "cpu": 3.5, "mem_used": 2 * 2**30, "mem_total": 16 * 2**30, "cpu_temp": 46.0},
            "guests": [{"kind": "lxc", "vmid": 100, "name": "pihole", "status": "running"}, {"kind": "qemu", "vmid": 101, "name": "win11", "status": "stopped"}],
            "updates": 3}
    assert describe("Proxmox server", view) == ("The Proxmox server is on, up 1 day 2 h, CPU 3.5%, using 2.0 of 16.0 GB of memory, CPU at 115°F. "
                                                "Guests: pihole (container 100) running; win11 (virtual machine 101) stopped. 3 updates waiting.")
    assert describe("Proxmox server", {**view, "guests": [], "updates": 0}).endswith("It has no virtual machines or containers yet.")
    assert describe("Basement box", {"on": False}) == "The Basement box is off."


def test_the_control_script_answers_status_and_refuses_everything_else(tmp_path):
    status = control("status", tmp_path)
    values = parse_status(status.stdout)
    assert status.returncode == 0 and values["uptime"] > 0 and values["mem_total"] > 0 and values["vms"] == 0
    for other in ("uptime", "rm -rf /", "status; reboot", ""):
        refused = control(other, tmp_path)
        assert refused.returncode == 1 and refused.stdout.strip() == "only status, temps, upgrade, upgrade-status and poweroff are allowed", other
    temps = control("temps", tmp_path)
    assert temps.returncode == 0 and all(k in ("cpu_temp", "disk_temp") for k in parse_status(temps.stdout))


def test_it_is_only_offered_when_a_host_is_set(tmp_path):
    assert build_module(config(tmp_path)).available(config(tmp_path)) is False
    assert build_module(config(tmp_path, host="127.0.0.1")).available(config(tmp_path, host="127.0.0.1")) is True


def test_an_unreachable_server_reads_as_off_and_wakes_with_a_real_magic_packet(tmp_path):
    if check_port("127.0.0.1", WEB_PORT, timeout=0.5)[0]:
        pytest.skip("something on this computer listens on the Proxmox web port")
    listener = Listener()
    thread = listener.expect(1)
    try:
        machine = Proxmox(config(tmp_path, host="127.0.0.1", mac="02:00:00:00:00:03"), Waker(targets=[("127.0.0.1", listener.port)], check_ip=False))
        assert machine.overview() == {"on": False, "name": "Proxmox server"}
        assert machine.power("shutdown") == "The Proxmox server is already off."
        assert machine.power("reboot") == "The Proxmox server is already off."
        assert machine.power("on") == "I sent the wake signal; the Proxmox server takes about a minute to start."
        thread.join(5)
    finally:
        listener.close()
    assert listener.received == [WANT.replace(bytes.fromhex("001122334455"), bytes.fromhex("020000000003"))]
    with pytest.raises(ProxmoxError, match="no MAC address"):
        Proxmox(config(tmp_path, host="127.0.0.1")).power("on")
    with pytest.raises(ProxmoxError, match="turned on, restarted or shut down"):
        Proxmox(config(tmp_path, host="127.0.0.1")).power("format")


def test_without_the_hub_key_or_login_it_says_so(tmp_path):
    with pytest.raises(ProxmoxError, match="has no key for the Basement box"):
        Proxmox(config(tmp_path, host="127.0.0.1", name="Basement box")).ssh("status")
    with pytest.raises(ProxmoxError, match="no Proxmox login for the Basement box"):
        Proxmox(config(tmp_path, host="127.0.0.1", name="Basement box")).api("GET", "/nodes")


def live():
    cfg = load_config()
    machine = Proxmox(cfg)
    if not cfg.proxmox.host or not load_secrets(cfg).get("token_id") or not machine.reachable():
        pytest.skip("no reachable Proxmox server set up here")
    return cfg, machine


def test_live_overview_on_the_hub():
    cfg, machine = live()
    view = machine.overview()
    node = view["node"]
    assert view["on"] and node["uptime"] > 0 and 0 <= node["cpu"] <= 100 and node["mem_total"] > node["mem_used"] > 0
    assert node["cpu_temp"] is None or 10 < node["cpu_temp"] < 110
    assert {s["name"] for s in view["storage"]} >= {"local"} and isinstance(view["updates"], int)
    assert all(g["actions"] == list(GUEST_ACTIONS[g["kind"]]) for g in view["guests"]) and view["history"]
    text = result_texts(asyncio.run({spec.name: spec.fn.handler for spec in build_module(cfg).tools}["proxmox_status"]({})))[0]
    assert text.startswith(f"The {cfg.proxmox.name} is on, up ")


def test_live_server_refuses_what_the_hub_may_not_do_and_a_wrong_certificate():
    cfg, machine = live()
    with pytest.raises(ProxmoxError, match="doesn't allow the hub"):
        machine.api("DELETE", "/access/users/hub@pve")
    with pytest.raises(ProxmoxError, match="no virtual machine or container called nothing-here"):
        machine.guest("nothing-here", "start")
    with pytest.raises(ProxmoxError, match="unknown Proxmox action"):
        machine.act({"action": "delete"})
    impostor = Proxmox(cfg, secrets={**load_secrets(cfg), "fingerprint": ":".join(["00"] * 32)})
    with pytest.raises(ProxmoxError, match="different certificate"):
        impostor.api("GET", "/nodes")
