"""Moving a game to another disk: files, update backup, state and the Steam shortcut.

    python -m unittest discover -s tests
"""
import copy
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop import movegame, storage, usage  # noqa: E402
from deckdrop.state import STATE  # noqa: E402


def wait(job, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status in ("done", "error", "cancelled"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"move did not finish: {job.status} {job.error}")


class MoveTest(unittest.TestCase):
    def setUp(self):
        usage._SIZES.clear()
        self.base = Path(tempfile.mkdtemp())
        self.games = self.base / "home" / "Games"
        self.sd = self.base / "card"
        self.game = self.games / "Game"
        (self.game / "data").mkdir(parents=True)
        (self.game / "Game.exe").write_bytes(b"MZ" + b"x" * 100)
        (self.game / "data" / "level.bin").write_bytes(b"y" * 200)
        backup = self.games / ".deckdrop-updates" / "Game" / "files"
        backup.mkdir(parents=True)
        (backup / "old.bin").write_bytes(b"z" * 10)
        self.sd.mkdir()
        self.exe = str(self.game / "Game.exe")
        self.saved = {k: copy.deepcopy(STATE.get(k)) for k in ("added", "hidden", "imported", "pending")}
        STATE["added"] = {}
        STATE["hidden"] = []
        STATE["imported"] = []
        STATE["pending"] = []
        self.env = os.environ.get("DECKDROP_DISKS")
        os.environ["DECKDROP_DISKS"] = f"Card={self.sd}"
        storage._DISKS.update(at=0.0, scan=None)
        self.patches = [
            mock.patch.object(storage, "GAMES_DIR", self.games),
            mock.patch.object(movegame, "shortcuts_index", return_value={}),
            mock.patch.object(movegame, "game_running", return_value=False),
            mock.patch.object(movegame.CDP, "available", return_value=False),
            mock.patch.object(movegame, "save_state"),
        ]
        for patcher in self.patches:
            patcher.start()
        movegame.UPDATES.clear()
        with movegame.LOCK:
            movegame.JOBS.clear()

    def tearDown(self):
        for patcher in reversed(self.patches):
            patcher.stop()
        if self.env is None:
            os.environ.pop("DECKDROP_DISKS", None)
        else:
            os.environ["DECKDROP_DISKS"] = self.env
        storage._DISKS.update(at=0.0, scan=None)
        for key, value in self.saved.items():
            STATE[key] = value
        movegame.UPDATES.clear()
        with movegame.LOCK:
            movegame.JOBS.clear()

    def test_moves_with_the_backup_and_keeps_the_tree(self):
        STATE["added"] = {self.exe: {"at": 1, "appid": None, "name": "Game"}}
        job = wait(movegame.start(str(self.game), "sd:card"))
        self.assertEqual(job.status, "done", job.error)
        target = self.sd / "Games" / "Game"
        self.assertTrue((target / "Game.exe").is_file())
        self.assertEqual((target / "data" / "level.bin").read_bytes(), b"y" * 200)
        self.assertFalse(self.game.exists())
        self.assertEqual((self.sd / "Games" / ".deckdrop-updates" / "Game" / "files" / "old.bin").read_bytes(),
                         b"z" * 10)
        self.assertFalse((self.games / ".deckdrop-updates").exists())
        self.assertIn(str(target / "Game.exe"), STATE["added"])
        self.assertNotIn(self.exe, STATE["added"])
        self.assertEqual(job.new_path, str(target))

    def test_copy_path_is_used_when_disks_differ(self):
        with mock.patch.object(movegame, "_same_device", return_value=False):
            job = wait(movegame.start(str(self.game), "sd:card"))
        self.assertEqual(job.status, "done", job.error)
        target = self.sd / "Games" / "Game"
        self.assertEqual((target / "Game.exe").read_bytes(), b"MZ" + b"x" * 100)
        self.assertFalse(self.game.exists())

    def test_state_paths_follow_the_game(self):
        STATE["added"] = {self.exe: {"at": 1, "appid": 7, "name": "Game"}}
        STATE["hidden"] = [str(self.game)]
        STATE["imported"] = [str(self.game)]
        STATE["pending"] = [{"op": "rename", "exe": self.exe, "name": "Nice"}]
        with mock.patch.object(movegame.CDP, "available", return_value=True), \
                mock.patch.object(movegame.CDP, "set_exe") as setter:
            job = wait(movegame.start(str(self.game), "sd:card"))
        self.assertEqual(job.status, "done", job.error)
        target = self.sd / "Games" / "Game"
        exe = str(target / "Game.exe")
        setter.assert_called_once_with(7, f'"{exe}"', f'"{target}"')
        self.assertEqual(STATE["added"][str(target / "Game.exe")]["appid"], 7)
        self.assertEqual(STATE["hidden"], [str(target)])
        self.assertEqual(STATE["imported"], [])          # it is inside a games root now
        self.assertEqual(STATE["pending"][0]["exe"], str(target / "Game.exe"))

    def test_refuses_a_running_game(self):
        with mock.patch.object(movegame, "game_running", return_value=True):
            with self.assertRaises(ValueError):
                movegame.start(str(self.game), "sd:card")

    def test_refuses_a_shortcut_without_steam_control(self):
        STATE["added"] = {self.exe: {"at": 1, "appid": 7, "via": "cdp"}}
        with self.assertRaises(ValueError):
            movegame.start(str(self.game), "sd:card")

    def test_refuses_an_existing_target(self):
        (self.sd / "Games" / "Game").mkdir(parents=True)
        with self.assertRaises(ValueError):
            movegame.start(str(self.game), "sd:card")

    def test_refuses_the_same_disk(self):
        with self.assertRaises(ValueError):
            movegame.start(str(self.game), "internal")

    def test_refuses_when_there_is_no_space(self):
        with mock.patch.object(movegame.shutil, "disk_usage") as du:
            du.return_value = type("U", (), {"free": 1, "total": 1000})()
            with self.assertRaises(ValueError):
                movegame.start(str(self.game), "sd:card")


if __name__ == "__main__":
    unittest.main()
