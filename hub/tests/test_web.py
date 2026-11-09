import json
import urllib.error
import urllib.request

import pytest

from homehub.config import ConfigError, parse_config
from homehub.web import StatusPage


def get(page, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{page.port}{path}", timeout=5) as response:
        return response.status, response.headers.get("Content-Type"), response.read().decode("utf-8")


def test_status_page_serves_the_page_state_and_the_latest_lines():
    state = {"bedroom": "idle"}
    lists = {"shopping": ["crickets", "paper towels"]}
    page = StatusPage("127.0.0.1", 0, lambda: dict(state), "homehub 0.1.0 (test)", keep=3, lists=lambda: lists).start()
    try:
        status, kind, html = get(page, "/")
        assert status == 200 and kind.startswith("text/html") and "<title>Home Hub</title>" in html and "status.json" in html
        for i in range(5):
            page.on_event({"name": "transcript.line", "room": "bedroom", "kind": "you", "text": f"line {i}"})
        page.on_event({"name": "conversation.state", "room": "bedroom", "state": "thinking"})
        state["bedroom"] = "speaking"
        status, kind, body = get(page, "/status.json")
        data = json.loads(body)
        assert kind == "application/json" and data["state"] == {"bedroom": "speaking"} and data["version"] == "homehub 0.1.0 (test)"
        assert [l["text"] for l in data["lines"]] == ["line 2", "line 3", "line 4"] and data["lines"][0]["kind"] == "you"
        assert data["lists"] == {"shopping": ["crickets", "paper towels"]} and 'id="lists"' in html
        with pytest.raises(urllib.error.HTTPError) as missing:
            get(page, "/secrets.toml")
        assert missing.value.code == 404
    finally:
        page.stop()
    with pytest.raises(urllib.error.URLError):
        get(page, "/")


def test_web_config_is_off_by_default_and_checked():
    assert parse_config({}).web.port == 0 and parse_config({}).web.host == "127.0.0.1"
    assert parse_config({"web": {"port": 8765}}).web.port == 8765
    with pytest.raises(ConfigError):
        parse_config({"web": {"port": 70000}})
