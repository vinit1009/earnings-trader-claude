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

    def revenue_surprise_pct(self) -> float | None:
        """Revenue actual vs estimate as a percent. Positive = beat."""
        if (
            self.revenue_actual is None
            or self.revenue_estimate is None
            or self.revenue_estimate == 0
        ):
            return None
        return (self.revenue_actual - self.revenue_estimate) / abs(self.revenue_estimate) * 100


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


@functools.lru_cache(maxsize=1)
def _alpaca_data():
    """Alpaca historical-bars client. Used because Finnhub free tier no longer
    exposes /stock/candle (returns 403). Alpaca paper accounts include free
    historical IEX bars."""
    from alpaca.data.historical import StockHistoricalDataClient

    key = os.environ.get("ALPACA_KEY_ID")
    secret = os.environ.get("ALPACA_SECRET")
    if not key or not secret:
        raise RuntimeError("ALPACA_KEY_ID / ALPACA_SECRET not set in env")
    return StockHistoricalDataClient(api_key=key, secret_key=secret)


@functools.lru_cache(maxsize=64)
def _fetch_year_bars(symbol: str) -> list[tuple[_dt.date, float]] | None:
    """Fetch ~14 months of daily (date, close) bars, cached per process per symbol.

    Returns an ascending list of (trading_date, close_price). None on any error.
    14 months (420 calendar days) covers 4 prior earnings quarters for the
    implied-move proxy. The cache eliminates redundant Alpaca calls when
    compute_implied_move_proxy and get_realized_vol both need the same symbol.
    """
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    end_date = _dt.date.today() - _dt.timedelta(days=1)
    start_date = end_date - _dt.timedelta(days=420)
    try:
        req = StockBarsRequest(
            symbol_or_symbols=[symbol.upper()],
            timeframe=TimeFrame.Day,
            start=_dt.datetime.combine(start_date, _dt.time.min),
            end=_dt.datetime.combine(end_date, _dt.time.max),
            feed=DataFeed.IEX,
        )
        result = _alpaca_data().get_stock_bars(req)
    except Exception as e:
        log.info("Alpaca year-bars fetch failed for %s: %s", symbol, e)
        return None
    try:
        bars = result[symbol.upper()]
    except (KeyError, TypeError):
        return None
    if not bars:
        return None
    out: list[tuple[_dt.date, float]] = []
    for b in bars:
        ts = b.timestamp
        d: _dt.date = ts.date() if hasattr(ts, "date") else _dt.date.fromisoformat(str(ts)[:10])
        out.append((d, float(b.close)))
    return out


def get_daily_closes(symbol: str, days: int) -> list[float] | None:
    """Return up to `days` most-recent daily closes for `symbol` (ascending date order).

    Backed by _fetch_year_bars (cached per process). Returns None on auth or API errors.
    """
    bars = _fetch_year_bars(symbol.upper())
    if not bars:
        return None
    closes = [c for _, c in bars]
    return closes[-days:]


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


def get_realized_vol(symbol: str, days: int = 30) -> float | None:
    """Annualized realized volatility from daily log-returns over last `days` candles.

    Returns percent (e.g. 35.2 means 35.2% annualized vol). None on insufficient data.
    Uses cached year bars — no extra Alpaca call if the symbol was already fetched.
    """
    import math

    bars = _fetch_year_bars(symbol.upper())
    if not bars or len(bars) < days + 1:
        return None
    closes = [c for _, c in bars[-(days + 1):]]
    log_returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
    if not log_returns:
        return None
    mean = sum(log_returns) / len(log_returns)
    var = sum((r - mean) ** 2 for r in log_returns) / max(1, len(log_returns) - 1)
    return round(math.sqrt(var) * math.sqrt(252) * 100, 2)


def get_market_regime() -> dict:
    """Classify the market regime from SPY trend + recent volatility.

    Uses Alpaca historical bars (Finnhub free tier blocks /stock/candle). SPY-only
    classification — when SPY breaks down, realized vol rises in correlation,
    making a separate VIX feed redundant for our coarse regime buckets.

    Returns:
        {regime, spy_price, spy_50dma, spy_pct_above_50dma, spy_1d_pct, spy_5d_pct, reason}
    """
    closes = get_daily_closes("SPY", 70) or []
    if len(closes) < 50:
        return {"regime": "UNKNOWN", "reason": f"only {len(closes)} SPY candles from Alpaca"}

    spy_price = closes[-1]
    spy_50dma = sum(closes[-50:]) / 50.0
    spy_pct_above_50dma = (spy_price - spy_50dma) / spy_50dma * 100
    spy_1d_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0.0
    spy_5d_pct = (closes[-1] - closes[-6]) / closes[-6] * 100 if len(closes) >= 6 else 0.0

    if spy_1d_pct < -2.0 and spy_5d_pct < -5.0:
        regime = "CRISIS"
    elif spy_pct_above_50dma < -2.0 or spy_5d_pct < -3.0:
        regime = "STRESSED"
    elif spy_pct_above_50dma > 1.0 and spy_5d_pct > 0:
        regime = "TRENDING_UP"
    else:
        regime = "RANGEBOUND"

    return {
        "regime": regime,
        "spy_price": round(spy_price, 2),
        "spy_50dma": round(spy_50dma, 2),
        "spy_pct_above_50dma": round(spy_pct_above_50dma, 2),
        "spy_1d_pct": round(spy_1d_pct, 2),
        "spy_5d_pct": round(spy_5d_pct, 2),
        "reason": "spy-based classification",
    }


