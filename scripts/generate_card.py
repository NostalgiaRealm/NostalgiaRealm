#!/usr/bin/env python3
"""Render the Nostalgia Realm neofetch-style profile card (assets/nostalgiarealm-card.svg).

Standard library only, so it runs on a bare GitHub Actions runner.

Environment:
  GH_TOKEN / GITHUB_TOKEN  GitHub token for live GitHub stats (optional; falls back to cache)
  YOUTUBE_API_KEY          YouTube Data API v3 key for live channel stats (optional)
  OFFLINE=1                skip all network calls and render from data/stats.json
"""
from __future__ import annotations

import base64
import datetime as dt
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUT = ROOT / "assets" / "nostalgiarealm-card.svg"
FONT = ROOT / "assets" / "fonts" / "VT323-subset.woff2"
UA = "nostalgiarealm-profile-card"

# ---------------------------------------------------------------- layout --
CARD_W = 1000
ADV = 0.4                      # VT323 advance width (em)
A_FS = 15                      # ASCII art font size
A_LH = round(A_FS * 0.82, 2)   # ASCII line height
A_X, A_Y = 36, 90              # ASCII art top-left
R_FS = 19                      # info column font size
R_CW = R_FS * ADV              # 7.6 px per char
R_LH = 20.5
R_X, R_Y = 444, 98             # first baseline of the info column
R_COLS = 68                    # info column width in chars

# brand palette (from the five channel icons + neon banner)
C = {
    "dim": "#5d4c94", "key": "#72e6ff", "val": "#f3ecff", "num": "#ffd166",
    "add": "#4dffa6", "del": "#ff5c8a", "user": "#ff5fd2", "host": "#72e6ff",
    "at": "#ffffff", "path": "#b48cff",
    "red": "#ff3b5c", "orange": "#ffa62b", "blue": "#3d7bff",
    "teal": "#19d3c0", "purple": "#b84dff", "pink": "#ff4fb8",
}


# ----------------------------------------------------------------- stats --
def http_json(url, *, data=None, headers=None, retries=2):
    hdrs = {"User-Agent": UA, "Accept": "application/json"}
    hdrs.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            if e.code in (500, 502, 503) and attempt < retries:
                time.sleep(2)
                continue
            raise


def fetch_github(login, token, cache):
    gql = "https://api.github.com/graphql"
    auth = {"Authorization": f"bearer {token}"}
    q = """query($login:String!){ user(login:$login){
        createdAt followers{totalCount}
        repositories(ownerAffiliations:OWNER, privacy:PUBLIC, first:100){
          totalCount nodes{ name isFork stargazerCount } }
        repositoriesContributedTo(first:1, includeUserRepositories:false,
          contributionTypes:[COMMIT, PULL_REQUEST, ISSUE, REPOSITORY]){ totalCount }
    }}"""
    _, res = http_json(gql, data={"query": q, "variables": {"login": login}}, headers=auth)
    if res.get("errors"):
        raise RuntimeError(res["errors"])
    u = res["data"]["user"]
    repos = u["repositories"]["nodes"]

    # commits: contributionsCollection is limited to 1-year windows
    start_year = int(u["createdAt"][:4])
    now = dt.datetime.now(dt.timezone.utc)
    parts = []
    for y in range(start_year, now.year + 1):
        parts.append(
            f'y{y}: contributionsCollection(from:"{y}-01-01T00:00:00Z", to:"{y}-12-31T23:59:59Z")'
            "{ totalCommitContributions restrictedContributionsCount }")
    q2 = "query($login:String!){ user(login:$login){ " + " ".join(parts) + " } }"
    _, res2 = http_json(gql, data={"query": q2, "variables": {"login": login}}, headers=auth)
    if res2.get("errors"):
        raise RuntimeError(res2["errors"])
    commits = sum(v["totalCommitContributions"] for v in res2["data"]["user"].values())

    # lines of code: additions/deletions authored by `login` in each owned repo
    loc_cache = cache.get("loc_by_repo", {})
    loc = {}
    for repo in repos:
        name = repo["name"]
        url = f"https://api.github.com/repos/{login}/{name}/stats/contributors"
        got = None
        for _ in range(8):                      # 202 = GitHub still computing
            status, body = http_json(url, headers={"Authorization": f"token {token}"})
            if status == 200 and isinstance(body, list):
                got = [0, 0]
                for entry in body:
                    if (entry.get("author") or {}).get("login", "").lower() == login.lower():
                        got = [sum(w["a"] for w in entry["weeks"]), sum(w["d"] for w in entry["weeks"])]
                break
            if status == 204:
                got = [0, 0]
                break
            time.sleep(3)
        loc[name] = got if got is not None else loc_cache.get(name, [0, 0])
    return {
        "repos": u["repositories"]["totalCount"],
        "contributed": u["repositoriesContributedTo"]["totalCount"],
        "stars": sum(r["stargazerCount"] for r in repos),
        "commits": commits,
        "followers": u["followers"]["totalCount"],
        "loc_add": sum(a for a, _ in loc.values()),
        "loc_del": sum(d for _, d in loc.values()),
        "loc_by_repo": loc,
    }


def fetch_youtube(handle, key):
    qs = urllib.parse.urlencode({"part": "statistics", "forHandle": handle.lstrip("@"), "key": key})
    _, res = http_json(f"https://www.googleapis.com/youtube/v3/channels?{qs}")
    st = res["items"][0]["statistics"]
    return {
        "subscribers": int(st["subscriberCount"]),
        "videos": int(st["videoCount"]),
        "views": int(st["viewCount"]),
        "source": "YouTube Data API",
    }


