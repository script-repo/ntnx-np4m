"""
Master-side probe orchestration endpoints.

Responsible for:
- registering pre-deployed probe VMs (their management IP + bearer token
  + which AHV VM UUID + which vNIC is the "test" NIC),
- listing registered probes + live-pinging /probe/health,
- running a per-VLAN test loop: re-point the probe vNIC's subnet via PC
  v4 VM API, push IP/gw to the probe over its management NIC, ask it to
  ping gateway + external, stream NDJSON results back to the UI.

PC v4 VM NIC swap (the part that "changes its own interface from within
Prism"):

  GET    /api/vmm/v4.0/ahv/config/vms/{vmExtId}                -> body + ETag
  PUT    /api/vmm/v4.0/ahv/config/vms/{vmExtId}/nics/{nicExtId}
         If-Match: <etag>
         body: { networkInfo: { subnet: { extId: <subnetExtId> } } }
                                                              -> task ref
  GET    /api/prism/v4.0/config/tasks/{extId}                  -> poll until SUCCEEDED

This module reuses `_make_pc_session`, `_get_session`, and `_wait_for_task`
from app.py.
"""

from __future__ import annotations

import json
import os
import pathlib
import secrets
import sys
import threading
import time
import uuid
from typing import Any

import requests
import urllib3
from flask import Blueprint, Response, jsonify, request, stream_with_context

import app as _app  # circular-safe at import time: only attributes used at call time

master_probe_bp = Blueprint("master_probe", __name__)

# Probe registry, persisted to disk so a master restart doesn't force the
# operator to re-register every probe (and risk typing the wrong bearer
# token, which used to cause silent token/probe desync).
#
# The file is co-located with app.py so sudo/systemd HOME translation
# can't redirect it somewhere unwritable (same reasoning as
# probe_config.DEFAULT_PATH on the probe side).
_REGISTRY_FILE = pathlib.Path(__file__).resolve().parent / ".np4m-master.json"
PROBES: dict[str, dict[str, Any]] = {}
_probes_lock = threading.Lock()
_persist_warned = False


def _save_probes_unlocked() -> None:
    """Atomically write PROBES to disk. Caller MUST hold _probes_lock.
    Any failure is logged once to stderr; we never raise (a save failure
    must not break the API call that triggered it)."""
    global _persist_warned
    try:
        _REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _REGISTRY_FILE.with_suffix(_REGISTRY_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(PROBES, indent=2, sort_keys=True), "utf-8")
        tmp.replace(_REGISTRY_FILE)
        try:
            os.chmod(_REGISTRY_FILE, 0o600)
        except Exception:
            pass
        _persist_warned = False
    except Exception as exc:
        if not _persist_warned:
            print(
                f"np4m master: failed to persist probe registry {_REGISTRY_FILE!s}: {exc}",
                file=sys.stderr,
                flush=True,
            )
            _persist_warned = True


def _load_probes_from_disk() -> None:
    """Best-effort load of the probe registry. Called once at module
    import; missing/corrupt file just leaves PROBES empty."""
    if not _REGISTRY_FILE.exists():
        return
    try:
        raw = json.loads(_REGISTRY_FILE.read_text("utf-8"))
    except Exception as exc:
        print(
            f"np4m master: probe registry {_REGISTRY_FILE!s} is unreadable ({exc}); starting empty",
            file=sys.stderr,
            flush=True,
        )
        return
    if not isinstance(raw, dict):
        return
    with _probes_lock:
        for probe_id, record in raw.items():
            if isinstance(probe_id, str) and isinstance(record, dict):
                PROBES[probe_id] = record


_load_probes_from_disk()


def _redacted_probe(p: dict[str, Any]) -> dict[str, Any]:
    """Strip the bearer token before exposing a probe to the UI, but
    include a non-secret fingerprint so the UI can detect master/probe
    token desync without ever sending the token to the browser."""
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "mgmt_host": p.get("mgmt_host"),
        "mgmt_port": p.get("mgmt_port"),
        "use_https": p.get("use_https"),
        "probe_vm_uuid": p.get("probe_vm_uuid"),
        "test_nic_ext_id": p.get("test_nic_ext_id"),
        "test_nic_mac": p.get("test_nic_mac"),
        "created_at": p.get("created_at"),
        "stored_token_fp": _tok_fp(p.get("token") or ""),
    }


def _probe_base_url(p: dict[str, Any]) -> str:
    scheme = "https" if p.get("use_https") else "http"
    return f"{scheme}://{p['mgmt_host']}:{int(p['mgmt_port'])}"


def _tok_fp(t: str) -> str:
    """Match probe_routes._tok_fp so master + probe logs use the same
    fingerprint format and can be compared by eye."""
    if not t:
        return "(empty)"
    return f"{t[:6]}...({len(t)})"


def _probe_request(
    p: dict[str, Any],
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    timeout: int = 30,
) -> requests.Response:
    url = _probe_base_url(p) + path
    headers: dict[str, str] = {"Accept": "application/json"}
    token = p.get("token") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    return requests.request(
        method, url,
        headers=headers,
        json=json_body,
        timeout=timeout,
        verify=False,
    )


# ---------------------------------------------------------------------------
# Probe registry endpoints
# ---------------------------------------------------------------------------


