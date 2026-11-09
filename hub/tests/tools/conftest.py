import dataclasses

import pytest

from homehub import household
from homehub.brain.permissions import Permissions
from homehub.brain.prompt import build_system_prompt
from homehub.brain.session import Session, build_options
from homehub.household import sync_household
from homehub.tools import CallContext, Registry, builtins


class ClaudeHarness:
    def __init__(self, cfg, storage, modules, person_id="alex", google=False, **features):
        self.cfg = dataclasses.replace(cfg, features=dataclasses.replace(cfg.features, google=google, **features))
        self.storage = storage
        self.events = []
        self.answers = []
        self.registry = Registry(self.cfg, [*modules, builtins.WEB, builtins.FILES, builtins.SHELL, builtins.HIDDEN_BUILTINS], emit=self.emit)
        self.permissions = Permissions(self.registry, storage)
        person = storage.person(person_id)
        self.ctx = CallContext(conversation_id="c1", person_id=person["id"], person_name=person["name"], role=person["role"],
                               confidence="vpn_key", emit=self.emit)
        self.registry.context = self.ctx
        self.prompt = build_system_prompt(self.cfg, self.registry, storage, person)
        self.session = None
        if storage.conversation("c1") is None:
            storage.start_conversation("c1", "app", person["id"])

    def emit(self, name, **payload):
        self.events.append((name, payload))

    async def confirm(self, summary, ask_once):
        self.answers.append(summary)
        return False

    async def can_use_tool(self, tool_name, tool_input, sdk_context):
        return await self.permissions.can_use_tool(tool_name, tool_input, sdk_context, self.ctx, self.confirm)

    def options(self):
        return build_options(self.cfg, self.registry, self.prompt, self.can_use_tool)

    async def __aenter__(self):
        self.session = await Session(self.options, emit=self.emit).connect()
        return self

    async def __aexit__(self, *exc):
        await self.session.close()

    async def ask(self, text):
        self.events.clear()
        return await self.session.ask(text, None, self.ctx)

    def calls(self, name=None):
        return [p["input"] for n, p in self.events if n == "tool.called" and (name is None or p["tool"].endswith("__" + name))]

    def tools_called(self):
        return [p["tool"].rsplit("__", 1)[-1] for n, p in self.events if n == "tool.called"]


@pytest.fixture
def harness(example_config, temp_storage):
    sync_household(example_config, temp_storage, household.EXAMPLE_PATH)

    def make(modules, person_id="alex", google=False, **features):
        return ClaudeHarness(example_config, temp_storage, modules, person_id, google, **features)

    return make
