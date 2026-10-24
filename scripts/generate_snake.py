#!/usr/bin/env python3
"""Render GitHub contribution-calendar SVG animations with the Python standard library.

No third-party rendering tools, downloaded executables or Python dependencies.
The token is used solely for an authenticated GitHub GraphQL read.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

GRAPHQL_URL = "https://api.github.com/graphql"
GRAPHQL_QUERY = """
query ContributionSnake($login: String!) {
  viewer { login }
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks {
          contributionDays { date weekday contributionCount contributionLevel }
        }
      }
    }
  }
}
"""

CELL = 12
GAP = 4
PITCH = CELL + GAP
PAD = 8
SNAKE_LENGTH = 4
SECONDS_PER_FRAME = 0.085

THEMES = {
    "light": {"empty": "#ebedf0", "c1": "#9be9a8", "c2": "#40c463", "c3": "#30a14e", "c4": "#216e39", "snake": "#475569"},
    "dark": {"empty": "#161b22", "c1": "#0e4429", "c2": "#006d32", "c3": "#26a641", "c4": "#39d353", "snake": "#94a3b8"},
}
LEVELS = {
    "NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2,
    "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4,
}


@dataclass(frozen=True)
class Day:
    week: int
    weekday: int
    date: str
    count: int
    level: int


def fetch_calendar(login: str, token: str) -> dict:
    """Read-only request, without logging the token, Authorization header or response."""
    payload = json.dumps({"query": GRAPHQL_QUERY, "variables": {"login": login}}).encode()
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "andrew-contribution-snake/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.load(response)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        # Don't print URLs containing credentials (none are used), request headers, etc.
        raise RuntimeError(f"GitHub GraphQL request failed ({type(exc).__name__})") from None

    if result.get("errors"):
        errors = result["errors"]
        # GraphQL errors can be arbitrary server strings; don't echo them to logs.
        raise RuntimeError(f"GitHub GraphQL reported {len(errors)} error(s)")
    data = result.get("data") or {}
    if (data.get("viewer") or {}).get("login", "").casefold() != login.casefold():
        raise RuntimeError("SNAKE_TOKEN is not authenticated as the requested user")
    user = data.get("user")
    if not user:
        raise RuntimeError("User not found in GitHub GraphQL response")
    calendar = (user.get("contributionsCollection") or {}).get("contributionCalendar")
    if not isinstance(calendar, dict):
        raise RuntimeError("Contribution calendar is missing")
    return calendar


def parse_days(calendar: dict) -> tuple[int, list[Day]]:
    weeks = calendar.get("weeks")
    if not isinstance(weeks, list) or not weeks or len(weeks) > 54:
        raise ValueError("Expected 1 to 54 contribution weeks")
    days = []
    seen = set()
    for week_idx, week in enumerate(weeks):
        for raw in week.get("contributionDays", []):
            weekday = raw["weekday"]
            count = raw["contributionCount"]
            date = raw["date"]
            level_name = raw["contributionLevel"]
            if type(weekday) is not int or weekday not in range(7):
                raise ValueError("Invalid weekday")
            if type(count) is not int or count < 0:
                raise ValueError("Invalid contribution count")
            if not isinstance(date, str):
                raise ValueError("Invalid date")
            dt.date.fromisoformat(date)
            if date in seen or (week_idx, weekday) in seen:
                raise ValueError("Duplicate contribution date/cell")
            seen.add(date)
            seen.add((week_idx, weekday))
            if level_name not in LEVELS:
                raise ValueError("Unknown contribution level")
            level = LEVELS[level_name]
            if count == 0 and level != 0 or count > 0 and level == 0:
                raise ValueError("Inconsistent contribution level/count")
            days.append(Day(week_idx, weekday, date, count, level))
    if not days:
        raise ValueError("Contribution calendar is empty")
    total = calendar.get("totalContributions")
    if type(total) is not int or total < 0:
        raise ValueError("Missing or invalid totalContributions")
    return total, days


def plan_route(days: list[Day], columns: int) -> tuple[list[tuple[int, int]], dict[tuple[int, int], int]]:
    """Greedy nearest-food snake, one grid step at a time, deterministic."""
    food = {(d.week, d.weekday) for d in days if d.count > 0}
    path = [(-4, 0), (-3, 0), (-2, 0), (-1, 0), (0, 0)]
    visited_at = {}

    def visit(point: tuple[int, int]) -> None:
        if point in food:
            food.remove(point)
            visited_at[point] = len(path) - 1

    visit((0, 0))
    while food:
        x, y = path[-1]
        target_x, target_y = min(food, key=lambda p: (abs(p[0] - x) + abs(p[1] - y), p[0], p[1]))
        # Traverse vertically, then horizontally. Visiting other food on the way
        # also consumes it, which keeps the animation physically consistent.
        while y != target_y:
            y += 1 if y < target_y else -1
            path.append((x, y))
            visit((x, y))
        while x != target_x:
            x += 1 if x < target_x else -1
            path.append((x, y))
            visit((x, y))

    # Give the tail time to leave the final occupied cell before looping.
    x, y = path[-1]
    for _ in range(SNAKE_LENGTH + 5):
        x += 1 if x < columns + SNAKE_LENGTH else 0
        # Repeated positions are okay during the final stationary hold.
        path.append((x, y))
    return path, visited_at


def _css_keyframe(name: str, positions: list[tuple[int, int]], total_frames: int, offset: int) -> str:
    values = []
    for frame in range(total_frames + 1):
        p = positions[frame + (SNAKE_LENGTH - 1) - offset]
        tx = PAD + p[0] * PITCH
        ty = PAD + p[1] * PITCH
        values.append(f"{100 * frame / total_frames:.3f}%{{transform:translate({tx}px,{ty}px)}}")
    return f"@keyframes {name}{{{''.join(values)}}}"


def render_svg(days: list[Day], theme: str) -> str:
    if theme not in THEMES:
        raise ValueError("Unknown theme")
    palette = THEMES[theme]
    columns = max(d.week for d in days) + 1
    w = 2 * PAD + columns * PITCH
    h = 2 * PAD + 7 * PITCH
    path, visited_at = plan_route(days, columns)
    frames = len(path) - SNAKE_LENGTH
    duration = max(5.0, (frames + 1) * SECONDS_PER_FRAME)
    colored = {(d.week, d.weekday): d for d in days if d.level > 0}
    css = [
        f".tile{{fill:{palette['empty']};shape-rendering:geometricPrecision}}",
        ".food{animation-timing-function:linear;animation-iteration-count:infinite}",
        f".snake{{fill:{palette['snake']};animation-timing-function:steps(1,end);animation-iteration-count:infinite}}",
    ]
    for k, (point, index) in enumerate(sorted(visited_at.items(), key=lambda item: item[1])):
        d = colored[point]
        frame = index - (SNAKE_LENGTH - 1)
        percentage = max(0.0, 100.0 * frame / frames)
        p_before = max(0.0, percentage - 0.025)
        color = palette[f"c{d.level}"]
        css.append(f"@keyframes eat{k}{{0%,{p_before:.3f}%{{fill:{color}}}{percentage:.3f}%,100%{{fill:{palette['empty']}}}}}")
        css.append(f".food{k}{{fill:{color};animation-name:eat{k};animation-duration:{duration:.3f}s}}")
    for offset in range(SNAKE_LENGTH):
        css.append(_css_keyframe(f"move{offset}", path, frames, offset))
        css.append(f".snake{offset}{{animation-name:move{offset};animation-duration:{duration:.3f}s}}")

    # Unique XML elements with no active content, script, external resource or links.
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img" aria-label="Animated GitHub contributions snake">',
        "<title>GitHub contribution snake</title>",
        f"<style>{''.join(css)}</style>",
    ]
    food_ids = {point: k for k, (point, _) in enumerate(sorted(visited_at.items(), key=lambda item: item[1]))}
    for col in range(columns):
        for row in range(7):
            x, y = PAD + PITCH * col, PAD + PITCH * row
            point = col, row
            if point in colored:
                d = colored[point]
                out.append(f'<rect class="tile food food{food_ids[point]}" x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2"/>')
            else:
                out.append(f'<rect class="tile" x="{x}" y="{y}" width="{CELL}" height="{CELL}" rx="2"/>')
    for i in range(SNAKE_LENGTH - 1, -1, -1):
        out.append(f'<rect class="snake snake{i}" x="0" y="0" width="{CELL}" height="{CELL}" rx="3"/>')
    out.append("</svg>")
    svg = "\n".join(out) + "\n"
    ET.fromstring(svg)  # Validate before writing; no external resources parsed.
    return svg


def generate(login: str, token: str, output_dir: Path) -> tuple[int, int]:
    calendar = fetch_calendar(login, token)
    total, days = parse_days(calendar)
    output_dir.mkdir(parents=True, exist_ok=True)
    # Atomic writes prevent publishing partially generated SVG files.
    for theme, filename in (("light", "github-snake.svg"), ("dark", "github-snake-dark.svg")):
        output = output_dir / filename
        temp = output.with_suffix(".svg.tmp")
        temp.write_text(render_svg(days, theme), encoding="utf-8")
        temp.replace(output)
    return total, len([d for d in days if d.count > 0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user", default="andr4ykaa")
    parser.add_argument("--output-dir", type=Path, default=Path("dist"))
    args = parser.parse_args(argv)
    token = os.environ.get("SNAKE_TOKEN", "")
    if not token:
        print("ERROR: SNAKE_TOKEN is missing", file=sys.stderr)
        return 1
    try:
        total, occupied = generate(args.user, token, args.output_dir)
    except (RuntimeError, ValueError, KeyError, TypeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Created 2 SVGs; total contributions: {total}; occupied days: {occupied}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
