import secrets
import time

import pytest

from homehub.tools.notify import Ntfy, build_module


def test_notifier_is_off_until_configured(example_config):
    off = Ntfy("", "")
    assert not off.enabled
    with pytest.raises(RuntimeError, match="aren't set up"):
        off.send("hello")
    assert build_module(example_config).is_available(example_config) is False
    assert Ntfy("https://ntfy.example.com/", "home").url == "https://ntfy.example.com" and Ntfy("https://ntfy.example.com", "home").enabled


@pytest.mark.live
def test_live_round_trip_through_a_real_ntfy_server():
    topic = "homehub-test-" + secrets.token_hex(6)
    ntfy = Ntfy("https://ntfy.sh", topic)
    sent = ntfy.send("Fixed the office computer: the disk was full.", title="Home Hub test", tags=("white_check_mark",))
    assert sent.get("topic") == topic and sent.get("message", "").startswith("Fixed the office computer")
    recent = []
    for _ in range(10):
        time.sleep(1)
        recent = ntfy.recent()
        if recent:
            break
    assert any(m.get("message", "").startswith("Fixed the office computer") and m.get("title") == "Home Hub test" for m in recent)


def wait_for_messages(ntfy, seconds=12):
    for _ in range(seconds):
        time.sleep(1)
        recent = ntfy.recent()
        if recent:
            return recent
    return []


@pytest.mark.live
def test_a_due_reminder_is_pushed_to_the_phone_by_the_running_app(tmp_path):
    import os
    from homehub import household
    from homehub.app import App
    from homehub.config import parse_config

    topic = "homehub-test-" + secrets.token_hex(6)
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")},
                        "notify": {"url": "https://ntfy.sh", "topic": topic}})
    os.environ[household.ENV_VAR] = str(household.EXAMPLE_PATH)
    app = App(cfg).prepare()
    try:
        app.pusher.push({"kind": "reminder", "label": "Feed the bearded dragon", "owner_id": "alex"})
        recent = wait_for_messages(app.ntfy)
    finally:
        app.lock.release()
    assert [(m.get("title"), m.get("message")) for m in recent if m.get("event") == "message"] == [("Reminder", "Reminder: Feed the bearded dragon.")]
