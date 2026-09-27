"""User data must survive an update: nothing the web app stored may be lost or rewritten.

An installed copy with a lived-in home (state.json with every setting changed from its
default, games, the inbox, the cache) updates itself, then the new version runs on the
same home. Checked:

- the update touches nothing but the program file and its .bak;
- the new version sees the same settings, admin PIN and media password;
- when it saves state.json, every stored key is written back unchanged, including keys
  it does not know (written by a newer version); the only intended change is the
  update link moving from main to the releases.

    python -m unittest discover -s tests
"""
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from test_update import LEGACY_COMMIT, ROOT, UPDATE, Served, build

LEGACY_URL = "https://raw.githubusercontent.com/aniforka/deckdrop/main/deckdrop.py"
RELEASE_URL = "https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py"
PIN = "4321"
MEDIA_PASSWORD = "секрет gallery"


def pbkdf2(password, salt):
    # same scheme as state.pw_hash; spelled out so the test does not trust the code it checks
    return {"salt": salt,
            "hash": hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000).hex()}


def lived_in_home(home, update_url):
    """A home as a user has it after a while; returns the state.json written."""
    games = home / "Games"
    game = games / "My Game"
    (game / "save").mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"MZ" + b"\0" * 64)
    (game / "save" / "slot1.sav").write_bytes(os.urandom(256))
    (games / "_inbox").mkdir()
    (games / "_inbox" / "Other.part1.rar").write_bytes(os.urandom(512))
    imported = home / "Elsewhere" / "Imported Game"
    imported.mkdir(parents=True)
    (imported / "run.sh").write_text("#!/bin/sh\n")
    hidden = games / "Hidden Game"
    hidden.mkdir()
    (hidden / "h.exe").write_bytes(b"MZ")
    (home / ".cache" / "deckdrop" / "patches").mkdir(parents=True)
    (home / ".cache" / "deckdrop" / "thumb.png").write_bytes(os.urandom(64))
    (home / "deckdrop" / "notes.txt").write_text("мои заметки\n", "utf-8")
    exe = str(game / "Game.exe")
    state = {
        "admin_pin": PIN,
        "hidden": [str(hidden)],
        "imported": [str(imported)],
        "added": {exe: {"at": 1700000000, "appid": 3123456789, "name": "Моя игра", "compat": "proton_9",
                        "via": "cdp", "art": True, "art_source": "vndb", "vndb": "v17"}},
        "pending": [{"op": "rename", "exe": exe, "name": "Моя игра"}],
        "update_url": update_url,
        "media_pw": pbkdf2(MEDIA_PASSWORD, "00112233445566778899aabbccddeeff"),
        "default_compat": "proton_9",
        "prefer_linux": False,
        "vndb_auto": False,
        "vndb_nsfw": False,
        "archive_passwords": ["pw1", "пароль2"],
        "default_disk": "internal",
        "proxy": "socks5://user:p%40ss@10.0.0.1:1080",
        "proxy_downloads": True,
        "mega_verify": False,
        "cef_enabled": False,
        "from_a_future_version": {"keep": ["me", 1, None]},
    }
    cfg = home / ".config" / "deckdrop"
    cfg.mkdir(parents=True)
    (cfg / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")
    return state


def snapshot(home, program):
    """Content hash of every file in home, except the program and its update leftovers."""
    skip = {program, program.with_suffix(".py.bak"), program.with_suffix(".py.new")}
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(home.rglob("*")) if p.is_file() and p not in skip and "__pycache__" not in p.parts}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    """The installed program serving on a free port, as the systemd service would run it."""

    def __init__(self, program, env):
        self.port = free_port()
        self.proc = subprocess.Popen([sys.executable, str(program)], env=dict(env, DECKDROP_PORT=str(self.port)),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while True:
            try:
                self.call("/api/state")
                return
            except OSError:
                if self.proc.poll() is not None or time.time() > deadline:
                    self.stop()
                    raise AssertionError("new version did not start:\n" + self.log)
                time.sleep(0.3)

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def stop(self):
        self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.log = self.proc.stdout.read().decode(errors="replace")
        self.proc.stdout.close()


class UserDataTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.program = self.home / "deckdrop" / "deckdrop.py"
        self.program.parent.mkdir()
        self.state_file = self.home / ".config" / "deckdrop" / "state.json"
        # like the systemd unit: no overrides, everything comes from the default paths in HOME
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        self.env.update(HOME=str(self.home), DECKDROP_NO_RESTART="1", DECKDROP_CEF="0",
                        DECKDROP_STEAM=str(self.home / "no-steam"))
        (self.home / "serve").mkdir()
        (self.home / "serve" / "deckdrop.py").write_text(build.build(), "utf-8")
        self.server = Served(self.home / "serve")

    def tearDown(self):
        self.server.close()

    def check(self, installed_program, update_url, expect_url):
        self.program.write_bytes(installed_program)
        state = lived_in_home(self.home, update_url)
        before = snapshot(self.home, self.program)
        del before["serve/deckdrop.py"]

        res = subprocess.run([sys.executable, "-c", UPDATE, str(self.program), self.server.url + "deckdrop.py"],
                             env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertEqual(self.program.read_text("utf-8"), build.build(), "program was not replaced")

        after = snapshot(self.home, self.program)
        del after["serve/deckdrop.py"]
        self.assertEqual(after, before, "the update changed files other than the program")

        app = Running(self.program, self.env)
        try:
            status, st = app.call("/api/state")
            self.assertEqual(status, 200)
            settings = st["settings"]
            for key in ("default_compat", "prefer_linux", "vndb_auto", "vndb_nsfw", "archive_passwords",
                        "default_disk", "cef_enabled", "proxy_downloads", "mega_verify"):
                self.assertEqual(settings[key], state[key], key)
            self.assertEqual(settings["update_url"], expect_url)
            self.assertTrue(settings["proxy_set"], "proxy lost")
            self.assertNotIn("p%40ss", json.dumps(st), "proxy password leaked to the page")
            names = {g["name"] for g in st["games"]}
            self.assertIn("My Game", names)
            self.assertIn("Imported Game", names)

            status, res = app.call("/api/media/login", {"password": MEDIA_PASSWORD})
            self.assertEqual(status, 200, f"media password lost: {res}")
            self.assertTrue(res.get("token"))

            # a PIN-protected write: proves the PIN survived and makes the new version save state.json
            status, res = app.call("/api/settings", {"pin": PIN, "proxy": state["proxy"], "vndb_auto": True})
            self.assertEqual(status, 200, f"admin PIN lost: {res}")
        finally:
            app.stop()

        saved = json.loads(self.state_file.read_text("utf-8"))
        want = dict(state, update_url=expect_url, vndb_auto=True)
        for key, value in want.items():
            self.assertIn(key, saved, f"{key} dropped from state.json\n{app.log}")
            self.assertEqual(saved[key], value, f"{key} rewritten in state.json")

    def test_legacy_single_file_to_build(self):
        res = subprocess.run(["git", "show", f"{LEGACY_COMMIT}:deckdrop.py"], cwd=ROOT, capture_output=True)
        if res.returncode != 0:
            self.skipTest("legacy commit is not in this checkout (shallow clone)")
        self.check(res.stdout, LEGACY_URL, RELEASE_URL)

    def test_build_to_build_keeps_own_update_url(self):
        own = "http://192.168.1.10:8000/deckdrop.py"
        old = build.build().replace(f'__version__ = "{self.version()}"', '__version__ = "0.0.1"')
        self.check(old.encode(), own, own)

    @staticmethod
    def version():
        text = (ROOT / "src" / "deckdrop" / "__init__.py").read_text("utf-8")
        return text.split('__version__ = "', 1)[1].split('"', 1)[0]


if __name__ == "__main__":
    unittest.main()
