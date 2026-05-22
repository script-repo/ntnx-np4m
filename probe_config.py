"""
Probe-side runtime configuration store.

Lives in ``~/.np4m-probe.json`` (override with ``$NP4M_PROBE_CONFIG``).
The probe agent reads ``token``, ``mgmt_iface`` and ``test_iface`` from
this file before falling back to the legacy env-var defaults
(``NP4M_PROBE_TOKEN``, ``NP4M_MGMT_IFACE``, ``NP4M_TEST_IFACE``). The
master can update any of these at runtime over the API
(``/probe/config``, ``/probe/token/rotate``) and the change persists
across agent restarts without touching shell rc files.

All accessors are thread-safe. Writes are atomic (tmp + rename) and
the file is chmodded to 0600 best-effort.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import threading
from typing import Any

CONFIG_PATH_ENV = "NP4M_PROBE_CONFIG"
# Co-locate the persistence file with the agent's own source tree
# instead of using ``~`` so that sudo/systemd HOME translation can't
# silently redirect writes to an unintended directory (or a directory
# the agent doesn't have permission to create). The repo dir is always
# writable by whatever user the agent is running as, since it owns the
# .venv there.
DEFAULT_PATH = pathlib.Path(__file__).resolve().parent / ".np4m-probe.json"

TOKEN_ENV = "NP4M_PROBE_TOKEN"
MGMT_IFACE_ENV = "NP4M_MGMT_IFACE"
TEST_IFACE_ENV = "NP4M_TEST_IFACE"

_lock = threading.RLock()
_cache: dict[str, Any] = {}
_loaded = False


def _path() -> pathlib.Path:
    p = os.environ.get(CONFIG_PATH_ENV)
    return pathlib.Path(p) if p else DEFAULT_PATH


def _load_unlocked() -> None:
    global _cache, _loaded
    if _loaded:
        return
    p = _path()
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            _cache = data if isinstance(data, dict) else {}
        except Exception:
            _cache = {}
    else:
        _cache = {}
    _loaded = True


def _save_unlocked() -> None:
    p = _path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(_cache, indent=2, sort_keys=True), "utf-8")
        tmp.replace(p)
        try:
            os.chmod(p, 0o600)
        except Exception:
            pass
    except Exception as exc:
        # Earlier builds swallowed this silently, which made
        # token-rotate / iface-save look like they worked when in fact
        # nothing was persisted. Surface it loudly to stderr so it
        # shows up in the systemd / nohup log on the probe.
        print(
            f"np4m probe_config: failed to persist {p!s}: {exc}",
            file=sys.stderr,
            flush=True,
        )


def _get(key: str, env_fallback: str | None = None) -> str | None:
    with _lock:
        _load_unlocked()
        val = _cache.get(key)
    if val is not None:
        s = str(val).strip()
        if s:
            return s
    if env_fallback:
        ev = (os.environ.get(env_fallback) or "").strip()
        return ev or None
    return None


def _set(key: str, value: str | None) -> None:
    with _lock:
        _load_unlocked()
        if value is None or (isinstance(value, str) and value.strip() == ""):
            _cache.pop(key, None)
        else:
            _cache[key] = str(value).strip()
        _save_unlocked()


def get_token() -> str | None:
    return _get("token", TOKEN_ENV)


def set_token(value: str | None) -> None:
    _set("token", value)


def get_mgmt_iface() -> str | None:
    return _get("mgmt_iface", MGMT_IFACE_ENV)


def set_mgmt_iface(value: str | None) -> None:
    _set("mgmt_iface", value)


def get_test_iface() -> str | None:
    return _get("test_iface", TEST_IFACE_ENV)


def set_test_iface(value: str | None) -> None:
    _set("test_iface", value)


def snapshot() -> dict[str, Any]:
    """Return the cached config (for debug logging)."""
    with _lock:
        _load_unlocked()
        return dict(_cache)


def config_path() -> str:
    return str(_path())
