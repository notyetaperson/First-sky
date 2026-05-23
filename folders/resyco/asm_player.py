"""Launch the Ascii-Media-Player sibling project from the FirstSky menu or CLI.

Looks for a checkout next to this repo (``../Ascii-Media-Player``), with a
fallback to ``../ascii-player``. Override with:

- ``ASCII_MEDIA_PLAYER_ROOT`` or ``ASM_ROOT`` (absolute or relative path)

Optional full command override (shell-style token list):

- ``ASM_ENTRY`` e.g. ``python -m mypkg`` or ``C:\\Python311\\python.exe main.py``
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def _cuetilities_root() -> Path:
    # folders/resyco/asm_player.py -> Cuetilities
    return Path(__file__).resolve().parent.parent.parent


def _curses_available() -> bool:
    try:
        import curses  # noqa: F401
    except ImportError:
        return False
    return True


def resolve_ascii_media_player_root() -> Path:
    raw = (os.environ.get("ASCII_MEDIA_PLAYER_ROOT") or os.environ.get("ASM_ROOT") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    parent = _cuetilities_root().parent
    for name in ("Ascii-Media-Player", "ascii-player"):
        p = (parent / name).resolve()
        if p.is_dir():
            return p
    return (parent / "Ascii-Media-Player").resolve()


def _script_usable(name: str, root: Path) -> bool:
    if name == "curses-player.py" and not _curses_available():
        return False
    # new-player.py hard-codes main("star-wars.ascii"); skip if the asset is missing.
    if name == "new-player.py" and not (root / "star-wars.ascii").is_file():
        return False
    return True


def _maybe_fetch_ascii_player_demo_film(root: Path) -> bool:
    """
    The sibling ``ascii-player`` demo needs ``star-wars.ascii`` for ``new-player.py``.
    If missing, run ``copy_film.py`` once to download it (network required).

    Set ``ASM_NO_AUTO_FETCH=1`` to skip.
    """
    if (os.environ.get("ASM_NO_AUTO_FETCH") or "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    target = root / "star-wars.ascii"
    if target.is_file():
        return False
    copy_f = root / "copy_film.py"
    new_p = root / "new-player.py"
    if not copy_f.is_file() or not new_p.is_file():
        return False
    print("FirstSky: fetching ascii-player demo (star-wars.ascii) — one-time, needs network…", flush=True)
    r = subprocess.run(
        [sys.executable, str(copy_f)],
        cwd=str(root),
        env=os.environ.copy(),
    )
    return r.returncode == 0 and target.is_file()


def _discover_python_entry(root: Path) -> list[str] | None:
    # Put generic names first; demo/tty-specific scripts last.
    for name in (
        "main.py",
        "run.py",
        "cli.py",
        "app.py",
        "curses-player.py",
        "new-player.py",
    ):
        p = root / name
        if not p.is_file():
            continue
        if not _script_usable(name, root):
            continue
        return [sys.executable, str(p)]
    try:
        for child in sorted(root.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            if (child / "__main__.py").is_file():
                return [sys.executable, "-m", child.name]
    except OSError:
        pass
    return None


def _entry_from_env(root: Path) -> list[str] | None:
    raw = (os.environ.get("ASM_ENTRY") or "").strip()
    if not raw:
        return None
    try:
        parts = shlex.split(raw, posix=os.name != "nt")
    except ValueError:
        return None
    if not parts:
        return None
    if parts[0].lower() in ("python", "python3", "py"):
        parts[0] = sys.executable
    for i, p in enumerate(parts):
        if p in ("$ROOT", "%ROOT%") or p == "ROOT":
            parts[i] = str(root)
    return parts


def _extra_argv_from_env() -> list[str]:
    raw = (os.environ.get("ASM_ARGS") or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=os.name != "nt")
    except ValueError:
        return []


def build_ascii_media_command(root: Path) -> list[str]:
    cmd = _entry_from_env(root)
    if cmd is not None:
        return cmd
    discovered = _discover_python_entry(root)
    if discovered is None and _maybe_fetch_ascii_player_demo_film(root):
        discovered = _discover_python_entry(root)
    if discovered is None:
        raise FileNotFoundError(
            f"No automatic entry point under {root}. "
            "On Windows, curses-based players are skipped (no stdlib curses). "
            "For the ascii-player demo, ensure star-wars.ascii exists (auto-fetch runs copy_film.py once unless "
            "ASM_NO_AUTO_FETCH=1), or set ASM_ENTRY to a working command (see README)."
        )
    return discovered


def launch_ascii_media_player(*, extra_args: list[str] | None = None) -> int:
    """Run Ascii-Media-Player; returns the child process exit code (0 = success)."""
    root = resolve_ascii_media_player_root()
    if not root.is_dir():
        raise FileNotFoundError(
            f"Ascii-Media-Player directory not found at {root}. "
            "Clone the repo next to Cuetilities or set ASCII_MEDIA_PLAYER_ROOT."
        )
    cmd = list(build_ascii_media_command(root))
    cmd.extend(_extra_argv_from_env())
    if extra_args:
        cmd.extend(extra_args)
    return int(
        subprocess.run(
            cmd,
            cwd=str(root),
            env=os.environ.copy(),
        ).returncode
    )
