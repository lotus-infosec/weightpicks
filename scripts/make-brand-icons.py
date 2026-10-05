"""Make the default brand images from the project logo (docs/assets/logo.png).

Run once after the logo changes: `uv run python scripts/make-brand-icons.py`.
Writes the README logo (cropped to the tile) and the app's default icons. The small
icons use only the figure, because the wording can't be read at 32 px.
"""

from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "assets" / "logo.png"
BRAND = ROOT / "app" / "web" / "static" / "brand"
FIGURE = (476, 64, 932, 520)  # a square of the tile above the wording: torsos and arrows


def square(im: Image.Image) -> Image.Image:
    side = max(im.size)
    canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    return canvas


def tile_icon(src: Image.Image, size: int) -> Image.Image:
    """The figure on the tile's own background, in a rounded square."""
    figure = src.crop(FIGURE)
    side = figure.width
    mask = Image.new("L", (side, side), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, side - 1, side - 1), radius=side // 6, fill=255)
    figure.putalpha(mask)
    return figure.resize((size, size), Image.Resampling.LANCZOS)


def small(im: Image.Image) -> Image.Image:
    """A 256-colour palette keeps the PNGs light; the logo has few colours anyway."""
    return im.quantize(256, method=Image.Quantize.FASTOCTREE)


def main() -> None:
    src = Image.open(SOURCE).convert("RGBA")
    bbox = src.getchannel("A").getbbox()
    if bbox is None:
        raise SystemExit("the logo has no visible pixels")
    tile = square(src.crop(bbox))
    BRAND.mkdir(parents=True, exist_ok=True)
    small(tile.resize((320, 320), Image.Resampling.LANCZOS)).save(
        ROOT / "docs" / "assets" / "logo-readme.png", optimize=True
    )
    for size in (512, 192):
        small(tile.resize((size, size), Image.Resampling.LANCZOS)).save(
            BRAND / f"logo-{size}.png", optimize=True
        )
    small(tile_icon(src, 180)).save(BRAND / "apple-touch-icon.png", optimize=True)
    tile_icon(src, 32).save(BRAND / "favicon-32.png", optimize=True)
    tile_icon(src, 64).save(BRAND / "mark-64.png", optimize=True)
    for f in sorted([*BRAND.glob("*.png"), ROOT / "docs" / "assets" / "logo-readme.png"]):
        print(f.relative_to(ROOT), f.stat().st_size)


if __name__ == "__main__":
    main()
