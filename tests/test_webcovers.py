"""Cover art from the Steam store CDN and SteamGridDB: search, matching and slot mapping.

    python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop.art import webcovers  # noqa: E402
from deckdrop.state import STATE  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"png"
JPG = b"\xff\xd8\xff" + b"jpg"


class SteamTest(unittest.TestCase):
    def test_search_and_exact_match(self):
        with mock.patch.object(webcovers, "_json", return_value={"items": [
                {"id": 10, "name": "Va-11 Hall-A", "tiny_image": "t.jpg"},
                {"id": 11, "name": "Va-11 Hall-A: Cyberpunk Bartender Action", "tiny_image": "t2.jpg"}]}):
            self.assertEqual(webcovers.steam_search("Va-11")[0]["id"], "10")
            self.assertEqual(webcovers.steam_pick("Va-11 Hall-A"), (10, "Va-11 Hall-A"))

    def test_no_exact_match_is_not_guessed(self):
        with mock.patch.object(webcovers, "_json", return_value={"items": [
                {"id": 11, "name": "Va-11 Hall-A: Cyberpunk Bartender Action"}]}):
            self.assertEqual(webcovers.steam_pick("Va-11 Hall-A"), (None, None))

    def test_slots_from_the_cdn(self):
        def download(url, source, ident, slot, headers=None):
            return PNG if slot == "logo" else JPG
        with mock.patch.object(webcovers, "_download", side_effect=download), \
                mock.patch.object(webcovers, "FFMPEG", None):
            images, info, notes = webcovers.steam_set(10)
        self.assertEqual(set(images), {"portrait", "landscape", "hero", "logo"})
        self.assertEqual(images["logo"], (PNG, "png"))
        self.assertEqual(images["portrait"], (JPG, "jpg"))
        self.assertEqual(info["art_source"], "steamcdn")
        self.assertEqual(info["art_id"], 10)
        self.assertEqual(notes, [])

    def test_a_jpeg_logo_is_skipped(self):
        with mock.patch.object(webcovers, "_download", return_value=JPG), \
                mock.patch.object(webcovers, "FFMPEG", None):
            images, _info, notes = webcovers.steam_set(10)
        self.assertNotIn("logo", images)
        self.assertTrue(notes)


class GridDBTest(unittest.TestCase):
    def setUp(self):
        self.old_key = STATE.get("griddb_key")
        self.old_nsfw = STATE.get("vndb_nsfw")
        STATE["griddb_key"] = "k"
        STATE["vndb_nsfw"] = True

    def tearDown(self):
        STATE["griddb_key"] = self.old_key
        STATE["vndb_nsfw"] = self.old_nsfw

    def test_search_needs_a_key(self):
        STATE["griddb_key"] = ""
        with self.assertRaises(ValueError):
            webcovers.griddb_search("portal")

    def test_search_and_match(self):
        with mock.patch.object(webcovers, "_json", return_value={"data": [
                {"id": 5, "name": "Portal 2"}, {"id": 6, "name": "Portal 2: Sixense"}]}):
            self.assertEqual(webcovers.griddb_search("portal")[0]["id"], "5")
            self.assertEqual(webcovers.griddb_pick("Portal 2"), (5, "Portal 2"))

    def test_slots_and_nsfw_filter(self):
        def fake_json(url, headers=None, timeout=25):
            if "/grids/" in url:
                return {"data": [
                    {"width": 600, "height": 900, "url": "p", "nsfw": True},
                    {"width": 600, "height": 900, "url": "p2", "nsfw": False},
                    {"width": 920, "height": 430, "url": "w", "nsfw": False}]}
            if "/heroes/" in url:
                return {"data": [{"url": "h"}]}
            if "/logos/" in url:
                return {"data": [{"url": "l"}]}
            return {"data": [{"url": "i"}]}

        def fake_download(url, source, ident, slot, headers=None):
            return PNG if slot == "logo" else JPG
        STATE["vndb_nsfw"] = False
        with mock.patch.object(webcovers, "_json", side_effect=fake_json), \
                mock.patch.object(webcovers, "_download", side_effect=fake_download), \
                mock.patch.object(webcovers, "FFMPEG", None):
            images, info, _notes = webcovers.griddb_set(5)
        self.assertEqual(set(images), {"portrait", "landscape", "hero", "logo", "icon"})
        self.assertEqual(info["art_source"], "griddb")
        self.assertEqual(info["art_id"], 5)
        self.assertEqual(images["logo"], (PNG, "png"))

    def test_empty_answer_is_an_error(self):
        with mock.patch.object(webcovers, "_json", return_value={"data": []}):
            with self.assertRaises(RuntimeError):
                webcovers.griddb_set(5)

    def test_key_check(self):
        with mock.patch.object(webcovers, "griddb_search", return_value=[{"id": "1"}]):
            self.assertEqual(webcovers.griddb_test(), {"found": 1})


if __name__ == "__main__":
    unittest.main()
