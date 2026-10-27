# -*- coding: utf-8 -*-
"""生成应用图标 ``assets/icon.ico``（多尺寸）。

只依赖 Pillow（打包脚本已用到）；重复执行会覆盖。
用法：``python packaging/make_icon.py``
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "icon.ico"

BG_TOP = (77, 141, 255)
BG_BOT = (32, 78, 178)
INK = (255, 255, 255)


def _font(size: int):
    for name in ("msyhbd.ttc", "msyh.ttc", "arialbd.ttf", "segoeuib.ttf"):
        p = Path("C:/Windows/Fonts") / name
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size)
            except Exception:
                continue
    return ImageFont.load_default()


def _rounded(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 竖直渐变
    for y in range(size):
        t = y / max(1, size - 1)
        color = tuple(int(BG_TOP[i] * (1 - t) + BG_BOT[i] * t) for i in range(3))
        d.line([(0, y), (size, y)], fill=color + (255,))
    # 圆角遮罩
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
    img.putalpha(mask)

    d = ImageDraw.Draw(img)
    # 下载箭头
    cx = size * 0.5
    top = size * 0.20
    shaft_w = max(2, int(size * 0.085))
    d.rounded_rectangle(
        [cx - shaft_w / 2, top, cx + shaft_w / 2, size * 0.50],
        radius=shaft_w / 2, fill=INK + (235,))
    head_w = size * 0.30
    d.polygon([(cx - head_w / 2, size * 0.46), (cx + head_w / 2, size * 0.46),
               (cx, size * 0.63)], fill=INK + (235,))
    # 底部托盘
    d.rounded_rectangle(
        [size * 0.24, size * 0.70, size * 0.76, size * 0.78],
        radius=size * 0.04, fill=INK + (210,))
    return img


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [_rounded(s) for s in sizes]
    imgs[-1].save(OUT, format="ICO", sizes=[(s, s) for s in sizes])
    # 顺带存一张 png 给文档/安装包用
    png = OUT.with_suffix(".png")
    imgs[-1].save(png)
    print(f"icon -> {OUT}  ({OUT.stat().st_size} bytes)  + {png.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
