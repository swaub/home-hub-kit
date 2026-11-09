import asyncio
import re
import socket
import time
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.network import IS_WINDOWS, default_gateways, ping_once, run

SPEED_URL = "https://speed.cloudflare.com"
DOWNLOAD_BYTES = 25_000_000
UPLOAD_BYTES = 5_000_000
SPEED_TIME_LIMIT = 12
PUBLIC_IPS = ("1.1.1.1", "8.8.8.8")
WEBSITES = ("https://www.google.com", "https://www.cloudflare.com")
DNS_NAME = "www.google.com"
SLOW_MS = 1500
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PROMPT = ("For 'is the internet down or slow' use check_internet, which says where the problem is; wifi_status gives this computer's "
          "Wi-Fi signal; speed_test measures download and upload speed when they ask how fast the internet is (it moves about 30 MB).")


def parse_iw(dev_raw, link_raw):
    if not re.search(r"Interface\s+(\S+)", dev_raw):
        return None
    if "Not connected" in link_raw or "SSID" not in link_raw:
        return None
    dbm = re.search(r"signal:\s*(-?\d+)", link_raw)
    signal = int(dbm.group(1)) if dbm else -100
    rx = re.search(r"rx bitrate:\s*([\d.]+)", link_raw)
    tx = re.search(r"tx bitrate:\s*([\d.]+)", link_raw)
    freq = re.search(r"freq:\s*(\d+)", link_raw)
    return {"ssid": re.search(r"SSID:\s*(.+)", link_raw).group(1).strip(), "signal_percent": max(0, min(100, 2 * (signal + 100))),
            "signal_dbm": signal, "receive_mbps": rx.group(1) if rx else "", "transmit_mbps": tx.group(1) if tx else "",
            "frequency_mhz": int(freq.group(1)) if freq else None}


def parse_netsh(raw):
    fields = dict(re.findall(r"^\s+([^:\n]+?)\s+:\s(.+)$", raw, re.M))
    if fields.get("State", "").strip() != "connected":
        return None
    signal = int((fields.get("Signal", "0%").strip().rstrip("%") or 0))
    return {"ssid": fields.get("SSID", "").strip(), "signal_percent": signal, "signal_dbm": None,
            "receive_mbps": fields.get("Receive rate (Mbps)", "").strip(), "transmit_mbps": fields.get("Transmit rate (Mbps)", "").strip(),
            "frequency_mhz": None, "channel": fields.get("Channel", "").strip(), "radio": fields.get("Radio type", "").strip()}


def wifi_status():
    if IS_WINDOWS:
        return parse_netsh(run(["netsh", "wlan", "show", "interfaces"]))
    dev = run(["iw", "dev"])
    iface = re.search(r"Interface\s+(\S+)", dev)
    if not iface:
        return None
    return parse_iw(dev, run(["iw", "dev", iface.group(1), "link"]))


def timed_https(url, timeout=6):
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": BROWSER_UA}), timeout=timeout) as r:
            r.read(2048)
        return (time.perf_counter() - start) * 1000
    except Exception:
        return None


def resolve_ms(name=DNS_NAME):
    start = time.perf_counter()
    try:
        socket.getaddrinfo(name, 443)
        return (time.perf_counter() - start) * 1000
    except OSError:
        return None


def probe():
    gateways = default_gateways()
    gateway = gateways[0] if gateways else None
    return {"gateway": gateway, "gateway_ms": ping_once(gateway) if gateway else None,
            "public": {ip: ping_once(ip) for ip in PUBLIC_IPS}, "dns_ms": resolve_ms(),
            "web": {url: timed_https(url) for url in WEBSITES}}


