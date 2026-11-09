import asyncio
import ipaddress
import os
import socket

import pytest

from homehub.tools import result_texts
from homehub.tools import network
from homehub.tools.network import (IS_WINDOWS, SCANS_OFF, build_module, check_port, choose_interface, format_devices, parse_arp, parse_ip_addr,
                                      parse_ip_neigh, parse_ip_route, parse_ping_ms, parse_windows_interfaces, sort_ips, sweep_target)

IP_ADDR = ("1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever preferred_lft forever\n"
           "2: eth0    inet 192.168.1.23/24 brd 192.168.1.255 scope global dynamic eth0\\       valid_lft 86000sec preferred_lft 86000sec\n"
           "3: docker0    inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\\       valid_lft forever preferred_lft forever\n"
           "4: wlan0    inet 169.254.10.5/16 brd 169.254.255.255 scope link wlan0\\       valid_lft forever preferred_lft forever\n"
           "5: tailscale0    inet 100.101.102.103/32 scope global tailscale0\\       valid_lft forever preferred_lft forever\n")
IP_NEIGH = ("192.168.1.1 dev eth0 lladdr A4:2B:8C:11:22:33 REACHABLE\n"
            "192.168.1.50 dev eth0 lladdr 2c:f0:5d:11:22:33 STALE\n"
            "192.168.1.77 dev eth0  FAILED\n"
            "192.168.1.255 dev eth0 lladdr ff:ff:ff:ff:ff:ff PERMANENT\n"
            "224.0.0.251 dev eth0 lladdr 01:00:5e:00:00:fb NOARP\n"
            "fe80::1 dev eth0 lladdr a4:2b:8c:11:22:33 router REACHABLE\n")


def text(result):
    return result_texts(result)[0]


def interfaces():
    return parse_ip_addr(IP_ADDR)


def test_parses_addresses_routes_and_neighbors():
    found = interfaces()
    assert [(str(i), name) for i, name in found] == [("192.168.1.23/24", "eth0"), ("172.17.0.1/16", "docker0"), ("100.101.102.103/32", "tailscale0")]
    assert parse_ip_route("default via 192.168.1.1 dev eth0 proto dhcp metric 100 \ndefault via 172.17.0.1 dev docker0 \n") == ["192.168.1.1", "172.17.0.1"]
    assert parse_ip_route("") == []
    assert parse_ip_neigh(IP_NEIGH) == {"192.168.1.1": "a4:2b:8c:11:22:33", "192.168.1.50": "2c:f0:5d:11:22:33"}
    assert parse_ping_ms("64 bytes from 1.1.1.1: icmp_seq=1 ttl=57 time=12.3 ms") == 12.3
    assert parse_ping_ms("From 192.168.1.23 icmp_seq=1 Destination Host Unreachable") is None
    assert parse_ping_ms("") is None


def test_parses_windows_interfaces_and_arp():
    raw = '[{"IPAddress":"192.168.1.25","PrefixLength":24,"InterfaceAlias":"Wi-Fi"},{"IPAddress":"172.30.80.1","PrefixLength":20,"InterfaceAlias":"vEthernet (WSL (Hyper-V firewall))"},{"IPAddress":"127.0.0.1","PrefixLength":8,"InterfaceAlias":"Loopback Pseudo-Interface 1"},{"IPAddress":"169.254.5.9","PrefixLength":16,"InterfaceAlias":"Bluetooth"}]'
    found = parse_windows_interfaces(raw)
    assert [(str(i), n) for i, n in found] == [("192.168.1.25/24", "Wi-Fi"), ("172.30.80.1/20", "vEthernet (WSL (Hyper-V firewall))")]
    assert str(choose_interface(found, ["192.168.1.1"]).ip) == "192.168.1.25" and str(choose_interface(found, []).ip) == "192.168.1.25"
    assert parse_windows_interfaces("not json") == [] and parse_windows_interfaces('{"IPAddress":"10.0.0.2","PrefixLength":24,"InterfaceAlias":"Ethernet"}')[0][1] == "Ethernet"
    arp = ("Interface: 192.168.1.25 --- 0x10\n  Internet Address      Physical Address      Type\n  192.168.1.1          a4-2b-8c-11-22-33     dynamic\n"
           "  192.168.1.4          2C-F0-5D-11-22-33     dynamic\n  192.168.1.255        ff-ff-ff-ff-ff-ff     static\n  224.0.0.251           01-00-5e-00-00-fb     static\n")
    assert parse_arp(arp) == {"192.168.1.1": "a4:2b:8c:11:22:33", "192.168.1.4": "2c:f0:5d:11:22:33"}
    assert parse_ping_ms("Reply from 192.168.1.1: bytes=32 time=3ms TTL=64") == 3.0 and parse_ping_ms("Reply from 192.168.1.1: bytes=32 time<1ms TTL=64") == 1.0


def test_picks_the_routed_physical_interface_and_a_safe_sweep_target():
    assert str(choose_interface(interfaces(), ["192.168.1.1"]).ip) == "192.168.1.23"
    assert str(choose_interface(interfaces(), ["172.17.0.1"]).ip) == "192.168.1.23"
    assert str(choose_interface(interfaces(), []).ip) == "192.168.1.23"
    docker_only = [i for i in interfaces() if i[1] != "eth0"]
    assert str(choose_interface(docker_only, []).ip) == "172.17.0.1"
    assert choose_interface([], []) is None
    assert sweep_target(None, interfaces(), ["192.168.1.1"]) == ipaddress.ip_network("192.168.1.0/24")
    wide = [(ipaddress.ip_interface("172.19.140.101/20"), "eth0")]
    assert sweep_target(None, wide, ["172.19.128.1"]) == ipaddress.ip_network("172.19.140.0/24")
    assert sweep_target("192.168.1.0/24") == ipaddress.ip_network("192.168.1.0/24")
    with pytest.raises(ValueError, match="private"):
        sweep_target("8.8.8.0/24")
    with pytest.raises(ValueError, match="too big"):
        sweep_target("10.0.0.0/8")
    with pytest.raises(ValueError, match="No private local network"):
        sweep_target(None, [], [])


