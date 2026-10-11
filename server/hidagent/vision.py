"""Small image checks on screen frames: did the screen change, and where is the pointer."""

import io

from PIL import Image, ImageChops


def _gray(jpeg: bytes) -> Image.Image:
    return Image.open(io.BytesIO(jpeg)).convert("L")


def changed(before: bytes | None, after: bytes | None, threshold: float = 1.0) -> bool:
    """True when the screen content changed. Tiny thumbnails ignore the pointer and JPEG noise."""
    if not before or not after:
        return True
    a, b = (_gray(f).resize((32, 64), Image.BILINEAR) for f in (before, after))
    diff = ImageChops.difference(a, b).tobytes()
    return sum(diff) / len(diff) > threshold


def pointer(before: bytes, after: bytes, near: tuple[float, float], shift: float) -> tuple[float, float] | None:
    """Where the pointer is in `before`, given it moved right by `shift` (fraction of width) in `after`.

    Only the pointer moved between the two frames, so the changed pixels around `near` are the pointer
    at both spots; their centroid sits shift/2 to the right of the first one. None if unclear.
    """
    a, b = _gray(before), _gray(after)
    if a.size != b.size:
        return None
    w, h = a.size
    left, top = max(0, int((near[0] - 0.12) * w)), max(0, int((near[1] - 0.08) * h))
    box = (left, top, min(w, int((near[0] + 0.12) * w)), min(h, int((near[1] + 0.08) * h)))
    diff = ImageChops.difference(a.crop(box), b.crop(box)).tobytes()
    bw = box[2] - box[0]
    hits = [(i % bw, i // bw) for i, v in enumerate(diff) if v > 30]
    if not 15 <= len(hits) <= len(diff) // 4:  # nothing moved, or much more than a pointer
        return None
    cx = sum(x for x, _ in hits) / len(hits) + left
    cy = sum(y for _, y in hits) / len(hits) + top
    return cx / w - shift / 2, cy / h
