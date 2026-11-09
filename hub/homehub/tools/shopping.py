import asyncio
import re
import urllib.parse

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

PROMPT = ("Shopping: you can't log in to stores or buy anything; instead you send one-tap links to their phone. For any request to "
          "find, compare or buy a product, search the web with site:amazon.com (for example 'double sided mounting tape site:amazon.com') "
          "to get real amazon.com/dp/ product links and prices (reviews elsewhere are fine for choosing), then ALWAYS finish by calling "
          "shop_suggest with the best two or three (title, price, the amazon.com/dp/ link, a few-word reason) and say each in one "
          "short sentence. To add one ('add number two', 'add the cheapest', or a product they name) call shop_send with the option "
          "number, or a store link after finding it; then tell them the notification opens the product in their Amazon app where they tap "
          "Add to Cart; never say it's already in the cart. shop_list_to_phone sends their shopping list as tappable search links.")
ASIN = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/|/product/)([A-Z0-9]{10})(?:[/?]|$)")
STORES = {"amazon": ("amazon.com", "amzn."), "ebay": ("ebay.com",), "aliexpress": ("aliexpress.com", "aliexpress.us")}
SETTING = "shop_options"


class ShopError(RuntimeError):
    pass


def store_of(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for store, domains in STORES.items():
        if any(host == d or host.endswith("." + d) or host.startswith(d) for d in domains):
            return store
    raise ShopError("That link isn't from Amazon, eBay or AliExpress.")


def asin_of(url):
    m = ASIN.search(urllib.parse.urlparse(url).path + "/")
    return m.group(1) if m else None


def cart_link(url):
    store = store_of(url)
    if store == "amazon":
        asin = asin_of(url)
        return "Open in Amazon", f"https://www.amazon.com/dp/{asin}" if asin else url
    return ("Open on eBay" if store == "ebay" else "Open on AliExpress"), url


def search_links(item):
    q = urllib.parse.quote_plus(item)
    return {"Amazon": f"https://www.amazon.com/s?k={q}", "eBay": f"https://www.ebay.com/sch/i.html?_nkw={q}",
            "AliExpress": f"https://www.aliexpress.us/w/wholesale-{urllib.parse.quote(item.replace(' ', '-'))}.html"}


def build_module(storage, ntfy):
    def push(message, title, actions=(), markdown=False):
        if not ntfy.enabled:
            raise ShopError("Phone notifications aren't set up, so I can't send the link.")
        ntfy.send(message, title=title, tags=("shopping_cart",), actions=actions, click=actions[0][1] if actions else None, markdown=markdown)

    def suggest(args):
        options = []
        for raw in (args.get("options") or [])[:5]:
            title, url = str(raw.get("title", "")).strip(), str(raw.get("url", "")).strip()
            if not title or not url:
                continue
            store_of(url)
            options.append({"title": title[:120], "url": url, "price": str(raw.get("price", "")).strip(), "why": str(raw.get("why", "")).strip()[:120]})
        if not options:
            raise ShopError("Give at least one option with a title and a store link.")
        storage.set_setting(SETTING, {"query": args.get("query", ""), "options": options})
        lines = [f"{i}. **{o['title']}**" + (f" — {o['price']}" if o["price"] else "") + (f"  \n{o['why']}" if o["why"] else "")
                 for i, o in enumerate(options, 1)]
        buttons = [(f"Open {i}", cart_link(o["url"])[1]) for i, o in enumerate(options[:3], 1)]
        push("\n\n".join(lines), title=f"Options: {args.get('query', 'shopping')}"[:80], actions=buttons, markdown=True)
        return f"Sent {len(options)} options to their phone; they can tap one there or say which number to add."

    def send(args):
        url, title, price = str(args.get("url") or "").strip(), str(args.get("title") or "").strip(), str(args.get("price") or "").strip()
        if args.get("option"):
            saved = (storage.get_setting(SETTING) or {}).get("options", [])
            index = int(args["option"]) - 1
            if not 0 <= index < len(saved):
                raise ShopError(f"There's no option {args['option']}; the last search had {len(saved)}.")
            url, title, price = saved[index]["url"], saved[index]["title"], saved[index]["price"]
        if not url:
            raise ShopError("Give an option number from the last search or a store link.")
        label, link = cart_link(url)
        push(f"{title or 'Your item'}" + (f" — {price}" if price else "") + "\nTap to open it, then tap Add to Cart.", title=label, actions=[(label, link)])
        return f"Sent '{label}' for {title or 'that item'} to their phone; it opens the product page, where they tap Add to Cart."

    def list_to_phone(args):
        name = args.get("list") or "shopping"
        items = [i["text"] for i in storage.list_items(name)]
        if not items:
            return f"The {storage.list_key(name)} list is empty."
        for item in items[:10]:
            links = search_links(item)
            push(item, title=f"{storage.list_key(name).capitalize()} list", actions=list(links.items()))
        more = f" (the first 10 of {len(items)})" if len(items) > 10 else ""
        return f"Sent the {storage.list_key(name)} list to their phone, one notification per item with store buttons{more}."

    def run(fn):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args))
            except (ShopError, OSError, ValueError, KeyError) as e:
                return text_result(str(e), error=True)
        return call

    option = {"type": "object", "properties": {"title": {"type": "string"}, "url": {"type": "string"}, "price": {"type": "string"}, "why": {"type": "string"}},
              "required": ["title", "url"]}
    shop_suggest = tool("shop_suggest", "Save two to five product options from your research and send them to the owner's phone as a comparison with tap-to-add buttons.",
                        {"type": "object", "properties": {"query": {"type": "string"}, "options": {"type": "array", "items": option}}, "required": ["options"]})(run(suggest))
    shop_send = tool("shop_send", "Send a one-tap add-to-cart (Amazon) or open-listing (eBay, AliExpress) link to the owner's phone, by option number from the last "
                     "shop_suggest or by store link.", {"type": "object", "properties": {"option": {"type": "integer"}, "url": {"type": "string"},
                                                                                         "title": {"type": "string"}, "price": {"type": "string"}}})(run(send))
    shop_list_to_phone = tool("shop_list_to_phone", "Send a list (default shopping) to the owner's phone with Amazon, eBay and AliExpress search links per item.",
                              {"type": "object", "properties": {"list": {"type": "string"}}})(run(list_to_phone))
    return ToolModule(server="shopping", prompt=PROMPT, available=lambda c: ntfy.enabled, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household()) for fn in (shop_suggest, shop_send, shop_list_to_phone)])
