# FirstSky

**FirstSky** is an automated Reddit-to-short-video toolkit: it fetches posts, ranks them, generates narration (local or optional browser TTS), mixes music and b-roll, burns stylized subtitles, and muxes publish-ready MP4s. The repo root `main.py` launcher adds `folders/` to `sys.path` so `resyco` and `ffv` import from `folders/resyco` and `folders/ffv`.

**Contents:** [Quick start](#quick-start) · [Layout](#repository-layout) · [Tools](#interactive-tool-selector) · [PTK](#ptk-pipeline-tool-ptk) · [FFV](#ffv-tool-ffv) · [ASM](#asm-tool-asm) · [Runtime](#runtime-layout-folders) · [Install](#installation-details) · [Env vars](#environment-variables-high-level) · [Troubleshooting](#troubleshooting)

---

## Quick start

```bash
cd path/to/FirstSky
pip install -r requirements.txt
python main.py
```

Same app via editable install (recommended for `firstsky` on `PATH`) or by setting `PYTHONPATH=folders` if you invoke the module directly:

```bash
pip install -e .   # optional: adds `firstsky` on PATH (see pyproject.toml)
firstsky --help
# or: PYTHONPATH=folders python -m resyco
```

You need **Python 3.10+**, **ffmpeg** and **ffprobe** on `PATH`, and a writable tree under `folders/` (created on first run).

```bash
python main.py --help      # CLI help
python main.py --version   # version string
python main.py --once      # single PTK-style render, then exit (no tool menu)
python main.py --asm       # run Ascii-Media-Player (sibling ../Ascii-Media-Player)
```

---

## Repository layout

| Path | Role |
|------|------|
| `main.py` | Entry point → prepends `folders/` to `sys.path` → `resyco.cli.main` |
| `folders/resyco/` | Application code: `constants`, `ui`, `impl` (pipelines), `cli`, `teen_formats` |
| `folders/ffv/` | **FFV** — reactions + SFX engine (`theory.txt` corpus, presets) |
| `../Ascii-Media-Player/` | Optional sibling checkout for **ASM** (terminal media player); or set `ASCII_MEDIA_PLAYER_ROOT` |
| `folders/tools/` | Maintenance scripts (e.g. `split_main.py`) |
| `folders/vendor/` | Optional vendored assets (if present) |
| `requirements.txt` / `pyproject.toml` | Core dependencies; PEP 621 metadata + `firstsky` console script |
| `folders/` | Runtime data: `assets/`, `output/`, `music/`, `presets/`, … (legacy `F0LD3R$` / root `assets` merged on startup when safe) |

---

## Interactive tool selector

On `python main.py`, you get a **tool menu** (numbered **1–40**). Pick by number or **code** (e.g. `ptk`, `ffv`, `rsg`, `twao`).

- **`h`** — redraw menu / help  
- **`q`** — quit  
- **`/text`** — filter tools by code, description, or category (e.g. `/spotlight`, `/story`, `/teen`)  
- **`/clear`** — reset filter  

Fuzzy matching suggests close codes if you typo while filtered.

### Tools at a glance

| # | Code | Category | Summary |
|---|------|----------|---------|
| 1 | PTK | Core | Full Reddit story → vertical video (`videoN.mp4`) |
| 2 | FFV   | Core | Reactions + SFX (see **FFV** below) |
| 3 | ORL | Core | English Wikipedia science → slideshow (`orlN.mp4`) |
| 4 | R3U | Core | "3 unknowns" facts about an ordinary object (`r3uN.mp4`) |
| 5 | ASM | Utility | Ascii-Media-Player — external terminal app (sibling repo; not a FirstSky render) |
| 3–26      | RST … RSL | Story | Themed shorts (`rstN.mp4`, `rssN.mp4`, …) |
| 27 | RSG  | Spotlight | Good-news / uplifting (`rsgN.mp4`) |
| 28 | RSD  | Spotlight | Plot-twist / drama (`rsdN.mp4`) |
| 29 | RSI  | Spotlight | Interesting finds (`rsiN.mp4`) |
| 30 | RSJ  | Spotlight | Science / wonder (`rsjN.mp4`) |
| 31 | RSK2 | Spotlight | History / mystery (`rsk2N.mp4`) |
| 32 | TWAO | Teen | Wrong answers only (`twaoN.mp4`) — scripts in `resyco/teen_formats.py` |
| 33 | TPOV | Teen | POV “brain during…” moment (`tpovN.mp4`) |
| 34 | TRATE| Teen | Yelp-style rating (`trateN.mp4`) |
| 35 | TOBJ | Teen | One-object story (`tobjN.mp4`) |
| 36 | TSET | Teen | Honest fake-app settings (`tsetN.mp4`) |
| 37 | TMUS | Teen | Gen-Z “museum 2045” plaque (`tmusN.mp4`) |
| 38 | TSIL | Teen | Silent / caption-card skit script (`tsilN.mp4`) |
| 39 | TSPD | Teen | School-day speedrun commentary (`tspdN.mp4`) |
| 40 | TCRE | Teen | Small creator (50–500 subs) petty online beef (`tcreN.mp4`) — satire storytime from `teen_formats.py` |

**Teen tools** use **no Reddit fetch**: each run picks random lines from template pools, then the same TTS → music → b-roll → subs → mux path as other shorts. Batch a folder of renders with `python main.py --queue my.txt` and lines like `twao`, `tspd`, `tcre`, etc. (see `python main.py --extras-help`).

Inside each video tool, commands match the **command deck** banner:

- **`start`** / **`start(n)`** / **`start(*)`** — render one, *n*, or continuous  
- **`re`** / **`re(n)`** / **`re(*)`** — same, after a completed run  
- **`ex`** — back to tool menu  

---

## PTK pipeline (tool **PTK**)

Default output: `output/videoN.mp4` (repo root).

The main story pipeline runs **8 stages** (this is what the progress bar tracks):

1. Fetch candidates from configured subreddits  
2. Rank with virality-style signals  
3. Pick one story (weighted randomness, ~0.5% decay per rank step)  
4. Generate narration audio  
5. Mix narration + background music  
6. Build ASMR-style b-roll (e.g. from `r/oddlysatisfying`)  
7. Build subtitles (word-level timing when possible)  
8. Mux final MP4  

**Story sources** and thresholds (e.g. minimum body length) are defined in `resyco/constants.py` — adjust `MIN_POST_CHARS` and subreddit lists there rather than assuming fixed values in docs.

**Deduplication:** `output/used_stories.db` tracks used stories; rows are pruned if the matching output file is gone.

---

## FFV (tool **FFV**)

**FFV** builds reaction-style content from image reactables, reactions, and SFX, driven by `theory.txt` (expected at `folders/theory.txt`; copy from repo root on first run if present) and the `ffv` package.

- **`funny`** in the FFV shell switches to the **LOL preset** (funny reactables, meme-flavored reactions, punchy SFX ordering).  
- **`theory`** / **`default`** / **`reset`** return to the theory-style corpus.  
- Environment: **`FFV_FUNNY=1`** (or `true` / `yes` / `on`) starts FFV already in the LOL preset.

See `ffv/catalog.py` for preset definitions and `FFV_ENV`-documented tunables.

---

## ASM (tool **ASM**)

**ASM** does not render an MP4: it spawns the separate **Ascii-Media-Player** project from a checkout next to this repo (`../Ascii-Media-Player`), or from `../ascii-player` if that is present. Set **`ASCII_MEDIA_PLAYER_ROOT`** (or **`ASM_ROOT`**) to an absolute path if the project lives elsewhere.

- **`ASM_ENTRY`**: optional full command to run (e.g. `python -m yourpackage`); otherwise the launcher looks for `main.py`, `run.py`, `cli.py`, or a one-level subpackage with `__main__.py`.
- **`ASM_ARGS`**: optional extra arguments (shell-quoted) appended to the command.
- CLI: `python main.py --asm` — any arguments after `--asm` are passed through to the player (e.g. a media path).
- **Windows:** Python’s `curses` module is not available, so `curses-player.py` is skipped automatically. The bundled **`ascii-player`** layout: on first **ASM** launch, FirstSky runs `copy_film.py` once (network) to create `star-wars.ascii`, then starts `new-player.py`. Set **`ASM_NO_AUTO_FETCH=1`** to skip that. For other checkouts, use **`ASM_ENTRY`** or WSL if you need curses.

---

## Runtime layout (`folders/`)

- `folders/assets/` — temp work, TTS web assets, caches  
- `output/` (repo root) — rendered `.mp4` (and dedupe DB)  
- `folders/music/` — local background music (recursive scan)  

On startup, legacy folders (`F0LD3R$/`, `universal/`, root `assets/`, `output/`, `Music/`) may be merged in when safe; old dedupe text files migrate into `used_stories.db`.

---

## Installation details

### System

- **ffmpeg** / **ffprobe** on `PATH`

### Python

From repo root:

```bash
pip install -r requirements.txt
```

Notable packages: `requests`, `colorama`, `fake-useragent`, `pyttsx3`, `gTTS`, `Pillow`, `numpy` (used by some optional helpers).

### Optional

- **`faster-whisper`** — set `PTK_WHISPER=1` for Whisper-aligned subtitles (uncomment in `requirements.txt` or install manually)  
- **`playwright`** — optional browser TTS (`PTK_BROWSER_TTS=1`); then `playwright install chromium`  
- **`edge-tts`** — when `PTK_TTS_ENGINE` is `edge` / `microsoft` / `azure`, or `PTK_EDGE_TTS=1`  

---

## Environment variables (high level)

**Core story pipeline (`PTK_*`)**

| Variable | Effect |
|----------|--------|
| `PTK_FAST` | Fast profile (default on in typical setups) |
| `PTK_AGGRESSIVE` | Smaller resolution / FPS, faster subtitle path, etc. |
| `PTK_HWENC` | Try GPU H.264 for subtitle-burn mux (NVENC / AMF / QSV), else CPU |
| `PTK_WHISPER` / `PTK_WHISPER_MODEL` | Whisper subtitles |
| `PTK_BROWSER_TTS` | Playwright-captured Web Speech TTS |
| `PTK_UA` | Override HTTP User-Agent |
| `PTK_TTS_ENGINE`, `PTK_TTS_VOICE`, `PTK_EDGE_TTS` | TTS engine / voice selection |

**FFV (`FFV_*`)**

| Variable | Effect |
|----------|--------|
| `FFV_FUNNY` | Start in LOL / funny preset |

Full lists and notes: `ffv/catalog.py` (`FFV_ENV`) and inline help from `python main.py --help`.

---

## Performance modes (summary)

- **Fast + aggressive:** e.g. `576x1024 @ 24fps`, tighter Reddit fetch, ultra-fast subtitle timing when enabled.  
- **Fast (non-aggressive):** e.g. `720x1280 @ 30fps`.  
- **Quality (`PTK_FAST` off):** e.g. `1080x1920 @ 30fps`.  

Helpers include ffprobe duration cache, music directory scan cache, and optional `PTK_HWENC` for the final encode step.

---

## Narration, music, b-roll, subtitles

- **TTS:** `pyttsx3` by default; optional browser path with `PTK_BROWSER_TTS=1`; Edge neural TTS when configured. Narration speed uses ffmpeg `atempo` (see `NARRATION_SPEED` in `resyco/constants.py`).  
- **Music:** Prefer random file under `folders/music/`, then legacy `Music/`, then synthetic bed.  
- **B-roll:** Reddit-hosted video from satisfying-style subs; loop or concat to match narration length.  
- **Subtitles:** Burned-in ASS; timing order: fast aggressive word mode → Whisper → silence detection → proportional fallback.  

---

## Error handling

Successful runs clean temp dirs under `assets/`. Failures and **Ctrl+C** trigger cleanup where possible; PTK-style flows print a short warning when a run did not finish.

---

## Troubleshooting

| Issue | What to try |
|-------|-------------|
| `ffmpeg` / `ffprobe` not found | Install FFmpeg and fix `PATH`. |
| No music | Add files under `folders/music/` or rely on generated bed. |
| Reddit 429 / fetch errors | Waits and retries are built in; set `PTK_UA` to a stable UA if needed. |
| TTS failures | Check OS voices for `pyttsx3`; disable browser TTS if misconfigured. |
| Slow renders | Keep fast/aggressive env defaults; SSD + CPU headroom help; consider `PTK_HWENC=1`. |
| Stale help text | Prefer **`python main.py`** as the documented entry. |
| Duplicate stories | Inspect `used_stories.db`; avoid renaming outputs without updating the DB. |

---

## Safety / publishing

Reddit text can be sensitive or copyrighted; add moderation and respect platform rules. License background music and b-roll for your distribution channel.
