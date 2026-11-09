import asyncio
import concurrent.futures
import ipaddress
import json
import platform
import re
import socket
import subprocess
import time

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

IS_WINDOWS = platform.system() == "Windows"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
MAX_SWEEP_HOSTS = 256
NAME_LOOKUP_SECONDS = 1.5
VIRTUAL_INTERFACE = re.compile(r"vEthernet|docker|veth|br-|virbr|WSL|tailscale|^wg|wireguard|^lo$", re.IGNORECASE)
SCANS_OFF = "Active network scans are turned off in the config, so I can only list devices this computer has already talked to."
PROMPT = ("You can see the home network: network_info gives this computer's addresses and router, list_network_devices lists what's "
          "on the network (passive unless they ask for a full scan), ping_host and check_port check whether a device answers.")


def run(cmd, timeout=15):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        return out.stdout + out.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return str(e)


def powershell(command, timeout=20):
    return run(["powershell.exe", "-NoProfile", "-Command", command], timeout=timeout)


def parse_windows_interfaces(raw):
    try:
        items = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    found = []
    for item in items if isinstance(items, list) else [items]:
        try:
            iface = ipaddress.ip_interface(f"{item['IPAddress']}/{item['PrefixLength']}")
        except (KeyError, ValueError):
            continue
        if not iface.ip.is_loopback and not iface.ip.is_link_local:
            found.append((iface, str(item.get("InterfaceAlias", ""))))
    return found


def parse_arp(raw):
    table = {}
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+(\w+)", raw, re.IGNORECASE):
        table[m.group(1)] = m.group(2).replace("-", ":").lower()
    return {ip: mac for ip, mac in table.items()
            if mac != "ff:ff:ff:ff:ff:ff" and not ipaddress.ip_address(ip).is_multicast and not ip.endswith(".255")}


