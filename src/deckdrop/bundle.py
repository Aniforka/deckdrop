"""Where this copy runs from: the single-file build or the source tree.

The build (tools/build.py) sets PATH and FILES from its bootstrap before anything
else is imported; from the source tree both stay None.
"""
from pathlib import Path

PATH = None    # the built deckdrop.py this copy runs from; None when run from src/
FILES = None   # data files embedded in the build: {"web/app.js": text, ...}


def resource(rel):
    """Text of a data file shipped with the package, e.g. "web/app.js"."""
    if FILES is not None:
        return FILES[rel]
    return (Path(__file__).parent / rel).read_text("utf-8")
