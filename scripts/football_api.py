"""Fetch real match data (result, formations, lineups, stats) from API-Football.

Everything this module returns is *grounded* — it comes from the API, not from a
model. The digest prompt is only allowed to explain these numbers, never to
invent them, which is what keeps the tactical write-up trustworthy for someone
who can't yet spot a wrong claim on their own.

Requires FOOTBALL_API_KEY (free tier at https://dashboard.api-football.com).
Returns an empty list if the key is absent or the plan rejects a query, so the
digest degrades cleanly rather than failing the run.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://v3.football.api-sports.io"
ARSENAL_TEAM_ID = 42

# Stats we surface, mapped from API-Football's labels to our own keys.
STAT_KEYS = {
    "expected_goals": "xg",
    "Ball Possession": "possession",
    "Total Shots": "shots",
    "Shots on Goal": "shotsOnTarget",
    "Shots insidebox": "shotsInBox",
    "Corner Kicks": "corners",
    "Fouls": "fouls",
    "Offsides": "offsides",
    "Total passes": "passes",
    "Passes accurate": "passesAccurate",
    "Passes %": "passAccuracy",
    "Yellow Cards": "yellowCards",
    "Red Cards": "redCards",
    "Goalkeeper Saves": "saves",
}


def _get(path, params):
    """Single GET against API-Football. Returns the `response` list, or None."""
    api_key = os.environ.get("FOOTBALL_API_KEY")
    if not api_key:
        return None
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    req = Request(url, headers={
        "x-apisports-key": api_key,
        "User-Agent": "arsenal-tracker/1.0",
    })
    try:
        with urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, ValueError, TimeoutError) as e:
        print(f"[warn] football api {path} failed: {e}")
        return None

    errors = payload.get("errors")
    # API-Football returns [] on success and a dict of messages on failure.
    if isinstance(errors, dict) and errors:
        print(f"[warn] football api {path} returned errors: {errors}")
        return None
    return payload.get("response") or []


def _num(value):
    """API-Football mixes ints, decimal strings, percent strings and None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip().rstrip("%")
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return None


def _side(fixture, team_id):
    return "home" if fixture["teams"]["home"]["id"] == team_id else "away"


def fetch_recent_matches(count=3):
    """The last `count` completed Arsenal fixtures, oldest first.

    Fetching several rather than one matters during a congested week: the digest
    runs every 3 days, so a league game plus a cup tie would otherwise mean the
    earlier match is never written up at all.

    Returns [] when the key is missing, the API fails, or nothing has been
    played — every caller must handle that.
    """
    if not os.environ.get("FOOTBALL_API_KEY"):
        print("[info] FOOTBALL_API_KEY not set; skipping tactical breakdown")
        return []

    fixtures = _fetch_fixture_list(count)
    if not fixtures:
        print("[info] no completed Arsenal fixture available")
        return []

    matches = [m for m in (_build_match(fx) for fx in fixtures) if m]
    matches.sort(key=lambda m: m.get("date") or "")
    return matches[-count:]


def _season_for(moment):
    """API-Football labels European seasons by their starting year."""
    return moment.year if moment.month >= 7 else moment.year - 1