def parse_ip_addr(raw):
    found = []
    for line in raw.splitlines():
        m = re.search(r"^\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if m:
            iface = ipaddress.ip_interface(m.group(2))
            if not iface.ip.is_loopback and not iface.ip.is_link_local:
                found.append((iface, m.group(1)))
    return found


def parse_ip_route(raw):
    return re.findall(r"default via (\S+)", raw)


def parse_ip_neigh(raw):
    table = {}
    for m in re.finditer(r"(\d+\.\d+\.\d+\.\d+)\s.*?lladdr\s+(\S+)\s+(\w+)", raw):
        if m.group(3) != "FAILED":
            table[m.group(1)] = m.group(2).lower()
    return {ip: mac for ip, mac in table.items()
            if mac != "ff:ff:ff:ff:ff:ff" and not ipaddress.ip_address(ip).is_multicast and not ip.endswith(".255")}


def local_interfaces():
    if IS_WINDOWS:
        return parse_windows_interfaces(powershell("Get-NetIPAddress -AddressFamily IPv4 | Select-Object IPAddress, PrefixLength, InterfaceAlias | ConvertTo-Json -Compress"))
    return parse_ip_addr(run(["ip", "-4", "-o", "addr", "show"]))


def default_gateways():
    if IS_WINDOWS:
        raw = powershell("(Get-NetRoute -DestinationPrefix 0.0.0.0/0 -ErrorAction SilentlyContinue).NextHop")
        return [g for g in raw.split() if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", g) and g != "0.0.0.0"]
    return parse_ip_route(run(["ip", "route", "show", "default"]))


def neighbor_table():
    if IS_WINDOWS:
        return parse_arp(run(["arp", "-a"]))
    return parse_ip_neigh(run(["ip", "neigh"]))


def parse_ping_ms(out):
    m = re.search(r"time[=<]\s*([\d.]+)\s*ms", out)
    return float(m.group(1)) if m and "unreachable" not in out.lower() else None


def ping_once(ip, timeout_seconds=1):
    if IS_WINDOWS:
        cmd = ["ping", "-n", "1", "-w", str(int(timeout_seconds * 1000)), ip]
    else:
        cmd = ["ping", "-c", "1", "-W", str(int(timeout_seconds)), ip]
    return parse_ping_ms(run(cmd, timeout=timeout_seconds + 2))


def ping(host, count=4):
    count = max(1, min(10, int(count or 4)))
    return run(["ping", "-n" if IS_WINDOWS else "-c", str(count), host], timeout=30).strip()[-1500:]


def reverse_name(ip):
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def names_for(ips, seconds=NAME_LOOKUP_SECONDS):
    if not ips:
        return {}
    pool = concurrent.futures.ThreadPoolExecutor(32)
    futures = {ip: pool.submit(reverse_name, ip) for ip in ips}
    deadline = time.monotonic() + seconds
    names = {}
    for ip, fut in futures.items():
        try:
            names[ip] = fut.result(timeout=max(0.05, deadline - time.monotonic()))
        except concurrent.futures.TimeoutError:
            names[ip] = ""
    pool.shutdown(wait=False, cancel_futures=True)
    return names


def choose_interface(interfaces, gateways):
    private = [(i, name) for i, name in interfaces if i.ip.is_private]
    gateway_ips = []
    for g in gateways:
        try:
            gateway_ips.append(ipaddress.ip_address(g))
        except ValueError:
            continue
    physical = [(i, name) for i, name in private if not VIRTUAL_INTERFACE.search(name)]
    routed = [(i, name) for i, name in physical if any(g in i.network for g in gateway_ips)]
    return (routed or physical or private or [(None, "")])[0][0]


def sweep_target(network_text=None, interfaces=None, gateways=None):
    if network_text:
        net = ipaddress.ip_network(network_text.strip(), strict=False)
        if not net.is_private:
            raise ValueError("Only private (home) networks can be scanned.")
        if net.num_addresses > MAX_SWEEP_HOSTS + 2:
            raise ValueError(f"{net} is too big to scan; use a /24 like 192.168.1.0/24.")
        return net
    iface = choose_interface(local_interfaces() if interfaces is None else interfaces, default_gateways() if gateways is None else gateways)
    if iface is None:
        raise ValueError("No private local network found.")
    if iface.network.num_addresses > MAX_SWEEP_HOSTS + 2:
        return ipaddress.ip_network(f"{iface.ip}/24", strict=False)
    return iface.network


def sweep(network):
    hosts = list(network.hosts())[:MAX_SWEEP_HOSTS]
    with concurrent.futures.ThreadPoolExecutor(64) as pool:
        results = list(pool.map(lambda h: (str(h), ping_once(str(h))), hosts))
    return {ip: ms for ip, ms in results if ms is not None}


def sort_ips(ips):
    return sorted(set(ips), key=lambda ip: tuple(int(p) for p in ip.split(".")))


def check_port(host, port, timeout=2.0):
    start = time.perf_counter()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, (time.perf_counter() - start) * 1000, ""
    except OSError as e:
        return False, (time.perf_counter() - start) * 1000, type(e).__name__


def format_devices(table, alive, names):
    ips = sort_ips(set(table) | set(alive))
    if not ips:
        return "No devices found."
    header = "IP\tname\tMAC" + ("\tping" if alive else "")
    lines = [f"{ip}\t{names.get(ip) or '(no name)'}\t{table.get(ip, '?')}" + (f"\t{alive[ip]:.0f} ms" if ip in alive else "") for ip in ips]
    return header + "\n" + "\n".join(lines)


def build_module(active_scans=True):
    @tool("network_info", "Show this computer's network addresses, which networks it is on, and the default gateway (router).",
          {"type": "object", "properties": {}})
    async def network_info(args):
        interfaces = await asyncio.to_thread(local_interfaces)
        gateways = await asyncio.to_thread(default_gateways)
        lines = [f"{name}: {iface.with_prefixlen}" for iface, name in interfaces]
        lines.append("Default gateway: " + (", ".join(gateways) or "none"))
        return text_result("\n".join(lines))

    @tool("list_network_devices", "List devices on the local network with IP, hostname and MAC address. By default this is passive "
          "(devices this computer has recently talked to; sends nothing). active_scan=true pings every address on the local /24 "
          f"(max {MAX_SWEEP_HOSTS}) to find everything; only do that when the user asks for a full scan, because active scans can trip "
          "security alerts on work or school networks. Optional network like 192.168.1.0/24.",
          {"type": "object", "properties": {"active_scan": {"type": "boolean"}, "network": {"type": "string"}}})
    async def list_network_devices(args):
        alive = {}
        if args.get("active_scan"):
            if not active_scans:
                return text_result(SCANS_OFF, error=True)
            try:
                net = await asyncio.to_thread(sweep_target, args.get("network"))
            except ValueError as e:
                return text_result(str(e), error=True)
            alive = await asyncio.to_thread(sweep, net)
        table = await asyncio.to_thread(neighbor_table)
        names = await asyncio.to_thread(names_for, sort_ips(set(table) | set(alive)))
        return text_result(format_devices(table, alive, names))

    @tool("ping_host", "Ping a device by IP or hostname to check it's reachable and how fast it responds.",
          {"type": "object", "properties": {"host": {"type": "string"}, "count": {"type": "integer"}}, "required": ["host"]})
    async def ping_host(args):
        return text_result(await asyncio.to_thread(ping, args["host"], args.get("count")))

    @tool("check_port", "Check whether a TCP port is open on a device (e.g. 80 web, 443 https, 22 ssh, 554 camera RTSP, 8123 Home "
          "Assistant, 5555 adb).", {"type": "object", "properties": {"host": {"type": "string"}, "port": {"type": "integer"}},
                                    "required": ["host", "port"]})
    async def check_port_tool(args):
        is_open, ms, error = await asyncio.to_thread(check_port, args["host"], args["port"])
        if is_open:
            return text_result(f"Port {args['port']} on {args['host']} is OPEN ({ms:.0f} ms).")
        return text_result(f"Port {args['port']} on {args['host']} is closed or unreachable ({error}).")

    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (network_info, list_network_devices, ping_host, check_port_tool)]
    return ToolModule(server="network", tools=specs, prompt=PROMPT)
