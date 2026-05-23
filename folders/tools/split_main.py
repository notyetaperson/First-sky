"""One-off: split legacy monolithic main.py into the ``resyco/`` package tree."""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAIN = REPO / "main.py"
PKG = Path(__file__).resolve().parents[1] / "resyco"


def main() -> None:
    lines = MAIN.read_text(encoding="utf-8").splitlines(keepends=True)
    if len(lines) < 8000:
        raise SystemExit(
            "main.py looks already split (too few lines). Restore full main.py before re-running."
        )

    constants_header = '''"""Paths, pools, regex patterns, and tunables for the FirstSky app."""
from __future__ import annotations

__version__ = "1.0.0"

import os
import re
from pathlib import Path
from typing import Any

import requests

'''

    const_body = lines[708:2939]
    text = "".join(const_body)
    text = text.replace(
        "ROOT = Path(__file__).resolve().parent\n",
        "ROOT = Path(__file__).resolve().parent.parent.parent\n",
        1,
    )
    (PKG / "constants.py").write_text(constants_header + text, encoding="utf-8")

    ui_header = '''"""Terminal UI: banner, menus, progress, logging helpers."""
from __future__ import annotations

import contextlib
import importlib.util
import itertools
import os
import shutil
import sys
import threading
import time
from typing import Iterator

from .constants import *

'''
    (PKG / "ui.py").write_text(ui_header + "".join(lines[43:708]), encoding="utf-8")

    impl_header = '''"""Pipelines, Reddit fetch, ffmpeg, TTS, and interactive shells."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import json
import math
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlparse

import requests

from .constants import *
from .ui import *

'''
    # Line 2942 through end of main() (line 11141); excludes trailing ``if __name__`` block.
    (PKG / "impl.py").write_text(impl_header + "".join(lines[2941:11142]), encoding="utf-8")

    (PKG / "cli.py").write_text(
        '''"""CLI entrypoint."""
from __future__ import annotations

from .impl import main

__all__ = ["main"]
''',
        encoding="utf-8",
    )

    (PKG / "__init__.py").write_text(
        '''"""FirstSky application package."""
from __future__ import annotations

from .cli import main
from .constants import __version__

__all__ = ["main", "__version__"]
''',
        encoding="utf-8",
    )

    shim = '''#!/usr/bin/env python3
"""FirstSky — thin launcher for the bundled application package."""

from resyco.cli import main
from resyco.ui import _exit_on_interrupt

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _exit_on_interrupt()
'''
    MAIN.write_text(shim, encoding="utf-8")
    print("Wrote resyco/*.py and main.py shim.")


if __name__ == "__main__":
    PKG.mkdir(exist_ok=True)
    main()
