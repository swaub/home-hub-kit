import asyncio
import socket

from homehub.service import Notifier


def listener(tmp_path):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(tmp_path / "notify"))
    sock.settimeout(2)
    return sock


def test_notifier_sends_systemd_messages_to_a_real_socket(tmp_path):
    sock = listener(tmp_path)
    try:
        notifier = Notifier(address=str(tmp_path / "notify"), watchdog_usec="60000000")
        assert notifier.enabled and notifier.interval() == 20.0 and notifier.watchdog_seconds == 60.0
        assert notifier.ready("ready in 9.7 s") and sock.recv(200) == b"READY=1\nSTATUS=ready in 9.7 s"
        assert notifier.watchdog() and sock.recv(200) == b"WATCHDOG=1"
        assert notifier.status("x" * 300) and len(sock.recv(400)) == 207
        assert notifier.stopping() and sock.recv(200) == b"STOPPING=1"
        assert notifier.sent[0].startswith("READY=1") and notifier.failures == 0
    finally:
        sock.close()
    gone = Notifier(address=str(tmp_path / "notify"))
    assert gone.watchdog() is False and gone.failures == 1


def test_notifier_is_quiet_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    notifier = Notifier()
    assert not notifier.enabled and notifier.ready() is False and not notifier.sent and notifier.interval() == 20.0
    assert Notifier(address="", watchdog_usec="10000000").interval() == 5.0
    assert Notifier(address="", watchdog_usec="1000000").interval() == 1.0


def test_app_heartbeat_feeds_the_watchdog(tmp_path):
    from homehub.app import App
    from homehub.config import parse_config

    sock = listener(tmp_path)
    try:
        cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}})
        app = App(cfg, notifier=Notifier(address=str(tmp_path / "notify"), watchdog_usec="4000000"))
        assert app.notifier.interval() == 2.0
        assert asyncio.run(app.heartbeat(force=True)) is True and sock.recv(100) == b"WATCHDOG=1"
        assert asyncio.run(app.heartbeat()) is False
        app.last_heartbeat -= 3
        assert asyncio.run(app.heartbeat()) is True and sock.recv(100) == b"WATCHDOG=1"
    finally:
        sock.close()


def test_the_app_starts_without_any_devices_and_a_due_timer_reaches_the_app(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    from homehub import household
    from homehub.app import App
    from homehub.config import parse_config

    monkeypatch.setenv(household.ENV_VAR, str(household.EXAMPLE_PATH))
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "features": {"google": False}})

    async def scenario():
        app = App(cfg).prepare()
        lines = []
        app.bus.start()
        app.bus.subscribe("transcript.line", lines.append)
        try:
            await app.start()
            assert app.checks["reminders"].startswith("shown in the app only") and app.remote is None
            app.scheduler.add("timer", datetime.now() + timedelta(seconds=1), "tea", owner_id="alex")
            for _ in range(50):
                if lines:
                    break
                await asyncio.sleep(0.1)
            assert [line["text"] for line in lines] == ["timer due: Your tea timer is done."]
            assert app.status_file.read(cfg.state_dir())["state"] == {"app": "off"}
        finally:
            await app.close()

    asyncio.run(scenario())
