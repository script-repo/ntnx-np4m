"""
Probe-side network interface configurator.

The probe VM has two NICs:
- a management NIC with a static IP — the master talks to it here.
- a "probe" NIC whose subnet/IP changes per VLAN under test.

This module reconfigures the probe NIC inside the guest. It is the second
half of the per-VLAN test sequence; the *first* half (re-pointing the
probe vNIC at a new AHV subnet) is driven by the master via Prism Central
v4 VM APIs, so by the time we get here the NIC is already on the right
L2, we just need to give it the right L3 settings.

We pick a backend in this priority order:
  1. NetworkManager (`nmcli`)
  2. systemd-networkd (`networkctl reload`) - only when an existing
     drop-in shows the iface is managed by networkd
  3. raw `ip addr / ip route` (works everywhere with iproute2)

We deliberately refuse to touch the management interface; otherwise a
typo in the master could pull the rug out from under the active session.

All apply functions return a dict shaped:
    {
        "ok": bool,
        "backend": "nmcli" | "ip" | ...,
        "iface": str,
        "applied": {"ip": "...", "prefix": ..., "gateway": "..."},
        "output": str,        # raw stdout+stderr from the commands run
        "error": str | None,
    }
"""

from __future__ import annotations

import ipaddress
import os
import platform
import shutil
import socket
import subprocess
from typing import Any

import probe_config as _probe_config

# Re-exported for backward compatibility with earlier code paths that
# imported the env-var names from this module.
MGMT_IFACE_ENV = _probe_config.MGMT_IFACE_ENV
TEST_IFACE_ENV = _probe_config.TEST_IFACE_ENV


class IfaceError(Exception):
    pass


def get_mgmt_iface() -> str | None:
    """Persistent config wins, env-var is the seed default."""
    return _probe_config.get_mgmt_iface()


def get_test_iface() -> str | None:
    """Persistent config wins, env-var is the seed default."""
    return _probe_config.get_test_iface()


def list_interfaces() -> list[dict[str, Any]]:
    """Best-effort inventory of local interfaces with their current IPs.
    Used by `/probe/health` so the operator can see what's there.
    Returns `[]` if iproute2 / equivalent isn't available.
    """
    out: list[dict[str, Any]] = []
    if shutil.which("ip"):
        try:
            p = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                capture_output=True, text=True, timeout=5,
            )
            for line in (p.stdout or "").splitlines():
                # "2: ens4    inet 10.1.2.3/24 brd ..."
                parts = line.split()
                if len(parts) < 4:
                    continue
                name = parts[1]
                if name == "lo":
                    continue
                addr = parts[3] if "/" in parts[3] else None
                out.append({"name": name, "ipv4": addr})
        except Exception:
            pass
    if not out:
        try:
            host_ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            host_ip = None
        if host_ip:
            out.append({"name": "(default)", "ipv4": host_ip})
    return out


