"""
Probe-side HTTP API.

The master pushes commands here over the management NIC. The probe agent
exposes three working endpoints (`/probe/configure`, `/probe/run-test`,
`/probe/logs`) plus one unauthenticated discovery endpoint
(`/probe/health`) so a master that's just been registered can quickly
verify the probe is alive and identify its interfaces before issuing any
write calls.

Write endpoints require an `Authorization: Bearer <token>` header that
matches `NP4M_PROBE_TOKEN` (constant-time compare). If the env var is
empty the probe is open — useful in a closed lab, dangerous in
production; logged on startup so the operator can't miss it.

All log lines go into a bounded in-memory deque that the master can read
via `GET /probe/logs?limit=N` after every test, so the per-VLAN summary
the master streams back to the UI can include the raw probe-side context.
"""

from __future__ import annotations

import hmac
import platform
import secrets
import socket
import threading
import time
from collections import deque
from typing import Any

from flask import Blueprint, g, jsonify, request

import iface
import probe_config
import tester

probe_bp = Blueprint("probe", __name__)

PROBE_TOKEN_ENV = probe_config.TOKEN_ENV
DEFAULT_PORT_ENV = "NP4M_PROBE_PORT"

_log_lock = threading.Lock()
_log_buffer: deque[dict[str, Any]] = deque(maxlen=1024)


def _record(level: str, msg: str, **extra: Any) -> None:
    entry: dict[str, Any] = {
        "ts": time.time(),
        "level": level,
        "msg": msg,
    }
    if extra:
        entry["extra"] = extra
    with _log_lock:
        _log_buffer.append(entry)


def _expected_token() -> str:
    return (probe_config.get_token() or "").strip()


def _tok_fp(t: str) -> str:
    """Short fingerprint of a bearer token for log messages — first 6 hex
    chars and the length, without leaking the secret."""
    if not t:
        return "(empty)"
    return f"{t[:6]}...({len(t)})"


def _check_auth() -> tuple[bool, str | None]:
    expected = _expected_token()
    if not expected:
        return True, None
    hdr = request.headers.get("Authorization", "")
    if not hdr.lower().startswith("bearer "):
        return False, "missing Bearer token"
    presented = hdr.split(" ", 1)[1].strip()
    if not hmac.compare_digest(presented, expected):
        return False, "bad bearer token"
    return True, None




@probe_bp.before_request
def _probe_auth_gate() -> Any:
    # /health is intentionally open so the master can discover + register
    # an unauthenticated probe and *then* push a token to it (out of band).
    if request.endpoint and request.endpoint.endswith(".probe_health"):
        return None
    ok, err = _check_auth()
    if not ok:
        # Log every failed attempt with a non-leaky fingerprint of both
        # tokens. This is the only diagnostic that can pin down "master
        # thinks it has token X but probe rejects" — both sides print
        # the same prefix when they match.
        hdr = request.headers.get("Authorization", "")
        presented = ""
        if hdr.lower().startswith("bearer "):
            presented = hdr.split(" ", 1)[1].strip()
        _record(
            "warn",
            f"auth-fail {request.method} {request.path}: "
            f"presented={_tok_fp(presented)} expected={_tok_fp(_expected_token())} "
            f"from={request.remote_addr}",
        )
        return jsonify(error=err or "unauthorized"), 401
    g.probe_authed = True
    return None


@probe_bp.get("/probe/health")
def probe_health() -> Any:
    try:
        from app import BUILD, __version__  # local import dodges circular dep
    except Exception:
        BUILD = None
        __version__ = "unknown"

    mgmt = iface.get_mgmt_iface()
    test = iface.get_test_iface()
    ifaces = iface.list_interfaces()
    mgmt_ip = None
    test_ip = None
    for it in ifaces:
        if mgmt and it.get("name") == mgmt:
            mgmt_ip = it.get("ipv4")
        if test and it.get("name") == test:
            test_ip = it.get("ipv4")

    payload = {
        "ok": True,
        "mode": "probe",
        "version": __version__,
        "build": BUILD,
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "mgmt_iface": mgmt,
        "mgmt_ip": mgmt_ip,
        "test_iface": test,
        "test_ip": test_ip,
        "interfaces": ifaces,
        "auth_required": bool(_expected_token()),
    }
    return jsonify(payload)


