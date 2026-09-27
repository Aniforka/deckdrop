#!/usr/bin/env python3
"""The CHANGELOG.md section of a version: for release notes, and a check that it is there.

    python tools/changelog.py 0.4.0      print the section of 0.4.0 (the GitHub release notes)
    python tools/changelog.py --check    fail unless the current version has a full section

A section starts with "## <version> — <YYYY-MM-DD>" and holds "### English" and
"### Русский", each with at least one "- " item. Newest version first.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
HEAD = re.compile(r"^## (\d+(?:\.\d+)+) — (\d{4}-\d{2}-\d{2})\s*$", re.M)
LANGS = ("English", "Русский")


def current_version():
    text = (ROOT / "src" / "deckdrop" / "__init__.py").read_text("utf-8")
    return re.search(r'^__version__ = "([^"]+)"', text, re.M).group(1)


def sections(text=None):
    """[(version, date, body)] in file order."""
    text = CHANGELOG.read_text("utf-8") if text is None else text
    heads = list(HEAD.finditer(text))
    out = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        out.append((m.group(1), m.group(2), text[m.end():end].strip()))
    return out


def section(version, text=None):
    for ver, _, body in sections(text):
        if ver == version:
            return body
    return None


def problems(version, text=None):
    """What is wrong with the changelog for version; empty when it is fine."""
    found = sections(text)
    if not found:
        return ["CHANGELOG.md has no '## <version> — <date>' sections"]
    out = []
    versions = [tuple(int(x) for x in v.split(".")) for v, _, _ in found]
    if versions != sorted(versions, reverse=True) or len(set(versions)) != len(versions):
        out.append("versions must be listed newest first, each once")
    body = section(version, text)
    if body is None:
        return out + [f"no section for {version}: add '## {version} — <date>' at the top of CHANGELOG.md, "
                      "saying what changed in English and in Russian"]
    if found[0][0] != version:
        out.append(f"the section for {version} must be the first one")
    parts = re.split(r"^### (.+?)\s*$", body, flags=re.M)
    langs = dict(zip(parts[1::2], parts[2::2]))
    for lang in LANGS:
        if lang not in langs:
            out.append(f"{version}: no '### {lang}' part")
        elif not re.search(r"^- \S", langs[lang], re.M):
            out.append(f"{version}: '### {lang}' lists no changes ('- ' items)")
    return out


def main():
    if sys.argv[1:] == ["--check"]:
        ver = current_version()
        errors = problems(ver)
        if errors:
            sys.exit("CHANGELOG.md:\n  " + "\n  ".join(errors))
        print(f"CHANGELOG.md describes {ver}")
    elif len(sys.argv) == 2:
        body = section(sys.argv[1])
        if body is None:
            sys.exit(f"no CHANGELOG.md section for {sys.argv[1]}")
        print(body)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
