"""Fetch Arsenal news, refresh live odds, update tracker JSON, email a digest."""

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
MAX_ODDS_HISTORY = 180

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
ODDS_SPORTS = {
    "Premier League": "soccer_epl",
    "Champions League": "soccer_uefa_champs_league",
}


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
    dedup_key = lambda i: re.sub(r"\W+", "", i["title"].lower())[:80]
    seen, unique = set(), []
    for it in items:
        k = dedup_key(it)
        if k and k not in seen:
            seen.add(k)
            unique.append(it)
    return unique


# ---------- odds ----------

def fetch_arsenal_odds(sport_key, api_key):
    """Return (best_decimal_odds, bookmaker_title) or (None, None)."""
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

    for comp_name, sport_key in ODDS_SPORTS.items():
        price, book = fetch_arsenal_odds(sport_key, api_key)
        if price is None:
            print(f"[warn] no live odds for {comp_name}; keeping previous value")
            new_items.append(prev_map.get(comp_name, {
                "competition": comp_name, "odds": None,
                "impliedProbability": None, "bestBookmaker": None,
                "lastUpdated": current.get("lastUpdated", ""),
            }))
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
    else:
        new_items.append(prev_map.get("Double (PL + UCL)", {
            "competition": "Double (PL + UCL)", "odds": None,
            "impliedProbability": None, "bestBookmaker": None,
            "lastUpdated": current.get("lastUpdated", ""),
        }))

    history = current.get("history", [])
    if len(live_snapshot) > 1:
        history.append(live_snapshot)
        history = history[-MAX_ODDS_HISTORY:]

    odds_path.write_text(json.dumps({
        "lastUpdated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "items": new_items,
        "history": history,
    }, indent=2) + "\n")
    print(f"[ok] odds refreshed: {[i['competition']+'='+str(i['odds']) for i in new_items]}")


# ---------- LLM: structured digest ----------

