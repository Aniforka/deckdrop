#!/usr/bin/env python3
"""Build the single-file deckdrop.py from the package in src/deckdrop.

    python tools/build.py                  write dist/deckdrop.py
    python tools/build.py -o FILE          write FILE instead

The result is what users install and update from: one stdlib-only script. It carries
every module of the package as source text plus the web files, and a small importer
that serves them as the `deckdrop` package, so the code runs exactly as it does from
src/ (tracebacks name the original files and lines).

Copies already installed accept an update only if the file mentions "DeckDrop" in its
first 3000 bytes and compiles; they read the version and the default port with the
regexes __version__ = "x" and DECKDROP_PORT", "n". The header below keeps all of that.
"""
import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "src" / "deckdrop"
DATA_EXTS = (".html", ".css", ".js")

BOOT = r'''
import importlib.abc
import importlib.util
import linecache
import sys


class _BundleImporter(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Serves the `deckdrop` package from the sources embedded above."""

    def find_spec(self, name, path=None, target=None):
        if name not in _MODULES:
            return None
        is_pkg, rel, _ = _MODULES[name]
        spec = importlib.util.spec_from_loader(name, self, origin=rel, is_package=is_pkg)
        spec.has_location = True     # sets __file__ (to a relative, not existing path)
        return spec

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        _, rel, src = _MODULES[module.__name__]
        # register the source so tracebacks show the lines of src/deckdrop/<rel>
        linecache.cache[rel] = (len(src), None, src.splitlines(True), rel)
        exec(compile(src, rel, "exec"), module.__dict__)


sys.meta_path.insert(0, _BundleImporter())

import deckdrop.bundle  # noqa: E402

deckdrop.bundle.PATH = __file__
deckdrop.bundle.FILES = _FILES

if __name__ == "__main__":
    from deckdrop.app import main
    main()
'''


def literal(text):
    """Readable literal for embedded text: a raw triple-quoted string when that is exact."""
    if "'''" not in text and text.endswith("\n") and "\r" not in text:
        return "r'''" + text + "'''"
    return repr(text)


def build():
    init = (PKG / "__init__.py").read_text("utf-8")
    version = re.search(r'^__version__ = "([^"]+)"', init, re.M).group(1)
    doc = re.match(r'"""(.*?)"""', init, re.S).group(1)
    port = re.search(r'"DECKDROP_PORT",\s*"(\d+)"', (PKG / "config.py").read_text("utf-8")).group(1)

    modules, files = [], []
    for p in sorted(PKG.rglob("*")):
        rel = p.relative_to(PKG).as_posix()
        if "__pycache__" in rel or not p.is_file():
            continue
        if p.suffix == ".py":
            parts = ["deckdrop"] + rel[:-3].split("/")
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts.pop()
            if parts[-1] == "__main__":
                continue
            modules.append((".".join(parts), is_pkg, "deckdrop/" + rel, p.read_text("utf-8")))
        elif p.suffix in DATA_EXTS:
            files.append((rel, p.read_text("utf-8")))

    out = ["#!/usr/bin/env python3\n",
           f'"""{doc.rstrip()}\n\n'
           "This file is built from src/deckdrop by tools/build.py: edit the sources, not this file.\n"
           '"""\n',
           f'__version__ = "{version}"\n',
           f'DEFAULT_PORT = ("DECKDROP_PORT", "{port}")   # read by the updater of installed copies\n\n',
           "_MODULES = {\n"]
    for name, is_pkg, rel, src in modules:
        out.append(f"    {name!r}: ({is_pkg}, {rel!r}, {literal(src)}),\n")
    out.append("}\n\n_FILES = {\n")
    for rel, text in files:
        out.append(f"    {rel!r}: {literal(text)},\n")
    out.append("}\n")
    out.append(BOOT)
    return "".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", default=str(ROOT / "dist" / "deckdrop.py"))
    args = ap.parse_args()
    text = build()
    compile(text, "deckdrop.py", "exec")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    out.chmod(0o755)
    print(f"built {out} ({len(text.encode()) // 1024} KiB, {len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
