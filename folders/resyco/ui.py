"""Terminal UI: banner, menus, progress, logging helpers."""
from __future__ import annotations

import contextlib
import difflib
import importlib.util
import os
import shutil
import sys
import threading
import time
from typing import Iterator

from .constants import *

def _enable_windows_vt_mode() -> None:
    """Help \\r single-line updates work in modern Windows consoles."""
    if sys.platform != "win32" or not hasattr(sys.stdout, "isatty") or not sys.stdout.isatty():
        return
    try:
        import ctypes

        h = ctypes.windll.kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if ctypes.windll.kernel32.GetConsoleMode(h, ctypes.byref(mode)):
            ctypes.windll.kernel32.SetConsoleMode(h, mode.value | 0x0004)
    except Exception:
        pass


_enable_windows_vt_mode()

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
except ImportError:
    _NC = ""

    class _NoColor:
        def __getattr__(self, _: str) -> str:
            return _NC

    Fore = Style = _NoColor()  # type: ignore[misc, assignment]

from .theme import *

# -----------------------------------------------------------------------------
# Terminal styling
# -----------------------------------------------------------------------------


def _print_banner() -> None:
    """Command deck / quick help (no ASCII logo)."""
    _print_quick_help()


def _edge_tts_wants() -> bool:
    """Use Microsoft Edge neural TTS (``edge-tts``) when enabled; requires network."""
    e = os.environ.get("PTK_TTS_ENGINE", "").strip().lower()
    if e in ("edge", "microsoft", "azure"):
        return True
    if e == "auto":
        try:
            return importlib.util.find_spec("edge_tts") is not None
        except (ValueError, AttributeError, ModuleNotFoundError):
            return False
    return os.environ.get("PTK_EDGE_TTS", "").strip().lower() in ("1", "true", "yes", "on")


def _tts_mode_banner() -> str:
    edge = _edge_tts_wants()
    raw = (os.environ.get("PTK_TTS_VOICE") or "").strip()
    if not raw:
        return "EDGE·1" if edge else "FIRST"
    r = raw.lower()
    if r in ("random", "any", "shuffle"):
        return "EDGE·RND" if edge else "RANDOM"
    if r.isdigit():
        return (f"EDGE·#{raw}" if edge else f"#{raw}")
    tail = raw[:12] + ("…" if len(raw) > 12 else "")
    return f"E:{tail}" if edge else tail


def _print_quick_help() -> None:
    w = max(60, min(94, _terminal_columns() - 2))
    line = "═" * w
    print(f"{C_FRAME}╔{line}╗{C_RESET}")
    print(
        f"{C_FRAME}║{C_RESET} "
        f"{C_ACCENT}{Style.BRIGHT}COMMAND DECK{C_RESET}  "
        f"{C_KEY}{Style.BRIGHT}[start]{C_RESET} / start(n) / start(*)  "
        f"{C_KEY}{Style.BRIGHT}[re]{C_RESET} / re(n) / re(*)  "
        f"{C_EXIT}{Style.BRIGHT}[ex]{C_RESET} back to tool menu"
    )
    fast_txt = "FAST" if FAST_RENDER_MODE else "QUALITY"
    whisper_txt = "ON" if os.environ.get("PTK_WHISPER", "").strip().lower() in ("1", "true", "yes") else "OFF"
    print(
        f"{C_FRAME}║{C_RESET} "
        f"{C_LABEL}mode:{C_RESET} {C_VALUE}{fast_txt}{C_RESET}   "
        f"{C_LABEL}subs:{C_RESET} {C_VALUE}WORD-BY-WORD{C_RESET}   "
        f"{C_LABEL}whisper:{C_RESET} {C_VALUE}{whisper_txt}{C_RESET}   "
        f"{C_LABEL}tts:{C_RESET} {C_VALUE}{_tts_mode_banner()}{C_RESET}"
    )
    print(f"{C_FRAME}╚{line}╝{C_RESET}")
    print()


