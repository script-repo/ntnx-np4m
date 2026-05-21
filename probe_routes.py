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
import os
import platform
import socket
import threading
import time
from collections import deque
from typing import Any

from flask import Blueprint, g, jsonify, request

import iface
import tester

probe_bp = Blueprint("probe", __name__)

PROBE_TOKEN_ENV = "NP4M_PROBE_TOKEN"
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
    return (os.environ.get(PROBE_TOKEN_ENV, "") or "").strip()


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
