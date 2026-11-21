"""Where the numbers layer draws (``nova.island.overlay``): physical px -> the layer's logical px, labels on screen."""

from nova.island.overlay import tag_box, to_logical


def test_to_logical_at_200_percent_on_a_second_monitor():
    monitor = (2880, 0, 5760, 1800)
    assert to_logical((2980, 200, 3180, 260), monitor, (1440, 0), 2.0) == (50.0, 100.0, 100.0, 30.0)


def test_tag_labels_stay_on_screen():
    x, y, w, h = tag_box(0.0, 0.0, "12", 11.0, (1440.0, 900.0))
    assert (x, y) == (0.0, 0.0) and w > 0 and h > 0
    x, y, w, h = tag_box(1439.0, 899.0, "99", 11.0, (1440.0, 900.0))
    assert x + w <= 1440.0 and y + h <= 900.0
