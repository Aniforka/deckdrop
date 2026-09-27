"""Games tab: list, import, add to Steam, hide, delete."""

import shutil
import subprocess
import threading
import urllib.parse
from pathlib import Path

from .art.covers import art_files, art_worker
from .config import log
from .detect import (
    clean_title, find_exes, game_exe_path, is_linux_exe, name_candidates, pretty_name,
    recommend, strip_exe_ext,
)
from .i18n import carry, tr
from .paths import dir_size, split_ext
from .state import STATE, STATE_LOCK, save_state, update_added
from .steam.cdp import CDP
from .steam.compat import compat_for, compat_label, compat_mapping
from .steam.library import shortcuts_index
from .steam.session import session_env
from .steam.shortcuts import queue_pending
from .storage import disk_label_for, game_roots, import_allowed, imported_dirs, inside, inside_any


def add_to_steam(game_dir, exe, name=None, tool=None):
    """Add the executable to Steam under `name` (default: pretty_name) with compat `tool`
    (None = default from settings, "" = none). Returns a note for the UI."""
    p = game_exe_path(game_dir, exe)
    linux = is_linux_exe(p)
    if linux:
        try:
            p.chmod(p.stat().st_mode | 0o111)
        except OSError:
            pass
    name = (name or "").strip() or pretty_name(p, game_dir)
    tool = None if linux else ((STATE.get("default_compat") if tool is None else tool) or None)
    if CDP.available():
        appid = CDP.add_shortcut(name, str(p), str(p.parent))
        try:
            CDP.set_exe(appid, f'"{p}"', f'"{p.parent}"')
        except Exception as e:  # noqa: BLE001
            log(f"set exe/startdir after add: {e}")
        if tool:
            CDP.set_compat(appid, tool)
        update_added(str(p), appid=appid, name=name, compat=tool, via="cdp", art=False)
        threading.Thread(target=carry(art_worker), args=(str(p), False, False), daemon=True).start()
        return tr("addsteam.done", name=name) + ", " + (compat_label(tool) if tool else tr("addsteam.native"))
    # fallback: steamos-add-to-steam names the shortcut after the file; fix it up later via CEF
    env, running = session_env()
    if not running:
        raise RuntimeError(tr("steam.not_running"))
    ok = False
    if shutil.which("steamos-add-to-steam"):
        res = subprocess.run(["steamos-add-to-steam", str(p)], capture_output=True, text=True, env=env, timeout=60)
        ok = res.returncode == 0
        if not ok:
            log(f"steamos-add-to-steam failed ({res.returncode}): {(res.stderr or res.stdout).strip()}")
    if not ok:
        steam = shutil.which("steam")
        if not steam:
            raise RuntimeError(tr("steam.not_found"))
        subprocess.Popen([steam, "steam://addnonsteamgame/" + urllib.parse.quote(str(p))],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    update_added(str(p), name=name, compat=tool, via="steamos", art=False)
    queue_pending(op="rename", exe=str(p), name=name)
    if tool:
        queue_pending(op="compat", exe=str(p), tool=tool)
    threading.Thread(target=carry(art_worker), args=(str(p), False, True), daemon=True).start()
    return tr("addsteam.queued", name=name)


def game_dirs():
    """(dir, imported) for every card on the Games tab: folders inside the roots, plus imported games."""
    out = []
    for root in game_roots():
        if not root.is_dir():
            continue
        try:
            kids = sorted(root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except OSError:
            continue
        out += [(d, False) for d in kids if d.is_dir() and not d.name.startswith((".", "_"))]
    out += [(d, True) for d in imported_dirs()]
    return out


def list_games():
    out = []
    try:
        idx = shortcuts_index()
    except Exception as e:  # noqa: BLE001
        log(f"shortcuts index failed: {e}")
        idx = {}
    with STATE_LOCK:
        added = dict(STATE.get("added", {}))
        hidden = set(STATE.get("hidden", []))
        pending = STATE.get("pending") or []
    pending_exes = {op.get("exe") for op in pending}
    if True:
        for d, is_imported in game_dirs():
            disk = disk_label_for(d)
            rels = find_exes(d)
            rec_exe = recommend(rels)
            fulls = {r: str((d / r).resolve()) for r in rels}
            steam_names = [idx[fulls[r]]["name"] for r in rels if fulls[r] in idx]
            names = name_candidates(d, rels, steam_names)
            exes = []
            for rel in rels:
                full = fulls[rel]
                sc = idx.get(full)
                rec = added.get(full, {})
                appid = (sc or {}).get("appid") or rec.get("appid")
                compat_info = compat_for(appid, rec)
                exes.append({"exe": rel, "linux": is_linux_exe(rel), "recommended": rel == rec_exe,
                             "in_steam": bool(sc) or bool(rec.get("appid") or rec.get("via")),
                             "name": (sc or {}).get("name") or rec.get("name") or pretty_name(rel, d),
                             "clean_name": clean_title((sc or {}).get("name") or rec.get("name") or "") or pretty_name(rel, d),
                             "art": bool(rec.get("art")), "art_error": rec.get("art_error"),
                             "art_source": rec.get("art_source"), "vndb_title": rec.get("vndb_title"),
                             "art_note": rec.get("art_note"),
                             "compat": compat_info[0], "compat_from": compat_info[1],
                             "pending": full in pending_exes,
                             "appid": appid})
            chosen = [x for x in exes if x["in_steam"]]
            title = chosen[0]["name"] if chosen else (names[0] if names else d.name)
            out.append({"name": d.name, "title": title, "names": names, "path": str(d), "disk": disk,
                        "exes": exes, "hidden": str(d) in hidden, "imported": is_imported})
    return out


# an exe often sits in a subfolder; the game folder is the one above it
NESTED_DIRS = {"bin", "bin64", "binaries", "game", "x64", "x86", "win", "win32", "win64",
               "windows", "data", "app", "runtime", "release"}


def adopt_steam_settings(d):
    """Copy what Steam already knows about the games in this folder into DeckDrop's own record.

    Reads shortcuts.vdf, config.vdf and the grid folder; writes only DeckDrop's state file.
    """
    try:
        idx = shortcuts_index()
        ctmap = compat_mapping()
    except Exception as e:  # noqa: BLE001
        log(f"adopt: {e}")
        return []
    adopted = []
    for rel in find_exes(d):
        full = str((d / rel).resolve())
        sc = idx.get(full)
        if not sc:
            continue
        appid = sc["appid"]
        kv = {"appid": appid, "via": "steam"}
        name = clean_title(sc.get("name") or "") or strip_exe_ext(sc.get("name") or "")
        if name:
            kv["name"] = name
        if appid in ctmap:
            kv["compat"] = ctmap[appid] or None
        try:
            covers = sorted(art_files(appid, Path(sc["userdata"])))
        except OSError:
            covers = []
        if covers:
            kv.update(art=True, art_source="steam", art_note=tr("import.art_from_steam"))
        update_added(full, **kv)
        adopted.append({"exe": rel, "name": kv.get("name"), "compat": kv.get("compat"),
                        "covers": covers, "appid": appid})
    return adopted


def import_game(path):
    """Add one game that already lives somewhere on the Deck.

    Nothing is copied, moved or installed: the folder stays where it is and simply becomes a card.
    """
    raw = (path or "").strip().strip('"').strip("'")
    if not raw:
        raise ValueError(tr("import.need_path_long"))
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError(tr("import.need_absolute"))
    try:
        p = p.resolve()
    except OSError as e:
        raise ValueError(tr("import.bad_path", error=e)) from e
    if not p.exists():
        raise ValueError(tr("import.nothing_there", path=p))
    if not import_allowed(p):
        raise ValueError(tr("import.not_allowed"))
    d = p if p.is_dir() else p.parent
    if not p.is_dir() and d.name.lower() in NESTED_DIRS and d.parent != d and import_allowed(d.parent):
        d = d.parent                      # .../Fate/bin/Fate.exe -> the game folder is .../Fate
    if any(r.resolve() == d or inside(r, d) for r in game_roots() if r.exists()):
        raise ValueError(tr("import.already_inside"))
    if d in imported_dirs():
        raise ValueError(tr("import.already_added"))
    exes = find_exes(d)
    if not exes:
        raise ValueError(tr("import.no_exe", name=d.name))
    with STATE_LOCK:
        STATE["imported"] = sorted({*(STATE.get("imported") or []), str(d)})
        save_state()
    adopted = adopt_steam_settings(d)
    log(f"imported game {d} ({len(exes)} executables, {len(adopted)} already in Steam)")
    return {"path": str(d), "name": d.name, "exes": exes, "disk": disk_label_for(d), "adopted": adopted}


def unimport_game(path):
    """Stop showing an imported game. The folder itself is left untouched."""
    p = Path(path).resolve()
    with STATE_LOCK:
        cur = list(STATE.get("imported") or [])
        keep = [x for x in cur if Path(x).resolve() != p]
        if len(keep) == len(cur):
            raise ValueError(tr("import.not_imported"))
        STATE["imported"] = keep
        STATE["hidden"] = [h for h in STATE.get("hidden", []) if Path(h).resolve() != p]
        STATE["added"] = {k: v for k, v in STATE.get("added", {}).items() if not inside(p, k)}
        STATE["pending"] = [o for o in STATE.get("pending") or [] if not inside(p, o.get("exe", ""))]
        save_state()
    return p.name


def import_candidates():
    """Non-Steam shortcuts pointing outside DeckDrop's folders: one tap to show them here too."""
    out, seen = [], set()
    try:
        idx = shortcuts_index()
    except Exception as e:  # noqa: BLE001
        log(f"import scan: {e}")
        return out
    imported = imported_dirs()
    for exe, sc in idx.items():
        if not exe or not Path(exe).is_absolute():
            continue
        p = Path(exe)
        d = p.parent
        if str(d) in seen or any(inside(r, p) for r in game_roots() if r.exists()):
            continue
        if d in imported or not import_allowed(d):
            continue
        seen.add(str(d))
        out.append({"exe": exe, "dir": str(d), "name": sc.get("name"), "appid": sc.get("appid"),
                    "exists": p.exists(), "disk": disk_label_for(d)})
    out.sort(key=lambda c: (not c["exists"], (c["name"] or "").lower()))
    return out


def game_dir_path(path):
    p = Path(path).resolve()
    if p in imported_dirs():
        return p
    if not inside_any(p) or p.name.startswith("_") or not p.is_dir() or any(p == r.resolve() for r in game_roots()):
        raise ValueError(tr("err.bad_path"))
    return p


def game_info(path):
    p = game_dir_path(path)
    size, files = dir_size(p)
    return {"size": size, "files": files, "mtime": int(p.stat().st_mtime)}


def set_hidden(path, hidden):
    p = str(game_dir_path(path))
    with STATE_LOCK:
        h = set(STATE.get("hidden", []))
        (h.add if hidden else h.discard)(p)
        STATE["hidden"] = sorted(h)
        save_state()


def delete_game(path, remove_shortcut=False):
    p = game_dir_path(path)
    removed, notes = [p.name], []
    if remove_shortcut:
        with STATE_LOCK:
            recs = {k: v for k, v in STATE.get("added", {}).items() if inside(p, k)}
        idx = shortcuts_index()
        appids = {v.get("appid") for v in recs.values() if v.get("appid")}
        appids |= {v["appid"] for k, v in idx.items() if inside(p, k)}
        if appids and CDP.available():
            for appid in appids:
                try:
                    CDP.remove_shortcut(appid)
                    notes.append(tr("delete.shortcut_removed", appid=appid))
                except Exception as e:  # noqa: BLE001
                    notes.append(tr("delete.shortcut_error", appid=appid, error=e))
        elif appids:
            notes.append(tr("delete.shortcut_kept"))
    shutil.rmtree(p)
    inbox = p.parent / "_inbox"
    if inbox.is_dir():
        for f in inbox.iterdir():
            if f.is_file() and split_ext(f.name)[0] == p.name:
                f.unlink()
                removed.append(f.name)
    with STATE_LOCK:
        STATE["hidden"] = [h for h in STATE.get("hidden", []) if h != str(p)]
        STATE["imported"] = [x for x in STATE.get("imported") or [] if Path(x).resolve() != p]
        STATE["added"] = {k: v for k, v in STATE.get("added", {}).items() if not inside(p, k)}
        STATE["pending"] = [o for o in STATE.get("pending") or [] if not inside(p, o.get("exe", ""))]
        save_state()
    return removed, notes
