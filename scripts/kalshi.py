"""Arsenal trophy prices from Kalshi's public market data.

Kalshi contracts settle at $1, so a price *is* an implied probability — no
bookmaker overround to strip out. The tradeoff is a bid/ask spread, so we quote
the midpoint and record both sides.

No API key: the market-data endpoints are unauthenticated.

Season tickers carry a year suffix (KXPREMIERLEAGUE-27-ARS), so markets are
discovered from the series rather than hardcoded, and the tracker keeps working
when the season rolls over.

The Community Shield has no Kalshi market — it is already won — so it is a fixed
100% carried alongside the four live prices.
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

SHIELD = "Community Shield"

# The named parlays, largest first. Each is the product of its legs, which
# assumes independence — the same squad plays all five, so treat it as a
# sketch rather than a price.
MULTIPLES = [
    ("The Pent", ["Premier League", "Champions League", "FA Cup", "Carabao Cup", SHIELD]),
    ("The Quad", ["Premier League", "Champions League", "FA Cup", "Carabao Cup"]),
    ("The Treble", ["Premier League", "Champions League", "FA Cup"]),
    ("The Duo", ["Premier League", "Champions League"]),
]

LEG_LABELS = {
    "Premier League": "PL",
    "Champions League": "UCL",
    "FA Cup": "FA Cup",
    "Carabao Cup": "Carabao",
    SHIELD: "Shield",
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


def shield_item(last_updated=None):
    """The Community Shield, already won, as a fixed 100%.

    Kalshi has no market for it, so it is asserted rather than priced. It still
    belongs in the list: every multiple that includes it needs a leg to
    multiply, and the tracker should show the trophy Arsenal actually has.
    """
    return {
        "competition": SHIELD,
        "odds": 1.0,
        "impliedProbability": 1.0,
        "bestBookmaker": "Settled — Arsenal won it",
        "source": "settled",
        "priceCents": 100.0,
        "lastUpdated": last_updated or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def multiple_items(items):
    """The Pent, Quad, Treble and Duo, each priced as the product of its legs.

    A multiple whose legs are not all present is skipped rather than priced off
    a partial set, which would quietly overstate it.
    """
    by_name = {i["competition"]: i for i in items}
    out = []

    for name, legs in MULTIPLES:
        priced = [by_name.get(leg) for leg in legs]
        if any(p is None or p.get("impliedProbability") is None for p in priced):
            print(f"[warn] skipping {name}: missing a leg")
            continue

        probability = 1.0
        for p in priced:
            probability *= p["impliedProbability"]
        if probability <= 0:
            continue

        out.append({
            "competition": name,
            "legs": " + ".join(LEG_LABELS[leg] for leg in legs),
            "odds": round(1.0 / probability, 2),
            "impliedProbability": round(probability, 4),
            "bestBookmaker": "Implied (product of the legs)",
            "source": "derived",
            "priceCents": round(probability * 100, 2),
            "lastUpdated": priced[0]["lastUpdated"],
        })

    return out
