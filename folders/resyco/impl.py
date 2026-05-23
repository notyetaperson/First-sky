"""Pipelines, Reddit fetch, ffmpeg, TTS, and interactive shells."""
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
from .features import DryRunComplete
from . import features as extras
from . import teen_formats
from .ui import *

_MUX_EXTRA_META = threading.local()


def _set_mux_extra_meta(**kwargs: Any) -> None:
    _MUX_EXTRA_META.data = kwargs  # type: ignore[attr-defined]


def _clear_mux_extra_meta() -> None:
    if hasattr(_MUX_EXTRA_META, "data"):
        delattr(_MUX_EXTRA_META, "data")

def _pick_random_matching_line(
    blob: str,
    rows: tuple[tuple[tuple[str, ...], str], ...] | list[tuple[tuple[str, ...], str]],
    fallbacks: tuple[str, ...],
) -> str:
    """Return a random line from every row whose keywords appear in ``blob``; else random fallback."""
    b = (blob or "").lower()
    hits = [line for keys, line in rows if any(k in b for k in keys)]
    if hits:
        return random.choice(hits)
    return random.choice(fallbacks)


def _adopt_legacy_layout() -> None:
    """Merge legacy runtime dirs at repo root into ``folders/`` (``UNIVERSAL_DIR``) when safe."""
    for legacy_dir in (LEGACY_UNIVERSAL_DIR, LEGACY_UNIVERSAL_ALT):
        if not legacy_dir.exists():
            continue
        if not UNIVERSAL_DIR.exists():
            try:
                shutil.move(str(legacy_dir), str(UNIVERSAL_DIR))
            except OSError:
                pass
            break
        for child in legacy_dir.iterdir():
            dst = UNIVERSAL_DIR / child.name
            if dst.exists():
                continue
            try:
                shutil.move(str(child), str(dst))
            except OSError:
                pass
        try:
            legacy_dir.rmdir()
        except OSError:
            pass

    lt = ROOT / "theory.txt"
    th = UNIVERSAL_DIR / "theory.txt"
    if lt.is_file() and not th.is_file():
        try:
            shutil.copy2(lt, th)
        except OSError:
            pass

    # Previous default was ``folders/output``; merge into repo-root ``output/``.
    old_out = LEGACY_OUTPUT_IN_FOLDERS
    if old_out.exists() and old_out.resolve() != OUTPUT.resolve():
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        if not OUTPUT.exists():
            try:
                shutil.move(str(old_out), str(OUTPUT))
            except OSError:
                pass
        else:
            for child in old_out.iterdir():
                dst = OUTPUT / child.name
                if dst.exists():
                    continue
                try:
                    shutil.move(str(child), str(dst))
                except OSError:
                    pass
            try:
                old_out.rmdir()
            except OSError:
                pass

    migrations = (
        (LEGACY_ASSETS, ASSETS),
        (LEGACY_MUSIC, MUSIC),
    )
    for src, dst in migrations:
        if not src.exists() or dst.exists():
            continue
        try:
            shutil.move(str(src), str(dst))
        except OSError:
            # Non-fatal: keep compatibility by continuing to read legacy where needed.
            pass


def _ensure_dirs() -> None:
    _adopt_legacy_layout()
    UNIVERSAL_DIR.mkdir(parents=True, exist_ok=True)
    ASSETS.mkdir(parents=True, exist_ok=True)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(parents=True, exist_ok=True)
    MUSIC.mkdir(parents=True, exist_ok=True)
    _migrate_legacy_used_story_files_to_db()


def _story_db_conn() -> sqlite3.Connection:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(USED_STORY_DB_FILE)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS used_stories (
            story_key TEXT PRIMARY KEY,
            video_name TEXT NOT NULL
        )
        """
    )
    return conn


def _migrate_legacy_used_story_files_to_db() -> None:
    """One-time migration from legacy text/tsv files into SQLite."""
    if not LEGACY_USED_STORY_IDS_FILE.exists() and not LEGACY_USED_STORY_MAP_FILE.exists():
        return

    story_map: dict[str, str] = {}
    if LEGACY_USED_STORY_MAP_FILE.is_file():
        try:
            for ln in LEGACY_USED_STORY_MAP_FILE.read_text(encoding="utf-8").splitlines():
                row = ln.strip()
                if not row or "\t" not in row:
                    continue
                k, v = row.split("\t", 1)
                k = k.strip()
                v = v.strip()
                if k and v:
                    story_map[k] = v
        except OSError:
            pass

    if LEGACY_USED_STORY_IDS_FILE.is_file():
        try:
            for ln in LEGACY_USED_STORY_IDS_FILE.read_text(encoding="utf-8").splitlines():
                k = ln.strip()
                if k:
                    story_map.setdefault(k, "")
        except OSError:
            pass

    if story_map:
        with _story_db_conn() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO used_stories (story_key, video_name) VALUES (?, ?)",
                [(k, v) for k, v in story_map.items()],
            )
            conn.commit()

    LEGACY_USED_STORY_IDS_FILE.unlink(missing_ok=True)
    LEGACY_USED_STORY_MAP_FILE.unlink(missing_ok=True)


def _story_key(post: dict[str, Any]) -> str:
    sub = str(post.get("subreddit") or "").strip().lower()
    pid = str(post.get("id") or "").strip().lower()
    if sub and pid:
        return f"{sub}:{pid}"
    pm = str(post.get("permalink") or "").strip().lower()
    return pm


def _prune_used_story_keys_by_existing_videos() -> set[str]:
    """
    Keep only used-story keys whose mapped video file still exists.
    If a video is deleted, its story key is automatically released.
    Skips a full DB rewrite when nothing was orphaned (faster hot path).
    """
    _migrate_legacy_used_story_files_to_db()
    with _story_db_conn() as conn:
        rows = conn.execute("SELECT story_key, video_name FROM used_stories").fetchall()
        if not rows:
            return set()
        kept: list[tuple[str, str]] = []
        dropped = False
        for key, video_name in rows:
            video_name = str(video_name or "").strip()
            if not video_name or (OUTPUT / video_name).is_file():
                kept.append((str(key), video_name))
            else:
                dropped = True
        if not dropped:
            return {k for k, _ in kept}
        conn.execute("DELETE FROM used_stories")
        if kept:
            conn.executemany(
                "INSERT OR REPLACE INTO used_stories (story_key, video_name) VALUES (?, ?)",
                kept,
            )
        conn.commit()
    return {k for k, _ in kept}


def _record_used_story(post: dict[str, Any], out_video: Path) -> None:
    global _USED_STORY_KEYS_CACHE
    key = _story_key(post)
    if not key:
        return
    with _story_db_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO used_stories (story_key, video_name) VALUES (?, ?)",
            (key, out_video.name),
        )
        conn.commit()
    _USED_STORY_KEYS_CACHE = None


def _used_story_keys_for_filter() -> set[str]:
    """Cached view of used keys; short TTL so deleted videos free stories quickly."""
    global _USED_STORY_KEYS_CACHE
    now = time.time()
    if _USED_STORY_KEYS_CACHE is not None:
        t0, keys = _USED_STORY_KEYS_CACHE
        if now - t0 < _USED_STORY_KEYS_CACHE_TTL_SEC:
            return set(keys)
    keys = frozenset(_prune_used_story_keys_by_existing_videos())
    _USED_STORY_KEYS_CACHE = (now, keys)
    return set(keys)


def _filter_unused_story_candidates(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used = _used_story_keys_for_filter()
    if not used:
        return posts
    return [p for p in posts if _story_key(p) not in used]


def _reddit_compliant_user_agent() -> str:
    """Reddit asks for a unique, descriptive UA (not a bare browser string on .json)."""
    return f"python:firstsky:v{__version__} (story/b-roll fetcher)"


def _user_agent() -> str:
    if os.environ.get("PTK_UA"):
        return os.environ["PTK_UA"]
    if os.environ.get("PTK_UA_COMPLIANT", "").strip().lower() in ("1", "true", "yes", "on") or os.environ.get(
        "PTK_REDDIT_UA", ""
    ).strip().lower() in ("1", "true", "yes", "on"):
        return _reddit_compliant_user_agent()
    global _UA_PROVIDER
    try:
        if _UA_PROVIDER is None:
            from fake_useragent import UserAgent

            _UA_PROVIDER = UserAgent(fallback=random.choice(_BROWSER_USER_AGENTS))
        return _UA_PROVIDER.random
    except Exception:
        pass
    return random.choice(_BROWSER_USER_AGENTS)


def _reddit_get(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    max_attempts: int = 4,
    per_request_timeout: tuple[float, float] = (10, 30),
    use_alt_hosts: bool = True,
) -> Any:
    time.sleep(REQUEST_PAUSE_SEC)
    params = params or {}

    def _empty_listing() -> dict[str, Any]:
        return {"data": {"children": [], "after": None}}

    def _candidate_urls(src: str) -> list[str]:
        try:
            p = urllib.parse.urlsplit(src)
        except ValueError:
            return [src]
        if not p.netloc:
            return [src]
        hosts = ("www.reddit.com", "old.reddit.com", "api.reddit.com")
        out: list[str] = []
        for h in hosts:
            if p.netloc == h:
                u = src
            else:
                u = urllib.parse.urlunsplit((p.scheme or "https", h, p.path, p.query, p.fragment))
            if u not in out:
                out.append(u)
        return out or [src]

    last_err: Exception | None = None
    saw_block_or_rate = False
    urls = _candidate_urls(url) if use_alt_hosts else [url]
    uas = [_reddit_compliant_user_agent(), _user_agent()]

    max_attempts = max(1, int(max_attempts))
    for attempt in range(max_attempts):
        for u in urls:
            for ua in uas:
                headers = {
                    "User-Agent": ua,
                    "Accept": "application/json, text/javascript, */*;q=0.01",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": "https://www.reddit.com/",
                    "Origin": "https://www.reddit.com",
                    "Connection": "keep-alive",
                }
                try:
                    r = _REDDIT_SESSION.get(u, params=params, headers=headers, timeout=per_request_timeout)
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
        if attempt < max_attempts - 1:
            time.sleep(1.2 * (attempt + 1))

    # Do not crash pipeline for subreddit-level blocking/rate-limit responses.
    if saw_block_or_rate:
        return _empty_listing()
    if last_err is not None:
        raise last_err
    return _empty_listing()


def fetch_story_candidates() -> list[dict[str, Any]]:
    """Collect self-posts > MIN_POST_CHARS from all story subreddits (paginated)."""
    posts: list[dict[str, Any]] = []
    subs = list(STORY_SUBREDDITS)
    random.shuffle(subs)
    if len(subs) > STORY_MAX_SUBS_PER_RUN:
        subs = subs[:STORY_MAX_SUBS_PER_RUN]
    t0 = time.time()

    for sub in subs:
        if len(posts) >= MIN_STORY_CANDIDATES_STOP:
            return posts
        if time.time() - t0 > STORY_FETCH_BUDGET_SEC:
            break
        _info(f"  r/{sub} … ({len(posts)} long posts so far)")
        after: str | None = None
        for _ in range(REDDIT_PAGES_PER_SUB):
            if len(posts) >= MIN_STORY_CANDIDATES_STOP:
                return posts
            if time.time() - t0 > STORY_FETCH_BUDGET_SEC:
                break
            url = f"https://www.reddit.com/r/{sub}/hot.json"
            try:
                data = _reddit_get(
                    url, {"limit": 100, "raw_json": 1, **({"after": after} if after else {})}
                )
            except requests.RequestException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (403, 404, 429, 451):
                    break
                raise
            if not isinstance(data, dict):
                break
            root = data.get("data", {}).get("children") or []
            if not root:
                break
            for child in root:
                c = child.get("data") or {}
                if c.get("is_self") and len(c.get("selftext") or "") >= MIN_POST_CHARS:
                    if _story_looks_advertisement(c):
                        continue
                    rec: dict[str, Any] = {
                        "subreddit": sub,
                        "id": c.get("id"),
                        "title": c.get("title") or "",
                        "selftext": c.get("selftext") or "",
                        "permalink": c.get("permalink") or "",
                        "score": int(c.get("score") or 0),
                        "num_comments": int(c.get("num_comments") or 0),
                        "created_utc": float(c.get("created_utc") or 0.0),
                    }
                    if _story_contains_nsfw_blocked_words(rec):
                        continue
                    posts.append(rec)
            if len(posts) >= MIN_STORY_CANDIDATES_STOP:
                return posts
            after = data.get("data", {}).get("after")
            if not after:
                break
    return posts


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def _sentiment_score(text: str) -> float:
    """Simple lexicon sentiment strength in [0..1] (neutral around 0.5)."""
    t = (text or "").lower()
    if not t:
        return 0.5
    pos = sum(t.count(w) for w in _SENTIMENT_POS_WORDS)
    neg = sum(t.count(w) for w in _SENTIMENT_NEG_WORDS)
    total = pos + neg
    if total <= 0:
        return 0.5
    # Intensity only, not polarity, because both highly positive and highly negative stories can be viral.
    intensity = abs(pos - neg) / total
    return _clamp01(0.45 + 0.55 * intensity)


def _conflict_intensity_score(text: str) -> float:
    t = (text or "").lower()
    if not t:
        return 0.0
    hits = sum(t.count(w) for w in _CONFLICT_TERMS)
    punct = t.count("!") + t.count("?")
    val = (hits * 0.12) + (punct * 0.01)
    return _clamp01(val)


def _payoff_quality_score(text: str) -> float:
    """
    Approximate payoff by checking whether the final section contains closure keywords.
    """
    raw = (text or "").strip()
    if not raw:
        return 0.0
    end = raw[-1200:].lower()
    hits = sum(end.count(w) for w in _PAYOFF_TERMS)
    return _clamp01(hits / 4.0)


def _rank_stories_for_virality(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Rank stories using virality signals:
    - comment count
    - upvote velocity
    - sentiment intensity
    - conflict intensity
    - payoff quality
    """
    if not posts:
        return []
    now = time.time()
    scores = [max(0.0, float(p.get("score", 0))) for p in posts]
    comments = [max(0.0, float(p.get("num_comments", 0))) for p in posts]
    velocities: list[float] = []
    for p, s in zip(posts, scores):
        created = float(p.get("created_utc") or 0.0)
        age_hours = max(1.0 / 6.0, (now - created) / 3600.0) if created > 0 else 24.0
        velocities.append(s / age_hours)
    max_score = max(scores) if scores else 1.0
    max_comments = max(comments) if comments else 1.0
    max_velocity = max(velocities) if velocities else 1.0

    ranked: list[dict[str, Any]] = []
    for i, p in enumerate(posts):
        text = f"{p.get('title', '')}\n{p.get('selftext', '')}"
        score_norm = (scores[i] / max_score) if max_score > 0 else 0.0
        comments_norm = (comments[i] / max_comments) if max_comments > 0 else 0.0
        velocity_norm = (velocities[i] / max_velocity) if max_velocity > 0 else 0.0
        sentiment = _sentiment_score(text)
        conflict = _conflict_intensity_score(text)
        payoff = _payoff_quality_score(text)
        virality = (
            0.30 * comments_norm
            + 0.28 * velocity_norm
            + 0.16 * sentiment
            + 0.16 * conflict
            + 0.10 * payoff
        )
        q = dict(p)
        q["virality_score"] = round(float(virality), 6)
        ranked.append(q)

    ranked.sort(key=lambda p: float(p.get("virality_score", 0.0)), reverse=True)
    return ranked


