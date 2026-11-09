import shutil

import pytest

from homehub.config import load_config, parse_config
from homehub.vpn import Vpn, VpnError, client_conf, parse_dump, without_peer

DUMP = ("SERVERPRIV\tSERVERPUB=\t51820\toff\n"
        "PHONEPUB=\tPSK\t50.1.2.3:60519\t10.77.0.2/32\t1794167000\t2048\t4096\toff\n"
        "SPAREPUB=\tPSK\t(none)\t10.77.0.4/32\t0\t0\t0\toff\n")
CONF = """[Interface]
Address = 10.77.0.1/24
ListenPort = 51820
PrivateKey = SECRET

[Peer]
PublicKey = PHONEPUB=
PresharedKey = A
AllowedIPs = 10.77.0.2/32

[Peer]
PublicKey = SPAREPUB=
PresharedKey = B
AllowedIPs = 10.77.0.4/32
"""


def test_the_wireguard_dump_reads_into_peers():
    state = parse_dump(DUMP)
    assert state["public"] == "SERVERPUB=" and state["port"] == 51820
    assert state["peers"][0] == {"public": "PHONEPUB=", "endpoint": "50.1.2.3:60519", "ip": "10.77.0.2", "handshake": 1794167000, "received": 2048, "sent": 4096}
    assert state["peers"][1]["endpoint"] == "" and state["peers"][1]["handshake"] == 0
    with pytest.raises(VpnError, match="isn't running"):
        parse_dump("")


def test_removing_a_peer_keeps_the_interface_and_every_other_peer():
    left = without_peer(CONF, "SPAREPUB=")
    assert "SPAREPUB=" not in left and "PHONEPUB=" in left and left.startswith("[Interface]\nAddress = 10.77.0.1/24") and "PrivateKey = SECRET" in left
    assert without_peer(CONF, "NOBODY=") == CONF and without_peer(CONF, "PHONEPUB") == CONF


def test_a_new_device_profile_reaches_only_the_vpn_through_the_home_name():
    conf = client_conf("PRIV", "10.77.0.5", "SERVERPUB=", "PSK", "home.duckdns.org:51820", "10.77.0.0/24")
    assert "Address = 10.77.0.5/32" in conf and "Endpoint = home.duckdns.org:51820" in conf and "AllowedIPs = 10.77.0.0/24" in conf
    assert "PersistentKeepalive = 25" in conf and "0.0.0.0/0" not in conf


def test_only_the_owner_manages_devices_and_names_are_plain(tmp_path):
    vpn = Vpn(parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}}))
    for who in (None, {"person": "kid", "role": "child", "ip": "10.77.0.9"}, {"person": "guest", "role": "", "ip": "10.77.0.8"}):
        with pytest.raises(VpnError, match="Only the household's owner"):
            vpn.act({"action": "add", "name": "phone"}, who)
        with pytest.raises(VpnError, match="Only the household's owner"):
            vpn.act({"action": "remove", "name": "phone"}, who)
    owner = {"person": "alex", "role": "owner", "ip": "10.77.0.2"}
    for bad in ("", "../etc", "a b;rm", "x" * 30, "-dash"):
        with pytest.raises(VpnError, match="letters, numbers and dashes"):
            vpn.act({"action": "add", "name": bad}, owner)
    with pytest.raises(VpnError, match="unknown VPN action"):
        vpn.act({"action": "export"}, owner)
    with pytest.raises(VpnError, match="home DuckDNS name"):
        vpn.endpoint(51820)


@pytest.mark.live
def test_the_real_vpn_lists_its_devices_without_keys():
    cfg = load_config()
    if not cfg.remote.port or not shutil.which("wg"):
        pytest.skip("no VPN on this computer")
    try:
        view = Vpn(cfg).overview({"person": "alex", "role": "owner", "ip": "10.77.0.3"})
    except VpnError as e:
        pytest.skip(f"the VPN can't be read here: {e}")
    assert view["devices"] and view["port"] == 51820 and view["can_manage"]
    assert all(set(d) == {"name", "ip", "person", "last_seen", "online", "received", "sent", "this_device", "managed"} for d in view["devices"])
    assert "Key" not in str(view) and "PSK" not in str(view)
