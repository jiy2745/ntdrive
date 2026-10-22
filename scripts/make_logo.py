"""Draw the ntdrive wordmark for the README header.

Pillow is not a project dependency, because this runs once per logo change and nothing else needs
it. Run it with an ephemeral environment instead:

    uv run --with pillow python scripts/make_logo.py

The mark is `kd> ntdrive` with a block cursor: `kd>` is the kernel debugger prompt this whole tool
exists to drive, so the wordmark says what the project is without a picture of anything.

The background is transparent and a README is shown on both of GitHub's themes, so each colour has
to read on white AND on #0D1117. These two were picked by measuring, not by eye: the blue scores
4.63:1 on white and 4.08:1 on dark, the grey 4.55:1 and 4.16:1. GitHub's own accent (#2F81F7) and
muted grey (#8B949E) look right in dark mode but fall to 3.75:1 and 3.08:1 on white. Everything
here clears the 3:1 that WCAG asks of text this large with room to spare, and nearly clears the
4.5:1 for body text, which no single colour can do against both backgrounds at once.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "ntdrive-wordmark.png"

# Rendered at 4x and displayed at a quarter of the width, so it stays crisp on a HiDPI screen.
SCALE = 4
FONT_PX = 64 * SCALE
PAD_X = 10 * SCALE
PAD_Y = 8 * SCALE

PROMPT = "kd>"
NAME = "ntdrive"
GREY = (110, 119, 129, 255)  # 6E7781, readable on white and on dark
BLUE = (31, 111, 235, 255)  # 1F6FEB, the same
CURSOR = BLUE

FONTS = ("CascadiaCode.ttf", "consolab.ttf", "CascadiaMono.ttf", "cour.ttf")


def load_font() -> ImageFont.FreeTypeFont:
    """A monospace font from the Windows font directory, in order of preference."""
    for name in FONTS:
        path = Path("C:/Windows/Fonts") / name
        if path.is_file():
            return ImageFont.truetype(str(path), FONT_PX)
    raise SystemExit(f"no monospace font found, looked for {', '.join(FONTS)}")


def main() -> int:
    font = load_font()
    space = font.getlength(" ")
    gap = space * 0.55  # a full monospace space reads as a hole at this size
    nudge = space * 0.3  # breathing room before the cursor block
    prompt_w = font.getlength(PROMPT)
    name_w = font.getlength(NAME)
    cursor_w = font.getlength("n")
    ascent, descent = font.getmetrics()

    width = int(PAD_X * 2 + prompt_w + gap + name_w + nudge + cursor_w)
    height = int(PAD_Y * 2 + ascent + descent)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    x = PAD_X
    draw.text((x, PAD_Y), PROMPT, font=font, fill=GREY)
    x += prompt_w + gap
    draw.text((x, PAD_Y), NAME, font=font, fill=BLUE)
    x += name_w + nudge
    # A filled block where a terminal would park its cursor.
    top = PAD_Y + ascent * 0.18
    draw.rectangle([x, top, x + cursor_w, PAD_Y + ascent], fill=CURSOR)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"{OUT.relative_to(ROOT)}  {img.width}x{img.height}  {OUT.stat().st_size} bytes")
    print(f"display width: {img.width // SCALE}px")
    return 0


if __name__ == "__main__":
    sys.exit(main())
