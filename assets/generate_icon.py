"""Generate crisp Windows-compliant Reper .ico (BMP frames) and Inno Setup wizard bitmaps."""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent

DARK = (24, 27, 31, 255)        # #181B1F Drafting Graphite
BORDER = (62, 68, 78, 255)      # #3E444E Perimeter frame
CHALK = (245, 243, 238, 255)    # #F5F3EE Architectural chalk
SIGNAL = (209, 59, 39, 255)     # #D13B27 Signal Vermilion


def _draw_logo(size: int, *, with_tile: bool = True) -> Image.Image:
    """Draw the Cut-Mark Reper emblem: datum shelf + leveling chevron + vermilion benchmark node."""
    scale = 4
    w = size * scale
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    if with_tile:
        margin = round(w * 0.04)
        radius = round(w * 0.20)
        d.rounded_rectangle(
            [margin, margin, w - margin, w - margin],
            radius=radius,
            fill=DARK,
            outline=BORDER,
            width=max(2, round(w * 0.025)),
        )

    cx = w // 2
    sw = max(2, round(w * 0.072))
    # Horizontal datum bar at top
    bar_y = round(w * 0.25)
    d.line([(round(w * 0.21), bar_y), (round(w * 0.79), bar_y)], fill=CHALK, width=sw)

    # Structural leveling wedge / chevron + central plumb drop line
    top_y = round(w * 0.45)
    bot_y = round(w * 0.78)
    span_x = round(w * 0.275)
    d.line(
        [(cx - span_x, bot_y), (cx, top_y), (cx + span_x, bot_y)],
        fill=CHALK,
        width=max(3, round(w * 0.082)),
        joint="miter",
    )
    d.line(
        [(cx, round(w * 0.56)), (cx, round(w * 0.79))],
        fill=CHALK,
        width=max(2, round(w * 0.062)),
    )

    # Vermilion benchmark control node at apex-datum junction
    dot_y = round(w * 0.37)
    dot_r = round(w * 0.074)
    d.rectangle([cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r], fill=SIGNAL)

    return img.resize((size, size), Image.Resampling.LANCZOS)


def build_icon(out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = [_draw_logo(s) for s in sizes]
    frames[0].save(
        out_path,
        format="ICO",
        bitmap_format="bmp",
        append_images=frames[1:],
    )


def build_wizard_bitmaps(assets_dir: Path) -> None:
    # Inno Setup WizardImageFile (164x314, 24-bit BMP)
    large = Image.new("RGB", (164, 314), DARK[:3])
    d = ImageDraw.Draw(large)
    for x in range(0, 164, 24):
        d.line([(x, 0), (x, 314)], fill=(34, 38, 44), width=1)
    for y in range(0, 314, 24):
        d.line([(0, y), (164, y)], fill=(34, 38, 44), width=1)
    d.line([(163, 0), (163, 314)], fill=SIGNAL[:3], width=2)
    logo_lg = _draw_logo(96, with_tile=False)
    large.paste(logo_lg, ((164 - 96) // 2, 72), logo_lg)
    large.save(assets_dir / "wizard_large.bmp", format="BMP")

    # Inno Setup WizardSmallImageFile (55x58, 24-bit BMP on white header background)
    small = Image.new("RGB", (55, 58), (255, 255, 255))
    logo_sm = _draw_logo(48)
    small.paste(logo_sm, ((55 - 48) // 2, (58 - 48) // 2), logo_sm)
    small.save(assets_dir / "wizard_small.bmp", format="BMP")


if __name__ == "__main__":
    ico = ROOT / "reper.ico"
    build_icon(ico)
    build_wizard_bitmaps(ROOT)
    print(f"Generated Reper BMP-framed icon and wizard bitmaps in: {ROOT}")