DIGEST_TOOL = {
    "name": "publish_digest",
    "description": "Emit the Arsenal digest as structured data plus HTML email body.",
    "input_schema": {
        "type": "object",
        "properties": {
            "email_html": {"type": "string", "description": "HTML email body (no <html>/<body> wrapper). Inline CSS only."},
            "subject_highlight": {"type": "string", "description": "3-8 word top-story summary for the email subject."},
            "additions": {
                "type": "object",
                "properties": {
                    "transfers_in": {"type": "array", "items": {"type": "object", "properties": {
                        "player": {"type": "string"}, "club": {"type": "string"},
                        "fee": {"type": "string"}, "date": {"type": "string"},
                    }, "required": ["player"]}},
                    "transfers_out": {"type": "array", "items": {"type": "object", "properties": {
                        "player": {"type": "string"}, "club": {"type": "string"},
                        "fee": {"type": "string"}, "date": {"type": "string"},
                    }, "required": ["player"]}},
                    "pl_transfers": {"type": "array", "items": {"type": "object", "properties": {
                        "player": {"type": "string"}, "from": {"type": "string"},
                        "to": {"type": "string"}, "fee": {"type": "string"}, "date": {"type": "string"},
                    }, "required": ["player"]}},
                    "rumors": {"type": "array", "items": {"type": "object", "properties": {
                        "headline": {"type": "string"},
                        "reliability": {"type": "string", "enum": ["high", "medium", "low"]},
                        "source": {"type": "string"}, "sourceUrl": {"type": "string"},
                        "date": {"type": "string"},
                    }, "required": ["headline"]}},
                },
            },
        },
        "required": ["email_html", "subject_highlight", "additions"],
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

    prompt = f"""You are writing an Arsenal FC news digest email for a fan.

TODAY: {today}
LOOKBACK: last {LOOKBACK_HOURS} hours

CURRENT LIVE ODDS (Arsenal to win):
{odds_lines}

ALREADY-TRACKED ITEMS (do not re-add these; only add genuinely new items):
{json.dumps(tracker_state, indent=2)[:4000]}

RECENT ARTICLES:
{articles_block}

Call the `publish_digest` tool with:

1. `additions` — ONLY items not already tracked above. Use exact field names. Rumor reliability rules:
   - High: David Ornstein, Fabrizio Romano, BBC, The Athletic
   - Medium: Sky Sports, The Guardian, The Telegraph
   - Low: tabloids, unnamed sources, aggregators
   Leave arrays empty if nothing new.

2. `subject_highlight` — the single biggest story in 3-8 words (e.g. "Rice contract extension announced"). Neutral, no clickbait.

3. `email_html` — HTML body only (no <html>/<body>). Sections in this order:
   - <h1 style="color:#EF0107">Arsenal Digest — {today}</h1>
   - "Trophy Odds" (use the live odds above; format like "PL: 3.50 (28.6%)")
   - "Transfers — In" (confirmed signings)
   - "Transfers — Out" (confirmed departures)
   - "Rumors" (with colored reliability badges: green=high, amber=medium, red=low)
   - "Squad News" (injuries, contracts, returns)
   - "Around the Premier League" (moves at rival clubs)
   - "Upcoming" (fixtures/dates if mentioned)
   Rules for the HTML:
   - Every item MUST include a source link.
   - Inline CSS only. Use gold (#DB9E00) for section-heading borders.
   - Bullet points, one-line summaries.
   - Empty sections: <p><em>Nothing to report.</em></p>
   - Do not invent facts.
   - ~2 pages max when printed.
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
    in_added = _merge(transfers["in"], additions.get("transfers_in"), lambda t: _norm(t.get("player")))
    out_added = _merge(transfers["out"], additions.get("transfers_out"), lambda t: _norm(t.get("player")))
    transfers["lastUpdated"] = today
    (DATA_DIR / "transfers.json").write_text(json.dumps(transfers, indent=2) + "\n")

    pl = load_json("pl-transfers.json") or {"window": "Summer 2026", "items": []}
    pl.setdefault("items", [])
    pl_added = _merge(pl["items"], additions.get("pl_transfers"),
                      lambda t: _norm(t.get("player")) + "|" + _norm(t.get("to")))
    pl["lastUpdated"] = today
    (DATA_DIR / "pl-transfers.json").write_text(json.dumps(pl, indent=2) + "\n")

    rumors = load_json("rumors.json") or {"items": []}
    rumors.setdefault("items", [])
    rumor_added = _merge(rumors["items"], additions.get("rumors"),
                         lambda r: _norm(r.get("headline"))[:80])
    rumors["lastUpdated"] = today
    (DATA_DIR / "rumors.json").write_text(json.dumps(rumors, indent=2) + "\n")

    print(f"[ok] additions applied: transfers_in={in_added} transfers_out={out_added} "
          f"pl_transfers={pl_added} rumors={rumor_added}")


# ---------- email ----------

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

    body = (
        "The Arsenal digest workflow failed while running.\n\n"
        f"Error:\n{err_text}\n\n"
        "Check the Actions log for full traceback."
    )
    msg = MIMEText(body)
    msg["Subject"] = "Arsenal Digest — FAILED"
    msg["From"] = sender
    msg["To"] = recipient
    ctx = ssl.create_default_context()
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
            server.login(sender, password)
            server.sendmail(sender, [recipient], msg.as_string())
        print("[ok] sent failure email")
    except Exception as e:
        print(f"[warn] failure email send failed: {e}")


# ---------- main ----------

def main():
    try:
        refresh_odds_file()
        items = gather_news()
        print(f"[info] gathered {len(items)} unique articles from {len(FEEDS)} feeds")

        odds_snapshot = load_json("odds.json").get("items", [])

        if not items:
            html = (
                "<h1 style='color:#EF0107'>Arsenal Digest</h1>"
                "<p>No news items found in the last 3 days. Pipeline is alive — "
                "quiet stretch, or a feed changed format.</p>"
            )
            send_email(html, "quiet news cycle", 0)
            return

        result = generate_digest(items, odds_snapshot)
        apply_additions(result.get("additions") or {})
        send_email(result["email_html"], result.get("subject_highlight", ""), len(items))
    except Exception:
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        send_failure_email(tb)
        raise


if __name__ == "__main__":
    main()
