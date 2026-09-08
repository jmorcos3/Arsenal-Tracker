"""Fetch Arsenal news, refresh live odds, update tracker JSON, email a digest."""

import copy
import html
import json
import os
import re
import ssl
import smtplib
import sys
import traceback
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError

import feedparser
from anthropic import Anthropic

from fotmob import fetch_recent_matches, fetch_upcoming_fixtures, summarize_for_prompt
from kalshi import fetch_trophy_prices, double_item

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

FEEDS = [
    ("Arsenal.com", "https://www.arsenal.com/rss.xml"),
    ("BBC Sport", "http://feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("The Guardian", "https://www.theguardian.com/football/arsenal/rss"),
    ("Sky Sports — Transfers", "https://www.skysports.com/rss/12691"),
]

# News window. Derived from the last successful send so it always matches the
# actual cadence; the fallback covers the longest normal gap (Thu -> Tue).
DEFAULT_LOOKBACK_HOURS = 120
MIN_LOOKBACK_HOURS = 12
MAX_LOOKBACK_HOURS = 336
STATE_FILE = "state.json"
MODEL = "claude-haiku-4-5-20251001"
# The tactics write-up is the one piece of generated content a beginner can't
# sanity-check for themselves, so it gets the strongest model.
TACTICS_MODEL = "claude-opus-5"
MAX_ODDS_HISTORY = 180
MAX_TACTICS_HISTORY = 40

# Fallback only. FotMob is the primary source; if its unofficial API changes
# shape, the digest researches the match from published reports instead and
# records the URLs it used on the entry.
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 10}
MAX_RESEARCH_TURNS = 6

# Order the trophy cards appear in, on the site and in the email.
INDIVIDUAL_COMPS = ["Premier League", "Champions League", "FA Cup", "Carabao Cup"]

SITE_URL = os.environ.get("SITE_URL", "https://github.com/jmorcos3/Arsenal-Tracker")

# Marker so the workflow does not also send its "script never started" alert.
FAILURE_SENTINEL = ".failure-notified"


# ---------- news gathering ----------

def news_window():
    """(cutoff, hours) for this run, measured from the last successful digest.

    Falling back to a fixed window would either miss news over the long
    weekend gap or repeat it on the short one, depending on the constant.
    """
    now = datetime.now(timezone.utc)
    last = (load_json(STATE_FILE) or {}).get("lastDigestAt")
    if last:
        try:
            previous = datetime.fromisoformat(last)
            hours = (now - previous).total_seconds() / 3600
            if hours <= MAX_LOOKBACK_HOURS:
                # A manual re-run soon after a send gets a short window rather
                # than the full default, so it doesn't replay days of old news.
                hours = max(hours, MIN_LOOKBACK_HOURS)
                return now - timedelta(hours=hours), hours
            print(f"[info] last send was {hours:.0f}h ago, beyond the cap; using default window")
        except (TypeError, ValueError):
            print("[warn] unreadable lastDigestAt; using default window")
    return now - timedelta(hours=DEFAULT_LOOKBACK_HOURS), DEFAULT_LOOKBACK_HOURS


def record_send():
    path = DATA_DIR / STATE_FILE
    state = load_json(STATE_FILE) or {}
    state["lastDigestAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(state, indent=2) + "\n")


def gather_news():
    cutoff, hours = news_window()
    print(f"[info] news window: {hours:.0f}h")
    items = []
    for source, url in FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:25]:
                pub_struct = entry.get("published_parsed") or entry.get("updated_parsed")
                if pub_struct:
                    pub_dt = datetime(*pub_struct[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                    published = pub_dt.strftime("%Y-%m-%d %H:%M UTC")
                else:
                    published = entry.get("published") or entry.get("updated") or ""
                items.append({
                    "source": source,
                    "title": (entry.get("title") or "").strip(),
                    "link": (entry.get("link") or "").strip(),
                    "summary": (entry.get("summary") or "").strip()[:600],
                    "published": published,
                })
        except Exception as e:
            print(f"[warn] failed to fetch {source}: {e}")
    seen, unique = set(), []
    for it in items:
        k = re.sub(r"\W+", "", it["title"].lower())[:80]
        if k and k not in seen:
            seen.add(k)
            unique.append(it)
    return unique, hours


# ---------- odds ----------

def refresh_odds_file():
    """Refresh trophy prices from Kalshi.

    Kalshi needs no key, so unlike the old bookmaker feed this can't silently
    stop updating because a secret was never set. Competitions with no open
    market keep their previous value rather than blanking out.
    """
    odds_path = DATA_DIR / "odds.json"
    current = json.loads(odds_path.read_text()) if odds_path.exists() else {"items": [], "history": []}
    prev_map = {i["competition"]: i for i in current.get("items", [])}

    live = fetch_trophy_prices()
    if not live:
        print("[warn] no Kalshi prices returned; leaving odds untouched")
        return

    by_name = {i["competition"]: i for i in live}
    new_items = []
    for comp_name in INDIVIDUAL_COMPS:
        item = by_name.get(comp_name) or prev_map.get(comp_name)
        if item:
            new_items.append(item)
            if comp_name not in by_name:
                print(f"[warn] no live Kalshi market for {comp_name}; keeping previous value")

    double = double_item(new_items) or prev_map.get("Double (PL + UCL)")
    if double:
        new_items.append(double)

    snapshot = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
    for item in new_items:
        if item.get("impliedProbability") is not None:
            # Store probability, not decimal odds — it's what Kalshi actually
            # quotes, and it makes the history directly chartable.
            snapshot[item["competition"]] = item["impliedProbability"]

    history = current.get("history", [])
    if len(snapshot) > 1:
        history.append(snapshot)
        history = history[-MAX_ODDS_HISTORY:]

    odds_path.write_text(json.dumps({
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Kalshi",
        "items": new_items,
        "history": history,
    }, indent=2) + "\n")
    print(f"[ok] odds refreshed from Kalshi ({len(live)} live markets)")


def refresh_fixtures(count=5):
    """Store the next fixtures, keeping the previous list if the fetch fails."""
    path = DATA_DIR / "fixtures.json"
    fixtures = fetch_upcoming_fixtures(count)
    if not fixtures:
        print("[warn] no upcoming fixtures returned; keeping previous list")
        return (load_json("fixtures.json") or {}).get("items", [])
    path.write_text(json.dumps({
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "timezone": "America/New_York",
        "items": fixtures,
    }, indent=2) + "\n")
    return fixtures


# ---------- LLM: structured digest ----------

DIGEST_TOOL = {
    "name": "publish_digest",
    "description": "Emit the Arsenal digest as structured data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "subject_highlight": {"type": "string", "description": "3-8 word top-story summary for the email subject."},
            "additions": {
                "type": "object",
                "properties": {
                    "transfers_in": {"type": "array", "items": {"type": "object", "properties": {
                        "player": {"type": "string"}, "club": {"type": "string"},
                        "fee": {"type": "string"}, "date": {"type": "string"},
                        "sourceUrl": {"type": "string"},
                    }, "required": ["player"]}},
                    "transfers_out": {"type": "array", "items": {"type": "object", "properties": {
                        "player": {"type": "string"}, "club": {"type": "string"},
                        "fee": {"type": "string"}, "date": {"type": "string"},
                        "sourceUrl": {"type": "string"},
                    }, "required": ["player"]}},
                    "pl_transfers": {"type": "array", "items": {"type": "object", "properties": {
                        "player": {"type": "string"}, "from": {"type": "string"},
                        "to": {"type": "string"}, "fee": {"type": "string"},
                        "date": {"type": "string"}, "sourceUrl": {"type": "string"},
                    }, "required": ["player"]}},
                    "rumors": {"type": "array", "items": {"type": "object", "properties": {
                        "headline": {"type": "string"},
                        "reliability": {"type": "string", "enum": ["high", "medium", "low"]},
                        "source": {"type": "string"}, "sourceUrl": {"type": "string"},
                        "date": {"type": "string"},
                    }, "required": ["headline"]}},
                },
            },
            "narrative": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "1-2 sentence overview of the past 3 days."},
                    "odds_commentary": {"type": "string", "description": "Optional short note on odds movement."},
                    "squad_news": {"type": "array", "items": {"type": "object", "properties": {
                        "text": {"type": "string"}, "url": {"type": "string"},
                    }, "required": ["text"]}},
                    "around_pl_notes": {"type": "array", "items": {"type": "object", "properties": {
                        "text": {"type": "string"}, "url": {"type": "string"},
                    }, "required": ["text"]}},
                    "fixtures": {"type": "array", "items": {"type": "object", "properties": {
                        "text": {"type": "string"}, "url": {"type": "string"},
                    }, "required": ["text"]}},
                },
                "required": ["summary"],
            },
        },
        "required": ["subject_highlight", "additions", "narrative"],
    },
}


