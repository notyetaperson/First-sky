"""
FFV engine: Reddit reactable + reaction + SFX pipeline (theory.txt).

Timings: 5.0s reactable hold, 3.5s reaction with 0.25s fade-in and fade-out, SFX aligned to reaction window.
"""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import unquote, urlparse, urlunsplit

import requests

from ffv import catalog as ffv_catalog
from ffv.catalog import (
    FFV_ENV_CATALOG,
    FFV_ERROR_CODES,
    FFV_FFMPEG_TUNING_PROFILES,
    format_phase_deck,
    load_sfx_rows_from_theory,
    sfx_is_music_like_url,
    sfx_families_compatible_with_sub,
    sfx_infer_families,
    sfx_slug_from_url,
    subreddit_diversity_hint,
)

try:
    from colorama import Fore, Style, init

    init(autoreset=True)
except ImportError:

    class _NoColor:
        def __getattr__(self, _: str) -> str:
            return ""

    Fore = Style = _NoColor()  # type: ignore[misc, assignment]

from resyco.theme import (
    C_ACCENT,
    C_BAD,
    C_BRAND,
    C_EXIT,
    C_FRAME,
    C_LABEL,
    C_OK,
    C_PROGRESS,
    C_RESET,
    C_VALUE,
    C_WARN,
)


def _enable_windows_vt_mode() -> None:
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

# -----------------------------------------------------------------------------
# Paths: ``engine.py`` is ``folders/ffv/engine.py`` — ``folders/`` is UNIVERSAL_DIR.
# -----------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
UNIVERSAL_DIR = Path(__file__).resolve().parent.parent
ASSETS = UNIVERSAL_DIR / "assets"
OUTPUT_DIR = ROOT / "output"
MUSIC_DIR = UNIVERSAL_DIR / "music"
THEORY_FILE = UNIVERSAL_DIR / "theory.txt"
FFV_ASSETS = ASSETS / "ffv"
FFV_SESSIONS = FFV_ASSETS / "sessions"

# -----------------------------------------------------------------------------
# Theory constants (seconds) — fades per theory.txt (0.25 in + out)
# -----------------------------------------------------------------------------

FFV_VERSION = "1.1.0-enjoy"
REACTABLE_HOLD = 5.0
REACTION_HOLD = 3.5
REACTION_FADE_IN = 0.25
REACTION_FADE_OUT = 0.25
SEGMENT_VISUAL_TOTAL = REACTABLE_HOLD + REACTION_HOLD
VIDEO_SEGMENTS_MIN = 4
VIDEO_SEGMENTS_MAX = 12
RANK_WEIGHT_DECAY = 0.02

# short = single 9:16; video = multi-segment 16:9 (swapped vs legacy theory.txt wording)
SHORT_W, SHORT_H = 1080, 1920
VERT_W, VERT_H = 1920, 1080

# “video” compile — crossfades + motion (multi-segment only)
_XFADE_PRESETS = (
    "fade",
    "wipeleft",
    "wiperight",
    "slideleft",
    "slideright",
    "slideup",
    "slidedown",
    "diagtl",
    "diagtr",
    "diagbl",
    "diagbr",
    "radial",
    "circleopen",
    "circleclose",
    "smoothleft",
    "smoothright",
    "smoothup",
    "smoothdown",
    "pixelize",
    "squeezeh",
    "squeezev",
    "hlslice",
    "hrslice",
    "wind",
)

_FFV_GRADE_EQ_PRESETS: tuple[str, ...] = (
    "eq=contrast=1.05:saturation=1.08:brightness=0.01",
    "eq=contrast=1.08:saturation=1.12:brightness=-0.01",
    "eq=contrast=1.03:saturation=1.18:brightness=0.0",
    "eq=contrast=1.12:saturation=1.05:brightness=-0.02:gamma=1.02",
    "eq=contrast=1.06:saturation=0.95:brightness=0.015",
    "eq=contrast=1.04:saturation=1.22:brightness=-0.025",
    "eq=contrast=1.1:saturation=1.0:brightness=0.0:gamma=0.98",
    "eq=contrast=1.02:saturation=1.14:brightness=0.02",
)

_IMG_EXT = (".jpg", ".jpeg", ".png", ".webp", ".gif")
_REDDIT_SUB = re.compile(r"https?://(?:www\.)?reddit\.com/r/([^/]+)/?", re.I)
_RE_MYINSTANTS = re.compile(r"https://www\.myinstants\.com/[^\s]+", re.I)


def _terminal_columns() -> int:
    try:
        return max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
    except (OSError, ValueError, AttributeError):
        return 100


_FFV_PROGRESS_ACTIVE = False
_FFV_PIPELINE_T0: float | None = None


def _ffv_pipeline_progress(step_done: float, total_steps: int, label: str, *, complete: bool = False) -> None:
    """Same in-place bar animation as PTK (main.py)."""
    cols = _terminal_columns()
    bar_w = max(16, min(34, cols - 44))
    frac = 0.0 if total_steps <= 0 else max(0.0, min(1.0, float(step_done) / total_steps))
    fill = int(round(bar_w * frac))
    bar = "█" * fill + "·" * max(0, bar_w - fill)
    whole = int(step_done) if not complete else total_steps
    whole = max(0, min(total_steps, whole))
    frames = "◐◓◑◒"
    spin = frames[int(time.time() * 8) % len(frames)]
    if _FFV_PIPELINE_T0 is not None:
        elapsed = max(0, int(time.time() - _FFV_PIPELINE_T0))
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
def _ffv_progress_stage(done_before: int, total_steps: int, label: str) -> Iterator[None]:
    stop = threading.Event()
    live = float(done_before)
    stage_end_soft = min(float(total_steps), done_before + 0.92)

    def run() -> None:
        nonlocal live
        while not stop.wait(0.09):
            if live < stage_end_soft:
                live = min(stage_end_soft, live + max(0.012, (stage_end_soft - live) * 0.1))
            _ffv_pipeline_progress(live, total_steps, label)

    _ffv_pipeline_progress(float(done_before), total_steps, label)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join(timeout=0.5)
        _ffv_pipeline_progress(float(done_before + 1), total_steps, label)


def _which(cmd: str) -> str | None:
    return shutil.which(cmd)


def _ffmpeg_stderr_sink() -> Any:
    """Avoid stderr spam and parallel garbling; set FFV_FFMPEG_VERBOSE=1 to inherit stderr."""
    if os.environ.get("FFV_FFMPEG_VERBOSE", "").strip().lower() in ("1", "true", "yes", "on"):
        return None
    return subprocess.DEVNULL


def _ffv_ffmpeg_threads_prefix() -> list[str]:
    raw = (os.environ.get("FFV_FFMPEG_THREADS") or "0").strip().lower()
    if raw in ("", "none", "off"):
        return []
    if raw in ("auto", "0"):
        return ["-threads", "0"]
    try:
        n = int(raw)
    except ValueError:
        return ["-threads", "0"]
    if n <= 0:
        return ["-threads", "0"]
    return ["-threads", str(n)]


def _ffv_ffmpeg_std_head() -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *_ffv_ffmpeg_threads_prefix(),
    ]


def _run_ffmpeg(cmd: list[str], *, timeout: float | None = None) -> None:
    if cmd[0] != "ffmpeg":
        raise ValueError("_run_ffmpeg expects ffmpeg as argv[0]")
    body = list(cmd[1:])
    if body and isinstance(body[-1], str):
        last = body[-1]
        if not last.startswith("-") and last not in ("-", "pipe:", "pipe:1") and "-map_metadata" not in body:
            ex = _ffv_ffmpeg_strip_metadata_argv(last)
            if ex:
                body = body[:-1] + ex + [last]
    argv = _ffv_ffmpeg_std_head() + body
    subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=_ffmpeg_stderr_sink(),
        timeout=timeout,
    )


@lru_cache(maxsize=32)
def _ffmpeg_knob_items(preset_key: str) -> tuple[tuple[str, Any], ...]:
    d = FFV_FFMPEG_TUNING_PROFILES.get(preset_key, FFV_FFMPEG_TUNING_PROFILES["ffv_default"])
    keys = sorted(d.keys())
    return tuple((k, d[k]) for k in keys)


def _ffmpeg_video_knobs() -> dict[str, Any]:
    default_key = "ffv_fast" if _ffv_turbo_enabled() else "ffv_default"
    key = (os.environ.get("FFV_FFMPEG_PRESET") or default_key).strip().lower()
    return dict(_ffmpeg_knob_items(key))


def _ffv_libx264_extra_argv(fk: dict[str, Any]) -> list[str]:
    if str(fk.get("video_codec", "libx264")) != "libx264":
        return []
    out: list[str] = []
    tune = fk.get("tune")
    if tune:
        out.extend(["-tune", str(tune)])
    xp = fk.get("x264_params")
    if xp:
        out.extend(["-x264-params", str(xp)])
    return out


def _ffv_ffmpeg_strip_metadata_argv(out_arg: str) -> list[str]:
    tail = out_arg.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()
    if tail.endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v")):
        return ["-map_metadata", "-1", "-map_chapters", "-1"]
    return []


def _ffv_mp4_container_args() -> list[str]:
    """Moov at file start + sane timestamps — avoids broken playback / upload \"encoding\" rejects."""
    return ["-movflags", "+faststart", "-avoid_negative_ts", "make_zero"]


def _ffv_aac_audio_args(fk: dict[str, Any]) -> list[str]:
    """AAC-LC is the widest-supported profile for shorts platforms."""
    return [
        "-c:a",
        str(fk.get("audio_codec", "aac")),
        "-profile:a",
        "aac_low",
        "-b:a",
        str(fk.get("audio_bitrate", "192k")),
    ]


def _ffv_scale_flags(fk: dict[str, Any]) -> str:
    return str(fk.get("scale_flags") or "bicubic")


def _ffv_one_line_chyron(text: str, max_len: int = 92) -> str:
    t = " ".join((text or "").split())
    if len(t) > max_len:
        t = t[: max_len - 1].rstrip() + "…"
    return t or " "


def _ffv_chyron_fontfile() -> str | None:
    custom = (os.environ.get("FFV_VIDEO_FONT") or "").strip()
    if custom:
        p = Path(custom)
        if p.is_file():
            return p.resolve().as_posix()
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        arial = Path(windir) / "Fonts" / "arial.ttf"
        if arial.is_file():
            return arial.resolve().as_posix()
    return None


def _ffv_filter_escape_path_posix(path: Path) -> str:
    s = path.resolve().as_posix()
    if len(s) >= 2 and s[1] == ":":
        s = s[0] + r"\:" + s[2:]
    return s.replace("'", r"'\''")


def _ffv_apply_cheap_image_effects(src: Path, work: Path | None, tag: str) -> Path:
    """
    Fast, single-frame enhancement using Pillow (3rd-party) so turbo mode keeps
    visual pop without costly per-frame FFmpeg grading chains.
    """
    if src.suffix.lower() == ".gif":
        return src
    try:
        from PIL import Image, ImageEnhance, ImageOps  # type: ignore[import-not-found]
    except ImportError:
        return src
    out_dir = work if work is not None else src.parent
    out = out_dir / f"{tag}_cheapfx.jpg"
    try:
        with Image.open(src) as im:
            rgb = im.convert("RGB")
            rgb = ImageOps.autocontrast(rgb, cutoff=1)
            rgb = ImageEnhance.Contrast(rgb).enhance(1.06)
            rgb = ImageEnhance.Color(rgb).enhance(1.08)
            rgb = ImageEnhance.Sharpness(rgb).enhance(1.12)
            out.parent.mkdir(parents=True, exist_ok=True)
            rgb.save(out, format="JPEG", quality=88, optimize=True)
        if out.is_file() and out.stat().st_size > 256:
            return out
    except Exception:
        out.unlink(missing_ok=True)
    return src


def _ffv_append_chyron_vf(
    vf: str,
    *,
    work: Path,
    tag: str,
    line: str,
    fontsize_expr: str,
) -> str:
    fp = work / f"{tag}_chyron.txt"
    fp.write_text(_ffv_one_line_chyron(line), encoding="utf-8")
    pesc = _ffv_filter_escape_path_posix(fp)
    font_opt = ""
    ff = _ffv_chyron_fontfile()
    if ff:
        font_opt = f":fontfile='{_ffv_filter_escape_path_posix(Path(ff))}'"
    return (
        f"{vf},drawtext=textfile='{pesc}'{font_opt}:reload=0:fontcolor=white@0.92:"
        f"box=1:boxcolor=black@0.42:boxborderw=8:x=(w-text_w)/2:y=h-text_h-22:fontsize={fontsize_expr}"
    )


