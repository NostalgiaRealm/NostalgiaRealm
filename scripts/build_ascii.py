#!/usr/bin/env python3
"""One-off: turn the Nostalgia Realm CRT logo into colored ASCII art.

Usage: python scripts/build_ascii.py <logo.png> [cols]
Writes data/ascii.json (rows of [text, color] runs). Needs Pillow.
"""
import json
import sys
from collections import Counter, deque
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
RAMP = " .:-=+*#%@"          # sparse -> dense
CELL_ASPECT = 0.4 / 0.82      # VT323 advance (0.4em) / line height (0.82em)


def background_mask(im):
    """Flood-fill from the border over non-outline pixels: True = background."""
    w, h = im.size
    px = im.load()
    bg = [[False] * w for _ in range(h)]
    q = deque()
    for x in range(w):
        q.extend([(x, 0), (x, h - 1)])
    for y in range(h):
        q.extend([(0, y), (w - 1, y)])
    while q:
        x, y = q.popleft()
        if bg[y][x]:
            continue
        r, g, b = px[x, y]
        # only the dithered blue/purple backdrop is fillable; outline,
        # bezel, CD and screen glow all stop the fill
        if not (g <= 51 and b >= 102 and r <= 153):
            continue
        bg[y][x] = True
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not bg[ny][nx]:
                q.append((nx, ny))
    # second pass: peel the light "sticker" highlight ring that hugs the
    # outline (<= 16 px from the backdrop), so the art starts at the outline
    q = deque((x, y, 0) for y in range(h) for x in range(w) if bg[y][x])
    seen = set()
    while q:
        x, y, d = q.popleft()
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if not (0 <= nx < w and 0 <= ny < h) or bg[ny][nx] or (nx, ny) in seen:
                continue
            r, g, b = px[nx, ny]
            if min(r, g, b) >= 150 and d < 16:
                seen.add((nx, ny))
                bg[ny][nx] = True
                q.append((nx, ny, d + 1))
    return bg


def value(c):
    """Brightness that keeps saturated blues/purples mid-dense instead of empty."""
    return (max(c) + sum(c) / 3) / 2 / 255


def boost(c, amt=0.18):
    """Lift dark/saturated colors so they read on a near-black card."""
    return tuple(min(255, int(v + (255 - v) * amt)) for v in c)


def main():
    src = Path(sys.argv[1])
    cols = int(sys.argv[2]) if len(sys.argv) > 2 else 52
    im = Image.open(src).convert("RGB")
    bg = background_mask(im)
    w, h = im.size
    # bounding box of the object
    xs = [x for y in range(h) for x in range(w) if not bg[y][x]]
    ys = [y for y in range(h) if not all(bg[y])]
    x0, x1, y0, y1 = min(xs), max(xs) + 1, min(ys), max(ys) + 1
    cw = (x1 - x0) / cols
    ch = cw / CELL_ASPECT
    rows = int(round((y1 - y0) / ch))
    px = im.load()
    out = []
    for r in range(rows):
        line = []
        for c in range(cols):
            cx0, cy0 = x0 + c * cw, y0 + r * ch
            cnt = Counter()
            total = obj = 0
            for yy in range(int(cy0), min(h, int(cy0 + ch))):
                for xx in range(int(cx0), min(w, int(cx0 + cw))):
                    total += 1
                    if not bg[yy][xx]:
                        obj += 1
                        cnt[px[xx, yy]] += 1
            if total == 0 or obj / total < 0.35:
                line.append((" ", None))
                continue
            col, _ = cnt.most_common(1)[0]
            if max(col) < 90:                  # outline cell
                line.append((":", "#4a3590"))
                continue
            L = value(col)
            ch_ = RAMP[min(len(RAMP) - 1, max(1, int(L * (len(RAMP) - 1) + 0.5)))]
            line.append((ch_, "#%02x%02x%02x" % boost(col)))
        out.append(line)
    # the CD-ROM is redrawn procedurally so it reads as a clean disc
    import math
    cdx, cdy, cdr = 672, 592, 117          # disc centre/radius in source px
    for r in range(rows):
        for c in range(cols):
            xs, ys = x0 + (c + 0.5) * cw, y0 + (r + 0.5) * ch
            d = math.hypot(xs - cdx, ys - cdy)
            if d > cdr:
                continue
            ang = math.degrees(math.atan2(ys - cdy, xs - cdx)) % 180
            if d > cdr - 22:
                out[r][c] = ("#", "#d9d3f2")
            elif d < 13:
                out[r][c] = (" ", None)
            elif d < 27:
                out[r][c] = ("o", "#8f88c0")
            elif abs(ang - 35) < 16:
                out[r][c] = ("#", "#ff9ad5")
            elif abs(ang - 125) < 14:
                out[r][c] = ("*", "#8ae9ff")
            else:
                out[r][c] = ("%" if d < 80 else "=", "#e9e6f7" if d < 80 else "#c6c0e6")

    # merge runs
    runs = []
    for line in out:
        merged = []
        for ch_, col in line:
            if merged and merged[-1][1] == col:
                merged[-1][0] += ch_
            else:
                merged.append([ch_, col])
        runs.append(merged)
    meta = {"source": src.name, "cols": cols, "rows": rows,
            "bbox": [x0, y0, x1, y1], "cell": [cw, ch],
            # measured in the logo: CRT screen interior and the CD-ROM in front of it
            "screen": [185, 220, 660, 530], "cd": [cdx, cdy, cdr]}
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "ascii.json").write_text(json.dumps({"meta": meta, "rows": runs}))
    for line in out:
        print("".join(c for c, _ in line))
    print(meta)


if __name__ == "__main__":
    main()
