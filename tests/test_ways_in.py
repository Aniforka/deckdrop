"""The two new ways in: a whole game folder from a PC, and a magnet or .torrent link.

The folder is sent the way the page sends it: a job for the upload, every file with its
relative path, then "done". DeckDrop has to keep the tree, refuse a path that climbs out of
the upload folder, and end with the folder next to the other games.

    python -m unittest discover -s tests
"""
import json
import http.server
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from test_user_data import ROOT, Running  # noqa: E402

from deckdrop import adopt, jobs, paths, torrent  # noqa: E402


def put(app, path, data):
    """A raw PUT, the way the page sends one file of a folder."""
    req = urllib.request.Request(f"http://127.0.0.1:{app.port}{path}", data=data, method="PUT")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class TreeUploadTest(unittest.TestCase):
    """A folder from a PC becomes a game, file by file."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        (cls.home / "no-steam").mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(cls.home), DECKDROP_CEF="0", DECKDROP_PIN="1234",
                   DECKDROP_STEAM=str(cls.home / "no-steam"))
        cls.app = Running(ROOT / "tools" / "dev.py", env)

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def begin(self, name, total, files):
        status, job = self.app.call("/api/upload/tree/begin",
                                    {"name": name, "total": total, "files": files})
        self.assertEqual(status, 200, job)
        return job

    def wait_done(self, job_id, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            status, state = self.app.call("/api/state")
            self.assertEqual(status, 200)
            job = next((j for j in state.get("jobs", []) if j["id"] == job_id), None)
            if job and job["status"] in ("done", "error", "cancelled"):
                return job
            time.sleep(0.2)
        self.fail(f"upload {job_id} did not finish")

    def test_folder_arrives_as_a_game(self):
        exe, data = b"MZ game", b"level 1"
        job = self.begin("Tree Game", len(exe) + len(data), 2)
        for rel, body in (("Game.exe", exe), ("data/level1.dat", data)):
            status, res = put(self.app, f"/api/upload/tree?job={job['id']}&path="
                                        + urllib.parse.quote(rel, safe=""), body)
            self.assertEqual(status, 200, res)
            self.assertEqual(res["size"], len(body))
        status, res = self.app.call("/api/upload/tree/done", {"job": job["id"]})
        self.assertEqual(status, 200, res)
        job = self.wait_done(job["id"])
        self.assertEqual(job["status"], "done", job)
        game = self.home / "Games" / "Tree Game"
        self.assertEqual((game / "Game.exe").read_bytes(), exe)
        self.assertEqual((game / "data" / "level1.dat").read_bytes(), data)
        self.assertFalse(list((self.home / "Games" / "_inbox").glob(".tree-*")),
                         "the upload folder should be gone")

    def test_a_path_cannot_climb_out(self):
        job = self.begin("Escape", 4, 1)
        status, res = put(self.app, f"/api/upload/tree?job={job['id']}&path=..%2Fescaped.txt", b"evil")
        self.assertEqual(status, 400, res)
        self.app.call("/api/upload/tree/abort", {"job": job["id"]})
        self.assertFalse((self.home / "escaped.txt").exists())
        self.assertFalse((self.home / "Games" / "escaped.txt").exists())

    def test_the_limit_on_files(self):
        status, res = self.app.call("/api/upload/tree/begin",
                                    {"name": "Huge", "files": 100_001, "total": 1})
        self.assertEqual(status, 400, res)
        self.assertIn("100000", res.get("error", ""))


class AdoptTest(unittest.TestCase):
    """A downloaded folder is turned into a game the same way for a folder upload and a torrent."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_one_nested_folder_is_flattened(self):
        """The usual repack shape: Games/Foo/bin/Game.exe, and the game is Foo, not Games."""
        (self.root / "Foo" / "bin").mkdir(parents=True)
        (self.root / "Foo" / "bin" / "Game.exe").write_bytes(b"MZ")
        self.assertEqual(adopt.game_source(self.root), self.root / "Foo")

    def test_a_folder_with_the_exe_is_kept(self):
        (self.root / "Foo").mkdir()
        (self.root / "Foo" / "game.exe").write_bytes(b"MZ")
        (self.root / "Foo" / "data").mkdir()
        self.assertEqual(adopt.game_source(self.root / "Foo"), self.root / "Foo")

    def test_an_exe_in_a_direct_subfolder_needs_no_flattening(self):
        (self.root / "Foo").mkdir()
        (self.root / "Foo" / "Game.exe").write_bytes(b"MZ")
        self.assertEqual(adopt.game_source(self.root), self.root)

    def test_move_game_renames_and_moves(self):
        (self.root / "work" / "Torrent Name").mkdir(parents=True)
        (self.root / "work" / "Torrent Name" / "Game.exe").write_bytes(b"MZ")
        (self.root / "Games").mkdir()
        job = jobs.new_job("torrent", "Torrent Name", self.root / "Games")
        dest = adopt.move_game(job, self.root / "work" / "Torrent Name", "Clean Name")
        self.assertEqual(dest, self.root / "Games" / "Clean Name")
        self.assertTrue((dest / "Game.exe").is_file())
        self.assertEqual(job.game_dir, str(dest))

    def test_an_installer_is_not_a_game(self):
        """maybe_add looks at the exe without its extension: a shortcut to setup.exe is not a game."""
        self.assertTrue(adopt.INSTALLER.match("setup"))
        self.assertTrue(adopt.INSTALLER.match("install"))
        self.assertTrue(adopt.INSTALLER.match("Game-Setup"))
        self.assertFalse(adopt.INSTALLER.match("Game"))
        self.assertFalse(adopt.INSTALLER.match("game64"))


