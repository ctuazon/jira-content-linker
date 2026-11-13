"""Draw assets/app.png and assets/app.ico (two chain links on a blue tile). Needs Pillow; run once."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SIZE = 1024  # drawn large, then downsampled for smooth edges
TOP = (38, 132, 255)
BOTTOM = (0, 82, 204)


def _tile() -> Image.Image:
    gradient = Image.new("RGBA", (SIZE, SIZE))
    draw = ImageDraw.Draw(gradient)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        draw.line([(0, y), (SIZE, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,))
    mask = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(mask).rounded_rectangle([32, 32, SIZE - 32, SIZE - 32], radius=210, fill=255)
    tile = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    tile.paste(gradient, (0, 0), mask)
    return tile


def _link_mask(x: int, y: int, width: int, height: int, stroke: int) -> Image.Image:
    """One horizontal chain link (a stadium outline) as an L mask."""
    mask = Image.new("L", (SIZE, SIZE), 0)
    draw = ImageDraw.Draw(mask)
    radius = height // 2
    draw.rounded_rectangle([x, y, x + width, y + height], radius=radius, fill=255)
    draw.rounded_rectangle([x + stroke, y + stroke, x + width - stroke, y + height - stroke],
                           radius=radius - stroke, fill=0)
    return mask


def draw() -> Image.Image:
    link_w, link_h, stroke, overlap = 500, 240, 64, 200
    x0 = (SIZE - (link_w * 2 - overlap)) // 2
    y0 = (SIZE - link_h) // 2
    left = _link_mask(x0, y0, link_w, link_h, stroke)
    right = _link_mask(x0 + link_w - overlap, y0, link_w, link_h, stroke)
    halo = 31  # odd; the blue gap drawn around the strand that passes over
    left_halo = left.filter(ImageFilter.MaxFilter(halo))
    right_halo = right.filter(ImageFilter.MaxFilter(halo))

    # Woven: the left link passes over at the top crossing, the right one over at the bottom.
    top = ImageChops.lighter(left, ImageChops.subtract(right, left_halo))
    bottom = ImageChops.lighter(right, ImageChops.subtract(left, right_halo))
    upper = Image.new("L", (SIZE, SIZE), 0)
    ImageDraw.Draw(upper).rectangle([0, 0, SIZE, SIZE // 2], fill=255)
    links = Image.composite(top, bottom, upper).rotate(-35, resample=Image.BICUBIC, center=(SIZE / 2, SIZE / 2))

    tile = _tile()
    white = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 255))
    tile.paste(white, (0, 0), links)
    return tile


def main() -> None:
    ASSETS.mkdir(exist_ok=True)
    art = draw()
    art.resize((256, 256), Image.LANCZOS).save(ASSETS / "app.png")
    art.resize((256, 256), Image.LANCZOS).save(
        ASSETS / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    )
    print(f"Wrote {ASSETS / 'app.png'} and {ASSETS / 'app.ico'}")


if __name__ == "__main__":
    main()
