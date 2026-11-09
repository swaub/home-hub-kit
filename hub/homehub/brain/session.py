import asyncio
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import time
import uuid
from datetime import datetime
from pathlib import Path

from homehub.tools import current_context

log = logging.getLogger("homehub.brain")
SECRET_ENV_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
WARM_UP_PROMPT = "Startup check. Reply with only the word ready."
MCP_WAIT_SECONDS = 15.0
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
CLAUSE_END = re.compile(r"(?<=[,;:])\s+")
FIRST_CLAUSE_WORDS = 4


def split_ready(pending, first):
    *done, rest = SENTENCE_END.split(pending)
    if first and not done:
        for m in CLAUSE_END.finditer(rest):
            if len(rest[:m.start()].split()) >= FIRST_CLAUSE_WORDS:
                return [rest[:m.start()]], rest[m.end():]
    return done, rest
SPOKEN_ERRORS = {
    "offline": "I can't reach the internet right now, so I can't think about that.",
    "unreachable": "I couldn't reach Claude just now. Try again in a moment.",
    "usage_limit": "I've hit this week's Claude usage limit{resets}, so I can't answer until it resets.",
    "billing": "My Claude subscription isn't active right now, so I can't answer that. Check billing on claude.ai.",
    "auth": "My Claude login has expired. Please run claude auth login on the hub.",
    "other": "Sorry, something went wrong talking to Claude.",
}
ERROR_PATTERNS = (
    ("usage_limit", re.compile(r"usage limit|rate limit|rate_limit|429|exceeded your|quota|overloaded", re.IGNORECASE)),
    ("billing", re.compile(r"payment|past due|billing|renew|subscription (has )?(ended|expired|lapsed)", re.IGNORECASE)),
    ("auth", re.compile(r"not logged in|unauthori[sz]ed|401|403|invalid api key|authentication|login|expired token|oauth", re.IGNORECASE)),
    ("offline", re.compile(r"getaddrinfo|name resolution|ENOTFOUND|network is unreachable|no internet|dns|EAI_AGAIN|temporary failure", re.IGNORECASE)),
    ("unreachable", re.compile(r"ECONNREFUSED|ECONNRESET|connection refused|connection reset|timed? ?out|timeout|unavailable|502|503|504|fetch failed|socket", re.IGNORECASE)),
)


def scrub_environment():
    return [name for name in SECRET_ENV_VARS if os.environ.pop(name, None) is not None]


def claude_cli_path():
    exe = "claude.exe" if platform.system() == "Windows" else "claude"
    try:
        import claude_agent_sdk

        bundled = Path(claude_agent_sdk.__file__).parent / "_bundled" / exe
        if bundled.exists():
            return str(bundled)
    except ImportError:
        pass
    return shutil.which("claude")


def auth_status(timeout=30):
    cli = claude_cli_path()
    if not cli:
        return {"loggedIn": False, "error": "Claude Code CLI not found"}
    try:
        out = subprocess.run([cli, "auth", "status"], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"loggedIn": False, "error": str(e)}
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return {"loggedIn": False, "error": (out.stdout + out.stderr).strip() or f"claude auth status exited {out.returncode}"}


def describe_auth(status):
    if status.get("loggedIn") and status.get("authMethod") == "claude.ai":
        return "ok", "signed in with the Claude subscription (claude.ai)"
    if status.get("loggedIn"):
        return "warn", f"signed in with {status.get('authMethod') or 'an unknown method'}, not the subscription; run claude auth login and pick the Claude account"
    return "fail", f"not signed in ({status.get('error') or 'run claude auth login'})"


def classify_error(message):
    text = str(message)
    for kind, pattern in ERROR_PATTERNS:
        if pattern.search(text):
            return kind
    return "other"


class SessionError(RuntimeError):
    def __init__(self, message, kind=None, resets_at=None):
        super().__init__(message)
        self.kind = kind or classify_error(message)
        self.resets_at = resets_at

    @property
    def spoken(self):
        resets = f"; it resets {self.resets_at}" if self.resets_at else ""
        return SPOKEN_ERRORS[self.kind].format(resets=resets)


def time_prefix(now=None):
    now = now or datetime.now().astimezone()
    return now.strftime("[%A, %B %d, %Y, %I:%M %p %Z]")


def user_mcp_servers(path=None):
    path = Path(path or Path.home() / ".claude.json")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    names = set(data.get("mcpServers") or {})
    for project in (data.get("projects") or {}).values():
        names.update((project or {}).get("mcpServers") or {})
    return sorted(names)


def hidden_user_servers(registry, path=None):
    return [f"mcp__{name}" for name in user_mcp_servers(path) if name not in registry.servers]


