"""Generate every brand image the project publishes, from logo.png alone.

Run from the repository root:

    python tools/brand-assets.py

Two families come out of here:

  youtube assets/   channel art and watermarks, sized to what YouTube accepts
  apps/site/img/    favicons, the OAuth consent logo and the Open Graph card

Nothing here is a build input, so it is not wired into CI — the outputs are
committed and only regenerated when the brand changes. Keeping the generator
next to them is what makes "the brand changed" a one-command job rather than an
afternoon in an image editor.

Every mark is drawn at 8x and downsampled with LANCZOS. A 150px watermark is
rendered by the player at roughly 64px, and a mark rasterised at its final size
looks visibly chewed there.

Requires Pillow (pip install pillow); it is not a project dependency.
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
YT = ROOT / "youtube assets"
IMG = ROOT / "apps/site/img"

INK = (1, 1, 21)
PLATE = (10, 10, 30, 255)
FONTS = Path("C:/Windows/Fonts")

# Sampled from logo.png, in the order the lockup runs through them.
STOPS = [
    (0.00, (0x2E, 0x9B, 0xF0)),
    (0.35, (0x7A, 0x3C, 0xF0)),
    (0.65, (0xE6, 0x3A, 0xC0)),
    (1.00, (0xFF, 0x9A, 0x2E)),
]

LOGO = Image.open(ROOT / "logo.png").convert("RGB")
LOCKUP = LOGO.crop((146, 207, 1114, 997))   # mark + wordmark + tagline, tight
MARK = LOGO.crop((286, 190, 965, 690))      # the mark alone

S = 1200                                    # working canvas for drawn marks


# ── Primitives ───────────────────────────────────────────────────────────────


def _lut(channel):
    out = []
    for i in range(256):
        t = i / 255
        for (a, ca), (b, cb) in zip(STOPS, STOPS[1:]):
            if a <= t <= b:
                f = (t - a) / (b - a)
                out.append(round(ca[channel] + (cb[channel] - ca[channel]) * f))
                break
        else:
            out.append(STOPS[-1][1][channel])
    return out


def gradient(size):
    """The brand gradient on a 45-degree diagonal, as an RGB square."""
    big = int(size * 1.7)
    g = Image.linear_gradient("L").resize((big, big), Image.BICUBIC).rotate(-45, Image.BICUBIC)
    o = (big - size) // 2
    g = g.crop((o, o, o + size, o + size))
    return Image.merge("RGB", (g.point(_lut(0)), g.point(_lut(1)), g.point(_lut(2))))


def mark_mask(size):
    """Film frame, sprocket rail and play triangle — the logo, simplified.

    The full mark has an orbit swoosh and a waveform that turn to mush below
    about 100px, so anything that renders small gets this instead.
    """
    k = size / S

    def r(*v):
        return [x * k for x in v]

    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle(r(170, 180, 1030, 1020), radius=180 * k, outline=255, width=round(78 * k))
    y = 300
    for _ in range(4):
        d.rounded_rectangle(r(285, y, 375, y + 110), radius=26 * k, fill=255)
        y += 165
    d.polygon(r(480, 350, 480, 850, 900, 600), fill=255)
    return m


def glow(canvas, xy, radius, colour, strength):
    """Soft radial light, drawn small and blurred so it costs nothing."""
    w, h = canvas.size
    s = 6
    m = Image.new("L", (w // s, h // s), 0)
    x, y, rr = xy[0] // s, xy[1] // s, radius // s
    ImageDraw.Draw(m).ellipse([x - rr, y - rr, x + rr, y + rr], fill=strength)
    m = m.filter(ImageFilter.GaussianBlur(rr * 0.55)).resize((w, h), Image.BICUBIC)
    canvas.paste(Image.new("RGB", (w, h), colour), (0, 0), m)


def over(canvas, art, x, y):
    """Composite brand art whose own background is near-black.

    `lighter` rather than paste: the logo's dark field can then never show up as
    a rectangle over whatever glow sits behind it, and no cut-out is needed.
    """
    region = canvas.crop((x, y, x + art.width, y + art.height))
    canvas.paste(ImageChops.lighter(region, art), (x, y))


def fit(font_path, text, size, max_w):
    """Shrink until the line fits, rather than trusting a guessed size."""
    while size > 12:
        f = ImageFont.truetype(str(font_path), size)
        if f.getbbox(text)[2] <= max_w:
            return f
        size -= 1
    return ImageFont.truetype(str(font_path), size)


def badge(size, plate=True):
    """The drawn mark on the dark plate. Legible at 16px, which is the point."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    if plate:
        p = Image.new("L", (S, S), 0)
        ImageDraw.Draw(p).rounded_rectangle([0, 0, S - 1, S - 1], radius=270, fill=255)
        img.paste(PLATE, (0, 0), p)
    inner = round(S * 0.80)
    art = Image.new("RGBA", (inner, inner), (0, 0, 0, 0))
    art.paste(gradient(inner), (0, 0), mark_mask(inner))
    img.alpha_composite(art, ((S - inner) // 2, (S - inner) // 2))
    return img.resize((size, size), Image.LANCZOS)


def save(img, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    print(f"  {path.relative_to(ROOT).as_posix():44} {img.size[0]}x{img.size[1]}"
          f"  {path.stat().st_size / 1024:7.1f} KB")


# ── Watermarks ───────────────────────────────────────────────────────────────


def watermarks():
    mk = mark_mask(S)

    # White with a soft dark halo. Without the halo it vanishes over snow,
    # paper or a whiteboard, and a watermark that only works over dark footage
    # is a watermark that fails on the clip you most wanted branded.
    halo = mk.filter(ImageFilter.GaussianBlur(30)).point(lambda v: min(255, int(v * 1.9)))
    white = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    white.paste((4, 4, 14, 255), (0, 0), halo.point(lambda v: int(v * 0.62)))
    white.paste((255, 255, 255, 255), (0, 0), mk)
    save(white.resize((150, 150), Image.LANCZOS), YT / "watermark-white-150.png")

    save(badge(150), YT / "watermark-badge-150.png")

    grad = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    grad.paste(gradient(S), (0, 0), mk)
    save(grad.resize((150, 150), Image.LANCZOS), YT / "watermark-gradient-150.png")


# ── Channel art ──────────────────────────────────────────────────────────────


def banner():
    W, H = 2560, 1440
    SAFE_W, SAFE_H = 1546, 423            # the box that survives on a phone
    SX, SY = (W - SAFE_W) // 2, (H - SAFE_H) // 2

    bg = Image.new("RGB", (W, H), INK)
    glow(bg, (620, 470), 1100, (0x2E, 0x9B, 0xF0), 58)
    glow(bg, (1960, 1010), 1200, (0xE6, 0x3A, 0xC0), 46)
    glow(bg, (2430, 1330), 700, (0xFF, 0x9A, 0x2E), 30)
    glow(bg, (1280, 720), 900, (0x7A, 0x3C, 0xF0), 26)

    arc = Image.new("L", (W, H), 0)
    ImageDraw.Draw(arc).arc([-600, 120, W + 600, H - 40], 200, 340, fill=70, width=7)
    arc = arc.rotate(-8, Image.BICUBIC).filter(ImageFilter.GaussianBlur(2))
    bg.paste(Image.new("RGB", (W, H), (0x7A, 0x3C, 0xF0)), (0, 0), arc)

    vign = Image.new("L", (W, H), 0)
    ImageDraw.Draw(vign).ellipse([-W // 3, -H // 2, W + W // 3, H + H // 2], fill=255)
    bg = Image.composite(bg, Image.new("RGB", (W, H), (0, 0, 6)),
                         vign.filter(ImageFilter.GaussianBlur(180)))

    logo_h = 364
    logo = LOCKUP.resize((round(LOCKUP.width * logo_h / LOCKUP.height), logo_h), Image.LANCZOS)
    lx, ly = SX, SY + (SAFE_H - logo_h) // 2
    over(bg, logo, lx, ly)

    d = ImageDraw.Draw(bg)
    rule_x = lx + logo.width + 78
    d.line([rule_x, SY + 66, rule_x, SY + SAFE_H - 66], fill=(58, 58, 104), width=3)

    tx = rule_x + 74
    room = SX + SAFE_W - tx
    lines = [
        ("segoeuib.ttf", "Local-first AI content agent", 64, (255, 255, 255), 0),
        ("segoeuil.ttf", "Long-form video in. Vertical clips out.", 44, (0x9E, 0xA6, 0xC8), 26),
        ("segoeuil.ttf", "Transcribed, cut and encoded on your own GPU.", 44, (0x9E, 0xA6, 0xC8), 6),
        ("segoeui.ttf", "getclipforge.web.app", 36, (0x4F, 0xB0, 0xFF), 30),
    ]
    fonts = [(fit(FONTS / p, t, s, room), t, c, gap) for p, t, s, c, gap in lines]
    total = sum(f.getbbox(t)[3] - f.getbbox(t)[1] + gap for f, t, _, gap in fonts)
    y = SY + (SAFE_H - total) // 2
    for f, t, colour, gap in fonts:
        y += gap
        d.text((tx, y), t, font=f, fill=colour)
        y += f.getbbox(t)[3] - f.getbbox(t)[1]

    save(bg, YT / "banner-2560x1440.png")
    save(bg.resize((2048, 1152), Image.LANCZOS), YT / "banner-2048x1152.png")

    guide = bg.copy()
    g = ImageDraw.Draw(guide)
    g.rectangle([SX, SY, SX + SAFE_W, SY + SAFE_H], outline=(0x4F, 0xFF, 0xA0), width=4)
    g.rectangle([(W - 1855) // 2, SY, (W + 1855) // 2, SY + SAFE_H],
                outline=(0xFF, 0xC8, 0x4F), width=3)
    g.text((SX + 10, SY - 46), "1546 x 423  phone / all devices",
           font=ImageFont.truetype(str(FONTS / "segoeui.ttf"), 34), fill=(0x4F, 0xFF, 0xA0))
    save(guide, YT / "banner-safe-area-check.png")


def profile():
    a = Image.new("RGB", (800, 800), INK)
    glow(a, (400, 400), 760, (0x7A, 0x3C, 0xF0), 72)
    mk = MARK.resize((560, round(MARK.height * 560 / MARK.width)), Image.LANCZOS)
    over(a, mk, (800 - mk.width) // 2, (800 - mk.height) // 2)
    save(a, YT / "profile-800x800.png")


# ── Site and OAuth ───────────────────────────────────────────────────────────


def icons():
    for size in (512, 192, 180, 120, 48, 32, 16):
        name = {180: "apple-touch-icon-180.png", 120: "consent-logo-120.png"}.get(
            size, f"icon-{size}.png")
        save(badge(size), IMG / name)


def og_card():
    """1200x630, the size every social preview crops toward."""
    W, H = 1200, 630
    bg = Image.new("RGB", (W, H), INK)
    glow(bg, (250, 180), 620, (0x2E, 0x9B, 0xF0), 66)
    glow(bg, (980, 520), 640, (0xE6, 0x3A, 0xC0), 54)
    glow(bg, (600, 340), 500, (0x7A, 0x3C, 0xF0), 30)

    arc = Image.new("L", (W, H), 0)
    ImageDraw.Draw(arc).arc([-260, 40, W + 260, H + 120], 200, 340, fill=64, width=4)
    bg.paste(Image.new("RGB", (W, H), (0x7A, 0x3C, 0xF0)), (0, 0),
             arc.filter(ImageFilter.GaussianBlur(1.5)))

    lock_h = 232
    lock = LOCKUP.resize((round(LOCKUP.width * lock_h / LOCKUP.height), lock_h), Image.LANCZOS)
    over(bg, lock, 84, 96)

    d = ImageDraw.Draw(bg)
    d.text((88, 386), "Long-form video in. Vertical clips out.",
           font=fit(FONTS / "segoeuib.ttf", "Long-form video in. Vertical clips out.", 54, 1030),
           fill=(255, 255, 255))
    d.text((88, 456), "Transcribed, analysed, cut and encoded on your own GPU.",
           font=fit(FONTS / "segoeuil.ttf",
                    "Transcribed, analysed, cut and encoded on your own GPU.", 40, 1030),
           fill=(0x9E, 0xA6, 0xC8))
    d.text((88, 536), "getclipforge.web.app",
           font=ImageFont.truetype(str(FONTS / "segoeui.ttf"), 32), fill=(0x4F, 0xB0, 0xFF))
    save(bg, IMG / "og-card.png")


def favicon_ico():
    """One .ico holding 16/32/48, for the crawlers and browsers that only ask
    for /favicon.ico and ignore the link tags."""
    path = IMG / "favicon.ico"
    badge(48).save(path, sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"  {path.relative_to(ROOT).as_posix():44} 16/32/48"
          f"  {path.stat().st_size / 1024:7.1f} KB")


if __name__ == "__main__":
    print("youtube assets")
    watermarks()
    banner()
    profile()
    print("apps/site/img")
    icons()
    og_card()
    favicon_ico()
