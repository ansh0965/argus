"""Generate the ARGUS app icon: a watching eye inside a ring.

Drawn at 1024px and downsampled so the small sizes stay crisp, then packed as a
multi-resolution .ico (Windows picks 16/32 for the tray and taskbar, 256 for
large-icon views).
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024
BG_OUTER = (12, 16, 23, 255)
CYAN = (34, 211, 238, 255)
CYAN_DIM = (16, 130, 150, 255)
WHITE = (232, 240, 248, 255)


def rounded_square(size: int, radius_frac: float, fill) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(size * radius_frac)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=fill)
    return img


def build() -> Image.Image:
    img = rounded_square(S, 0.22, BG_OUTER)
    d = ImageDraw.Draw(img)
    c = S // 2

    # subtle inner glow so the disc does not read as flat
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([c - 330, c - 330, c + 330, c + 330],
                                 fill=(34, 211, 238, 46))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))

    # outer ring
    d.ellipse([c - 380, c - 380, c + 380, c + 380], outline=CYAN, width=44)

    # the eye: two arcs meeting at the corners (a lens shape)
    w, h = 300, 175
    d.arc([c - w, c - h, c + w, c + h], start=180, end=360, fill=WHITE, width=40)
    d.arc([c - w, c - h, c + w, c + h], start=0, end=180, fill=WHITE, width=40)

    # iris + pupil
    d.ellipse([c - 108, c - 108, c + 108, c + 108], fill=CYAN)
    d.ellipse([c - 46, c - 46, c + 46, c + 46], fill=(8, 12, 18, 255))
    # catch-light, sells it as an eye rather than a target
    d.ellipse([c + 26, c - 74, c + 74, c - 26], fill=(255, 255, 255, 210))

    # lash ticks around the ring - the "hundred eyes" nod
    import math
    for i in range(12):
        a = math.radians(i * 30 + 15)
        x1, y1 = c + 405 * math.cos(a), c + 405 * math.sin(a)
        x2, y2 = c + 455 * math.cos(a), c + 455 * math.sin(a)
        d.line([x1, y1, x2, y2], fill=CYAN_DIM, width=26)
    return img


if __name__ == "__main__":
    out = Path(__file__).parent
    master = build()
    master.resize((512, 512), Image.LANCZOS).save(out / "argus.png")
    sizes = [(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (24, 24), (16, 16)]
    master.save(out / "argus.ico", format="ICO", sizes=sizes)
    print("wrote", out / "argus.ico", "and argus.png")