def _print_tool_dropdown_menu(search: str = "") -> None:
    """Terminal UI styled like a dropdown: pick PTK/FFV/ORL/R3U/ASM quickly."""
    w = max(84, min(130, _terminal_columns() - 4))
    bar = "═" * w

    def row(text: str) -> None:
        pad = max(0, w - 2 - len(text))
        print(
            f"{C_FRAME}║{C_RESET} {text}{' ' * pad}"
            f"{C_FRAME}║{C_RESET}"
        )

    def cell(n: int, code: str, desc: str, col: str, width: int) -> str:
        badge = f"[{code}]"
        label = f"{n:>2}) {badge:<6} {desc}"
        if len(label) > width:
            label = label[: max(0, width - 1)].rstrip() + "…"
        return f"{col}{Style.BRIGHT}{label:<{width}}{C_RESET}"

    # Auto-pack options into 2/3/4 columns based on terminal width.
    col_count = 4 if w >= 124 else (3 if w >= 106 else 2)
    sep = f" {C_FRAME}│{C_RESET} "
    inner_w = w - 2
    col_w = max(22, (inner_w - (col_count - 1) * len(sep)) // col_count)
    options: list[tuple[int, str, str, str, str]] = [
        (1, "PTK", "Reddit story -> full video", C_TOOL_A, "Core"),
        (2, "FFV", "Reactions + SFX (funny preset)", C_TOOL_B, "Core"),
        (
            3,
            "ORL",
            "English Wikipedia science -> 9:16 slideshow",
            C_TOOL_C,
            "Core",
        ),
        (4, "R3U", "3 unknowns about an ordinary object", C_TOOL_A, "Core"),
        (5, "ASM", "Ascii-Media-Player (external terminal app)", C_TOOL_B, "Utility"),
    ]
    q = (search or "").strip().lower()
    if q:
        scored: list[tuple[float, tuple[int, str, str, str, str]]] = []
        for o in options:
            key = o[1].lower()
            desc = o[2].lower()
            group = o[4].lower()
            blob = f"{key} {desc} {group}"
            if q in key or q in desc or q in group:
                scored.append((1.0, o))
                continue
            ratio = difflib.SequenceMatcher(None, q, blob).ratio()
            # Gentle fuzzy threshold to recover from typos like "hstory".
            if ratio >= 0.28:
                scored.append((ratio, o))
        scored.sort(key=lambda x: (x[0], x[1][0]), reverse=True)
        options = [o for _s, o in scored]

    print()
    print(f"{C_FRAME}╔{bar}╗{C_RESET}")
    row(f"{C_ACCENT}{Style.BRIGHT}▼ TOOL SELECTOR{C_RESET}")
    row(
        f"{C_FRAME}Type number or code. "
        f"{C_VALUE}h{C_RESET}{C_FRAME}=help, "
        f"{C_VALUE}q{C_RESET}{C_FRAME}=quit.{C_RESET}"
    )
    row(
        f"{C_LABEL}Active tools:{C_RESET} "
        f"{C_VALUE}{len(options)}{C_RESET}   "
        f"{C_LABEL}Layout:{C_RESET} "
        f"{C_VALUE}{col_count} columns{C_RESET}   "
        f"{C_LABEL}Profiles:{C_RESET} "
        f"{C_VALUE}Core{C_RESET}"
    )
    if q:
        row(f"{C_ACCENT}Filter:{C_RESET} /{q}")
    print(f"{C_FRAME}╠{bar}╣{C_RESET}")
    if options:
        _gorder = ("Core", "Story", "Spotlight", "Teen", "Utility")
        groups = sorted(
            set(o[4] for o in options),
            key=lambda x: _gorder.index(x) if x in _gorder else 99,
        )
    else:
        groups = []
    for g in groups:
        row(f"{C_FRAME}· {g}{C_RESET}")
        subset = [o for o in options if o[4] == g]
        for i in range(0, len(subset), col_count):
            chunk = list(subset[i : i + col_count])
            while len(chunk) < col_count:
                chunk.append((0, "", "", C_FRAME, ""))
            rendered: list[str] = []
            for n, code, desc, col, _group in chunk:
                if n == 0:
                    rendered.append(" " * col_w)
                else:
                    rendered.append(cell(n, code, desc, col, col_w))
            row(sep.join(rendered))
    if not options:
        row(f"{C_WARN}No tools match this filter. Try {C_ACCENT}/clear{C_RESET}{C_WARN}.{C_RESET}")
    print(f"{C_FRAME}╠{bar}╣{C_RESET}")
    row(
        f"{C_KEY}{Style.BRIGHT}Quick picks:{C_RESET} "
        f"{C_VALUE}1=PTK  2=FFV  3=ORL  4=R3U  5=ASM{C_RESET}   "
        f"{C_EXIT}{Style.BRIGHT}q=quit{C_RESET}"
    )
    row(
        f"{C_ACCENT}/text{C_RESET} "
        f"{C_VALUE}= filter tools by code/description/category{C_RESET}   "
        f"{C_ACCENT}/clear{C_RESET}{C_VALUE}=reset filter{C_RESET}"
    )
    print(f"{C_FRAME}╚{bar}╝{C_RESET}")


def _prompt_tool_menu_choice() -> str:
    """Return a tool key (``ptk``, ``ffv``, ``orl``, ``r3u``, ``asm``) or ``quit``."""
    search = ""
    _print_tool_dropdown_menu(search)
    while True:
        try:
            raw = input(
                f"{C_WARN}Select tool "
                f"{C_KEY}[1-5/code]{C_RESET}"
                f"{C_WARN}, "
                f"{C_ACCENT}[/text]{C_RESET}"
                f"{C_WARN}=filter, "
                f"{C_ACCENT}[h]{C_RESET}"
                f"{C_WARN}=help, "
                f"{C_EXIT}[q]{C_RESET}"
                f"{C_WARN}=quit:{C_RESET} "
            ).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if raw.startswith("/"):
            cmd = raw[1:].strip()
            if cmd in ("", "clear", "all"):
                search = ""
            else:
                search = cmd
            _print_tool_dropdown_menu(search)
            continue
        if search and raw and raw not in ("h", "help", "?", "q", "quit", "exit", "0"):
            # Smart suggestion while filtered: best close codes for typed text.
            known_codes = ("ptk", "ffv", "orl", "r3u", "asm")
            hits = difflib.get_close_matches(raw, known_codes, n=5, cutoff=0.4)
            if hits:
                _info("Suggestions: " + ", ".join(hits))
        if raw in ("h", "help", "?"):
            _print_tool_dropdown_menu(search)
            continue
        if raw in ("q", "quit", "exit", "0"):
            return "quit"
        if raw in ("1", "ptk"):
            return "ptk"
        if raw in ("2", "ffv"):
            return "ffv"
        if raw in ("3", "orl"):
            return "orl"
        if raw in ("4", "r3u"):
            return "r3u"
        if raw in ("5", "asm"):
            return "asm"
        if raw:
            _warn(
                "Invalid selection. Enter 1–5, ptk, ffv, orl, r3u, asm, or q to quit."
            )


def _launch_ffv() -> None:
    from ffv.engine import ffv_interactive_main

    print()
    _step(
        f"{C_VALUE}{Style.BRIGHT}FFV{C_RESET} — "
        f"{C_KEY}{Style.BRIGHT}Reddit reaction pipeline{C_RESET}"
    )
    if os.environ.get("FFV_FUNNY", "").strip().lower() in ("1", "true", "yes", "on"):
        _info(
            "FFV_FUNNY=1 — opening in LOL preset (funny pics → meme reactions → punchy SFX)."
        )
    else:
        _info(
            "Inside FFV: type funny for LOL preset, or theory/default for full theory corpus. "
            "Tip: set FFV_FUNNY=1 to start in LOL mode."
        )
    ffv_interactive_main()


def _launch_asm() -> None:
    from .asm_player import launch_ascii_media_player

    print()
    _step(
        f"{C_VALUE}{Style.BRIGHT}ASM{C_RESET} — "
        f"{C_KEY}{Style.BRIGHT}Ascii-Media-Player{C_RESET}"
    )
    _info(
        "Resolves ../Ascii-Media-Player (or ../ascii-player), or set ASCII_MEDIA_PLAYER_ROOT. "
        "Override launch with ASM_ENTRY; pass-through args: ASM_ARGS or: python main.py --asm <args>."
    )
    try:
        code = launch_ascii_media_player()
    except FileNotFoundError as e:
        _warn(str(e))
        return
    except OSError as e:
        _warn(f"Failed to start player: {e}")
        return
    if code != 0:
        _warn(f"Ascii-Media-Player exited with code {code}.")


def _terminal_columns() -> int:
    try:
        return max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
    except (OSError, ValueError, AttributeError):
        return 100


def _pipeline_progress(step_done: float, total_steps: int, label: str, *, complete: bool = False) -> None:
    """Single in-place progress bar for the whole render pipeline."""
    cols = _terminal_columns()
    bar_w = max(16, min(34, cols - 44))
    frac = 0.0 if total_steps <= 0 else max(0.0, min(1.0, float(step_done) / total_steps))
    fill = int(round(bar_w * frac))
    bar = "█" * fill + "·" * max(0, bar_w - fill)
    whole = int(step_done) if not complete else total_steps
    whole = max(0, min(total_steps, whole))
    frames = "◐◓◑◒"
    spin = frames[int(time.time() * 8) % len(frames)]
    if _PIPELINE_T0 is not None:
        elapsed = max(0, int(time.time() - _PIPELINE_T0))
        em, es = divmod(elapsed, 60)
        et = f"{em:02d}:{es:02d}"
    else:
        et = "--:--"
    msg = f"{spin} {whole}/{total_steps} [{bar}] {label}  ⏱ {et}"
    vis = msg if len(msg) <= cols - 1 else msg[: cols - 2] + "…"
    pad = " " * max(0, cols - 1 - len(vis))
    sys.stdout.write(f"\r{C_PROGRESS}{vis}{C_RESET}{pad}")
    if complete:
        sys.stdout.write("\n")
    sys.stdout.flush()


@contextlib.contextmanager
def _progress_stage(done_before: int, total_steps: int, label: str) -> Iterator[None]:
    """
    Smooth in-stage animation:
    e.g. stage 2 animates 2.00 -> 2.92, then completes at 3.00.
    """
    stop = threading.Event()
    live = float(done_before)
    stage_end_soft = min(float(total_steps), done_before + 0.92)

    def run() -> None:
        nonlocal live
        while not stop.wait(0.09):
            if live < stage_end_soft:
                live = min(stage_end_soft, live + max(0.012, (stage_end_soft - live) * 0.1))
            _pipeline_progress(live, total_steps, label)

    _pipeline_progress(float(done_before), total_steps, label)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=0.5)
        _pipeline_progress(float(done_before + 1), total_steps, label)


