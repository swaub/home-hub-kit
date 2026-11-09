from datetime import datetime, timedelta

import pytest

from homehub import household
from homehub.packages import (Gmail, PackageError, PackageTracker, detect_carrier, html_text, merge, message_body, parse_email,
                                 resolve_eta, track_link)

SENT = datetime(2026, 10, 29, 12, 40)
AMAZON_SHIPPED = """Alex, your package was shipped!
Ordered

Shipped

Out for delivery

Delivered

Arriving today 5 PM – 8 PM

Order #
112-3456789-1234567

Track package
https://www.amazon.com/progress-tracker/package?_encoding=UTF8&orderId=112-3456789-1234567&packageIndex=0&shipmentId=Abc123&ref_=x
"""
SHOP_SHIPPED = """Your order is on the way

Order #48213

UPS tracking number: 1Z999AA10123456784
( http://wwwapps.ups.com/WebTracking/processRequest?tracknum=1Z999AA10123456784 )

Items in this shipment
----------------------

Ceramic Plant Pot × 2
"""


def test_carriers_are_recognized_by_their_number_formats():
    assert detect_carrier("UPS tracking number: 1Z999AA10123456784") == ("UPS", "1Z999AA10123456784")
    assert detect_carrier("USPS 9400 1000 0000 0000 0000 00")[0] == ""
    assert detect_carrier("Tracking: 9400100000000000000000") == ("USPS", "9400100000000000000000")
    assert detect_carrier("FedEx tracking number 123456789012") == ("FedEx", "123456789012")
    assert detect_carrier("order 123456789012 shipped") == ("", "")
    assert detect_carrier("Amazon TBA123456789012") == ("Amazon", "TBA123456789012")
    assert detect_carrier("Package YT2629421606015423 is out for delivery") == ("AliExpress", "YT2629421606015423")
    assert track_link("UPS", "1Z999AA10123456784") == "https://www.ups.com/track?tracknum=1Z999AA10123456784"
    assert track_link("USPS", "9400100000000000000000").endswith("tLabels=9400100000000000000000")


def test_arrival_words_become_a_real_day():
    assert resolve_eta("Arriving today 5 PM – 8 PM", SENT) == "Thu, Oct 29 5 PM – 8 PM"
    assert resolve_eta("Arriving tomorrow by 10 PM", SENT) == "Fri, Oct 30 by 10 PM"
    assert resolve_eta("Arriving Monday", SENT) == "Mon, Nov 2"
    assert resolve_eta("Estimated delivery: Nov 6 - Nov 9", SENT) == "Nov 6 - Nov 9"
    assert resolve_eta("Thanks for shopping", SENT) == ""


def test_shipping_emails_from_amazon_shops_and_aliexpress_become_packages():
    amazon = parse_email("Amazon.com <shipment-tracking@amazon.com>", "Shipped 1 item: Phone Accessories", AMAZON_SHIPPED, SENT)
    assert amazon["key"] == "amazon:112-3456789-1234567" and amazon["status"] == "shipped" and amazon["item"] == "Phone Accessories"
    assert amazon["merchant"] == "Amazon" and amazon["eta"] == "Thu, Oct 29 5 PM – 8 PM" and "progress-tracker" in amazon["url"]
    shop = parse_email("Plant Shop <team@plantshop.com>", "A shipment from order #48213 is on the way", SHOP_SHIPPED, SENT)
    assert shop["key"] == "tracking:1Z999AA10123456784" and shop["carrier"] == "UPS" and shop["status"] == "shipped"
    assert shop["item"] == "Ceramic Plant Pot × 2" and shop["merchant"] == "Plant Shop" and shop["url"].startswith("https://www.ups.com/track")
    ali = parse_email("transaction@notice.aliexpress.com", "Package YT2629421606015423 is out for delivery", "", SENT)
    assert ali["status"] == "out_for_delivery" and ali["carrier"] == "AliExpress" and ali["merchant"] == "Aliexpress"
    assert ali["item"] == "Aliexpress package"
    confirmed = parse_email("Plant Shop <team@plantshop.com>", "Order #48213 confirmed", "Thanks! Order #48213", SENT)
    assert confirmed["key"] == "order:plantshop.com:48213" and confirmed["status"] == "ordered"


def test_food_deliveries_sales_and_chatter_are_not_packages():
    assert parse_email("DoorDash <no-reply@doordash.com>", "Your order is on the way", "Dasher is on the way", SENT) is None
    assert parse_email("eBay <ebay@ebay.com>", "You made the sale for Apple MacBook Pro", "Ship by Nov 3", SENT) is None
    assert parse_email("seller <x@members.ebay.com>", "seller sent a message about your item", "", SENT) is None
    assert parse_email("News <news@site.com>", "Weekly digest", "nothing shipped here", SENT) is None
    assert parse_email("Shop <a@shop.com>", "Your package is on its way", "no numbers at all", SENT) is None


