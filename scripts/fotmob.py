"""Fetch real Arsenal match data (result, formations, XI, stats) from FotMob.

FotMob's public JSON endpoints carry what the paid football APIs charge for and
the free tiers withhold: current-season formations, expected goals, xGOT, big
chances and territorial stats. Everything returned here is structured data from
the source, never model output — the digest prompt may only explain these
numbers, which is what keeps the write-up trustworthy for a reader who can't yet
spot a wrong claim.

Unofficial and unversioned, so every field is treated as optional and any shape
change degrades to a missing stat rather than a failed run.
"""

import json
from datetime import datetime, timedelta, timezone
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://www.fotmob.com/api/data"
CREST_BASE = "https://images.fotmob.com/image_resources/logo/teamlogo"
ARSENAL_TEAM_ID = 9825
FINISHED_PERIOD = "All"
# Don't reach back into a finished season and spend lessons on stale matches.
MAX_MATCH_AGE_DAYS = 45

# FotMob stat labels -> our keys. Anything unlisted is ignored.
STAT_KEYS = {
    "Expected goals (xG)": "xg",
    "xG on target (xGOT)": "xgot",
    "xG open play": "xgOpenPlay",
    "xG set play": "xgSetPlay",
    "Ball possession": "possession",
    "Total shots": "shots",
    "Shots on target": "shotsOnTarget",
    "Shots inside box": "shotsInBox",
    "Big chances": "bigChances",
    "Big chances missed": "bigChancesMissed",
    "Touches in opposition box": "touchesInBox",
    "Corners": "corners",
    "Accurate passes": "accuratePasses",
    "Opposition half": "passesOppositionHalf",
    "Tackles": "tackles",
    "Interceptions": "interceptions",
    "Keeper saves": "saves",
    "Fouls committed": "fouls",
}


def _get(path, params):
    url = f"{API_BASE}/{path}?{urlencode(params)}"
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; arsenal-tracker/1.0)",
        "Accept": "application/json",
    })
    try:
        with urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (URLError, ValueError, TimeoutError) as e:
        print(f"[warn] fotmob {path} failed: {e}")
        return None


