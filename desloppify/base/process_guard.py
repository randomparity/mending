"""Refuse process creation while a guard is open (#75, ADR 0017).

CPython raises these audit events before it starts a program, so a refused call
runs nothing. An installed hook cannot be removed; it acts only while open.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager

# os.fork: os.spawn* forks first and execs in the child, out of the parent's count.
_REFUSED_EVENTS = frozenset({
    "os.exec", "os.fork", "os.forkpty", "os.posix_spawn", "os.spawn", "os.startfile",
    "os.system", "pty.spawn", "subprocess.Popen",
})
_installed = False
_depth = 0
_refusals = 0


class ExternalToolRefused(PermissionError):
    """Raised instead of starting a program while process creation is denied."""


def _audit(event: str, _args: tuple[object, ...]) -> None:
    global _refusals
    if _depth and event in _REFUSED_EVENTS:
        _refusals += 1
        raise ExternalToolRefused(f"external tools are disabled for this scan ({event})")


@contextmanager
def deny_process_creation() -> Iterator[None]:
    """Refuse every audited process creation in this process until exit."""
    global _installed, _depth
    if not _installed:
        sys.addaudithook(_audit)
        _installed = True
    _depth += 1
    try:
        yield
    finally:
        _depth -= 1


def process_creation_denied() -> bool:
    return _depth > 0


def refusal_count() -> int:
    return _refusals
