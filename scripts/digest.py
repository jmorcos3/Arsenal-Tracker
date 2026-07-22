"""Fetch Arsenal news from RSS feeds, summarize with Claude, and email a digest."""

import os
import ssl
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser
from anthropic import Anthropic

FEEDS = [
    ("Arsenal.com", "https://www.arsenal.com/rss.xml"),
    ("BBC Sport", "http://feeds.bbci.co.uk/sport/football/teams/arsenal/rss.xml"),
    ("Sky Sports", "https://www.skysports.com/rss/12040"),
    ("The Guardian", "https://www.theguardian.com/football/arsenal/rss"),
    ("Sky Sports — Transfers", "https://www.skysports.com/rss/12691"),
]

LOOKBACK_HOURS = 72
MODEL = "claude-haiku-4-5-20251001"


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
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", "").strip(),
                    "summary": (entry.get("summary", "") or "").strip()[:600],
                    "published": published,
                })
        except Exception as e:
            print(f"[warn] failed to fetch {source}: {e}")
    return items


def generate_digest(items):
    client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    articles_block = "\n\n".join(
        f"[{i['source']} · {i['published']}] {i['title']}\n"
        f"URL: {i['link']}\n"
        f"Summary: {i['summary']}"
        for i in items
    )

    today = datetime.now(timezone.utc).strftime("%A, %B %d, %Y")

    prompt = f"""You are writing an Arsenal FC news digest email for a fan.

TODAY: {today}
LOOKBACK: last {LOOKBACK_HOURS} hours

Below are recent articles from Arsenal-focused news feeds. Write an HTML email body (no <html> or <body> wrapper — just the inner HTML) organized into these sections in this order:

1. Transfers — In (confirmed signings/loans in)
2. Transfers — Out (confirmed departures/loans out)
3. Rumors (unconfirmed transfer speculation). Tag each with a reliability badge: High (Ornstein/Romano/BBC/Athletic), Medium (Sky/Guardian/Telegraph), Low (tabloids). Use a colored inline-styled span.
4. Squad News (injuries, returns, contract news, tactical notes)
5. Around the Premier League (notable moves at rival clubs)
6. Upcoming Fixtures (if mentioned)

RULES:
- Keep the whole email to ~2 pages max when printed.
- Every item MUST link to its source.
- Use bullet lists with concise one-line summaries.
- If a section has no relevant items, write: <p><em>Nothing to report.</em></p>
- Use inline CSS only (email clients ignore <style> blocks).
- Use a red (#EF0107) header accent and gold (#DB9E00) section dividers.
- Do not invent facts. If information is missing, say so.
- Start with an <h1>Arsenal Digest — {today}</h1> and end with a short signoff line.

ARTICLES:
{articles_block}
"""

    resp = client.messages.create(
        model=MODEL,
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text


def send_email(html_body, item_count):
    sender = os.environ["DIGEST_FROM"]
    recipient = os.environ["DIGEST_TO"]
    password = os.environ["GMAIL_APP_PASSWORD"]

    subject = f"Arsenal Digest — {datetime.now().strftime('%b %d')} ({item_count} items)"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = recipient
    msg.attach(MIMEText("This email requires an HTML-capable client.", "plain"))
    msg.attach(MIMEText(html_body, "html"))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as server:
        server.login(sender, password)
        server.sendmail(sender, [recipient], msg.as_string())


def main():
    items = gather_news()
    print(f"Gathered {len(items)} items from {len(FEEDS)} feeds")

    if not items:
        html = (
            "<h1 style='color:#EF0107'>Arsenal Digest</h1>"
            "<p>No news items found in the last 3 days. The pipeline is alive; "
            "either it was a quiet stretch or a feed changed format.</p>"
        )
    else:
        html = generate_digest(items)

    send_email(html, len(items))
    print(f"Sent digest to {os.environ['DIGEST_TO']}")


if __name__ == "__main__":
    main()
