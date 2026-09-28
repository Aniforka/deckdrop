"""Controller layouts: saving, renaming and deleting change DeckDrop's layout files and nothing else.

A live server runs on a fake home with a Steam folder laid out like a real Deck's (Steam Controller
Configs with a game's current and saved layouts, other games, configsets, Valve's templates). Every
test takes a snapshot of every file in the home before and after, and checks the exact difference:
a game's own layouts, other games, Steam's templates and state.json are never touched.

    python -m unittest discover -s tests
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_user_data import Running  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = "360931465"
APPID = 2514257537


def layout(title, controller="controller_neptune", extra=""):
    return f'''"controller_mappings"
{{
\t"version"\t\t"3"
\t"title"\t\t"{title}"
\t"description"\t\t"#SettingsController_AutosaveDescription"
\t"creator"\t\t"76561198000000000"
\t"controller_type"\t\t"{controller}"
\t"localization"
\t{{
\t\t"english"
\t\t{{
\t\t\t"title"\t\t"WASD"
\t\t\t"description"\t\t"A template"
\t\t}}
\t}}
\t"group"
\t{{
\t\t"id"\t\t"0"
\t\t"mode"\t\t"four_buttons"{extra}
\t}}
}}
'''


def shortcuts_vdf(name, exe, appid):
    """A binary shortcuts.vdf with one non-Steam game."""
    def s(key, val):
        return b"\x01" + key.encode() + b"\0" + val.encode() + b"\0"
    entry = b"\x000\0" + b"\x02appid\0" + appid.to_bytes(4, "little") + s("appname", name) + s("exe", f'"{exe}"') + b"\x08"
    return b"\x00shortcuts\0" + entry + b"\x08\x08"


def snapshot(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def diff(before, after):
    return {"added": sorted(set(after) - set(before)), "removed": sorted(set(before) - set(after)),
            "changed": sorted(k for k in set(before) & set(after) if before[k] != after[k])}


class LayoutsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        h = cls.home
        cls.game = h / "Games" / "Yosuga"
        cls.game.mkdir(parents=True)
        cls.exe = cls.game / "Yosuga.exe"
        cls.exe.write_bytes(b"MZ" + b"\0" * 64)
        cls.steam = h / "Steam"
        cfg = cls.steam / "steamapps/common/Steam Controller Configs" / ACCOUNT / "config"
        cls.cfg = cfg
        # Steam named the folder after the shortcut's first name ("Yosuga.exe"), renamed since
        (cfg / "yosugaexe").mkdir(parents=True)
        (cfg / "yosugaexe" / "FYZZ53700874.vdf").write_text(layout("#Title"), "utf-8")
        (cfg / "yosugaexe" / "vns_0.vdf").write_text("﻿" + layout("VNs"), "utf-8")
        (cfg / "yosugaexe" / "touch_0.vdf").write_text(layout("Touch", "controller_mobile_touch"), "utf-8")
        (cfg / "yosugaexe" / "broken_0.vdf").write_text('"controller_mappings" { "title" "half', "utf-8")
        (cfg / "fatestay night").mkdir()
        (cfg / "fatestay night" / "vns 2.0_0.vdf").write_text(layout("VNs 2.0"), "utf-8")
        (cfg / "configset_controller_neptune.vdf").write_text('"controller_config"\n{\n\t"yosugaexe"\n\t{\n'
                                                              '\t\t"autosave"\t\t"1"\n\t}\n}\n', "utf-8")
        cls.templates = cls.steam / "controller_base" / "templates"
        cls.templates.mkdir(parents=True)
        (cls.templates / "controller_neptune_wasd.vdf").write_text(layout("#Title"), "utf-8")
        (cls.templates / "controller_neptune_deckdrop_probe.vdf").write_text(layout("DeckDrop probe"), "utf-8")
        ud = cls.steam / "userdata" / ACCOUNT / "config"
        ud.mkdir(parents=True)
        (ud / "shortcuts.vdf").write_bytes(shortcuts_vdf("Yosuga no Sora", cls.exe, APPID))
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(h), DECKDROP_CEF="0", DECKDROP_PIN="1234", DECKDROP_STEAM=str(cls.steam),
                   DECKDROP_GAMES=str(h / "Games"))
        cls.app = Running(ROOT / "tools" / "dev.py", env)
        cls.layouts = h / ".config" / "deckdrop" / "layouts"
        cls.state = h / ".config" / "deckdrop" / "state.json"

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()
        shutil.rmtree(cls.home, ignore_errors=True)

    def setUp(self):
        # every test starts with no saved layouts; nothing else is reset between tests
        if self.layouts.exists():
            for p in self.layouts.iterdir():
                p.unlink()
        for p in self.templates.glob("controller_neptune_deckdrop_*.vdf"):
            if p.name != "controller_neptune_deckdrop_probe.vdf":
                p.unlink()

    # ---- helpers
    def q(self):
        return f"game={urllib.parse.quote(str(self.game))}&exe=Yosuga.exe"

    def sources(self):
        status, res = self.app.get("/api/layouts/sources?" + self.q())
        self.assertEqual(status, 200, res)
        return res["sources"]

    def save(self, src, name):
        return self.app.call("/api/layouts/save", {"game": str(self.game), "exe": "Yosuga.exe", "src": src, "name": name})

    def src(self, kind="current"):
        return next(s["src"] for s in self.sources() if s["kind"] == kind)

    def listed(self):
        status, res = self.app.get("/api/layouts")
        self.assertEqual(status, 200, res)
        return res

    def put(self, path, data):
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", data=data, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def files_of(self, lid):
        return [f".config/deckdrop/layouts/{lid}.json", f".config/deckdrop/layouts/{lid}.vdf",
                f"Steam/controller_base/templates/controller_neptune_deckdrop_{lid}.vdf"]

    # ---- what Steam has for the game
    def test_sources_of_a_renamed_shortcut(self):
        got = {(s["kind"], s["title"], s["base"]) for s in self.sources()}
        self.assertEqual(got, {("current", "", "WASD"), ("saved", "VNs", "")})
        self.assertTrue(all(s["src"].startswith(ACCOUNT + "/yosugaexe/") for s in self.sources()))

    # ---- save
    def test_save_adds_only_its_own_files(self):
        before = snapshot(self.home)
        status, res = self.save(self.src(), "Для новелл")
        self.assertEqual(status, 200, res)
        self.assertTrue(res["template"])
        self.assertEqual(diff(before, snapshot(self.home)),
                         {"added": sorted(self.files_of(res["id"])), "removed": [], "changed": []})
        stored = (self.layouts / f"{res['id']}.vdf").read_bytes()
        self.assertEqual(stored, (self.cfg / "yosugaexe" / "FYZZ53700874.vdf").read_bytes(), "an exact copy")
        tpl = (self.templates / f"controller_neptune_deckdrop_{res['id']}.vdf").read_text("utf-8")
        self.assertIn('"title"\t\t"DeckDrop: Для новелл"', tpl)
        self.assertIn('"title"\t\t"WASD"', tpl, "only the layout's own title changes")
        meta = json.loads((self.layouts / f"{res['id']}.json").read_text("utf-8"))
        self.assertEqual((meta["name"], meta["game"]), ("Для новелл", "Yosuga no Sora"))

    def test_saved_layout_with_a_bom(self):
        status, res = self.save(self.src("saved"), "VNs")
        self.assertEqual(status, 200, res)
        self.assertEqual([x["name"] for x in self.listed()["layouts"]], ["VNs"])

    def test_same_name_twice_keeps_both(self):
        _, a = self.save(self.src(), "Same")
        first = snapshot(self.layouts)
        _, b = self.save(self.src("saved"), "Same")
        self.assertNotEqual(a["id"], b["id"])
        after = snapshot(self.layouts)
        for name, digest in first.items():
            self.assertEqual(after[name], digest, "the first layout was rewritten")
        self.assertEqual(sorted(x["id"] for x in self.listed()["layouts"]), sorted([a["id"], b["id"]]))

    def test_save_refuses_files_that_are_not_the_games_sources(self):
        before = snapshot(self.home)
        bad = [f"{ACCOUNT}/fatestay night/vns 2.0_0.vdf",           # another game's layout
               f"{ACCOUNT}/yosugaexe/touch_0.vdf",                   # not a Deck layout
               f"{ACCOUNT}/yosugaexe/broken_0.vdf",
               f"{ACCOUNT}/yosugaexe/../../../../../.config/deckdrop/state.json",
               "../../state.json", "", "a/b"]
        for src in bad:
            status, _ = self.save(src, "x")
            self.assertEqual(status, 400, src)
        status, _ = self.save(self.src(), "   ")
        self.assertEqual(status, 400, "an empty name")
        self.assertEqual(diff(before, snapshot(self.home)), {"added": [], "removed": [], "changed": []})

    # ---- rename
    def test_rename_changes_only_its_record_and_template(self):
        _, a = self.save(self.src(), "Old")
        _, b = self.save(self.src("saved"), "Other")
        before = snapshot(self.home)
        status, res = self.app.call("/api/layouts/rename", {"id": a["id"], "name": 'New "one"'})
        self.assertEqual(status, 200, res)
        f = self.files_of(a["id"])
        self.assertEqual(diff(before, snapshot(self.home)), {"added": [], "removed": [], "changed": sorted([f[0], f[2]])})
        tpl = (self.templates / f"controller_neptune_deckdrop_{a['id']}.vdf").read_text("utf-8")
        self.assertIn('"title"\t\t"DeckDrop: New \\"one\\""', tpl)
        self.assertEqual({x["id"]: x["name"] for x in self.listed()["layouts"]}, {a["id"]: 'New "one"', b["id"]: "Other"})

    # ---- delete
    def test_delete_removes_only_its_files(self):
        _, a = self.save(self.src(), "A")
        _, b = self.save(self.src("saved"), "B")
        before = snapshot(self.home)
        status, res = self.app.call("/api/layouts/delete", {"id": a["id"]})
        self.assertEqual(status, 200, res)
        self.assertEqual(diff(before, snapshot(self.home)),
                         {"added": [], "removed": sorted(self.files_of(a["id"])), "changed": []})
        self.assertEqual([x["id"] for x in self.listed()["layouts"]], [b["id"]])

    def test_bad_ids_touch_nothing(self):
        self.save(self.src(), "A")
        before = snapshot(self.home)
        for lid in ("../state", "state", "", "0000000g", "../../Steam/controller_base/templates/x", "deadbeef"):
            for path, body in (("/api/layouts/delete", {"id": lid}), ("/api/layouts/rename", {"id": lid, "name": "x"})):
                status, _ = self.app.call(path, body)
                self.assertEqual(status, 400, (path, lid))
        status, _ = self.app.get("/api/layouts/file?id=../state")
        self.assertEqual(status, 400)
        self.assertEqual(diff(before, snapshot(self.home)), {"added": [], "removed": [], "changed": []})

    # ---- Steam's templates follow DeckDrop
    def test_templates_come_back_and_strays_go(self):
        _, a = self.save(self.src(), "A")
        mine = self.templates / f"controller_neptune_deckdrop_{a['id']}.vdf"
        want = mine.read_bytes()
        mine.unlink()                                                         # a Steam update wiped it
        stray = self.templates / "controller_neptune_deckdrop_0badc0de.vdf"   # its layout was removed by hand
        stray.write_text(layout("DeckDrop: gone"), "utf-8")
        valve = snapshot(self.templates)
        del valve[stray.name]
        self.assertTrue(self.listed()["templates"])
        self.assertEqual(mine.read_bytes(), want)
        self.assertFalse(stray.exists())
        now = snapshot(self.templates)
        for name, digest in valve.items():
            self.assertEqual(now.get(name), digest, f"{name} is not DeckDrop's and must stay as it was")

    def test_a_broken_record_does_not_break_the_list(self):
        _, a = self.save(self.src(), "A")
        (self.layouts / "1234abcd.json").write_text("{not json", "utf-8")
        (self.layouts / "1234abcd.vdf").write_text(layout("x"), "utf-8")
        (self.layouts / "notes.txt").write_text("мои заметки", "utf-8")
        self.assertEqual([x["id"] for x in self.listed()["layouts"]], [a["id"]])
        self.assertTrue((self.layouts / "notes.txt").exists())

    # ---- files in and out
    def test_download_and_upload(self):
        _, a = self.save(self.src(), "Моя раскладка")
        req = urllib.request.urlopen(f"http://127.0.0.1:{self.app.port}/api/layouts/file?id={a['id']}", timeout=30)
        data = req.read()
        self.assertIn("attachment", req.headers["Content-Disposition"])
        self.assertIn('"title"\t\t"Моя раскладка"', data.decode("utf-8"))
        before = snapshot(self.home)
        status, b = self.put("/api/layouts/upload?name=" + urllib.parse.quote("../из файла"), data)
        self.assertEqual(status, 200, b)
        self.assertEqual(diff(before, snapshot(self.home)), {"added": sorted(self.files_of(b["id"])), "removed": [], "changed": []})
        self.assertIn("../из файла", [x["name"] for x in self.listed()["layouts"]])
        before = snapshot(self.home)
        for body in (b"not a layout", layout("Touch", "controller_mobile_touch").encode(), b"\0" * ((1 << 20) + 1)):
            status, _ = self.put("/api/layouts/upload?name=x", body)
            self.assertEqual(status, 400)
        self.assertEqual(diff(before, snapshot(self.home)), {"added": [], "removed": [], "changed": []})


class NoSteamTemplatesTest(unittest.TestCase):
    """Without Steam's templates folder a layout is still kept, and nothing is created in Steam."""

    def test_saved_without_templates(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        steam = home / "Steam"
        (steam / "userdata").mkdir(parents=True)
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(home), DECKDROP_CEF="0", DECKDROP_STEAM=str(steam))
        app = Running(ROOT / "tools" / "dev.py", env)
        self.addCleanup(app.stop)
        before = snapshot(steam)
        req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/layouts/upload?name=A",
                                     data=layout("A").encode(), method="PUT")
        with urllib.request.urlopen(req, timeout=30) as r:
            res = json.loads(r.read())
        self.assertFalse(res["template"])
        status, listed = app.get("/api/layouts")
        self.assertFalse(listed["templates"])
        self.assertEqual([x["name"] for x in listed["layouts"]], ["A"])
        self.assertEqual(snapshot(steam), before)


if __name__ == "__main__":
    unittest.main()
