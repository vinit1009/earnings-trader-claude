"""SEC EDGAR client for 8-K earnings release retrieval.

Free public data (no API key). SEC asks callers to identify themselves via the
User-Agent header. Rate limit: 10 requests/sec.

The cloud routine env must allowlist `www.sec.gov` for this to work in production.
"""
from __future__ import annotations

import datetime as _dt
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx


log = logging.getLogger(__name__)

EDGAR_BASE = "https://www.sec.gov"
EDGAR_DATA_BASE = "https://data.sec.gov"
USER_AGENT = "earnings-trader-claude (vinitshah268@gmail.com)"
_HEADERS = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
_CLIENT_TIMEOUT = 15.0
_REQ_INTERVAL_SEC = 0.12  # ~8 req/sec, under SEC's 10/sec ceiling

_ticker_to_cik: dict[str, str] = {}
_ticker_cache_date: _dt.date | None = None
_last_request_at: float = 0.0


def _client() -> httpx.Client:
    return httpx.Client(headers=_HEADERS, timeout=_CLIENT_TIMEOUT, follow_redirects=True)


def _throttle() -> None:
    """Sleep to stay under SEC's rate ceiling."""
    global _last_request_at
    now = time.monotonic()
    delta = now - _last_request_at
    if delta < _REQ_INTERVAL_SEC:
        time.sleep(_REQ_INTERVAL_SEC - delta)
    _last_request_at = time.monotonic()


def _refresh_ticker_cache() -> None:
    """Reload the ticker→CIK map from SEC (~13k entries, refreshed daily)."""
    global _ticker_to_cik, _ticker_cache_date
    if _ticker_cache_date == _dt.date.today() and _ticker_to_cik:
        return
    _throttle()
    with _client() as c:
        resp = c.get(f"{EDGAR_BASE}/files/company_tickers.json")
        resp.raise_for_status()
        data = resp.json()
    new_map = {}
    for item in data.values():
        ticker = (item.get("ticker") or "").upper()
        cik_raw = item.get("cik_str")
        if ticker and cik_raw is not None:
            new_map[ticker] = str(cik_raw).zfill(10)
    if new_map:
        _ticker_to_cik = new_map
        _ticker_cache_date = _dt.date.today()
        log.info("EDGAR ticker cache: %d entries", len(_ticker_to_cik))


def get_cik(symbol: str) -> str | None:
    """Return zero-padded 10-digit CIK for a ticker, or None if unknown."""
    _refresh_ticker_cache()
    return _ticker_to_cik.get(symbol.upper())


@dataclass(frozen=True)
class EarningsRelease:
    symbol: str
    cik: str
    accession_number: str
    filing_date: str          # ISO 'YYYY-MM-DD'
    primary_doc_url: str
    text: str                  # cleaned HTML-stripped text
    item_codes: list[str]      # e.g. ['2.02', '9.01']


def _find_press_release_exhibit(
    index_url: str, accession: str, primary_doc: str
) -> str | None:
    """Try to find Exhibit 99.1 (the earnings press release) in the 8-K filing dir.

    Looks at the JSON file index — exhibits are usually named ex-99.1, ex991, exhibit99-1, etc.
    Returns the URL of the press release file, or None if we can't find it.
    """
    _throttle()
    with _client() as c:
        try:
            resp = c.get(f"{index_url}/index.json")
            resp.raise_for_status()
            idx = resp.json()
        except (httpx.HTTPError, ValueError):
            return None
    items = (idx.get("directory") or {}).get("item") or []
    names = [(f.get("name") or "") for f in items]

    primary_lower = primary_doc.lower()

    def _eligible(name: str) -> bool:
        n = name.lower()
        if n == primary_lower:
            return False
        if not n.endswith((".htm", ".html")):
            return False
        if "index" in n or "metalinks" in n or "summary" in n:
            return False
        return True

    patterns = [
        lambda n: "ex99" in n or "ex-99" in n,                  # Standard exhibit naming
        lambda n: n.endswith("pr.htm") or n.endswith("pr.html"),  # NVDA-style q4fy26pr.htm
        lambda n: "press" in n or "release" in n,                # "press release"
        lambda n: "earnings" in n,
    ]
    for matcher in patterns:
        for name in names:
            if _eligible(name) and matcher(name.lower()):
                return f"{index_url}/{name}"
    return None


def _strip_html(html: str) -> str:
    """Lightweight HTML→text; no BeautifulSoup dependency."""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"</p[^>]*>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_latest_earnings_8k(
    symbol: str,
    *,
    on_or_after: _dt.date | None = None,
    max_chars: int = 15000,
) -> EarningsRelease | None:
    """Find the latest 8-K filing containing Item 2.02 (earnings release).

    Returns the press release text (stripped of HTML), or None if no qualifying
    filing exists since `on_or_after` (default: 2 days ago).
    """
    cik = get_cik(symbol)
    if not cik:
        log.info("EDGAR: unknown ticker %s", symbol)
        return None

    cutoff = on_or_after or (_dt.date.today() - _dt.timedelta(days=2))

    _throttle()
    with _client() as c:
        sub_url = f"{EDGAR_DATA_BASE}/submissions/CIK{cik}.json"
        try:
            resp = c.get(sub_url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            log.info("EDGAR submissions fetch failed for %s: %s", symbol, e)
            return None
        subs = resp.json()

    recent = (subs.get("filings") or {}).get("recent") or {}
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primary_docs = recent.get("primaryDocument", [])
    items_list = recent.get("items", [])

    for i, form in enumerate(forms):
        if form != "8-K":
            continue
        items_raw = items_list[i] if i < len(items_list) else ""
        item_codes = [x.strip() for x in re.split(r"[,;\s]+", items_raw) if x.strip()]
        if not any("2.02" in c for c in item_codes):
            continue
        try:
            filing_date = _dt.date.fromisoformat(dates[i])
        except (ValueError, IndexError):
            continue
        if filing_date < cutoff:
            # filings are date-desc; once we're past the cutoff, stop looking
            break

        accession = accessions[i].replace("-", "")
        primary_doc = primary_docs[i]
        index_url = f"{EDGAR_BASE}/Archives/edgar/data/{int(cik)}/{accession}"

        text = ""
        doc_url = f"{index_url}/{primary_doc}"
        ex_url = _find_press_release_exhibit(index_url, accession, primary_doc)
        if ex_url:
            doc_url = ex_url

        _throttle()
        with _client() as c:
            try:
                resp = c.get(doc_url)
                resp.raise_for_status()
                text = _strip_html(resp.text)[:max_chars]
            except httpx.HTTPError as e:
                log.info("EDGAR doc fetch failed for %s: %s", symbol, e)
                return None
        return EarningsRelease(
            symbol=symbol.upper(),
            cik=cik,
            accession_number=accessions[i],
            filing_date=dates[i],
            primary_doc_url=doc_url,
            text=text,
            item_codes=item_codes,
        )

    return None