def build_options(cfg, registry, system_prompt, can_use_tool=None, stderr=None):
    from claude_agent_sdk import ClaudeAgentOptions

    google = cfg.features.google
    disallowed = registry.disallowed_tools() + (hidden_user_servers(registry) if google else [])
    options = ClaudeAgentOptions(
        model=cfg.brain.model,
        system_prompt=system_prompt,
        tools=registry.builtin_tools(),
        allowed_tools=registry.allowed_tools(),
        mcp_servers=registry.mcp_servers(),
        disallowed_tools=disallowed,
        strict_mcp_config=not google,
        setting_sources=["user"] if google else [],
        permission_mode="default",
        can_use_tool=can_use_tool,
        max_turns=cfg.brain.max_turns,
        include_partial_messages=True,
        cwd=str(Path.home()),
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "true" if google else "false"},
        stderr=stderr,
    )
    if cfg.brain.effort:
        options.effort = cfg.brain.effort
    return options


class Session:
    def __init__(self, options_factory, emit=None, label=None):
        self.options_factory = options_factory
        self.emit = emit
        self.label = label or uuid.uuid4().hex[:6]
        self.client = None
        self.info = {}
        self.warm = False
        self.turns = 0
        self.cost_usd = 0.0

    def publish(self, name, **payload):
        if self.emit is not None:
            self.emit(name, **payload)

    async def connect(self):
        from claude_agent_sdk import ClaudeSDKClient

        scrub_environment()
        self.client = ClaudeSDKClient(self.options_factory())
        try:
            await self.client.connect()
        except Exception as e:
            raise SessionError(f"connect failed: {e}") from e
        return self

    async def servers_ready(self, timeout=MCP_WAIT_SECONDS):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                status = await self.client.get_mcp_status()
            except Exception as e:
                log.warning("mcp status failed: %s", e)
                return False
            servers = status.get("mcpServers") or []
            if servers and not any(s.get("status") == "pending" for s in servers):
                return True
            await asyncio.sleep(0.25)
        return False

    async def warm_up(self):
        await self.ask(WARM_UP_PROMPT)
        await self.servers_ready()
        reply = await self.ask(WARM_UP_PROMPT)
        self.warm = True
        self.publish("brain.session", event="warm", session_id=self.label)
        return reply

    async def ask(self, text, on_sentence=None, ctx=None):
        if self.client is None:
            raise SessionError("session is not connected", kind="unreachable")
        from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, SystemMessage, TextBlock, ToolUseBlock

        token = current_context.set(ctx)
        started = time.perf_counter()
        first_sentence_at = None
        texts, result, pending, streamed = [], None, "", False
        try:
            await self.client.query(f"{time_prefix()} {text}")
            async for msg in self.client.receive_response():
                if isinstance(msg, StreamEvent):
                    event = msg.event
                    if event.get("type") == "content_block_start" and (event.get("content_block") or {}).get("type") == "tool_use":
                        if getattr(ctx, "on_tool", None) is not None:
                            ctx.on_tool(event["content_block"].get("name", ""))
                    if on_sentence and event.get("type") == "content_block_delta" and event.get("delta", {}).get("type") == "text_delta":
                        pending += event["delta"]["text"]
                        done, pending = split_ready(pending, first_sentence_at is None)
                        for sentence in done:
                            if sentence.strip():
                                if first_sentence_at is None:
                                    first_sentence_at = time.perf_counter()
                                on_sentence(sentence.strip())
                                streamed = True
                    elif on_sentence and event.get("type") == "content_block_stop" and pending.strip():
                        if first_sentence_at is None:
                            first_sentence_at = time.perf_counter()
                        on_sentence(pending.strip())
                        pending, streamed = "", True
                elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                    self.info.update(model=msg.data.get("model"), api_key_source=msg.data.get("apiKeySource"), tools=msg.data.get("tools"))
                elif isinstance(msg, AssistantMessage):
                    if msg.error:
                        raise SessionError(" ".join(b.text for b in msg.content if isinstance(b, TextBlock)) or str(msg.error))
                    texts = [b.text for b in msg.content if isinstance(b, TextBlock)] or texts
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock) and (not block.name.startswith("mcp__") or block.name.startswith("mcp__claude_ai_")):
                            detail = block.input.get("query") or block.input.get("q") or block.input.get("url") or block.input.get("pattern") \
                                or block.input.get("file_path") or block.input.get("description") or ""
                            self.publish("tool.called", room=getattr(ctx, "room", ""), conversation_id=getattr(ctx, "conversation_id", ""),
                                         tool=block.name, input={"detail": str(detail)[:200]}, person_id=getattr(ctx, "person_id", None))
                elif isinstance(msg, ResultMessage):
                    result = msg
        except SessionError:
            raise
        except Exception as e:
            raise SessionError(str(e)) from e
        finally:
            current_context.reset(token)
        if result is None:
            raise SessionError("No result from Claude", kind="unreachable")
        if result.is_error:
            raise SessionError(result.result or result.subtype or "Claude returned an error")
        if on_sentence and pending.strip():
            on_sentence(pending.strip())
            streamed = True
        reply = (result.result or " ".join(texts)).strip()
        if on_sentence and not streamed and reply:
            first_sentence_at = first_sentence_at or time.perf_counter()
            on_sentence(reply)
        self.turns += 1
        usd = result.total_cost_usd or 0.0
        self.cost_usd += usd
        self.info["last"] = {
            "usd": usd, "steps": result.num_turns, "model": self.info.get("model"),
            "first_sentence_ms": round((first_sentence_at - started) * 1000) if first_sentence_at else None,
            "total_ms": round((time.perf_counter() - started) * 1000), "duration_api_ms": result.duration_api_ms,
            "models_used": list(result.model_usage) if result.model_usage else [],
            "usage": {k: result.usage[k] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens") if k in result.usage} if result.usage else {},
        }
        return reply

    async def interrupt(self):
        if self.client is not None:
            try:
                await self.client.interrupt()
            except Exception as e:
                log.warning("interrupt failed: %s", e)

    async def close(self):
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception as e:
                log.warning("disconnect failed: %s", e)
        self.publish("brain.session", event="closed", session_id=self.label)


class SessionPool:
    def __init__(self, options_factory, emit=None, keep_spare=True, warm=True):
        self.options_factory = options_factory
        self.emit = emit
        self.keep_spare = keep_spare
        self.warm = warm
        self.active = None
        self.spare_task = None
        self.closing = set()
        self.failures = 0
        self.down = None
        self.lock = asyncio.Lock()

    def publish(self, name, **payload):
        if self.emit is not None:
            self.emit(name, **payload)

    async def _fresh(self):
        session = Session(self.options_factory, self.emit)
        await session.connect()
        if self.warm:
            await session.warm_up()
        return session

    def _start_spare(self):
        if self.keep_spare and (self.spare_task is None or self.spare_task.done()):
            self.spare_task = asyncio.create_task(self._fresh())

    async def start(self):
        self.active = await self._fresh()
        self.down = None
        self._start_spare()
        return self.active

    async def start_if_able(self):
        try:
            return await self.start()
        except Exception as e:
            error = e if isinstance(e, SessionError) else SessionError(str(e))
            self.down = error
            self.publish("brain.error", kind=error.kind, message=str(error), resets_at=error.resets_at)
            return None

    async def take_spare(self):
        if self.spare_task is None:
            return await self._fresh()
        task, self.spare_task = self.spare_task, None
        try:
            return await task
        except Exception as e:
            log.warning("spare session failed: %s", e)
            return await self._fresh()

    async def rotate(self):
        async with self.lock:
            old, self.active = self.active, None
            if old is not None:
                task = asyncio.create_task(old.close())
                self.closing.add(task)
                task.add_done_callback(self.closing.discard)
            self.active = await self.take_spare()
            self.publish("brain.session", event="promoted", session_id=self.active.label)
            self._start_spare()
            return self.active

    async def ask(self, text, on_sentence=None, ctx=None):
        if self.active is None:
            await self.start()
        try:
            reply = await self.active.ask(text, on_sentence, ctx)
            self.failures = 0
            return reply
        except SessionError as e:
            if e.kind not in ("unreachable", "other"):
                raise
            self.failures += 1
            delay = min(2 ** (self.failures - 1), 30)
            self.publish("brain.error", kind=e.kind, message=str(e), retry_in=delay)
            await asyncio.sleep(delay)
            await self.rotate()
            self.publish("brain.session", event="reconnected", session_id=self.active.label)
            return await self.active.ask(text, on_sentence, ctx)

    async def interrupt(self):
        if self.active is not None:
            await self.active.interrupt()

    def status(self):
        spare = "none"
        if self.spare_task is not None:
            spare = "warming" if not self.spare_task.done() else ("ready" if not self.spare_task.exception() else "failed")
        return {"active": self.active.label if self.active else None, "spare": spare, "failures": self.failures}

    async def close(self):
        if self.spare_task is not None:
            self.spare_task.cancel()
            try:
                spare = await self.spare_task
                await spare.close()
            except (asyncio.CancelledError, Exception):
                pass
            self.spare_task = None
        if self.active is not None:
            await self.active.close()
            self.active = None
        if self.closing:
            await asyncio.gather(*self.closing, return_exceptions=True)
