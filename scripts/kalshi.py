"""Arsenal trophy prices from Kalshi's public market data.

Kalshi contracts settle at $1, so a price *is* an implied probability — no
bookmaker overround to strip out. The tradeoff is a bid/ask spread, so we quote
the midpoint and record both sides.

No API key: the market-data endpoints are unauthenticated.

Season tickers carry a year suffix (KXPREMIERLEAGUE-27-ARS), so markets are
discovered from the series rather than hardcoded, and the tracker keeps working
when the season rolls over.
"""

import json
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
TEAM_SUFFIX = "-ARS"

# Competition name (as shown in the tracker) -> Kalshi series ticker.
SERIES = {
    "Premier League": "KXPREMIERLEAGUE",
    "Champions League": "KXUCL",
    "FA Cup": "KXFACUP",
    "Carabao Cup": "KXEFLCUP",
}


def _get(path, params=None):
    url = f"{API_BASE}/{path}"
    if params:
        url += "?" + urlencode(params)
    req = Request(url, headers={
        "accept": "application/json",
        "User-Agent": "arsenal-tracker/1.0",
    })
    try:
        with urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, ValueError, TimeoutError) as e:
        print(f"[warn] kalshi {path} failed: {e}")
        return None


def _money(market, key):
    """Kalshi returns prices as dollar strings ('0.4400'); older int fields are null."""
    value = market.get(key)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _arsenal_market(series_ticker):
    """The current-season Arsenal market in a series.

    Several seasons can be open at once, so prefer the one closing soonest.
    """
    payload = _get("markets", {"series_ticker": series_ticker, "status": "open", "limit": 300})
    if not payload:
        return None

    candidates = [
        m for m in payload.get("markets") or []
        if (m.get("ticker") or "").upper().endswith(TEAM_SUFFIX)
    ]
    if not candidates:
        print(f"[warn] no Arsenal market in series {series_ticker}")
        return None
    candidates.sort(key=lambda m: m.get("close_time") or "9999")
    return candidates[0]


def _price(market):
    """Midpoint of the spread, falling back to last traded price.

    The midpoint is the fairer read of what the market believes: quoting the ask
    alone would systematically overstate Arsenal's chances.
    """
    bid, ask = _money(market, "yes_bid_dollars"), _money(market, "yes_ask_dollars")
    if bid is not None and ask is not None and 0 < (bid + ask) / 2 < 1:
        return (bid + ask) / 2, bid, ask
    last = _money(market, "last_price_dollars")
    if last is not None and 0 < last < 1:
        return last, bid, ask
    return None, bid, ask


def fetch_trophy_prices():
    """One item per competition, in the tracker's odds.json shape.

    Competitions Kalshi has no open market for are simply absent, so the caller
    can keep whatever it had rather than blanking a row.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    items = []

    for competition, series_ticker in SERIES.items():
        market = _arsenal_market(series_ticker)
        if not market:
            continue

        probability, bid, ask = _price(market)
        if probability is None:
            print(f"[warn] no usable price for {competition} ({market.get('ticker')})")
            continue

        ticker = market.get("ticker")
        items.append({
            "competition": competition,
            "odds": round(1.0 / probability, 2),
            "impliedProbability": round(probability, 4),
            "bestBookmaker": "Kalshi",
            "source": "kalshi",
            "marketTicker": ticker,
            "marketUrl": f"https://kalshi.com/markets/{series_ticker.lower()}",
            "priceCents": round(probability * 100, 1),
            "bidCents": None if bid is None else round(bid * 100),
            "askCents": None if ask is None else round(ask * 100),
            "openInterest": _money(market, "open_interest_fp"),
            "lastUpdated": today,
        })
        print(f"[ok] kalshi {competition}: {probability * 100:.1f}% ({ticker})")

    return items


def double_item(items):
    """PL + UCL parlay, priced as the product of the two probabilities.

    Independence is an approximation — the same squad plays both — but it is the
    same assumption the previous bookmaker-derived figure made.
    """
    by_name = {i["competition"]: i for i in items}
    pl = by_name.get("Premier League")
    ucl = by_name.get("Champions League")
    if not pl or not ucl:
        return None

    probability = pl["impliedProbability"] * ucl["impliedProbability"]
    if probability <= 0:
        return None
    return {
        "competition": "Double (PL + UCL)",
        "odds": round(1.0 / probability, 2),
        "impliedProbability": round(probability, 4),
        "bestBookmaker": "Implied (product of the two Kalshi markets)",
        "source": "derived",
        "priceCents": round(probability * 100, 2),
        "lastUpdated": pl["lastUpdated"],
    }