@contextlib.contextmanager
def _spinner(label: str) -> Iterator[None]:
    """Animated working indicator on one line; width-capped so lines do not wrap (\\r then works)."""
    stop = threading.Event()

    def run() -> None:
        frames = "|/-\\"
        i = 0
        while not stop.wait(0.09):
            c = frames[i % len(frames)]
            i += 1
            cols = _terminal_columns()
            # "{c} {vis}" must stay within one physical line or \\r only clears the last wrapped row.
            cap = max(4, cols - 3)
            vis = label if len(label) <= cap else f"{label[: cap - 1]}…"
            plain = f"{c} {vis}"
            pad = max(0, cols - 1 - len(plain))
            sys.stdout.write(f"\r{C_ACCENT}{c}{C_RESET} {vis}{' ' * pad}")
            sys.stdout.flush()

    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=0.6)
        cols = _terminal_columns()
        sys.stdout.write("\r" + " " * cols + "\r")
        sys.stdout.flush()


def _ok_pop(msg: str) -> None:
    """Short emphasis animation on success."""
    for i in range(1, 4):
        sys.stdout.write(
            f"\r{C_KEY}{Style.BRIGHT}✔ {msg}{'.' * i}{' ' * 4}{C_RESET}"
        )
        sys.stdout.flush()
        time.sleep(0.1)
    print(f"\r{C_KEY}{Style.BRIGHT}✔ {msg}{C_RESET}")


