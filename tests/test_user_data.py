"""A regular update must not lose or rewrite anything the user stored in the web app.

For each earlier release (the last few v* tags; the last single-file version while there
are none) this runs what a user does:

1. the old version runs in a fresh home, and the user sets it up through its own web API:
   changes the admin PIN, sets every setting away from its default (proxy with a password
   included), sets the media gallery password, hides one game and imports another;
   state the UI cannot write (games added to Steam, pending Steam ops, a key from a newer
   version) is put into state.json the way those versions leave it;
2. the user presses "update" (POST /api/update of the old version) pointing at the new build;
3. checks: the update touched no file but the program and its .bak; the new version,
   started on the same home, shows the same settings and games, accepts the old PIN and
   media password, and after it writes state.json every stored key keeps its value.

CI runs this as its own required step, before every release too.

    python -m unittest discover -s tests
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_update import LEGACY_COMMIT, ROOT, Served, build  # noqa: E402

PIN = "4321"
FIRST_PIN = "1111"
MEDIA_PASSWORD = "секрет gallery"
KEEP_TAGS = 3        # how many earlier releases to update from
SETTINGS = {         # every setting away from its default
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
}


def version_key(v):
    return tuple(int(x) for x in re.findall(r"\d+", v))


def current_version():
    text = (ROOT / "src" / "deckdrop" / "__init__.py").read_text("utf-8")
    return re.search(r'__version__ = "([^"]+)"', text).group(1)


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def program_at(ref, workdir):
    """The deckdrop.py a user got from `ref`: built from src/ if it has one, else the single file."""
    tree = workdir / ref
    tree.mkdir(parents=True)
    archive = workdir / (ref + ".tar")
    archive.write_bytes(git("archive", "--format=tar", ref))
    with tarfile.open(archive) as t:
        t.extractall(tree)
    if (tree / "tools" / "build.py").exists():
        subprocess.run([sys.executable, str(tree / "tools" / "build.py"), "-o", str(tree / "built.py")],
                       check=True, capture_output=True)
        return (tree / "built.py").read_bytes()
    return (tree / "deckdrop.py").read_bytes()


def earlier_releases():
    """[(label, git ref)] of the releases users may be updating from, oldest first."""
    try:
        tags = git("tag", "-l", "v*").decode().split()
    except (OSError, subprocess.CalledProcessError):
        return []
    cur = version_key(current_version())
    tags = sorted((t for t in tags if version_key(t) < cur), key=version_key)[-KEEP_TAGS:]
    if tags:
        return [(t, t) for t in tags]
    try:
        git("cat-file", "-e", LEGACY_COMMIT)
    except subprocess.CalledProcessError:
        return []
    return [("0.3.23 (single file)", LEGACY_COMMIT)]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    """A program serving on a free port, the way the systemd service runs it."""

    def __init__(self, program, env):
        self.port = free_port()
        self.log = ""
        self.proc = subprocess.Popen([sys.executable, str(program)], env=dict(env, DECKDROP_PORT=str(self.port)),
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while True:
            try:
                self.get("/api/state")
                return
            except OSError:
                if self.proc.poll() is not None or time.time() > deadline:
                    self.stop()
                    raise AssertionError(f"{program.name} did not start:\n{self.log}")
                time.sleep(0.3)

    def get(self, path):
        return self.call(path)

    def call(self, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def post_ok(self, path, body):
        status, res = self.call(path, body)
        if status != 200:
            raise AssertionError(f"POST {path} -> {status} {res}\n{self.log}")
        return res

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        self.log = self.proc.stdout.read().decode(errors="replace")
        self.proc.stdout.close()


def snapshot(home, program):
    """Content hash of every file in home but the program and its update leftovers."""
    skip = {program, program.with_suffix(".py.bak"), program.with_suffix(".py.new")}
    return {str(p.relative_to(home)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(home.rglob("*"))
            if p.is_file() and p not in skip and "__pycache__" not in p.parts}


@unittest.skipIf(os.environ.get("DECKDROP_SKIP_USER_DATA") == "1", "runs as its own CI step")
class UserDataSurvivesUpdate(unittest.TestCase):
    def test_update_from_earlier_releases(self):
        releases = earlier_releases()
        if not releases:
            if os.environ.get("CI"):
                self.fail("no earlier release to update from: CI needs the full history (fetch-depth: 0)")
            self.skipTest("no earlier release in this checkout")
        work = Path(tempfile.mkdtemp())
        new_build = build.build()
        (work / "serve").mkdir()
        (work / "serve" / "deckdrop.py").write_text(new_build, "utf-8")
        server = Served(work / "serve")
        try:
            for label, ref in releases:
                with self.subTest(update_from=label):
                    self.check(label, program_at(ref, work / "trees"), new_build,
                               server.url + "deckdrop.py", work / ("home-" + re.sub(r"\W", "_", ref)))
        finally:
            server.close()
            shutil.rmtree(work, ignore_errors=True)

    def check(self, label, old_program, new_build, url, home):
        games = home / "Games"
        game, hidden = games / "My Game", games / "Hidden Game"
        imported = home / "Elsewhere" / "Imported Game"
        for d, exe in ((game, "Game.exe"), (hidden, "h.exe"), (imported, "run.exe")):
            d.mkdir(parents=True)
            (d / exe).write_bytes(b"MZ" + b"\0" * 64)
        (game / "save").mkdir()
        (game / "save" / "slot1.sav").write_bytes(os.urandom(256))
        (games / "_inbox").mkdir()
        (games / "_inbox" / "Other.part1.rar").write_bytes(os.urandom(512))
        (home / ".cache" / "deckdrop").mkdir(parents=True)
        (home / ".cache" / "deckdrop" / "thumb.png").write_bytes(os.urandom(64))
        program = home / "deckdrop" / "deckdrop.py"
        program.parent.mkdir()
        (program.parent / "notes.txt").write_text("мои заметки\n", "utf-8")
        program.write_bytes(old_program)
        state_file = home / ".config" / "deckdrop" / "state.json"
        # the service has no DECKDROP_* overrides: everything lives at the default paths in HOME
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(home), DECKDROP_NO_RESTART="1", DECKDROP_CEF="0",
                   DECKDROP_STEAM=str(home / "no-steam"))

        # 1. the user sets up the old version through its web page
        old = Running(program, dict(env, DECKDROP_PIN=FIRST_PIN))
        try:
            old.post_ok("/api/settings/pin", {"old": FIRST_PIN, "new": PIN})
            old.post_ok("/api/settings", dict(SETTINGS, pin=PIN, update_url=url))
            old.post_ok("/api/media/setup", {"password": MEDIA_PASSWORD})
            old.post_ok("/api/game/hide", {"path": str(hidden), "hidden": True})
            status, res = old.call("/api/game/import", {"path": str(imported)})
            # releases up to 0.3.24 used dict | dict here, which needs Python 3.9
            if status != 200 and not (sys.version_info < (3, 9) and "unsupported operand" in str(res)):
                raise AssertionError(f"POST /api/game/import -> {status} {res}")
        finally:
            old.stop()
        state = json.loads(state_file.read_text("utf-8"))
        if str(imported) not in state.setdefault("imported", []):
            state["imported"].append(str(imported))
        exe = str(game / "Game.exe")
        state.update({
            "added": {exe: {"at": 1700000000, "appid": 3123456789, "name": "Моя игра", "compat": "proton_9",
                            "via": "cdp", "art": True, "art_source": "vndb", "vndb": "v17"}},
            "pending": [{"op": "rename", "exe": exe, "name": "Моя игра"}],
            "from_a_newer_version": {"keep": ["me", 1, None]},
        })
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")

        # 2. the user presses "update" in the old version
        old = Running(program, env)
        try:
            before = snapshot(home, program)
            res = old.post_ok("/api/update", {"pin": PIN, "url": url})
            self.assertIn("->", res["note"], f"{label}: did not update: {res}")
        finally:
            old.stop()
        self.assertEqual(program.read_text("utf-8"), new_build, f"{label}: program not replaced")
        stored = json.loads(state_file.read_text("utf-8"))
        self.assertEqual(snapshot(home, program), before,
                         f"{label}: the update changed files other than the program")

        # 3. the new version on the same home
        new = Running(program, env)
        try:
            status, st = new.get("/api/state")
            self.assertEqual(status, 200)
            shown = st["settings"]
            for key, value in SETTINGS.items():
                if key != "proxy":
                    self.assertEqual(shown[key], value, f"{label}: setting {key}")
            self.assertEqual(shown["update_url"], url, f"{label}: own update link")
            self.assertTrue(shown["proxy_set"], f"{label}: proxy lost")
            self.assertNotIn("p%40ss", json.dumps(st), f"{label}: proxy password shown to the page")
            by_name = {g["name"]: g for g in st["games"]}
            self.assertIn("My Game", by_name, f"{label}: game list")
            self.assertTrue(by_name.get("Imported Game", {}).get("imported"), f"{label}: imported game lost")
            self.assertTrue(by_name.get("Hidden Game", {}).get("hidden"), f"{label}: hidden flag lost")

            res = new.post_ok("/api/media/login", {"password": MEDIA_PASSWORD})
            self.assertTrue(res.get("token"), f"{label}: media password")
            status, res = new.call("/api/settings", {"pin": "0000", "proxy": SETTINGS["proxy"]})
            self.assertEqual(status, 403, f"{label}: a wrong PIN must be refused: {res}")
            # a PIN-protected write: the old PIN still works, and state.json is written by the new code
            new.post_ok("/api/settings", {"pin": PIN, "proxy": SETTINGS["proxy"], "vndb_auto": True})
            # an endpoint the new code serves by itself, on the same data
            spare = home / "Elsewhere" / "Second Game"
            spare.mkdir()
            (spare / "g.exe").write_bytes(b"MZ")
            new.post_ok("/api/game/import", {"path": str(spare)})
        finally:
            new.stop()

        saved = json.loads(state_file.read_text("utf-8"))
        want = dict(stored, vndb_auto=True)
        want["imported"] = stored["imported"] + [str(spare)]
        for key, value in want.items():
            self.assertIn(key, saved, f"{label}: {key} dropped from state.json\n{new.log}")
            self.assertEqual(saved[key], value, f"{label}: {key} rewritten in state.json")


if __name__ == "__main__":
    unittest.main()