def pick_story_from_ranked(ranked: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Weighted random choice from an already-ranked list.
    Rank 0 is strongest. Each lower rank loses 0.5% selection chance.
    """
    ranked = [p for p in ranked if not _story_contains_nsfw_blocked_words(p)]
    if not ranked:
        raise RuntimeError("No ranked story candidates.")
    weights: list[float] = []
    for idx, p in enumerate(ranked):
        rank_multiplier = max(0.005, 1.0 - (0.005 * idx))
        popularity = 0.5 + 0.5 * float(p.get("virality_score", 0.0))
        weights.append(max(0.0001, rank_multiplier * popularity))
    return random.choices(ranked, weights=weights, k=1)[0]


def pick_story_weighted(posts: list[dict[str, Any]]) -> dict[str, Any]:
    """Rank posts by virality, then weighted-random pick."""
    ranked = _rank_stories_for_virality(posts)
    return pick_story_from_ranked(ranked)


def build_narration_text(post: dict[str, Any]) -> str:
    """Narration text from post (hook + title + body, no comments)."""
    title = str(post.get("title") or "").strip()
    body = str(post.get("selftext") or "").strip()
    if not HOOKS_ENABLED:
        return f"{title}\n\n{body}".strip()
    hook = _generate_story_hook(post)
    if hook:
        return f"{hook}\n\n{title}\n\n{body}".strip()
    return f"{title}\n\n{body}".strip()


def fetch_today_history_events(*, max_items: int = 5) -> list[dict[str, str]]:
    """Fetch notable events for today's month/day from Wikipedia REST API."""
    now = datetime.now()
    url = f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{now.month}/{now.day}"
    headers = {"User-Agent": f"python:firstsky:v{__version__} (RST history fetcher)"}
    r = requests.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    data = r.json()
    events_raw = data.get("events") or []
    cleaned: list[dict[str, str]] = []
    for ev in events_raw:
        year = str(ev.get("year") or "").strip()
        text = re.sub(r"\s+", " ", str(ev.get("text") or "").strip())
        if not year or not text:
            continue
        if len(text) > 220:
            text = text[:219].rstrip(" ,.;:-") + "…"
        cleaned.append({"year": year, "text": text})
    if not cleaned:
        return []
    random.shuffle(cleaned)
    return cleaned[: max(1, int(max_items))]


def build_rst_narration_text(events: list[dict[str, str]], *, now: datetime | None = None) -> str:
    """Narration script for RST mode from month/day historical events."""
    dt = now or datetime.now()
    stamp = dt.strftime("%B %-d") if os.name != "nt" else dt.strftime("%B %#d")
    lines = [
        random.choice(_VAR_RST_INTROS).format(stamp=stamp),
        random.choice(
            (
                "Here are a few events that happened on this day.",
                "Here are snapshots from the past tied to this date.",
                "A handful of moments that share this calendar day.",
                "Three quick beats from history on this day.",
            )
        ),
    ]
    for ev in events:
        lines.append(f"In {ev['year']}: {ev['text']}")
    lines.append(random.choice(_VAR_RST_OUTROS))
    return "\n\n".join(lines)


def _rss_trim_line(s: str, max_len: int) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    if len(t) <= max_len:
        return t
    return t[: max_len - 1].rstrip(" ,.;:-") + "…"


def _rss_side_b_from_text(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    patterns = [
        (("money", "bill", "rent", "pay"), "the other side says finances should be shared more fairly"),
        (("wedding", "birthday", "holiday", "family"), "the other side says family expectations should come first"),
        (("roommate", "house", "apartment", "chores"), "the other side says shared-space rules were ignored"),
        (("boss", "coworker", "job", "work"), "the other side says your reaction was unprofessional"),
        (("boyfriend", "girlfriend", "husband", "wife", "partner"), "the other side says you did not communicate enough"),
        (("friend", "friends", "groupchat", "group chat"), "the other side says you escalated in public instead of talking privately"),
        (("ex", "exes", "breakup", "split"), "the other side says you are still carrying old relationship baggage into this"),
        (("car", "drive", "road", "parking"), "the other side says you made a mountain out of a minor inconvenience"),
        (("pet", "dog", "cat", "vet"), "the other side says the pet issue became a proxy war for deeper resentment"),
        (("phone", "text", "message", "dm"), "the other side says tone gets lost over text and you assumed the worst"),
        (("gift", "present", "birthday gift"), "the other side says gratitude matters more than the price tag"),
        (("food", "cook", "dinner", "takeout"), "the other side says you turned a meal into a referendum on effort"),
        (("clean", "mess", "dishes", "laundry"), "the other side says standards differ and you did not negotiate them"),
        (("time", "late", "schedule", "plans"), "the other side says you punished them for life logistics, not intent"),
        (("baby", "kid", "child", "pregnant"), "the other side says stress about kids does not automatically make you right"),
        (("trip", "vacation", "flight", "hotel"), "the other side says travel stress explains behavior but does not excuse harm"),
        (("party", "wedding guest", "invite"), "the other side says social events come with obligations you ignored"),
        (("inheritance", "will", "estate", "money"), "the other side says family money makes everyone act territorial"),
        (("sick", "ill", "hospital", "doctor"), "the other side says caregiving burnout goes both ways"),
        (("neighbor", "noise", "hoa", "fence"), "the other side says you picked the nuclear option over a simple conversation"),
        (("school", "teacher", "grade", "homework"), "the other side says you fought the adult battle through your kid"),
        (("religion", "church", "faith", "atheist"), "the other side says beliefs deserve respect even when you disagree"),
        (("politics", "vote", "election"), "the other side says you made identity politics the whole personality of the fight"),
        (("weight", "diet", "gym", "body"), "the other side says comments about bodies are never neutral"),
        (("drink", "drunk", "alcohol", "bar"), "the other side says substances change accountability, not facts"),
        (("game", "gaming", "console", "stream"), "the other side says hobbies are not a moral failing"),
        (("hair", "cut", "tattoo", "piercing"), "the other side says bodily autonomy is not a debate prize"),
        (("phone password", "privacy", "snoop"), "the other side says trust cannot mean unlimited surveillance"),
        (("sleep", "insomnia", "snore"), "the other side says rest is a shared resource in a shared home"),
        (("wifi", "internet", "router"), "the other side says tech problems are frustrating, not character flaws"),
    ]
    return _pick_random_matching_line(blob, tuple(patterns), _RSS_SIDE_B_DEFAULTS)


def fetch_rss_dilemma_post() -> dict[str, Any]:
    """Pick one likely dilemma story, preferring r/AmItheAsshole."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSS.")
    ranked = _rank_stories_for_virality(candidates)
    aita = [p for p in ranked if str(p.get("subreddit") or "").strip().lower() == "amitheasshole"]
    pool = aita if aita else ranked
    if not pool:
        raise RuntimeError("No ranked dilemma candidates for RSS.")
    return pick_story_from_ranked(pool)


def build_rss_narration_text(post: dict[str, Any]) -> str:
    """Rule-based two-sides dilemma script from one Reddit self-post."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 210)
    side_a = _rss_trim_line(title or first or "I made a decision that upset everyone.", 190)
    side_b = _rss_side_b_from_text(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(_VAR_RSS_OPENERS),
        f"Scenario: {first or side_a}",
        f"Side A says: {side_a}",
        f"Side B says: {side_b}.",
        random.choice(
            (
                f"This came from r slash {src_sub}.",
                f"Pulled from r slash {src_sub}.",
                f"Story source: r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSS_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rsp_opinion_post() -> dict[str, Any]:
    """Pick one opinion-style post, preferring hot-take subreddits."""
    target_subs = {"unpopularopinion", "changemyview", "trueoffmychest", "offmychest"}
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSP.")
    ranked = _rank_stories_for_virality(candidates)
    focused = [p for p in ranked if str(p.get("subreddit") or "").strip().lower() in target_subs]
    pool = focused if focused else ranked
    if not pool:
        raise RuntimeError("No ranked opinion candidates for RSP.")
    return pick_story_from_ranked(pool)


def build_rsp_narration_text(post: dict[str, Any]) -> str:
    """Rule-based hot-take script with agree/disagree call-to-action."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    stance = title or first or "This might be unpopular, but here is my take."
    reason = _rss_trim_line(first if first and first != stance else body, 210)
    if not reason:
        reason = "The argument is that this choice is more practical than people admit."
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(_VAR_RSP_OPENERS),
        f"Opinion: {stance}",
        f"Reason: {reason}",
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"Found on r slash {src_sub}.",
                f"Context from r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSP_CTAS),
    ]
    return "\n\n".join(lines)


def _rsx_red_flags_from_text(title: str, body: str) -> list[str]:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("lied", "lying", "secret", "hid"), "Hidden details and broken trust"),
        (("yell", "scream", "shout", "insult"), "Escalating communication and disrespect"),
        (("money", "debt", "rent", "bill"), "Financial pressure driving bad decisions"),
        (("control", "allowed", "permission", "forbid"), "Control patterns instead of mutual boundaries"),
        (("ignore", "ghost", "silent treatment", "blocked"), "Conflict avoidance and stonewalling"),
        (("family", "parent", "in-laws", "friends"), "Outside pressure overwhelming the relationship"),
        (("jealous", "insecure", "snoop", "password"), "Possessiveness framed as care"),
        (("compare", "ex", "someone else", "better than"), "Comparison traps that erode security"),
        (("always", "never", "every time", "you always"), "Absolute language that shuts down repair"),
        (("punish", "revenge", "payback", "lesson"), "Punishment cycles instead of problem solving"),
        (("drunk", "drinking", "high", "substance"), "Substance-fueled moments with messy accountability"),
        (("threat", "ultimatum", "leave", "divorce"), "Threats used to win arguments quickly"),
        (("phone", "text", "dm", "message"), "Digital boundaries blurred or weaponized"),
        (("work", "overtime", "career", "boss"), "Work stress leaking into relationship scorekeeping"),
        (("chores", "clean", "dishes", "laundry"), "Domestic labor fights masking deeper disrespect"),
        (("public", "embarrass", "humiliate", "joke"), "Public digs that train you to shrink"),
        (("kids", "child", "custody", "coparent"), "Parenting used as leverage in adult conflict"),
        (("money", "gift", "cheap", "greedy"), "Gift-giving turned into a moral judgment game"),
        (("sleep", "bed", "insomnia", "snore"), "Rest and privacy treated as optional for one partner"),
        (("social", "party", "friends", "invite"), "Social life controlled through guilt or double standards"),
        (("therapy", "counseling", "therapist"), "Growth tools used as weapons instead of help"),
        (("religion", "church", "faith"), "Belief systems used to justify control"),
        (("body", "weight", "looks", "appearance"), "Appearance comments that land like criticism"),
        (("time", "late", "schedule", "plans"), "Chronic lateness or broken plans without repair"),
        (("apology", "sorry", "forgive"), "Sorry without change, repeated on loop"),
    )
    combined = mapping + _BULK_STORY_INSIGHT_ROWS
    hits = [msg for keys, msg in combined if any(k in blob for k in keys)]
    random.shuffle(hits)
    out: list[str] = []
    for msg in hits:
        if msg not in out:
            out.append(msg)
        if len(out) >= 3:
            break
    while len(out) < 3:
        pool = [f for f in _RSX_FLAG_FILLERS if f not in out]
        out.append(random.choice(pool))
    return out[:3]


def fetch_rsx_story_post() -> dict[str, Any]:
    """Pick one conflict-heavy story for RSX checklist videos."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSX.")
    ranked = _rank_stories_for_virality(candidates)
    focused: list[dict[str, Any]] = []
    for p in ranked:
        text = f"{p.get('title') or ''}\n{p.get('selftext') or ''}"
        if _conflict_intensity_score(text) >= 0.18:
            focused.append(p)
    pool = focused if focused else ranked
    if not pool:
        raise RuntimeError("No ranked conflict candidates for RSX.")
    return pick_story_from_ranked(pool)


def build_rsx_narration_text(post: dict[str, Any]) -> str:
    """Build a red-flag checklist narration from one Reddit story."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 200)
    flags = _rsx_red_flags_from_text(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(_VAR_RSX_OPENERS),
        f"Case: {first or title or 'A conflict situation with unclear boundaries.'}",
        f"Red flag one: {flags[0]}",
        f"Red flag two: {flags[1]}",
        f"Red flag three: {flags[2]}",
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSX_CTAS),
    ]
    return "\n\n".join(lines)


def _rsy_make_myth_line(title: str, first: str) -> str:
    base = title or first or "If someone says sorry once, trust is fully restored."
    t = _rss_trim_line(base, 170)
    if not t.endswith("."):
        t += "."
    return t


def _rsy_fact_from_text(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("communication", "talk", "conversation"), "Fact: Clarity beats assumptions in almost every conflict."),
        (("money", "rent", "debt", "bill"), "Fact: Transparent budgets reduce repeat fights more than promises do."),
        (("family", "parent", "in-laws"), "Fact: Boundaries matter most when outside pressure is high."),
        (("lied", "secret", "hid"), "Fact: Rebuilding trust needs consistent behavior over time."),
        (("angry", "yell", "scream"), "Fact: De-escalation first, solutions second, usually works better."),
        (("friend", "friends", "group"), "Fact: Loyalty without honesty often enables bigger problems."),
        (("work", "job", "boss", "career"), "Fact: Career stress spreads fastest when schedules are not protected."),
        (("roommate", "lease", "apartment"), "Fact: Written house norms prevent silent resentment."),
        (("ex", "breakup", "split"), "Fact: Closure is a process, not a single conversation."),
        (("phone", "text", "message"), "Fact: Tone is guessed wrong more often than people admit."),
        (("chore", "clean", "dishes"), "Fact: Fair splits beat heroic bursts that burn people out."),
        (("time", "late", "plans"), "Fact: Reliability is respect made visible."),
        (("jealous", "insecure", "trust"), "Fact: Security grows from actions, not surveillance."),
        (("kids", "child", "parent"), "Fact: Kids do better when adults stop making them pick sides."),
        (("therapy", "mental", "anxiety"), "Fact: Skills beat willpower when emotions run hot."),
        (("social", "party", "invite"), "Fact: Expectations need dates, not vibes."),
        (("gift", "birthday", "holiday"), "Fact: Thoughtfulness scales; price tags do not."),
        (("sleep", "tired", "exhausted"), "Fact: Sleep deprivation turns small problems into emergencies."),
        (("neighbor", "noise", "hoa"), "Fact: Calm documentation beats hallway theatrics."),
        (("pet", "dog", "cat"), "Fact: Pet conflicts are usually people conflicts in a fur coat."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSY_FACT_FALLBACKS)
    if raw.startswith("Fact:"):
        return raw
    return f"Fact: {raw}"


def fetch_rsy_story_post() -> dict[str, Any]:
    """Pick one everyday-life conflict post for myth-vs-fact shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSY.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSY.")
    return pick_story_from_ranked(ranked)


def build_rsy_narration_text(post: dict[str, Any]) -> str:
    """Build a myth-vs-fact narration from one Reddit story."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 185)
    myth = _rsy_make_myth_line(title, first)
    fact = _rsy_fact_from_text(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(_VAR_RSY_OPENERS),
        f"Myth: {myth}",
        fact,
        random.choice(
            (
                f"Case line: {first or title}",
                f"Anchor line: {first or title}",
                f"Story hook: {first or title}",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSY_CTAS),
    ]
    return "\n\n".join(lines)


def _r3u_slug(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _r3u_pick_object() -> str:
    return random.choice(R3U_OBJECT_POOL)


def _r3u_facts_for_object(obj: str) -> tuple[str, str, str]:
    key = _r3u_slug(obj)
    pool: dict[str, tuple[str, str, str]] = {
        "zipper": (
            "Most zippers use tiny Y-shaped teeth that lock by being squeezed together.",
            "The zipper was promoted as less embarrassing than buttons, so early ads targeted boots and pouches first.",
            "Many zippers fail from slider wear, not broken teeth, which is why replacing only the slider can revive them.",
        ),
        "mirror": (
            "Modern household mirrors are usually glass with a thin reflective metal layer on the back, not polished metal alone.",
            "Most bathroom mirrors look slightly green at the edge because standard glass contains trace iron.",
            "The left-right flip feeling is a brain effect: mirrors actually reverse front and back.",
        ),
        "elevator": (
            "Counterweights let an elevator move heavy loads using less energy than lifting full cabin weight every time.",
            "Most elevator doors are designed to reopen on obstruction, but the car can still move only when door locks are fully engaged.",
            "Older elevators used operators; automated button control became mainstream as safety interlocks improved.",
        ),
        "stapler": (
            "Staple strips are engineered to break into single staples smoothly as the driver blade pushes down.",
            "The classic inward-bent staple shape is called clinching and increases hold strength on thin stacks.",
            "Many staplers have a reversible anvil mode for temporary pinning that straightens staple legs for easy removal.",
        ),
        "toothbrush": (
            "Soft bristles usually clean gumlines better than hard bristles because they flex into tight edges.",
            "A toothbrush head becomes less efficient as bristles fray, even when it still looks usable at a glance.",
            "Brushing pressure matters more than speed: short, gentle passes reduce enamel and gum wear.",
        ),
        "keyboard": (
            "Standard keyboard rows come from mechanical typewriter compromises, not modern finger-efficiency science.",
            "Many keys have slightly different profiles by row so your fingers can locate home position without looking.",
            "Membrane and mechanical keyboards register presses differently, but both rely on matrix scanning for speed.",
        ),
        "umbrella": (
            "Ribs in a folding umbrella are linked with sliding joints so one motion opens multiple supports at once.",
            "Umbrella fabric often has water-repellent coating, but seam construction affects leakage more than color or pattern.",
            "Wind-resistant umbrellas usually vent air through layered canopies to reduce frame inversion.",
        ),
        "microwave": (
            "A turntable mainly evens heating by moving food through uneven energy zones inside the cavity.",
            "Microwave-safe labels focus on container stability and heat resistance, not whether the food itself changes chemically.",
            "Covering food traps steam, which helps heat spread and can reduce cold spots.",
        ),
        "headphones": (
            "Most headphones convert electrical signals to motion using a tiny driver with a lightweight diaphragm.",
            "Closed-back models isolate better because their cups block ambient sound paths around the ear.",
            "Perceived loudness changes with frequency balance, so two headphones at the same volume setting can feel very different.",
        ),
        "backpack": (
            "The chest strap on many backpacks improves stability by reducing shoulder strap drift, not by carrying most of the load.",
            "Load feels lighter when weight is packed high and close to your back because leverage is reduced.",
            "Many backpack fabrics are woven for tear resistance, then coated for water resistance as a separate layer.",
        ),
        "paper clip": (
            "A paper clip holds by spring tension and friction, not by puncturing paper like staples.",
            "The double-loop Gem clip design became popular because it balances grip and easy reuse.",
            "Paper clips can lose holding force after repeated over-bending because the wire takes a permanent set.",
        ),
        "sneaker": (
            "Sneaker midsoles usually provide most cushioning while outsoles focus on traction and wear resistance.",
            "Toe spring, the slight upward curve at the front, helps rolling motion during walking.",
            "Mesh uppers improve airflow but durability often depends on reinforcement placement, not mesh thickness alone.",
        ),
        "coffee mug": (
            "Mug handles are shaped to reduce heat transfer from the cup wall to your fingers.",
            "Ceramic mugs retain heat partly because their material stores thermal energy and cools gradually.",
            "A wider mug mouth can cool drinks faster by exposing more surface area to air.",
        ),
        "light switch": (
            "Most wall switches act as simple circuit interrupters, opening or closing current flow with a spring-loaded mechanism.",
            "Toggle switches snap quickly between states to reduce electrical arcing time.",
            "Many modern switches share similar faceplates, but internal ratings differ for lighting, motors, and specialty loads.",
        ),
        "door handle": (
            "Lever handles became widespread partly because they are easier to use with limited grip strength.",
            "Latch mechanisms keep doors closed via angled bolts that retract when the handle rotates.",
            "Some handles include privacy locks that block interior rotation only, while entry sets tie into key cylinders.",
        ),
    }
    return pool.get(
        key,
        (
            f"{obj.title()} design usually balances durability, cost, and ease of use more than looks alone.",
            f"Small material choices in a {obj} often change performance more than people expect.",
            f"Most {obj} failures come from repeated stress on one tiny component.",
        ),
    )


def build_r3u_narration_text() -> str:
    """Build a short 3-unknowns script for one ordinary object."""
    obj = _r3u_pick_object()
    hook = random.choice(_VAR_R3U_HOOKS).format(obj=obj)
    f1, f2, f3 = _r3u_facts_for_object(obj)
    cta = random.choice(_VAR_R3U_CTAS)
    lines = [
        hook,
        f"Object: {obj.title()}",
        f"Fact one: {f1}",
        f"Fact two: {f2}",
        f"Fact three: {f3}",
        cta,
    ]
    return "\n\n".join(lines)


def _rsz_extract_lesson(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Set money rules early and write them down."),
        (("family", "parent", "in-laws"), "Set boundaries before conflict escalates."),
        (("lied", "secret", "hid"), "Trust is rebuilt with actions, not one apology."),
        (("work", "boss", "coworker", "job"), "Document expectations before disagreements grow."),
        (("roommate", "house", "chores"), "Shared spaces need explicit agreements, not assumptions."),
        (("legal", "lawyer", "court"), "Get clarity on paper before you argue from memory."),
        (("landlord", "lease", "evict"), "Know your lease terms before you negotiate with adrenaline."),
        (("wedding", "marriage", "spouse"), "Align on values before you align on table seating."),
        (("kid", "child", "custody", "coparent"), "Put the kid's stability ahead of adult scorekeeping."),
        (("health", "doctor", "hospital"), "Medical fear is not a character flaw; plan while calm."),
        (("travel", "trip", "flight"), "Travel plans need a backup when stress hits."),
        (("pet", "dog", "cat"), "Pet care rules are relationship rules with fur."),
        (("neighbor", "noise", "hoa"), "Document patterns before you knock on the door angry."),
        (("addict", "sober", "alcohol"), "Support boundaries, not chaos, when substances are involved."),
        (("dating", "tinder", "bumble"), "Early dating is data collection, not a contract."),
    )
    return _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSZ_LESSON_FALLBACKS)


def fetch_rsz_story_post() -> dict[str, Any]:
    """Pick one practical-life story for lesson-learned shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSZ.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSZ.")
    return pick_story_from_ranked(ranked)


def build_rsz_narration_text(post: dict[str, Any]) -> str:
    """Build a lesson-learned script from one Reddit story."""
    title = _rss_trim_line(str(post.get("title") or ""), 165)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    lesson = _rsz_extract_lesson(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(_VAR_RSZ_OPENERS),
        random.choice(
            (
                f"What happened: {first or title}",
                f"The setup: {first or title}",
                f"Here is the situation: {first or title}",
            )
        ),
        random.choice(
            (
                f"Lesson learned: {lesson}",
                f"Takeaway: {lesson}",
                f"The lesson I would steal: {lesson}",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSZ_CTAS),
    ]
    return "\n\n".join(lines)


def _rsw_choice_pair(title: str, body: str) -> tuple[str, str]:
    blob = f"{title}\n{body}".lower()

    def pick(pairs: tuple[tuple[str, str], ...]) -> tuple[str, str]:
        return random.choice(pairs)

    if any(k in blob for k in ("money", "rent", "debt", "bill", "loan", "mortgage", "salary")):
        return pick(
            (
                ("protect your budget first", "help anyway to keep peace"),
                ("freeze shared spending until terms are clear", "cover it to avoid a fight"),
                ("negotiate a written payment plan", "eat the cost for harmony"),
                ("say no with a clear reason", "say yes and resent it quietly"),
                ("split costs exactly down the line", "absorb extra for stability"),
            )
        )
    if any(k in blob for k in ("family", "parent", "in-laws", "sibling", "mother", "father")):
        return pick(
            (
                ("set boundaries now", "avoid conflict and stay quiet"),
                ("host a calm boundaries talk", "keep the peace with passive agreement"),
                ("define what you will not discuss", "let relatives steer the topic"),
                ("leave early when disrespect shows up", "stay and endure the gathering"),
                ("present a united front with your partner", "avoid backing anyone publicly"),
            )
        )
    if any(k in blob for k in ("lied", "secret", "hid", "trust", "cheat", "affair")):
        return pick(
            (
                ("pause and verify before trusting", "forgive immediately and move on"),
                ("ask for transparency protocols", "pretend it never happened"),
                ("require couples counseling if staying", "decide alone without support"),
                ("protect your accounts and documents", "share access to prove good faith"),
            )
        )
    if any(k in blob for k in ("work", "boss", "coworker", "job", "hr", "layoff")):
        return pick(
            (
                ("document everything first", "handle it informally and hope it resolves"),
                ("email a concise summary after meetings", "keep it verbal to stay friendly"),
                ("involve hr with facts", "avoid hr to protect relationships"),
                ("look for a new role quietly", "fight for this role publicly"),
            )
        )
    if any(k in blob for k in ("roommate", "lease", "apartment", "landlord")):
        return pick(
            (
                ("rewrite house rules with signatures", "hint harder and hope they notice"),
                ("move out on a timeline", "tough it out until the lease ends"),
                ("involve the landlord in writing", "keep the landlord out of drama"),
            )
        )
    if any(k in blob for k in ("neighbor", "hoa", "noise")):
        return pick(
            (
                ("log incidents with dates", "confront them hot in the hallway"),
                ("offer a compromise schedule", "demand silence with no middle ground"),
            )
        )
    if any(k in blob for k in ("kid", "child", "custody", "coparent")):
        return pick(
            (
                ("use a parenting app for logistics", "coordinate by text when angry"),
                ("get a court-approved plan", "wing it week to week"),
            )
        )
    if any(k in blob for k in ("pet", "dog", "cat")):
        return pick(
            (
                ("split vet bills by agreement", "pay alone to avoid arguing"),
                ("rehome if care is unequal", "keep the pet and absorb the work"),
            )
        )
    if any(k in blob for k in ("wedding", "bride", "groom")):
        return pick(
            (
                ("cut the guest list to protect budget", "borrow to protect feelings"),
                ("elope and skip politics", "host the big event and manage fallout"),
            )
        )
    if any(k in blob for k in ("legal", "lawyer", "court", "police")):
        return pick(
            (
                ("follow counsel even if it feels slow", "rush a public confrontation"),
                ("preserve evidence calmly", "delete threads to reduce stress"),
            )
        )
    return random.choice(_DEFAULT_RSW_PAIR_FALLBACKS)


def fetch_rsw_story_post() -> dict[str, Any]:
    """Pick one dilemma-like story for what-would-you-do shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSW.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSW.")
    return pick_story_from_ranked(ranked)


def build_rsw_narration_text(post: dict[str, Any]) -> str:
    """Build a what-would-you-do choice script."""
    title = _rss_trim_line(str(post.get("title") or ""), 165)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 180)
    opt_a, opt_b = _rsw_choice_pair(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(_VAR_RSW_OPENERS),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"The moment: {first or title}",
                f"Setup: {first or title}",
            )
        ),
        f"Option A: {opt_a}.",
        f"Option B: {opt_b}.",
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSW_CTAS),
    ]
    return "\n\n".join(lines)


def _rsv_advice_from_text(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Advice: Set a hard limit and communicate it early."),
        (("family", "parent", "in-laws"), "Advice: Use clear boundaries and repeat them calmly."),
        (("lied", "secret", "hid", "trust"), "Advice: Ask for consistent actions before full trust."),
        (("work", "boss", "coworker", "job"), "Advice: Document facts first, then escalate professionally."),
        (("roommate", "chores", "house"), "Advice: Write shared rules and review them weekly."),
        (("neighbor", "hoa", "noise"), "Advice: Keep a dated log before you escalate."),
        (("legal", "lawyer", "court"), "Advice: Let paperwork guide you more than adrenaline."),
        (("dating", "boyfriend", "girlfriend", "partner"), "Advice: Name needs plainly before resentment stockpiles."),
        (("ex", "coparent", "custody"), "Advice: Use boring, repeatable channels for logistics."),
        (("landlord", "lease", "evict"), "Advice: Cite lease clauses in writing, not in hallway debates."),
        (("addict", "sober", "alcohol"), "Advice: Protect yourself first; support is not self-destruction."),
        (("school", "teacher", "principal"), "Advice: Email facts; avoid venting in school parking lots."),
        (("health", "doctor", "hospital"), "Advice: Bring an advocate or notes when stress is high."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSV_ADVICE_FALLBACKS)
    if raw.startswith("Advice:"):
        return raw
    return f"Advice: {raw}"


def fetch_rsv_story_post() -> dict[str, Any]:
    """Pick one story suitable for quick practical advice."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSV.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSV.")
    return pick_story_from_ranked(ranked)


def build_rsv_narration_text(post: dict[str, Any]) -> str:
    """Build a quick-advice script from one Reddit story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 170)
    advice = _rsv_advice_from_text(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Quick advice in under a minute.",
                "Speedrun advice: one move you can make today.",
                "Practical take: short, blunt, usable.",
                "Advice mode: no fluff, just a next step.",
            )
        ),
        random.choice(
            (
                f"Situation: {first or title}",
                f"Context: {first or title}",
                f"Here is the gist: {first or title}",
            )
        ),
        advice,
        random.choice(_VAR_RSV_ACTIONS),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSV_CTAS),
    ]
    return "\n\n".join(lines)


def build_rst2_narration_text(post: dict[str, Any]) -> str:
    """Build a one-minute timeline script from one Reddit story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 155)
    second = ""
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", body) if p.strip()]
    if len(parts) > 1:
        second = _rss_trim_line(parts[1], 155)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "One-minute timeline.",
                "Timeline mode: same story, faster beats.",
                "Sixty-second arc: setup, rise, snap.",
            )
        ),
        random.choice(
            (
                f"Minute zero: {first or title}",
                f"Start here: {first or title}",
                f"Opening beat: {first or title}",
            )
        ),
        random.choice(
            (
                f"Then: {second or 'Tension builds and choices get harder.'}",
                f"Next beat: {second or 'Pressure rises and options narrow.'}",
            )
        ),
        random.choice(_VAR_RST2_BEATS),
        random.choice(
            (
                "Final moment: one decision changes the outcome.",
                "Last beat: one choice tilts everything.",
                "Closing beat: a single move decides the tone.",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RST2_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rst2_story_post() -> dict[str, Any]:
    """Pick one high-conflict story for timeline shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RST2.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RST2.")
    return pick_story_from_ranked(ranked)


def build_rsq_narration_text(post: dict[str, Any]) -> str:
    """Build a quote-centric script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", body) if p.strip()]
    quote = ""
    for p in parts:
        if len(p) >= 45:
            quote = _rss_trim_line(p, 170)
            break
    if not quote:
        quote = _rss_trim_line(first if (first := _first_sentence(body)) else title, 170)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Quote of the story.",
                "The line that haunts this whole thread.",
                "One sentence that carries the whole mood.",
            )
        ),
        f'\"{quote}\"',
        random.choice(
            (
                "This one line tells you everything about the situation.",
                "If you only hear one line, hear this.",
                "That line is doing a lot of narrative work.",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSQ_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rsq_story_post() -> dict[str, Any]:
    """Pick one story with strong quote-worthy lines."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSQ.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSQ.")
    return pick_story_from_ranked(ranked)


def _rsk_takeaway_from_text(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Key takeaway: financial boundaries should be explicit from day one."),
        (("family", "parent", "in-laws"), "Key takeaway: clear boundaries prevent outside pressure from taking over."),
        (("lied", "secret", "trust"), "Key takeaway: trust rebuilds slowly through consistent behavior."),
        (("work", "boss", "coworker", "job"), "Key takeaway: document facts before emotions drive decisions."),
        (("roommate", "house", "chores"), "Key takeaway: shared expectations must be written, not assumed."),
        (("legal", "lawyer", "court"), "Key takeaway: paper trails beat memory when stakes rise."),
        (("neighbor", "hoa", "noise"), "Key takeaway: patterns beat one loud night in court of public opinion."),
        (("dating", "partner", "spouse"), "Key takeaway: compatibility is partly logistics, not only chemistry."),
        (("kid", "child", "custody"), "Key takeaway: stability for kids is a strategy, not a vibe."),
        (("health", "doctor", "therapy"), "Key takeaway: care plans need maintenance like relationships do."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSK_TAKEAWAY_FALLBACKS)
    if raw.startswith("Key takeaway:"):
        return raw
    return f"Key takeaway: {raw}"


def build_rsk_narration_text(post: dict[str, Any]) -> str:
    """Build a key-takeaway script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    takeaway = _rsk_takeaway_from_text(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Story breakdown in one key takeaway.",
                "One story, one headline lesson.",
                "TLDR with teeth: the real point of this thread.",
            )
        ),
        random.choice(
            (
                f"Context: {first or title}",
                f"Setup: {first or title}",
            )
        ),
        takeaway,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSK_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rsk_story_post() -> dict[str, Any]:
    """Pick one story for key-takeaway shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSK.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSK.")
    return pick_story_from_ranked(ranked)


def _rsm_mistake_from_text(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Avoid this mistake: making money decisions without clear limits."),
        (("family", "parent", "in-laws"), "Avoid this mistake: letting outside pressure decide your boundaries."),
        (("lied", "secret", "trust"), "Avoid this mistake: restoring trust before behavior changes."),
        (("work", "boss", "coworker", "job"), "Avoid this mistake: reacting before documenting facts."),
        (("roommate", "house", "chores"), "Avoid this mistake: assuming shared expectations are obvious."),
        (("legal", "police", "court"), "Avoid this mistake: improvising legal strategy from Reddit adrenaline."),
        (("neighbor", "hoa"), "Avoid this mistake: escalating without a dated record."),
        (("wedding", "marriage"), "Avoid this mistake: financing feelings with debt you resent later."),
        (("ex", "coparent"), "Avoid this mistake: using kids as messengers."),
        (("landlord", "lease"), "Avoid this mistake: breaking rules quietly and hoping for kindness."),
        (("phone", "text", "dm"), "Avoid this mistake: sending paragraphs when you need a boundary, not a jury."),
        (("addict", "alcohol", "sober"), "Avoid this mistake: becoming the entire support system alone."),
    )
    return _pick_random_matching_line(blob, mapping, _DEFAULT_RSM_MISTAKE_FALLBACKS)


def build_rsm_narration_text(post: dict[str, Any]) -> str:
    """Build a mistake-to-avoid script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    mistake = _rsm_mistake_from_text(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "One story. One mistake to avoid.",
                "Mistake radar: do not repeat this one.",
                "Anti-pattern alert from a real post.",
            )
        ),
        random.choice(
            (
                f"Context: {first or title}",
                f"Where it starts: {first or title}",
            )
        ),
        mistake,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSM_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rsm_story_post() -> dict[str, Any]:
    """Pick one story for mistake-to-avoid shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSM.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSM.")
    return pick_story_from_ranked(ranked)


def _rsr_reality_check_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Reality check: feelings do not erase financial limits."),
        (("family", "parent", "in-laws"), "Reality check: peace without boundaries is usually temporary."),
        (("lied", "secret", "trust"), "Reality check: trust follows patterns, not promises."),
        (("work", "boss", "coworker", "job"), "Reality check: professional problems need documented facts."),
        (("roommate", "house", "chores"), "Reality check: unclear expectations create repeat conflicts."),
        (("legal", "lawyer"), "Reality check: courts care about evidence, not your best monologue."),
        (("dating", "tinder", "bumble"), "Reality check: chemistry does not cancel incompatibility."),
        (("kid", "custody"), "Reality check: kids notice tension even when adults whisper."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSR_REALITY_FALLBACKS)
    if raw.startswith("Reality check:"):
        return raw
    return f"Reality check: {raw}"


def build_rsr_narration_text(post: dict[str, Any]) -> str:
    """Build a reality-check script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    check = _rsr_reality_check_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Quick reality check.",
                "Reality check, no sugar coating.",
                "Ground truth pass on this situation.",
            )
        ),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"Setup: {first or title}",
            )
        ),
        check,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSR_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rsr_story_post() -> dict[str, Any]:
    """Pick one story for reality-check shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSR.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSR.")
    return pick_story_from_ranked(ranked)


def _rsh_truth_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Hard truth: ignoring numbers does not protect relationships."),
        (("family", "parent", "in-laws"), "Hard truth: avoiding boundaries creates bigger conflict later."),
        (("lied", "secret", "trust"), "Hard truth: trust without accountability is denial."),
        (("work", "boss", "coworker", "job"), "Hard truth: silence at work is often interpreted as consent."),
        (("roommate", "house", "chores"), "Hard truth: unclear rules always become recurring fights."),
        (("friend", "friends"), "Hard truth: friendship is not a free pass to skip reciprocity."),
        (("ex", "breakup"), "Hard truth: closure is often a decision, not a delivery."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSH_HARD_FALLBACKS)
    if raw.startswith("Hard truth:"):
        return raw
    return f"Hard truth: {raw}"


def fetch_rsh_story_post() -> dict[str, Any]:
    """Pick one story for hard-truth shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSH.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSH.")
    return pick_story_from_ranked(ranked)


def build_rsh_narration_text(post: dict[str, Any]) -> str:
    """Build a hard-truth script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 160)
    truth = _rsh_truth_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Hard truth in 30 seconds.",
                "Hard truth, fast delivery.",
                "Blunt read: the part people avoid saying.",
            )
        ),
        random.choice(
            (
                f"Context: {first or title}",
                f"Situation: {first or title}",
            )
        ),
        truth,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSH_CTAS),
    ]
    return "\n\n".join(lines)


def _rsu_take_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Unpopular take: saying no about money can be the most caring choice."),
        (("family", "parent", "in-laws"), "Unpopular take: boundaries are not disrespect, they are maintenance."),
        (("lied", "secret", "trust"), "Unpopular take: forgiveness and access are not the same thing."),
        (("work", "boss", "coworker", "job"), "Unpopular take: being liked at work is less important than being clear."),
        (("roommate", "house", "chores"), "Unpopular take: written rules beat good intentions every time."),
        (("wedding", "bride", "groom"), "Unpopular take: your wedding is not a debt sentence."),
        (("neighbor", "hoa"), "Unpopular take: being nice once does not obligate you forever."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSU_UNPOPULAR_FALLBACKS)
    if raw.startswith("Unpopular take:"):
        return raw
    return f"Unpopular take: {raw}"


def fetch_rsu_story_post() -> dict[str, Any]:
    """Pick one story for unpopular-take shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSU.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSU.")
    return pick_story_from_ranked(ranked)


def build_rsu_narration_text(post: dict[str, Any]) -> str:
    """Build an unpopular-take script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    take = _rsu_take_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Unpopular take of the day.",
                "Unpopular opinion, popular consequences.",
                "The take people dislike until it saves them.",
            )
        ),
        random.choice(
            (
                f"Context: {first or title}",
                f"Backdrop: {first or title}",
            )
        ),
        take,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSU_CTAS),
    ]
    return "\n\n".join(lines)


def _rsv2_verdict_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Verdict: protect your financial baseline first."),
        (("family", "parent", "in-laws"), "Verdict: boundaries are mandatory, not optional."),
        (("lied", "secret", "trust"), "Verdict: trust should be earned back in steps."),
        (("work", "boss", "coworker", "job"), "Verdict: document now, escalate only if needed."),
        (("roommate", "house", "chores"), "Verdict: define rules clearly before resentment builds."),
        (("legal", "police"), "Verdict: follow professional guidance before you post the story."),
        (("kid", "custody"), "Verdict: choose the path that reduces instability for the child."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSV2_VERDICT_FALLBACKS)
    if raw.startswith("Verdict:"):
        return raw
    return f"Verdict: {raw}"


def fetch_rsv2_story_post() -> dict[str, Any]:
    """Pick one story for verdict shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSV2.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSV2.")
    return pick_story_from_ranked(ranked)


def build_rsv2_narration_text(post: dict[str, Any]) -> str:
    """Build a verdict-style script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    verdict = _rsv2_verdict_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Final verdict in under a minute.",
                "Verdict mode: one ruling, no fluff.",
                "Closing argument style: pick the least-bad path.",
            )
        ),
        random.choice(
            (
                f"Case: {first or title}",
                f"Docket summary: {first or title}",
            )
        ),
        verdict,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSV2_CTAS),
    ]
    return "\n\n".join(lines)


def _ptk2_cold_take_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Cold take: budgeting beats apology every time."),
        (("family", "parent", "in-laws"), "Cold take: boundaries are more useful than approval."),
        (("lied", "secret", "trust"), "Cold take: transparency is the minimum, not a bonus."),
        (("work", "boss", "coworker", "job"), "Cold take: documented facts win over office politics."),
        (("roommate", "house", "chores"), "Cold take: vague agreements create predictable drama."),
        (("dating", "ex"), "Cold take: closure is often a decision you make alone."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_PTK2_COLD_FALLBACKS)
    if raw.startswith("Cold take:"):
        return raw
    return f"Cold take: {raw}"


def fetch_ptk2_story_post() -> dict[str, Any]:
    """Pick one story for cold-take shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for PTK2.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for PTK2.")
    return pick_story_from_ranked(ranked)


def build_ptk2_narration_text(post: dict[str, Any]) -> str:
    """Build a cold-take script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    take = _ptk2_cold_take_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        "Cold take in 30 seconds.",
        f"Context: {first or title}",
        take,
        f"Source: r slash {src_sub}.",
        "Too harsh, or accurate? Comment below.",
    ]
    return "\n\n".join(lines)


def _rsa_accountability_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Accountability means owning your financial promises."),
        (("family", "parent", "in-laws"), "Accountability means enforcing boundaries you set."),
        (("lied", "secret", "trust"), "Accountability means repair through consistent actions."),
        (("work", "boss", "coworker", "job"), "Accountability means documenting and following through."),
        (("roommate", "house", "chores"), "Accountability means doing your share without reminders."),
        (("kid", "child", "custody"), "Accountability means protecting kids from adult scorekeeping."),
        (("legal", "court"), "Accountability means complying with what you agreed to in writing."),
        (("friend", "friends"), "Accountability means showing up when reciprocity matters."),
        (("ex", "coparent"), "Accountability means keeping logistics boring and consistent."),
    )
    return _pick_random_matching_line(blob, mapping, _DEFAULT_RSA_ACCOUNTABILITY_FALLBACKS)


def fetch_rsa_story_post() -> dict[str, Any]:
    """Pick one story for accountability shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSA.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSA.")
    return pick_story_from_ranked(ranked)


