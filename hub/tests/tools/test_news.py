import asyncio
import time

import pytest

from homehub.config import parse_config
from homehub.tools import result_texts
from homehub.tools.news import News, age, build_module, parse_feed, strip_markup

FEED = """<?xml version="1.0"?><rss version="2.0"><channel><title>Test</title>
<item><title>Older story</title><description>&lt;p&gt;Rain &amp;amp; wind&lt;/p&gt;</description><pubDate>Sat, 31 Oct 2026 08:00:00 -0700</pubDate></item>
<item><title>Newer story about the Seahawks</title><description>Game recap</description><pubDate>Sat, 31 Oct 2026 12:00:00 -0700</pubDate></item>
<item><title></title></item>
</channel></rss>"""


def test_feeds_are_parsed_newest_first_with_plain_text():
    items = parse_feed(FEED, "Test")
    assert [i["title"] for i in items] == ["Newer story about the Seahawks", "Older story"]
    assert items[1]["summary"] == "Rain & wind" and items[0]["source"] == "Test"
    assert strip_markup("<b>Hi</b> &amp; bye") == "Hi & bye"
    now = items[0]["published"]
    assert age(now - 120, now) == "2 min ago" and age(now - 3 * 3600, now) == "3 h ago" and age(now - 3 * 86400, now) == "3 days ago"


def test_local_news_is_a_topic_only_when_configured():
    assert "local" not in News(parse_config({})).topics
    news = News(parse_config({"news": {"local_feed": "https://www.king5.com/feeds/syndication/rss/news/local", "local_name": "KING 5"}}))
    assert news.topics["local"][0] == "KING 5"
    with pytest.raises(ValueError):
        news.headlines("gossip")


@pytest.mark.live
def test_real_feeds_give_recent_headlines_and_search_works():
    cfg = parse_config({"news": {"local_feed": "https://www.king5.com/feeds/syndication/rss/news/local", "local_name": "KING 5"}})
    tools = {spec.name: spec.fn.handler for spec in build_module(cfg).tools}
    top = result_texts(asyncio.run(tools["news_headlines"]({"topic": "top", "count": 3})))[0]
    assert top.count("\n- ") == 2 and "(NPR, " in top
    local = result_texts(asyncio.run(tools["news_headlines"]({"topic": "local", "count": 2})))[0]
    assert "(KING 5, " in local
    news = News(cfg)
    first_word = [w for w in news.headlines("top", 1)[0]["title"].split() if len(w) > 4][0]
    assert news.search(first_word)


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_reads_the_news_from_the_feeds(harness):
    h = harness([build_module(parse_config({}))])

    async def scenario():
        async with h:
            started = time.perf_counter()
            reply = await h.ask("What's in the news today?")
            return reply, h.tools_called(), time.perf_counter() - started

    reply, called, seconds = asyncio.run(scenario())
    print(f"\n  news -> {called} in {seconds:.1f} s\n      reply: {reply}")
    assert "news_headlines" in called and "WebSearch" not in called and seconds < 15