def _ffv_sfx_mix_level() -> float:
    try:
        v = float(os.environ.get("FFV_SFX_MIX_LEVEL", "0.88") or "0.88")
        return max(0.05, min(1.0, v))
    except ValueError:
        return 0.88


def _ffv_pick_bg_music_track() -> Path | None:
    if not MUSIC_DIR.is_dir():
        return None
    exts = (".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")
    tracks = [p for p in MUSIC_DIR.iterdir() if p.is_file() and p.suffix.lower() in exts]
    if not tracks:
        return None
    return random.choice(tracks)


def _verbose_phases_enabled() -> bool:
    return os.environ.get("FFV_VERBOSE_PHASES", "").strip().lower() in ("1", "true", "yes", "on")


def _maybe_print_phase(hint: str) -> None:
    if _verbose_phases_enabled():
        print(f"{C_FRAME}⟨phase⟩ {hint}{C_RESET}")


def _dry_run_enabled() -> bool:
    return os.environ.get("FFV_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, "").strip() or default)
    except ValueError:
        return default


def _env_flag(key: str, default: bool = True) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_flag_default(key: str, *, default: bool) -> bool:
    raw = os.environ.get(key, "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def _video_inner_xfade_sec() -> float:
    if _ffv_turbo_enabled():
        return max(0.04, min(0.12, _env_float("FFV_VIDEO_INNER_XFADE", 0.08)))
    v = _env_float("FFV_VIDEO_INNER_XFADE", 0.18)
    return max(0.0, min(0.45, v))


def _video_outer_xfade_sec() -> float:
    if _ffv_turbo_enabled():
        return max(0.04, min(0.14, _env_float("FFV_VIDEO_OUTER_XFADE", 0.08)))
    v = _env_float("FFV_VIDEO_OUTER_XFADE", 0.14)
    return max(0.0, min(0.5, v))


def _xfade_transition_choices() -> tuple[str, ...]:
    raw = (os.environ.get("FFV_VIDEO_TRANSITIONS") or "").strip()
    if raw:
        parts = tuple(x.strip() for x in raw.split(",") if x.strip())
        return parts if parts else _XFADE_PRESETS
    return _XFADE_PRESETS


def _pick_xfade_transition() -> str:
    choices = _xfade_transition_choices()
    return random.choice(choices)


def _ffprobe_duration(path: Path) -> float:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return max(0.01, float(r.stdout.strip()))
    except (subprocess.CalledProcessError, ValueError, OSError, subprocess.TimeoutExpired):
        return SEGMENT_VISUAL_TOTAL


def _ffprobe_has_audio(path: Path) -> bool:
    try:
        r = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return bool(r.stdout.strip())
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        return False


# -----------------------------------------------------------------------------
# Data contracts
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RedditPickRecord:
    subreddit: str
    post_id: str
    permalink: str
    url: str
    virality: float
    rank_index: int
    weight: float
    title: str = ""


@dataclass(frozen=True)
class SegmentBlueprint:
    index: int
    reactable: RedditPickRecord
    reaction: RedditPickRecord
    sfx_url: str
    segment_seconds: float = SEGMENT_VISUAL_TOTAL


@dataclass
class RenderDAGNode:
    node_id: str
    kind: str
    status: str
    detail: str = ""
    t0: float = 0.0
    t1: float = 0.0


@dataclass
class FFVProjectState:
    seed: int | None
    session_id: str
    renders_ok: int = 0
    renders_fail: int = 0
    last_output: Path | None = None
    theory_corpus_digest: str = ""
    corpus_preset: str = "theory"  # "theory" | "lol"
    dag_history: list[list[dict[str, Any]]] = field(default_factory=list)
    recent_sfx_families: list[str] = field(default_factory=list)
    recent_sfx_urls: list[str] = field(default_factory=list)
    last_reactable_sub: str | None = None
    recent_titles: list[str] = field(default_factory=list)


_FFV_TITLE_ADJECTIVES: tuple[str, ...] = (
    "corny",
    "stupid",
    "dumb",
    "unhinged",
    "cursed",
    "wild",
    "chaotic",
    "brainrot",
    "goofy",
)
_FFV_TITLE_SUBJECTS: tuple[str, ...] = (
    "screenshots",
    "posts",
    "tweets",
    "threads",
    "comment sections",
    "internet posts",
)


def _ffv_pick_filtered_word(pool: tuple[str, ...], recent: list[str], fallback: str) -> str:
    block = set(recent[-6:])
    opts = [w for w in pool if w not in block]
    return random.choice(opts if opts else list(pool) or [fallback])


def _ffv_make_upload_title(
    state: FFVProjectState,
    *,
    is_video: bool,
    out_index: int,
    n_segments: int = 1,
) -> str:
    adj = _ffv_pick_filtered_word(_FFV_TITLE_ADJECTIVES, state.recent_titles, "unhinged")
    subj = _ffv_pick_filtered_word(_FFV_TITLE_SUBJECTS, state.recent_titles, "posts")
    title = f"{adj} {subj} from the internet #{out_index}"
    if is_video:
        title += f" part 1-{n_segments}"
    state.recent_titles.extend([adj, subj])
    if len(state.recent_titles) > 64:
        state.recent_titles = state.recent_titles[-64:]
    return title


def _ffv_write_title_sidecar(out_path: Path, title: str) -> Path:
    sidecar = out_path.with_suffix(".title.txt")
    sidecar.write_text(title + "\n", encoding="utf-8")
    return sidecar


def _ffv_safe_filename_base(title: str, *, fallback: str) -> str:
    cleaned = (title or "").strip()
    cleaned = re.sub(r'[\\/:*?"<>|]+', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".")
    return cleaned[:120] if cleaned else fallback


def _ffv_titled_output_path(prefix: str, idx: int, title: str) -> Path:
    base = _ffv_safe_filename_base(title, fallback=f"{prefix}_{idx}")
    candidate = OUTPUT_DIR / f"{base}.mp4"
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        alt = OUTPUT_DIR / f"{base} ({n}).mp4"
        if not alt.exists():
            return alt
        n += 1


class TheoryCorpus:
    def __init__(
        self,
        reactable_subs: list[str],
        reaction_subs: list[str],
        sfx_urls: list[str],
        *,
        sfx_rows: list[tuple[str, str, frozenset[str]]] | None = None,
    ) -> None:
        self.reactable_subs = reactable_subs
        self.reaction_subs = reaction_subs
        self.sfx_urls = sfx_urls
        self.sfx_rows = sfx_rows or [(u, sfx_slug_from_url(u), sfx_infer_families(sfx_slug_from_url(u))) for u in sfx_urls]

    @classmethod
    def load(cls, path: Path = THEORY_FILE) -> TheoryCorpus:
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        react: set[str] = set()
        react_img: set[str] = set()
        sfx: list[str] = []
        for line in text.splitlines():
            for u in _RE_MYINSTANTS.findall(line):
                sfx.append(u.strip().split()[0])
        section: str | None = None
        for line in text.splitlines():
            ls = line.strip()
            lol = ls.lower()
            if lol.startswith("reactables"):
                section = "react"
                continue
            if lol.startswith("reactions"):
                section = "reimg"
                continue
            if lol.startswith("sfx"):
                section = "sfx"
                continue
            if "myinstants.com" in ls:
                for u in _RE_MYINSTANTS.findall(ls):
                    sfx.append(u.strip())
                continue
            m = _REDDIT_SUB.search(ls)
            if m and section == "react":
                react.add(m.group(1).strip())
            elif m and section == "reimg":
                react_img.add(m.group(1).strip())

        if not react:
            react = {"hmmm", "facepalm", "CrappyDesign", "Unexpected", "AbruptChaos"}
        if not react_img:
            react_img = {"reactionimages", "DeepFriedMemes", "DeepFreezedMemes"}
        react.update(ffv_catalog.FFV_EXTRA_REACTABLE_SUBS)
        sfx_u = list(dict.fromkeys(sfx))
        if not sfx_u:
            sfx_u = [
                "https://www.myinstants.com/en/instant/vine-boom-sound-70972/?utm_source=copy&utm_medium=share",
                "https://www.myinstants.com/en/instant/bruh/?utm_source=copy&utm_medium=share",
            ]
        sfx_u = list(dict.fromkeys([*sfx_u, *ffv_catalog.FFV_BONUS_SFX_URLS]))
        pack = (os.environ.get("FFV_USER_PACK") or "").strip()
        if pack:
            pp = Path(pack).expanduser()
            if not pp.is_dir():
                pp = Path(os.getcwd()) / pack
            if pp.is_dir():
                extra_local: list[str] = []
                for f in sorted(pp.glob("**/*")):
                    if f.is_file() and f.suffix.lower() in {".mp3", ".wav", ".m4a", ".ogg", ".flac"}:
                        extra_local.append(str(f.resolve()))
                if extra_local:
                    sfx_u = list(dict.fromkeys([*extra_local, *sfx_u]))
        rows_theory = load_sfx_rows_from_theory(path) if path.is_file() else []
        by_url: dict[str, tuple[str, str, frozenset[str]]] = {r[0]: r for r in rows_theory}
        for u in sfx_u:
            if u not in by_url:
                by_url[u] = (u, sfx_slug_from_url(u), sfx_infer_families(sfx_slug_from_url(u)))
        rows = [by_url[u] for u in sfx_u]
        return cls(sorted(react), sorted(react_img), sfx_u, sfx_rows=rows)

    def digest(self) -> str:
        return f"{len(self.reactable_subs)}|{len(self.reaction_subs)}|{len(self.sfx_urls)}|{ffv_catalog.catalog_digest()}"

    def as_funny(self) -> TheoryCorpus:
        """LOL preset: funny/wtf reactables, meme reaction subs, SFX list front-loaded with meme hits."""
        fr = ffv_catalog.FFV_FUNNY_PRESET_REACTABLES
        react = [s for s in self.reactable_subs if s in fr]
        if len(react) < 10:
            react = sorted(fr)
        rr = ffv_catalog.FFV_FUNNY_PRESET_REACTIONS
        rimg = [s for s in self.reaction_subs if s in rr]
        if len(rimg) < 4:
            rimg = sorted(rr)
        pri = list(ffv_catalog.FFV_FUNNY_SFX_PRIORITY_URLS)
        seen = set(pri)
        tail = [u for u in self.sfx_urls if u not in seen]
        sfx_u = pri + tail
        by_url: dict[str, tuple[str, str, frozenset[str]]] = {}
        for u in sfx_u:
            slug = sfx_slug_from_url(u)
            by_url[u] = (u, slug, sfx_infer_families(slug))
        rows = [by_url[u] for u in sfx_u]
        return TheoryCorpus(react, rimg, sfx_u, sfx_rows=rows)


class ViralityIndex:
    @staticmethod
    def score(child: dict[str, Any]) -> float:
        sc = max(0.0, float(child.get("score") or 0.0))
        nc = max(0.0, float(child.get("num_comments") or 0.0))
        return math.log1p(sc) * 0.58 + math.log1p(nc) * 0.42


def _post_unique_keys(c: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    pid = str(c.get("id") or "").strip()
    if pid:
        keys.add(f"id:{pid}")
    url = str(c.get("_ffv_image_url") or c.get("url") or "").strip().lower()
    if url:
        keys.add(f"url:{url}")
    permalink = str(c.get("permalink") or "").strip().lower()
    if permalink:
        keys.add(f"perm:{permalink}")
    title = " ".join(str(c.get("title") or "").split()).strip().lower()
    if title:
        keys.add(f"title:{title}")
    return keys


def _pool_minus_ids(pool: list[dict[str, Any]], exclude: frozenset[str] | None) -> list[dict[str, Any]]:
    if not exclude:
        return pool
    out: list[dict[str, Any]] = []
    for c in pool:
        if _post_unique_keys(c).isdisjoint(exclude):
            out.append(c)
    return out


def weighted_pick_ranked(
    rows: list[dict[str, Any]],
    key_fn: Callable[[dict[str, Any]], float],
    *,
    exclude_ids: frozenset[str] | None = None,
    relax_if_small: bool = True,
) -> tuple[dict[str, Any], int, float]:
    filtered = _pool_minus_ids(rows, exclude_ids)
    if relax_if_small and len(filtered) < max(3, len(rows) // 8):
        filtered = rows
    ranked = sorted(filtered, key=key_fn, reverse=True)
    if not ranked:
        raise RuntimeError("empty ranked pool")
    weights: list[float] = []
    for idx, row in enumerate(ranked):
        mult = max(0.005, 1.0 - (RANK_WEIGHT_DECAY * idx))
        v = key_fn(row)
        pop = 0.45 + 0.55 * (1.0 - math.exp(-v / 6.0))
        weights.append(max(1e-6, mult * pop))
    choice = random.choices(ranked, weights=weights, k=1)[0]
    ri = ranked.index(choice)
    wi = weights[ri]
    return choice, ri, wi


_FFV_TLS = threading.local()
_FFV_UA_DEFAULT = (
    "python:firstsky:ffv:1.0 "
    "(FFV reactable/reaction image pool; +https://github.com/reddit-archive/reddit/wiki/api)"
)
_FFV_UA = os.environ.get("FFV_UA") or os.environ.get("PTK_UA") or _FFV_UA_DEFAULT

_SFX_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _ffv_session() -> requests.Session:
    s = getattr(_FFV_TLS, "session", None)
    if s is None:
        s = requests.Session()
        _FFV_TLS.session = s
    return s


def _reddit_empty_listing() -> dict[str, Any]:
    return {"data": {"children": [], "after": None}}


def _reddit_json_url_variants(url: str) -> list[str]:
    try:
        p = urlparse(url)
    except ValueError:
        return [url]
    if not p.netloc or "reddit.com" not in (p.netloc or "").lower():
        return [url]
    hosts = ("www.reddit.com", "old.reddit.com", "api.reddit.com")
    out: list[str] = []
    path, query, fragment = p.path, p.query, p.fragment
    for h in hosts:
        u = urlunsplit(("https", h, path, query, fragment))
        if u not in out:
            out.append(u)
    return out or [url]


def _ffv_reddit_json_headers(ua: str) -> dict[str, str]:
    return {
        "User-Agent": ua,
        "Accept": "application/json, text/javascript, */*;q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.reddit.com/",
    }


def _ffv_reddit_fetch_user_agents() -> list[str]:
    primary = (os.environ.get("FFV_UA") or os.environ.get("PTK_UA") or "").strip()
    uas: list[str] = []
    if primary:
        uas.append(primary)
    if _FFV_UA_DEFAULT not in uas:
        uas.append(_FFV_UA_DEFAULT)
    if _SFX_BROWSER_UA not in uas:
        uas.append(_SFX_BROWSER_UA)
    return list(dict.fromkeys(uas))


def ffv_order_subs_for_image_fetch(subs: list[str], *, reaction: bool = False) -> list[str]:
    """Image-heavy subs first so ``collect_pool`` does not starve on text-only communities."""
    pri = (
        ffv_catalog.FFV_POOL_FETCH_PRIORITY_REACTIONS
        if reaction
        else ffv_catalog.FFV_POOL_FETCH_PRIORITY_REACTABLES
    )
    seen: set[str] = set()
    prio: list[str] = []
    for s in subs:
        key = (s or "").strip().lower()
        if not key or key in seen:
            continue
        if key in pri:
            prio.append(s)
            seen.add(key)
    rest = [s for s in subs if (s or "").strip().lower() not in seen]
    random.shuffle(rest)
    return prio + rest


def _reddit_get_json(url: str, params: dict[str, Any] | None = None) -> Any:
    params = dict(params or {})
    pause = float(os.environ.get("FFV_REDDIT_PAUSE", "0.25"))
    attempts = max(1, min(12, int(os.environ.get("FFV_REDDIT_JSON_ATTEMPTS", "4"))))
    url_variants = _reddit_json_url_variants(url)
    uas = _ffv_reddit_fetch_user_agents()
    saw_block_or_rate = False
    last_err: Exception | None = None
    for attempt in range(attempts):
        time.sleep(pause)
        for u in url_variants:
            for ua in uas:
                try:
                    r = _ffv_session().get(
                        u,
                        params=params,
                        headers=_ffv_reddit_json_headers(ua),
                        timeout=(10, 28),
                    )
                    if r.status_code in (403, 429):
                        saw_block_or_rate = True
                        continue
                    if 500 <= r.status_code < 600:
                        continue
                    r.raise_for_status()
                    return r.json()
                except (requests.RequestException, ValueError) as e:
                    last_err = e
                    continue
        if attempt < attempts - 1:
            time.sleep(0.9 * (attempt + 1))
    if saw_block_or_rate:
        return _reddit_empty_listing()
    if last_err is not None:
        raise last_err
    return _reddit_empty_listing()


def _child_image_url(c: dict[str, Any]) -> str | None:
    if c.get("is_video") or c.get("is_gallery"):
        return None
    url = str(c.get("url") or "")
    if "reddit.com/gallery/" in url:
        return None
    if url.lower().endswith(_IMG_EXT):
        return url
    ph = str(c.get("post_hint") or "")
    if ph == "image" and url:
        return url
    prev = c.get("preview") or {}
    imgs = prev.get("images") or []
    if imgs:
        src = imgs[0].get("source") or {}
        u = src.get("url")
        if u:
            return str(u).replace("&amp;", "&")
    return None


def fetch_image_posts(sub: str, limit: int = 48) -> list[dict[str, Any]]:
    url = f"https://www.reddit.com/r/{sub}/hot.json"
    data = _reddit_get_json(url, {"limit": limit, "raw_json": 1})
    out: list[dict[str, Any]] = []
    for ch in data.get("data", {}).get("children") or []:
        c = ch.get("data") or {}
        u = _child_image_url(c)
        if not u or c.get("over_18"):
            continue
        c = dict(c)
        c["_ffv_image_url"] = u
        c["_ffv_sub"] = sub
        out.append(c)
    return out


def reddit_pick_to_record(c: dict[str, Any], rank_index: int, weight: float) -> RedditPickRecord:
    raw_title = c.get("title")
    title = str(raw_title).replace("\n", " ").strip() if raw_title is not None else ""
    return RedditPickRecord(
        subreddit=str(c.get("_ffv_sub") or ""),
        post_id=str(c.get("id") or ""),
        permalink=str(c.get("permalink") or ""),
        url=str(c.get("_ffv_image_url") or ""),
        virality=ViralityIndex.score(c),
        rank_index=rank_index,
        weight=weight,
        title=title,
    )


def download_bytes(url: str, dest: Path, timeout: int = 60) -> bool:
    parsed = urlparse(url)
    netloc = (parsed.netloc or "").lower()
    variant_urls = [url]
    bare = url.split("?", 1)[0]
    if bare != url:
        variant_urls.append(bare)
    if "external-preview.redd.it" in netloc:
        if bare not in variant_urls:
            variant_urls.append(bare)
    primary_ua = (os.environ.get("FFV_UA") or os.environ.get("PTK_UA") or "").strip() or _FFV_UA_DEFAULT
    header_variants: list[dict[str, str]] = [
        {"User-Agent": primary_ua, "Accept": "image/*,*/*;q=0.8"},
        {
            "User-Agent": _SFX_BROWSER_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.reddit.com/",
        },
    ]
    dest.parent.mkdir(parents=True, exist_ok=True)
    for u in dict.fromkeys(variant_urls):
        for hdr in header_variants:
            try:
                r = _ffv_session().get(u, headers=hdr, timeout=timeout, stream=True)
                r.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(1024 * 512):
                        if chunk:
                            f.write(chunk)
                if dest.is_file() and dest.stat().st_size > 80:
                    return True
                dest.unlink(missing_ok=True)
            except requests.RequestException:
                dest.unlink(missing_ok=True)
                continue
    return False


def _looks_like_mp3_header(chunk: bytes) -> bool:
    if len(chunk) < 3:
        return False
    if chunk[:3] == b"ID3":
        return True
    return chunk[0] == 0xFF and (chunk[1] & 0xE0) == 0xE0


def download_sfx_bytes(url: str, dest: Path, *, referer: str | None = None, timeout: int = 60) -> bool:
    headers: dict[str, str] = {
        "User-Agent": _SFX_BROWSER_UA,
        "Accept": "audio/mpeg,audio/*,*/*;q=0.9",
    }
    if referer:
        headers["Referer"] = referer
    try:
        r = _ffv_session().get(url, headers=headers, timeout=timeout, stream=True)
        r.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        first = b""
        total = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024 * 64):
                if not chunk:
                    continue
                if not first:
                    first = chunk[:16]
                f.write(chunk)
                total += len(chunk)
        if total < 256:
            dest.unlink(missing_ok=True)
            return False
        if dest.suffix.lower() == ".mp3" and not _looks_like_mp3_header(first):
            dest.unlink(missing_ok=True)
            return False
        return True
    except requests.RequestException:
        dest.unlink(missing_ok=True)
        return False


def _yt_dlp_argv() -> list[str] | None:
    exe = _which("yt-dlp") or _which("yt_dlp")
    if exe:
        return [exe]
    try:
        subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], check=True, capture_output=True, timeout=15)
        return [sys.executable, "-m", "yt_dlp"]
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def _myinstants_slug(page_url: str) -> str | None:
    try:
        p = urlparse(page_url)
        if "myinstants.com" not in (p.netloc or "").lower():
            return None
        parts = [x for x in p.path.split("/") if x]
        for i, seg in enumerate(parts):
            if seg.lower() == "instant" and i + 1 < len(parts):
                return unquote(parts[i + 1].rstrip("/"))
    except (ValueError, IndexError):
        pass
    return None


def _myinstants_direct_mp3_urls(page_url: str) -> list[str]:
    slug = _myinstants_slug(page_url)
    if not slug:
        return []
    base = re.sub(r"-\d+$", "", slug)
    seen: set[str] = set()
    out: list[str] = []
    for name in (base, slug):
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(f"https://www.myinstants.com/media/sounds/{name}.mp3")
    return out


def download_sfx(url: str, work: Path, stem: str) -> Path | None:
    work.mkdir(parents=True, exist_ok=True)
    dest_mp3 = work / f"{stem}.mp3"
    local = Path(url)
    if local.is_file():
        try:
            if local.suffix.lower() == ".mp3":
                shutil.copy2(local, dest_mp3)
                return dest_mp3
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(local),
                    "-acodec",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(dest_mp3),
                ],
                check=True,
                timeout=120,
                capture_output=True,
            )
            if dest_mp3.is_file():
                return dest_mp3
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    for mp3_u in _myinstants_direct_mp3_urls(url):
        if download_sfx_bytes(mp3_u, dest_mp3, referer=url):
            return dest_mp3
    out_tpl = work / f"{stem}.%(ext)s"
    ytdl = _yt_dlp_argv()
    if ytdl:
        try:
            subprocess.run(
                ytdl + ["-x", "--audio-format", "mp3", "--no-playlist", "--no-warnings", "-o", str(out_tpl), url],
                check=True,
                timeout=120,
                capture_output=True,
                text=True,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    for p in sorted(work.glob(f"{stem}.*"), key=lambda x: x.stat().st_mtime, reverse=True):
        if p.suffix.lower() in {".mp3", ".m4a", ".wav", ".ogg"}:
            return p
    try:
        r = _ffv_session().get(
            url,
            headers={"User-Agent": _SFX_BROWSER_UA, "Accept": "text/html,*/*;q=0.8"},
            timeout=30,
        )
        r.raise_for_status()
        found: list[str] = []
        for m in re.finditer(r"https?://[^\s\"'<>]+\.mp3", r.text, flags=re.I):
            found.append(m.group(0))
        for m in re.finditer(r'(/media/sounds/[^\s\"\'<>]+\.mp3)', r.text, flags=re.I):
            found.append("https://www.myinstants.com" + m.group(1))
        for u in dict.fromkeys(found):
            if download_sfx_bytes(u, dest_mp3, referer=url):
                return dest_mp3
    except requests.RequestException:
        pass
    return None


_FFV_SFX_CTX_KEYWORDS: dict[str, tuple[str, ...]] = {
    "impact_meme": (
        "crash",
        "smash",
        "wreck",
        "fall",
        "drop",
        "explode",
        "boom",
        "chaos",
        "destroy",
        "fail",
        "wtf",
    ),
    "grossout_comedy": ("gross", "disgust", "fart", "poop", "toilet", "nasty"),
    "alarm_tech": ("error", "warning", "alert", "fail", "glitch", "bug", "bsod", "alarm"),
    "digital_ping": ("notification", "phone", "discord", "message", "ring", "ping"),
    "industrial_hit": ("metal", "pipe", "gear", "clang", "machin", "factory"),
    "dramatic_sting": ("suspense", "dramatic", "ominous", "mystery", "crime", "plot"),
    "sad_stinger": ("sad", "depress", "lonely", "heartbreak", "tragic", "rip"),
    "hype_stinger": ("win", "hype", "clutch", "legend", "epic", "boss", "insane"),
    "gaming_toon": ("game", "fortnite", "roblox", "minecraft", "mario", "fnaf", "npc"),
    "anime_reaction": ("anime", "waifu", "senpai", "kawaii"),
    "ironic_voice": ("bro", "sigma", "brainrot", "skibidi", "rizz", "cringe"),
}
_FFV_SFX_TOKEN_RE = re.compile(r"[a-z0-9_']+")
_FFV_SFX_HIGH_ENERGY_WORDS = frozenset(
    {
        "crash",
        "explosion",
        "explode",
        "fight",
        "chaos",
        "destroy",
        "panic",
        "insane",
        "crazy",
        "brutal",
        "rage",
        "epic",
        "massive",
    }
)
_FFV_SFX_LOW_ENERGY_WORDS = frozenset(
    {"sad", "quiet", "awkward", "slow", "calm", "gentle", "soft", "peaceful", "sleep", "boring"}
)
_FFV_SFX_HIGH_ENERGY_FAMS = frozenset(
    {"impact_meme", "industrial_hit", "percussion_hit", "alarm_tech", "hype_stinger"}
)
_FFV_SFX_LOW_ENERGY_FAMS = frozenset({"sad_stinger", "digital_ping", "anime_reaction"})


def _ffv_sfx_context_strength() -> float:
    try:
        v = float(os.environ.get("FFV_SFX_CONTEXT_STRENGTH", "0.55") or "0.55")
        return max(0.0, min(1.0, v))
    except ValueError:
        return 0.55


def _ffv_sfx_context_enabled() -> bool:
    return _env_flag_default("FFV_SFX_CONTEXT", default=True)


def _ffv_tts_enabled() -> bool:
    return _env_flag_default("FFV_TTS", default=True)


def _ffv_turbo_enabled() -> bool:
    return _env_flag_default("FFV_TURBO", default=True)


def _ffv_tts_trim_text(text: str, max_len: int = 260) -> str:
    if _ffv_turbo_enabled():
        max_len = min(max_len, 120)
    cleaned = " ".join((text or "").split())
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1].rstrip() + "…"
    return cleaned


