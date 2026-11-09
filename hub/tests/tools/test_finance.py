import asyncio
import time

import pytest

from homehub.tools import result_texts
from homehub.tools.finance import Prices, build_module, change_words, money


def test_prices_read_naturally():
    assert money(77466) == "$77,466.00" and money(0.1234) == "$0.1234"
    assert change_words(1.234) == "up 1.2% today" and change_words(-0.56) == "down 0.6% today" and change_words(None) == ""


@pytest.mark.live
def test_real_stock_and_crypto_prices():
    prices = Prices()
    apple = prices.stock("Apple")
    assert apple.startswith("Apple Inc. (AAPL): $")
    assert prices.stock("AAPL").startswith("Apple Inc. (AAPL): $") and "(VOO): $" in prices.stock("VOO")
    bitcoin = prices.crypto("bitcoin")
    assert bitcoin.startswith("Bitcoin (BTC): $")
    assert prices.crypto("eth").startswith("Ethereum (ETH): $")
    tools = {spec.name: spec.fn.handler for spec in build_module().tools}
    assert "Couldn't" in result_texts(asyncio.run(tools["crypto_price"]({"query": "zzqqxxnotacoin"})))[0]


@pytest.mark.live
@pytest.mark.claude
@pytest.mark.slow
def test_claude_quotes_prices_from_the_tools(harness):
    h = harness([build_module()])

    async def scenario():
        async with h:
            started = time.perf_counter()
            reply = await h.ask("How are Apple stock and Bitcoin doing today?")
            return reply, h.tools_called(), time.perf_counter() - started

    reply, called, seconds = asyncio.run(scenario())
    print(f"\n  finance -> {called} in {seconds:.1f} s\n      reply: {reply}")
    assert "stock_price" in called and "crypto_price" in called and "WebSearch" not in called
