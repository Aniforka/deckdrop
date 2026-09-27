#!/usr/bin/env python3
"""Run DeckDrop straight from src/, without building: python tools/dev.py [args]

Takes the same arguments as the built deckdrop.py, except --install (build first).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from deckdrop.app import main  # noqa: E402

main()
