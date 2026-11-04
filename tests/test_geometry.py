"""Window geometry (SPEC §8): snap areas, layouts, centering, moving between monitors. Pure math."""

import pytest

from nova.actions.native import Monitor
from nova.actions.windows import area_rect, centered, close_enough, pick_monitor, translated

WORK = (0, 0, 2880, 1728)  # 2880x1800 at 200 %, taskbar 72 px


@pytest.mark.parametrize(
    ("area", "rect"),
    [
        ("left", (0, 0, 1440, 1728)),
        ("right", (1440, 0, 2880, 1728)),
        ("top", (0, 0, 2880, 864)),
        ("bottom", (0, 864, 2880, 1728)),
        ("top_left", (0, 0, 1440, 864)),
        ("bottom_right", (1440, 864, 2880, 1728)),
        ("left_third", (0, 0, 960, 1728)),
        ("middle_third", (960, 0, 1920, 1728)),
        ("right_third", (1920, 0, 2880, 1728)),
        ("full", WORK),
    ],
)
def test_areas_tile_the_work_area(area, rect):
    assert area_rect(WORK, area) == rect


def test_areas_on_a_monitor_left_of_the_primary():
    assert area_rect((-1920, 0, 0, 1040), "right") == (-960, 0, 0, 1040)


def test_centered_keeps_size_but_fits():
    assert centered(WORK, (1000, 800)) == (940, 464, 1940, 1264)
    assert centered(WORK, (4000, 3000)) == (144, 86, 2736, 1641)  # too big: 90 % of the work area


LAPTOP = Monitor(1, (0, 0, 2880, 1800), (0, 0, 2880, 1728), True, r"\\.\DISPLAY1", 192)
EXTERNAL = Monitor(2, (2880, 0, 4800, 1080), (2880, 0, 4800, 1040), False, r"\\.\DISPLAY2", 96)
LEFT = Monitor(3, (-1920, 0, 0, 1080), (-1920, 0, 0, 1040), False, r"\\.\DISPLAY3", 96)


def test_pick_monitor():
    two = [LAPTOP, EXTERNAL]
    assert pick_monitor(two, LAPTOP.handle, "other") == EXTERNAL
    assert pick_monitor(two, EXTERNAL.handle, "other") == LAPTOP
    assert pick_monitor(two, LAPTOP.handle, "next") == EXTERNAL
    assert pick_monitor(two, EXTERNAL.handle, "next") == LAPTOP  # wraps
    assert pick_monitor(two, EXTERNAL.handle, "primary") == LAPTOP
    three = [LEFT, LAPTOP, EXTERNAL]
    assert pick_monitor(three, LAPTOP.handle, "left") == LEFT
    assert pick_monitor(three, LAPTOP.handle, "right") == EXTERNAL
    assert pick_monitor([LAPTOP], LAPTOP.handle, "other") is None  # one monitor: nowhere to go


def test_translated_keeps_relative_place_and_scales_for_dpi():
    # Left half of the 200 % laptop -> left half-ish of the 100 % external, same logical size.
    rect = translated((0, 0, 1440, 1728), LAPTOP, EXTERNAL)
    assert rect == (2880, 0, 3600, 864)
    # Never bigger than the destination work area.
    big = translated((0, 0, 2880, 1728), LAPTOP, Monitor(4, (0, 0, 1280, 720), (0, 0, 1280, 680), False, "x", 96))
    assert big[2] - big[0] <= 1280 and big[3] - big[1] <= 680


def test_close_enough():
    assert close_enough((0, 0, 100, 100), (2, -1, 101, 99))
    assert not close_enough((0, 0, 100, 100), (20, 0, 100, 100))