def test_the_newest_email_wins_and_delivery_is_remembered():
    first = parse_email("Plant Shop <team@plantshop.com>", "A shipment from order #48213 is on the way", SHOP_SHIPPED, SENT)
    out = {**first, "status": "out_for_delivery", "updated": (SENT + timedelta(days=2)).isoformat()}
    late_copy = {**first, "status": "shipped", "updated": (SENT + timedelta(days=1)).isoformat()}
    row = merge(merge(None, first, "alex"), out, "alex")
    assert row["status"] == "out_for_delivery"
    assert merge(row, late_copy, "alex")["status"] == "out_for_delivery"
    done = merge(row, {**first, "status": "delivered", "eta": "", "updated": (SENT + timedelta(days=2, hours=5)).isoformat()}, "alex")
    assert done["status"] == "delivered" and done["delivered_at"] and done["eta"] == ""
    assert merge(done, {**first, "status": "problem", "updated": (SENT + timedelta(days=3)).isoformat()}, "alex")["status"] == "problem"


def test_message_bodies_come_from_plain_text_or_cleaned_html():
    import base64

    encode = lambda text: base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    plain = {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": encode("Arriving today")}},
                                                            {"mimeType": "text/html", "body": {"data": encode("<b>ignored</b>")}}]}
    assert message_body(plain) == "Arriving today"
    rich = {"mimeType": "text/html", "body": {"data": encode('<style>p{}</style><p>UPS &amp; you</p><a href="https://ups.com/x">Track</a>')}}
    assert "UPS & you" in message_body(rich) and "https://ups.com/x" in message_body(rich) and "p{}" not in html_text("<style>p{}</style>")


def test_the_tracker_keeps_one_list_per_owner_and_pushes_the_moments_that_matter(private_config, temp_storage):
    example_config = private_config
    household.sync_household(example_config, temp_storage, household.EXAMPLE_PATH)
    pushed, events = [], []
    tracker = PackageTracker(example_config, temp_storage, "alex", emit=lambda name, **p: events.append((name, p)),
                             notify=lambda title, text, url: pushed.append((title, text)))
    now = datetime.now().replace(microsecond=0)
    shop = parse_email("Plant Shop <team@plantshop.com>", "A shipment from order #48213 is on the way", SHOP_SHIPPED, now - timedelta(hours=3))
    tracker.apply(shop)
    tracker.apply({**shop, "status": "out_for_delivery", "updated": (now - timedelta(hours=1)).isoformat()})
    assert pushed == [("Out for delivery: Ceramic Plant Pot × 2", "Plant Shop · UPS")]
    assert [p["status"] for n, p in events if n == "package.changed"] == ["shipped", "out_for_delivery"]
    tracker.apply({**shop, "status": "out_for_delivery", "updated": now.isoformat()})
    assert len(pushed) == 1
    old = parse_email("Amazon.com <shipment-tracking@amazon.com>", "Delivered 1 item: Old Thing", AMAZON_SHIPPED.replace("112-3456789", "113-0000000"),
                      now - timedelta(days=9))
    tracker.apply(old)
    assert len(pushed) == 1
    added = tracker.add("9400 1000 0000 0000 0000 00", "Birthday gift")
    assert added["carrier"] == "USPS" and added["status"] == "tracking" and added["url"].startswith("https://tools.usps.com")
    with pytest.raises(PackageError):
        tracker.add("hi")
    current = tracker.current()
    assert [r["key"] for r in current] == ["tracking:1Z999AA10123456784", "tracking:9400100000000000000000"]
    assert current[0]["label"] == "Out for delivery"
    assert "Ceramic Plant Pot × 2 from Plant Shop: out for delivery by UPS" in tracker.describe()
    assert PackageTracker(example_config, temp_storage, "sam").current() == []
    assert tracker.hide("tracking:9400100000000000000000") and [r["key"] for r in tracker.current()] == ["tracking:1Z999AA10123456784"]
    assert not PackageTracker(example_config, temp_storage, "sam").hide("tracking:1Z999AA10123456784")


def test_without_a_gmail_sign_in_the_scan_says_how_to_connect(private_config, temp_storage):
    tracker = PackageTracker(private_config, temp_storage, "alex")
    with pytest.raises(PackageError, match="Gmail isn't connected"):
        tracker.scan()
    assert "Gmail isn't connected" in tracker.last_error


@pytest.mark.live
def test_a_real_gmail_scan_fills_the_list_with_known_statuses(temp_storage):
    from homehub.config import load_config

    cfg = load_config()
    if not Gmail(cfg).ready():
        pytest.skip("Gmail isn't connected on this machine")
    tracker = PackageTracker(cfg, temp_storage, None)
    tracker.scan(days=30)
    rows = temp_storage.packages(None)
    assert tracker.last_error == "" and all(r["status"] in ("ordered", "label", "shipped", "out_for_delivery", "delivered", "problem") for r in rows)
    assert all(r["key"].startswith(("amazon:", "tracking:", "order:")) for r in rows)
    assert tracker.scan(days=30) == 0