def _info(msg: str) -> None:
    if _PROGRESS_ACTIVE:
        return
    print(f"{C_ACCENT}ℹ {msg}{C_RESET}")


def _step(msg: str) -> None:
    print(f"{C_LABEL}▶ {msg}{C_RESET}")


def _ok(msg: str) -> None:
    print(f"{C_KEY}{Style.BRIGHT}✔ {msg}{C_RESET}")


def _warn(msg: str) -> None:
    print(f"{C_WARN}⚠ {msg}{C_RESET}")


def _err(msg: str) -> None:
    print(f"{C_BAD}✖ {msg}{C_RESET}")


def _prompt_start() -> str:
    return (
        f"{C_FRAME}╭─{C_RESET}{C_LABEL}{Style.BRIGHT}FirstSky{C_RESET}{C_FRAME}─ render{C_RESET}\n"
        f"{C_FRAME}╰─{C_RESET} "
        f"{C_KEY}{Style.BRIGHT}start{C_RESET}"
        f"{C_FRAME} | {C_RESET}"
        f"{C_KEY}{Style.BRIGHT}start(n){C_RESET}"
        f"{C_FRAME} | {C_RESET}"
        f"{C_KEY}{Style.BRIGHT}start(*){C_RESET}"
        f"{C_FRAME} | {C_RESET}"
        f"{C_EXIT}{Style.BRIGHT}ex{C_RESET}"
        f"{C_FRAME} back to tools{C_RESET}\n"
        f"{C_WARN}> {C_RESET}"
    )


