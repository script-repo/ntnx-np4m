"""
Probe-side connectivity tester.

The probe runs this against a target (subnet gateway, then an external IP)
after the probe NIC has been reconfigured for the VLAN under test.
Returns structured results so the master can stream them back to the
operator's UI without parsing free-form `ping` output.

`ping_sequence` keeps the two checks deliberately separate so the master
can distinguish "L2 OK but no upstream route" from "L2 broken".
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import time
from typing import Any


def _ping_argv(target: str, count: int, timeout_s: float) -> list[str]:
    """Build a platform-appropriate ping argv.

    Linux/macOS:  ping -c N -W <secs> <target>
    Windows:      ping -n N -w <millis> <target>
    """
    is_windows = platform.system().lower().startswith("win")
    if is_windows:
        return [
            "ping",
            "-n", str(count),
            "-w", str(int(timeout_s * 1000)),
            target,
        ]
    return [
        "ping",
        "-c", str(count),
        "-W", str(int(max(1, timeout_s))),
        target,
    ]


_LINUX_RTT_RE = re.compile(
    r"min/avg/max(?:/mdev)?\s*=\s*[\d.]+/([\d.]+)/[\d.]+",
    re.IGNORECASE,
)
_WINDOWS_RTT_RE = re.compile(
    r"Average\s*=\s*(\d+)\s*ms",
    re.IGNORECASE,
)
_LINUX_PKT_RE = re.compile(
    r"(\d+)\s+packets transmitted,\s+(\d+)\s+received",
    re.IGNORECASE,
)
_WINDOWS_PKT_RE = re.compile(
    r"Sent\s*=\s*(\d+),\s*Received\s*=\s*(\d+)",
    re.IGNORECASE,
)


def _parse_ping_output(text: str) -> dict[str, Any]:
    """Extract avg RTT and packet counts from ping stdout, best-effort."""
    sent: int | None = None
    recv: int | None = None
    rtt_ms: float | None = None

    m = _LINUX_PKT_RE.search(text)
    if m:
        sent, recv = int(m.group(1)), int(m.group(2))
    else:
        m = _WINDOWS_PKT_RE.search(text)
        if m:
            sent, recv = int(m.group(1)), int(m.group(2))

    m = _LINUX_RTT_RE.search(text)
    if m:
        try:
            rtt_ms = float(m.group(1))
        except ValueError:
            rtt_ms = None
    else:
        m = _WINDOWS_RTT_RE.search(text)
        if m:
            try:
                rtt_ms = float(m.group(1))
            except ValueError:
                rtt_ms = None
    return {"packets_sent": sent, "packets_recv": recv, "rtt_ms_avg": rtt_ms}


def ping_once(
    target: str,
    *,
    count: int = 3,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """Run a single ping batch against `target`.

    Returns: `{ok, target, packets_sent, packets_recv, rtt_ms_avg,
               elapsed_ms, raw, error}`.
    `ok` is true iff at least one reply came back AND the process returned 0.
    """
    if not target or not isinstance(target, str):
        return {
            "ok": False,
            "target": target,
            "packets_sent": 0,
            "packets_recv": 0,
            "rtt_ms_avg": None,
            "elapsed_ms": 0,
            "raw": "",
            "error": "no target supplied",
        }
    if not shutil.which("ping"):
        return {
            "ok": False,
            "target": target,
            "packets_sent": 0,
            "packets_recv": 0,
            "rtt_ms_avg": None,
            "elapsed_ms": 0,
            "raw": "",
            "error": "ping binary not found on PATH",
        }

    argv = _ping_argv(target, count=count, timeout_s=timeout_s)
    # Hard wall-clock cap so a busy DNS lookup or routing black-hole
    # can't wedge the request thread; budget = per-ping timeout * count
    # plus a small fixed overhead.
    wall_cap = int(timeout_s * count + 5)
    t0 = time.time()
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=wall_cap,
        )
        raw = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        raw += " [timeout]"
        rc = 124
    except Exception as exc:
        return {
            "ok": False,
            "target": target,
            "packets_sent": 0,
            "packets_recv": 0,
            "rtt_ms_avg": None,
            "elapsed_ms": int((time.time() - t0) * 1000),
            "raw": "",
            "error": f"ping invocation failed: {exc}",
        }

    parsed = _parse_ping_output(raw)
    sent = parsed["packets_sent"]
    recv = parsed["packets_recv"]
    ok = rc == 0 and (recv is None or recv > 0)
    return {
        "ok": bool(ok),
        "target": target,
        "packets_sent": sent if sent is not None else count,
        "packets_recv": recv if recv is not None else (count if ok else 0),
        "rtt_ms_avg": parsed["rtt_ms_avg"],
        "elapsed_ms": int((time.time() - t0) * 1000),
        "raw": raw[-2000:],
        "error": None if ok else (f"ping rc={rc}"),
    }


def ping_sequence(
    gateway: str,
    external_ip: str,
    *,
    count: int = 3,
    timeout_s: float = 2.0,
) -> dict[str, Any]:
    """Probe sequence: gateway first, then the external IP only if the
    gateway responded (no point pinging Google with a dead L2)."""
    gw_result = ping_once(gateway, count=count, timeout_s=timeout_s) if gateway else None
    ext_result: dict[str, Any] | None
    if not external_ip:
        ext_result = None
    elif gw_result and gw_result["ok"]:
        ext_result = ping_once(external_ip, count=count, timeout_s=timeout_s)
    else:
        ext_result = {
            "ok": False,
            "target": external_ip,
            "packets_sent": 0,
            "packets_recv": 0,
            "rtt_ms_avg": None,
            "elapsed_ms": 0,
            "raw": "",
            "error": "skipped: gateway unreachable",
            "skipped": True,
        }

    return {
        "gateway": gw_result,
        "external": ext_result,
        "gateway_ok": bool(gw_result and gw_result["ok"]),
        "external_ok": bool(ext_result and ext_result.get("ok")),
    }
