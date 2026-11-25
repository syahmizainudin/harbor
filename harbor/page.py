"""The console page, put together from ``harbor/ui/``.

``ui/shell.html`` is the page around the tabs: the sidebar, the shared CSS, the Account, sign-in and
folder-picker dialogs, and the JS every tab uses (``api``, ``showTab``, permissions, history, ``render``,
polling). Each tab has its own file, ``ui/<name>.html``, with up to five sections, each starting on a
marker line of its own:

    <!-- @style -->   <style> … </style>    CSS only this tab uses
    <!-- @tab -->     the sidebar button
    <!-- @pane -->    the tab's pane, inside <main>
    <!-- @dialog -->  dialogs only this tab opens
    <!-- @script -->  <script> … </script>  its JS

``@tab`` and ``@pane`` are required. Anything before the first marker is a comment for the reader and is
dropped. ``build`` puts each section, in ``PARTS`` order, where the shell's matching ``@parts:`` line
is. The result is one page with one ``<style>`` and one ``<script>``, as before the split: the tabs'
scripts share the shell's top-level names, and nothing in a tab's script may run at load time that needs
a name the shell defines after the ``@parts:script`` line (``render``, history, account, polling).

This is what a Node build step would do later; until then it runs once, when ``harbor.app`` is imported.
"""

from __future__ import annotations

import re
from pathlib import Path

UI = Path(__file__).parent / "ui"

# Sidebar order. Every part is always in the page; ``applyPerms`` hides the tabs a user can't use.
PARTS = ("convert", "download", "library", "games", "admin")

SECTIONS = ("style", "tab", "pane", "dialog", "script")
REQUIRED = ("tab", "pane")
WRAPPED = {"style": "style", "script": "script"}  # sections written inside <style> / <script> tags

_MARKER = re.compile(r"^<!-- @(\w+) -->\n", re.MULTILINE)
_SLOT = re.compile(r"^[ \t]*(?:<!--|/\*) @parts:(\w+) (?:-->|\*/)\n", re.MULTILINE)


def read_part(path: Path) -> dict[str, str]:
    """A tab's file as {section: text}. Raises ValueError, naming the file, if it's malformed."""
    pieces = _MARKER.split(path.read_text())
    found: dict[str, str] = {}
    for name, body in zip(pieces[1::2], pieces[2::2], strict=True):
        if name not in SECTIONS:
            raise ValueError(f"{path.name}: unknown section @{name} (expected one of {', '.join(SECTIONS)})")
        if name in found:
            raise ValueError(f"{path.name}: @{name} appears twice")
        tag = WRAPPED.get(name)
        if tag:
            m = re.fullmatch(rf"<{tag}>\n(.*)</{tag}>\n", body, re.DOTALL)
            if not m:
                raise ValueError(f"{path.name}: @{name} must be one <{tag}> block, tags on own lines")
            body = m.group(1)
        found[name] = body
    for name in REQUIRED:
        if name not in found:
            raise ValueError(f"{path.name}: no @{name} section")
    return found


def build(ui: Path = UI, parts: tuple[str, ...] = PARTS) -> bytes:
    """The whole page: the shell with every part's sections in place of its ``@parts:`` lines."""
    loaded = [read_part(ui / f"{name}.html") for name in parts]
    shell = (ui / "shell.html").read_text()
    slots = _SLOT.findall(shell)
    if sorted(slots) != sorted(SECTIONS):
        raise ValueError(f"shell.html: needs one @parts: line for each of {', '.join(SECTIONS)}; has {slots}")
    page = _SLOT.sub(lambda m: "".join(p.get(m.group(1), "") for p in loaded), shell)
    return page.encode()
