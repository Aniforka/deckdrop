"""Every version says what changed: CHANGELOG.md has a full section for it (the release notes).

    python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import changelog  # noqa: E402

GOOD = """# Changelog

## 1.1.0 — 2026-10-01

### English

**Added**
- A thing.

### Русский

**Добавлено**
- Штука.

## 1.0.0 — 2026-09-01

### English
- First.

### Русский
- Первая.
"""


class ChangelogTest(unittest.TestCase):
    def test_current_version_is_described(self):
        """Bumping __version__ without saying what changed fails here, and so in CI and before a release."""
        self.assertEqual(changelog.problems(changelog.current_version()), [])

    def test_release_notes_are_the_section(self):
        body = changelog.section("1.1.0", GOOD)
        self.assertTrue(body.startswith("### English"))
        self.assertIn("- Штука.", body)
        self.assertNotIn("First.", body)

    def test_good(self):
        self.assertEqual(changelog.problems("1.1.0", GOOD), [])

    def test_missing_version(self):
        self.assertTrue(any("no section for 1.2.0" in p for p in changelog.problems("1.2.0", GOOD)))

    def test_not_on_top(self):
        self.assertTrue(any("first one" in p for p in changelog.problems("1.0.0", GOOD)))

    def test_missing_language(self):
        text = GOOD.replace("### Русский\n\n**Добавлено**\n- Штука.\n", "")
        self.assertTrue(any("Русский" in p for p in changelog.problems("1.1.0", text)))

    def test_empty_language(self):
        text = GOOD.replace("- A thing.", "TODO")
        self.assertTrue(any("English" in p and "no changes" in p for p in changelog.problems("1.1.0", text)))

    def test_order(self):
        text = GOOD.replace("## 1.0.0", "## 1.2.0")
        self.assertTrue(any("newest first" in p for p in changelog.problems("1.1.0", text)))


if __name__ == "__main__":
    unittest.main()
