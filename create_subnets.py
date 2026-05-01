"""
create_subnets.py
=================

Programmatically create 10 unmanaged AHV subnets on a Nutanix cluster
using the Prism Central v4 REST APIs.

It creates:
    network_VLAN_1001  (VLAN 1001)
    network_VLAN_1002  (VLAN 1002)
    ...
    network_VLAN_1010  (VLAN 1010)

"Unmanaged" here means a plain VLAN-backed L2 subnet with no IPAM /
DHCP block configured (no `ipConfig` is sent in the request body).

Usage
-----
    # Set credentials via env vars (recommended), then run:
    set PC_HOST=10.0.0.10
    set PC_USERNAME=admin
    set PC_PASSWORD=<password>
    python create_subnets.py --cluster-name MyCluster

    # Or pass everything on the command line:
    python create_subnets.py \\
        --pc-host 10.0.0.10 \\
        --username admin \\
        --password <password> \\
        --cluster-uuid 0005f1ab-1234-5678-9abc-def012345678

    # Override range / naming:
    python create_subnets.py --cluster-name MyCluster \\
        --vlan-start 2000 --vlan-end 2009 --name-prefix net_VLAN_

APIs used
---------
- GET  /api/clustermgmt/v4.0/config/clusters            (resolve cluster UUID)
- POST /api/networking/v4.0/config/subnets              (create subnet)
- GET  /api/prism/v4.0/config/tasks/{extId}             (poll task status)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import uuid
from typing import Any

import requests
import urllib3


def build_session(username: str, password: str, verify_tls: bool) -> requests.Session:
    s = requests.Session()
    s.auth = (username, password)
    s.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    s.verify = verify_tls
    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return s


def resolve_cluster_uuid(
    session: requests.Session, base_url: str, cluster_name: str
) -> str:
    """Look up a cluster's extId (UUID) by name via the clustermgmt v4 API."""
    odata_filter = f"name eq '{cluster_name}'"
    url = (
        f"{base_url}/api/clustermgmt/v4.0/config/clusters"
        f"?$filter={urllib.parse.quote(odata_filter)}"
    )
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    payload = resp.json() or {}
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"No cluster found with name '{cluster_name}'")
    matches = [
        c for c in data if (c.get("name") or "").lower() == cluster_name.lower()
    ]
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple clusters named '{cluster_name}'; pass --cluster-uuid instead"
        )
    chosen = matches[0] if matches else data[0]
    ext_id = chosen.get("extId")
    if not ext_id:
        raise RuntimeError(f"Cluster '{cluster_name}' has no extId in response")
    return ext_id


def create_unmanaged_vlan_subnet(
    session: requests.Session,
    base_url: str,
    name: str,
    vlan_id: int,
    cluster_uuid: str,
) -> tuple[int, dict[str, Any]]:
    """POST a single unmanaged VLAN subnet. Returns (status_code, json_body)."""
    body = {
        "name": name,
        "description": f"Unmanaged VLAN {vlan_id} subnet (created via v4 API)",
        "subnetType": "VLAN",
        "networkId": vlan_id,
        "clusterReference": cluster_uuid,
        "isExternal": False,
        "isAdvancedNetworking": False,
    }
    headers = {
        "NTNX-Request-Id": str(uuid.uuid4()),
    }
    url = f"{base_url}/api/networking/v4.0/config/subnets"
    resp = session.post(url, json=body, headers=headers, timeout=60)
    try:
        payload = resp.json()
    except ValueError:
        payload = {"raw": resp.text}
    return resp.status_code, payload


