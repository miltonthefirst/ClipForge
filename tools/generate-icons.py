"""Generate every icon the PWA needs from the one brand asset, `logo.png`.

Run it whenever the logo changes:

    uv run --with pillow python tools/generate-icons.py

`--with` rather than a project dependency: this runs by hand a few times a year,
and Pillow has no business in the worker's runtime closure just because the
favicon was regenerated once.

## Why this is not a simple resize

`logo.png` is a full lockup — mark, wordmark, and a tagline — painted on an
opaque dark field. Three things follow, and each is a decision this script makes
rather than a detail it hides:

**Only the mark survives.** "ClipForge" is unreadable below about 96px and the
tagline below about 300px. Scaling the whole lockup into a 32px favicon produces
grey mush. So the mark is isolated and everything else discarded.

**The background has to come off, and the artwork was drawn assuming it would
not.** The file has no alpha, and the card's face is painted the *same* navy as
the backdrop behind it — the dark interior is not a colour the designer chose, it
is the plate showing through. So there is no faithful answer to "what colour is
that region on white": the mark is a dark-background lockup.

Rather than invent one, this treats the navy as negative space everywhere it
appears. Alpha comes from each pixel's distance to the plate colour, so on the
dark theme the result is pixel-identical to the original, and on the light theme
the negative space becomes the page — a deliberate light-mode variant rather than
a dark rectangle dropped onto a white page.

Distance, not luminance: the orbit's deeper purples are dark but strongly
coloured, and a luminance threshold would make them half-transparent. Distance
from the plate keeps them.

The result is then **un-matted**. The source is already composited over navy, so
every partially-transparent pixel has navy mixed into it; pasting that onto white
leaves a dark fringe. Recovering `artwork = (observed - plate*(1-a)) / a` removes
it.

**Maskable is not the same as any.** Android crops a maskable icon to whatever
shape the launcher likes, keeping only the middle 80%. An icon drawn to the
edges gets its corners eaten. The manifest previously declared both icons
`"any maskable"` while neither had a safe zone, so this emits a genuinely
separate, padded pair.
"""

from __future__ import annotations

import struct
from math import sqrt
from pathlib import Path

from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "logo.png"
PUBLIC = ROOT / "apps" / "web" / "public"
ICONS = PUBLIC / "icons"

# The mark's bounding box in logo.png, measured by scanning for rows that differ
# from the corner colour: the lockup separates into three bands and this is the
# first. Recorded rather than recomputed so a re-run is deterministic, and
# checked at load time so a replaced logo fails loudly instead of silently
# cropping the wrong region.
MARK_BOX = (285, 207, 964, 672)
SOURCE_SIZE = (1254, 1254)

# Sampled from the plate behind the mark. Near-black with a blue cast, which is
# what keeps the icon reading as *this* brand rather than as a generic dark tile.
TILE = (10, 10, 31)

# The plate colour the artwork was composited over, and the band over which alpha
# ramps away from it. The plate is a subtle radial gradient rather than a flat
# fill: sampling its outer frame puts the worst-case departure from PLATE at 31,
# so NEAR sits just above that. Below NEAR is plate, above FAR is certainly
# artwork, and the ramp between them is what keeps the orbit's glow soft.
PLATE = (3, 1, 22)
NEAR = 34.0
FAR = 70.0


