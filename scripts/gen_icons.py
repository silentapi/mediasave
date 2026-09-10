#!/usr/bin/env python3
"""Generate the PWA icons without Pillow: a rounded dark tile with a download arrow.

Usage: python3 scripts/gen_icons.py   (writes server/static/icons/{192,512,maskable}.png)
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

BG = (17, 17, 17)
FG = (79, 156, 255)
TRAY = (61, 220, 132)


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + r for r in rows)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def render(size: int, maskable: bool) -> bytes:
    s = size
    r = s * (0 if maskable else 0.18)  # corner radius (0 for maskable: full bleed)
    pad = s * (0.28 if maskable else 0.2)
    # arrow geometry
    shaft_w = s * 0.11
    cx = s / 2
    top = pad
    head_top = s * 0.5
    head_half = s * 0.22
    tip = s * 0.68
    tray_y0, tray_y1 = s * 0.74, s * 0.80
    tray_x0, tray_x1 = pad, s - pad
    rows = []
    for y in range(s):
        row = bytearray()
        for x in range(s):
            # rounded-rect alpha
            a = 255
            if r:
                dx = max(r - x, x - (s - 1 - r), 0)
                dy = max(r - y, y - (s - 1 - r), 0)
                if dx * dx + dy * dy > r * r:
                    a = 0
            px, py = x + 0.5, y + 0.5
            color = BG
            if abs(px - cx) <= shaft_w / 2 and top <= py <= head_top + s * 0.02:
                color = FG
            elif head_top <= py <= tip and abs(px - cx) <= head_half * (1 - (py - head_top) / (tip - head_top)):
                color = FG
            elif tray_y0 <= py <= tray_y1 and tray_x0 <= px <= tray_x1:
                color = TRAY
            row += bytes(color) + bytes([a])
        rows.append(bytes(row))
    return _png(s, s, rows)


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "server" / "static" / "icons"
    out.mkdir(parents=True, exist_ok=True)
    (out / "192.png").write_bytes(render(192, False))
    (out / "512.png").write_bytes(render(512, False))
    (out / "maskable.png").write_bytes(render(512, True))
    print("wrote icons to", out)


if __name__ == "__main__":
    main()
