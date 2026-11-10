#!/usr/bin/env python3
"""Render the profile README cards into cards/*.svg.

Every card is a self-contained SVG meant to be shown through an <img> tag, so
it only uses what works there:
  * one inline <style> block with CSS keyframes (entrance motion),
  * SMIL <animate>/<animateMotion> for a few decorative loops,
  * Bricolage Grotesque, instanced at the portfolio's weights/widths, subset
    per card with pyftsubset and embedded as base64 WOFF data URIs.

Motion rules
  * Every element's *base* style is its final frame. Entrance keyframes run
    from a hidden state to that base, so `animation: none` leaves the final
    frame on screen.
  * CSS cannot stop SMIL, but `display: none` removes an element reliably, so
    SMIL only ever lives in `.motion` groups. Under prefers-reduced-motion
    those are hidden and a `.still` stand-in (same thing, not moving) shows.
  * Delays are global (hero first, contribution graph last), so the page plays
    top to bottom even though each card is a separate image.

STATIC=1 writes the final frame only: no keyframes, no SMIL. It is exactly
what a reduced-motion visitor sees, which makes it the thing to screenshot.

Input:  data/contributions.json  (refreshed daily by .github/workflows/main.yml)
Output: cards/*.svg
Needs:  fonttools  (pip install fonttools)
"""

from __future__ import annotations

import base64
import calendar
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "contributions.json"
OUT = ROOT / "cards"
FONT = ROOT / "fonts" / "BricolageGrotesque-Variable.ttf"
PORTRAIT = ROOT / "assets" / "portrait.jpg"  # optional: delete it to drop the portrait

STATIC = os.environ.get("STATIC") == "1"

# ---------------------------------------------------------------- content ---
# Mirrors the portfolio (aryenmukundam-me.vercel.app): src/data/profile.js and
# src/data/projects.js. Keep the README links in step with these.

PROFILE = {
    "first": "Aryen",
    "last": "Mukundam",
    "handle": "aryen1101",
    "place": "Bengaluru, India",
    "summary": "I build AI developer tools, retrieval pipelines and the full-stack products around them.",
}

TILES = [
    {"slug": "website", "label": "Website", "value": "aryenmukundam-me.vercel.app"},
    {"slug": "linkedin", "label": "LinkedIn", "value": "in/aryen-mukundam"},
    {"slug": "leetcode", "label": "LeetCode", "value": "500+ problems solved"},
]

# GitHub repo name -> how the portfolio presents it.
FEATURED = [
    {"repo": "PRHawk", "title": "PRHawk", "kind": "AI developer tool",
     "pitch": "A GitHub App that reviews every pull request and posts line-level feedback, using the team’s own conventions."},
    {"repo": "Notebook_LLM", "title": "NotebookLLM", "kind": "Retrieval-augmented generation",
     "pitch": "Upload a document and question it, with every answer graded before you see it."},
    {"repo": "Search_Typeahead", "title": "Search Typeahead", "kind": "Distributed systems",
     "pitch": "Search-as-you-type suggestions served from a sharded cache in about a millisecond."},
    {"repo": "Bricks", "title": "Bricks Dashboard", "kind": "Education platform",
     "pitch": "The learning platform behind Bricks Education, built for how its schools actually ran classes."},
]

# ------------------------------------------------------------------ brand ---
# Ground Station tokens (portfolio src/styles/tokens.css).
INK = "#0b1c3b"
RAISED = "#10264b"
SUNK = "#081630"
RULE = "#24446f"
GRID = "#96b8eb"  # used at 6.5% opacity, as --grid
PAPER = "#eaf0f8"
HAZE = "#a3b6d4"
DIM = "#7a91b5"
FOIL = "#f4b43c"
EASE = "cubic-bezier(0.23, 1, 0.32, 1)"