def _post_print_abs_move_pct(symbol: str, print_date: _dt.date) -> float | None:
    """Absolute % move on the trading day after `print_date`, using cached year bars.

    Returns None if we can't determine the move (no history, recent IPO, etc.).
    Compares the close at the first trading day on/after (print_date - 2 days) to the
    next trading day's close — same semantic as the original per-quarter Alpaca query.
    """
    bars = _fetch_year_bars(symbol.upper())
    if not bars:
        return None
    window_start = print_date - _dt.timedelta(days=2)
    idx = next((i for i, (d, _) in enumerate(bars) if d >= window_start), None)
    if idx is None or idx + 1 >= len(bars):
        return None
    base = bars[idx][1]
    next_close = bars[idx + 1][1]
    if base == 0:
        return None
    return abs((next_close - base) / base) * 100


@functools.lru_cache(maxsize=64)
def get_announcement_dates(symbol: str, lookback_days: int = 400) -> list[_dt.date]:
    """Return actual announcement dates for a symbol over the past `lookback_days`.

    Finnhub `company_earnings` returns fiscal period END dates, not announcement dates.
    This queries the earnings calendar historically to get the true announcement dates,
    which is what we need to measure the post-print price move correctly.
    """
    end = _dt.date.today()
    start = end - _dt.timedelta(days=lookback_days)
    try:
        raw = _with_rate_limit(
            _client().earnings_calendar,
            _from=start.isoformat(),
            to=end.isoformat(),
            symbol=symbol.upper(),
            international=False,
        )
    except Exception as e:
        log.info("get_announcement_dates failed for %s: %s", symbol, e)
        return []
    dates = []
    for row in (raw.get("earningsCalendar") or []):
        d_str = row.get("date", "")
        if d_str:
            try:
                dates.append(_dt.date.fromisoformat(d_str))
            except ValueError:
                pass
    return sorted(dates)


def compute_implied_move_proxy(
    symbol: str, prior_quarters: list[EarningsEvent]
) -> float | None:
    """Mean absolute next-day-return over the last 4 earnings prints, in percent.

    This stands in for the options-implied move (which the free Finnhub tier doesn't expose).
    The mean of |1-day return| around a stock's last 4 prints is a reasonable proxy:
    if the stock has historically moved ~5% on earnings, ~5% is a fair baseline expectation.

    Uses actual announcement dates from the Finnhub earnings calendar rather than fiscal
    period-end dates — for companies with Dec 31 fiscal year-ends, the announcement is
    typically in late January/early February, not the last trading day of December.

    Returns None if we can't compute (no history, candle endpoint dry).
    """
    ann_dates = get_announcement_dates(symbol)

    moves: list[float] = []
    for q in prior_quarters:
        if not q.date:
            continue
        try:
            period_end = _dt.date.fromisoformat(q.date)
        except ValueError:
            continue

        # Find announcement date: first calendar entry within 90 days after fiscal period end
        ann_date: _dt.date | None = None
        for d in ann_dates:
            if period_end <= d <= period_end + _dt.timedelta(days=90):
                ann_date = d
                break

        if ann_date is None:
            # Fallback: center of the typical 30-60d announcement window
            ann_date = period_end + _dt.timedelta(days=45)

        m = _post_print_abs_move_pct(symbol, ann_date)
        if m is not None:
            moves.append(m)
    if not moves:
        return None
    return sum(moves) / len(moves)


def get_pre_earnings_drift(symbol: str) -> float | None:
    """5-trading-day price drift leading into today's earnings print, in percent.

    A large positive drift (>+5%) means buy-the-rumor has partially priced in
    the expected beat — PEAD effect is weaker. A flat/negative drift means
    the market was caught off-guard and PEAD extends further on a beat.

    Uses cached year bars — no extra Alpaca call if the symbol was already fetched.
    """
    bars = _fetch_year_bars(symbol.upper())
    if not bars or len(bars) < 6:
        return None
    # bars[-1] is yesterday's close (most recent); bars[-6] is 5 trading days before that
    recent_close = bars[-1][1]
    five_days_ago_close = bars[-6][1]
    if five_days_ago_close == 0:
        return None
    return round((recent_close - five_days_ago_close) / five_days_ago_close * 100, 2)


