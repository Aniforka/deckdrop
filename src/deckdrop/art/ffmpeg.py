"""ffmpeg helpers (optional, better quality covers and logos)."""

import os
import re
import secrets
import subprocess
from pathlib import Path

from ..config import CACHE_DIR, FFMPEG
from ..i18n import tr


def ff_run(inp, in_ext, args, out_ext, timeout=120):
    """Run ffmpeg on bytes -> bytes (via temp files). Raises RuntimeError on failure."""
    if not FFMPEG:
        raise RuntimeError(tr("ffmpeg.missing"))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = secrets.token_hex(4)
    src = CACHE_DIR / f"ff_{tag}_in.{in_ext}"
    dst = CACHE_DIR / f"ff_{tag}_out.{out_ext}"
    try:
        src.write_bytes(inp)
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(src)] + args + [str(dst)],
                             capture_output=True, text=True, errors="replace", timeout=timeout)
        if res.returncode != 0 or not dst.is_file():
            raise RuntimeError("ffmpeg: " + res.stderr.strip()[-300:])
        return dst.read_bytes()
    finally:
        for f in (src, dst):
            try:
                f.unlink()
            except OSError:
                pass


def ff_cover(inp, in_ext, w, h):
    """Scale-to-fill and centre-crop to exactly w x h (jpg)."""
    return ff_run(inp, in_ext, ["-vf", f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}",
                                "-frames:v", "1", "-q:v", "3"], "jpg")


def ff_fit_blur(inp, in_ext, w, h):
    """Fit inside w x h over a blurred, filled copy of itself (jpg)."""
    vf = (f"split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[bg];"
          f"[b]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")
    return ff_run(inp, in_ext, ["-vf", vf, "-frames:v", "1", "-q:v", "3"], "jpg")


def find_font(text=""):
    cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", text))
    dirs = [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts", Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"]
    prefs = (["NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf", "NotoSansCJK-Regular.ttc"] if cjk else []) + \
            ["DejaVuSans-Bold.ttf", "NotoSans-Bold.ttf", "LiberationSans-Bold.ttf", "arialbd.ttf", "segoeuib.ttf"]
    found = {}
    for d in dirs:
        if d.is_dir():
            for f in d.rglob("*"):
                if f.suffix.lower() in (".ttf", ".otf", ".ttc") and f.name not in found:
                    found[f.name] = f
    for name in prefs:
        if name in found:
            return found[name]
    return next(iter(found.values()), None)


def ff_logo(title):
    """Transparent PNG with the title as text (Steam 'logo' asset)."""
    font = find_font(title)
    if not font:
        raise RuntimeError(tr("ffmpeg.no_font"))
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tag = secrets.token_hex(4)
    txt = CACHE_DIR / f"logo_{tag}.txt"
    out = CACHE_DIR / f"logo_{tag}.png"
    size = max(48, min(120, int(1700 / max(len(title), 1))))
    fontfile = str(font).replace("\\", "/").replace(":", "\\:")
    try:
        txt.write_text(title, "utf-8")
        vf = (f"drawtext=fontfile='{fontfile}':textfile='{str(txt).replace(chr(92), '/').replace(':', chr(92) + ':')}'"
              f":fontsize={size}:fontcolor=white:borderw=4:bordercolor=black@0.55:x=(w-text_w)/2:y=(h-text_h)/2")
        res = subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black@0.0:s=1280x400,format=rgba",
                              "-vf", vf, "-frames:v", "1", str(out)], capture_output=True, text=True, errors="replace", timeout=60)
        if res.returncode != 0 or not out.is_file():
            raise RuntimeError("ffmpeg drawtext: " + res.stderr.strip()[-200:])
        return out.read_bytes()
    finally:
        for f in (txt, out):
            try:
                f.unlink()
            except OSError:
                pass
