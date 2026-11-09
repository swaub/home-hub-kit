import asyncio
import json
import time
import urllib.parse
import urllib.request

from claude_agent_sdk import tool

from homehub.tools import Scope, ToolModule, ToolSpec, text_result

QUOTE_URL = "https://api.nasdaq.com/api/quote/{symbol}/info?assetclass={asset}"
SEARCH_URL = "https://api.nasdaq.com/api/autocomplete/slookup/10?search={query}"
COIN_SEARCH_URL = "https://api.coingecko.com/api/v3/search?query={query}"
COIN_PRICE_URL = "https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true"
TIMEOUT = 8
CACHE_SECONDS = 60
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PROMPT = ("For US stock and fund prices use stock_price with a ticker or company name; for cryptocurrencies use crypto_price. "
          "Say the price in dollars and today's change as a percent. These are delayed market data, not advice.")


def money(value):
    return f"${value:,.2f}" if value >= 1 else f"${value:,.4f}"


def change_words(percent):
    if percent is None:
        return ""
    direction = "up" if percent >= 0 else "down"
    return f"{direction} {abs(percent):.1f}% today"


class Prices:
    def __init__(self):
        self.cache = {}

    def get(self, url, browser=False):
        cached = self.cache.get(url)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA if browser else "homehub", "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
        self.cache[url] = (time.monotonic(), data)
        return data

    def lookup(self, query):
        text = query.strip()
        found = self.get(SEARCH_URL.format(query=urllib.parse.quote(text)), browser=True).get("data") or []
        exact = [f for f in found if f.get("symbol", "").upper() == text.upper()]
        best = (exact or found or [None])[0]
        if best is None:
            return text.upper(), "stocks"
        return best["symbol"], "etf" if best.get("asset") == "ETF" else "stocks"

    def stock(self, query):
        symbol, asset = self.lookup(query)
        data = self.get(QUOTE_URL.format(symbol=urllib.parse.quote(symbol), asset=asset), browser=True).get("data")
        if not data or not data.get("primaryData", {}).get("lastSalePrice"):
            raise ValueError(f"No stock or fund found for {query}.")
        primary = data["primaryData"]
        price = float(primary["lastSalePrice"].replace("$", "").replace(",", ""))
        percent = primary.get("percentageChange", "").replace("%", "").replace("+", "")
        percent = float(percent) if percent not in ("", "UNCH", "N/A") else None
        name = data.get("companyName", symbol).replace(" Common Stock", "")
        return f"{name} ({data.get('symbol', symbol)}): {money(price)}, {change_words(percent)}".rstrip(", ")

    def crypto(self, query):
        coins = self.get(COIN_SEARCH_URL.format(query=urllib.parse.quote(query.strip()))).get("coins", [])
        if not coins:
            raise ValueError(f"No cryptocurrency found for {query}.")
        coin = coins[0]
        data = self.get(COIN_PRICE_URL.format(ids=coin["id"])).get(coin["id"])
        if not data:
            raise ValueError(f"No price for {coin['name']} right now.")
        return f"{coin['name']} ({coin['symbol'].upper()}): {money(data['usd'])}, {change_words(data.get('usd_24h_change'))}".rstrip(", ")


def build_module():
    prices = Prices()

    def run(fn, what):
        async def call(args):
            try:
                return text_result(await asyncio.to_thread(fn, args.get("query", "")))
            except (ValueError, OSError, KeyError) as e:
                return text_result(f"Couldn't get that {what} price: {e}", error=True)
        return call

    stock_price = tool("stock_price", "Latest price and today's change for a US stock or fund, by ticker (AAPL, VOO) or company name.",
                       {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})(run(prices.stock, "stock"))
    crypto_price = tool("crypto_price", "Latest US dollar price and 24-hour change for a cryptocurrency by name or symbol (bitcoin, ETH).",
                        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]})(run(prices.crypto, "crypto"))
    return ToolModule(server="finance", prompt=PROMPT, tools=[
        ToolSpec(fn=fn, policy="auto", scope=Scope.household(), untrusted_output=True, untrusted_source="market data") for fn in (stock_price, crypto_price)])
