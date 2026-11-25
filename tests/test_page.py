"""The console page put together from harbor/ui/ (harbor/page.py)."""

import re
import tempfile
import unittest
from pathlib import Path

from harbor import page

GOOD_PART = """<!-- a note -->
<!-- @style -->
<style>
  .x { color: red; }
</style>
<!-- @tab -->
    <button class="tab" data-tab="x">X</button>
<!-- @pane -->
  <div id="pane-x"></div>
<!-- @script -->
<script>
const x = 1;
</script>
"""

SHELL = """<style>
  /* @parts:style */
</style>
<div class="tabs">
    <!-- @parts:tab -->
</div>
<main>
  <!-- @parts:pane -->
</main>
<!-- @parts:dialog -->
<script>
/* @parts:script */
</script>
"""


class BuiltPageTests(unittest.TestCase):
    def setUp(self):
        self.page = page.build().decode()

    def test_no_markers_left_and_one_style_and_script(self):
        self.assertNotIn("@parts:", self.page)
        for name in page.SECTIONS:
            self.assertNotIn(f"<!-- @{name} -->", self.page)
        self.assertEqual(self.page.count("<style>"), 1)
        self.assertEqual(self.page.count("<script>"), 1)
        self.assertTrue(self.page.startswith("<!doctype html>"))
        self.assertTrue(self.page.rstrip().endswith("</html>"))

    def test_every_part_has_its_tab_and_pane_in_order(self):
        tabs = re.findall(r'<button class="tab"[^>]* data-tab="(\w+)"', self.page)
        self.assertEqual(tabs, list(page.PARTS))
        for name in page.PARTS:
            self.assertEqual(self.page.count(f'id="pane-{name}"'), 1, name)
            self.assertEqual(self.page.count(f'id="tab-{name}"'), 1, name)

    def test_ids_are_unique(self):
        ids = re.findall(r'\sid="([^"$]+)"', self.page)
        dupes = {i for i in ids if ids.count(i) > 1}
        self.assertEqual(dupes, set())

    def test_every_ui_file_is_a_part_or_the_shell(self):
        files = {p.stem for p in page.UI.glob("*.html")}
        self.assertEqual(files, {"shell", *page.PARTS})


class ReadPartTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, text):
        (self.dir / name).write_text(text)
        return self.dir / name

    def test_sections_unwrapped(self):
        got = page.read_part(self.write("x.html", GOOD_PART))
        self.assertEqual(got["style"], "  .x { color: red; }\n")
        self.assertEqual(got["script"], "const x = 1;\n")
        self.assertEqual(got["pane"], '  <div id="pane-x"></div>\n')
        self.assertNotIn("dialog", got)

    def test_build_from_a_shell(self):
        self.write("x.html", GOOD_PART)
        self.write("shell.html", SHELL)
        out = page.build(self.dir, ("x",)).decode()
        self.assertIn("<style>\n  .x { color: red; }\n</style>", out)
        self.assertIn('<main>\n  <div id="pane-x"></div>\n</main>', out)
        self.assertIn("<script>\nconst x = 1;\n</script>", out)
        self.assertNotIn("@parts:", out)

    def test_malformed_parts_name_the_file(self):
        cases = {
            "unknown": GOOD_PART.replace("@style", "@styles"),
            "twice": GOOD_PART + "<!-- @tab -->\n<button></button>\n",
            "no pane": GOOD_PART.replace("<!-- @pane -->\n", ""),
            "unwrapped": GOOD_PART.replace("<script>\n", ""),
        }
        for why, text in cases.items():
            with self.subTest(why), self.assertRaisesRegex(ValueError, r"^bad\.html: "):
                page.read_part(self.write("bad.html", text))

    def test_shell_missing_a_slot(self):
        self.write("x.html", GOOD_PART)
        self.write("shell.html", SHELL.replace("<!-- @parts:dialog -->\n", ""))
        with self.assertRaisesRegex(ValueError, "shell.html"):
            page.build(self.dir, ("x",))


if __name__ == "__main__":
    unittest.main()