class SafePathTest(unittest.TestCase):
    def test_relative_paths_are_kept(self):
        self.assertEqual(paths.safe_rel("data/level1.dat"), "data/level1.dat")
        self.assertEqual(paths.safe_rel("data\\level1.dat"), "data/level1.dat")
        self.assertEqual(paths.safe_rel("/game/a.txt"), "game/a.txt")

    def test_escaping_paths_are_refused(self):
        for bad in ("../evil", "a/../../evil", "C:/Windows/evil", "C:\\Windows\\evil"):
            self.assertEqual(paths.safe_rel(bad), "", bad)

    def test_reserved_characters_are_replaced(self):
        self.assertEqual(paths.safe_rel('a<b>c:d.txt'), "a_b_c_d.txt")

    def test_unique_file_keeps_the_name_then_numbers(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        self.assertEqual(paths.unique_file(d, "Game.zip"), d / "Game.zip")
        (d / "Game.zip").write_bytes(b"")
        self.assertEqual(paths.unique_file(d, "Game.zip"), d / "Game (2).zip")


class TorrentLinkTest(unittest.TestCase):
    def test_magnets_and_torrent_files(self):
        self.assertTrue(torrent.is_torrent("magnet:?xt=urn:btih:abc&dn=Game"))
        self.assertTrue(torrent.is_torrent("https://example.com/Game.torrent"))
        self.assertFalse(torrent.is_torrent("https://example.com/Game.zip"))
        self.assertFalse(torrent.is_torrent("magnet"))

    def test_a_name_comes_from_the_link(self):
        self.assertEqual(torrent.torrent_name("magnet:?xt=urn:btih:abc&dn=My%20Game"), "My Game")
        self.assertEqual(torrent.torrent_name("https://example.com/Game.torrent"), "Game.torrent")

    def test_something_else_is_refused_before_a_job_is_made(self):
        before = len(jobs.JOBS)
        with self.assertRaises(ValueError):
            torrent.start("https://example.com/Game.zip")
        self.assertEqual(len(jobs.JOBS), before, "a link that is not a torrent made no job")


class FakeQB(http.server.BaseHTTPRequestHandler):
    """The part of the qBittorrent Web API DeckDrop uses, reporting one finished torrent."""

    content = None

    def log_message(self, *args):
        pass

    def answer(self, body, code=200, headers=None):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/v2/app/version"):
            return self.answer("v4.6.0")
        if self.path.startswith("/api/v2/torrents/info"):
            return self.answer(json.dumps([{
                "hash": "abc", "name": "Torrent Game", "progress": 1.0, "state": "uploading",
                "content_path": str(self.content), "save_path": str(self.content.parent),
                "total_size": 4, "dlspeed": 0,
            }]))
        self.answer("", 404)

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path.startswith("/api/v2/auth/login"):
            return self.answer("Ok.", headers={"Set-Cookie": "SID=test; path=/"})
        if self.path.startswith("/api/v2/torrents/add"):
            return self.answer("Ok.")
        if self.path.startswith("/api/v2/torrents/delete"):
            return self.answer("")
        self.answer("", 404)


class TorrentFlowTest(unittest.TestCase):
    """A torrent the client has finished arrives as a game in the games folder."""

    def test_a_finished_torrent_becomes_a_game(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        content = root / "content" / "Torrent Game"
        content.mkdir(parents=True)
        (content / "Game.exe").write_bytes(b"MZ game")
        FakeQB.content = content
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeQB)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        saved = dict(torrent.STATE)
        self.addCleanup(lambda: (torrent.STATE.clear(), torrent.STATE.update(saved)))
        torrent.STATE["torrent_url"] = f"http://127.0.0.1:{server.server_address[1]}"
        torrent.STATE["auto_add_steam"] = False      # no Steam writes from a unit test
        (root / "Games").mkdir()
        job = jobs.new_job("torrent", "Torrent Game", root / "Games")
        threading.Thread(target=torrent.run, daemon=True,
                         args=(job, "magnet:?xt=urn:btih:abc&dn=Torrent+Game")).start()
        deadline = time.time() + 20
        while job.status not in ("done", "error", "cancelled") and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(job.status, "done", job.error)
        self.assertTrue((root / "Games" / "Torrent Game" / "Game.exe").is_file())


if __name__ == "__main__":
    unittest.main()
