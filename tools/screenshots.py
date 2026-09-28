#!/usr/bin/env python3
"""Screenshots and the demo GIF for the READMEs, in every language, from a demo Deck.

    python tools/screenshots.py                 -> docs/media/<lang>/*.png, demo.gif
    python tools/screenshots.py --lang en       one language only
    CHROMIUM=/path/to/chrome python tools/screenshots.py

Needs Playwright and Pillow (pip install playwright pillow; python -m playwright install
chromium). Nothing here touches a real Steam: a temporary home gets a fake Steam folder
(shortcuts.vdf, grid artwork, Proton manifests, screenshots) and a few made-up games, and
a local server hands out a game archive slowly enough to show a download in progress.
Run it again whenever the page changes.
"""
import argparse
import http.server
import io
import json
import os
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
import zlib
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
import build  # noqa: E402
from fake_cdp import FakeSteam  # noqa: E402  (Steam control, so the page offers to apply layouts)

PIN = "1234"
MEDIA_PASSWORD = "deckdrop"
PHONE = {"width": 390, "height": 844}
DESKTOP = {"width": 1280, "height": 860}
# made-up games: (folder, exe, name in Steam or None, palette, disk)
GAMES = [
    ("Ember Knight", "EmberKnight.exe", "Ember Knight", ((255, 120, 60), (70, 20, 60)), "internal"),
    ("Paper Lanterns", "PaperLanterns.exe", "Paper Lanterns", ((255, 200, 120), (60, 40, 110)), "internal"),
    ("Neon Drift", "NeonDrift.x86_64", "Neon Drift", ((40, 230, 220), (40, 20, 90)), "internal"),
    ("Moss and Stone", "MossAndStone.exe", None, ((120, 200, 110), (20, 50, 40)), "sd"),
]
NEW_GAME = ("Starlight Harbor", "Starlight Harbor.exe")
ARCHIVE = "Starlight Harbor.zip"
USER = "10000001"
LAYOUT_GAME = "Paper Lanterns"
# controller layouts Steam has for LAYOUT_GAME: (file, title, based on)
LAYOUTS = [("controller_neptune.vdf", "#Title", "Gamepad with Mouse Trackpad"),
           ("reading_0.vdf", {"ru": "Для чтения", "en": "Reading"}, None)]
SAVED = {"ru": ["Визуальные новеллы", "Шутеры"], "en": ["Visual novels", "Shooters"]}


def layout_vdf(title, based_on):
    return (f'"controller_mappings"\n{{\n\t"version"\t\t"3"\n\t"title"\t\t"{title}"\n'
            f'\t"controller_type"\t\t"controller_neptune"\n\t"localization"\n\t{{\n\t\t"english"\n\t\t{{\n'
            f'\t\t\t"title"\t\t"{based_on or title}"\n\t\t}}\n\t}}\n\t"group"\n\t{{\n\t\t"id"\t\t"0"\n\t}}\n}}\n')


def font(size, bold=True):
    for name in (("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"), "Arial.ttf"):
        for base in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF", "/usr/share/fonts/dejavu", ""):
            try:
                return ImageFont.truetype(str(Path(base) / name) if base else name, size)
            except OSError:
                continue
    return ImageFont.load_default()


