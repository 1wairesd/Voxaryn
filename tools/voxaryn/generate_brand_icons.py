#!/usr/bin/env python3
"""Export Voxaryn's supplied PNG without changing the original. Requires Pillow 12.2.

Run from any directory: py -3 tools/voxaryn/generate_brand_icons.py [--check].
Historical screenshots and Horizon's design candidates are not generated assets.
"""
from __future__ import annotations

import argparse
import base64
import io
from pathlib import Path
import struct

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "docs/media/brand/voxaryn-source.png"
SIZES = (16, 24, 32, 48, 64, 128, 256, 512)
ICNS = ((b"icp4", 16), (b"icp5", 32), (b"icp6", 64), (b"ic07", 128),
        (b"ic08", 256), (b"ic09", 512), (b"ic10", 1024), (b"ic11", 32),
        (b"ic12", 64), (b"ic13", 256), (b"ic14", 512))


def encode(image: Image.Image, fmt: str = "PNG", **kwargs) -> bytes:
    out = io.BytesIO()
    image.save(out, format=fmt, optimize=True, **kwargs)
    return out.getvalue()


def master() -> Image.Image:
    image = Image.open(SOURCE).convert("RGBA")
    # Remove only nearly invisible background speckles (alpha <= 8 / 255).
    # Keep the supplied tile's transparency, colours and antialiased silhouette.
    alpha = image.getchannel("A").point(lambda a: 0 if a <= 8 else a)
    image.putalpha(alpha)
    left, top, right, bottom = alpha.getbbox()
    side = max(right - left, bottom - top)
    # Square crop with a 1% safety margin, without stretching the tile.
    side += 2 * max(1, round(side * .01))
    x = (left + right - side) // 2
    y = (top + bottom - side) // 2
    assert x >= 0 and y >= 0 and x + side <= image.width and y + side <= image.height
    return image.crop((x, y, x + side, y + side))


def resize(image: Image.Image, size: int, macos: bool = False) -> Image.Image:
    assert size <= image.width, "Do not upscale the source"
    tile = round(size * 824 / 1024) if macos else size
    # Pillow's RGBA resampling uses premultiplied alpha to avoid dark edge fringes.
    small = image.resize((tile, tile), Image.Resampling.LANCZOS)
    if not macos:
        return small
    canvas = Image.new("RGBA", (size, size))
    canvas.alpha_composite(small, ((size - tile) // 2, (size - tile) // 2))
    return canvas


def svg(png: bytes, size: int) -> bytes:
    # A self-contained raster wrapper, not a vector reconstruction of the logo.
    data = base64.b64encode(png).decode("ascii")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
            f'<title>Voxaryn</title><image width="{size}" height="{size}" '
            f'xlink:href="data:image/png;base64,{data}"/></svg>\n').encode()


def ico(images: dict[int, bytes]) -> bytes:
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for size, png in images.items():
        out += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return out + b"".join(images.values())


def card(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    canvas = Image.new("RGB", size, "#0d0e13")
    logo_size = min(size[1] // 2, 320)
    logo = resize(image, logo_size)
    x, y = size[0] // 10, (size[1] - logo_size) // 2
    canvas.paste(logo, (x, y), logo)
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.truetype(str(ROOT / "crates/horizon-theme/fonts/Geist-Medium.ttf"), size[1] // 9)
    draw.text((x + logo_size + size[0] // 24, size[1] // 2), "Voxaryn", font=font,
              fill="#ecedf3", anchor="lm")
    return canvas


def dmg(image: Image.Image, scale: int) -> Image.Image:
    canvas = Image.new("RGB", (660 * scale, 420 * scale), "#fafafc")
    logo = resize(image, 16 * scale)
    canvas.paste(logo, (24 * scale, 22 * scale), logo)
    draw = ImageDraw.Draw(canvas)
    fonts = ROOT / "crates/horizon-theme/fonts"
    font = ImageFont.truetype(str(fonts / "Geist-Medium.ttf"), 13 * scale)
    mono = ImageFont.truetype(str(fonts / "GeistMono-Regular.ttf"), 11 * scale)
    draw.text((48 * scale, 22 * scale), "Voxaryn", font=font, fill="#16181f")
    draw.line([(262 * scale, 199 * scale), (398 * scale, 199 * scale)], fill="#5b3fd6", width=scale)
    draw.line([(394 * scale, 195 * scale), (398 * scale, 199 * scale),
               (394 * scale, 203 * scale)], fill="#5b3fd6", width=scale)
    draw.text((330 * scale, 346 * scale), "DRAG VOXARYN TO APPLICATIONS",
              anchor="mt", font=mono, fill="#646875")
    return canvas


def outputs() -> dict[str, bytes]:
    image = master()
    pngs = {s: encode(resize(image, s)) for s in (*SIZES, 180, 500, 1024)}
    macs = {s: encode(resize(image, s, macos=True)) for _, s in ICNS}
    files = {f"packaging/icons/hicolor/{s}x{s}/apps/horizon.png": pngs[s] for s in SIZES}
    files["packaging/icons/horizon.ico"] = ico({s: pngs[s] for s in SIZES if s <= 256})
    body = b"".join(kind + struct.pack(">I", len(macs[s]) + 8) + macs[s] for kind, s in ICNS)
    files["packaging/icons/horizon.icns"] = b"icns" + struct.pack(">I", len(body) + 8) + body
    for path, data in {
        "crates/horizon-theme/icons/horizon-64.png": pngs[64],
        "docs/media/icon-256.png": pngs[256],
        "docs/media/brand/icon-1024.png": pngs[1024],
        "docs/media/brand/icon-macos-1024.png": macs[1024],
        "docs/media/brand/social/github-avatar-500.png": pngs[500],
        "website/public/favicon-32.png": pngs[32],
        "website/public/apple-touch-icon.png": pngs[180],
        "website/public/favicon.svg": svg(pngs[64], 64),
        "packaging/icons/hicolor/scalable/apps/horizon.svg": svg(pngs[512], 512),
        "docs/media/brand/icon.svg": svg(pngs[512], 512),
        "docs/media/brand/icon-macos.svg": svg(macs[512], 512),
        "docs/media/brand/avatar.svg": svg(pngs[500], 500),
        "docs/media/brand/mark.svg": svg(pngs[256], 256),
        "docs/media/brand/mark-dark.svg": svg(pngs[256], 256),
        "docs/media/brand/social/github-social-1280x640.png": encode(card(image, (1280, 640))),
        "website/public/og.png": encode(card(image, (1200, 630))),
        "docs/media/social-preview.jpg": encode(card(image, (1200, 630)), "JPEG", quality=95),
        "packaging/macos/background.png": encode(dmg(image, 1)),
        "packaging/macos/background@2x.png": encode(dmg(image, 2)),
    }.items():
        files[path] = data
    return files


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare exports without writing")
    args = parser.parse_args()
    files = outputs()
    for name, data in files.items():
        path = ROOT / name
        if args.check:
            actual = path.read_bytes() if path.is_file() else b""
            if path.suffix == ".svg":
                actual = actual.replace(b"\r\n", b"\n")
            if actual != data:
                raise SystemExit(f"Stale branding resource: {name}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print(f"{'Checked' if args.check else 'Wrote'} {len(files)} resources; {sum(map(len, files.values())):,} bytes")


if __name__ == "__main__":
    main()
