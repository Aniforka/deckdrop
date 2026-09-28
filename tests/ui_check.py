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
from test_layouts import layout, shortcuts_vdf  # noqa: E402
from test_user_data import Running  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PROGRAM = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "dist" / "deckdrop.py")
KEY_LIKE = re.compile(r"\b[a-z]+(?:\.[a-z_]+)+\b")
NOT_KEYS = {"e.g", "i.e", "deckdrop.py", "mega.nz", "vndb.org", "api.vndb.org", "t.vndb.org", "icon.png",
            "manifest.json", "state.json", "github.com"}   # the last two: paths and links in the self-check


def main():
    home = Path(tempfile.mkdtemp())
    game = home / "Games" / "Cool Game"
    game.mkdir(parents=True)
    (game / "Game.exe").write_bytes(b"MZ")
    # the game is in Steam and has a controller layout there, so its page offers to save it
    steam = home / "Steam"
    (steam / "userdata" / "123" / "config").mkdir(parents=True)
    (steam / "userdata" / "123" / "config" / "shortcuts.vdf").write_bytes(
        shortcuts_vdf("Cool Game", game / "Game.exe", 3000000001))
    cfg = steam / "steamapps" / "common" / "Steam Controller Configs" / "123" / "config" / "cool game"
    cfg.mkdir(parents=True)
    (cfg / "controller_neptune.vdf").write_text(layout("#Title"), "utf-8")
    (steam / "controller_base" / "templates").mkdir(parents=True)
    (home / ".steam").mkdir()                          # the Deck's Steam is in Russian
    (home / ".steam" / "registry.vdf").write_text('"Registry" { "HKCU" { "Software" { "Valve" { "Steam" '
                                                  '{ "language" "russian" } } } } }', "utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
    env.update(HOME=str(home), DECKDROP_CEF="0", DECKDROP_PIN="1234", DECKDROP_STEAM=str(steam))
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
                polls = []
                page.on("request", lambda r: "/api/state" in r.url and polls.append(r.url))
                page.goto(f"http://127.0.0.1:{app.port}/")
                page.wait_for_timeout(1200)
                seen = [page.inner_text("body")]
                # a hidden tab (phone locked, another app) must not poll the Deck; shown again, it catches up
                page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => true});"
                              "document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(300)
                polls.clear()
                page.wait_for_timeout(2500)
                hidden_polls = len(polls)
                page.evaluate("Object.defineProperty(document, 'hidden', {configurable: true, get: () => false});"
                              "document.dispatchEvent(new Event('visibilitychange'))")
                page.wait_for_timeout(400)
                shown_polls = len(polls) - hidden_polls
                if hidden_polls or not shown_polls:
                    errors.append(f"polling: {hidden_polls} while hidden, {shown_polls} once shown")
                for tab in ("#tabArch", "#tabMedia", "#tabSettings", "#tabGames"):
                    page.click(tab)
                    page.wait_for_timeout(600)
                    seen.append(page.inner_text("body"))
                # Settings -> Performance: both buttons, their results must be translated too
                page.click("#tabSettings")
                page.click("#perfCheck")
                page.wait_for_selector(".perf-sum", timeout=40000)
                page.click("#perfBench")
                page.wait_for_selector(".perf-tab", timeout=60000)
                seen.append(page.inner_text("#perfRes"))
                page.click("#tabGames")
                page.click(".gcard")
                page.wait_for_timeout(1000)
                seen.append(page.inner_text("body"))
                # the game's layout: save it to DeckDrop, then it is listed in Settings
                page.click("[data-lysave]")
                page.fill("#mf0", f"Layout {lang}")
                page.click("#mok")
                page.wait_for_selector(".toast.ok", timeout=10000)
                seen.append(page.inner_text("#toasts"))
                page.click("#tabSettings")
                page.wait_for_selector(f"#lyList >> text=Layout {lang}", timeout=10000)
                seen.append(page.inner_text("#sLayouts"))
                page.click("#tabGames")
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
