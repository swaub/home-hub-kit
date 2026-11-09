import asyncio

from claude_agent_sdk import tool

from homehub.minecraft import McError
from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

PROMPT = ("The hub runs the household's Minecraft servers (the same ones on the Minecraft page of the private app). minecraft_servers "
          "lists them with state, who's online, TPS and the address friends use; minecraft_server gives one server's settings, whitelist, "
          "operators, memory, mods and recent console lines. minecraft_control starts, stops or restarts one; minecraft_settings changes "
          "max players, message, difficulty, game mode, memory, port, whitelist on or off, account checks or its custom address (a "
          "running server restarts to apply it, kicking players for about a minute, so say so); minecraft_whitelist adds or removes a "
          "player; minecraft_command runs a console command or says something in the game chat; minecraft_public opens or closes it to "
          "friends outside the home through the router. minecraft_find_mods searches Modrinth for server-side mods that fit a server and "
          "minecraft_mods adds or removes them by slug; minecraft_files lists a folder or reads a text file in the server's folder and "
          "minecraft_write_file saves one; minecraft_create makes a new server. When there's only one server, 'the Minecraft server' "
          "means that one. Deleting a server is only done on the Minecraft page, where its name has to be typed. server-port in "
          "server.properties is always 25565 because that's the port inside the server's own container; the port players use is the "
          "server's port setting, so change it with minecraft_settings and never edit server-port.")
SETTINGS = {"max_players": {"type": "integer"}, "motd": {"type": "string"}, "difficulty": {"type": "string", "enum": ["peaceful", "easy", "normal", "hard"]},
            "mode": {"type": "string", "enum": ["survival", "creative", "adventure"]}, "memory_gb": {"type": "integer"},
            "port": {"type": "integer"}, "whitelist": {"type": "boolean"}, "online_mode": {"type": "boolean"}, "host": {"type": "string"}}
WORDS = {"max_players": "max players", "motd": "message", "mode": "game mode", "memory_gb": "memory (GB)", "online_mode": "account checks",
         "host": "custom address"}
SERVER = {"type": "string", "description": "the server's name; leave out when there's only one"}


def players_line(info):
    if info["state"] != "online":
        return ""
    names = ", ".join(info["player_names"])
    count = info["players_online"]
    who = f"{count}/{info['max_players']} online" + (f": {names}" if names else "") if count is not None else "players unknown"
    tps = f", TPS {info['tps']} ({info['mspt']} ms per tick)" if info["tps"] is not None else ""
    return f"; {who}{tps}"


def address_line(info):
    return f"friends join at {info['public_address']}" if info["public_address"] else f"VPN only at {info['vpn_address']}"


def describe_brief(info):
    return f"{info['name']} ({info['loader']} {info['version']}): {info['state']}{players_line(info)}; {address_line(info)}"


def describe_detail(info, log):
    lines = [describe_brief(info),
             f"Settings: up to {info['max_players']} players, {info['difficulty']}, {info['mode']}, message \"{info['motd']}\", "
             f"{info['memory_gb']} GB memory, {info['disk_gb']} GB disk limit, port {info['port']}, whitelist {'on' if info['whitelist_on'] else 'off'}, "
             f"account checks {'on' if info['online_mode'] else 'off'}" + (f", custom address {info['host']}" if info["host"] else ""),
             f"Whitelist: {', '.join(info['whitelist']) or 'nobody'}; operators: {', '.join(info['operators']) or 'nobody'}"]
    if info["memory_used"] is not None:
        lines.append(f"Using {info['memory_used'] / 1024 ** 3:.1f} GB memory and {info['cpu_percent'] / info['cpus']:.0f}% of its {info['cpus']} threads")
    if info["mods"]:
        lines.append(f"{len(info['mods'])} mods: " + ", ".join(m.split(":")[0] for m in info["mods"]))
    if info["note"]:
        lines.append(f"Note: {info['note']}")
    if log:
        lines.append("Recent console lines:\n" + log)
    return "\n".join(lines)