def build_rsa_narration_text(post: dict[str, Any]) -> str:
    """Build an accountability-focused script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    line = _rsa_accountability_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Accountability check.",
                "Who owns what? Accountability pass.",
                "Responsibility audit on this situation.",
            )
        ),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"Scene: {first or title}",
            )
        ),
        line,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSA_CTAS),
    ]
    return "\n\n".join(lines)


def _rsb_boundary_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Boundary check: define what support stops where."),
        (("family", "parent", "in-laws"), "Boundary check: decide what is your call, not theirs."),
        (("lied", "secret", "trust"), "Boundary check: access should match accountability."),
        (("work", "boss", "coworker", "job"), "Boundary check: separate professionalism from people-pleasing."),
        (("roommate", "house", "chores"), "Boundary check: rules need consequences, not reminders."),
        (("phone", "text", "dm"), "Boundary check: availability is not unlimited."),
        (("time", "schedule", "calendar"), "Boundary check: protect time like it is money."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSB_BOUNDARY_FALLBACKS)
    if raw.startswith("Boundary check:"):
        return raw
    return f"Boundary check: {raw}"


def fetch_rsb_story_post() -> dict[str, Any]:
    """Pick one story for boundary-check shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSB.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSB.")
    return pick_story_from_ranked(ranked)


def build_rsb_narration_text(post: dict[str, Any]) -> str:
    """Build a boundary-check script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    line = _rsb_boundary_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Boundary check in under a minute.",
                "Boundary audit: what is yours to control?",
                "Draw the line test: quick and blunt.",
            )
        ),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"Situation: {first or title}",
            )
        ),
        line,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSB_CTAS),
    ]
    return "\n\n".join(lines)


def _rse_empathy_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Empathy check: fear around money can look like anger."),
        (("family", "parent", "in-laws"), "Empathy check: pressure and loyalty can conflict at the same time."),
        (("lied", "secret", "trust"), "Empathy check: hurt people still need accountability."),
        (("work", "boss", "coworker", "job"), "Empathy check: stress does not excuse unclear behavior."),
        (("roommate", "house", "chores"), "Empathy check: assumptions often hide unspoken expectations."),
        (("kid", "child"), "Empathy check: kids feel tension even when adults whisper."),
        (("health", "sick", "hospital"), "Empathy check: pain shrinks patience for everyone."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSE_EMPATHY_FALLBACKS)
    if raw.startswith("Empathy check:"):
        return raw
    return f"Empathy check: {raw}"


def fetch_rse_story_post() -> dict[str, Any]:
    """Pick one story for empathy-check shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSE.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSE.")
    return pick_story_from_ranked(ranked)


def build_rse_narration_text(post: dict[str, Any]) -> str:
    """Build an empathy-check script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    line = _rse_empathy_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Empathy check.",
                "Hold two feelings at once: empathy pass.",
                "Feelings check without picking a villain.",
            )
        ),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"Scene: {first or title}",
            )
        ),
        line,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSE_CTAS),
    ]
    return "\n\n".join(lines)


def _rsn_negotiation_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Negotiation check: define numbers, deadlines, and consequences."),
        (("family", "parent", "in-laws"), "Negotiation check: align on one boundary before discussing details."),
        (("lied", "secret", "trust"), "Negotiation check: ask for behavior changes, not just apologies."),
        (("work", "boss", "coworker", "job"), "Negotiation check: agree on expectations in writing."),
        (("roommate", "house", "chores"), "Negotiation check: split responsibilities with clear ownership."),
        (("custody", "coparent", "kid"), "Negotiation check: pick channels that reduce misreads."),
        (("landlord", "lease"), "Negotiation check: cite the lease before you cite feelings."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSN_NEGOTIATION_FALLBACKS)
    if raw.startswith("Negotiation check:"):
        return raw
    return f"Negotiation check: {raw}"


def fetch_rsn_story_post() -> dict[str, Any]:
    """Pick one story for negotiation-check shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSN.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSN.")
    return pick_story_from_ranked(ranked)


def build_rsn_narration_text(post: dict[str, Any]) -> str:
    """Build a negotiation-check script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    line = _rsn_negotiation_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        "Negotiation check.",
        f"Scenario: {first or title}",
        line,
        f"Source: r slash {src_sub}.",
        "Would this solve it, or not enough? Comment below.",
    ]
    return "\n\n".join(lines)


def _rsp2_shift_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Perspective shift: this may be fear, not greed."),
        (("family", "parent", "in-laws"), "Perspective shift: this might be loyalty pressure, not malice."),
        (("lied", "secret", "trust"), "Perspective shift: accountability and compassion can coexist."),
        (("work", "boss", "coworker", "job"), "Perspective shift: stress can explain tone, not actions."),
        (("roommate", "house", "chores"), "Perspective shift: conflict may be about clarity, not character."),
        (("neighbor", "hoa"), "Perspective shift: proximity makes small annoyances feel huge."),
        (("ex", "breakup"), "Perspective shift: old wounds can hijack new reactions."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSP2_SHIFT_FALLBACKS)
    if raw.startswith("Perspective shift:"):
        return raw
    return f"Perspective shift: {raw}"


def fetch_rsp2_story_post() -> dict[str, Any]:
    """Pick one story for perspective-shift shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSP2.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSP2.")
    return pick_story_from_ranked(ranked)


def build_rsp2_narration_text(post: dict[str, Any]) -> str:
    """Build a perspective-shift script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    line = _rsp2_shift_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Perspective shift.",
                "Try a different seat in the same room.",
                "Angle swap: same facts, new lens.",
            )
        ),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"Starting picture: {first or title}",
            )
        ),
        line,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSP2_CTAS),
    ]
    return "\n\n".join(lines)

_ORL_SCIENCE_TOPICS_EN: tuple[str, ...] = (
    # Cosmology & gravity
    "Zero-point energy",
    "Dark matter",
    "Dark energy",
    "Cosmic inflation",
    "Cosmic microwave background",
    "Big Bang",
    "Chronology of the universe",
    "Ultimate fate of the universe",
    "Heat death of the universe",
    "Big Crunch",
    "Big Bounce",
    "Multiverse",
    "Observable universe",
    "Cosmological constant",
    "Lambda-CDM model",
    "Quintessence (physics)",
    "Phantom energy",
    "Vacuum energy",
    "False vacuum",
    "Quantum foam",
    "Chaos theory",
    "Strange attractor",
    "Black hole",
    "Black hole thermodynamics",
    "Black hole information paradox",
    "Hawking radiation",
    "Penrose process",
    "Ergosphere",
    "Photon sphere",
    "Event horizon",
    "Apparent horizon",
    "Schwarzschild metric",
    "Kerr metric",
    "Gravitational singularity",
    "Naked singularity",
    "Primordial black hole",
    "Intermediate-mass black hole",
    "Supermassive black hole",
    "Quasar",
    "Blazar",
    "Active galactic nucleus",
    "Spaghettification",
    "Gravitational time dilation",
    "Gravitational lens",
    "Gravitational redshift",
    "Gravitational wave",
    "LIGO",
    "Frame-dragging",
    "Geodetic effect",
    "Wormhole",
    "Einstein–Rosen bridge",
    "White hole",
    "ER=EPR",
    "Holographic principle",
    "Firewall (physics)",
    "Cosmic censorship hypothesis",
    "No-hair theorem",
    "Information paradox",
    "Olbers' paradox",
    "Fermi paradox",
    "Great Filter",
    "Drake equation",
    "Kardashev scale",
    "Dyson sphere",
    "Matrioshka brain",
    "Technological singularity",
    "Simulation hypothesis",
    "Digital physics",
    "Mathematical universe hypothesis",
    "Anthropic principle",
    "Boltzmann brain",
    "Vacuum catastrophe",
    "Axis of evil (cosmology)",
    "Horizon problem",
    "Flatness problem",
    "Magnetic monopole",
    "Baryon asymmetry",
    "Leptogenesis",
    "Cosmic string",
    "Domain wall (physics)",
    # Quantum
    "Quantum mechanics",
    "Wave–particle duality",
    "Uncertainty principle",
    "Quantum entanglement",
    "Bell's theorem",
    "EPR paradox",
    "Quantum nonlocality",
    "Quantum decoherence",
    "Quantum superposition",
    "Schrödinger's cat",
    "Quantum Zeno effect",
    "Quantum tunnelling",
    "Quantum field theory",
    "Quantum chromodynamics",
    "Quantum electrodynamics",
    "Standard Model",
    "Higgs mechanism",
    "Higgs boson",
    "Neutrino oscillation",
    "CP violation",
    "Strong CP problem",
    "Hierarchy problem",
    "Naturalness (physics)",
    "Supersymmetry",
    "String theory",
    "M-theory",
    "Loop quantum gravity",
    "Causal sets",
    "Emergent gravity",
    "Many-worlds interpretation",
    "Copenhagen interpretation",
    "Pilot-wave theory",
    "Quantum Bayesianism",
    "Quantum Darwinism",
    "Quantum error correction",
    "Topological quantum field theory",
    "Anyon",
    "Quantum Hall effect",
    "Casimir effect",
    "Lamb shift",
    "Aharonov–Bohm effect",
    "Quantum teleportation",
    "Quantum key distribution",
    "Delayed-choice quantum eraser",
    "Wheeler's delayed-choice experiment",
    "Double-slit experiment",
    "Stern–Gerlach experiment",
    "Ultraviolet catastrophe",
    "Ultraviolet divergence",
    "Anomaly (physics)",
    "Chiral anomaly",
    "Color confinement",
    "Asymptotic freedom",
    "Parton (particle physics)",
    "Quark–gluon plasma",
    "Strange matter",
    "Strangelet",
    "Neutron star",
    "Magnetar",
    "Pulsar",
    "White dwarf",
    "Chandrasekhar limit",
    "Type Ia supernova",
    "Gamma-ray burst",
    "Fast radio burst",
    "Wow! signal",
    "Time crystal (physics)",
    "Topological insulator",
    "Time travel",
    "Grandfather paradox",
    "Twin paradox",
    "Ladder paradox",
    "Ehrenfest paradox",
    "Relativity of simultaneity",
    "Rietdijk–Putnam argument",
    "Block universe",
    "Eternalism (philosophy of time)",
    # Thermodynamics & statistical
    "Entropy",
    "Second law of thermodynamics",
    "Maxwell's demon",
    "Brownian motion",
    "Arrow of time",
    "Loschmidt's paradox",
    "Gibbs paradox",
    "Phase transition",
    "Critical opalescence",
    "Self-organized criticality",
    "Perpetual motion",
    "Thermodynamics of the universe",
    # Chemistry & materials
    "Fullerene",
    "Graphene",
    "High-temperature superconductivity",
    "Superconductivity",
    "Superfluidity",
    "Bose–Einstein condensate",
    "Chemical bond",
    "Catalysis",
    "Enzyme",
    "Origin of life",
    "RNA world",
    "Abiogenesis",
    "Panspermia",
    "Extremophile",
    "Water memory",
    "Polywater",
    # Biology & neuroscience
    "Evolution",
    "Natural selection",
    "Punctuated equilibrium",
    "Horizontal gene transfer",
    "Endosymbiotic theory",
    "Mitochondrial Eve",
    "Y-chromosomal Adam",
    "Epigenetics",
    "CRISPR gene editing",
    "Consciousness",
    "Hard problem of consciousness",
    "Global workspace theory",
    "Integrated information theory",
    "Neuroplasticity",
    "Split-brain",
    "Phantom limb",
    "Synesthesia",
    "Lucid dream",
    "Near-death experience",
    "Placebo",
    "Nocebo",
    "Hematopoietic stem cell transplantation",
    "Immunotherapy",
    "Senescence",
    "Telomere",
    "Hayflick limit",
    "Biological immortality",
    "Turritopsis dohrnii",
    "Cambrian explosion",
    "Mass extinction",
    "Permian–Triassic extinction event",
    "Cretaceous–Paleogene extinction event",
    "Dinosaur",
    "Feathered dinosaur",
    "Archaeopteryx",
    "Human evolution",
    "Out of Africa theory",
    "Neanderthal",
    "Denisovan",
    "Mitochondrial DNA",
    "Virus",
    "Prion",
    "Giant virus",
    "Horizontal gene transfer",
    # Earth & space
    "Plate tectonics",
    "Geomagnetic reversal",
    "Van Allen radiation belt",
    "Aurora",
    "Ozone depletion",
    "Climate change",
    "Atlantic meridional overturning circulation",
    "Snowball Earth",
    "Great Oxygenation Event",
    "K–Pg boundary",
    "Impact event",
    "Tunguska event",
    "Chelyabinsk meteor",
    "Shoemaker–Levy 9",
    "\u02bbOumuamua",
    "Titan (moon)",
    "Europa (moon)",
    "Enceladus",
    "Exoplanet",
    "Hot Jupiter",
    "Super-Earth",
    "Habitable zone",
    "Faint young Sun paradox",
    "Rare Earth hypothesis",
    "Moon",
    "Formation and evolution of the Solar System",
    "Planet Nine",
    "Kuiper belt",
    "Oort cloud",
    "Voyager program",
    "Pioneer anomaly",
    "Flyby anomaly",
    # Math, logic, foundations
    "Gödel's incompleteness theorems",
    "Halting problem",
    "P versus NP problem",
    "Riemann hypothesis",
    "Twin prime conjecture",
    "Goldbach's conjecture",
    "Collatz conjecture",
    "Navier–Stokes existence and smoothness",
    "Yang–Mills existence and mass gap",
    "Hodge conjecture",
    "Continuum hypothesis",
    "Banach–Tarski paradox",
    "Russell's paradox",
    "Barber paradox",
    "Ship of Theseus",
    "Sorites paradox",
    "Zeno's paradoxes",
    "Monty Hall problem",
    "Birthday problem",
    "Secretary problem",
    "Prisoner's dilemma",
    "Tragedy of the commons",
    "Butterfly effect",
    "Lorenz system",
    "Mandelbrot set",
    "Fractal",
    "Fibonacci sequence",
    "Golden ratio",
    "Euler's identity",
    "Imaginary unit",
    "Complex number",
    "Quaternion",
    "Infinity",
    "Hilbert's hotel",
    "Gabriel's horn",
    "Klein bottle",
    "Möbius strip",
    "Four color theorem",
    "Proof of Fermat's Last Theorem",
    "Twin prime",
    # Physics misc
    "Special relativity",
    "General relativity",
    "Equivalence principle",
    "Lorentz transformation",
    "Time dilation",
    "Length contraction",
    "Michelson–Morley experiment",
    "Luminiferous aether",
    "Nuclear fusion",
    "Nuclear fission",
    "Cold fusion",
    "Tokamak",
    "ITER",
    "Nuclear magnetic resonance",
    "Magnetic resonance imaging",
    "Particle accelerator",
    "Large Hadron Collider",
    "Antimatter",
    "Positron",
    "Muon",
    "Neutrino",
    "Neutrino detector",
    "Solar neutrino problem",
    "CP violation",
    "Symmetry breaking",
    "Spontaneous symmetry breaking",
    "Higgs boson",
    "W and Z bosons",
    "Gluon",
    "Quark",
    "Parton (particle physics)",
    "Color charge",
    "Strong interaction",
    "Weak interaction",
    "Electroweak interaction",
    "Grand Unified Theory",
    "Theory of everything (physics)",
    "Technicolor (physics)",
    "Preon",
    "Tachyon",
    "Virtual particle",
    "Vacuum state",
    "Zero-point energy",
    "Casimir effect",
    "Sonoluminescence",
    "Ball lightning",
    "St. Elmo's fire",
    "Sprites (lightning)",
    "Earthquake light",
    "Mpemba effect",
    "Leidenfrost effect",
    "Brazil nut effect",
    "Kaye effect",
    "Triboluminescence",
    "Cherenkov radiation",
    "Synchrotron radiation",
    "Bremsstrahlung",
    "Photoelectric effect",
    "Compton scattering",
    "Pair production",
    "Annihilation",
    "Quantum electrodynamics",
    "Renormalization",
    "Anomaly (physics)",
    "CP violation",
    "Neutron",
    "Proton decay",
    "Proton",
    "Electron",
    "Muon g-2",
    "Anomalous magnetic dipole moment",
    "Fine-structure constant",
    "Planck units",
    "Planck epoch",
    "Planck length",
    "Planck time",
    "Observable universe",
    "Cosmic inflation",
    "Eternal inflation",
    "Inflation (cosmology)",
    "Scalar field (physics)",
    "Inflaton",
    "BICEP and Keck Array",
    "Baryogenesis",
    "Leptogenesis",
    "Electroweak epoch",
    "Quark epoch",
    "Hadron epoch",
    "Lepton epoch",
    "Photon epoch",
    "Big Bang nucleosynthesis",
    "Cosmic neutrino background",
    "Dark fluid",
    "Modified Newtonian dynamics",
    "Modified gravity",
    "Emergent gravity",
    "Verlinde's entropic gravity",
    "Unparticle",
    # Extra cosmology / astrophysics
    "Cosmic ray",
    "Ultra-high-energy cosmic ray",
    "Oh-My-God particle",
    "Centaurus A",
    "Sagittarius A*",
    "Andromeda Galaxy",
    "Redshift",
    "Blueshift",
    "Hubble's law",
    "Accelerating expansion of the universe",
    "Deceleration parameter",
    "Sound horizon",
    "Silk damping",
    "Baryon acoustic oscillations",
    "Recombination (cosmology)",
    "Photon decoupling",
    "Last scattering surface",
    "Cosmic infrared background",
    "Diffuse intergalactic medium",
    "Lyman-alpha forest",
    "Intergalactic star",
    "Hypervelocity star",
    "Stellar nucleosynthesis",
    "R-process",
    "S-process",
    "Helium flash",
    "Red giant",
    "Asymptotic giant branch",
    "Planetary nebula",
    "Supernova nucleosynthesis",
    "Pair instability supernova",
    "Hypernova",
    "Kilonova",
    "Neutron star merger",
    "Gravitational-wave observatory",
    "LISA (spacecraft)",
    "Einstein Telescope",
    "Nuclear pasta",
    "Strange star",
    "Quark star",
    "Fuzzball (physics)",
    "Firewall (physics)",
    "Soft hair (physics)",
    "Black hole complementarity",
    "Trans-Planckian problem",
    "Information loss paradox",
    # More quantum / particles
    "Quantum gravity",
    "Asymptotic safety",
    "Causal dynamical triangulation",
    "Spin network",
    "Spin foam",
    "Wheeler–DeWitt equation",
    "Path integral formulation",
    "Propagator",
    "Feynman diagram",
    "Virtual particle",
    "Off-shell (physics)",
    "LSZ reduction formula",
    "S-matrix",
    "Bootstrap model",
    "Dual resonance model",
    "AdS/CFT correspondence",
    "Holographic entanglement entropy",
    "Ryu–Takayanagi conjecture",
    "ER=EPR",
    "Quantum graphity",
    "Induced gravity",
    "Brane cosmology",
    "Ekpyrotic universe",
    "Cyclic model",
    "Conformal cyclic cosmology",
    "Penrose diagrams",
    "Conformal field theory",
    "Conformal anomaly",
    "Trace anomaly",
    "Casimir effect",
    "Dynamical Casimir effect",
    "Scharnhorst effect",
    "Schwinger effect",
    "Unruh effect",
    "Hawking temperature",
    "Bekenstein bound",
    "Black hole entropy",
    "Ryu-Takayanagi formula",
    "Island formula",
    "Page curve",
    "Quantum extremal surface",
    "Firewall (physics)",
    # Condensed matter & AMO
    "Topological order",
    "Fractional quantum Hall effect",
    "Quantum spin liquid",
    "Spin ice",
    "Magnetic monopole",
    "Dirac string",
    "Aharonov–Casher effect",
    "Geometric phase",
    "Berry phase",
    "Quantum Hall effect",
    "Quantum anomalous Hall effect",
    "Majorana fermion",
    "Majorana zero mode",
    "Kitaev chain",
    "Topological quantum computer",
    "Surface code",
    "Toroidal fusion reactor",
    "Stellarator",
    "Magnetic confinement fusion",
    "Inertial confinement fusion",
    "National Ignition Facility",
    "Fusion power",
    # Earth / planetary / life
    "Gaia hypothesis",
    "Snowball Earth",
    "Paleocene–Eocene Thermal Maximum",
    "Younger Dryas",
    "8.2 kiloyear event",
    "4.2 kiloyear event",
    "Bronze Age collapse",
    "Toba catastrophe theory",
    "Human mitochondrial genetics",
    "Y-chromosome Adam",
    "Recent African origin of modern humans",
    "Multiregional origin of modern humans",
    "Domestication",
    "Neolithic Revolution",
    "Agricultural Revolution",
    "Green Revolution",
    "Blue Brain Project",
    "Human Brain Project",
    "Connectome",
    "Optogenetics",
    "Brain–computer interface",
    "Memory consolidation",
    "Default mode network",
    "Out-of-body experience",
    "Autoscopic hallucination",
    "Hypnagogia",
    "Sleep paralysis",
    "Exploding head syndrome",
    "Tetris effect",
    "Frequency illusion",
    "Baader–Meinhof phenomenon",
    "Déjà vu",
    "Jamais vu",
    "Capgras delusion",
    "Cotard delusion",
    "Alien hand syndrome",
    "Hemispatial neglect",
    "Blindsight",
    "Akinetopsia",
    "Prosopagnosia",
    "Synaptic pruning",
    "Neurogenesis",
    "Adult neurogenesis",
    "Gut–brain axis",
    "Microbiome",
    "Horizontal gene transfer",
    "Transposable element",
    "Endogenous retrovirus",
    "Viral evolution",
    "Lytic cycle",
    "Lysogenic cycle",
    "Bacteriophage",
    "Mimivirus",
    "Virophage",
    "LUCA",
    "Last universal common ancestor",
    "RNA world",
    "Iron–sulfur world hypothesis",
    "Deep sea hydrothermal vent",
    "Black smoker",
    "Chemosynthesis",
    "Chemolithotrophy",
    "Extremophile",
    "Tardigrade",
    "Cryptobiosis",
    "Anhydrobiosis",
    "Lazarus taxon",
    "Living fossil",
    "Coelacanth",
    "Ginkgo biloba",
    "Horseshoe crab",
    "Nautilus",
    "Cambrian explosion",
    "Burgess Shale",
    "Ediacaran biota",
    "Avalon explosion",
    "Great Ordovician Biodiversification Event",
    "Carboniferous rainforest collapse",
    "Permian–Triassic extinction event",
    "Triassic–Jurassic extinction event",
    "Cretaceous–Paleogene extinction event",
    "Holocene extinction",
    "Sixth extinction",
    "Island gigantism",
    "Island dwarfism",
    "Foster's rule",
    "Insular dwarfism",
    "Evolutionary mismatch",
    "Red Queen hypothesis",
    "Müllerian mimicry",
    "Batesian mimicry",
    "Aposematism",
    "Mimicry",
    "Convergent evolution",
    "Parallel evolution",
    "Coevolution",
    "Symbiogenesis",
    "Lynn Margulis",
    "Mitochondrion",
    "Chloroplast",
    "Secondary endosymbiosis",
    "Plastid",
    "Hydrogen hypothesis",
    "Syntrophy",
    "Wood–Ljungdahl pathway",
    "Citric acid cycle",
    "ATP synthase",
    "Sodium–potassium pump",
    "Action potential",
    "Long-term potentiation",
    "Hebbian theory",
    "Mirror neuron",
    "Theory of mind",
    "Sapience",
    "Sentience",
    "Animal consciousness",
    "Cambridge Declaration on Consciousness",
    "Chinese room",
    "Turing test",
    "AI safety",
    "Alignment problem",
    "Instrumental convergence",
    "Orthogonality thesis",
    "Paperclip maximizer",
    "Roko's basilisk",
    "AI control problem",
    "Friendly artificial intelligence",
    "Superintelligence",
    "Artificial general intelligence",
    "Neural network (machine learning)",
    "Deep learning",
    "Transformer (deep learning architecture)",
    "Large language model",
    "Hallucination (artificial intelligence)",
    "Black box (systems)",
    # Chemistry / materials / lab
    "Fullerene chemistry",
    "Carbon nanotube",
    "Metal–insulator transition",
    "Mott insulator",
    "Strange metal",
    "Quantum spin liquid",
    "Spin ice",
    "Spin glass",
    "Frustration (physics)",
    "Quasicrystal",
    "Time crystal (physics)",
    "Metamaterial",
    "Negative index metamaterials",
    "Cloak of invisibility",
    "Acoustic metamaterial",
    "Sonic black hole",
    "Acoustic droplet ejection",
    "Sonochemistry",
    "Cold fusion",
    "Muon-catalyzed fusion",
    "Bubble fusion",
    "Low-energy nuclear reaction",
    "Polywater",
    "Cold fusion",
    "Philosopher's stone",
    "Alchemy",
    "Phlogiston theory",
    "Caloric theory",
    "Luminiferous aether",
    "N-ray",
    "Polywater",
    "Pathological science",
    "Cargo cult science",
    "Scientific misconduct",
    "Replication crisis",
    "P-hacking",
    "HARKing",
    "File drawer problem",
    "Publication bias",
    # Math & computing
    "Busy beaver",
    "Rice's theorem",
    "Gödel numbering",
    "Diagonal lemma",
    "Turing machine",
    "Church–Turing thesis",
    "Hypercomputation",
    "Oracle machine",
    "Quantum Turing machine",
    "Quantum supremacy",
    "Shor's algorithm",
    "Grover's algorithm",
    "Quantum error correction",
    "Surface code",
    "Topological quantum computing",
    "Adiabatic quantum computation",
    "Quantum annealing",
    "D-Wave Systems",
    "Quantum volume",
    "No-cloning theorem",
    "No-communication theorem",
    "Quantum money",
    "BB84",
    "E91 protocol",
    "Post-quantum cryptography",
    "Lattice-based cryptography",
    "One-way function",
    "Trapdoor function",
    "Integer factorization",
    "Discrete logarithm",
    "Elliptic-curve cryptography",
    "RSA (cryptosystem)",
    "Diffie–Hellman key exchange",
    "Zero-knowledge proof",
    "zk-SNARK",
    "Homomorphic encryption",
    "Byzantine fault",
    "CAP theorem",
    "Halting problem",
    "Busy beaver",
    "Chaitin's constant",
    "Kolmogorov complexity",
    "Algorithmic information theory",
    "Penrose tiling",
    "Penrose stairs",
    "Impossible object",
    "Necker cube",
    "Ames room",
    "Forced perspective",
    "Moon illusion",
    "Mach bands",
    "Checker shadow illusion",
    "Rubin's vase",
    "Necker cube",
    # History of science / experiments
    "Michelson–Morley experiment",
    "Oil drop experiment",
    "Double-slit experiment",
    "Davisson–Germer experiment",
    "Stern–Gerlach experiment",
    "Franck–Hertz experiment",
    "Photoelectric effect",
    "Compton scattering",
    "Lamb–Retherford experiment",
    "Wu experiment",
    "Bell test experiments",
    "Aspect experiment",
    "Delayed-choice quantum eraser",
    "Gravity Probe B",
    "Gravity Probe A",
    "Cavendish experiment",
    "Eötvös experiment",
    "Foucault pendulum",
    "Pound–Rebka experiment",
    "Hafele–Keating experiment",
    "Lunar laser ranging experiment",
    "Apollo 15 seismic experiment",
    "Voyager Golden Record",
    "Arecibo message",
    "Wow! signal",
    "Fast radio burst",
    "Tabby's Star",
    "KIC 8462852",
    "Przybylski's Star",
    "HD 140283",
    "Methuselah star",
    "Hypervelocity star",
    "Hypernova",
    "Kilonova",
    "Magnetar",
    "Soft gamma repeater",
    "Anomalous X-ray pulsar",
    "Rotating radio transient",
    "Fast radio burst",
    "Blitzar",
    "Black hole",
    "Intermediate-mass black hole",
    "Stellar black hole",
    "Primordial black hole",
    "Micro black hole",
    "Extremal black hole",
    "Kerr–Newman metric",
    "Reissner–Nordström metric",
    "No-hair theorem",
    "Cosmic censorship hypothesis",
    "Naked singularity",
    "Tipler cylinder",
    "Van Stockum dust",
    "Gödel metric",
    "Alcubierre drive",
    "Wormhole",
    "Einstein–Rosen bridge",
    "Morris–Thorne wormhole",
    "Traversable wormhole",
    "Exotic matter",
    "Negative mass",
    "Casimir effect",
    "Quantum fluctuation",
    "Vacuum energy",
    "Cosmological constant problem",
    "Cosmological constant",
    "Quintessence (physics)",
    "Phantom energy",
    "Big Rip",
    "Heat death of the universe",
    "Big Crunch",
    "Conformal cyclic cosmology",
    "Penrose–Hawking singularity theorems",
    "Hawking radiation",
    "Black hole thermodynamics",
    "Bekenstein–Hawking formula",
    "Firewall (physics)",
    "Black hole complementarity",
    "AMPS paradox",
    "ER=EPR",
    "Complexity equals action",
    "Holographic principle",
    "AdS/CFT correspondence",
    "Gauge/gravity duality",
    "Maldacena duality",
    "Randall–Sundrum model",
    "Large extra dimensions",
    "Kaluza–Klein theory",
    "String theory landscape",
    "Swampland (physics)",
    "Weak gravity conjecture",
    "Cosmic inflation",
    "Eternal inflation",
    "Bubble universe",
    "Multiverse",
    "Many-worlds interpretation",
    "Pilot-wave theory",
    "Objective-collapse theory",
    "GRW theory",
    "Penrose interpretation",
    "Orchestrated objective reduction",
    "Integrated information theory",
    "Global workspace theory",
    "Attention schema theory",
    "Predictive coding",
    "Bayesian brain",
    "Free energy principle",
    "Active inference",
    "Markov blanket",
    "Autopoiesis",
    "Maturana",
    "Varela",
    "Artificial life",
    "Digital organism",
    "Tierra (computer simulation)",
    "Avida (software)",
    "Conway's Game of Life",
    "Langton's ant",
    "Rule 110",
    "Turing completeness",
    "Lambda calculus",
    "Combinatory logic",
    "Curry–Howard correspondence",
    "Homotopy type theory",
    "Univalent foundations",
    "Continuum hypothesis",
    "Forcing (mathematics)",
    "Large cardinal",
    "Inaccessible cardinal",
    "Measurable cardinal",
    "Axiom of choice",
    "Banach–Tarski paradox",
    "Well-ordering theorem",
    "Zorn's lemma",
    "Schröder–Bernstein theorem",
    "Cantor's diagonal argument",
    "Cantor's theorem",
    "Power set",
    "Cardinality of the continuum",
    "Aleph number",
    "Beth number",
    "Ordinal number",
    "Transfinite induction",
    "Goodstein's theorem",
    "Paris–Harrington theorem",
    "Gödel's incompleteness theorems",
    "Second incompleteness theorem",
    "Löb's theorem",
    "Tarski's undefinability theorem",
    "Richard's paradox",
    "Berry paradox",
    "Grelling–Nelson paradox",
    "Liar paradox",
    "Yablo's paradox",
    "Curry's paradox",
    "Lottery paradox",
    "Preface paradox",
    "Raven paradox",
    "Grue and bleen",
    "New riddle of induction",
    "Problem of induction",
    "Demarcation problem",
    "Pseudoscience",
    "Scientific method",
    "Falsifiability",
    "Duhem–Quine thesis",
    "Underdetermination",
    "Theory-ladenness",
    "Pessimistic induction",
    "No miracles argument",
    "Structural realism",
    "Instrumentalism",
    "Scientific realism",
    "Entity realism",
    "Constructive empiricism",
    "Verificationism",
    "Operationalism",
    "Copenhagen interpretation",
    "QBism",
    "Relational quantum mechanics",
    "Relational interpretation",
    "Transactional interpretation",
    "Stochastic electrodynamics",
    "Zero-point energy",
    "Stochastic electrodynamics",
    "Vacuum catastrophe",
    "Cosmological constant problem",
    "Hierarchy problem",
    "Strong CP problem",
    "Muon g-2",
    "Proton radius puzzle",
    "Neutrino mass",
    "Neutrino oscillation",
    "Neutrino detector",
    "IceCube Neutrino Observatory",
    "Super-Kamiokande",
    "Sudbury Neutrino Observatory",
    "Daya Bay Reactor Neutrino Experiment",
    "Double Chooz",
    "STEREO experiment",
    "KATRIN",
    "Project 8",
    "KamLAND",
    "Borexino",
    "Gallex",
    "SAGE (experiment)",
    "Homestake experiment",
    "Cowan–Reines neutrino experiment",
    "Neutrino",
    "Sterile neutrino",
)


def _orl_fetch_extract_paragraphs(lang: str, title: str) -> tuple[str, list[str]]:
    try:
        data = _orl_wiki_get(
            lang,
            {
                "action": "query",
                "titles": title,
                "redirects": 1,
                "prop": "extracts",
                "explaintext": 1,
                "exsectionformat": "plain",
            },
        )
    except requests.RequestException as e:
        raise RuntimeError(f"ORL: Wikipedia extract failed: {e}") from e
    pages = data.get("query", {}).get("pages", {})
    for _pid, page in pages.items():
        if page.get("missing"):
            return "", []
        display = str(page.get("title") or title).strip()
        extract = str(page.get("extract") or "").strip()
        paras = [p.strip() for p in extract.split("\n") if p.strip()]
        return display, paras
    return "", []


def _orl_fetch_image_filenames(lang: str, title: str) -> list[str]:
    try:
        data = _orl_wiki_get(lang, {"action": "parse", "page": title, "prop": "images"})
    except requests.RequestException:
        return []
    imgs = (data.get("parse") or {}).get("images") or []
    skip_frag = (
        "icon",
        "logo",
        "flag",
        "edit-",
        "commons-logo",
        "symbol",
        "pictogram",
        "wikidata",
        "button",
    )
    good: list[str] = []
    for name in imgs:
        low = name.lower()
        if low.endswith((".svg", ".gif")):
            continue
        if any(s in low for s in skip_frag):
            continue
        good.append(name)
    return good


def _orl_resolve_image_urls(lang: str, filenames: list[str], limit: int = 8) -> list[str]:
    urls: list[str] = []
    chunk: list[str] = []
    for fn in filenames:
        if len(urls) >= limit:
            break
        ft = fn if fn.lower().startswith("file:") else f"File:{fn}"
        chunk.append(ft)
        if len(chunk) < 6:
            continue
        pipe = "|".join(chunk)
        chunk.clear()
        try:
            data = _orl_wiki_get(
                lang,
                {
                    "action": "query",
                    "titles": pipe,
                    "prop": "imageinfo",
                    "iiprop": "url|mime",
                    "redirects": 1,
                },
            )
        except requests.RequestException:
            continue
        for _pid, page in (data.get("query", {}).get("pages") or {}).items():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            mime = str(info.get("mime") or "").lower()
            if "svg" in mime or "gif" in mime:
                continue
            u = info.get("url")
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
            if len(urls) >= limit:
                break
    if chunk and len(urls) < limit:
        try:
            data = _orl_wiki_get(
                lang,
                {
                    "action": "query",
                    "titles": "|".join(chunk),
                    "prop": "imageinfo",
                    "iiprop": "url|mime",
                    "redirects": 1,
                },
            )
        except requests.RequestException:
            return urls[:limit]
        for _pid, page in (data.get("query", {}).get("pages") or {}).items():
            infos = page.get("imageinfo") or []
            if not infos:
                continue
            info = infos[0]
            mime = str(info.get("mime") or "").lower()
            if "svg" in mime or "gif" in mime:
                continue
            u = info.get("url")
            if isinstance(u, str) and u.startswith("http"):
                urls.append(u)
            if len(urls) >= limit:
                break
    return urls[:limit]


# When the English article has no usable raster images, try other-language editions (langlinks),
# preferring large wikis that often illustrate STEM articles.
_ORL_IMAGE_LANG_PRIORITY: tuple[str, ...] = (
    "de",
    "fr",
    "es",
    "it",
    "pt",
    "ru",
    "ja",
    "zh",
    "pl",
    "nl",
    "ar",
    "ko",
    "uk",
    "sv",
    "vi",
    "tr",
    "id",
    "cs",
    "fi",
    "hu",
    "he",
    "ro",
    "el",
    "da",
    "no",
    "sk",
    "bg",
    "hr",
    "sl",
    "sr",
    "ca",
    "hi",
    "th",
    "ms",
)


def _orl_fetch_langlinks(lang: str, title: str) -> list[tuple[str, str]]:
    """Return (wiki_lang_code, page_title) for sister-language articles."""
    try:
        data = _orl_wiki_get(
            lang,
            {
                "action": "query",
                "titles": title,
                "redirects": 1,
                "prop": "langlinks",
                "lllimit": "500",
            },
        )
    except requests.RequestException:
        return []
    out: list[tuple[str, str]] = []
    for _pid, page in (data.get("query", {}).get("pages") or {}).items():
        if page.get("missing"):
            continue
        for ll in page.get("langlinks") or []:
            lcode = str(ll.get("lang") or "").strip()
            t = str(ll.get("*") or "").strip()
            if lcode and t:
                out.append((lcode, t))
    return out


def _orl_sort_langlinks_for_images(links: list[tuple[str, str]]) -> list[tuple[str, str]]:
    pri = {code: i for i, code in enumerate(_ORL_IMAGE_LANG_PRIORITY)}

    def key(item: tuple[str, str]) -> tuple[int, str]:
        lg = item[0]
        return (pri[lg], lg) if lg in pri else (9000, lg)

    return sorted(links, key=key)


def _orl_collect_image_urls(lang: str, title: str, *, limit: int) -> list[str]:
    filenames = _orl_fetch_image_filenames(lang, title)
    return _orl_resolve_image_urls(lang, filenames, limit=limit)


def _orl_fill_image_urls_from_sister_wikis(
    primary_lang: str,
    primary_title: str,
    *,
    limit: int,
    max_wikis_to_try: int = 36,
) -> list[str]:
    urls = _orl_collect_image_urls(primary_lang, primary_title, limit=limit)
    if urls:
        return urls
    links = _orl_fetch_langlinks(primary_lang, primary_title)
    if not links:
        return []
    tried = 0
    for olang, otitle in _orl_sort_langlinks_for_images(links):
        if olang == primary_lang:
            continue
        tried += 1
        if tried > max_wikis_to_try:
            break
        try:
            urls = _orl_collect_image_urls(olang, otitle, limit=limit)
        except requests.RequestException:
            continue
        if urls:
            return urls
    return []


def fetch_orl_wikipedia_bundle() -> dict[str, Any]:
    """English Wikipedia only: random science topic; plain-text paragraphs and ordered article images."""
    en_title = random.choice(_ORL_SCIENCE_TOPICS_EN)
    lang = "en"
    display, paragraphs = _orl_fetch_extract_paragraphs(lang, en_title)
    if not paragraphs:
        raise RuntimeError(f"ORL: no English Wikipedia extract for {en_title!r}.")
    parse_title = display or en_title
    img_limit = max(8, _ORL_SLIDES_MAX)
    image_urls = _orl_fill_image_urls_from_sister_wikis(lang, parse_title, limit=img_limit)
    return {
        "lang": lang,
        "title": parse_title,
        "paragraphs": paragraphs,
        "image_urls": image_urls,
        "seed_en_title": en_title,
    }


def build_orl_narration_text(bundle: dict[str, Any]) -> str:
    """Hook line + lead (intro) paragraph + outro CTA."""
    adj = random.choice(("strangest", "scariest", "coolest"))
    noun = random.choice(("theory", "phenomenon"))
    hook = f"This is the {adj} {noun} in science."
    paras = list(bundle.get("paragraphs") or [])
    if not paras:
        raise RuntimeError("ORL: no usable paragraphs for narration.")
    para = paras[0]
    if len(para) > 1400:
        para = para[:1400].rsplit(" ", 1)[0] + "…"
    lines = [hook, para, random.choice(_VAR_ORL_CTAS)]
    return "\n\n".join(lines)


def _rsl_leverage_line(title: str, body: str) -> str:
    blob = f"{title}\n{body}".lower()
    mapping: tuple[tuple[tuple[str, ...], str], ...] = (
        (("money", "rent", "debt", "bill"), "Dark-psych check: controlling cash flow can be used as coercive leverage."),
        (("family", "parent", "in-laws"), "Dark-psych check: social approval can be weaponized as pressure."),
        (("lied", "secret", "trust"), "Dark-psych check: information asymmetry can manipulate decisions."),
        (("work", "boss", "coworker", "job"), "Dark-psych check: selective documentation can control the narrative."),
        (("roommate", "house", "chores"), "Dark-psych check: access to shared space can become silent leverage."),
        (("kid", "custody", "coparent"), "Dark-psych check: access to children can become negotiation fuel."),
        (("legal", "lawyer"), "Dark-psych check: procedural confusion can be exploited on purpose."),
        (("phone", "text", "dm"), "Dark-psych check: message flooding can reset your emotional baseline."),
    )
    raw = _pick_random_matching_line(blob, mapping + _BULK_STORY_INSIGHT_ROWS, _DEFAULT_RSL_LEVERAGE_FALLBACKS)
    if raw.startswith("Dark-psych check:"):
        return raw
    return f"Dark-psych check: {raw}"


def fetch_rsl_story_post() -> dict[str, Any]:
    """Pick one story for leverage-check shorts."""
    candidates = fetch_story_candidates()
    if not candidates:
        raise RuntimeError("No story candidates available for RSL.")
    ranked = _rank_stories_for_virality(candidates)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSL.")
    return pick_story_from_ranked(ranked)


def build_rsl_narration_text(post: dict[str, Any]) -> str:
    """Build a leverage-check script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 160)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 165)
    line = _rsl_leverage_line(title, body)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    lines = [
        random.choice(
            (
                "Dark psychology leverage check.",
                "Leverage and pressure patterns: quick scan.",
                "Power moves disguised as normal talk.",
            )
        ),
        random.choice(
            (
                f"Scenario: {first or title}",
                f"Case file: {first or title}",
            )
        ),
        line,
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(_VAR_RSL_CTAS),
    ]
    return "\n\n".join(lines)


