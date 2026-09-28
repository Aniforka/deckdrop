"""Controller layouts of the Deck saved in DeckDrop and offered to every game as Steam templates.

Steam keeps a game's layouts in Steam Controller Configs/<account>/config/<game>/: the current one
(controller_neptune.vdf, or <controller serial>.vdf) and the ones saved under a name ("<name>_0.vdf").
It lists only that folder's files for the game, which is why a saved layout cannot be picked in
another game. A file put into controller_base/templates, though, is offered to every game.

So DeckDrop copies a layout into its own folder (<id>.vdf + <id>.json) and mirrors each one into
the templates as controller_neptune_deckdrop_<id>.vdf, titled "DeckDrop: <name>". Saving, renaming
and deleting touch only those files: never a game's layouts, Steam's own templates or state.json.

Applying one to a game is asked of the running Steam (Steam control), the way its own layout
picker does it: select template://<that file> for the game on the Deck's controller. Steam then
records the choice in its configset and keeps the game's previous layout file as it was. The
Deck's built-in controls are not controller 0: Steam gives them an index of its own (15 on a
Deck, measured), found in its ControllerStore.
"""

import json
import os
import re
import secrets
import threading
import time
from pathlib import Path

from ..config import STATE_FILE, STEAM_ROOT, log
from ..detect import game_exe_path
from .. import i18n
from ..i18n import tr
from ..state import STATE, update_added
from .cdp import CDP
from .library import shortcuts_index, text_vdf, vget

LAYOUTS_DIR = Path(os.environ.get("DECKDROP_LAYOUTS", str(STATE_FILE.parent / "layouts")))
CONFIGS = "steamapps/common/Steam Controller Configs"
TEMPLATE_PREFIX = "controller_neptune_deckdrop_"
DECK = "controller_neptune"
ID = re.compile(r"[0-9a-f]{8}")
SAVED = re.compile(r".+_\d+\.vdf")          # "<name>_<n>.vdf": saved under a name in Steam
MAX_SIZE = 1 << 20
NAME_MAX = 80
_LOCK = threading.RLock()


# ---- reading layout files

def read_layout(data):
    """(text, controller_mappings dict) of a layout file's bytes; ValueError when it is not one."""
    text = data.decode("utf-8-sig", "replace") if isinstance(data, bytes) else data
    m = vget(text_vdf(text), "controller_mappings")
    if not isinstance(m, dict):
        raise ValueError(tr("layouts.not_a_layout"))
    return text, m


def layout_title(m):
    """What Steam shows: the title, or the English one of a template ("#Title" is a lookup key)."""
    title = str(m.get("title") or "")
    if title.startswith("#"):
        title = str(vget(m, "localization", "english", "title") or "")
    return title.strip()


def set_value(text, key, value):
    """Text with the first "key" "value" pair (the layout's own, above any block) set to value."""
    value = re.sub(r"[\x00-\x1f]", " ", value).replace("\\", "\\\\").replace('"', '\\"')
    pat = re.compile(r'("' + re.escape(key) + r'"[ \t]+)"(?:\\.|[^"\\\n])*"')
    if pat.search(text):
        return pat.sub(lambda mm: mm.group(1) + '"' + value + '"', text, count=1)
    # no such line: add it right after the opening brace of controller_mappings
    return re.sub(r'("controller_mappings"\s*\{)', lambda mm: f'{mm.group(1)}\n\t"{key}"\t\t"{value}"', text, count=1)


def clean_name(name):
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]", " ", str(name or ""))).strip()
    if not name:
        raise ValueError(tr("layouts.need_name"))
    return name[:NAME_MAX]


# ---- where a game's layouts are

def folder_key(name):
    """Steam's folder for a non-Steam game: its name in lower case with only letters, digits, _, ! and spaces."""
    return re.sub(r"[^\w !]", "", str(name or "").lower())


def config_dirs(account=None):
    base = STEAM_ROOT / CONFIGS
    if not base.is_dir():
        return []
    accounts = [base / account] if account else sorted(p for p in base.iterdir() if p.is_dir())
    return [a / "config" for a in accounts if (a / "config").is_dir()]


