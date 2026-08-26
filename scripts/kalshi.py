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
import unicodedata
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

BALLON_SERIES = "KXBALLONDOR"
BALLON_TOP_N = 20

# Kalshi lists Ballon d'Or candidates by name with no club, so the squad has to
# be named here. Worth a look each transfer window.
ARSENAL_SQUAD = [
    "David Raya", "Kepa Arrizabalaga",
    "Ben White", "William Saliba", "Gabriel", "Gabriel Magalhaes",
    "Cristhian Mosquera", "Riccardo Calafiori", "Jurrien Timber",
    "Myles Lewis-Skelly", "Piero Hincapie", "Ezri Konsa",
    "Martin Odegaard", "Declan Rice", "Martin Zubimendi", "Mikel Merino",
    "Bruno Guimaraes", "Ethan Nwaneri", "Axel Donczew",
    "Bukayo Saka", "Noni Madueke", "Leandro Trossard", "Eberechi Eze",
    "Kai Havertz", "Viktor Gyokeres", "Christos Tzolis",
]

# The named parlays, largest first. Each is the product of its legs, which
# assumes independence — the same squad plays all five, so treat it as a
# sketch rather than a price.
MULTIPLES = [
    ("The Pent", ["Premier League", "Champions League", "FA Cup", "Carabao Cup", SHIELD]),
    ("The Quad", ["Premier League", "Champions League", "FA Cup", "Carabao Cup"]),
    ("The Treble", ["Premier League", "Champions League", "FA Cup"]),
    ("The Double", ["Premier League", "Champions League"]),
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
    # A zero side is an absent quote, not a price of zero. Averaging it in would
    # halve the price of every one-sided market — the whole tail of the Ballon
    # d'Or field sits on a lone 1c ask — so fall back to the side that exists.
    quoted = [side for side in (bid, ask) if side]
    if quoted:
        mid = sum(quoted) / len(quoted)
        if 0 < mid < 1:
            return mid, bid, ask
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
            "odds": round(1.0 / probability, 1),
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
    """The Pent, Quad, Treble and Double, each priced as the product of its legs.

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
            "odds": round(1.0 / probability, 1),
            "impliedProbability": round(probability, 4),
            "bestBookmaker": "Implied (product of the legs)",
            "source": "derived",
            "priceCents": round(probability * 100, 1),
            "lastUpdated": priced[0]["lastUpdated"],
        })

    return out


def _normalize_name(name):
    """Kalshi writes names unaccented ("Ousmane Dembele"), so compare stripped.

    NFD leaves 'ø' intact, so it is mapped explicitly before the accent strip.
    """
    lowered = (name or "").lower().replace("ø", "o").replace("æ", "ae").replace("ł", "l")
    decomposed = unicodedata.normalize("NFD", lowered)
    return "".join(c for c in decomposed if c.isalpha() and not unicodedata.combining(c))


_SQUAD_KEYS = {_normalize_name(n) for n in ARSENAL_SQUAD}


def fetch_ballon_dor():
    """Arsenal players priced inside the top 20 of Kalshi's Ballon d'Or field.

    Ranking is standard competition ranking — players on the same price share
    the best rank they could hold. The tail of this market is a long tie on one
    cent, so breaking it alphabetically would include or drop players at random.

    Returns None when no Arsenal player makes the cut, which is the normal case
    and keeps the section off the page entirely.
    """
    payload = _get("markets", {"series_ticker": BALLON_SERIES, "status": "open", "limit": 300})
    if not payload:
        return None

    field = []
    for market in payload.get("markets") or []:
        probability, _, _ = _price(market)
        if probability is None:
            continue
        field.append({
            "player": market.get("yes_sub_title") or market.get("ticker"),
            "probability": round(probability, 4),
        })
    if not field:
        print("[warn] no priced Ballon d'Or markets")
        return None

    field.sort(key=lambda e: e["probability"], reverse=True)
    for entry in field:
        better = [e for e in field if e["probability"] > entry["probability"]]
        same = [e for e in field if e["probability"] == entry["probability"]]
        entry["rank"] = len(better) + 1
        entry["tiedWith"] = len(same) - 1

    arsenal = [
        e for e in field
        if _normalize_name(e["player"]) in _SQUAD_KEYS and e["rank"] <= BALLON_TOP_N
    ]
    if not arsenal:
        print("[ok] no Arsenal player in the Ballon d'Or top 20")
        return None

    print(f"[ok] ballon d'or: {', '.join(e['player'] for e in arsenal)}")
    return {
        "marketUrl": "https://kalshi.com/markets/kxballondor",
        "fieldSize": len(field),
        "leader": field[0],
        "arsenal": arsenal,
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
