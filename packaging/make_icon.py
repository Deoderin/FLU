"""Значок UGV Quick Setup (.ico) без сторонніх бібліотек: силует НРК на синьому квадраті.

Малюємо кожен розмір окремо зі згладжуванням (4x4 вибірки на піксель), пакуємо в PNG, а PNG — в ICO
(Windows Vista+ читає PNG усередині ICO для будь-якого розміру).
Запуск: python packaging/make_icon.py build/app.ico
"""
from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

SIZES = (16, 24, 32, 48, 64, 256)
BG = (43, 98, 245)    # синій, як кнопки майстра
FG = (255, 255, 255)


def rounded_rect(x: float, y: float, x0: float, y0: float, x1: float, y1: float, r: float) -> bool:
    if not (x0 <= x <= x1 and y0 <= y <= y1):
        return False
    cx = min(max(x, x0 + r), x1 - r)
    cy = min(max(y, y0 + r), y1 - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def segment(x: float, y: float, ax: float, ay: float, bx: float, by: float, w: float) -> bool:
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy)))
    return (x - ax - t * dx) ** 2 + (y - ay - t * dy) ** 2 <= (w / 2) ** 2


def ugv(x: float, y: float) -> bool:
    """Білий силует: корпус, кабіна з камерою, антена, три колеса. Координати 0..1."""
    return (rounded_rect(x, y, 0.16, 0.42, 0.84, 0.60, 0.05)
            or rounded_rect(x, y, 0.30, 0.30, 0.54, 0.43, 0.03)
            or segment(x, y, 0.68, 0.43, 0.76, 0.20, 0.045)
            or circle(x, y, 0.76, 0.19, 0.045)
            or any(circle(x, y, cx, 0.71, 0.085) for cx in (0.29, 0.50, 0.71)))


def render(size: int, ss: int = 4) -> bytes:
    """RGBA рядками, з фільтром PNG «0» на початку кожного рядка."""
    rows = bytearray()
    for py in range(size):
        rows.append(0)
        for px in range(size):
            bg = fg = 0
            for sy in range(ss):
                for sx in range(ss):
                    x, y = (px + (sx + 0.5) / ss) / size, (py + (sy + 0.5) / ss) / size
                    if rounded_rect(x, y, 0.03, 0.03, 0.97, 0.97, 0.20):
                        if ugv(x, y):
                            fg += 1
                        else:
                            bg += 1
            n = bg + fg
            if n == 0:
                rows += b"\0\0\0\0"
                continue
            rows += bytes(round((BG[i] * bg + FG[i] * fg) / n) for i in range(3))
            rows.append(round(255 * n / (ss * ss)))
    return bytes(rows)


def png(size: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # 8 біт, RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(render(size), 9)) + chunk(b"IEND", b"")


def make_icon(path: Path) -> Path:
    images = [png(s) for s in SIZES]
    offset = 6 + 16 * len(images)
    out = bytearray(struct.pack("<HHH", 0, 1, len(images)))
    for size, data in zip(SIZES, images):
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    for data in images:
        out += data
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))
    return path


if __name__ == "__main__":
    print(make_icon(Path(sys.argv[1] if len(sys.argv) > 1 else "build/app.ico")))
