"""Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import generate_snake as snake


def sample_calendar():
    start = dt.date(2026, 10, 4)
    weeks = []
    counts = {(0, 1): 1, (0, 3): 6, (1, 4): 3}
    for col in range(2):
        days = []
        for weekday in range(7):
            count = counts.get((col, weekday), 0)
            level = "FOURTH_QUARTILE" if count >= 6 else "SECOND_QUARTILE" if count > 0 else "NONE"
            days.append({
                "weekday": weekday,
                "date": (start + dt.timedelta(days=7*col+weekday)).isoformat(),
                "contributionCount": count,
                "contributionLevel": level,
            })
        weeks.append({"contributionDays": days})
    return {"weeks": weeks, "totalContributions": 10}


class TestOwnSnake(unittest.TestCase):
    def setUp(self):
        self.calendar = sample_calendar()
        self.total, self.days = snake.parse_days(self.calendar)

    def test_calendar_parses_days_and_totals(self):
        self.assertEqual(self.total, 10)
        self.assertEqual(len(self.days), 14)
        self.assertEqual(sum(d.count for d in self.days), 10)

    def test_route_reaches_every_contribution_day(self):
        route, eaten = snake.plan_route(self.days, 2)
        occupied = {(d.week, d.weekday) for d in self.days if d.count > 0}
        self.assertEqual(set(eaten), occupied)
        for a, b in zip(route[:-(snake.SNAKE_LENGTH + 5)], route[1:]):
            self.assertEqual(abs(b[0]-a[0]) + abs(b[1]-a[1]), 1)

    def test_svg_wellformed_and_no_network_assets(self):
        for theme in snake.THEMES:
            with self.subTest(theme=theme):
                svg = snake.render_svg(self.days, theme)
                root = ET.fromstring(svg)
                self.assertEqual(root.tag, '{http://www.w3.org/2000/svg}svg')
                rects = root.findall('{http://www.w3.org/2000/svg}rect')
                self.assertEqual(len(rects), 2*7 + snake.SNAKE_LENGTH)
                self.assertEqual(svg.count('class="tile food food'), 3)
                self.assertIn('@keyframes move0', svg)
                self.assertIn('@keyframes eat0', svg)
                self.assertNotIn('github_token', svg.lower())
                self.assertNotIn('<script', svg.lower())
                self.assertNotIn('<image', svg.lower())
                self.assertNotIn('foreignObject', svg)

    def test_svg_contains_no_token(self):
        secret = 'ghp_test_dont_include_me_123456'
        with tempfile.TemporaryDirectory() as tmp, patch.object(snake, 'fetch_calendar', return_value=self.calendar):
            total, occupied = snake.generate('andr4ykaa', secret, Path(tmp))
            self.assertEqual((total, occupied), (10, 3))
            for filename in ('github-snake.svg', 'github-snake-dark.svg'):
                text = (Path(tmp) / filename).read_text()
                self.assertNotIn(secret, text)
                self.assertEqual(ET.fromstring(text).tag, '{http://www.w3.org/2000/svg}svg')

    def test_bad_days_rejected(self):
        calendar = sample_calendar()
        calendar['weeks'][0]['contributionDays'][0]['weekday'] = 8
        with self.assertRaises(ValueError):
            snake.parse_days(calendar)

    def test_inconsistent_level_rejected(self):
        calendar = sample_calendar()
        calendar['weeks'][0]['contributionDays'][1]['contributionCount'] = 0
        with self.assertRaises(ValueError):
            snake.parse_days(calendar)

    def test_absent_token_returns_error(self):
        with patch.dict(os.environ, {'SNAKE_TOKEN': ''}):
            self.assertEqual(snake.main(['--user', 'andr4ykaa']), 1)


if __name__ == '__main__':
    unittest.main()
