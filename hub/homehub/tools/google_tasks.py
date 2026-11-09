import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result
from homehub.tools.google_auth import GoogleAuth, load_app

API = "https://tasks.googleapis.com/tasks/v1"
SCOPE = "https://www.googleapis.com/auth/tasks"
TOKEN_FILE = "google-tasks-token.json"
VERIFIER_FILE = "google-tasks-login.json"
TIMEOUT = 10
PROMPT = ("Google Tasks: tasks_read lists open tasks (in a named list or the default one), tasks_add adds one (due is a date like "
          "2026-11-03), tasks_complete marks one done by its title, tasks_lists names the task lists. These are their Google Tasks, "
          "separate from the assistant's own shopping and to do lists.")
NOT_SET_UP = ("Google Tasks isn't set up: put client_id and client_secret under [google_tasks] in secrets.toml, then run "
              "'homehub google-tasks login' on the hub.")


class TasksError(RuntimeError):
    pass


def ready(cfg):
    return load_app(cfg) is not None and (cfg.data_dir() / TOKEN_FILE).is_file()


class GoogleTasks:
    def __init__(self, cfg):
        self.cfg = cfg
        self.auth = GoogleAuth(cfg, SCOPE, TOKEN_FILE, VERIFIER_FILE, TasksError, NOT_SET_UP,
                               "Start again with 'homehub google-tasks login' so the link and the code match.")

    def auth_url(self):
        return self.auth.auth_url()

    def login(self, pasted):
        return self.auth.login(pasted)

    def access_token(self):
        return self.auth.access_token()

    def api(self, method, path, body=None, params=None):
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        request = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                         headers={"Authorization": f"Bearer {self.access_token()}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raise TasksError(f"Google Tasks said {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e

    def lists(self):
        return self.api("GET", "/users/@me/lists").get("items", [])

    def find_list(self, name):
        lists = self.lists()
        if not lists:
            raise TasksError("There are no Google Tasks lists.")
        if not name:
            return lists[0]
        wanted = name.strip().lower()
        for item in lists:
            if wanted in item["title"].lower():
                return item
        raise TasksError(f"No Google Tasks list called {name}. Lists: {', '.join(l['title'] for l in lists)}.")

    def read(self, name=""):
        task_list = self.find_list(name)
        tasks = self.api("GET", f"/lists/{task_list['id']}/tasks", params={"showCompleted": "false", "maxResults": 50}).get("items", [])
        lines = [t["title"] + (f" (due {t['due'][:10]})" if t.get("due") else "") for t in tasks if t.get("title")]
        return f"{task_list['title']}: " + ("; ".join(lines) if lines else "nothing open") + "."

    def add(self, title, due="", notes="", name=""):
        task_list = self.find_list(name)
        body = {"title": title.strip()}
        if notes:
            body["notes"] = notes
        if due:
            body["due"] = date.fromisoformat(due[:10]).isoformat() + "T00:00:00.000Z"
        self.api("POST", f"/lists/{task_list['id']}/tasks", body=body)
        return f"Added '{body['title']}' to {task_list['title']}" + (f", due {due[:10]}" if due else "") + "."

    def complete(self, title, name=""):
        task_list = self.find_list(name)
        tasks = self.api("GET", f"/lists/{task_list['id']}/tasks", params={"showCompleted": "false", "maxResults": 100}).get("items", [])
        wanted = title.strip().lower()
        match = [t for t in tasks if t.get("title", "").lower() == wanted] or [t for t in tasks if wanted in t.get("title", "").lower()]
        if not match:
            raise TasksError(f"No open task like {title} in {task_list['title']}.")
        self.api("PATCH", f"/lists/{task_list['id']}/tasks/{match[0]['id']}", body={"status": "completed"})
        return f"Marked '{match[0]['title']}' done."


def build_module(cfg, client=None):
    tasks = client or GoogleTasks(cfg)

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (TasksError, OSError, KeyError, ValueError) as e:
                return text_result(str(e), error=True)
        return call

    tasks_lists = tool("tasks_lists", "Name the Google Tasks lists.", {"type": "object", "properties": {}})(
        run(lambda a: "; ".join(l["title"] for l in tasks.lists()) or "No lists."))
    tasks_read = tool("tasks_read", "Open tasks in a Google Tasks list (default list if none named).",
                      {"type": "object", "properties": {"list": {"type": "string"}}})(run(lambda a: tasks.read(a.get("list") or "")))
    tasks_add = tool("tasks_add", "Add a Google Task with an optional due date (YYYY-MM-DD), notes and list.",
                     {"type": "object", "properties": {"title": {"type": "string"}, "due": {"type": "string"}, "notes": {"type": "string"},
                                                       "list": {"type": "string"}}, "required": ["title"]})(
        run(lambda a: tasks.add(a["title"], a.get("due") or "", a.get("notes") or "", a.get("list") or "")))
    tasks_complete = tool("tasks_complete", "Mark a Google Task done by its title.",
                          {"type": "object", "properties": {"title": {"type": "string"}, "list": {"type": "string"}}, "required": ["title"]})(
        run(lambda a: tasks.complete(a["title"], a.get("list") or "")))
    specs = [ToolSpec(fn=fn, policy="auto", scope=Scope.personal("tasks")) for fn in (tasks_add, tasks_complete)]
    specs += [ToolSpec(fn=fn, policy="auto", scope=Scope.personal("tasks"), untrusted_output=True, untrusted_source="Google Tasks") for fn in (tasks_lists, tasks_read)]
    return ToolModule(server="google_tasks", prompt=PROMPT, available=ready, tools=specs)
