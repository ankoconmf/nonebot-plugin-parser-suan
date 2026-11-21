"""X 推文 $cashtag 的行情抓取 (仅用于渲染卡片上的股票小组件)

推文数据里只有代码 (`cashtag_attachments`) 和公司名 (`entities.symbols`), 价格/涨跌/分时
由前端另调接口, 所以这里自己补一份行情。

数据源用 Yahoo Finance 的公开 chart 接口 (无需 key)。**任何失败都不抛异常**:
整条组件静默不渲染, 不影响卡片其余部分。
"""

from __future__ import annotations

from time import monotonic
from asyncio import gather
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
from nonebot import logger

QUOTE_API = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
REQUEST_TIMEOUT = 6.0
"""单次请求超时 (秒), 超时就当作没有行情"""
CACHE_TTL = 300.0
"""同代码行情缓存 (秒), 避免同一条推反复解析时反复请求"""
MAX_TICKERS = 3
"""一条推最多渲染几个股票组件"""
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


@dataclass(slots=True)
class StockQuote:
    """模板直接消费的行情 (文本已格式化好)"""

    symbol: str
    """代码, 例如 NVDA"""
    name: str
    """公司名"""
    price_text: str
    """现价, 例如 $125.53"""
    change_text: str
    """涨跌额, 例如 +1.23 / -20.45"""
    pct_text: str
    """涨跌幅, 例如 +1.56%"""
    up: bool
    """是否上涨 (决定颜色/箭头)"""
    market_text: str
    """行情时间说明, 例如 '2026-11-20 16:00 收盘'"""
    spark_points: str
    """SVG polyline 坐标串 (viewBox 168x56), 数据不足时为空串"""


_CACHE: dict[str, tuple[float, StockQuote]] = {}


def _spark_points(closes: list[float | None], width: int = 168, height: int = 56) -> str:
    values = [value for value in closes if value is not None]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    pad = 4
    step = (width - 2 * pad) / (len(values) - 1)
    return " ".join(
        f"{pad + index * step:.1f},{height - pad - (value - low) / span * (height - 2 * pad):.1f}"
        for index, value in enumerate(values)
    )


def _yahoo_symbol(ticker: str) -> str:
    """X 的 cashtag 基本是美股代码, 已是别名的代码 (BRK.B / BTC-USD / 600519.SS) 原样透传"""
    return ticker.strip().upper()


async def _fetch_one(
    client: httpx.AsyncClient, ticker: str, name: str
) -> StockQuote | None:
    response = await client.get(
        QUOTE_API.format(symbol=_yahoo_symbol(ticker)),
        params={"range": "1d", "interval": "5m"},
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    chart = ((response.json().get("chart") or {}).get("result") or [None])[0]
    if not chart:
        return None

    meta = chart.get("meta") or {}
    price = meta.get("regularMarketPrice")
    if price is None:
        return None

    previous = meta.get("chartPreviousClose") or meta.get("previousClose") or price
    change = price - previous
    percent = change / previous * 100 if previous else 0.0
    currency = (meta.get("currency") or "USD").upper()
    prefix = "$" if currency == "USD" else ""

    closes = ((chart.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    stamp = meta.get("regularMarketTime")
    market_text = "最新价"
    if stamp:
        offset = meta.get("gmtoffset")
        tz = timezone(timedelta(seconds=int(offset))) if offset is not None else timezone.utc
        moment = datetime.fromtimestamp(int(stamp), tz)
        session = "盘中" if (datetime.now(timezone.utc) - moment).total_seconds() < 900 else "收盘"
        market_text = f"{moment.strftime('%Y-%m-%d %H:%M')} {session}"

    return StockQuote(
        symbol=ticker,
        name=name or meta.get("shortName") or "",
        price_text=f"{prefix}{price:,.2f}",
        change_text=f"{change:+,.2f}",
        pct_text=f"{percent:+.2f}%",
        up=change >= 0,
        market_text=market_text,
        spark_points=_spark_points(closes),
    )


async def fetch_quotes(tickers: list[tuple[str, str]]) -> list[StockQuote]:
    """并发拉取行情, 失败的代码直接丢弃 (绝不抛异常)"""
    targets = [item for item in tickers if item[0]][:MAX_TICKERS]
    if not targets:
        return []

    quotes: dict[str, StockQuote] = {}
    now = monotonic()
    pending: list[tuple[str, str]] = []
    for ticker, name in targets:
        cached = _CACHE.get(ticker)
        if cached and now - cached[0] < CACHE_TTL:
            quotes[ticker] = cached[1]
        else:
            pending.append((ticker, name))

    if pending:
        try:
            async with httpx.AsyncClient(
                timeout=REQUEST_TIMEOUT, follow_redirects=True
            ) as client:
                gathered = await gather(
                    *(_fetch_one(client, ticker, name) for ticker, name in pending),
                    return_exceptions=True,
                )
            for (ticker, _), outcome in zip(pending, gathered, strict=True):
                if isinstance(outcome, StockQuote):
                    quotes[ticker] = outcome
                    _CACHE[ticker] = (now, outcome)
                elif isinstance(outcome, Exception):
                    logger.debug(
                        f"X 股票行情获取失败 {ticker}: {type(outcome).__name__}"
                    )
        except Exception as error:  # noqa: BLE001 - 连客户端都建不起来时同样静默
            logger.debug(f"X 股票行情客户端异常: {type(error).__name__}")

    # 保持推文里的先后顺序
    return [quotes[ticker] for ticker, _ in targets if ticker in quotes]