@probe_bp.post("/probe/configure")
def probe_configure() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    test_iface_default = iface.get_test_iface()
    iface_name = (body.get("iface") or test_iface_default or "").strip()
    ip = (body.get("ip") or "").strip()
    gateway = (body.get("gateway") or "").strip()
    try:
        prefix = int(body.get("prefix"))
    except (TypeError, ValueError):
        return jsonify(error="prefix must be an integer 1..32"), 400
    if not iface_name:
        return jsonify(error="iface is required (set NP4M_TEST_IFACE on the probe to default it)"), 400
    if not ip:
        return jsonify(error="ip is required"), 400

    _record("info", f"configure {iface_name} -> {ip}/{prefix} gw {gateway or '(none)'}")
    result = iface.apply_static(iface_name, ip, prefix, gateway)
    if result.get("ok"):
        _record("ok", f"  applied via {result.get('backend')}")
    else:
        _record("error", f"  {result.get('error')}", output=result.get("output"))
    status = 200 if result.get("ok") else 500
    return jsonify(result), status


@probe_bp.post("/probe/run-test")
def probe_run_test() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    gateway = (body.get("gateway") or "").strip()
    external_ip = (body.get("external_ip") or "").strip()
    try:
        count = int(body.get("count") or 3)
    except (TypeError, ValueError):
        count = 3
    try:
        timeout_s = float(body.get("timeout_s") or 2.0)
    except (TypeError, ValueError):
        timeout_s = 2.0

    if not gateway and not external_ip:
        return jsonify(error="at least one of gateway / external_ip is required"), 400

    _record(
        "info",
        f"run-test gw={gateway or '(none)'} external={external_ip or '(none)'} "
        f"count={count} timeout={timeout_s}s",
    )
    result = tester.ping_sequence(
        gateway, external_ip, count=count, timeout_s=timeout_s,
    )
    if result["gateway_ok"] and result["external_ok"]:
        _record("ok", "  PASS: gateway + external both reachable")
    elif result["gateway_ok"]:
        _record("warn", "  PARTIAL: gateway OK, external failed")
    else:
        _record("error", "  FAIL: gateway unreachable")
    return jsonify(result)


@probe_bp.get("/probe/logs")
def probe_logs() -> Any:
    try:
        limit = int(request.args.get("limit") or 200)
    except ValueError:
        limit = 200
    limit = max(1, min(limit, 1024))
    with _log_lock:
        items = list(_log_buffer)[-limit:]
    return jsonify(logs=items, count=len(items))


_VALID_IFACE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-",
)


def _valid_iface_name(name: str | None) -> bool:
    if name is None:
        return True
    if not isinstance(name, str):
        return False
    n = name.strip()
    if not n or len(n) > 32:
        return False
    return all(c in _VALID_IFACE_CHARS for c in n)


@probe_bp.get("/probe/config")
def probe_config_get() -> Any:
    """Return the runtime config the operator can change from the master
    UI. Deliberately does NOT echo the bearer token; the master already
    holds it (and the operator can rotate via /probe/token/rotate)."""
    return jsonify(
        mgmt_iface=iface.get_mgmt_iface(),
        test_iface=iface.get_test_iface(),
        auth_required=bool(_expected_token()),
        token_set=bool(_expected_token()),
        config_path=probe_config.config_path(),
    )


@probe_bp.post("/probe/config")
def probe_config_set() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    mgmt = body.get("mgmt_iface")
    test = body.get("test_iface")

    changes: list[str] = []
    if "mgmt_iface" in body:
        new_mgmt = (mgmt or "").strip() or None
        if not _valid_iface_name(new_mgmt):
            return jsonify(error=f"invalid mgmt_iface name {mgmt!r}"), 400
        probe_config.set_mgmt_iface(new_mgmt)
        changes.append(f"mgmt_iface={new_mgmt!r}")
    if "test_iface" in body:
        new_test = (test or "").strip() or None
        if not _valid_iface_name(new_test):
            return jsonify(error=f"invalid test_iface name {test!r}"), 400
        # Guard against the operator pointing test_iface at the management
        # iface — the apply layer also refuses but we want a clearer error.
        if new_test and new_test == probe_config.get_mgmt_iface():
            return (
                jsonify(error="test_iface must differ from mgmt_iface"),
                400,
            )
        probe_config.set_test_iface(new_test)
        changes.append(f"test_iface={new_test!r}")

    if changes:
        _record("info", "config update: " + ", ".join(changes))
    return jsonify(
        mgmt_iface=iface.get_mgmt_iface(),
        test_iface=iface.get_test_iface(),
        auth_required=bool(_expected_token()),
        token_set=bool(_expected_token()),
        config_path=probe_config.config_path(),
        changed=changes,
    )


@probe_bp.post("/probe/token/rotate")
def probe_token_rotate() -> Any:
    """Generate a fresh bearer token, persist it, and return it ONCE in
    the response body. After this call, subsequent writes must present
    the new token."""
    new_token = secrets.token_hex(24)
    probe_config.set_token(new_token)
    _record("warn", "bearer token rotated by master")
    return jsonify(token=new_token, auth_required=True)