# Font styles, as instanced axis locations (hero name = 700 weight, 75% width).
STYLES = {
    "d": {"wght": 700, "wdth": 75, "opsz": 96},   # display
    "m": {"wght": 500, "wdth": 100, "opsz": 32},  # medium
    "t": {"wght": 400, "wdth": 100, "opsz": 14},  # text
    "l": {"wght": 600, "wdth": 100, "opsz": 12},  # label
}

# Each card's place in the page-wide sequence (seconds). Kept short on purpose:
# a browser starts an <img> SVG's clock when it is first painted, so a card
# below the fold waits its full delay *after* it scrolls into view.
SEQ = {
    "hero": 0.0,
    "tile": 0.35,      # + 0.1 per tile
    "activity": 0.6,
    "repo": 0.8,       # + 0.08 per repo
    "graph": 1.1,
}

# ------------------------------------------------------------------ fonts ---

_tmp = Path(tempfile.mkdtemp(prefix="cards-fonts-"))


@lru_cache(maxsize=None)
def instance(style: str) -> Path:
    """Pin the variable font to one style's axes and cache it as a static TTF."""
    path = _tmp / f"{style}.ttf"
    font = instancer.instantiateVariableFont(TTFont(FONT), STYLES[style])
    font.save(path)
    return path


@lru_cache(maxsize=None)
def metrics(style: str):
    font = TTFont(instance(style))
    return font.getBestCmap(), font["hmtx"].metrics, font["head"].unitsPerEm


def has_glyph(ch: str) -> bool:
    return ord(ch) in metrics("t")[0]


def measure(text: str, style: str, size: float, tracking: float = 0.0) -> float:
    """Advance width in user units (kerning ignored, so it errs slightly wide)."""
    cmap, hmtx, upm = metrics(style)
    units = sum(hmtx[cmap[ord(c)]][0] if ord(c) in cmap else upm * 0.5 for c in text)
    return units * size / upm + tracking * size * len(text)


def wrap(text: str, style: str, size: float, width: float, max_lines: int = 99) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if line and measure(trial, style, size) > width:
            lines.append(line)
            line = word
        else:
            line = trial
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and measure(lines[-1] + "…", style, size) > width:
            lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
        lines[-1] += "…"
    return lines


def woff_data_uri(style: str, chars: set[str]) -> str:
    """Subset one style to exactly the characters a card uses (pyftsubset)."""
    text_file = _tmp / f"{style}-text.txt"
    out_file = _tmp / f"{style}-sub.woff"
    text_file.write_text("".join(sorted(chars | {" "})), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "fontTools.subset", str(instance(style)),  # == pyftsubset
         f"--text-file={text_file}", "--flavor=woff", f"--output-file={out_file}",
         "--layout-features=kern,liga", "--no-hinting", "--desubroutinize",
         "--name-IDs=", "--drop-tables+=DSIG"],
        check=True,
    )
    return "data:font/woff;base64," + base64.b64encode(out_file.read_bytes()).decode()


# ------------------------------------------------------------------- card ---


