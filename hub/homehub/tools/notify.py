import json
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = "notify_owner sends a short push message to the household owner's phone, for a summary of a fix on someone's computer or anything they should know later."


class Ntfy:
    def __init__(self, url, topic, timeout=10):
        self.url, self.topic, self.timeout = (url or "").rstrip("/"), (topic or "").strip(), timeout

    @property
    def enabled(self):
        return bool(self.url and self.topic)

    def send(self, message, title=None, priority=3, tags=(), click=None, actions=(), markdown=False):
        if not self.enabled:
            raise RuntimeError("push notifications aren't set up (notify.url and notify.topic in the config)")
        body = {"topic": self.topic, "message": message, "priority": int(priority)}
        if title:
            body["title"] = title
        if tags:
            body["tags"] = list(tags)
        if click:
            body["click"] = click
        if actions:
            body["actions"] = [{"action": "view", "label": label[:40], "url": url, "clear": True} for label, url in list(actions)[:3]]
        if markdown:
            body["markdown"] = True
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.loads(response.read().decode("utf-8") or "{}")

    def recent(self, since="all"):
        with urllib.request.urlopen(f"{self.url}/{self.topic}/json?poll=1&since={since}", timeout=self.timeout) as response:
            return [json.loads(line) for line in response.read().decode("utf-8").splitlines() if line.strip()]


def build_module(cfg):
    ntfy = Ntfy(cfg.notify.url, cfg.notify.topic)

    @tool("notify_owner", "Send a short push notification to the household owner's phone (for example a summary after fixing someone's computer).",
          {"type": "object", "properties": {"message": {"type": "string"}, "title": {"type": "string"}}, "required": ["message"]})
    async def notify_owner(args):
        try:
            ntfy.send(args["message"][:1000], title=args.get("title"))
        except Exception as e:
            return text_result(f"Couldn't send the notification: {e}", error=True)
        return text_result("Sent.")

    return ToolModule(server="notify", tools=[ToolSpec(fn=notify_owner, policy="auto", scope=Scope.household())], available=lambda c: bool(c.notify.url and c.notify.topic), prompt=PROMPT)