def _ffv_tts_lang() -> str:
    raw = (os.environ.get("FFV_TTS_LANG") or "en").strip().lower()
    return raw or "en"


def _ffv_tts_voice_tracks_for_segment(bp: SegmentBlueprint) -> list[tuple[str, float, str]]:
    if not _ffv_tts_enabled():
        return []
    out: list[tuple[str, float, str]] = []
    react_text = _ffv_tts_trim_text(bp.reactable.title)
    if react_text:
        out.append(("react", 0.0, react_text))
    if not _ffv_turbo_enabled():
        reaction_text = _ffv_tts_trim_text(bp.reaction.title)
        if reaction_text:
            out.append(("reaction", REACTABLE_HOLD, f"Reaction: {reaction_text}"))
    return out


def _ffv_synthesize_tts_mp3(text: str, out_mp3: Path) -> bool:
    try:
        from gtts import gTTS  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        gTTS(text=text, lang=_ffv_tts_lang(), slow=False).save(str(out_mp3))
        return out_mp3.is_file() and out_mp3.stat().st_size > 256
    except Exception:
        out_mp3.unlink(missing_ok=True)
        return False


def _ffv_sfx_tokens(*parts: str) -> set[str]:
    out: set[str] = set()
    for p in parts:
        if not p:
            continue
        for t in _FFV_SFX_TOKEN_RE.findall(p.lower()):
            if len(t) >= 3:
                out.add(t)
    return out


