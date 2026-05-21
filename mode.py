"""
Runtime role controller for NP4M.

The same codebase boots as either a master control plane (orchestrates probe
VMs, drives Prism Central, etc.) or a probe agent (configures the test NIC
inside its own VM and runs ping checks). Both modes coexist in one process
and the operator can flip between them at runtime via the Web UI
(`POST /api/mode`) or the TUI without restarting the binary.

The flag is shared by:
- `app.before_request` (rejects role-mismatched routes with HTTP 423)
- the Web UI (shows / hides master-only and probe-only cards)
- the TUI (re-renders its dashboard when the role flips)

Subscribers register a zero-arg callback via `subscribe()` and get invoked
on every successful `set_mode()`. Callbacks run synchronously under the
state lock, so keep them cheap; offload anything slow to a thread.
"""

from __future__ import annotations

import enum
import os
import threading
from typing import Callable


class Mode(str, enum.Enum):
    MASTER = "master"
    PROBE = "probe"

    @classmethod
    def parse(cls, raw: object) -> "Mode":
        if isinstance(raw, Mode):
            return raw
        if not isinstance(raw, str):
            raise ValueError(f"mode must be a string, got {type(raw).__name__}")
        norm = raw.strip().lower()
        for m in cls:
            if m.value == norm:
                return m
        raise ValueError(f"unknown mode {raw!r}; expected 'master' or 'probe'")


_DEFAULT_MODE = Mode.MASTER

_lock = threading.RLock()
_current_mode: Mode = _DEFAULT_MODE
_subscribers: list[Callable[[Mode, Mode], None]] = []


def _bootstrap_from_env() -> None:
    """Honor NP4M_MODE at import time so an operator can pin the role from
    the shell / systemd unit / container. Invalid values silently fall
    through to MASTER (the historical default)."""
    global _current_mode
    raw = os.environ.get("NP4M_MODE")
    if not raw:
        return
    try:
        _current_mode = Mode.parse(raw)
    except ValueError:
        return


_bootstrap_from_env()


def get_mode() -> Mode:
    with _lock:
        return _current_mode


def set_mode(new_mode: object) -> Mode:
    """Atomically swap the active mode and notify subscribers.

    Returns the resolved Mode. Raises ValueError on bad input.
    """
    global _current_mode
    target = Mode.parse(new_mode)
    with _lock:
        old = _current_mode
        if old == target:
            return target
        _current_mode = target
        subs = list(_subscribers)
    for cb in subs:
        try:
            cb(old, target)
        except Exception:
            pass
    return target


def subscribe(cb: Callable[[Mode, Mode], None]) -> Callable[[], None]:
    """Register `cb(old, new)` to fire after every successful mode flip.
    Returns an unsubscribe function."""
    with _lock:
        _subscribers.append(cb)

    def _unsub() -> None:
        with _lock:
            try:
                _subscribers.remove(cb)
            except ValueError:
                pass

    return _unsub


def is_master() -> bool:
    return get_mode() is Mode.MASTER


def is_probe() -> bool:
    return get_mode() is Mode.PROBE