class Card:
    def __init__(self, name: str, width: int, height: int, start: float, label: str):
        self.name, self.w, self.h, self.start, self.label = name, width, height, start, label
        self.chars: dict[str, set[str]] = defaultdict(set)
        self.defs: list[str] = []
        self.parts: list[str] = []
        self.rules: list[str] = []
        self._n = 0

    # -- building blocks
    def add(self, markup: str):
        self.parts.append(markup)

    def text(self, x, y, s, style="t", size=16, fill=PAPER, anchor="start", tracking=0.0,
             cls="", attrs=""):
        self.chars[style].update(s)
        ls = f' letter-spacing="{tracking * size:.2f}"' if tracking else ""
        klass = f"{style} {cls}".strip()
        return (f'<text x="{x:.1f}" y="{y:.1f}" class="{klass}" font-size="{size}" fill="{fill}" '
                f'text-anchor="{anchor}"{ls}{attrs}>{escape(s)}</text>')

    def anim(self, keyframes: str, delay: float, dur: float = 0.9, ease: str = EASE, extra: str = "") -> str:
        """Register an entrance animation; returns the class to put on the element."""
        self._n += 1
        cls = f"a{self._n}"
        self.rules.append(f".{cls}{{animation:{keyframes} {dur}s {ease} {self.start + delay:.2f}s both;{extra}}}")
        return cls

    def motion(self, markup: str) -> str:
        """Decorative SMIL loop: hidden under reduced motion, absent when STATIC."""
        return "" if STATIC else f'<g class="motion">{markup}</g>'

    def still(self, markup: str) -> str:
        """Stand-in for a .motion element, shown when motion is off."""
        return markup if STATIC else f'<g class="still">{markup}</g>'

    def frame(self, eyebrow: str | None = None):
        """Blueprint card: ink field, 48px drafting grid, registration marks."""
        self.defs.append(
            f'<pattern id="grid" width="48" height="48" patternUnits="userSpaceOnUse">'
            f'<path d="M48 0H0V48" fill="none" stroke="{GRID}" stroke-opacity="0.065" stroke-width="1"/></pattern>'
            f'<clipPath id="card"><rect width="{self.w}" height="{self.h}" rx="10"/></clipPath>'
        )
        # The whole frame waits for this card's turn, so later cards don't sit
        # on the page as empty shells while earlier ones play.
        self.add(f'<g class="{self.anim("fade", 0.0, 0.5)}">'
                 f'<g clip-path="url(#card)"><rect width="{self.w}" height="{self.h}" fill="{INK}"/>'
                 f'<rect width="{self.w}" height="{self.h}" fill="url(#grid)"/></g>'
                 f'<rect x="0.5" y="0.5" width="{self.w - 1}" height="{self.h - 1}" rx="9.5" fill="none" stroke="{RULE}"/></g>')
        marks = []
        for cx, cy in ((14, 14), (self.w - 14, 14), (14, self.h - 14), (self.w - 14, self.h - 14)):
            marks.append(f'<path d="M{cx - 4} {cy}h8M{cx} {cy - 4}v8" stroke="{RULE}" stroke-width="1"/>')
        self.add(f'<g class="{self.anim("fade", 0.0, 0.6)}">{"".join(marks)}</g>')
        if eyebrow:
            cls = self.anim("fadeUp", 0.05, 0.7)
            self.add(self.text(40, 46, eyebrow.upper(), "l", 12.5, DIM, tracking=0.12, cls=cls))

    # -- output
    def svg(self) -> str:
        faces = "".join(
            f'@font-face{{font-family:"BG-{s}";src:url({woff_data_uri(s, chars)}) format("woff");}}'
            f'.{s}{{font-family:"BG-{s}",ui-sans-serif,system-ui,sans-serif;}}'
            for s, chars in self.chars.items() if chars
        )
        css = [faces, "text{font-synthesis:none;font-kerning:normal}"]
        if not STATIC:
            css += [
                "@keyframes fade{from{opacity:0}}",
                "@keyframes fadeUp{from{opacity:0;transform:translateY(14px)}}",
                "@keyframes slideIn{from{opacity:0;transform:translateX(-18px)}}",
                "@keyframes rise{from{transform:translateY(130px)}}",
                "@keyframes draw{from{stroke-dashoffset:1}}",
                "@keyframes grow{from{transform:scaleY(0)}}",
                "@keyframes pop{from{opacity:0;transform:scale(.2)}}",
                "@keyframes settle{from{opacity:0;transform:scale(1.08)}}",
                "@keyframes nudge{0%,100%{transform:none}50%{transform:translate(3px,-3px)}}",
                "*{transform-box:fill-box}",
                ".still{display:none}",
                *self.rules,
                "@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important}"
                ".motion{display:none}.still{display:inline}}",
            ]
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {self.w} {self.h}" width="{self.w}" height="{self.h}" role="img" '
            f'aria-label="{escape(self.label, {chr(34): "&quot;"})}">'
            f'<title>{escape(self.label)}</title><style>{"".join(css)}</style>'
            f'<defs>{"".join(self.defs)}</defs>{"".join(self.parts)}</svg>\n'
        )

    def save(self):
        (OUT / f"{self.name}.svg").write_text(self.svg(), encoding="utf-8")
        print(f"  cards/{self.name}.svg  {(OUT / f'{self.name}.svg').stat().st_size / 1024:.1f} KB")


