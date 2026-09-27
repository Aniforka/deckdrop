"""Non-Steam shortcuts: resolve app ids, rename, set Proton, pending ops."""

import time
from pathlib import Path

from ..config import log
from ..detect import game_exe_path
from ..i18n import tr
from ..state import STATE, STATE_LOCK, added_rec, save_state, update_added
from ..steam.cdp import CDP
from ..steam.compat import compat_label
from ..steam.library import shortcuts_index


def queue_pending(**op):
    with STATE_LOCK:
        pend = list(STATE.get("pending") or [])
        pend = [o for o in pend if not (o.get("op") == op["op"] and o.get("exe") == op["exe"])]
        pend.append(op)
        STATE["pending"] = pend
        save_state()


def resolve_appid(exe, wait=0):
    """appid of the shortcut for exe: from state, else shortcuts.vdf (polling up to `wait` s)."""
    rec = added_rec(str(exe))
    if rec.get("appid"):
        return rec["appid"]
    deadline = time.time() + wait
    while True:
        sc = shortcuts_index().get(str(exe))
        if sc:
            update_added(str(exe), appid=sc["appid"])
            return sc["appid"]
        if time.time() >= deadline:
            return None
        time.sleep(1)


def rename_shortcut(game_dir, exe, name):
    p = game_exe_path(game_dir, exe)
    name = name.strip()
    if not name:
        raise ValueError(tr("rename.empty"))
    update_added(str(p), name=name)
    appid = resolve_appid(p)
    if CDP.available() and appid:
        CDP.set_name(appid, name)
        return tr("rename.done", name=name)
    queue_pending(op="rename", exe=str(p), name=name)
    return tr("rename.queued")


def set_compat_for(game_dir, exe, tool):
    p = game_exe_path(game_dir, exe)
    update_added(str(p), compat=tool or None)
    appid = resolve_appid(p)
    if CDP.available() and appid:
        CDP.set_compat(appid, tool or "")
        return f"Proton: {compat_label(tool)}"
    queue_pending(op="compat", exe=str(p), tool=tool or "")
    return tr("compat.queued")


def drain_pending():
    with STATE_LOCK:
        ops = list(STATE.get("pending") or [])
    if not ops or not CDP.available():
        return 0
    remaining, done = [], 0
    for op in ops:
        try:
            appid = resolve_appid(op["exe"])
            if not appid:
                remaining.append(op)
                continue
            if op["op"] == "rename":
                CDP.set_name(appid, op["name"])
            elif op["op"] == "compat":
                CDP.set_compat(appid, op.get("tool") or "")
            done += 1
            log(f"pending {op['op']} applied for {Path(op['exe']).name}")
        except Exception as e:  # noqa: BLE001
            op["error"] = str(e)
            op["tries"] = op.get("tries", 0) + 1
            if op["tries"] < 20:
                remaining.append(op)
            log(f"pending {op['op']} failed: {e}")
    with STATE_LOCK:
        STATE["pending"] = remaining
        save_state()
    return done


def pending_loop():
    while True:
        time.sleep(20)
        try:
            drain_pending()
        except Exception as e:  # noqa: BLE001
            log(f"pending loop: {e}")
