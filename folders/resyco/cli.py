"""CLI entrypoint."""
from __future__ import annotations

from .impl import main as _impl_main
from .ui import _exit_on_interrupt


def main() -> None:
    try:
        _impl_main()
    except KeyboardInterrupt:
        _exit_on_interrupt()


__all__ = ["main"]
