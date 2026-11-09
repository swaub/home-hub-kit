import asyncio
import ipaddress
import socket
import threading

import pytest

from homehub import household
from homehub.household import sync_household
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools import memory
from homehub.tools.wake_on_lan import Waker, broadcast_addresses, build_module, format_mac, magic_packet, parse_mac

WANT = b"\xff" * 6 + bytes.fromhex("001122334455") * 16


def text(result):
    return result_texts(result)[0]


class Listener:
    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.sock.settimeout(5)
        self.received = []

    def expect(self, count=1):
        def collect():
            for _ in range(count):
                try:
                    self.received.append(self.sock.recvfrom(200)[0])
                except OSError:
                    return

        thread = threading.Thread(target=collect)
        thread.start()
        return thread

    def drain(self, timeout=1.0):
        self.sock.settimeout(timeout)
        while True:
            try:
                self.received.append(self.sock.recvfrom(200)[0])
            except OSError:
                return self.received

    def waker(self):
        return Waker(targets=[("127.0.0.1", self.port)], check_ip=False)

    def close(self):
        self.sock.close()


def test_magic_packets_and_mac_forms():
    for form in ("00:11:22:33:44:55", "00-11-22-33-44-55", "0011.2233.4455", "001122334455", "00 11 22 33 44 55"):
        assert magic_packet(form) == WANT and len(WANT) == 102, form
    for bad in ("00:11:22:33:44", "zz:11:22:33:44:55", "hello", ""):
        with pytest.raises(ValueError, match="isn't a MAC address"):
            parse_mac(bad)
    assert format_mac(parse_mac("2C-F0-5D-11-22-33")) == "2c:f0:5d:11:22:33"


def test_broadcast_targets_skip_virtual_interfaces():
    interfaces = [(ipaddress.ip_interface("192.168.1.23/24"), "eth0"), (ipaddress.ip_interface("172.17.0.1/16"), "docker0"),
                  (ipaddress.ip_interface("172.19.140.101/20"), "eth0"), (ipaddress.ip_interface("100.101.102.103/32"), "tailscale0"),
                  (ipaddress.ip_interface("10.0.0.5/31"), "eth1"), (ipaddress.ip_interface("8.8.8.8/24"), "eth2"),
                  (ipaddress.ip_interface("10.77.0.1/24"), "wg0")]
    assert broadcast_addresses(interfaces) == ["172.19.143.255", "192.168.1.255", "255.255.255.255"]
    assert all(a.endswith(".255") for a in broadcast_addresses())


def test_packet_is_really_sent_over_udp_to_a_local_socket():
    listener = Listener()
    thread = listener.expect(1)
    try:
        sent = listener.waker().send("00:11:22:33:44:55")
        thread.join(5)
    finally:
        listener.close()
    assert sent == ["127.0.0.1"] and listener.received == [WANT]


def test_an_address_that_refuses_the_packet_does_not_stop_the_others():
    listener = Listener()
    thread = listener.expect(1)
    try:
        sent = Waker(targets=[("no-such-host.invalid", 9), ("127.0.0.1", listener.port)], check_ip=False).send("00:11:22:33:44:55")
        thread.join(5)
    finally:
        listener.close()
    assert sent == ["127.0.0.1"] and listener.received == [WANT]
    with pytest.raises(OSError, match="couldn't send the wake signal to no-such-host.invalid"):
        Waker(targets=[("no-such-host.invalid", 9)], check_ip=False).send("00:11:22:33:44:55")


@pytest.fixture
def wol(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    listener = Listener()
    module = build_module(temp_storage, listener.waker())
    yield listener, {spec.name: spec.fn.handler for spec in module.tools}, temp_storage
    listener.close()


def call(tools, name, args, ctx=None):
    token = current_context.set(ctx or CallContext(room="bedroom", person_id="alex", role="owner"))
    try:
        return asyncio.run(tools[name](args))
    finally:
        current_context.reset(token)


def test_wake_resolves_household_devices_through_the_registry(wol):
    listener, tools, storage = wol
    assert call(tools, "wake_computer", {})["is_error"] is True
    missing = call(tools, "wake_computer", {"device": "the office computer"})
    assert missing["is_error"] is True and text(missing).startswith("I don't have a MAC address for Sam's office computer yet")
    unknown = call(tools, "wake_computer", {"device": "the attic server"})
    assert unknown["is_error"] is True and "don't know a device called 'the attic server'" in text(unknown)
    thread = listener.expect(1)
    learned = text(call(tools, "wake_computer", {"device": "Sam's office computer", "mac": "00-11-22-33-44-55"}))
    thread.join(5)
    assert learned.startswith("Sent the wake-up packet to Sam's office computer (00:11:22:33:44:55, via 127.0.0.1).")
    assert listener.received == [WANT] and storage.device("sam_office_pc")["mac"] == "00:11:22:33:44:55"
    thread = listener.expect(1)
    again = text(call(tools, "wake_computer", {"device": "the office computer"}))
    thread.join(5)
    assert again.startswith("Sent the wake-up packet to Sam's office computer (00:11:22:33:44:55") and len(listener.received) == 2
    assert "30 to 60 seconds" in again
    bad = call(tools, "wake_computer", {"mac": "12:34"})
    assert bad["is_error"] is True and "isn't a MAC address" in text(bad)


@pytest.mark.claude
@pytest.mark.slow
def test_claude_wakes_a_remembered_mac_and_a_registered_device(harness, temp_storage):
    listener = Listener()
    modules = [memory.build_module(temp_storage), build_module(temp_storage, listener.waker())]
    temp_storage.set_device_runtime("sam_office_pc", mac="00:11:22:33:44:55")

    async def first_session():
        async with harness(modules) as h:
            reply = await h.ask("Remember that my gaming desktop's MAC address is 2C:F0:5D:11:22:33 and it's usually at 192.168.1.50.")
            return reply, h.tools_called()

    async def second_session():
        async with harness(modules) as h:
            out = []
            for q in ("Turn on my gaming desktop.", "Turn on Sam's office computer."):
                reply = await h.ask(q)
                out.append((q, reply, h.tools_called(), h.calls("wake_computer")))
            return out

    try:
        first_reply, first_calls = asyncio.run(first_session())
        print(f"\n  remember -> {first_calls}\n      reply: {first_reply}")
        results = asyncio.run(second_session())
        received = listener.drain()
    finally:
        listener.close()
    for q, reply, called, _ in results:
        print(f"\n  {q} -> {called}\n      reply: {reply}")
    assert "remember" in first_calls or "remember_device" in first_calls
    assert "wake_computer" in results[0][2] and "wake_computer" in results[1][2]
    assert received[0] == b"\xff" * 6 + bytes.fromhex("2cf05d112233") * 16
    assert received[1] == WANT and len(received) == 2
    assert all(not c.get("ip") or c.get("ip") == "192.168.1.50" for c in results[0][3])
