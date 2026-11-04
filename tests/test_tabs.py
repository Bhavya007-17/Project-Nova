"""Which Chrome tab: one already on a site he opens again, the tab he names. Pure."""

from nova.chrome.bridge import TabInfo
from nova.chrome.tabs import best_tab, same_site, window_title_matches

TABS = (
    TabInfo(1, 10, "https://www.youtube.com/watch?v=abc", "lofi hip hop radio - YouTube", active=False, last=100.0),
    TabInfo(2, 10, "https://canvas.vt.edu/courses/11/assignments/501", "HW 5: ME 3124", active=True, last=300.0),
    TabInfo(3, 10, "https://mail.google.com/mail/u/0/#inbox", "Inbox (3) - bhavya@vt.edu - Gmail", active=False, last=200.0),
    TabInfo(4, 20, "https://docs.google.com/document/d/x/edit", "Lab writeup - Google Docs", active=True, last=50.0),
    TabInfo(5, 20, "https://www.youtube.com/", "YouTube", active=False, last=20.0),
)


def test_same_site():
    assert same_site("https://canvas.vt.edu/courses/11", "https://canvas.vt.edu")
    assert same_site("https://www.youtube.com/watch?v=1", "https://youtube.com/")
    assert not same_site("https://docs.google.com/x", "https://mail.google.com")
    assert same_site("https://github.com/Bhavya007-17/nova", "https://github.com/Bhavya007-17")
    assert not same_site("https://github.com/someone/else", "https://github.com/Bhavya007-17")
    assert not same_site("chrome://newtab/", "https://canvas.vt.edu")


def test_named_tabs():
    assert best_tab(TABS, "youtube")[0].id == 1  # the most recently used of the two
    assert best_tab(TABS, "gmail")[0].id == 3
    assert best_tab(TABS, "canvas")[0].id == 2
    assert best_tab(TABS, "lab writeup")[0].id == 4
    assert best_tab(TABS, "the docs one")[0].id == 4
    assert best_tab(TABS, "spotify") == []


def test_window_title_matches_the_tab():
    assert window_title_matches("HW 5: ME 3124 - Google Chrome", "HW 5: ME 3124")
    assert window_title_matches("HW 5: ME 3124 - Google Chrome for Testing", "HW 5: ME 3124")
    assert not window_title_matches("Inbox - Google Chrome", "HW 5: ME 3124")
    assert not window_title_matches("Untitled - Notepad", "")
