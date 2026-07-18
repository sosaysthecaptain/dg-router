#!/usr/bin/env python3
"""Generate the toolbar icon (pure stdlib PNG writer, no PIL).

A 48x48 dark tile with a green right-angle "trace" and a via dot — evokes
routing. Green matches the house style (#4bde80).
"""

import os
import zlib
import struct

W = H = 48
BG = (0x1e, 0x1e, 0x1e)
GREEN = (0x4b, 0xde, 0x80)


def _png(path, w, h, pixels):
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type 0 per scanline
        for x in range(w):
            raw += bytes(pixels[y * w + x])

    def chunk(typ, data):
        return (struct.pack(">I", len(data)) + typ + data
                + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit truecolor RGB
    idat = zlib.compress(bytes(raw), 9)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat)
                + chunk(b"IEND", b""))


def build():
    px = [BG] * (W * H)

    def hline(y, x0, x1, t):
        for yy in range(y - t, y + t + 1):
            for xx in range(x0, x1 + 1):
                if 0 <= xx < W and 0 <= yy < H:
                    px[yy * W + xx] = GREEN

    def vline(x, y0, y1, t):
        for xx in range(x - t, x + t + 1):
            for yy in range(y0, y1 + 1):
                if 0 <= xx < W and 0 <= yy < H:
                    px[yy * W + xx] = GREEN

    def dot(cx, cy, r):
        for yy in range(cy - r, cy + r + 1):
            for xx in range(cx - r, cx + r + 1):
                if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                    if 0 <= xx < W and 0 <= yy < H:
                        px[yy * W + xx] = GREEN

    def line(x0, y0, x1, y1, t):
        n = max(abs(x1 - x0), abs(y1 - y0)) or 1
        for s in range(n + 1):
            u = s / n
            cx = round(x0 + (x1 - x0) * u)
            cy = round(y0 + (y1 - y0) * u)
            for yy in range(cy - t, cy + t + 1):
                for xx in range(cx - t, cx + t + 1):
                    if 0 <= xx < W and 0 <= yy < H:
                        px[yy * W + xx] = GREEN

    # octilinear trace with a 45deg bend: horizontal -> 45 -> vertical, via at
    # each end. Shifted right so the trace is centered in the field.
    line(13, 35, 24, 35, 2)     # horizontal in
    line(24, 35, 33, 26, 2)     # 45-degree bend
    line(33, 26, 33, 14, 2)     # vertical up
    dot(13, 35, 4)
    dot(33, 14, 4)
    return px


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "dg_router_plugin", "icon.png")
    _png(out, W, H, build())
    print("wrote", out)