def _ffv_sfx_desired_families(react_sub: str, react_title: str, reaction_title: str) -> set[str]:
    toks = _ffv_sfx_tokens(react_sub.replace("_", " "), react_title, reaction_title)
    desired: set[str] = set()
    for fam, kws in _FFV_SFX_CTX_KEYWORDS.items():
        if any(k in toks or any(t.startswith(k) for t in toks) for k in kws):
            desired.add(fam)
    return desired


def _ffv_sfx_target_energy(react_title: str, reaction_title: str, react_score: float, reaction_score: float) -> float:
    txt = _ffv_sfx_tokens(react_title, reaction_title)
    high_hits = len(txt & _FFV_SFX_HIGH_ENERGY_WORDS)
    low_hits = len(txt & _FFV_SFX_LOW_ENERGY_WORDS)
    lexical = 0.58 + 0.09 * high_hits - 0.08 * low_hits
    viral = 0.42 + min(0.52, (react_score + reaction_score) / 26.0)
    return max(0.18, min(1.0, 0.55 * lexical + 0.45 * viral))


def _ffv_sfx_url_canon(url: str) -> str:
    """Treat MyInstants URLs with/without ?utm as the same for repeat penalties."""
    try:
        return url.split("?", 1)[0].rstrip("/").lower()
    except (TypeError, AttributeError):
        return ""


def _ffv_sfx_family_energy(fams: frozenset[str]) -> float:
    val = 0.55
    if fams & _FFV_SFX_HIGH_ENERGY_FAMS:
        val += 0.22
    if fams & _FFV_SFX_LOW_ENERGY_FAMS:
        val -= 0.2
    if "generic" in fams:
        val -= 0.05
    return max(0.05, min(1.0, val))


def pick_weighted_sfx_url(
    corpus: TheoryCorpus,
    react_sub: str,
    state: FFVProjectState,
    *,
    react_title: str = "",
    reaction_title: str = "",
    react_score: float = 0.0,
    reaction_score: float = 0.0,
) -> str:
    forced = (os.environ.get("FFV_FORCE_SFX_URL") or "").strip()
    if forced:
        return forced
    diversity = float(os.environ.get("FFV_SFX_DIVERSITY", "0.52") or "0.52")
    diversity = max(0.0, min(1.0, diversity))
    try:
        url_lookback = int((os.environ.get("FFV_SFX_URL_LOOKBACK") or "10").strip())
    except ValueError:
        url_lookback = 10
    url_lookback = max(0, min(48, url_lookback))
    recent_url_keys = frozenset(
        _ffv_sfx_url_canon(u) for u in (state.recent_sfx_urls[-url_lookback:] if url_lookback else [])
    )
    rows = corpus.sfx_rows
    if not rows:
        return random.choice(corpus.sfx_urls)
    ctx_strength = _ffv_sfx_context_strength() if _ffv_sfx_context_enabled() else 0.0
    desired_fams = _ffv_sfx_desired_families(react_sub, react_title, reaction_title) if ctx_strength > 0 else set()
    target_energy = (
        _ffv_sfx_target_energy(react_title, reaction_title, react_score, reaction_score)
        if ctx_strength > 0
        else 0.55
    )
    weights: list[float] = []
    recent = frozenset(state.recent_sfx_families[-12:])
    for url, _slug, fams in rows:
        base = 1.0
        if recent_url_keys and _ffv_sfx_url_canon(url) in recent_url_keys:
            base *= max(0.06, 1.0 - 0.72 * diversity)
        if recent:
            overlap = len(fams & recent)
            base *= 1.0 - diversity * min(1.0, overlap * 0.28)
        base *= 0.85 + 0.15 * sfx_families_compatible_with_sub(fams, react_sub)
        if desired_fams:
            hit = len(desired_fams & fams)
            miss = max(0, len(desired_fams) - hit)
            fam_boost = 1.0 + ctx_strength * min(0.6, hit * 0.18)
            fam_penalty = 1.0 - ctx_strength * min(0.35, miss * 0.06)
            base *= max(0.35, fam_boost * fam_penalty)
        fe = _ffv_sfx_family_energy(fams)
        energy_align = 1.0 - min(0.8, abs(fe - target_energy))
        base *= 0.75 + 0.25 * energy_align
        base *= random.uniform(0.92, 1.09)
        weights.append(max(0.05, base))
    choice = random.choices([r[0] for r in rows], weights=weights, k=1)[0]
    slug = sfx_slug_from_url(choice)
    state.recent_sfx_families.extend(sfx_infer_families(slug))
    if len(state.recent_sfx_families) > 48:
        state.recent_sfx_families = state.recent_sfx_families[-48:]
    state.recent_sfx_urls.append(choice)
    if len(state.recent_sfx_urls) > 64:
        state.recent_sfx_urls = state.recent_sfx_urls[-64:]
    return choice


def _motion_zoompan_upscale_w(frame_w: int, frame_h: int) -> int:
    try:
        cap = int(os.environ.get("FFV_ZOOMPAN_MAX_W", "2880"))
        cap = max(1536, min(8192, cap))
    except ValueError:
        cap = 2880
    need = max(int(frame_w * 2.8), int(frame_h * 2.8), 2048)
    return min(cap, need)


def _motion_zoompan_vf(
    w: int, h: int, fps: int, duration: float, profile: str, *, fk: dict[str, Any] | None = None
) -> str:
    fk = fk or _ffmpeg_video_knobs()
    nf = max(2, int(round(duration * max(1, fps))))
    denom = max(1, nf - 1)
    zx = "iw/2-(iw/zoom/2)"
    zy = "ih/2-(ih/zoom/2)"
    if profile == "ken_in":
        ze = f"1+0.075*on/{denom}"
    elif profile == "ken_out":
        ze = f"1.1-0.1*on/{denom}"
    elif profile == "breathe":
        ze = f"1+0.04*sin(2*PI*on/{nf})"
    elif profile == "drift":
        ze = "1.06"
        zx = f"(iw/zoom-iw)*on/{denom}"
        zy = "ih/2-(ih/zoom/2)"
    else:
        ze = f"1+0.05*on/{denom}"
    up = _motion_zoompan_upscale_w(w, h)
    flags = _ffv_scale_flags(fk)
    return f"scale={up}:-1:flags={flags},zoompan=z='{ze}':x='{zx}':y='{zy}':d=1:s={w}x{h}:fps={fps}"


def image_to_video_segment(
    image: Path,
    duration: float,
    out_mp4: Path,
    w: int,
    h: int,
    *,
    fade_in: bool = False,
    fade_out: bool = False,
    motion: str | None = None,
    grade: bool = False,
    enhance_look: bool = False,
    look_is_video: bool = False,
    work: Path | None = None,
    chyron_tag: str | None = None,
    chyron_line: str | None = None,
    chyron_size: str = "h*0.026",
) -> None:
    fk = _ffmpeg_video_knobs()
    if _ffv_turbo_enabled():
        grade = False
        enhance_look = False
        image = _ffv_apply_cheap_image_effects(image, work, chyron_tag or f"img_{uuid.uuid4().hex[:6]}")
    fps = int(fk.get("still_fps", 30))
    flags = _ffv_scale_flags(fk)
    is_gif = image.suffix.lower() == ".gif"
    # GIF decodes as pal8; eq/fade need YUV. Still images use -loop 1; GIF is a video stream — use stream_loop.
    base = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags={flags},"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p,fps={fps}"
    )
    if motion and not is_gif:
        base = _motion_zoompan_vf(w, h, fps, duration, motion, fk=fk) + ",format=yuv420p"
    if grade:
        base += "," + random.choice(_FFV_GRADE_EQ_PRESETS)
    if enhance_look:
        ld_vig = True
        if _env_flag_default("FFV_VIDEO_SHARPEN", default=ld_vig) and random.random() < 0.42:
            base += ",unsharp=5:5:0.32:5:5:0.0"
        if _env_flag_default("FFV_VIDEO_VIGNETTE", default=ld_vig) and random.random() < 0.55:
            base += ",vignette=angle=PI/5"
        if _env_flag_default("FFV_VIDEO_GRAIN", default=False) and random.random() < 0.12:
            base += ",noise=alls=14:allf=t+u"
    vf = base
    if fade_in:
        vf += f",fade=t=in:st=0:d={REACTION_FADE_IN}"
    if fade_out:
        vf += f",fade=t=out:st={max(0.01, duration - REACTION_FADE_OUT)}:d={REACTION_FADE_OUT}"
    vf_pre_text = vf
    chyron_applied = False
    if (
        look_is_video
        and chyron_line
        and chyron_tag
        and work is not None
        and _env_flag_default("FFV_VIDEO_CHYRON", default=True)
    ):
        vf = _ffv_append_chyron_vf(
            vf, work=work, tag=chyron_tag, line=chyron_line, fontsize_expr=chyron_size
        )
        chyron_applied = True
    if is_gif:
        pre = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(image)]
    else:
        pre = ["ffmpeg", "-y", "-loop", "1", "-i", str(image)]
    xargs = _ffv_libx264_extra_argv(fk)
    argv = [
        *pre,
        "-vf",
        vf,
        "-t",
        str(duration),
        "-an",
        "-c:v",
        str(fk.get("video_codec", "libx264")),
        "-preset",
        str(fk.get("preset", "veryfast")),
        "-crf",
        str(fk.get("crf", "23")),
        "-pix_fmt",
        str(fk.get("pix_fmt", "yuv420p")),
        *xargs,
        *_ffv_mp4_container_args(),
        str(out_mp4),
    ]
    try:
        _run_ffmpeg(argv, timeout=max(180, int(duration * 45) + 90))
    except subprocess.CalledProcessError:
        if not chyron_applied:
            raise
        argv_retry = list(argv)
        ixf = argv_retry.index("-vf")
        argv_retry[ixf + 1] = vf_pre_text
        _run_ffmpeg(argv_retry, timeout=max(180, int(duration * 45) + 90))