def fetch_rsg_story_post() -> dict[str, Any]:
    """Pick one wholesome/uplifting story from positive subreddits."""
    target_subs = ("MadeMeSmile", "HumansBeingBros", "UpliftingNews", "wholesomememes")
    picked: list[dict[str, Any]] = []
    for sub in target_subs:
        data = _reddit_get(
            f"https://www.reddit.com/r/{sub}/hot.json",
            params={"limit": "60", "raw_json": "1"},
            use_alt_hosts=True,
        )
        for child in (data.get("data", {}) or {}).get("children", []) or []:
            c = child.get("data", {}) or {}
            if c.get("over_18"):
                continue
            title = str(c.get("title") or "").strip()
            body = str(c.get("selftext") or "").strip()
            if len(title) + len(body) < MIN_POST_CHARS:
                continue
            rec = {
                "id": c.get("id"),
                "title": title,
                "selftext": body,
                "score": c.get("score", 0),
                "num_comments": c.get("num_comments", 0),
                "subreddit": c.get("subreddit", sub),
            }
            if _story_looks_advertisement(rec):
                continue
            picked.append(rec)
        if len(picked) >= 20:
            break

    if not picked:
        # Fallback to general pool if targeted subs fail.
        candidates = fetch_story_candidates()
        if not candidates:
            raise RuntimeError("No story candidates available for RSG.")
        picked = candidates

    ranked = _rank_stories_for_virality(picked)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSG.")
    return pick_story_from_ranked(ranked)


def build_rsg_narration_text(post: dict[str, Any]) -> str:
    """Build a good-news spotlight script from one uplifting story."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    hero = _rss_trim_line(first or title or "A small moment turned into a big win.", 190)
    lines = [
        random.choice(
            (
                "Good-news spotlight.",
                "Quick lift: internet humanity edition.",
                "One wholesome win for your timeline.",
            )
        ),
        random.choice(
            (
                f"Story: {hero}",
                f"What happened: {hero}",
            )
        ),
        random.choice(
            (
                "Why it hits: small actions can compound into real change.",
                "Why it matters: this is what trust in public looks like.",
                "Takeaway: people copy kindness faster than we think.",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"Pulled from r slash {src_sub}.",
            )
        ),
        random.choice(
            (
                "Drop one good thing you saw today.",
                "Tag someone who needed this reset.",
                "Follow for more stories that restore your faith in people.",
            )
        ),
    ]
    return "\n\n".join(lines)


def fetch_rsd_story_post() -> dict[str, Any]:
    """Pick one drama/plot-twist story from conflict-heavy subreddits."""
    target_subs = ("BestofRedditorUpdates", "tifu", "pettyrevenge", "MaliciousCompliance")
    picked: list[dict[str, Any]] = []
    for sub in target_subs:
        data = _reddit_get(
            f"https://www.reddit.com/r/{sub}/hot.json",
            params={"limit": "70", "raw_json": "1"},
            use_alt_hosts=True,
        )
        for child in (data.get("data", {}) or {}).get("children", []) or []:
            c = child.get("data", {}) or {}
            if c.get("over_18"):
                continue
            title = str(c.get("title") or "").strip()
            body = str(c.get("selftext") or "").strip()
            if len(title) + len(body) < MIN_POST_CHARS:
                continue
            rec = {
                "id": c.get("id"),
                "title": title,
                "selftext": body,
                "score": c.get("score", 0),
                "num_comments": c.get("num_comments", 0),
                "subreddit": c.get("subreddit", sub),
            }
            if _story_looks_advertisement(rec):
                continue
            if _story_contains_nsfw_blocked_words(rec):
                continue
            picked.append(rec)
        if len(picked) >= 30:
            break

    if not picked:
        candidates = fetch_story_candidates()
        if not candidates:
            raise RuntimeError("No story candidates available for RSD.")
        picked = candidates

    ranked = _rank_stories_for_virality(picked)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSD.")
    return pick_story_from_ranked(ranked)


def build_rsd_narration_text(post: dict[str, Any]) -> str:
    """Build a plot-twist drama script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    premise = _rss_trim_line(first or title or "It started normal, then everything flipped.", 190)
    lines = [
        random.choice(
            (
                "Plot twist drama check.",
                "You think this is one story. Wait for the turn.",
                "Quick drama drop: setup, twist, fallout.",
            )
        ),
        random.choice(
            (
                f"Setup: {premise}",
                f"Initial scene: {premise}",
            )
        ),
        random.choice(
            (
                "Twist: the hidden context flips who looks right.",
                "Twist: one missing detail changes the whole read.",
                "Twist: the consequence nobody expected becomes the headline.",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(
            (
                "Was this justice, luck, or chaos?",
                "Whose side are you on after the twist?",
                "Drop your verdict in one sentence.",
            )
        ),
    ]
    return "\n\n".join(lines)


def fetch_rsi_story_post() -> dict[str, Any]:
    """Pick one curiosity/interesting story from discovery-heavy subreddits."""
    target_subs = ("todayilearned", "interestingasfuck", "Damnthatsinteresting", "mildlyinteresting")
    picked: list[dict[str, Any]] = []
    for sub in target_subs:
        data = _reddit_get(
            f"https://www.reddit.com/r/{sub}/hot.json",
            params={"limit": "70", "raw_json": "1"},
            use_alt_hosts=True,
        )
        for child in (data.get("data", {}) or {}).get("children", []) or []:
            c = child.get("data", {}) or {}
            if c.get("over_18"):
                continue
            title = str(c.get("title") or "").strip()
            body = str(c.get("selftext") or "").strip()
            if len(title) + len(body) < MIN_POST_CHARS:
                continue
            rec = {
                "id": c.get("id"),
                "title": title,
                "selftext": body,
                "score": c.get("score", 0),
                "num_comments": c.get("num_comments", 0),
                "subreddit": c.get("subreddit", sub),
            }
            if _story_looks_advertisement(rec):
                continue
            if _story_contains_nsfw_blocked_words(rec):
                continue
            picked.append(rec)
        if len(picked) >= 30:
            break

    if not picked:
        candidates = fetch_story_candidates()
        if not candidates:
            raise RuntimeError("No story candidates available for RSI.")
        picked = candidates

    ranked = _rank_stories_for_virality(picked)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSI.")
    return pick_story_from_ranked(ranked)


def build_rsi_narration_text(post: dict[str, Any]) -> str:
    """Build an interesting-find spotlight script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    core = _rss_trim_line(first or title or "A surprising fact with real-world impact.", 190)
    lines = [
        random.choice(
            (
                "Interesting find spotlight.",
                "Quick curiosity hit.",
                "One wild thing worth knowing today.",
            )
        ),
        random.choice(
            (
                f"What surfaced: {core}",
                f"Today’s find: {core}",
            )
        ),
        random.choice(
            (
                "Why it sticks: it sounds small but changes how you see the topic.",
                "Why it matters: this detail rewires the bigger picture.",
                "Context check: once you notice this, you see it everywhere.",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(
            (
                "Want a part two on this topic?",
                "Drop the weirdest fact you learned this week.",
                "Follow for daily high-signal curiosities.",
            )
        ),
    ]
    return "\n\n".join(lines)


def fetch_rsj_story_post() -> dict[str, Any]:
    """Pick one science/space wonder story."""
    target_subs = ("space", "science", "Physics", "Futurology")
    picked: list[dict[str, Any]] = []
    for sub in target_subs:
        data = _reddit_get(
            f"https://www.reddit.com/r/{sub}/hot.json",
            params={"limit": "70", "raw_json": "1"},
            use_alt_hosts=True,
        )
        for child in (data.get("data", {}) or {}).get("children", []) or []:
            c = child.get("data", {}) or {}
            if c.get("over_18"):
                continue
            title = str(c.get("title") or "").strip()
            body = str(c.get("selftext") or "").strip()
            if len(title) + len(body) < MIN_POST_CHARS:
                continue
            rec = {
                "id": c.get("id"),
                "title": title,
                "selftext": body,
                "score": c.get("score", 0),
                "num_comments": c.get("num_comments", 0),
                "subreddit": c.get("subreddit", sub),
            }
            if _story_looks_advertisement(rec):
                continue
            if _story_contains_nsfw_blocked_words(rec):
                continue
            picked.append(rec)
        if len(picked) >= 30:
            break

    if not picked:
        candidates = fetch_story_candidates()
        if not candidates:
            raise RuntimeError("No story candidates available for RSJ.")
        picked = candidates

    ranked = _rank_stories_for_virality(picked)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSJ.")
    return pick_story_from_ranked(ranked)


def build_rsj_narration_text(post: dict[str, Any]) -> str:
    """Build a science-wonder spotlight script from one story."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    core = _rss_trim_line(first or title or "A discovery that changes scale and perspective.", 190)
    lines = [
        random.choice(
            (
                "Science wonder spotlight.",
                "One discovery to bend your perspective.",
                "Quick cosmic/physics reality check.",
            )
        ),
        random.choice(
            (
                f"Headline: {core}",
                f"Today’s signal: {core}",
            )
        ),
        random.choice(
            (
                "Why it matters: this is the kind of result that rewrites assumptions.",
                "Scale check: this puts everyday intuition in a blender.",
                "Takeaway: the universe is stranger and more consistent than it looks.",
            )
        ),
        random.choice(
            (
                f"Source: r slash {src_sub}.",
                f"From r slash {src_sub}.",
            )
        ),
        random.choice(
            (
                "Want more science/space drops like this?",
                "Comment the next topic: space, AI, energy, or biology.",
                "Follow for daily high-signal science finds.",
            )
        ),
    ]
    return "\n\n".join(lines)


def fetch_rsk2_story_post() -> dict[str, Any]:
    """Pick one history/mystery style story."""
    target_subs = ("UnresolvedMysteries", "todayilearned", "AskHistorians", "history")
    picked: list[dict[str, Any]] = []
    for sub in target_subs:
        data = _reddit_get(
            f"https://www.reddit.com/r/{sub}/hot.json",
            params={"limit": "70", "raw_json": "1"},
            use_alt_hosts=True,
        )
        for child in (data.get("data", {}) or {}).get("children", []) or []:
            c = child.get("data", {}) or {}
            if c.get("over_18"):
                continue
            title = str(c.get("title") or "").strip()
            body = str(c.get("selftext") or "").strip()
            if len(title) + len(body) < MIN_POST_CHARS:
                continue
            rec = {
                "id": c.get("id"),
                "title": title,
                "selftext": body,
                "score": c.get("score", 0),
                "num_comments": c.get("num_comments", 0),
                "subreddit": c.get("subreddit", sub),
            }
            if _story_looks_advertisement(rec) or _story_contains_nsfw_blocked_words(rec):
                continue
            picked.append(rec)
        if len(picked) >= 30:
            break
    if not picked:
        fallback = fetch_story_candidates()
        if not fallback:
            raise RuntimeError("No story candidates available for RSK2.")
        picked = fallback
    ranked = _rank_stories_for_virality(picked)
    if not ranked:
        raise RuntimeError("No ranked candidates for RSK2.")
    return pick_story_from_ranked(ranked)


def build_rsk2_narration_text(post: dict[str, Any]) -> str:
    """Build a history-mystery spotlight script."""
    title = _rss_trim_line(str(post.get("title") or ""), 170)
    body = str(post.get("selftext") or "").strip()
    first = _rss_trim_line(_first_sentence(body), 190)
    src_sub = str(post.get("subreddit") or "").strip() or "reddit"
    core = _rss_trim_line(first or title or "A historical clue that still raises questions.", 190)
    lines = [
        random.choice(("History mystery spotlight.", "Cold case from the past.", "One unresolved historical puzzle.")),
        random.choice((f"Case file: {core}", f"What surfaced: {core}")),
        random.choice(
            (
                "Key tension: records exist, but certainty still doesn’t.",
                "Context gap: one missing piece changes the conclusion.",
                "Takeaway: evidence narrows the field, but mystery survives.",
            )
        ),
        random.choice((f"Source: r slash {src_sub}.", f"From r slash {src_sub}.")),
        random.choice(("Drop your theory in one sentence.", "Want a part two on this case?", "Follow for more history mysteries.")),
    ]
    return "\n\n".join(lines)


def _hook_keywords(text: str, *, max_items: int = 8) -> list[str]:
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]{3,}", text or "")]
    out: list[str] = []
    seen: set[str] = set()
    for w in words:
        if w in HOOK_STOPWORDS or w in seen:
            continue
        seen.add(w)
        out.append(w)
        if len(out) >= max_items:
            break
    return out


def _first_sentence(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip())
    if not raw:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", raw)
    return (parts[0] if parts else raw).strip()


def _clip_hook_text(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    if len(t) <= HOOK_MAX_CHARS:
        return t
    t = t[: HOOK_MAX_CHARS - 1].rstrip(" ,.;:-")
    return t + "…"


def _score_hook_candidate(
    hook: str,
    *,
    source_keywords: list[str],
    conflict: float,
    payoff: float,
    sentiment: float,
) -> float:
    h = hook.lower()
    if not h:
        return 0.0
    # Target compact hooks that still feel punchy.
    length = len(hook)
    length_fit = 1.0 - min(1.0, abs(length - 68) / 68.0)
    overlap = 0.0
    if source_keywords:
        overlap = sum(1.0 for k in source_keywords if k in h) / len(source_keywords)
    punch = sum(1.0 for w in HOOK_POWER_WORDS if w in h) / max(1, len(HOOK_POWER_WORDS))
    q_bonus = 0.15 if "?" in hook else 0.0
    excl_bonus = 0.08 if "!" in hook else 0.0
    intensity = (0.55 * conflict) + (0.25 * payoff) + (0.20 * sentiment)
    score = (0.34 * length_fit) + (0.24 * overlap) + (0.22 * punch) + (0.20 * intensity) + q_bonus + excl_bonus
    return _clamp01(score)


def _generate_story_hook(post: dict[str, Any]) -> str:
    title = str(post.get("title") or "").strip()
    body = str(post.get("selftext") or "").strip()
    blob = f"{title}\n{body}".strip()
    if not blob:
        return ""

    first = _first_sentence(body)
    keywords = _hook_keywords(f"{title} {first}", max_items=10)
    lead_kw = keywords[0] if keywords else "this"
    second_kw = keywords[1] if len(keywords) > 1 else "everything"
    conflict = _conflict_intensity_score(blob)
    payoff = _payoff_quality_score(blob)
    sentiment = _sentiment_score(blob)

    base_candidates = [
        f"POV: one decision about {lead_kw} changed everything.",
        f"You will not believe what happened after {lead_kw}.",
        f"This started with {lead_kw} and ended in total chaos.",
        f"I thought {lead_kw} was normal. I was completely wrong.",
        f"Wait for the twist at the end of this story.",
        f"Would you do the same thing in this situation?",
        f"Nobody warned me what {lead_kw} would trigger.",
        f"By the time {second_kw} happened, it was too late.",
        f"If {lead_kw} feels small, the fallout was not.",
        f"This is the kind of story that starts quiet and ends loud.",
        f"One moment about {lead_kw} flipped the whole situation.",
        f"Everyone had an opinion about {lead_kw}.",
        f"I kept rereading the part about {lead_kw} like it was a typo.",
        f"The comments section would lose it over {lead_kw}.",
        f"Tell me you see the problem with {lead_kw} without telling me.",
        f"This is not the update anyone expected after {lead_kw}.",
        f"Somewhere between {lead_kw} and {second_kw}, it became a mess.",
        f"I am still processing what {lead_kw} meant in context.",
        f"That detail about {lead_kw} is doing a lot of heavy lifting.",
        f"If you blink, you miss why {lead_kw} matters.",
        f"This is a masterclass in how {lead_kw} escalates.",
        f"Imagine explaining {lead_kw} to a stranger on a bus.",
        f"The second half pays off because of {lead_kw}.",
        f"I thought I understood {lead_kw}. I did not.",
        f"Plot twist: {lead_kw} was not the real issue.",
        f"Okay but why did nobody stop it at {lead_kw}?",
        f"This is messy, loud, and weirdly relatable because of {lead_kw}.",
        f"I need you to hear the part about {lead_kw} twice.",
        f"If you only remember one thing, remember {lead_kw}.",
        f"The story pivots the moment {lead_kw} shows up.",
        f"Some people will defend this. Others will rage about {lead_kw}.",
        f"I am not taking sides yet, but {lead_kw} is suspicious.",
        f"This is what happens when boundaries meet {lead_kw}.",
        f"That one sentence about {lead_kw} changes the vibe.",
        f"I am here for the drama and {lead_kw} delivered.",
        f"You can feel the tension spike around {lead_kw}.",
        f"If this was a movie, {lead_kw} would be the trailer beat.",
        f"Real life said: hold my {lead_kw}.",
        f"Not me getting invested in {lead_kw} at 2 a.m.",
        f"This is why group chats exist: {lead_kw}.",
        f"Brace yourself: {lead_kw} is worse in context.",
        f"I did not expect {second_kw} after {lead_kw}.",
        f"Small detail, huge consequences: {lead_kw}.",
        f"That escalated from {lead_kw} way too fast.",
        f"If you respect yourself, watch for {lead_kw} in your life.",
        f"This is a cautionary tale powered by {lead_kw}.",
        f"Some lessons cost more than {lead_kw} looks.",
        f"I would have walked away at {lead_kw}. Would you?",
        f"The internet would be split on {lead_kw} alone.",
        f"Every update makes {lead_kw} hit different.",
        f"This is uncomfortable, honest, and centered on {lead_kw}.",
    ]
    if first:
        base_candidates.extend(
            [
                f"It began like this: {first}",
                f"This was the first red flag: {first}",
            ]
        )
    if conflict >= 0.45:
        base_candidates.extend(
            [
                "This family argument spiraled way further than expected.",
                "One confrontation turned into full damage control.",
                "This is what happens when everyone pushes too far.",
            ]
        )
    if payoff >= 0.35:
        base_candidates.extend(
            [
                "The ending makes this entire story worth hearing.",
                "The final reveal changes how this all looks.",
            ]
        )

    scored: list[tuple[float, str]] = []
    for cand in base_candidates:
        c = _clip_hook_text(cand)
        if not c:
            continue
        s = _score_hook_candidate(
            c,
            source_keywords=keywords,
            conflict=conflict,
            payoff=payoff,
            sentiment=sentiment,
        )
        scored.append((s, c))
    if not scored:
        return ""
    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[: min(5, len(scored))]
    weights = [max(0.01, s) for s, _ in top]
    pick = random.choices(top, weights=weights, k=1)[0][1]
    return pick


def looks_sponsored_asmr(post: dict[str, Any]) -> bool:
    t = (post.get("title") or "").lower()
    if "sponsored" in t or "advertisement" in t or "paid partnership" in t:
        return True
    link_flair = (post.get("link_flair_text") or "").lower()
    if "sponsored" in link_flair or "ad" == link_flair:
        return True
    return False


def _is_reddit_hosted_video(c: dict[str, Any]) -> bool:
    if c.get("is_video"):
        return True
    u = c.get("url") or ""
    if "v.redd.it" in u or "reddit.com/video" in u:
        return True
    if c.get("post_hint") in ("hosted:video", "rich:video"):
        return True
    sm = c.get("secure_media") or {}
    m = c.get("media") or {}
    return bool((sm.get("reddit_video") or m.get("reddit_video")))


def _reddit_video_fallback_url(c: dict[str, Any]) -> str:
    sm = c.get("secure_media") or {}
    m = c.get("media") or {}
    rv = (sm.get("reddit_video") or m.get("reddit_video") or {})
    u = str(rv.get("fallback_url") or "").strip()
    return u


def fetch_asmr_video_posts() -> list[dict[str, Any]]:
    """Video posts from ``BROLL_SUBREDDITS`` with direct Reddit media URLs."""
    found: list[dict[str, Any]] = []
    seen_permalinks: set[str] = set()
    subs = list(BROLL_SUBREDDITS)
    random.shuffle(subs)
    if len(subs) > BROLL_MAX_SUBS_PER_RUN:
        subs = subs[:BROLL_MAX_SUBS_PER_RUN]
    t0 = time.time()

    for sub in subs:
        if len(found) >= BROLL_TARGET_POSTS:
            break
        if time.time() - t0 > BROLL_FETCH_BUDGET_SEC:
            break
        after: str | None = None
        for _ in range(BROLL_PAGES_PER_SUB):
            if len(found) >= BROLL_TARGET_POSTS:
                break
            if time.time() - t0 > BROLL_FETCH_BUDGET_SEC:
                break
            url = f"https://www.reddit.com/r/{sub}/hot.json"
            try:
                data = _reddit_get(
                    url,
                    {"limit": 100, "raw_json": 1, **({"after": after} if after else {})},
                    max_attempts=2,
                    per_request_timeout=(6, 12),
                    use_alt_hosts=False,
                )
            except requests.RequestException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (403, 404, 429, 451):
                    break
                raise
            for child in data.get("data", {}).get("children") or []:
                c = child.get("data") or {}
                if not _is_reddit_hosted_video(c):
                    continue
                if looks_sponsored_asmr(c):
                    continue
                permalink = c.get("permalink")
                if not permalink or permalink in seen_permalinks:
                    continue
                media_url = _reddit_video_fallback_url(c)
                if not media_url:
                    continue
                seen_permalinks.add(permalink)
                found.append(
                    {
                        "title": c.get("title"),
                        "permalink": permalink,
                        "url": "https://www.reddit.com" + permalink,
                        "media_url": media_url,
                    }
                )
            after = data.get("data", {}).get("after")
            if not after:
                break
    return found


def _which(cmd: str) -> str | None:
    p = shutil.which(cmd)
    return p


def _run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def _ffmpeg_strip_metadata_args_if_video(out_arg: str) -> list[str]:
    """Strip global metadata / chapters on common video containers (no title, encoder churn, etc.)."""
    tail = out_arg.replace("\\", "/").rsplit("/", maxsplit=1)[-1].lower()
    if tail.endswith((".mp4", ".mov", ".mkv", ".webm", ".m4v")):
        return ["-map_metadata", "-1", "-map_chapters", "-1"]
    return []


_WIN_FONTCONFIG_PATH: Path | None = None
_WIN_FONTCONFIG_TRIED = False


def _ensure_windows_fontconfig_file() -> Path | None:
    """
    Minimal fontconfig so libass/drawtext on Windows do not log ``Fontconfig error: ... (null)``.
    Points at ``%WINDIR%\\Fonts``. Other OSes use normal fontconfig.
    """
    global _WIN_FONTCONFIG_PATH, _WIN_FONTCONFIG_TRIED
    if sys.platform != "win32":
        return None
    if _WIN_FONTCONFIG_TRIED:
        return _WIN_FONTCONFIG_PATH
    _WIN_FONTCONFIG_TRIED = True
    windir = os.environ.get("WINDIR", "C:\\Windows")
    fonts = Path(windir) / "Fonts"
    if not fonts.is_dir():
        return None
    p = Path(tempfile.gettempdir()) / "firstsky_fontconfig.conf"
    try:
        d = fonts.resolve().as_posix()
        p.write_text(
            "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
            "<!DOCTYPE fontconfig SYSTEM \"fonts.dtd\">\n"
            f"<fontconfig><dir>{d}</dir></fontconfig>\n",
            encoding="utf-8",
        )
        _WIN_FONTCONFIG_PATH = p
        return p
    except OSError:
        return None


def _ffmpeg_subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    if sys.platform == "win32":
        fc = _ensure_windows_fontconfig_file()
        if fc is not None:
            env["FONTCONFIG_FILE"] = str(fc.resolve())
    return env


def _watermark_fontfile_for_drawtext() -> str | None:
    """TTF path for ``drawtext`` so ffmpeg does not rely on a broken fontconfig (Windows)."""
    raw = (os.environ.get("PTK_WATERMARK_FONT") or "").strip()
    candidates: list[Path] = []
    if raw:
        candidates.append(Path(raw))
    if sys.platform == "win32":
        fonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        for name in ("segoeui.ttf", "segoeuib.ttf", "arial.ttf", "calibri.ttf", "impact.ttf"):
            candidates.append(fonts / name)
    elif sys.platform == "darwin":
        candidates.extend(
            (
                Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
                Path("/Library/Fonts/Arial.ttf"),
            )
        )
    else:
        for rel in (
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            "/usr/share/fonts/TTF/DejaVuSans.ttf",
        ):
            candidates.append(Path(rel))
    for p in candidates:
        try:
            if p.is_file():
                s = str(p.resolve()).replace("\\", "/")
                if sys.platform == "win32" and len(s) >= 2 and s[1] == ":":
                    s = s[0] + "\\:" + s[2:]
                return s
        except OSError:
            continue
    return None


def _run_ffmpeg(cmd: list[str], *, timeout: float | None = None) -> None:
    """
    Run ffmpeg without capturing stdout/stderr. With capture_output, ffmpeg's progress
    on stderr fills the OS pipe (~64 KiB) and the process blocks forever.
    """
    exe = cmd[0]
    rest = list(cmd[1:])
    if rest and isinstance(rest[-1], str):
        last = rest[-1]
        if not last.startswith("-") and last not in ("-", "pipe:", "pipe:1") and "-map_metadata" not in rest:
            strip = _ffmpeg_strip_metadata_args_if_video(last)
            if strip:
                rest = rest[:-1] + strip + [last]
    argv = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", *rest]
    subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        timeout=timeout,
        env=_ffmpeg_subprocess_env(),
    )


def _run_ffmpeg_with_percent(
    cmd: list[str],
    *,
    total_seconds: float,
    label: str,
    timeout: float | None = None,
) -> None:
    """
    Run ffmpeg and render a live percentage bar from ffmpeg ``-progress`` data.
    Percentage uses ``out_time_ms`` / target duration so it reflects encoded output.
    """
    exe = cmd[0]
    rest = list(cmd[1:])
    if rest and isinstance(rest[-1], str):
        last = rest[-1]
        if not last.startswith("-") and last not in ("-", "pipe:", "pipe:1") and "-map_metadata" not in rest:
            strip = _ffmpeg_strip_metadata_args_if_video(last)
            if strip:
                rest = rest[:-1] + strip + [last]
    argv = [exe, "-nostdin", "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1", *rest]
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=None,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_ffmpeg_subprocess_env(),
    )
    assert proc.stdout is not None
    t0 = time.time()
    tgt = max(0.001, float(total_seconds))
    last_draw = 0.0
    out_sec = 0.0

    def draw(force: bool = False) -> None:
        nonlocal last_draw
        now = time.time()
        if not force and (now - last_draw) < 0.15:
            return
        last_draw = now
        frac = max(0.0, min(1.0, out_sec / tgt))
        pct = frac * 100.0
        cols = _terminal_columns()
        bar_w = max(16, min(38, cols - 38))
        fill = int(round(bar_w * frac))
        bar = "=" * fill + "-" * max(0, bar_w - fill)
        em, es = divmod(max(0, int(out_sec)), 60)
        tm, ts = divmod(max(0, int(tgt)), 60)
        msg = f"{label} {pct:6.2f}% [{bar}] {em:02d}:{es:02d}/{tm:02d}:{ts:02d}"
        vis = msg if len(msg) <= cols - 1 else msg[: cols - 2] + "."
        sys.stdout.write("\r" + vis + " " * max(0, cols - 1 - len(vis)))
        sys.stdout.flush()

    try:
        while True:
            if timeout is not None and (time.time() - t0) > timeout:
                proc.kill()
                raise subprocess.TimeoutExpired(argv, timeout)
            ln = proc.stdout.readline()
            if ln == "":
                if proc.poll() is not None:
                    break
                time.sleep(0.02)
                continue
            row = ln.strip()
            if not row or "=" not in row:
                continue
            k, v = row.split("=", 1)
            if k == "out_time_ms":
                try:
                    out_sec = max(out_sec, float(v) / 1_000_000.0)
                except ValueError:
                    pass
                draw()
            elif k == "out_time_us":
                try:
                    out_sec = max(out_sec, float(v) / 1_000_000.0)
                except ValueError:
                    pass
                draw()
            elif k == "progress" and v == "end":
                out_sec = tgt
                draw(force=True)
        rc = proc.wait(timeout=5)
        if rc != 0:
            raise subprocess.CalledProcessError(rc, argv)
        out_sec = tgt
        draw(force=True)
        sys.stdout.write("\n")
        sys.stdout.flush()
    finally:
        if proc.poll() is None:
            proc.kill()


