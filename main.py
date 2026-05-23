#!/usr/bin/env python3
"""FirstSky — thin launcher for the bundled application package."""

from __future__ import annotations

import sys
from pathlib import Path

_folders = Path(__file__).resolve().parent / "folders"
if _folders.is_dir():
    _p = str(_folders.resolve())
    if _p not in sys.path:
        sys.path.insert(0, _p)

from resyco.cli import main

if __name__ == "__main__":
    main()