def mux_segment_audio(
    video_silent: Path,
    sfx: Path | None,
    bg_music: Path | None,
    out_mp4: Path,
    segment_len: float,
    *,
    narration_tracks: list[tuple[Path, float]] | None = None,
    sfx_delay_sec: float | None = None,
) -> None:
    fk = _ffmpeg_video_knobs()
    delay_sec = float(REACTABLE_HOLD if sfx_delay_sec is None else sfx_delay_sec)
    delay_ms = max(0, int(round(delay_sec * 1000)))
    bg_reactable_vol = 0.3
    bg_reaction_vol = 0.3
    bg_vol_expr = (
        f"if(between(t\\,{delay_sec:.3f}\\,{segment_len:.3f})\\,{bg_reaction_vol:.3f}\\,{bg_reactable_vol:.3f})"
    )
    bg_ok = bg_music is not None and bg_music.is_file()
    sfx_ok = sfx is not None and sfx.is_file()
    narration_ok: list[tuple[Path, float]] = []
    for p, off in (narration_tracks or []):
        if p is not None and p.is_file():
            narration_ok.append((p, max(0.0, float(off))))
    narr_vol = _env_float("FFV_TTS_VOLUME", 1.8)
    narr_vol = max(0.1, min(2.0, narr_vol))
    vol = _ffv_sfx_mix_level()
    rh = float(REACTION_HOLD)
    argv = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r=44100:cl=stereo:d={segment_len}",
        "-i",
        str(video_silent),
    ]
    next_idx = 2
    sfx_idx: int | None = None
    bg_idx: int | None = None
    narr_entries: list[tuple[int, float]] = []
    if sfx_ok:
        sfx_idx = next_idx
        argv.extend(["-i", str(sfx)])
        next_idx += 1
    if bg_ok:
        bg_idx = next_idx
        argv.extend(["-i", str(bg_music)])
        next_idx += 1
    for npath, noff in narration_ok:
        narr_entries.append((next_idx, noff))
        argv.extend(["-i", str(npath)])
        next_idx += 1
    chains: list[str] = []
    mix_labels = ["[0:a]"]
    if sfx_idx is not None:
        if _env_flag_default("FFV_SFX_AFADE", default=True):
            fo = min(0.12, max(0.04, rh * 0.04))
            st_out = max(fo + 0.02, rh - fo)
            chains.append(
                f"[{sfx_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"atrim=0:{rh},asetpts=PTS-STARTPTS,volume={vol},"
                f"afade=t=in:st=0:d={fo:.3f},afade=t=out:st={st_out:.3f}:d={fo:.3f}[sfx]"
            )
        else:
            chains.append(
                f"[{sfx_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                f"atrim=0:{rh},asetpts=PTS-STARTPTS,volume={vol}[sfx]"
            )
        chains.append(f"[sfx]adelay={delay_ms}|{delay_ms}[ds]")
        mix_labels.append("[ds]")
    if bg_idx is not None:
        chains.append(
            f"[{bg_idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{segment_len},asetpts=PTS-STARTPTS,volume='{bg_vol_expr}'[bg]"
        )
        mix_labels.append("[bg]")
    for i, (idx, off_sec) in enumerate(narr_entries):
        off_ms = max(0, int(round(off_sec * 1000)))
        label = f"[n{i}]"
        chains.append(
            f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=stereo,"
            f"atrim=0:{segment_len},asetpts=PTS-STARTPTS,volume={narr_vol},"
            f"adelay={off_ms}|{off_ms}{label}"
        )
        mix_labels.append(label)
    if len(mix_labels) > 1:
        chains.append(
            "".join(mix_labels) + f"amix=inputs={len(mix_labels)}:duration=first:dropout_transition=0[aout]"
        )
        fc = ";".join(chains)
        argv.extend(["-filter_complex", fc, "-map", "1:v:0", "-map", "[aout]"])
    else:
        argv.extend(["-map", "1:v:0", "-map", "0:a:0"])
    argv.extend(
        [
            "-c:v",
            "copy",
            *_ffv_aac_audio_args(fk),
            "-t",
            str(segment_len),
            *_ffv_mp4_container_args(),
            str(out_mp4),
        ]
    )
    _run_ffmpeg(
        argv,
        timeout=300,
    )


def mux_continuous_bg_under(video_in: Path, bg_music: Path, video_out: Path) -> None:
    """
    Mix one background music pass under the full compiled video (no restart each segment).
    Expects video_in to already carry segment SFX + silence; bg is trimmed/looped to match duration.
    """
    if not bg_music.is_file():
        shutil.copy(video_in, video_out)
        return
    if not _ffprobe_has_audio(video_in):
        shutil.copy(video_in, video_out)
        return
    fk = _ffmpeg_video_knobs()
    dur = max(0.1, _ffprobe_duration(video_in))
    try:
        vol = float((os.environ.get("FFV_BG_CONTINUOUS_VOL") or "0.3").strip())
    except ValueError:
        vol = 0.36
    vol = max(0.05, min(0.85, vol))
    fc = (
        f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
        f"atrim=0:{dur:.5f},asetpts=PTS-STARTPTS,volume={vol}[bgm];"
        f"[0:a][bgm]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_in),
            "-stream_loop",
            "-1",
            "-i",
            str(bg_music),
            "-filter_complex",
            fc,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:v",
            "copy",
            *_ffv_aac_audio_args(fk),
            "-t",
            str(dur),
            *_ffv_mp4_container_args(),
            str(video_out),
        ],
        timeout=max(600.0, dur * 3.0 + 180.0),
    )


def _xfade_join_two_clips(a: Path, b: Path, out: Path, td: float, transition: str) -> None:
    fk = _ffmpeg_video_knobs()
    off = max(0.05, REACTABLE_HOLD - td)
    dur_out = REACTABLE_HOLD + REACTION_HOLD - td
    last_err: Exception | None = None
    for tr in (transition, "fade", "wipeleft"):
        fc = f"[0:v][1:v]xfade=transition={tr}:duration={td}:offset={off:.5f}[v]"
        try:
            subprocess.run(
                [
                    *_ffv_ffmpeg_std_head(),
                    "-y",
                    "-i",
                    str(a),
                    "-i",
                    str(b),
                    "-filter_complex",
                    fc,
                    "-map",
                    "[v]",
                    "-an",
                    "-c:v",
                    str(fk.get("video_codec", "libx264")),
                    "-preset",
                    str(fk.get("preset", "veryfast")),
                    "-crf",
                    str(fk.get("crf", "23")),
                    "-pix_fmt",
                    str(fk.get("pix_fmt", "yuv420p")),
                    *_ffv_libx264_extra_argv(fk),
                    "-t",
                    str(dur_out),
                    *_ffv_mp4_container_args(),
                    *_ffv_ffmpeg_strip_metadata_argv(str(out)),
                    str(out),
                ],
                check=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=_ffmpeg_stderr_sink(),
                timeout=max(120, int(dur_out * 20)),
            )
            return
        except subprocess.CalledProcessError as e:
            last_err = e
    raise RuntimeError(f"xfade join failed: {last_err}") from last_err


def concat_mp4_list(parts: list[Path], out_mp4: Path) -> None:
    lst = out_mp4.parent / "_ffv_concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write(f"file '{p.resolve().as_posix()}'\n")
    try:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c",
                "copy",
                *_ffv_mp4_container_args(),
                str(out_mp4),
            ],
            timeout=max(300, 120 * len(parts)),
        )
    except subprocess.CalledProcessError:
        fk = _ffmpeg_video_knobs()
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(lst),
                "-c:v",
                str(fk.get("video_codec", "libx264")),
                "-preset",
                str(fk.get("preset", "veryfast")),
                "-crf",
                str(fk.get("crf", "23")),
                "-pix_fmt",
                str(fk.get("pix_fmt", "yuv420p")),
                *_ffv_libx264_extra_argv(fk),
                *_ffv_aac_audio_args(fk),
                *_ffv_mp4_container_args(),
                str(out_mp4),
            ],
            timeout=max(600, 180 * len(parts)),
        )
    lst.unlink(missing_ok=True)