def _fetch_fixture_list(count):
    """Completed Arsenal fixtures, newest last.

    The free plan rejects the `last` parameter outright, so this walks a series
    of query shapes and uses the first that returns data, logging each attempt.
    Scoping by season is the form the free tier documents as supported.
    """
    now = datetime.now(timezone.utc)
    season = _season_for(now)
    window_start = (now - timedelta(days=45)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    team = ARSENAL_TEAM_ID

    attempts = [
        ("season+status", {"team": team, "season": season, "status": "FT-AET-PEN"}),
        ("season+date-range", {"team": team, "season": season, "from": window_start, "to": today}),
        ("season only", {"team": team, "season": season}),
        ("last (paid plans only)", {"team": team, "last": count, "status": "FT-AET-PEN"}),
    ]

    for label, params in attempts:
        resp = _get("fixtures", params)
        if not resp:
            print(f"[info] fixtures via {label}: nothing usable")
            continue
        finished = [fx for fx in resp if _is_finished(fx)]
        print(f"[ok] fixtures via {label}: {len(resp)} returned, {len(finished)} completed")
        if finished:
            finished.sort(key=lambda fx: (fx.get("fixture") or {}).get("date") or "")
            return finished[-count:]

    return []


def _is_finished(fx):
    short = (((fx.get("fixture") or {}).get("status")) or {}).get("short")
    return short in {"FT", "AET", "PEN"}


def _build_match(fx):
    fixture_id = fx["fixture"]["id"]
    side = _side(fx, ARSENAL_TEAM_ID)
    opp_side = "away" if side == "home" else "home"
    opponent = fx["teams"][opp_side]

    match = {
        "fixtureId": fixture_id,
        "date": (fx["fixture"].get("date") or "")[:10],
        "competition": (fx.get("league") or {}).get("name"),
        "round": (fx.get("league") or {}).get("round"),
        "venue": (fx["fixture"].get("venue") or {}).get("name"),
        "homeAway": "H" if side == "home" else "A",
        "opponent": opponent.get("name"),
        "opponentId": opponent.get("id"),
        "goalsFor": (fx.get("goals") or {}).get(side),
        "goalsAgainst": (fx.get("goals") or {}).get(opp_side),
        "arsenalFormation": None,
        "opponentFormation": None,
        "arsenalCoach": None,
        "opponentCoach": None,
        "arsenalXI": [],
        "opponentXI": [],
        "stats": {"arsenal": {}, "opponent": {}},
        "goalscorers": [],
    }
    match["result"] = _result_letter(match["goalsFor"], match["goalsAgainst"])

    _attach_lineups(match, fixture_id)
    _attach_stats(match, fixture_id)
    _attach_goals(match, fixture_id)
    return match


def _result_letter(gf, ga):
    if gf is None or ga is None:
        return None
    return "W" if gf > ga else ("L" if gf < ga else "D")


def _attach_lineups(match, fixture_id):
    lineups = _get("fixtures/lineups", {"fixture": fixture_id})
    for entry in lineups or []:
        is_arsenal = (entry.get("team") or {}).get("id") == ARSENAL_TEAM_ID
        names = [
            (p.get("player") or {}).get("name")
            for p in entry.get("startXI") or []
            if (p.get("player") or {}).get("name")
        ]
        coach = (entry.get("coach") or {}).get("name")
        if is_arsenal:
            match["arsenalFormation"] = entry.get("formation")
            match["arsenalCoach"] = coach
            match["arsenalXI"] = names
        else:
            match["opponentFormation"] = entry.get("formation")
            match["opponentCoach"] = coach
            match["opponentXI"] = names


def _attach_stats(match, fixture_id):
    stats = _get("fixtures/statistics", {"fixture": fixture_id})
    for entry in stats or []:
        is_arsenal = (entry.get("team") or {}).get("id") == ARSENAL_TEAM_ID
        bucket = {}
        for row in entry.get("statistics") or []:
            key = STAT_KEYS.get(row.get("type"))
            if not key:
                continue
            value = _num(row.get("value"))
            if value is not None:
                bucket[key] = value
        match["stats"]["arsenal" if is_arsenal else "opponent"] = bucket


def _attach_goals(match, fixture_id):
    events = _get("fixtures/events", {"fixture": fixture_id})
    for ev in events or []:
        if (ev.get("type") or "").lower() != "goal":
            continue
        match["goalscorers"].append({
            "player": (ev.get("player") or {}).get("name"),
            "minute": (ev.get("time") or {}).get("elapsed"),
            "team": (ev.get("team") or {}).get("name"),
            "detail": ev.get("detail"),
        })
    match["goalscorers"].sort(key=lambda g: g.get("minute") or 0)


def summarize_for_prompt(match):
    """Compact, unambiguous rendering of the grounded data for the LLM.

    Only facts present here may appear in the generated tactical commentary.
    """
    if not match:
        return "(no match data available)"

    def stat_line(label, key, suffix=""):
        ars = match["stats"]["arsenal"].get(key)
        opp = match["stats"]["opponent"].get(key)
        if ars is None and opp is None:
            return None
        fmt = lambda v: "n/a" if v is None else f"{v}{suffix}"
        return f"- {label}: Arsenal {fmt(ars)} | {match['opponent']} {fmt(opp)}"

    rows = [
        stat_line("Expected goals (xG)", "xg"),
        stat_line("Possession", "possession", "%"),
        stat_line("Total shots", "shots"),
        stat_line("Shots on target", "shotsOnTarget"),
        stat_line("Shots inside box", "shotsInBox"),
        stat_line("Corners", "corners"),
        stat_line("Total passes", "passes"),
        stat_line("Pass accuracy", "passAccuracy", "%"),
        stat_line("Fouls", "fouls"),
        stat_line("Goalkeeper saves", "saves"),
    ]
    stats_block = "\n".join(r for r in rows if r) or "- (no statistics returned for this fixture)"

    goals = "\n".join(
        f"- {g['minute']}' {g['player']} ({g['team']}){' — ' + g['detail'] if g.get('detail') else ''}"
        for g in match["goalscorers"]
    ) or "- (none)"

    return f"""FIXTURE: Arsenal {match['goalsFor']}-{match['goalsAgainst']} {match['opponent']} \
({'home' if match['homeAway'] == 'H' else 'away'}, {match['competition']}, {match['date']})
ARSENAL FORMATION (as listed): {match['arsenalFormation'] or 'not reported'}
OPPONENT FORMATION (as listed): {match['opponentFormation'] or 'not reported'}
ARSENAL STARTING XI: {', '.join(match['arsenalXI']) or 'not reported'}
OPPONENT MANAGER: {match['opponentCoach'] or 'not reported'}

MATCH STATISTICS:
{stats_block}

GOALS:
{goals}"""