def diagnose(p):
    lines = [f"Router ({p['gateway'] or 'none found'}): " + (f"{p['gateway_ms']:.0f} ms" if p["gateway_ms"] is not None else "NOT responding")]
    lines.append("Internet ping: " + ", ".join(f"{ip} " + (f"{ms:.0f} ms" if ms is not None else "no reply") for ip, ms in p["public"].items()))
    lines.append(f"DNS lookup: {p['dns_ms']:.0f} ms" if p["dns_ms"] is not None else "DNS lookup: FAILED")
    lines.append("Websites: " + ", ".join(f"{u.split('//')[1]} " + (f"{ms:.0f} ms" if ms is not None else "FAILED") for u, ms in p["web"].items()))
    web_down = all(v is None for v in p["web"].values())
    pings_down = all(v is None for v in p["public"].values())
    if p["gateway_ms"] is None and p["gateway"]:
        verdict = "Problem is between this device and the router (Wi-Fi/cable or the router itself)."
    elif web_down and pings_down:
        verdict = "Router is fine but the internet is down (likely the internet provider or modem)."
    elif p["dns_ms"] is None:
        verdict = "Internet connection works but DNS (looking up website names) is failing."
    elif web_down:
        verdict = "Pings work but websites don't load (possibly a firewall, proxy or captive portal)."
    elif max((v for v in p["web"].values() if v is not None), default=0) > SLOW_MS:
        verdict = "Internet works but is slow to respond."
    else:
        verdict = "Internet looks healthy."
    return verdict, lines


def internet_check():
    return diagnose(probe())


def speed_test():
    start = time.perf_counter()
    got = 0
    down_req = urllib.request.Request(f"{SPEED_URL}/__down?bytes={DOWNLOAD_BYTES}", headers={"User-Agent": BROWSER_UA})
    with urllib.request.urlopen(down_req, timeout=SPEED_TIME_LIMIT) as r:
        while time.perf_counter() - start < SPEED_TIME_LIMIT:
            chunk = r.read(262144)
            if not chunk:
                break
            got += len(chunk)
    down = got * 8 / (time.perf_counter() - start) / 1e6
    payload = b"0" * UPLOAD_BYTES
    start = time.perf_counter()
    req = urllib.request.Request(f"{SPEED_URL}/__up", data=payload, method="POST",
                                 headers={"Content-Type": "application/octet-stream", "User-Agent": BROWSER_UA})
    with urllib.request.urlopen(req, timeout=SPEED_TIME_LIMIT * 2) as r:
        r.read()
    up = len(payload) * 8 / (time.perf_counter() - start) / 1e6
    latency = sorted(v for v in (ping_once("1.1.1.1") for _ in range(4)) if v is not None)
    return down, up, (latency[len(latency) // 2] if latency else None), got


def quality(percent):
    return "excellent" if percent >= 80 else "good" if percent >= 60 else "fair" if percent >= 40 else "weak"


def describe_wifi(info):
    if not info:
        return "Not connected to Wi-Fi (may be on a wired connection)."
    band = f", {info['frequency_mhz'] / 1000:.1f} GHz band" if info.get("frequency_mhz") else (f", channel {info['channel']} ({info['radio']})" if info.get("channel") else "")
    return (f"Wi-Fi '{info['ssid']}': signal {info['signal_percent']}% ({quality(info['signal_percent'])}), receive {info['receive_mbps']} Mbps, "
            f"transmit {info['transmit_mbps']} Mbps{band}.")


def build_module():
    @tool("check_internet", "Diagnose the internet connection: router reachability, internet pings, DNS and website load times, with a "
          "plain-English verdict of where any problem is. Use for 'is the internet down/slow?'.", {"type": "object", "properties": {}})
    async def check_internet(args):
        verdict, lines = await asyncio.to_thread(internet_check)
        wifi = await asyncio.to_thread(wifi_status)
        if wifi:
            lines.append(f"Wi-Fi: {wifi['ssid']}, signal {wifi['signal_percent']}%, link {wifi['receive_mbps']} Mbps")
        return text_result(verdict + "\n" + "\n".join(lines))

    @tool("speed_test", "Measure internet download and upload speed (Mbps) and latency using Cloudflare's free speed test. Takes about "
          "10-20 seconds and uses ~30 MB of data.", {"type": "object", "properties": {}})
    async def run_speed_test(args):
        try:
            down, up, latency, got = await asyncio.to_thread(speed_test)
        except Exception as e:
            return text_result(f"Speed test failed: {e}", error=True)
        return text_result(f"Download {down:.0f} Mbps, upload {up:.0f} Mbps, latency " +
                           (f"{latency:.0f} ms" if latency is not None else "unknown") + f" ({got / 1e6:.0f} MB downloaded).")

    @tool("wifi_status", "Show this device's Wi-Fi connection: network name, signal strength (%) and link speed.",
          {"type": "object", "properties": {}})
    async def wifi(args):
        return text_result(describe_wifi(await asyncio.to_thread(wifi_status)))

    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (check_internet, run_speed_test, wifi)]
    return ToolModule(server="internet", tools=specs, prompt=PROMPT)
