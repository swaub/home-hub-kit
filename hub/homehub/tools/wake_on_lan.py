import asyncio
import re
import socket

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.network import VIRTUAL_INTERFACE, local_interfaces, ping_once

PORTS = (9, 7)
PROMPT = ("wake_computer turns on a computer with Wake-on-LAN. For any computer in the household (anyone's, including other people's "
          "PCs listed in the household) call it with device set to that computer's name first; the household registry holds its MAC "
          "address. Only pass mac for a computer that isn't in the household, using a MAC they told you or that recall finds. Reply right "
          "away that it's starting up; never wait for it to boot.")


def parse_mac(text):
    hexdigits = re.sub(r"[^0-9a-f]", "", (text or "").lower())
    if len(hexdigits) != 12:
        raise ValueError(f"'{text}' isn't a MAC address (expected 12 hex digits like AA:BB:CC:DD:EE:FF).")
    return bytes.fromhex(hexdigits)


def format_mac(raw):
    return ":".join(f"{b:02x}" for b in raw)


def magic_packet(mac):
    return b"\xff" * 6 + parse_mac(mac) * 16


def broadcast_addresses(interfaces=None):
    found = {"255.255.255.255"}
    for iface, name in (local_interfaces() if interfaces is None else interfaces):
        if iface.ip.is_private and iface.network.prefixlen < 31 and not VIRTUAL_INTERFACE.search(name):
            found.add(str(iface.network.broadcast_address))
    return sorted(found)


class Waker:
    def __init__(self, targets=None, ports=PORTS, check_ip=True):
        self.targets = targets
        self.ports = tuple(ports)
        self.check_ip = check_ip

    def send(self, mac):
        packet = magic_packet(mac)
        sent, failed = [], None
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            for target in (self.targets if self.targets is not None else [(a, None) for a in broadcast_addresses()]):
                address, port = target
                try:
                    for p in ([port] if port else self.ports):
                        sock.sendto(packet, (address, p))
                except OSError as e:
                    failed = OSError(f"couldn't send the wake signal to {address}: {e}")
                    continue
                sent.append(address)
        if not sent and failed is not None:
            raise failed
        return sent

    def responding(self, ip):
        return bool(ip) and self.check_ip and ping_once(ip) is not None


def resolve(storage, args):
    name = (args.get("device") or "").strip()
    mac = (args.get("mac") or "").strip()
    device = storage.find_device(name) if name else None
    if mac:
        try:
            mac = format_mac(parse_mac(mac))
        except ValueError as e:
            return device, None, None, str(e)
    if name and device is None and not mac:
        return None, None, None, f"I don't know a device called '{name}' in the household; give me its MAC address or add it to people.toml."
    if device is not None and not mac and not device.get("mac"):
        return device, None, None, f"I don't have a MAC address for {device['name']} yet; tell me it and I'll keep it."
    if device is not None and mac and not device.get("mac"):
        device = storage.set_device_runtime(device["id"], mac=mac)
    mac = mac or device["mac"]
    ip = (args.get("ip") or "").strip() or (device.get("last_ip") if device else None)
    return device, mac, ip, None


def build_module(storage, waker=None):
    waker = waker or Waker()

    @tool("wake_computer", "Turn on (wake) a sleeping or powered-off computer on the local network with Wake-on-LAN. For a household "
          "computer give device (its name as listed in the household, e.g. \"Dad's office computer\"); the registry knows its MAC, so "
          "don't look the MAC up first. For any other computer give mac (its network card's MAC address, e.g. one the user told you "
          "before; recall finds it). Optional ip: its last known IP. Wake-on-LAN must be enabled in that computer's BIOS/network "
          "settings and usually needs a wired connection.",
          {"type": "object", "properties": {"device": {"type": "string"}, "mac": {"type": "string"}, "ip": {"type": "string"}}})
    async def wake_computer(args):
        if not (args.get("device") or args.get("mac")):
            return text_result("Give the device's name or its MAC address.", error=True)
        device, mac, ip, problem = resolve(storage, args)
        if problem:
            return text_result(problem, error=True)
        try:
            sent = await asyncio.to_thread(waker.send, mac)
        except (ValueError, OSError) as e:
            return text_result(str(e), error=True)
        what = device["name"] if device else mac
        result = f"Sent the wake-up packet to {what} ({mac}, via {', '.join(sent)})."
        if ip and await asyncio.to_thread(waker.responding, ip):
            return text_result(result + f" {ip} is already responding, so it's on.")
        return text_result(result + " Computers take about 30 to 60 seconds to boot; say it's on its way and that they can ask whether "
                           "it's on yet (use ping_host on its IP then).")

    return ToolModule(server="wol", tools=[ToolSpec(fn=wake_computer, policy="auto", scope=Scope.device("device"))], prompt=PROMPT)