def load_json(name):
    p = DATA_DIR / name
    return json.loads(p.read_text()) if p.exists() else {}


def generate_digest(items, odds_snapshot, lookback_hours=DEFAULT_LOOKBACK_HOURS):
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    articles_block = "\n\n".join(
        f"[{i['source']} · {i['published']}] {i['title']}\n"
        f"URL: {i['link']}\n"
        f"Summary: {i['summary']}"
        for i in items
    ) or "(no articles found in lookback window)"

    tracker_state = {
        "transfers": load_json("transfers.json"),
        "pl_transfers": load_json("pl-transfers.json"),
        "rumors": load_json("rumors.json"),
    }
    odds_lines = "\n".join(
        f"- {i['competition']}: {i.get('odds')} ({i.get('bestBookmaker') or 'n/a'})"
        for i in odds_snapshot
    )

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    prompt = f"""You are producing structured content for an Arsenal FC news digest.

TODAY: {today}
LOOKBACK: last {lookback_hours:.0f} hours

CURRENT LIVE ODDS (Arsenal to win each trophy):
{odds_lines}

ALREADY-TRACKED (do not re-add; only add genuinely new items):
{json.dumps(tracker_state, indent=2)[:4000]}

RECENT ARTICLES:
{articles_block}

Call the `publish_digest` tool. Rules:

1. `subject_highlight` — the biggest single story of the last 3 days in 3-8 words. Neutral phrasing.

2. `additions` — ONLY new items (not in ALREADY-TRACKED). Include a `sourceUrl` on each item when a URL was provided. Rumor reliability:
   - high: David Ornstein, Fabrizio Romano, BBC, The Athletic
   - medium: Sky Sports, The Guardian, The Telegraph
   - low: tabloids, unnamed sources, aggregators

3. `narrative` — extra content for the email body that doesn't fit as tracker rows:
   - `summary`: 1-2 sentence overview.
   - `odds_commentary`: optional 1-liner if odds moved notably.
   - `squad_news`: bullets on injuries, contract extensions, returns, tactical notes. Each with `text` and `url` if from a specific article.
   - `around_pl_notes`: bullets on rival club news beyond confirmed transfers (manager changes, contract news, etc.).
   - `fixtures`: bullets on upcoming/notable fixtures mentioned.

Do NOT invent facts. If a category has nothing new, leave the array empty (or omit).
"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        tools=[DIGEST_TOOL],
        tool_choice={"type": "tool", "name": "publish_digest"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "publish_digest":
            return block.input
    raise RuntimeError("LLM did not return a publish_digest tool call")


# ---------- LLM: tactical breakdown ----------

TACTICS_TOOL = {
    "name": "publish_tactics",
    "description": "Emit a beginner-friendly tactical breakdown of one Arsenal match.",
    "input_schema": {
        "type": "object",
        "properties": {
            "matchFound": {
                "type": "boolean",
                "description": "False if you could not confirm a completed Arsenal match to write up. If false, set it and stop — do not invent one.",
            },
            "match": {
                "type": "object",
                "description": "The verified facts. Every field here must be supported by a source you actually retrieved.",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD."},
                    "opponent": {"type": "string"},
                    "competition": {"type": "string"},
                    "homeAway": {"type": "string", "enum": ["H", "A"]},
                    "goalsFor": {"type": "integer", "description": "Arsenal's goals."},
                    "goalsAgainst": {"type": "integer"},
                    "arsenalFormation": {
                        "type": "string",
                        "description": "Starting formation as reported, e.g. '4-3-3'. Omit entirely if no source states it — do not guess from the XI.",
                    },
                    "opponentFormation": {"type": "string", "description": "Omit if unreported."},
                    "arsenalXI": {"type": "array", "items": {"type": "string"}},
                    "goalscorers": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "player": {"type": "string"},
                                "minute": {"type": "integer"},
                                "team": {"type": "string"},
                            },
                            "required": ["player", "team"],
                        },
                    },
                    "stats": {
                        "type": "array",
                        "description": "Reported match statistics. Include only figures you actually found (xG, possession, shots, shots on target, corners, touches in box).",
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string"},
                                "arsenal": {"type": "string"},
                                "opponent": {"type": "string"},
                            },
                            "required": ["label", "arsenal", "opponent"],
                        },
                    },
                    "sources": {
                        "type": "array",
                        "description": "URLs you actually retrieved and drew facts from. At least one. Never cite a URL you did not open.",
                        "items": {"type": "string"},
                    },
                },
                "required": ["date", "opponent", "competition", "homeAway",
                             "goalsFor", "goalsAgainst", "sources"],
            },
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high = score, formations and stats all confirmed by reliable sources; medium = score confirmed, some detail missing; low = only the basics confirmed.",
            },
            "whatHappened": {
                "type": "string",
                "description": "2-3 sentences on how the match actually played out, in plain English. No jargon at all — this is the on-ramp.",
            },
            "shape": {
                "type": "object",
                "properties": {
                    "arsenalInPossession": {
                        "type": "string",
                        "description": "The shape Arsenal TYPICALLY take with the ball given the listed formation and personnel, e.g. '3-2-5'. This is a known pattern, not a measurement of this match.",
                    },
                    "arsenalOutOfPossession": {
                        "type": "string",
                        "description": "The shape Arsenal typically defend in, e.g. '4-4-2 mid block'.",
                    },
                    "plainEnglish": {
                        "type": "string",
                        "description": "2-3 sentences explaining WHY the shape changes between those two, and which player's movement causes it. Written for someone who has never heard the term.",
                    },
                },
                "required": ["plainEnglish"],
            },
            "opponentPlan": {
                "type": "string",
                "description": "What the opposition were trying to do, and how Arsenal's setup answered it. Ground this in the listed opponent formation and the match statistics.",
            },
            "keyMoment": {
                "type": "string",
                "description": "The tactical turning point — a substitution, a shape change, or a goal that shifted the pattern. Reference a real minute or goal from the grounded data.",
            },
            "lesson": {
                "type": "object",
                "description": "The ONE concept to teach from this match. Must be a concept not already taught.",
                "properties": {
                    "conceptId": {"type": "string", "description": "id from the glossary, e.g. 'half-space'."},
                    "term": {"type": "string"},
                    "level": {"type": "integer", "description": "1, 2 or 3."},
                    "explain": {
                        "type": "string",
                        "description": "3-4 sentences teaching the concept THROUGH what happened in this specific match. Concrete, not abstract.",
                    },
                    "spotIt": {
                        "type": "string",
                        "description": "One sentence: exactly what to watch for on screen in the next match to see this concept live.",
                    },
                },
                "required": ["conceptId", "term", "level", "explain", "spotIt"],
            },
            "statTranslations": {
                "type": "array",
                "description": "Translate the most revealing 3-5 numbers into plain meaning. Only use numbers present in the grounded data.",
                "items": {
                    "type": "object",
                    "properties": {
                        "stat": {"type": "string", "description": "e.g. 'xG 2.31 vs 0.84'"},
                        "plain": {"type": "string", "description": "What that actually tells you, in one sentence."},
                    },
                    "required": ["stat", "plain"],
                },
            },
            "nerdCorner": {
                "type": "string",
                "description": "One genuinely sharp observation for an obsessive fan — a pattern, a trade-off, a regression argument, or something the scoreline hides. Assume they already know the basics.",
            },
        },
        "required": ["matchFound"],
    },
}


def _concept_menu(concepts_taught):
    """Eligible lessons plus the level cap, so the curriculum ramps."""
    glossary = load_json("glossary.json").get("items", [])
    taught = set(concepts_taught or [])
    available = [g for g in glossary if g["id"] not in taught]
    if not available:
        # Curriculum complete — allow revisiting, deepest concepts first.
        available = sorted(glossary, key=lambda g: -g["level"])

    max_level = 1 if len(taught) < 6 else (2 if len(taught) < 12 else 3)
    eligible = [g for g in available if g["level"] <= max_level] or available
    menu = "\n".join(f"- {g['id']} (level {g['level']}) — {g['term']}: {g['short']}" for g in eligible)
    return taught, menu


READER_BRIEF = """THE READER: a passionate Arsenal fan who watches every match but has never been taught
how to read tactics. They are smart and want to learn properly — they are not stupid,
they just lack the vocabulary. Never condescend. Never pad. Explain like a good coach
talking to an interested adult."""


def research_and_generate_tactics(concepts_taught, known_keys):
    """Research Arsenal's most recent match on the web, then write it up.

    Used because no free match-data API covers the current season. Claude does the
    searching server-side and must cite the pages it actually opened, so a beginner
    who can't fact-check the analysis can at least follow it back to a source.
    """
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    taught, menu = _concept_menu(concepts_taught)
    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    already = "\n".join(f"- {k}" for k in sorted(known_keys)) or "(none yet)"

    prompt = f"""You are writing the tactical education section of an Arsenal FC digest.

