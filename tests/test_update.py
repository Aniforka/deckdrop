"""The update path end to end: an installed copy fetches a newer build over HTTP and replaces itself.

    python -m unittest discover -s tests
"""
import functools
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import build  # noqa: E402

# the last single-file release, installed before the package layout; see update_from_legacy
LEGACY_COMMIT = "68218c7"

UPDATE = """
import runpy, sys
g = runpy.run_path(sys.argv[1], run_name="installed")
update = g.get("self_update") or __import__("deckdrop.update", fromlist=["x"]).self_update
print(update(sys.argv[2])[0])   # (note, port) up to 0.3.24, (note, port, updated) since
"""


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


class Served:
    """Serve a directory over HTTP on a free local port."""

    def __init__(self, directory):
        handler = functools.partial(Quiet, directory=str(directory))
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class UpdateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.env = dict(os.environ, HOME=str(self.tmp), DECKDROP_NO_RESTART="1", DECKDROP_CEF="0",
                        DECKDROP_STATE=str(self.tmp / "state.json"))
        (self.tmp / "serve").mkdir()
        self.newer = build.build().replace(f'__version__ = "{self.version()}"', '__version__ = "99.0.0"')
        (self.tmp / "serve" / "deckdrop.py").write_text(self.newer, "utf-8")
        self.server = Served(self.tmp / "serve")

    def tearDown(self):
        self.server.close()

    @staticmethod
    def version():
        text = (ROOT / "src" / "deckdrop" / "__init__.py").read_text("utf-8")
        return text.split('__version__ = "', 1)[1].split('"', 1)[0]

    def update(self, installed):
        res = subprocess.run([sys.executable, "-c", UPDATE, str(installed), self.server.url + "deckdrop.py"],
                             env=self.env, capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def test_built_copy_updates_itself(self):
        installed = self.tmp / "deckdrop" / "deckdrop.py"
        installed.parent.mkdir()
        old = build.build()
        installed.write_text(old, "utf-8")
        note = self.update(installed)
        self.assertIn("99.0.0", note)
        self.assertEqual(installed.read_text("utf-8"), self.newer)
        self.assertEqual(installed.with_suffix(".py.bak").read_text("utf-8"), old)

    def test_update_from_legacy_single_file(self):
        """0.3.23 (one hand-written file, updating from main) must accept the built file."""
        res = subprocess.run(["git", "show", f"{LEGACY_COMMIT}:deckdrop.py"], cwd=ROOT, capture_output=True)
        if res.returncode != 0:
            self.skipTest("legacy commit is not in this checkout (shallow clone)")
        installed = self.tmp / "deckdrop" / "deckdrop.py"
        installed.parent.mkdir()
        installed.write_bytes(res.stdout)
        legacy_url = "https://raw.githubusercontent.com/aniforka/deckdrop/main/deckdrop.py"
        (self.tmp / "state.json").write_text(json.dumps({"update_url": legacy_url}), "utf-8")
        self.assertIn("99.0.0", self.update(installed))
        self.assertEqual(installed.read_text("utf-8"), self.newer)
        # the new build moves the remembered update link from main to the releases
        check = ("import runpy, sys; runpy.run_path(sys.argv[1], run_name='installed');"
                 "from deckdrop import state; state.load_state(); print(state.STATE['update_url'])")
        res = subprocess.run([sys.executable, "-c", check, str(installed)], env=self.env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.stdout.strip(),
                         "https://github.com/Aniforka/deckdrop/releases/latest/download/deckdrop.py", res.stderr)


if __name__ == "__main__":
    unittest.main()
