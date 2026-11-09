import asyncio
import secrets
import time

import pytest

from homehub.tools import result_texts
from homehub.tools.notify import Ntfy
from homehub.tools.shopping import ShopError, asin_of, build_module, cart_link, search_links, store_of

AMAZON = "https://www.amazon.com/3M-Double-Sided-Mounting-Tape/dp/B00004Z4DU/ref=sr_1_3?keywords=tape"


def test_store_links_become_cart_or_listing_links():
    assert store_of(AMAZON) == "amazon" and store_of("https://www.ebay.com/itm/1234") == "ebay" and store_of("https://www.aliexpress.us/item/1.html") == "aliexpress"
    with pytest.raises(ShopError):
        store_of("https://www.walmart.com/ip/1")
    assert asin_of(AMAZON) == "B00004Z4DU" and asin_of("https://www.amazon.com/gp/product/B08N5WRWNW") == "B08N5WRWNW"
    assert cart_link(AMAZON) == ("Open in Amazon", "https://www.amazon.com/dp/B00004Z4DU")
    assert cart_link("https://www.ebay.com/itm/1234") == ("Open on eBay", "https://www.ebay.com/itm/1234")
    links = search_links("double sided tape")
    assert links["Amazon"] == "https://www.amazon.com/s?k=double+sided+tape" and links["AliExpress"].endswith("wholesale-double-sided-tape.html")


def test_shopping_needs_phone_push():
    from homehub.config import parse_config

    assert not build_module(None, Ntfy("", "")).available(parse_config({}))
    assert build_module(None, Ntfy("https://ntfy.sh", "x")).available(parse_config({}))


@pytest.mark.live
def test_options_are_saved_and_pushed_with_buttons_and_a_number_sends_the_cart_link(temp_storage):
    ntfy = Ntfy("https://ntfy.sh", "homehub-test-" + secrets.token_hex(6))
    tools = {spec.name: spec.fn.handler for spec in build_module(temp_storage, ntfy).tools}
    options = [{"title": "3M mounting tape", "url": AMAZON, "price": "$8.99", "why": "strong and removable"},
               {"title": "Gorilla tape", "url": "https://www.ebay.com/itm/1234", "price": "$7.50"}]
    reply = result_texts(asyncio.run(tools["shop_suggest"]({"query": "double sided tape", "options": options})))[0]
    assert reply.startswith("Sent 2 options")
    assert temp_storage.get_setting("shop_options")["options"][1]["title"] == "Gorilla tape"
    sent = result_texts(asyncio.run(tools["shop_send"]({"option": 1})))[0]
    assert sent.startswith("Sent 'Open in Amazon' for 3M mounting tape")
    assert "no option 5" in result_texts(asyncio.run(tools["shop_send"]({"option": 5})))[0]
    messages = []
    for _ in range(12):
        time.sleep(1)
        messages = [m for m in ntfy.recent() if m.get("event") == "message"]
        if len(messages) >= 2:
            break
    first, second = messages[0], messages[1]
    assert first["title"] == "Options: double sided tape" and [a["label"] for a in first["actions"]] == ["Open 1", "Open 2"]
    assert second["actions"][0]["url"] == "https://www.amazon.com/dp/B00004Z4DU"


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_researches_options_and_adds_one_by_number_in_a_later_conversation(harness, temp_storage):
    from homehub.brain.session import Session

    ntfy = Ntfy("https://ntfy.sh", "homehub-test-" + secrets.token_hex(6))
    h = harness([build_module(temp_storage, ntfy)])

    async def scenario():
        async with h:
            first = await h.ask("Find me the best double sided mounting tape under 15 dollars on Amazon.")
            first_calls = h.calls()
        h.session = await Session(h.options, emit=h.emit).connect()
        try:
            second = await h.ask("Add number one to my cart.")
            return first, first_calls, second, h.tools_called()
        finally:
            await h.session.close()

    first, first_calls, second, second_tools = asyncio.run(scenario())
    print(f"\n  first: {first}\n  second: {second} {second_tools}")
    saved = temp_storage.get_setting("shop_options")
    assert saved and all(store_of(o["url"]) in ("amazon", "ebay", "aliexpress") for o in saved["options"])
    assert "shop_send" in second_tools and "add to cart" in second.lower()


@pytest.mark.live
def test_the_list_goes_to_the_phone_with_real_buttons(temp_storage):
    ntfy = Ntfy("https://ntfy.sh", "homehub-test-" + secrets.token_hex(6))
    temp_storage.add_list_items("shopping", ["double sided tape", "crickets"])
    tools = {spec.name: spec.fn.handler for spec in build_module(temp_storage, ntfy).tools}
    assert "one notification per item" in result_texts(asyncio.run(tools["shop_list_to_phone"]({})))[0]
    messages = []
    for _ in range(12):
        time.sleep(1)
        messages = [m for m in ntfy.recent() if m.get("event") == "message"]
        if len(messages) >= 2:
            break
    assert [m["message"] for m in messages] == ["double sided tape", "crickets"]
    assert [a["label"] for a in messages[0]["actions"]] == ["Amazon", "eBay", "AliExpress"]
