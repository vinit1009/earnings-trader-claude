"""Finnhub-backed earnings + news data layer.

Reads FINNHUB_KEY from env. Wraps the bits we need:
- upcoming earnings calendar with EPS/revenue estimates
- per-symbol earnings history (for prior reaction analysis)
- recent company news (around earnings prints)
- aggregate news sentiment
- live quote (for risk.py price-deviation check)
"""

from __future__ import annotations

import collections
import datetime as _dt
import functools
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

import finnhub
from finnhub.exceptions import FinnhubAPIException


log = logging.getLogger(__name__)

# Finnhub free tier is 60 req/min — keep headroom.
_RATE_LIMIT_PER_MIN = 50
_RATE_WINDOW_SEC = 60.0
_call_timestamps: collections.deque[float] = collections.deque()
_rate_lock = threading.Lock()
_T = TypeVar("_T")


def _throttle() -> None:
    """Block until we're under the per-minute call budget."""
    with _rate_lock:
        now = time.monotonic()
        while _call_timestamps and now - _call_timestamps[0] > _RATE_WINDOW_SEC:
            _call_timestamps.popleft()
        if len(_call_timestamps) >= _RATE_LIMIT_PER_MIN:
            wait = _RATE_WINDOW_SEC - (now - _call_timestamps[0]) + 0.1
            if wait > 0:
                log.info("rate limit headroom hit; sleeping %.1fs", wait)
                time.sleep(wait)
                now = time.monotonic()
                while _call_timestamps and now - _call_timestamps[0] > _RATE_WINDOW_SEC:
                    _call_timestamps.popleft()
        _call_timestamps.append(time.monotonic())