def test_sorting_and_formatting():
    assert sort_ips(["192.168.1.100", "192.168.1.9", "10.0.0.1", "192.168.1.9"]) == ["10.0.0.1", "192.168.1.9", "192.168.1.100"]
    table = {"192.168.1.1": "a4:2b:8c:11:22:33", "192.168.1.50": "2c:f0:5d:11:22:33"}
    assert format_devices({}, {}, {}) == "No devices found."
    passive = format_devices(table, {}, {"192.168.1.1": "router.lan"})
    assert passive == "IP\tname\tMAC\n192.168.1.1\trouter.lan\ta4:2b:8c:11:22:33\n192.168.1.50\t(no name)\t2c:f0:5d:11:22:33"
    active = format_devices(table, {"192.168.1.50": 3.4, "192.168.1.9": 1.0}, {})
    assert active.splitlines()[0] == "IP\tname\tMAC\tping" and active.splitlines()[1].startswith("192.168.1.1\t") and "192.168.1.9\t(no name)\t?\t1 ms" in active


def test_check_port_against_a_real_local_socket():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        is_open, ms, error = check_port("127.0.0.1", port)
        assert is_open and ms < 1000 and error == ""
    finally:
        listener.close()
    is_open, ms, error = check_port("127.0.0.1", port)
    assert not is_open and error == "ConnectionRefusedError"


def test_tools_run_passively_on_this_computer():
    tools = {spec.name: spec.fn.handler for spec in build_module(active_scans=False).tools}
    info = text(asyncio.run(tools["network_info"]({})))
    assert "Default gateway:" in info and any("/" in line for line in info.splitlines()[:-1])
    assert asyncio.run(tools["list_network_devices"]({"active_scan": True}))["is_error"] is True
    assert text(asyncio.run(tools["list_network_devices"]({"active_scan": True}))) == SCANS_OFF
    pinged = text(asyncio.run(tools["ping_host"]({"host": "127.0.0.1", "count": 1})))
    assert "1 packets transmitted, 1 received" in pinged
    closed = text(asyncio.run(tools["check_port"]({"host": "127.0.0.1", "port": 1})))
    assert closed.startswith("Port 1 on 127.0.0.1 is closed or unreachable")
    module = build_module()
    assert [spec.policy for spec in module.tools] == ["auto"] * 4 and module.server == "network"


@pytest.mark.live
def test_passive_device_listing_on_the_live_network():
    tools = {spec.name: spec.fn.handler for spec in build_module(active_scans=False).tools}
    listed = text(asyncio.run(tools["list_network_devices"]({})))
    gateways = network.default_gateways()
    assert listed.startswith("IP\tname\tMAC") and (not gateways or gateways[0] in listed)


@pytest.mark.claude
@pytest.mark.slow
def test_claude_uses_the_network_tools_without_scanning(harness):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    h = harness([build_module(active_scans=False)])

    async def scenario():
        async with h:
            out = []
            for q in ("What's this computer's IP address?", f"Is port {port} open on 127.0.0.1?", "Ping 127.0.0.1 twice and tell me the time.",
                      "What devices are on my network?"):
                reply = await h.ask(q)
                out.append((q, reply, h.tools_called(), h.calls("list_network_devices")))
            return out

    try:
        results = asyncio.run(scenario())
    finally:
        listener.close()
    for q, reply, called, _ in results:
        print(f"\n  {q} -> {called}\n      reply: {reply}")
    assert "network_info" in results[0][2]
    assert "check_port" in results[1][2] and "open" in results[1][1].lower()
    assert "ping_host" in results[2][2]
    assert "list_network_devices" in results[3][2]
    assert not any(c.get("active_scan") for _, _, _, calls in results for c in calls)


@pytest.mark.live
@pytest.mark.skipif(not IS_WINDOWS, reason="the home-LAN scan runs from the Windows side of the home laptop")
@pytest.mark.skipif(os.environ.get("HOMEHUB_ALLOW_SCAN") != "1", reason="scans the home network; set HOMEHUB_ALLOW_SCAN=1 to allow it")
def test_live_home_network_scan_from_windows():
    from homehub.tools.wake_on_lan import Waker, broadcast_addresses

    tools = {spec.name: spec.fn.handler for spec in build_module().tools}
    info = text(asyncio.run(tools["network_info"]({})))
    gateway = network.default_gateways()
    assert gateway and gateway[0] in info and any(line.startswith("Wi-Fi") or line.startswith("Ethernet") for line in info.splitlines())
    listed = text(asyncio.run(tools["list_network_devices"]({"active_scan": True})))
    lines = listed.splitlines()
    assert lines[0] == "IP\tname\tMAC\tping" and any(line.startswith(gateway[0] + "\t") for line in lines[1:])
    assert len(lines) >= 3
    pinged = text(asyncio.run(tools["ping_host"]({"host": gateway[0], "count": 2})))
    assert "Reply from" in pinged or "bytes from" in pinged
    casts = broadcast_addresses()
    assert "255.255.255.255" in casts and any(c.startswith(gateway[0].rsplit(".", 1)[0]) for c in casts)
    sent = Waker().send("00:11:22:33:44:55")
    assert sent == casts
    print(f"\n  {len(lines) - 1} devices on the home network; gateway {gateway[0]}; wake packets went to {sent}")
