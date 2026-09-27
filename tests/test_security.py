"""Security checks: foreign sites, DNS rebinding, paths out of a game, guessing the PIN."""
import io
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from deckdrop import state  # noqa: E402
from deckdrop.archives import drop_escaping_links  # noqa: E402
from deckdrop.art import images  # noqa: E402
from deckdrop.net import mask_proxy  # noqa: E402
from deckdrop.update import update_url_ok  # noqa: E402
from deckdrop.web.server import host_ok, origin_ok  # noqa: E402


class RulesTest(unittest.TestCase):
    def test_host(self):
        for h in ("192.168.1.20:8088", "10.0.0.5", "[fe80::1]:8088", "steamdeck", "steamdeck.local:8088",
                  "localhost:8088", "deck.lan", "deck.home.arpa", "deck.tail1234.ts.net", ""):
            self.assertTrue(host_ok(h), h)
        for h in ("evil.example.com", "evil.example.com:8088", "1.2.3.4.nip.io", "a b", "x:y:z"):
            self.assertFalse(host_ok(h), h)

    def test_extra_hosts(self):
        os.environ["DECKDROP_HOSTS"] = "deck.example.org, .mine.net"
        try:
            self.assertTrue(host_ok("deck.example.org:8088"))
            self.assertTrue(host_ok("a.mine.net"))
            self.assertFalse(host_ok("other.example.org"))
        finally:
            del os.environ["DECKDROP_HOSTS"]

    def test_origin(self):
        self.assertTrue(origin_ok(None, "192.168.1.20:8088"))
        self.assertTrue(origin_ok("http://192.168.1.20:8088", "192.168.1.20:8088"))
        self.assertFalse(origin_ok("https://evil.example.com", "192.168.1.20:8088"))
        self.assertFalse(origin_ok("null", "192.168.1.20:8088"))

    def test_update_url(self):
        self.assertTrue(update_url_ok("https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py"))
        self.assertTrue(update_url_ok("http://192.168.1.10:8000/deckdrop.py"))
        self.assertTrue(update_url_ok("http://my-pc.local:8000/deckdrop.py"))
        self.assertFalse(update_url_ok("http://example.com/deckdrop.py"))
        self.assertFalse(update_url_ok("http://8.8.8.8/deckdrop.py"))
        self.assertFalse(update_url_ok("file:///etc/passwd"))

    def test_throttle(self):
        t = state.Throttle()
        t.FREE, t.MAX_WAIT = 2, 60
        sleep, state.time.sleep = state.time.sleep, lambda s: None
        try:
            for _ in range(2):
                with self.assertRaisesRegex(PermissionError, "no"):
                    t.check(lambda: False, "no")
            with self.assertRaises(PermissionError) as e:      # locked: even the right answer waits
                t.check(lambda: True, "no")
            self.assertNotEqual(str(e.exception), "no")
            t.until = 0
            t.check(lambda: True, "no")
            self.assertEqual(t.fails, 0)
        finally:
            state.time.sleep = sleep


class ProxyMaskTest(unittest.TestCase):
    def test_no_credentials_shown(self):
        for raw, want in (("socks5://user:pass@10.0.0.2:1080", "socks5://***:***@10.0.0.2:1080"),
                          ("user:pass@10.0.0.2:3128", "***:***@10.0.0.2:3128"),
                          ("http://user:p/a#ss@proxy.lan:3128", "http://***:***@proxy.lan:3128"),
                          ("socks5://10.0.0.2:1080", "socks5://10.0.0.2:1080"), ("", "")):
            self.assertEqual(mask_proxy(raw), want)


class ImagesTest(unittest.TestCase):
    """Icons come from downloaded games: a crafted file must not hang or eat the Deck's memory."""

    def png(self, w, h, raw):
        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    def test_huge_sizes_refused(self):
        with self.assertRaises(ValueError):
            images.png_decode(self.png(1 << 28, 1, b"\0"))
        with self.assertRaises(ValueError):
            images.dib_decode(struct.pack("<IiiHHI", 40, 100000, 200000, 1, 32, 0) + b"\0" * 64)

    def test_zip_bomb_is_cut(self):
        w, h, px = images.png_decode(self.png(4, 4, b"\0" * (64 << 20)))
        self.assertEqual((w, h, len(px)), (4, 4, 64))

    def test_resource_loop(self):
        # a PE whose icon group directory entry points at itself
        m = bytearray(0x400)
        m[:2] = b"MZ"
        struct.pack_into("<I", m, 0x3C, 0x40)
        m[0x40:0x44] = b"PE\0\0"
        struct.pack_into("<HH", m, 0x46, 1, 0)             # 1 section; optional header size set below
        struct.pack_into("<H", m, 0x54, 0xE0)
        struct.pack_into("<H", m, 0x58, 0x10B)             # PE32
        struct.pack_into("<I", m, 0x58 + 96 + 16, 0x1000)  # resource directory RVA
        struct.pack_into("<IIII", m, 0x58 + 0xE0 + 8, 0x200, 0x1000, 0x200, 0x200)
        base = 0x200
        struct.pack_into("<HH", m, base + 12, 0, 2)
        struct.pack_into("<II", m, base + 16, 3, 0x80000000 | 0x40)
        struct.pack_into("<II", m, base + 24, 14, 0x80000000 | 0x80)
        struct.pack_into("<HH", m, base + 0x80 + 12, 0, 1)
        struct.pack_into("<II", m, base + 0x80 + 16, 1, 0x80000000 | 0x80)   # points at itself
        d = Path(tempfile.mkdtemp())
        try:
            (d / "loop.exe").write_bytes(bytes(m))
            t = time.time()
            self.assertIsNone(images.pe_icon(d / "loop.exe"))
            self.assertLess(time.time() - t, 2)
        finally:
            shutil.rmtree(d)