def shortcut_of(game_dir, exe):
    """(exe path, appid or None, account or None) of the game's Steam shortcut."""
    p = game_exe_path(game_dir, exe)
    sc = shortcuts_index().get(str(p)) or {}
    rec = (STATE.get("added") or {}).get(str(p)) or {}
    account = Path(sc["userdata"]).name if sc.get("userdata") else None
    return p, sc.get("appid") or rec.get("appid"), account


def folder_of_url(url):
    """The game folder of an autosave:///…/Steam Controller Configs/<account>/config/<folder>/<file> URL."""
    m = re.match(r"autosave://(/.*)$", str(url or ""))
    if not m:
        return None
    f = Path(m.group(1))
    base = (STEAM_ROOT / CONFIGS).resolve()
    try:
        if f.parent.resolve().parent.parent.parent != base:
            return None
    except OSError:
        return None
    return f.parent.resolve()


def game_folders(game_dir, exe, current=None):
    """Folders holding the layouts of the game's shortcut. Steam names the folder after the shortcut
    when it first sees it, so a shortcut renamed later still uses the old name: look for all of them.
    The running Steam, when asked (`current`), says exactly where its current layout is."""
    p = game_exe_path(game_dir, exe)
    sc = shortcuts_index().get(str(p)) or {}
    rec = (STATE.get("added") or {}).get(str(p)) or {}
    names = [sc.get("name"), rec.get("name"), p.name, p.stem]
    keys = []
    for k in [folder_key(n) for n in names if n] + [str(a) for a in (sc.get("appid"), rec.get("appid")) if a]:
        if k and k not in keys and k not in (".", ".."):
            keys.append(k)
    account = Path(sc["userdata"]).name if sc.get("userdata") else None
    out = []
    live = folder_of_url((current or {}).get("url"))
    if live and live.is_dir():
        out.append(live)
    for cfg in config_dirs(account) or config_dirs():
        out += [cfg / k for k in keys if (cfg / k).is_dir() and (cfg / k).resolve() != live]
    return out


def game_sources(game_dir, exe, current=None):
    """The Deck layouts Steam has for this game: [{src, title, kind, mtime}], current ones first."""
    out = []
    for folder in game_folders(game_dir, exe, current):
        for f in sorted(folder.glob("*.vdf")):
            try:
                if f.stat().st_size > MAX_SIZE:
                    continue
                _, m = read_layout(f.read_bytes())
            except (OSError, ValueError):
                continue
            if str(m.get("controller_type") or "") != DECK:
                continue
            kind = "saved" if SAVED.fullmatch(f.name) and f.name != DECK + ".vdf" else "current"
            base = layout_title(m) if kind == "current" else ""
            out.append({"src": f"{folder.parent.parent.name}/{folder.name}/{f.name}", "kind": kind,
                        "title": layout_title(m) if kind == "saved" else "", "base": base,
                        "mtime": int(f.stat().st_mtime)})
    out.sort(key=lambda s: (s["kind"] != "current", -s["mtime"]))
    return out


def source_file(game_dir, exe, src):
    """The file behind a source token, only if it is one of this game's sources."""
    _, appid, _ = shortcut_of(game_dir, exe)
    if not any(s["src"] == src for s in game_sources(game_dir, exe, steam_current(appid))):
        raise ValueError(tr("layouts.source_gone"))
    account, folder, name = src.split("/")
    return STEAM_ROOT / CONFIGS / account / "config" / folder / name


# ---- DeckDrop's own copies

def _meta_path(lid):
    return LAYOUTS_DIR / f"{lid}.json"


def _vdf_path(lid):
    return LAYOUTS_DIR / f"{lid}.vdf"


def _check_id(lid):
    lid = str(lid or "")
    if not ID.fullmatch(lid) or not _meta_path(lid).is_file():
        raise ValueError(tr("layouts.not_found"))
    return lid


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def list_layouts():
    out = []
    if LAYOUTS_DIR.is_dir():
        for f in LAYOUTS_DIR.glob("*.json"):
            if not ID.fullmatch(f.stem) or not _vdf_path(f.stem).is_file():
                continue
            try:
                meta = json.loads(f.read_text("utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                out.append({"id": f.stem, "name": str(meta.get("name") or f.stem), "game": str(meta.get("game") or ""),
                            "created": int(meta.get("created") or 0), "template": _has_template(f.stem)})
    out.sort(key=lambda x: (x["name"].lower(), x["created"]))
    return out


