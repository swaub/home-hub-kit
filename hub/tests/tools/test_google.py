import asyncio

import pytest

from homehub.tools import Registry
from homehub.tools import google


def test_policy_lists_cover_every_gmail_and_calendar_tool(example_config):
    reg = Registry(example_config, [google.MODULE])
    allowed, confirm, hidden = set(reg.allowed_tools()), set(reg.confirm_tools()), set(reg.disallowed_tools())
    assert allowed == ({google.GMAIL + n for n in google.GMAIL_READ + google.GMAIL_DRAFT} | {google.GCAL + n for n in google.GCAL_READ}
                       | {google.GDRIVE + n for n in google.GDRIVE_READ} | {google.GDOCS + "read_doc"})
    assert {google.GDRIVE + n for n in google.GDRIVE_WRITE} | {google.GDOCS + "update_doc"} <= confirm
    assert {google.GDRIVE + n for n in google.GDRIVE_HIDDEN} <= hidden and google.GMAIL + "send_message" in hidden
    assert confirm == ({google.GCAL + n for n in google.GCAL_WRITE} | {google.GDRIVE + n for n in google.GDRIVE_WRITE}
                       | {google.GDOCS + "update_doc"})
    assert {google.GMAIL + n for n in google.GMAIL_WRITE} <= hidden and set(google.HIDDEN_CONNECTORS) <= hidden
    assert not (allowed & hidden) and not (confirm & hidden)
    reads = [reg.spec(google.GMAIL + n) for n in google.GMAIL_READ] + [reg.spec(google.GCAL + n) for n in google.GCAL_READ]
    assert all(spec.untrusted_output and spec.scope.kind == "personal" for spec in reads)
    assert {spec.scope.detail for spec in reads} == {"email", "calendar"}
    writes = [reg.spec(google.GCAL + n) for n in google.GCAL_WRITE]
    assert all(spec.outward and spec.policy == "confirm" for spec in writes)
    assert reg.spec(google.GCAL + "create_event").describe({"summary": "Dentist"}) == "add 'Dentist' to your Google Calendar"
    assert reg.spec(google.GCAL + "delete_event").describe({}) == "delete an event from your Google Calendar"
    assert google.PROMPT in reg.prompt_snippets()


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_live_gmail_and_calendar_are_read_only_and_calendar_writes_ask_first(harness):
    h = harness([google.MODULE], google=True)

    async def scenario():
        async with h:
            await h.ask("Reply with just: ok")
            tools = h.session.info.get("tools") or []
            connectors = sorted(t for t in tools if t.startswith("mcp__claude_ai_"))
            if not any(t.startswith(google.GMAIL) for t in connectors):
                return None, connectors, []
            out = []
            for q in ("What's on my calendar tomorrow? Keep it short.", "Do I have any important unread emails? Just the top two, briefly.",
                      "Add an event called Voice assistant test to my calendar tomorrow at 3 pm."):
                reply = await h.ask(q)
                out.append((q, reply, [p["tool"] for n, p in h.events if n == "tool.called"]))
            return tools, connectors, out

    tools, connectors, results = asyncio.run(scenario())
    if tools is None:
        pytest.skip(f"Gmail and Calendar connectors are not visible in this session (connectors seen: {connectors})")
    for q, reply, used in results:
        print(f"\n  {q} -> {[u.split('__')[-1] for u in used]}\n      reply: {reply[:300]}")
    visible = {t for t in connectors if t.startswith((google.GMAIL, google.GCAL))}
    allowed = ({google.GMAIL + n for n in google.GMAIL_READ + google.GMAIL_DRAFT} | {google.GCAL + n for n in google.GCAL_READ}
               | {google.GCAL + n for n in google.GCAL_WRITE})
    blocked = set(h.registry.disallowed_tools())
    unexpected = {t for t in visible if t not in allowed and t not in blocked and not any(t.startswith(p) for p in blocked)}
    print(f"\n  connectors listed: {len(visible)} Gmail/Calendar tools, {len(visible - allowed)} of them hidden by policy")
    assert unexpected == set(), sorted(unexpected)
    called = [p["tool"] for n, p in h.events if n == "tool.called"]
    assert not any(t.endswith(("send_message", "trash_message", "forward")) for t in called)
    calendar, mail, add = results
    assert len(calendar[1]) > 10 and not any(w in calendar[1].lower() for w in ("can't access", "cannot access", "don't have access", "no access"))
    assert len(mail[1]) > 10 and not any(w in mail[1].lower() for w in ("can't access", "cannot access", "don't have access", "no access"))
    assert len(h.answers) >= 1 and "calendar" in h.answers[0].lower()
    assert not any(p.get("ok") and p["tool"].endswith(("create_event", "update_event", "delete_event")) for n, p in h.events if n == "tool.result")
    decided = [(p["tool"], p["how"]) for n, p in h.events if n == "permission.decided"]
    assert any(tool.endswith("create_event") and how == "declined" for tool, how in decided), decided


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_live_packages_come_from_shipping_emails(harness):
    h = harness([google.MODULE], google=True)

    async def scenario():
        async with h:
            await h.ask("Reply with just: ok")
            tools = h.session.info.get("tools") or []
            if not any(t.startswith(google.GMAIL) for t in tools):
                return None, None
            reply = await h.ask("What packages do I have coming this week?")
            return reply, [p for n, p in h.events if n == "tool.called"]

    reply, calls = asyncio.run(scenario())
    if reply is None:
        pytest.skip("the Gmail connector is not visible in this session")
    searches = [c["input"]["detail"] for c in calls if c["tool"] == google.GMAIL + "search_threads"]
    print(f"\n  searches: {searches}\n  reply: {reply}")
    assert searches and any("newer_than" in s for s in searches)
    assert len(reply.split()) <= 80 and not any(w in reply.lower() for w in ("can't access", "cannot access", "no access"))


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_live_google_drive_is_searchable_read_only(harness):
    h = harness([google.MODULE], google=True)

    async def scenario():
        async with h:
            await h.ask("Reply with just: ok")
            tools = h.session.info.get("tools") or []
            if not any(t.startswith(google.GDRIVE) for t in tools):
                return None, None
            reply = await h.ask("What are the three most recent files in my Google Drive? Just their names.")
            return reply, [p["tool"] for n, p in h.events if n == "tool.called"]

    reply, called = asyncio.run(scenario())
    if reply is None:
        pytest.skip("the Google Drive connector is not visible in this session")
    print(f"\n  drive -> {called}\n  reply: {reply}")
    assert any(c.startswith(google.GDRIVE) for c in called)
    assert not any(c.endswith(("share_file", "trash_file", "create_file", "update_file")) for c in called)