def _num(value):
    """FotMob mixes ints, decimal strings and '352 (85%)' composites."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    head = text.split(" ")[0].rstrip("%")
    try:
        return float(head) if "." in head else int(head)
    except ValueError:
        return None


def fetch_recent_matches(count=3):
    """The last `count` completed Arsenal fixtures, oldest first.

    Several rather than one because the digest runs every 3 days: a league game
    plus a cup tie in the same window would otherwise leave the earlier match
    with no write-up at all.
    """
    team = _get("teams", {"id": ARSENAL_TEAM_ID})
    if not team:
        return []

    fixtures = (((team.get("fixtures") or {}).get("allFixtures") or {}).get("fixtures")) or []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=MAX_MATCH_AGE_DAYS)).strftime("%Y-%m-%d")
    finished = [
        f for f in fixtures
        if (f.get("status") or {}).get("finished")
        and f.get("id")
        and not _is_friendly(f)
        and ((f.get("status") or {}).get("utcTime") or "")[:10] >= cutoff
    ]
    if not finished:
        print(f"[info] no completed competitive Arsenal fixture since {cutoff}")
        return []

    finished.sort(key=lambda f: (f.get("status") or {}).get("utcTime") or "")
    matches = []
    for fixture in finished[-count:]:
        match = fetch_match(fixture["id"], fixture.get("pageUrl"))
        if match:
            matches.append(match)
    return matches


def _is_friendly(fixture):
    """Pre-season friendlies make poor teaching material and would burn lessons."""
    name = ((fixture.get("tournament") or {}).get("name") or "").lower()
    return "friendl" in name


def fetch_match(match_id, page_url=None):
    """Full detail for one fixture, or None.

    `page_url` comes from the fixture list; the match-detail payload carries no
    canonical link of its own.
    """
    payload = _get("matchDetails", {"matchId": match_id})
    if not payload:
        return None

    general = payload.get("general") or {}
    content = payload.get("content") or {}
    header = payload.get("header") or {}

    home = general.get("homeTeam") or {}
    away = general.get("awayTeam") or {}
    arsenal_home = home.get("id") == ARSENAL_TEAM_ID
    opponent = away if arsenal_home else home

    scores = _scores(header, arsenal_home)
    match = {
        "fixtureId": match_id,
        "source": "fotmob",
        "sourceUrl": (f"https://www.fotmob.com{page_url}" if page_url
                      else f"https://www.fotmob.com/match/{match_id}"),
        "date": (general.get("matchTimeUTCDate") or "")[:10],
        "competition": general.get("leagueName") or general.get("parentLeagueName"),
        "round": general.get("leagueRoundName"),
        "homeAway": "H" if arsenal_home else "A",
        "opponent": opponent.get("name"),
        "opponentId": opponent.get("id"),
        "goalsFor": scores[0],
        "goalsAgainst": scores[1],
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

    _attach_lineups(match, content, arsenal_home)
    _attach_stats(match, content, arsenal_home)
    _attach_goals(match, content, arsenal_home)
    return match


def _scores(header, arsenal_home):
    teams = header.get("teams") or []
    if len(teams) != 2:
        return None, None
    home_score, away_score = teams[0].get("score"), teams[1].get("score")
    return (home_score, away_score) if arsenal_home else (away_score, home_score)


def _result_letter(gf, ga):
    if gf is None or ga is None:
        return None
    return "W" if gf > ga else ("L" if gf < ga else "D")


def _attach_lineups(match, content, arsenal_home):
    lineup = content.get("lineup") or {}
    home, away = lineup.get("homeTeam") or {}, lineup.get("awayTeam") or {}
    ars, opp = (home, away) if arsenal_home else (away, home)

    def names(team):
        return [p.get("name") for p in (team.get("starters") or []) if isinstance(p, dict) and p.get("name")]

    def coach(team):
        c = team.get("coach")
        if isinstance(c, dict):
            return c.get("name")
        if isinstance(c, list) and c and isinstance(c[0], dict):
            return c[0].get("name")
        return c if isinstance(c, str) else None

    match["arsenalFormation"] = ars.get("formation")
    match["opponentFormation"] = opp.get("formation")
    match["arsenalCoach"] = coach(ars)
    match["opponentCoach"] = coach(opp)
    match["arsenalXI"] = names(ars)
    match["opponentXI"] = names(opp)


def _attach_stats(match, content, arsenal_home):
    """Read the full-match period only — the halves are also present."""
    periods = ((content.get("stats") or {}).get("Periods") or {})
    full = periods.get(FINISHED_PERIOD) or {}

    for group in full.get("stats") or []:
        for row in (group or {}).get("stats") or []:
            key = STAT_KEYS.get((row or {}).get("title"))
            values = (row or {}).get("stats")
            if not key or not isinstance(values, list) or len(values) != 2:
                continue
            home_val, away_val = _num(values[0]), _num(values[1])
            ars_val, opp_val = (home_val, away_val) if arsenal_home else (away_val, home_val)
            # Groups repeat titles across sections; keep the first real value.
            if ars_val is not None:
                match["stats"]["arsenal"].setdefault(key, ars_val)
            if opp_val is not None:
                match["stats"]["opponent"].setdefault(key, opp_val)


def _attach_goals(match, content, arsenal_home):
    events = ((content.get("matchFacts") or {}).get("events") or {}).get("events") or []
    for ev in events:
        if (ev or {}).get("type") != "Goal":
            continue
        is_home = ev.get("isHome")
        for_arsenal = (is_home is arsenal_home)
        match["goalscorers"].append({
            "player": (ev.get("player") or {}).get("name"),
            "minute": _num(ev.get("timeStr")),
            "team": "Arsenal" if for_arsenal else match["opponent"],
            "assist": (ev.get("assistStr") or "").replace("assist by ", "") or None,
        })
    match["goalscorers"].sort(key=lambda g: g.get("minute") or 0)


def summarize_for_prompt(match):
    """Compact rendering of the grounded data. Only these facts may be stated."""
    if not match:
        return "(no match data available)"

    labels = [
        ("Expected goals (xG)", "xg", ""),
        ("xG on target (xGOT)", "xgot", ""),
        ("Big chances", "bigChances", ""),
        ("Possession", "possession", "%"),
        ("Total shots", "shots", ""),
        ("Shots on target", "shotsOnTarget", ""),
        ("Shots inside box", "shotsInBox", ""),
        ("Touches in opposition box", "touchesInBox", ""),
        ("Passes in opposition half", "passesOppositionHalf", ""),
        ("Corners", "corners", ""),
        ("Tackles", "tackles", ""),
        ("Interceptions", "interceptions", ""),
        ("Goalkeeper saves", "saves", ""),
    ]
    rows = []
    for label, key, suffix in labels:
        ars = match["stats"]["arsenal"].get(key)
        opp = match["stats"]["opponent"].get(key)
        if ars is None and opp is None:
            continue
        fmt = lambda v: "n/a" if v is None else f"{v}{suffix}"
        rows.append(f"- {label}: Arsenal {fmt(ars)} | {match['opponent']} {fmt(opp)}")
    stats_block = "\n".join(rows) or "- (no statistics available for this fixture)"

    goals = "\n".join(
        f"- {g['minute']}' {g['player']} ({g['team']})"
        + (f", assist {g['assist']}" if g.get("assist") else "")
        for g in match["goalscorers"]
    ) or "- (none)"

    venue = "home" if match["homeAway"] == "H" else "away"
    return f"""FIXTURE: Arsenal {match['goalsFor']}-{match['goalsAgainst']} {match['opponent']} \
