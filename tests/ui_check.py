#!/usr/bin/env python3
"""The page in a real browser, in every language: no JavaScript errors, no untranslated keys.

Needs Playwright (pip install playwright; python -m playwright install chromium), so it is
not part of the unittest run. CI runs it as its own step.

    python tests/ui_check.py [dist/deckdrop.py | tools/dev.py]
    CHROMIUM=/path/to/chrome python tests/ui_check.py      use an installed browser
"""
import os
import re
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_user_data import Running  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "deckdrop.py")
KEY_LIKE = re.compile(r"\b[a-z]+(?:\.[a-z_]+)+\b")
NOT_KEYS = {"e.g", "i.e", "deckdrop.py", "mega.nz", "vndb.org", "api.vndb.org", "t.vndb.org", "icon.png",
            "manifest.json"}


def main():
    home = Path(tempfile.mkdtemp())
    game = home / "Games" / "Cool Game"
    game.mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"MZ")
    (home / ".steam").mkdir()                          # the Deck's Steam is in Russian
    (home / ".steam" / "registry.vdf").write_text('"Registry" { "HKCU" { "Software" { "Valve" { "Steam" '
                                                  '{ "language" "russian" } } } } }', "utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
    env.update(HOME=str(home), DECKDROP_CEF="0", DECKDROP_PIN="1234", DECKDROP_STEAM=str(home / "no-steam"))
    app = Running(PROGRAM, env)
    failed = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None)
            # no choice on the device: Steam's language, even from an English browser; then a device set to English
            for locale, choice, lang in (("en-US", None, "ru"), ("ru-RU", "en", "en")):
                ctx = browser.new_context(locale=locale, viewport={"width": 390, "height": 844})
                if choice:
                    ctx.add_cookies([{"name": "deckdrop_lang", "value": choice, "url": f"http://127.0.0.1:{app.port}/"}])
                ctx.route("**/favicon.ico", lambda route: route.fulfill(status=204))
                page = ctx.new_page()
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: m.type == "error" and errors.append(m.text))
                page.goto(f"http://127.0.0.1:{app.port}/")
                page.wait_for_timeout(1200)
                seen = [page.inner_text("body")]
                for tab in ("#tabArch", "#tabMedia", "#tabSettings", "#tabGames"):
                    page.click(tab)
                    page.wait_for_timeout(600)
                    seen.append(page.inner_text("body"))
                page.click(".gcard")
                page.wait_for_timeout(1000)
                seen.append(page.inner_text("body"))
                page.click("#gpBack")
                page.click("#importGame")
                page.wait_for_timeout(500)
                seen.append(page.inner_text("body"))
                page.keyboard.press("Escape")
                # switching the language in Settings reloads the page in the other language
                other = "en" if lang == "ru" else "ru"
                page.click("#tabSettings")
                page.select_option("#sLang", other)
                page.wait_for_load_state()
                page.wait_for_timeout(800)
                switched = page.evaluate("LANG")
                keys = sorted(k for k in set(KEY_LIKE.findall("\n".join(seen))) - NOT_KEYS
                              if not k.endswith(".local"))   # the Deck's own address, e.g. steamdeck.local
                actual = page.evaluate("document.documentElement.lang")
                print(f"{locale}: page {lang}, after switch {switched}/{actual}, errors {errors}, raw keys {keys}")
                first = seen[0]
                if errors or keys or switched != other or ("Настройки" in first) != (lang == "ru"):
                    failed.append(locale)
                ctx.close()
            browser.close()
    finally:
        app.stop()
    if failed:
        sys.exit(f"UI check failed for {failed}")
    print("ui ok")


if __name__ == "__main__":
    main()
