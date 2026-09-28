"""tools/layouts_probe.py runs on a Deck-like Steam folder and writes only when asked to.

    python -m unittest discover -s tests
"""
import subprocess
import sys
import tempfile
import unittest
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


class LayoutsProbeTest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.steam = self.home / "Steam"
        cfg = self.steam / "steamapps/common/Steam Controller Configs/123/config"
        (cfg / "some game").mkdir(parents=True)
        (cfg / "some game" / "controller_neptune.vdf").write_text(LAYOUT)
        (cfg / "configset_controller_neptune.vdf").write_text(CONFIGSET)
        (self.steam / "userdata/123/241100/remote").mkdir(parents=True)
        (self.steam / "controller_base/templates").mkdir(parents=True)
        (self.steam / "controller_base/templates/controller_neptune_gamepad+mouse.vdf").write_text(LAYOUT)
        self.report = self.home / "report.txt"

    def run_probe(self, *extra):
        r = subprocess.run([sys.executable, str(PROBE), "--steam", str(self.steam), "--no-cdp",
                            "--report", str(self.report), *extra], capture_output=True, text=True, timeout=60)
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

    def test_template_experiment_and_cleanup(self):
        self.run_probe("--template", "some game")
        probe = self.steam / "controller_base/templates/controller_neptune_deckdrop_probe.vdf"
        self.assertIn('"title"\t\t"DeckDrop probe"', probe.read_text())
        self.assertIn("probe template is in place", self.run_probe())
        self.run_probe("--cleanup")
        self.assertFalse(probe.exists())

    def test_unknown_game(self):
        self.assertIn("pick a folder name", self.run_probe("--template", "nope"))


if __name__ == "__main__":
    unittest.main()