@master_probe_bp.post("/api/probes/register")
def api_probes_register() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    name = (body.get("name") or "").strip()
    mgmt_host = (body.get("mgmt_host") or "").strip()
    mgmt_port = body.get("mgmt_port") or 5050
    token = (body.get("token") or "").strip()
    probe_vm_uuid = (body.get("probe_vm_uuid") or "").strip() or None
    test_nic_ext_id = (body.get("test_nic_ext_id") or "").strip() or None
    test_nic_mac = (body.get("test_nic_mac") or "").strip() or None
    use_https = bool(body.get("use_https", False))

    if not mgmt_host:
        return jsonify(error="mgmt_host is required"), 400
    try:
        mgmt_port = int(mgmt_port)
    except (TypeError, ValueError):
        return jsonify(error="mgmt_port must be an integer"), 400

    probe_id = secrets.token_urlsafe(12)
    record = {
        "id": probe_id,
        "name": name or mgmt_host,
        "mgmt_host": mgmt_host,
        "mgmt_port": mgmt_port,
        "use_https": use_https,
        "token": token,
        "probe_vm_uuid": probe_vm_uuid,
        "test_nic_ext_id": test_nic_ext_id,
        "test_nic_mac": test_nic_mac,
        "created_at": time.time(),
    }
    with _probes_lock:
        PROBES[probe_id] = record
        _save_probes_unlocked()
    return jsonify(probe=_redacted_probe(record))


@master_probe_bp.post("/api/probes/update")
def api_probes_update() -> Any:
    """Patch an existing probe — used by the UI's "Fetch probe NICs" flow
    so the operator can pick which vNIC is the test NIC after the fact."""
    body = request.get_json(force=True, silent=True) or {}
    probe_id = (body.get("probe_id") or "").strip()
    with _probes_lock:
        p = PROBES.get(probe_id)
        if not p:
            return jsonify(error="probe not found"), 404
        for field in ("name", "probe_vm_uuid", "test_nic_ext_id", "test_nic_mac", "token"):
            if field in body and body[field] is not None:
                val = body[field]
                if isinstance(val, str):
                    val = val.strip()
                p[field] = val or None
        # If the operator re-picks the test NIC without supplying a MAC,
        # drop any previously-captured MAC so the by-MAC self-heal can't
        # relocate back to the old NIC; it'll be re-captured on next run.
        if "test_nic_ext_id" in body and "test_nic_mac" not in body:
            p["test_nic_mac"] = None
        snapshot = dict(p)
        _save_probes_unlocked()
    return jsonify(probe=_redacted_probe(snapshot))


@master_probe_bp.post("/api/probes/delete")
def api_probes_delete() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    probe_id = (body.get("probe_id") or "").strip()
    with _probes_lock:
        p = PROBES.pop(probe_id, None)
        if p:
            _save_probes_unlocked()
    return jsonify(removed=bool(p))


@master_probe_bp.get("/api/probes")
def api_probes_list() -> Any:
    """List probes, opportunistically probing /probe/health for liveness."""
    with _probes_lock:
        snapshot = [dict(p) for p in PROBES.values()]
    out: list[dict[str, Any]] = []
    for p in snapshot:
        info = _redacted_probe(p)
        info["health"] = _probe_health_safely(p)
        out.append(info)
    return jsonify(probes=out)


@master_probe_bp.post("/api/probes/health")
def api_probes_health_one() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    probe_id = (body.get("probe_id") or "").strip()
    with _probes_lock:
        p = dict(PROBES.get(probe_id) or {})
    if not p:
        return jsonify(error="probe not found"), 404
    return jsonify(health=_probe_health_safely(p))


def _resolve_probe(body: dict[str, Any]) -> dict[str, Any] | None:
    probe_id = (body.get("probe_id") or "").strip()
    with _probes_lock:
        return dict(PROBES.get(probe_id) or {}) or None


@master_probe_bp.post("/api/probes/token-reveal")
def api_probes_token_reveal() -> Any:
    """Return the currently-stored bearer token for the given probe.
    The master is single-tenant + LAN-local, so we don't gate this with
    its own auth — anyone who can reach the master UI can already read
    PC credentials too."""
    body = request.get_json(force=True, silent=True) or {}
    p = _resolve_probe(body)
    if p is None:
        return jsonify(error="probe not found"), 404
    return jsonify(token=p.get("token") or "")


@master_probe_bp.post("/api/probes/proxy-logs")
def api_probes_proxy_logs() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    p = _resolve_probe(body)
    if p is None:
        return jsonify(error="probe not found"), 404
    try:
        limit = int(body.get("limit") or 200)
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1024))
    try:
        r = _probe_request(p, "GET", f"/probe/logs?limit={limit}", timeout=10)
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    if r.status_code >= 400:
        return jsonify(error=f"HTTP {r.status_code}: {r.text[:200]}"), 502
    try:
        return jsonify(r.json() or {})
    except ValueError:
        return jsonify(error="probe returned non-JSON logs response"), 502


@master_probe_bp.post("/api/probes/proxy-config")
def api_probes_proxy_config_get() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    p = _resolve_probe(body)
    if p is None:
        return jsonify(error="probe not found"), 404
    try:
        r = _probe_request(p, "GET", "/probe/config", timeout=10)
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    if r.status_code >= 400:
        return jsonify(error=f"HTTP {r.status_code}: {r.text[:200]}"), 502
    try:
        return jsonify(r.json() or {})
    except ValueError:
        return jsonify(error="probe returned non-JSON config response"), 502


