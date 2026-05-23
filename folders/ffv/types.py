"""
FFV static typing surface: protocols and aliases used across the reaction pipeline.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Protocol, TypeAlias, runtime_checkable

# ---------------------------------------------------------------------------
# Reddit JSON (partial) — we only touch a small slice of the listing payload.
# ---------------------------------------------------------------------------

RedditChildData: TypeAlias = dict[str, Any]
RedditPool: TypeAlias = list[RedditChildData]

# ---------------------------------------------------------------------------
# Callable contracts
# ---------------------------------------------------------------------------


class SupportsFFmpeg(Protocol):
    def __call__(self, cmd: list[str], *, timeout: float | None = None) -> None: ...


class SupportsWhich(Protocol):
    def __call__(self, cmd: str) -> str | None: ...


class ViralityScorer(Protocol):
    @staticmethod
    def score(child: RedditChildData) -> float: ...


@runtime_checkable
class AuditSink(Protocol):
    def write_event(self, session_dir: Path, event: dict[str, Any]) -> None: ...


class WeightPicker(Protocol):
    def __call__(
        self,
        rows: RedditPool,
        key_fn: Callable[[RedditChildData], float],
        *,
        exclude_ids: frozenset[str] | None,
    ) -> tuple[RedditChildData, int, float]: ...


class SegmentPlanner(Protocol):
    def plan(
        self,
        index: int,
        react_pool: RedditPool,
        react_img_pool: RedditPool,
        *,
        exclude_react: frozenset[str] | None,
        exclude_re: frozenset[str] | None,
    ) -> Any: ...


class ImageNormalizer(Protocol):
    def __call__(self, path: Path) -> Path: ...


class SfxResolver(Protocol):
    def resolve(self, page_url: str, work: Path, stem: str) -> Path | None: ...

