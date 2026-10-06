"""Gallery, several items at once: download them as one zip, delete them with one PIN.

Runs DeckDrop from src/ in a throwaway home whose ~/Pictures holds a few pictures, two of them
with the same file name in different folders.
"""
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_user_data import Running  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIN = "4321"
PICTURES = {"Lantern/shot.png": b"lantern", "Harbor/shot.png": b"harbor", "Harbor/sea.jpg": b"sea",
            "keep.jpg": b"keep"}


class MediaSelection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        home = Path(self.tmp.name)
        self.pics = home / "Pictures"
        for rel, data in PICTURES.items():
            (self.pics / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.pics / rel).write_bytes(data)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(home), DECKDROP_PIN=PIN, DECKDROP_CEF="0", DECKDROP_STEAM=str(home / "Steam"))
        self.app = Running(ROOT / "tools" / "dev.py", env)
        self.token = self.app.post_ok("/api/media/setup", {"password": "secret"})["token"]
        status, res = self.app.call(f"/api/media/list?t={self.token}")
        self.assertEqual(status, 200, res)
        self.items = {it["name"] if it["name"] != "shot.png" else it["game"] + "/shot.png": it for it in res["items"]}

    def tearDown(self):
        self.app.stop()
        self.tmp.cleanup()

    def raw(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.app.port}{path}", timeout=30) as r:
            return r.headers, r.read()

    def test_zip_of_the_selection(self):
        ids = [self.items[k]["id"] for k in ("Lantern/shot.png", "Harbor/shot.png", "sea.jpg")]
        headers, data = self.raw(f"/api/media/zip?t={self.token}&ids={','.join(ids)}")
        self.assertIn("attachment", headers["Content-Disposition"])
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            self.assertIsNone(z.testzip())
            got = {n: z.read(n) for n in z.namelist()}
        self.assertEqual(got, {"Lantern/shot.png": b"lantern", "Harbor/shot.png": b"harbor",
                               "Harbor/sea.jpg": b"sea"})

    def test_zip_needs_the_password_and_known_ids(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.raw(f"/api/media/zip?ids={self.items['sea.jpg']['id']}")
        self.assertEqual(e.exception.code, 401)
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.raw(f"/api/media/zip?t={self.token}&ids={self.items['sea.jpg']['id']},0123456789abcdef")
        self.assertEqual(e.exception.code, 404)

    def test_delete_several_with_one_pin(self):
        ids = [self.items[k]["id"] for k in ("Lantern/shot.png", "sea.jpg")]
        status, res = self.app.call("/api/media/delete", {"ids": ids, "pin": "0000", "token": self.token})
        self.assertEqual(status, 403, res)
        self.assertTrue((self.pics / "Harbor/sea.jpg").is_file(), "a wrong PIN deletes nothing")
        res = self.app.post_ok("/api/media/delete", {"ids": ids, "pin": PIN, "token": self.token})
        self.assertEqual(sorted(res["removed"]), ["sea.jpg", "shot.png"])
        self.assertEqual(res["errors"], [])
        left = sorted(str(p.relative_to(self.pics)) for p in self.pics.rglob("*") if p.is_file())
        self.assertEqual(left, ["Harbor/shot.png", "keep.jpg"])

    def test_delete_one_still_works(self):
        res = self.app.post_ok("/api/media/delete", {"id": self.items["keep.jpg"]["id"], "pin": PIN,
                                                     "token": self.token})
        self.assertEqual(res["removed"], "keep.jpg")
        self.assertFalse((self.pics / "keep.jpg").exists())


if __name__ == "__main__":
    unittest.main()