def build_module(minecraft):
    def find(name):
        servers = minecraft.list()
        if not servers:
            raise McError("There are no Minecraft servers on the hub yet; make one on the Minecraft page")
        if not name:
            if len(servers) == 1:
                return servers[0]
            raise McError("Which server? There are " + ", ".join(s["name"] for s in servers))
        key = str(name).strip().lower()
        for test in (lambda s: key in (s["slug"], s["name"].lower()), lambda s: key in s["name"].lower() or key in s["slug"]):
            found = [s for s in servers if test(s)]
            if len(found) == 1:
                return found[0]
        raise McError(f"No server called {name}; there are " + ", ".join(s["name"] for s in servers))

    def who():
        ctx = current_context.get()
        return (ctx.person_id if ctx else None) or "assistant"

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (McError, OSError, ValueError, KeyError) as e:
                return text_result(str(e), error=True)
        return call

    def servers(args):
        return "\n".join(describe_brief(minecraft.detail(s["slug"])) for s in minecraft.list()) or "There are no Minecraft servers on the hub yet."

    def server(args):
        slug = find(args.get("server"))["slug"]
        lines = max(0, min(int(args.get("log_lines") or 0), 60))
        log = "\n".join(minecraft.log(slug).splitlines()[-lines:]) if lines else ""
        return describe_detail(minecraft.detail(slug), log)

    def control(args):
        found = find(args.get("server"))
        action = args["action"]
        if action not in ("start", "stop", "restart"):
            raise McError("The action is start, stop or restart")
        minecraft.action(found["slug"], action, who())
        return {"start": f"Starting {found['name']}; it takes a minute or two to be joinable.",
                "stop": f"Stopped {found['name']}.", "restart": f"Restarted {found['name']}; it'll be back in about a minute."}[action]

    def settings(args):
        found = find(args.get("server"))
        spec = {k: args[k] for k in SETTINGS if k in args}
        if not spec:
            raise McError("Nothing to change")
        result = minecraft.update(found["slug"], spec, who())
        changes = ", ".join(f"{WORDS.get(k, k)} {'on' if v is True else 'off' if v is False else v}" for k, v in spec.items())
        restarted = " It's restarting to apply that." if result["state"] != "stopped" and set(spec) - {"host"} else ""
        return f"Changed {found['name']}: {changes}.{restarted}"

    def whitelist(args):
        found = find(args.get("server"))
        add = args.get("add", True)
        names = minecraft.change_whitelist(found["slug"], args["player"], add, who())["whitelist"]
        return f"{'Added' if add else 'Removed'} {args['player']} {'to' if add else 'from'} {found['name']}'s whitelist. Now on it: {', '.join(names) or 'nobody'}."

    def command(args):
        found = find(args.get("server"))
        if args.get("chat"):
            ctx = current_context.get()
            return minecraft.command(found["slug"], args["text"], chat_as=(ctx.person_name if ctx and ctx.person_name else "Hub"))
        return minecraft.command(found["slug"], args["text"]) or "Done, with no reply from the server."

    def public(args):
        found = find(args.get("server"))
        minecraft.set_public(found["slug"], bool(args["open"]), who())
        info = minecraft.detail(found["slug"])
        return f"{found['name']} is open to friends at {info['public_address']}." if info["public"] else f"{found['name']} is closed to friends; only the VPN reaches it now."

    def find_mods(args):
        if args.get("server"):
            meta = minecraft.meta(find(args["server"])["slug"])
            loader, version = meta["loader"], meta["version"]
        else:
            loader, version = args.get("loader") or "fabric", args.get("version") or minecraft.catalog.loader_versions(args.get("loader") or "fabric")[0]
        found = minecraft.catalog.mods(loader, version, args.get("query") or "")
        if not found["hits"]:
            return f"No server-side {loader} mods for {version} match that."
        return f"{loader} {version}, {found['total']} matches:\n" + "\n".join(
            f"{h['slug']}: {h['title']} ({h['downloads']:,} downloads; players {'need it too' if h['client'] == 'required' else 'need nothing'}) - {h['description']}"
            for h in found["hits"][:10])

    def mods(args):
        found = find(args.get("server"))
        current = minecraft.meta(found["slug"])["mods"]
        drop = {str(m).lower() for m in args.get("remove") or []}
        kept = [m for m in current if m.split(":")[0].lower() not in drop]
        added = [m for m in args.get("add") or [] if m.split(":")[0] not in {k.split(":")[0] for k in kept}]
        if len(kept) == len(current) and not added:
            raise McError("Nothing to change: those mods are already as asked")
        result = minecraft.update(found["slug"], {"mods": kept + added}, who())
        parts = ([f"added {', '.join(added)}"] if added else []) + ([f"removed {len(current) - len(kept)}"] if len(kept) < len(current) else [])
        return f"{found['name']}: {' and '.join(parts)}." + (" It's restarting to install them." if result["state"] != "stopped" else " They install on its next start.")

    def files(args):
        found = find(args.get("server"))
        listing = minecraft.files(found["slug"], args.get("path") or "")
        if listing["dir"]:
            rows = [f"{e['name']}/" if e["dir"] else f"{e['name']} ({e['size']:,} bytes)" for e in listing["entries"]]
            return f"{listing['path'] or 'the server folder'}:\n" + ("\n".join(rows[:200]) or "empty")
        if listing["text"] is None:
            return f"{listing['path']} is a {listing['size']:,} byte file that isn't text, so it can't be shown here."
        return f"{listing['path']}:\n{listing['text']}"

    def write_file(args):
        found = find(args.get("server"))
        minecraft.write_text(found["slug"], args["path"], args["text"])
        restart = " Restart the server for it to take effect." if minecraft.detail(found["slug"])["state"] != "stopped" else ""
        return f"Saved {args['path']} on {found['name']}.{restart}"

    def create(args):
        loader = args.get("loader") or "fabric"
        version = args.get("version") or minecraft.catalog.loader_versions(loader)[0]
        spec = {k: args[k] for k in ("name", "memory_gb", "max_players", "difficulty", "mode", "seed", "motd") if k in args}
        spec.update(loader=loader, version=version, eula=True, mods=args.get("mods") or [], players=args.get("players") or [])
        made = minecraft.create(spec, who())
        return (f"Creating {made['name']} ({loader} {version}, {made['memory_gb']} GB, up to {made['max_players']} players) on port {made['port']}. "
                "It downloads and starts on its own in a few minutes; it's VPN-only until it's opened to friends.")

    def name_of(tool_input):
        return tool_input.get("server") or "the Minecraft server"

    tools = [
        (tool("minecraft_servers", "List the hub's Minecraft servers: state, who's online, TPS and the join address.",
              {"type": "object", "properties": {}})(run(servers)), "auto", None),
        (tool("minecraft_server", "One Minecraft server in detail: settings, whitelist, operators, memory and CPU, mods, and optionally the "
              "last log_lines console lines (up to 60) to see chat, joins or errors.",
              {"type": "object", "properties": {"server": SERVER, "log_lines": {"type": "integer"}}})(run(server)), "auto", None),
        (tool("minecraft_control", "Start, stop or restart a Minecraft server.",
              {"type": "object", "properties": {"server": SERVER, "action": {"type": "string", "enum": ["start", "stop", "restart"]}},
               "required": ["action"]})(run(control)), "confirm", lambda a: f"{a.get('action', 'change')} {name_of(a)}"),
        (tool("minecraft_settings", "Change a Minecraft server's settings; give only the ones to change. memory_gb is up to the hub's limit, "
              "port 1024 to 65535 (a port another server or the router uses is refused), host is a custom address like name.duckdns.org "
              "or empty for the home address. A running server restarts to apply it.",
              {"type": "object", "properties": {"server": SERVER, **SETTINGS}})(run(settings)), "confirm",
         lambda a: f"change {name_of(a)}: " + ", ".join(f"{WORDS.get(k, k)} to {v}" for k, v in a.items() if k in SETTINGS)),
        (tool("minecraft_whitelist", "Add a player to a Minecraft server's whitelist (add true) or remove them (add false), by their "
              "Minecraft username.",
              {"type": "object", "properties": {"server": SERVER, "player": {"type": "string"}, "add": {"type": "boolean"}},
               "required": ["player"]})(run(whitelist)), "confirm",
         lambda a: f"{'add' if a.get('add', True) else 'remove'} {a.get('player', '')} {'to' if a.get('add', True) else 'from'} the whitelist on {name_of(a)}"),
        (tool("minecraft_command", "Run a console command on a Minecraft server (like 'time set day' or 'kick Steve'), or with chat true, "
              "say text in the game chat to everyone online.",
              {"type": "object", "properties": {"server": SERVER, "text": {"type": "string"}, "chat": {"type": "boolean"}},
               "required": ["text"]})(run(command)), "confirm",
         lambda a: f"{'say' if a.get('chat') else 'run'} \"{a.get('text', '')}\" on {name_of(a)}"),
        (tool("minecraft_public", "Open a Minecraft server to friends outside the home (open true: forwards its port on the router) or "
              "close it again (open false).",
              {"type": "object", "properties": {"server": SERVER, "open": {"type": "boolean"}}, "required": ["open"]})(run(public)), "confirm",
         lambda a: f"{'open' if a.get('open') else 'close'} {name_of(a)} {'to' if a.get('open') else 'for'} friends outside the home"),
        (tool("minecraft_find_mods", "Search Modrinth for mods or plugins that run on the server side, for a server (its loader and "
              "version) or for a loader and version. Results give the slug to use with minecraft_mods or minecraft_create and whether "
              "players would have to install it too.",
              {"type": "object", "properties": {"server": SERVER, "query": {"type": "string"},
                                                "loader": {"type": "string", "enum": ["fabric", "paper", "forge", "neoforge"]},
                                                "version": {"type": "string"}}})(run(find_mods)), "auto", None),
        (tool("minecraft_mods", "Add mods to a Minecraft server (Modrinth slugs from minecraft_find_mods) or remove some it has. A running "
              "server restarts to install them.",
              {"type": "object", "properties": {"server": SERVER, "add": {"type": "array", "items": {"type": "string"}},
                                                "remove": {"type": "array", "items": {"type": "string"}}}})(run(mods)), "confirm",
         lambda a: f"change mods on {name_of(a)}: " + "; ".join(([f"add {', '.join(a['add'])}"] if a.get("add") else []) +
                                                               ([f"remove {', '.join(a['remove'])}"] if a.get("remove") else []))),
        (tool("minecraft_files", "List a folder (path empty for the top) or read a text file such as server.properties or a mod's config "
              "in a Minecraft server's folder.",
              {"type": "object", "properties": {"server": SERVER, "path": {"type": "string"}}})(run(files)), "auto", None),
        (tool("minecraft_write_file", "Save a whole text file in a Minecraft server's folder (read it first with minecraft_files and send "
              "back the full edited text). Max players, message, difficulty, game mode, whitelist and account checks in "
              "server.properties carry into the server's settings.",
              {"type": "object", "properties": {"server": SERVER, "path": {"type": "string"}, "text": {"type": "string"}},
               "required": ["path", "text"]})(run(write_file)), "confirm", lambda a: f"save {a.get('path', 'a file')} on {name_of(a)}"),
        (tool("minecraft_create", "Make a new Minecraft server. loader is fabric (default, for mods), paper (plugins), forge, neoforge or "
              "vanilla; version defaults to the newest; mods are Modrinth slugs; players are usernames for the whitelist, the first one "
              "an operator. It starts VPN-only.",
              {"type": "object", "properties": {"name": {"type": "string"},
                                                "loader": {"type": "string", "enum": ["fabric", "paper", "forge", "neoforge", "vanilla"]},
                                                "version": {"type": "string"}, "memory_gb": {"type": "integer"}, "max_players": {"type": "integer"},
                                                "difficulty": SETTINGS["difficulty"], "mode": SETTINGS["mode"], "seed": {"type": "string"},
                                                "motd": {"type": "string"}, "mods": {"type": "array", "items": {"type": "string"}},
                                                "players": {"type": "array", "items": {"type": "string"}}},
               "required": ["name"]})(run(create)), "confirm",
         lambda a: f"create a Minecraft server called {a.get('name', '')} ({a.get('loader') or 'fabric'} {a.get('version') or 'newest'})"),
    ]
    return ToolModule(server="minecraft", prompt=PROMPT, tools=[
        ToolSpec(fn=fn, policy=policy, scope=Scope.household(), describe=describe,
                 untrusted_output=policy == "auto", untrusted_source="the Minecraft server" if policy == "auto" else "")
        for fn, policy, describe in tools])
