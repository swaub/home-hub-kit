import asyncio

from claude_agent_sdk import tool

from homehub.packages import PackageError, PackageTracker
from homehub.tools import Scope, ToolModule, ToolSpec, current_context, text_result

PROMPT = ("packages lists the person's packages the hub keeps from their shipping emails (Amazon, store shipments with UPS, USPS, "
          "FedEx or DHL, AliExpress): what each is, who's delivering it, its status and the expected day. Use it first for 'where's my "
          "package', 'anything arriving today' or 'did my order ship'; give tracking numbers only when asked, and mention delivered ones "
          "only when asked or delivered today. track_package adds a tracking number they read out so it shows on their list; check_packages "
          "looks through their email right now for new shipping news; remove_package takes one off the list.")


def build_module(cfg, storage):
    def tracker():
        ctx = current_context.get()
        return PackageTracker(cfg, storage, getattr(ctx, "person_id", None))

    @tool("packages", "The person's packages on the way and recently delivered, from their shipping emails: item, store, carrier, "
          "status, expected day and tracking number.", {"type": "object", "properties": {}})
    async def packages(args):
        return text_result(await asyncio.to_thread(lambda: tracker().describe()))

    @tool("track_package", "Add a tracking number (UPS, USPS, FedEx, DHL, Amazon, AliExpress) to the person's package list, with an "
          "optional short description of what it is.", {"type": "object", "properties": {"number": {"type": "string"}, "what": {"type": "string"}},
                                                        "required": ["number"]})
    async def track_package(args):
        try:
            row = await asyncio.to_thread(lambda: tracker().add(args.get("number", ""), args.get("what") or ""))
        except PackageError as e:
            return text_result(str(e), error=True)
        return text_result(f"Added {row['tracking']}" + (f" ({row['carrier']})" if row["carrier"] else "") + " to their packages; it opens on "
                           "the carrier's site from the Packages page.")

    @tool("check_packages", "Look through the person's email right now for new shipping, delivery or order emails, then list their "
          "packages. The hub also does this on its own every few minutes.", {"type": "object", "properties": {}})
    async def check_packages(args):
        def check():
            keeper = tracker()
            found = keeper.scan()
            return (f"{found} new shipping email{'s' if found != 1 else ''}. " if found else "No new shipping emails. ") + keeper.describe()
        try:
            return text_result(await asyncio.to_thread(check))
        except (PackageError, OSError, ValueError, KeyError) as e:
            return text_result(str(e), error=True)

    @tool("remove_package", "Take a package off the person's list (it stays off even if more emails about it come). which is words from "
          "its item, store or tracking number.", {"type": "object", "properties": {"which": {"type": "string"}}, "required": ["which"]})
    async def remove_package(args):
        def remove():
            keeper = tracker()
            words = " ".join(str(args.get("which") or "").lower().split())
            rows = keeper.current()
            found = [r for r in rows if words and words in " ".join(str(r[k] or "") for k in ("item", "merchant", "tracking", "carrier")).lower()]
            if len(found) != 1:
                names = "; ".join(r["item"] or r["merchant"] or r["tracking"] for r in (found or rows))
                raise PackageError((f"More than one package matches: {names}" if found else f"No package matches that. On the list: {names or 'nothing'}") + ".")
            keeper.hide(found[0]["key"])
            return f"Took {found[0]['item'] or found[0]['tracking']} off the package list."
        try:
            return text_result(await asyncio.to_thread(remove))
        except PackageError as e:
            return text_result(str(e), error=True)

    return ToolModule(server="tracking", prompt=PROMPT, tools=[
        ToolSpec(fn=packages, policy="auto", scope=Scope.personal("email"), untrusted_output=True, untrusted_source="shipping emails"),
        ToolSpec(fn=track_package, policy="auto", scope=Scope.personal("email")),
        ToolSpec(fn=check_packages, policy="auto", scope=Scope.personal("email"), untrusted_output=True, untrusted_source="shipping emails"),
        ToolSpec(fn=remove_package, policy="auto", scope=Scope.personal("email"))])
