"""Settings -> Performance: a self-check of everything DeckDrop relies on, and measurements of
how much it loads the Deck.

Both run on the Deck itself when the user presses a button. SSH is off on a stock Deck, so
numbers taken anywhere else would say little about it; the page shows the result and can copy
it as text, e.g. for a bug report.

Texts come back already translated; values that the page formats itself (sizes, speeds,
durations) come as numbers with a unit.
"""

import json
import os
import platform
import re
import secrets
import shutil
import statistics
import threading
import time
import zipfile
from pathlib import Path

from . import __version__, bundle
from .aes import AesCtr, _load_libcrypto, aes_cbc_encrypt
from .config import CACHE_DIR, FFMPEG, RECENT_LOG, STARTED, STATE_FILE, STEAM_ROOT, UPDATE_URL_DEFAULT
from .i18n import carry, tr
from .jobs import ACTIVE, JOBS, LOCK
from .net import net_open, net_reason, proxy_url
from .state import STATE, STATE_LOCK
from .steam.cdp import CDP
from .steam.library import shortcuts_index, userdata_dirs
from .storage import disks

PENDING_THREAD = "deckdrop-pending"     # name of the background loop started by app.main
GB = 1 << 30
MB = 1 << 20
LOW_SPACE, NO_SPACE = 5 * GB, 1 * GB
LEFTOVERS_BIG = 1 * GB
PROBE_URL = "https://github.com"
VNDB_PROBE = "https://api.vndb.org/kana/schema"
BENCH_LOCK = threading.Lock()
LAST = {}                               # "check" / "bench" -> {"at", "result"}: what the report downloads
ICONS = {"ok": "✅", "warn": "⚠️", "fail": "❌", "info": "ℹ️"}
MODELS = {"Jupiter": "Steam Deck LCD", "Galileo": "Steam Deck OLED"}   # DMI product names
BENCH_DISK_BYTES = 64 * MB              # written with fsync to every disk, then removed
BENCH_ZIP_PARTS = 4                     # files of 4 MB each: half random, half text
# log lines that point at something broken; failed downloads are the user's business and are
# shown on the job itself, so "job N error" lines do not count
TROUBLE = re.compile(r"\b(fail(ed)?|error|traceback|exception)\b", re.I)


def fmt_size(n):
    """Same style as fmt() on the page."""
    if n < 1e6:
        return f"{n / 1e3:.0f} KB"
    return f"{n / 1e6:.1f} MB" if n < 1e9 else f"{n / 1e9:.2f} GB"