def art(size, colors, title=None, seed=0, transparent=False):
    """A poster-like image: a diagonal gradient, soft light blobs and the title."""
    w, h = size
    if transparent:
        img = Image.new("RGBA", size, (0, 0, 0, 0))
    else:
        a, b = colors
        grad = Image.linear_gradient("L").resize(size)
        img = Image.composite(Image.new("RGB", size, a), Image.new("RGB", size, b), grad).convert("RGBA")
        rnd = random.Random(seed)
        glow = Image.new("RGBA", size, (0, 0, 0, 0))
        d = ImageDraw.Draw(glow)
        for _ in range(7):
            r = rnd.randint(min(w, h) // 8, min(w, h) // 3)
            x, y = rnd.randint(0, w), rnd.randint(0, h)
            d.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rnd.randint(18, 45)))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(min(w, h) // 25)))
    if title:
        d = ImageDraw.Draw(img)
        f = font(max(18, int(min(w * 0.11, h * 0.2))))
        box = d.textbbox((0, 0), title, font=f)
        tw, th = box[2] - box[0], box[3] - box[1]
        x, y = (w - tw) // 2, int(h * 0.72 - th / 2) if not transparent else (h - th) // 2
        d.text((x + 3, y + 3), title, font=f, fill=(0, 0, 0, 140))
        d.text((x, y), title, font=f, fill=(255, 255, 255, 255))
    return img


def save(img, path, quality=82):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jpg":
        img.convert("RGB").save(path, quality=quality)
    else:
        img.save(path)


def shortcut_appid(exe_quoted, name):
    return (zlib.crc32((exe_quoted + name).encode()) | 0x80000000) & 0xFFFFFFFF


def shortcuts_vdf(entries):
    """Binary shortcuts.vdf as Steam writes it: {i: {appid, AppName, Exe, StartDir}}."""
    def s(k, v):
        return b"\x01" + k.encode() + b"\0" + v.encode() + b"\0"
    out = b"\x00shortcuts\x00"
    for i, (appid, name, exe_q, start) in enumerate(entries):
        out += b"\x00" + str(i).encode() + b"\0"
        out += b"\x02appid\x00" + struct.pack("<i", appid - (1 << 32) if appid >= 1 << 31 else appid)
        out += s("AppName", name) + s("Exe", exe_q) + s("StartDir", start) + b"\x08"
    return out + b"\x08\x08"


def demo_home(home, lang):
    """A lived-in Deck: games on two disks, three in Steam with artwork, screenshots, saves."""
    (home / ".steam").mkdir(parents=True, exist_ok=True)      # the Steam language picks the page's
    (home / ".steam" / "registry.vdf").write_text(
        '"Registry" { "HKCU" { "Software" { "Valve" { "Steam" { "language" "%s" } } } } }'
        % {"ru": "russian", "en": "english"}[lang])
    steam = home / ".local" / "share" / "Steam"
    ud = steam / "userdata" / USER
    (ud / "config" / "grid").mkdir(parents=True)
    apps = steam / "steamapps"
    apps.mkdir(parents=True)
    (apps / "libraryfolders.vdf").write_text('"libraryfolders"\n{\n "0"\n {\n  "path" "%s"\n }\n}\n' % steam)
    for appid, name in ((1493710, "Proton Experimental"), (2805730, "Proton 9.0"), (2348590, "Proton 8.0")):
        (apps / f"appmanifest_{appid}.acf").write_text('"AppState"\n{\n "appid" "%d"\n "name" "%s"\n}\n' % (appid, name))
    sd = home / "sdcard"
    entries, added = [], {}
    for i, (folder, exe, steam_name, colors, disk) in enumerate(GAMES):
        root = (home / "Games") if disk == "internal" else (sd / "Games")
        d = root / folder
        (d / "save").mkdir(parents=True)
        (d / exe).write_bytes(b"MZ" + os.urandom(2048))
        (d / "unins000.exe").write_bytes(b"MZ")
        (d / "save" / "slot1.sav").write_bytes(os.urandom(4096))
        if not steam_name:
            continue
        exe_path = str((d / exe).resolve())
        exe_q = f'"{exe_path}"'
        appid = shortcut_appid(exe_q, steam_name)
        entries.append((appid, steam_name, exe_q, f'"{d}"'))
        grid = ud / "config" / "grid"
        save(art((600, 900), colors, steam_name, i), grid / f"{appid}p.jpg")
        save(art((920, 430), colors, steam_name, i + 10), grid / f"{appid}.jpg")
        save(art((1920, 620), colors, None, i + 20), grid / f"{appid}_hero.jpg", 70)
        save(art((640, 200), colors, steam_name, transparent=True), grid / f"{appid}_logo.png")
        save(art((256, 256), colors, steam_name[0], i + 30), grid / f"{appid}_icon.png")
        added[exe_path] = {"at": int(time.time()) - 86400 * (i + 1), "appid": appid, "name": steam_name,
                           "compat": None if exe.endswith(".x86_64") else "proton_experimental",
                           "via": "cdp", "art": True, "art_source": "vndb" if "Lantern" in folder else "icon",
                           "vndb_title": steam_name if "Lantern" in folder else None}
        shots = ud / "760" / "remote" / str(appid) / "screenshots"
        (shots / "thumbnails").mkdir(parents=True)
        for k in range(3):
            img = art((1280, 800), colors, None, i * 10 + k)
            name = f"2026092{k}{i}1530_{k + 1}.jpg"
            save(img, shots / name)
            save(img.resize((320, 200)), shots / "thumbnails" / name)
            when = time.time() - 86400 * (3 * k + i) - 3600 * (i + 2 * k)
            for f in (shots / name, shots / "thumbnails" / name):
                os.utime(f, (when, when))
    (ud / "config" / "shortcuts.vdf").write_bytes(shortcuts_vdf(entries))
    # Steam's layouts for one game, and its templates folder
    cfg = steam / "steamapps" / "common" / "Steam Controller Configs" / USER / "config" / LAYOUT_GAME.lower()
    cfg.mkdir(parents=True)
    for name, title, based_on in LAYOUTS:
        (cfg / name).write_text(layout_vdf(title if isinstance(title, str) else title[lang], based_on), "utf-8")
    (steam / "controller_base" / "templates").mkdir(parents=True)
    state = home / ".config" / "deckdrop" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"admin_pin": PIN, "added": added, "default_disk": "internal",
                                 "cef_enabled": True}, indent=1))
    return steam, sd


