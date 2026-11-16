"""Tests for ci/release_notes.py, which gates the release pipeline."""

import importlib.util
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_notes", ROOT / "ci" / "release_notes.py")
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## Unreleased

## 1.2.0 - 2026-11-20

- New thing.

## 1.1.0

- Old thing.
"""


class ReleaseNotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "harbor").mkdir()
        self.set_version("1.2.0")
        (self.root / "CHANGELOG.md").write_text(CHANGELOG)

    def tearDown(self):
        self.tmp.cleanup()

    def set_version(self, v):
        (self.root / "harbor" / "__init__.py").write_text(f'"""x"""\n\n__version__ = "{v}"\n')

    def test_returns_only_that_section(self):
        self.assertEqual(release_notes.notes_for("v1.2.0", self.root), "- New thing.\n")

    def test_last_section(self):
        self.set_version("1.1.0")
        self.assertEqual(release_notes.notes_for("v1.1.0", self.root), "- Old thing.\n")

    def test_tag_must_match_version(self):
        with self.assertRaisesRegex(ValueError, "does not match"):
            release_notes.notes_for("v1.3.0", self.root)
        with self.assertRaisesRegex(ValueError, "does not match"):
            release_notes.notes_for("1.2.0", self.root)

    def test_missing_or_empty_section(self):
        self.set_version("2.0.0")
        with self.assertRaisesRegex(ValueError, "no '## 2.0.0'"):
            release_notes.notes_for("v2.0.0", self.root)
        (self.root / "CHANGELOG.md").write_text("## 2.0.0\n\n## 1.0.0\n- x\n")
        with self.assertRaisesRegex(ValueError, "no '## 2.0.0'"):
            release_notes.notes_for("v2.0.0", self.root)

    def test_real_changelog_has_current_version(self):
        version = release_notes.code_version()
        self.assertTrue(release_notes.changelog_section(version).strip())


if __name__ == "__main__":
    unittest.main()
