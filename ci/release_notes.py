#!/usr/bin/env python3
"""Check a release tag against the code and print its CHANGELOG section.

Usage: python3 ci/release_notes.py v0.2.0 > notes.md

Fails (exit 1) when the tag is not ``v<harbor.__version__>`` or CHANGELOG.md has no
``## <version>`` section with text in it, so a release can't go out half-prepared.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def code_version(root: Path = ROOT) -> str:
    text = (root / "harbor" / "__init__.py").read_text()
    m = re.search(r'^__version__ = "([^"]+)"', text, re.M)
    if not m:
        raise ValueError("no __version__ in harbor/__init__.py")
    return m.group(1)


def changelog_section(version: str, root: Path = ROOT) -> str:
    text = (root / "CHANGELOG.md").read_text()
    m = re.search(rf"^## \[?{re.escape(version)}\]?[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not m or not m.group(1).strip():
        raise ValueError(f"CHANGELOG.md has no '## {version}' section with text")
    return m.group(1).strip() + "\n"


def notes_for(tag: str, root: Path = ROOT) -> str:
    version = code_version(root)
    if tag != f"v{version}":
        raise ValueError(f"tag {tag} does not match __version__ {version} (expected v{version})")
    return changelog_section(version, root)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        sys.stdout.write(notes_for(argv[1]))
    except ValueError as e:
        print(f"release_notes: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
