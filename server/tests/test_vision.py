import io

import pytest
from PIL import Image, ImageDraw

from hidagent.vision import changed, pointer

W, H = 498, 1080  # a mirrored iPhone frame


def screen(pointer_at=None, button=False) -> bytes:
    """A synthetic home screen: gradient background, optional button and pointer dot."""
    img = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    for y in range(H):
        draw.line([(0, y), (W, y)], fill=(40, 60 + y // 10, 120))
    if button:
        draw.rectangle([150, 400, 350, 600], fill=(250, 250, 250))
    if pointer_at:
        x, y = pointer_at
        draw.ellipse([x - 12, y - 12, x + 12, y + 12], fill=(200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=75)
    return buf.getvalue()


def test_pointer_move_is_not_a_screen_change():
    assert not changed(screen((260, 856)), screen((272, 856)))


def test_new_content_is_a_screen_change():
    assert changed(screen((260, 856)), screen((260, 856), button=True))


def test_missing_frame_counts_as_changed():
    assert changed(None, screen())


def test_pointer_is_found_from_a_nudge():
    shift = 12 / W
    found = pointer(screen((262, 856)), screen((274, 856)), near=(0.5, 0.8), shift=shift)
    assert found is not None
    assert found[0] * W == pytest.approx(262, abs=3)
    assert found[1] * H == pytest.approx(856, abs=3)


def test_pointer_not_found_when_nothing_moved():
    frame = screen((262, 856))
    assert pointer(frame, frame, near=(0.5, 0.8), shift=12 / W) is None
