import shutil

import pytest

from homehub.config import parse_config
from homehub.tools.bluetooth import Bluetooth, BluetoothError, build_module, parse_devices, parse_info

LIST = "Device 02:00:00:00:00:07 Kyle’s Fun Time Room BT\nDevice 02:00:00:00:00:01 JBL Flip 6\nnoise line\n"
INFO = "Device 02:00:00:00:00:01 (public)\n\tName: JBL Flip 6\n\tAlias: JBL Flip 6\n\tPaired: yes\n\tTrusted: yes\n\tConnected: no\n\tIcon: audio-card\n"


def test_bluetoothctl_output_is_read():
    assert parse_devices(LIST) == [{"mac": "02:00:00:00:00:07", "name": "Kyle’s Fun Time Room BT"}, {"mac": "02:00:00:00:00:01", "name": "JBL Flip 6"}]
    assert parse_info(INFO) == {"name": "JBL Flip 6", "alias": "JBL Flip 6", "paired": "yes", "trusted": "yes", "connected": "no", "icon": "audio-card"}
    pool = parse_devices(LIST)
    assert Bluetooth(exe="/bin/true").find("jbl", pool)["mac"] == "02:00:00:00:00:01"
    with pytest.raises(BluetoothError, match="No paired Bluetooth device called Bose"):
        Bluetooth(exe="/bin/true").find("Bose", pool)


def test_pairing_asks_first_and_the_feature_switch_hides_the_tools():
    assert not build_module().available(parse_config({}))
    specs = {spec.name: spec for spec in build_module().tools}
    assert specs["bt_pair"].policy == "confirm" and specs["bt_pair"].describe({"name": "JBL Flip 6"}) == "pair the hub with the Bluetooth device JBL Flip 6"
    assert {name: spec.policy for name, spec in specs.items() if name != "bt_pair"} == {"bt_devices": "auto", "bt_connect": "auto", "bt_disconnect": "auto"}


@pytest.mark.live
def test_the_real_adapter_lists_paired_devices():
    if not shutil.which("bluetoothctl"):
        pytest.skip("no bluetoothctl here")
    assert isinstance(Bluetooth().paired(), list)
