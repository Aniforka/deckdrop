"""Which cover source fills which slot, and in what order `auto` tries them.

    python -m unittest discover -s tests
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop.art import covers  # noqa: E402
from deckdrop.state import STATE  # noqa: E402

JPG = b"\xff\xd8\xff" + b"jpg"
PNG = b"\x89PNG\r\n\x1a\n" + b"png"
FULL = {"portrait": (JPG, "jpg"), "landscape": (JPG, "jpg"),
        "hero": (JPG, "jpg"), "logo": (PNG, "png")}


class ArtSourcesTest(unittest.TestCase):
    def setUp(self):
        self.old = {k: STATE.get(k) for k in ("vndb_auto", "art_steam", "art_griddb")}
        STATE["vndb_auto"] = True
        STATE["art_steam"] = True
        STATE["art_griddb"] = True
        self.dir = Path(__import__("tempfile").mkdtemp())
        self.exe = self.dir / "Game" / "Game.exe"
        self.exe.parent.mkdir(parents=True)
        self.exe.write_bytes(b"MZ")
        self.updated = {}
        self.patches = [
            mock.patch.object(covers, "added_rec", return_value={}),
            mock.patch.object(covers, "resolve_appid", return_value=123),
            mock.patch.object(covers, "shortcuts_index", return_value={}),
            mock.patch.object(covers, "apply_artwork", return_value="files"),
            mock.patch.object(covers, "update_added", side_effect=lambda exe, **kv: self.updated.update(kv)),
            mock.patch.object(covers, "load_icon", return_value=None),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        STATE.update(self.old)

    def test_explicit_steam_fills_the_set(self):
        steam = {"art_source": "steamcdn", "art_id": 10, "art_title": "Steam Title"}
        with mock.patch.object(covers.webcovers, "steam_pick", return_value=(10, "Steam Title")), \
                mock.patch.object(covers.webcovers, "steam_set", return_value=(dict(FULL), steam, [])) as get:
            covers.ensure_art(str(self.exe), force=True, source="steam")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(self.updated["art_source"], "steamcdn")
        self.assertEqual(self.updated["art_title"], "Steam Title")
        self.assertEqual(self.updated["art_id"], 10)

    def test_vndb_then_steam_fills_only_the_empty_slots(self):
        vn = {"id": "v1", "title": "The Novel"}
        steam = {"art_source": "steamcdn", "art_id": 10, "art_title": "Steam Title"}
        with mock.patch.object(covers, "vndb_pick", return_value=vn), \
                mock.patch.object(covers, "vndb_images", return_value=({"portrait": (JPG, "jpg")}, [])), \
                mock.patch.object(covers.webcovers, "steam_pick", return_value=(10, "Steam Title")), \
                mock.patch.object(covers.webcovers, "steam_set", return_value=(dict(FULL), steam, [])) as get, \
                mock.patch.object(covers.webcovers, "griddb_set") as grid:
            covers.ensure_art(str(self.exe), force=True, source="auto")
        self.assertEqual(get.call_count, 1)
        self.assertEqual(grid.call_count, 0)
        self.assertEqual(self.updated["art_source"], "vndb")
        self.assertEqual(self.updated["vndb_title"], "The Novel")

    def test_a_full_vndb_set_does_not_hit_the_web(self):
        vn = {"id": "v1", "title": "The Novel"}
        with mock.patch.object(covers, "vndb_pick", return_value=vn), \
                mock.patch.object(covers, "vndb_images", return_value=(dict(FULL), [])), \
                mock.patch.object(covers.webcovers, "steam_set") as steam, \
                mock.patch.object(covers.webcovers, "griddb_set") as grid:
            covers.ensure_art(str(self.exe), force=True, source="auto")
        self.assertEqual(steam.call_count, 0)
        self.assertEqual(grid.call_count, 0)
        self.assertEqual(self.updated["art_source"], "vndb")

    def test_explicit_griddb_uses_the_chosen_id(self):
        info = {"art_source": "griddb", "art_id": 55, "art_title": "Grid Title"}
        with mock.patch.object(covers.webcovers, "griddb_set", return_value=(dict(FULL), info, [])) as get:
            covers.ensure_art(str(self.exe), force=True, source="griddb", art_id="55", art_title="Grid Title")
        get.assert_called_once_with(55)
        self.assertEqual(self.updated["art_source"], "griddb")
        self.assertEqual(self.updated["art_id"], 55)


if __name__ == "__main__":
    unittest.main()
