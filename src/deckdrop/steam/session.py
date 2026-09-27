"""Environment of the running Steam session (fallback when CEF control is off)."""

import os
from pathlib import Path

STEAM_PROCS = ("steam", "steamwebhelper", "gamescope")  # ranked: best env source first
SESSION_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE")


def proc_env(d, keys):
    try:
        raw = (d / "environ").read_bytes()
    except OSError:
        return {}
    got = {}
    for item in raw.split(b"\0"):
        k, sep, v = item.decode("utf-8", "replace").partition("=")
        if sep and k in keys and v:
            got[k] = v
    return got


def session_env(proc="/proc"):
    """Return (env, steam_running), borrowing session vars from the running Steam.

    A systemd user service starts with a bare environment: no DISPLAY,
    WAYLAND_DISPLAY, XDG_RUNTIME_DIR or DBUS_SESSION_BUS_ADDRESS. Without them
    `steam steam://...` cannot reach the client running in Gaming Mode, so read
    those values out of Steam's own /proc entry.
    """
    proc = Path(proc)
    env = dict(os.environ)
    if not proc.is_dir():
        return env, False
    uid = getattr(os, "getuid", lambda: None)()
    running, ranked = False, []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            if uid is not None and d.stat().st_uid != uid:
                continue
            comm = (d / "comm").read_text(errors="replace").strip()
        except OSError:
            continue
        if comm not in STEAM_PROCS:
            continue
        running = running or comm in ("steam", "steamwebhelper")
        got = proc_env(d, SESSION_KEYS)
        if got.get("XDG_RUNTIME_DIR") and (got.get("DISPLAY") or got.get("WAYLAND_DISPLAY")):
            ranked.append((STEAM_PROCS.index(comm), got))
    if ranked:
        env.update(min(ranked, key=lambda r: r[0])[1])
    return env, running
