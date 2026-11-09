import asyncio
import html
import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

TOPICS = {
    "top": ("NPR", "https://feeds.npr.org/1001/rss.xml"),
    "us": ("BBC", "https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml"),
    "world": ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    "business": ("NPR", "https://feeds.npr.org/1006/rss.xml"),
    "technology": ("NPR", "https://feeds.npr.org/1019/rss.xml"),
    "science": ("NPR", "https://feeds.npr.org/1007/rss.xml"),
    "health": ("NPR", "https://feeds.npr.org/1128/rss.xml"),
}
CACHE_SECONDS = 600
TIMEOUT = 8
PROMPT = ("For news use news_headlines (topics: top, local, us, world, business, technology, science, health) or news_search for a "
          "subject; never a web search for headlines. Read out the few most important headlines in plain words, newest first, "
          "with how long ago when it matters.")


def strip_markup(text):
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", text or "")).split())


def parse_feed(xml_text, source):
    root = ET.fromstring(xml_text)
    items = []
    for item in root.iter("item"):
        title = strip_markup(item.findtext("title"))
        if not title:
            continue
        published = None
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").timestamp()
        except (TypeError, ValueError):
            pass
        items.append({"title": title, "summary": strip_markup(item.findtext("description"))[:300], "source": source,
                      "published": published, "link": (item.findtext("link") or "").strip()})
    return sorted(items, key=lambda i: -(i["published"] or 0))


def age(published, now=None):
    if not published:
        return ""
    minutes = int(((now or time.time()) - published) // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min ago"
    if minutes < 48 * 60:
        return f"{minutes // 60} h ago"
    return f"{minutes // 1440} days ago"


class News:
    def __init__(self, cfg):
        self.topics = dict(TOPICS)
        if cfg.news.local_feed.strip():
            self.topics["local"] = (cfg.news.local_name.strip() or "local news", cfg.news.local_feed.strip())
        self.cache = {}

    def fetch(self, topic):
        source, url = self.topics[topic]
        cached = self.cache.get(topic)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        request = urllib.request.Request(url, headers={"User-Agent": "homehub"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            items = parse_feed(response.read(), source)
        self.cache[topic] = (time.monotonic(), items)
        return items

    def headlines(self, topic="top", count=5):
        topic = (topic or "top").strip().lower()
        if topic not in self.topics:
            raise ValueError(f"No news topic called {topic}. Topics: {', '.join(self.topics)}.")
        return self.fetch(topic)[:max(1, min(int(count), 15))]

    def search(self, query, count=5):
        words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
        if not words:
            raise ValueError("Say what the news should be about.")
        found, seen = [], set()
        for topic in self.topics:
            try:
                items = self.fetch(topic)
            except OSError:
                continue
            for item in items:
                text = f"{item['title']} {item['summary']}".lower()
                if item["title"] not in seen and all(w in text for w in words):
                    seen.add(item["title"])
                    found.append(item)
        return sorted(found, key=lambda i: -(i["published"] or 0))[:max(1, min(int(count), 15))]


def describe(items):
    return "\n".join(f"- {i['title']} ({i['source']}, {age(i['published'])})" + (f": {i['summary']}" if i["summary"] else "")
                     for i in items)


def build_module(cfg):
    news = News(cfg)

    @tool("news_headlines", "Latest headlines for a topic: top, local (if set up), us, world, business, technology, science, health. "
          "count is how many (default 5).", {"type": "object", "properties": {"topic": {"type": "string"}, "count": {"type": "integer"}}})
    async def news_headlines(args):
        try:
            items = await asyncio.to_thread(news.headlines, args.get("topic") or "top", args.get("count") or 5)
        except (ValueError, OSError, ET.ParseError) as e:
            return text_result(f"Couldn't get the news: {e}", error=True)
        return text_result(describe(items) if items else "No headlines right now.")

    @tool("news_search", "Find recent headlines about a subject across all the news feeds (for example 'Seahawks' or 'interest rates').",
          {"type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer"}}, "required": ["query"]})
    async def news_search(args):
        try:
            items = await asyncio.to_thread(news.search, args["query"], args.get("count") or 5)
        except ValueError as e:
            return text_result(str(e), error=True)
        return text_result(describe(items) if items else f"No recent headlines mention {args['query']}.")

    return ToolModule(server="news", prompt=PROMPT, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="news feeds")
        for fn in (news_headlines, news_search)])