def concat_mp4_list_with_transitions(parts: list[Path], out_mp4: Path, td: float) -> None:
    if td <= 0 or len(parts) < 2:
        concat_mp4_list(parts, out_mp4)
        return
    if not all(_ffprobe_has_audio(p) for p in parts):
        concat_mp4_list(parts, out_mp4)
        return
    durs = [_ffprobe_duration(p) for p in parts]
    fk = _ffmpeg_video_knobs()
    ins: list[str] = []
    for p in parts:
        ins.extend(["-i", str(p)])
    v_lb = "[0:v]"
    a_lb = "[0:a]"
    L = durs[0]
    fcs: list[str] = []
    for i in range(1, len(parts)):
        tr = _pick_xfade_transition()
        off = max(0.0, L - td)
        vn = f"[vx{i}]"
        an = f"[ax{i}]"
        fcs.append(f"{v_lb}[{i}:v]xfade=transition={tr}:duration={td}:offset={off:.5f}{vn}")
        fcs.append(f"{a_lb}[{i}:a]acrossfade=d={td}:c1=tri:c2=tri{an}")
        v_lb, a_lb = vn, an
        L += durs[i] - td
    fc = ";".join(fcs)
    try:
        subprocess.run(
            [
                *_ffv_ffmpeg_std_head(),
                "-y",
                *ins,
                "-filter_complex",
                fc,
                "-map",
                v_lb,
                "-map",
                a_lb,
                "-c:v",
                str(fk.get("video_codec", "libx264")),
                "-preset",
                str(fk.get("preset", "veryfast")),
                "-crf",
                str(fk.get("crf", "23")),
                "-pix_fmt",
                str(fk.get("pix_fmt", "yuv420p")),
                *_ffv_libx264_extra_argv(fk),
                *_ffv_aac_audio_args(fk),
                *_ffv_mp4_container_args(),
                *_ffv_ffmpeg_strip_metadata_argv(str(out_mp4)),
                str(out_mp4),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=_ffmpeg_stderr_sink(),
            timeout=max(900, 400 * len(parts)),
        )
    except subprocess.CalledProcessError:
        concat_mp4_list(parts, out_mp4)


def audit_log(session_dir: Path, event: dict[str, Any]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    logf = session_dir / "audit.jsonl"
    event = dict(event)
    event["ts"] = datetime.now(timezone.utc).isoformat()
    with open(logf, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_render_dag(
    nodes: list[RenderDAGNode],
    runner: Callable[[RenderDAGNode], None],
    state: FFVProjectState,
    *,
    progress_total: int | None = None,
    progress_label_for: Callable[[RenderDAGNode, int], str] | None = None,
) -> bool:
    snap: list[dict[str, Any]] = []
    ok = True
    use_prog = progress_total is not None and progress_label_for is not None
    for i, n in enumerate(nodes):
        n.status = "running"
        n.t0 = time.time()
        stage_cm = (
            _ffv_progress_stage(i, progress_total, progress_label_for(n, i))
            if use_prog
            else contextlib.nullcontext()
        )
        with stage_cm:
            try:
                runner(n)
                n.status = "ok"
            except Exception as exc:
                n.status = "fail"
                n.detail = str(exc)
                ok = False
        n.t1 = time.time()
        snap.append(
            {
                "id": n.node_id,
                "kind": n.kind,
                "status": n.status,
                "detail": n.detail,
                "ms": int((n.t1 - n.t0) * 1000),
            }
        )
    state.dag_history.append(snap)
    return ok


def _ffv_pool_fetch_workers(n_subs: int) -> int:
    raw = (os.environ.get("FFV_POOL_WORKERS") or "").strip()
    if raw == "1":
        return 1
    if raw.isdigit() and int(raw) >= 1:
        return min(12, int(raw))
    if n_subs <= 1:
        return 1
    return min(8, max(2, n_subs))


def collect_pool(corpus: TheoryCorpus, subs: list[str], per_sub: int = 24) -> list[dict[str, Any]]:
    if not subs:
        return []
    if os.environ.get("FFV_POOL_SERIAL", "").strip().lower() in ("1", "true", "yes", "on"):
        pool: list[dict[str, Any]] = []
        for sub in subs:
            try:
                pool.extend(fetch_image_posts(sub, limit=per_sub))
            except requests.RequestException:
                continue
        return pool
    workers = _ffv_pool_fetch_workers(len(subs))
    if workers <= 1:
        pool = []
        for sub in subs:
            try:
                pool.extend(fetch_image_posts(sub, limit=per_sub))
            except requests.RequestException:
                continue
        return pool
    pool = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(fetch_image_posts, sub, per_sub) for sub in subs]
        for fut in as_completed(futures):
            try:
                pool.extend(fut.result())
            except requests.RequestException:
                continue
    return pool


def _ffv_segment_encode_workers() -> int:
    raw = (os.environ.get("FFV_SEGMENT_WORKERS") or "").strip()
    if raw == "1":
        return 1
    if raw.isdigit() and int(raw) >= 1:
        return min(10, int(raw))
    cpu = os.cpu_count() or 4
    if _ffv_turbo_enabled():
        return min(12, max(4, cpu))
    return min(6, max(2, (cpu + 1) // 2))


def plan_segment(
    corpus: TheoryCorpus,
    react_pool: list[dict[str, Any]],
    react_img_pool: list[dict[str, Any]],
    index: int,
    state: FFVProjectState,
    *,
    exclude_react: frozenset[str] | None = None,
    exclude_re: frozenset[str] | None = None,
    exclude_react_subs: frozenset[str] | None = None,
    exclude_reaction_subs: frozenset[str] | None = None,
    strict_dedupe: bool = False,
) -> SegmentBlueprint:
    react_rows = react_pool
    if exclude_react_subs:
        filtered = [c for c in react_pool if str(c.get("_ffv_sub") or "") not in exclude_react_subs]
        if len(filtered) >= max(6, len(react_pool) // 6):
            react_rows = filtered
    reaction_rows = react_img_pool
    if exclude_reaction_subs:
        filtered = [c for c in react_img_pool if str(c.get("_ffv_sub") or "") not in exclude_reaction_subs]
        if len(filtered) >= max(4, len(react_img_pool) // 6):
            reaction_rows = filtered
    rc, ri, rw = weighted_pick_ranked(
        react_rows, ViralityIndex.score, exclude_ids=exclude_react, relax_if_small=not strict_dedupe
    )
    rr, rri, rrw = weighted_pick_ranked(
        reaction_rows, ViralityIndex.score, exclude_ids=exclude_re, relax_if_small=not strict_dedupe
    )
    react_sub = str(rc.get("_ffv_sub") or "")
    sfx_url = pick_weighted_sfx_url(
        corpus,
        react_sub,
        state,
        react_title=str(rc.get("title") or ""),
        reaction_title=str(rr.get("title") or ""),
        react_score=ViralityIndex.score(rc),
        reaction_score=ViralityIndex.score(rr),
    )
    state.last_reactable_sub = react_sub
    return SegmentBlueprint(
        index=index,
        reactable=reddit_pick_to_record(rc, ri, rw),
        reaction=reddit_pick_to_record(rr, rri, rrw),
        sfx_url=sfx_url,
    )


def _record_unique_keys(r: RedditPickRecord) -> set[str]:
    keys: set[str] = set()
    if r.post_id:
        keys.add(f"id:{r.post_id}")
    if r.url:
        keys.add(f"url:{r.url.strip().lower()}")
    if r.permalink:
        keys.add(f"perm:{r.permalink.strip().lower()}")
    t = " ".join((r.title or "").split()).strip().lower()
    if t:
        keys.add(f"title:{t}")
    return keys


def _url_ext(url: str) -> str:
    path = Path(url.split("?")[0])
    ext = path.suffix.lower() if path.suffix else ".jpg"
    return ext if ext in _IMG_EXT else ".jpg"


def _ffv_img_suffix_canon(suffix: str) -> str:
    s = (suffix or "").lower()
    if s in (".jpeg", ".jpe"):
        return ".jpg"
    return s


def _ffv_head_bytes(path: Path, nbytes: int = 65536) -> bytes:
    data = path.read_bytes()[:nbytes]
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data


def _ffv_sniff_image_suffix(buf: bytes) -> str | None:
    if len(buf) < 12:
        return None
    if buf.startswith(b"\x89PNG\r\n\x1a\n") or buf.startswith(b"\x89PNG\n\x1a\n"):
        return ".png"
    if buf.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if buf.startswith(b"GIF87a") or buf.startswith(b"GIF89a"):
        return ".gif"
    if buf.startswith(b"RIFF") and len(buf) >= 12 and buf[8:12] == b"WEBP":
        return ".webp"
    if buf.startswith(b"BM"):
        return ".bmp"
    if len(buf) >= 12 and buf[4:8] == b"ftyp":
        blob = buf[:64]
        if b"avif" in blob or b"avis" in blob:
            return ".avif"
        if b"heic" in blob or b"heix" in blob or b"mif1" in blob or b"msf1" in blob:
            return ".heic"
    if buf.startswith(b"\x00\x00\x00") and b"ftyp" in buf[:32]:
        blob = buf[:64]
        if b"avif" in blob or b"avis" in blob:
            return ".avif"
        if b"heic" in blob or b"mif1" in blob:
            return ".heic"
    return None


def _ffv_normalize_image_file(path: Path) -> Path:
    head = _ffv_head_bytes(path, 8192)
    strip = head.lstrip()
    if strip.startswith((b"<!DOCTYPE", b"<!doctype", b"<html", b"<HTML", b"<?xml")):
        path.unlink(missing_ok=True)
        raise RuntimeError(f"image URL returned HTML/XML instead of an image ({path.name})")
    suf = _ffv_sniff_image_suffix(head[:256])
    if not suf:
        path.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded file is not a known image; first bytes {head[:8]!r} ({path.name})")
    if path.suffix.lower() == suf:
        return path
    newp = path.with_suffix(suf)
    if newp.resolve() != path.resolve() and newp.exists():
        newp.unlink()
    path.rename(newp)
    return newp


def _ffv_skip_image_reencode() -> bool:
    return os.environ.get("FFV_SKIP_IMAGE_REENCODE", "").strip().lower() in ("1", "true", "yes", "on")


def _ffv_demux_still_to_jpeg(src: Path, work: Path, key: str) -> Path:
    """
    Decode bytes with ffmpeg using a neutral .bin name so the demuxer probes content,
    not the URL suffix. Fixes i.redd.it-style JPEG/WebP/AVIF served on .png paths.
    """
    if _ffv_skip_image_reencode():
        return src
    raw = work / f"{key}_probe.bin"
    dst = work / f"{key}_ff.jpg"
    shutil.copy2(src, raw)
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "quiet",
                *_ffv_ffmpeg_threads_prefix(),
                "-y",
                "-i",
                str(raw),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(dst),
            ],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
        )
        if dst.is_file() and dst.stat().st_size > 200:
            if src.resolve() != dst.resolve() and src.is_file():
                try:
                    src.unlink()
                except OSError:
                    pass
            return dst
    except (subprocess.CalledProcessError, OSError, subprocess.TimeoutExpired):
        pass
    finally:
        raw.unlink(missing_ok=True)
        if dst.is_file() and dst.stat().st_size <= 200:
            dst.unlink(missing_ok=True)
    return src


def _ffv_maybe_demux_still_to_jpeg(src: Path, work: Path, key: str, url_ext: str) -> Path:
    """
    Decode via ffmpeg to JPEG when URL/extension may lie (e.g. JPEG on .png Reddit URLs).

    Only skip demux when URL and sniffed file type both agree on JPEG — matching .png/.webp
    is unsafe because CDNs often serve a different codec under that suffix.
    """
    if _ffv_skip_image_reencode():
        return src
    if src.suffix.lower() == ".gif":
        return src
    if os.environ.get("FFV_ALWAYS_IMAGE_DEMUX", "").strip().lower() in ("1", "true", "yes", "on"):
        return _ffv_demux_still_to_jpeg(src, work, key)
    ua = _ffv_img_suffix_canon(url_ext)
    sa = _ffv_img_suffix_canon(src.suffix)
    if ua == sa and sa == ".jpg":
        return src
    return _ffv_demux_still_to_jpeg(src, work, key)


def build_segment_file(
    bp: SegmentBlueprint,
    work: Path,
    w: int,
    h: int,
    *,
    vertical_pack: bool = False,
    bg_music_track: Path | None = None,
) -> Path:
    work.mkdir(parents=True, exist_ok=True)
    stem = f"seg_{bp.index:03d}"
    ext_r = _url_ext(bp.reactable.url)
    react_img = work / f"{stem}_react{ext_r}"
    if not download_bytes(bp.reactable.url, react_img):
        raise RuntimeError(f"reactable image fetch failed {bp.reactable.url[:120]}")
    react_img = _ffv_normalize_image_file(react_img)
    react_img = _ffv_maybe_demux_still_to_jpeg(react_img, work, f"{stem}_react", ext_r)
    ext_i = _url_ext(bp.reaction.url)
    re_img = work / f"{stem}_re{ext_i}"
    if not download_bytes(bp.reaction.url, re_img):
        raise RuntimeError("reaction image fetch failed")
    re_img = _ffv_normalize_image_file(re_img)
    re_img = _ffv_maybe_demux_still_to_jpeg(re_img, work, f"{stem}_re", ext_i)
    p1 = work / f"{stem}_a.mp4"
    p2 = work / f"{stem}_b.mp4"
    motion_r: str | None = None
    motion_re: str | None = None
    grade_r = grade_re = False
    inner_td = 0.0
    inner_tr = ""
    enhance = vertical_pack or _env_flag("FFV_SHORT_LOOK", False)
    look_video = vertical_pack
    if vertical_pack:
        motion_opts = ("ken_in", "ken_out", "breathe", "drift")
        if _env_flag("FFV_VIDEO_MOTION", True):
            motion_r = random.choice(motion_opts)
            motion_re = random.choice(motion_opts)
        if _env_flag("FFV_VIDEO_GRADE", True):
            grade_r = random.random() < 0.62
            grade_re = random.random() < 0.62
    elif enhance and _env_flag("FFV_VIDEO_GRADE", True):
        grade_r = random.random() < 0.42
        grade_re = random.random() < 0.42
    ch_r = ch_re = None
    if look_video and _env_flag_default("FFV_VIDEO_CHYRON", default=True):
        ch_r = f"r/{bp.reactable.subreddit} · {bp.reactable.title}".strip()
        ch_re = f"r/{bp.reaction.subreddit} · reaction".strip()
    image_to_video_segment(
        react_img,
        REACTABLE_HOLD,
        p1,
        w,
        h,
        motion=motion_r,
        grade=grade_r,
        enhance_look=enhance,
        look_is_video=look_video,
        work=work,
        chyron_tag=f"{stem}_react" if ch_r else None,
        chyron_line=ch_r,
        chyron_size="h*0.028",
    )
    image_to_video_segment(
        re_img,
        REACTION_HOLD,
        p2,
        w,
        h,
        fade_in=True,
        fade_out=True,
        motion=motion_re,
        grade=grade_re,
        enhance_look=enhance,
        look_is_video=look_video,
        work=work,
        chyron_tag=f"{stem}_re" if ch_re else None,
        chyron_line=ch_re,
        chyron_size="h*0.023",
    )
    vcat = work / f"{stem}_silent.mp4"
    if vertical_pack:
        inner_td = _video_inner_xfade_sec()
    if vertical_pack and inner_td >= 0.04 and inner_td <= REACTABLE_HOLD - 0.1:
        inner_tr = _pick_xfade_transition()
        try:
            _xfade_join_two_clips(p1, p2, vcat, inner_td, inner_tr)
        except RuntimeError:
            concat_mp4_list([p1, p2], vcat)
            inner_td = 0.0
            inner_tr = ""
    else:
        concat_mp4_list([p1, p2], vcat)
    seg_len = _ffprobe_duration(vcat)
    sfx_delay = max(0.0, REACTABLE_HOLD - inner_td)
    sfx_path = download_sfx(bp.sfx_url, work, f"{stem}_sfx")
    effective_bg_music = None if sfx_is_music_like_url(bp.sfx_url) else bg_music_track
    narration_tracks: list[tuple[Path, float]] = []
    for tts_tag, tts_offset, tts_text in _ffv_tts_voice_tracks_for_segment(bp):
        narr_mp3 = work / f"{stem}_{tts_tag}_tts.mp3"
        if _ffv_synthesize_tts_mp3(tts_text, narr_mp3):
            narration_tracks.append((narr_mp3, tts_offset))
    final = work / f"{stem}_final.mp4"
    mux_segment_audio(
        vcat,
        sfx_path,
        effective_bg_music,
        final,
        seg_len,
        narration_tracks=narration_tracks,
        sfx_delay_sec=sfx_delay,
    )
    return final


def next_ffv_index(prefix: str) -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    nums: list[int] = []
    for p in OUTPUT_DIR.glob(f"{prefix}_*.mp4"):
        m = re.search(rf"{re.escape(prefix)}_(\d+)\.mp4$", p.name, re.I)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def _write_dry_run_manifest(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def render_short(state: FFVProjectState, corpus: TheoryCorpus) -> Path:
    global _FFV_PROGRESS_ACTIVE, _FFV_PIPELINE_T0
    _maybe_print_phase("FFV_P11_BLUEPRINT_MATERIALIZE → single 9:16 segment")
    sid = state.session_id
    sdir = FFV_SESSIONS / sid
    work = FFV_ASSETS / "work" / f"{sid}_{uuid.uuid4().hex[:10]}"
    pool_r: list[dict[str, Any]] = []
    pool_i: list[dict[str, Any]] = []
    idx = next_ffv_index("ffv_short")
    title = _ffv_make_upload_title(state, is_video=False, out_index=idx)
    dest = _ffv_titled_output_path("ffv_short", idx, title)
    dry_manifest = OUTPUT_DIR / f"ffv_short_{idx}.dryrun.json"
    total_steps = 3

    def _dag_label(n: RenderDAGNode, i: int) -> str:
        return {
            "pool_reactables": "1/3 Fetch reactable posts",
            "pool_reactions": "2/3 Fetch reaction posts",
            "compose": "3/3 Encode segment + mux audio",
        }.get(n.kind, f"{i + 1}/3")

    try:
        audit_log(sdir, {"event": "render_short_start", "digest": corpus.digest(), "dry_run": _dry_run_enabled()})
        _FFV_PROGRESS_ACTIVE = True
        _FFV_PIPELINE_T0 = time.time()
        _ffv_pipeline_progress(0.0, total_steps, "FFV short · starting")

        def _node_run(n: RenderDAGNode) -> None:
            if n.kind == "pool_reactables":
                n.detail = "fetching reactable subs"
                subs_ord = ffv_order_subs_for_image_fetch(list(corpus.reactable_subs), reaction=False)
                cap = 160
                if len(subs_ord) <= cap:
                    pick = list(subs_ord)
                else:
                    head = subs_ord[:90]
                    tail_pool = subs_ord[90:]
                    k = min(70, len(tail_pool))
                    pick = head + (random.sample(tail_pool, k) if k else [])
                pool_r.clear()
                pool_r.extend(collect_pool(corpus, pick, per_sub=120))
                if len(pool_r) < 8:
                    more = [s for s in subs_ord if s not in pick]
                    random.shuffle(more)
                    for i in range(0, len(more), 40):
                        if len(pool_r) >= 10:
                            break
                        chunk = more[i : i + 40]
                        if chunk:
                            pool_r.extend(collect_pool(corpus, chunk, per_sub=96))
            elif n.kind == "pool_reactions":
                n.detail = "fetching reaction subs"
                sub_i = ffv_order_subs_for_image_fetch(list(corpus.reaction_subs), reaction=True)
                pool_i.clear()
                pool_i.extend(collect_pool(corpus, sub_i, per_sub=64))
            elif n.kind == "compose":
                n.detail = "encode 9:16 segment"
                if len(pool_r) < 4 or len(pool_i) < 2:
                    raise RuntimeError(
                        "not enough Reddit image posts (set PTK_UA or FFV_UA, try FFV_REDDIT_JSON_ATTEMPTS=8, or VPN)"
                    )
                bp = plan_segment(corpus, pool_r, pool_i, 0, state)
                audit_log(sdir, {"event": "blueprint", "blueprint": asdict(bp)})
                if _dry_run_enabled():
                    _write_dry_run_manifest(dry_manifest, {"mode": "short", "blueprint": asdict(bp)})
                    return
                bg_track = _ffv_pick_bg_music_track()
                if bg_track is not None:
                    audit_log(sdir, {"event": "bg_music_pick", "track": bg_track.name, "mode": "short"})
                outv = build_segment_file(
                    bp,
                    work,
                    SHORT_W,
                    SHORT_H,
                    bg_music_track=bg_track,
                )
                shutil.move(str(outv), str(dest))

        nodes = [
            RenderDAGNode("n1", "pool_reactables", "pending"),
            RenderDAGNode("n2", "pool_reactions", "pending"),
            RenderDAGNode("n3", "compose", "pending"),
        ]
        try:
            ok = run_render_dag(
                nodes,
                _node_run,
                state,
                progress_total=total_steps,
                progress_label_for=_dag_label,
            )
            if not ok:
                sys.stdout.write("\n")
                failed = next((x for x in reversed(nodes) if x.status == "fail"), None)
                raise RuntimeError((failed.detail if failed else None) or "render failed")
            if _dry_run_enabled():
                _ffv_pipeline_progress(
                    total_steps, total_steps, "FFV short complete (dry run)", complete=True
                )
                audit_log(
                    sdir,
                    {
                        "event": "render_short_ok",
                        "path": dry_manifest.name,
                        "dry_run": True,
                        "title": title,
                    },
                )
                return dry_manifest
            _ffv_pipeline_progress(total_steps, total_steps, "FFV short complete", complete=True)
            audit_log(sdir, {"event": "render_short_ok", "path": dest.name, "title": title})
            print(f"{C_OK}title:{C_RESET} {title}")
            return dest
        except BaseException:
            sys.stdout.write("\n")
            raise
    except Exception as exc:
        audit_log(sdir, {"event": "render_short_fail", "error": str(exc)})
        raise
    finally:
        _FFV_PROGRESS_ACTIVE = False
        _FFV_PIPELINE_T0 = None
        shutil.rmtree(work, ignore_errors=True)


def render_vertical_video(state: FFVProjectState, corpus: TheoryCorpus, n_segments: int) -> Path:
    global _FFV_PROGRESS_ACTIVE, _FFV_PIPELINE_T0
    if n_segments < VIDEO_SEGMENTS_MIN or n_segments > VIDEO_SEGMENTS_MAX:
        raise RuntimeError(f"segment count must be {VIDEO_SEGMENTS_MIN}–{VIDEO_SEGMENTS_MAX}")
    _maybe_print_phase(f"FFV_P23_VERTICAL_CONCAT_LIST → {n_segments} segments")
    sid = state.session_id
    sdir = FFV_SESSIONS / sid
    work = FFV_ASSETS / "work" / f"{sid}_{uuid.uuid4().hex[:10]}"
    segs: list[Path] = []
    used_react: set[str] = set()
    used_re: set[str] = set()
    used_react_subs: set[str] = set()
    used_reaction_subs: set[str] = set()
    total_steps = n_segments + 2
    try:
        audit_log(sdir, {"event": "render_video_start", "segments": n_segments, "dry_run": _dry_run_enabled()})
        _FFV_PROGRESS_ACTIVE = True
        _FFV_PIPELINE_T0 = time.time()
        _ffv_pipeline_progress(0.0, total_steps, "FFV video · starting")
        try:
            with _ffv_progress_stage(
                0,
                total_steps,
                f"1/{total_steps} Fetch react + reaction pools",
            ):
                subs_ord = ffv_order_subs_for_image_fetch(list(corpus.reactable_subs), reaction=False)
                cap = 200
                if len(subs_ord) <= cap:
                    pick = list(subs_ord)
                else:
                    head = subs_ord[:120]
                    tail_pool = subs_ord[120:]
                    k = min(80, len(tail_pool))
                    pick = head + (random.sample(tail_pool, k) if k else [])
                pool_r = collect_pool(corpus, pick, per_sub=140)
                if len(pool_r) < 8:
                    more = [s for s in subs_ord if s not in pick]
                    for i in range(0, len(more), 40):
                        if len(pool_r) >= 12:
                            break
                        chunk = more[i : i + 40]
                        if chunk:
                            pool_r.extend(collect_pool(corpus, chunk, per_sub=110))
                sub_i = ffv_order_subs_for_image_fetch(list(corpus.reaction_subs), reaction=True)
                pool_i = collect_pool(corpus, sub_i, per_sub=88)
                if len(pool_r) < 6 or len(pool_i) < 3:
                    raise RuntimeError(
                        "not enough Reddit image posts for video compile "
                        "(set PTK_UA or FFV_UA, try FFV_REDDIT_JSON_ATTEMPTS=8, or VPN)"
                    )
            vid_idx = next_ffv_index("ffv_video")
            title = _ffv_make_upload_title(
                state,
                is_video=True,
                out_index=vid_idx,
                n_segments=n_segments,
            )
            out = _ffv_titled_output_path("ffv_video", vid_idx, title)
            dry_manifest = OUTPUT_DIR / f"ffv_video_{vid_idx}.dryrun.json"
            if _dry_run_enabled():
                bps: list[dict[str, Any]] = []
                for i in range(n_segments):
                    with _ffv_progress_stage(
                        1 + i,
                        total_steps,
                        f"{i + 2}/{total_steps} Plan segment {i + 1}/{n_segments} (dry)",
                    ):
                        bp = plan_segment(
                            corpus,
                            pool_r,
                            pool_i,
                            i,
                            state,
                            exclude_react=frozenset(used_react),
                            exclude_re=frozenset(used_re),
                            exclude_react_subs=frozenset(used_react_subs),
                            exclude_reaction_subs=frozenset(used_reaction_subs),
                            strict_dedupe=True,
                        )
                        used_react.update(_record_unique_keys(bp.reactable))
                        used_re.update(_record_unique_keys(bp.reaction))
                        used_react_subs.add(bp.reactable.subreddit)
                        used_reaction_subs.add(bp.reaction.subreddit)
                        audit_log(sdir, {"event": "segment_blueprint", "i": i, "bp": asdict(bp)})
                        bps.append(asdict(bp))
                with _ffv_progress_stage(
                    1 + n_segments,
                    total_steps,
                    f"{n_segments + 2}/{total_steps} Write dry-run manifest",
                ):
                    _write_dry_run_manifest(
                        dry_manifest,
                        {
                            "mode": "video",
                            "segments": n_segments,
                            "title": title,
                            "blueprints": bps,
                        },
                    )
                audit_log(
                    sdir,
                    {
                        "event": "render_video_ok",
                        "path": dry_manifest.name,
                        "dry_run": True,
                        "title": title,
                    },
                )
                _ffv_pipeline_progress(
                    total_steps, total_steps, "FFV video complete (dry run)", complete=True
                )
                return dry_manifest
            bps: list[SegmentBlueprint] = []
            bg_track = _ffv_pick_bg_music_track()
            if bg_track is not None:
                audit_log(sdir, {"event": "bg_music_pick", "track": bg_track.name, "mode": "video"})
            _ffv_pipeline_progress(1.0, total_steps, f"Plan {n_segments} segments")
            for i in range(n_segments):
                bp = plan_segment(
                    corpus,
                    pool_r,
                    pool_i,
                    i,
                    state,
                    exclude_react=frozenset(used_react),
                    exclude_re=frozenset(used_re),
                    exclude_react_subs=frozenset(used_react_subs),
                    exclude_reaction_subs=frozenset(used_reaction_subs),
                    strict_dedupe=True,
                )
                used_react.update(_record_unique_keys(bp.reactable))
                used_re.update(_record_unique_keys(bp.reaction))
                used_react_subs.add(bp.reactable.subreddit)
                used_reaction_subs.add(bp.reaction.subreddit)
                audit_log(sdir, {"event": "segment_blueprint", "i": i, "bp": asdict(bp)})
                bps.append(bp)
            seg_workers = _ffv_segment_encode_workers()
            serial_seg = os.environ.get("FFV_SEGMENT_SERIAL", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            )
            if seg_workers <= 1 or serial_seg:
                segs = []
                for i in range(n_segments):
                    with _ffv_progress_stage(
                        1 + i,
                        total_steps,
                        f"{i + 2}/{total_steps} Build segment {i + 1}/{n_segments}",
                    ):
                        segs.append(
                            build_segment_file(
                                bps[i],
                                work,
                                VERT_W,
                                VERT_H,
                                vertical_pack=True,
                                bg_music_track=None,
                            )
                        )
            else:
                seg_lock = threading.Lock()
                built: dict[int, Path] = {}

                def _encode_one(idx: int) -> None:
                    p = build_segment_file(
                        bps[idx],
                        work,
                        VERT_W,
                        VERT_H,
                        vertical_pack=True,
                        bg_music_track=None,
                    )
                    with seg_lock:
                        built[idx] = p
                        done = len(built)
                        _ffv_pipeline_progress(
                            float(1 + done),
                            total_steps,
                            f"Build segments ({done}/{n_segments})",
                        )

                with ThreadPoolExecutor(max_workers=seg_workers) as ex:
                    futs = [ex.submit(_encode_one, i) for i in range(n_segments)]
                    for fut in futs:
                        fut.result()
                segs = [built[i] for i in range(n_segments)]
            with _ffv_progress_stage(
                1 + n_segments,
                total_steps,
                f"{n_segments + 2}/{total_steps} Concat + xfade timeline",
            ):
                concat_mp4_list_with_transitions(segs, out, _video_outer_xfade_sec())
            if bg_track is not None and bg_track.is_file() and _ffprobe_has_audio(out):
                tmp_bg = work / f"_ffv_full_bg_{uuid.uuid4().hex[:10]}.mp4"
                try:
                    mux_continuous_bg_under(out, bg_track, tmp_bg)
                    out.unlink(missing_ok=True)
                    shutil.move(str(tmp_bg), str(out))
                    audit_log(
                        sdir,
                        {"event": "bg_music_mux_full", "track": bg_track.name, "mode": "video_continuous"},
                    )
                except (subprocess.CalledProcessError, OSError) as e:
                    audit_log(
                        sdir,
                        {"event": "bg_music_mux_full_fail", "error": str(e), "track": bg_track.name},
                    )
            audit_log(sdir, {"event": "render_video_ok", "path": out.name, "title": title})
            _ffv_pipeline_progress(total_steps, total_steps, "FFV video complete", complete=True)
            print(f"{C_OK}title:{C_RESET} {title}")
            return out
        except BaseException:
            sys.stdout.write("\n")
            raise
    except Exception as exc:
        audit_log(sdir, {"event": "render_video_fail", "error": str(exc)})
        raise
    finally:
        _FFV_PROGRESS_ACTIVE = False
        _FFV_PIPELINE_T0 = None
        shutil.rmtree(work, ignore_errors=True)


def _print_ffv_banner() -> None:
    print(
        f"{C_ACCENT}  theory-driven reaction pipeline  "
        f"{C_FRAME}v{FFV_VERSION}{C_RESET}\n"
    )


def _print_ffv_command_deck() -> None:
    w = max(58, min(88, _terminal_columns() - 2))
    bar = "═" * w
    print(f"{C_FRAME}╔{bar}╗{C_RESET}")
    print(
        f"{C_FRAME}║{C_RESET} {C_ACCENT}{Style.BRIGHT}FFV COMMAND DECK{C_RESET}  "
        f"{C_OK}[short]{C_RESET} 9:16 single  "
        f"{C_OK}[video]{C_RESET} 16:9 ×10–30 (xfade+motion)  "
        f"{C_OK}[seed]{C_RESET}  "
        f"{C_OK}[status]{C_RESET}  "
        f"{C_OK}[plan]{C_RESET}  "
        f"{C_OK}[catalog]{C_RESET}  "
        f"{C_OK}[deep]{C_RESET}  "
        f"{C_OK}[funny]{C_RESET} LOL preset  "
        f"{C_OK}[theory]{C_RESET} full theory corpus  "
        f"{C_EXIT}[ex]{C_RESET} menu"
    )
    print(
        f"{C_FRAME}║{C_RESET} {C_LABEL}theory{C_RESET} "
        f"{C_VALUE}{THEORY_FILE.name}{C_RESET}  "
        f"{C_LABEL}timings{C_RESET} "
        f"{C_VALUE}{REACTABLE_HOLD}s react + {REACTION_HOLD}s reaction "
        f"(fade in {REACTION_FADE_IN}s / out {REACTION_FADE_OUT}s){C_RESET}"
    )
    print(
        f"{C_FRAME}║{C_RESET} {C_LABEL}env{C_RESET} "
        f"{C_VALUE}FFV_FFMPEG_PRESET{C_RESET}=ffv_default|quality|premium · "
        f"{C_VALUE}FFV_DRY_RUN{C_RESET}=1 · "
        f"{C_VALUE}FFV_VIDEO_CHYRON{C_RESET} · "
        f"{C_VALUE}FFV_SHORT_LOOK{C_RESET} · "
        f"{C_VALUE}catalog{C_RESET} for full list"
    )
    print(f"{C_FRAME}╚{bar}╝{C_RESET}\n")


def _apply_seed(state: FFVProjectState, raw: str) -> None:
    parts = raw.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        print(f"{C_WARN}Usage: seed <integer>{C_RESET}")
        return
    try:
        s = int(parts[1].strip())
        state.seed = s
        random.seed(s)
        print(f"{C_OK}✔ RNG seed = {s}{C_RESET}")
    except ValueError:
        print(f"{C_BAD}✖ seed must be an integer{C_RESET}")


def _cmd_plan(state: FFVProjectState, corpus: TheoryCorpus) -> None:
    try:
        subs = ffv_order_subs_for_image_fetch(list(corpus.reactable_subs), reaction=False)
        pick = subs if len(subs) <= 120 else subs[:70] + random.sample(subs[70:], 50)
        pool_r = collect_pool(corpus, pick, per_sub=100)
        sub_i = ffv_order_subs_for_image_fetch(list(corpus.reaction_subs), reaction=True)
        pool_i = collect_pool(corpus, sub_i, per_sub=36)
        if len(pool_r) < 2 or len(pool_i) < 2:
            print(f"{C_WARN}⚠ Not enough posts to plan (Reddit blocked?){C_RESET}")
            return
        bp = plan_segment(corpus, pool_r, pool_i, 0, state)
        print(f"{C_ACCENT}Sample blueprint (not rendered):{C_RESET}")
        print(json.dumps(asdict(bp), indent=2, default=str))
    except Exception as e:
        print(f"{C_BAD}✖ plan failed: {e}{C_RESET}")


def _cmd_status(state: FFVProjectState, corpus: TheoryCorpus) -> None:
    print(f"{C_LABEL}session{C_RESET} {state.session_id}")
    print(f"{C_LABEL}preset{C_RESET} {state.corpus_preset}")
    print(f"{C_LABEL}seed{C_RESET} {repr(state.seed)}")
    print(f"{C_LABEL}renders ok/fail{C_RESET} {state.renders_ok}/{state.renders_fail}")
    print(f"{C_LABEL}last output{C_RESET} {state.last_output}")
    print(
        f"{C_LABEL}corpus{C_RESET} {len(corpus.reactable_subs)} react / "
        f"{len(corpus.reaction_subs)} re / {len(corpus.sfx_urls)} sfx"
    )
    if state.last_reactable_sub:
        hint = subreddit_diversity_hint(state.last_reactable_sub)
        print(f"{C_LABEL}last reactable sub meta{C_RESET} {hint}")
    if state.dag_history:
        print(f"{C_LABEL}last DAG{C_RESET} {state.dag_history[-1]}")


def _cmd_catalog() -> None:
    print(f"{C_ACCENT}Static catalog digest:{C_RESET} {ffv_catalog.catalog_digest()}")
    print(f"{C_ACCENT}Render phases:{C_RESET} {len(ffv_catalog.FFV_RENDER_PHASES)}")
    print(f"{C_ACCENT}Registered error codes:{C_RESET} {len(FFV_ERROR_CODES)}")
    print(f"{C_ACCENT}FFmpeg profiles:{C_RESET} {', '.join(FFV_FFMPEG_TUNING_PROFILES.keys())}")
    print(f"{C_ACCENT}Environment keys:{C_RESET}")
    for name, kind, desc in FFV_ENV_CATALOG:
        print(f"  {C_VALUE}{name}{C_RESET} ({kind}) — {desc}")


def _cmd_deep() -> None:
    print(f"{C_ACCENT}{Style.BRIGHT}FFV phase deck (abridged){C_RESET}")
    deck = format_phase_deck(limit=12)
    print(deck)
    print(f"{C_FRAME}… {len(ffv_catalog.FFV_RENDER_PHASES) - 12} more phases; see ffv/catalog.py{C_RESET}")
    print()
    print(f"{C_ACCENT}{Style.BRIGHT}Phase dependency sample{C_RESET}")
    for a, b, lbl in ffv_catalog.FFV_PHASE_EDGES[:8]:
        print(f"  {a} → {b}  ({lbl})")


def _exit_ffv_interrupt() -> None:
    print()
    print(f"{C_WARN}Interrupted — use ex to return to tool menu.{C_RESET}")


def ffv_interactive_main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    FFV_ASSETS.mkdir(parents=True, exist_ok=True)
    base_corpus = TheoryCorpus.load()
    funny_default = os.environ.get("FFV_FUNNY", "").strip().lower() in ("1", "true", "yes", "on")
    active_corpus = base_corpus.as_funny() if funny_default else base_corpus
    preset = "lol" if funny_default else "theory"
    state = FFVProjectState(
        seed=None,
        session_id=uuid.uuid4().hex[:12],
        theory_corpus_digest=active_corpus.digest(),
        corpus_preset=preset,
    )
    _print_ffv_banner()
    _print_ffv_command_deck()
    while True:
        try:
            cmd = input(
                f"{C_WARN}┌─{C_RESET}{C_LABEL}FFV{C_RESET}"
                f"{C_WARN}─►{C_RESET} "
            ).strip().lower()
        except KeyboardInterrupt:
            _exit_ffv_interrupt()
            continue
        if cmd in ("ex", "exit", "quit", "q"):
            return
        if cmd in ("help", "?", "h"):
            _print_ffv_command_deck()
            continue
        if cmd == "status":
            _cmd_status(state, active_corpus)
            continue
        if cmd == "plan":
            _cmd_plan(state, active_corpus)
            continue
        if cmd == "catalog":
            _cmd_catalog()
            continue
        if cmd in ("deep", "help deep", "help_deep"):
            _cmd_deep()
            continue
        if cmd.startswith("seed"):
            _apply_seed(state, cmd)
            continue
        if cmd == "funny":
            base_corpus = TheoryCorpus.load()
            active_corpus = base_corpus.as_funny()
            state.corpus_preset = "lol"
            state.theory_corpus_digest = active_corpus.digest()
            print(
                f"{C_OK}✔ LOL preset{C_RESET} — funny pics / meme reactions / punchy SFX. "
                f"{C_FRAME}({len(active_corpus.reactable_subs)} react · "
                f"{len(active_corpus.reaction_subs)} re){C_RESET}"
            )
            continue
        if cmd in ("theory", "default", "reset"):
            base_corpus = TheoryCorpus.load()
            active_corpus = base_corpus
            state.corpus_preset = "theory"
            state.theory_corpus_digest = active_corpus.digest()
            print(
                f"{C_OK}✔ Full theory corpus{C_RESET} "
                f"{C_FRAME}({len(active_corpus.reactable_subs)} react · "
                f"{len(active_corpus.reaction_subs)} re){C_RESET}"
            )
            continue
        if cmd == "short":
            try:
                dest = render_short(state, active_corpus)
                state.renders_ok += 1
                state.last_output = dest
                print(f"{C_OK}✔ Wrote {dest}{C_RESET}")
            except Exception as e:
                state.renders_fail += 1
                print(f"{C_BAD}✖ {e}{C_RESET}")
            continue
        if cmd == "video" or cmd.startswith("video "):
            n = random.randint(VIDEO_SEGMENTS_MIN, VIDEO_SEGMENTS_MAX)
            if cmd.startswith("video "):
                tail = cmd.split(maxsplit=1)[1].strip()
                if tail.isdigit():
                    n = max(VIDEO_SEGMENTS_MIN, min(VIDEO_SEGMENTS_MAX, int(tail)))
            print(
                f"{C_ACCENT}ℹ Building 16:9 video ×{n} segments: inner xfade {_video_inner_xfade_sec():.2f}s, "
                f"outer {_video_outer_xfade_sec():.2f}s, Ken Burns / grade / linked audio.{C_RESET}"
            )
            try:
                dest = render_vertical_video(state, active_corpus, n)
                state.renders_ok += 1
                state.last_output = dest
                print(f"{C_OK}✔ Wrote {dest}{C_RESET}")
            except Exception as e:
                state.renders_fail += 1
                print(f"{C_BAD}✖ {e}{C_RESET}")
            continue
        if cmd:
            print(
                f"{C_WARN}Unknown. Try short, video, video 15, seed 42, funny, theory, "
                f"plan, status, catalog, deep, ex.{C_RESET}"
            )
