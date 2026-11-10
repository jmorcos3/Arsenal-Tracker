#!/usr/bin/env python3
"""Render the digest email to an HTML file from the data already in data/.

The template is otherwise only ever seen after a live send, which makes it
awkward to change. This runs the same `render_email` the digest uses on the
repo's real odds, fixtures, standing, comparison and latest match write-up,
with a sample news narrative, so layout changes can be checked in a browser at
phone and desktop width — and in dark mode — before anything goes out.

    python3 scripts/preview_email.py [outfile]   # default: preview.html

No network, no API keys, and nothing in data/ is written.
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The renderer never touches these; stub them only if they aren't installed.
for _name, _attr in (("feedparser", "parse"), ("anthropic", "Anthropic")):
    try:
        __import__(_name)
    except ImportError:
        _module = types.ModuleType(_name)
        setattr(_module, _attr, lambda *a, **k: None)
        sys.modules[_name] = _module

import digest  # noqa: E402

NARRATIVE = {
    "summary": "A clean week: three points at home, a place in the next round, and the "
               "title price moves further in Arsenal's favour.",
    "odds_commentary": "The Premier League price has firmed by three points since the "
                       "last digest.",
    "squad_news": [
        {"text": "Saka back in full training ahead of the weekend", "url": "https://example.com"},
        {"text": "Contract talks opened with a first-team regular"},
    ],
    "around_pl_notes": [{"text": "City agree a deal for a Brazilian centre back"}],
    "fixtures": [],
}


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview.html")
    tactics = digest.load_json("tactics.json") or {}
    entry = (tactics.get("matches") or [None])[0]
    body = digest.render_email(
        odds_items=(digest.load_json("odds.json") or {}).get("items", []),
        additions={"pl_transfers": [
            {"player": "Example Striker", "from": "Ajax", "to": "Tottenham",
             "fee": "€40M", "date": "2026-11-02", "sourceUrl": "https://example.com"}]},
        narrative=NARRATIVE,
        today_str="Sunday, November 8, 2026",
        preheader=NARRATIVE["summary"],
        tactics_entry=entry,
        lesson_label=digest.lesson_label(entry, tactics.get("conceptsTaught", [])),
        fixtures=(digest.load_json("fixtures.json") or {}).get("items", []),
        standing=digest.load_json("standing.json") or None,
        transfers=digest.recent_transfers(days=120),
        rumors=digest.recent_rumors(),
        compare=digest.load_json("season-compare.json") or None,
    )
    out.write_text(body, encoding="utf-8")
    kb = len(body.encode()) / 1024
    print(f"[ok] wrote {out} ({kb:.1f} KB)")
    # Gmail clips anything past ~102KB behind a "View entire message" link.
    if kb > 90:
        print(f"[warn] {kb:.1f} KB is close to Gmail's 102 KB clipping threshold")


if __name__ == "__main__":
    main()
