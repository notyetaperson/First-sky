"""Optional extras: presets, queue runner, preview mux, filters, dashboard, i18n, A/B hooks."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .constants import (
    DASHBOARD_DIR,
    DEFAULT_QUEUE_FILE,
    EXTRAS_LOG_DIR,
    OUTPUT,
    PRESETS_DIR,
    ROOT,
    UNIVERSAL_DIR,
)


def _env_truthy(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (TypeError, ValueError):
        return default


class DryRunComplete(Exception):
    """Raised when PTK_DRY_RUN finishes planning without writing a video."""


def extras_help_text() -> str:
    return """
FirstSky optional extras (environment + CLI)

CLI:
  --preset <name|path.json>   Load JSON of env vars (see folders/presets/)
  --save-preset <name>        Save current PTK_* / FFV_* env to presets/<name>.json
  --queue <file>              Run batch lines (see below), then exit
  --dashboard                 Regenerate output/dashboard/index.html (repo root)
  --extras-help               This text

Presets:
  Save key=value pairs in JSON. Keys are applied to os.environ before runs.
  Built-in folder: folders/presets/<name>.json
  Or pass a full path to any .json file.

Queue file (one command per line, # comments):
  preset myprofile          -> load presets/myprofile.json
  ptk                       -> run Reddit-story pipeline once (FFV is interactive-only in the tool menu)
  asm                       -> launch Ascii-Media-Player once (sibling repo or ASCII_MEDIA_PLAYER_ROOT)
  r3u                       -> run "3 unknowns" ordinary-object short once

Content & safety:
  PTK_BLOCK_KEYWORDS        Comma-separated blocked substrings (case-insensitive)
  PTK_BLOCK_FILE            Text file, one phrase per line to block
  PTK_MIN_NARRATION_CHARS   Minimum narration length (0 = off)
  PTK_MAX_NARRATION_CHARS   Maximum narration length (0 = off)

Human gate (PTK only):
  PTK_HUMAN_APPROVE=1       After pick, prompt to approve story before TTS

Dry run (PTK only):
  PTK_DRY_RUN=1             Log plan + estimates; no TTS/video

Translation (PTK narration text, before TTS):
  PTK_TARGET_LANG           e.g. es, fr, de  (requires: pip install deep-translator)

Preview & publish helpers:
  PTK_WATERMARK=0           Disable small bottom-left FirstSky mark on final mux (default: on, 70% opacity)
  PTK_WATERMARK_FONT        Optional path to a .ttf for the watermark (default: system sans / Impact on Windows)
  PTK_PREVIEW=1             Write <stem>_preview.mp4 (15s, low scale, watermark)
  PTK_PREVIEW_SECONDS       Default 15
  PTK_UPLOAD_DIR            Copy final MP4 here after mux (optional)
  PTK_THUMBNAIL=1           Write <stem>_thumb.jpg next to output
  PTK_DASHBOARD=1           Refresh HTML dashboard after each mux (or use --dashboard)

A/B hooks (PTK):
  PTK_HOOK_VARIANTS=1       Write hook_variants.txt in temp work (on-screen hook ideas)

Beat hints (optional):
  PTK_BEAT_SYNC=1           Log simple RMS peaks to beat_hints.txt (needs numpy)

FFV user SFX pack:
  FFV_USER_PACK=<folder>    Append local .mp3/.wav/.m4a/.ogg into SFX pool (see FFV catalog help)
""".strip()


def ensure_extras_dirs() -> None:
    PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)
    EXTRAS_LOG_DIR.mkdir(parents=True, exist_ok=True)


def load_preset(path_or_name: str | Path) -> None:
    """Merge JSON object into os.environ (string values only)."""
    ensure_extras_dirs()
    p = Path(path_or_name)
    if not p.suffix.lower() == ".json":
        p = PRESETS_DIR / f"{path_or_name}.json"
    if not p.is_file():
        raise FileNotFoundError(f"Preset not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Preset JSON must be an object of string keys to string values.")
    for k, v in data.items():
        if v is None:
            continue
        os.environ[str(k)] = str(v)


def save_preset(name: str) -> Path:
    """Save current PTK_* / FFV_* environment keys to presets/<name>.json."""
    ensure_extras_dirs()
    data = {k: v for k, v in os.environ.items() if k.startswith(("PTK_", "FFV_"))}
    out = PRESETS_DIR / f"{name.strip()}.json"
    out.write_text(json.dumps(dict(sorted(data.items())), indent=2), encoding="utf-8")
    return out


def _blocked_keywords() -> list[str]:
    raw = (os.environ.get("PTK_BLOCK_KEYWORDS") or "").replace(";", ",")
    parts = [x.strip().lower() for x in raw.split(",") if x.strip()]
    path = (os.environ.get("PTK_BLOCK_FILE") or "").strip()
    if path:
        fp = Path(path)
        if not fp.is_file():
            fp = UNIVERSAL_DIR / path
        if fp.is_file():
            for ln in fp.read_text(encoding="utf-8", errors="replace").splitlines():
                t = ln.strip()
                if t and not t.startswith("#"):
                    parts.append(t.lower())
    return list(dict.fromkeys(parts))


def extra_keyword_blocked(text: str) -> bool:
    if not _blocked_keywords():
        return False
    low = (text or "").lower()
    return any(k in low for k in _blocked_keywords())


def narration_length_blocked(text: str) -> bool:
    n = len((text or "").strip())
    lo = int(_env_float("PTK_MIN_NARRATION_CHARS", 0))
    hi = int(_env_float("PTK_MAX_NARRATION_CHARS", 0))
    if lo > 0 and n < lo:
        return True
    if hi > 0 and n > hi:
        return True
    return False


def human_gate_enabled() -> bool:
    return _env_truthy("PTK_HUMAN_APPROVE", False)


def human_story_approved(post: dict[str, Any], text: str) -> bool:
    if not human_gate_enabled():
        return True
    title = str(post.get("title") or "").strip()
    sub = str(post.get("subreddit") or "").strip()
    excerpt = (text or "").strip().replace("\n", " ")[:900]
    print("\n--- Human approve (PTK_HUMAN_APPROVE=1) ---")
    print(f"r/{sub} — {title[:200]}")
    print(f"Excerpt: {excerpt}{'…' if len(text or '') > 900 else ''}")
    try:
        ans = input("Approve this story? [y/n]: ").strip().lower()
    except EOFError:
        return False
    if ans in ("y", "yes", ""):
        return True
    return False


def is_dry_run() -> bool:
    return _env_truthy("PTK_DRY_RUN", False)


def log_dry_run(
    post: dict[str, Any],
    text: str,
    *,
    n_cand: int,
    n_rank: int,
) -> None:
    ensure_extras_dirs()
    title = str(post.get("title") or "")
    chars = len(text or "")
    est_sec = max(8.0, chars / 14.0)
    line = (
        f"{datetime.now(timezone.utc).isoformat()} | cand={n_cand} rank={n_rank} | "
        f"chars={chars} ~audio_s={est_sec:.1f} | {title[:120]!r}\n"
    )
    (EXTRAS_LOG_DIR / "dry_run.log").open("a", encoding="utf-8").write(line)
    print(f"[dry-run] candidates={n_cand} ranked={n_rank} narration_chars={chars} ~{est_sec:.0f}s TTS")


def maybe_translate_narration(text: str) -> str:
    lang = (os.environ.get("PTK_TARGET_LANG") or "").strip()
    if not lang:
        return text
    try:
        from deep_translator import GoogleTranslator  # type: ignore[import-untyped]
    except ImportError:
        print("[translate] deep-translator not installed (pip install deep-translator); keeping English.", file=sys.stderr)
        return text
    try:
        return GoogleTranslator(source="auto", target=lang).translate(text)
    except Exception as e:
        print(f"[translate] failed ({e}); keeping source text.", file=sys.stderr)
        return text


def write_hook_variants(work: Path, post: dict[str, Any], text: str) -> None:
    if not _env_truthy("PTK_HOOK_VARIANTS", False):
        return
    title = str(post.get("title") or "").strip()
    body0 = (str(post.get("selftext") or "").strip().split("\n\n")[0] or "")[:200]
    hooks = [
        f"POV: {title[:100]}",
        f"Story time — {title[:90]}",
        f"You need to hear this: {title[:80]}",
        f"Wait for it… {title[:70]}",
        body0[:120] if body0 else title[:120],
    ]
    (work / "hook_variants.txt").write_text("\n".join(hooks), encoding="utf-8")


def maybe_log_beat_sync_hints(audio_path: Path, work: Path) -> None:
    if not _env_truthy("PTK_BEAT_SYNC", False):
        return
    try:
        import numpy as np
    except ImportError:
        (work / "beat_hints.txt").write_text("# Install numpy for RMS peak hints.\n", encoding="utf-8")
        return
    try:
        import wave
        import struct

        # Minimal WAV reader; for mp3 skip with note
        if audio_path.suffix.lower() != ".wav":
            (work / "beat_hints.txt").write_text(
                "# Beat sync: provide narration as WAV or extend with ffmpeg decode.\n", encoding="utf-8"
            )
            return
        with wave.open(str(audio_path), "rb") as wf:
            nch = wf.getnchannels()
            sw = wf.getsampwidth()
            fr = wf.getframerate()
            nframes = wf.getnframes()
            raw = wf.readframes(min(nframes, fr * 120))
        fmt = {1: "B", 2: "h", 4: "i"}.get(sw)
        if not fmt:
            return
        samples = struct.unpack(f"{len(raw) // sw}{fmt}", raw)
        if nch > 1:
            samples = samples[::nch]
        x = np.abs(np.array(samples, dtype=np.float64))
        win = max(1, fr // 20)
        peaks: list[float] = []
        for i in range(0, len(x) - win, win):
            peaks.append(float(x[i : i + win].mean()))
        thr = float(np.median(peaks)) * 1.8 if peaks else 0.0
        hits = [i * (win / fr) for i, p in enumerate(peaks) if p >= thr][:32]
        (work / "beat_hints.txt").write_text(
            "# Seconds (approx) of high RMS windows — for manual edit timing.\n" + "\n".join(f"{t:.2f}" for t in hits),
            encoding="utf-8",
        )
    except OSError:
        pass


def _run_ff(args: list[str], *, timeout: int = 600) -> None:
    subprocess.run(args, check=True, timeout=timeout, capture_output=True)


def after_mux_extras(out_mp4: Path, meta: dict[str, Any] | None = None) -> None:
    """Post-mux: preview clip, thumbnail, upload copy, dashboard."""
    if not out_mp4.is_file():
        return
    meta = meta or {}
    title = str(meta.get("title") or out_mp4.stem)

    if _env_truthy("PTK_PREVIEW", False):
        sec = max(3.0, _env_float("PTK_PREVIEW_SECONDS", 15.0))
        prev = out_mp4.with_name(out_mp4.stem + "_preview.mp4")
        try:
            _run_ff(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(out_mp4),
                    "-t",
                    str(sec),
                    "-vf",
                    "scale=480:-2,drawtext=text=PREVIEW:fontcolor=white@0.55:fontsize=28:x=12:y=12",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "96k",
                    str(prev),
                ],
                timeout=300,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass

    if _env_truthy("PTK_THUMBNAIL", False):
        thumb = out_mp4.with_name(out_mp4.stem + "_thumb.jpg")
        try:
            _run_ff(
                [
                    "ffmpeg",
                    "-y",
                    "-ss",
                    "1",
                    "-i",
                    str(out_mp4),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(thumb),
                ],
                timeout=60,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
            pass
        if thumb.is_file() and title:
            try:
                from PIL import Image, ImageDraw, ImageFont

                im = Image.open(thumb).convert("RGBA")
                overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
                draw = ImageDraw.Draw(overlay)
                t = title[:80]
                w, h = im.size
                draw.rectangle((0, h - 48, w, h), fill=(0, 0, 0, 160))
                draw.text((8, h - 40), t, fill=(255, 255, 255, 255))
                Image.alpha_composite(im, overlay).convert("RGB").save(thumb, quality=90)
            except Exception:
                pass

    upload = (os.environ.get("PTK_UPLOAD_DIR") or "").strip()
    if upload:
        dest_dir = Path(upload)
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(out_mp4, dest_dir / out_mp4.name)
        except OSError:
            pass

    if _env_truthy("PTK_DASHBOARD", False):
        refresh_dashboard()


def refresh_dashboard() -> Path:
    """Write a simple HTML index of OUTPUT/*.mp4."""
    ensure_extras_dirs()
    rows: list[str] = []
    for p in sorted(OUTPUT.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True)[:400]:
        st = p.stat()
        mtime = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sz = st.st_size // (1024 * 1024)
        rel = html.escape(p.name)
        rows.append(f"<tr><td>{mtime}</td><td>{sz} MB</td><td><code>{rel}</code></td></tr>")
    body = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>FirstSky output</title>
<style>
body {{ font-family: system-ui, sans-serif; background:#121a24; color:#d2dbe7; margin:24px; }}
table {{ border-collapse: collapse; width:100%; max-width:900px; }}
th, td {{ border-bottom:1px solid #2a3545; padding:8px; text-align:left; }}
code {{ color:#9bb1cc; }}
h1 {{ font-weight:600; color:#a7bbd3; }}
</style></head><body>
<h1>FirstSky dashboard</h1>
<p>Folder: <code>{html.escape(str(OUTPUT.resolve()))}</code></p>
<table><thead><tr><th>Modified</th><th>Size</th><th>File</th></tr></thead>
<tbody>{"".join(rows) or "<tr><td colspan='3'>No MP4 files yet.</td></tr>"}</tbody></table>
</body></html>"""
    out = DASHBOARD_DIR / "index.html"
    out.write_text(body, encoding="utf-8")
    return out


_PIPELINE_ALIASES: dict[str, str] = {
    "ptk": "run_pipeline_tts",
    "orl": "run_pipeline_orl",
    "r3u": "run_pipeline_r3u",
    "asm": "run_asm_player",
}


def run_queue(path: Path | None = None) -> None:
    """Execute queue file lines (preset / pipeline names)."""
    from .impl import _ensure_dirs
    from . import impl as impl_mod

    qp = path or DEFAULT_QUEUE_FILE
    if not qp.is_file():
        raise FileNotFoundError(f"Queue file not found: {qp}")
    _ensure_dirs()
    for raw in qp.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        cmd = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd == "preset":
            if not arg:
                raise ValueError("preset requires a name")
            load_preset(arg)
            continue
        fn_name = _PIPELINE_ALIASES.get(cmd)
        if not fn_name:
            raise ValueError(f"Unknown queue command {cmd!r} (see --extras-help)")
        fn = getattr(impl_mod, fn_name, None)
        if fn is None or not callable(fn):
            raise ValueError(f"Pipeline not available: {fn_name}")
        fn()