TODAY: {today}

{READER_BRIEF}

YOUR JOB, in order:

1. Use web search to find Arsenal's most recently COMPLETED first-team match as of today.
   Confirm the competition, the date, the final score, and who scored.
2. Search again for that specific match's team news and statistics — the starting
   formations, the starting XI, and whatever match statistics were published
   (xG, possession, shots, shots on target, corners, touches in the box).
3. Search once more for tactical analysis of that match if any exists.
4. Then call `publish_tactics` with what you actually verified.

ALREADY WRITTEN UP (if the most recent match is one of these, set matchFound=false —
do not write it up twice):
{already}

CONCEPTS ALREADY TAUGHT (do not repeat these): {', '.join(sorted(taught)) or '(none yet — this is lesson 1)'}

CONCEPTS AVAILABLE TO TEACH THIS TIME (pick exactly one, the one this match illustrates best):
{menu}

HARD RULES — a wrong claim here is worse than no claim, because the reader cannot catch it:

1. Every score, minute, goalscorer, formation, player name and statistic must come from
   a page you actually retrieved. Do not fill gaps from memory or from what seems likely.
2. `match.sources` must list the URLs you genuinely opened and used. Never cite a page
   you did not read.
3. If a formation is not reported anywhere you found, omit the field. Do not reconstruct
   it from the starting XI — a list of names does not tell you the shape.
4. If a statistic is absent, do not mention it and do not estimate it. xG, PPDA and field
   tilt are often unpublished; say nothing rather than invent a number.
5. The `shape` fields describe how this formation and these players TYPICALLY behave. Word
   them as general patterns ("Arsenal usually...", "this shape tends to..."), never as a
   measured claim about this specific match, because nobody measured it.
6. Do not claim to know what was said at half-time, what the manager intended, or what a
   player was thinking. Stick to what the shape and the numbers support.
7. Set `confidence` honestly. If you only confirmed the score, that is "low" — say so
   rather than dressing up thin sourcing.
