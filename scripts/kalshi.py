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
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
# Kalshi throttles bursts with 429s. Without a retry, a throttled series kept
# its previous price under a fresh timestamp, so stale odds read as current.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_BACKOFF_SECONDS = 2
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
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429 and attempt < RATE_LIMIT_RETRIES:
                try:
                    wait = float(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    wait = 0
                time.sleep(max(wait, RATE_LIMIT_BACKOFF_SECONDS * 2 ** attempt))
                continue
            print(f"[warn] kalshi {path} failed: {e}")
            return None
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


def _arsenal_markets(series_ticker, status):
    payload = _get("markets", {"series_ticker": series_ticker, "status": status, "limit": 300})
    if not payload:
        return None
    return [
        m for m in payload.get("markets") or []
        if (m.get("ticker") or "").upper().endswith(TEAM_SUFFIX)
    ]


def _arsenal_market(series_ticker):
    """(market, settled) for the current season, or (None, False).

    Several seasons can be open at once, so prefer the one closing soonest.
    With no open market, look for one that has settled: when Arsenal are knocked
    out, Kalshi resolves their contract to "no" and delists it. Reporting that
    as "no market" left a pre-elimination Carabao Cup price on the site for two
    weeks after they had gone out.
    """
    live = _arsenal_markets(series_ticker, "open")
    if live is None:
        return None, False
    if live:
        live.sort(key=lambda m: m.get("close_time") or "9999")
        return live[0], False

    settled = [m for m in _arsenal_markets(series_ticker, "settled") or []
               if m.get("result") in ("yes", "no")]
    if settled:
        settled.sort(key=lambda m: m.get("close_time") or "")
        return settled[-1], True
    print(f"[warn] no Arsenal market in series {series_ticker}")
    return None, False


def _price(market):
    """Midpoint of the spread, falling back to last traded price.

    The midpoint is the fairer read of what the market believes: quoting the ask
    alone would systematically overstate Arsenal's chances. It needs both sides,
    though — when a thin market loses its bid, Kalshi reports the bid as 0, and
    averaging that in would halve the price overnight.
    """
    bid, ask = _money(market, "yes_bid_dollars"), _money(market, "yes_ask_dollars")
    if bid and ask and bid <= ask < 1:
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
        market, settled = _arsenal_market(series_ticker)
        if not market:
            continue

        if settled:
            won = market["result"] == "yes"
            items.append({
                "competition": competition,
                "odds": None,
                "impliedProbability": 1.0 if won else 0.0,
                # The trophy is decided: rendered as "Won"/"Out", never as a price.
                "settled": "won" if won else "out",
                "bestBookmaker": "Kalshi",
                "source": "kalshi",
                "marketTicker": market.get("ticker"),
                "marketUrl": f"https://kalshi.com/markets/{series_ticker.lower()}",
                "priceCents": 100.0 if won else 0.0,
                "bidCents": None,
                "askCents": None,
                "lastUpdated": (market.get("close_time") or today)[:10],
            })
            print(f"[ok] kalshi {competition}: settled, Arsenal {'won' if won else 'out'} "
                  f"({market.get('ticker')})")
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
        if "out" not in (pl.get("settled"), ucl.get("settled")):
            return None
        # One leg is gone, so the double is too.
        return {
            "competition": "Double (PL + UCL)",
            "odds": None,
            "impliedProbability": 0.0,
            "settled": "out",
            "bestBookmaker": "Implied (product of the two Kalshi markets)",
            "source": "derived",
            "priceCents": 0.0,
            "lastUpdated": pl["lastUpdated"],
        }
    return {
        "competition": "Double (PL + UCL)",
        "odds": round(1.0 / probability, 2),
        "impliedProbability": round(probability, 4),
        "bestBookmaker": "Implied (product of the two Kalshi markets)",
        "source": "derived",
        "priceCents": round(probability * 100, 2),
        "lastUpdated": pl["lastUpdated"],
    }
