import asyncio
import dataclasses
import http.client
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from homehub import household
from homehub.app import tool_modules
from homehub.brain.costs import CostTracker
from homehub.brain.permissions import Permissions
from homehub.config import ConfigError, load_config
from homehub.remote import RemoteApp, RemoteChat, parse_apps, parse_peers
from homehub.sysinfo import SystemStats
from homehub.tools import Registry
from homehub.tools.timers import Scheduler


@pytest.fixture
def loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=5)


@pytest.fixture
def chat(example_config, temp_storage):
    household.sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    cfg = dataclasses.replace(example_config, features=dataclasses.replace(example_config.features, google=False))
    registry = Registry(cfg, tool_modules(cfg, Scheduler(temp_storage), temp_storage, None))
    events = []
    chat = RemoteChat(cfg, registry, Permissions(registry, temp_storage), temp_storage, CostTracker(temp_storage),
                      lambda name, **payload: events.append((name, payload)))
    chat.events = events
    return chat


def serve(chat, loop, address="127.0.0.1", peers=None, power=None, apps=(), minecraft=None, packages=None, devices=None):
    status = lambda: {"version": "test", "lists": {"shopping": ["milk"]}}
    stats = SystemStats()
    return RemoteApp("127.0.0.1", 0, address, peers if peers is not None else {"127.0.0.1": "alex"}, chat, loop, status,
                     stats.sample, power, apps, minecraft, None, packages, devices=devices).start()