def get_analyst_signals(symbol: str, since_date: _dt.date) -> dict:
    """Return analyst rating changes and consensus since `since_date`.

    Uses Finnhub free-tier endpoints: stock_upgrades_downgrades and recommendation_trends.
    Both are available on the free plan.
    """
    sym = symbol.upper()

    # Rating changes since since_date
    upgrades: list[dict] = []
    downgrades: list[dict] = []
    try:
        raw_changes = _with_rate_limit(
            _client().stock_upgrades_downgrades,
            symbol=sym,
            _from=since_date.isoformat(),
        )
        for row in raw_changes or []:
            action = (row.get("action") or "").lower()
            entry = {
                "date": row.get("gradeDate", ""),
                "firm": row.get("company") or row.get("firm") or "",
                "from_grade": row.get("fromGrade") or "",
                "to_grade": row.get("toGrade") or "",
                "action": action,
            }
            if action in ("upgrade", "buy", "strong buy", "outperform", "overweight"):
                upgrades.append(entry)
            elif action in ("downgrade", "sell", "underperform", "underweight"):
                downgrades.append(entry)
            elif row.get("toGrade") and row.get("fromGrade"):
                # Classify by grade direction when action label is vague
                to_g = (row.get("toGrade") or "").lower()
                from_g = (row.get("fromGrade") or "").lower()
                buy_grades = {"buy", "strong buy", "outperform", "overweight", "add"}
                sell_grades = {"sell", "underperform", "underweight", "reduce", "strong sell"}
                if to_g in buy_grades and from_g not in buy_grades:
                    upgrades.append(entry)
                elif to_g in sell_grades and from_g not in sell_grades:
                    downgrades.append(entry)
    except Exception as e:
        log.info("analyst upgrade/downgrade fetch failed for %s: %s", sym, e)

    # Consensus
    consensus: dict = {}
    try:
        raw_trends = _with_rate_limit(_client().recommendation_trends, sym)
        if raw_trends:
            latest = raw_trends[0]  # most recent month
            consensus = {
                "strong_buy": int(latest.get("strongBuy") or 0),
                "buy": int(latest.get("buy") or 0),
                "hold": int(latest.get("hold") or 0),
                "sell": int(latest.get("sell") or 0),
                "strong_sell": int(latest.get("strongSell") or 0),
                "period": latest.get("period") or "",
            }
    except Exception as e:
        log.info("recommendation_trends fetch failed for %s: %s", sym, e)

    return {
        "upgrades_since_open": upgrades,
        "downgrades_since_open": downgrades,
        "consensus": consensus,
    }


def get_ah_volume_today(symbol: str) -> float | None:
    """Total shares traded in today's AH session (4 PM–now ET). None on any error.

    Uses Alpaca SIP feed (extended hours). Returns None gracefully if SIP is unavailable
    on the current Alpaca plan — callers must handle None. A very low value (<200K for
    large-caps) signals a thin tape that may reverse at the regular-session open.
    """
    import zoneinfo

    et = zoneinfo.ZoneInfo("America/New_York")
    today = _dt.date.today()
    ah_start = _dt.datetime(today.year, today.month, today.day, 16, 0, tzinfo=et)
    now_et = _dt.datetime.now(tz=et)
    if now_et < ah_start:
        return None  # regular session not yet closed

    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        req = StockBarsRequest(
            symbol_or_symbols=[symbol.upper()],
            timeframe=TimeFrame.Hour,
            start=ah_start,
            end=now_et,
            feed=DataFeed.SIP,
        )
        result = _alpaca_data().get_stock_bars(req)
        bars = result[symbol.upper()] or []
        total = sum(float(b.volume) for b in bars)
        return round(total, 0) if total > 0 else None
    except Exception as e:
        log.info("AH volume fetch failed for %s: %s", symbol, e)
        return None


def compute_beat_consistency(prior_quarters: list[EarningsEvent]) -> dict:
    """Aggregate EPS surprise history to detect consistent beaters and sandbaggers.

    sandbagging_flag=True means the company routinely beats by >20% — the market expects
    it and the composite EPS beat signal should be discounted by 1 point.
    beat_rate_4q=1.0 AND NOT sandbagging_flag means a reliable non-sandbagging beater
    worth a +0.5 composite bonus.
    """
    surprises = [
        q.eps_surprise_pct()
        for q in prior_quarters
        if q.eps_surprise_pct() is not None
    ]
    if not surprises:
        return {"beat_rate_4q": None, "avg_eps_surprise_4q": None, "sandbagging_flag": False}
    beat_count = sum(1 for s in surprises if s > 2.0)
    beat_rate = beat_count / len(surprises)
    avg_surprise = sum(surprises) / len(surprises)
    return {
        "beat_rate_4q": round(beat_rate, 2),
        "avg_eps_surprise_4q": round(avg_surprise, 2),
        "sandbagging_flag": avg_surprise > 20.0,
    }


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