def ffprobe_duration(path: Path) -> float:
    p = path.resolve()
    key = str(p)
    try:
        mtime_ns = int(p.stat().st_mtime_ns)
    except OSError:
        mtime_ns = -1
    hit = _FFPROBE_DURATION_CACHE.get(key)
    if hit is not None and hit[0] == mtime_ns:
        return hit[1]
    out = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )
    d = float(out.stdout.strip())
    if mtime_ns >= 0:
        _FFPROBE_DURATION_CACHE[key] = (mtime_ns, d)
    return d


def ffprobe_video_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) of the first video stream; vertical-default if unknown."""
    try:
        out = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0:s=x",
                str(path),
            ]
        )
        line = out.stdout.strip()
        if "x" in line:
            w, h = line.split("x", 1)
            return max(1, int(w)), max(1, int(h))
    except Exception:
        pass
    return 1080, 1920


def _libx264_encode_args(*, crf: str, with_fps_r: bool = False) -> list[str]:
    args = ["-c:v", "libx264", "-preset", X264_PRESET, "-crf", crf]
    if FAST_RENDER_MODE:
        args.extend(["-tune", "zerolatency"])
    if with_fps_r:
        args.extend(["-r", str(OUTPUT_FPS)])
    return args


def _ffmpeg_encoder_usable(name: str) -> bool:
    cached = _FFMPEG_ENCODER_AVAILABLE.get(name)
    if cached is not None:
        return cached
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-h", f"encoder={name}"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        blob = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and "Encoder " in blob
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired):
        ok = False
    _FFMPEG_ENCODER_AVAILABLE[name] = ok
    return ok


def _hw_encode_args_for_subs_mux() -> list[str] | None:
    """Optional GPU encode for subtitle burn mux (``PTK_HWENC=1``). Falls back to libx264 if unavailable."""
    if os.environ.get("PTK_HWENC", "").strip().lower() not in ("1", "true", "yes", "on"):
        return None
    cq = "28" if (FAST_RENDER_MODE and AGGRESSIVE_MODE) else ("26" if FAST_RENDER_MODE else "23")
    if _ffmpeg_encoder_usable("h264_nvenc"):
        return ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", cq, "-bf", "0"]
    if _ffmpeg_encoder_usable("h264_amf"):
        return ["-c:v", "h264_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", cq, "-qp_p", cq]
    if _ffmpeg_encoder_usable("h264_qsv"):
        return ["-c:v", "h264_qsv", "-preset", "veryfast", "-global_quality", cq]
    return None


def _list_local_music_tracks() -> list[Path]:
    global _LOCAL_MUSIC_SCAN_AT, _LOCAL_MUSIC_PATHS
    now = time.time()
    if _LOCAL_MUSIC_PATHS and (now - _LOCAL_MUSIC_SCAN_AT) < _LOCAL_MUSIC_TTL_SEC:
        return _LOCAL_MUSIC_PATHS
    source_dirs = [MUSIC]
    if LEGACY_MUSIC.is_dir():
        source_dirs.append(LEGACY_MUSIC)
    exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
    tracks: list[Path] = []
    for root in source_dirs:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in exts:
                tracks.append(p)
    _LOCAL_MUSIC_PATHS = tracks
    _LOCAL_MUSIC_SCAN_AT = now
    return tracks


_RE_SRT_TIME_LINE = re.compile(
    r"^(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)


def _srt_ts_to_ass(ts: str) -> str:
    """SRT ``HH:MM:SS,mmm`` → ASS ``H:MM:SS.cc`` (centiseconds)."""
    parts = ts.strip().split(":")
    if len(parts) != 3:
        return "0:00:00.00"
    h, m, s_ms = parts
    if "," not in s_ms:
        return "0:00:00.00"
    s, ms = s_ms.split(",", 1)
    total_cs = (
        int(h) * 360000
        + int(m) * 6000
        + int(s) * 100
        + max(0, min(99, (int(ms) + 5) // 10))
    )
    H = total_cs // 360000
    rem = total_cs % 360000
    M = rem // 6000
    rem %= 6000
    S = rem // 100
    Cs = rem % 100
    return f"{H}:{M:02d}:{S:02d}.{Cs:02d}"


def _iter_srt_cues(content: str) -> Iterator[tuple[str, str, str]]:
    """Yield ``(t0_srt, t1_srt, text)`` per cue; text uses ASS ``\\N`` for line breaks."""
    raw = content.lstrip("\ufeff").strip()
    if not raw:
        return
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.split("\n") if ln.strip() != ""]
        if len(lines) < 2:
            continue
        i = 0
        if re.match(r"^\d+$", lines[0].strip()):
            i = 1
        if i >= len(lines):
            continue
        m = _RE_SRT_TIME_LINE.match(lines[i].strip())
        if not m:
            continue
        t0, t1 = m.group(1), m.group(2)
        body = lines[i + 1 :]
        text = r"\N".join(body) if body else ""
        yield t0, t1, text


def _write_burn_ass_from_srt(srt: Path, play_w: int, play_h: int, out_ass: Path) -> int:
    """
    Wrap SRT cues in a proper ASS script with ``PlayRes`` matching the video.

    Libass often mis-centers SRT + ``force_style`` (colour tokens contain ``=`` / ``&``);
    embedding styles + ``Alignment=5`` here keeps subtitles dead center.

    Returns the number of dialogue lines written.
    """
    text = srt.read_text(encoding="utf-8", errors="replace")
    lines_ev: list[str] = []
    for t0, t1, cue in _iter_srt_cues(text):
        if not cue.strip():
            continue
        a0, a1 = _srt_ts_to_ass(t0), _srt_ts_to_ass(t1)
        # Escape ASS field-breaking newlines only; cue already has ASS overrides from ``_centered_cue_text``.
        safe = cue.replace("\n", r"\N")
        lines_ev.append(f"Dialogue: 0,{a0},{a1},Default,,0,0,0,,{safe}")
    header = (
        "[Script Info]\n"
        "Title: FirstSky burn\n"
        "ScriptType: v4.00+\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        f"PlayResX: {play_w}\n"
        f"PlayResY: {play_h}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Default,Impact,90,&H00F7F8FF,&H706E7480,&H00111624,&H00000000,-1,0,0,0,100,100,0,0,1,13,7,"
        "5,0,0,0,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    out_ass.write_text(
        "\ufeff" + header + "\n".join(lines_ev) + ("\n" if lines_ev else ""),
        encoding="utf-8",
    )
    return len(lines_ev)


def _tts_chunks(text: str, max_chars: int = 3000) -> list[str]:
    t = text.strip()
    if len(t) <= max_chars:
        return [t]
    out: list[str] = []
    while t:
        if len(t) <= max_chars:
            out.append(t)
            break
        chunk = t[:max_chars]
        br = chunk.rfind(". ")
        if br > max_chars // 3:
            out.append(t[: br + 1].strip())
            t = t[br + 1 :].strip()
        else:
            out.append(chunk.strip())
            t = t[max_chars:].strip()
    return out


def _tts_english_voices(engine: Any) -> list[Any]:
    """Collect installed pyttsx3 voices that look English-capable."""
    try:
        voices = list(engine.getProperty("voices") or [])
    except Exception:
        return []
    out: list[Any] = []
    for v in voices:
        vid = str(getattr(v, "id", "")).lower()
        vname = str(getattr(v, "name", "")).lower()
        ok = (
            "english" in vname
            or "en-us" in vid
            or "en-gb" in vid
            or "en_gb" in vid
            or "en-au" in vid
            or "en_au" in vid
            or "(united states)" in vname
            or "(united kingdom)" in vname
            or "(australia)" in vname
            or re.search(r"\ben[-_][a-z]{2}", vid) is not None
        )
        if not ok and ("en" in vid or "english" in vname):
            ok = True
        if ok and getattr(v, "id", None):
            out.append(v)
    seen: set[str] = set()
    uniq: list[Any] = []
    for v in out:
        oid = str(getattr(v, "id", ""))
        if oid not in seen:
            seen.add(oid)
            uniq.append(v)
    return uniq


def _pick_pyttsx3_voice(engine: Any) -> Any | None:
    """
    Choose a pyttsx3 voice. ``PTK_TTS_VOICE``:
    unset / empty — first English voice (stable);
    ``random`` / ``any`` / ``shuffle`` — uniform pick among English voices;
    ``0`` … ``N`` (digits only) — index into the English list (wraps);
    any other string — substring match on voice id or name (case-insensitive).
    """
    eng = _tts_english_voices(engine)
    if not eng:
        try:
            all_v = list(engine.getProperty("voices") or [])
            return all_v[0] if all_v else None
        except Exception:
            return None
    raw = (os.environ.get("PTK_TTS_VOICE") or "").strip()
    if not raw:
        return eng[0]
    key = raw.lower()
    if key in ("random", "any", "shuffle"):
        return random.choice(eng)
    if key in ("first", "default") or raw == "0":
        return eng[0]
    if raw.isdigit():
        return eng[int(raw) % len(eng)]
    for v in eng:
        vid = str(getattr(v, "id", "")).lower()
        vname = str(getattr(v, "name", "")).lower()
        if key in vid or key in vname:
            return v
    return eng[0]


def _pick_edge_tts_voice() -> str:
    """
    Neural voice from ``EDGE_TTS_ENGLISH_NEURAL`` using ``PTK_TTS_VOICE``:
    unset — first in list; random / index / substring on short name (e.g. ``jenny``, ``guy``);
    or a full Edge id such as ``en-US-GuyNeural``.
    """
    voices = EDGE_TTS_ENGLISH_NEURAL
    raw = (os.environ.get("PTK_TTS_VOICE") or "").strip()
    if not raw:
        return voices[0]
    key = raw.lower()
    if key in ("random", "any", "shuffle"):
        return random.choice(voices)
    if key in ("first", "default") or raw == "0":
        return voices[0]
    if raw.isdigit():
        return voices[int(raw) % len(voices)]
    for v in voices:
        if raw.lower() == v.lower():
            return v
    for v in voices:
        if key in v.lower():
            return v
    if "neural" in key and raw.count("-") >= 2:
        return raw
    return voices[0]


def text_to_speech_edge_mp3(text: str, out_mp3: Path) -> None:
    """Neural TTS via ``edge-tts`` (online); writes ``out_mp3``."""
    import edge_tts

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    voice = _pick_edge_tts_voice()
    parts = _tts_chunks(text, max_chars=EDGE_TTS_MAX_CHUNK)
    tmpdir = out_mp3.parent
    mp3_paths: list[Path] = []

    warned_fallback = False

    async def _synth() -> None:
        nonlocal warned_fallback
        for i, seg in enumerate(parts):
            p = tmpdir / f"_edge_{i}.mp3"
            ok = False
            for vtry in _edge_tts_voice_attempt_order(voice):
                try:
                    await edge_tts.Communicate(
                        seg, vtry, rate=_edge_tts_rate_param()
                    ).save(str(p))
                except Exception:
                    p.unlink(missing_ok=True)
                    continue
                if p.is_file() and p.stat().st_size >= 80:
                    if vtry != voice and not warned_fallback:
                        _warn(
                            f"Edge TTS: voice {voice!r} failed or unavailable; using {vtry!r} instead."
                        )
                        warned_fallback = True
                    mp3_paths.append(p)
                    ok = True
                    break
                p.unlink(missing_ok=True)
            if not ok:
                for q in mp3_paths:
                    q.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Edge TTS failed for segment {i + 1}/{len(parts)} "
                    f"(tried {_edge_tts_voice_attempt_order(voice)!r})."
                )

    asyncio.run(_synth())

    for p in mp3_paths:
        if not p.is_file() or p.stat().st_size < 80:
            for q in mp3_paths:
                q.unlink(missing_ok=True)
            raise RuntimeError("Edge TTS produced no audio.")

    if len(mp3_paths) == 1:
        shutil.move(str(mp3_paths[0]), str(out_mp3))
        return

    lst = tmpdir / "_edge_concat.txt"
    merged = tmpdir / "_edge_merged.mp3"
    try:
        with open(lst, "w", encoding="utf-8") as f:
            for p in mp3_paths:
                f.write(f"file '{p.as_posix()}'\n")
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
                    str(merged),
                ],
                timeout=600,
            )
        except subprocess.CalledProcessError:
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
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(merged),
                ],
                timeout=900,
            )
        shutil.move(str(merged), str(out_mp3))
    finally:
        lst.unlink(missing_ok=True)
        merged.unlink(missing_ok=True)
        for p in mp3_paths:
            p.unlink(missing_ok=True)


def text_to_speech_mp3(text: str, out_mp3: Path) -> None:
    """
    Offline TTS via pyttsx3 (local system voice), then convert WAV -> MP3.

    Multiple English voices: set ``PTK_TTS_VOICE=random`` or an index / name substring
    (see ``_pick_pyttsx3_voice``). Install more voices in OS settings if the list is short.
    """
    import pyttsx3

    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    parts = _tts_chunks(text)
    tmpdir = out_mp3.parent
    wav_chunks: list[Path] = []
    engine = pyttsx3.init()
    v = _pick_pyttsx3_voice(engine)
    if v is not None:
        try:
            engine.setProperty("voice", getattr(v, "id"))
        except Exception:
            pass
    for i, seg in enumerate(parts):
        w = tmpdir / f"_tts_{i}.wav"
        wav_chunks.append(w)
        engine.save_to_file(seg, str(w))
    engine.runAndWait()
    engine.stop()
    for w in wav_chunks:
        if not w.is_file() or w.stat().st_size < 100:
            raise RuntimeError("Local TTS produced no audio.")

    if len(wav_chunks) == 1:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(wav_chunks[0]),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(out_mp3),
            ]
        )
    else:
        lst = tmpdir / "_tts_concat.txt"
        with open(lst, "w", encoding="utf-8") as f:
            for p in wav_chunks:
                f.write(f"file '{p.as_posix()}'\n")
        merged_wav = tmpdir / "_tts_merged.wav"
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
                str(merged_wav),
            ]
        )
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(merged_wav),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(out_mp3),
            ]
        )
        merged_wav.unlink(missing_ok=True)
        lst.unlink(missing_ok=True)
    for p in wav_chunks:
        p.unlink(missing_ok=True)


def _ptk_gtts_lang() -> str:
    raw = (os.environ.get("PTK_GTTS_LANG") or os.environ.get("PTK_TTS_LANG") or "en").strip().lower()
    if not raw:
        return "en"
    return raw.split("-", 1)[0]


def text_to_speech_gtts_mp3(text: str, out_mp3: Path) -> None:
    """Google Translate TTS (online). Used when ``pyttsx3`` is not installed."""
    try:
        from gtts import gTTS  # type: ignore[import-not-found]
    except ImportError as e:
        raise RuntimeError(
            "Neither pyttsx3 nor gTTS is available. Install dependencies: pip install pyttsx3 gTTS"
        ) from e
    out_mp3.parent.mkdir(parents=True, exist_ok=True)
    parts = _tts_chunks(text, max_chars=2500)
    tmpdir = out_mp3.parent
    mp3_chunks: list[Path] = []
    lang = _ptk_gtts_lang()
    for i, seg in enumerate(parts):
        p = tmpdir / f"_gtts_{i}.mp3"
        mp3_chunks.append(p)
        gTTS(text=seg, lang=lang, slow=False).save(str(p))
    if not mp3_chunks or not all(x.is_file() and x.stat().st_size > 64 for x in mp3_chunks):
        for x in mp3_chunks:
            x.unlink(missing_ok=True)
        raise RuntimeError("gTTS produced no audio.")
    if len(mp3_chunks) == 1:
        shutil.move(str(mp3_chunks[0]), str(out_mp3))
        return
    lst = tmpdir / "_gtts_concat.txt"
    with open(lst, "w", encoding="utf-8") as f:
        for p in mp3_chunks:
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
                str(out_mp3),
            ]
        )
    finally:
        lst.unlink(missing_ok=True)
        for p in mp3_chunks:
            p.unlink(missing_ok=True)


def fetch_random_incompetech_music(out_mp3: Path) -> Path | None:
    # Prefer local tracks from folders/music first (cached directory scan).
    local_tracks = _list_local_music_tracks()
    if local_tracks:
        random.SystemRandom().shuffle(local_tracks)
        pick = local_tracks[0]
        try:
            _run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(pick),
                    "-vn",
                    "-c:a",
                    "libmp3lame",
                    "-q:a",
                    "2",
                    str(out_mp3),
                ],
                timeout=180,
            )
            return out_mp3 if out_mp3.is_file() else None
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            _warn("Local Music track conversion failed; using synthesized fallback.")
            return None
    if not local_tracks:
        _warn("No local tracks found in music folder; using synthesized fallback.")
    return None


def synth_fallback_bg_music(duration_sec: float, out_mp3: Path) -> Path | None:
    """
    Guaranteed local fallback: generate a soft ambient bed tone with ffmpeg.
    This avoids "no music" outputs when remote downloads fail.
    """
    d = max(8.0, min(3600.0, duration_sec + 2.0))
    try:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=220:sample_rate=44100:duration={d}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=329.63:sample_rate=44100:duration={d}",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:sample_rate=44100:duration={d}",
                "-filter_complex",
                "[0:a]volume=0.08[a0];[1:a]volume=0.06[a1];[2:a]volume=0.04[a2];"
                "[a0][a1][a2]amix=inputs=3:normalize=0,afade=t=in:st=0:d=1.2,afade=t=out:st="
                f"{max(0.0, d - 1.6)}:d=1.6[aout]",
                "-map",
                "[aout]",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "3",
                str(out_mp3),
            ],
            timeout=min(900, max(60, int(d * 2))),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out_mp3 if out_mp3.is_file() else None


def _edge_tts_rate_param() -> str:
    raw = (os.environ.get("PTK_EDGE_TTS_RATE") or EDGE_TTS_DEFAULT_RATE).strip()
    if raw.lower() in ("0", "off", "false", "none"):
        return "+0%"
    return raw or EDGE_TTS_DEFAULT_RATE


def _narration_ffmpeg_afilters_atempo(tempo: float) -> str:
    t = max(0.5, min(2.0, float(tempo)))
    if os.environ.get("PTK_NARRATION_CLEAR_FILTERS", "").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return f"atempo={t:.3f}"
    # Cut sub-bass mud; small presence lift (~3 kHz) for intelligibility after TTS encode.
    return (
        f"highpass=f=90,equalizer=f=3200:width_type=h:width=2200:g=2.5,atempo={t:.3f}"
    )


def speed_up_audio(src_mp3: Path, out_mp3: Path, factor: float = NARRATION_SPEED) -> None:
    """Adjust narration tempo; optional mild speech EQ for clarity (see ``_narration_ffmpeg_afilters_atempo``)."""
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src_mp3),
            "-filter:a",
            _narration_ffmpeg_afilters_atempo(factor),
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(out_mp3),
        ],
        timeout=600,
    )


def mix_narration_and_music(narration: Path, music: Path | None, out_mp3: Path) -> None:
    if music is None or not music.is_file():
        shutil.copy(narration, out_mp3)
        return
    try:
        narr_d = ffprobe_duration(narration)
    except Exception:
        narr_d = 600.0
    narr_r = narration.resolve()
    music_r = music.resolve()
    out_r = out_mp3.resolve()
    work_dir = str(out_r.parent)
    cwd: str | None = work_dir
    try:
        in_narr = Path(os.path.relpath(narr_r, work_dir)).as_posix()
        in_music = Path(os.path.relpath(music_r, work_dir)).as_posix()
        out_arg = out_r.name
    except ValueError:
        cwd = None
        in_narr, in_music, out_arg = str(narration), str(music), str(out_mp3)
    argv = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        in_narr,
        "-i",
        in_music,
        "-filter_complex",
        f"[0:a]volume={NARRATION_MIX_VOICE_GAIN:.3f}[v];"
        f"[1:a]volume={NARRATION_MIX_MUSIC_GAIN:.3f}[m];"
        "[v][m]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map",
        "[aout]",
        "-c:a",
        "libmp3lame",
        "-q:a",
        "2",
        out_arg,
    ]
    subprocess.run(
        argv,
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=None,
        timeout=min(7200, max(120, int(narr_d * 4) + 120)),
        cwd=cwd,
    )


def _srt_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60.0
    return f"{h:02d}:{m:02d}:{s:06.3f}".replace(".", ",")


def _parse_srt_ts(ts: str) -> float:
    ts = ts.strip()
    h_s, m_s, s_ms = ts.split(":", 2)
    sec_s, ms_s = s_ms.split(",", 1)
    return int(h_s, 10) * 3600 + int(m_s, 10) * 60 + int(sec_s, 10) + int(ms_s, 10) / 1000.0


def _shift_srt_times(path: Path, offset_sec: float) -> None:
    """Add ``offset_sec`` to every cue time range (e.g. after prepending lead audio)."""
    off = float(offset_sec)
    if off <= 0.0 or not path.is_file():
        return
    raw = path.read_text(encoding="utf-8")

    def repl(m: re.Match[str]) -> str:
        t0 = _parse_srt_ts(m.group(1)) + off
        t1 = _parse_srt_ts(m.group(2)) + off
        return f"{_srt_ts(t0)} --> {_srt_ts(t1)}"

    path.write_text(_RE_SRT_ARROW.sub(repl, raw), encoding="utf-8")


def _split_narration_cues(text: str) -> list[str]:
    """Break narration into short on-screen cues (sentence-ish, capped length)."""
    raw = _RE_WS.sub(" ", (text or "").strip())
    if not raw:
        return []
    parts = _RE_SENTENCE_SPLIT.split(raw)
    cues: list[str] = []
    buf = ""
    max_chars = 96

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            cues.append(buf.strip())
        buf = ""

    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(buf) + len(p) + 1 <= max_chars:
            buf = f"{buf} {p}".strip() if buf else p
        else:
            flush()
            if len(p) <= max_chars:
                buf = p
            else:
                for i in range(0, len(p), max_chars):
                    cues.append(p[i : i + max_chars].strip())
    flush()
    return cues


def _centered_cue_text(text: str, *, cue_duration_sec: float | None = None) -> str:
    """
    Center-middle ASS placement with optional duration-aware effects (Aegisub-style):
    ``\\fad`` fade in/out, ``\\blur`` soft edge, subtle ``\\t`` scale pop, and for
    multi-word cues with enough time, ``\\k`` karaoke timing so SecondaryColour tracks unread text.
    """
    t = (text or "").replace("\n", " ").strip()
    if not t:
        return r"{\an5\fad(60,80)}…"
    words = t.split()
    d = float(cue_duration_sec) if cue_duration_sec is not None and cue_duration_sec > 0 else None
    d_ms = (d * 1000.0) if d is not None else None
    # Fade in/out (ms): ~10–14% of cue length, clamped (Aegisub ``\\fad``).
    if d_ms is not None:
        fi = int(max(35, min(220, d_ms * 0.14)))
        fo = int(max(35, min(240, d_ms * 0.12)))
    else:
        fi, fo = 85, 95
    # Subtle pop-in via ``\\t`` (ms from line start); skip on very short cues.
    pop = ""
    if d_ms is not None and d_ms >= 140:
        t_up = int(max(80, min(160, d_ms * 0.18)))
        t_dn = int(max(100, min(200, d_ms * 0.22)))
        t2 = min(int(d_ms * 0.45), t_up + t_dn + 40)
        pop = rf"\t(0,{t_up},\fscx108\fscy108)\t({t_up},{t2},\fscx100\fscy100)"
    head = rf"{{\an5\fad({fi},{fo})\blur0.55{pop}}}"
    palette = (
        "&H00F7F8FF&",  # near white (cool)
        "&H00FFD36B&",  # warm amber
        "&H00B2A0FF&",  # lilac
        "&H0098F4FF&",  # cyan
    )
    # Karaoke ``\\k`` (centiseconds): unread syllables use SecondaryColour from style.
    use_karaoke = (
        d is not None
        and len(words) >= 2
        and d >= 0.32
        and len(words) <= 14
    )
    if use_karaoke:
        # ``\\k`` karaoke sweep: unread text uses SecondaryColour from style (Aegisub / libass).
        total_cs = max(24, int(d * 100))
        per = max(2, total_cs // len(words))
        parts: list[str] = []
        for w in words:
            safe = w.replace("{", "(").replace("}", ")")
            parts.append(r"{\k" + str(per) + "}" + safe)
        return head + " ".join(parts) + r"{\r}"
    colored = []
    for i, w in enumerate(words):
        safe = w.replace("{", "(").replace("}", ")")
        colored.append(r"{\c" + palette[i % len(palette)] + "}" + safe)
    return head + " ".join(colored) + r"{\r}"


def write_proportional_srt(text: str, duration_sec: float, out_srt: Path) -> None:
    """Spread cues across ``duration_sec`` weighted by cue word count."""
    cues = _split_narration_cues(text)
    d = max(0.5, float(duration_sec))
    if not cues:
        out_srt.write_text(
            "1\n00:00:00,000 --> {}\n…\n".format(_srt_ts(min(2.0, d))),
            encoding="utf-8",
        )
        return
    # Better than fixed slices: longer cues get proportionally longer on-screen time.
    weights: list[int] = []
    for cue in cues:
        w = len(_RE_NONSPACE.findall(cue))
        weights.append(max(1, w))
    total_weight = max(1, sum(weights))

    lines: list[str] = []
    elapsed = 0.0
    for i, cue in enumerate(cues):
        t0 = elapsed
        if i == len(cues) - 1:
            t1 = d
        else:
            t1 = elapsed + (d * (weights[i] / total_weight))
        if t1 - t0 < 0.2:
            t1 = t0 + 0.2
        safe = _centered_cue_text(cue, cue_duration_sec=t1 - t0)
        lines.append(f"{i + 1}\n{_srt_ts(t0)} --> {_srt_ts(t1)}\n{safe}\n")
        elapsed = t1
    out_srt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_word_by_word_srt_fast(text: str, duration_sec: float, out_srt: Path) -> None:
    """Very fast subtitle writer: no audio analysis, pure text timing."""
    words = _RE_NONSPACE.findall((text or "").strip())
    d = max(0.5, float(duration_sec))
    if not words:
        out_srt.write_text(
            "1\n00:00:00,000 --> {}\n…\n".format(_srt_ts(min(2.0, d))),
            encoding="utf-8",
        )
        return
    per = max(0.06, d / max(1, len(words)))
    lines: list[str] = []
    t = 0.0
    for i, w in enumerate(words, start=1):
        t0 = t
        t1 = d if i == len(words) else min(d, t + per)
        if t1 <= t0:
            t1 = min(d, t0 + 0.06)
        lines.append(
            f"{i}\n{_srt_ts(t0)} --> {_srt_ts(t1)}\n{_centered_cue_text(w, cue_duration_sec=t1 - t0)}\n"
        )
        t = t1
    out_srt.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _detect_silence_ranges(audio_mp3: Path, duration_sec: float) -> list[tuple[float, float]]:
    """
    Detect silence windows in **narration-only** audio with ffmpeg silencedetect.
    High-pass + mono collapse reduces false breaks from music bed / rumble if the
    wrong bus were ever analyzed; primary fix is always passing voice-only paths
    from the pipeline (``narr_for_mix``, never ``mixed``).
    """
    sink = "NUL" if sys.platform == "win32" else "/dev/null"
    # High-pass reduces sub-bass from music beds if analysis input were ever contaminated;
    # mono-safe (no stereo-only pan) so TTS / mono narration does not error out.
    af = "highpass=f=220,silencedetect=noise=-32dB:d=0.28"
    try:
        proc = _run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-i",
                str(audio_mp3),
                "-af",
                af,
                "-f",
                "null",
                sink,
            ],
        )
    except subprocess.CalledProcessError:
        return []
    log = f"{proc.stdout}\n{proc.stderr}"
    starts = [float(m.group(1)) for m in _RE_SILENCE_START.finditer(log)]
    ends = [float(m.group(1)) for m in _RE_SILENCE_END.finditer(log)]
    if not starts and not ends:
        return []
    ranges: list[tuple[float, float]] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration_sec
        if e > s:
            ranges.append((max(0.0, s), min(duration_sec, e)))
    return ranges


def _voice_segments_from_silence(duration_sec: float, silences: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Build non-silent segments from silence ranges."""
    if duration_sec <= 0:
        return []
    if not silences:
        return [(0.0, duration_sec)]
    segs: list[tuple[float, float]] = []
    cur = 0.0
    for s, e in silences:
        s = max(0.0, min(duration_sec, s))
        e = max(0.0, min(duration_sec, e))
        if s > cur + 0.06:
            segs.append((cur, s))
        cur = max(cur, e)
    if cur < duration_sec - 0.06:
        segs.append((cur, duration_sec))
    merged: list[tuple[float, float]] = []
    for s, e in segs:
        if e - s < 0.18:
            if merged:
                ms, me = merged[-1]
                merged[-1] = (ms, e)
            continue
        merged.append((s, e))
    return merged or [(0.0, duration_sec)]


