"""Compatibility tools (Proton) and which one a shortcut uses."""

import re
import time
from pathlib import Path

from ..config import STEAM_ROOT, log
from ..i18n import tr
from ..steam.library import library_folders, text_vdf, vget


_CT_CACHE = {"at": 0, "tools": []}


def compat_tools():
    """[{name, label}] - installed Valve Protons (from appmanifests) + compatibilitytools.d."""
    if time.time() - _CT_CACHE["at"] < 60:
        return _CT_CACHE["tools"]
    tools, seen = [], set()

    def add(name, label, rank):
        if name and name not in seen:
            seen.add(name)
            tools.append((rank, {"name": name, "label": label}))

    for lib in library_folders():
        for acf in (lib / "steamapps").glob("appmanifest_*.acf"):
            try:
                m = re.search(r'"name"\s+"([^"]*)"', acf.read_text("utf-8", "replace"))
            except OSError:
                continue
            title = m.group(1) if m else ""
            m = re.match(r"Proton (Experimental|Hotfix|(\d+)\.(\d+))", title)
            if not m:
                continue
            if m.group(1) == "Experimental":
                add("proton_experimental", title, (0, 0))
            elif m.group(1) == "Hotfix":
                add("proton_hotfix", title, (1, 0))
            else:
                major, minor = int(m.group(2)), int(m.group(3))
                add(f"proton_{major}" if minor == 0 else f"proton_{major}{minor}", title, (2, -(major * 100 + minor)))
    for d in (STEAM_ROOT / "compatibilitytools.d", Path.home() / ".steam" / "root" / "compatibilitytools.d"):
        for vdf in d.glob("*/compatibilitytool.vdf"):
            try:
                data = text_vdf(vdf.read_text("utf-8", "replace"))
                ct = vget(data, "compatibilitytools", "compat_tools") or {}
                for name, info in ct.items():
                    label = vget(info, "display_name") if isinstance(info, dict) else None
                    add(name, label or name, (3, 0))
            except Exception as e:  # noqa: BLE001
                log(f"{vdf}: {e}")
    tools.sort(key=lambda t: t[0])
    _CT_CACHE.update(at=time.time(), tools=[t[1] for t in tools])
    return _CT_CACHE["tools"]


_CT_MAP = {"key": None, "map": {}}


def compat_mapping():
    """appid -> compat tool name, straight from Steam's config.vdf.

    This is the setting Steam actually uses, so a game added to Steam by hand (or imported into
    DeckDrop) shows its real Proton instead of an empty box.
    """
    f = STEAM_ROOT / "config" / "config.vdf"
    try:
        key = (str(f), f.stat().st_mtime_ns)
    except OSError:
        return {}
    if key == _CT_MAP["key"]:
        return _CT_MAP["map"]
    out = {}
    try:
        data = text_vdf(f.read_text("utf-8", "replace"))
        mapping = vget(data, "InstallConfigStore", "Software", "Valve", "Steam", "CompatToolMapping") or {}
        for appid, info in mapping.items():
            if appid.isdigit() and isinstance(info, dict):
                out[int(appid)] = vget(info, "name") or ""
    except Exception as e:  # noqa: BLE001
        log(f"config.vdf: {e}")
        return _CT_MAP["map"]
    _CT_MAP.update(key=key, map=out)
    return out


def compat_for(appid, rec):
    """Proton of a shortcut: our own record wins (it may be newer than the file), else Steam's."""
    if "compat" in rec:
        return rec.get("compat"), "deckdrop"
    if appid is not None:
        m = compat_mapping()
        if appid in m:
            return (m[appid] or None), "steam"
    return None, None


def compat_label(name):
    for t in compat_tools():
        if t["name"] == name:
            return t["label"]
    return name or tr("compat.none")
