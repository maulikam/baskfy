"""Turn the 4096px source mark into the asset set the app and the desk console need (M37).

Run:  uv run --with pillow python apps/web/brand-src/build_brand.py

WHY THE MATTE IS NOT `white -> transparent`
-------------------------------------------
The source is flat art composited on white, in three brand oranges: #FF6A00, #FF8B2D, #FFAD5C.
The obvious matte -- alpha = 1 - min(R,G,B)/255 -- is exact for the darkest of those and *wrong*
for the other two. #FFAD5C has a blue channel of 92, so that formula would render the light tint
at 64% opacity: the mark would go translucent wherever it is palest, and every edge would carry a
white halo on a coloured background.

Instead each pixel is projected onto the line from white toward each brand colour. The colour
whose line the pixel sits closest to is the one it belongs to; how far along that line it sits is
its alpha. An edge pixel halfway between white and #FF6A00 comes out as #FF6A00 at 50%, not as a
washed-out orange at 100%, which is what removes the halo rather than hiding it.

Below `FLOOR` the pixel is treated as paper. The source carries a very faint grey vignette in its
corners; without a floor that vignette survives as a 2%-opacity grey square around the mark.
"""

from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
WEB = HERE.parent
SOURCE = HERE / "baskfy-mark-source.png"
PUBLIC = WEB / "public" / "brand"
APP = WEB / "src" / "app"
#: The desk console's Jinja static directory, so both faces of the product carry one mark.
DESK_STATIC = WEB.parents[2] / "kite-momentum-rebalancer" / "app" / "static"

#: The palette the mark is drawn in, darkest first.
BRAND = ((0xFF, 0x6A, 0x00), (0xFF, 0x8B, 0x2D), (0xFF, 0xAD, 0x5C))

#: Alpha below this is paper, not art. Kills the source's faint corner vignette.
FLOOR = 0.06

#: A little air, so the mark is not flush to the icon's edge at every size.
PAD_RATIO = 0.06

#: Sizes the whole woven mark is legible at. See `THE 16px ACCEPTANCE` below for where it stops.
FULL_SIZES = (1024, 512, 192, 64)

#: Sizes drawn from the crossing glyph instead.
GLYPH_SIZES = (48, 32, 16)
ICO_SIZES = (16, 32, 48)

#: THE 16px ACCEPTANCE, AND THE CALL IT FORCED
#: -------------------------------------------
#: Rendered at actual size, the whole mark does not survive. The weave is six ribbons separated by
#: white channels roughly one pixel wide at 16px, and Lanczos averages them into an orange smear
#: with no shape. Eroding the art first (so the channels widen before the downsample) was tried at
#: three radii and only made the smear paler.
#:
#: So the favicon is a crop, as the brief anticipated: the strongest single ribbon-crossing, where
#: the deep #FF6A00 band sweeps across two pale ones. At 16px that is three high-contrast diagonals
#: and a hook — a shape, rather than a texture. Chosen from five candidates rendered at actual size
#: and compared; `docs/DECISIONS-MERGE.md` §M37.3 records the call.
#:
#: `(left, top, side)` in the 4096px source's own coordinates.
GLYPH_BOX = (2000, 1600, 1600)