def duration(seconds):
    seconds = int(seconds)
    if seconds < 3600:
        return tr("unit.duration_min", m=seconds // 60)
    return tr("unit.duration", h=seconds // 3600, m=seconds % 3600 // 60)


def item(status, title, detail=""):
    return {"status": status, "title": title, "detail": detail}


def probe_write(folder):
    """Create and remove a small file in folder; None if that works, else the reason."""
    p = Path(folder) / f".deckdrop-probe-{secrets.token_hex(4)}"
    try:
        p.write_bytes(b"ok")
        p.unlink()
        return None
    except OSError as e:
        try:
            p.unlink()
        except OSError:
            pass
        return e.strerror or str(e)


def tree_size(p):
    p = Path(p)
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _, files in os.walk(p):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


# ---------------------------------------------------------------------------- self-check

def check_version():
    return item("info", tr("perf.c.version"),
                tr("perf.c.version.detail", version=__version__, python=platform.python_version(),
                   uptime=duration(time.time() - STARTED)))


def check_service():
    title = tr("perf.c.service")
    units = Path.home() / ".config" / "systemd" / "user"
    unit = units / "deckdrop.service"
    if not unit.is_file():
        return item("info", title, tr("perf.c.service.manual"))
    here = str(Path(bundle.PATH).resolve()) if bundle.PATH else None
    try:
        text = unit.read_text("utf-8", "replace")
    except OSError as e:
        return item("warn", title, str(e))
    if here and here not in text:
        return item("warn", title, tr("perf.c.service.other_file"))
    if not (units / "default.target.wants" / "deckdrop.service").exists():
        return item("warn", title, tr("perf.c.service.not_enabled"))
    return item("ok", title, tr("perf.c.service.ok"))


def check_background():
    alive = any(t.name == PENDING_THREAD and t.is_alive() for t in threading.enumerate())
    return item("ok" if alive else "fail", tr("perf.c.background"),
                tr("perf.c.background.ok") if alive else tr("perf.c.background.dead"))


def check_state():
    title = tr("perf.c.state")
    try:
        if STATE_FILE.exists():
            json.loads(STATE_FILE.read_text("utf-8"))
    except ValueError:
        return item("fail", title, tr("perf.c.state.broken", path=str(STATE_FILE)))
    except OSError as e:
        return item("fail", title, tr("perf.c.state.unreadable", error=e.strerror or str(e)))
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    err = probe_write(STATE_FILE.parent)
    if err:
        return item("fail", title, tr("perf.c.state.readonly", error=err))
    return item("ok", title, tr("perf.c.state.ok", path=str(STATE_FILE)))


def check_disks():
    out = []
    for d in disks():
        title = tr("perf.c.disk", label=d["label"])
        root = Path(d["root"])
        if not root.is_dir():
            if d["id"] == "internal":
                out.append(item("warn", title, tr("perf.c.disk.missing", path=str(root))))
            else:
                out.append(item("info", title, tr("perf.c.disk.later", path=str(root))))
            continue
        err = probe_write(root)
        if err:
            out.append(item("fail", title, tr("perf.c.disk.readonly", error=err)))
            continue
        free = d["free"] or 0
        size = fmt_size(free)
        if free < NO_SPACE:
            out.append(item("fail", title, tr("perf.c.disk.full", free=size)))
        elif free < LOW_SPACE:
            out.append(item("warn", title, tr("perf.c.disk.low", free=size)))
        else:
            out.append(item("ok", title, tr("perf.c.disk.ok", free=size)))
    return out


def leftovers():
    """Half-written downloads, Mega staging folders and unpacked patches nobody uses any more."""
    with LOCK:
        busy = {str(Path(j.work)) for j in JOBS.values() if j.work and j.status in ACTIVE}
    found = []
    for d in disks():
        inbox = Path(d["root"]) / "_inbox"
        if not inbox.is_dir():
            continue
        for p in inbox.iterdir():
            if (p.name.endswith(".part") or p.name.startswith((".mega-", ".deckdrop-upload-"))) \
                    and str(p) not in busy:
                found.append(p)
    patches = CACHE_DIR / "patches"
    if patches.is_dir():
        found += list(patches.iterdir())
    return found


def check_leftovers():
    title = tr("perf.c.leftovers")
    err = probe_write(CACHE_DIR) if CACHE_DIR.is_dir() else None
    if err:
        return item("fail", title, tr("perf.c.cache.readonly", path=str(CACHE_DIR), error=err))
    items = leftovers()
    size = sum(tree_size(p) for p in items)
    if not items:
        return item("ok", title, tr("perf.c.leftovers.none"))
    return item("warn" if size >= LEFTOVERS_BIG else "info", title,
                tr("perf.c.leftovers.some", n=len(items), size=fmt_size(size)))


def check_steam():
    title = tr("perf.c.steam")
    users = userdata_dirs() if (STEAM_ROOT / "userdata").is_dir() else []
    if not users:
        return item("warn", title, tr("perf.c.steam.missing", path=str(STEAM_ROOT)))
    try:
        n = len(shortcuts_index())
    except Exception as e:  # noqa: BLE001
        return item("fail", title, tr("perf.c.steam.broken", error=str(e)))
    return item("ok", title, tr("perf.c.steam.ok", n=n))


def check_steam_control():
    title = tr("perf.c.cdp")
    st = CDP.status()
    if st["available"]:
        return item("ok", title, tr("cdp.long.live"))
    if not st["enabled"]:
        return item("info", title, tr("cdp.long.off"))
    if st["marker"]:
        return item("warn", title, tr("cdp.long.after_reboot"))
    return item("warn", title, tr("cdp.long.no_steam"))


def check_pending():
    title = tr("perf.c.pending")
    with STATE_LOCK:
        ops = list(STATE.get("pending") or [])
    stuck = [op for op in ops if op.get("tries", 0) >= 5]
    if stuck:
        return item("warn", title, tr("perf.c.pending.stuck", n=len(stuck),
                                      error=stuck[-1].get("error") or "?"))
    if ops:
        return item("info", title, tr("info.pending_n", n=len(ops)))
    return item("ok", title, tr("perf.c.pending.none"))


def check_unpack():
    title = tr("perf.c.unpack")
    tools = [x for x in ("7z", "bsdtar", "unrar") if shutil.which(x)]
    if any(x in tools for x in ("7z", "bsdtar")):
        return item("ok", title, tr("perf.c.unpack.ok", tools=", ".join(tools)))
    return item("warn", title, tr("perf.c.unpack.zip_only"))


def check_crypto():
    title = tr("perf.c.crypto")
    if _load_libcrypto():
        return item("ok", title, tr("perf.c.crypto.ok"))
    return item("warn", title, tr("perf.c.crypto.slow"))


def check_ffmpeg():
    return item("ok" if FFMPEG else "info", tr("perf.c.ffmpeg"),
                tr("ffmpeg.found") if FFMPEG else tr("ffmpeg.missing_long"))


def reach(proxy, url=PROBE_URL):
    """(milliseconds, None) if url answers at all, else (None, reason)."""
    t0 = time.time()
    try:
        with net_open(url, timeout=6, proxy=proxy, method="HEAD") as r:
            r.read(0)
        return int((time.time() - t0) * 1000), None
    except Exception as e:  # noqa: BLE001
        code = getattr(e, "code", None)
        if code:                               # any HTTP answer means the way out works
            return int((time.time() - t0) * 1000), None
        return None, net_reason(e)


def check_internet():
    title = tr("perf.c.net")
    ms, err = reach(None)
    parts = [tr("perf.c.net.line", mode=tr("net.direct"),
                result=tr("unit.ms", n=ms) if ms is not None else err)]
    ok = ms is not None
    px = proxy_url()
    if px:
        pms, perr = reach(px)
        parts.append(tr("perf.c.net.line", mode=tr("net.via_proxy"),
                        result=tr("unit.ms", n=pms) if pms is not None else perr))
        ok = ok or pms is not None
    return item("ok" if ok else "warn", title, "; ".join(parts))


RELEASE_ASSET = re.compile(r"^(https://github\.com/[^/]+/[^/]+)/releases/(?:latest/)?download/", re.I)


def update_probe(url):
    """What to knock on to see that updates can be fetched. Not a release file itself: GitHub counts
    even a HEAD on it as a download, so every self-check (and every test run) would add one. The
    releases page answers from the same place without touching the counter."""
    m = RELEASE_ASSET.match(url)
    return m.group(1) + "/releases/latest" if m else url


def check_update():
    title = tr("perf.c.update")
    url = STATE.get("update_url") or UPDATE_URL_DEFAULT
    ms, err = reach(None, update_probe(url))
    if ms is None:
        return item("warn", title, tr("perf.c.update.fail", url=url, error=err))
    return item("ok", title, tr("perf.c.update.ok", url=url, ms=ms))


def check_vndb():
    title = tr("perf.c.vndb")
    if not STATE.get("vndb_auto"):
        return item("info", title, tr("perf.c.vndb.off"))
    px = proxy_url()
    ms, err = reach(px, VNDB_PROBE)
    mode = tr("net.via_proxy") if px else tr("net.direct")
    if ms is None:
        return item("warn", title, tr("perf.c.vndb.fail", mode=mode, error=err))
    return item("ok", title, tr("perf.c.vndb.ok", mode=mode, ms=ms))


def check_games():
    """Shortcuts made by DeckDrop and imported games whose files are gone (deleted, or the card is out)."""
    title = tr("perf.c.games")
    with STATE_LOCK:
        exes = [e for e, rec in (STATE.get("added") or {}).items() if rec.get("appid") or rec.get("via")]
        imported = list(STATE.get("imported") or [])
    gone = [e for e in exes if not Path(e).exists()] + [d for d in imported if not Path(d).is_dir()]
    if not gone:
        return item("ok", title, tr("perf.c.games.ok", n=len(exes) + len(imported)))
    return item("warn", title, tr("perf.c.games.gone", n=len(gone), example=Path(gone[0]).name))


def check_log():
    title = tr("perf.c.log")
    bad = [msg for _, msg in list(RECENT_LOG) if TROUBLE.search(msg) and not msg.startswith("job ")]
    if not bad:
        return item("ok", title, tr("perf.c.log.clean"))
    return item("warn", title, tr("perf.c.log.errors", n=len(bad), last=bad[-1][:160]))


CHECKS = (check_version, check_service, check_background, check_state, check_disks, check_leftovers,
          check_steam, check_steam_control, check_pending, check_games, check_unpack, check_crypto,
          check_ffmpeg, check_internet, check_update, check_vndb, check_log)


def run_check(check):
    try:
        res = check()
    except Exception as e:  # noqa: BLE001      a broken check is a finding, not a crash
        res = item("fail", check.__name__, str(e))
    return res if isinstance(res, list) else [res]


def self_check():
    """Every check, in the order the page shows them, plus how many of each status.

    The checks run side by side: the network ones wait for time-outs when the Deck is offline,
    and one after another they would keep the page waiting for half a minute.
    """
    results = {}

    def work(check):
        results[check] = run_check(check)

    threads = [threading.Thread(target=carry(work), args=(c,), daemon=True) for c in CHECKS]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    items = []
    for check in CHECKS:
        items += results.get(check) or [item("fail", check.__name__, tr("perf.c.timeout"))]
    counts = {s: sum(1 for i in items if i["status"] == s) for s in ("ok", "warn", "fail", "info")}
    return remember("check", {"items": items, "counts": counts})


# ---------------------------------------------------------------------------- measurements

def row(label, value, unit="text", note=""):
    return {"label": label, "value": value, "unit": unit, "note": note}


def read_kv(path, sep=":"):
    out = {}
    try:
        for line in Path(path).read_text("utf-8", "replace").splitlines():
            k, _, v = line.partition(sep)
            if v:
                out.setdefault(k.strip(), v.strip())
    except OSError:
        pass
    return out


def kb_value(v):
    """'123456 kB' from /proc -> bytes."""
    m = re.match(r"(\d+)", v or "")
    return int(m.group(1)) * 1024 if m else None


def cpu_seconds():
    t = os.times()
    return t.user + t.system


def system_busy():
    """(busy, total) jiffies of the whole machine, from /proc/stat; None where there is none."""
    try:
        with open("/proc/stat") as f:
            nums = [int(x) for x in f.readline().split()[1:]]
    except (OSError, ValueError):
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return sum(nums) - idle, sum(nums)


def battery():
    """(watts, status, percent) of the Deck's battery from /sys, or None off a handheld."""
    for b in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        def num(name):
            try:
                return int((b / name).read_text().strip())
            except (OSError, ValueError):
                return None
        try:
            status = (b / "status").read_text().strip()
        except OSError:
            continue
        power = num("power_now")
        if power is None and num("current_now") is not None and num("voltage_now") is not None:
            power = num("current_now") * num("voltage_now") // 1_000_000
        if power is not None:
            return power / 1e6, status, num("capacity")
    return None


def device_model():
    """"Steam Deck OLED" and the like, from the firmware's product name; None off a Deck-like machine."""
    try:
        name = Path("/sys/class/dmi/id/product_name").read_text().strip()
    except OSError:
        return None
    return MODELS.get(name, name) or None


def section_system():
    info = read_kv("/proc/cpuinfo")
    mem = read_kv("/proc/meminfo")
    osr = read_kv("/etc/os-release", "=")
    cpu = info.get("model name") or platform.processor() or platform.machine()
    model = device_model()
    rows = [row(tr("perf.b.model"), model)] if model else []
    rows += [row(tr("perf.b.cpu"), tr("perf.b.cpu.value", model=cpu, n=os.cpu_count() or 1)),
            row(tr("perf.b.ram"), kb_value(mem.get("MemTotal")), "bytes",
                tr("perf.b.ram.available", size=fmt_size(kb_value(mem.get("MemAvailable")) or 0))
                if mem.get("MemAvailable") else ""),
            row(tr("perf.b.os"), osr.get("PRETTY_NAME", "").strip('"') or platform.platform()),
            row("Python", platform.python_version())]
    if hasattr(os, "getloadavg"):
        rows.append(row(tr("perf.b.loadavg"), " / ".join(f"{x:.2f}" for x in os.getloadavg()),
                        note=tr("perf.b.loadavg.note")))
    return {"title": tr("perf.s.system"), "rows": rows}


def section_process():
    st = read_kv("/proc/self/status")
    uptime = max(time.time() - STARTED, 0.001)
    rss = kb_value(st.get("VmRSS"))
    total = kb_value(read_kv("/proc/meminfo").get("MemTotal"))
    try:
        files = len(os.listdir("/proc/self/fd"))
    except OSError:
        files = None
    rows = [row(tr("perf.b.rss"), rss, "bytes",
                tr("perf.b.rss.share", pct=f"{rss * 100 / total:.1f}") if rss and total else ""),
            row(tr("perf.b.peak"), kb_value(st.get("VmHWM")), "bytes"),
            row(tr("perf.b.threads"), int(st["Threads"]) if st.get("Threads") else threading.active_count()),
            row(tr("perf.b.files"), files),
            row(tr("perf.b.uptime"), int(uptime), "duration"),
            row(tr("perf.b.cpu_avg"), round(cpu_seconds() * 100 / uptime, 2), "pct",
                tr("perf.b.cpu_avg.note"))]
    return {"title": tr("perf.s.process"), "rows": rows}


def section_load(window=3.0):
    with LOCK:
        active = sum(1 for j in JOBS.values() if j.status in ACTIVE)
    b0 = battery()
    c0, s0, w0 = cpu_seconds(), system_busy(), time.time()
    time.sleep(window)
    c1, s1, w1 = cpu_seconds(), system_busy(), time.time()
    b1 = battery()
    rows = [row(tr("perf.b.load_self"), round((c1 - c0) * 100 / (w1 - w0), 2), "pct",
                tr("perf.b.load_self.note", jobs=active))]
    if s0 and s1 and s1[1] > s0[1]:
        rows.append(row(tr("perf.b.load_all"), round((s1[0] - s0[0]) * 100 / (s1[1] - s0[1]), 1), "pct",
                        tr("perf.b.load_all.note", n=os.cpu_count() or 1)))
    if b0 and b1:
        watts, status, pct = (b0[0] + b1[0]) / 2, b1[1], b1[2]
        if status == "Discharging":
            note = tr("perf.b.power.battery", pct=pct if pct is not None else "?")
        elif status == "Charging":
            note = tr("perf.b.power.charging")
        else:
            note = tr("perf.b.power.plugged")
        rows.append(row(tr("perf.b.power"), round(watts, 1), "watts", note))
    return {"title": tr("perf.s.load", n=int(window)), "rows": rows}


def section_page(state_payload, runs=5):
    walls, cpus, size, games = [], [], 0, 0
    for _ in range(runs):
        c0, w0 = cpu_seconds(), time.perf_counter()
        obj = state_payload()
        body = json.dumps(obj, ensure_ascii=False).encode()
        walls.append((time.perf_counter() - w0) * 1000)
        cpus.append((cpu_seconds() - c0) * 1000)
        size, games = len(body), len(obj.get("games") or [])
    wall = statistics.median(walls)
    # CPU clocks tick coarsely on some systems: a zero reading falls back to the wall time
    cpu = statistics.median(cpus) or wall
    rows = [row(tr("perf.b.page_time"), round(wall, 1), "ms", tr("perf.b.page_time.note", n=games)),
            row(tr("perf.b.page_size"), size, "bytes"),
            # the page asks once a second, so the CPU milliseconds of one answer are the share of a core
            row(tr("perf.b.page_share"), round(cpu / 10, 2), "pct", tr("perf.b.page_share.note"))]
    return {"title": tr("perf.s.page"), "rows": rows}


def rate(fn, chunk, budget):
    """MB/s of fn over repeated chunks until the time budget is spent (at least one round)."""
    n, t0 = 0, time.perf_counter()
    while True:
        fn()
        n += 1
        dt = time.perf_counter() - t0
        if dt >= budget:
            return round(n * chunk / MB / dt, 1)


def bench_zip(work):
    """Unpacking speed of a zip with the built-in unpacker, MB of content per second."""
    src = work / "bench.zip"
    part = os.urandom(2 * MB) + (b"DeckDrop benchmark line of text. " * 64000)[:2 * MB]
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        for i in range(BENCH_ZIP_PARTS):
            z.writestr(f"part{i}.bin", part)
    out = work / "out"
    t0 = time.perf_counter()
    with zipfile.ZipFile(src) as z:
        z.extractall(out)
    dt = time.perf_counter() - t0
    return round(BENCH_ZIP_PARTS * len(part) / MB / dt, 1)


def bench_disk(root):
    """Sequential write speed with fsync, MB/s; the file is removed right after."""
    p = Path(root) / f".deckdrop-bench-{secrets.token_hex(4)}"
    block = os.urandom(4 * MB)
    try:
        t0 = time.perf_counter()
        with open(p, "wb") as f:
            for _ in range(BENCH_DISK_BYTES // len(block)):
                f.write(block)
            f.flush()
            os.fsync(f.fileno())
        dt = time.perf_counter() - t0
        return round(BENCH_DISK_BYTES / MB / dt, 1)
    finally:
        try:
            p.unlink()
        except OSError:
            pass


def section_speed():
    rows = []
    key, iv = os.urandom(16), os.urandom(16)
    fast = bool(_load_libcrypto())
    chunk = MB if fast else 16 * 1024
    data = os.urandom(chunk)
    ctr = AesCtr(key, iv)
    try:
        engine = "libcrypto" if fast else tr("perf.b.no_libcrypto")
        rows.append(row(tr("perf.b.mega_decrypt"), rate(lambda: ctr.xor(data), chunk, 0.4), "mbps", engine))
    finally:
        ctr.close()
    rows.append(row(tr("perf.b.mega_verify"), rate(lambda: aes_cbc_encrypt(key, data), chunk, 0.4), "mbps",
                    tr("perf.b.mega_verify.note")))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    work = CACHE_DIR / f"bench-{secrets.token_hex(4)}"
    try:
        work.mkdir()
        rows.append(row(tr("perf.b.zip"), bench_zip(work), "mbps", tr("perf.b.zip.note")))
    except OSError as e:
        rows.append(row(tr("perf.b.zip"), None, "mbps", e.strerror or str(e)))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    for d in disks():
        label = tr("perf.b.disk_write", label=d["label"])
        root = Path(d["root"])
        if not root.is_dir():
            rows.append(row(label, None, "mbps", tr("perf.b.disk_skip.missing")))
        elif (d["free"] or 0) < BENCH_DISK_BYTES + NO_SPACE:
            rows.append(row(label, None, "mbps", tr("perf.b.disk_skip.full")))
        else:
            try:
                rows.append(row(label, bench_disk(root), "mbps", tr("perf.b.disk_write.note")))
            except OSError as e:
                rows.append(row(label, None, "mbps", e.strerror or str(e)))
    return {"title": tr("perf.s.speed"), "rows": rows}


def benchmark(state_payload):
    """All measurements, about ten seconds; one at a time. state_payload builds what /api/state sends."""
    if not BENCH_LOCK.acquire(blocking=False):
        raise ValueError(tr("perf.busy"))
    try:
        t0 = time.time()
        # the idle load first, before this very measurement starts to load the Deck
        sections = [section_load(), section_system(), section_process(), section_page(state_payload),
                    section_speed()]
        return remember("bench", {"sections": sections, "seconds": round(time.time() - t0, 1)})
    finally:
        BENCH_LOCK.release()


# ---------------------------------------------------------------------------- the report file

def remember(kind, result):
    LAST[kind] = {"at": time.time(), "result": result}
    return result


def value_text(r):
    """A measured value as the page shows it (perfVal() in app.js)."""
    v, unit = r["value"], r["unit"]
    if v is None:
        return "—"
    if unit == "bytes":
        return fmt_size(v)
    if unit == "mbps":
        return tr("unit.mb_s", n=v)
    if unit == "ms":
        return tr("unit.ms", n=v)
    if unit == "watts":
        return tr("unit.watts", n=v)
    if unit == "pct":
        return f"{v}%"
    if unit == "duration":
        return duration(v)
    return str(v)


def cell(text):
    """Text that stays inside one Markdown table cell."""
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def report_md(kind):
    """(file name, Markdown) of the last self-check or measurement, from the template web/report.md."""
    if kind not in ("check", "bench"):
        raise ValueError(kind)
    rec = LAST.get(kind)
    if not rec:
        raise FileNotFoundError(tr("perf.md.none"))
    res, at = rec["result"], time.localtime(rec["at"])
    if kind == "check":
        c = res["counts"]
        title = tr("perf.md.title.check")
        summary = (tr("perf.summary.fail", n=c["fail"]) if c["fail"] else
                   tr("perf.summary.warn", n=c["warn"]) if c["warn"] else tr("perf.summary.ok"))
        body = "\n".join(f"- {ICONS[i['status']]} **{i['title']}**" + (f" — {i['detail']}" if i["detail"] else "")
                         for i in res["items"])
    else:
        title = tr("perf.md.title.bench")
        summary = tr("perf.took", n=res["seconds"])
        tables = []
        for s in res["sections"]:
            lines = [f"## {s['title']}", "",
                     f"| {tr('perf.md.metric')} | {tr('perf.md.value')} | {tr('perf.md.note')} |", "|---|---|---|"]
            lines += [f"| {cell(r['label'])} | **{cell(value_text(r))}** | {cell(r['note'])} |" for r in s["rows"]]
            tables.append("\n".join(lines))
        body = "\n\n".join(tables)
    meta = " · ".join(x for x in (f"DeckDrop {__version__}", device_model(),
                                   time.strftime("%Y-%m-%d %H:%M", at)) if x)
    fill = {"title": title, "meta": meta, "summary": summary, "body": body, "footer": tr("perf.md.footer")}
    template = re.sub(r"<!--.*?-->\s*", "", bundle.resource("web/report.md"), count=1, flags=re.S)
    text = re.sub(r"\{\{(\w+)\}\}", lambda m: fill.get(m.group(1), m.group(0)), template)
    name = f"deckdrop-{'selfcheck' if kind == 'check' else 'load'}-{time.strftime('%Y%m%d-%H%M', at)}.md"
    return name, text