@master_probe_bp.post("/api/probes/proxy-config-set")
def api_probes_proxy_config_set() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    p = _resolve_probe(body)
    if p is None:
        return jsonify(error="probe not found"), 404
    forward: dict[str, Any] = {}
    for k in ("mgmt_iface", "test_iface"):
        if k in body:
            forward[k] = body[k]
    if not forward:
        return jsonify(error="nothing to set"), 400
    try:
        r = _probe_request(
            p, "POST", "/probe/config", json_body=forward, timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    try:
        payload = r.json() or {}
    except ValueError:
        payload = {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    if r.status_code >= 400:
        return jsonify(payload), r.status_code
    return jsonify(payload)


@master_probe_bp.post("/api/probes/proxy-token-rotate")
def api_probes_proxy_token_rotate() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    probe_id = (body.get("probe_id") or "").strip()
    with _probes_lock:
        p = dict(PROBES.get(probe_id) or {})
    if not p:
        return jsonify(error="probe not found"), 404
    try:
        r = _probe_request(p, "POST", "/probe/token/rotate", json_body={}, timeout=15)
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    try:
        payload = r.json() or {}
    except ValueError:
        return jsonify(error=f"HTTP {r.status_code}: {r.text[:200]}"), 502
    if r.status_code >= 400:
        return jsonify(payload), r.status_code
    new_token = (payload.get("token") or "").strip()
    if new_token:
        with _probes_lock:
            stored = PROBES.get(probe_id)
            if stored is not None:
                stored["token"] = new_token
                _save_probes_unlocked()
    return jsonify(payload)


def _probe_health_safely(p: dict[str, Any]) -> dict[str, Any]:
    try:
        r = _probe_request(p, "GET", "/probe/health", timeout=5)
        if r.status_code >= 400:
            return {"reachable": False, "error": f"HTTP {r.status_code}"}
        try:
            data = r.json()
        except ValueError:
            data = {}
        data["reachable"] = True
        return data
    except requests.RequestException as exc:
        return {"reachable": False, "error": str(exc)}


# ---------------------------------------------------------------------------
# PC v4 VM NIC subnet swap
# ---------------------------------------------------------------------------


def _pc_get_vm(
    session: requests.Session, base_url: str, vm_ext_id: str,
) -> tuple[dict[str, Any], str | None]:
    """Fetch a VM body. Returns (body, etag). Raises requests.RequestException."""
    r = session.get(
        f"{base_url}/api/vmm/v4.0/ahv/config/vms/{vm_ext_id}",
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"GET vm {vm_ext_id} failed HTTP {r.status_code}: {r.text[:200]}"
        )
    etag = r.headers.get("ETag") or r.headers.get("Etag")
    try:
        body = r.json() or {}
    except ValueError:
        body = {}
    return body, etag


def _pc_list_vm_nics(
    session: requests.Session, base_url: str, vm_ext_id: str,
) -> list[dict[str, Any]]:
    """Return a small projection of the VM's nics: [{extId, mac, subnet:{extId,name}}, ...]."""
    body, _etag = _pc_get_vm(session, base_url, vm_ext_id)
    data = body.get("data") or {}
    nics = data.get("nics") or []
    out: list[dict[str, Any]] = []
    for n in nics:
        if not isinstance(n, dict):
            continue
        net_info = n.get("networkInfo") or {}
        subnet = net_info.get("subnet") or {}
        backing = n.get("backingInfo") or {}
        out.append({
            "extId": n.get("extId"),
            "mac": backing.get("macAddress"),
            "vlanMode": net_info.get("vlanMode"),
            "trunkedVlans": net_info.get("trunkedVlans") or [],
            "subnet": {
                "extId": subnet.get("extId"),
                "name": subnet.get("name"),
            },
        })
    return out


def _pc_get_nic(
    session: requests.Session,
    base_url: str,
    vm_ext_id: str,
    nic_ext_id: str,
) -> tuple[dict[str, Any], str | None]:
    """Fetch a single NIC body. Returns (nic_object, etag).

    The PC v4 envelope is ``{"data": {...nic object...}}`` on success;
    some older builds return the object at the top level, so we
    tolerate both."""
    r = session.get(
        f"{base_url}/api/vmm/v4.0/ahv/config/vms/{vm_ext_id}/nics/{nic_ext_id}",
        timeout=20,
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"GET nic {nic_ext_id} on vm {vm_ext_id} failed "
            f"HTTP {r.status_code}: {r.text[:200]}"
        )
    try:
        envelope = r.json() or {}
    except ValueError:
        envelope = {}
    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    if not isinstance(data, dict):
        raise RuntimeError(
            f"GET nic {nic_ext_id} returned unexpected body shape"
        )
    etag = r.headers.get("ETag") or r.headers.get("Etag")
    return data, etag


# Fields that PC v4 echoes back on a NIC GET but rejects (or silently
# ignores) in a PUT body. Strip them before sending the mutation.
_NIC_READONLY_FIELDS: frozenset[str] = frozenset({
    "links", "tenantId",
})


def _nic_subnet_ext_id(nic: dict[str, Any]) -> str | None:
    """Pull the bound subnet extId out of a NIC body (v4 ``networkInfo``)."""
    return ((nic.get("networkInfo") or {}).get("subnet") or {}).get("extId")


def _nic_mac(nic: dict[str, Any]) -> str | None:
    """MAC from either the public ``backingInfo`` or the older
    ``nicBackingInfo`` projection PC 7.3 also returns."""
    return (
        (nic.get("backingInfo") or {}).get("macAddress")
        or (nic.get("nicBackingInfo") or {}).get("macAddress")
    )


def _pc_find_nic_extid_by_mac(
    session: requests.Session,
    base_url: str,
    vm_ext_id: str,
    mac: str,
) -> str | None:
    """Return the extId of the VM's NIC whose MAC matches ``mac`` (case-
    insensitive), or None. Used to relocate the test NIC after a recreate
    swapped its extId out from under a stale registry entry."""
    if not mac:
        return None
    for n in _pc_list_vm_nics(session, base_url, vm_ext_id):
        if (n.get("mac") or "").lower() == mac.lower():
            return n.get("extId")
    return None


def _pc_delete_nic(
    session: requests.Session,
    base_url: str,
    vm_ext_id: str,
    nic_ext_id: str,
) -> None:
    """Delete a NIC and wait for the task. Raises on failure."""
    _nic, etag = _pc_get_nic(session, base_url, vm_ext_id, nic_ext_id)
    headers: dict[str, str] = {"NTNX-Request-Id": str(uuid.uuid4())}
    if etag:
        headers["If-Match"] = etag
    r = session.delete(
        f"{base_url}/api/vmm/v4.0/ahv/config/vms/{vm_ext_id}/nics/{nic_ext_id}",
        headers=headers,
        timeout=60,
    )
    if r.status_code not in (200, 201, 202):
        try:
            err = r.json()
        except ValueError:
            err = {"raw": r.text}
        raise RuntimeError(
            f"DELETE nic failed HTTP {r.status_code}: {_app._format_api_error(err)}"
        )
    try:
        payload = r.json() or {}
    except ValueError:
        payload = {}
    task_id = _app._extract_task_ext_id(payload)
    if task_id:
        task = _app._wait_for_task(session, base_url, task_id, timeout_seconds=120)
        if task.get("status") != "SUCCEEDED":
            raise RuntimeError(
                f"NIC delete task ended {task.get('status')}: "
                f"{_app._format_api_error({'data': task})}"
            )


def _pc_create_nic(
    session: requests.Session,
    base_url: str,
    vm_ext_id: str,
    *,
    mac: str,
    subnet_ext_id: str,
    vlan_mode: str = "ACCESS",
    nic_type: str = "NORMAL_NIC",
    is_connected: bool = True,
    trunked_vlans: list[int] | None = None,
) -> str | None:
    """Create a NIC on ``vm_ext_id`` bound to ``subnet_ext_id``, preserving
    ``mac``. Returns the new NIC extId (located by MAC after the task).

    Creating a NIC mutates the VM, so PC v4 requires ``If-Match`` set to the
    *VM* ETag (not a NIC ETag) — without it PC answers 428 Precondition
    Required.
    """
    _vm, vm_etag = _pc_get_vm(session, base_url, vm_ext_id)
    network_info: dict[str, Any] = {
        "nicType": nic_type,
        "vlanMode": vlan_mode,
        "subnet": {"extId": subnet_ext_id},
    }
    if vlan_mode == "TRUNKED" and trunked_vlans:
        network_info["trunkedVlans"] = trunked_vlans
    body: dict[str, Any] = {
        "backingInfo": {"isConnected": is_connected, "macAddress": mac},
        "networkInfo": network_info,
    }
    headers: dict[str, str] = {"NTNX-Request-Id": str(uuid.uuid4())}
    if vm_etag:
        headers["If-Match"] = vm_etag
    r = session.post(
        f"{base_url}/api/vmm/v4.0/ahv/config/vms/{vm_ext_id}/nics",
        json=body,
        headers=headers,
        timeout=60,
    )
    if r.status_code not in (200, 201, 202):
        try:
            err = r.json()
        except ValueError:
            err = {"raw": r.text}
        raise RuntimeError(
            f"POST nic failed HTTP {r.status_code}: {_app._format_api_error(err)}"
        )
    try:
        payload = r.json() or {}
    except ValueError:
        payload = {}
    task_id = _app._extract_task_ext_id(payload)
    if task_id:
        task = _app._wait_for_task(session, base_url, task_id, timeout_seconds=120)
        if task.get("status") != "SUCCEEDED":
            raise RuntimeError(
                f"NIC create task ended {task.get('status')}: "
                f"{_app._format_api_error({'data': task})}"
            )
    # Locate the freshly-created NIC by MAC (its extId is brand new and not
    # returned in the task body). Retry briefly; the VM read can lag the task.
    for _ in range(5):
        for n in _pc_list_vm_nics(session, base_url, vm_ext_id):
            if (n.get("mac") or "").lower() == mac.lower():
                return n.get("extId")
        time.sleep(1.0)
    return None


def _pc_swap_nic_subnet(
    session: requests.Session,
    base_url: str,
    vm_ext_id: str,
    nic_ext_id: str,
    subnet_ext_id: str,
    *,
    mac: str | None = None,
) -> dict[str, Any]:
    """Re-point a NIC at ``subnet_ext_id``, transparently handling the
    AHV-version difference.

    On PC 7.5+ / AHV 11+, ``PUT /vms/{vm}/nics/{nic}`` with a changed
    ``networkInfo.subnet`` updates the NIC in place. On PC 7.3 / AHV 10.3
    the NIC's network is **non-updatable**: the PUT (changing only the
    public ``networkInfo``) is accepted and the task reports SUCCEEDED, but
    the subnet never actually changes (the authoritative ``nicNetworkInfo``
    is left untouched, and forcing it fails the task with VMM-30110). So we
    PUT, *verify* the subnet actually moved, and if it didn't, fall back to
    delete + recreate the NIC on the target subnet (preserving the MAC).

    Because the recreate path changes the NIC extId, a stored extId can go
    stale (e.g. an interrupted run, or recreate done out of band). When a
    ``mac`` is supplied we self-heal: if the extId no longer resolves, we
    relocate the NIC by MAC.

    Returns ``{"status", "nic_ext_id", "mac", "method", "note"}``.
    ``nic_ext_id`` is the NIC's *current* extId — which changes when the
    recreate path is taken, so the caller must re-track it (and ``mac``,
    captured here on first contact, lets future runs self-heal).
    """
    nic = etag = None
    if nic_ext_id:
        try:
            nic, etag = _pc_get_nic(session, base_url, vm_ext_id, nic_ext_id)
        except RuntimeError:
            nic = None  # stale extId — fall through to MAC relocation
    if nic is None:
        relocated = _pc_find_nic_extid_by_mac(session, base_url, vm_ext_id, mac or "")
        if not relocated:
            raise RuntimeError(
                f"test NIC {nic_ext_id!r} not found on VM {vm_ext_id} and it "
                f"could not be relocated by MAC ({mac or 'none on record'})"
            )
        nic_ext_id = relocated
        nic, etag = _pc_get_nic(session, base_url, vm_ext_id, nic_ext_id)

    live_mac = _nic_mac(nic) or mac
    if _nic_subnet_ext_id(nic) == subnet_ext_id:
        return {
            "status": "SUCCEEDED", "nic_ext_id": nic_ext_id, "mac": live_mac,
            "method": "noop", "note": "already on target subnet",
        }

    ni = nic.get("networkInfo") or {}
    vlan_mode = ni.get("vlanMode") or "ACCESS"
    nic_type = ni.get("nicType") or "NORMAL_NIC"
    trunked = ni.get("trunkedVlans") or []
    is_connected = (nic.get("backingInfo") or {}).get("isConnected", True)

    # --- 1) In-place update (PC 7.5+ / AHV 11+) ---------------------------
    body: dict[str, Any] = {
        k: v for k, v in nic.items() if k not in _NIC_READONLY_FIELDS
    }
    network_info = dict(body.get("networkInfo") or {})
    network_info["subnet"] = {"extId": subnet_ext_id}
    body["networkInfo"] = network_info
    headers: dict[str, str] = {"NTNX-Request-Id": str(uuid.uuid4())}
    if etag:
        headers["If-Match"] = etag
    r = session.put(
        f"{base_url}/api/vmm/v4.0/ahv/config/vms/{vm_ext_id}/nics/{nic_ext_id}",
        json=body,
        headers=headers,
        timeout=60,
    )
    inplace_applied = False
    if r.status_code in (200, 201, 202):
        try:
            payload = r.json() or {}
        except ValueError:
            payload = {}
        task_id = _app._extract_task_ext_id(payload)
        if task_id:
            # The task can report SUCCEEDED on 7.3 even though nothing
            # changed, so its status alone is not trustworthy here.
            _app._wait_for_task(session, base_url, task_id, timeout_seconds=120)
        try:
            after, _ = _pc_get_nic(session, base_url, vm_ext_id, nic_ext_id)
            inplace_applied = _nic_subnet_ext_id(after) == subnet_ext_id
        except RuntimeError:
            inplace_applied = False

    if inplace_applied:
        return {
            "status": "SUCCEEDED", "nic_ext_id": nic_ext_id, "mac": live_mac,
            "method": "in_place", "note": "",
        }

    # --- 2) Fallback: delete + recreate (PC 7.3 / AHV 10.3) ---------------
    if not live_mac:
        raise RuntimeError(
            "NIC subnet is not updatable in place on this AHV version and "
            "the existing MAC could not be read, so the NIC cannot be "
            "recreated on the target subnet"
        )
    _pc_delete_nic(session, base_url, vm_ext_id, nic_ext_id)
    new_id = _pc_create_nic(
        session, base_url, vm_ext_id,
        mac=live_mac, subnet_ext_id=subnet_ext_id,
        vlan_mode=vlan_mode, nic_type=nic_type,
        is_connected=is_connected,
        trunked_vlans=trunked,
    )
    if not new_id:
        raise RuntimeError(
            "NIC was recreated on the target subnet but could not be located "
            f"by MAC {live_mac} afterward"
        )
    return {
        "status": "SUCCEEDED", "nic_ext_id": new_id, "mac": live_mac,
        "method": "recreate",
        "note": (
            "AHV subnet not updatable in place (PC 7.3 / AHV 10.3); "
            "recreated NIC on the target subnet — extId changed"
        ),
    }


def _find_subnet_extid_for_vlan(
    session: requests.Session,
    base_url: str,
    cluster_uuid: str,
    vlan: int,
    name: str | None = None,
) -> tuple[str | None, str | None]:
    """Resolve the target subnet's extId on `cluster_uuid`.

    Targets by (name, VLAN) so that duplicate-VLAN subnets are
    disambiguated by the name the operator created -- the NIC swap moves
    by subnet UUID, and a bare VLAN lookup can't tell two subnets on the
    same VLAN apart. Returns ``(extId, note)`` where ``note`` carries a
    human-readable reason on refusal or a warning on ambiguity.

    Refuse-and-skip: when a name is given but no subnet on that VLAN has
    that exact name (case-insensitive), returns ``(None, reason)`` rather
    than guessing -- pointing the probe at the wrong subnet would silently
    test the wrong network.
    """
    try:
        r = session.get(
            f"{base_url}/api/networking/v4.0/config/subnets?$limit=100",
            timeout=20,
        )
        if r.status_code >= 400:
            return None, f"subnet list returned HTTP {r.status_code}"
        data = (r.json() or {}).get("data") or []
    except requests.RequestException as exc:
        return None, f"subnet list request failed: {exc}"

    vlan_matches = [
        s for s in data
        if s.get("clusterReference") == cluster_uuid
        and s.get("networkId") == vlan
    ]
    if not vlan_matches:
        return None, None

    if name:
        want = name.strip().lower()
        named = [
            s for s in vlan_matches
            if (s.get("name") or "").strip().lower() == want
        ]
        if len(named) == 1:
            return named[0].get("extId"), None
        if len(named) > 1:
            return named[0].get("extId"), (
                f"{len(named)} subnets named '{name}' on VLAN {vlan}; "
                f"using the first ({named[0].get('extId')})"
            )
        # Name given but no exact match on this VLAN -> refuse and skip.
        others = ", ".join(repr(s.get("name")) for s in vlan_matches)
        return None, (
            f"no subnet named '{name}' on VLAN {vlan} "
            f"(VLAN {vlan} subnets present: {others})"
        )

    # No name supplied: fall back to VLAN-only resolution.
    if len(vlan_matches) == 1:
        return vlan_matches[0].get("extId"), None
    return vlan_matches[0].get("extId"), (
        f"{len(vlan_matches)} subnets on VLAN {vlan} and no name given; "
        f"using the first ({vlan_matches[0].get('extId')})"
    )


# ---------------------------------------------------------------------------
# Endpoints for probe-VM NIC introspection (used by the UI to populate the
# "which vNIC is the test NIC" picker after register)
# ---------------------------------------------------------------------------


def _pc_list_vms(
    session: requests.Session,
    base_url: str,
    cluster_ext_id: str | None,
    *,
    page_limit: int = 100,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """Walk PC v4 ``GET /api/vmm/v4.0/ahv/config/vms`` and return a flat
    projection of [{extId, name, cluster_ext_id}] for the VM picker UI.

    When ``cluster_ext_id`` is set we narrow server-side with ``$filter``
    so we don't drag every VM across the wire on big PCs. We also stop as
    soon as a page comes back short, since ``totalAvailableResults`` is
    not always present on older PC v4 builds."""
    out: list[dict[str, Any]] = []
    page = 0
    while page < max_pages:
        params: dict[str, Any] = {"$page": page, "$limit": page_limit}
        if cluster_ext_id:
            # OData v4 filter on the embedded cluster reference. PC accepts
            # both quoted (string) and bare (extId) forms; quoted is safer
            # across builds.
            params["$filter"] = f"cluster/extId eq '{cluster_ext_id}'"
        r = session.get(
            f"{base_url}/api/vmm/v4.0/ahv/config/vms",
            params=params,
            timeout=30,
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"GET vms (page {page}) failed HTTP {r.status_code}: {r.text[:200]}"
            )
        try:
            envelope = r.json() or {}
        except ValueError:
            envelope = {}
        data = envelope.get("data") or []
        if not isinstance(data, list):
            data = []
        for vm in data:
            if not isinstance(vm, dict):
                continue
            cluster = vm.get("cluster") or {}
            out.append({
                "extId": vm.get("extId"),
                "name": vm.get("name") or "(unnamed)",
                "cluster_ext_id": cluster.get("extId"),
            })
        if len(data) < page_limit:
            break
        page += 1
    return out


@master_probe_bp.post("/api/probes/vm-list")
def api_probes_vm_list() -> Any:
    """List VMs from the connected target PC for the probe-VM picker.

    The UI calls this in lieu of the operator hunting the probe VM in
    Prism Central, copying the UUID, pasting it into the input. Filters
    server-side by cluster_uuid when one is supplied so the dropdown
    stays manageable on large PCs."""
    body = request.get_json(force=True, silent=True) or {}
    target_token = body.get("target_token")
    cluster_uuid = (body.get("cluster_uuid") or "").strip() or None
    sess = _app._get_session(target_token)
    if not sess:
        return jsonify(error="not connected to target PC"), 401
    s = _app._make_pc_session(sess["auth"])
    try:
        vms = _pc_list_vms(s, sess["base_url"], cluster_uuid)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    vms.sort(key=lambda v: (v.get("name") or "").lower())
    return jsonify(vms=vms, cluster_uuid=cluster_uuid)


@master_probe_bp.post("/api/probes/vm-nics")
def api_probes_vm_nics() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    target_token = body.get("target_token")
    vm_ext_id = (body.get("vm_ext_id") or "").strip()
    if not vm_ext_id:
        return jsonify(error="vm_ext_id is required"), 400
    sess = _app._get_session(target_token)
    if not sess:
        return jsonify(error="not connected to target PC"), 401
    s = _app._make_pc_session(sess["auth"])
    try:
        nics = _pc_list_vm_nics(s, sess["base_url"], vm_ext_id)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 502
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    return jsonify(nics=nics)


# ---------------------------------------------------------------------------
# Per-VLAN run loop: stream NDJSON same way /api/create does
# ---------------------------------------------------------------------------


@master_probe_bp.post("/api/probe-tests/run")
def api_probe_tests_run() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    probe_id = (body.get("probe_id") or "").strip()
    target_token = body.get("target_token")
    cluster_uuid = body.get("cluster_uuid")
    networks = body.get("networks") or []

    with _probes_lock:
        probe = dict(PROBES.get(probe_id) or {})
    if not probe:
        return jsonify(error="probe not found"), 404
    sess = _app._get_session(target_token)
    if not sess:
        return jsonify(error="not connected to target PC"), 401
    if not cluster_uuid:
        return jsonify(error="cluster_uuid is required"), 400
    if not isinstance(networks, list) or not networks:
        return jsonify(error="networks list is required"), 400
    if not probe.get("probe_vm_uuid") or not probe.get("test_nic_ext_id"):
        return (
            jsonify(error="probe is missing probe_vm_uuid / test_nic_ext_id; update it first"),
            400,
        )

    pc_session = _app._make_pc_session(sess["auth"])
    base_url = sess["base_url"]
    test_iface_default = (body.get("test_iface") or "").strip() or None

    def emit(level: str, msg: str) -> str:
        return json.dumps({"level": level, "msg": msg}) + "\n"

    def emit_result(**fields: Any) -> str:
        """Structured per-VLAN row for the UI's Export report CSV.

        ``level`` is set to a sentinel ("result") so the client's existing
        log renderer can ignore it (it's intended for the report buffer
        only, not the human-readable log pane). The shape matches the 8
        columns the user asked for, with a couple of extras (subnet_ext,
        nic_ext_id, vlan, run_ts) that make the CSV self-describing if
        re-imported into a spreadsheet later."""
        payload = {"level": "result", "event": "vlan_result"}
        payload.update(fields)
        return json.dumps(payload) + "\n"

    def gen():
        ok_count = 0
        partial_count = 0
        fail_count = 0
        run_ts = time.time()
        yield emit("system", f"Probe-test run against probe '{probe.get('name')}' ({probe['mgmt_host']}).")
        yield emit("info", f"Probe VM UUID: {probe.get('probe_vm_uuid')}, test NIC: {probe.get('test_nic_ext_id')}")

        # Liveness pre-check so we fail fast if the probe is unreachable.
        health = _probe_health_safely(probe)
        if not health.get("reachable"):
            yield emit("error", f"Probe /health unreachable: {health.get('error')}")
            yield emit("error", "Aborting run.")
            return
        yield emit("ok", f"  probe reachable: {health.get('hostname')} (test_iface={health.get('test_iface')})")

        effective_test_iface = test_iface_default or health.get("test_iface")
        if not effective_test_iface:
            yield emit("error", "No probe test iface known (set NP4M_TEST_IFACE on the probe or pass test_iface in the request body).")
            return

        for net in networks:
            name = (net or {}).get("name") or "(unnamed)"
            # Seed the result row up front so every terminal branch
            # (skip / NIC-swap fail / configure fail / partial / pass)
            # can emit a row with the fields it knows about.
            row: dict[str, Any] = {
                "run_ts": run_ts,
                "subnet_name": name,
                "vlan": None,
                "subnet_ext_id": None,
                "test_nic_ext_id": probe.get("test_nic_ext_id"),
                "test_iface": effective_test_iface,
                "guest_ip": None,
                "prefix": None,
                "gateway": None,
                "external_ip": None,
                "gateway_ping": "SKIP",
                "external_ping": "SKIP",
                "status": "FAIL",
                "detail": "",
            }

            try:
                vlan = int((net or {}).get("vlan"))
                row["vlan"] = vlan
            except (TypeError, ValueError):
                yield emit("error", f"  {name}: invalid VLAN; skipping")
                row["detail"] = "invalid VLAN"
                yield emit_result(**row)
                fail_count += 1
                continue
            test_ip = (net or {}).get("test_ip") or ""
            try:
                prefix = int((net or {}).get("prefix"))
                row["prefix"] = prefix
            except (TypeError, ValueError):
                yield emit("error", f"  {name}: invalid prefix; skipping")
                row["detail"] = "invalid prefix"
                yield emit_result(**row)
                fail_count += 1
                continue
            gateway = (net or {}).get("gateway") or ""
            external_ip = (net or {}).get("external_ip") or ""
            row["guest_ip"] = test_ip
            row["gateway"] = gateway
            row["external_ip"] = external_ip
            if not test_ip or not gateway:
                yield emit("error", f"  {name}: test_ip and gateway are required; skipping")
                row["detail"] = "missing test_ip or gateway"
                yield emit_result(**row)
                fail_count += 1
                continue

            yield emit("info", f"VLAN {vlan} ({name}): finding target subnet on cluster...")
            subnet_ext, sel_note = _find_subnet_extid_for_vlan(
                pc_session, base_url, cluster_uuid, vlan,
                name=(net or {}).get("name") or None,
            )
            if not subnet_ext:
                reason = sel_note or f"no subnet on this cluster matches VLAN {vlan}"
                yield emit("error", f"  {reason}; skipping")
                row["detail"] = reason
                yield emit_result(**row)
                fail_count += 1
                continue
            if sel_note:
                yield emit("warn", f"  subnet selection: {sel_note}")
            row["subnet_ext_id"] = subnet_ext
            yield emit("info", f"  subnet extId: {subnet_ext} (name '{name}', VLAN {vlan})")

            yield emit("info", f"  pointing probe NIC {probe['test_nic_ext_id']} at this subnet...")
            try:
                swap = _pc_swap_nic_subnet(
                    pc_session, base_url,
                    probe["probe_vm_uuid"], probe["test_nic_ext_id"], subnet_ext,
                    mac=probe.get("test_nic_mac"),
                )
            except (RuntimeError, requests.RequestException) as exc:
                yield emit("error", f"  PC NIC swap failed: {exc}")
                row["detail"] = f"PC NIC swap failed: {exc}"
                yield emit_result(**row)
                fail_count += 1
                continue

            # The recreate fallback (PC 7.3 / AHV 10.3) deletes + recreates
            # the NIC, so its extId changes. Re-track the extId — and the MAC
            # captured on first contact — on the in-memory probe and persist
            # to the registry so the next VLAN, and the next run, target the
            # live NIC (and can self-heal a stale extId by MAC).
            new_nic = swap.get("nic_ext_id")
            new_mac = swap.get("mac")
            nic_changed = bool(new_nic and new_nic != probe.get("test_nic_ext_id"))
            mac_changed = bool(new_mac and new_mac != probe.get("test_nic_mac"))
            if nic_changed or mac_changed:
                old_nic = probe.get("test_nic_ext_id")
                if new_nic:
                    probe["test_nic_ext_id"] = new_nic
                    row["test_nic_ext_id"] = new_nic
                if new_mac:
                    probe["test_nic_mac"] = new_mac
                with _probes_lock:
                    rec = PROBES.get(probe_id)
                    if rec is not None:
                        rec["test_nic_ext_id"] = probe.get("test_nic_ext_id")
                        rec["test_nic_mac"] = probe.get("test_nic_mac")
                        _save_probes_unlocked()
                if nic_changed:
                    yield emit(
                        "warn",
                        f"  {swap.get('note') or 'NIC recreated'}: "
                        f"test NIC extId {old_nic} -> {new_nic}",
                    )

            # Give the guest a moment to notice the link bounce on the
            # AHV side before we shove a new IP at it; some kernels race
            # the carrier event vs nmcli's apply.
            time.sleep(2.0)

            yield emit("info", f"  configuring probe iface {effective_test_iface} -> {test_ip}/{prefix} gw {gateway}")
            try:
                r = _probe_request(
                    probe, "POST", "/probe/configure",
                    json_body={
                        "iface": effective_test_iface,
                        "ip": test_ip,
                        "prefix": prefix,
                        "gateway": gateway,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                yield emit("error", f"  probe /configure failed: {exc}")
                row["detail"] = f"probe /configure transport failed: {exc}"
                yield emit_result(**row)
                fail_count += 1
                continue
            try:
                conf = r.json()
            except ValueError:
                conf = {"ok": False, "error": f"non-JSON: {r.text[:200]}"}
            if not conf.get("ok"):
                # Include the HTTP status and a token fingerprint so a 401
                # is immediately distinguishable from an in-guest nmcli
                # failure, and the fingerprint can be matched against the
                # probe's own auth-fail log line.
                yield emit(
                    "error",
                    "  probe /configure rejected: "
                    f"status={r.status_code} error={conf.get('error')} "
                    f"sent_token={_tok_fp(probe.get('token') or '')}",
                )
                row["detail"] = f"/configure rejected (HTTP {r.status_code}): {conf.get('error')}"
                yield emit_result(**row)
                fail_count += 1
                continue
            yield emit("ok", f"    backend={conf.get('backend')}")

            yield emit("info", f"  pinging gw {gateway}{' then ' + external_ip if external_ip else ''}...")
            try:
                r = _probe_request(
                    probe, "POST", "/probe/run-test",
                    json_body={
                        "gateway": gateway,
                        "external_ip": external_ip,
                        "count": 3,
                        "timeout_s": 2,
                    },
                    timeout=30,
                )
            except requests.RequestException as exc:
                yield emit("error", f"  probe /run-test failed: {exc}")
                row["detail"] = f"probe /run-test transport failed: {exc}"
                yield emit_result(**row)
                fail_count += 1
                continue
            try:
                res = r.json()
            except ValueError:
                res = {"gateway_ok": False, "external_ok": False, "raw": r.text[:200]}

            row["gateway_ping"] = "PASS" if res.get("gateway_ok") else "FAIL"
            if external_ip:
                row["external_ping"] = "PASS" if res.get("external_ok") else "FAIL"
            else:
                row["external_ping"] = "SKIP"

            gw_part = "gw OK" if res.get("gateway_ok") else "gw FAIL"
            ext_part = "external OK" if res.get("external_ok") else ("external FAIL" if external_ip else "external skipped")
            if res.get("gateway_ok") and (res.get("external_ok") or not external_ip):
                yield emit("ok", f"  PASS: VLAN {vlan} ({name}) — {gw_part}, {ext_part}")
                row["status"] = "PASS"
                ok_count += 1
            elif res.get("gateway_ok"):
                yield emit("warn", f"  PARTIAL: VLAN {vlan} ({name}) — {gw_part}, {ext_part}")
                row["status"] = "PARTIAL"
                row["detail"] = "gateway OK, external FAIL"
                partial_count += 1
            else:
                gw_raw = ((res.get("gateway") or {}).get("raw") or "")[:200]
                yield emit("error", f"  FAIL: VLAN {vlan} ({name}) — {gw_part}: {gw_raw}")
                row["status"] = "FAIL"
                row["detail"] = f"gateway ping failed: {gw_raw}"
                fail_count += 1

            yield emit_result(**row)

        total = len(networks)
        yield emit(
            "system",
            f"Done. {ok_count} pass, {partial_count} partial, {fail_count} fail (of {total}).",
        )

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