def load_mark() -> Image.Image:
    """The mark alone: plate removed, un-matted, tightly cropped, soft edges."""
    image = Image.open(SOURCE).convert("RGB")
    if image.size != SOURCE_SIZE:
        raise SystemExit(
            f"{SOURCE.name} is {image.size}, expected {SOURCE_SIZE}. The mark's bounding "
            "box is measured against that size — re-measure MARK_BOX before regenerating."
        )

    mark = image.crop(MARK_BOX)
    width, height = mark.size
    source = mark.load()

    out = Image.new("RGBA", (width, height))
    target = out.load()

    pr, pg, pb = PLATE
    span = FAR - NEAR

    for y in range(height):
        for x in range(width):
            r, g, b = source[x, y]
            dr, dg, db = r - pr, g - pg, b - pb
            distance = sqrt(dr * dr + dg * dg + db * db)

            if distance <= NEAR:
                target[x, y] = (0, 0, 0, 0)
                continue

            alpha = 1.0 if distance >= FAR else (distance - NEAR) / span

            # Un-matt: the observed pixel is already artwork over PLATE, so
            # without this every soft edge carries navy into whatever it is
            # pasted onto — a dark fringe, visible on the light theme and
            # nowhere else, which is exactly the kind of thing that ships.
            inverse = 1.0 - alpha
            target[x, y] = (
                min(255, max(0, round((r - pr * inverse) / alpha))),
                min(255, max(0, round((g - pg * inverse) / alpha))),
                min(255, max(0, round((b - pb * inverse) / alpha))),
                round(alpha * 255),
            )

    # A half-pixel of softening on alpha only. The threshold is decided per
    # pixel, so without this the edge stairsteps once it is scaled down.
    r, g, b, a = out.split()
    return Image.merge("RGBA", (r, g, b, a.filter(ImageFilter.GaussianBlur(0.6))))


def tile(mark: Image.Image, size: int, *, coverage: float, background: tuple[int, int, int] | None) -> Image.Image:
    """The mark centred on a square canvas, occupying `coverage` of its width."""
    canvas = Image.new("RGBA", (size, size), (*background, 255) if background else (0, 0, 0, 0))

    target = size * coverage
    scale = min(target / mark.width, target / mark.height)
    scaled = mark.resize(
        (max(1, round(mark.width * scale)), max(1, round(mark.height * scale))),
        Image.LANCZOS,
    )

    canvas.alpha_composite(
        scaled, ((size - scaled.width) // 2, (size - scaled.height) // 2)
    )
    return canvas


def write_png(image: Image.Image, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    print(f"  {path.relative_to(ROOT)}  {image.width}x{image.height}  {path.stat().st_size:,} bytes")


def main() -> None:
    print(f"reading {SOURCE.relative_to(ROOT)}")
    mark = load_mark()
    print(f"  mark extracted: {mark.width}x{mark.height} with alpha")

    print("\nicons")

    # `any`: drawn edge to edge, so the mark fills most of the tile. This is what
    # a browser tab and a desktop shortcut show.
    for size in (192, 512):
        write_png(tile(mark, size, coverage=0.82, background=TILE), ICONS / f"icon-{size}.png")

    # `maskable`: the launcher keeps only the middle 80% and may crop to a circle.
    # 0.58 keeps the orbit's extremes inside that circle rather than clipped by it.
    for size in (192, 512):
        write_png(
            tile(mark, size, coverage=0.58, background=TILE),
            ICONS / f"icon-maskable-{size}.png",
        )

    # iOS applies its own rounded mask and composites on white if there is alpha,
    # so this one is deliberately opaque.
    write_png(tile(mark, 180, coverage=0.78, background=TILE), ICONS / "apple-touch-icon.png")

    # Transparent, for the in-app header, where it sits on whichever theme is
    # active rather than on a tile of its own.
    write_png(tile(mark, 256, coverage=1.0, background=None), ICONS / "mark.png")

    # A multi-size .ico. Browsers pick per context — 16 in the tab, 32 in
    # bookmarks, 48 in the Windows taskbar — and shipping one size makes the
    # others someone else's resampling problem.
    favicon = PUBLIC / "favicon.ico"
    tile(mark, 48, coverage=0.92, background=TILE).save(
        favicon, format="ICO", sizes=[(16, 16), (32, 32), (48, 48)]
    )
    with favicon.open("rb") as handle:
        count = struct.unpack("<H", handle.read(6)[4:6])[0]
    print(f"  {favicon.relative_to(ROOT)}  {count} sizes  {favicon.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