def arrow(x, y, size=14, color=FOIL, width=1.8) -> str:
    """North-east arrow (the font has no ↗)."""
    s = size
    return (f'<path d="M{x} {y + s}L{x + s} {y}M{x + s * 0.3} {y}H{x + s}V{y + s * 0.7}" fill="none" '
            f'stroke="{color}" stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round"/>')


def pulse(cx, cy, r=4.5) -> str:
    """Foil 'live' dot. The ring is a SMIL loop; motion-off shows the bare dot."""
    ring = (f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="none" stroke="{FOIL}" stroke-width="1.5">'
            f'<animate attributeName="r" values="{r};{r * 3.2}" dur="2.4s" repeatCount="indefinite"/>'
            f'<animate attributeName="stroke-opacity" values="0.8;0" dur="2.4s" repeatCount="indefinite"/></circle>')
    return f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{FOIL}"/>' + (f'<g class="motion">{ring}</g>' if not STATIC else "")


# ------------------------------------------------------------------- data ---


def load():
    user = json.loads(DATA.read_text(encoding="utf-8"))["data"]["user"]
    cal = user["contributionsCollection"]["contributionCalendar"]
    weeks = [w["contributionDays"] for w in cal["weeks"]]
    days = [d for w in weeks for d in w]
    repos = {r["name"]: r for r in user["repositories"]["nodes"]}
    return cal["totalContributions"], weeks, days, repos


def streaks(days):
    counts = [d["contributionCount"] for d in days]
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    # Today isn't over: a zero today doesn't break the streak yet.
    i = len(counts) - 1
    if counts[i] == 0:
        i -= 1
    current = 0
    while i >= 0 and counts[i]:
        current += 1
        i -= 1
    return current, longest


def monthly(days, n=12):
    """Last n calendar months ending with the current (partial) month."""
    last = dt.date.fromisoformat(days[-1]["date"])
    keys, y, m = [], last.year, last.month
    for _ in range(n):
        keys.append((y, m))
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    keys.reverse()
    totals = dict.fromkeys(keys, 0)
    for d in days:
        k = tuple(map(int, d["date"][:7].split("-")))
        if k in totals:
            totals[k] += d["contributionCount"]
    return [(k, totals[k]) for k in keys], last