def _prompt_re_ex() -> str:
    return (
        f"{C_FRAME}╭─{C_RESET}{C_LABEL}{Style.BRIGHT}FirstSky{C_RESET}{C_FRAME}─ rerender{C_RESET}\n"
        f"{C_FRAME}╰─{C_RESET} "
        f"{C_KEY}{Style.BRIGHT}re{C_RESET}"
        f"{C_FRAME} | {C_RESET}"
        f"{C_KEY}{Style.BRIGHT}re(n){C_RESET}"
        f"{C_FRAME} | {C_RESET}"
        f"{C_KEY}{Style.BRIGHT}re(*){C_RESET}"
        f"{C_FRAME} | {C_RESET}"
        f"{C_EXIT}{Style.BRIGHT}ex{C_RESET}"
        f"{C_FRAME} back to tools{C_RESET}\n"
        f"{C_WARN}> {C_RESET}"
    )


def _exit_on_interrupt() -> None:
    print()
    _warn("Interrupted (Ctrl+C) — exiting.")
    sys.exit(130)


def _cleanup_incomplete_session(
    work: Path,
    output_mp4: Path | None,
) -> None:
    """Remove work dir and partial export for an incomplete run."""
    shutil.rmtree(work, ignore_errors=True)
    if output_mp4 is not None and output_mp4.exists():
        try:
            output_mp4.unlink()
        except OSError:
            pass
    for orphan in TEMP.glob("_spotdl_*.spotdl"):
        try:
            orphan.unlink()
        except OSError:
            pass


def _wipe_assets_tree() -> None:
    """Delete everything inside folders/assets/ after a successful render."""
    if not ASSETS.exists():
        return
    for p in ASSETS.iterdir():
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _export_star(name: str) -> bool:
    """Names exported via star-import from this module (underscore-prefixed UI helpers included)."""
    if name.startswith("__") and name.endswith("__"):
        return False
    return True


__all__ = tuple(sorted(k for k in globals() if k != "_export_star" and _export_star(k)))
del _export_star
