# -*- coding: utf-8 -*-
"""bloom 的 logo（方案一，只留花，不带气泡尾巴）→ SVG + PNG（32…1024）+ ICO + 横版字标。
用法：python brand/make_logo.py   产物在 brand/ 下。"""
import math
import os
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
DEEP, MAIN, LIGHT, PETAL, PAPER = "#1D4ED8", "#3B82F6", "#60A5FA", "#BFDBFE", "#EFF6FF"


def svg_mark() -> str:
    petals = []
    for k in range(5):
        col = MAIN if k % 2 == 0 else LIGHT
        petals.append(f'<ellipse cx="0" cy="-46" rx="22" ry="40" fill="{col}" transform="rotate({k * 72})"/>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" width="240" height="240">'
        '<title>bloom</title>'
        '<g transform="translate(120,120)">' + "".join(petals) +
        f'<circle r="26" fill="{DEEP}"/><circle r="9" fill="{PAPER}"/>'
        '</g></svg>'
    )


def svg_wordmark() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 640 200" width="640" height="200">'
        '<title>bloom</title>'
        + svg_mark().split("<title>bloom</title>")[1].replace("</svg>", "").replace('translate(120,120)', 'translate(100,100) scale(0.78)') +
        f'<text x="200" y="128" font-family="Inter, Segoe UI, PingFang SC, Microsoft YaHei, sans-serif" font-size="96" font-weight="600" letter-spacing="-3" fill="{DEEP}">bloom</text>'
        f'<text x="204" y="166" font-family="PingFang SC, Microsoft YaHei, sans-serif" font-size="26" fill="{MAIN}">组会 · blue + room</text>'
        '</svg>'
    )


def render_png(size: int, bg=None, zoom: float = 1.0) -> Image.Image:
    """PIL 没有 SVG 渲染器：每片花瓣画在独立透明层上再旋转合成，4 倍超采样抗锯齿。"""
    S = size * 4
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0) if bg is None else bg)
    scale = S / 240 * zoom          # zoom：花在画布里放大多少（1.0 时约占七成；应用图标用 1.34，花瓣几乎顶到边）
    cx, cy = S / 2, S / 2
    for k in range(5):
        layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        rx, ry, dy = 22 * scale, 40 * scale, 46 * scale
        d.ellipse([cx - rx, cy - dy - ry, cx + rx, cy - dy + ry], fill=MAIN if k % 2 == 0 else LIGHT)
        layer = layer.rotate(-72 * k, resample=Image.BICUBIC, center=(cx, cy))
        img = Image.alpha_composite(img, layer)
    d = ImageDraw.Draw(img)
    r = 26 * scale
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DEEP)
    r2 = 9 * scale
    d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2], fill=PAPER)
    return img.resize((size, size), Image.LANCZOS)


def main():
    (HERE / "bloom-mark.svg").write_text(svg_mark(), encoding="utf-8")
    (HERE / "bloom-wordmark.svg").write_text(svg_wordmark(), encoding="utf-8")
    out = HERE / "png"
    out.mkdir(exist_ok=True)
    sizes = [16, 24, 32, 48, 64, 128, 256, 512, 1024]
    imgs = {s: render_png(s) for s in sizes}
    for s, im in imgs.items():
        im.save(out / f"bloom-{s}.png")
    # 口喷鸡 / Tauri 需要的几个名字
    tauri = HERE / "tauri-icons"
    tauri.mkdir(exist_ok=True)
    Z = 1.34                         # 应用图标里的花放大，标题栏那 16 像素才看得清
    big = {s: render_png(s, zoom=Z) for s in (16, 24, 32, 48, 64, 128, 256, 512)}
    big[32].save(tauri / "32x32.png")
    big[64].save(tauri / "64x64.png")
    big[128].save(tauri / "128x128.png")
    big[256].save(tauri / "128x128@2x.png")
    big[512].save(tauri / "icon.png")
    for s, name in [(30, "Square30x30Logo.png"), (44, "Square44x44Logo.png"), (71, "Square71x71Logo.png"), (89, "Square89x89Logo.png"),
                    (107, "Square107x107Logo.png"), (142, "Square142x142Logo.png"), (150, "Square150x150Logo.png"),
                    (284, "Square284x284Logo.png"), (310, "Square310x310Logo.png"), (50, "StoreLogo.png")]:
        render_png(s, zoom=Z).save(tauri / name)
    big[256].save(tauri / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
                  append_images=[big[s] for s in (16, 24, 32, 48, 64, 128)])
    # 深色底预览图
    prev = Image.new("RGBA", (1024, 1024), "#0F172A")
    prev.alpha_composite(imgs[1024])
    prev.save(out / "bloom-1024-on-dark.png")
    print("ok:", HERE)


if __name__ == "__main__":
    main()