def fmt_day(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{calendar.day_abbr[d.weekday()]}, {d.day} {calendar.month_abbr[d.month]} {d.year}"


def clean(s: str) -> str:
    """Drop characters the font can't draw (emoji, box glyphs) and tidy spaces."""
    return " ".join("".join(c for c in (s or "") if has_glyph(c)).split())


# ------------------------------------------------------------------ cards ---


def hero_card():
    W, H = 1000, 400
    c = Card("hero", W, H, SEQ["hero"], f"{PROFILE['first']} {PROFILE['last']}. {PROFILE['summary']}")
    c.frame(f"{PROFILE['handle']}  ·  {PROFILE['place']}")
    portrait = PORTRAIT.exists()
    text_w = 590 if portrait else 860

    # Orbit behind the name, as on the portfolio hero.
    c.defs.append('<path id="orbit" d="M40 196a300 74 -7 1 0 600 0a300 74 -7 1 0 -600 0" pathLength="1"/>')
    c.add(f'<use href="#orbit" xlink:href="#orbit" fill="none" stroke="{FOIL}" stroke-opacity="0.55" '
          f'stroke-width="1.4" stroke-dasharray="1" class="{c.anim("draw", 0.35, 2.2, extra="stroke-dashoffset:0")}"/>')

    # Name: each line rises out of its own mask.
    size, x0 = 116, 36
    for i, (word, y, dx) in enumerate(((PROFILE["first"], 160, 0), (PROFILE["last"], 256, size * 0.62))):
        c.defs.append(f'<clipPath id="line{i}"><rect x="0" y="{y - size}" width="{W}" height="{size * 1.12}"/></clipPath>')
        cls = c.anim("rise", 0.1 + i * 0.12, 1.3)
        c.add(f'<g clip-path="url(#line{i})">{c.text(x0 + dx, y, word, "d", size, PAPER, tracking=-0.035, cls=cls)}</g>')

    # Satellite riding the orbit (decorative SMIL loop) + its stand-in.
    sat = (f'<rect x="-9" y="-2.5" width="6" height="5" fill="{HAZE}"/><rect x="3" y="-2.5" width="6" height="5" fill="{HAZE}"/>'
           f'<rect x="-3" y="-3.5" width="6" height="7" rx="1" fill="{FOIL}"/>')
    c.add(c.motion(f'<g class="{c.anim("fade", 1.6, 0.8)}"><g>{sat}'
                   f'<animateMotion dur="16s" repeatCount="indefinite" rotate="auto">'
                   f'<mpath href="#orbit" xlink:href="#orbit"/></animateMotion></g></g>'))
    c.add(c.still(f'<g transform="translate(612 140) rotate(-30)">{sat}</g>'))

    # One line about me.
    lines = wrap(PROFILE["summary"], "m", 25, text_w)
    for i, line in enumerate(lines):
        c.add(c.text(40, 318 + i * 33, line, "m", 25, PAPER, tracking=-0.015, cls=c.anim("fadeUp", 0.7 + i * 0.08)))

    if portrait:
        pw, ph = 262, 310
        px, py = W - 40 - pw, 45
        data = base64.b64encode(PORTRAIT.read_bytes()).decode()
        c.defs.append(f'<clipPath id="pic"><rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="6"/></clipPath>')
        cls = c.anim("settle", 0.45, 1.4)
        c.add(f'<g clip-path="url(#pic)"><image class="{cls}" x="{px}" y="{py}" width="{pw}" height="{ph}" '
              f'preserveAspectRatio="xMidYMid slice" href="data:image/jpeg;base64,{data}" '
              f'xlink:href="data:image/jpeg;base64,{data}"/></g>')
        c.add(f'<rect x="{px + 0.5}" y="{py + 0.5}" width="{pw - 1}" height="{ph - 1}" rx="6" fill="none" '
              f'stroke="{PAPER}" stroke-opacity="0.18"/>')
        c.add(c.text(px, py + ph + 24, "Bengaluru, at night", "t", 13, DIM, cls=c.anim("fade", 1.2)))
    c.save()


def tile_card(i, tile):
    W, H = 360, 180
    c = Card(f"tile-{tile['slug']}", W, H, SEQ["tile"] + i * 0.1, f"{tile['label']}: {tile['value']}")
    c.frame()
    c.add(c.text(28, 46, f"0{i + 1}", "l", 13, FOIL, tracking=0.12, cls=c.anim("fade", 0.1)))
    c.add(f'<g class="{c.anim("fadeUp", 0.2)}"><g class="{c.anim("nudge", 1.4, 1.2, "ease-in-out")}">'
          f'{arrow(W - 46, 30, 15)}</g></g>')
    c.add(c.text(28, 112, tile["label"], "d", 40, PAPER, tracking=-0.02, cls=c.anim("fadeUp", 0.15)))
    value = tile["value"]
    size = 16
    while measure(value, "t", size) > W - 56 and size > 12:
        size -= 0.5
    c.add(c.text(28, 146, value, "t", size, HAZE, cls=c.anim("fadeUp", 0.25)))
    c.add(f'<path d="M28 160H{W - 28}" stroke="{FOIL}" stroke-opacity="0.6" pathLength="1" stroke-dasharray="1" '
          f'class="{c.anim("draw", 0.35, 1.1, extra="stroke-dashoffset:0")}"/>')
    c.save()


def activity_card(total, days):
    W, H = 1000, 540
    months, last = monthly(days)
    current, longest = streaks(days)
    active = sum(1 for d in days if d["contributionCount"])
    busiest = max(days, key=lambda d: (d["contributionCount"], d["date"]))
    top = max(v for _, v in months) or 1

    c = Card("activity", W, H, SEQ["activity"],
             f"{total:,} contributions in the last year. Current streak {current} days, longest {longest} days, "
             f"{active} active days, busiest day {fmt_day(busiest['date'])} with {busiest['contributionCount']}.")
    c.frame("Contributions per month")
    c.add(c.text(40, 100, f"{total:,} contributions in the last year", "d", 44, PAPER, tracking=-0.02,
                 cls=c.anim("fadeUp", 0.1)))

    # Bars
    x0, x1, base, tall = 40, W - 40, 350, 180
    slot = (x1 - x0) / len(months)
    bw = min(46, slot * 0.58)
    c.add(f'<path d="M{x0} {base + 0.5}H{x1}" stroke="{RULE}"/>')
    for j, ((y, m), v) in enumerate(months):
        cx = x0 + slot * (j + 0.5)
        h = max(2, v / top * tall) if v else 0
        partial = (y, m) == (last.year, last.month)
        peak = v == top
        delay = 0.3 + j * 0.06
        if h:
            fill = FOIL if peak else HAZE
            op = "1" if peak else "0.38"
            dash = ' stroke-dasharray="4 3"' if partial else ""
            c.add(f'<g class="{c.anim("grow", delay, 1.1, extra="transform-origin:50% 100%")}">'
                  f'<rect x="{cx - bw / 2:.1f}" y="{base - h:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="2" '
                  f'fill="{fill}" fill-opacity="{op}" stroke="{FOIL if peak else PAPER}" stroke-opacity="{0 if peak else 0.5}"{dash}/>'
                  f'<path d="M{cx - bw / 2:.1f} {base - h:.1f}h{bw:.1f}" stroke="{FOIL if peak else PAPER}" stroke-width="2"/></g>')
        c.add(c.text(cx, base - h - 10, f"{v:,}", "m", 15, FOIL if peak else HAZE, "middle",
                     cls=c.anim("fade", delay + 0.5, 0.6)))
        label = calendar.month_abbr[m]
        c.add(c.text(cx, base + 26, label, "l", 13, PAPER if partial else DIM, "middle", cls=c.anim("fade", delay)))
        if partial or m == 1 or j == 0:
            c.add(c.text(cx, base + 44, "so far" if partial else f"{y}", "t", 12, DIM, "middle",
                         cls=c.anim("fade", delay)))

    # Stats
    c.add(f'<path d="M40 418.5H{W - 40}" stroke="{RULE}"/>')
    stats = [
        ("Current streak", f"{current} day{'s' if current != 1 else ''}", "and counting" if current else "starts today"),
        ("Longest streak", f"{longest} days", "in the last year"),
        ("Active days", f"{active}", f"of the last {len(days)}"),
        ("Busiest day", f"{busiest['contributionCount']}", fmt_day(busiest["date"])),
    ]
    col = (W - 80) / 4
    for k, (lab, val, sub) in enumerate(stats):
        x = 40 + k * col
        d = 1.0 + k * 0.1
        c.add(c.text(x + (18 if k == 0 else 0), 452, lab.upper(), "l", 12, DIM, tracking=0.12, cls=c.anim("fade", d)))
        c.add(c.text(x, 494, val, "d", 38, PAPER, tracking=-0.02, cls=c.anim("fadeUp", d + 0.05)))
        c.add(c.text(x, 518, sub, "t", 14, HAZE, cls=c.anim("fadeUp", d + 0.1)))
    c.add(f'<g class="{c.anim("fade", 1.0)}">{pulse(45, 448, 4)}</g>')  # live: the streak is ongoing
    c.save()


def repo_card(i, spec, repo):
    W, H = 490, 260
    c = Card(f"repo-{spec['repo'].lower()}", W, H, SEQ["repo"] + i * 0.08,
             f"{spec['title']}: {spec['pitch']}")
    c.frame(spec["kind"])
    pushed = dt.date.fromisoformat(repo["pushedAt"][:10])
    c.add(c.text(W - 40, 46, str(pushed.year), "l", 12.5, DIM, "end", tracking=0.12, cls=c.anim("fade", 0.1)))
    c.add(c.text(40, 100, spec["title"], "d", 46, PAPER, tracking=-0.025, cls=c.anim("slideIn", 0.1, 1.0)))
    c.add(f'<g class="{c.anim("fadeUp", 0.25)}">{arrow(W - 54, 72, 16)}</g>')
    for j, line in enumerate(wrap(clean(spec["pitch"] or repo["description"]), "t", 17, W - 80, 3)):
        c.add(c.text(40, 136 + j * 25, line, "t", 17, HAZE, cls=c.anim("fadeUp", 0.3 + j * 0.06)))

    # Footer: language, stars, forks, last push.
    y = 228
    c.add(f'<path d="M40 {y - 26}.5H{W - 40}" stroke="{RULE}" pathLength="1" stroke-dasharray="1" '
          f'class="{c.anim("draw", 0.4, 1.0, extra="stroke-dashoffset:0")}"/>')
    items, x = [], 40.0
    lang = (repo.get("primaryLanguage") or {}).get("name")
    if lang:
        items.append(f'<circle cx="{x + 5}" cy="{y - 5}" r="5" fill="none" stroke="{HAZE}" stroke-width="1.5"/>')
        items.append(c.text(x + 16, y, lang, "t", 14, HAZE))
        x += 16 + measure(lang, "t", 14) + 22
    star = (f'<path transform="translate({x:.1f} {y - 12})" d="M6.5 0l1.9 4.2 4.6.5-3.4 3.1 1 4.5-4.1-2.4-4 2.4 1-4.5L0 4.7l4.6-.5z" '
            f'fill="none" stroke="{HAZE}" stroke-width="1.2" stroke-linejoin="round"/>')
    items += [star, c.text(x + 19, y, f"{repo['stargazerCount']}", "t", 14, HAZE)]
    x += 19 + measure(str(repo["stargazerCount"]), "t", 14) + 22
    fork = (f'<g transform="translate({x:.1f} {y - 12})" fill="none" stroke="{HAZE}" stroke-width="1.2">'
            f'<circle cx="2.5" cy="2" r="2"/><circle cx="10.5" cy="2" r="2"/><circle cx="6.5" cy="11" r="2"/>'
            f'<path d="M2.5 4v1.5q0 2 2 2h4q2 0 2-2V4M6.5 7.5V9"/></g>')
    items += [fork, c.text(x + 19, y, f"{repo['forkCount']}", "t", 14, HAZE)]
    items.append(c.text(W - 40, y, f"Updated {calendar.month_abbr[pushed.month]} {pushed.year}", "t", 14, DIM, "end"))
    c.add(f'<g class="{c.anim("fade", 0.55)}">{"".join(items)}</g>')
    c.save()


def graph_card(total, weeks):
    W = 1000
    cols = len(weeks)
    left, right = 74, 40
    pitch = (W - left - right) / cols
    cell = pitch * 0.8
    top = 108
    H = int(top + 7 * pitch + 66)
    last = weeks[-1][-1]["date"]

    c = Card("graph", W, H, SEQ["graph"], f"Contribution graph: {total:,} contributions in the last year.")
    c.frame("Contribution graph")
    c.add(c.text(W - 40, 46, f"Updated {fmt_day(last)}", "t", 13, DIM, "end", cls=c.anim("fade", 0.1)))

    def blend(a):
        f = [int(FOIL[i:i + 2], 16) for i in (1, 3, 5)]
        b = [int(INK[i:i + 2], 16) for i in (1, 3, 5)]
        return "#" + "".join(f"{round(b[k] + (f[k] - b[k]) * a):02x}" for k in range(3))

    level = {"NONE": RAISED, "FIRST_QUARTILE": blend(0.28), "SECOND_QUARTILE": blend(0.5),
             "THIRD_QUARTILE": blend(0.75), "FOURTH_QUARTILE": FOIL}

    for r, name in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        c.add(c.text(40, top + r * pitch + cell * 0.78, name, "l", 12, DIM, cls=c.anim("fade", 0.1)))
    seen = None
    for w, week in enumerate(weeks):
        first = dt.date.fromisoformat(week[0]["date"])
        if first.month != seen and (w == 0 or first.day <= 7) and w < cols - 2:
            seen = first.month
            c.add(c.text(left + w * pitch, top - 12, calendar.month_abbr[first.month], "l", 12, DIM,
                         cls=c.anim("fade", 0.15 + w * 0.012)))
        cls = c.anim("pop", 0.2 + w * 0.016, 0.5, extra="transform-origin:50% 50%")
        rects = []
        for d in week:
            x, y = left + w * pitch, top + d["weekday"] * pitch
            rects.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{cell:.1f}" height="{cell:.1f}" rx="2.5" '
                         f'fill="{level[d["contributionLevel"]]}"/>')
        c.add(f'<g class="{cls}">{"".join(rects)}</g>')

    # Today: foil outline, with a live pulse.
    d = weeks[-1][-1]
    tx, ty = left + (cols - 1) * pitch + cell / 2, top + d["weekday"] * pitch + cell / 2
    c.add(f'<g class="{c.anim("fade", 0.3 + cols * 0.016)}">')
    c.add(f'<rect x="{tx - cell / 2 - 2:.1f}" y="{ty - cell / 2 - 2:.1f}" width="{cell + 4:.1f}" height="{cell + 4:.1f}" '
          f'rx="3.5" fill="none" stroke="{FOIL}" stroke-width="1.4"/>')
    c.add(c.motion(f'<rect x="{tx - cell / 2 - 2:.1f}" y="{ty - cell / 2 - 2:.1f}" width="{cell + 4:.1f}" height="{cell + 4:.1f}" '
                   f'rx="3.5" fill="none" stroke="{FOIL}" stroke-width="1.4">'
                   f'<animate attributeName="stroke-opacity" values="0.9;0" dur="2.4s" repeatCount="indefinite"/>'
                   f'<animate attributeName="stroke-width" values="1.4;7" dur="2.4s" repeatCount="indefinite"/></rect>'))
    c.add("</g>")

    # Legend
    ly = H - 34
    lx = W - 40 - measure("More", "l", 12) - 6 - 5 * pitch
    parts = [c.text(lx - 10, ly + cell * 0.8, "Less", "l", 12, DIM, "end")]
    for k, key in enumerate(level):
        parts.append(f'<rect x="{lx + k * pitch:.1f}" y="{ly}" width="{cell:.1f}" height="{cell:.1f}" rx="2.5" fill="{level[key]}"/>')
    parts.append(c.text(lx + 5 * pitch + 6, ly + cell * 0.8, "More", "l", 12, DIM))
    c.add(f'<g class="{c.anim("fade", 1.1)}">{"".join(parts)}</g>')
    c.add(c.text(40, ly + cell * 0.8, f"{sum(x['contributionCount'] for x in weeks[-1])} this week", "t", 13, HAZE,
                 cls=c.anim("fade", 1.1)))
    c.save()


# ------------------------------------------------------------------- main ---


def main():
    OUT.mkdir(exist_ok=True)
    total, weeks, days, repos = load()
    print(f"Rendering cards ({'static final frame' if STATIC else 'animated'}) from {DATA.relative_to(ROOT)}")
    hero_card()
    for i, tile in enumerate(TILES):
        tile_card(i, tile)
    activity_card(total, days)
    for i, spec in enumerate(FEATURED):
        repo = repos.get(spec["repo"])
        if not repo:
            print(f"  ! {spec['repo']} not found in the data; skipping its card", file=sys.stderr)
            continue
        repo_card(i, spec, repo)
    graph_card(total, weeks)


if __name__ == "__main__":
    main()