def extract_task_ext_id(payload: dict[str, Any]) -> str | None:
    """Pull the task extId out of a v4 ApiResponse, if present."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        if "extId" in data and isinstance(data["extId"], str):
            return data["extId"]
        for key in ("taskReference", "task"):
            ref = data.get(key)
            if isinstance(ref, dict) and isinstance(ref.get("extId"), str):
                return ref["extId"]
    return None


def wait_for_task(
    session: requests.Session,
    base_url: str,
    task_ext_id: str,
    timeout_seconds: int = 120,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    url = f"{base_url}/api/prism/v4.0/config/tasks/{task_ext_id}"
    deadline = time.time() + timeout_seconds
    last_status = "UNKNOWN"
    while time.time() < deadline:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
        body = resp.json() or {}
        data = body.get("data") or {}
        last_status = data.get("status") or last_status
        if last_status in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}:
            return data
        time.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"Task {task_ext_id} did not finish within {timeout_seconds}s "
        f"(last status: {last_status})"
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Create 10 unmanaged AHV VLAN subnets via Nutanix v4 REST APIs."
    )
    p.add_argument(
        "--pc-host",
        default=os.environ.get("PC_HOST"),
        help="Prism Central FQDN or IP (env: PC_HOST)",
    )
    p.add_argument(
        "--pc-port",
        type=int,
        default=int(os.environ.get("PC_PORT", "9440")),
        help="Prism Central port (default: 9440)",
    )
    p.add_argument(
        "--username",
        default=os.environ.get("PC_USERNAME", "admin"),
        help="PC username (env: PC_USERNAME, default: admin)",
    )
    p.add_argument(
        "--password",
        default=os.environ.get("PC_PASSWORD"),
        help="PC password (env: PC_PASSWORD)",
    )
    cluster = p.add_mutually_exclusive_group(required=True)
    cluster.add_argument(
        "--cluster-uuid",
        help="Target AHV cluster extId (UUID).",
    )
    cluster.add_argument(
        "--cluster-name",
        help="Target AHV cluster name (resolved to UUID via /clustermgmt v4).",
    )
    p.add_argument(
        "--name-prefix",
        default="network_VLAN_",
        help="Subnet name prefix (default: 'network_VLAN_').",
    )
    p.add_argument(
        "--vlan-start", type=int, default=1001, help="First VLAN id (default: 1001)."
    )
    p.add_argument(
        "--vlan-end", type=int, default=1010, help="Last VLAN id (default: 1010)."
    )
    p.add_argument(
        "--verify-tls",
        action="store_true",
        help="Verify Prism Central TLS certificate (default: off, PC is usually self-signed).",
    )
    p.add_argument(
        "--no-wait",
        action="store_true",
        help="Don't poll task status after each create.",
    )
    p.add_argument(
        "--task-timeout",
        type=int,
        default=120,
        help="Per-subnet task wait timeout in seconds (default: 120).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    if not args.pc_host:
        print("ERROR: --pc-host (or PC_HOST env var) is required.", file=sys.stderr)
        return 2
    if not args.password:
        print(
            "ERROR: --password (or PC_PASSWORD env var) is required.", file=sys.stderr
        )
        return 2
    if args.vlan_end < args.vlan_start:
        print("ERROR: --vlan-end must be >= --vlan-start", file=sys.stderr)
        return 2

    base_url = f"https://{args.pc_host}:{args.pc_port}"
    session = build_session(args.username, args.password, args.verify_tls)

    if args.cluster_uuid:
        cluster_uuid = args.cluster_uuid
        print(f"Using cluster UUID: {cluster_uuid}")
    else:
        print(f"Resolving cluster UUID for name '{args.cluster_name}'...")
        cluster_uuid = resolve_cluster_uuid(session, base_url, args.cluster_name)
        print(f"  -> {cluster_uuid}")

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    for vlan_id in range(args.vlan_start, args.vlan_end + 1):
        name = f"{args.name_prefix}{vlan_id}"
        print(f"\nCreating subnet '{name}' (VLAN {vlan_id})...")
        try:
            status, payload = create_unmanaged_vlan_subnet(
                session, base_url, name, vlan_id, cluster_uuid
            )
        except requests.RequestException as exc:
            print(f"  ! HTTP error: {exc}")
            failures.append((name, str(exc)))
            continue

        if status not in (200, 201, 202):
            err = payload.get("data") or payload
            print(f"  ! HTTP {status}: {json.dumps(err)[:500]}")
            failures.append((name, f"HTTP {status}"))
            continue

        task_id = extract_task_ext_id(payload)
        if task_id and not args.no_wait:
            print(f"  -> task {task_id}, waiting...")
            try:
                task = wait_for_task(
                    session, base_url, task_id, timeout_seconds=args.task_timeout
                )
            except (TimeoutError, requests.RequestException) as exc:
                print(f"  ! task wait failed: {exc}")
                failures.append((name, str(exc)))
                continue
            task_status = task.get("status")
            if task_status == "SUCCEEDED":
                print(f"  OK: {name} created (task {task_id} SUCCEEDED).")
                successes.append(name)
            else:
                err_detail = task.get("errorMessages") or task.get("status")
                print(f"  ! task {task_status}: {err_detail}")
                failures.append((name, f"task {task_status}"))
        else:
            note = "task polling skipped" if args.no_wait else "no task id returned"
            print(f"  OK: HTTP {status} ({note}).")
            successes.append(name)

    print("\n=== Summary ===")
    print(f"  Created/accepted : {len(successes)}")
    for n in successes:
        print(f"    + {n}")
    if failures:
        print(f"  Failed           : {len(failures)}")
        for n, why in failures:
            print(f"    - {n}: {why}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
