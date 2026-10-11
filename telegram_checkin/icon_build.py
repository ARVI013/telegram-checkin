# -*- coding: utf-8 -*-
"""图标构建：telegram.svg → 多尺寸 telegram.ico（用 Edge 无头渲染 + Pillow 打包）。

只用本机已有能力：Edge 渲染 SVG，Pillow 生成 ICO（PyInstaller 的 --icon 需要 .ico）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
SVG = Path(os.environ.get("TG_ICON_SVG") or (HERE / "telegram.svg"))
SIZES = [16, 24, 32, 48, 64, 128, 256]
EDGES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]


def render_png(out_png: Path, size: int = 256) -> Path:
    if not SVG.is_file():
        raise SystemExit(f"找不到图标源文件：{SVG}")
    html = HERE / "_icon_render.html"
    html.write_text(
        "<html><head><meta charset='utf-8'><style>html,body{margin:0;padding:0;"
        "background:transparent;overflow:hidden}</style></head><body>"
        f"<img src='{SVG.as_uri()}' width='{size}' height='{size}'>"
        "</body></html>", encoding="utf-8")
    exe = next((e for e in EDGES if Path(e).is_file()), None)
    if not exe:
        raise SystemExit("找不到 Microsoft Edge，无法把 SVG 渲染成 PNG")
    out_png = out_png.resolve()
    if out_png.exists():
        out_png.unlink()
    cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
           "--disable-extensions", "--force-device-scale-factor=1",
           "--default-background-color=00000000",
           f"--window-size={size},{size}", f"--screenshot={out_png}", html.as_uri()]
    subprocess.run(cmd, check=True, timeout=180,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if not out_png.is_file() or out_png.stat().st_size == 0:
        raise SystemExit(f"Edge 渲染失败，没有生成 {out_png}")
    return out_png


def build():
    png = render_png(HERE / "telegram_256.png", 256)
    im = Image.open(png).convert("RGBA")
    # 把四角的白/透明底统一处理成透明，圆形图标更干净
    if im.getextrema()[3][0] == 255:
        px = im.load()
        w, h = im.size
        cx, cy, r = w / 2.0, h / 2.0, w / 2.0 - 1
        for y in range(h):
            for x in range(w):
                if (x - cx) ** 2 + (y - cy) ** 2 > r * r:
                    px[x, y] = (0, 0, 0, 0)
    ico = HERE / "telegram.ico"
    im.save(ico, sizes=[(s, s) for s in SIZES])
    print(f"已生成 {ico}（尺寸 {SIZES}）")
    print(f"预览 PNG：{png}")
    return ico


if __name__ == "__main__":
    build()
    sys.exit(0)
