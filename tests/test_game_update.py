"""A new version of a game over the old one: saves, settings and the Steam shortcut survive.

Runs DeckDrop from src/ in a throwaway home with one game in ~/Games, "added to Steam" in its
state file, and uploads new versions of it the way the game page does.
"""
import io
import json
import os
import http.server
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_user_data import Running  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OLD = {"Game.exe": "exe v1", "data/a.dat": "a v1", "readme_v1.txt": "old only",
       "game/saves/1-1.save": "MY SAVE", "game/saves/persistent": "MY PERSISTENT", "config.ini": "volume=3"}


def zip_of(files, top="Lantern-1.3-pc"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for rel, text in files.items():
            z.writestr(f"{top}/{rel}" if top else rel, text)
    return buf.getvalue()


class GameUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.game = home / "Games" / "Lantern"
        for rel, text in OLD.items():
            (self.game / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.game / rel).write_text(text)
        self.state = home / ".config" / "deckdrop" / "state.json"
        self.state.parent.mkdir(parents=True)
        exe = str((self.game / "Game.exe").resolve())
        self.state.write_text(json.dumps({"admin_pin": "4321", "added": {
            exe: {"at": 1, "appid": 3000000001, "name": "Lantern", "via": "cdp", "compat": "proton_9"}}}))
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(home), DECKDROP_CEF="0", DECKDROP_STEAM=str(home / "Steam"))
        self.app = Running(ROOT / "tools" / "dev.py", env)
        self.gpath = str(self.game.resolve())

    def tearDown(self):
        self.app.stop()
        self.tmp.cleanup()

    def files(self):
        return {p.relative_to(self.game).as_posix(): p.read_text() for p in self.game.rglob("*") if p.is_file()}

    def upload(self, data, name="Lantern-1.3-pc.zip", exe="Game.exe"):
        q = urllib.parse.urlencode({"game": self.gpath, "exe": exe, "name": name})
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}/api/game/update/upload?{q}",
                                     data=data, method="PUT")
        with urllib.request.urlopen(req, timeout=30) as r:
            job = json.loads(r.read())
        deadline = time.time() + 20
        while time.time() < deadline:
            _, st = self.app.get("/api/state")
            j = next(x for x in st["jobs"] if x["id"] == job["id"])
            if j["status"] not in ("uploading", "extracting", "queued"):
                self.assertEqual(j["status"], "update_ready", j)
                self.assertEqual(j["update"], "Lantern")
                break
            time.sleep(0.2)
        status, res = self.app.get("/api/game/update?" + urllib.parse.urlencode({"game": self.gpath}))
        self.assertEqual(status, 200, res)
        self.assertTrue(res["pending"], res)
        return res["pending"]

    def apply(self, pending, **kw):
        return self.app.post_ok("/api/game/update/apply", {"game": self.gpath, "token": pending["token"], **kw})

    def test_new_version_keeps_saves_and_settings(self):
        new = {"Game.exe": "exe v2", "data/a.dat": "a v2", "data/b.dat": "b new",
               "game/saves/persistent": "SHIPPED DEFAULT", "config.ini": "volume=10"}
        p = self.upload(zip_of(new))
        self.assertEqual(p["version"], "Lantern-1.3-pc")
        self.assertTrue(p["exe_found"])
        self.assertEqual(p["new_exe"], "Game.exe")
        self.assertEqual((p["added"], p["replaced"], p["kept_saves"]), (1, 2, 1))
        self.assertEqual(p["settings"], ["config.ini"])
        self.assertEqual(p["old_only"], 2)                        # readme_v1.txt and a save
        res = self.apply(p)
        self.assertEqual((res["added"], res["replaced"], res["kept_saves"], res["kept_settings"]), (1, 2, 1, 1))
        self.assertIsNone(res["exe_change"])
        self.assertEqual(self.files(), {
            "Game.exe": "exe v2", "data/a.dat": "a v2", "data/b.dat": "b new", "readme_v1.txt": "old only",
            "game/saves/1-1.save": "MY SAVE", "game/saves/persistent": "MY PERSISTENT", "config.ini": "volume=3"})
        st = json.loads(self.state.read_text())
        self.assertEqual(st["added"][str((self.game / "Game.exe").resolve())]["appid"], 3000000001)
        self.assertFalse(list((self.game.parent / ".deckdrop-updates").glob("stage-*")), "stage cleared")

        # the old version comes back, saves made meanwhile stay
        (self.game / "game/saves/2-1.save").write_text("PLAYED ON v2")
        status, res = self.app.get("/api/game/update?" + urllib.parse.urlencode({"game": self.gpath}))
        self.assertEqual(res["backup"]["replaced"], 2)
        self.app.post_ok("/api/game/update/rollback", {"game": self.gpath})
        want = dict(OLD, **{"game/saves/2-1.save": "PLAYED ON v2"})
        self.assertEqual(self.files(), want)
        self.assertFalse((self.game / "data/b.dat").exists())
        status, res = self.app.get("/api/game/update?" + urllib.parse.urlencode({"game": self.gpath}))
        self.assertIsNone(res["backup"])

    def test_by_link(self):
        www = Path(self.tmp.name) / "www"
        www.mkdir()
        (www / "Lantern-2.0.zip").write_bytes(zip_of({"Game.exe": "exe v2"}))
        handler = lambda *a, **kw: http.server.SimpleHTTPRequestHandler(*a, directory=str(www), **kw)  # noqa: E731
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            job = self.app.post_ok("/api/game/update/link", {
                "game": self.gpath, "exe": "Game.exe", "url": f"http://127.0.0.1:{srv.server_port}/Lantern-2.0.zip"})
            self.assertEqual(job["update"], "Lantern")
            deadline = time.time() + 20
            while time.time() < deadline:
                _, st = self.app.get("/api/state")
                j = next(x for x in st["jobs"] if x["id"] == job["id"])
                if j["status"] in ("update_ready", "error", "done"):
                    break
                time.sleep(0.2)
            self.assertEqual(j["status"], "update_ready", j)
        finally:
            srv.shutdown()
            srv.server_close()
        _, res = self.app.get("/api/game/update?" + urllib.parse.urlencode({"game": self.gpath}))
        self.apply(res["pending"])
        self.assertEqual(self.files()["Game.exe"], "exe v2")
        self.assertFalse(list((Path(self.tmp.name) / "Games" / "_inbox").glob("*.zip")), "the archive is not kept")
        _, st = self.app.get("/api/state")
        self.assertEqual([g["name"] for g in st["games"]], ["Lantern"], "no second game appears")

    def test_settings_replaced_when_asked(self):
        p = self.upload(zip_of({"Game.exe": "exe v2", "config.ini": "volume=10"}))
        self.apply(p, keep_settings=False)
        self.assertEqual(self.files()["config.ini"], "volume=10")

    def test_cleanup_moves_old_files_aside_but_not_saves(self):
        p = self.upload(zip_of({"Game.exe": "exe v2", "data/a.dat": "a v2"}, top=None))
        self.assertEqual(p["old_only"] - p["old_only_kept"], 1)
        res = self.apply(p, cleanup=True)
        self.assertEqual(res["removed"], 1)                       # readme_v1.txt; not a save or config.ini
        self.assertEqual(self.files(), {"Game.exe": "exe v2", "data/a.dat": "a v2", "config.ini": "volume=3",
                                        "game/saves/1-1.save": "MY SAVE",
                                        "game/saves/persistent": "MY PERSISTENT"})
        self.app.post_ok("/api/game/update/rollback", {"game": self.gpath})
        self.assertEqual(self.files(), OLD)

    def test_cleanup_with_settings_replaced(self):
        p = self.upload(zip_of({"Game.exe": "exe v2"}, top=None))
        res = self.apply(p, cleanup=True, keep_settings=False)
        self.assertEqual(res["removed"], 3)                       # readme_v1.txt, data/a.dat, config.ini
        self.assertNotIn("config.ini", self.files())
        self.assertIn("game/saves/1-1.save", self.files())

    def test_renamed_exe_keeps_the_shortcut(self):
        new = {"Lantern-1.3.exe": "exe v2", "data/a.dat": "a v2"}
        p = self.upload(zip_of(new))
        self.assertFalse(p["exe_found"])
        self.assertEqual(p["new_exe"], "Lantern-1.3.exe")
        res = self.apply(p, exe="Lantern-1.3.exe")
        self.assertEqual(res["exe_change"]["new"], "Lantern-1.3.exe")
        st = json.loads(self.state.read_text())
        old, new_full = str((self.game / "Game.exe").resolve()), str((self.game / "Lantern-1.3.exe").resolve())
        self.assertNotIn(old, st["added"])
        self.assertEqual(st["added"][new_full]["appid"], 3000000001, "same shortcut, same Proton prefix")
        self.assertEqual(st["added"][new_full]["compat"], "proton_9")
        self.assertIn({"op": "exe", "exe": new_full, "old": old, "appid": 3000000001}, st["pending"])
        self.app.post_ok("/api/game/update/rollback", {"game": self.gpath})
        st = json.loads(self.state.read_text())
        self.assertIn(old, st["added"])
        self.assertNotIn(new_full, st["added"])
        self.assertEqual(self.files(), OLD)

    def test_deeper_archive_lines_up_with_the_game(self):
        # the old game keeps its exe in bin/, the new archive has one more folder level
        p = self.upload(zip_of({"Game-1.3/Game.exe": "exe v2", "Game-1.3/data/a.dat": "a v2",
                                "Extras/manual.pdf": "pdf"}, top=None))
        self.assertTrue(p["exe_found"])
        self.apply(p)
        self.assertEqual(self.files()["Game.exe"], "exe v2")
        self.assertEqual(self.files()["data/a.dat"], "a v2")
        self.assertNotIn("Game-1.3/Game.exe", self.files())

    def test_not_while_the_game_runs(self):
        p = self.upload(zip_of({"Game.exe": "exe v2"}))
        proc = subprocess.Popen(["sleep", "30"], cwd=self.game)
        try:
            status, res = self.app.call("/api/game/update/apply", {"game": self.gpath, "token": p["token"]})
            self.assertEqual(status, 400, res)
            self.assertEqual(self.files()["Game.exe"], "exe v1")
        finally:
            proc.kill()
            proc.wait()
        self.apply(p)
        self.assertEqual(self.files()["Game.exe"], "exe v2")

    def test_discard_leaves_the_game_alone(self):
        p = self.upload(zip_of({"Game.exe": "exe v2"}))
        self.app.post_ok("/api/game/update/discard", {"game": self.gpath})
        status, res = self.app.call("/api/game/update/apply", {"game": self.gpath, "token": p["token"]})
        self.assertEqual(status, 400, res)
        self.assertEqual(self.files(), OLD)
        self.assertFalse(list((self.game.parent / ".deckdrop-updates").glob("stage-*")))

    def test_only_a_listed_game(self):
        q = urllib.parse.urlencode({"game": str(Path(self.tmp.name)), "exe": "x.exe", "name": "a.zip"})
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}/api/game/update/upload?{q}",
                                     data=b"zz", method="PUT")
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=30)
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
