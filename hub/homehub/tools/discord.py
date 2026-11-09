import asyncio
import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

API = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (home-hub, 0.1)"
TIMEOUT = 10
CHANNEL_CACHE_SECONDS = 300
PROMPT = ("Discord (through the household's bot, only in servers it was invited to): discord_channels lists text channels, "
          "discord_read reads a channel's latest messages (summarize, don't read every word), discord_send posts a message "
          "(it asks first). Personal DMs are not available.")
NOT_SET_UP = "Discord isn't set up: add [discord] with bot_token (from a bot you create in Discord's developer portal) to secrets.toml."


class DiscordError(RuntimeError):
    pass


def load_token(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return ""
    with open(path, "rb") as f:
        return str((tomllib.load(f).get("discord") or {}).get("bot_token", "")).strip()


class Discord:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cached = None

    def api(self, method, path, body=None, params=None):
        token = load_token(self.cfg)
        if not token:
            raise DiscordError(NOT_SET_UP)
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Authorization": f"Bot {token}", "User-Agent": USER_AGENT, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise DiscordError("Discord rejected the bot token.") from e
            if e.code == 403:
                raise DiscordError("The bot isn't allowed to see or post in that channel.") from e
            raise DiscordError(f"Discord said {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e

    def channels(self):
        if self.cached and time.monotonic() - self.cached[0] < CHANNEL_CACHE_SECONDS:
            return self.cached[1]
        found = []
        for guild in self.api("GET", "/users/@me/guilds") or []:
            for channel in self.api("GET", f"/guilds/{guild['id']}/channels") or []:
                if channel.get("type") in (0, 5):
                    found.append({"id": channel["id"], "name": channel["name"], "server": guild["name"]})
        self.cached = (time.monotonic(), found)
        return found

    def channel(self, name):
        wanted = name.strip().lstrip("#").lower()
        options = self.channels()
        exact = [c for c in options if c["name"].lower() == wanted or f"{c['server']} {c['name']}".lower() == wanted]
        loose = [c for c in options if wanted in c["name"].lower()]
        chosen = (exact or loose or [None])[0]
        if chosen is None:
            raise DiscordError(f"No channel called {name}. Channels: {', '.join('#' + c['name'] for c in options) or 'none (invite the bot to a server first)'}.")
        return chosen

    def read(self, name, count=15):
        chosen = self.channel(name)
        messages = self.api("GET", f"/channels/{chosen['id']}/messages", params={"limit": max(1, min(int(count), 50))}) or []
        lines = [f"{m['author'].get('global_name') or m['author']['username']}: {m.get('content') or '(attachment or embed)'}" for m in reversed(messages)]
        if lines and all(line.endswith("(attachment or embed)") for line in lines):
            lines.append("(message text is hidden: turn on Message Content Intent for the bot in the developer portal)")
        return f"#{chosen['name']} in {chosen['server']}:\n" + ("\n".join(lines) if lines else "no messages")

    def send(self, name, message):
        chosen = self.channel(name)
        self.api("POST", f"/channels/{chosen['id']}/messages", body={"content": message[:1900], "allowed_mentions": {"parse": []}})
        return f"Posted in #{chosen['name']}."


def build_module(cfg, client=None):
    discord = client or Discord(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (DiscordError, OSError, KeyError, ValueError) as e:
                return text_result(str(e), error=True)
        return call

    def list_channels(args):
        found = discord.channels()
        return "; ".join(f"#{c['name']} ({c['server']})" for c in found) if found else "The bot isn't in any servers yet."

    discord_channels = tool("discord_channels", "List the Discord text channels the household bot can see.", {"type": "object", "properties": {}})(run(list_channels))
    discord_read = tool("discord_read", "Read the latest messages in a Discord channel by name (count up to 50, default 15).",
                        {"type": "object", "properties": {"channel": {"type": "string"}, "count": {"type": "integer"}}, "required": ["channel"]})(
        run(lambda a: discord.read(a["channel"], a.get("count") or 15)))
    discord_send = tool("discord_send", "Post a message in a Discord channel as the household bot.",
                        {"type": "object", "properties": {"channel": {"type": "string"}, "message": {"type": "string"}}, "required": ["channel", "message"]})(
        run(lambda a: discord.send(a["channel"], a["message"])))
    return ToolModule(server="discord", prompt=PROMPT, available=lambda c: bool(load_token(c)), tools=[
        ToolSpec(fn=discord_channels, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="Discord"),
        ToolSpec(fn=discord_read, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="Discord"),
        ToolSpec(fn=discord_send, policy="confirm", scope=Scope.household(), outward=True,
                 describe=lambda a: f"post in #{str(a.get('channel', '')).lstrip('#')} on Discord: {str(a.get('message', ''))[:120]}"),
    ])
