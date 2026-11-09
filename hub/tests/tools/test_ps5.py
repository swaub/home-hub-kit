import asyncio

import pytest

from homehub.config import load_config, parse_config
from homehub.tools import result_texts
from homehub.tools.ps5 import NOT_PAIRED, Ps5, Ps5Error, build_module, credential, message, parse_reply, search


def test_discovery_messages_and_replies():
    assert message("SRCH") == b"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:00030010\n"
    wake = message("WAKEUP", {"user-credential": "123", "client-type": "vr"}).decode()
    assert wake.startswith("WAKEUP * HTTP/1.1\nuser-credential:123\nclient-type:vr\n") and wake.endswith("version:00030010\n")
    rest = parse_reply(b"HTTP/1.1 620 Server Standby\nhost-id:B41F4D8AAF8D\nhost-type:PS5\nhost-name:PS5-675\n")
    assert rest == {"code": "620", "on": False, "host-id": "B41F4D8AAF8D", "host-type": "PS5", "host-name": "PS5-675"}
    assert parse_reply(b"HTTP/1.1 200 Ok\nhost-type:PS5\n")["on"] is True and parse_reply(b"garbage") == {}


def test_the_wake_credential_is_the_registration_key_as_a_number():
    key = "0123456789abcdef".encode().hex()
    assert credential(key) == str(int("0123456789abcdef", 16))


def test_power_needs_pairing_and_the_tool_needs_the_feature(tmp_path):
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
    assert not build_module(cfg).available(cfg)
    assert build_module(parse_config({"features": {"ps5": True}})).available(parse_config({"features": {"ps5": True}}))
    with pytest.raises(Ps5Error, match="isn't paired"):
        Ps5(cfg).wake()
    with pytest.raises(Ps5Error, match="isn't paired"):
        Ps5(cfg).rest()
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    assert result_texts(asyncio.run(tools["ps5_power"]({"on": True})))[0] == NOT_PAIRED


@pytest.mark.live
def test_a_real_ps5_on_the_network_reports_its_state():
    if not load_config().features.ps5:
        pytest.skip("features.ps5 is off here")
    found = search(["255.255.255.255"])
    assert found and found[0]["host-type"] == "PS5"
    status = Ps5(load_config()).status()
    assert status.startswith(found[0]["host-name"]) and (status.endswith("is in rest mode.") or " is on" in status)


@pytest.mark.live
def test_rest_mode_needs_the_tv_and_never_opens_remote_play():
    cfg = load_config()
    console = Ps5(cfg)
    if not console.pairing():
        pytest.skip("no paired PS5 here")
    try:
        on = console.find()["on"]
    except Ps5Error:
        pytest.skip("the PS5 isn't on the network")
    if on:
        with pytest.raises(Ps5Error, match="through the TV's HDMI link"):
            console.rest()
    else:
        assert console.rest() == "The PS5 is already in rest mode."
    assert console.status().endswith("is in rest mode.") == (not on)
