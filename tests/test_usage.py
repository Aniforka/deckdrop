"""Where the space went: sizes per disk, Proton prefixes, inbox and update backups.

    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop import usage  # noqa: E402


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TreeSizeTest(unittest.TestCase):
    def setUp(self):
        usage._SIZES.clear()
        self.root = Path(tempfile.mkdtemp())

    def test_counts_nested_files(self):
        put(self.root / "a.bin", b"x" * 100)
        put(self.root / "sub" / "b.bin", b"y" * 25)
        put(self.root / "sub" / "deeper" / "c.bin", b"z" * 5)
        size, files = usage.tree_size(self.root)
        self.assertEqual((size, files), (130, 3))

    def test_missing_folder_is_zero(self):
        self.assertEqual(usage.tree_size(self.root / "nope"), (0, 0))

    def test_symlink_is_not_followed(self):
        outside = Path(tempfile.mkdtemp())
        put(outside / "big.bin", b"x" * 1000)
        put(self.root / "own.bin", b"x" * 10)
        try:
            (self.root / "link").symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available here")
        self.assertEqual(usage.tree_size(self.root), (10, 1))


class BuildTest(unittest.TestCase):
    def setUp(self):
        usage._SIZES.clear()
        usage._REPORT.update(at=0.0, data=None, building=False, error=None)
        base = Path(tempfile.mkdtemp())
        self.internal = base / "internal"
        self.sd_mount = base / "card"
        self.prefix = base / "steam" / "steamapps" / "compatdata" / "123"
        put(self.internal / "Games" / "A" / "game.exe", b"a" * 100)
        put(self.internal / "Games" / "A" / "data" / "x.bin", b"b" * 200)
        put(self.internal / "Games" / "B" / "game.exe", b"c" * 50)
        put(self.internal / "Games" / "_inbox" / "old.zip", b"d" * 10)
        put(self.internal / "Games" / ".deckdrop-updates" / "A" / "files" / "x.bin", b"e" * 5)
        put(self.sd_mount / "Games" / "C" / "game.exe", b"f" * 70)
        put(self.sd_mount / "Games" / "_inbox" / "new.zip", b"g" * 7)
        put(self.prefix / "drive_c" / "game" / "save.dat", b"h" * 1000)
        self.disks = [
            {"id": "internal", "label": "Internal", "root": str(self.internal / "Games"),
             "free": 5000, "total": 10000},
            {"id": "sd:card", "label": "microSD", "root": str(self.sd_mount / "Games"),
             "free": 500, "total": 1000},
        ]
        self.games = [
            {"name": "A", "title": "Same Game", "path": str(self.internal / "Games" / "A"),
             "disk": "Internal", "exes": [{"in_steam": True, "appid": 123}]},
            {"name": "B", "title": "Other Game", "path": str(self.internal / "Games" / "B"),
             "disk": "Internal", "exes": [{"in_steam": False, "appid": None}]},
            {"name": "C", "title": "Same Game", "path": str(self.sd_mount / "Games" / "C"),
             "disk": "microSD", "exes": [{"in_steam": False, "appid": None}]},
        ]

    def build(self):
        with mock.patch.object(usage, "disks", return_value=self.disks), \
                mock.patch.object(usage, "sd_mounts", return_value=[("microSD", self.sd_mount)]), \
                mock.patch.object(usage, "list_games", return_value=self.games), \
                mock.patch.object(usage, "compatdata_dir",
                                  side_effect=lambda appid: self.prefix if appid == 123 else Path("/nope")):
            return usage.build()

    def disk(self, data, disk_id):
        return next(d for d in data["disks"] if d["id"] == disk_id)

    def test_parts_per_disk(self):
        data = self.build()
        internal = self.disk(data, "internal")["parts"]
        self.assertEqual(internal["games"], 350)
        self.assertEqual(internal["prefixes"], 1000)
        self.assertEqual(internal["inbox"], 10)
        self.assertEqual(internal["backups"], 5)
        self.assertEqual(internal["other"], 5000 - 1365)
        sd = self.disk(data, "sd:card")["parts"]
        self.assertEqual(sd["games"], 70)
        self.assertEqual(sd["inbox"], 7)
        self.assertEqual(sd["other"], 500 - 77)

    def test_prefix_is_attributed_to_its_own_disk(self):
        data = self.build()
        self.assertEqual(self.disk(data, "internal")["parts"]["prefixes"], 1000)
        self.assertEqual(self.disk(data, "sd:card")["parts"]["prefixes"], 0)
        row = next(r for r in self.disk(data, "internal")["games"] if r["name"] == "A")
        self.assertEqual(row["prefix"], 1000)
        self.assertEqual(row["backup"], 5)
        self.assertEqual(self.disk(data, "internal")["prefixes"][0]["appid"], 123)

    def test_games_are_sorted_and_limited(self):
        data = self.build()
        sizes = [g["size"] for g in self.disk(data, "internal")["games"]]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_duplicate_on_two_disks(self):
        data = self.build()
        dups = self.disk(data, "internal")["duplicates"]
        self.assertEqual(len(dups), 1)
        self.assertEqual(dups[0]["title"], "Same Game")
        self.assertEqual(dups[0]["size"], 370)
        self.assertEqual(sorted(dups[0]["disks"]), ["internal", "sd:card"])

    def test_missing_disk_is_not_fatal(self):
        self.disks[0]["total"] = None
        self.disks[0]["free"] = None
        data = self.build()
        internal = self.disk(data, "internal")
        self.assertIsNone(internal["used"])
        self.assertEqual(internal["parts"]["other"], 0)

    def test_report_returns_building_when_empty(self):
        with mock.patch.object(usage, "_run", side_effect=lambda: None):
            out = usage.report()
        self.assertTrue(out["building"])
        self.assertEqual(out["disks"], [])


if __name__ == "__main__":
    unittest.main()
