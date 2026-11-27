"""macOS backend: the Windows-only modules' public APIs, implemented with Quartz, Accessibility and AppKit.

``install_redirects()`` (called from ``nova/__init__.py`` on a Mac) makes ``import nova.actions.native`` and
friends load the module here instead, so the brain, executor, dictation and Chrome code run unchanged.

Permissions (System Settings > Privacy & Security) for the app that runs Nova (Terminal, iTerm, VS Code...):
- Accessibility: window moves, focus reads, typing, and the trigger chord (the event tap swallows it).
- Microphone: asked on first use.
- Automation (System Events): only for "volume" via osascript - asked on first use.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import sys

REDIRECTS = {
    "nova.actions.native": "nova.macos.native",
    "nova.actions.apps": "nova.macos.apps",
    "nova.actions.input": "nova.macos.input",
    "nova.actions.system": "nova.macos.system",
    "nova.actions.windows": "nova.macos.windows",
    "nova.actions.uia": "nova.macos.uia",
    "nova.hook": "nova.macos.hook",
    "nova.session": "nova.macos.session",
    "nova.detect": "nova.macos.detect",
    "nova.console": "nova.macos.console",
    "nova.cpu": "nova.macos.cpu",
    "nova.startup": "nova.macos.startup",
    "nova.chrome.install": "nova.macos.chrome_install",
}


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self._target = target

    def create_module(self, spec):  # noqa: ANN001
        module = importlib.import_module(self._target)
        sys.modules[spec.name] = module
        return module

    def exec_module(self, module) -> None:  # noqa: ANN001 - already executed as its real name
        pass


class _Redirect(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        real = REDIRECTS.get(fullname)
        if real is None:
            return None
        return importlib.machinery.ModuleSpec(fullname, _AliasLoader(real))


_installed = False


def install_redirects() -> None:
    global _installed
    if not _installed:
        sys.meta_path.insert(0, _Redirect())
        _installed = True