def game_archive(path):
    """The game the demo downloads: a zip big enough to show progress."""
    folder, exe = NEW_GAME
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as z:
        z.writestr(f"{folder}/{exe}", b"MZ" + os.urandom(4096))
        z.writestr(f"{folder}/data.pak", os.urandom(9 * 1024 * 1024))
        z.writestr(f"{folder}/save/.keep", b"")


class Slow(http.server.SimpleHTTPRequestHandler):
    """Serves files at about 1.3 MB/s, like a real download."""

    def copyfile(self, source, outputfile):
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            outputfile.write(chunk)
            time.sleep(0.05)

    def log_message(self, *args):
        pass


def serve(directory):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a: Slow(*a, directory=str(directory)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def post(port, path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def png(shot, path, width=None):
    """Store a screenshot small: fewer colors, same look (flat UI)."""
    img = Image.open(io.BytesIO(shot)).convert("RGB")
    if width and img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.LANCZOS)
    img.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.FLOYDSTEINBERG).save(
        path, optimize=True)


def shoot(lang, out, chromium):
    locale = {"ru": "ru-RU", "en": "en-US"}[lang]
    work = Path(tempfile.mkdtemp())
    # /home/deck when it is free (a build machine), so paths look like the Deck's; never a real Deck's home
    home = Path("/home/deck")
    try:
        home.mkdir(parents=True)
        own_home = True
    except OSError:
        home, own_home = work / "home", False
    steam, sd = demo_home(home, lang)
    (work / "www").mkdir()
    game_archive(work / "www" / ARCHIVE)
    srv, base = serve(work / "www")
    program = work / "deckdrop.py"
    program.write_text(build.build(), "utf-8")
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("DECKDROP_")}
    fake = FakeSteam()
    lantern = next(e for e in json.loads((home / ".config/deckdrop/state.json").read_text())["added"].items()
                   if LAYOUT_GAME in e[0])
    fake.current[lantern[1]["appid"]] = ("", "")
    env.update(HOME=str(home), DECKDROP_PORT=str(port), DECKDROP_STEAM=str(steam), DECKDROP_CEF="1",
               DECKDROP_CEF_PORT=str(fake.port), DECKDROP_DISKS=f"microSD={sd / 'Games'}")
    app = subprocess.Popen([sys.executable, str(program)], env=env, stdout=subprocess.DEVNULL,
                           stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/"
    for _ in range(100):
        try:
            urllib.request.urlopen(url + "api/state")
            break
        except OSError:
            time.sleep(0.2)
    post(port, "/api/media/setup", {"password": MEDIA_PASSWORD})
    out = out / lang
    out.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=chromium or None)

            def context(viewport, scale):
                ctx = browser.new_context(locale=locale, viewport=viewport, device_scale_factor=scale,
                                          is_mobile=viewport is PHONE, has_touch=viewport is PHONE)
                ctx.route("**/favicon.ico", lambda route: route.fulfill(status=204))
                ctx.route("**/api/state", deck_address)
                return ctx

            def close(ctx):
                ctx.unroute_all(behavior="ignoreErrors")
                ctx.close()

            def deck_address(route):
                # the page shows the machine's own address; show the one a Deck has
                try:
                    res = route.fetch()
                    st = res.json()
                    st["urls"] = ["http://steamdeck.local:8088", "http://192.168.1.42:8088"]
                    route.fulfill(response=res, json=st)
                except Exception:   # noqa: BLE001 - the page polls every second; a closing tab drops it
                    pass

            # the demo: paste a link, watch it download and unpack, open the new game
            ctx = context(PHONE, 1)
            page = ctx.new_page()
            page.goto(url)
            page.wait_for_timeout(1500)
            frames = []

            def frame(n=1, gap=0.0):
                for _ in range(n):
                    frames.append(page.screenshot())
                    if gap:
                        time.sleep(gap)
            link = base + urllib.parse.quote(ARCHIVE)
            frame(4)
            page.click("#url")
            for ch in link:
                page.keyboard.type(ch)
                if len(frames) % 1 == 0 and random.random() < 0.18:
                    frame()
            frame(2)
            page.click("#go")
            deadline = time.time() + 40
            while time.time() < deadline:
                frame(gap=0.25)
                st = json.loads(urllib.request.urlopen(url + "api/state").read())
                if any("Starlight" in g["name"] for g in st["games"]) and not any(
                        j["status"] in ("queued", "resolving", "downloading", "extracting") for j in st["jobs"]):
                    break
            page.wait_for_timeout(1200)
            frame(6)
            page.click(".gcard[data-open*='Starlight']")
            page.wait_for_timeout(1200)
            frame(10)
            gif(frames, out / "demo.gif")
            close(ctx)

            # phone and desktop: the games tab with a download in progress (the demo's game removed first)
            post(port, "/api/game/delete", {"path": str(home / "Games" / NEW_GAME[0]), "pin": PIN})
            post(port, "/api/clear", {})
            ctx = context(PHONE, 2)
            page = ctx.new_page()
            desk_ctx = context(DESKTOP, 1)
            desk = desk_ctx.new_page()
            page.goto(url)
            desk.goto(url)
            page.wait_for_timeout(800)
            post(port, "/api/download", {"url": link})
            page.wait_for_timeout(3500)
            png(page.screenshot(), out / "games.png", 780)
            png(desk.screenshot(), out / "desktop.png")
            close(desk_ctx)
            # phone: a game page (launch, artwork)
            page.click(".gcard[data-open$='Paper Lanterns']")
            page.wait_for_timeout(2500)
            png(page.screenshot(full_page=True, clip={"x": 0, "y": 0, "width": 390, "height": 1190}),
                out / "game.png", 780)
            # phone: the game's controller layouts, two saved in DeckDrop, one applied
            lantern_dir = str(Path(lantern[0]).parent)
            q = f"game={urllib.parse.quote(lantern_dir)}&exe={urllib.parse.quote(Path(lantern[0]).name)}"
            sources = json.loads(urllib.request.urlopen(url + "api/layouts/sources?" + q).read())["sources"]
            ids = [post(port, "/api/layouts/save", {"game": lantern_dir, "exe": Path(lantern[0]).name,
                                                    "src": src["src"], "name": name})["id"]
                   for src, name in zip(sources, SAVED[lang])]
            post(port, "/api/layouts/apply", {"game": lantern_dir, "exe": Path(lantern[0]).name, "id": ids[0]})
            # what Steam then reports for the game, as the Deck did
            fake.selected.clear()
            fake.current[lantern[1]["appid"]] = (f"template://controller_neptune_deckdrop_{ids[0]}.vdf",
                                                 "DeckDrop: " + SAVED[lang][0])
            post(port, "/api/settings", {"default_layout": ids[0]})
            page.goto(url + "#g=" + urllib.parse.quote(lantern_dir))
            page.wait_for_timeout(2500)
            card = page.locator("#gpLayoutsCard")
            card.scroll_into_view_if_needed()
            page.wait_for_timeout(300)
            png(card.screenshot(), out / "layouts.png", 780)
            page.goto(url)
            page.wait_for_timeout(1200)
            page.click("#tabSettings")
            page.wait_for_timeout(1200)
            card = page.locator("#sLayouts")
            card.scroll_into_view_if_needed()
            png(card.screenshot(), out / "layouts-settings.png", 780)
            # phone: the gallery
            page.goto(url)
            page.click("#tabMedia")
            page.wait_for_timeout(400)
            page.fill("#pw", MEDIA_PASSWORD)
            page.click("#pwgo")
            page.wait_for_timeout(1800)
            png(page.screenshot(), out / "media.png", 780)
            # phone: settings with the language picker
            page.click("#tabSettings")
            page.wait_for_timeout(700)
            png(page.screenshot(), out / "settings.png", 780)
            close(ctx)

            browser.close()
    finally:
        app.terminate()
        srv.shutdown()
        fake.close()
        if own_home:
            shutil.rmtree(home, ignore_errors=True)
    print(f"{lang}: " + ", ".join(f"{f.name} {f.stat().st_size // 1024} KiB" for f in sorted(out.iterdir())))


def gif(frames, path, width=360):
    """Frames -> an optimized GIF; the last frame lingers."""
    imgs = []
    for shot in frames:
        img = Image.open(io.BytesIO(shot)).convert("RGB")
        imgs.append(img.resize((width, round(img.height * width / img.width)), Image.LANCZOS))
    palette = imgs[len(imgs) // 2].quantize(colors=128, method=Image.Quantize.MEDIANCUT)
    out = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in imgs]
    durations = [220] * len(out)
    durations[-1] = 2500
    out[0].save(path, save_all=True, append_images=out[1:], duration=durations, loop=0, optimize=True, disposal=1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lang", action="append", help="language to shoot (default: all)")
    ap.add_argument("--out", default=str(ROOT / "docs" / "media"))
    args = ap.parse_args()
    random.seed(7)
    for lang in args.lang or ["ru", "en"]:
        shoot(lang, Path(args.out), os.environ.get("CHROMIUM"))


if __name__ == "__main__":
    main()
