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
import secrets
import threading
import time
import uuid
from typing import Any

import requests
import urllib3
from flask import Blueprint, Response, jsonify, request, stream_with_context

import app as _app  # circular-safe at import time: only attributes used at call time

master_probe_bp = Blueprint("master_probe", __name__)

# Probe registry. Lives in process memory like the other SESSIONS dicts
# in app.py; bouncing the process clears it.
PROBES: dict[str, dict[str, Any]] = {}
_probes_lock = threading.Lock()


def _redacted_probe(p: dict[str, Any]) -> dict[str, Any]:
    """Strip the bearer token before exposing a probe to the UI."""
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "mgmt_host": p.get("mgmt_host"),
        "mgmt_port": p.get("mgmt_port"),
        "use_https": p.get("use_https"),
        "probe_vm_uuid": p.get("probe_vm_uuid"),
        "test_nic_ext_id": p.get("test_nic_ext_id"),
        "created_at": p.get("created_at"),
    }


def _probe_base_url(p: dict[str, Any]) -> str:
    scheme = "https" if p.get("use_https") else "http"
    return f"{scheme}://{p['mgmt_host']}:{int(p['mgmt_port'])}"


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
        "created_at": time.time(),
    }
    with _probes_lock:
        PROBES[probe_id] = record
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
        for field in ("name", "probe_vm_uuid", "test_nic_ext_id", "token"):
            if field in body and body[field] is not None:
                val = body[field]
                if isinstance(val, str):
                    val = val.strip()
                p[field] = val or None
        snapshot = dict(p)
    return jsonify(probe=_redacted_probe(snapshot))


@master_probe_bp.post("/api/probes/delete")
def api_probes_delete() -> Any:
    body = request.get_json(force=True, silent=True) or {}
    probe_id = (body.get("probe_id") or "").strip()
    with _probes_lock:
        p = PROBES.pop(probe_id, None)
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


def _pc_swap_nic_subnet(
    session: requests.Session,
    base_url: str,
    vm_ext_id: str,
    nic_ext_id: str,
    subnet_ext_id: str,
) -> dict[str, Any]:
    """Re-point an existing NIC at a new subnet.

    PC v4 treats PUT /vms/{vmExtId}/nics/{nicExtId} as a full-object
    replacement, so we have to round-trip the *complete* NIC body (incl.
    MAC, vlanMode, trunk list, IP config) and only mutate the subnet
    reference. Anything else gets reset to defaults server-side — most
    visibly, the MAC becomes EMPTY and the task fails with "VM NIC MAC
    address: EMPTY".
    """
    nic, etag = _pc_get_nic(session, base_url, vm_ext_id, nic_ext_id)
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
    if r.status_code not in (200, 201, 202):
        try:
            err = r.json()
        except ValueError:
            err = {"raw": r.text}
        raise RuntimeError(
            f"PUT nic failed HTTP {r.status_code}: "
            f"{_app._format_api_error(err)}"
        )
    try:
        payload = r.json() or {}
    except ValueError:
        payload = {}
    task_id = _app._extract_task_ext_id(payload)
    if not task_id:
        return {"status": "SUCCEEDED", "note": "no task id returned"}
    task = _app._wait_for_task(session, base_url, task_id, timeout_seconds=120)
    if task.get("status") != "SUCCEEDED":
        raise RuntimeError(
            f"NIC swap task ended {task.get('status')}: "
            f"{_app._format_api_error({'data': task})}"
        )
    return task


def _find_subnet_extid_for_vlan(
    session: requests.Session,
    base_url: str,
    cluster_uuid: str,
    vlan: int,
) -> str | None:
    """Find the (first) subnet on `cluster_uuid` whose `networkId == vlan`."""
    try:
        r = session.get(
            f"{base_url}/api/networking/v4.0/config/subnets?$limit=100",
            timeout=20,
        )
        if r.status_code >= 400:
            return None
        data = (r.json() or {}).get("data") or []
        for s in data:
            if s.get("clusterReference") != cluster_uuid:
                continue
            if s.get("networkId") == vlan:
                return s.get("extId")
        return None
    except requests.RequestException:
        return None


# ---------------------------------------------------------------------------
# Endpoints for probe-VM NIC introspection (used by the UI to populate the
# "which vNIC is the test NIC" picker after register)
# ---------------------------------------------------------------------------


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

    def gen():
        ok_count = 0
        partial_count = 0
        fail_count = 0
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
            try:
                vlan = int((net or {}).get("vlan"))
            except (TypeError, ValueError):
                yield emit("error", f"  {name}: invalid VLAN; skipping")
                fail_count += 1
                continue
            test_ip = (net or {}).get("test_ip") or ""
            try:
                prefix = int((net or {}).get("prefix"))
            except (TypeError, ValueError):
                yield emit("error", f"  {name}: invalid prefix; skipping")
                fail_count += 1
                continue
            gateway = (net or {}).get("gateway") or ""
            external_ip = (net or {}).get("external_ip") or ""
            if not test_ip or not gateway:
                yield emit("error", f"  {name}: test_ip and gateway are required; skipping")
                fail_count += 1
                continue

            yield emit("info", f"VLAN {vlan} ({name}): finding target subnet on cluster...")
            subnet_ext = _find_subnet_extid_for_vlan(pc_session, base_url, cluster_uuid, vlan)
            if not subnet_ext:
                yield emit("error", f"  no subnet on this cluster matches VLAN {vlan}; skipping")
                fail_count += 1
                continue
            yield emit("info", f"  subnet extId: {subnet_ext}")

            yield emit("info", f"  pointing probe NIC {probe['test_nic_ext_id']} at this subnet...")
            try:
                _pc_swap_nic_subnet(
                    pc_session, base_url,
                    probe["probe_vm_uuid"], probe["test_nic_ext_id"], subnet_ext,
                )
            except (RuntimeError, requests.RequestException) as exc:
                yield emit("error", f"  PC NIC swap failed: {exc}")
                fail_count += 1
                continue

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
                fail_count += 1
                continue
            try:
                conf = r.json()
            except ValueError:
                conf = {"ok": False, "error": f"non-JSON: {r.text[:200]}"}
            if not conf.get("ok"):
                yield emit("error", f"  probe /configure rejected: {conf.get('error')}")
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
                fail_count += 1
                continue
            try:
                res = r.json()
            except ValueError:
                res = {"gateway_ok": False, "external_ok": False, "raw": r.text[:200]}

            gw_part = "gw OK" if res.get("gateway_ok") else "gw FAIL"
            ext_part = "external OK" if res.get("external_ok") else ("external FAIL" if external_ip else "external skipped")
            if res.get("gateway_ok") and (res.get("external_ok") or not external_ip):
                yield emit("ok", f"  PASS: VLAN {vlan} ({name}) — {gw_part}, {ext_part}")
                ok_count += 1
            elif res.get("gateway_ok"):
                yield emit("warn", f"  PARTIAL: VLAN {vlan} ({name}) — {gw_part}, {ext_part}")
                partial_count += 1
            else:
                gw_raw = ((res.get("gateway") or {}).get("raw") or "")[:200]
                yield emit("error", f"  FAIL: VLAN {vlan} ({name}) — {gw_part}: {gw_raw}")
                fail_count += 1

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
