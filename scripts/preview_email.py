#!/usr/bin/env python3
"""Render the digest email to an HTML file using sample data.

The template is only ever seen after a live send, which makes it awkward to
change. This renders the same `render_email` the digest uses, with a fully
populated fake digest, so layout changes can be checked in a browser at phone
and desktop width before anything goes out.

    python3 scripts/preview_email.py [outfile]   # default: preview.html
"""

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Stub the network-facing modules so the renderer imports without credentials.
for _name, _attrs in {
    "feedparser": ["parse"],
    "anthropic": ["Anthropic"],
    "fotmob": ["fetch_recent_matches", "fetch_standing", "fetch_transfers",
               "fetch_upcoming_fixtures", "summarize_for_prompt"],
    "kalshi": ["fetch_trophy_prices", "double_item"],
}.items():
    _m = types.ModuleType(_name)
    for _a in _attrs:
        setattr(_m, _a, lambda *a, **k: None)
    sys.modules.setdefault(_name, _m)

import digest  # noqa: E402

ODDS = [
    {"competition": "Premier League", "impliedProbability": 0.412, "odds": 2.43,
     "bidCents": 40, "askCents": 42},
    {"competition": "Champions League", "impliedProbability": 0.118, "odds": 8.47,
     "bidCents": 11, "askCents": 13},
    {"competition": "FA Cup", "impliedProbability": 0.155, "odds": 6.45,
     "bidCents": 15, "askCents": 16},
    {"competition": "Carabao Cup", "impliedProbability": 0.089, "odds": 11.2,
     "bidCents": 8, "askCents": 10},
    {"competition": "Double (PL + UCL)", "impliedProbability": 0.061, "odds": 16.4},
]

STANDING = {"position": 1, "competition": "Premier League", "points": 58, "wins": 18,
            "draws": 4, "losses": 2, "goalDifference": 34, "played": 24,
            "form": ["W", "W", "D", "W", "L"]}

FIXTURES = [
    {"opponent": "Manchester City", "homeAway": "H", "competition": "Premier League",
     "kickoffLocal": "Sat 11:30 AM"},
    {"opponent": "Real Madrid", "homeAway": "A", "competition": "Champions League",
     "kickoffLocal": "Tue 3:00 PM"},
    {"opponent": "Nottingham Forest", "homeAway": "H", "competition": "Premier League",
     "kickoffLocal": "Sun 9:00 AM"},
]

TRANSFERS = {
    "in": [{"player": "Example Winger", "position": "RW", "club": "Sporting CP",
            "fee": "£52m", "date": "Feb 2", "sourceUrl": "https://example.com"}],
    "out": [{"player": "Example Fullback", "club": "Marseille", "onLoan": True,
             "date": "Feb 1"}],
}

RUMORS = [
    {"player": "Example Midfielder", "position": "CM", "direction": "in",
     "club": "Leverkusen", "fee": "£70m", "date": "Feb 3"},
    {"headline": "Arsenal monitoring a January move for a backup keeper",
     "reliability": "low", "source": "The Guardian",
     "sourceUrl": "https://example.com", "date": "Feb 3"},
]

TACTICS = {
    "confidence": "high",
    "grounded": {"opponent": "Chelsea", "goalsFor": 3, "goalsAgainst": 1, "homeAway": "H",
                 "competition": "Premier League", "date": "Feb 1",
                 "arsenalFormation": "4-3-3", "opponentFormation": "4-2-3-1",
                 "sources": ["https://example.com/a", "https://example.com/b"]},
    "explained": {
        "whatHappened": "Arsenal controlled the first half through the left side and "
                        "scored twice from set pieces before Chelsea pulled one back.",
        "shape": {"arsenalInPossession": "3-2-5", "arsenalOutOfPossession": "4-4-2",
                  "plainEnglish": "With the ball the left back steps into midfield, which "
                                  "turns the back four into a back three and pushes five "
                                  "players high."},
        "opponentPlan": "Chelsea sat in a mid-block and tried to spring the counter "
                        "through the channels.",
        "keyMoment": "The second goal, six minutes after half time, forced Chelsea out.",
        "lesson": {"term": "Inverted fullback", "level": 2,
                   "explain": "A fullback who moves inside into central midfield instead "
                              "of running the touchline, giving the team an extra passer "
                              "in the middle.",
                   "spotIt": "Watch the left back at a goal kick — if he lines up next to "
                             "the holding midfielder rather than on the sideline, he is "
                             "inverting."},
        "statTranslations": [
            {"stat": "xG 2.6 vs 0.9",
             "plain": "Arsenal's chances were worth about three goals; Chelsea's about one."},
            {"stat": "PPDA 8.1",
             "plain": "Arsenal let Chelsea make roughly eight passes before pressing."},
        ],
        "nerdCorner": "The 3-2-5 only works because the right winger holds width; without "
                      "him pinning the fullback the extra central man is marked out.",
    },
}

NARRATIVE = {
    "summary": "A clean week: three points at home, one signing over the line, and the "
               "title price moves in Arsenal's favour.",
    "odds_commentary": "The Premier League price has firmed from 38% to 41% since the "
                       "last digest.",
    "squad_news": [{"text": "Saka back in full training", "url": "https://example.com"}],
    "around_pl_notes": [{"text": "City agree a deal for a Brazilian centre back"}],
    "fixtures": [],
}


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "preview.html")
    body = digest.render_email(
        odds_items=ODDS,
        additions={"pl_transfers": [
            {"player": "Example Striker", "from": "Ajax", "to": "Tottenham",
             "fee": "£40m", "date": "Feb 2", "sourceUrl": "https://example.com"}]},
        narrative=NARRATIVE,
        today_str="Monday, February 3, 2026",
        preheader="Title price firms to 41%",
        tactics_entry=TACTICS,
        lesson_number=7,
        fixtures=FIXTURES,
        standing=STANDING,
        transfers=TRANSFERS,
        rumors=RUMORS,
    )
    out.write_text(body)
    kb = len(body.encode()) / 1024
    print(f"[ok] wrote {out} ({kb:.1f} KB)")
    # Gmail clips anything past ~102KB behind a "View entire message" link.
    if kb > 90:
        print(f"[warn] {kb:.1f} KB is close to Gmail's 102 KB clipping threshold")


if __name__ == "__main__":
    main()
