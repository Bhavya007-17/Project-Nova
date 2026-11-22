"""UI Automation beyond the focused control (SPEC §8.1): every clickable thing in a window in one cached batch read,
acting on one of them (Invoke / Toggle / Select / Expand / default action), and ScrollPattern with a receipt.

Runs on FocusReader's thread (``automation`` is its IUIAutomation). Hand-written ctypes like ``uia.py``; slots and
ids from UIAutomationClient.h (SDK 10.0.26100). Rectangles are physical virtual-desktop pixels (the calls run
per-monitor DPI aware).
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import POINTER, byref, c_double, c_int, c_void_p, wintypes
from dataclasses import dataclass

from . import native

_log = logging.getLogger(__name__)

_oleaut32 = ctypes.WinDLL("oleaut32")
_oleaut32.SysFreeString.argtypes = [c_void_p]
_oleaut32.SysFreeString.restype = None

# IUIAutomation
_ELEMENT_FROM_HANDLE, _ELEMENT_FROM_POINT, _GET_FOCUSED, _ELEMENT_FROM_HANDLE_BUILD_CACHE = 6, 7, 8, 10
_CREATE_TREE_WALKER, _CONTROL_VIEW_CONDITION, _CREATE_CACHE_REQUEST = 13, 18, 20
# IUIAutomationElement
_SET_FOCUS, _GET_CACHED_PROPERTY_VALUE, _GET_CURRENT_PATTERN_AS, _GET_CACHED_CHILDREN = 3, 12, 14, 19
_CURRENT_BOUNDING_RECTANGLE = 43
_CACHED_CONTROL_TYPE, _CACHED_NAME, _CACHED_IS_ENABLED = 53, 55, 60
_CACHED_IS_OFFSCREEN, _CACHED_BOUNDING_RECTANGLE = 70, 75
# IUIAutomationCacheRequest / ElementArray / TreeWalker
_CACHE_ADD_PROPERTY, _CACHE_PUT_TREE_SCOPE = 3, 7
_ARRAY_LENGTH, _ARRAY_ELEMENT = 3, 4
_WALK_PARENT, _WALK_FIRST_CHILD_CACHED, _WALK_NEXT_SIBLING_CACHED = 3, 10, 12
# patterns: (id, IID, slot of the one call we make)
_INVOKE = (10000, native.guid("{fb377fbe-8ea6-46d5-9c73-6499642d3059}"), 3)  # Invoke()
_TOGGLE = (10015, native.guid("{94cf8058-9b8d-4ab9-8bfd-4cd0a33c8c70}"), 3)  # Toggle()
_SELECT = (10010, native.guid("{a8efa66a-0fda-421a-9194-38021f3578ea}"), 3)  # Select()
_EXPAND = (10005, native.guid("{619be086-1f4e-4ee4-bafa-210128738730}"), 3)  # Expand()
_LEGACY = (10018, native.guid("{828055ad-355b-4435-86d5-3b51c14a9b1b}"), 4)  # DoDefaultAction()
_SCROLL_ID, _IID_SCROLL = 10004, native.guid("{88f4d42a-e881-459d-a77c-73bbbb7e02dc}")
_SCROLL, _SET_PERCENT, _H_PERCENT, _V_PERCENT, _H_VIEW, _V_VIEW, _H_SCROLLABLE, _V_SCROLLABLE = 3, 4, 5, 6, 7, 8, 9, 10
_NO_SCROLL = -1.0
_LARGE_DECREMENT, _NO_AMOUNT, _LARGE_INCREMENT = 0, 2, 3
# property ids
_P_RECT, _P_CONTROL_TYPE, _P_NAME, _P_FOCUSABLE, _P_ENABLED, _P_OFFSCREEN = 30001, 30003, 30005, 30009, 30010, 30022
_P_EXPAND, _P_INVOKE, _P_SCROLL, _P_SELECT, _P_TOGGLE = 30028, 30031, 30034, 30036, 30041
_PATTERN_PROPS = {"invoke": _P_INVOKE, "toggle": _P_TOGGLE, "select": _P_SELECT, "expand": _P_EXPAND,
                  "scroll": _P_SCROLL, "focusable": _P_FOCUSABLE}
_SUBTREE = 7  # TreeScope: element + children + descendants
_DOCUMENT = 50030

ROLES = {
    50000: "button", 50001: "calendar", 50002: "checkbox", 50003: "combo box", 50004: "edit", 50005: "hyperlink",
    50006: "image", 50007: "list item", 50008: "list", 50009: "menu", 50010: "menu bar", 50011: "menu item",
    50012: "progress bar", 50013: "radio button", 50014: "scroll bar", 50015: "slider", 50016: "spinner",
    50017: "status bar", 50018: "tab", 50019: "tab item", 50020: "text", 50021: "toolbar", 50022: "tooltip",
    50023: "tree", 50024: "tree item", 50025: "custom", 50026: "group", 50027: "thumb", 50028: "data grid",
    50029: "data item", 50030: "document", 50031: "split button", 50032: "window", 50033: "pane", 50034: "header",
    50035: "header item", 50036: "table", 50037: "title bar", 50038: "separator",
}
_CLICKABLE_TYPES = {50000, 50002, 50003, 50004, 50005, 50007, 50011, 50013, 50015, 50019, 50024, 50029, 50031,
                    50035}
_NEVER = {50012, 50014, 50017, 50022, 50027, 50032, 50037, 50038}  # progress, scroll bar, status, tooltip, ...


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("value", ctypes.c_longlong), ("extra", ctypes.c_longlong)]


@dataclass(frozen=True)
class Element:
    index: int  # position in the snapshot (what ``act`` takes)
    name: str
    control_type: int
    rect: tuple[int, int, int, int]  # left, top, right, bottom (physical px)
    patterns: frozenset[str]  # invoke | toggle | select | expand | scroll | focusable

    @property
    def role(self) -> str:
        return ROLES.get(self.control_type, "")

    @property
    def clickable(self) -> bool:
        if self.control_type in _NEVER:
            return False
        return self.control_type in _CLICKABLE_TYPES or bool(self.patterns & {"invoke", "toggle", "select", "expand"})


def _out_pointer(pointer: c_void_p, slot: int, *args: object, types: tuple = ()) -> c_void_p | None:
    out = c_void_p()
    if native.com_call(pointer, slot, *args, byref(out), types=(*types, POINTER(c_void_p))) < 0 or not out:
        return None
    return out


def _bstr(pointer: c_void_p, slot: int) -> str:
    value = c_void_p()
    if native.com_call(pointer, slot, byref(value), types=(POINTER(c_void_p),)) < 0 or not value.value:
        return ""
    try:
        return ctypes.wstring_at(value.value)
    finally:
        _oleaut32.SysFreeString(value)


def _int(pointer: c_void_p, slot: int) -> int:
    value = c_int()
    native.com_call(pointer, slot, byref(value), types=(POINTER(c_int),))
    return value.value


def _rect(pointer: c_void_p, slot: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    if native.com_call(pointer, slot, byref(r), types=(POINTER(wintypes.RECT),)) < 0:
        return (0, 0, 0, 0)
    return (r.left, r.top, r.right, r.bottom)


def _cached_bool(element: c_void_p, prop: int) -> bool:
    v = _VARIANT()
    if native.com_call(element, _GET_CACHED_PROPERTY_VALUE, prop, byref(v), types=(c_int, POINTER(_VARIANT))) < 0:
        return False
    return v.vt == 11 and (v.value & 0xFFFF) != 0  # VT_BOOL, VARIANT_TRUE = -1


def add_ref(pointer: c_void_p) -> None:
    vtable = ctypes.cast(pointer, POINTER(POINTER(c_void_p)))[0]
    ctypes.WINFUNCTYPE(ctypes.c_ulong, c_void_p)(vtable[1])(pointer)


def _cache_request(automation: c_void_p) -> c_void_p | None:
    request = _out_pointer(automation, _CREATE_CACHE_REQUEST)
    if request is None:
        return None
    for prop in (_P_RECT, _P_CONTROL_TYPE, _P_NAME, _P_ENABLED, _P_OFFSCREEN, *_PATTERN_PROPS.values()):
        native.com_call(request, _CACHE_ADD_PROPERTY, prop, types=(c_int,))
    return request


def _read(element: c_void_p) -> tuple[str, int, tuple[int, int, int, int], bool, frozenset[str]] | None:
    """(name, control type, rect, usable, patterns) from the cache."""
    rect = _rect(element, _CACHED_BOUNDING_RECTANGLE)
    if rect[2] <= rect[0] or rect[3] <= rect[1]:
        return None
    enabled = wintypes.BOOL()
    offscreen = wintypes.BOOL()
    native.com_call(element, _CACHED_IS_ENABLED, byref(enabled), types=(POINTER(wintypes.BOOL),))
    native.com_call(element, _CACHED_IS_OFFSCREEN, byref(offscreen), types=(POINTER(wintypes.BOOL),))
    patterns = frozenset(name for name, prop in _PATTERN_PROPS.items() if _cached_bool(element, prop))
    return (_bstr(element, _CACHED_NAME)[:120], _int(element, _CACHED_CONTROL_TYPE), rect,
            bool(enabled.value) and not offscreen.value, patterns)


def _visible(rect: tuple[int, int, int, int], window: tuple[int, int, int, int]) -> bool:
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    return window[0] <= cx < window[2] and window[1] <= cy < window[3]


def snapshot(automation: c_void_p, hwnd: int, *, skip_documents: bool = False,
             cap: int = 1500) -> tuple[list[Element], list[c_void_p]]:
    """Every usable element in the window, in tree order, with its element pointer (caller releases them).
    ``skip_documents`` walks the tree without going into web pages (Chrome: its own toolbar only, the page map
    covers the page and asking for it would switch on Chrome's accessibility tree for the page)."""
    with native.dpi_aware():
        window = native.window_rect(hwnd)
        request = _cache_request(automation)
        if request is None:
            return [], []
        try:
            if skip_documents:
                root = _out_pointer(automation, _ELEMENT_FROM_HANDLE, c_void_p(hwnd), types=(c_void_p,))
                raw = _walk(automation, root, request, cap) if root is not None else []
                native.com_release(root)
            else:
                raw = _subtree(automation, hwnd, request, cap)
        finally:
            native.com_release(request)
        pointers: list[c_void_p] = []
        elements: list[Element] = []
        for pointer in raw:
            info = _read(pointer)
            if info is None or not info[3] or not _visible(info[2], window) or info[1] in _NEVER:
                native.com_release(pointer)
                continue
            name, control_type, rect, _, patterns = info
            elements.append(Element(len(pointers), name, control_type, rect, patterns))
            pointers.append(pointer)
        return elements, pointers


# Chromium (Chrome, and Electron apps: Claude, VS Code, Discord) builds its accessibility tree only once a UIA
# client asks, so the first snapshot of a cold window holds just the caption buttons. Re-ask for a moment.
CHROMIUM = "Chrome_WidgetWin_"
COLD_TREE_MAX = 3  # Minimize / Maximize / Close
COLD_WAIT_S = 1.0
COLD_RETRY_S = 0.15


def warm_snapshot(focus: object, win: native.Win, wait: float = COLD_WAIT_S) -> list[Element]:
    """``focus.snapshot(win.hwnd)``, asked again for up to ``wait`` s while a Chromium window shows only its
    caption buttons (``focus`` is a ``uia.FocusReader`` or a test fake with ``snapshot``)."""
    elements = focus.snapshot(win.hwnd)
    if win.cls.startswith(CHROMIUM):
        deadline = time.perf_counter() + wait
        while sum(e.clickable for e in elements) <= COLD_TREE_MAX and time.perf_counter() < deadline:
            time.sleep(COLD_RETRY_S)
            elements = focus.snapshot(win.hwnd)
    return elements


def _subtree(automation: c_void_p, hwnd: int, request: c_void_p, cap: int) -> list[c_void_p]:
    """The whole window in one batched read (the cache request scoped to the subtree), in tree order. ~3x faster
    than FindAll: Chrome 202 nodes 168 ms vs 533 (GOTCHAS)."""
    native.com_call(request, _CACHE_PUT_TREE_SCOPE, _SUBTREE, types=(c_int,))
    root = _out_pointer(automation, _ELEMENT_FROM_HANDLE_BUILD_CACHE, c_void_p(hwnd), request,
                        types=(c_void_p, c_void_p))
    out: list[c_void_p] = []
    stack = [root] if root is not None else []
    while stack:
        node = stack.pop()
        if len(out) >= cap:
            native.com_release(node)
            continue
        out.append(node)
        kids = _out_pointer(node, _GET_CACHED_CHILDREN)
        if kids is None:
            continue
        children = [_out_pointer(kids, _ARRAY_ELEMENT, i, types=(c_int,)) for i in range(_int(kids, _ARRAY_LENGTH))]
        native.com_release(kids)
        stack.extend(reversed([c for c in children if c is not None]))
    return out


def _walk(automation: c_void_p, root: c_void_p, request: c_void_p, cap: int) -> list[c_void_p]:
    condition = _out_pointer(automation, _CONTROL_VIEW_CONDITION)
    if condition is None:
        return []
    walker = _out_pointer(automation, _CREATE_TREE_WALKER, condition, types=(c_void_p,))
    native.com_release(condition)
    if walker is None:
        return []
    out: list[c_void_p] = []
    try:
        stack = [(root, 0)]
        add_ref(root)
        while stack and len(out) < cap:
            node, depth = stack.pop()
            child = _out_pointer(walker, _WALK_FIRST_CHILD_CACHED, node, request, types=(c_void_p, c_void_p))
            native.com_release(node)
            siblings = []
            while child is not None and len(out) + len(siblings) < cap:
                siblings.append(child)
                child = _out_pointer(walker, _WALK_NEXT_SIBLING_CACHED, child, request, types=(c_void_p, c_void_p))
            for sibling in reversed(siblings):
                out.append(sibling)
                if depth < 30 and _int(sibling, _CACHED_CONTROL_TYPE) != _DOCUMENT:
                    add_ref(sibling)
                    stack.append((sibling, depth + 1))
        for node, _ in stack:
            native.com_release(node)
        return out
    finally:
        native.com_release(walker)


def _pattern(element: c_void_p, pattern_id: int, iid: native.GUID) -> c_void_p | None:
    return _out_pointer(element, _GET_CURRENT_PATTERN_AS, pattern_id, byref(iid), types=(c_int, POINTER(native.GUID)))


def activate(element: c_void_p, patterns: frozenset[str]) -> str:
    """Invoke → Toggle → Select → Expand → default action. Returns the method that took, "" = needs a real click.
    Runs on its own thread (an Invoke that opens a modal dialog can block until the dialog closes)."""
    native.com_init()
    order = [("invoke", _INVOKE), ("toggle", _TOGGLE), ("select", _SELECT), ("expand", _EXPAND)]
    for name, (pattern_id, iid, slot) in order:
        if name not in patterns:
            continue
        pattern = _pattern(element, pattern_id, iid)
        if pattern is None:
            continue
        try:
            if native.com_call(pattern, slot) >= 0:
                return name
        finally:
            native.com_release(pattern)
    pattern = _pattern(element, _LEGACY[0], _LEGACY[1])
    if pattern is not None:
        try:
            if native.com_call(pattern, _LEGACY[2]) >= 0:
                return "default action"
        finally:
            native.com_release(pattern)
    return ""


def activate_with_deadline(element: c_void_p, patterns: frozenset[str], timeout: float = 1.0) -> str | None:
    """``activate`` on a helper thread. None = still running after ``timeout`` (a modal dialog probably opened:
    treated as done; the receipt decides)."""
    add_ref(element)
    result: list[str] = []

    def run() -> None:
        try:
            result.append(activate(element, patterns))
        except OSError:
            result.append("")
        finally:
            native.com_release(element)

    thread = threading.Thread(target=run, name="nova-uia-act", daemon=True)
    thread.start()
    thread.join(timeout)
    return result[0] if result else None


# --- scrolling -----------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class ScrollReceipt:
    before: float  # percent, 0-100
    after: float
    view: float  # how much of the content shows, percent

    @property
    def moved(self) -> bool:
        return abs(self.after - self.before) > 0.05


def _double(pointer: c_void_p, slot: int) -> float:
    value = c_double(_NO_SCROLL)
    native.com_call(pointer, slot, byref(value), types=(POINTER(c_double),))
    return value.value


def _scroller(automation: c_void_p, x: int | None, y: int | None, vertical: bool) -> c_void_p | None:
    """The ScrollPattern of the element under (x, y) — or the focused one — or its nearest scrollable ancestor."""
    with native.dpi_aware():
        if x is not None and y is not None:
            element = _out_pointer(automation, _ELEMENT_FROM_POINT, wintypes.POINT(x, y), types=(wintypes.POINT,))
        else:
            element = _out_pointer(automation, _GET_FOCUSED)
    if element is None:
        return None
    condition = _out_pointer(automation, _CONTROL_VIEW_CONDITION)
    walker = _out_pointer(automation, _CREATE_TREE_WALKER, condition, types=(c_void_p,)) if condition else None
    native.com_release(condition)
    try:
        for _ in range(25):
            pattern = _pattern(element, _SCROLL_ID, _IID_SCROLL)
            if pattern is not None:
                ok = wintypes.BOOL()
                native.com_call(pattern, _V_SCROLLABLE if vertical else _H_SCROLLABLE, byref(ok),
                                types=(POINTER(wintypes.BOOL),))
                if ok.value:
                    return pattern
                native.com_release(pattern)
            parent = _out_pointer(walker, _WALK_PARENT, element, types=(c_void_p,)) if walker else None
            native.com_release(element)
            element = parent
            if element is None:
                return None
        return None
    finally:
        native.com_release(element)
        native.com_release(walker)


def scroll(automation: c_void_p, direction: str, amount: str, x: int | None = None,
           y: int | None = None) -> ScrollReceipt | None:
    """ScrollPattern on the content under (x, y) / the focus. None = nothing scrollable there."""
    vertical = direction in ("up", "down", "top", "bottom")
    pattern = _scroller(automation, x, y, vertical)
    if pattern is None:
        return None
    percent_slot, view_slot = (_V_PERCENT, _V_VIEW) if vertical else (_H_PERCENT, _H_VIEW)
    try:
        before, view = _double(pattern, percent_slot), _double(pattern, view_slot)
        if before < 0:
            return None
        target = scroll_target(before, view, direction, amount)
        if target is None:  # a page: the control's own page step
            step = _LARGE_INCREMENT if direction in ("down", "right") else _LARGE_DECREMENT
            h, v = (_NO_AMOUNT, step) if vertical else (step, _NO_AMOUNT)
            native.com_call(pattern, _SCROLL, h, v, types=(c_int, c_int))
        else:
            h, v = (_NO_SCROLL, target) if vertical else (target, _NO_SCROLL)
            native.com_call(pattern, _SET_PERCENT, c_double(h), c_double(v), types=(c_double, c_double))
        after = _double(pattern, percent_slot)
        if abs(after - before) <= 0.05:  # some controls move a beat later
            time.sleep(0.12)
            after = _double(pattern, percent_slot)
        return ScrollReceipt(before, after, view)
    finally:
        native.com_release(pattern)


def scroll_target(before: float, view: float, direction: str, amount: str) -> float | None:
    """Where a ScrollPattern scroll should land, in percent. None = one page (the control's own LargeIncrement)."""
    if direction in ("top", "bottom") or amount == "end":
        return 0.0 if direction in ("top", "up", "left") else 100.0
    if amount == "half":
        return 50.0
    sign = 1 if direction in ("down", "right") else -1
    # percent is of the scrollable range; a page moves view / (100 - view) of it
    page = 100.0 * view / max(1.0, 100.0 - view) if 0 < view < 100 else 10.0
    share = {"bit": 0.25, "small": 0.25, "large": 3.0}.get(amount)
    if share is None:
        return None
    return max(0.0, min(100.0, before + sign * page * share))


def focus_at(automation: c_void_p, x: int, y: int) -> bool:
    """Give keyboard focus to what's under (x, y) without clicking it (a Chrome PDF's content takes PageDown only
    once it has focus). False = nothing there, or it refused."""
    with native.dpi_aware():
        element = _out_pointer(automation, _ELEMENT_FROM_POINT, wintypes.POINT(x, y), types=(wintypes.POINT,))
    if element is None:
        return False
    try:
        hr = native.com_call(element, _SET_FOCUS)
        if hr < 0:
            _log.info("SetFocus refused: 0x%08x", hr & 0xFFFFFFFF)
        return hr >= 0
    finally:
        native.com_release(element)