def load_stats(profile):
    path = DATA / "stats.json"
    stats = json.loads(path.read_text())
    if os.environ.get("OFFLINE") == "1":
        return stats
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        try:
            stats["github"] = fetch_github(profile["github_login"], token, stats["github"])
        except Exception as e:  # keep the cached numbers, never break the card
            print(f"warning: GitHub stats failed, using cache: {e}", file=sys.stderr)
    key = os.environ.get("YOUTUBE_API_KEY")
    if key:
        try:
            stats["youtube"] = fetch_youtube(profile["youtube_handle"], key)
        except Exception as e:
            print(f"warning: YouTube stats failed, using cache: {e}", file=sys.stderr)
    stats["updated"] = dt.date.today().isoformat()
    path.write_text(json.dumps(stats, indent=2) + "\n")
    return stats


# --------------------------------------------------------------- helpers --
def uptime(start: str) -> str:
    s = dt.date.fromisoformat(start)
    t = dt.date.today()
    months = (t.year - s.year) * 12 + (t.month - s.month) - (t.day < s.day)
    y, m = divmod(months, 12)
    return f"{y} years, {m} month{'s' if m != 1 else ''}"


def compact(n: int) -> str:
    for div, suf in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= div:
            v = n / div
            return (f"{v:.1f}".rstrip("0").rstrip(".") if v < 100 else f"{v:.0f}") + suf
    return str(n)


def kv(key, value, width):
    """'. Key: ........ value' as styled runs, padded to `width` chars."""
    vals = value if isinstance(value, list) else [("val", value)]
    vlen = sum(len(t) for _, t in vals)
    dots = max(1, width - len(key) - vlen - 5)
    return [("dim", ". "), ("key", key), ("dim", ":"), ("dim", " " + "." * dots + " ")] + vals


def wrap_value(value, cap_first, cap_rest):
    """Split a long value at ', ' (falling back to spaces) so each line fits its cap."""
    out = [value]
    for sep, tail in ((", ", ","), (" ", "")):
        items = value.split(sep)
        out, cur = [], []
        for i, it in enumerate(items):
            cap = cap_first if not out else cap_rest
            trial = sep.join(cur + [it]) + ("" if i == len(items) - 1 else tail)
            if cur and len(trial) > cap:
                out.append(sep.join(cur) + tail)
                cur = [it]
            else:
                cur.append(it)
        out.append(sep.join(cur))
        caps = [cap_first] + [cap_rest] * (len(out) - 1)
        if all(len(line) <= c for line, c in zip(out, caps)):
            break
    return out


def kv_lines(key, value, width):
    """kv() that wraps long string values onto right-aligned continuation lines."""
    cap_first = width - len(key) - 6          # ". " + key + ": " + min 1 dot + " "
    if not isinstance(value, str) or len(value) <= cap_first:
        return [kv(key, value, width)]
    parts = wrap_value(value, cap_first, width - 2)
    return [kv(key, parts[0], width)] + [
        [("dim", " " * max(0, width - len(p))), ("val", p)] for p in parts[1:]]


def run_len(runs):
    return sum(len(t) for _, t in runs)


def text_el(x, y, runs, *, size, cls_prefix="", extra="", cw=None):
    cw = cw if cw is not None else size * ADV
    n = run_len(runs)
    spans = "".join(f'<tspan class="{cls_prefix}{c}">{escape(t)}</tspan>' for c, t in runs if t)
    return (f'<text x="{x}" y="{y:.2f}" font-size="{size}" textLength="{n * cw:.1f}" '
            f'lengthAdjust="spacingAndGlyphs"{extra}>{spans}</text>')


# ------------------------------------------------------------ pixel icons --
# 16x16 maps, 2 px per pixel. '.' = transparent. Frames animate via CSS.
PIX = {
    "K": "#1b1238", "W": "#fff6fd", "S": "#b9b3e0", "D": "#3a2d6e",
    "P": "#ffc2e8", "L": "#c9b8f0", "G": "#8f88c0", "C": "#72e6ff",
    "R": "#ff3b5c", "H": "#2a1f55", "Y": "#ffd166", "M": "#ff8ad4",
}

FLOPPY = """
KKKKKKKKKKKKKK..
KPPPKSSSSSSKPPK.
KPPPKSSDDSSKPPPK
KPPPKSSDDSSKPPPK
KPPPKSSDDSSKPPPK
KPPPKSSSSSSKPPPK
KPPPPKKKKKKPPPPK
KPPPPPPPPPPPPPPK
KPPWWWWWWWWWWPPK
KPPWLLLLLLLLWPPK
KPPWWWWWWWWWWPPK
KPPWLLLLLLLLWPPK
KPPWWWWWWWWWWPPK
KPPWWWWWWWWWWPPK
KKKKKKKKKKKKKKKK
"""

MONITOR = """
.KKKKKKKKKKKKKK.
.KWWWWWWWWWWWWK.
.KWKKKKKKKKKKWK.
.KWKHHHHHHHHKWK.
.KWKHCCCCHHHKWK.
.KWKHHHHHHHHKWK.
.KWKHMMMHHHHKWK.
.KWKHHHHHHHHKWK.
.KWKKKKKKKKKKWK.
.KWWWWWWWWWGRWK.
.KKKKKKKKKKKKKK.
......KWWK......
....KKKWWKKK....
...KWWWWWWWWK...
...KKKKKKKKKK...
"""
MONITOR_CURSOR = [(6, 9), (6, 10)]       # (row, col) of blinking screen cursor