def _split_words_to_segment_durations(text: str, segs: list[tuple[float, float]]) -> list[str]:
    """Distribute narration words across timing segments proportionally by duration."""
    words = _RE_NONSPACE.findall((text or "").strip())
    if not words:
        return ["…"] * max(1, len(segs))
    n = max(1, len(segs))
    if n == 1:
        return [" ".join(words)]
    durs = [max(0.001, e - s) for s, e in segs]
    total_d = sum(durs) or 1.0
    raw = [len(words) * (d / total_d) for d in durs]
    counts = [max(1, int(round(x))) for x in raw]
    diff = sum(counts) - len(words)
    while diff > 0:
        i = max(range(n), key=lambda k: counts[k])
        if counts[i] > 1:
            counts[i] -= 1
            diff -= 1
        else:
            break
    while diff < 0:
        i = min(range(n), key=lambda k: counts[k])
        counts[i] += 1
        diff += 1
    out: list[str] = []
    j = 0
    for c in counts:
        part = words[j : j + c]
        out.append(" ".join(part).strip() or "…")
        j += c
    if j < len(words):
        out[-1] = (out[-1] + " " + " ".join(words[j:])).strip()
    return out


def _word_cues_from_segments(text: str, segs: list[tuple[float, float]]) -> list[tuple[float, float, str]]:
    """Build per-word subtitle cues using silence-derived speech segments."""
    words = _RE_NONSPACE.findall((text or "").strip())
    if not words:
        return []
    chunks = _split_words_to_segment_durations(text, segs)
    if not chunks:
        return []
    cues: list[tuple[float, float, str]] = []
    for (t0, t1), chunk in zip(segs, chunks):
        seg_words = _RE_NONSPACE.findall(chunk)
        if not seg_words:
            continue
        dur = max(0.10, t1 - t0)
        per = dur / len(seg_words)
        cur = t0
        for i, w in enumerate(seg_words):
            nt = t1 if i == len(seg_words) - 1 else min(t1, cur + per)
            if nt - cur < 0.05:
                nt = min(t1, cur + 0.05)
            cues.append((cur, nt, w))
            cur = nt
    return cues


def try_write_pause_based_srt(text: str, audio_mp3: Path, duration_sec: float, out_srt: Path) -> bool:
    """
    Word-by-word subtitle timing from **narration-only** volume drops (never mixed music).
    Timeline length always comes from ``audio_mp3`` via ffprobe so it cannot drift from
    the file used for silencedetect (avoids matching the mixed master length by mistake).
    """
    try:
        d = max(0.5, float(ffprobe_duration(audio_mp3)))
    except Exception:
        try:
            d = max(0.5, float(duration_sec))
        except (TypeError, ValueError):
            return False
    silences = _detect_silence_ranges(audio_mp3, d)
    segs = _voice_segments_from_silence(d, silences)
    if not segs:
        return False
    word_cues = _word_cues_from_segments(text, segs)
    if not word_cues:
        return False
    lines: list[str] = []
    for i, (t0, t1, cue) in enumerate(word_cues, start=1):
        if t1 - t0 < 0.04:
            continue
        lines.append(
            f"{i}\n{_srt_ts(t0)} --> {_srt_ts(t1)}\n{_centered_cue_text(cue, cue_duration_sec=t1 - t0)}\n"
        )
    if not lines:
        return False
    out_srt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return True


def try_write_whisper_srt(audio_mp3: Path, out_srt: Path) -> bool:
    """
    If ``PTK_WHISPER=1`` and faster-whisper is installed, write word-aligned SRT
    from the sped-up narration audio (no background music).
    """
    if os.environ.get("PTK_WHISPER", "").strip() not in ("1", "true", "yes"):
        return False
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        _warn("PTK_WHISPER=1 set but faster-whisper is not installed; using proportional subs.")
        return False
    try:
        model_name = os.environ.get("PTK_WHISPER_MODEL", "base").strip() or "base"
        model = WhisperModel(model_name, device="cpu", compute_type="int8")
        segments, _ = model.transcribe(
            str(audio_mp3),
            language="en",
            vad_filter=True,
            word_timestamps=True,
        )
        lines: list[str] = []
        idx = 1
        for seg in segments:
            seg_words = getattr(seg, "words", None) or []
            wrote_words = False
            for w in seg_words:
                t0 = float(getattr(w, "start", 0.0) or 0.0)
                t1 = float(getattr(w, "end", 0.0) or 0.0)
                txt = str(getattr(w, "word", "") or "").strip()
                if not txt or t1 <= t0:
                    continue
                lines.append(
                    f"{idx}\n{_srt_ts(t0)} --> {_srt_ts(t1)}\n"
                    f"{_centered_cue_text(txt, cue_duration_sec=t1 - t0)}\n"
                )
                idx += 1
                wrote_words = True
            if wrote_words:
                continue
            t0, t1 = float(seg.start), float(seg.end)
            if t1 <= t0:
                continue
            txt = (seg.text or "").strip()
            if not txt:
                continue
            lines.append(
                f"{idx}\n{_srt_ts(t0)} --> {_srt_ts(t1)}\n"
                f"{_centered_cue_text(txt, cue_duration_sec=t1 - t0)}\n"
            )
            idx += 1
        if not lines:
            return False
        out_srt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except Exception as e:
        _warn(f"Whisper subtitle pass failed ({e}); using proportional subs.")
        return False


def _vf_subtitles_burn(
    sub_path: Path,
    *,
    force_style: bool = True,
) -> str:
    """Path for ffmpeg ``subtitles`` filter (Windows-safe). Optional ``force_style`` for bare SRT."""
    p = sub_path.resolve()
    if sys.platform == "win32":
        s = str(p).replace("\\", "/")
        if len(s) >= 2 and s[1] == ":":
            s = s[0] + "\\:" + s[2:]
    else:
        s = str(p)
    s = s.replace("'", r"'\\''")
    if not force_style:
        return f"subtitles='{s}'"
    # Legacy path: SRT + force_style (colour tokens can confuse the filter parser; prefer ASS burn).
    style = (
        "FontName=Impact,FontSize=90,Bold=1,Italic=0,Underline=0,"
        "Outline=13,Shadow=7,Spacing=1.6,Angle=0,"
        "PrimaryColour=&H00F7F8FF&,SecondaryColour=&H706E7480&,"
        "OutlineColour=&H00111624&,BackColour=&H00000000&,BorderStyle=1,"
        "Alignment=5,MarginL=0,MarginR=0,MarginV=0"
    )
    return f"subtitles='{s}':force_style='{style}'"


def _concat_audio_disclaimer_first(disclaimer: Path, narration_mixed: Path, out_mp3: Path) -> None:
    try:
        d0 = ffprobe_duration(disclaimer)
        d1 = ffprobe_duration(narration_mixed)
    except Exception:
        d0, d1 = 30.0, 600.0
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(disclaimer),
            "-i",
            str(narration_mixed),
            "-filter_complex",
            "[0:a][1:a]concat=n=2:v=0:a=1[aout]",
            "-map",
            "[aout]",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(out_mp3),
        ],
        timeout=min(7200, max(120, int((d0 + d1) * 4) + 120)),
    )


def _generate_disclaimer_tts_random_voice(out_mp3: Path) -> bool:
    """
    Generate disclaimer audio using a random TTS voice each call.
    Falls back to the existing static disclaimer file if synthesis fails.
    """
    txt = (os.environ.get("PTK_DISCLAIMER_TEXT") or DEFAULT_DISCLAIMER_TTS_TEXT).strip()
    if not txt:
        return False
    prev = os.environ.get("PTK_TTS_VOICE")
    try:
        os.environ["PTK_TTS_VOICE"] = "random"
        text_to_speech_preferred(txt, out_mp3)
        return out_mp3.is_file() and out_mp3.stat().st_size > 200
    except Exception as e:
        _warn(f"Dynamic disclaimer TTS failed ({e}); trying static disclaimer audio.")
        return False
    finally:
        if prev is None:
            os.environ.pop("PTK_TTS_VOICE", None)
        else:
            os.environ["PTK_TTS_VOICE"] = prev


def _prepend_black_video_lead(bg_video: Path, lead_sec: float, out_mp4: Path) -> None:
    lead = max(0.05, float(lead_sec))
    w, h, fps = OUTPUT_W, OUTPUT_H, OUTPUT_FPS
    try:
        vd = ffprobe_duration(bg_video)
    except Exception:
        vd = 120.0
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={w}x{h}:r={fps}:d={lead}",
            "-i",
            str(bg_video),
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[outv]",
            "-map",
            "[outv]",
            *_libx264_encode_args(crf=X264_CRF_BG),
            "-an",
            str(out_mp4),
        ],
        timeout=min(7200, max(180, int(lead + vd) * 2 + 120)),
    )


def _mux_watermark_drawtext_filter() -> str | None:
    """Small bottom-left ``FirstSky`` mark at 70% opacity. Disable with ``PTK_WATERMARK=0``."""
    if os.environ.get("PTK_WATERMARK", "").strip().lower() in ("0", "false", "no", "off"):
        return None
    ff = _watermark_fontfile_for_drawtext()
    head = f"drawtext=fontfile='{ff}':text=" if ff else "drawtext=text="
    return (
        head
        + r"'FirstSky':fontcolor=white@0.7:"
        r"fontsize=min(22\,max(14\,h/85)):x=16:y=h-text_h-16"
    )


def _vf_append_watermark(base: str) -> str:
    wm = _mux_watermark_drawtext_filter()
    if not wm:
        return base
    return f"{base},{wm}" if base else wm


def mux_video_audio(
    video: Path,
    audio: Path,
    out_mp4: Path,
    *,
    subtitles: Path | None = None,
) -> None:
    try:
        d = max(ffprobe_duration(video), ffprobe_duration(audio))
    except Exception:
        d = 600.0
    mux_timeout = min(7200, max(300, int(d * 10) + 300))
    threads = max(1, int(os.cpu_count() or 4))

    if subtitles is not None and subtitles.is_file():
        vw, vh = ffprobe_video_dimensions(video)
        ass_burn = subtitles.parent / f"{subtitles.stem}.cuet_burn.ass"
        try:
            n_cues = _write_burn_ass_from_srt(subtitles, vw, vh, ass_burn)
            vf = (
                _vf_subtitles_burn(ass_burn, force_style=False)
                if n_cues > 0
                else _vf_subtitles_burn(subtitles, force_style=True)
            )
            if n_cues == 0:
                ass_burn.unlink(missing_ok=True)
            vf = _vf_append_watermark(vf)
            venc = _hw_encode_args_for_subs_mux() or _libx264_encode_args(crf=X264_CRF_SUBS)
            _run_ffmpeg(
                [
                    "ffmpeg",
                    "-y",
                    "-i",
                    str(video),
                    "-i",
                    str(audio),
                    "-threads",
                    str(threads),
                    "-filter_threads",
                    str(threads),
                    "-vf",
                    vf,
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    *venc,
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-shortest",
                    "-movflags",
                    "+faststart",
                    str(out_mp4),
                ],
                timeout=mux_timeout,
            )
        finally:
            ass_burn.unlink(missing_ok=True)
        meta = getattr(_MUX_EXTRA_META, "data", None)
        try:
            extras.after_mux_extras(out_mp4, meta if isinstance(meta, dict) else None)
        except Exception as e:
            _warn(f"Output extras: {e}")
        return

    wm = _mux_watermark_drawtext_filter()
    if wm:
        venc = _hw_encode_args_for_subs_mux() or _libx264_encode_args(crf=X264_CRF_SUBS)
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-threads",
                str(threads),
                "-filter_threads",
                str(threads),
                "-vf",
                wm,
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                *venc,
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ],
            timeout=mux_timeout,
        )
    else:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-shortest",
                "-movflags",
                "+faststart",
                str(out_mp4),
            ],
            timeout=mux_timeout,
        )
    meta = getattr(_MUX_EXTRA_META, "data", None)
    try:
        extras.after_mux_extras(out_mp4, meta if isinstance(meta, dict) else None)
    except Exception as e:
        _warn(f"Output extras: {e}")


def download_reddit_media(media_url: str, out_template: Path) -> Path | None:
    """Download Reddit-hosted video directly via HTTP."""
    out_path = out_template.with_suffix(".mp4")
    headers = {
        "User-Agent": _reddit_compliant_user_agent(),
        "Accept": "*/*",
        "Referer": "https://www.reddit.com/",
    }
    try:
        with _REDDIT_SESSION.get(media_url, headers=headers, timeout=(8, 45), stream=True) as r:
            r.raise_for_status()
            with open(out_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException:
        return None
    if not out_path.is_file() or out_path.stat().st_size < 50_000:
        out_path.unlink(missing_ok=True)
        return None
    return out_path


def _ffmpeg_concat_demux_line(rel_or_name: str) -> str:
    """One line for ffmpeg concat demuxer; escape single quotes in the path token."""
    tok = rel_or_name.replace("'", r"'\''")
    return f"file '{tok}'"


def concat_videos(paths: list[Path], out: Path) -> None:
    """
    Join clips with ffmpeg's concat demuxer.

    On Windows, some absolute paths can confuse the concat demuxer, so we run ffmpeg with ``cwd`` set to the output
    directory and list only **relative** paths in the concat script.
    """
    work_dir = out.parent.resolve()
    lst = work_dir / "concat_list.txt"
    lines: list[str] = []
    for p in paths:
        pr = p.resolve()
        if not pr.is_file() or pr.stat().st_size < 32:
            raise RuntimeError(f"Concat missing or empty clip: {pr}")
        try:
            rel = Path(os.path.relpath(pr, work_dir)).as_posix()
        except ValueError:
            rel = pr.name
        if rel.startswith(".."):
            raise RuntimeError(f"Concat clip outside work dir {work_dir}: {pr}")
        lines.append(_ffmpeg_concat_demux_line(rel))
    lst.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _invoke(argv_tail: list[str], *, timeout: float) -> None:
        meta = _ffmpeg_strip_metadata_args_if_video(out.name)
        argv = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            lst.name,
            *argv_tail,
            *meta,
            out.name,
        ]
        subprocess.run(
            argv,
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=None,
            timeout=timeout,
            cwd=str(work_dir),
        )

    try:
        _invoke(["-c", "copy"], timeout=max(120, 30 * len(paths)))
    except subprocess.CalledProcessError:
        _warn("Concat stream-copy failed (mixed codecs); re-encoding join…")
        _invoke(["-an", *_libx264_encode_args(crf=X264_CRF_BG)], timeout=max(300, 60 * len(paths)))


def scale_to_9x16_and_trim(src: Path, duration: float, out: Path) -> None:
    w = OUTPUT_W
    h = OUTPUT_H
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
        f"trim=duration={duration},setpts=PTS-STARTPTS"
    )
    enc_timeout = min(7200, max(180, int(duration * 2.5) + 120))
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vf",
            vf,
            "-t",
            str(duration),
            "-an",
            *_libx264_encode_args(crf=X264_CRF_BG, with_fps_r=True),
            str(out),
        ],
        timeout=enc_timeout,
    )


# ORL slideshow: without caps, many sequential 30fps x264 still encodes + GIF decodes feel "stuck" on 4/7.
_ORL_SLIDES_MAX = 6
_ORL_SLIDE_MAX_SEC = 10.0
_ORL_STILL_FPS = 12


def _orl_portrait_wh() -> tuple[int, int]:
    """9:16 vertical canvas — same as ``OUTPUT_W`` × ``OUTPUT_H`` (shorts)."""
    return OUTPUT_W, OUTPUT_H


def _libx264_encode_args_orl_slideshow(*, crf: str, fps: int) -> list[str]:
    """x264 for ORL slideshow (Ken Burns motion — no ``stillimage`` tune)."""
    return [
        "-c:v",
        "libx264",
        "-preset",
        X264_PRESET,
        "-crf",
        crf,
        "-r",
        str(fps),
    ]


def _orl_wikipedia_user_agent() -> str:
    return (
        f"python:firstsky:v{__version__} "
        f"(ORL Wikipedia slideshow; https://www.mediawiki.org/wiki/API:Etiquette)"
    )


def _orl_wiki_get(lang: str, params: dict[str, Any]) -> dict[str, Any]:
    url = f"https://{lang}.wikipedia.org/w/api.php"
    headers = {"User-Agent": _orl_wikipedia_user_agent()}
    r = requests.get(url, params={**params, "format": "json"}, headers=headers, timeout=(8, 45))
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise RuntimeError("ORL: invalid Wikipedia API response.")
    return data


def _orl_fallback_portrait_bg(target_duration: float, work: Path, w: int, h: int) -> Path:
    d = max(1.0, float(target_duration))
    out = work / "orl_portrait_fallback.mp4"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={w}x{h}:r={OUTPUT_FPS}:d={d}",
            "-an",
            *_libx264_encode_args(crf=X264_CRF_BG),
            str(out),
        ],
        timeout=min(300, max(30, int(d) + 20)),
    )
    return out


def _orl_scale_trim_portrait(src: Path, duration: float, out: Path, w: int, h: int) -> None:
    d = max(0.1, float(duration))
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
        f"trim=duration={d},setpts=PTS-STARTPTS"
    )
    enc_timeout = min(7200, max(180, int(d * 2.5) + 120))
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vf",
            vf,
            "-t",
            str(d),
            "-an",
            *_libx264_encode_args(crf=X264_CRF_BG, with_fps_r=True),
            str(out),
        ],
        timeout=enc_timeout,
    )


