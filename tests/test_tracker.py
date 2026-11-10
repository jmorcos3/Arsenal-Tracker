"""Offline checks for the logic that decides what gets sent and shown.

No network and no API keys: run with `python -m unittest discover tests`.
"""

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import digest  # noqa: E402
import fotmob  # noqa: E402
import kalshi  # noqa: E402

FIXTURES = [
    {"id": "bayern", "kickoff": "2026-10-21T19:00:00.000Z", "opponent": "Bayern", "competition": "Champions League"},
    {"id": "everton", "kickoff": "2026-10-24T14:00:00.000Z", "opponent": "Everton", "competition": "Premier League"},
    {"id": "fleetwood", "kickoff": "2026-10-27T20:00:00.000Z", "opponent": "Fleetwood", "competition": "EFL Cup"},
    # 7:30pm ET kickoff: already the next day in UTC, still match day in ET.
    {"id": "late", "kickoff": "2026-10-29T23:30:00.000Z", "opponent": "Late KO", "competition": "FA Cup"},
]


class FrozenDatetime(datetime):
    NOW = None

    @classmethod
    def now(cls, tz=None):
        return cls.NOW.astimezone(tz) if tz else cls.NOW.replace(tzinfo=None)


class SendGate(unittest.TestCase):
    """The digest goes out the day after a match, once, and catches up if a run is missed."""

    def setUp(self):
        self.state = {"lastDigestAt": "2026-10-20T17:14:39+00:00"}
        self._saved = (digest.datetime, digest.load_json, digest.fetch_finished_matches)
        digest.datetime = FrozenDatetime
        digest.load_json = lambda name: json.loads(json.dumps(self.state)) if name == digest.STATE_FILE else {}

    def tearDown(self):
        digest.datetime, digest.load_json, digest.fetch_finished_matches = self._saved

    def run_at(self, utc, send=True):
        FrozenDatetime.NOW = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)
        digest.fetch_finished_matches = lambda days=7: [f for f in FIXTURES if f["kickoff"] < utc]
        due, covered = digest.due_matches()
        if due and send:
            self.state["coveredMatchIds"] = covered + [m["id"] for m in due]
        return [m["id"] for m in due]

    def test_schedule(self):
        self.assertEqual(self.run_at("2026-10-21T21:00:00"), [], "never on match day")
        self.assertEqual(self.run_at("2026-10-22T12:17:00"), ["bayern"])
        self.assertEqual(self.run_at("2026-10-23T12:17:00"), [], "nothing new, nothing re-sent")
        self.assertEqual(self.run_at("2026-10-25T12:17:00", send=False), ["everton"])
        self.assertEqual(self.run_at("2026-10-26T12:17:00"), ["everton"], "a failed run is caught up")
        self.assertEqual(self.run_at("2026-10-28T12:17:00"), ["fleetwood"])
        self.assertEqual(self.run_at("2026-10-30T01:00:00"), [], "still match day in ET")
        self.assertEqual(self.run_at("2026-10-30T12:17:00"), ["late"])

    def test_unreadable_fixtures(self):
        digest.fetch_finished_matches = lambda days=7: None
        self.assertEqual(digest.due_matches(), (None, []))


class KalshiPrices(unittest.TestCase):
    def test_midpoint_needs_both_sides(self):
        price = lambda **m: kalshi._price(m)[0]
        self.assertAlmostEqual(price(yes_bid_dollars="0.6000", yes_ask_dollars="0.6200"), 0.61)
        # A vanished bid comes back as 0; averaging it in would halve the price.
        self.assertAlmostEqual(
            price(yes_bid_dollars="0.0000", yes_ask_dollars="0.2700", last_price_dollars="0.2200"), 0.22)
        self.assertIsNone(price(yes_bid_dollars="0.0000", yes_ask_dollars="0.2700"))

    def test_double_follows_a_settled_leg(self):
        pl = {"competition": "Premier League", "impliedProbability": 0.7, "lastUpdated": "2026-11-10"}
        ucl = {"competition": "Champions League", "impliedProbability": 0.2, "lastUpdated": "2026-11-10"}
        self.assertAlmostEqual(kalshi.double_item([pl, ucl])["impliedProbability"], 0.14)
        out = dict(ucl, impliedProbability=0.0, settled="out")
        self.assertEqual(kalshi.double_item([pl, out])["settled"], "out")


class Curriculum(unittest.TestCase):
    def setUp(self):
        self.ids = [g["id"] for g in digest.load_json("glossary.json")["items"]]

    def test_untaught_concepts_are_offered(self):
        block = digest._curriculum_block(self.ids[:3])
        self.assertIn("do not repeat", block)
        self.assertNotIn(f"- {self.ids[0]} (", block)

    def test_finished_curriculum_becomes_refresher(self):
        block = digest._curriculum_block(self.ids)
        self.assertIn("REFRESHER", block)
        self.assertNotIn("do not repeat", block)
        self.assertIn("\n- ", block, "there must still be something to pick")

    def test_lesson_label(self):
        entry = {"explained": {"lesson": {"conceptId": "xg"}}}
        self.assertEqual(digest.lesson_label(entry, ["press", "xg"]), "Lesson 2")
        self.assertEqual(digest.lesson_label(dict(entry, refresher=True), ["press", "xg"]), "Refresher")
        self.assertEqual(digest.lesson_label(None, []), "Lesson")


class Shootouts(unittest.TestCase):
    def test_shootout_read_from_arsenals_side(self):
        header = {"status": {"reason": {"penalties": [5, 3]}}}
        self.assertEqual(fotmob._shootout(header, arsenal_home=False),
                         {"arsenal": 3, "opponent": 5, "won": False})
        self.assertEqual(fotmob._shootout(header, arsenal_home=True)["won"], True)
        self.assertIsNone(fotmob._shootout({"status": {"reason": {"short": "FT"}}}, True))


class EmailTemplate(unittest.TestCase):
    def test_dark_classes_follow_inline_colours(self):
        tagged = digest._tag_for_dark_mode(
            f'<td class="px" style="padding:4px;color:{digest.INK};background:{digest.CARD_ALT};'
            f'border-bottom:1px solid {digest.BORDER};">x</td>')
        self.assertIn('class="px d-ink d-alt d-bb"', tagged)

    def test_fixed_colour_chips_keep_their_text(self):
        chip = f'<span style="background:#fdeaec;color:{digest.INK_SOFT};">HOME</span>'
        self.assertEqual(digest._tag_for_dark_mode(chip), chip)

    def test_every_dark_class_has_a_rule(self):
        for cls, _ in digest.DARK_RULES.values():
            self.assertIn(f".{cls} ", digest.EMAIL_STYLE)

    def test_short_labels(self):
        self.assertEqual(digest._short_date("2026-11-07"), "Nov 7")
        self.assertEqual(digest._short_date("2026-11-07", weekday=True), "Sat Nov 7")
        self.assertEqual(digest._short_date("Feb 1"), "Feb 1")
        self.assertEqual(digest._short_season("2025/2026"), "25/26")


if __name__ == "__main__":
    unittest.main()