8. If you cannot confirm a completed match at all, call `publish_tactics` with
   matchFound=false and nothing else. Never invent a fixture."""

    messages = [{"role": "user", "content": prompt}]
    tools = [WEB_SEARCH_TOOL, TACTICS_TOOL]

    for turn in range(MAX_RESEARCH_TURNS):
        resp = client.messages.create(
            model=TACTICS_MODEL,
            max_tokens=16000,
            tools=tools,
            messages=messages,
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "publish_tactics":
                return block.input

        # Server tools run in their own loop; `pause_turn` means it hit the
        # iteration cap mid-research and needs resuming with no extra prompting.
        if resp.stop_reason != "pause_turn":
            print(f"[warn] research ended with stop_reason={resp.stop_reason} and no tactics")
            return None
        messages.append({"role": "assistant", "content": resp.content})
        print(f"[info] research paused at turn {turn + 1}; resuming")

    print("[warn] research did not converge within turn limit")
    return None



def _strictify(node):
    """Recursively set additionalProperties:false, as strict tool use requires."""
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
        for value in node.values():
            _strictify(value)
    elif isinstance(node, list):
        for value in node:
            _strictify(value)
    return node


def _build_grounded_tool():
    """Schema for the FotMob path, where the match facts are already verified.

    The research schema lets the model decline via `matchFound` and requires
    nothing else — correct when it has to find the match itself, but wrong here.
    Under strict validation that made a bare {"matchFound": true} a *valid*
    response, which is exactly what silently dropped Coventry. On this path the
    model is handed the data, so every content field is mandatory.
    """
    tool = copy.deepcopy(TACTICS_TOOL)
    schema = tool["input_schema"]
    for research_only in ("matchFound", "match", "confidence", "sources"):
        schema["properties"].pop(research_only, None)
    schema["required"] = list(schema["properties"].keys())

    lesson = schema["properties"].get("lesson", {})
    if "properties" in lesson:
        lesson["required"] = list(lesson["properties"].keys())
    shape = schema["properties"].get("shape", {})
    if "properties" in shape:
        shape["required"] = list(shape["properties"].keys())

    tool["description"] = (
        "Emit a beginner-friendly tactical breakdown of the Arsenal match whose "
        "verified data is given in the prompt. Every field is required."
    )
    return dict(_strictify(tool), strict=True)


GROUNDED_TACTICS_TOOL = _build_grounded_tool()


def generate_tactics(match, concepts_taught):
    """Explain a match. Every fact must come from `match`; the model adds only interpretation."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    taught, concept_menu = _concept_menu(concepts_taught)

    prompt = f"""You are writing the tactical education section of an Arsenal FC digest.

{READER_BRIEF}

Set matchFound=true. You do not need to fill the `match` object — the facts below are
already recorded from a structured data source.

GROUNDED MATCH DATA — this is the complete set of facts you may state:
{summarize_for_prompt(match)}

CONCEPTS ALREADY TAUGHT (do not repeat these): {', '.join(sorted(taught)) or '(none yet — this is lesson 1)'}

CONCEPTS AVAILABLE TO TEACH THIS TIME (pick exactly one, the one this match illustrates best):
{concept_menu}

HARD RULES — a wrong claim here is worse than no claim, because the reader cannot catch it:

1. Every score, minute, goalscorer, formation, player name and statistic you state must
   appear verbatim in GROUNDED MATCH DATA above. Do not infer, round differently, or embellish.
2. If a statistic is absent above, do not mention it, and do not guess at it. In particular
   xG, PPDA and field tilt are often unavailable — say nothing rather than invent a number.
3. The `shape` fields describe how this formation and these players TYPICALLY behave. Word
   them as general patterns ("Arsenal usually...", "this shape tends to..."), never as a
   measured claim about this specific match, because nobody measured it.
4. Do not claim to know what was said at half-time, what the manager intended, or what a
   player was thinking. Stick to what the shape and numbers support.
5. `keyMoment` must reference something real from the data — a listed goal and its minute,
   or the final scoreline pattern. If nothing in the data supports a turning point, describe
   the match's overall pattern instead.

Call the `publish_tactics` tool."""

    def call(tool):
        resp = client.messages.create(
            model=TACTICS_MODEL,
            max_tokens=3000,
            tools=[tool],
            tool_choice={"type": "tool", "name": "publish_tactics"},
            messages=[{"role": "user", "content": prompt}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and block.name == "publish_tactics":
                return block.input
        raise RuntimeError("LLM did not return a publish_tactics tool call")

    # Strict tool use validates the input against the schema server-side, which
    # is what stops `lesson` coming back as a bare string. If the schema is
    # rejected for any reason, fall back rather than lose the write-up entirely.
    try:
        return call(GROUNDED_TACTICS_TOOL)
    except Exception as e:
        if "strict" not in str(e).lower() and "schema" not in str(e).lower():
            raise
        print(f"[warn] strict tool use rejected ({e}); retrying without it")
        return call(TACTICS_TOOL)


def latest_tactics_entry(max_age_days=10):
    """Most recent stored breakdown, if it's still topical.

    Covers the run where nothing new was played but the last match hasn't been
    emailed yet — without re-sending an old write-up through an international
    break.
    """
    matches = (load_json("tactics.json") or {}).get("matches") or []
    if not matches:
        return None
    # Pick by date rather than list position, so "most recent" can never depend
    # on the file happening to be sorted correctly.
    entry = max(matches, key=lambda m: (m.get("date") or ""))
    try:
        played = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return entry if (datetime.now(timezone.utc) - played).days <= max_age_days else None


def _normalize_explained(explained):
    """Coerce model output into the shape the renderer expects.

    Tool inputs aren't schema-validated unless the tool is declared strict, so a
    field documented as an object can come back as a bare string. Rather than let
    that crash the run — or worse, persist and crash every later render — drop
    anything malformed and keep what's usable.
    """
    if not isinstance(explained, dict):
        return {}

    clean = {}
    for key in ("whatHappened", "opponentPlan", "keyMoment", "nerdCorner"):
        value = explained.get(key)
        if isinstance(value, str) and value.strip():
            clean[key] = value

    shape = explained.get("shape")
    if isinstance(shape, dict):
        clean["shape"] = {k: v for k, v in shape.items() if isinstance(v, str)}

    lesson = explained.get("lesson")
    if isinstance(lesson, dict) and isinstance(lesson.get("conceptId"), str):
        clean["lesson"] = lesson
    elif lesson is not None:
        print(f"[warn] discarding malformed lesson ({type(lesson).__name__})")

    rows = explained.get("statTranslations")
    if isinstance(rows, list):
        clean["statTranslations"] = [
            r for r in rows
            if isinstance(r, dict) and isinstance(r.get("stat"), str) and isinstance(r.get("plain"), str)
        ]

    return clean


def match_key(date, opponent):
    """Stable identity for a fixture across both data paths.

    The API path has a fixtureId; researched matches don't, so date + opponent
    is the key both can produce.
    """
    return f"{(date or '')[:10]}|{_norm(opponent)}"


def _entry_keys(entries):
    keys = set()
    for e in entries:
        g = e.get("grounded") or {}
        keys.add(match_key(g.get("date"), g.get("opponent")))
    return keys


def _research_entry(current):
    """Build one tactics entry by web research, or None if nothing new/verifiable."""
    known = _entry_keys(current["matches"])
    try:
        result = research_and_generate_tactics(current["conceptsTaught"], known)
    except Exception as e:
        print(f"[warn] tactics research failed: {e}")
        return None

    if not result or not result.get("matchFound"):
        print("[info] research found no new completed match to write up")
        return None

    match = result.get("match") or {}
    if not match.get("sources"):
        # Unsourced facts are exactly what this feature exists to avoid.
        print("[warn] research returned no sources; discarding")
        return None

    key = match_key(match.get("date"), match.get("opponent"))
    if key in known:
        print(f"[info] research returned an already-recorded match ({key})")
        return None

    explained = _normalize_explained(
        {k: v for k, v in result.items() if k not in {"matchFound", "match", "confidence"}}
    )
    if not explained.get("lesson"):
        print("[warn] research returned no usable lesson; discarding")
        return None
    concept_id = explained["lesson"].get("conceptId")
    if concept_id and concept_id not in current["conceptsTaught"]:
        current["conceptsTaught"].append(concept_id)

    print(f"[ok] tactics researched for {match.get('opponent')} "
          f"({concept_id}, confidence={result.get('confidence')}, "
          f"{len(match['sources'])} sources)")

    return {
        "date": (match.get("date") or "")[:10],
        "researched": True,
        "confidence": result.get("confidence"),
        "grounded": match,
        "explained": explained,
    }


def ensure_latest_match(matches, entry):
    """Guarantee the digest leads with the most recent match actually played.

    If the newest fixture has no write-up — generation failed, or it finished
    between runs — fall back to a result-only entry for *that* match rather than
    to an older one. Repeating a match across digests is fine; showing last
    week's game when a newer one exists is not.
    """
    if not matches:
        return entry
    newest = matches[-1]
    if entry and entry.get("fixtureId") == newest.get("fixtureId"):
        return entry
    print(f"[warn] newest match ({newest.get('opponent')} on {newest.get('date')}) has no "
          f"write-up; showing it result-only rather than an older match")
    return {
        "fixtureId": newest.get("fixtureId"),
        "date": newest.get("date"),
        "grounded": newest,
        "explained": {},
        "partial": True,
    }


def refresh_tactics(matches):
    """Write up every supplied fixture that isn't already covered.

    Matches arrive oldest-first so lessons are taught in the order they were
    played. Falls back to web research when no structured data is available,
    which is the normal path on the free API tiers. Returns the newest entry
    for the email, or None.
    """
    path = DATA_DIR / "tactics.json"
    current = load_json("tactics.json") or {}
    current.setdefault("matches", [])
    current.setdefault("conceptsTaught", [])

    newest = None
    wrote = False

    if not matches:
        entry = _research_entry(current)
        if entry:
            current["matches"].insert(0, entry)
            newest, wrote = entry, True

    for match in matches or []:
        if not match or not match.get("fixtureId"):
            continue

        existing = next((m for m in current["matches"] if m.get("fixtureId") == match["fixtureId"]), None)
        if existing:
            newest = existing
            continue

        explained = {}
        for attempt in (1, 2):
            try:
                explained = _normalize_explained(generate_tactics(match, current["conceptsTaught"]))
            except Exception as e:
                # One bad fixture shouldn't cost us the rest of the backlog.
                print(f"[warn] tactics generation failed for {match.get('opponent')} "
                      f"(attempt {attempt}): {e}")
                explained = {}
            if explained.get("whatHappened"):
                break
            if attempt == 1:
                print(f"[info] retrying {match.get('opponent')}")
        if not explained.get("whatHappened"):
            # Nothing usable at all — a lesson alone is not worth an entry.
            print(f"[warn] no usable write-up for {match.get('opponent')}; skipping "
                  f"(model returned keys: {sorted(explained.keys()) or 'none'})")
            continue
        if not explained.get("lesson"):
            # Keep the breakdown; only the teaching slot is lost. Discarding the
            # whole match here is what silently dropped Coventry from the Aug 22
            # digest.
            print(f"[warn] no usable lesson for {match.get('opponent')}; storing write-up without one")

        entry = {
            "fixtureId": match["fixtureId"],
            "date": match["date"],
            "grounded": match,
            "explained": explained,
        }
        current["matches"].insert(0, entry)
        newest = entry
        wrote = True

        concept_id = (explained.get("lesson") or {}).get("conceptId")
        if concept_id and concept_id not in current["conceptsTaught"]:
            current["conceptsTaught"].append(concept_id)
        print(f"[ok] tactics recorded for {match['opponent']} ({concept_id})")

    if wrote:
        current["matches"].sort(key=lambda m: m.get("date") or "", reverse=True)
        current["matches"] = current["matches"][:MAX_TACTICS_HISTORY]
        current["lastUpdated"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        path.write_text(json.dumps(current, indent=2) + "\n")

    return newest


# ---------- JSON merge / persist ----------

def _norm(s):
    return re.sub(r"\W+", "", (s or "").lower())


def _merge(existing_list, additions, key_fn):
    seen = {key_fn(x) for x in existing_list}
    added = 0
    for a in additions or []:
        k = key_fn(a)
        if k and k not in seen:
            existing_list.append(a)
            seen.add(k)
            added += 1
    return added


def apply_additions(additions):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    transfers = load_json("transfers.json") or {"window": "Summer 2026", "in": [], "out": []}
    transfers.setdefault("in", [])
    transfers.setdefault("out", [])
    _merge(transfers["in"], additions.get("transfers_in"), lambda t: _norm(t.get("player")))
    _merge(transfers["out"], additions.get("transfers_out"), lambda t: _norm(t.get("player")))
    transfers["lastUpdated"] = today
    (DATA_DIR / "transfers.json").write_text(json.dumps(transfers, indent=2) + "\n")

    pl = load_json("pl-transfers.json") or {"window": "Summer 2026", "items": []}
    pl.setdefault("items", [])
    _merge(pl["items"], additions.get("pl_transfers"),
           lambda t: _norm(t.get("player")) + "|" + _norm(t.get("to")))
    pl["lastUpdated"] = today
    (DATA_DIR / "pl-transfers.json").write_text(json.dumps(pl, indent=2) + "\n")

    rumors = load_json("rumors.json") or {"items": []}
    rumors.setdefault("items", [])
    _merge(rumors["items"], additions.get("rumors"),
           lambda r: _norm(r.get("headline"))[:80])
    rumors["lastUpdated"] = today
    (DATA_DIR / "rumors.json").write_text(json.dumps(rumors, indent=2) + "\n")

    print("[ok] additions applied")


# ---------- email HTML rendering ----------

E = html.escape

# Colors reused across the template
RED = "#EF0107"
RED_DARK = "#B8000D"
GOLD = "#DB9E00"
INK = "#101418"
INK_SOFT = "#4a5560"
BORDER = "#e3e6ea"
BG = "#f6f7f9"
CARD_ALT = "#fafbfc"

REL_STYLES = {
    "high":   ("#d4edda", "#155724", "High"),
    "medium": ("#fff3cd", "#856404", "Medium"),
    "low":    ("#f8d7da", "#721c24", "Low"),
}


def _reliability_badge(reliability):
    bg, fg, label = REL_STYLES.get(reliability, REL_STYLES["medium"])
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};'
        f'padding:2px 8px;border-radius:999px;font-size:10px;font-weight:700;'
        f'text-transform:uppercase;letter-spacing:.05em;margin-left:6px;'
        f'vertical-align:middle;">{E(label)}</span>'
    )


def _source_link(url, text):
    if not url:
        return E(text)
    return f'<a href="{E(url)}" style="color:{RED_DARK};text-decoration:none;">{E(text)}</a>'


def _section_header(title):
    return (
        f'<tr><td style="padding:18px 28px 4px;">'
        f'<h2 style="margin:0;color:{RED_DARK};font-size:16px;'
        f'border-left:4px solid {GOLD};padding:2px 0 2px 10px;'
        f'text-transform:uppercase;letter-spacing:.03em;">{E(title)}</h2>'
        f'</td></tr>'
    )


def _section_body(inner_html):
    return f'<tr><td style="padding:6px 28px 14px;font-size:14px;line-height:1.55;color:{INK};">{inner_html}</td></tr>'


def _empty():
    return f'<p style="margin:6px 0;color:{INK_SOFT};font-style:italic;">Nothing to report.</p>'


def _bullet_list(items_html):
    lis = "".join(f'<li style="margin:4px 0;">{h}</li>' for h in items_html)
    return f'<ul style="margin:4px 0 0;padding:0 0 0 20px;">{lis}</ul>'


def render_header(today_str):
    return (
        f'<tr><td style="background:linear-gradient(135deg,{RED},{RED_DARK});'
        f'padding:24px 28px;border-bottom:4px solid {GOLD};">'
        f'<h1 style="margin:0;color:#ffffff;font-size:24px;font-weight:700;letter-spacing:-0.01em;">'
        f'Arsenal Digest</h1>'
        f'<p style="margin:6px 0 0;color:rgba(255,255,255,0.9);font-size:13px;">'
        f'{E(today_str)} · since the last digest</p>'
        f'</td></tr>'
    )


def render_intro(summary):
    if not summary:
        return ""
    return (
        f'<tr><td style="padding:18px 28px 4px;font-size:15px;line-height:1.5;color:{INK};">'
        f'{E(summary)}</td></tr>'
    )


def render_odds(odds_items, commentary):
    by_name = {i["competition"]: i for i in odds_items}

    def cell(comp):
        item = by_name.get(comp)
        # Kalshi quotes probability directly, so that leads; decimal odds follow
        # for anyone used to reading a bookmaker price.
        prob = "—" if not item or item.get("impliedProbability") is None else f'{item["impliedProbability"] * 100:.1f}%'
        odds = "" if not item or item.get("odds") is None else f'{item["odds"]:.2f} decimal'
        spread = ""
        if item and item.get("bidCents") is not None and item.get("askCents") is not None:
            spread = f'{item["bidCents"]}–{item["askCents"]}\u00a2 bid/ask'
        return (
            f'<td width="25%" valign="top" style="background:{CARD_ALT};border:1px solid {BORDER};'
            f'border-radius:6px;padding:12px 6px;text-align:center;">'
            f'<div style="font-size:10px;color:{INK_SOFT};text-transform:uppercase;letter-spacing:.05em;font-weight:600;">{E(comp)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{RED_DARK};margin:6px 0 2px;">{E(prob)}</div>'
            f'<div style="font-size:11px;color:{INK_SOFT};">{E(odds)}</div>'
            f'<div style="font-size:10px;color:{INK_SOFT};margin-top:2px;">{E(spread)}</div>'
            f'</td>'
        )

    grid = (
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="border-collapse:separate;border-spacing:6px;">'
        f'<tr>{cell("Premier League")}{cell("Champions League")}{cell("FA Cup")}{cell("Carabao Cup")}</tr>'
        f'</table>'
    )

    double = by_name.get("Double (PL + UCL)")
    double_row = ""
    if double and double.get("odds") is not None:
        prob = f'{double["impliedProbability"] * 100:.1f}%' if double.get("impliedProbability") is not None else ""
        double_row = (
            f'<div style="margin-top:10px;padding:8px 12px;background:{CARD_ALT};'
            f'border:1px solid {BORDER};border-radius:6px;text-align:center;font-size:13px;color:{INK_SOFT};">'
            f'Double (PL + UCL): <strong style="color:{RED_DARK};font-size:16px;">{E(prob)}</strong>'
            f' · {double["odds"]:.2f} decimal'
            f'</div>'
        )

    commentary_html = f'<p style="margin:8px 0 0;color:{INK_SOFT};font-size:13px;font-style:italic;">{E(commentary)}</p>' if commentary else ""
    attribution = (
        f'<p style="margin:8px 0 0;color:{INK_SOFT};font-size:11px;text-align:center;">'
        f'Live prices from <a href="https://kalshi.com" style="color:{RED_DARK};text-decoration:none;">Kalshi</a>'
        f' — contracts settle at $1, so the price is the market\u2019s implied probability.</p>'
    )

    return _section_header("Trophy Odds") + _section_body(grid + double_row + commentary_html + attribution)


def _transfer_line(t, direction):
    name = t.get("player") or ""
    club = t.get("club") or ""
    fee = t.get("fee") or ""
    date = t.get("date") or ""
    arrow = "←" if direction == "in" else "→"
    parts = []
    if club:
        parts.append(f'{arrow} {E(club)}')
    if fee:
        parts.append(E(fee))
    if date:
        parts.append(f'<span style="color:{INK_SOFT};">{E(date)}</span>')
    meta = " · ".join(parts)
    body = f'<strong>{E(name)}</strong>' + (f' {meta}' if meta else "")
    if t.get("sourceUrl"):
        body += f' <a href="{E(t["sourceUrl"])}" style="color:{RED_DARK};text-decoration:none;font-size:12px;">[source]</a>'
    return body


def render_transfers_in(items):
    if not items:
        return _section_header("Transfers — In") + _section_body(_empty())
    lines = [_transfer_line(t, "in") for t in items]
    return _section_header("Transfers — In") + _section_body(_bullet_list(lines))


def render_transfers_out(items):
    if not items:
        return _section_header("Transfers — Out") + _section_body(_empty())
    lines = [_transfer_line(t, "out") for t in items]
    return _section_header("Transfers — Out") + _section_body(_bullet_list(lines))


def render_rumors(items):
    if not items:
        return _section_header("Rumors") + _section_body(_empty())
    lines = []
    for r in items:
        badge = _reliability_badge(r.get("reliability", "medium"))
        head = f'<strong>{E(r.get("headline") or "")}</strong>{badge}'
        meta_bits = []
        if r.get("source"):
            meta_bits.append(_source_link(r.get("sourceUrl"), r.get("source")))
        if r.get("date"):
            meta_bits.append(E(r["date"]))
        meta = f'<div style="font-size:12px;color:{INK_SOFT};margin-top:2px;">{" · ".join(meta_bits)}</div>' if meta_bits else ""
        lines.append(head + meta)
    return _section_header("Rumors") + _section_body(_bullet_list(lines))


def _note_line(note):
    text = E(note.get("text") or "")
    if note.get("url"):
        text += f' <a href="{E(note["url"])}" style="color:{RED_DARK};text-decoration:none;font-size:12px;">[source]</a>'
    return text


def render_squad(notes):
    if not notes:
        return _section_header("Squad News") + _section_body(_empty())
    return _section_header("Squad News") + _section_body(_bullet_list([_note_line(n) for n in notes]))


def render_pl(transfers, notes):
    lines = []
    for t in transfers or []:
        name = t.get("player") or ""
        move = f'{E(t.get("from") or "?")} → {E(t.get("to") or "?")}'
        fee = f' · {E(t.get("fee"))}' if t.get("fee") else ""
        date = f' <span style="color:{INK_SOFT};">{E(t.get("date"))}</span>' if t.get("date") else ""
        src = f' <a href="{E(t["sourceUrl"])}" style="color:{RED_DARK};text-decoration:none;font-size:12px;">[source]</a>' if t.get("sourceUrl") else ""
        lines.append(f'<strong>{E(name)}</strong> {move}{fee}{date}{src}')
    for n in notes or []:
        lines.append(_note_line(n))
    if not lines:
        return _section_header("Around the Premier League") + _section_body(_empty())
    return _section_header("Around the Premier League") + _section_body(_bullet_list(lines))


COMP_COLORS = {
    "premier league": ("#eef4ff", "#1c4f82"),
    "champions league": ("#f3e8ff", "#5b21b6"),
    "efl cup": ("#e8f6ee", "#1c6b3f"),
    "carabao cup": ("#e8f6ee", "#1c6b3f"),
    "fa cup": ("#fff3cd", "#7a5a00"),
}


def _comp_badge(name):
    bg, fg = COMP_COLORS.get((name or "").lower(), (CARD_ALT, INK_SOFT))
    return (
        f'<span style="display:inline-block;background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:999px;font-size:10px;font-weight:700;white-space:nowrap;">{E(name or "")}</span>'
    )


def render_fixtures(fixtures, notes=None):
    """Next fixtures as a table with club crests, dates and competitions.

    Crests are hotlinked, so every row still reads correctly on the alt text
    alone if a client blocks remote images.
    """
    body = ""
    if fixtures:
        rows = ""
        for f in fixtures:
            home = f.get("homeAway") == "H"
            crest = (
                f'<img src="{E(f["crestUrl"])}" width="28" height="28" alt="{E(f.get("opponent") or "")}" '
                f'style="display:block;width:28px;height:28px;border:0;outline:none;'
                f'text-decoration:none;" />'
            ) if f.get("crestUrl") else ""
            rows += (
                f'<tr>'
                f'<td width="40" valign="middle" style="padding:10px 10px 10px 0;'
                f'border-bottom:1px solid {BORDER};">{crest}</td>'
                f'<td valign="middle" style="padding:10px 10px 10px 0;border-bottom:1px solid {BORDER};">'
                f'<div style="font-size:15px;font-weight:700;color:{INK};line-height:1.3;">'
                f'{E(f.get("opponent") or "?")}</div>'
                f'<div style="font-size:11px;font-weight:700;color:{RED_DARK if home else INK_SOFT};'
                f'text-transform:uppercase;letter-spacing:.05em;margin-top:2px;">'
                f'{"Home" if home else "Away"}</div>'
                f'</td>'
                f'<td valign="middle" style="padding:10px 10px 10px 0;border-bottom:1px solid {BORDER};">'
                f'{_comp_badge(f.get("competition"))}</td>'
                f'<td valign="middle" align="right" style="padding:10px 0;border-bottom:1px solid {BORDER};'
                f'font-size:13px;color:{INK_SOFT};white-space:nowrap;">'
                f'{E(f.get("kickoffLocal") or f.get("date") or "")}</td>'
                f'</tr>'
            )
        body += (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:collapse;">{rows}</table>'
            f'<p style="margin:10px 0 0;font-size:11px;color:{INK_SOFT};">Kickoff times in ET.</p>'
        )
    if notes:
        body += _bullet_list([_note_line(n) for n in notes])
    return _section_header("Next 5 Fixtures") + _section_body(body or _empty())


def _tactics_callout(label, body, accent):
    return (
        f'<div style="margin:10px 0;padding:10px 14px;background:{CARD_ALT};'
        f'border-left:3px solid {accent};border-radius:0 6px 6px 0;">'
        f'<div style="font-size:10px;font-weight:700;color:{INK_SOFT};text-transform:uppercase;'
        f'letter-spacing:.06em;margin-bottom:3px;">{E(label)}</div>'
        f'<div style="font-size:14px;line-height:1.55;color:{INK};">{body}</div>'
        f'</div>'
    )


def render_tactics(entry, lesson_number):
    if not entry:
        return ""

    g = entry.get("grounded") or {}
    x = entry.get("explained") or {}

    score = f'{g.get("goalsFor")}–{g.get("goalsAgainst")}'
    venue = "vs" if g.get("homeAway") == "H" else "away at"
    header_line = (
        f'<div style="font-size:15px;font-weight:700;color:{INK};margin-bottom:2px;">'
        f'Arsenal {E(score)} {E(venue)} {E(g.get("opponent") or "?")}</div>'
        f'<div style="font-size:12px;color:{INK_SOFT};margin-bottom:10px;">'
        f'{E(g.get("competition") or "")} · {E(g.get("date") or "")}</div>'
    )

    body = header_line
    if entry.get("partial") or not x.get("whatHappened"):
        body += _tactics_callout(
            "Breakdown pending",
            "This is the most recent match played. The full tactical write-up "
            "wasn't ready in time for this send and will appear in the next one.",
            GOLD)
    if x.get("whatHappened"):
        body += f'<p style="margin:0 0 4px;font-size:14px;line-height:1.6;">{E(x["whatHappened"])}</p>'

    # Shape — the concept the whole feature is built around.
    shape = x.get("shape") or {}
    listed = g.get("arsenalFormation")
    opp_listed = g.get("opponentFormation")
    chips = []
    if listed:
        chips.append(("On the teamsheet", listed))
    if shape.get("arsenalInPossession"):
        chips.append(("With the ball", shape["arsenalInPossession"]))
    if shape.get("arsenalOutOfPossession"):
        chips.append(("Without the ball", shape["arsenalOutOfPossession"]))
    if chips:
        cells = "".join(
            f'<td width="33%" valign="top" style="background:{CARD_ALT};border:1px solid {BORDER};'
            f'border-radius:6px;padding:10px 6px;text-align:center;">'
            f'<div style="font-size:9px;color:{INK_SOFT};text-transform:uppercase;'
            f'letter-spacing:.05em;font-weight:700;">{E(label)}</div>'
            f'<div style="font-size:19px;font-weight:700;color:{RED_DARK};margin-top:4px;">{E(value)}</div>'
            f'</td>'
            for label, value in chips
        )
        body += (
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:separate;border-spacing:5px;margin:10px 0 2px;">'
            f'<tr>{cells}</tr></table>'
        )
        if opp_listed:
            body += (
                f'<div style="font-size:12px;color:{INK_SOFT};text-align:center;margin-bottom:4px;">'
                f'{E(g.get("opponent") or "Opponent")} lined up {E(opp_listed)}</div>'
            )
    if shape.get("plainEnglish"):
        body += _tactics_callout("Why the shape changes", E(shape["plainEnglish"]), GOLD)

    if x.get("opponentPlan"):
        body += _tactics_callout("What they tried", E(x["opponentPlan"]), INK_SOFT)
    if x.get("keyMoment"):
        body += _tactics_callout("Turning point", E(x["keyMoment"]), INK_SOFT)

    # The lesson — the reason this section exists, so it gets the loudest styling.
    lesson = x.get("lesson") or {}
    if lesson.get("term"):
        level = lesson.get("level") or 1
        body += (
            f'<div style="margin:14px 0 6px;padding:14px 16px;background:#fffdf5;'
            f'border:2px solid {GOLD};border-radius:8px;">'
            f'<div style="font-size:10px;font-weight:700;color:{GOLD};text-transform:uppercase;'
            f'letter-spacing:.08em;">Lesson {lesson_number} · Level {level} of 3</div>'
            f'<div style="font-size:17px;font-weight:700;color:{INK};margin:4px 0 6px;">'
            f'{E(lesson["term"])}</div>'
            f'<p style="margin:0 0 8px;font-size:14px;line-height:1.6;color:{INK};">'
            f'{E(lesson.get("explain") or "")}</p>'
            f'<div style="padding:8px 12px;background:#fff;border-radius:5px;'
            f'border:1px solid {BORDER};font-size:13px;line-height:1.5;">'
            f'<strong style="color:{RED_DARK};">Watch for it:</strong> {E(lesson.get("spotIt") or "")}'
            f'</div></div>'
        )

    translations = x.get("statTranslations") or []
    if translations:
        rows = "".join(
            f'<tr>'
            f'<td valign="top" style="padding:6px 10px 6px 0;font-size:13px;font-weight:700;'
            f'color:{RED_DARK};white-space:nowrap;">{E(t.get("stat") or "")}</td>'
            f'<td valign="top" style="padding:6px 0;font-size:13px;line-height:1.5;color:{INK};'
            f'border-bottom:1px solid {BORDER};">{E(t.get("plain") or "")}</td>'
            f'</tr>'
            for t in translations
        )
        body += (
            f'<div style="font-size:10px;font-weight:700;color:{INK_SOFT};text-transform:uppercase;'
            f'letter-spacing:.06em;margin:14px 0 2px;">By the numbers</div>'
            f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
            f'style="border-collapse:collapse;">{rows}</table>'
        )

    if x.get("nerdCorner"):
        body += (
            f'<div style="margin:14px 0 0;padding:12px 14px;background:{INK};border-radius:8px;">'
            f'<div style="font-size:10px;font-weight:700;color:{GOLD};text-transform:uppercase;'
            f'letter-spacing:.08em;margin-bottom:4px;">Nerd corner</div>'
            f'<div style="font-size:13px;line-height:1.6;color:#e8ecf0;">{E(x["nerdCorner"])}</div>'
            f'</div>'
        )

    # Provenance: these write-ups are researched from published reports, so the
    # reader needs to be able to follow any claim back to where it came from.
    sources = g.get("sources") or []
    if sources:
        links = " · ".join(
            f'<a href="{E(url)}" style="color:{RED_DARK};text-decoration:none;">[{i}]</a>'
            for i, url in enumerate(sources, 1)
        )
        confidence = entry.get("confidence")
        note = f' · sourcing confidence: {E(confidence)}' if confidence else ""
        body += (
            f'<p style="margin:12px 0 0;font-size:11px;color:{INK_SOFT};line-height:1.5;">'
            f'Sources: {links}{note}</p>'
        )

    return _section_header("Tactics Lab") + _section_body(body)


def render_footer():
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return (
        f'<tr><td style="padding:16px 28px 22px;border-top:1px solid {BORDER};background:{CARD_ALT};">'
        f'<p style="margin:0;font-size:12px;color:{INK_SOFT};text-align:center;line-height:1.5;">'
        f'Arsenal Tracker · Generated {E(ts)} · '
        f'<a href="{E(SITE_URL)}" style="color:{RED_DARK};text-decoration:none;">View full tracker</a>'
        f'</p></td></tr>'
    )


def render_email(odds_items, additions, narrative, today_str, preheader,
                 tactics_entry=None, lesson_number=1, fixtures=None):
    additions = additions or {}
    narrative = narrative or {}

    inner = (
        render_header(today_str)
        + render_intro(narrative.get("summary"))
        + render_tactics(tactics_entry, lesson_number)
        + render_odds(odds_items, narrative.get("odds_commentary"))
        + render_transfers_in(additions.get("transfers_in") or [])
        + render_transfers_out(additions.get("transfers_out") or [])
        + render_rumors(additions.get("rumors") or [])
        + render_squad(narrative.get("squad_news") or [])
        + render_pl(additions.get("pl_transfers") or [], narrative.get("around_pl_notes") or [])
        + render_fixtures(fixtures or [], narrative.get("fixtures") or [])
        + render_footer()
    )

    return (
        f'<!--preheader--><div style="display:none;font-size:1px;color:{BG};'
        f'line-height:1px;max-height:0;max-width:0;opacity:0;overflow:hidden;">{E(preheader)}</div>'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        f'style="background:{BG};padding:24px 0;margin:0;">'
        f'<tr><td align="center">'
        f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="600" '
        f'style="max-width:600px;background:#ffffff;border-radius:10px;overflow:hidden;'
        f'box-shadow:0 1px 3px rgba(0,0,0,.06);'
        f'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        f'color:{INK};">'
        f'{inner}'
        f'</table>'
        f'</td></tr></table>'
    )


# ---------- email transport ----------

def send_email(html_body, subject_highlight, item_count):
    sender = os.environ["DIGEST_FROM"]
    recipient = os.environ["DIGEST_TO"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    date_str = datetime.now().strftime("%b %d")
    subj = f"Arsenal Digest — {date_str}: {subject_highlight}" if subject_highlight else f"Arsenal Digest — {date_str}"
    subj = subj[:140]

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subj
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText("This email requires an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())
    print(f"[ok] sent digest to {recipient} ({item_count} source items)")


def send_failure_email(err_text):
    sender = os.environ.get("DIGEST_FROM", "jmorcos3@gmail.com")
    recipient = os.environ.get("DIGEST_TO", "jmorcos3@gmail.com")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not password:
        print("[warn] no GMAIL_APP_PASSWORD; cannot send failure email")
        return
    body = f"The Arsenal digest workflow failed.\n\nError:\n{err_text}\n\nSee Actions log for details."
    msg = MIMEText(body)
    msg["Subject"] = "Arsenal Digest — FAILED"
    msg["From"] = sender
    msg["To"] = recipient
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        # Tells the workflow's fallback notifier to stand down: it exists for
        # failures this script never got to see, and its wording says so.
        (REPO_ROOT / FAILURE_SENTINEL).touch()
        print("[ok] sent failure email")
    except Exception as e:
        print(f"[warn] failure email send failed: {e}")


# ---------- main ----------

def main():
    try:
        refresh_odds_file()

        # Tactics runs before the news call so a feed outage can't cost us the
        # match breakdown, which is the harder half to reproduce.
        recent_matches = fetch_recent_matches()
        tactics_entry = ensure_latest_match(
            recent_matches, refresh_tactics(recent_matches) or latest_tactics_entry())
        fixtures = refresh_fixtures()
        lesson_number = len(load_json("tactics.json").get("conceptsTaught", [])) or 1

        items, lookback_hours = gather_news()
        print(f"[info] gathered {len(items)} unique articles")

        odds_items = load_json("odds.json").get("items", [])
        today_str = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

        if not items:
            html_body = render_email(
                odds_items=odds_items,
                additions={},
                narrative={"summary": "Quiet news cycle. Nothing new picked up from the tracked feeds in the last 3 days."},
                today_str=today_str,
                preheader="Quiet news cycle",
                tactics_entry=tactics_entry,
                lesson_number=lesson_number,
                fixtures=fixtures,
            )
            send_email(html_body, "quiet news cycle", 0)
            record_send()
            return

        result = generate_digest(items, odds_items, lookback_hours)
        apply_additions(result.get("additions") or {})

        # Reload odds in case anything was updated during the run (harmless if unchanged)
        odds_items = load_json("odds.json").get("items", [])

        html_body = render_email(
            odds_items=odds_items,
            additions=result.get("additions") or {},
            narrative=result.get("narrative") or {},
            today_str=today_str,
            preheader=result.get("subject_highlight", "Arsenal digest"),
            tactics_entry=tactics_entry,
            lesson_number=lesson_number,
            fixtures=fixtures,
        )
        send_email(html_body, result.get("subject_highlight", ""), len(items))
        record_send()
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        send_failure_email(tb)
        raise


if __name__ == "__main__":
    main()