def _with_rate_limit(fn: Callable[..., _T], *args, retries: int = 3, **kwargs) -> _T:
    """Run a Finnhub call under our rate limiter, retrying transient 429s."""
    delay = 5.0
    for attempt in range(retries):
        _throttle()
        try:
            return fn(*args, **kwargs)
        except FinnhubAPIException as e:
            if "429" in str(e) and attempt < retries - 1:
                log.warning("finnhub 429 on attempt %d, sleeping %.1fs", attempt + 1, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")  # pragma: no cover


@dataclass(frozen=True)
class EarningsEvent:
    symbol: str
    date: str           # ISO date 'YYYY-MM-DD'
    hour: str           # 'amc' | 'bmo' | 'dmh' | '' (during market hours / unknown)
    eps_estimate: float | None
    eps_actual: float | None
    revenue_estimate: float | None
    revenue_actual: float | None

    @property
    def is_amc(self) -> bool:
        return self.hour == "amc"

    @property
    def is_bmo(self) -> bool:
        return self.hour == "bmo"

    def eps_surprise_pct(self) -> float | None:
        """Actual vs estimate as a percent. Positive = beat."""
        if (
            self.eps_actual is None
            or self.eps_estimate is None
            or self.eps_estimate == 0
        ):
            return None
        return (self.eps_actual - self.eps_estimate) / abs(self.eps_estimate) * 100


@dataclass(frozen=True)
class NewsHeadline:
    symbol: str
    headline: str
    summary: str
    source: str
    url: str
    published_at: _dt.datetime
    category: str


@dataclass(frozen=True)
class Sentiment:
    symbol: str
    buzz_articles_in_period: int
    weekly_avg_buzz: float
    bullish_pct: float
    bearish_pct: float
    company_news_score: float        # finnhub composite, ~ -1 (negative) to 1 (positive)
    sector_avg_news_score: float


@dataclass(frozen=True)
class CompanyMetrics:
    symbol: str
    market_cap_usd: float | None        # in dollars (Finnhub returns millions; we convert)
    avg_volume_10d: float | None
    current_price: float | None
    exchange: str
    country: str
    finnhub_industry: str
    share_class: str                     # "common stock", "etf", "adr", ...

    def passes_filter(
        self,
        *,
        min_market_cap_usd: float,
        min_avg_volume: float,
        min_price: float,
        allowed_countries: set[str] | None = None,
        allowed_exchanges: set[str] | None = None,
        common_stock_only: bool = True,
    ) -> tuple[bool, str]:
        if self.market_cap_usd is None or self.market_cap_usd < min_market_cap_usd:
            return False, f"market_cap=${self.market_cap_usd or 0:,.0f} < ${min_market_cap_usd:,.0f}"
        if self.avg_volume_10d is None or self.avg_volume_10d < min_avg_volume:
            return False, f"avg_volume={self.avg_volume_10d or 0:,.0f} < {min_avg_volume:,.0f}"
        if self.current_price is None or self.current_price < min_price:
            return False, f"price=${self.current_price or 0:.2f} < ${min_price:.2f}"
        if allowed_countries and self.country not in allowed_countries:
            return False, f"country={self.country!r}"
        if allowed_exchanges and self.exchange and self.exchange.upper() not in allowed_exchanges:
            return False, f"exchange={self.exchange!r}"
        if common_stock_only and self.share_class and self.share_class.lower() not in ("", "common stock"):
            return False, f"share_class={self.share_class!r}"
        return True, ""


@dataclass(frozen=True)
class Quote:
    symbol: str
    current: float
    high: float
    low: float
    open: float
    previous_close: float
    timestamp: int

    def pct_change(self) -> float:
        if self.previous_close == 0:
            return 0.0
        return (self.current - self.previous_close) / self.previous_close * 100


@functools.lru_cache(maxsize=1)
def _client() -> finnhub.Client:
    key = os.environ.get("FINNHUB_KEY")
    if not key:
        raise RuntimeError("FINNHUB_KEY not set in env")
    return finnhub.Client(api_key=key)


def get_upcoming_earnings(
    days: int = 7,
    watchlist: set[str] | None = None,
    *,
    from_date: _dt.date | None = None,
) -> list[EarningsEvent]:
    """Earnings between `from_date` (default today) and that + `days`.

    Optionally filtered by watchlist. `watchlist=None` returns the full universe.
    """
    start = from_date or _dt.date.today()
    end = start + _dt.timedelta(days=days)
    raw = _with_rate_limit(_client().earnings_calendar,
        _from=start.isoformat(), to=end.isoformat(), symbol="", international=False
    )
    events = []
    for row in raw.get("earningsCalendar") or []:
        sym = row.get("symbol", "").upper()
        if watchlist is not None and sym not in watchlist:
            continue
        events.append(
            EarningsEvent(
                symbol=sym,
                date=row.get("date", ""),
                hour=(row.get("hour") or "").lower(),
                eps_estimate=_safe_float(row.get("epsEstimate")),
                eps_actual=_safe_float(row.get("epsActual")),
                revenue_estimate=_safe_float(row.get("revenueEstimate")),
                revenue_actual=_safe_float(row.get("revenueActual")),
            )
        )
    return events


def get_recent_earnings(symbol: str, quarters: int = 4) -> list[EarningsEvent]:
    """Recent earnings history for a single ticker. Used to study prior reactions."""
    raw = _with_rate_limit(_client().company_earnings, symbol.upper(), limit=quarters)
    out = []
    for row in raw or []:
        out.append(
            EarningsEvent(
                symbol=symbol.upper(),
                date=row.get("period", ""),
                hour="",
                eps_estimate=_safe_float(row.get("estimate")),
                eps_actual=_safe_float(row.get("actual")),
                revenue_estimate=None,
                revenue_actual=None,
            )
        )
    return out


def get_company_news(symbol: str, hours: int = 48) -> list[NewsHeadline]:
    """Recent news for a single ticker. Default last 48 hours."""
    end = _dt.date.today()
    start = end - _dt.timedelta(days=max(1, hours // 24))
    raw = _with_rate_limit(
        _client().company_news,
        symbol.upper(),
        _from=start.isoformat(),
        to=end.isoformat(),
    )
    cutoff_ts = (_dt.datetime.now() - _dt.timedelta(hours=hours)).timestamp()
    out = []
    for n in raw or []:
        ts = n.get("datetime")
        if ts and ts < cutoff_ts:
            continue
        out.append(
            NewsHeadline(
                symbol=symbol.upper(),
                headline=n.get("headline") or "",
                summary=n.get("summary") or "",
                source=n.get("source") or "",
                url=n.get("url") or "",
                published_at=_dt.datetime.fromtimestamp(ts) if ts else _dt.datetime.now(),
                category=n.get("category") or "",
            )
        )
    out.sort(key=lambda h: h.published_at, reverse=True)
    return out


def get_sentiment(symbol: str) -> Sentiment | None:
    """Finnhub's news_sentiment endpoint requires a paid plan; returns None on free."""
    try:
        raw = _with_rate_limit(_client().news_sentiment, symbol.upper())
    except FinnhubAPIException as e:
        if "403" in str(e) or "don't have access" in str(e).lower():
            return None
        raise
    if not raw or "sentiment" not in raw:
        return None
    s = raw.get("sentiment") or {}
    buzz = raw.get("buzz") or {}
    return Sentiment(
        symbol=symbol.upper(),
        buzz_articles_in_period=int(buzz.get("articlesInLastWeek") or 0),
        weekly_avg_buzz=float(buzz.get("weeklyAverage") or 0.0),
        bullish_pct=float(s.get("bullishPercent") or 0.0) * 100,
        bearish_pct=float(s.get("bearishPercent") or 0.0) * 100,
        company_news_score=float(raw.get("companyNewsScore") or 0.0),
        sector_avg_news_score=float(raw.get("sectorAverageNewsScore") or 0.0),
    )


def get_quote(symbol: str) -> Quote | None:
    raw = _with_rate_limit(_client().quote, symbol.upper())
    if not raw or raw.get("c") in (None, 0):
        return None
    return Quote(
        symbol=symbol.upper(),
        current=float(raw["c"]),
        high=float(raw["h"]),
        low=float(raw["l"]),
        open=float(raw["o"]),
        previous_close=float(raw["pc"]),
        timestamp=int(raw.get("t") or 0),
    )


def get_company_metrics(symbol: str) -> CompanyMetrics | None:
    """Fetch market cap, avg volume, exchange/country/share-class for filtering.

    Combines /stock/metric and /stock/profile2 (one call each).
    Returns None if Finnhub has no data for this ticker.
    """
    sym = symbol.upper()
    try:
        metric_raw = _with_rate_limit(_client().company_basic_financials, sym, "all")
    except FinnhubAPIException as e:
        log.info("metrics fetch failed for %s: %s", sym, e)
        return None

    metric = (metric_raw or {}).get("metric") or {}
    market_cap_millions = _safe_float(metric.get("marketCapitalization"))
    market_cap_usd = market_cap_millions * 1_000_000 if market_cap_millions else None
    avg_volume_10d = _safe_float(metric.get("10DayAverageTradingVolume"))
    if avg_volume_10d is not None:
        avg_volume_10d *= 1_000_000  # Finnhub returns millions of shares

    try:
        profile = _with_rate_limit(_client().company_profile2, symbol=sym) or {}
    except FinnhubAPIException:
        profile = {}

    quote = get_quote(sym)
    current_price = quote.current if quote else None

    return CompanyMetrics(
        symbol=sym,
        market_cap_usd=market_cap_usd,
        avg_volume_10d=avg_volume_10d,
        current_price=current_price,
        exchange=profile.get("exchange") or "",
        country=profile.get("country") or "",
        finnhub_industry=profile.get("finnhubIndustry") or "",
        share_class=profile.get("shareClassFIGI") and "common stock" or (profile.get("type") or ""),
    )


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