def _store(data, name, game, source=""):
    _, m = read_layout(data)
    if str(m.get("controller_type") or "") != DECK:
        raise ValueError(tr("layouts.not_deck"))
    name = clean_name(name)                 # checked before anything is written
    with _LOCK:
        lid = secrets.token_hex(4)
        while _meta_path(lid).exists() or _vdf_path(lid).exists():
            lid = secrets.token_hex(4)
        _write(_vdf_path(lid), data)
        meta = {"name": name, "game": game, "source": source, "created": int(time.time())}
        _write(_meta_path(lid), json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
        sync_templates()
    return {"id": lid, "name": meta["name"], "template": _has_template(lid)}


def save_from_game(game_dir, exe, src, name):
    f = source_file(game_dir, exe, src)
    data = f.read_bytes()
    if len(data) > MAX_SIZE:
        raise ValueError(tr("layouts.too_big"))
    game = (shortcuts_index().get(str(game_exe_path(game_dir, exe))) or {}).get("name") or Path(game_dir).name
    return _store(data, name, game, src)


def save_upload(data, name):
    if len(data) > MAX_SIZE:
        raise ValueError(tr("layouts.too_big"))
    return _store(data, name, "")


def rename_layout(lid, name):
    with _LOCK:
        lid = _check_id(lid)
        meta = json.loads(_meta_path(lid).read_text("utf-8"))
        meta["name"] = clean_name(name)
        _write(_meta_path(lid), json.dumps(meta, ensure_ascii=False, indent=1).encode("utf-8"))
        sync_templates()
    return {"id": lid, "name": meta["name"]}


def delete_layout(lid):
    with _LOCK:
        lid = _check_id(lid)
        for p in (_template_path(lid), _vdf_path(lid), _meta_path(lid)):
            if p is None:
                continue
            try:
                p.unlink()
            except FileNotFoundError:
                pass
    return lid


def export_layout(lid):
    """(file name, bytes) of a saved layout, titled with its DeckDrop name."""
    lid = _check_id(lid)
    name = json.loads(_meta_path(lid).read_text("utf-8")).get("name") or lid
    text, _ = read_layout(_vdf_path(lid).read_bytes())
    safe = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", name).strip(" .") or lid
    return safe + ".vdf", set_value(text, "title", name).encode("utf-8")


# ---- templates: what Steam offers every game

def templates_dir():
    d = STEAM_ROOT / "controller_base" / "templates"
    return d if d.is_dir() else None


def _template_path(lid):
    d = templates_dir()
    return d / f"{TEMPLATE_PREFIX}{lid}.vdf" if d else None


def _has_template(lid):
    p = _template_path(lid)
    return bool(p and p.is_file())


def _template_text(lid, meta):
    text, _ = read_layout(_vdf_path(lid).read_bytes())
    text = set_value(text, "title", "DeckDrop: " + meta["name"])
    game = meta.get("game") or ""
    # in the language of the Deck's Steam, whoever's phone made the change
    desc = tr("layouts.template_desc", lang=i18n.default(), game=game) if game else "DeckDrop"
    return set_value(text, "description", desc)


def sync_templates():
    """Make the templates match the saved layouts: add missing ones (a Steam update may wipe them),
    retitle renamed ones, drop ones whose layout is gone. Only DeckDrop's template files are touched."""
    d = templates_dir()
    if d is None:
        return 0
    changed = 0
    with _LOCK:
        keep = set()
        for item in list_layouts():
            lid = item["id"]
            keep.add(lid)
            try:
                meta = json.loads(_meta_path(lid).read_text("utf-8"))
                want = _template_text(lid, {"name": item["name"], "game": meta.get("game") or ""}).encode("utf-8")
                p = _template_path(lid)
                if not p.is_file() or p.read_bytes() != want:
                    _write(p, want)
                    changed += 1
            except (OSError, ValueError) as e:
                log(f"layout template {lid}: {e}")
        for p in d.glob(TEMPLATE_PREFIX + "*.vdf"):
            lid = p.name[len(TEMPLATE_PREFIX):-4]
            if ID.fullmatch(lid) and lid not in keep:
                p.unlink()
                changed += 1
    return changed


# ---- the running Steam: which layout a game has, and switching it

# the Deck's own controls: the Steam Deck entry of Steam's controller list, else the last active one
DECK_CONTROLLER_JS = """(() => {
  const store = window.ControllerStore || {}, list = store.m_controllerList || [];
  const deck = list.find(c => c.eControllerType === 4) || list[0];
  const index = deck ? deck.nControllerIndex : store.m_nLastValidActiveControllerIndex;
  return index === undefined ? null : index;
})()"""

CURRENT_JS = """(async (appid) => {
  const index = __DECK__;
  if (index === null) return null;
  const c = await SteamClient.Input.GetConfigForAppAndController(appid, index);
  return c && c.bConfigurationEnabled ? {url: String(c.URL || ''), title: String(c.Title || ''), index} : null;
})(__APPID__)"""

# select the layout the way Steam's own picker does (the last two arguments: not a preview, a user's
# choice; with fewer, Steam selects nothing), then read it back until Steam reports it
APPLY_JS = """(async (appid, url) => {
  const input = SteamClient.Input, index = __DECK__;
  if (index === null) return {ok: false, reason: 'no_controller'};
  const read = async () => {
    const c = await input.GetConfigForAppAndController(appid, index);
    return c ? {url: String(c.URL || ''), title: String(c.Title || '')} : {url: '', title: ''};
  };
  await input.SetSelectedConfigForApp(appid, index, url, false, 1);
  for (let i = 0; i < 12; i++) {
    const now = await read();
    if (now.url === url) return {ok: true, index, ...now};
    await new Promise(r => setTimeout(r, 250));
  }
  return {ok: false, reason: 'not_switched', index, ...(await read())};
})(__APPID__, __URL__)"""


def _js(template, appid, url=None):
    js = template.replace("__DECK__", DECK_CONTROLLER_JS).replace("__APPID__", str(int(appid)))
    return js.replace("__URL__", json.dumps(url)) if url is not None else js


def steam_current(appid):
    """{url, title, index} of the game's layout in the running Steam, or None (no Steam control)."""
    if not appid or not CDP.available():
        return None
    try:
        res = CDP.eval(_js(CURRENT_JS, appid), timeout=10)
    except Exception as e:  # noqa: BLE001
        log(f"layout of {appid}: {e}")
        return None
    return res if isinstance(res, dict) else None


def template_url(lid):
    return f"template://{TEMPLATE_PREFIX}{lid}.vdf"


def apply_to_app(appid, lid):
    """Switch the game to a saved layout through the running Steam; the layout's name when it took."""
    with _LOCK:
        lid = _check_id(lid)
        sync_templates()
        if not _has_template(lid):
            raise RuntimeError(tr("layouts.no_templates"))
        name = json.loads(_meta_path(lid).read_text("utf-8")).get("name") or lid
    if not CDP.available():
        raise RuntimeError(tr("layouts.need_cdp"))
    res = CDP.eval(_js(APPLY_JS, appid, template_url(lid)), timeout=20)
    if not isinstance(res, dict) or not res.get("ok"):
        reason = (res or {}).get("reason") if isinstance(res, dict) else None
        raise RuntimeError(tr("layouts.no_controller") if reason == "no_controller" else tr("layouts.not_applied"))
    return name


def apply_layout(game_dir, exe, lid):
    p, appid, _ = shortcut_of(game_dir, exe)
    if not appid:
        raise ValueError(tr("layouts.not_in_steam_game"))
    name = apply_to_app(appid, lid)
    update_added(str(p), layout=name, layout_error=None)
    return tr("layouts.applied", name=name)


def default_layout():
    """The saved layout new games get (Settings), or None: none chosen, or it was deleted since."""
    lid = str(STATE.get("default_layout") or "")
    return lid if ID.fullmatch(lid) and _meta_path(lid).is_file() and _vdf_path(lid).is_file() else None


def apply_default(exe, appid):
    """Give a game DeckDrop just added to Steam the default layout; records how it went."""
    lid = default_layout()
    if not lid:
        return None
    try:
        name = apply_to_app(appid, lid)
    except Exception as e:  # noqa: BLE001
        log(f"default layout for {Path(exe).name}: {e}")
        update_added(str(exe), layout_error=str(e))
        return None
    update_added(str(exe), layout=name, layout_error=None)
    return name
