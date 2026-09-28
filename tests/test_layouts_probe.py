"""tools/layouts_probe.py runs on a Deck-like Steam folder and writes only when asked to.

    python -m unittest discover -s tests
"""
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tools" / "layouts_probe.py"

LAYOUT = """"controller_mappings"
{
\t"version"\t\t"3"
\t"title"\t\t"My \\"cool\\" layout"
\t"controller_type"\t\t"controller_neptune"
\t"group"
\t{
\t\t"id"\t\t"0"
\t\t"mode"\t\t"four_buttons"
\t}
\t"group"
\t{
\t\t"id"\t\t"1"
\t\t"mode"\t\t"dpad"
\t}
}
"""
CONFIGSET = """"controller_config"
{
\t"some game"
\t{
\t\t"template"\t\t"controller_neptune_gamepad+mouse.vdf"
\t}
}
"""


def shortcuts_vdf(name, appid):
    """A binary shortcuts.vdf with one non-Steam game."""
    def s(key, val):
        return b"\x01" + key.encode() + b"\0" + val.encode() + b"\0"
    entry = (b"\x00" + b"0\0" + b"\x02appid\0" + appid.to_bytes(4, "little")
             + s("appname", name) + s("exe", '"/games/y/Yosuga.exe"') + b"\x08")
    return b"\x00shortcuts\0" + entry + b"\x08\x08"


class LayoutsProbeTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.steam = self.home / "Steam"
        cfg = self.steam / "steamapps/common/Steam Controller Configs/123/config"
        (cfg / "some game").mkdir(parents=True)
        (cfg / "some game" / "controller_neptune.vdf").write_text(LAYOUT)
        (cfg / "some game" / "my pad_0.vdf").write_text("\ufeff" + LAYOUT)     # a saved layout, with a BOM
        (cfg / "1234").mkdir()
        (cfg / "yosugaexe").mkdir()
        (self.steam / "userdata/123/config").mkdir(parents=True)
        (self.steam / "userdata/123/config/shortcuts.vdf").write_bytes(shortcuts_vdf("Yosuga.exe", 3000000001))
        (cfg / "configset_controller_neptune.vdf").write_text(CONFIGSET)
        (self.steam / "userdata/123/241100/remote").mkdir(parents=True)
        (self.steam / "controller_base/templates").mkdir(parents=True)
        (self.steam / "controller_base/templates/controller_neptune_gamepad+mouse.vdf").write_text(LAYOUT)
        self.report = self.home / "report.txt"

    def run_probe(self, *extra):
        env = dict(os.environ, HOME=str(self.home), DECKDROP_STEAM=str(self.steam))
        r = subprocess.run([sys.executable, str(PROBE), "--steam", str(self.steam), "--no-cdp", "--no-serve",
                            "--report", str(self.report), *extra], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return self.report.read_text("utf-8")

    def files(self):
        return sorted(str(p.relative_to(self.steam)) for p in self.steam.rglob("*"))

    def test_report_describes_layouts_and_changes_nothing(self):
        before = self.files()
        text = self.run_probe()
        self.assertIn("some game/", text)
        self.assertIn('"title": "My \\"cool\\" layout"', text)      # parsed, escaped quotes kept
        self.assertIn("'group': 2", text)                             # repeated blocks counted
        self.assertIn('"template"\t\t"controller_neptune_gamepad+mouse.vdf"', text)
        self.assertIn("title='My \"cool\" layout'", text)
        self.assertEqual(self.files(), before)

    def test_non_steam_games_and_their_folders(self):
        self.assertIn("'Yosuga.exe'  appid=3000000001  folder='yosugaexe' (exists)", self.run_probe())

    def test_copy_experiment_and_cleanup(self):
        before = self.files()
        text = self.run_probe("--copy", "some game", "1234")
        self.assertIn("my pad_0.vdf", text.split("experiment:")[1])       # a saved layout is preferred
        template = self.steam / "controller_base/templates/controller_neptune_deckdrop_probe.vdf"
        personal = self.steam / "steamapps/common/Steam Controller Configs/123/config/1234/deckdrop probe_0.vdf"
        for p in (template, personal):
            body = p.read_text("utf-8")
            self.assertFalse(body.startswith("\ufeff"))
            self.assertIn('"title"\t\t"DeckDrop probe"', body)
            self.assertEqual(body.count("DeckDrop probe"), 1)
        self.assertIn("probe template is in place", self.run_probe())
        self.assertIn("title='DeckDrop probe'", self.run_probe())
        self.run_probe("--cleanup")
        self.assertEqual(self.files(), before)

    def test_report_is_shared_and_nothing_else(self):
        p = subprocess.Popen([sys.executable, "-u", str(PROBE), "--steam", str(self.steam), "--no-cdp",
                              "--report", str(self.report), "--serve-port", "0"],
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        self.addCleanup(p.stdout.close)
        for line in p.stdout:
            if "/download" in line:
                url = line.split()[0].rsplit("/", 1)[0]
                break
        url = url.replace(url.split("//")[1].split(":")[0], "127.0.0.1")
        report = self.report.read_bytes()
        for path in ("/", "/download", "/../../etc/passwd"):
            with urllib.request.urlopen(url + path, timeout=10) as r:
                self.assertEqual(r.read(), report)
        with urllib.request.urlopen(url + "/download", timeout=10) as r:
            self.assertIn("attachment", r.headers["Content-Disposition"])

    def test_unknown_game(self):
        self.assertIn("pick folder names", self.run_probe("--copy", "some game", "nope"))


if __name__ == "__main__":
    unittest.main()
