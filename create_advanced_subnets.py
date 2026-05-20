"""
create_advanced_subnets.py
==========================

Standalone helper for creating VLAN-backed AHV subnets on a specific virtual
switch through the Prism Central v4 REST APIs.

What this script does
---------------------
1. Connects to Prism Central using basic authentication.
2. Resolves a target AHV cluster by name or accepts its UUID directly.
3. Resolves a target virtual switch by name or accepts its UUID directly.
4. Creates one or more unmanaged VLAN subnets with advanced networking enabled:

       "subnetType": "VLAN"
       "networkId": <vlan id>
       "clusterReference": <cluster UUID>
       "virtualSwitchReference": <virtual switch UUID>
       "isAdvancedNetworking": true

Quick configuration
-------------------
Edit the DEFAULT_* values below for a simple run, or override them with CLI
arguments / environment variables. Environment variables use the same names as
the DEFAULT_* constants without the DEFAULT_ prefix, for example PC_HOST.

Example with edited defaults:
    python create_advanced_subnets.py

Example with environment variables:
    set PC_HOST=10.0.0.10
    set PC_USERNAME=admin
    set PC_PASSWORD=<password>
    set CLUSTER_NAME=MyCluster
    set VS_NAME=vs0
    python create_advanced_subnets.py --vlan-start 1001 --vlan-end 1010

Example with explicit non-contiguous subnets:
    python create_advanced_subnets.py ^
        --pc-host 10.0.0.10 ^
        --username admin ^
        --password <password> ^
        --cluster-name MyCluster ^
        --vs-name vs0 ^
        --subnet web_100,100 ^
        --subnet app_220,220

Notes
-----
- TLS verification is disabled by default because Prism Central often uses a
  self-signed certificate in lab environments. Pass --verify-tls to enforce it.
- This script intentionally creates unmanaged subnets only; it does not send
  IPAM, DHCP, or IP pool configuration.
- The script does not fall back to isAdvancedNetworking=false. If advanced
  networking is not supported on the target cluster, the create will fail so
  you can fix the target or choose a different workflow explicitly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from typing import Any

import requests
import urllib3


# ---------------------------------------------------------------------------
# Quick config: edit these values if you prefer running without CLI arguments.
# CLI arguments and environment variables override these defaults.
# ---------------------------------------------------------------------------

DEFAULT_PC_HOST = ""
DEFAULT_PC_PORT = 9440
DEFAULT_PC_USERNAME = "admin"
DEFAULT_PC_PASSWORD = ""

# Provide either a cluster UUID or a cluster name.
DEFAULT_CLUSTER_UUID = ""
DEFAULT_CLUSTER_NAME = ""

# Provide either a virtual switch UUID or a virtual switch name.
DEFAULT_VS_UUID = ""
DEFAULT_VS_NAME = "vs0"

# Used when no --subnet entries are supplied.
DEFAULT_NAME_PREFIX = "network_VLAN_"
DEFAULT_VLAN_START = 1001
DEFAULT_VLAN_END = 1010

# Keep False for typical self-signed Prism Central certificates.
DEFAULT_VERIFY_TLS = False


@dataclass(frozen=True)
class SubnetSpec:
    name: str
    vlan: int


def env_or_default(name: str, default: str | int | bool) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return str(default)
    return value


def build_session(username: str, password: str, verify_tls: bool) -> requests.Session:
    session = requests.Session()
    session.auth = (username, password)
    session.headers.update(
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
    )
    session.verify = verify_tls
    if not verify_tls:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return session


def pc_get(session: requests.Session, base_url: str, path: str) -> dict[str, Any]:
    response = session.get(f"{base_url}{path}", timeout=30)
    response.raise_for_status()
    return response.json() or {}


def resolve_cluster_uuid(
    session: requests.Session,
    base_url: str,
    cluster_name: str,
) -> str:
    """Resolve an AHV cluster extId from its Prism Central display name."""
    escaped = cluster_name.replace("'", "''")
    odata_filter = urllib.parse.quote(f"name eq '{escaped}'")
    payload = pc_get(
        session,
        base_url,
        f"/api/clustermgmt/v4.0/config/clusters?$filter={odata_filter}&$limit=100",
    )
    candidates = payload.get("data") or []
    exact_matches = [
        cluster
        for cluster in candidates
        if (cluster.get("name") or "").lower() == cluster_name.lower()
    ]
    if not exact_matches:
        raise RuntimeError(f"No cluster found with name '{cluster_name}'")
    if len(exact_matches) > 1:
        raise RuntimeError(
            f"Multiple clusters named '{cluster_name}'; use --cluster-uuid"
        )
    ext_id = exact_matches[0].get("extId")
    if not ext_id:
        raise RuntimeError(f"Cluster '{cluster_name}' has no extId in response")
    return ext_id


def virtual_switch_clusters(virtual_switch: dict[str, Any]) -> set[str]:
    cluster_ids: set[str] = set()
    for cluster in virtual_switch.get("clusters") or []:
        if isinstance(cluster, dict):
            ext_id = cluster.get("extId") or cluster.get("uuid")
            if ext_id:
                cluster_ids.add(ext_id)
        elif isinstance(cluster, str):
            cluster_ids.add(cluster)
    return cluster_ids


def resolve_virtual_switch_uuid(
    session: requests.Session,
    base_url: str,
    vs_name: str,
    cluster_uuid: str,
) -> str:
    """Resolve a virtual switch extId by name, preferring switches on the cluster."""
    payload = pc_get(
        session,
        base_url,
        "/api/networking/v4.0/config/virtual-switches?$limit=100",
    )
    matches: list[dict[str, Any]] = []
    for virtual_switch in payload.get("data") or []:
        if (virtual_switch.get("name") or "").lower() != vs_name.lower():
            continue
        cluster_ids = virtual_switch_clusters(virtual_switch)
        if cluster_ids and cluster_uuid not in cluster_ids:
            continue
        matches.append(virtual_switch)
    if not matches:
        raise RuntimeError(
            f"No virtual switch named '{vs_name}' found for cluster {cluster_uuid}"
        )
    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple virtual switches named '{vs_name}'; use --vs-uuid"
        )
    ext_id = matches[0].get("extId")
    if not ext_id:
        raise RuntimeError(f"Virtual switch '{vs_name}' has no extId in response")
    return ext_id


def parse_subnet_entry(entry: str) -> SubnetSpec:
    parts = [part.strip() for part in entry.replace("\t", ",").split(",")]
    parts = [part for part in parts if part]
    if len(parts) != 2:
        raise ValueError(f"invalid subnet entry '{entry}', expected name,vlan")
    name, vlan_raw = parts
    try:
        vlan = int(vlan_raw)
    except ValueError as exc:
        raise ValueError(f"invalid VLAN '{vlan_raw}' for subnet '{name}'") from exc
    return SubnetSpec(name=name, vlan=vlan)


def build_subnet_specs(args: argparse.Namespace) -> list[SubnetSpec]:
    if args.subnet:
        specs = [parse_subnet_entry(entry) for entry in args.subnet]
    else:
        if args.vlan_end < args.vlan_start:
            raise ValueError("--vlan-end must be greater than or equal to --vlan-start")
        specs = [
            SubnetSpec(name=f"{args.name_prefix}{vlan}", vlan=vlan)
            for vlan in range(args.vlan_start, args.vlan_end + 1)
        ]

    seen_names: set[str] = set()
    validated: list[SubnetSpec] = []
    for spec in specs:
        if not spec.name:
            raise ValueError("subnet names must be non-empty")
        if spec.vlan < 0 or spec.vlan > 4094:
            raise ValueError(
                f"subnet '{spec.name}' has VLAN {spec.vlan}; expected 0..4094"
            )
        name_key = spec.name.lower()
        if name_key in seen_names:
            raise ValueError(f"duplicate subnet name in request: '{spec.name}'")
        seen_names.add(name_key)
        validated.append(spec)
    return validated


def create_advanced_vlan_subnet(
    session: requests.Session,
    base_url: str,
    spec: SubnetSpec,
    cluster_uuid: str,
    vs_uuid: str,
) -> tuple[int, dict[str, Any]]:
    body = {
        "name": spec.name,
        "description": (
            f"Advanced unmanaged VLAN {spec.vlan} subnet "
            f"(created via create_advanced_subnets.py)"
        ),
        "subnetType": "VLAN",
        "networkId": spec.vlan,
        "clusterReference": cluster_uuid,
        "virtualSwitchReference": vs_uuid,
        "isExternal": False,
        "isAdvancedNetworking": True,
    }
    response = session.post(
        f"{base_url}/api/networking/v4.0/config/subnets",
        json=body,
        headers={"NTNX-Request-Id": str(uuid.uuid4())},
        timeout=60,
    )
    try:
        payload = response.json()
    except ValueError:
        payload = {"raw": response.text}
    return response.status_code, payload


def extract_task_ext_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        if isinstance(data.get("extId"), str):
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
    timeout_seconds: int,
    poll_interval_seconds: float = 2.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status = "UNKNOWN"
    while time.time() < deadline:
        response = session.get(
            f"{base_url}/api/prism/v4.0/config/tasks/{task_ext_id}",
            timeout=30,
        )
        response.raise_for_status()
        body = response.json() or {}
        data = body.get("data") or {}
        last_status = data.get("status") or last_status
        if last_status in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}:
            return data
        time.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"Task {task_ext_id} did not finish within {timeout_seconds}s "
        f"(last status: {last_status})"
    )


def format_api_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:500]
    data = payload.get("data") or payload
    candidates = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            candidates = error.get("messageList")
        candidates = candidates or data.get("messageList") or data.get("errorMessages")
    if isinstance(candidates, list):
        messages: list[str] = []
        for item in candidates:
            if isinstance(item, dict):
                msg = item.get("message") or item.get("description")
                if msg:
                    messages.append(str(msg))
            else:
                messages.append(str(item))
        if messages:
            return "; ".join(messages)
    return json.dumps(data)[:500]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create advanced networking VLAN subnets on a specified AHV "
            "virtual switch through Prism Central v4 APIs."
        )
    )
    parser.add_argument(
        "--pc-host",
        default=env_or_default("PC_HOST", DEFAULT_PC_HOST),
        help="Prism Central FQDN or IP (env: PC_HOST).",
    )
    parser.add_argument(
        "--pc-port",
        type=int,
        default=int(env_or_default("PC_PORT", DEFAULT_PC_PORT)),
        help="Prism Central port (env: PC_PORT, default: 9440).",
    )
    parser.add_argument(
        "--username",
        default=env_or_default("PC_USERNAME", DEFAULT_PC_USERNAME),
        help="Prism Central username (env: PC_USERNAME, default: admin).",
    )
    parser.add_argument(
        "--password",
        default=env_or_default("PC_PASSWORD", DEFAULT_PC_PASSWORD),
        help="Prism Central password (env: PC_PASSWORD).",
    )
    parser.add_argument(
        "--cluster-uuid",
        default=env_or_default("CLUSTER_UUID", DEFAULT_CLUSTER_UUID),
        help="Target AHV cluster extId/UUID (env: CLUSTER_UUID).",
    )
    parser.add_argument(
        "--cluster-name",
        default=env_or_default("CLUSTER_NAME", DEFAULT_CLUSTER_NAME),
        help="Target AHV cluster name to resolve (env: CLUSTER_NAME).",
    )
    parser.add_argument(
        "--vs-uuid",
        default=env_or_default("VS_UUID", DEFAULT_VS_UUID),
        help="Target virtual switch extId/UUID (env: VS_UUID).",
    )
    parser.add_argument(
        "--vs-name",
        default=env_or_default("VS_NAME", DEFAULT_VS_NAME),
        help="Target virtual switch name to resolve (env: VS_NAME, default: vs0).",
    )
    parser.add_argument(
        "--name-prefix",
        default=env_or_default("NAME_PREFIX", DEFAULT_NAME_PREFIX),
        help="Name prefix for generated range entries (env: NAME_PREFIX).",
    )
    parser.add_argument(
        "--vlan-start",
        type=int,
        default=int(env_or_default("VLAN_START", DEFAULT_VLAN_START)),
        help="First VLAN for generated range entries (env: VLAN_START).",
    )
    parser.add_argument(
        "--vlan-end",
        type=int,
        default=int(env_or_default("VLAN_END", DEFAULT_VLAN_END)),
        help="Last VLAN for generated range entries (env: VLAN_END).",
    )
    parser.add_argument(
        "--subnet",
        action="append",
        help=(
            "Explicit subnet in name,vlan format. Repeat for multiple entries. "
            "When supplied, --vlan-start/--vlan-end are ignored."
        ),
    )
    parser.add_argument(
        "--verify-tls",
        action="store_true",
        default=DEFAULT_VERIFY_TLS,
        help="Verify the Prism Central TLS certificate (default: disabled).",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not poll the Prism Central task after each create request.",
    )
    parser.add_argument(
        "--task-timeout",
        type=int,
        default=int(env_or_default("TASK_TIMEOUT", 120)),
        help="Per-subnet task wait timeout in seconds (env: TASK_TIMEOUT).",
    )
    return parser.parse_args()


def require_configuration(args: argparse.Namespace) -> None:
    missing: list[str] = []
    if not args.pc_host:
        missing.append("--pc-host or PC_HOST")
    if not args.username:
        missing.append("--username or PC_USERNAME")
    if not args.password:
        missing.append("--password or PC_PASSWORD")
    if not args.cluster_uuid and not args.cluster_name:
        missing.append("--cluster-uuid/CLUSTER_UUID or --cluster-name/CLUSTER_NAME")
    if not args.vs_uuid and not args.vs_name:
        missing.append("--vs-uuid/VS_UUID or --vs-name/VS_NAME")
    if missing:
        raise ValueError("missing required configuration: " + ", ".join(missing))


def main() -> int:
    args = parse_args()
    try:
        require_configuration(args)
        subnet_specs = build_subnet_specs(args)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    base_url = f"https://{args.pc_host}:{args.pc_port}"
    session = build_session(args.username, args.password, args.verify_tls)

    try:
        if args.cluster_uuid:
            cluster_uuid = args.cluster_uuid
            print(f"Using cluster UUID: {cluster_uuid}")
        else:
            print(f"Resolving cluster UUID for '{args.cluster_name}'...")
            cluster_uuid = resolve_cluster_uuid(session, base_url, args.cluster_name)
            print(f"  -> {cluster_uuid}")

        if args.vs_uuid:
            vs_uuid = args.vs_uuid
            print(f"Using virtual switch UUID: {vs_uuid}")
        else:
            print(f"Resolving virtual switch UUID for '{args.vs_name}'...")
            vs_uuid = resolve_virtual_switch_uuid(
                session, base_url, args.vs_name, cluster_uuid
            )
            print(f"  -> {vs_uuid}")
    except (requests.RequestException, RuntimeError) as exc:
        print(f"ERROR: lookup failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"\nCreating {len(subnet_specs)} advanced VLAN subnet(s) "
        f"on cluster {cluster_uuid}, virtual switch {vs_uuid}."
    )

    successes: list[str] = []
    failures: list[tuple[str, str]] = []

    for spec in subnet_specs:
        print(
            f"\nCreating subnet '{spec.name}' "
            f"(VLAN {spec.vlan}, isAdvancedNetworking=true)..."
        )
        try:
            status, payload = create_advanced_vlan_subnet(
                session, base_url, spec, cluster_uuid, vs_uuid
            )
        except requests.RequestException as exc:
            reason = f"HTTP error: {exc}"
            print(f"  ! FAIL: {reason}")
            failures.append((spec.name, reason))
            continue

        if status not in (200, 201, 202):
            reason = f"HTTP {status}: {format_api_error(payload)}"
            print(f"  ! FAIL: {reason}")
            failures.append((spec.name, reason))
            continue

        task_id = extract_task_ext_id(payload)
        if args.no_wait:
            note = f"task {task_id}" if task_id else f"HTTP {status}, no task id"
            print(f"  OK: accepted ({note}; task polling skipped).")
            successes.append(spec.name)
            continue
        if not task_id:
            print(f"  OK: accepted (HTTP {status}, no task id returned).")
            successes.append(spec.name)
            continue

        print(f"  -> task {task_id}, waiting...")
        try:
            task = wait_for_task(
                session, base_url, task_id, timeout_seconds=args.task_timeout
            )
        except (TimeoutError, requests.RequestException) as exc:
            reason = f"task wait failed: {exc}"
            print(f"  ! FAIL: {reason}")
            failures.append((spec.name, reason))
            continue

        task_status = task.get("status")
        if task_status == "SUCCEEDED":
            print("  OK: created.")
            successes.append(spec.name)
        else:
            reason = f"task {task_status}: {format_api_error({'data': task})}"
            print(f"  ! FAIL: {reason}")
            failures.append((spec.name, reason))

    print("\n=== Summary ===")
    print(f"  Created/accepted : {len(successes)}")
    for name in successes:
        print(f"    + {name}")
    if failures:
        print(f"  Failed           : {len(failures)}")
        for name, reason in failures:
            print(f"    - {name}: {reason}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