class LinksTest(unittest.TestCase):
    def test_links_out_of_an_archive_are_dropped(self):
        d = Path(tempfile.mkdtemp())
        try:
            (d / "lib").mkdir()
            (d / "lib" / "libfoo.so.1").write_bytes(b"x")
            os.symlink("libfoo.so.1", d / "lib" / "libfoo.so")
            os.symlink("/etc", d / "etc")
            os.symlink("../../..", d / "lib" / "up")
            self.assertEqual(drop_escaping_links(d), 2)
            self.assertTrue((d / "lib" / "libfoo.so").is_symlink())
            self.assertFalse(os.path.lexists(d / "etc"))
            self.assertFalse(os.path.lexists(d / "lib" / "up"))
        finally:
            shutil.rmtree(d)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_user_data import Running
        cls.home = Path(tempfile.mkdtemp())
        cls.game = cls.home / "Games" / "TestGame"
        (cls.game / "save").mkdir(parents=True)
        (cls.game / "game.sh").write_text("#!/bin/sh\n")
        (cls.home / "Elsewhere" / "Other").mkdir(parents=True)
        (cls.home / "Elsewhere" / "Other" / "run.sh").write_text("#!/bin/sh\n")
        (cls.home / "start.sh").write_text("#!/bin/sh\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(cls.home), DECKDROP_CEF="0", DECKDROP_PIN="1234", DECKDROP_STEAM=str(cls.home / "x"))
        cls.app = Running(ROOT / "tools" / "dev.py", env)

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def request(self, path, body=None, method=None, headers=None, raw=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", data=data, method=method,
                                     headers=dict({"Content-Type": "application/json"}, **(headers or {})))
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_page_headers(self):
        status, headers, _ = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("script-src 'sha256-", headers["Content-Security-Policy"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_dns_rebinding_is_refused(self):
        status, _, _ = self.request("/api/state", headers={"Host": "evil.example.com:8088"})
        self.assertEqual(status, 403)

    def test_foreign_site_cannot_post(self):
        body = {"url": "https://example.com/x.zip"}
        status, _, _ = self.request("/api/download", body, headers={"Origin": "https://evil.example.com"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("/api/settings", {"vndb_auto": True},
                                    headers={"Origin": f"http://127.0.0.1:{self.app.port}"})
        self.assertEqual(status, 200)

    def test_add_only_as_a_page(self):
        q = "/add?url=" + urllib.parse.quote("http://127.0.0.1:1/x.zip")
        status, _, _ = self.request(q, headers={"Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"})
        self.assertEqual(status, 403)

    def test_bad_content_length(self):
        status, _, _ = self.request("/api/settings", raw=b"{}", headers={"Content-Length": "-5"})
        self.assertEqual(status, 400)

    def test_import_home_is_refused(self):
        for path in (str(self.home), str(self.home / "start.sh"), "/"):
            status, _, body = self.request("/api/game/import", {"path": path})
            self.assertNotEqual(status, 200, (path, body))
        status, _, body = self.request("/api/game/import", {"path": str(self.home / "Elsewhere" / "Other")})
        self.assertEqual(status, 200, body)

    def test_saves_import_stays_in_save_folders(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("manifest.json", json.dumps({"game": "TestGame"}))
            z.writestr("game/save/1.sav", "ok")
            z.writestr("home/.renpy/TestGame/persistent", "ok")
            z.writestr("home/.bashrc", "evil")
            z.writestr("home/.config/systemd/user/evil.service", "evil")
            z.writestr("game/../../evil", "evil")
        q = urllib.parse.urlencode({"game": str(self.game), "exe": "game.sh"})
        status, _, body = self.request(f"/api/saves/import?{q}", raw=buf.getvalue(), method="PUT")
        self.assertEqual(status, 200, body)
        res = json.loads(body)
        self.assertEqual((res["written"], res["skipped"]), (2, 3))
        self.assertFalse((self.home / ".bashrc").exists())
        self.assertFalse((self.home / ".config" / "systemd").exists())
        self.assertEqual((self.home / ".renpy" / "TestGame" / "persistent").read_text(), "ok")

    def test_saves_need_a_game_folder(self):
        rel = (self.game / "game.sh").relative_to("/")
        q = urllib.parse.urlencode({"game": "/", "exe": str(rel)})
        status, _, _ = self.request(f"/api/saves/info?{q}")
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
