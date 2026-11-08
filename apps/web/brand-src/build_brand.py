"""Cut the transparent master into the icon set the app and the desk console need (M38).

Run:  node scripts/build-brand-svg.mjs        # stage one: the vector, and a 4096px master
      uv run --with pillow python apps/web/brand-src/build_brand.py

Stage two of two. `scripts/build-brand-svg.mjs` turns the supplied `logo.svg` into
`brand-src/mark-master.png`, already transparent; this cuts that into every size and writes the
`.ico`.

M37 built these from a raster on a white background and had to *reconstruct* the alpha channel by
projecting each pixel onto the line from white toward each brand colour -- the naive
`1 - min(R,G,B)/255` renders #FFAD5C at 64% opacity and haloes every edge. None of that is needed
now: the vector's channels are genuinely empty, so the mark sits on the dark theme without
anything being inferred. The matte went with the source it existed for
(`docs/DECISIONS-MERGE.md` §M38.1).
"""

from __future__ import annotations

import struct
from io import BytesIO
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
WEB = HERE.parent
SOURCE = HERE / "mark-master.png"
PUBLIC = WEB / "public" / "brand"
APP = WEB / "src" / "app"
#: The desk console's Jinja static directory, so both faces of the product carry one mark.
DESK_STATIC = WEB.parents[2] / "kite-momentum-rebalancer" / "app" / "static"

#: A little air, so the mark is not flush to the icon's edge at every size.
#:
#: This lives here and NOT in the SVG. An icon is cropped and masked by the operating system and
#: needs the margin baked in; an SVG drawn beside a line of type does not, and a margin there comes
#: straight off the mark's optical size. `build-brand-svg.mjs` keeps the vector tight to its ink.
PAD_RATIO = 0.06

#: Every size is the whole mark. See `THE 16px ACCEPTANCE` below.
FULL_SIZES = (1024, 512, 192, 64, 48, 32, 16)
ICO_SIZES = (16, 32, 48)

#: THE 16px ACCEPTANCE, AND THE CALL M38 REVERSED
#: ----------------------------------------------
#: M37 rasterised a white-background PNG and the whole mark did not survive at 16px: the weave is
#: six ribbons separated by roughly one pixel of channel, and the downsample averaged them into a
#: smear. The favicon became a crop of the strongest ribbon-crossing.
#:
#: **The vector changed the answer, and the answer was re-measured rather than assumed.** Rendered
#: from `logo.svg`, 16px is soft but keeps a silhouette you can identify, and 32px is unambiguously
#: the woven basket. Held against the crop at both sizes on paper and on ink, the crop is sharper
#: and says nothing -- it reads as anonymous diagonal stripes. A favicon exists to be recognised,
#: so sharpness that costs recognition is the wrong trade.
#:
#: One icon at every size. `docs/DECISIONS-MERGE.md` §M38.3 records the reversal and the comparison
#: it rests on; `brand-src/_final_glyph.png` is regenerable from the snippet in that entry.


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
    source = Image.open(SOURCE).convert("RGBA")
    master = square(source)

    PUBLIC.mkdir(parents=True, exist_ok=True)

    for size in FULL_SIZES:
        master.resize((size, size), Image.LANCZOS).save(
            PUBLIC / f"logo-mark-{size}.png", optimize=True
        )

    master.resize((180, 180), Image.LANCZOS).save(PUBLIC / "apple-icon.png", optimize=True)
    write_ico(master, PUBLIC / "favicon.ico", ICO_SIZES)

    # Next 15 App Router picks these two up by filename alone — no <link> tags needed.
    master.resize((512, 512), Image.LANCZOS).save(APP / "icon.png", optimize=True)
    master.resize((180, 180), Image.LANCZOS).save(APP / "apple-icon.png", optimize=True)
    write_ico(master, APP / "favicon.ico", ICO_SIZES)

    # The desk console is a Jinja app with its own static directory, and it gets the same face.
    if DESK_STATIC.exists():
        # The vector too: the desk console's brand mark is drawn at 28px and deserves the same
        # crispness the web app gets. Copied rather than symlinked -- the two trees deploy apart.
        (DESK_STATIC / "logo.svg").write_bytes((PUBLIC / "logo.svg").read_bytes())
        write_ico(master, DESK_STATIC / "favicon.ico", ICO_SIZES)
        master.resize((180, 180), Image.LANCZOS).save(
            DESK_STATIC / "apple-touch-icon.png", optimize=True
        )
        master.resize((512, 512), Image.LANCZOS).save(DESK_STATIC / "logo-mark.png", optimize=True)

    print(f"master {master.size[0]}px")
    for path in sorted(PUBLIC.iterdir()):
        print(f"  {path.name:24} {path.stat().st_size:>8,} bytes")
    for name in ("icon.png", "apple-icon.png", "favicon.ico"):
        print(f"  src/app/{name:16} {(APP / name).stat().st_size:>8,} bytes")
    if DESK_STATIC.exists():
        for name in ("favicon.ico", "apple-touch-icon.png", "logo-mark.png", "logo.svg"):
            print(f"  desk/{name:19} {(DESK_STATIC / name).stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
