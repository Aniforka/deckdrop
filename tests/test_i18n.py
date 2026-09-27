"""Interface languages: complete catalogs, no stray text in the code, the right language per request.

    python -m unittest discover -s tests
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "deckdrop"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from deckdrop import i18n  # noqa: E402
from deckdrop.web.page import render  # noqa: E402

PLURAL_FORMS = {"ru": {"one", "few", "many"}, "en": {"one", "other"}}
CYRILLIC = re.compile("[Ѐ-ӿ]")
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def catalogs():
    return {lang: i18n.catalog(lang) for lang in i18n.available()}


def placeholders(value):
    texts = value.values() if isinstance(value, dict) else [value]
    return set().union(*(PLACEHOLDER.findall(v) for v in texts))


def keys_used_in_code():
    """Every catalog key the code asks for, with where it was found."""
    used = {}

    def found(key, where):
        used.setdefault(key, where)

    for p in SRC.rglob("*.py"):
        text = p.read_text("utf-8")
        for key in re.findall(r"""\btr\(\s*["']([\w.]+)["']""", text):
            found(key, p.name)
        # message keys kept in tables (MEGA_ERR, SOCKS_ERR): string values shaped like a key
        for key in re.findall(r""":\s*["']((?:mega\.err|socks)\.[\w.]+)["']""", text):
            found(key, p.name)
    js = (SRC / "web" / "app.js").read_text("utf-8")
    for key in re.findall(r"""\bt\(\s*['"]([\w.]+)['"]""", js):
        if not key.endswith("."):                      # t('status.'+x) builds the key: see test_no_unused_keys
            found(key, "app.js")
    for key in re.findall(r"\{\{([\w.]+)\}\}", (SRC / "web" / "index.html").read_text("utf-8")):
        if key != "lang":
            found(key, "index.html")
    found("lang.name", "web/page.py")                  # the language picker lists every catalog by it
    return used


class CatalogTest(unittest.TestCase):
    def test_languages(self):
        self.assertIn("en", i18n.available())
        self.assertIn("ru", i18n.available())

    def test_same_keys_everywhere(self):
        cats = catalogs()
        en = set(cats["en"])
        for lang, cat in cats.items():
            self.assertEqual(sorted(set(cat) - en), [], f"{lang}.json has keys en.json lacks")
            self.assertEqual(sorted(en - set(cat)), [], f"{lang}.json is missing keys")

    def test_placeholders_match(self):
        cats = catalogs()
        for key, value in cats["en"].items():
            for lang, cat in cats.items():
                self.assertEqual(placeholders(cat[key]), placeholders(value), f"{lang}: {key}")

    def test_plural_forms(self):
        for lang, cat in catalogs().items():
            for key, value in cat.items():
                if isinstance(value, dict):
                    self.assertEqual(set(value), PLURAL_FORMS[lang], f"{lang}: {key}")
                    self.assertIn("n", placeholders(value), f"{lang}: {key} needs {{n}}")

    def test_no_empty_texts(self):
        for lang, cat in catalogs().items():
            for key, value in cat.items():
                for text in (value.values() if isinstance(value, dict) else [value]):
                    self.assertTrue(text.strip(), f"{lang}: {key} is empty")

    def test_markup_only_in_html_keys(self):
        for lang, cat in catalogs().items():
            for key, value in cat.items():
                if not key.endswith("_html") and isinstance(value, str):
                    self.assertNotRegex(value, r"<\w", f"{lang}: {key} has markup but no _html suffix")

    def test_english_catalog_is_english(self):
        for key, value in i18n.catalog("en").items():
            if key != "lang.name":
                self.assertNotRegex(str(value), CYRILLIC, f"en: {key}")

    def test_every_used_key_exists(self):
        en = i18n.catalog("en")
        missing = {k: w for k, w in keys_used_in_code().items() if k not in en}
        self.assertEqual(missing, {}, "keys used in the code but missing from the catalogs")

    def test_no_unused_keys(self):
        used = keys_used_in_code()
        dynamic = ("status.",)                         # app.js: t('status.'+job.status)
        unused = sorted(k for k in i18n.catalog("en") if k not in used and not k.startswith(dynamic))
        self.assertEqual(unused, [], "keys nobody uses: delete them or use them")

    def test_every_job_status_has_a_text(self):
        from deckdrop import jobs
        statuses = set(jobs.ACTIVE) | {"cancelled", "done", "error", "needs_password"}
        for s in statuses:
            self.assertIn("status." + s, i18n.catalog("en"))


class NoStrayTextTest(unittest.TestCase):
    def test_no_cyrillic_outside_catalogs(self):
        """Everything a user reads comes from i18n/<lang>.json, so no language is left half done."""
        found = []
        for p in sorted(SRC.rglob("*")):
            if p.suffix in (".py", ".js", ".html", ".css") and "__pycache__" not in p.parts:
                for i, line in enumerate(p.read_text("utf-8").splitlines(), 1):
                    if CYRILLIC.search(line):
                        found.append(f"{p.relative_to(SRC)}:{i}: {line.strip()[:80]}")
        self.assertEqual(found, [], "text belongs in src/deckdrop/i18n/<lang>.json")


class TranslateTest(unittest.TestCase):
    def test_fill_and_fallback(self):
        self.assertEqual(i18n.t("toast.downloading", "en", name="x.zip"), "Downloading: x.zip")
        self.assertEqual(i18n.t("toast.downloading", "ru", name="x.zip"), "Скачиваю: x.zip")
        self.assertEqual(i18n.t("no.such.key", "ru"), "no.such.key")
        self.assertEqual(i18n.t("toast.downloading", "xx", name="a"), "Downloading: a")   # unknown: English

    def test_plural_rules(self):
        self.assertEqual([i18n.plural_form("ru", n) for n in (1, 2, 5, 11, 21, 22, 25, 111, 0)],
                         ["one", "few", "many", "many", "one", "few", "many", "many", "many"])
        self.assertEqual([i18n.plural_form("en", n) for n in (1, 0, 2)], ["one", "other", "other"])

    def test_threads_keep_the_language(self):
        import threading
        out = {}
        i18n.set_current("ru")
        worker = i18n.carry(lambda: out.setdefault("lang", i18n.current()))
        i18n.set_current("en")
        th = threading.Thread(target=worker)
        th.start()
        th.join()
        self.assertEqual(out["lang"], "ru")


class DetectTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        i18n._steam.update(at=0.0, lang=None)

    def tearDown(self):
        os.environ["HOME"] = self.old_home or ""
        i18n._steam.update(at=0.0, lang=None)

    def steam(self, language):
        d = Path(self.home) / ".steam"
        d.mkdir(exist_ok=True)
        (d / "registry.vdf").write_text('"Registry"\n{\n "HKCU"\n {\n  "Software"\n  {\n   "Valve"\n   {\n'
                                        f'    "Steam"\n    {{\n     "language"\t\t"{language}"\n    }}\n'
                                        "   }\n  }\n }\n}\n", "utf-8")
        i18n._steam.update(at=0.0, lang=None)

    def test_order(self):
        self.steam("russian")
        both = {"Cookie": "x=1; deckdrop_lang=en", "Accept-Language": "ru-RU,ru;q=0.9"}
        self.assertEqual(i18n.for_request(both), ("en", "choice"))
        self.assertEqual(i18n.for_request({"Accept-Language": "en-US,en;q=0.9"}), ("en", "browser"))
        self.assertEqual(i18n.for_request({"Accept-Language": "de-DE"}), ("ru", "steam"))
        self.assertEqual(i18n.for_request({}), ("ru", "steam"))

    def test_nothing_known_is_english(self):
        self.assertEqual(i18n.for_request({"Accept-Language": "ja"}), ("en", "default"))

    def test_steam_language_names(self):
        self.steam("english")
        self.assertEqual(i18n.steam_language(), "en")
        self.steam("schinese")                        # no catalog for it (yet): not picked
        self.assertIsNone(i18n.steam_language())

    def test_accept_language_weights(self):
        self.assertEqual(i18n.from_accept_language("de-DE,de;q=0.9,ru;q=0.8,en;q=0.7"), "ru")
        self.assertEqual(i18n.from_accept_language("en;q=0.5, ru;q=0.6"), "ru")
        self.assertEqual(i18n.from_accept_language("ru;q=0, en"), "en")
        self.assertIsNone(i18n.from_accept_language("*"))

    def test_bad_cookie_is_ignored(self):
        self.assertEqual(i18n.for_request({"Cookie": "deckdrop_lang=xx", "Accept-Language": "ru"}), ("ru", "browser"))


class PageTest(unittest.TestCase):
    def test_every_language_renders(self):
        for lang in i18n.available():
            page = render(lang)
            self.assertIn(f'<html lang="{lang}">', page)
            self.assertNotRegex(page, r"\{\{[\w.]+\}\}", f"{lang}: placeholder left in the page")
            self.assertIn(f'const LANG="{lang}"', page)
            inline = re.search(r"const LANG=.*?,L=(\{.*?\});\n", page, re.S)
            self.assertEqual(json.loads(inline.group(1).replace("<\\/", "</"))["lang.name"], i18n.t("lang.name", lang))
            self.assertEqual(page.count("</script>"), 1, "catalog text closed the script tag")

    def test_page_is_translated(self):
        self.assertIn(">Settings<", render("en"))
        self.assertIn(">Настройки<", render("ru"))


class ServerLanguageTest(unittest.TestCase):
    """The running server answers the page and API errors in the language of the request."""

    @classmethod
    def setUpClass(cls):
        from test_user_data import Running
        cls.home = Path(tempfile.mkdtemp())
        env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
        env.update(HOME=str(cls.home), DECKDROP_CEF="0", DECKDROP_PIN="1234", DECKDROP_STEAM=str(cls.home / "x"))
        cls.app = Running(ROOT / "tools" / "dev.py", env)

    @classmethod
    def tearDownClass(cls):
        cls.app.stop()

    def request(self, path, headers, body=None):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{self.app.port}{path}", headers=headers,
                                     data=None if body is None else json.dumps(body).encode())
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_page_language(self):
        _, ru = self.request("/", {"Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"})
        _, en = self.request("/", {"Accept-Language": "ru-RU", "Cookie": "deckdrop_lang=en"})
        self.assertIn('<html lang="ru">', ru)
        self.assertIn('<html lang="en">', en)

    def test_error_language(self):
        body = {"old": "0000", "new": "5555"}
        _, ru = self.request("/api/settings/pin", {"Accept-Language": "ru"}, body)
        _, en = self.request("/api/settings/pin", {"Accept-Language": "en"}, body)
        self.assertEqual(json.loads(ru)["error"], "неверный PIN")
        self.assertEqual(json.loads(en)["error"], "wrong PIN")


if __name__ == "__main__":
    unittest.main()
