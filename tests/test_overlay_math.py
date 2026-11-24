"""Where the numbers layer draws (``nova.island.overlay``): physical px -> the layer's logical px, labels on screen."""

from nova.island.overlay import ring_rect, tag_box, to_logical


def test_to_logical_at_200_percent_on_a_second_monitor():
    monitor = (2880, 0, 5760, 1800)
    assert to_logical((2980, 200, 3180, 260), monitor, (1440, 0), 2.0) == (50.0, 100.0, 100.0, 30.0)


def test_tag_labels_stay_on_screen():
    x, y, w, h = tag_box(0.0, 0.0, "12", 11.0, (1440.0, 900.0))
    assert (x, y) == (0.0, 0.0) and w > 0 and h > 0
    x, y, w, h = tag_box(1439.0, 899.0, "99", 11.0, (1440.0, 900.0))
    assert x + w <= 1440.0 and y + h <= 900.0


def test_the_guide_ring_goes_around_the_control_and_stays_on_screen():
    monitor = (2880, 0, 5760, 1800)
    assert ring_rect((2980, 200, 3180, 260), monitor, 2.0, (1440.0, 900.0)) == (44.0, 94.0, 112.0, 42.0)
    assert ring_rect((2880, 0, 2900, 20), monitor, 2.0, (1440.0, 900.0)) == (0.0, 0.0, 16.0, 16.0)  # in the corner
    x, y, w, h = ring_rect((5740, 1780, 5760, 1800), monitor, 2.0, (1440.0, 900.0))
    assert x + w == 1440.0 and y + h == 900.0