VHS = """
................
KKKKKKKKKKKKKKKK
KWWWWWWWWWWWWWWK
KWYYYYYYYYYYYYWK
KWWWWWWWWWWWWWWK
KWKKKKKKKKKKKKWK
KWKSWWSDDSWWSKWK
KWKWHHWDDWHHWKWK
KWKWHHWDDWHHWKWK
KWKSWWSDDSWWSKWK
KWKKKKKKKKKKKKWK
KWWWWKKKKKKWWWWK
KKKKKKKKKKKKKKKK
"""
# spinning spokes: a cyan glint walks around each spool (row, col offsets)
VHS_SPOOLS = [(6, 3), (6, 9)]
GLINT = [(0, 1), (1, 3), (3, 2), (2, 0)]

CAMCORDER = """
................
......KKKKKK....
......KSSSSK....
KKK.KKKKKKKKKKK.
KLLKKWWWWWWWWWWK
KLCLKWWWWWWWWWWK
KLLKKWWWWWWDDDWK
KKK.KWWWWWWWWWWK
....KWWWWWWWWWWK
....KKKKKKKKKKKK
......KK....KK..
"""
CAM_LED = (4, 13)


def parse_map(s):
    return [r for r in s.strip("\n").split("\n")]


def pixels_to_paths(cells, ox, oy, p=2):
    """cells: dict (row,col)->color. Returns merged-run <path>s grouped by color."""
    by_color = {}
    for (r, c), col in cells.items():
        by_color.setdefault(col, []).append((r, c))
    out = []
    for col, pts in by_color.items():
        pts.sort()
        d = []
        i = 0
        while i < len(pts):
            r, c = pts[i]
            j = i
            while j + 1 < len(pts) and pts[j + 1] == (r, pts[j][1] + 1):
                j += 1
            w = pts[j][1] - c + 1
            d.append(f"M{ox + c * p} {oy + r * p}h{w * p}v{p}h-{w * p}z")
            i = j + 1
        out.append(f'<path fill="{PIX.get(col, col)}" d="{"".join(d)}"/>')
    return "".join(out)


def map_cells(rows):
    cells = {}
    for r, row in enumerate(rows):
        for c, ch in enumerate(row):
            if ch != ".":
                cells[(r, c)] = ch
    return cells


def cd_frames():
    """Procedural pixel CD with 4 rotating sheen frames."""
    import math
    base, frames = {}, [{} for _ in range(4)]
    for r in range(16):
        for c in range(16):
            dx, dy = c + 0.5 - 8, r + 0.5 - 8
            d = math.hypot(dx, dy)
            if d > 7.95 or d < 1.3:
                continue
            if d > 7.15:
                base[(r, c)] = "K"
            elif d < 2.2:
                base[(r, c)] = "K"
            elif d < 3.1:
                base[(r, c)] = "G"
            else:
                base[(r, c)] = "W" if d < 5.4 else "S"
                ang = math.degrees(math.atan2(dy, dx)) % 180
                for f in range(4):
                    a0 = f * 45
                    diff = min(abs(ang - a0), 180 - abs(ang - a0))
                    if diff < 14 and 3.4 < d < 6.9:
                        frames[f][(r, c)] = "M" if d > 5.3 else "C"
    return base, frames


def icon(cx, cy, fill, idx):
    """One neon ring with pixel art inside. idx selects the glyph."""
    ox, oy = cx - 16, cy - 16
    ring = (f'<circle cx="{cx}" cy="{cy}" r="27" fill="url(#ring{idx})" stroke="#fff" '
            f'stroke-width="2.5" filter="url(#glow-{idx})"/>')
    art = ""
    if idx == 0:
        base, frames = cd_frames()
        art = pixels_to_paths(base, ox, oy)
        for f, cells in enumerate(frames):
            art += f'<g class="spin spin{f}">{pixels_to_paths(cells, ox, oy)}</g>'
    elif idx == 1:
        art = pixels_to_paths(map_cells(parse_map(FLOPPY)), ox, oy + 1)
    elif idx == 2:
        cells = map_cells(parse_map(MONITOR))
        art = pixels_to_paths(cells, ox, oy + 1)
        cur = {rc: "C" for rc in MONITOR_CURSOR}
        art += f'<g class="blink">{pixels_to_paths(cur, ox, oy + 1)}</g>'
    elif idx == 3:
        cells = map_cells(parse_map(VHS))
        art = pixels_to_paths(cells, ox, oy + 2)
        for f, (dr, dc) in enumerate(GLINT):
            fc = {(r0 + dr, c0 + dc): "C" for (r0, c0) in VHS_SPOOLS}
            art += f'<g class="spin spin{f}">{pixels_to_paths(fc, ox, oy + 2)}</g>'
    elif idx == 4:
        cells = map_cells(parse_map(CAMCORDER))
        art = pixels_to_paths(cells, ox, oy + 5)
        art += f'<g class="rec">{pixels_to_paths({CAM_LED: "R"}, ox, oy + 5)}</g>'
    return ring + art


# ------------------------------------------------------------ CRT glitch --
GLITCH_S = 8          # one loop: calm, small tear, v-hold hiccup, big glitch + snow, tear


def keyframes(name, prop, events, default):
    """Discrete keyframes: every value holds until the next event (pair with steps(1,end))."""
    pts = [(0, default)] + sorted(events) + [(100, default)]
    return f"@keyframes {name}{{" + "".join(f"{p:g}%{{{prop}:{v}}}" for p, v in pts) + "}"


def tx_(x, y=0):
    return f"translate({x}px,{y}px)"