({venue}, {match['competition']}, {match['date']})
ARSENAL FORMATION (as recorded): {match['arsenalFormation'] or 'not reported'}
OPPONENT FORMATION (as recorded): {match['opponentFormation'] or 'not reported'}
ARSENAL STARTING XI: {', '.join(match['arsenalXI']) or 'not reported'}
ARSENAL MANAGER: {match['arsenalCoach'] or 'not reported'}
OPPONENT MANAGER: {match['opponentCoach'] or 'not reported'}

MATCH STATISTICS (full match):
{stats_block}

GOALS:
{goals}"""

DISPLAY_TZ = "America/New_York"


def _kickoff_local(utc_text):
    """(ISO date, human kickoff) in the reader's timezone."""
    if not utc_text:
        return None, None
    try:
        moment = datetime.fromisoformat(utc_text.replace("Z", "+00:00"))
    except ValueError:
        return utc_text[:10], None
    try:
        from zoneinfo import ZoneInfo
        local = moment.astimezone(ZoneInfo(DISPLAY_TZ))
    except Exception:
        local = moment  # tzdata unavailable: fall back to UTC rather than fail
    return local.strftime("%Y-%m-%d"), local.strftime("%a %b %-d, %-I:%M %p %Z")


def fetch_upcoming_fixtures(count=5):
    """The next `count` scheduled Arsenal fixtures, soonest first.

    Home/away comes from the home/away team ids, not the pageUrl slug — the slug
    ordering does not reliably match (an Arsenal-away fixture can still be
    slugged "arsenal-vs-...").
    """
    team = _get("teams", {"id": ARSENAL_TEAM_ID})
    if not team:
        return []

    fixtures = (((team.get("fixtures") or {}).get("allFixtures") or {}).get("fixtures")) or []
    now = datetime.now(timezone.utc).isoformat()
    upcoming = [
        f for f in fixtures
        if not (f.get("status") or {}).get("finished")
        and not (f.get("status") or {}).get("cancelled")
        and ((f.get("status") or {}).get("utcTime") or "") > now
    ]
    upcoming.sort(key=lambda f: (f.get("status") or {}).get("utcTime") or "")

    out = []
    for f in upcoming[:count]:
        utc_time = (f.get("status") or {}).get("utcTime")
        date, kickoff = _kickoff_local(utc_time)
        home_id = (f.get("home") or {}).get("id")
        opponent = f.get("opponent") or {}
        opponent_id = opponent.get("id")
        out.append({
            "fixtureId": f.get("id"),
            "date": date,
            "kickoffUtc": utc_time,
            "kickoffLocal": kickoff,
            "opponent": opponent.get("name"),
            "opponentId": opponent_id,
            # FotMob serves club crests off a predictable path keyed by team id.
            "crestUrl": f"{CREST_BASE}/{opponent_id}.png" if opponent_id else None,
            "competition": (f.get("tournament") or {}).get("name"),
            "homeAway": "H" if home_id == ARSENAL_TEAM_ID else "A",
        })
    if out:
        print(f"[ok] fotmob upcoming fixtures: {len(out)}")
    return out
