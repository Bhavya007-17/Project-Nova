"""Screenshots (prompt 07c §7): the byte shuffling and the file name."""

import struct
from datetime import datetime

from nova.actions.capture import screenshot_name, to_dib, to_rgb

# 2x2 BGRA, top-down: red, green / blue, white
BGRA = bytes([0, 0, 255, 255, 0, 255, 0, 255, 255, 0, 0, 255, 255, 255, 255, 255])


def test_to_rgb():
    assert to_rgb(BGRA, 2, 2) == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])


def test_to_dib_is_bottom_up_24_bit_with_padded_rows():
    dib = to_dib(BGRA, 2, 2)
    size, width, height, planes, bits, compression, image = struct.unpack("<IiiHHII", dib[:24])
    assert (size, width, height, planes, bits, compression) == (40, 2, 2, 1, 24, 0)
    assert image == 16 and len(dib) == 40 + 16  # 6 bytes a row, padded to 8
    rows = dib[40:]
    assert rows[:8] == bytes([255, 0, 0, 255, 255, 255, 0, 0])  # bottom row first: blue, white (BGR)
    assert rows[8:] == bytes([0, 0, 255, 0, 255, 0, 0, 0])  # then red, green


def test_screenshot_name_counts_up_when_taken():
    now = datetime(2026, 11, 21, 14, 30, 15)
    assert screenshot_name(now, lambda name: False) == "Screenshot 2026-11-21 143015.png"
    taken = {"Screenshot 2026-11-21 143015.png", "Screenshot 2026-11-21 143015 (2).png"}
    assert screenshot_name(now, taken.__contains__) == "Screenshot 2026-11-21 143015 (3).png"