def glitch_css(n_bands, screen_h):
    loop = f"{GLITCH_S}s steps(1,end) infinite"
    css = []
    # whole screen content: vertical-hold slips + horizontal jitter
    css.append(keyframes("jit", "transform", [
        (47.0, tx_(0, -3)), (47.5, tx_(0, 1)), (48.0, tx_(0)),
        (70.8, tx_(2)), (71.4, tx_(-3, -9)), (72.0, tx_(0, 5)), (72.6, tx_(3, -2)), (73.2, tx_(0)),
        (75.4, tx_(-2)), (75.9, tx_(0))], tx_(0)))
    # brightness drops while the signal breaks up
    css.append(keyframes("sop", "opacity", [
        (47.2, .55), (47.6, 1), (70.8, .7), (71.2, 1), (72.8, .4), (73.6, .85), (74.4, .5),
        (75.2, 1), (88.0, .8), (88.3, 1), (92.6, .75), (92.9, 1)], 1))
    # constant mains-hum flicker on its own, unsynced cycle
    css.append(keyframes("flk", "opacity", [
        (13, .9), (13.6, 1), (41, .94), (41.5, 1), (77, .88), (77.4, 1)], 1))
    # RGB split: cyan left, magenta right; wider during bursts
    gh = [(22.0, -7, 0), (22.6, -4, 1), (23.2, -3, 0), (47.0, -5, -3), (47.6, -3, 0),
          (71.0, -9, 0), (71.8, 5, -1), (72.6, -12, 0), (73.6, -6, 2), (74.8, -9, 0), (76.0, -3, 0),
          (92.6, -6, 0), (93.2, -3, 0)]
    css.append(keyframes("ghc", "transform", [(p, tx_(x, y)) for p, x, y in gh], tx_(-3)))
    css.append(keyframes("ghm", "transform", [(p, tx_(-x, -y)) for p, x, y in gh], tx_(3)))
    # tear slices (band index -> [(pct, dx)])
    tears = {
        0: [(71.0, 6), (71.6, -4), (72.4, 0)],
        1: [(22.0, 9), (22.5, 0), (72.0, -11), (72.8, 3), (73.6, 0)],
        2: [(71.2, 14), (71.9, -6), (73.0, 0), (92.6, 10), (93.0, 0)],
        3: [(22.2, -7), (22.9, 0), (71.6, -9), (72.6, 0), (74.2, 6), (74.8, 0)],
        4: [(47.1, 5), (47.5, 0), (71.0, -5), (71.5, 12), (72.2, -3), (73.4, 0)],
        5: [(22.4, 5), (22.8, 0), (72.2, 8), (73.0, -8), (73.8, 0)],
        6: [(71.4, -6), (72.0, 4), (72.9, 0), (93.0, -7), (93.3, 0)],
    }
    for k in range(n_bands):
        css.append(keyframes(f"tr{k}", "transform", [(p, tx_(dx)) for p, dx in tears.get(k, [])], tx_(0)))
        css.append(f".tr{k}{{animation:tr{k} {loop}}}")
    # ASCII snow frames + a white flash when the big glitch hits
    snow = {0: [(72.4, 1), (72.9, 0), (73.9, 1), (74.4, 0)],
            1: [(22.3, .45), (22.7, 0), (72.9, 1), (73.4, 0), (74.4, 1), (74.9, 0)],
            2: [(73.4, 1), (73.9, 0), (74.9, .8), (75.3, 0)]}
    for k, ev in snow.items():
        css.append(keyframes(f"sn{k}", "opacity", ev, 0))
        css.append(f".sn{k}{{animation:sn{k} {loop}}}")
    css.append(keyframes("fl", "opacity", [(70.8, .22), (71.0, 0), (92.6, .1), (92.75, 0)], 0))
    css += [
        f".jit{{animation:jit {loop}}}", f".sop{{animation:sop {loop}}}",
        ".flk{animation:flk 2.9s steps(1,end) infinite}",
        f".gh-c{{transform:{tx_(-3)};animation:ghc {loop}}}",
        f".gh-m{{transform:{tx_(3)};animation:ghm {loop}}}",
        ".sn,.fl{opacity:0}", f".fl{{animation:fl {loop}}}",
        ".sa{fill:#e8ecff;fill-opacity:.85}.sb{fill:#93a6ff;fill-opacity:.85}",
        f"@keyframes hb{{from{{transform:translateY(0)}}to{{transform:translateY({screen_h + 16:.0f}px)}}}}",
        ".hb{animation:hb 3.6s linear infinite}",
        "@media (prefers-reduced-motion:reduce){*{animation:none!important}}",
    ]
    return css