def _run(cmd: list[str], *, timeout: int = 10) -> tuple[int, str]:
    """Run a subprocess, return (rc, combined_output). Never raises."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return p.returncode, ((p.stdout or "") + (p.stderr or "")).strip()
    except FileNotFoundError as exc:
        return 127, f"executable not found: {exc}"
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s"
    except Exception as exc:
        return 1, f"unexpected error: {exc}"


def _validate(iface: str, ip: str, prefix: int, gateway: str) -> None:
    if not iface or not isinstance(iface, str):
        raise IfaceError("iface is required")
    if iface == get_mgmt_iface():
        raise IfaceError(
            f"refusing to reconfigure the management iface ({iface}); "
            f"the master must target the probe iface instead"
        )
    try:
        ipaddress.IPv4Address(ip)
    except ValueError as exc:
        raise IfaceError(f"invalid ip {ip!r}: {exc}") from exc
    if not isinstance(prefix, int) or prefix < 1 or prefix > 32:
        raise IfaceError(f"invalid prefix {prefix!r}; must be 1..32")
    if gateway:
        try:
            ipaddress.IPv4Address(gateway)
        except ValueError as exc:
            raise IfaceError(f"invalid gateway {gateway!r}: {exc}") from exc
    try:
        net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
        if gateway and ipaddress.IPv4Address(gateway) not in net:
            # Allow it, but surface as a warning in the output. Some
            # lab setups use a gateway outside the local /N with a
            # /32 trick; refusing here is too strict.
            pass
    except ValueError:
        pass


def _detect_backend() -> str:
    """Pick a backend. We don't auto-fall-back at apply-time on Linux —
    if the operator has nmcli, we use it consistently."""
    if not platform.system().lower().startswith("linux"):
        return "stub"
    if shutil.which("nmcli"):
        return "nmcli"
    if shutil.which("ip"):
        return "ip"
    return "stub"


def apply_static(
    iface: str,
    ip: str,
    prefix: int,
    gateway: str,
    *,
    backend: str | None = None,
) -> dict[str, Any]:
    """Reconfigure `iface` with a static IPv4 + default gateway.

    Returns the result dict described at the top of this module. Never
    raises — failures surface as `{"ok": False, "error": "..."}` so the
    HTTP layer can serialize them straight back to the master.
    """
    try:
        _validate(iface, ip, prefix, gateway)
    except IfaceError as exc:
        return {
            "ok": False,
            "backend": "validate",
            "iface": iface,
            "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
            "output": "",
            "error": str(exc),
        }

    backend = backend or _detect_backend()
    if backend == "nmcli":
        return _apply_nmcli(iface, ip, prefix, gateway)
    if backend == "ip":
        return _apply_iproute2(iface, ip, prefix, gateway)
    return {
        "ok": False,
        "backend": backend,
        "iface": iface,
        "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
        "output": "",
        "error": (
            f"no usable backend on this OS ({platform.system()}); install "
            f"NetworkManager or iproute2, or run the probe on Linux"
        ),
    }


# ---------------------------------------------------------------------------
# Backend: NetworkManager via nmcli
# ---------------------------------------------------------------------------


def _nmcli_conn_for_iface(iface: str) -> str | None:
    """Find the NM connection profile name bound to `iface`. Returns None
    if NM doesn't manage this device (we'll create a fresh profile)."""
    rc, out = _run(
        ["nmcli", "-t", "-f", "GENERAL.CONNECTION", "device", "show", iface],
        timeout=5,
    )
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("GENERAL.CONNECTION:"):
            val = line.split(":", 1)[1].strip()
            if val and val != "--":
                return val
    return None


def _apply_nmcli(
    iface: str, ip: str, prefix: int, gateway: str,
) -> dict[str, Any]:
    log: list[str] = []
    conn = _nmcli_conn_for_iface(iface)
    if not conn:
        conn = f"np4m-probe-{iface}"
        rc, out = _run(
            [
                "nmcli", "connection", "add",
                "type", "ethernet",
                "ifname", iface,
                "con-name", conn,
                "ipv4.method", "manual",
                "ipv4.addresses", f"{ip}/{prefix}",
                "ipv4.gateway", gateway or "",
                "ipv6.method", "ignore",
            ],
            timeout=10,
        )
        log.append(f"$ nmcli connection add ... -> rc={rc}\n{out}")
        if rc != 0:
            return {
                "ok": False, "backend": "nmcli", "iface": iface,
                "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
                "output": "\n".join(log),
                "error": f"nmcli connection add failed (rc={rc})",
            }
    else:
        rc, out = _run(
            [
                "nmcli", "connection", "modify", conn,
                "ipv4.method", "manual",
                "ipv4.addresses", f"{ip}/{prefix}",
                "ipv4.gateway", gateway or "",
                "ipv6.method", "ignore",
            ],
            timeout=10,
        )
        log.append(f"$ nmcli connection modify {conn} ... -> rc={rc}\n{out}")
        if rc != 0:
            return {
                "ok": False, "backend": "nmcli", "iface": iface,
                "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
                "output": "\n".join(log),
                "error": f"nmcli connection modify failed (rc={rc})",
            }

    rc, out = _run(["nmcli", "connection", "down", conn], timeout=10)
    log.append(f"$ nmcli connection down {conn} -> rc={rc}\n{out}")
    rc, out = _run(["nmcli", "connection", "up", conn], timeout=20)
    log.append(f"$ nmcli connection up {conn} -> rc={rc}\n{out}")
    if rc != 0:
        return {
            "ok": False, "backend": "nmcli", "iface": iface,
            "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
            "output": "\n".join(log),
            "error": f"nmcli connection up failed (rc={rc})",
        }
    return {
        "ok": True, "backend": "nmcli", "iface": iface,
        "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
        "output": "\n".join(log),
        "error": None,
    }


# ---------------------------------------------------------------------------
# Backend: raw iproute2 ("ip" command)
#
# This is the lowest-common-denominator path. It will NOT survive a reboot
# (which is fine: the probe VM stays up across runs and only the test NIC
# is being touched). The master always re-applies the IP before the next
# test anyway.
# ---------------------------------------------------------------------------


def _apply_iproute2(
    iface: str, ip: str, prefix: int, gateway: str,
) -> dict[str, Any]:
    log: list[str] = []
    for cmd in [
        ["ip", "addr", "flush", "dev", iface],
        ["ip", "link", "set", "dev", iface, "up"],
        ["ip", "addr", "add", f"{ip}/{prefix}", "dev", iface],
    ]:
        rc, out = _run(cmd, timeout=8)
        log.append(f"$ {' '.join(cmd)} -> rc={rc}\n{out}")
        if rc != 0:
            return {
                "ok": False, "backend": "ip", "iface": iface,
                "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
                "output": "\n".join(log),
                "error": f"`ip` command failed (rc={rc}): {' '.join(cmd)}",
            }
    if gateway:
        rc, out = _run(
            ["ip", "route", "replace", "default", "via", gateway, "dev", iface],
            timeout=8,
        )
        log.append(
            f"$ ip route replace default via {gateway} dev {iface} -> rc={rc}\n{out}"
        )
        if rc != 0:
            return {
                "ok": False, "backend": "ip", "iface": iface,
                "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
                "output": "\n".join(log),
                "error": f"could not set default gateway (rc={rc})",
            }
    return {
        "ok": True, "backend": "ip", "iface": iface,
        "applied": {"ip": ip, "prefix": prefix, "gateway": gateway},
        "output": "\n".join(log),
        "error": None,
    }
