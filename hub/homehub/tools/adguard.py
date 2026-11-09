import asyncio
import base64
import json
import re
import tomllib
import urllib.error
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("AdGuard Home on the hub blocks ads and trackers for the whole house. adguard_status gives today's numbers and the most "
          "blocked sites; adguard_pause turns blocking off for some minutes (or back on with minutes 0); adguard_unblock always allows a "
          "site that blocking broke, adguard_block blocks one. Unblocking is the fix when an app or site fails at home but works on mobile data.")
TIMEOUT = 8
MAX_PAUSE_MINUTES = 240
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class AdGuardError(RuntimeError):
    pass


def load_login(cfg):
    path = cfg.data_dir() / "secrets.toml"
    if not path.is_file():
        return None
    with open(path, "rb") as f:
        table = tomllib.load(f).get("adguard") or {}
    if not table.get("url") or not table.get("user") or not table.get("password"):
        return None
    return {"url": str(table["url"]).rstrip("/"), "user": str(table["user"]), "password": str(table["password"])}


def clean_domain(text):
    domain = str(text or "").strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0].split(":")[0].strip(".")
    if not DOMAIN.match(domain):
        raise AdGuardError(f"{text!r} isn't a website name like example.com.")
    return domain


class AdGuard:
    def __init__(self, cfg, login=None):
        self.cfg = cfg
        self.login = login

    def credentials(self):
        found = self.login or load_login(self.cfg)
        if found is None:
            raise AdGuardError("AdGuard isn't set up for the hub: add url, user and password under [adguard] in secrets.toml.")
        return found

    def call(self, method, path, body=None):
        login = self.credentials()
        auth = base64.b64encode(f"{login['user']}:{login['password']}".encode()).decode()
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(login["url"] + path, data=data, method=method,
                                         headers={"Authorization": f"Basic {auth}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AdGuardError("AdGuard didn't accept the hub's login.") from e
            raise AdGuardError(f"AdGuard refused that: {e.code} {e.read().decode('utf-8', 'replace')[:120]}") from e
        except OSError as e:
            raise AdGuardError(f"AdGuard isn't answering: {e}") from e
        try:
            return json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {"text": raw.strip()}

    def overview(self, names=None):
        names = names or {}
        status = self.call("GET", "/control/status")
        stats = self.call("GET", "/control/stats")
        log = self.call("GET", "/control/querylog?response_status=blocked&limit=60").get("data", [])
        rules = self.call("GET", "/control/filtering/status")
        recent, seen = [], set()
        for entry in log:
            domain = entry.get("question", {}).get("name", "")
            if domain and domain not in seen:
                seen.add(domain)
                recent.append({"domain": domain, "client": names.get(entry.get("client"), entry.get("client")), "time": entry.get("time")})
            if len(recent) == 12:
                break
        allowed = [r[4:-1] for r in rules.get("user_rules", []) if r.startswith("@@||") and r.endswith("^")]
        blocked = [r[2:-1] for r in rules.get("user_rules", []) if r.startswith("||") and r.endswith("^")]
        queries, hits = stats.get("num_dns_queries", 0), stats.get("num_blocked_filtering", 0)
        return {"protection": bool(status.get("protection_enabled")), "paused_ms": status.get("protection_disabled_duration") or 0,
                "version": status.get("version", ""), "queries": queries, "blocked": hits, "percent": round(100 * hits / queries, 1) if queries else 0,
                "top_blocked": [{"domain": list(d)[0], "count": list(d.values())[0]} for d in stats.get("top_blocked_domains", [])[:8]],
                "top_clients": [{"client": names.get(list(c)[0], list(c)[0]), "count": list(c.values())[0]} for c in stats.get("top_clients", [])[:6]],
                "recent_blocked": recent, "allowed": allowed, "custom_blocked": blocked,
                "lists": [{"name": f["name"], "enabled": f["enabled"], "rules": f.get("rules_count", 0)} for f in rules.get("filters", [])]}

    def pause(self, minutes):
        minutes = int(minutes)
        if not 0 <= minutes <= MAX_PAUSE_MINUTES:
            raise AdGuardError(f"Pause for 1 to {MAX_PAUSE_MINUTES} minutes, or 0 to turn blocking back on.")
        if minutes == 0:
            self.call("POST", "/control/protection", {"enabled": True})
            return "Ad blocking is back on."
        self.call("POST", "/control/protection", {"enabled": False, "duration": minutes * 60_000})
        return f"Ad blocking is paused for {minutes} minute{'s' if minutes != 1 else ''}; it turns back on by itself."

    def set_rule(self, domain, allow):
        domain = clean_domain(domain)
        rules = self.call("GET", "/control/filtering/status").get("user_rules", [])
        allow_rule, block_rule = f"@@||{domain}^", f"||{domain}^"
        wanted, opposite = (allow_rule, block_rule) if allow else (block_rule, allow_rule)
        if wanted in rules:
            return f"{domain} is already {'allowed' if allow else 'blocked'}."
        updated = [r for r in rules if r != opposite] + [wanted]
        self.call("POST", "/control/filtering/set_rules", {"rules": updated})
        return f"{domain} is now {'always allowed' if allow else 'blocked'} for the whole house."

    def remove_rule(self, domain):
        domain = clean_domain(domain)
        rules = self.call("GET", "/control/filtering/status").get("user_rules", [])
        updated = [r for r in rules if r not in (f"@@||{domain}^", f"||{domain}^")]
        if updated == rules:
            return f"There's no rule for {domain}."
        self.call("POST", "/control/filtering/set_rules", {"rules": updated})
        return f"Removed the rule for {domain}; the blocklists decide again."


def build_module(cfg, client=None):
    guard = client or AdGuard(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (AdGuardError, ValueError, TypeError) as e:
                return text_result(str(e), error=True)
        return call

    def status(args):
        view = guard.overview()
        state = "on" if view["protection"] else f"paused for {round(view['paused_ms'] / 60000)} more minutes" if view["paused_ms"] else "off"
        top = ", ".join(d["domain"] for d in view["top_blocked"][:5])
        return (f"Ad blocking is {state}. Today so far: {view['queries']} lookups, {view['blocked']} blocked ({view['percent']}%). "
                f"Most blocked: {top or 'nothing yet'}.")

    adguard_status = tool("adguard_status", "AdGuard ad blocking: whether it's on, today's lookups and blocks, and the most blocked sites.",
                          {"type": "object", "properties": {}})(run(status))
    adguard_pause = tool("adguard_pause", f"Pause ad blocking for the whole house for 1 to {MAX_PAUSE_MINUTES} minutes; minutes 0 turns it back on.",
                         {"type": "object", "properties": {"minutes": {"type": "integer"}}, "required": ["minutes"]})(run(lambda a: guard.pause(a.get("minutes"))))
    adguard_unblock = tool("adguard_unblock", "Always allow a website (like example.com) that ad blocking broke, for the whole house.",
                           {"type": "object", "properties": {"site": {"type": "string"}}, "required": ["site"]})(run(lambda a: guard.set_rule(a.get("site"), True)))
    adguard_block = tool("adguard_block", "Block a website (like example.com) for the whole house.",
                         {"type": "object", "properties": {"site": {"type": "string"}}, "required": ["site"]})(run(lambda a: guard.set_rule(a.get("site"), False)))
    return ToolModule(server="adguard", prompt=PROMPT, available=lambda c: load_login(c) is not None, tools=[
        ToolSpec(fn=adguard_status, policy="auto", scope=Scope.household()),
        ToolSpec(fn=adguard_pause, policy="auto", scope=Scope.household()),
        ToolSpec(fn=adguard_unblock, policy="auto", scope=Scope.household()),
        ToolSpec(fn=adguard_block, policy="confirm", scope=Scope.household(), describe=lambda a: f"block {a.get('site')} for the whole house")])
