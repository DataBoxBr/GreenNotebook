#!/usr/bin/env python3

import colorsys
from pathlib import Path

from PIL import Image


REPO = Path(__file__).resolve().parents[1]
ICON_DIR = REPO / "rednotebook" / "images" / "rednotebook-icon"


def recolor_red_pixels(path):
    with Image.open(path) as source:
        image = source.convert("RGBA")

    pixels = []
    for red, green, blue, alpha in image.getdata():
        hue, saturation, value = colorsys.rgb_to_hsv(red / 255, green / 255, blue / 255)
        if (hue < 0.04 or hue > 0.94) and saturation >= 0.45 and value < 0.93:
            red, green, blue = (
                round(channel * 255) for channel in colorsys.hsv_to_rgb(0.39, saturation, value)
            )
        pixels.append((red, green, blue, alpha))

    image.putdata(pixels)
    image.save(path)


def main():
    png_paths = sorted(ICON_DIR.glob("rn-*.png"))
    png_paths.append(REPO / "rednotebook.png")
    for path in png_paths:
        recolor_red_pixels(path)

    with Image.open(ICON_DIR / "rn-256.png") as source:
        source.convert("RGBA").save(
            REPO / "win" / "rednotebook.ico",
            format="ICO",
            sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        )


if __name__ == "__main__":
    main()
