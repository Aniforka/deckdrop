"""The page polls every second: the game list must not walk every game's files each time.

find_exes() is cached per game folder by the mtimes of the folder and its subfolders, and the
disk scan (mounts, free space) is shared for DISKS_TTL. These tests pin down that the cache is
used, and that it never hides a change.

    python -m unittest discover -s tests
"""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop import detect, i18n, storage  # noqa: E402

OLD = time.time() - 3600


def age(*paths, when=OLD):
    """Pretend the folders were last changed long ago, as a game that has been on the disk a while."""
    for p in paths:
        os.utime(p, (when, when))


class FindExesCacheTest(unittest.TestCase):
    def setUp(self):
        self.game = Path(tempfile.mkdtemp()) / "Some Game"
        (self.game / "bin").mkdir(parents=True)
        (self.game / "Game_Data").mkdir()
        (self.game / "Game.exe").write_bytes(b"MZ")
        (self.game / "bin" / "Tool.exe").write_bytes(b"MZ")
        for i in range(50):
            (self.game / "Game_Data" / f"a{i}.assets").touch()
        age(self.game, self.game / "bin", self.game / "Game_Data")
        detect._EXES.clear()
        self.globs = 0
        real = detect._glob_exes

        def counting(d):
            self.globs += 1
            return real(d)
        patcher = mock.patch.object(detect, "_glob_exes", counting)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_look_does_not_walk_the_files(self):
        self.assertEqual(detect.find_exes(self.game), ["Game.exe", "bin/Tool.exe"])
        self.assertEqual(detect.find_exes(self.game), ["Game.exe", "bin/Tool.exe"])
        self.assertEqual(self.globs, 1)

    def test_new_exe_in_a_subfolder_is_seen(self):
        detect.find_exes(self.game)
        (self.game / "bin" / "Editor.exe").write_bytes(b"MZ")
        age(self.game / "bin", when=OLD + 60)               # its folder changed, as the file system does
        self.assertIn("bin/Editor.exe", detect.find_exes(self.game))
        self.assertEqual(self.globs, 2)

    def test_removed_and_renamed_exes_are_seen(self):
        detect.find_exes(self.game)
        (self.game / "Game.exe").rename(self.game / "Renamed.exe")
        (self.game / "bin" / "Tool.exe").unlink()
        age(self.game, self.game / "bin", when=OLD + 60)
        self.assertEqual(detect.find_exes(self.game), ["Renamed.exe"])

    def test_new_subfolder_is_seen(self):
        detect.find_exes(self.game)
        (self.game / "linux").mkdir()
        (self.game / "linux" / "game.x86_64").write_bytes(b"\x7fELF")
        age(self.game / "linux", self.game, when=OLD + 60)
        self.assertIn("linux/game.x86_64", detect.find_exes(self.game))

    def test_a_folder_changed_just_now_is_not_cached(self):
        """Coarse clocks (FAT keeps 2 s) could hide a second change inside the same tick."""
        age(self.game / "bin", when=time.time())
        detect.find_exes(self.game)
        detect.find_exes(self.game)
        self.assertEqual(self.globs, 2)

    def test_callers_cannot_spoil_the_cache(self):
        detect.find_exes(self.game).append("junk.exe")
        self.assertNotIn("junk.exe", detect.find_exes(self.game))

    def test_missing_folder(self):
        self.assertEqual(detect.find_exes(self.game.parent / "gone"), [])


class DisksCacheTest(unittest.TestCase):
    def setUp(self):
        storage._DISKS["scan"] = None
        self.addCleanup(lambda: storage._DISKS.update(scan=None))

    def test_one_scan_per_ttl(self):
        with mock.patch.object(storage, "sd_mounts", wraps=storage.sd_mounts) as mounts:
            storage.disks()
            storage.disks()
            storage.disk_label_for(Path.home())
            storage.game_roots()
            self.assertEqual(mounts.call_count, 1)
            storage._DISKS["at"] -= storage.DISKS_TTL          # time passes
            storage.disks()
            self.assertEqual(mounts.call_count, 2)

    def test_labels_follow_the_language(self):
        i18n.set_current("ru")
        self.assertEqual(storage.disks()[0]["label"], "Внутренний")
        i18n.set_current("en")
        self.assertEqual(storage.disks()[0]["label"], "Internal")


if __name__ == "__main__":
    unittest.main()