def _orl_ken_burns_vf(w: int, h: int, d_sec: float, sfps: int, slide_idx: int) -> str:
    """
    Subtle Ken Burns: upscale, then ``zoompan`` (zoom in/out or pan) into ``w``×``h``.
    Patterns rotate by slide index so adjacent clips do not all move the same way.
    """
    d_sec = max(0.35, float(d_sec))
    fr = max(1, int(round(d_sec * sfps)))
    den = max(1, fr - 1)
    p = slide_idx % 4
    if p == 0:
        z = f"1+0.08*on/{den}"
        x, y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif p == 1:
        z = f"1.08-0.08*on/{den}"
        x, y = "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"
    elif p == 2:
        z = "1.075"
        x = f"(iw-iw/zoom)*on/{den}"
        y = "(ih-ih/zoom)/2"
    else:
        z = "1.075"
        x = "(iw-iw/zoom)/2"
        y = f"(ih-ih/zoom)*on/{den}"
    zp = f"zoompan=z='{z}':x='{x}':y='{y}':d={fr}:s={w}x{h}:fps={sfps}"
    upscale = r"scale='min(iw*4\,4800)':-2:flags=lanczos,setsar=1"
    return f"{upscale},{zp},format=yuv420p"


def _orl_encode_still_frame(
    image: Path,
    duration: float,
    out_mp4: Path,
    w: int,
    h: int,
    *,
    slide_idx: int = 0,
) -> None:
    d = max(0.35, float(duration))
    sfps = _ORL_STILL_FPS
    # Short fade-in on each still; fade-out before the cut so joins do not flash hard.
    fi = min(0.42, max(0.12, d * 0.22))
    fo = min(0.38, max(0.1, d * 0.16))
    if fi + fo > d - 0.06:
        s = (d - 0.06) / (fi + fo)
        fi *= s
        fo *= s
    st_out = max(0.0, d - fo)
    kb = _orl_ken_burns_vf(w, h, d, sfps, slide_idx)
    vf = (
        f"{kb},"
        f"fade=t=in:st=0:d={fi:.4f},fade=t=out:st={st_out:.4f}:d={fo:.4f},fps={sfps}"
    )
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image),
            "-t",
            str(d),
            "-vf",
            vf,
            "-an",
            *_libx264_encode_args_orl_slideshow(crf=X264_CRF_BG, fps=sfps),
            str(out_mp4),
        ],
        timeout=min(600, max(45, int(d * sfps // 2) + 90)),
    )


def _orl_download_wiki_images(urls: list[str], work: Path) -> list[Path]:
    headers = {"User-Agent": _orl_wikipedia_user_agent()}
    paths: list[Path] = []
    max_bytes = 9 * 1024 * 1024
    for i, url in enumerate(urls):
        ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
        if ext == ".gif":
            # Animated GIFs make ffmpeg decode huge frame counts; skip for predictable speed.
            continue
        if ext not in (".jpg", ".jpeg", ".png", ".webp", ".bmp", ""):
            ext = ".jpg"
        dest = work / f"orl_wiki_dl_{i:03d}{ext}"
        try:
            with requests.get(url, headers=headers, timeout=(8, 35), stream=True) as r:
                r.raise_for_status()
                n = 0
                with open(dest, "wb") as f:
                    for chunk in r.iter_content(chunk_size=256 * 1024):
                        if not chunk:
                            continue
                        n += len(chunk)
                        if n > max_bytes:
                            break
                        f.write(chunk)
            if n > max_bytes or not dest.is_file() or dest.stat().st_size < 800:
                dest.unlink(missing_ok=True)
                continue
            paths.append(dest)
        except (requests.RequestException, OSError):
            dest.unlink(missing_ok=True)
            continue
    return paths


def build_orl_wiki_slideshow(image_urls: list[str], target_duration: float, work: Path) -> Path:
    """
    Evenly spaced slideshow on 9:16 canvas; each image gets a subtle Ken Burns zoom/pan, total ~= target_duration.
    """
    w, h = _orl_portrait_wh()
    urls = list(image_urls or [])[:_ORL_SLIDES_MAX]
    if not urls:
        return _orl_fallback_portrait_bg(target_duration, work, w, h)
    local = _orl_download_wiki_images(urls, work)
    if not local:
        return _orl_fallback_portrait_bg(target_duration, work, w, h)
    n = len(local)
    per = float(target_duration) / n
    per = max(0.5, min(per, _ORL_SLIDE_MAX_SEC))
    clips: list[Path] = []
    for i, p in enumerate(local):
        clip = work / f"orl_slide_{i:03d}.mp4"
        _orl_encode_still_frame(p, per, clip, w, h, slide_idx=i)
        clips.append(clip)
    merged = work / "orl_slides_joined.mp4"
    if len(clips) == 1:
        shutil.copy(clips[0], merged)
    else:
        concat_videos(clips, merged)
    merged = _extend_video_to_length(merged, target_duration, work)
    final_v = work / "orl_9x16_bg.mp4"
    _orl_scale_trim_portrait(merged, target_duration, final_v, w, h)
    return final_v


def _strip_audio_video(src: Path, out: Path) -> Path:
    """Create a video-only copy/remux of a clip (guaranteed silent)."""
    if not _has_audio_stream(src):
        shutil.copy(src, out)
        return out
    try:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-an",
                "-c:v",
                "copy",
                str(out),
            ],
            timeout=180,
        )
        return out
    except subprocess.CalledProcessError:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(src),
                "-an",
                *_libx264_encode_args(crf=X264_CRF_BG),
                str(out),
            ],
            timeout=300,
        )
        return out


def _has_audio_stream(path: Path) -> bool:
    try:
        out = _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ]
        )
    except subprocess.CalledProcessError:
        return False
    return bool(out.stdout.strip())


def _extend_video_to_length(src: Path, target_sec: float, work: Path) -> Path:
    """Loop video when merged clips are shorter than narration (avoids endless Reddit downloads)."""
    try:
        d = ffprobe_duration(src)
    except Exception:
        return src
    if d >= target_sec - 0.2:
        return src
    out = work / "asmr_looped.mp4"
    try:
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-stream_loop",
                "-1",
                "-i",
                str(src),
                "-t",
                str(target_sec),
                "-an",
                "-c:v",
                "copy",
                str(out),
            ],
            timeout=min(300, max(60, int(target_sec) // 4)),
        )
        return out
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _warn("Video loop (copy) failed; re-encoding to target length…")
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-stream_loop",
                "-1",
                "-i",
                str(src),
                "-t",
                str(target_sec),
                "-an",
                *_libx264_encode_args(crf=X264_CRF_BG),
                str(out),
            ],
            timeout=min(3600, max(300, int(target_sec) // 2)),
        )
        return out


def _fallback_bg_video(target_duration: float, work: Path) -> Path:
    """Guaranteed quick fallback when Reddit b-roll is unavailable/slow."""
    d = max(1.0, float(target_duration))
    out = work / "asmr_fallback.mp4"
    _run_ffmpeg(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={OUTPUT_W}x{OUTPUT_H}:r={OUTPUT_FPS}:d={d}",
            "-an",
            *_libx264_encode_args(crf=X264_CRF_BG),
            str(out),
        ],
        timeout=min(300, max(30, int(d) + 20)),
    )
    return out


def build_background_video(target_duration: float, work: Path) -> Path:
    posts = fetch_asmr_video_posts()
    if not posts:
        _warn("No usable Reddit b-roll posts found; using fallback background.")
        return _fallback_bg_video(target_duration, work)
    shuffled_posts = list(posts)
    random.shuffle(shuffled_posts)
    post_idx = 0

    def next_post() -> dict[str, Any]:
        nonlocal post_idx
        if post_idx >= len(shuffled_posts):
            random.shuffle(shuffled_posts)
            post_idx = 0
        p = shuffled_posts[post_idx]
        post_idx += 1
        return p

    if FAST_RENDER_MODE:
        # Fast path with variety floor: collect multiple unique clips before any looping fallback.
        max_attempts = min(22, max(10, len(posts)))
        deadline = time.time() + BROLL_BUILD_BUDGET_SEC
        clips: list[Path] = []
        total = 0.0
        used_urls: set[str] = set()
        for i in range(max_attempts):
            if time.time() >= deadline:
                break
            p = next_post()
            media_url = str(p.get("media_url") or "")
            if not media_url or media_url in used_urls:
                continue
            raw = work / f"asmr_fast_{len(clips):03d}"
            path = download_reddit_media(media_url, raw)
            if not path or not path.is_file():
                continue
            silent = _strip_audio_video(path, work / f"asmr_fast_{len(clips):03d}_silent.mp4")
            try:
                d = ffprobe_duration(silent)
            except Exception:
                continue
            if d < 0.5:
                continue
            used_urls.add(media_url)
            clips.append(silent)
            total += d
            if len(clips) >= BROLL_MIN_UNIQUE_CLIPS and total >= max(8.0, target_duration * 0.65):
                break
        if len(clips) < BROLL_MIN_UNIQUE_CLIPS:
            _warn("Fast b-roll path could not collect enough unique clips; using fallback background.")
            return _fallback_bg_video(target_duration, work)
        merged = work / "asmr_merged.mp4"
        if len(clips) == 1:
            shutil.copy(clips[0], merged)
        else:
            concat_videos(clips, merged)
        _info(
            f"  ASMR fast-path: {len(clips)} unique clip(s), ~{total:.0f}s before optional loop to {target_duration:.0f}s"
        )
        merged = _extend_video_to_length(merged, target_duration, work)
        final_v = work / "asmr_9x16.mp4"
        scale_to_9x16_and_trim(merged, target_duration, final_v)
        return final_v

    clips: list[Path] = []
    total = 0.0
    attempts = 0
    fail_streak = 0
    used_urls: set[str] = set()
    # Cap attempts so long narration cannot mean endless media download retries.
    max_attempts = min(28, max(12, len(posts) * 2))
    deadline = time.time() + BROLL_BUILD_BUDGET_SEC
    while total < target_duration and attempts < max_attempts and fail_streak < 14:
        if time.time() >= deadline:
            break
        attempts += 1
        p = next_post()
        media_url = str(p.get("media_url") or "")
        if not media_url or media_url in used_urls:
            continue
        raw = work / f"asmr_{len(clips):03d}"
        path = download_reddit_media(media_url, raw)
        if not path or not path.is_file():
            fail_streak += 1
            continue
        silent = _strip_audio_video(path, work / f"asmr_{len(clips):03d}_silent.mp4")
        try:
            d = ffprobe_duration(silent)
        except Exception:
            fail_streak += 1
            continue
        if d < 0.5:
            fail_streak += 1
            continue
        fail_streak = 0
        used_urls.add(media_url)
        clips.append(silent)
        total += d
        if attempts % 4 == 0 or total >= target_duration:
            _info(
                f"  ASMR b-roll: {len(clips)} clip(s), ~{total:.0f}s / {target_duration:.0f}s "
                f"(attempt {attempts}/{max_attempts})"
            )
    if not clips:
        _warn("Could not download b-roll clips in time; using fallback background.")
        return _fallback_bg_video(target_duration, work)
    if len(clips) < BROLL_MIN_UNIQUE_CLIPS:
        _warn(
            f"Collected only {len(clips)} unique b-roll clip(s); need at least {BROLL_MIN_UNIQUE_CLIPS} before looping fallback."
        )
        return _fallback_bg_video(target_duration, work)
    merged = work / "asmr_merged.mp4"
    if len(clips) == 1:
        shutil.copy(clips[0], merged)
    else:
        concat_videos(clips, merged)
    merged = _extend_video_to_length(merged, target_duration, work)
    final_v = work / "asmr_9x16.mp4"
    scale_to_9x16_and_trim(merged, target_duration, final_v)
    return final_v


