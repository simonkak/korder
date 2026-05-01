#!/usr/bin/env python3
"""align-osd-screenshots.py — line up OSD screenshots to a uniform canvas.

When you take screenshots of the OSD across different states (Listening /
Thinking / Executing / Awaiting / …), Spectacle's region-grab usually
yields slightly different crops each time — variable image dimensions,
the pill anchored at varying offsets. Comparing them side-by-side as
documentation feels off because the eye reads the misalignment as the
*pill* shifting, when really only the camera moved.

This script realigns a directory of screenshots so:

  - all outputs share identical pixel dimensions (real cropped pixels —
    no transparent padding);
  - the OSD pill lands at the same (x, y) inside every output;
  - file sizes shrink, since per-image transparent padding compresses
    poorly compared to a tight uniform crop.

How it works:

  1. For each input PNG, builds a mask of pixels matching the pill's fill
     signature (dark + low-chroma — RGB ≈ 25–70, channel spread ≤ 12) and
     runs an iterative-BFS flood fill to find the largest connected
     region. That region's bounding box is the pill — this is robust
     against incidental dark patches in the wallpaper that satisfy the
     color test but aren't connected to the pill.
  2. Picks the smallest "wallpaper margin" (left/top/right/bottom) seen
     across all inputs as the shared margin for every output. Pill width
     in the canvas takes max(pill_width) so the widest pill content fits.
  3. Crops each input around its detected pill, with consistent margins.
     Because every margin is the smallest-available-anywhere, no crop
     ever exceeds its source image's bounds.

Usage:

    uv run python scripts/align-osd-screenshots.py
        — defaults to assets/screens/, processes a hardcoded list of OSD
          state shots, writes <name>_centered.png alongside each.

    uv run python scripts/align-osd-screenshots.py path/to/dir 1.png 2.png
        — explicit input dir + filenames; same naming convention for
          outputs.

Originals are never modified.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image


# Default input set — keep aligned with the screenshots in
# assets/screens/. Add new state captures here when they show up.
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "assets" / "screens"
DEFAULT_INPUTS = [
    "command_invocation.png",
    "command_thinking.png",
    "command_execution_feedback.png",
    "command_done.png",
    "command_pending_action.png",
]


def find_pill_bbox(img: Image.Image) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the OSD pill within ``img``.

    Strategy: build a mask of pixels matching the pill's fill (dark +
    low chroma) and pick the largest connected component. The pill on
    Breeze Dark with KWin blur reads in the RGB ~25–70 range with channel
    spread (max−min) ≤ 12; wallpaper has either higher chroma (sky blue,
    sunlit forest) or higher brightness (mountain ridges).

    Falls back to the whole image rectangle when nothing matches — a
    visible breadcrumb that the heuristic missed, vs. a silent failure.
    """
    rgba = img.convert("RGBA")
    w, h = rgba.size
    px = rgba.load()
    mask = bytearray(w * h)
    for y in range(h):
        row_off = y * w
        for x in range(w):
            r, g, b, _ = px[x, y]
            chroma = max(r, g, b) - min(r, g, b)
            brightness = (r + g + b) // 3
            if 12 <= brightness <= 70 and chroma <= 12:
                mask[row_off + x] = 1

    visited = bytearray(w * h)
    best_bbox: tuple[int, int, int, int] | None = None
    best_size = 0
    for y0 in range(h):
        for x0 in range(w):
            idx0 = y0 * w + x0
            if not mask[idx0] or visited[idx0]:
                continue
            stack = [(x0, y0)]
            min_x = max_x = x0
            min_y = max_y = y0
            size = 0
            while stack:
                x, y = stack.pop()
                if x < 0 or x >= w or y < 0 or y >= h:
                    continue
                idx = y * w + x
                if visited[idx] or not mask[idx]:
                    continue
                visited[idx] = 1
                size += 1
                if x < min_x: min_x = x
                if x > max_x: max_x = x
                if y < min_y: min_y = y
                if y > max_y: max_y = y
                stack.append((x + 1, y))
                stack.append((x - 1, y))
                stack.append((x, y + 1))
                stack.append((x, y - 1))
            if size > best_size:
                best_size = size
                best_bbox = (min_x, min_y, max_x + 1, max_y + 1)

    if best_bbox is None:
        return (0, 0, w, h)
    return best_bbox


def align(input_dir: Path, names: list[str]) -> None:
    images: list[tuple[Path, Image.Image, tuple[int, int, int, int]]] = []
    for name in names:
        path = input_dir / name
        img = Image.open(path).convert("RGBA")
        bbox = find_pill_bbox(img)
        l, t, r, b = bbox
        print(f"{name:<40} src={img.size}  pill=({l},{t},{r},{b}) "
              f"size={r - l}×{b - t}")
        images.append((path, img, bbox))

    # Smallest available margin in each direction across all inputs;
    # using min() guarantees no source image gets cropped beyond its
    # actual pixel bounds when we apply the shared margin uniformly.
    left_margin = min(l for _, _, (l, _, _, _) in images)
    top_margin = min(t for _, _, (_, t, _, _) in images)
    right_margin = min(img.size[0] - r for _, img, (_, _, r, _) in images)
    bottom_margin = min(img.size[1] - b for _, img, (_, _, _, b) in images)
    max_pill_w = max(r - l for _, _, (l, _, r, _) in images)
    max_pill_h = max(b - t for _, _, (_, t, _, b) in images)

    canvas_w = left_margin + max_pill_w + right_margin
    canvas_h = top_margin + max_pill_h + bottom_margin

    print(f"\nMargins: left={left_margin} top={top_margin} "
          f"right={right_margin} bottom={bottom_margin}")
    print(f"Max pill: {max_pill_w}×{max_pill_h}")
    print(f"Canvas:   {canvas_w}×{canvas_h}")

    for path, img, (l, t, r, b) in images:
        crop_left = l - left_margin
        crop_top = t - top_margin
        crop_right = crop_left + canvas_w
        crop_bottom = crop_top + canvas_h
        iw, ih = img.size
        # Should not trigger given the margin math above; assert anyway
        # so a future regression in find_pill_bbox is loud.
        assert 0 <= crop_left and crop_right <= iw, (
            f"{path.name}: crop x out of range ({crop_left}→{crop_right} vs {iw})")
        assert 0 <= crop_top and crop_bottom <= ih, (
            f"{path.name}: crop y out of range ({crop_top}→{crop_bottom} vs {ih})")
        out = img.crop((crop_left, crop_top, crop_right, crop_bottom))
        out_path = path.with_name(path.stem + "_centered" + path.suffix)
        out.save(out_path)
        print(f"  {path.name}  crop=({crop_left},{crop_top},"
              f"{crop_right},{crop_bottom})  → {out_path.name}")


def main(argv: list[str]) -> int:
    if len(argv) >= 2:
        input_dir = Path(argv[1]).resolve()
        names = argv[2:] if len(argv) > 2 else sorted(
            p.name for p in input_dir.glob("*.png")
            if not p.stem.endswith("_centered")
        )
    else:
        input_dir = DEFAULT_DIR
        names = DEFAULT_INPUTS

    if not names:
        print("no input PNGs found", file=sys.stderr)
        return 1

    align(input_dir, names)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
