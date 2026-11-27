"""Nova — local voice layer for Windows and macOS."""

__version__ = "0.0.0"

from .platform import MAC as _MAC

if _MAC:
    from .macos import install_redirects

    install_redirects()
