"""Visual change between two screenshots of the same page (blueprint §10.6). Pillow only; no extra service."""
from __future__ import annotations

from PIL import Image, ImageChops


def changed_ratio(a_path: str, b_path: str, threshold: int = 24, max_side: int = 640) -> float:
    """Share of pixels (0..1) that differ by more than `threshold` grey levels, after scaling both to the same
    small size (so compression noise and 1-px shifts in a header don't count as change)."""
    with Image.open(a_path) as a0, Image.open(b_path) as b0:
        a, b = a0.convert("L"), b0.convert("L")
        h = min(a.height, b.height)
        a, b = a.crop((0, 0, a.width, h)), b.crop((0, 0, b.width, h))
        scale = max_side / max(a.width, h)
        size = (max(1, int(a.width * scale)), max(1, int(h * scale)))
        a, b = a.resize(size), b.resize(size)
        diff = ImageChops.difference(a, b).point(lambda p: 255 if p > threshold else 0)
        return sum(diff.histogram()[255:]) / float(size[0] * size[1])