# ----------------------------------------------------------------- render --
def render(profile, stats):
    ascii_data = json.loads((DATA / "ascii.json").read_text())
    meta = ascii_data["meta"]
    a_cw = A_FS * ADV
    rnd = random.Random(1987)

    # ---- info column lines
    W = R_COLS
    lines = []   # (kind, runs, accent)
    user, host = profile["prompt_user"], profile["prompt_host"]
    lines.append(("head", [("user", user), ("at", "@"), ("host", host)], "pink"))
    for k, v in profile["system"]:
        v = v.replace("{uptime}", f'{uptime(profile["channel_start"])} ({profile["channel_start_label"]})')
        lines += [("kv", runs, None) for runs in kv_lines(k, v, W)]
    lines.append(("blank", [], None))
    lines.append(("sec", [("s-orange", f'- {profile["rebuilds_title"]} '),
                          ("dim", ":: "), ("val", profile["rebuilds_note"]), ("dim", " ")], "orange"))
    for k, v in profile["rebuilds"]:
        lines += [("kv", runs, None) for runs in kv_lines(k, v, W)]
    lines.append(("sec", [("s-teal", "- Contact ")], "teal"))
    for k, v in profile["contact"]:
        lines += [("kv", runs, None) for runs in kv_lines(k, v, W)]

    yt, gh = stats["youtube"], stats["github"]
    half = (W - 3) // 2
    lines.append(("sec", [("s-red", "- YouTube Stats ")], "red"))
    lines.append(("kv", kv("Subscribers", [("num", compact(yt["subscribers"]))], half)
                  + [("dim", " | ")] + kv("Videos", [("num", f'{yt["videos"]:,}')], W - half - 3), None))
    lines.append(("kv", kv("Views", [("num", f'{yt["views"]:,}')], W), None))
    lines.append(("sec", [("s-purple", "- GitHub Stats ")], "purple"))
    lines.append(("kv", kv("Repos", [("num", f'{gh["repos"]}'), ("val", " {Contributed: "),
                                     ("num", f'{gh["contributed"]}'), ("val", "}")], half)
                  + [("dim", " | ")] + kv("Stars", [("num", f'{gh["stars"]:,}')], W - half - 3), None))
    lines.append(("kv", kv("Commits", [("num", f'{gh["commits"]:,}')], half)
                  + [("dim", " | ")] + kv("Followers", [("num", f'{gh["followers"]:,}')], W - half - 3), None))
    net = gh["loc_add"] - gh["loc_del"]
    lines.append(("kv", kv("Lines of Code on GitHub", [
        ("num", f"{net:,}"), ("val", " ( "), ("add", f'{gh["loc_add"]:,}++'), ("val", ", "),
        ("del", f'{gh["loc_del"]:,}--'), ("val", " )")], W), None))

    last_y = R_Y + (len(lines) - 1) * R_LH
    H = int(last_y + 74)

    # ---- defs
    font_b64 = base64.b64encode(FONT.read_bytes()).decode()
    colors = sorted({col for row in ascii_data["rows"] for _, col in row if col})
    a_cls = {col: f"a{i}" for i, col in enumerate(colors)}
    css = [
        "@font-face{font-family:'NR VT323';src:url(data:font/woff2;base64," + font_b64 + ") format('woff2');}",
        "text{font-family:'NR VT323','VT323','Courier New',monospace;white-space:pre;}",
    ]
    css += [f".{c}{{fill:{v}}}" for c, v in C.items()]
    css += [f".s-{k}{{fill:{C[k]}}}" for k in ("orange", "teal", "red", "purple", "pink")]
    css += [f".{a_cls[col]}{{fill:{col}}}" for col in colors]
    css += [
        "@keyframes tw{0%,100%{opacity:.15}50%{opacity:1}}",
        ".tw{animation:tw 3.2s ease-in-out infinite}",
        "@keyframes blink{0%,49%{opacity:1}50%,100%{opacity:0}}",
        ".blink{animation:blink 1.06s steps(1) infinite}",
        ".rec{animation:blink 1.4s steps(1) infinite}",
        "@keyframes spin{0%,24.9%{opacity:1}25%,100%{opacity:0}}",
        ".spin{opacity:0;animation:spin .6s steps(1) infinite}",
        ".spin1{animation-delay:.15s}.spin2{animation-delay:.3s}.spin3{animation-delay:.45s}",
        f"@keyframes roll{{from{{transform:translateY(-140px)}}to{{transform:translateY({H}px)}}}}",
        ".roll{animation:roll 7s linear infinite}",
        "@keyframes hum{0%,100%{opacity:.92}50%{opacity:1}}",
        ".hum{animation:hum 4s ease-in-out infinite}",
    ]
    ring_cols = [C["red"], C["orange"], C["blue"], C["teal"], C["purple"]]
    defs = []
    for i, col in enumerate(ring_cols):
        defs.append(f'<radialGradient id="ring{i}" cx="50%" cy="40%" r="65%">'
                    f'<stop offset="0" stop-color="{col}" stop-opacity=".95"/>'
                    f'<stop offset="1" stop-color="{col}" stop-opacity=".55"/></radialGradient>')
        defs.append(f'<filter id="glow-{i}" x="-50%" y="-50%" width="200%" height="200%">'
                    f'<feDropShadow dx="0" dy="0" stdDeviation="4" flood-color="{col}" flood-opacity=".9"/>'
                    f'<feDropShadow dx="0" dy="0" stdDeviation="1.5" flood-color="#fff" flood-opacity=".6"/></filter>')
    defs.append(
        '<linearGradient id="sky" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#07041a"/><stop offset=".55" stop-color="#110a33"/>'
        '<stop offset="1" stop-color="#1e0b45"/></linearGradient>'
        '<linearGradient id="neon" x1="0" y1="0" x2="1" y2="1">'
        f'<stop offset="0" stop-color="{C["pink"]}"/><stop offset=".5" stop-color="{C["purple"]}"/>'
        f'<stop offset="1" stop-color="{C["key"]}"/></linearGradient>'
        '<linearGradient id="floorfade" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset="1" stop-color="#fff" stop-opacity=".55"/>'
        '</linearGradient>'
        '<mask id="floormask"><rect x="0" y="0" width="100%" height="100%" fill="url(#floorfade)"/></mask>'
        '<linearGradient id="horizon" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0" stop-color="{C["pink"]}" stop-opacity="0"/>'
        f'<stop offset=".5" stop-color="{C["pink"]}" stop-opacity=".22"/>'
        f'<stop offset="1" stop-color="{C["purple"]}" stop-opacity="0"/></linearGradient>'
        '<radialGradient id="vignette" cx="50%" cy="48%" r="72%">'
        '<stop offset=".62" stop-color="#000" stop-opacity="0"/><stop offset="1" stop-color="#000" stop-opacity=".55"/>'
        '</radialGradient>'
        '<linearGradient id="rollbar" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#b9a6ff" stop-opacity=".07"/>'
        '<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
        '<pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse">'
        '<rect width="4" height="1" fill="#000" fill-opacity=".28"/></pattern>'
        '<filter id="bloom" x="-5%" y="-5%" width="110%" height="110%">'
        '<feGaussianBlur stdDeviation="1.6" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        '<filter id="neonglow" x="-5%" y="-5%" width="110%" height="110%">'
        '<feGaussianBlur stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        '<linearGradient id="t1" x1="0" y1="0" x2="0" y2="1"><stop offset=".2" stop-color="#ffffff"/><stop offset="1" stop-color="#ffd2ef"/></linearGradient>'
        '<linearGradient id="t2" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#ffe3f4"/><stop offset="1" stop-color="#ff6cc4"/></linearGradient>'
        f'<clipPath id="card"><rect width="{CARD_W}" height="{H}" rx="18"/></clipPath>'
    )
    for name, col in (("pink", C["pink"]), ("orange", C["orange"]), ("teal", C["teal"]),
                      ("red", C["red"]), ("purple", C["purple"])):
        defs.append(f'<linearGradient id="rule-{name}" x1="0" y1="0" x2="1" y2="0">'
                    f'<stop offset="0" stop-color="{col}"/><stop offset="1" stop-color="{col}" stop-opacity=".05"/>'
                    '</linearGradient>')

    body = []
    # ---- background: sky, stars, horizon, perspective grid
    body.append(f'<rect width="{CARD_W}" height="{H}" fill="url(#sky)"/>')
    stars = []
    # keep stars out of text so they never read as stray dots/punctuation
    no_star = [
        (R_X - 6, R_Y - 20, R_X + R_COLS * R_CW + 6, last_y + 8),
        (A_X - 4, A_Y - 4, A_X + meta["cols"] * a_cw + 4, A_Y + meta["rows"] * A_LH + 4),
        (20, 20, 560, 60), (CARD_W - 150, 20, CARD_W - 20, 60),
    ]
    i = 0
    while len(stars) < 120:
        i += 1
        x, y = rnd.uniform(8, CARD_W - 8), rnd.uniform(8, H - 150)
        if any(b[0] <= x <= b[2] and b[1] <= y <= b[3] for b in no_star):
            continue
        s = rnd.choice((1, 1, 1.5, 2))
        col = rnd.choice(("#ffffff", "#ffffff", "#ffd6f5", "#bfe9ff", "#d9c8ff"))
        op = rnd.uniform(.25, .8)
        cls = ""
        if i % 6 == 0:
            cls = f' class="tw" style="animation-delay:-{rnd.uniform(0, 3.2):.2f}s;animation-duration:{rnd.uniform(2.2, 4.5):.2f}s"'
        stars.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{s}" height="{s}" fill="{col}" opacity="{op:.2f}"{cls}/>')
    body.append("<g>" + "".join(stars) + "</g>")

    hz = H - 150
    body.append(f'<rect x="0" y="{hz - 40}" width="{CARD_W}" height="80" fill="url(#horizon)"/>')
    grid = []
    vx = CARD_W / 2
    for k in range(-22, 23):
        grid.append(f'M{vx + k * 9:.1f} {hz}L{vx + k * 95:.1f} {H}')
    for i in range(1, 12):
        t = (i / 11) ** 2.1
        y = hz + (H - hz) * t
        grid.append(f'M0 {y:.1f}H{CARD_W}')
    body.append(f'<g mask="url(#floormask)"><path d="{"".join(grid)}" stroke="{C["pink"]}" '
                f'stroke-width="1.2" fill="none" opacity=".75"/>'
                f'<path d="M0 {hz}H{CARD_W}" stroke="{C["key"]}" stroke-width="1.5" opacity=".9"/></g>')

    # ---- top prompt + VHS OSD
    body.append(text_el(36, 46, [("user", user), ("at", "@"), ("host", host), ("path", ":~$ "),
                                 ("val", "neofetch --retro")], size=21))
    body.append('<g class="hum" filter="url(#bloom)">'
                f'<path d="M{CARD_W - 132} 31l13 8-13 8z" fill="#fff"/>'
                + text_el(CARD_W - 112, 46, [("val", "PLAY")], size=24)
                + text_el(CARD_W - 64, 46, [("dim", "SP")], size=18) + "</g>")
    body.append(f'<rect x="36" y="60" width="{CARD_W - 72}" height="1.2" fill="url(#neon)" opacity=".6"/>')

    # ---- ASCII CRT (left): static bezel + live, glitching screen
    x0, y0, x1, y1 = meta["bbox"]
    cw_src, ch_src = meta["cell"]
    sx, sy = a_cw / cw_src, A_LH / ch_src

    def src_to_svg(xs, ys):
        return A_X + (xs - x0) * sx, A_Y + (ys - y0) * sy

    scr = meta["screen"]
    cdx, cdy, cdr = meta["cd"]

    def cell_center(r, c):
        return x0 + (c + 0.5) * cw_src, y0 + (r + 0.5) * ch_src

    def on_screen(r, c):
        xs, ys = cell_center(r, c)
        return (scr[0] <= xs <= scr[2] and scr[1] <= ys <= scr[3]
                and math.hypot(xs - cdx, ys - cdy) > cdr + 6)

    # source-px boxes cleared of ASCII so real text can sit on top
    title_box = (196, 286, 652, 462)
    cd_box, rom_box = (630, 507, 714, 549), (606, 641, 734, 685)
    clear = (title_box, cd_box, rom_box)

    bezel_lines, screen_lines, spans = [], [], {}
    for r, row in enumerate(ascii_data["rows"]):
        cells = []
        for txt, col in row:
            for ch in txt:
                xs, ys = cell_center(r, len(cells))
                if any(b[0] <= xs <= b[2] and b[1] <= ys <= b[3] for b in clear):
                    ch = " "
                cells.append((ch, a_cls[col] if col else "dim"))
        scr_cols = [c for c in range(len(cells)) if on_screen(r, c)]
        pieces = [(0, len(cells), bezel_lines)]
        if scr_cols:
            c0, c1 = scr_cols[0], scr_cols[-1] + 1
            assert c1 - c0 == len(scr_cols), "screen cells must be contiguous per row"
            spans[r] = (c0, c1)
            pieces = [(0, c0, bezel_lines), (c0, c1, screen_lines), (c1, len(cells), bezel_lines)]
        y = A_Y + (r + 0.78) * A_LH
        for a, b, dest in pieces:
            seg = cells[a:b]
            if not seg or all(ch == " " for ch, _ in seg):
                continue
            runs = []
            for ch, cls in seg:
                if runs and runs[-1][0] == cls:
                    runs[-1][1] += ch
                else:
                    runs.append([cls, ch])
            dest.append(text_el(A_X + a * a_cw, y, [tuple(x) for x in runs], size=A_FS))
    body.append('<g filter="url(#bloom)">' + "".join(bezel_lines) + "</g>")

    # screen geometry in SVG space (mask = screen cells minus the CD disc)
    r_lo, r_hi = min(spans), max(spans)
    mx0 = A_X + min(a for a, _ in spans.values()) * a_cw
    mx1 = A_X + max(b for _, b in spans.values()) * a_cw
    my0, my1 = A_Y + r_lo * A_LH - 2, A_Y + (r_hi + 1) * A_LH + 2
    mw, mh = mx1 - mx0, my1 - my0
    (ccx, ccy), ccr = src_to_svg(cdx, cdy), (cdr + 6) * sx
    defs.append(
        f'<mask id="scrmask" maskUnits="userSpaceOnUse" x="{mx0 - 2:.1f}" y="{my0 - 2:.1f}" '
        f'width="{mw + 4:.1f}" height="{mh + 4:.1f}">'
        f'<rect x="{mx0:.1f}" y="{my0:.1f}" width="{mw:.1f}" height="{mh:.1f}" rx="7" fill="#fff"/>'
        f'<circle cx="{ccx:.1f}" cy="{ccy:.1f}" r="{ccr:.1f}" fill="#000"/></mask>'
        '<filter id="wmglow" x="-15%" y="-35%" width="130%" height="170%">'
        '<feGaussianBlur stdDeviation="3.5" result="b"/><feMerge><feMergeNode in="b"/>'
        '<feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
        '<linearGradient id="humgrad" x1="0" y1="0" x2="0" y2="1">'
        '<stop offset="0" stop-color="#fff" stop-opacity="0"/><stop offset=".5" stop-color="#dfe6ff" '
        'stop-opacity=".2"/><stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>')

    # the wordmark, as in the logo: NOSTALGIA / =REALM=
    tx, ty1 = src_to_svg(424, 368)
    _, ty2 = src_to_svg(424, 452)
    bar_l0, _ = src_to_svg(206, 0)
    bar_l1, _ = src_to_svg(270, 0)
    bar_r0, _ = src_to_svg(578, 0)
    bar_r1, _ = src_to_svg(642, 0)

    def wordmark(fill1, fill2, bar_fill, stroke=True):
        st = (' stroke="#1b0b3f" stroke-width="4" paint-order="stroke" stroke-linejoin="round"'
              if stroke else "")
        bars = "".join(
            f'<rect x="{a:.1f}" y="{ty2 + dy:.1f}" width="{b - a:.1f}" height="3.2" fill="{bar_fill}"/>'
            for a, b in ((bar_l0, bar_l1), (bar_r0, bar_r1)) for dy in (-21, -13))
        return (f'<g text-anchor="middle"{st}>'
                f'<text x="{tx:.1f}" y="{ty1:.1f}" font-size="44" fill="{fill1}" letter-spacing="1">NOSTALGIA</text>'
                f'<text x="{tx:.1f}" y="{ty2:.1f}" font-size="44" fill="{fill2}" letter-spacing="1">REALM</text>'
                f'</g>{bars}')

    defs.append(f'<g id="wm">{wordmark("url(#t1)", "url(#t2)", C["pink"])}</g>')

    # horizontal tear slices: the wordmark cut into bands that jump sideways
    wm_top, wm_bot = ty1 - 36, ty2 + 8
    cuts = [wm_top + (wm_bot - wm_top) * f for f in (0, .13, .27, .40, .52, .66, .80, 1)]
    bands = list(zip(cuts[:-1], cuts[1:]))
    bands[0], bands[-1] = (my0, bands[0][1]), (bands[-1][0], my1)
    for k, (b0, b1) in enumerate(bands):
        defs.append(f'<clipPath id="band{k}"><rect x="{mx0 - 30:.1f}" y="{b0:.1f}" '
                    f'width="{mw + 60:.1f}" height="{b1 - b0:.2f}"/></clipPath>')
    tears = "".join(f'<g class="tr tr{k}" clip-path="url(#band{k})"><use href="#wm"/></g>'
                    for k in range(len(bands)))

    # chromatic-aberration ghosts (cyan/magenta) peeking out under the wordmark
    ghosts = (f'<g class="gh-c">{wordmark("#19e6ff", "#19e6ff", "#19e6ff", stroke=False)}</g>'
              f'<g class="gh-m">{wordmark("#ff2bd6", "#ff2bd6", "#ff2bd6", stroke=False)}</g>')

    # CRT snow, drawn as ASCII static on the same character grid
    srnd = random.Random(404)
    snow = []
    for f in range(3):
        rows_ = []
        for r, (c0, c1) in sorted(spans.items()):
            s_ = "".join(srnd.choice(".:-=+*#%@") if srnd.random() < .58 else " " for _ in range(c1 - c0))
            rows_.append(text_el(A_X + c0 * a_cw, A_Y + (r + 0.78) * A_LH,
                                 [("sa" if (r + f) % 2 else "sb", s_)], size=A_FS))
        snow.append(f'<g class="sn sn{f}">{"".join(rows_)}</g>')

    body.append(
        '<g mask="url(#scrmask)"><g class="flk"><g class="sop"><g class="jit">'
        f'<g filter="url(#bloom)">{"".join(screen_lines)}</g>{ghosts}'
        f'<g filter="url(#wmglow)">{tears}</g></g></g></g>'
        f'{"".join(snow)}'
        f'<rect class="fl" x="{mx0:.1f}" y="{my0:.1f}" width="{mw:.1f}" height="{mh:.1f}" fill="#e3e9ff"/>'
        f'<rect class="hb" x="{mx0:.1f}" y="{my0 - 16:.1f}" width="{mw:.1f}" height="16" fill="url(#humgrad)"/>'
        '</g>')
    css.extend(glitch_css(len(bands), mh))

    labels = []
    for box, word in ((cd_box, "CD"), (rom_box, "ROM")):
        (bx0, by0), (bx1, by1) = src_to_svg(box[0], box[1]), src_to_svg(box[2], box[3])
        labels.append(f'<rect x="{bx0:.1f}" y="{by0:.1f}" width="{bx1 - bx0:.1f}" height="{by1 - by0:.1f}" '
                      f'rx="{(by1 - by0) / 2:.1f}" fill="#1b1238" stroke="#fff6fd" stroke-width="1.6"/>'
                      f'<text x="{(bx0 + bx1) / 2:.1f}" y="{by1 - 4:.1f}" font-size="18" fill="#fff6fd" '
                      f'text-anchor="middle" letter-spacing="1">{word}</text>')
    body.append(f'<g filter="url(#bloom)">{"".join(labels)}</g>')

    # ---- five channel icons under the CRT
    ascii_bottom = A_Y + meta["rows"] * A_LH
    icy = ascii_bottom + 68
    a_center = A_X + meta["cols"] * a_cw / 2
    icons = [icon(a_center + (i - 2) * 72, icy, ring_cols[i], i) for i in range(5)]
    body.append("".join(icons))
    tag = profile["tagline"]
    body.append(text_el(a_center - (len(tag) + 8) * 16 * ADV / 2, icy + 60,
                        [("dim", "-=[ "), ("pink", tag), ("dim", " ]=-")], size=16))

    # ---- info column
    info = []
    for i, (kind, runs, accent) in enumerate(lines):
        y = R_Y + i * R_LH
        if kind == "blank":
            continue
        if kind in ("head", "sec"):
            n = run_len(runs)
            size = 21 if kind == "head" else R_FS
            cw = size * ADV
            info.append(text_el(R_X, y, runs, size=size, extra=' filter="url(#bloom)"'))
            lx = R_X + n * cw + 6
            info.append(f'<rect x="{lx:.1f}" y="{y - 6:.1f}" width="{R_X + W * R_CW - lx:.1f}" '
                        f'height="2" fill="url(#rule-{accent})"/>')
        else:
            info.append(text_el(R_X, y, runs, size=R_FS, cw=R_CW))
    body.append("".join(info))

    # ---- bottom prompt with blinking cursor + sync stamp
    by = H - 26
    prompt = [("user", user), ("at", "@"), ("host", host), ("path", ":~$ ")]
    body.append(text_el(36, by, prompt, size=21))
    cx = 36 + run_len(prompt) * 21 * ADV + 2
    body.append(f'<rect class="blink" x="{cx:.1f}" y="{by - 16}" width="9" height="18" fill="{C["key"]}"/>')
    stamp = f'last sync {stats.get("updated", dt.date.today().isoformat())} :: auto-updated daily'
    body.append(text_el(CARD_W - 36 - len(stamp) * 16 * ADV, by, [("path", stamp)], size=16,
                        extra=' opacity=".8"'))

    # ---- CRT overlays + neon frame
    overlay = (
        f'<rect width="{CARD_W}" height="{H}" fill="url(#scan)" pointer-events="none"/>'
        f'<rect class="roll" width="{CARD_W}" height="140" fill="url(#rollbar)"/>'
        f'<rect width="{CARD_W}" height="{H}" fill="url(#vignette)"/>'
    )
    frame = (f'<rect x="1.5" y="1.5" width="{CARD_W - 3}" height="{H - 3}" rx="17" fill="none" '
             f'stroke="url(#neon)" stroke-width="2.5" filter="url(#neonglow)"/>')

    alt = (f"{user}@{host} – neofetch-style profile card for Nostalgia Realm: retro PC games, "
           "game preservation and reverse engineering.")
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CARD_W}" height="{H}" '
        f'viewBox="0 0 {CARD_W} {H}" role="img" aria-label="{escape(alt)}" xml:space="preserve">'
        f"<title>{escape(alt)}</title>"
        f"<style>{''.join(css)}</style><defs>{''.join(defs)}</defs>"
        f'<g clip-path="url(#card)">{"".join(body)}{overlay}</g>{frame}</svg>'
    )
    return svg


def main():
    profile = json.loads((DATA / "profile.json").read_text())
    stats = load_stats(profile)
    svg = render(profile, stats)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(svg)
    print(f"wrote {OUT.relative_to(ROOT)} ({len(svg.encode()) / 1024:.1f} KiB)")


if __name__ == "__main__":
    main()
