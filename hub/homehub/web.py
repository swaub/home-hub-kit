import collections
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Home Hub</title>
<style>
:root { --bg: #f6f5f2; --fg: #1d1d1b; --muted: #6b6a66; --card: #ffffff; --line: #e4e2dc; --you: #1f5fbf; --claude: #8a4b14; --info: #6b6a66; }
@media (prefers-color-scheme: dark) { :root { --bg: #161615; --fg: #ecebe6; --muted: #9b9a94; --card: #20201e; --line: #33322f;
  --you: #7fb0ff; --claude: #f0b67a; --info: #9b9a94; } }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.45 system-ui, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 16px; }
header { display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; margin-bottom: 12px; }
h1 { font-size: 20px; margin: 0; }
.pill { padding: 2px 10px; border-radius: 999px; background: var(--card); border: 1px solid var(--line); font-size: 14px; }
.meta { color: var(--muted); font-size: 13px; }
ol { list-style: none; margin: 0; padding: 0; }
li { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; margin: 6px 0; overflow-wrap: anywhere; }
li b { font-weight: 600; margin-right: 6px; }
.you b { color: var(--you); } .claude b { color: var(--claude); } .info, .action, .heard, .error { color: var(--info); font-size: 14px; }
#lists { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); margin-bottom: 12px; }
.list { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 8px 12px; }
.list h2 { font-size: 15px; margin: 0 0 4px; text-transform: capitalize; } .list ul { margin: 0; padding-left: 18px; }
time { float: right; color: var(--muted); font-size: 12px; margin-left: 8px; }
</style></head>
<body><main>
<header><h1>Home Hub</h1><span class="pill" id="state">connecting</span><span class="meta" id="meta"></span></header>
<section id="lists"></section>
<ol id="lines"></ol>
</main>
<script>
const names = { you: "You", claude: "Claude", info: "Info", action: "Action", heard: "Heard", error: "Error" };
async function refresh() {
  try {
    const r = await fetch("status.json", { cache: "no-store" });
    const s = await r.json();
    document.getElementById("state").textContent = Object.entries(s.state).map(([room, st]) => room + ": " + st).join(", ") || "starting";
    document.getElementById("meta").textContent = s.version + " · up since " + s.started;
    const lists = document.getElementById("lists");
    lists.replaceChildren(...Object.entries(s.lists || {}).map(([name, items]) => {
      const box = document.createElement("div"); box.className = "list";
      const h = document.createElement("h2"); h.textContent = name + " list"; box.append(h);
      const ul = document.createElement("ul");
      items.forEach(i => { const li = document.createElement("li"); li.textContent = i; ul.append(li); });
      box.append(ul); return box;
    }));
    const list = document.getElementById("lines");
    list.replaceChildren(...s.lines.slice().reverse().map(l => {
      const li = document.createElement("li"); li.className = l.kind;
      const t = document.createElement("time"); t.textContent = l.at; li.append(t);
      const b = document.createElement("b"); b.textContent = names[l.kind] || l.kind; li.append(b, document.createTextNode(l.text));
      return li;
    }));
  } catch (e) { document.getElementById("state").textContent = "not answering"; }
}
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


class StatusPage:
    def __init__(self, host, port, status, version, keep=200, lists=None):
        self.host, self.port = host, port
        self.status = status
        self.lists = lists or (lambda: {})
        self.version = version
        self.started = datetime.now().isoformat(timespec="seconds")
        self.lines = collections.deque(maxlen=keep)
        self.lock = threading.Lock()
        self.server = None
        self.thread = None

    def on_event(self, event):
        if event.get("name") != "transcript.line":
            return
        with self.lock:
            self.lines.append({"at": datetime.now().strftime("%H:%M:%S"), "room": event.get("room"), "kind": event.get("kind", ""),
                               "text": event.get("text", "")})

    def snapshot(self):
        with self.lock:
            lines = list(self.lines)
        return {"state": self.status(), "version": self.version, "started": self.started, "lines": lines, "lists": self.lists()}

    def start(self):
        page = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self.reply(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
                elif self.path == "/status.json":
                    self.reply(200, "application/json", json.dumps(page.snapshot()).encode("utf-8"))
                else:
                    self.reply(404, "text/plain", b"not found")

            def reply(self, code, kind, body):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, name="status-page", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
