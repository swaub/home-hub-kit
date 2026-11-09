import asyncio

import pytest

from homehub.tools import result_texts
from homehub.tools.internet import build_module, describe_wifi, diagnose, parse_iw, parse_netsh, quality, wifi_status

IW_DEV = "phy#0\n\tInterface wlan0\n\t\tifindex 3\n\t\ttype managed\n"
IW_LINK = ("Connected to aa:bb:cc:dd:ee:ff (on wlan0)\n\tSSID: PiHome\n\tfreq: 5180\n\tRX: 123456 bytes (789 packets)\n\tTX: 2345 bytes (67 packets)\n"
           "\tsignal: -61 dBm\n\trx bitrate: 72.2 MBit/s MCS 7\n\ttx bitrate: 65.0 MBit/s MCS 6\n")


def text(result):
    return result_texts(result)[0]


def probes(gateway_ms, public_ms, dns_ms, web_ms, gateway="10.0.0.1"):
    return {"gateway": gateway, "gateway_ms": gateway_ms, "public": {"1.1.1.1": public_ms, "8.8.8.8": public_ms}, "dns_ms": dns_ms,
            "web": {"https://www.google.com": web_ms, "https://www.cloudflare.com": web_ms}}


def test_diagnosis_names_where_the_problem_is():
    verdict, lines = diagnose(probes(None, None, None, None))
    assert "between this device and the router" in verdict and lines[0] == "Router (10.0.0.1): NOT responding"
    verdict, _ = diagnose(probes(3.0, None, None, None))
    assert "internet is down" in verdict
    verdict, lines = diagnose(probes(3.0, 5.0, None, None))
    assert verdict.startswith("Internet connection works but DNS") and lines[2] == "DNS lookup: FAILED"
    verdict, _ = diagnose(probes(3.0, 5.0, 20.0, None))
    assert "captive portal" in verdict
    verdict, lines = diagnose(probes(3.0, 5.0, 20.0, 2500.0))
    assert verdict == "Internet works but is slow to respond." and lines[3] == "Websites: www.google.com 2500 ms, www.cloudflare.com 2500 ms"
    verdict, lines = diagnose(probes(3.0, 5.0, 20.0, 120.0))
    assert verdict == "Internet looks healthy." and lines[1] == "Internet ping: 1.1.1.1 5 ms, 8.8.8.8 5 ms"
    verdict, lines = diagnose(probes(None, 5.0, 20.0, 120.0, gateway=None))
    assert verdict == "Internet looks healthy." and lines[0] == "Router (none found): NOT responding"


def test_wifi_parsing_and_wording():
    info = parse_iw(IW_DEV, IW_LINK)
    assert info == {"ssid": "PiHome", "signal_percent": 78, "signal_dbm": -61, "receive_mbps": "72.2", "transmit_mbps": "65.0", "frequency_mhz": 5180}
    assert describe_wifi(info) == "Wi-Fi 'PiHome': signal 78% (good), receive 72.2 Mbps, transmit 65.0 Mbps, 5.2 GHz band."
    assert parse_iw(IW_DEV, "Not connected.\n") is None
    assert parse_iw("", IW_LINK) is None
    assert describe_wifi(None) == "Not connected to Wi-Fi (may be on a wired connection)."
    assert [quality(p) for p in (85, 60, 45, 10)] == ["excellent", "good", "fair", "weak"]


def test_windows_wifi_parsing():
    sample = ("    Name                   : Wi-Fi\n    State                  : connected\n    SSID                   : HomeNet\n"
              "    Channel                : 36\n    Radio type             : 802.11ax\n    Receive rate (Mbps)    : 600\n"
              "    Transmit rate (Mbps)   : 480\n    Signal                 : 54%\n")
    info = parse_netsh(sample)
    assert info["ssid"] == "HomeNet" and info["signal_percent"] == 54 and info["receive_mbps"] == "600" and info["channel"] == "36"
    assert describe_wifi(info) == "Wi-Fi 'HomeNet': signal 54% (fair), receive 600 Mbps, transmit 480 Mbps, channel 36 (802.11ax)."
    assert parse_netsh("    State                  : disconnected\n") is None


def test_live_wifi_status_on_this_computer():
    live = wifi_status()
    assert live is None or live["signal_percent"] > 0
    tools = {spec.name: spec.fn.handler for spec in build_module().tools}
    assert text(asyncio.run(tools["wifi_status"]({}))).startswith(("Wi-Fi '", "Not connected to Wi-Fi"))


@pytest.mark.claude
@pytest.mark.slow
def test_claude_checks_wifi_and_internet(harness):
    h = harness([build_module()])

    async def scenario():
        async with h:
            out = []
            for q in ("How strong is my Wi-Fi signal?", "Is my internet working okay?"):
                reply = await h.ask(q)
                out.append((q, reply, h.tools_called()))
            return out

    results = asyncio.run(scenario())
    for q, reply, called in results:
        print(f"\n  {q} -> {called}\n      reply: {reply}")
    assert "wifi_status" in results[0][2]
    assert "check_internet" in results[1][2]


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_runs_a_speed_test_when_asked(harness):
    h = harness([build_module()])

    async def scenario():
        async with h:
            reply = await h.ask("How fast is my internet right now?")
            return reply, h.tools_called()

    reply, called = asyncio.run(scenario())
    print(f"\n  speed test -> {called}\n      reply: {reply}")
    assert "speed_test" in called and ("mbps" in reply.lower() or "megabit" in reply.lower())