def next_video_index() -> int:
    existing = list(OUTPUT.glob("video*.mp4"))
    nums = []
    for p in existing:
        m = _RE_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rst_video_index() -> int:
    existing = list(OUTPUT.glob("rst*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RST_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rss_video_index() -> int:
    existing = list(OUTPUT.glob("rss*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSS_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsp_video_index() -> int:
    existing = list(OUTPUT.glob("rsp*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSP_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsx_video_index() -> int:
    existing = list(OUTPUT.glob("rsx*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSX_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsy_video_index() -> int:
    existing = list(OUTPUT.glob("rsy*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSY_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsz_video_index() -> int:
    existing = list(OUTPUT.glob("rsz*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSZ_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsw_video_index() -> int:
    existing = list(OUTPUT.glob("rsw*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSW_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsv_video_index() -> int:
    existing = list(OUTPUT.glob("rsv*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSV_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rst2_video_index() -> int:
    existing = list(OUTPUT.glob("rst2*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RST2_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsq_video_index() -> int:
    existing = list(OUTPUT.glob("rsq*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSQ_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsk_video_index() -> int:
    existing = list(OUTPUT.glob("rsk*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSK_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsm_video_index() -> int:
    existing = list(OUTPUT.glob("rsm*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSM_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsr_video_index() -> int:
    existing = list(OUTPUT.glob("rsr*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSR_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsh_video_index() -> int:
    existing = list(OUTPUT.glob("rsh*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSH_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsu_video_index() -> int:
    existing = list(OUTPUT.glob("rsu*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSU_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsv2_video_index() -> int:
    existing = list(OUTPUT.glob("rsv2*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSV2_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_ptk2_video_index() -> int:
    existing = list(OUTPUT.glob("ptk2*.mp4"))
    nums = []
    for p in existing:
        m = _RE_PTK2_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsa_video_index() -> int:
    existing = list(OUTPUT.glob("rsa*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSA_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsb_video_index() -> int:
    existing = list(OUTPUT.glob("rsb*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSB_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rse_video_index() -> int:
    existing = list(OUTPUT.glob("rse*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSE_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsn_video_index() -> int:
    existing = list(OUTPUT.glob("rsn*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSN_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsp2_video_index() -> int:
    existing = list(OUTPUT.glob("rsp2*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSP2_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_orl_video_index() -> int:
    existing = list(OUTPUT.glob("orl*.mp4"))
    nums = []
    for p in existing:
        m = _RE_ORL_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsl_video_index() -> int:
    existing = list(OUTPUT.glob("rsl*.mp4"))
    nums = []
    for p in existing:
        m = _RE_RSL_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_r3u_video_index() -> int:
    existing = list(OUTPUT.glob("r3u*.mp4"))
    nums: list[int] = []
    for p in existing:
        m = _RE_R3U_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def next_rsg_video_index() -> int:
    existing = list(OUTPUT.glob("rsg*.mp4"))
    nums: list[int] = []
    for p in existing:
        stem = p.stem.lower()
        if not stem.startswith("rsg"):
            continue
        tail = stem[3:]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1


def next_rsd_video_index() -> int:
    existing = list(OUTPUT.glob("rsd*.mp4"))
    nums: list[int] = []
    for p in existing:
        stem = p.stem.lower()
        if not stem.startswith("rsd"):
            continue
        tail = stem[3:]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1


def next_rsi_video_index() -> int:
    existing = list(OUTPUT.glob("rsi*.mp4"))
    nums: list[int] = []
    for p in existing:
        stem = p.stem.lower()
        if not stem.startswith("rsi"):
            continue
        tail = stem[3:]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1


def next_rsj_video_index() -> int:
    existing = list(OUTPUT.glob("rsj*.mp4"))
    nums: list[int] = []
    for p in existing:
        stem = p.stem.lower()
        if not stem.startswith("rsj"):
            continue
        tail = stem[3:]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1


def next_rsk2_video_index() -> int:
    existing = list(OUTPUT.glob("rsk2*.mp4"))
    nums: list[int] = []
    for p in existing:
        stem = p.stem.lower()
        if not stem.startswith("rsk2"):
            continue
        tail = stem[4:]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1


def next_prefixed_video_index(prefix: str) -> int:
    """Next numeric suffix for ``{prefix}{n}.mp4`` in OUTPUT."""
    pl = prefix.lower()
    existing = list(OUTPUT.glob(f"{prefix}*.mp4"))
    nums: list[int] = []
    for p in existing:
        stem = p.stem.lower()
        if not stem.startswith(pl):
            continue
        tail = stem[len(pl) :]
        if tail.isdigit():
            nums.append(int(tail))
    return (max(nums) + 1) if nums else 1


def _serve_ttspeech_http(port_holder: list[int]) -> Callable[[], None]:
    """Serve ``folders/assets/ttspeech`` for optional browser-based Web Speech."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(TTSPEECH_DIR), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port_holder[0] = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def shutdown() -> None:
        httpd.shutdown()

    return shutdown


def optional_browser_tts_record(text: str, out_webm: Path) -> bool:
    """
    Optional: record Web Speech API (Google US English) via tab capture.
    Requires: pip install playwright, playwright install chrome, and Chrome.
    Set PTK_BROWSER_TTS=1. User may need to allow capture once.
    ``folders/assets/ttspeech/record_tts.html`` must expose ``window.__FIRSTSKY_RECORD__``.
    """
    if os.environ.get("PTK_BROWSER_TTS", "").lower() not in ("1", "true", "yes"):
        return False
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _warn("Playwright not installed; using local pyttsx3 TTS.")
        return False

    import base64

    port = [0]
    shutdown = _serve_ttspeech_http(port)
    url = f"http://127.0.0.1:{port[0]}/record_tts.html"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=[
                    "--auto-select-tab-capture-source-by-title=FirstSky TTS",
                    "--disable-features=AudioServiceOutOfProcess",
                ],
            )
            page = browser.new_page()
            page.goto(url, wait_until="domcontentloaded")
            b64 = page.evaluate(
                """async (txt) => {
                  const blob = await window.__FIRSTSKY_RECORD__(txt);
                  const ab = await blob.arrayBuffer();
                  const u8 = new Uint8Array(ab);
                  let s = '';
                  const chunk = 8192;
                  for (let i = 0; i < u8.length; i += chunk) {
                    s += String.fromCharCode.apply(null, u8.subarray(i, i + chunk));
                  }
                  return btoa(s);
                }""",
                text[:500_000],
            )
            browser.close()
        out_webm.write_bytes(base64.b64decode(b64))
        return True
    except Exception as e:
        _warn(f"Browser TTS failed: {e}")
        return False
    finally:
        shutdown()


# Extend pipeline entry if env requests browser TTS (writes webm then ffmpeg to mp3)
def text_to_speech_preferred(text: str, out_mp3: Path) -> None:
    webm = out_mp3.with_suffix(".webm")
    if optional_browser_tts_record(text, webm):
        try:
            if webm.is_file() and webm.stat().st_size > 1000:
                try:
                    wd = ffprobe_duration(webm)
                except Exception:
                    wd = 600.0
                _run_ffmpeg(
                    [
                        "ffmpeg",
                        "-y",
                        "-i",
                        str(webm),
                        "-c:a",
                        "libmp3lame",
                        "-q:a",
                        "2",
                        str(out_mp3),
                    ],
                    timeout=min(7200, max(120, int(wd * 4) + 120)),
                )
                return
        finally:
            webm.unlink(missing_ok=True)
    if _edge_tts_wants():
        try:
            text_to_speech_edge_mp3(text, out_mp3)
            return
        except ImportError:
            _warn("edge-tts not installed (pip install edge-tts); using pyttsx3.")
        except Exception as e:
            _warn(f"Edge TTS failed ({e}); using pyttsx3.")
    try:
        text_to_speech_mp3(text, out_mp3)
    except ModuleNotFoundError as e:
        if e.name == "pyttsx3":
            _warn(
                "pyttsx3 not installed — using gTTS (requires internet). "
                "For offline narration: pip install pyttsx3"
            )
            text_to_speech_gtts_mp3(text, out_mp3)
            return
        raise


# Wire preferred TTS into pipeline
def run_pipeline_tts() -> Path:
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="PTK_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 8

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting render")
        with _progress_stage(0, total_steps, "1/8 Fetch story candidates"):
            candidates = fetch_story_candidates()
            if not candidates:
                raise RuntimeError(
                    "No eligible story candidates (no long self-posts found, or all contained blocked terms)."
                )
            candidates = _filter_unused_story_candidates(candidates)
            if not candidates:
                raise RuntimeError(
                    f"No unused story candidates left. Expand sources or clear {USED_STORY_DB_FILE}."
                )

        with _progress_stage(1, total_steps, "2/8 Rank by virality signals"):
            ranked = _rank_stories_for_virality(candidates)
            if not ranked:
                raise RuntimeError("No ranked story candidates.")

        with _progress_stage(2, total_steps, "3/8 Weighted story pick"):
            remaining = list(ranked)
            post: dict[str, Any] | None = None
            text = ""
            while remaining:
                post = pick_story_from_ranked(remaining)
                text = build_narration_text(post)
                if extras.extra_keyword_blocked(text) or extras.narration_length_blocked(text):
                    _warn("Story blocked by PTK_BLOCK_* or length limits; trying another from pool…")
                    remaining = [p for p in remaining if _story_key(p) != _story_key(post)]
                    continue
                if not extras.human_story_approved(post, text):
                    remaining = [p for p in remaining if _story_key(p) != _story_key(post)]
                    if not remaining:
                        raise RuntimeError("No stories left after human rejections.")
                    continue
                break
            if not remaining or post is None:
                raise RuntimeError("No story passed filters (keywords / length / empty pool).")
            text = extras.maybe_translate_narration(text)
            extras.write_hook_variants(work, post, text)
            (work / "story.txt").write_text(text, encoding="utf-8")
            if extras.is_dry_run():
                extras.log_dry_run(post, text, n_cand=len(candidates), n_rank=len(ranked))
                raise DryRunComplete()

        narr = work / "narration_raw.mp3"
        with _progress_stage(3, total_steps, "4/8 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        # Voice-only track for subtitle timing (never the music mix).
        narr_voice_only = narr_for_mix

        mixed = work / "narration_mixed.mp3"
        with _progress_stage(4, total_steps, "5/8 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        extras.maybe_log_beat_sync_hints(mixed, work)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration
        with _progress_stage(5, total_steps, "6/8 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(6, total_steps, "7/8 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_video_index()
        out = OUTPUT / f"video{idx}.mp4"
        with _progress_stage(7, total_steps, f"8/8 Mux final .mp4 ({out.name})"):
            v_use = v_bg
            audio_use = mixed
            if _story_needs_disclaimer_audio(post):
                dyn_disclaimer = work / "disclaimer_dynamic.mp3"
                disclaimer_audio = dyn_disclaimer if _generate_disclaimer_tts_random_voice(dyn_disclaimer) else DISCLAIMER_MP3
                if disclaimer_audio.is_file():
                    try:
                        disc_d = ffprobe_duration(disclaimer_audio)
                    except Exception:
                        disc_d = 5.0
                    disc_d = max(0.05, float(disc_d))
                    audio_use = work / "audio_with_disclaimer.mp3"
                    _concat_audio_disclaimer_first(disclaimer_audio, mixed, audio_use)
                    v_use = work / "v_with_disclaimer_lead.mp4"
                    _prepend_black_video_lead(v_bg, disc_d, v_use)
                    _shift_srt_times(srt, disc_d)
                else:
                    _warn(f"Disclaimer audio missing ({DISCLAIMER_MP3}); skipping disclaimer prepend.")
            _set_mux_extra_meta(title=str(post.get("title") or ""), post_key=_story_key(post))
            try:
                mux_video_audio(v_use, audio_use, out, subtitles=srt)
            finally:
                _clear_mux_extra_meta()
            # Ensure no output sidecar SRT remains after render completion.
            (OUTPUT / f"video{idx}.srt").unlink(missing_ok=True)
            _record_used_story(post, out)
        _pipeline_progress(8, total_steps, "Render complete", complete=True)
        _ok_pop("Done")
        _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
        _step("Render finished — type re/ex")
        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rst() -> Path:
    """RST pipeline: today's historical events -> narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RST_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RST render")

        with _progress_stage(0, total_steps, "1/7 Fetch today's historical events"):
            events = fetch_today_history_events(max_items=5)
            if not events:
                raise RuntimeError("No history events returned by Wikipedia API.")
            text = build_rst_narration_text(events)
            (work / "rst_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rst_video_index()
        out = OUTPUT / f"rst{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rst{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RST render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rss() -> Path:
    """RSS pipeline: one dilemma post -> two-sides narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSS_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSS render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick dilemma story"):
            post = fetch_rss_dilemma_post()
            text = build_rss_narration_text(post)
            (work / "rss_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rss_video_index()
        out = OUTPUT / f"rss{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rss{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSS render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsp() -> Path:
    """RSP pipeline: one opinion post -> hot-take narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSP_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSP render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick opinion post"):
            post = fetch_rsp_opinion_post()
            text = build_rsp_narration_text(post)
            (work / "rsp_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsp_video_index()
        out = OUTPUT / f"rsp{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsp{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSP render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsx() -> Path:
    """RSX pipeline: one story -> red-flag checklist narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSX_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSX render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick conflict story"):
            post = fetch_rsx_story_post()
            text = build_rsx_narration_text(post)
            (work / "rsx_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsx_video_index()
        out = OUTPUT / f"rsx{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsx{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSX render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsy() -> Path:
    """RSY pipeline: one story -> myth-vs-fact narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSY_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSY render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsy_story_post()
            text = build_rsy_narration_text(post)
            (work / "rsy_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsy_video_index()
        out = OUTPUT / f"rsy{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsy{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSY render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_r3u() -> Path:
    """R3U pipeline: ordinary object -> 3 unknown facts narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="R3U_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting R3U render")

        with _progress_stage(0, total_steps, "1/7 Build 3-unknowns script"):
            text = build_r3u_narration_text()
            (work / "r3u_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_r3u_video_index()
        out = OUTPUT / f"r3u{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"r3u{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("R3U render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsz() -> Path:
    """RSZ pipeline: one story -> lesson-learned narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSZ_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSZ render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsz_story_post()
            text = build_rsz_narration_text(post)
            (work / "rsz_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsz_video_index()
        out = OUTPUT / f"rsz{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsz{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSZ render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsw() -> Path:
    """RSW pipeline: one story -> what-would-you-do narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSW_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSW render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsw_story_post()
            text = build_rsw_narration_text(post)
            (work / "rsw_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsw_video_index()
        out = OUTPUT / f"rsw{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsw{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSW render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsv() -> Path:
    """RSV pipeline: one story -> quick advice narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSV_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSV render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsv_story_post()
            text = build_rsv_narration_text(post)
            (work / "rsv_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsv_video_index()
        out = OUTPUT / f"rsv{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsv{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSV render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rst2() -> Path:
    """RST2 pipeline: one story -> timeline narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RST2_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RST2 render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rst2_story_post()
            text = build_rst2_narration_text(post)
            (work / "rst2_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rst2_video_index()
        out = OUTPUT / f"rst2{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rst2{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RST2 render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsq() -> Path:
    """RSQ pipeline: one story -> quote-centric narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSQ_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSQ render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsq_story_post()
            text = build_rsq_narration_text(post)
            (work / "rsq_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsq_video_index()
        out = OUTPUT / f"rsq{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsq{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSQ render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsk() -> Path:
    """RSK pipeline: one story -> key-takeaway narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSK_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSK render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsk_story_post()
            text = build_rsk_narration_text(post)
            (work / "rsk_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsk_video_index()
        out = OUTPUT / f"rsk{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsk{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSK render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsm() -> Path:
    """RSM pipeline: one story -> mistake-to-avoid narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSM_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSM render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsm_story_post()
            text = build_rsm_narration_text(post)
            (work / "rsm_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsm_video_index()
        out = OUTPUT / f"rsm{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsm{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSM render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsr() -> Path:
    """RSR pipeline: one story -> reality-check narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSR_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSR render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsr_story_post()
            text = build_rsr_narration_text(post)
            (work / "rsr_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsr_video_index()
        out = OUTPUT / f"rsr{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsr{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSR render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsh() -> Path:
    """RSH pipeline: one story -> hard-truth narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSH_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSH render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsh_story_post()
            text = build_rsh_narration_text(post)
            (work / "rsh_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsh_video_index()
        out = OUTPUT / f"rsh{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsh{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSH render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsu() -> Path:
    """RSU pipeline: one story -> unpopular-take narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSU_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSU render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsu_story_post()
            text = build_rsu_narration_text(post)
            (work / "rsu_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsu_video_index()
        out = OUTPUT / f"rsu{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsu{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSU render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsv2() -> Path:
    """RSV2 pipeline: one story -> verdict narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSV2_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSV2 render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsv2_story_post()
            text = build_rsv2_narration_text(post)
            (work / "rsv2_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsv2_video_index()
        out = OUTPUT / f"rsv2{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsv2{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSV2 render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_ptk2() -> Path:
    """PTK2 pipeline: one story -> cold-take narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="PTK2_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting PTK2 render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_ptk2_story_post()
            text = build_ptk2_narration_text(post)
            (work / "ptk2_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_ptk2_video_index()
        out = OUTPUT / f"ptk2{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"ptk2{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("PTK2 render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsa() -> Path:
    """RSA pipeline: one story -> accountability narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSA_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSA render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsa_story_post()
            text = build_rsa_narration_text(post)
            (work / "rsa_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsa_video_index()
        out = OUTPUT / f"rsa{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsa{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSA render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsb() -> Path:
    """RSB pipeline: one story -> boundary-check narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSB_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSB render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsb_story_post()
            text = build_rsb_narration_text(post)
            (work / "rsb_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsb_video_index()
        out = OUTPUT / f"rsb{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsb{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSB render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rse() -> Path:
    """RSE pipeline: one story -> empathy-check narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSE_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSE render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rse_story_post()
            text = build_rse_narration_text(post)
            (work / "rse_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rse_video_index()
        out = OUTPUT / f"rse{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rse{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSE render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsn() -> Path:
    """RSN pipeline: one story -> negotiation-check narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSN_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSN render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsn_story_post()
            text = build_rsn_narration_text(post)
            (work / "rsn_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsn_video_index()
        out = OUTPUT / f"rsn{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsn{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSN render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsp2() -> Path:
    """RSP2 pipeline: one story -> perspective-shift narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSP2_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSP2 render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsp2_story_post()
            text = build_rsp2_narration_text(post)
            (work / "rsp2_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsp2_video_index()
        out = OUTPUT / f"rsp2{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsp2{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSP2 render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


@contextlib.contextmanager
def _orl_english_tts_only() -> Iterator[None]:
    """
    ORL narration is English-only: force gTTS/lang env, and drop non-English Edge voice IDs
    (e.g. ``fr-FR-…``) that would otherwise bypass the English neural voice list.
    """
    keys = ("PTK_GTTS_LANG", "PTK_TTS_LANG", "PTK_TTS_VOICE")
    saved: dict[str, str | None] = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["PTK_GTTS_LANG"] = "en"
        os.environ["PTK_TTS_LANG"] = "en"
        vo = (saved.get("PTK_TTS_VOICE") or "").strip()
        if vo and re.match(r"^[a-z]{2}[-_][A-Za-z]{2}[-_]", vo) and not re.match(r"(?i)^en[-_]", vo):
            os.environ.pop("PTK_TTS_VOICE", None)
        yield
    finally:
        for k, prev in saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def run_pipeline_orl() -> Path:
    """ORL pipeline: English Wikipedia science article -> 9:16 slideshow + English narration."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="ORL_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7
    bundle: dict[str, Any] = {}

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting ORL render")

        with _progress_stage(0, total_steps, "1/7 Fetch English Wikipedia science article"):
            bundle = fetch_orl_wikipedia_bundle()
            text = build_orl_narration_text(bundle)
            (work / "orl_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio (English)"):
            with _orl_english_tts_only():
                text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build 9:16 Wikipedia image slideshow"):
            urls = list(bundle.get("image_urls") or [])
            v_bg = build_orl_wiki_slideshow(urls, duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_orl_video_index()
        out = OUTPUT / f"orl{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"orl{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("ORL render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsl() -> Path:
    """RSL pipeline: one story -> leverage-check narrated short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSL_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSL render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsl_story_post()
            text = build_rsl_narration_text(post)
            (work / "rsl_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsl_video_index()
        out = OUTPUT / f"rsl{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsl{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSL render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsg() -> Path:
    """RSG pipeline: one uplifting story -> good-news spotlight short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSG_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSG render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsg_story_post()
            text = build_rsg_narration_text(post)
            (work / "rsg_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsg_video_index()
        out = OUTPUT / f"rsg{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsg{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSG render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsd() -> Path:
    """RSD pipeline: one conflict story -> plot-twist drama short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSD_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSD render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsd_story_post()
            text = build_rsd_narration_text(post)
            (work / "rsd_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsd_video_index()
        out = OUTPUT / f"rsd{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsd{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSD render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsi() -> Path:
    """RSI pipeline: one curiosity story -> interesting-find spotlight short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSI_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSI render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsi_story_post()
            text = build_rsi_narration_text(post)
            (work / "rsi_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsi_video_index()
        out = OUTPUT / f"rsi{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsi{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSI render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsj() -> Path:
    """RSJ pipeline: one science story -> wonder spotlight short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSJ_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSJ render")

        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsj_story_post()
            text = build_rsj_narration_text(post)
            (work / "rsj_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_rsj_video_index()
        out = OUTPUT / f"rsj{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsj{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSJ render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_rsk2() -> Path:
    """RSK2 pipeline: one history mystery story -> spotlight short with subtitles."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix="RSK2_", dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7
    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, "Starting RSK2 render")
        with _progress_stage(0, total_steps, "1/7 Fetch and pick story"):
            post = fetch_rsk2_story_post()
            text = build_rsk2_narration_text(post)
            (work / "rsk2_script.txt").write_text(text, encoding="utf-8")
        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast
        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)
        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration
        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)
        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)
        idx = next_rsk2_video_index()
        out = OUTPUT / f"rsk2{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            mux_video_audio(v_bg, mixed, out, subtitles=srt)
            (OUTPUT / f"rsk2{idx}.srt").unlink(missing_ok=True)
        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step("RSK2 render finished — type re/ex")
        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


_TPOV_FFV_TAIL_SEC = 1.5


def _tpov_build_ffv_reaction_sfx_tail_mp4(work: Path) -> Path | None:
    """
    FFV-style tail: two **paired** reaction stills (distinct Reddit picks), then MyInstants SFX,
    total ``_TPOV_FFV_TAIL_SEC`` seconds vertical video. Requires network (Reddit pools + SFX host).
    """
    try:
        from ffv.engine import (
            FFVProjectState,
            TheoryCorpus,
            ViralityIndex,
            _record_unique_keys,
            collect_pool,
            concat_mp4_list,
            ffv_order_subs_for_image_fetch,
            download_bytes,
            download_sfx,
            image_to_video_segment,
            mux_segment_audio,
            plan_segment,
            weighted_pick_ranked,
            _ffv_maybe_demux_still_to_jpeg,
            _ffv_normalize_image_file,
            _url_ext,
        )
    except Exception:
        return None

    funny = os.environ.get("FFV_FUNNY", "").strip().lower() in ("1", "true", "yes", "on")
    try:
        corpus = TheoryCorpus.load()
        if funny:
            corpus = corpus.as_funny()
    except Exception:
        return None

    try:
        subs_r = ffv_order_subs_for_image_fetch(list(corpus.reactable_subs), reaction=False)
        subs_i = ffv_order_subs_for_image_fetch(list(corpus.reaction_subs), reaction=True)
        pool_r = collect_pool(corpus, subs_r, per_sub=28)
        pool_i = collect_pool(corpus, subs_i, per_sub=28)
    except Exception:
        return None
    if len(pool_r) < 2 or len(pool_i) < 2:
        return None

    state = FFVProjectState(
        seed=None,
        session_id=uuid.uuid4().hex[:12],
        theory_corpus_digest=corpus.digest(),
        corpus_preset="lol" if funny else "theory",
    )
    try:
        bp = plan_segment(corpus, pool_r, pool_i, 0, state)
    except Exception:
        return None

    sfx_url = (bp.sfx_url or "").strip()
    react_url = (bp.reaction.url or "").strip()
    if not sfx_url or not react_url:
        return None

    rr2: dict[str, Any] | None = None
    try:
        rr2, _, _ = weighted_pick_ranked(
            pool_i,
            ViralityIndex.score,
            exclude_ids=frozenset(_record_unique_keys(bp.reaction)),
            relax_if_small=False,
        )
    except RuntimeError:
        rr2 = None
    react_url_b = (
        str((rr2 or {}).get("_ffv_image_url") or (rr2 or {}).get("url") or "").strip() if rr2 else ""
    )
    half = _TPOV_FFV_TAIL_SEC * 0.5
    use_pair = bool(react_url_b) and react_url_b.lower() != react_url.lower()

    stem = f"tpov_tail_{uuid.uuid4().hex[:10]}"
    out_mp4 = work / f"{stem}.mp4"
    silent = work / f"{stem}_silent.mp4"

    def _fetch_re_still(url: str, tag: str) -> Path | None:
        ext = _url_ext(url)
        raw = work / f"{stem}_{tag}_raw{ext}"
        if not download_bytes(url, raw):
            return None
        img = _ffv_normalize_image_file(raw)
        return _ffv_maybe_demux_still_to_jpeg(img, work, f"{stem}_{tag}", ext)

    try:
        re_img_a = _fetch_re_still(react_url, "re_a")
        if re_img_a is None:
            return None
        if use_pair:
            re_img_b = _fetch_re_still(react_url_b, "re_b")
            if re_img_b is None:
                use_pair = False
        if use_pair:
            p1 = work / f"{stem}_re_a.mp4"
            p2 = work / f"{stem}_re_b.mp4"
            image_to_video_segment(
                re_img_a,
                half,
                p1,
                OUTPUT_W,
                OUTPUT_H,
                fade_in=True,
                fade_out=True,
                motion=None,
                work=work,
            )
            image_to_video_segment(
                re_img_b,
                half,
                p2,
                OUTPUT_W,
                OUTPUT_H,
                fade_in=True,
                fade_out=True,
                motion=None,
                work=work,
            )
            concat_mp4_list([p1, p2], silent)
        else:
            image_to_video_segment(
                re_img_a,
                _TPOV_FFV_TAIL_SEC,
                silent,
                OUTPUT_W,
                OUTPUT_H,
                fade_in=True,
                fade_out=True,
                motion=None,
                work=work,
            )
        sfx = download_sfx(sfx_url, work, f"{stem}_sfx")
        if sfx is None or not sfx.is_file():
            return None
        mux_segment_audio(
            silent,
            sfx,
            None,
            out_mp4,
            _TPOV_FFV_TAIL_SEC,
            sfx_delay_sec=0.0,
        )
    except Exception:
        return None
    return out_mp4 if out_mp4.is_file() and out_mp4.stat().st_size > 64 else None


def _tpov_concat_body_and_tail_in_output_dir(body: Path, tail: Path, out: Path) -> None:
    """``concat_videos`` only resolves clips under ``out.parent``; stage copies there then concat."""
    uid = uuid.uuid4().hex[:12]
    out_dir = out.parent.resolve()
    b = out_dir / f"_tpov_concat_body_{uid}.mp4"
    t = out_dir / f"_tpov_concat_tail_{uid}.mp4"
    try:
        shutil.copy2(body, b)
        shutil.copy2(tail, t)
        concat_videos([b, t], out)
    finally:
        b.unlink(missing_ok=True)
        t.unlink(missing_ok=True)


def _run_teen_format_pipeline(
    *,
    file_prefix: str,
    temp_prefix: str,
    display_name: str,
    script_builder: Callable[[], str],
    append_ffv_reaction_sfx_tail: bool = False,
) -> Path:
    """Template-only teen short: TTS + mix + b-roll + subs + mux (no Reddit, no features extras)."""
    global _PROGRESS_ACTIVE, _PIPELINE_T0
    _ensure_dirs()
    work = Path(tempfile.mkdtemp(prefix=temp_prefix, dir=TEMP))
    completed = False
    out: Path | None = None
    total_steps = 7

    try:
        _PROGRESS_ACTIVE = True
        _PIPELINE_T0 = time.time()
        _pipeline_progress(0.0, total_steps, f"Starting {display_name} render")

        with _progress_stage(0, total_steps, f"1/7 Generate {display_name} script"):
            text = script_builder()
            (work / f"{file_prefix}_script.txt").write_text(text, encoding="utf-8")

        narr = work / "narration_raw.mp3"
        with _progress_stage(1, total_steps, "2/7 Generate narration audio"):
            text_to_speech_preferred(text, narr)
            narr_fast = work / "narration_fast.mp3"
            speed_up_audio(narr, narr_fast, NARRATION_SPEED)
            narr_for_mix: Path = narr_fast

        narr_voice_only = narr_for_mix
        mixed = work / "narration_mixed.mp3"
        with _progress_stage(2, total_steps, "3/7 Mix narration + background music"):
            bg = fetch_random_incompetech_music(work / "incompetech_bg.mp3")
            if bg is None:
                try:
                    narr_d = ffprobe_duration(narr_for_mix)
                except Exception:
                    narr_d = 120.0
                bg = synth_fallback_bg_music(narr_d, work / "fallback_bg.mp3")
            mix_narration_and_music(narr_for_mix, bg, mixed)

        duration = ffprobe_duration(mixed)
        try:
            narr_subtitle_d = max(0.5, float(ffprobe_duration(narr_voice_only)))
        except Exception:
            narr_subtitle_d = duration

        with _progress_stage(3, total_steps, "4/7 Build background b-roll video"):
            v_bg = build_background_video(duration, work)

        srt = work / "narration.srt"
        with _progress_stage(4, total_steps, "5/7 Subtitles (word-by-word timing)"):
            if FAST_RENDER_MODE and AGGRESSIVE_MODE:
                write_word_by_word_srt_fast(text, narr_subtitle_d, srt)
            elif not try_write_whisper_srt(narr_voice_only, srt):
                if not try_write_pause_based_srt(text, narr_voice_only, narr_subtitle_d, srt):
                    write_proportional_srt(text, narr_subtitle_d, srt)

        idx = next_prefixed_video_index(file_prefix)
        out = OUTPUT / f"{file_prefix}{idx}.mp4"
        with _progress_stage(5, total_steps, f"6/7 Mux final .mp4 ({out.name})"):
            if append_ffv_reaction_sfx_tail:
                body = work / "teen_main_muxed.mp4"
                mux_video_audio(v_bg, mixed, body, subtitles=srt)
                (OUTPUT / f"{file_prefix}{idx}.srt").unlink(missing_ok=True)
                tail = _tpov_build_ffv_reaction_sfx_tail_mp4(work)
                if tail is not None and tail.is_file():
                    _tpov_concat_body_and_tail_in_output_dir(body, tail, out)
                else:
                    _warn(
                        f"{display_name} reaction + SFX tail skipped (network or FFV assets); main video only."
                    )
                    shutil.copy2(body, out)
            else:
                mux_video_audio(v_bg, mixed, out, subtitles=srt)
                (OUTPUT / f"{file_prefix}{idx}.srt").unlink(missing_ok=True)

        with _progress_stage(6, total_steps, "7/7 Finalize output"):
            _pipeline_progress(7, total_steps, "Render complete", complete=True)
            _ok_pop("Done")
            _info(f"Output: {C_OK}{out.resolve()}{C_RESET}")
            _step(f"{display_name} render finished — type re/ex")

        completed = True
        return out
    finally:
        _PROGRESS_ACTIVE = False
        _PIPELINE_T0 = None
        if completed:
            shutil.rmtree(work, ignore_errors=True)
            _wipe_assets_tree()
        else:
            _cleanup_incomplete_session(work, out)
            if sys.exc_info()[1] is not None:
                _warn("Cleaned up incomplete run (temp dir and partial video).")


def run_pipeline_twao() -> Path:
    """Wrong answers only (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="twao",
        temp_prefix="TWAO_",
        display_name="TWAO",
        script_builder=teen_formats.script_wrong_answers_only,
    )


def run_pipeline_tpov() -> Path:
    """POV brain moment (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tpov",
        temp_prefix="TPOV_",
        display_name="TPOV",
        script_builder=teen_formats.script_pov_brain,
        append_ffv_reaction_sfx_tail=True,
    )


def run_pipeline_trate() -> Path:
    """Yelp-style rating (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="trate",
        temp_prefix="TRATE_",
        display_name="TRATE",
        script_builder=teen_formats.script_rating_review,
    )


def run_pipeline_tobj() -> Path:
    """One-object story (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tobj",
        temp_prefix="TOBJ_",
        display_name="TOBJ",
        script_builder=teen_formats.script_one_object_story,
    )


def run_pipeline_tset() -> Path:
    """Honest settings parody (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tset",
        temp_prefix="TSET_",
        display_name="TSET",
        script_builder=teen_formats.script_honest_settings,
    )


def run_pipeline_tmus() -> Path:
    """Museum plaque 2045 (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tmus",
        temp_prefix="TMUS_",
        display_name="TMUS",
        script_builder=teen_formats.script_museum_2045,
    )


def run_pipeline_tsil() -> Path:
    """Silent / captions skit script (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tsil",
        temp_prefix="TSIL_",
        display_name="TSIL",
        script_builder=teen_formats.script_silent_captions,
    )


def run_pipeline_tspd() -> Path:
    """School-day speedrun commentary (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tspd",
        temp_prefix="TSPD_",
        display_name="TSPD",
        script_builder=teen_formats.script_speedrun_school,
    )


def run_pipeline_tcre() -> Path:
    """Small-creator (50–500 subs) petty online beef storytime (teen format)."""
    return _run_teen_format_pipeline(
        file_prefix="tcre",
        temp_prefix="TCRE_",
        display_name="TCRE",
        script_builder=teen_formats.script_small_creator_wronged,
    )


def _run_generic_re_render_shell(run_n: Callable[[int], bool]) -> None:
    """start/re loop for template pipelines."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not run_n(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if run_n(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_twao_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_twao, label="twao render"))


def _run_tpov_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tpov, label="tpov render"))


def _run_trate_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_trate, label="trate render"))


def _run_tobj_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tobj, label="tobj render"))


def _run_tset_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tset, label="tset render"))


def _run_tmus_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tmus, label="tmus render"))


def _run_tsil_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tsil, label="tsil render"))


def _run_tspd_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tspd, label="tspd render"))


def _run_tcre_interactive_shell() -> None:
    _run_generic_re_render_shell(lambda c: _run_n_renders_with(c, run_pipeline_tcre, label="tcre render"))


def _next_video_index_in(out_dir: Path) -> int:
    existing = list(out_dir.glob("video*.mp4"))
    nums: list[int] = []
    for p in existing:
        m = _RE_VIDEO_NUM.match(p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


def run_asm_player() -> None:
    """Run the external Ascii-Media-Player (sibling repo or ``ASCII_MEDIA_PLAYER_ROOT``)."""
    from .asm_player import launch_ascii_media_player

    try:
        code = launch_ascii_media_player()
    except OSError as e:
        _err(f"Could not start Ascii-Media-Player: {e}")
        return
    except FileNotFoundError as e:
        _err(str(e))
        return
    if code != 0:
        _warn(f"Ascii-Media-Player exited with code {code}.")


def _print_cli_help() -> None:
    print(
        f"""FirstSky {__version__} - short video toolkit

Usage:
  python main.py                Tool menu (PTK, ORL, R3U, FFV, ASM), then start / re / ex
  python main.py --once         Render one PTK video and exit
  python main.py --asm [args]  Run Ascii-Media-Player (sibling ../Ascii-Media-Player; see README)
  python main.py --preset NAME  Load folders/presets/NAME.json (env overrides) before run
  python main.py --queue FILE   Run batch lines (see python main.py --extras-help)
  python main.py --dashboard    Regenerate output/dashboard/index.html (repo root)
  python main.py --save-preset NAME   Save current PTK_* / FFV_* env to presets/NAME.json
  python main.py --extras-help  Optional features (preview, filters, dry-run, upload copy, ...)
  python main.py --help         Show this text
  python main.py --version      Print version

Dependencies: FFmpeg, Python packages in requirements.txt.
Ctrl+C during a render removes that run's temp files and partial output.

TTS: PTK_TTS_VOICE unset = first voice; random / numeric index / substring (name or Neural id).
  pyttsx3 (default): add voices in Windows Settings -> Speech, or macOS Accessibility -> Spoken Content.
  edge-tts (neural, online): pip install edge-tts, then PTK_EDGE_TTS=1 or PTK_TTS_ENGINE=edge
  (many en-* Neural voices in EDGE_TTS_ENGLISH_NEURAL; unset PTK_TTS_VOICE uses en-US-GuyNeural, with fallbacks if unavailable).
  PTK_TTS_ENGINE=auto uses edge if installed.
  Clarity: default Edge rate EDGE_TTS_DEFAULT_RATE (-6%); override PTK_EDGE_TTS_RATE (use 0 for +0%).
  FFmpeg speech EQ on narration after TTS: disable with PTK_NARRATION_CLEAR_FILTERS=0.

Reddit JSON: default User-Agent is random (fake-useragent / built-in pool). If Reddit returns HTTP 403, try
  PTK_UA_COMPLIANT=1 for python:firstsky:... style. PTK_UA=... overrides everything. Blocked subs are skipped.

FFV (tool menu -> FFV): reactable image + reaction image + MyInstants SFX (see theory.txt). In the FFV prompt,
  type funny for the LOL preset - curated funny/wtf subs, meme reaction pools, SFX list front-loaded with meme hits
  (vine boom, bruh, oof, ...). Type theory, default, or reset to use the full theory corpus again.
  Set FFV_FUNNY=1 before launching this app to open FFV already in LOL mode. Other knobs: FFV_* in ffv/catalog.py (catalog command inside FFV).

R3U (tool menu -> R3U): "3 unknowns" short-form facts about one ordinary object per run.

ASM (tool menu -> ASM): external Ascii-Media-Player (clone next to Cuetilities or set ASCII_MEDIA_PLAYER_ROOT).

"""
    )


_RENDER_FOREVER = -1


def _parse_batch_command(cmd: str, verb: str) -> int | None:
    """
    Parse ``start``, ``start(n)``, ``start(*)`` (and same for ``re``).
    Returns ``_RENDER_FOREVER`` for ``(*)`` infinite loop until Ctrl+C.
    """
    c = " ".join((cmd or "").strip().lower().split())
    if c == verb:
        return 1
    if not (c.startswith(f"{verb}(") and c.endswith(")")):
        return None
    inner = c[len(verb) + 1 : -1].strip()
    if inner == "*":
        return _RENDER_FOREVER
    if inner.isdigit():
        n = int(inner)
        return n if n >= 1 else None
    return None


def _parse_start_count(cmd: str) -> int | None:
    return _parse_batch_command(cmd, "start")


def _parse_re_count(cmd: str) -> int | None:
    return _parse_batch_command(cmd, "re")


def _run_n_renders_with(count: int, render_fn: Callable[[], Path], *, label: str = "render") -> bool:
    """Run ``count`` renders, or forever if ``count == _RENDER_FOREVER``. Returns True when done."""
    if count == _RENDER_FOREVER:
        n = 0
        while True:
            n += 1
            _step(f"Continuous {label} #{n} (Ctrl+C to stop)")
            try:
                render_fn()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            except DryRunComplete:
                _info("Dry run finished (no video written).")
            except Exception as e:
                _err(f"Error during continuous {label} #{n}: {e}")
                return False
    total = max(1, int(count))
    for i in range(total):
        if total > 1:
            _step(f"Batch {label} {i + 1}/{total}")
        try:
            render_fn()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        except DryRunComplete:
            _info("Dry run finished (no video written).")
        except Exception as e:
            _err(f"Error during {label} {i + 1}/{total}: {e}")
            return False
    return True


def _run_n_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_tts, label="render")


def _run_n_rst_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rst, label="rst render")


def _run_n_rss_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rss, label="rss render")


def _run_n_rsp_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsp, label="rsp render")


def _run_n_rsx_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsx, label="rsx render")


def _run_n_rsy_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsy, label="rsy render")


def _run_n_r3u_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_r3u, label="r3u render")


def _run_n_rsz_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsz, label="rsz render")


def _run_n_rsw_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsw, label="rsw render")


def _run_n_rsv_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsv, label="rsv render")


def _run_n_rst2_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rst2, label="rst2 render")


def _run_n_rsq_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsq, label="rsq render")


def _run_n_rsk_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsk, label="rsk render")


def _run_n_rsm_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsm, label="rsm render")


def _run_n_rsr_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsr, label="rsr render")


def _run_n_rsh_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsh, label="rsh render")


def _run_n_rsu_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsu, label="rsu render")


def _run_n_rsv2_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsv2, label="rsv2 render")


def _run_n_ptk2_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_ptk2, label="ptk2 render")


def _run_n_rsa_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsa, label="rsa render")


def _run_n_rsb_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsb, label="rsb render")


def _run_n_rse_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rse, label="rse render")


def _run_n_rsn_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsn, label="rsn render")


def _run_n_rsp2_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsp2, label="rsp2 render")


def _run_n_orl_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_orl, label="orl render")


def _run_n_rsl_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsl, label="rsl render")


def _run_n_rsg_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsg, label="rsg render")


def _run_n_rsd_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsd, label="rsd render")


def _run_n_rsi_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsi, label="rsi render")


def _run_n_rsj_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsj, label="rsj render")


def _run_n_rsk2_renders(count: int) -> bool:
    return _run_n_renders_with(count, run_pipeline_rsk2, label="rsk2 render")


def _run_ptk_interactive_shell() -> None:
    """PTK render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rst_interactive_shell() -> None:
    """RST render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rst_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rst_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rss_interactive_shell() -> None:
    """RSS render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rss_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rss_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsp_interactive_shell() -> None:
    """RSP render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsp_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsp_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsx_interactive_shell() -> None:
    """RSX render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsx_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsx_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsy_interactive_shell() -> None:
    """RSY render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsy_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsy_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_r3u_interactive_shell() -> None:
    """R3U render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_r3u_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_r3u_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsz_interactive_shell() -> None:
    """RSZ render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsz_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsz_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsw_interactive_shell() -> None:
    """RSW render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsw_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsw_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsv_interactive_shell() -> None:
    """RSV render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsv_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsv_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rst2_interactive_shell() -> None:
    """RST2 render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rst2_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rst2_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsq_interactive_shell() -> None:
    """RSQ render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsq_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsq_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsk_interactive_shell() -> None:
    """RSK render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsk_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsk_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsm_interactive_shell() -> None:
    """RSM render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsm_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsm_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsr_interactive_shell() -> None:
    """RSR render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsr_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsr_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsh_interactive_shell() -> None:
    """RSH render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsh_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsh_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsu_interactive_shell() -> None:
    """RSU render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsu_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsu_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsv2_interactive_shell() -> None:
    """RSV2 render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsv2_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsv2_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_ptk2_interactive_shell() -> None:
    """PTK2 render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_ptk2_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_ptk2_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsa_interactive_shell() -> None:
    """RSA render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsa_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsa_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsb_interactive_shell() -> None:
    """RSB render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsb_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsb_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rse_interactive_shell() -> None:
    """RSE render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rse_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rse_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsn_interactive_shell() -> None:
    """RSN render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsn_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsn_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsp2_interactive_shell() -> None:
    """RSP2 render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsp2_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsp2_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_orl_interactive_shell() -> None:
    """ORL render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_orl_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_orl_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsl_interactive_shell() -> None:
    """RSL render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsl_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsl_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsg_interactive_shell() -> None:
    """RSG render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsg_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsg_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsd_interactive_shell() -> None:
    """RSD render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsd_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsd_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsi_interactive_shell() -> None:
    """RSI render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsi_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsi_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsj_interactive_shell() -> None:
    """RSJ render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsj_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsj_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')


def _run_rsk2_interactive_shell() -> None:
    """RSK2 render loop; ``ex`` returns to the tool picker."""
    while True:
        try:
            cmd = input(_prompt_start()).strip().lower()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        if cmd == "ex":
            return
        count = _parse_start_count(cmd)
        if count is None:
            if cmd:
                _warn('Unknown command. Use "start", "start(n)", "start(*)", or "ex".')
            continue
        if not _run_n_rsk2_renders(count):
            continue
        while True:
            try:
                nxt = input(_prompt_re_ex()).strip().lower()
            except KeyboardInterrupt:
                _exit_on_interrupt()
            if nxt == "ex":
                return
            if nxt == "re":
                break
            re_spec = _parse_re_count(nxt)
            if re_spec is not None:
                if _run_n_rsk2_renders(re_spec):
                    break
                continue
            if nxt:
                _warn('Unknown command. Use "re", "re(n)", "re(*)", or "ex".')




def main() -> None:
    args = sys.argv[1:]
    run_once = False
    queue_path: Path | None = None
    preset_arg: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        lo = a.lower()
        if lo in ("-h", "--help", "help"):
            _print_cli_help()
            return
        if lo == "--extras-help":
            print(extras.extras_help_text())
            return
        if lo == "--dashboard":
            _ensure_dirs()
            p = extras.refresh_dashboard()
            print(f"Wrote {p}")
            return
        if lo == "--save-preset" and i + 1 < len(args):
            _ensure_dirs()
            out = extras.save_preset(args[i + 1])
            print(f"Saved {out}")
            return
        if lo == "--preset" and i + 1 < len(args):
            preset_arg = args[i + 1]
            i += 2
            continue
        if lo == "--queue" and i + 1 < len(args):
            queue_path = Path(args[i + 1])
            i += 2
            continue
        if lo in ("-V", "--version", "version"):
            print(__version__)
            return
        if lo == "--once":
            run_once = True
            i += 1
            continue
        if lo == "--asm":
            from .asm_player import launch_ascii_media_player

            rest = args[i + 1 :]
            try:
                code = launch_ascii_media_player(extra_args=rest)
            except (FileNotFoundError, OSError) as e:
                _err(str(e))
                sys.exit(1)
            sys.exit(int(code))
        _err(f"Unknown argument {a!r}.")
        print(f"Try: python {Path(sys.argv[0]).name} --help")
        sys.exit(2)

    _ensure_dirs()
    if preset_arg:
        extras.load_preset(preset_arg)
    if queue_path is not None:
        try:
            extras.run_queue(queue_path)
        except KeyboardInterrupt:
            _exit_on_interrupt()
        return
    if run_once:
        try:
            run_pipeline_tts()
        except KeyboardInterrupt:
            _exit_on_interrupt()
        except DryRunComplete:
            pass
        return
    while True:
        choice = _prompt_tool_menu_choice()
        if choice == "quit":
            return
        if choice == "ffv":
            _launch_ffv()
            continue
        if choice == "orl":
            _print_banner()
            _run_orl_interactive_shell()
            continue
        if choice == "r3u":
            _print_banner()
            _run_r3u_interactive_shell()
            continue
        if choice == "asm":
            _launch_asm()
            continue
        _print_banner()
        _run_ptk_interactive_shell()