def matte(image: Image.Image) -> Image.Image:
    """White out, alpha in — see the module docstring for why this is not a threshold."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float64)
    # Distance from white, per channel. Zero for paper, largest for the deepest orange.
    d = 255.0 - rgb

    best_alpha = np.zeros(rgb.shape[:2])
    best_residual = np.full(rgb.shape[:2], np.inf)
    best_colour = np.zeros_like(rgb)

    for colour in BRAND:
        axis = 255.0 - np.array(colour, dtype=np.float64)
        denominator = float(axis @ axis)
        if denominator == 0:  # pure white is not a brand colour
            continue
        alpha = (d @ axis) / denominator
        clamped = np.clip(alpha, 0.0, 1.0)
        residual = np.linalg.norm(d - clamped[..., None] * axis, axis=-1)
        take = residual < best_residual
        best_residual = np.where(take, residual, best_residual)
        best_alpha = np.where(take, clamped, best_alpha)
        best_colour = np.where(take[..., None], np.array(colour, dtype=np.float64), best_colour)

    best_alpha = np.where(best_alpha < FLOOR, 0.0, best_alpha)
    out = np.concatenate([best_colour, best_alpha[..., None] * 255.0], axis=-1)
    return Image.fromarray(out.round().astype("uint8"), mode="RGBA")


def square(image: Image.Image) -> Image.Image:
    """Trim to the art, then re-centre it in a padded square so no size crops differently."""
    box = image.getbbox()
    if box is None:
        raise SystemExit("the source matted to nothing — check FLOOR")
    art = image.crop(box)
    side = max(art.size)
    canvas = round(side * (1 + 2 * PAD_RATIO))
    out = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    out.paste(art, ((canvas - art.width) // 2, (canvas - art.height) // 2))
    return out


def write_ico(source: Image.Image, path: Path, sizes: tuple[int, ...]) -> None:
    """A multi-resolution .ico of PNG-encoded entries.

    Written here rather than with `Image.save(..., format="ICO")`, which re-samples from one
    bitmap. Each entry is resized from the full-resolution master independently, so the 16px
    glyph gets its own Lanczos pass rather than a downscale of a downscale.
    """
    entries = []
    for size in sizes:
        frame = source.resize((size, size), Image.LANCZOS)
        blob = _png_bytes(frame)
        entries.append((size, blob))

    offset = 6 + 16 * len(entries)
    header = struct.pack("<HHH", 0, 1, len(entries))
    directory = b""
    for size, blob in entries:
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(blob), offset
        )
        offset += len(blob)
    path.write_bytes(header + directory + b"".join(blob for _, blob in entries))


def _png_bytes(frame: Image.Image) -> bytes:
    buffer = BytesIO()
    frame.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def main() -> int:
    source = Image.open(SOURCE)
    matted = matte(source)
    master = square(matted)

    left, top, side = GLYPH_BOX
    glyph = square(matted.crop((left, top, left + side, top + side)))

    PUBLIC.mkdir(parents=True, exist_ok=True)

    for size in FULL_SIZES:
        master.resize((size, size), Image.LANCZOS).save(
            PUBLIC / f"logo-mark-{size}.png", optimize=True
        )
    for size in GLYPH_SIZES:
        glyph.resize((size, size), Image.LANCZOS).save(
            PUBLIC / f"logo-mark-{size}.png", optimize=True
        )
    glyph.resize((256, 256), Image.LANCZOS).save(PUBLIC / "logo-glyph-256.png", optimize=True)

    master.resize((180, 180), Image.LANCZOS).save(PUBLIC / "apple-icon.png", optimize=True)
    # Every entry in the .ico is 48px or smaller, so every entry is the glyph.
    write_ico(glyph, PUBLIC / "favicon.ico", ICO_SIZES)

    # Next 15 App Router picks these two up by filename alone — no <link> tags needed.
    master.resize((512, 512), Image.LANCZOS).save(APP / "icon.png", optimize=True)
    master.resize((180, 180), Image.LANCZOS).save(APP / "apple-icon.png", optimize=True)

    # The desk console is a Jinja app with its own static directory, and it gets the same face.
    if DESK_STATIC.exists():
        write_ico(glyph, DESK_STATIC / "favicon.ico", ICO_SIZES)
        master.resize((180, 180), Image.LANCZOS).save(
            DESK_STATIC / "apple-touch-icon.png", optimize=True
        )
        master.resize((512, 512), Image.LANCZOS).save(DESK_STATIC / "logo-mark.png", optimize=True)

    print(f"master {master.size[0]}px · glyph {glyph.size[0]}px")
    for path in sorted(PUBLIC.iterdir()):
        print(f"  {path.name:24} {path.stat().st_size:>8,} bytes")
    for name in ("icon.png", "apple-icon.png"):
        print(f"  src/app/{name:16} {(APP / name).stat().st_size:>8,} bytes")
    if DESK_STATIC.exists():
        for name in ("favicon.ico", "apple-touch-icon.png", "logo-mark.png"):
            print(f"  desk/{name:19} {(DESK_STATIC / name).stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
