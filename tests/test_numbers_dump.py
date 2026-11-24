"""`nova numbers --dump`: the saved record of each tag (pure)."""

from nova.actions.marks import Box, Mark, Target
from nova.actions.numbers_dump import records


def test_records_keep_screen_and_picture_boxes():
    marks = [Mark(1, Target(Box(110, 220, 150, 240), "Save", "button", "uia")),
             Mark(2, Target(Box(300, 400, 380, 420), "Chapter 3", "text", "ocr"))]
    got = records(marks, origin=(100, 200))
    assert got[0] == {"number": 1, "name": "Save", "role": "button", "source": "uia",
                      "box": [110, 220, 150, 240], "in_png": [10, 20, 50, 40]}
    assert got[1]["source"] == "ocr" and got[1]["in_png"] == [200, 200, 280, 220]


def test_no_marks_no_records():
    assert records([]) == []
