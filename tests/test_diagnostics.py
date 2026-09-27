"""Settings -> Performance: the self-check finds what is broken, the measurement cleans up after itself.

    python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_user_data import Running  # noqa: E402

STATUSES = {"ok", "warn", "fail", "info"}
UNITS = {"text", "bytes", "mbps", "ms", "pct", "duration", "watts"}
SECTIONS = 5


class DiagnosticsTest(unittest.TestCase):
    """A running DeckDrop with a few planted problems."""

    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        games = cls.home / "Games"
        (games / "Cool Game").mkdir(parents=True)
        (games / "Cool Game" / "Game.exe").write_bytes(b"MZ")
        (games / "_inbox").mkdir()
        (games / "_inbox" / "big.zip.part").write_bytes(b"x" * 4096)      # a forgotten half-download
        state = {"admin_pin": "1234",
                 # a shortcut DeckDrop made for a game whose files are gone
                 "added": {str(games / "Gone Game" / "Gone.exe"): {"appid": 123, "name": "Gone"}},
                 # a Steam change that keeps failing
                 "pending": [{"op": "rename", "exe": "x", "name": "y", "tries": 7, "error": "Steam JS: boom"}]}
        (cls.home / "state.json").write_text(json.dumps(state), "utf-8")
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(cls.home), USERPROFILE=str(cls.home), DECKDROP_CEF="0",
                   DECKDROP_STATE=str(cls.home / "state.json"), DECKDROP_GAMES=str(games),
                   DECKDROP_STEAM=str(cls.home / "no-steam"))
        cls.games = games
        cls.app = Running(ROOT / "tools" / "dev.py", env)

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()

    def check(self):
        return self.app.post_ok("/api/perf/check", {})

    def by_title(self, res, start):
        found = [i for i in res["items"] if i["title"].startswith(start)]
        self.assertTrue(found, f"no check titled {start!r}: {[i['title'] for i in res['items']]}")
        return found[0]

    def test_self_check_shape(self):
        res = self.check()
        self.assertTrue(res["items"])
        for i in res["items"]:
            self.assertIn(i["status"], STATUSES, i)
            self.assertTrue(i["title"].strip(), i)
        self.assertEqual(sum(res["counts"].values()), len(res["items"]))

    def test_self_check_finds_planted_problems(self):
        res = self.check()
        self.assertNotEqual(self.by_title(res, "Leftovers")["status"], "ok")
        self.assertIn("1", self.by_title(res, "Leftovers")["detail"])
        games = self.by_title(res, "Games on disk")
        self.assertEqual(games["status"], "warn")
        self.assertIn("Gone.exe", games["detail"])
        pending = self.by_title(res, "Queued Steam changes")
        self.assertEqual(pending["status"], "warn")
        self.assertIn("boom", pending["detail"])
        self.assertEqual(self.by_title(res, "Steam")["status"], "warn")          # no Steam folder here

    def test_self_check_healthy_parts(self):
        res = self.check()
        self.assertEqual(self.by_title(res, "Background tasks")["status"], "ok")
        self.assertEqual(self.by_title(res, "Settings file")["status"], "ok")
        self.assertEqual(self.by_title(res, "Disk: ")["status"], "ok")
        self.assertEqual(self.by_title(res, "Log")["status"], "ok")
        self.assertEqual(self.by_title(res, "Autostart")["status"], "info")     # started by hand, not by systemd

    def test_self_check_is_quick(self):
        t0 = time.time()
        self.check()
        self.assertLess(time.time() - t0, 25, "the checks should run side by side")

    def test_benchmark(self):
        res = self.app.post_ok("/api/perf/bench", {})
        self.assertEqual(len(res["sections"]), SECTIONS)
        for s in res["sections"]:
            self.assertTrue(s["title"].strip())
            self.assertTrue(s["rows"], s["title"])
            for r in s["rows"]:
                self.assertIn(r["unit"], UNITS, r)
                self.assertTrue(r["label"].strip(), r)
        rows = {r["label"]: r for s in res["sections"] for r in s["rows"]}
        self.assertGreater(rows["Mega decryption"]["value"], 0)
        self.assertGreater(rows["Unpacking zip"]["value"], 0)
        self.assertGreater(rows["Writing: Internal"]["value"], 0)
        self.assertGreaterEqual(rows["While the page is open"]["value"], 0)
        # nothing left behind: no test file on the disk, no work folder in the cache
        self.assertEqual([p.name for p in self.games.iterdir() if p.name.startswith(".deckdrop-")], [])
        cache = Path.home() / ".cache" / "deckdrop"
        if cache.is_dir():
            self.assertEqual([p.name for p in cache.iterdir() if p.name.startswith("bench-")], [])

    def download(self, kind):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.app.port}/api/perf/report?kind={kind}", timeout=20) as r:
                return r.status, dict(r.headers), r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read().decode("utf-8")

    def test_report_download(self):
        self.check()
        status, headers, text = self.download("check")
        self.assertEqual(status, 200)
        self.assertIn("text/markdown", headers["Content-Type"])
        self.assertRegex(headers["Content-Disposition"], r'attachment; filename="deckdrop-selfcheck-\d{8}-\d{4}\.md"')
        self.assertTrue(text.startswith("# DeckDrop self-check\n"), text[:80])
        self.assertIn("**Games on disk**", text)
        self.assertNotIn("{{", text)
        self.assertNotIn("<!--", text, "the template's own comment stays out of the report")

        self.app.post_ok("/api/perf/bench", {})
        status, headers, text = self.download("bench")
        self.assertEqual(status, 200)
        self.assertIn("deckdrop-load-", headers["Content-Disposition"])
        self.assertIn("| Metric | Value | Note |", text)
        self.assertRegex(text, r"\| Mega decryption \| \*\*[\d.]+ MB/s\*\* \|")
        self.assertNotIn("{{", text)

    def test_report_bad_kind(self):
        status, _, _ = self.download("nope")
        self.assertEqual(status, 400)

    def test_one_benchmark_at_a_time(self):
        first = {}
        th = threading.Thread(target=lambda: first.setdefault("res", self.app.call("/api/perf/bench", {})))
        th.start()
        time.sleep(1)                                   # the first one is measuring the idle load now
        status, res = self.app.call("/api/perf/bench", {})
        th.join()
        self.assertEqual(first["res"][0], 200)
        self.assertEqual(status, 400)
        self.assertIn("already running", res["error"])


class UnitTest(unittest.TestCase):
    def test_duration(self):
        from deckdrop import diagnostics, i18n
        i18n.set_current("en")
        self.assertEqual(diagnostics.duration(59), "0 min")
        self.assertEqual(diagnostics.duration(125), "2 min")
        self.assertEqual(diagnostics.duration(3 * 3600 + 7 * 60), "3 h 7 min")

    def test_read_kv(self):
        from deckdrop import diagnostics
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "meminfo"
            p.write_text("MemTotal:       16000000 kB\nMemAvailable:    8000000 kB\n", "utf-8")
            kv = diagnostics.read_kv(p)
            self.assertEqual(diagnostics.kb_value(kv["MemTotal"]), 16000000 * 1024)
            p.write_text('PRETTY_NAME="SteamOS"\nVERSION_ID=3.7\n', "utf-8")
            self.assertEqual(diagnostics.read_kv(p, "=")["VERSION_ID"], "3.7")

    def test_report_before_any_run(self):
        from deckdrop import diagnostics
        diagnostics.LAST.clear()
        with self.assertRaises(FileNotFoundError):
            diagnostics.report_md("bench")

    def test_cell_keeps_the_table_intact(self):
        from deckdrop import diagnostics
        self.assertEqual(diagnostics.cell("a|b\nc"), "a\\|b c")

    def test_battery_off_a_handheld_is_none_or_numbers(self):
        from deckdrop import diagnostics
        b = diagnostics.battery()
        if b is not None:
            watts, status, _ = b
            self.assertGreaterEqual(watts, 0)
            self.assertTrue(status)


if __name__ == "__main__":
    unittest.main()