def request(app, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    sent = {"Content-Type": "application/json"} if isinstance(body, dict) else {}
    sent.update(headers or {})
    conn.request(method, path, body=data, headers=sent)
    response = conn.getresponse()
    payload = response.read()
    conn.close()
    return response.status, response.getheader("Content-Security-Policy"), payload


def state(app):
    return json.loads(request(app, "GET", "/api/state")[2])


def wait_for(app, test, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        s = state(app)
        if test(s):
            return s
        time.sleep(0.1)
    raise AssertionError(f"timed out; last state {state(app)}")


def test_peers_map_vpn_addresses_to_people_and_bad_entries_are_refused(tmp_path):
    assert parse_peers(["10.77.0.2=alex", " 10.77.0.3 = alex "]) == {"10.77.0.2": "alex", "10.77.0.3": "alex"}
    for bad in (["10.77.0.2"], ["=alex"], ["10.77.0.2="]):
        with pytest.raises(ValueError):
            parse_peers(bad)
    path = tmp_path / "config.toml"
    path.write_text('[remote]\nport = 8770\npeers = ["10.77.0.2"]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)
    path.write_text('[remote]\nport = 8770\napps = ["Cockpit=10.77.0.1:9090"]\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(path)


def test_only_listed_vpn_peers_reach_the_app(chat, loop):
    closed = serve(chat, loop, address="10.77.0.1")
    unknown = serve(chat, loop, peers={"10.77.0.2": "alex"})
    open_app = serve(chat, loop)
    try:
        for app in (closed, unknown):
            for path in ("/", "/api/state", "/app.js", "/api/system", "/api/apps"):
                assert request(app, "GET", path)[0] == 403
            assert request(app, "POST", "/api/chat", {"text": "what time is it"})[0] == 403
            assert request(app, "POST", "/api/power", {"action": "hub"})[0] == 403
        code, policy, page = request(open_app, "GET", "/")
        assert code == 200 and b"<title>Home</title>" in page and "frame-ancestors 'none'" in policy
        for asset in ("/app.js", "/mc.js", "/app.css", "/manifest.json", "/icon.svg", "/icon-192.png", "/icon-maskable.png", "/apple-touch-icon.png"):
            assert request(open_app, "GET", asset)[0] == 200
        assert request(open_app, "GET", "/nope")[0] == 404
        body = state(open_app)
        assert body["person"] == "alex" and body["home"]["lists"] == {"shopping": ["milk"]} and body["chat"]["lines"] == []
    finally:
        for app in (closed, unknown, open_app):
            app.stop()


def test_posts_must_be_small_same_origin_json(chat, loop):
    app = serve(chat, loop)
    try:
        assert request(app, "POST", "/api/chat", b"text=hi", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
        assert request(app, "POST", "/api/chat", {"text": "hi"}, {"Origin": "http://evil.example"})[0] == 403
        assert request(app, "POST", "/api/chat", {"text": "x" * 9000})[0] == 413
        assert request(app, "POST", "/api/chat", {"text": "  "})[0] == 400
        assert request(app, "POST", "/api/chat", b"[1]", {"Content-Type": "application/json"})[0] == 400
        assert request(app, "POST", "/api/chat", {"text": "what time is it"}, {"Origin": f"http://127.0.0.1:{app.port}"})[0] == 202
    finally:
        app.stop()


def test_a_typed_command_is_answered_on_the_hub_as_the_keys_owner(chat, loop, temp_storage):
    app = serve(chat, loop)
    try:
        assert request(app, "POST", "/api/chat", {"text": "set a timer for 10 minutes"})[0] == 202
        s = wait_for(app, lambda s: any(l["kind"] == "claude" for l in s["chat"]["lines"]) and not s["chat"]["busy"], 10)
        kinds = [l["kind"] for l in s["chat"]["lines"]]
        assert kinds == ["you", "claude"] and "10 minutes" in s["chat"]["lines"][1]["text"]
        made = temp_storage.reminders(owner_id="alex")
        assert len(made) == 1 and made[0]["notify_phone"] is True
        assert ("person.identified", {"room": "remote", "person_id": "alex", "how": "vpn_key"}) in chat.events
        assert any(name == "quick.reply" for name, _ in chat.events)
        assert chat.registry.context is None
    finally:
        app.stop()


def test_a_confirmation_becomes_buttons_and_only_the_current_question_can_be_answered(chat, loop):
    app = serve(chat, loop)
    try:
        answer = asyncio.run_coroutine_threadsafe(chat.confirm("alex", "text Sam saying hi", True), loop)
        s = wait_for(app, lambda s: s["chat"]["confirm"] is not None, 5)
        assert s["chat"]["confirm"]["summary"] == "text Sam saying hi"
        assert request(app, "POST", "/api/confirm", {"id": "wrong", "yes": True})[0] == 409
        assert request(app, "POST", "/api/confirm", {"id": s["chat"]["confirm"]["id"], "yes": True})[0] == 200
        assert answer.result(timeout=5) is True
        s = wait_for(app, lambda s: s["chat"]["confirm"] is None, 5)
        assert [l["text"] for l in s["chat"]["lines"]] == ["OK to text Sam saying hi?", "approved"]
        declined = asyncio.run_coroutine_threadsafe(chat.confirm("alex", "call Sam", True), loop)
        s = wait_for(app, lambda s: s["chat"]["confirm"] is not None, 5)
        assert request(app, "POST", "/api/confirm", {"id": s["chat"]["confirm"]["id"], "yes": False})[0] == 200
        assert declined.result(timeout=5) is False
    finally:
        app.stop()


def test_a_chat_waits_for_another_request_to_release_the_tools(chat, loop):
    from homehub.tools import CallContext

    other = CallContext(conversation_id="v1", person_id="sam")
    chat.registry.context = other
    app = serve(chat, loop)
    try:
        assert request(app, "POST", "/api/chat", {"text": "what time is it"})[0] == 202
        time.sleep(1.0)
        assert state(app)["chat"]["busy"] is True and chat.registry.context is other
        chat.registry.context = None
        s = wait_for(app, lambda s: not s["chat"]["busy"] and any(l["kind"] == "claude" for l in s["chat"]["lines"]), 10)
        assert s["chat"]["lines"][-1]["kind"] == "claude"
    finally:
        app.stop()


@pytest.mark.claude
def test_anything_else_is_answered_by_claude_in_a_short_written_reply(chat, loop):
    app = serve(chat, loop)
    try:
        assert request(app, "POST", "/api/chat", {"text": "What is two plus two? Answer with the number only."})[0] == 202
        s = wait_for(app, lambda s: not s["chat"]["busy"] and any(l["kind"] in ("claude", "error") for l in s["chat"]["lines"]), 120)
        reply = s["chat"]["lines"][-1]
        assert reply["kind"] == "claude" and "4" in reply["text"]
        assert any(name == "brain.reply" for name, _ in chat.events)
    finally:
        app.stop()
        asyncio.run_coroutine_threadsafe(chat.close(), loop).result(timeout=30)


@pytest.mark.claude
def test_when_claude_refuses_the_chat_says_so_and_quick_commands_and_buttons_keep_working(chat, loop, temp_storage, tmp_path):
    factory = chat.options_factory

    def signed_out(person):
        options = factory(person)
        options.env = {**options.env, "CLAUDE_CONFIG_DIR": str(tmp_path)}
        return options

    chat.options_factory = signed_out
    app = serve(chat, loop)
    try:
        assert state(app)["chat"]["claude_down"] is None
        assert request(app, "POST", "/api/chat", {"text": "What is two plus two?"})[0] == 202
        s = wait_for(app, lambda s: not s["chat"]["busy"] and any(l["kind"] == "error" for l in s["chat"]["lines"]), 120)
        assert s["chat"]["claude_down"] and s["chat"]["lines"][-1]["text"] == s["chat"]["claude_down"]
        assert request(app, "POST", "/api/chat", {"text": "set a timer for 10 minutes"})[0] == 202
        s = wait_for(app, lambda s: not s["chat"]["busy"] and s["chat"]["lines"][-1]["kind"] == "claude", 10)
        assert "10 minutes" in s["chat"]["lines"][-1]["text"] and len(temp_storage.reminders(owner_id="alex")) == 1
        code, _, reply = request(app, "POST", "/api/control", {"action": "list.add", "list": "shopping", "item": "milk"})
        assert code == 200 and json.loads(reply)["ok"] and temp_storage.list_items("shopping")
        assert state(app)["chat"]["claude_down"]
    finally:
        app.stop()
        asyncio.run_coroutine_threadsafe(chat.close(), loop).result(timeout=30)


def test_the_system_view_reads_this_machines_real_counters_and_rates_appear_on_the_second_look(chat, loop):
    app = serve(chat, loop)
    try:
        first = json.loads(request(app, "GET", "/api/system")[2])
        time.sleep(0.5)
        second = json.loads(request(app, "GET", "/api/system")[2])
        assert first["cpu"]["percent"] is None and second["cpu"]["percent"] is not None and 0 <= second["cpu"]["percent"] <= 100
        assert second["uptime_seconds"] > 0 and abs(second["booted_at"] + second["uptime_seconds"] - time.time()) < 5
        assert 0 < second["memory"]["used"] < second["memory"]["total"]
        assert 0 < second["disk"]["used"] < second["disk"]["total"]
        assert second["cpu"]["cores"] >= 1 and len(second["cpu"]["load"]) == 3
        assert second["network"] and all(n["name"] != "lo" and not n["name"].startswith(("veth", "docker", "br-")) for n in second["network"])
        assert second["network"][0]["internet"] and sum(n["internet"] for n in second["network"]) == 1
        assert all(n["rx_rate"] is not None and n["rx_rate"] >= 0 for n in second["network"])
        assert second["power"] is False
    finally:
        app.stop()


def test_power_buttons_only_take_the_known_actions_from_the_same_origin(chat, loop):
    asked = []

    def power(action, person_id, wake_at=None):
        asked.append((action, person_id, wake_at))
        return True, "Restarting"

    app = serve(chat, loop, power=power)
    try:
        assert json.loads(request(app, "GET", "/api/system")[2])["power"] is True
        assert request(app, "POST", "/api/power", {"action": "shutdown"})[0] == 400
        assert request(app, "POST", "/api/power", {"action": "hub"}, {"Origin": "http://evil.example"})[0] == 403
        assert request(app, "POST", "/api/power", b"action=hub", {"Content-Type": "application/x-www-form-urlencoded"})[0] == 415
        assert asked == []
        code, _, body = request(app, "POST", "/api/power", {"action": "assistant"})
        assert code == 202 and json.loads(body) == {"ok": True, "message": "Restarting"}
        assert asked == [("assistant", "alex", None)]
        request(app, "POST", "/api/power", {"action": "off", "wake_at": 1800000000})
        request(app, "POST", "/api/power", {"action": "hub", "wake_at": 1800000000})
        assert asked[1:] == [("off", "alex", 1800000000), ("hub", "alex", None)]
    finally:
        app.stop()
    refused = serve(chat, loop, power=lambda action, person_id, wake_at=None: (False, "not allowed here"))
    try:
        code, _, body = request(refused, "POST", "/api/power", {"action": "hub"})
        assert code == 503 and json.loads(body)["message"] == "not allowed here"
    finally:
        refused.stop()


def test_apps_are_listed_with_whether_they_answer(chat, loop):
    assert parse_apps(["Cockpit=https://10.77.0.1:9090", "Photos = http://10.77.0.1"]) == [
        {"name": "Cockpit", "url": "https://10.77.0.1:9090", "host": "10.77.0.1", "port": 9090},
        {"name": "Photos", "url": "http://10.77.0.1", "host": "10.77.0.1", "port": 80}]
    for bad in (["Cockpit"], ["=https://x"], ["Cockpit=ftp://x"], ["Cockpit=https://"]):
        with pytest.raises(ValueError):
            parse_apps(bad)
    listening = serve(chat, loop)
    app = serve(chat, loop, apps=parse_apps([f"Itself=http://127.0.0.1:{listening.port}", "Closed=http://127.0.0.1:9"]))
    try:
        assert json.loads(request(app, "GET", "/api/apps")[2])["apps"] == [
            {"name": "Itself", "url": f"http://127.0.0.1:{listening.port}", "up": True},
            {"name": "Closed", "url": "http://127.0.0.1:9", "up": False}]
    finally:
        app.stop()
        listening.stop()


def reverse_proxy(backend_port):
    class Proxy(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", backend_port, timeout=2)
                conn.request("GET", self.path)
                answer = conn.getresponse()
                code, body = answer.status, answer.read()
                conn.close()
            except OSError:
                code, body = 502, b"bad gateway"
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_apps_behind_a_reverse_proxy_are_down_when_the_proxy_cannot_reach_them(chat, loop):
    backend = serve(chat, loop)
    live, dead = reverse_proxy(backend.port), reverse_proxy(9)
    apps = parse_apps([f"Live=http://localhost:{live.server_address[1]}", f"Dead=http://localhost:{dead.server_address[1]}",
                       f"Missing=http://localhost:{live.server_address[1]}/nope", "Nothing=http://localhost:9"])
    app = serve(chat, loop, apps=apps)
    try:
        started = time.monotonic()
        listed = json.loads(request(app, "GET", "/api/apps")[2])["apps"]
        assert {a["name"]: a["up"] for a in listed} == {"Live": True, "Dead": False, "Missing": True, "Nothing": False}
        assert [a["name"] for a in listed] == ["Live", "Dead", "Missing", "Nothing"] and time.monotonic() - started < 5
    finally:
        app.stop()
        backend.stop()
        for server in (live, dead):
            server.shutdown()
            server.server_close()


def test_home_buttons_cancel_timers_and_tick_off_list_items_as_the_keys_owner(chat, loop, temp_storage):
    app = serve(chat, loop)
    try:
        assert state(app)["controls"] == {"timers": True, "lists": True, "tv": chat.registry.spec("mcp__tv__tv_remote") is not None,
                                          "roku": chat.registry.spec("mcp__roku__roku_remote") is not None,
                                          "music": chat.registry.spec("mcp__spotify__spotify_control") is not None,
                                          "ps5": chat.registry.spec("mcp__ps5__ps5_power") is not None,
                                          "proxmox": chat.registry.spec("mcp__proxmox__proxmox_power") is not None,
                                          "weather": chat.registry.spec("mcp__weather__get_weather") is not None}
        assert request(app, "POST", "/api/chat", {"text": "set a timer for 10 minutes"})[0] == 202
        wait_for(app, lambda s: not s["chat"]["busy"] and len(s["chat"]["lines"]) == 2, 10)
        timer = temp_storage.reminders(owner_id="alex")[0]
        code, _, body = request(app, "POST", "/api/control", {"action": "timer.cancel", "id": timer["id"]})
        assert code == 200 and json.loads(body)["ok"] and temp_storage.reminders() == []
        assert json.loads(request(app, "POST", "/api/control", {"action": "timer.cancel", "id": timer["id"]})[2])["ok"] is False
        code, _, body = request(app, "POST", "/api/control", {"action": "list.add", "list": "shopping", "item": "oat milk"})
        assert code == 200 and json.loads(body)["ok"]
        request(app, "POST", "/api/control", {"action": "list.add", "list": "shopping", "item": "milk"})
        assert [i["text"] for i in temp_storage.list_items("shopping")] == ["oat milk", "milk"]
        assert request(app, "POST", "/api/control", {"action": "list.remove", "list": "shopping", "item": "milk"})[0] == 200
        assert [i["text"] for i in temp_storage.list_items("shopping")] == ["oat milk"]
        assert ("person.identified", {"room": "remote", "person_id": "alex", "how": "vpn_key"}) in chat.events
        assert any(name == "permission.decided" and p["tool"] == "mcp__lists__remove_from_list" and p["allowed"] for name, p in chat.events)
        assert chat.registry.context is None
    finally:
        app.stop()


def test_home_buttons_refuse_anything_outside_the_fixed_set(chat, loop, temp_storage):
    app = serve(chat, loop)
    try:
        for body in ({"action": "shell", "cmd": "ls"}, {"action": "tv.key", "key": "PowerOff"}, {"action": "roku.key", "key": "PowerOff"}, {"action": "music.control", "do": "delete"},
                     {"action": "list.add", "list": "shopping", "item": " "}, {"action": "list.add", "list": "x" * 61, "item": "milk"},
                     {"action": "timer.cancel", "id": "all"}):
            code, _, reply = request(app, "POST", "/api/control", body)
            assert code == 422 and json.loads(reply)["ok"] is False, body
        assert temp_storage.list_items("shopping") == []
        assert request(app, "POST", "/api/control", {"action": "list.add", "list": "shopping", "item": "milk"},
                       {"Origin": "http://evil.example"})[0] == 403
    finally:
        app.stop()
    closed = serve(chat, loop, address="10.77.0.1")
    try:
        assert request(closed, "POST", "/api/control", {"action": "list.add", "list": "shopping", "item": "milk"})[0] == 403
    finally:
        closed.stop()


def test_minecraft_files_can_be_browsed_edited_uploaded_and_downloaded_only_inside_the_server(chat, loop, tmp_path):
    from homehub.minecraft import Minecraft

    folder = tmp_path / "mc" / "world"
    (folder / "data").mkdir(parents=True)
    (folder / "server.json").write_text(json.dumps({"name": "World", "slug": "world", "loader": "paper", "version": "1.21.1", "mods": []}))
    (tmp_path / "mc" / "secret.txt").write_text("keep out")
    app = serve(chat, loop, minecraft=Minecraft(tmp_path / "mc"))
    bare = serve(chat, loop)
    try:
        assert json.loads(request(bare, "GET", "/api/features")[2]) == {"minecraft": False, "router": False, "packages": False, "devices": []}
        assert request(bare, "GET", "/api/mc")[0] == 404
        for name in ("proxmox", "tv", "nothing"):
            assert request(bare, "GET", f"/api/dev/{name}")[0] == 404 and request(bare, "POST", f"/api/dev/{name}", {"action": "reboot"})[0] == 404
        assert json.loads(request(app, "GET", "/api/features")[2])["minecraft"] is True
        assert request(app, "POST", "/api/mc/s/world/file", {"op": "write", "path": "server.properties", "text": "motd=hi\n" * 2000})[0] == 200
        listing = json.loads(request(app, "GET", "/api/mc/s/world/files?path=")[2])
        assert [e["name"] for e in listing["entries"]] == ["server.properties"]
        code, _, body = request(app, "PUT", "/api/mc/s/world/upload?dir=plugins&name=x.jar", b"PK\x03\x04\x00",
                                {"Content-Type": "application/octet-stream"})
        assert code == 200 and json.loads(body)["saved"] == "x.jar"
        code, _, body = request(app, "GET", "/api/mc/s/world/download?path=plugins/x.jar")
        assert code == 200 and body == b"PK\x03\x04\x00"
        assert request(app, "GET", "/api/mc/s/world/files?path=../secret.txt")[0] == 400
        assert request(app, "GET", "/api/mc/s/world/download?path=../secret.txt")[0] == 404
        assert request(app, "GET", "/api/mc/s/nope/files?path=")[0] == 400
        assert request(app, "PUT", "/api/mc/s/world/upload?dir=&name=y.jar", b"abc", {"Content-Type": "application/octet-stream",
                                                                                     "Origin": "http://evil.example"})[0] == 403
        assert request(app, "PUT", "/api/mc/s/world/upload?dir=&name=y.jar", b"abc", {"Content-Type": "text/plain"})[0] == 415
        assert request(app, "POST", "/api/mc/s/world/file", {"op": "delete", "path": ""})[0] == 400
        assert (tmp_path / "mc" / "secret.txt").read_text() == "keep out"
    finally:
        app.stop()
        bare.stop()
    closed = serve(chat, loop, address="10.77.0.1", minecraft=Minecraft(tmp_path / "mc"))
    try:
        assert request(closed, "GET", "/api/mc/s/world/files?path=")[0] == 403
        assert request(closed, "PUT", "/api/mc/s/world/upload?dir=&name=z.jar", b"abc", {"Content-Type": "application/octet-stream"})[0] == 403
    finally:
        closed.stop()


def test_every_element_the_app_scripts_look_up_exists_in_the_page():
    import re
    from importlib import resources

    folder = resources.files("homehub.remote_app")
    page = folder.joinpath("app.html").read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', page))
    for script in ("app.js", "mc.js", "packages.js"):
        used = set(re.findall(r'\$\("([a-z0-9-]+)"\)', folder.joinpath(script).read_text(encoding="utf-8")))
        assert used and used <= ids, (script, sorted(used - ids))


def test_every_fixed_control_the_app_scripts_send_exists_and_every_button_value_is_accepted():
    import re
    from importlib import resources

    from homehub.remote import control_call

    folder = resources.files("homehub.remote_app")
    page = folder.joinpath("app.html").read_text(encoding="utf-8")
    scripts = "".join(folder.joinpath(name).read_text(encoding="utf-8") for name in ("app.js", "mc.js", "packages.js"))
    tools = "".join(path.read_text(encoding="utf-8") for path in resources.files("homehub.tools").iterdir() if path.name.endswith(".py"))
    sample = {"id": "abc", "list": "shopping", "item": "milk", "key": "Home", "do": "pause", "value": 20, "on": True, "name": "Netflix"}
    actions = set(re.findall(r'\b(?:control|press|powerPress)\((?:[^,()"]+, )?"([a-z0-9]+\.[a-z_]+)"', scripts))
    assert {"timer.cancel", "list.add", "music.now", "tv.key", "roku.key", "music.control", "ps5.power"} <= actions
    for action in actions:
        tool, _ = control_call(action, sample)
        assert f'"{tool.split("__")[-1]}"' in tools, (action, tool)
    bindings = re.findall(r'querySelectorAll\("\[data-([a-z0-9-]+)\]"\)\.forEach\(\(b\) => b\.addEventListener\("click", (?:async )?\(\) => \{?\s*'
                          r'(?:await )?press\(b, "([a-z0-9.]+)", \{ (\w+): b\.dataset\.\w+ \}', scripts)
    assert {(attribute, action) for attribute, action, _ in bindings} >= {("key", "tv.key"), ("roku-key", "roku.key"), ("music", "music.control")}
    for attribute, action, field in bindings:
        values = re.findall(rf' data-{attribute}="([^"]*)"', page)
        assert values, attribute
        for value in values:
            control_call(action, {field: value})


def test_packages_are_only_for_the_email_owner_and_can_be_added_and_hidden(chat, loop, private_config, temp_storage):
    from homehub.packages import PackageTracker

    tracker = PackageTracker(private_config, temp_storage, "alex")
    app = serve(chat, loop, packages=tracker)
    other = serve(chat, loop, peers={"127.0.0.1": "sam"}, packages=tracker)
    try:
        assert json.loads(request(app, "GET", "/api/features")[2])["packages"] is True
        assert json.loads(request(other, "GET", "/api/features")[2])["packages"] is False
        assert request(other, "GET", "/api/packages")[0] == 403
        assert request(other, "POST", "/api/packages", {"action": "add", "number": "1Z999AA10123456784"})[0] == 403
        state0 = json.loads(request(app, "GET", "/api/packages")[2])
        assert state0["ready"] is False and state0["packages"] == []
        code, _, body = request(app, "POST", "/api/packages", {"action": "add", "number": "1Z999AA10123456784", "what": "plant pot"})
        added = json.loads(body)["packages"]
        assert code == 200 and added[0]["carrier"] == "UPS" and added[0]["item"] == "plant pot" and added[0]["label"] == "Tracking"
        code, _, body = request(app, "POST", "/api/packages", {"action": "refresh"})
        assert code == 400 and "Gmail isn't connected" in json.loads(body)["error"]
        code, _, body = request(app, "POST", "/api/packages", {"action": "connect_url"})
        assert code == 400 and "isn't connected" in json.loads(body)["error"]
        assert request(app, "POST", "/api/packages", {"action": "hide", "key": "tracking:1Z999AA10123456784"})[0] == 200
        assert json.loads(request(app, "GET", "/api/packages")[2])["packages"] == []
        assert request(app, "POST", "/api/packages", {"action": "hide", "key": "nope"})[0] == 400
        assert request(app, "POST", "/api/packages", {"action": "explode"})[0] == 400
    finally:
        app.stop()
        other.stop()


def make_cert(folder, name):
    import subprocess

    cert, key = folder / f"{name}.pem", folder / f"{name}.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-days", "2",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost", "-keyout", str(key), "-out", str(cert)],
                   check=True, capture_output=True)
    return cert, key


def test_https_serves_the_app_reloads_a_renewed_certificate_and_the_old_address_forwards(chat, loop, tmp_path):
    import http.client
    import shutil
    import ssl

    cert, key = make_cert(tmp_path, "first")
    live_cert, live_key = tmp_path / "live.pem", tmp_path / "live.key"
    shutil.copy(cert, live_cert)
    shutil.copy(key, live_key)
    status = lambda: {"version": "test", "lists": {}}
    app = RemoteApp("127.0.0.1", 0, "127.0.0.1", {"127.0.0.1": "alex"}, chat, loop, status,
                    tls={"port": 0, "cert": str(live_cert), "key": str(live_key), "name": "localhost", "host": "127.0.0.1"}).start()

    def fetch(trusted, path="/"):
        context = ssl.create_default_context(cafile=str(trusted))
        conn = http.client.HTTPSConnection("localhost", app.tls["port"], context=context, timeout=10)
        conn.request("GET", path)
        serial = conn.sock.getpeercert()["serialNumber"]
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, response.getheader("Strict-Transport-Security"), body, serial

    try:
        code, hsts, page, first_serial = fetch(cert)
        assert code == 200 and b"<title>Home</title>" in page and hsts == "max-age=31536000"
        assert fetch(cert, "/sw.js")[0] == 200 and fetch(cert, "/api/state")[0] == 200
        code, _, body = request(app, "GET", "/")
        assert code == 308 and body == b""
        plain = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
        plain.request("GET", "/")
        assert plain.getresponse().getheader("Location") == f"https://localhost:{app.tls['port']}/"
        plain.close()
        assert request(app, "GET", "/api/state")[0] == 200
        new_cert, new_key = make_cert(tmp_path, "renewed")
        shutil.copy(new_cert, live_cert)
        shutil.copy(new_key, live_key)
        code, _, _, renewed_serial = fetch(new_cert)
        assert code == 200 and renewed_serial != first_serial
        raw = __import__("socket").create_connection(("127.0.0.1", app.tls["port"]), timeout=5)
        raw.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        raw.close()
        assert fetch(new_cert)[0] == 200
    finally:
        app.stop()


def test_console_and_server_buttons_map_to_their_tools_with_a_plain_on_or_off():
    from homehub.remote import control_call

    assert control_call("ps5.now", {}) == ("mcp__ps5__ps5_status", {})
    assert control_call("ps5.power", {"on": 1}) == ("mcp__ps5__ps5_power", {"on": True})
    assert control_call("proxmox.now", {"extra": "ignored"}) == ("mcp__proxmox__proxmox_status", {})
    assert control_call("proxmox.power", {"on": "", "cmd": "reboot"}) == ("mcp__proxmox__proxmox_power", {"action": "shutdown"})
    assert control_call("proxmox.power", {"on": True}) == ("mcp__proxmox__proxmox_power", {"action": "on"})


def test_the_home_weather_chip_asks_the_weather_tool_for_the_household_town_only():
    from homehub.remote import control_call
    from homehub.tools.weather import format_report

    assert control_call("weather.now", {}) == ("mcp__weather__get_weather", {"days": 1})
    assert control_call("weather.now", {"place": "Paris, France", "days": 7}) == ("mcp__weather__get_weather", {"days": 1})
    report = format_report({"label": "Springfield, Illinois"},
                           {"current": {"temperature_2m": 51.6, "apparent_temperature": 49.2, "weather_code": 2, "wind_speed_10m": 4.4},
                            "daily": {"time": ["2026-11-09"], "weather_code": [61], "temperature_2m_max": [55.2], "temperature_2m_min": [40.1],
                                      "precipitation_probability_max": [40], "precipitation_sum": [0.1], "wind_speed_10m_max": [9.0]}},
                           {"temp": "F", "wind": "mph", "rain": "in"})
    first, today = report.split("\n")
    assert "right now: 52°F (feels like 49), partly cloudy, wind" in first
    assert today.startswith("Today (2026-11-09): light rain, high 55, low 40, chance of rain 40%")


def test_the_weather_button_answers_with_the_forecast_or_says_the_service_is_down(chat, loop):
    app = serve(chat, loop)
    try:
        code, _, body = request(app, "POST", "/api/control", {"action": "weather.now"})
        reply = json.loads(body)
        assert not reply["text"].startswith("Untrusted content")
        if code == 200:
            assert reply["ok"] and "right now: " in reply["text"] and "\nToday (" in reply["text"]
        else:
            assert code == 422 and reply["ok"] is False and reply["text"]
    finally:
        app.stop()


def test_the_offline_shell_is_served_and_every_switch_and_icon_button_has_a_name(chat, loop):
    import re
    from importlib import resources

    folder = resources.files("homehub.remote_app")
    worker = folder.joinpath("sw.js").read_text(encoding="utf-8")
    shell = re.findall(r'"(/[^"]*)"', re.search(r"const SHELL_FILES = \[(.+?)\];", worker, re.S).group(1))
    assert "/" in shell and "/app.js" in shell and "/app.css" in shell
    assert "/api" not in worker
    app = serve(chat, loop)
    try:
        for path in shell:
            code, csp, payload = request(app, "GET", path)
            assert code == 200 and payload, path
    finally:
        app.stop()
    page = folder.joinpath("app.html").read_text(encoding="utf-8")
    assert "Not connected — is the VPN on?" in page
    for switch in re.findall(r'<label class="switch"><input [^>]+>', page):
        assert "aria-label=" in switch, switch
    unnamed = re.findall(r"<button(?![^>]*aria-label)[^>]*>\s*<svg[^>]*>\s*<use[^>]*/>\s*</svg>\s*</button>", page)
    assert not unnamed, unnamed
    scripts = "".join(folder.joinpath(name).read_text(encoding="utf-8") for name in ("app.js", "mc.js", "packages.js"))
    assert not re.search(r"\b(?:confirm|prompt|alert)\(", scripts.replace("mcAsk(", ""))
    assert not re.search(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", scripts.replace("★ ${d.score", "")), "emoji in the app scripts"


def test_device_pages_read_and_act_through_their_own_route(chat, loop, tmp_path):
    from homehub.config import parse_config
    from homehub.tools.network import check_port
    from homehub.tools.proxmox import WEB_PORT, Proxmox

    if check_port("127.0.0.1", WEB_PORT, timeout=0.5)[0]:
        pytest.skip("something on this computer listens on the Proxmox web port")
    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "proxmox": {"host": "127.0.0.1", "name": "Lab box"}})
    app = serve(chat, loop, devices={"proxmox": Proxmox(cfg)})
    try:
        assert json.loads(request(app, "GET", "/api/features")[2])["devices"] == ["proxmox"]
        assert json.loads(request(app, "GET", "/api/dev/proxmox")[2]) == {"on": False, "name": "Lab box"}
        code, _, body = request(app, "POST", "/api/dev/proxmox", {"action": "reboot"})
        assert code == 200 and json.loads(body) == {"text": "The Lab box is already off."}
        code, _, body = request(app, "POST", "/api/dev/proxmox", {"action": "explode"})
        assert code == 400 and json.loads(body) == {"error": "unknown Proxmox action"}
        assert request(app, "GET", "/api/dev/tv")[0] == 404
    finally:
        app.stop()


def test_the_app_scripts_never_declare_the_same_top_level_name():
    import re
    from importlib import resources

    folder = resources.files("homehub.remote_app")
    seen = {}
    for script in ("app.js", "mc.js", "packages.js", "register.js"):
        for name in re.findall(r"^(?:async\s+)?(?:const|let|var|function|class)\s+([A-Za-z_$][\w$]*)", folder.joinpath(script).read_text(encoding="utf-8"), re.M):
            assert name not in seen, f"{name} is declared in both {seen[name]} and {script}"
            seen[name] = script


def test_every_page_in_the_app_is_routable_and_every_route_has_a_page():
    import re
    from importlib import resources

    folder = resources.files("homehub.remote_app")
    page = folder.joinpath("app.html").read_text(encoding="utf-8")
    script = folder.joinpath("app.js").read_text(encoding="utf-8")
    sections = set(re.findall(r'<section id="([a-z0-9]+)" class="view', page))
    titles = re.search(r"const TITLES = \{(.+?)\};", script, re.S).group(1)
    routed = set(re.findall(r"(\w+): \"", titles))
    parents = re.search(r"const PARENT = \{(.+?)\};", script, re.S).group(1)
    assert sections == routed, (sorted(sections - routed), sorted(routed - sections))
    assert set(re.findall(r"(\w+): \"(\w+)\"", parents)) and all(child in routed and parent in routed for child, parent in re.findall(r"(\w+): \"(\w+)\"", parents))
    assert set(re.findall(r'href="#([a-z0-9]+)"', page)) <= routed


def test_media_uploads_go_through_put_pieces_on_the_app_address(chat, loop, tmp_path):
    from homehub.config import parse_config
    from homehub.media import MediaLibrary

    cfg = parse_config({"paths": {"data_dir": str(tmp_path / "data"), "state_dir": str(tmp_path / "state")}, "media": {"folder": str(tmp_path / "media")}})
    (tmp_path / "media" / "Movies").mkdir(parents=True)
    app = serve(chat, loop, devices={"media": MediaLibrary(cfg)})
    try:
        data = b"movie-bytes" * 1000
        code, _, body = request(app, "POST", "/api/dev/media", {"action": "start", "kind": "movie", "name": "clip.mp4", "size": len(data), "title": "Clip", "year": 2020})
        upload = json.loads(body)
        assert code == 200 and upload["path"] == "Movies/Clip (2020)/Clip (2020).mp4"
        url = f"/api/media/upload/{upload['id']}?offset=0"
        assert request(app, "PUT", url, data[:5000], headers={"Content-Type": "text/plain"})[0] == 415
        assert request(app, "PUT", url, data[:5000], headers={"Content-Type": "application/octet-stream", "Origin": "https://evil.example"})[0] == 403
        code, _, body = request(app, "PUT", url, data[:5000], headers={"Content-Type": "application/octet-stream"})
        assert code == 200 and json.loads(body) == {"received": 5000}
        code, _, body = request(app, "PUT", f"/api/media/upload/{upload['id']}?offset=5000", data[5000:], headers={"Content-Type": "application/octet-stream"})
        assert json.loads(body) == {"received": len(data)}
        code, _, body = request(app, "POST", "/api/dev/media", {"action": "finish", "id": upload["id"]})
        assert code == 200 and (tmp_path / "media/Movies/Clip (2020)/Clip (2020).mp4").read_bytes() == data
        assert request(app, "PUT", "/api/media/upload/nope?offset=0", b"x", headers={"Content-Type": "application/octet-stream"})[0] == 400
    finally:
        app.stop()


def test_document_uploads_go_through_one_put_on_the_app_address(chat, loop, tmp_path):
    from homehub.config import parse_config
    from homehub.tools.paperless import PaperlessPanel

    bare = serve(chat, loop)
    app = serve(chat, loop, devices={"docs": PaperlessPanel(parse_config({"paths": {"data_dir": str(tmp_path / "data")}}))})
    try:
        headers = {"Content-Type": "application/octet-stream"}
        assert request(bare, "PUT", "/api/docs/upload?name=a.pdf", b"%PDF-1.4", headers)[0] == 404
        assert request(app, "PUT", "/api/docs/upload?name=a.pdf", b"%PDF-1.4", {"Content-Type": "text/plain"})[0] == 415
        assert request(app, "PUT", "/api/docs/upload?name=a.pdf", b"%PDF-1.4", {**headers, "Origin": "https://evil.example"})[0] == 403
        code, _, body = request(app, "PUT", "/api/docs/upload?name=a.exe", b"MZ", headers)
        assert code == 400 and "takes PDFs" in json.loads(body)["error"]
        code, _, body = request(app, "PUT", "/api/docs/upload?name=a.pdf", b"%PDF-1.4", headers)
        assert code == 400 and "isn't set up" in json.loads(body)["error"]
    finally:
        app.stop()
        bare.stop()
