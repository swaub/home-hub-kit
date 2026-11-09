import asyncio
import threading
from ipaddress import IPv4Address

import pytest

from homehub.config import load_config
from homehub.tools import result_texts
from homehub.tools.router import NOT_SET_UP, PortForwards, RouterReader, build_module, connection_words, describe, load_settings, merge, unchunk


def test_connections_read_as_words():
    assert connection_words("wired") == "wired"
    assert connection_words("host_2g") == "Wi-Fi 2.4 GHz" and connection_words("host_5g_2") == "Wi-Fi 5 GHz"
    assert connection_words("guest_6g") == "guest Wi-Fi 6 GHz" and connection_words("host_mlo_5g") == "Wi-Fi 5 GHz"


def test_devices_and_leases_merge_by_mac_connected_first():
    Connection = pytest.importorskip("tplinkrouterc6u").Connection
    dataclass = pytest.importorskip("tplinkrouterc6u.common.dataclass")
    Device, IPv4DHCPLease = dataclass.Device, dataclass.IPv4DHCPLease
    EUI48 = pytest.importorskip("macaddress").EUI48
    devices = [Device(Connection.HOST_5G, EUI48("02-00-00-00-00-08"), IPv4Address("192.168.1.4"), "Riley-s-Phone"),
               Device(Connection.WIRED, EUI48("02-00-00-00-00-04"), IPv4Address("192.168.1.19"), "audiocast")]
    leases = [IPv4DHCPLease(EUI48("02-00-00-00-00-08"), IPv4Address("192.168.1.4"), "", "1:00:00"),
              IPv4DHCPLease(EUI48("02-00-00-00-00-05"), IPv4Address("192.168.1.25"), "alex-SER", "0:10:00")]
    rows = merge(devices, leases)
    assert [r["name"] for r in rows] == ["audiocast", "Riley-s-Phone", "alex-SER"]
    assert rows[1] == {"name": "Riley-s-Phone", "ip": "192.168.1.4", "mac": "02:00:00:00:00:08", "connection": "Wi-Fi 5 GHz", "online": True}
    assert rows[2]["online"] is False
    text = describe(rows).splitlines()
    assert text[0] == "2 devices connected to the router now (1 more have an address but aren't connected)."
    assert text[4] == "alex-SER\t192.168.1.25\t02:00:00:00:00:05\tnot connected"


def test_without_a_router_login_the_tool_says_how_to_set_it_up(tmp_path):
    from homehub.config import parse_config

    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert load_settings(cfg) is None
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["router_devices"]({})))[0] == NOT_SET_UP
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "secrets.toml").write_text('[router]\nurl = "http://192.168.0.1/"\npassword = "x"\nkind = "netgear"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(cfg)


@pytest.mark.live
def test_the_real_router_lists_this_computer():
    pytest.importorskip("tplinkrouterc6u")
    cfg = load_config()
    if load_settings(cfg) is None:
        pytest.skip("no [router] login in secrets.toml here")
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    text = result_texts(asyncio.run(tools["router_devices"]({})))[0]
    assert "devices connected to the router now" in text and text.count("\n") > 3


def test_a_chunked_router_reply_is_joined_back_into_one_body():
    raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    assert unchunk(raw) == b"hello world"
    assert unchunk(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok") == b"ok"


def test_port_forwards_only_open_and_close_hub_entries_before_touching_the_router():
    forwards = PortForwards({"url": "http://127.0.0.1", "password": "x", "username": "", "kind": "tplink_ex"}, hub_address="127.0.0.1")
    for description, protocol, port in (("Minecraft", "TCP", 25565), ("Hub: x", "ICMP", 25565), ("Hub: x", "TCP", 22), ("Hub: x", "TCP", 70000)):
        with pytest.raises(ValueError):
            forwards.open(description, protocol, port)
    with pytest.raises(ValueError):
        forwards.close("WireGuard hub")


@pytest.mark.live
def test_the_router_answers_two_readers_at_once_because_they_take_turns():
    pytest.importorskip("tplinkrouterc6u")
    cfg = load_config()
    settings = load_settings(cfg)
    if settings is None:
        pytest.skip("no router login here")
    results = {}

    def devices():
        results["devices"] = RouterReader(cfg).read()

    def forwards():
        results["forwards"] = PortForwards(settings).list()

    threads = [threading.Thread(target=devices), threading.Thread(target=forwards)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)
    assert results["devices"] and isinstance(results["forwards"], list)
