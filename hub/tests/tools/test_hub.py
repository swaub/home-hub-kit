import asyncio
from datetime import datetime

import pytest

from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.hub import build_module, last_run, took


def call(module, name, args):
    handler = next(spec.fn.handler for spec in module.tools if spec.name == name)
    token = current_context.set(CallContext(room="remote", person_id="alex"))
    try:
        return result_texts(asyncio.run(handler(args)))[0]
    finally:
        current_context.reset(token)


def test_hub_status_reads_the_machine_and_its_services():
    module = build_module(apps=[{"name": "Immich", "url": "https://10.77.0.1:2283"}])
    text = call(module, "hub_status", {})
    lines = text.splitlines()
    assert lines[0].startswith("Up ") and lines[1].startswith("CPU ") and "% of " in lines[1]
    assert lines[2].startswith("Memory ") and lines[3].startswith("Disk ") and "Services:" in lines
    assert lines[-1] == "Links in the app: Immich https://10.77.0.1:2283"
    assert took(90061) == "1 d 1 h 1 min"
    jobs = [line for line in lines if line.startswith(("nightly", "DuckDNS", "HTTPS"))]
    assert all(": last run " in line and line.endswith(("worked", "failed")) or line.endswith("no run in the last week") for line in jobs)
    assert last_run("pytest-no-such-unit") == "no run in the last week"


def test_power_asks_every_time_and_goes_through_the_same_power_switch_as_the_app():
    asked = []
    module = build_module(power=lambda what, person, wake_at=None: (asked.append((what, person, wake_at)) or True, "Shutting down the hub"))
    policies = {spec.name: spec.policy for spec in module.tools}
    assert policies == {"hub_status": "auto", "hub_power": "confirm", "router_forwards": "auto"}
    spec = next(spec for spec in module.tools if spec.name == "hub_power")
    assert spec.ask_each_time
    assert spec.describe({"what": "hub"}) == "reboot the whole hub" and spec.describe({"what": "assistant"}) == "restart the assistant"
    assert spec.describe({"what": "off"}) == "shut down the hub, so it stays off until its power button is pressed"
    assert spec.describe({"what": "off", "wake_at": "2026-11-05 07:00"}) == "shut down the hub until 2026-11-05 07:00"
    assert call(module, "hub_power", {"what": "off", "wake_at": "2026-11-05 07:00"}) == "Shutting down the hub."
    assert call(module, "hub_power", {"what": "off"}) == "Shutting down the hub."
    assert call(module, "hub_power", {"what": "off", "wake_at": "tomorrow morning"}) == "The turn-on time has to look like 2026-11-05 07:00."
    assert asked == [("off", "alex", int(datetime(2026, 11, 5, 7, 0).timestamp())), ("off", "alex", None)]
    assert call(build_module(), "hub_power", {"what": "hub"}) == "Restarting isn't available here."
    assert call(build_module(), "router_forwards", {}) == "The router isn't set up for the hub."


@pytest.mark.live
def test_the_hub_panel_lists_real_services_and_backups_and_refuses_anything_else():
    import shutil

    from homehub.config import load_config
    from homehub.tools.hub import BACKUPS, HubError, HubPanel, compose_projects

    if not shutil.which("docker") or run_docker_ps() is None:
        pytest.skip("no Docker here")
    panel = HubPanel(load_config())
    view = panel.overview()
    assert view["services"] == compose_projects() and all(s["state"] in ("running", "problem", "stopped") for s in view["services"])
    assert {b["id"] for b in view["backups"]} <= {unit for unit, _ in BACKUPS}
    for body, message in (({"action": "restart", "id": "../../etc"}, "no such service"), ({"action": "backup", "id": "rm -rf"}, "no such backup"),
                          ({"action": "pair", "code": "123", "name": "TV"}, "12 digits"), ({"action": "pair", "code": "123456789012", "name": " "}, "Give the TV a name"),
                          ({"action": "adguard_pause", "minutes": 5}, "AdGuard isn't set up"), ({"action": "format"}, "unknown hub action"),
                          ({"action": "bt_connect", "mac": "00:00:00:00:00:00"}, "Bluetooth isn't available")):
        with pytest.raises(HubError, match=message):
            panel.act(body)


def run_docker_ps():
    from homehub.tools.hub import run

    return run(["docker", "ps", "-q"])


@pytest.mark.live
def test_bluetooth_from_the_panel_only_touches_paired_devices():
    import shutil

    from homehub.config import load_config
    from homehub.tools.bluetooth import Bluetooth
    from homehub.tools.hub import HubError, HubPanel

    if not shutil.which("bluetoothctl") or not shutil.which("docker"):
        pytest.skip("no Bluetooth or Docker here")
    panel = HubPanel(load_config(), bluetooth=Bluetooth())
    listed = panel.overview()["bluetooth"]
    assert listed is not None and all(set(d) == {"mac", "name", "kind", "connected"} for d in listed)
    with pytest.raises(HubError, match="isn't paired with the hub"):
        panel.act({"action": "bt_connect", "mac": "00:11:22:33:44:55"})
