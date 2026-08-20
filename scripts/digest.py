"""Fetch Arsenal news, refresh live odds, update tracker JSON, email a digest."""

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

from football_api import fetch_recent_matches, summarize_for_prompt

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

FEEDS = [
    ("Arsenal.com", "https://www.arsenal.com/rss.xml"),
    ("BBC Sport", "http://feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("The Guardian", "https://www.theguardian.com/football/arsenal/rss"),
    ("Sky Sports — Transfers", "https://www.skysports.com/rss/12691"),
]

LOOKBACK_HOURS = 72
MODEL = "claude-haiku-4-5-20251001"
# The tactics write-up is the one piece of generated content a beginner can't
# sanity-check for themselves, so it gets the stronger model.
TACTICS_MODEL = "claude-sonnet-5"
MAX_ODDS_HISTORY = 180
MAX_TACTICS_HISTORY = 40

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
ODDS_SPORTS = {
    "Premier League": "soccer_epl",
    "Champions League": "soccer_uefa_champs_league",
    "FA Cup": "soccer_fa_cup",
    "Carabao Cup": "soccer_efl_cup",
}
INDIVIDUAL_COMPS = list(ODDS_SPORTS.keys())

SITE_URL = os.environ.get("SITE_URL", "https://github.com/jmorcos3/Arsenal-Tracker")


# ---------- news gathering ----------

def gather_news():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
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
    return unique


# ---------- odds ----------

def fetch_arsenal_odds(sport_key, api_key):
    url = (
        f"{ODDS_API_BASE}/{sport_key}/odds"
        f"?apiKey={api_key}&regions=uk&markets=outrights&oddsFormat=decimal"
    )
    try:
        req = Request(url, headers={"User-Agent": "arsenal-tracker/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (URLError, ValueError, TimeoutError) as e:
        print(f"[warn] odds fetch failed for {sport_key}: {e}")
        return None, None

    best_price, best_book = None, None
    for event in data:
        for bm in event.get("bookmakers", []):
            for market in bm.get("markets", []):
                if market.get("key") != "outrights":
                    continue
                for outcome in market.get("outcomes", []):
                    name = (outcome.get("name") or "").lower()
                    if "arsenal" in name and "arsenal fan token" not in name:
                        price = outcome.get("price")
                        if isinstance(price, (int, float)) and (best_price is None or price > best_price):
                            best_price = float(price)
                            best_book = bm.get("title") or bm.get("key")
    return best_price, best_book


def refresh_odds_file():
    api_key = os.environ.get("ODDS_API_KEY")
    if not api_key:
        print("[info] ODDS_API_KEY not set; skipping live odds refresh")
        return

    odds_path = DATA_DIR / "odds.json"
    current = json.loads(odds_path.read_text()) if odds_path.exists() else {"items": [], "history": []}
    prev_map = {i["competition"]: i for i in current.get("items", [])}

    new_items = []
    live_snapshot = {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d")}

    for comp_name in INDIVIDUAL_COMPS:
        sport_key = ODDS_SPORTS[comp_name]
        price, book = fetch_arsenal_odds(sport_key, api_key)
        if price is None:
            prev = prev_map.get(comp_name)
            if prev:
                new_items.append(prev)
                print(f"[warn] no live odds for {comp_name}; keeping previous value {prev.get('odds')}")
            else:
                new_items.append({
                    "competition": comp_name, "odds": None,
                    "impliedProbability": None, "bestBookmaker": None,
                    "lastUpdated": current.get("lastUpdated", ""),
                })
                print(f"[warn] no live odds for {comp_name} and no previous value")
            continue
        new_items.append({
            "competition": comp_name,
            "odds": round(price, 2),
            "impliedProbability": round(1.0 / price, 4),
            "bestBookmaker": book,
            "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
        live_snapshot[comp_name] = round(price, 2)

    pl_price = next((i["odds"] for i in new_items if i["competition"] == "Premier League" and i["odds"]), None)
    ucl_price = next((i["odds"] for i in new_items if i["competition"] == "Champions League" and i["odds"]), None)
    if pl_price and ucl_price:
        double_price = round(pl_price * ucl_price, 2)
        new_items.append({
            "competition": "Double (PL + UCL)",
            "odds": double_price,
            "impliedProbability": round(1.0 / double_price, 4),
            "bestBookmaker": "Implied (product of individual odds)",
            "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        })
        live_snapshot["Double (PL + UCL)"] = double_price
    elif prev_map.get("Double (PL + UCL)"):
        new_items.append(prev_map["Double (PL + UCL)"])

    history = current.get("history", [])
    if len(live_snapshot) > 1:
        history.append(live_snapshot)
        history = history[-MAX_ODDS_HISTORY:]

    odds_path.write_text(json.dumps({
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": new_items,
        "history": history,
    }, indent=2) + "\n")
    print(f"[ok] odds refreshed")


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


def generate_digest(items, odds_snapshot):
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
LOOKBACK: last {LOOKBACK_HOURS} hours

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
        "required": ["whatHappened", "shape", "opponentPlan", "lesson", "statTranslations", "nerdCorner"],
    },
}


def generate_tactics(match, concepts_taught):
    """Explain a match. Every fact must come from `match`; the model adds only interpretation."""
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    glossary = load_json("glossary.json").get("items", [])
    taught = set(concepts_taught or [])
    available = [g for g in glossary if g["id"] not in taught]
    if not available:
        # Curriculum complete — allow revisiting, deepest concepts first.
        available = sorted(glossary, key=lambda g: -g["level"])

    # Ramp: stay on level 1 until the basics are covered, then open up.
    max_level = 1 if len(taught) < 6 else (2 if len(taught) < 12 else 3)
    eligible = [g for g in available if g["level"] <= max_level] or available

    concept_menu = "\n".join(
        f"- {g['id']} (level {g['level']}) — {g['term']}: {g['short']}" for g in eligible
    )

    prompt = f"""You are writing the tactical education section of an Arsenal FC digest.

THE READER: a passionate Arsenal fan who watches every match but has never been taught
how to read tactics. They are smart and want to learn properly — they are not stupid,
they just lack the vocabulary. Never condescend. Never pad. Explain like a good coach
talking to an interested adult.

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

    resp = client.messages.create(
        model=TACTICS_MODEL,
        max_tokens=3000,
        tools=[TACTICS_TOOL],
        tool_choice={"type": "tool", "name": "publish_tactics"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "publish_tactics":
            return block.input
    raise RuntimeError("LLM did not return a publish_tactics tool call")


def latest_tactics_entry(max_age_days=10):
    """Most recent stored breakdown, if it's still topical.

    Covers the run where nothing new was played but the last match hasn't been
    emailed yet — without re-sending an old write-up through an international
    break.
    """
    matches = (load_json("tactics.json") or {}).get("matches") or []
    if not matches:
        return None
    entry = matches[0]
    try:
        played = datetime.strptime(entry["date"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return None
    return entry if (datetime.now(timezone.utc) - played).days <= max_age_days else None


def refresh_tactics(matches):
    """Write up every supplied fixture that isn't already covered.

    Matches arrive oldest-first so lessons are taught in the order they were
    played. Returns the newest entry for the email, or None.
    """
    path = DATA_DIR / "tactics.json"
    current = load_json("tactics.json") or {}
    current.setdefault("matches", [])
    current.setdefault("conceptsTaught", [])

    newest = None
    wrote = False

    for match in matches or []:
        if not match or not match.get("fixtureId"):
            continue

        existing = next((m for m in current["matches"] if m.get("fixtureId") == match["fixtureId"]), None)
        if existing:
            newest = existing
            continue

        try:
            explained = generate_tactics(match, current["conceptsTaught"])
        except Exception as e:
            # One bad fixture shouldn't cost us the rest of the backlog.
            print(f"[warn] tactics generation failed for {match.get('opponent')}: {e}")
            continue

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
        f'{E(today_str)} · {LOOKBACK_HOURS // 24}-day recap</p>'
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
        odds = "—" if not item or item.get("odds") is None else f'{item["odds"]:.2f}'
        prob = "" if not item or item.get("impliedProbability") is None else f'{item["impliedProbability"] * 100:.1f}%'
        book = "" if not item or not item.get("bestBookmaker") else E(item["bestBookmaker"])
        return (
            f'<td width="25%" valign="top" style="background:{CARD_ALT};border:1px solid {BORDER};'
            f'border-radius:6px;padding:12px 6px;text-align:center;">'
            f'<div style="font-size:10px;color:{INK_SOFT};text-transform:uppercase;letter-spacing:.05em;font-weight:600;">{E(comp)}</div>'
            f'<div style="font-size:22px;font-weight:700;color:{RED_DARK};margin:6px 0 2px;">{E(odds)}</div>'
            f'<div style="font-size:11px;color:{INK_SOFT};">{E(prob)}</div>'
            f'<div style="font-size:10px;color:{INK_SOFT};margin-top:2px;">{book}</div>'
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
        prob = f'{double["impliedProbability"] * 100:.2f}%' if double.get("impliedProbability") is not None else ""
        double_row = (
            f'<div style="margin-top:10px;padding:8px 12px;background:{CARD_ALT};'
            f'border:1px solid {BORDER};border-radius:6px;text-align:center;font-size:13px;color:{INK_SOFT};">'
            f'Double (PL + UCL): <strong style="color:{RED_DARK};font-size:16px;">{double["odds"]:.2f}</strong>'
            f'{f" · {E(prob)}" if prob else ""}'
            f'</div>'
        )

    commentary_html = f'<p style="margin:8px 0 0;color:{INK_SOFT};font-size:13px;font-style:italic;">{E(commentary)}</p>' if commentary else ""

    return _section_header("Trophy Odds") + _section_body(grid + double_row + commentary_html)


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


def render_fixtures(fixtures):
    if not fixtures:
        return _section_header("Upcoming") + _section_body(_empty())
    return _section_header("Upcoming") + _section_body(_bullet_list([_note_line(n) for n in fixtures]))


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
                 tactics_entry=None, lesson_number=1):
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
        + render_fixtures(narrative.get("fixtures") or [])
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
        print("[ok] sent failure email")
    except Exception as e:
        print(f"[warn] failure email send failed: {e}")


# ---------- main ----------

def main():
    try:
        refresh_odds_file()

        # Tactics runs before the news call so a feed outage can't cost us the
        # match breakdown, which is the harder half to reproduce.
        tactics_entry = refresh_tactics(fetch_recent_matches()) or latest_tactics_entry()
        lesson_number = len(load_json("tactics.json").get("conceptsTaught", [])) or 1

        items = gather_news()
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
            )
            send_email(html_body, "quiet news cycle", 0)
            return

        result = generate_digest(items, odds_items)
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
        )
        send_email(html_body, result.get("subject_highlight", ""), len(items))
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        send_failure_email(tb)
        raise


if __name__ == "__main__":
    main()
