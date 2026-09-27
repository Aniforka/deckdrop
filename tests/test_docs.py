"""README links inside the page lead somewhere, and those of the Russian README are Latin.

The GitHub mobile app does not follow Cyrillic anchors (#быстрый-старт), so every heading of
README.ru.md carries an explicit Latin anchor, the same as its README.en.md counterpart.

    python -m unittest discover -s tests
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADING = re.compile(r"^#{1,6} (.+)$", re.M)


def slug(heading):
    """GitHub's anchor for a heading."""
    text = re.sub(r"<[^>]+>", "", heading).strip().lower()
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(text):
    return {slug(h) for h in HEADING.findall(text)} | set(re.findall(r'<a (?:id|name)="([^"]+)"', text))


def page_links(text):
    return re.findall(r"\]\(#([^)]+)\)", text)


class ReadmeLinksTest(unittest.TestCase):
    def test_links_lead_to_a_section(self):
        for name in ("README.md", "README.en.md", "README.ru.md", "CHANGELOG.md"):
            text = (ROOT / name).read_text("utf-8")
            missing = sorted(set(page_links(text)) - anchors(text))
            self.assertEqual(missing, [], f"{name}: links to sections that do not exist")

    def test_russian_links_are_latin(self):
        text = (ROOT / "README.ru.md").read_text("utf-8")
        cyrillic = [link for link in page_links(text) if not link.isascii()]
        self.assertEqual(cyrillic, [], "the GitHub app cannot follow these: give the heading a Latin <a id>")

    def test_russian_sections_use_the_english_anchors(self):
        ru = (ROOT / "README.ru.md").read_text("utf-8")
        en = (ROOT / "README.en.md").read_text("utf-8")
        ru_ids = [(re.findall(r'<a id="([^"]+)"', h) or [slug(h)])[0] for h in HEADING.findall(ru)[1:]]
        en_ids = [slug(h) for h in HEADING.findall(en)[1:]]
        self.assertEqual(ru_ids, en_ids, "README.ru.md and README.en.md headings drifted apart")


if __name__ == "__main__":
    unittest.main()
