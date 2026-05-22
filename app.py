"""
NP4M - bulk AHV subnet provisioning, with optional import from another
Prism Central, a VMware vCenter, or a standalone VMware ESXi host.

Run:
    python -m pip install -r requirements.txt
    python app.py
    # then open http://127.0.0.1:5000

Target flow:
    1. Connect to the *target* Prism Central. Auth is either basic
       (username + password) or an API key (Authorization: Bearer ...).
    2. Pick a target PE (AHV) cluster.
    3. Pick a virtual switch (filtered to the chosen cluster).
    4. Optionally connect to a *source* PC, vCenter, or standalone ESXi
       host and import its existing subnets / port-groups into the
       networks list.
    5. Paste / edit the networks textarea (one per line: `name,vlan`).
    6. Click "Create networks" -- the log streams in at the bottom.

The server keeps creds in memory keyed by an opaque session token so the
secret isn't round-tripped on every API call. Restarting the process clears
all sessions. Source connections live in their own session namespace so
target and source auth never collide.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from typing import Any

import requests
import urllib3
from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    render_template,
    request,
    stream_with_context,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# When this file is launched via `python app.py`, Python loads it as the
# `__main__` module. A sibling module (master_probe_routes) does `import
# app as _app` to reach helpers defined here; that import doesn't find an
# `app` entry in sys.modules and re-executes this file under the new name,
# producing two parallel half-initialized copies and a "circular import"
# AttributeError when the second copy reaches the blueprint registration.
# Aliasing __main__ -> "app" up-front means the sibling import gets the
# cached, in-flight module and the registration completes cleanly.
if __name__ == "__main__" and "app" not in sys.modules:
    sys.modules["app"] = sys.modules[__name__]

__version__ = "0.2.0"
BUILD = 24   # bump on every commit

app = Flask(__name__)

SESSIONS: dict[str, dict[str, Any]] = {}
SOURCE_SESSIONS: dict[str, dict[str, Any]] = {}
TARGET_VCENTER_SESSIONS: dict[str, dict[str, Any]] = {}
SESSION_TTL_SECONDS = 3600

# ---------------------------------------------------------------------------
# Role controller + role-scoped blueprints. The same Flask process runs in
# either MASTER or PROBE mode (toggled at runtime via /api/mode or the TUI).
# Routes belonging to the inactive role return HTTP 423 from the
# before_request hook below, so the Web UI / TUI can fail fast and tell the
# operator to flip the mode.
# ---------------------------------------------------------------------------
import mode as _mode  # noqa: E402  (after Flask app construction by design)

# Routes that are always available, regardless of mode. The endpoints
# below are added below this list (api_index, api_version, etc.).
_ALWAYS_ON_ENDPOINTS: set[str] = {
    "static",
    "index",
    "api_version",
    "api_check_update",
    "api_self_update_preflight",
    "api_self_update",
    "api_mode_get",
    "api_mode_set",
}

# Endpoint -> required mode. Filled in once we register the blueprints
# below. Anything not in this map (and not in _ALWAYS_ON_ENDPOINTS) defaults
# to master-only, which preserves backwards-compatible behavior for the
# pre-existing /api/* endpoints in this file.
_ROLE_BY_ENDPOINT: dict[str, _mode.Mode] = {}


def _register_role_endpoint(endpoint: str, role: _mode.Mode) -> None:
    _ROLE_BY_ENDPOINT[endpoint] = role


@app.before_request
def _enforce_mode_gate():
    ep = request.endpoint or ""
    if ep in _ALWAYS_ON_ENDPOINTS:
        return None
    expected = _ROLE_BY_ENDPOINT.get(ep, _mode.Mode.MASTER)
    if _mode.get_mode() is expected:
        return None
    return (
        jsonify(
            error="endpoint disabled in current mode",
            current_mode=_mode.get_mode().value,
            required_mode=expected.value,
            endpoint=ep,
        ),
        423,
    )


# Register the probe and master-probe blueprints. Probe-side imports are
# deferred to keep `python app.py --help` cheap on a fresh install.
import probe_routes as _probe_routes  # noqa: E402
import master_probe_routes as _master_probe_routes  # noqa: E402

app.register_blueprint(_probe_routes.probe_bp)
app.register_blueprint(_master_probe_routes.master_probe_bp)

# Bind each blueprint's endpoints to the role that should serve them.
for _rule in app.url_map.iter_rules():
    if _rule.endpoint.startswith("probe."):
        _register_role_endpoint(_rule.endpoint, _mode.Mode.PROBE)
    elif _rule.endpoint.startswith("master_probe."):
        _register_role_endpoint(_rule.endpoint, _mode.Mode.MASTER)


def _make_pc_session(auth: dict[str, Any]) -> requests.Session:
    """Build a requests.Session honoring an auth descriptor.

    Accepted shapes:
        {"mode": "basic", "username": "...", "password": "..."}
        {"mode": "token", "api_key": "...", "header": "Authorization"}
    """
    s = requests.Session()
    s.verify = False
    s.headers.update(
        {"Accept": "application/json", "Content-Type": "application/json"}
    )
    mode = (auth or {}).get("mode", "basic")
    if mode == "basic":
        s.auth = (auth.get("username") or "", auth.get("password") or "")
    elif mode == "token":
        api_key = (auth.get("api_key") or "").strip()
        if not api_key:
            raise ValueError("api_key is required for token auth")
        header_name = auth.get("header") or "Authorization"
        if header_name.lower() == "authorization":
            value = api_key if api_key.lower().startswith("bearer ") else f"Bearer {api_key}"
        else:
            value = api_key
        s.headers[header_name] = value
    else:
        raise ValueError(f"unsupported auth mode: {mode!r}")
    return s


def _looks_like_auth_error(status: int, text: str) -> bool:
    """Detect PC auth failures that come back wrapped in 5xx responses."""
    if status in (401, 403):
        return True
    if status >= 500 and text:
        lower = text.lower()
        if any(
            marker in lower
            for marker in (
                "response code 403",
                "response code 401",
                "unauthorized",
                "authentication failed",
            )
        ):
            return True
    return False


def _redact_auth(auth: dict[str, Any]) -> dict[str, Any]:
    """Return an auth descriptor safe to surface to the UI."""
    if not isinstance(auth, dict):
        return {"mode": "unknown"}
    if auth.get("mode") == "basic":
        return {"mode": "basic", "username": auth.get("username")}
    if auth.get("mode") == "token":
        return {"mode": "token"}
    return {"mode": auth.get("mode") or "unknown"}


def _get_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    sess = SESSIONS.get(token)
    if not sess:
        return None
    if time.time() - sess["created_at"] > SESSION_TTL_SECONDS:
        SESSIONS.pop(token, None)
        return None
    return sess


def _get_source_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    sess = SOURCE_SESSIONS.get(token)
    if not sess:
        return None
    if time.time() - sess["created_at"] > SESSION_TTL_SECONDS:
        SOURCE_SESSIONS.pop(token, None)
        return None
    return sess


def _get_target_vcenter_session(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    sess = TARGET_VCENTER_SESSIONS.get(token)
    if not sess:
        return None
    if time.time() - sess["created_at"] > SESSION_TTL_SECONDS:
        TARGET_VCENTER_SESSIONS.pop(token, None)
        return None
    return sess


def _wait_for_task(
    session: requests.Session,
    base_url: str,
    task_ext_id: str,
    timeout_seconds: int = 120,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    last_status = "UNKNOWN"
    while time.time() < deadline:
        r = session.get(
            f"{base_url}/api/prism/v4.0/config/tasks/{task_ext_id}", timeout=30
        )
        r.raise_for_status()
        body = r.json() or {}
        data = body.get("data") or {}
        last_status = data.get("status") or last_status
        if last_status in {"SUCCEEDED", "FAILED", "CANCELED", "CANCELLED"}:
            return data
        time.sleep(poll_interval)
    raise TimeoutError(
        f"Task {task_ext_id} did not finish within {timeout_seconds}s "
        f"(last status: {last_status})"
    )


def _extract_task_ext_id(payload: dict[str, Any]) -> str | None:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, dict):
        if isinstance(data.get("extId"), str):
            return data["extId"]
        for key in ("taskReference", "task"):
            ref = data.get(key)
            if isinstance(ref, dict) and isinstance(ref.get("extId"), str):
                return ref["extId"]
    return None


def _format_api_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return str(payload)[:300]
    data = payload.get("data") or payload
    msgs: list[str] = []
    if isinstance(data, dict):
        candidates = (
            data.get("error", {}).get("messageList")
            if isinstance(data.get("error"), dict)
            else None
        )
        candidates = candidates or data.get("messageList") or data.get("errorMessages")
        if isinstance(candidates, list):
            for m in candidates:
                if isinstance(m, dict):
                    msg = m.get("message") or m.get("description")
                    if msg:
                        msgs.append(str(msg))
                else:
                    msgs.append(str(m))
    return "; ".join(msgs) if msgs else json.dumps(data)[:300]


@app.get("/")
def index():
    # Disable browser caching so newly-deployed builds (which inline all
    # JS/CSS in the template) replace the old page on the very next request.
    template = (
        "probe.html"
        if _mode.get_mode() is _mode.Mode.PROBE
        else "index.html"
    )
    resp = make_response(
        render_template(
            template,
            version=__version__,
            build=BUILD,
            mode=_mode.get_mode().value,
        )
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/api/version")
def api_version():
    return jsonify(version=__version__, build=BUILD, mode=_mode.get_mode().value)


@app.get("/api/mode")
def api_mode_get():
    return jsonify(mode=_mode.get_mode().value)


@app.post("/api/mode")
def api_mode_set():
    body = request.get_json(force=True, silent=True) or {}
    target = body.get("mode")
    if not target:
        return jsonify(error="mode is required ('master' or 'probe')"), 400
    try:
        new_mode = _mode.set_mode(target)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(mode=new_mode.value)


# How long to remember the last successful upstream check so we don't hammer
# GitHub on every page load. The UI polls /api/check-update on load and at
# most once every few minutes after that.
_UPDATE_CHECK_CACHE: dict[str, Any] = {"at": 0.0, "result": None}
# Kept short on purpose so an upstream push shows up in the UI within
# ~1 minute. The primary fetch goes through the GitHub Contents API
# (which is *not* Fastly-fronted with a 5-minute TTL like raw.github-
# usercontent.com is), so 60 lookups/hour from one server is well
# inside the anonymous rate limit of 60/hr per IP. If we ever do hit
# the limit, the raw URL is used as a fallback.
_UPDATE_CHECK_TTL = 60  # seconds
_UPSTREAM_OWNER_REPO = "script-repo/ntnx-np4m"
_UPSTREAM_BRANCH = "main"
_UPSTREAM_CONTENTS_API = (
    f"https://api.github.com/repos/{_UPSTREAM_OWNER_REPO}/contents/app.py"
    f"?ref={_UPSTREAM_BRANCH}"
)
_UPSTREAM_APP_PY_URL = (
    f"https://raw.githubusercontent.com/{_UPSTREAM_OWNER_REPO}/{_UPSTREAM_BRANCH}/app.py"
)
_UPSTREAM_RELEASES_URL = f"https://github.com/{_UPSTREAM_OWNER_REPO}"


def _parse_remote_build(text: str) -> int | None:
    """Pull BUILD = <int> out of a raw app.py source dump."""
    if not text:
        return None
    m = re.search(r"^\s*BUILD\s*=\s*(\d+)\b", text, flags=re.MULTILINE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _fetch_upstream_app_py() -> tuple[str | None, str | None]:
    """Fetch upstream app.py text. Returns (text, error_message).

    Primary path: GitHub Contents API (returns base64 + commit sha; not
    fronted by the 5-minute Fastly cache that raw.githubusercontent.com
    sits behind, so a push shows up immediately).
    Fallback: raw URL with a cache-buster query param (best-effort; the
    edge cache may still win, but at least we tried).
    """
    import base64

    try:
        r = requests.get(
            _UPSTREAM_CONTENTS_API,
            timeout=8,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ntnx-np4m-update-check",
            },
        )
    except requests.RequestException as exc:
        contents_err = f"contents API: {exc}"
        r = None
    else:
        contents_err = None
    if r is not None and r.status_code == 200:
        try:
            j = r.json()
            blob = j.get("content", "")
            if isinstance(blob, str) and blob:
                txt = base64.b64decode(blob).decode("utf-8", errors="replace")
                return txt, None
            return None, "contents API returned no content"
        except Exception as exc:  # noqa: BLE001
            contents_err = f"contents API parse: {exc}"
    elif r is not None:
        contents_err = f"contents API HTTP {r.status_code}"

    # Fallback to the raw URL. Add a cache-bust query string; Fastly often
    # ignores this for github raw content but it costs nothing to try.
    try:
        r2 = requests.get(
            f"{_UPSTREAM_APP_PY_URL}?nocache={int(time.time())}",
            timeout=8,
            headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )
    except requests.RequestException as exc:
        return None, f"{contents_err}; raw fallback: {exc}"
    if r2.status_code >= 400:
        return None, f"{contents_err}; raw HTTP {r2.status_code}"
    return r2.text, None


@app.get("/api/check-update")
def api_check_update():
    """Compare local BUILD to upstream main BUILD and report.

    Cached server-side for 5 minutes so the page-load polling doesn't hit
    GitHub every refresh. Pass ?force=1 to bypass the cache.
    """
    force = request.args.get("force", "").lower() in {"1", "true", "yes"}
    now = time.time()

    def _resp(payload: dict[str, Any], status: int = 200) -> Response:
        # Browsers/proxies must never cache this JSON: a stale "up to date"
        # answer would freeze the green pill until a hard refresh.
        r = make_response(jsonify(payload), status)
        r.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        r.headers["Pragma"] = "no-cache"
        return r

    if (
        not force
        and _UPDATE_CHECK_CACHE["result"] is not None
        and now - _UPDATE_CHECK_CACHE["at"] < _UPDATE_CHECK_TTL
    ):
        return _resp(_UPDATE_CHECK_CACHE["result"])

    text, err = _fetch_upstream_app_py()
    if text is None:
        return _resp({
            "local": BUILD,
            "remote": None,
            "update_available": False,
            "error": err or "upstream fetch failed",
            "repo_url": _UPSTREAM_RELEASES_URL,
        })
    remote_build = _parse_remote_build(text)
    result = {
        "local": BUILD,
        "remote": remote_build,
        "update_available": (
            remote_build is not None and remote_build > BUILD
        ),
        "repo_url": _UPSTREAM_RELEASES_URL,
        "checked_at": int(now),
    }
    _UPDATE_CHECK_CACHE["at"] = now
    _UPDATE_CHECK_CACHE["result"] = result
    return _resp(result)


# ---------------------------------------------------------------------------
# Self-update: pull the latest source + Python deps, then ask systemd to
# restart us so the new code is loaded. Only enabled when we're running
# the install.sh deployment (Linux + gunicorn + systemd + Restart=always);
# refuses cleanly otherwise so Windows / dev runs don't accidentally try.
# ---------------------------------------------------------------------------


def _under_gunicorn() -> bool:
    """True if our parent process is gunicorn (so SIGTERM->parent triggers
    a systemd-managed restart of the master)."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open(f"/proc/{os.getppid()}/comm", "r", encoding="utf-8") as f:
            return "gunicorn" in f.read().lower()
    except Exception:
        return False


def _systemd_restart_mode() -> str | None:
    """Return the Restart= value of np4m.service (e.g. 'always'), or None
    if systemd isn't available or the unit isn't ours."""
    if not sys.platform.startswith("linux"):
        return None
    if not shutil.which("systemctl"):
        return None
    try:
        r = subprocess.run(
            ["systemctl", "show", "np4m", "-p", "Restart", "--value"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return (r.stdout or "").strip().lower() or None
    except Exception:
        pass
    return None


def _self_update_preflight() -> tuple[bool, str | None, dict[str, Any]]:
    """Return (ok, error_message, info_dict) describing whether the running
    deployment can update itself in place."""
    repo_dir = pathlib.Path(__file__).resolve().parent
    venv_pip_unix = repo_dir / ".venv" / "bin" / "pip"
    venv_pip_win = repo_dir / ".venv" / "Scripts" / "pip.exe"
    venv_pip = venv_pip_unix if venv_pip_unix.exists() else venv_pip_win
    restart_mode = _systemd_restart_mode()
    info: dict[str, Any] = {
        "platform": sys.platform,
        "repo_dir": str(repo_dir),
        "venv_pip": str(venv_pip) if venv_pip.exists() else None,
        "under_systemd": bool(os.environ.get("INVOCATION_ID")),
        "under_gunicorn": _under_gunicorn(),
        "systemd_restart": restart_mode,
        "git_dir": str(repo_dir / ".git") if (repo_dir / ".git").exists() else None,
    }

    if not sys.platform.startswith("linux"):
        return False, (
            "Auto-update is only wired up for Linux/systemd deployments. "
            "Use the upstream repo link to update manually."
        ), info
    if not info["under_gunicorn"]:
        return False, (
            "NP4M is not running under gunicorn; auto-update requires the "
            "install.sh deployment (use the link to update manually)."
        ), info
    if not info["under_systemd"]:
        return False, (
            "NP4M is not running under systemd; auto-update needs the "
            "systemd unit from install.sh."
        ), info
    if not info["git_dir"]:
        return False, (
            f"{repo_dir} is not a git checkout; auto-update needs `git pull`."
        ), info
    if not info["venv_pip"]:
        return False, (
            f"venv pip not found under {repo_dir}/.venv; reinstall via "
            "install.sh and try again."
        ), info
    if restart_mode != "always":
        return False, (
            f"systemd unit has Restart={restart_mode!r}; auto-update needs "
            "Restart=always so the service comes back after SIGTERM. Re-run "
            "the install.sh one-liner to refresh the unit."
        ), info
    return True, None, info


@app.get("/api/self-update/preflight")
def api_self_update_preflight():
    ok, err, info = _self_update_preflight()
    return jsonify(ok=ok, error=err, info=info, repo_url=_UPSTREAM_RELEASES_URL)


def _schedule_master_sigterm(delay: float = 2.0) -> None:
    """Send SIGTERM to our parent (the gunicorn master) after `delay`s.
    With Restart=always in the systemd unit, systemd will respawn it with
    the freshly-pulled code."""
    def _kick():
        time.sleep(delay)
        try:
            os.kill(os.getppid(), signal.SIGTERM)
        except Exception:
            pass
    threading.Thread(target=_kick, daemon=True).start()


@app.post("/api/self-update")
def api_self_update():
    """Stream NDJSON: preflight, git fetch+reset, pip install, restart.

    Returns an NDJSON stream so the UI can show progress in the log pane.
    Each line is a small object: {"level": "...", "msg": "...", ...}.
    Sends an "event": "preflight_failed" or "event": "restarting" marker
    line at the relevant transition so the front-end can branch on it.
    """
    ok, err, info = _self_update_preflight()

    def ndjson(obj: dict[str, Any]) -> str:
        return json.dumps(obj) + "\n"

    if not ok:
        # Single-line failure stream so the frontend handler is uniform.
        def fail_gen():
            yield ndjson({
                "level": "error",
                "msg": err or "auto-update unavailable",
                "event": "preflight_failed",
                "info": info,
                "repo_url": _UPSTREAM_RELEASES_URL,
            })
        return Response(
            stream_with_context(fail_gen()),
            mimetype="application/x-ndjson",
        )

    repo_dir = pathlib.Path(info["repo_dir"])
    venv_pip = pathlib.Path(info["venv_pip"])

    def generate():
        yield ndjson({"level": "system", "msg": f"NP4M auto-update starting (local build {BUILD})."})
        yield ndjson({"level": "info", "msg": f"  repo:    {repo_dir}"})
        yield ndjson({"level": "info", "msg": f"  venv:    {venv_pip.parent.parent}"})

        steps: list[tuple[str, list[str]]] = [
            ("git fetch origin",                ["git", "fetch", "origin", "--prune"]),
            ("git reset --hard origin/main",    ["git", "reset", "--hard", "origin/main"]),
            (
                "pip install -r requirements.txt",
                [str(venv_pip), "install", "--upgrade", "--disable-pip-version-check",
                 "-r", str(repo_dir / "requirements.txt")],
            ),
        ]
        for desc, cmd in steps:
            yield ndjson({"level": "system", "msg": f"$ {desc}"})
            try:
                proc = subprocess.Popen(
                    cmd, cwd=str(repo_dir),
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, bufsize=1,
                )
            except Exception as exc:
                yield ndjson({"level": "error", "msg": f"  failed to start: {exc}"})
                yield ndjson({"level": "error", "msg": "Update aborted.", "event": "aborted"})
                return
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    yield ndjson({"level": "info", "msg": f"  {line}"})
            try:
                rc = proc.wait(timeout=240)
            except subprocess.TimeoutExpired:
                proc.kill()
                yield ndjson({"level": "error", "msg": "  step timed out after 240s"})
                yield ndjson({"level": "error", "msg": "Update aborted.", "event": "aborted"})
                return
            if rc != 0:
                yield ndjson({"level": "error", "msg": f"  exit code {rc}; aborting"})
                yield ndjson({"level": "error", "msg": "Update aborted.", "event": "aborted"})
                return
            yield ndjson({"level": "ok", "msg": f"  {desc}: done"})

        yield ndjson({
            "level": "ok",
            "msg": "Update applied. Restarting service in ~2s...",
            "event": "restarting",
        })
        _schedule_master_sigterm(2.0)

    return Response(
        stream_with_context(generate()),
        mimetype="application/x-ndjson",
    )


def _auth_from_body(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Build an auth descriptor from a connect-request body.

    Accepted body shapes (in priority order):
        {"auth_mode": "token", "api_key": "..."}
        {"auth_mode": "basic", "username": "...", "password": "..."}
        # legacy/back-compat:
        {"api_key": "..."}                           -> token
        {"username": "...", "password": "..."}       -> basic
    Returns (auth_descriptor, error_message). Exactly one is non-None.
    """
    body = body or {}
    mode = (body.get("auth_mode") or "").strip().lower()
    api_key = (body.get("api_key") or "").strip()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    if not mode:
        if api_key:
            mode = "token"
        elif username or password:
            mode = "basic"
    if mode == "token":
        if not api_key:
            return None, "api_key is required for token auth"
        return {"mode": "token", "api_key": api_key}, None
    if mode == "basic":
        if not username or not password:
            return None, "username and password are required for basic auth"
        return {"mode": "basic", "username": username, "password": password}, None
    return None, "no credentials provided"


@app.post("/api/connect")
def api_connect():
    body = request.get_json(force=True, silent=True) or {}
    host = (body.get("host") or "").strip()
    port = body.get("port") or 9440
    if not host:
        return jsonify(error="host is required"), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify(error="port must be an integer"), 400
    auth, err = _auth_from_body(body)
    if err:
        return jsonify(error=err), 400
    base_url = f"https://{host}:{port}"
    try:
        s = _make_pc_session(auth)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        r = s.get(
            f"{base_url}/api/clustermgmt/v4.0/config/clusters?$limit=1",
            timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"connection failed: {exc}"), 502
    if _looks_like_auth_error(r.status_code, r.text):
        msg = (
            "invalid API key"
            if auth["mode"] == "token"
            else "invalid credentials"
        )
        return jsonify(error=msg), 401
    if r.status_code >= 400:
        return (
            jsonify(error=f"PC returned HTTP {r.status_code}: {r.text[:200]}"),
            502,
        )
    token = secrets.token_urlsafe(24)
    SESSIONS[token] = {
        "host": host,
        "port": port,
        "auth": auth,
        "base_url": base_url,
        "created_at": time.time(),
    }
    return jsonify(
        token=token,
        host=host,
        port=port,
        auth=_redact_auth(auth),
    )


@app.post("/api/clusters")
def api_clusters():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_session(body.get("token"))
    if not sess:
        return jsonify(error="not connected"), 401
    s = _make_pc_session(sess["auth"])
    try:
        r = s.get(
            f"{sess['base_url']}/api/clustermgmt/v4.0/config/clusters?$limit=100",
            timeout=20,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    if r.status_code >= 400:
        return jsonify(error=f"HTTP {r.status_code}"), 502
    data = (r.json() or {}).get("data") or []
    out: list[dict[str, Any]] = []
    for c in data:
        cfg = c.get("config") or {}
        funcs = cfg.get("clusterFunction") or []
        if "PRISM_CENTRAL" in funcs:
            continue
        out.append(
            {
                "extId": c.get("extId"),
                "name": c.get("name"),
                "function": funcs,
                "hypervisorTypes": cfg.get("hypervisorTypes") or [],
            }
        )
    out.sort(key=lambda x: (x.get("name") or "").lower())
    return jsonify(clusters=out)


@app.post("/api/virtual-switches")
def api_virtual_switches():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_session(body.get("token"))
    if not sess:
        return jsonify(error="not connected"), 401
    cluster_uuid = body.get("cluster_uuid")
    s = _make_pc_session(sess["auth"])
    try:
        r = s.get(
            f"{sess['base_url']}/api/networking/v4.0/config/virtual-switches?$limit=100",
            timeout=20,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    if r.status_code >= 400:
        return jsonify(error=f"HTTP {r.status_code}"), 502
    data = (r.json() or {}).get("data") or []
    out: list[dict[str, Any]] = []
    for vs in data:
        cluster_uuids = []
        for cl in vs.get("clusters") or []:
            if isinstance(cl, dict):
                ext = cl.get("extId") or cl.get("uuid")
                if ext:
                    cluster_uuids.append(ext)
            elif isinstance(cl, str):
                cluster_uuids.append(cl)
        if cluster_uuid and cluster_uuids and cluster_uuid not in cluster_uuids:
            continue
        out.append(
            {
                "extId": vs.get("extId"),
                "name": vs.get("name"),
                "isDefault": bool(vs.get("isDefault")),
                "bondMode": vs.get("bondMode"),
                "mtu": vs.get("mtu"),
                "clusters": cluster_uuids,
            }
        )
    out.sort(
        key=lambda x: (
            0 if x.get("isDefault") else 1,
            (x.get("name") or "").lower(),
        )
    )
    return jsonify(virtual_switches=out)


@app.post("/api/target-subnets")
def api_target_subnets():
    """List all subnets currently on the target cluster.

    Used by the UI to verify that subnets created via /api/create actually
    landed on the cluster. Returns enough fields (name, VLAN, type,
    virtual switch, advanced flag, IP summary) for a side-by-side table
    against the user's intended networks list.
    """
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_session(body.get("token"))
    if not sess:
        return jsonify(error="not connected"), 401
    cluster_uuid = body.get("cluster_uuid")
    if not cluster_uuid:
        return jsonify(error="cluster_uuid is required"), 400

    s = _make_pc_session(sess["auth"])
    base_url = sess["base_url"]

    vs_names: dict[str, str] = {}
    try:
        rv = s.get(
            f"{base_url}/api/networking/v4.0/config/virtual-switches?$limit=100",
            timeout=20,
        )
        if rv.status_code < 400:
            for vs in (rv.json() or {}).get("data") or []:
                ext = vs.get("extId")
                name = vs.get("name")
                if ext and name:
                    vs_names[ext] = name
    except requests.RequestException:
        pass

    try:
        r = s.get(
            f"{base_url}/api/networking/v4.0/config/subnets?$limit=100",
            timeout=20,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"request failed: {exc}"), 502
    if r.status_code >= 400:
        return jsonify(error=f"HTTP {r.status_code}: {r.text[:200]}"), 502

    payload = r.json() or {}
    data = payload.get("data") or []
    meta = payload.get("metadata") or {}

    out: list[dict[str, Any]] = []
    for sub in data:
        if sub.get("clusterReference") != cluster_uuid:
            continue
        vs_ref = sub.get("virtualSwitchReference")

        ip_summary: str | None = None
        ip_cfg = sub.get("ipConfig") or []
        if isinstance(ip_cfg, list) and ip_cfg:
            ipv4 = (ip_cfg[0] or {}).get("ipv4") or {}
            sub_block = ipv4.get("ipSubnet") or {}
            ip_val = (sub_block.get("ip") or {}).get("value")
            prefix = sub_block.get("prefixLength")
            gw = (ipv4.get("defaultGatewayIp") or {}).get("value")
            if ip_val and prefix is not None:
                ip_summary = f"{ip_val}/{prefix}"
                if gw:
                    ip_summary += f" gw {gw}"

        out.append(
            {
                "extId": sub.get("extId"),
                "name": sub.get("name"),
                "vlan": sub.get("networkId"),
                "subnetType": sub.get("subnetType"),
                "isAdvancedNetworking": bool(sub.get("isAdvancedNetworking")),
                "virtualSwitchExtId": vs_ref,
                "virtualSwitchName": vs_names.get(vs_ref) if vs_ref else None,
                "description": sub.get("description"),
                "ipSummary": ip_summary,
            }
        )

    out.sort(
        key=lambda x: (
            x.get("vlan") if isinstance(x.get("vlan"), int) else 99999,
            (x.get("name") or "").lower(),
        )
    )

    total = meta.get("totalAvailableResults")
    truncated = isinstance(total, int) and total > len(data)
    return jsonify(
        subnets=out,
        truncated=truncated,
        total=total if isinstance(total, int) else None,
    )


def _list_existing_subnet_names_on_cluster(
    session: requests.Session, base_url: str, cluster_uuid: str
) -> tuple[set[str], bool]:
    """Return (existing_names_lowercased, was_truncated_at_100)."""
    names: set[str] = set()
    truncated = False
    try:
        r = session.get(
            f"{base_url}/api/networking/v4.0/config/subnets?$limit=100",
            timeout=20,
        )
    except requests.RequestException:
        return names, False
    if r.status_code >= 400:
        return names, False
    body = r.json() or {}
    data = body.get("data") or []
    meta = body.get("metadata") or {}
    for sub in data:
        if sub.get("clusterReference") == cluster_uuid:
            n = sub.get("name")
            if n:
                names.add(n.lower())
    total = meta.get("totalAvailableResults")
    if isinstance(total, int) and total > len(data):
        truncated = True
    return names, truncated


def _create_unmanaged_subnet(
    session: requests.Session,
    base_url: str,
    name: str,
    vlan: int,
    cluster_uuid: str,
    vs_uuid: str | None,
    advanced: bool = True,
) -> tuple[int, dict[str, Any]]:
    """POST a single unmanaged VLAN subnet.

    `advanced` controls the `isAdvancedNetworking` body field. The default is
    True so the resulting subnet can later participate in Flow / VPC features
    without a migration. Callers should fall back to `advanced=False` if the
    target cluster rejects the advanced flag (e.g. FNS / Flow Networking is
    not licensed or not enabled).
    """
    body: dict[str, Any] = {
        "name": name,
        "description": f"Unmanaged VLAN {vlan} subnet (created via web UI)",
        "subnetType": "VLAN",
        "networkId": vlan,
        "clusterReference": cluster_uuid,
        "isExternal": False,
        "isAdvancedNetworking": bool(advanced),
    }
    if vs_uuid:
        body["virtualSwitchReference"] = vs_uuid
    headers = {"NTNX-Request-Id": str(uuid.uuid4())}
    r = session.post(
        f"{base_url}/api/networking/v4.0/config/subnets",
        json=body,
        headers=headers,
        timeout=60,
    )
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, {"raw": r.text}


@app.post("/api/create")
def api_create():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_session(body.get("token"))
    if not sess:
        return jsonify(error="not connected"), 401
    cluster_uuid = body.get("cluster_uuid")
    vs_uuid = body.get("vs_uuid") or None
    networks = body.get("networks") or []
    if not cluster_uuid:
        return jsonify(error="cluster_uuid is required"), 400
    if not isinstance(networks, list) or not networks:
        return jsonify(error="networks list is required"), 400

    pc_session = _make_pc_session(sess["auth"])
    base_url = sess["base_url"]

    def emit(level: str, msg: str) -> str:
        return json.dumps({"level": level, "msg": msg}) + "\n"

    def gen():
        success = 0
        fail = 0
        fallback_count = 0
        yield emit(
            "info",
            f"Starting creation of {len(networks)} subnet(s) on cluster "
            f"{cluster_uuid}.",
        )
        if vs_uuid:
            yield emit("info", f"Virtual switch: {vs_uuid}")
        else:
            yield emit("info", "Virtual switch: <default>")
        existing_names, truncated = _list_existing_subnet_names_on_cluster(
            pc_session, base_url, cluster_uuid
        )
        yield emit(
            "info",
            f"Cluster currently has {len(existing_names)} subnet(s) "
            f"{'(first 100 only -- pagination not implemented)' if truncated else ''}".rstrip(),
        )

        def attempt(net_name: str, net_vlan: int, adv: bool):
            """Single create attempt.

            Yields ndjson log lines that should be streamed live (e.g. the
            "task waiting..." marker before the long blocking poll).
            Returns (ok: bool, detail: str | None) where `detail` is a
            human-readable failure reason on failure or an optional note on
            success.
            """
            try:
                status, payload = _create_unmanaged_subnet(
                    pc_session, base_url, net_name, net_vlan,
                    cluster_uuid, vs_uuid, advanced=adv,
                )
            except requests.RequestException as exc:
                return False, f"HTTP error: {exc}"
            if status not in (200, 201, 202):
                return False, f"HTTP {status}: {_format_api_error(payload)}"
            task_id = _extract_task_ext_id(payload)
            if not task_id:
                return True, f"HTTP {status}, no task id returned"
            yield emit("info", f"  task {task_id} -- waiting...")
            try:
                task = _wait_for_task(
                    pc_session, base_url, task_id, timeout_seconds=120
                )
            except (TimeoutError, requests.RequestException) as exc:
                return False, f"task wait failed: {exc}"
            tstatus = task.get("status")
            if tstatus == "SUCCEEDED":
                return True, None
            detail = _format_api_error({"data": task}) or tstatus
            return False, f"task {tstatus}: {detail}"

        for net in networks:
            name = (net or {}).get("name")
            vlan = (net or {}).get("vlan")
            if not name or vlan is None:
                yield emit("error", f"Skipping invalid entry: {net!r}")
                fail += 1
                continue
            try:
                vlan_i = int(vlan)
            except (TypeError, ValueError):
                yield emit("error", f"{name}: invalid VLAN {vlan!r}")
                fail += 1
                continue
            if name.lower() in existing_names:
                yield emit(
                    "error",
                    f"Skipping '{name}': a subnet with that name already "
                    f"exists on this cluster.",
                )
                fail += 1
                continue

            yield emit(
                "info",
                f"Creating '{name}' (VLAN {vlan_i}) "
                f"with isAdvancedNetworking=true...",
            )
            ok, detail = yield from attempt(name, vlan_i, adv=True)
            flag_used = "isAdvancedNetworking=true"

            if not ok:
                yield emit(
                    "warn",
                    f"  '{name}' could not be created with "
                    f"isAdvancedNetworking=true.",
                )
                yield emit("warn", f"    reason: {detail}")
                yield emit(
                    "warn",
                    f"  Retrying '{name}' with isAdvancedNetworking=false...",
                )
                ok, detail = yield from attempt(name, vlan_i, adv=False)
                flag_used = "isAdvancedNetworking=false"
                if ok:
                    fallback_count += 1

            if ok:
                note = f" ({detail})" if detail else ""
                yield emit(
                    "ok",
                    f"  OK: '{name}' created [{flag_used}]{note}.",
                )
                existing_names.add(name.lower())
                success += 1
            else:
                yield emit(
                    "error",
                    f"  FAIL: '{name}' could not be created "
                    f"[{flag_used}]: {detail}",
                )
                fail += 1

        summary_extra = (
            f" ({fallback_count} via isAdvancedNetworking=false fallback)"
            if fallback_count
            else ""
        )
        yield emit(
            "info",
            f"Done. {success} succeeded{summary_extra}, "
            f"{fail} failed (of {len(networks)}).",
        )

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Source inventory: read-only views of *another* PC, a vCenter, or a
# standalone ESXi host so the user can import existing networks/port-groups
# into the create list.
# ---------------------------------------------------------------------------


@app.post("/api/source/pc/connect")
def api_source_pc_connect():
    body = request.get_json(force=True, silent=True) or {}
    host = (body.get("host") or "").strip()
    port = body.get("port") or 9440
    if not host:
        return jsonify(error="host is required"), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify(error="port must be an integer"), 400
    auth, err = _auth_from_body(body)
    if err:
        return jsonify(error=err), 400
    base_url = f"https://{host}:{port}"
    try:
        s = _make_pc_session(auth)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    try:
        r = s.get(
            f"{base_url}/api/clustermgmt/v4.0/config/clusters?$limit=1",
            timeout=15,
        )
    except requests.RequestException as exc:
        return jsonify(error=f"connection failed: {exc}"), 502
    if _looks_like_auth_error(r.status_code, r.text):
        msg = (
            "invalid API key"
            if auth["mode"] == "token"
            else "invalid credentials"
        )
        return jsonify(error=msg), 401
    if r.status_code >= 400:
        return (
            jsonify(error=f"PC returned HTTP {r.status_code}: {r.text[:200]}"),
            502,
        )
    token = secrets.token_urlsafe(24)
    SOURCE_SESSIONS[token] = {
        "kind": "pc",
        "host": host,
        "port": port,
        "auth": auth,
        "base_url": base_url,
        "created_at": time.time(),
    }
    return jsonify(
        source_token=token, kind="pc", host=host, port=port,
        auth=_redact_auth(auth),
    )


def _pc_paginated_get(
    session: requests.Session, base_url: str, path: str, *, limit: int = 100,
    max_pages: int = 50,
) -> list[dict[str, Any]]:
    """GET an OData v4 list endpoint, paginating until exhausted or capped."""
    out: list[dict[str, Any]] = []
    sep = "&" if "?" in path else "?"
    for page in range(max_pages):
        url = f"{base_url}{path}{sep}$page={page}&$limit={limit}"
        r = session.get(url, timeout=30)
        if r.status_code >= 400:
            break
        body = r.json() or {}
        data = body.get("data") or []
        out.extend(data)
        if len(data) < limit:
            break
    return out


@app.post("/api/source/pc/inventory")
def api_source_pc_inventory():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_source_session(body.get("source_token"))
    if not sess or sess.get("kind") != "pc":
        return jsonify(error="not connected to a source PC"), 401
    s = _make_pc_session(sess["auth"])
    base_url = sess["base_url"]

    clusters_raw = _pc_paginated_get(
        s, base_url, "/api/clustermgmt/v4.0/config/clusters"
    )
    cluster_lookup: dict[str, dict[str, Any]] = {}
    clusters_out: list[dict[str, Any]] = []
    for c in clusters_raw:
        cfg = c.get("config") or {}
        funcs = cfg.get("clusterFunction") or []
        if "PRISM_CENTRAL" in funcs:
            continue
        ext = c.get("extId")
        if not ext:
            continue
        info = {
            "extId": ext,
            "name": c.get("name"),
            "function": funcs,
            "hypervisorTypes": cfg.get("hypervisorTypes") or [],
        }
        cluster_lookup[ext] = info
        clusters_out.append(info)
    clusters_out.sort(key=lambda x: (x.get("name") or "").lower())

    vs_raw = _pc_paginated_get(
        s, base_url, "/api/networking/v4.0/config/virtual-switches"
    )
    vs_lookup: dict[str, dict[str, Any]] = {}
    vs_out: list[dict[str, Any]] = []
    for vs in vs_raw:
        ext = vs.get("extId")
        if not ext:
            continue
        cluster_uuids: list[str] = []
        host_uplink_summary: list[dict[str, Any]] = []
        for cl in vs.get("clusters") or []:
            if isinstance(cl, dict):
                cl_ext = cl.get("extId") or cl.get("uuid")
                if cl_ext:
                    cluster_uuids.append(cl_ext)
                for h in cl.get("hosts") or []:
                    host_uplink_summary.append(
                        {
                            "hostExtId": h.get("extId"),
                            "internalBridgeName": h.get("internalBridgeName"),
                            "hostNics": h.get("hostNics") or [],
                        }
                    )
            elif isinstance(cl, str):
                cluster_uuids.append(cl)
        info = {
            "extId": ext,
            "name": vs.get("name"),
            "isDefault": bool(vs.get("isDefault")),
            "bondMode": vs.get("bondMode"),
            "mtu": vs.get("mtu"),
            "clusters": cluster_uuids,
            "clusterNames": [
                (cluster_lookup.get(u) or {}).get("name") or u[:8]
                for u in cluster_uuids
            ],
            "hostUplinks": host_uplink_summary,
        }
        vs_lookup[ext] = info
        vs_out.append(info)
    vs_out.sort(
        key=lambda x: (
            0 if x.get("isDefault") else 1,
            (x.get("name") or "").lower(),
        )
    )

    subnets_raw = _pc_paginated_get(
        s, base_url, "/api/networking/v4.0/config/subnets"
    )
    subnets_out: list[dict[str, Any]] = []
    for sub in subnets_raw:
        cluster_ext = sub.get("clusterReference")
        vs_ext = sub.get("virtualSwitchReference")
        cluster_info = cluster_lookup.get(cluster_ext or "")
        vs_info = vs_lookup.get(vs_ext or "")
        ip_cfg = sub.get("ipConfig") or []
        managed = False
        ip_prefix = None
        gateway = None
        if isinstance(ip_cfg, list) and ip_cfg:
            first = ip_cfg[0] or {}
            ipv4 = first.get("ipv4") or first.get("ipv6") or {}
            ip = ipv4.get("ipSubnet") or {}
            ip_addr = (ip.get("ip") or {}).get("value") if isinstance(ip.get("ip"), dict) else ip.get("ip")
            prefix_len = ip.get("prefixLength")
            if ip_addr and prefix_len:
                ip_prefix = f"{ip_addr}/{prefix_len}"
            gw_obj = ipv4.get("defaultGatewayIp")
            if isinstance(gw_obj, dict):
                gateway = gw_obj.get("value")
            elif gw_obj:
                gateway = gw_obj
            managed = bool(ipv4.get("ipPools") or ipv4.get("dhcpOptions") or ip_prefix)
        subnets_out.append(
            {
                "kind": "subnet",
                "extId": sub.get("extId"),
                "name": sub.get("name"),
                "vlan": sub.get("networkId"),
                "vlanKind": (
                    "single" if isinstance(sub.get("networkId"), int) else "none"
                ),
                "subnetType": sub.get("subnetType"),
                "managed": managed,
                "ipPrefix": ip_prefix,
                "gateway": gateway,
                "clusterExtId": cluster_ext,
                "clusterName": (cluster_info or {}).get("name"),
                "switchExtId": vs_ext,
                "switchName": (vs_info or {}).get("name"),
                "switchKind": "AHV-VS",
                "activeUplinks": [],
                "standbyUplinks": [],
                "teamingPolicy": (vs_info or {}).get("bondMode"),
                "failback": None,
            }
        )
    subnets_out.sort(
        key=lambda x: (
            (x.get("clusterName") or "").lower(),
            (x.get("name") or "").lower(),
        )
    )

    return jsonify(
        kind="pc",
        host=sess["host"],
        clusters=clusters_out,
        virtual_switches=vs_out,
        rows=subnets_out,
    )


def _try_import_pyvmomi():
    try:
        from pyVim.connect import Disconnect, SmartConnect  # type: ignore
        from pyVmomi import vim  # type: ignore

        return SmartConnect, Disconnect, vim, None
    except Exception as exc:  # pragma: no cover - optional dep
        return None, None, None, exc


@app.post("/api/source/vcenter/connect")
def api_source_vcenter_connect():
    _, _, _, imp_err = _try_import_pyvmomi()
    if imp_err is not None:
        return (
            jsonify(
                error=(
                    "pyvmomi is not installed in this environment. "
                    "Run: python -m pip install -r requirements.txt"
                ),
                detail=str(imp_err),
            ),
            503,
        )
    body = request.get_json(force=True, silent=True) or {}
    host = (body.get("host") or "").strip()
    port = body.get("port") or 443
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    ignore_ssl = bool(body.get("ignore_ssl", True))
    if not host or not username or not password:
        return jsonify(error="host, username, and password are required"), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify(error="port must be an integer"), 400

    # We reuse the same SmartConnect for the inventory walk so the user
    # gets the connect + the listing in a single round-trip. This kills
    # the "click 'Connect & list' twice" symptom: two back-to-back
    # SmartConnects against a busy vCenter sometimes drop the second one,
    # leaving the UI on a stale empty table.
    tmp_sess = {
        "host": host, "port": port,
        "username": username, "password": password,
        "ignore_ssl": ignore_ssl,
    }
    try:
        _, Disconnect, vim, si = _source_vcenter_smartconnect(tmp_sess)
    except Exception as exc:
        msg = str(exc)
        lower = msg.lower()
        if "incorrect user name or password" in lower or "cannot complete login" in lower:
            return jsonify(error="invalid vCenter credentials"), 401
        return jsonify(error=f"vCenter connect failed: {msg}"), 502

    about = None
    rows: list[dict[str, Any]] = []
    walk_error: str | None = None
    try:
        try:
            about = si.content.about
        except Exception:
            pass
        try:
            rows = _walk_source_vcenter_inventory(vim, si)
        except Exception as exc:
            walk_error = f"vCenter inventory failed: {exc}"
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass

    token = secrets.token_urlsafe(24)
    SOURCE_SESSIONS[token] = {
        "kind": "vcenter",
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "ignore_ssl": ignore_ssl,
        "created_at": time.time(),
    }
    payload: dict[str, Any] = {
        "source_token": token,
        "kind": "vcenter",
        "host": host,
        "port": port,
        "username": username,
        "about": {
            "name": getattr(about, "name", None),
            "version": getattr(about, "version", None),
            "build": getattr(about, "build", None),
            "fullName": getattr(about, "fullName", None),
        },
        "rows": rows,
    }
    if walk_error:
        payload["inventory_error"] = walk_error
    return jsonify(payload)


def _vcenter_decode_vlan(vim, dvpg_default_config) -> tuple[int | None, str]:
    """Return (vlan_id_or_None, vlanKind) from a DVPG default port config."""
    try:
        spec = dvpg_default_config.vlan
    except Exception:
        return None, "none"
    if isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec):
        return None, "trunk"
    if isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.PvlanSpec):
        return getattr(spec, "pvlanId", None), "pvlan"
    if isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec):
        vid = getattr(spec, "vlanId", 0) or 0
        return (vid if vid > 0 else None, "single" if vid > 0 else "none")
    return None, "unknown"


def _vcenter_decode_teaming(vim, default_port_config) -> dict[str, Any]:
    """Pull teaming/uplink info from a DVPG default port config."""
    out: dict[str, Any] = {
        "activeUplinks": [],
        "standbyUplinks": [],
        "teamingPolicy": None,
        "failback": None,
    }
    try:
        teaming = default_port_config.uplinkTeamingPolicy
    except Exception:
        teaming = None
    if not teaming:
        return out
    try:
        policy = teaming.policy
        if policy is not None:
            out["teamingPolicy"] = getattr(policy, "value", None)
    except Exception:
        pass
    try:
        failback = teaming.notifySwitches  # not failback per se, kept for parity
        out["failback"] = (
            None
            if teaming.rollingOrder is None
            else (not teaming.rollingOrder.value)
        )
    except Exception:
        pass
    try:
        upo = teaming.uplinkPortOrder
        if upo is not None:
            out["activeUplinks"] = list(getattr(upo, "activeUplinkPort", []) or [])
            out["standbyUplinks"] = list(
                getattr(upo, "standbyUplinkPort", []) or []
            )
    except Exception:
        pass
    return out


def _vcenter_collect_objects(content, vim, vimType):
    """Iterate all managed objects of the given type via container view.

    Note: this returns the bare MORefs - touching any attribute on a
    returned object (e.g. ``getattr(pg, "name", None)`` or
    ``pg.config.defaultPortConfig``) triggers a *separate* RPC to vCenter
    behind the scenes. For "list a few things" this is fine; for "walk
    hundreds of port groups" use ``_vc_pc_retrieve`` instead, which
    pulls every property of every object back in one RPC.
    """
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vimType], True
    )
    try:
        return list(view.view)
    finally:
        try:
            view.Destroy()
        except Exception:
            pass


def _vc_pc_retrieve(content, vim, vim_type, path_set, root=None, page=500):
    """Bulk-fetch (MORef, {prop: value}) tuples for every managed object
    of ``vim_type`` under ``root`` (defaults to rootFolder), in *one*
    RPC per page of ``page`` objects.

    Replaces N per-object property lookups (each its own SOAP call) with
    a single PropertyCollector round-trip. Critical for environments
    with hundreds of port groups / hosts: the per-object pattern that
    pyvmomi makes look like attribute access scales linearly with
    object count and round-trip latency.

    Returns ``[(MORef, {prop_name: value, ...}), ...]``. Missing props
    (vCenter declined to populate them) simply don't appear in the dict.
    """
    pc = content.propertyCollector
    view = content.viewManager.CreateContainerView(
        root or content.rootFolder, [vim_type], True
    )
    try:
        traversal = vim.PropertyCollector.TraversalSpec(
            name="view_to_obj",
            path="view",
            skip=False,
            type=vim.view.ContainerView,
        )
        obj_spec = vim.PropertyCollector.ObjectSpec(
            obj=view, skip=True, selectSet=[traversal]
        )
        prop_spec = vim.PropertyCollector.PropertySpec(
            type=vim_type, all=False, pathSet=list(path_set)
        )
        filter_spec = vim.PropertyCollector.FilterSpec(
            objectSet=[obj_spec], propSet=[prop_spec]
        )
        opts = vim.PropertyCollector.RetrieveOptions(maxObjects=page)
        out: list[tuple[Any, dict[str, Any]]] = []
        result = pc.RetrievePropertiesEx([filter_spec], opts)
        while result:
            for o in result.objects or []:
                props: dict[str, Any] = {}
                for p in o.propSet or []:
                    props[p.name] = p.val
                out.append((o.obj, props))
            token = getattr(result, "token", None)
            if not token:
                break
            result = pc.ContinueRetrievePropertiesEx(token=token)
        return out
    finally:
        try:
            view.Destroy()
        except Exception:
            pass


def _walk_source_vcenter_inventory(vim, si) -> list[dict[str, Any]]:
    """Inventory walk shared by /connect and /inventory; expects a live SI.

    Uses ``_vc_pc_retrieve`` so a vCenter with hundreds of port groups
    and dozens of hosts comes back in ~3 RPCs total (DVS, DVPG, Host)
    instead of one RPC *per accessed attribute on each object*. On a
    medium environment (~50 DVPGs, ~10 hosts) the walk drops from
    ~20 seconds to under a second.
    """
    content = si.content
    rows: list[dict[str, Any]] = []

    # 1) All DVS: name + uuid; build a moref->{name,uuid} lookup so we
    #    can map each DVPG back to its parent switch.
    dvs_props = _vc_pc_retrieve(
        content, vim, vim.DistributedVirtualSwitch,
        path_set=["name", "uuid"],
    )
    dvs_meta: dict[Any, dict[str, Any]] = {}
    for dvs_ref, p in dvs_props:
        dvs_meta[dvs_ref] = {
            "name": p.get("name"),
            "uuid": p.get("uuid"),
        }

    # 2) All DVPortgroups in one shot. ``config`` is the smallest
    #    property that contains ``uplink``, ``defaultPortConfig`` (with
    #    VLAN + teaming) and ``distributedVirtualSwitch`` (the parent
    #    DVS MORef) - so one RPC per N port groups, not 3*N.
    dvpg_props = _vc_pc_retrieve(
        content, vim, vim.dvs.DistributedVirtualPortgroup,
        path_set=["name", "key", "config"],
    )
    for pg_ref, p in dvpg_props:
        cfg = p.get("config")
        if cfg is None:
            continue
        if getattr(cfg, "uplink", False):
            continue
        pg_name = p.get("name") or getattr(cfg, "name", None)
        default_cfg = getattr(cfg, "defaultPortConfig", None)
        vlan_id, vlan_kind = (
            _vcenter_decode_vlan(vim, default_cfg) if default_cfg else (None, "unknown")
        )
        teaming = (
            _vcenter_decode_teaming(vim, default_cfg) if default_cfg else {
                "activeUplinks": [], "standbyUplinks": [],
                "teamingPolicy": None, "failback": None,
            }
        )
        parent_dvs = getattr(cfg, "distributedVirtualSwitch", None)
        parent_meta = dvs_meta.get(parent_dvs, {}) if parent_dvs else {}
        rows.append(
            {
                "kind": "portgroup",
                "extId": p.get("key"),
                "name": pg_name,
                "vlan": vlan_id,
                "vlanKind": vlan_kind,
                "subnetType": "VLAN" if vlan_kind == "single" else None,
                "managed": False,
                "ipPrefix": None,
                "gateway": None,
                "clusterExtId": None,
                "clusterName": None,
                "switchExtId": parent_meta.get("uuid"),
                "switchName": parent_meta.get("name"),
                "switchKind": "DVS",
                **teaming,
            }
        )

    # 3) Standard-vSwitch port groups via HostSystem.config.network in
    #    one bulk fetch. We then dedupe across hosts on (vswitch, pg).
    host_props = _vc_pc_retrieve(
        content, vim, vim.HostSystem,
        path_set=["name", "config.network"],
    )
    seen_std_pg = set()
    for _host_ref, p in host_props:
        ns = p.get("config.network")
        if ns is None:
            continue
        for pg in getattr(ns, "portgroup", []) or []:
            spec = getattr(pg, "spec", None)
            if not spec:
                continue
            key = (getattr(spec, "name", None), getattr(spec, "vswitchName", None))
            if key in seen_std_pg:
                continue
            seen_std_pg.add(key)
            vlan_id_raw = getattr(spec, "vlanId", 0) or 0
            if vlan_id_raw == 4095:
                vlan_id, vlan_kind = None, "trunk"
            elif vlan_id_raw == 0:
                vlan_id, vlan_kind = None, "none"
            else:
                vlan_id, vlan_kind = vlan_id_raw, "single"
            teaming_policy = None
            active_uplinks: list[str] = []
            standby_uplinks: list[str] = []
            failback = None
            try:
                nic_pol = pg.computedPolicy.nicTeaming
                teaming_policy = getattr(nic_pol.policy, "value", None)
                failback = getattr(nic_pol, "rollingOrder", None)
                if failback is not None:
                    failback = not failback
                no = nic_pol.nicOrder
                if no:
                    active_uplinks = list(getattr(no, "activeNic", []) or [])
                    standby_uplinks = list(getattr(no, "standbyNic", []) or [])
            except Exception:
                pass
            rows.append(
                {
                    "kind": "portgroup",
                    "extId": f"std:{getattr(spec, 'vswitchName', '')}:{getattr(spec, 'name', '')}",
                    "name": getattr(spec, "name", None),
                    "vlan": vlan_id,
                    "vlanKind": vlan_kind,
                    "subnetType": "VLAN" if vlan_kind == "single" else None,
                    "managed": False,
                    "ipPrefix": None,
                    "gateway": None,
                    "clusterExtId": None,
                    "clusterName": None,
                    "switchExtId": None,
                    "switchName": getattr(spec, "vswitchName", None),
                    "switchKind": "vSwitch",
                    "activeUplinks": active_uplinks,
                    "standbyUplinks": standby_uplinks,
                    "teamingPolicy": teaming_policy,
                    "failback": failback,
                }
            )

    rows.sort(
        key=lambda x: (
            (x.get("switchName") or "").lower(),
            (x.get("name") or "").lower(),
        )
    )
    return rows


def _source_vcenter_smartconnect(sess: dict[str, Any]):
    """Open a fresh SI against a source vCenter session, with one retry.

    Used by both /api/source/vcenter/connect (to verify creds + walk
    inventory in one round trip) and /api/source/vcenter/inventory (the
    explicit refresh path). Same retry semantics as the target helper.
    """
    SmartConnect, Disconnect, vim, imp_err = _try_import_pyvmomi()
    if imp_err is not None:
        raise RuntimeError(f"pyvmomi is not installed: {imp_err}")
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    if sess.get("ignore_ssl", True):
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            si = SmartConnect(
                host=sess["host"], port=sess["port"], user=sess["username"],
                pwd=sess["password"], sslContext=ctx,
                connectionPoolTimeout=15,
            )
            return SmartConnect, Disconnect, vim, si
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.75)
            continue
    raise last_exc if last_exc else RuntimeError("vCenter connect failed")


@app.post("/api/source/vcenter/inventory")
def api_source_vcenter_inventory():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_source_session(body.get("source_token"))
    if not sess or sess.get("kind") != "vcenter":
        return jsonify(error="not connected to a source vCenter"), 401
    try:
        _, Disconnect, vim, si = _source_vcenter_smartconnect(sess)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        return jsonify(error=f"vCenter reconnect failed: {exc}"), 502
    try:
        rows = _walk_source_vcenter_inventory(vim, si)
        return jsonify(kind="vcenter", host=sess["host"], rows=rows)
    except Exception as exc:
        return jsonify(error=f"vCenter inventory failed: {exc}"), 502
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Target vCenter / ESXi: write-side endpoints for creating port groups on a
# VMware Standard vSwitch (VSS, per host) or a Distributed Virtual Switch
# (VDS, vCenter only). Sessions live in TARGET_VCENTER_SESSIONS so they do
# not collide with the source-side vCenter session namespace.
# ---------------------------------------------------------------------------


def _target_vcenter_connect_si(sess: dict[str, Any]):
    """Open a fresh ServiceInstance for the duration of a request.

    Mirrors the pattern used by api_source_vcenter_inventory: we do not hold
    long-lived vCenter sessions in process memory, only the credentials.

    Retries once on transient errors. vCenter occasionally drops the next
    SmartConnect after a fresh Disconnect (especially under quick succession,
    e.g. when the UI toggles VDS<->VSS), so a single back-off retry covers
    that without holding long-lived sessions in process memory.
    """
    SmartConnect, Disconnect, vim, imp_err = _try_import_pyvmomi()
    if imp_err is not None:
        raise RuntimeError(f"pyvmomi is not installed: {imp_err}")
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    if sess.get("ignore_ssl", True):
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            si = SmartConnect(
                host=sess["host"], port=sess["port"], user=sess["username"],
                pwd=sess["password"], sslContext=ctx,
                connectionPoolTimeout=15,
            )
            return SmartConnect, Disconnect, vim, si
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.75)
            continue
    raise last_exc if last_exc else RuntimeError("vCenter connect failed")


@app.post("/api/target/vcenter/connect")
def api_target_vcenter_connect():
    SmartConnect, Disconnect, vim, imp_err = _try_import_pyvmomi()
    if imp_err is not None:
        return (
            jsonify(
                error=(
                    "pyvmomi is not installed in this environment. "
                    "Run: python -m pip install -r requirements.txt"
                ),
                detail=str(imp_err),
            ),
            503,
        )
    body = request.get_json(force=True, silent=True) or {}
    host = (body.get("host") or "").strip()
    port = body.get("port") or 443
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    ignore_ssl = bool(body.get("ignore_ssl", True))
    if not host or not username or not password:
        return jsonify(error="host, username, and password are required"), 400
    try:
        port = int(port)
    except (TypeError, ValueError):
        return jsonify(error="port must be an integer"), 400

    import ssl as _ssl

    ctx = _ssl.create_default_context()
    if ignore_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    try:
        si = SmartConnect(
            host=host, port=port, user=username, pwd=password, sslContext=ctx,
            connectionPoolTimeout=15,
        )
    except Exception as exc:
        msg = str(exc)
        lower = msg.lower()
        if "incorrect user name or password" in lower or "cannot complete login" in lower:
            return jsonify(error="invalid vCenter credentials"), 401
        return jsonify(error=f"vCenter connect failed: {msg}"), 502
    about = None
    try:
        about = si.content.about
    except Exception:
        pass
    token = secrets.token_urlsafe(24)
    TARGET_VCENTER_SESSIONS[token] = {
        "kind": "vcenter",
        "host": host,
        "port": port,
        "username": username,
        "password": password,
        "ignore_ssl": ignore_ssl,
        "created_at": time.time(),
    }
    try:
        Disconnect(si)
    except Exception:
        pass
    return jsonify(
        target_token=token,
        kind="vcenter",
        host=host,
        port=port,
        username=username,
        about={
            "name": getattr(about, "name", None),
            "version": getattr(about, "version", None),
            "build": getattr(about, "build", None),
            "fullName": getattr(about, "fullName", None),
            "apiType": getattr(about, "apiType", None),
        },
    )


def _collect(content, vim, vim_type):
    view = content.viewManager.CreateContainerView(
        content.rootFolder, [vim_type], True
    )
    try:
        return list(view.view)
    finally:
        try:
            view.Destroy()
        except Exception:
            pass


@app.post("/api/target/vcenter/switches")
def api_target_vcenter_switches():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_target_vcenter_session(body.get("target_token"))
    if not sess:
        return jsonify(error="target vCenter session not found (expired, or NP4M restarted) - reconnect in step 1"), 401
    switch_kind = (body.get("switch_kind") or "").strip().lower()
    if switch_kind not in {"vss", "vds"}:
        return jsonify(error="switch_kind must be 'vss' or 'vds'"), 400
    try:
        _, Disconnect, vim, si = _target_vcenter_connect_si(sess)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        return jsonify(error=f"vCenter reconnect failed: {exc}"), 502
    try:
        content = si.content
        if switch_kind == "vds":
            # Bulk-fetch DVS metadata in one RPC. ``config.host`` carries
            # the list of host MORefs already; we resolve their names
            # with a single HostSystem.name fetch and a moref->name map
            # rather than ``getattr(host, "name", None)`` per member.
            dvs_props = _vc_pc_retrieve(
                content, vim, vim.DistributedVirtualSwitch,
                path_set=["name", "uuid", "config.host"],
            )
            host_props = _vc_pc_retrieve(
                content, vim, vim.HostSystem,
                path_set=["name"],
            )
            host_name_by_ref = {ref: p.get("name") for ref, p in host_props}
            out: list[dict[str, Any]] = []
            for _dvs_ref, p in dvs_props:
                hosts: list[str] = []
                for member in p.get("config.host") or []:
                    host_ref = getattr(member, "config", None)
                    host = getattr(host_ref, "host", None) if host_ref else None
                    name = host_name_by_ref.get(host) or getattr(host, "name", None)
                    if name:
                        hosts.append(name)
                out.append(
                    {
                        "name": p.get("name"),
                        "uuid": p.get("uuid"),
                        "hosts": hosts,
                    }
                )
            out.sort(key=lambda x: (x.get("name") or "").lower())
            return jsonify(kind="vds", switches=out)

        # VSS path: one bulk fetch of HostSystem.config.network. The
        # ``vswitch`` array on each host gives us the per-host list of
        # standard vSwitches.
        host_props = _vc_pc_retrieve(
            content, vim, vim.HostSystem,
            path_set=["name", "config.network"],
        )
        coverage: dict[str, set[str]] = {}
        for _host_ref, p in host_props:
            ns = p.get("config.network")
            if ns is None:
                continue
            host_name = p.get("name") or "(unknown)"
            for vsw in getattr(ns, "vswitch", []) or []:
                vsw_name = getattr(vsw, "name", None)
                if not vsw_name:
                    continue
                coverage.setdefault(vsw_name, set()).add(host_name)
        out_vss = [
            {"name": name, "hosts": sorted(hosts)}
            for name, hosts in coverage.items()
        ]
        out_vss.sort(key=lambda x: x["name"].lower())
        return jsonify(kind="vss", switches=out_vss)
    except Exception as exc:
        return jsonify(error=f"vCenter switch list failed: {exc}"), 502
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


@app.post("/api/target/vcenter/hosts")
def api_target_vcenter_hosts():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_target_vcenter_session(body.get("target_token"))
    if not sess:
        return jsonify(error="target vCenter session not found (expired, or NP4M restarted) - reconnect in step 1"), 401
    vswitch_name = (body.get("vswitch_name") or "").strip()
    try:
        _, Disconnect, vim, si = _target_vcenter_connect_si(sess)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        return jsonify(error=f"vCenter reconnect failed: {exc}"), 502
    try:
        # Bulk-fetch all hosts in one RPC: name + (only if filtering by
        # vSwitch) config.network. Without the filter we skip the heavy
        # config.network property entirely.
        if vswitch_name:
            host_props = _vc_pc_retrieve(
                si.content, vim, vim.HostSystem,
                path_set=["name", "config.network"],
            )
        else:
            host_props = _vc_pc_retrieve(
                si.content, vim, vim.HostSystem,
                path_set=["name"],
            )
        out: list[dict[str, Any]] = []
        for host_ref, p in host_props:
            host_name = p.get("name") or ""
            if vswitch_name:
                ns = p.get("config.network")
                names = []
                if ns is not None:
                    names = [getattr(v, "name", None) for v in getattr(ns, "vswitch", []) or []]
                if vswitch_name not in (names or []):
                    continue
            out.append(
                {
                    "name": host_name,
                    "moid": getattr(host_ref, "_moId", None),
                }
            )
        out.sort(key=lambda x: x["name"].lower())
        return jsonify(hosts=out)
    except Exception as exc:
        return jsonify(error=f"vCenter host list failed: {exc}"), 502
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


def _vds_pg_rows(vim, content, switch_name: str) -> list[dict[str, Any]]:
    """List port groups on a specific DVS by name, using PC bulk-fetch
    for both the DVS lookup and the port-group walk. One RPC each,
    instead of the old N-attribute-per-PG pattern that scales linearly
    with port-group count."""
    rows: list[dict[str, Any]] = []

    # Resolve the target DVS by name once, so we can match per-PG by
    # MORef equality rather than triggering a `.name` RPC per PG.
    dvs_props = _vc_pc_retrieve(
        content, vim, vim.DistributedVirtualSwitch,
        path_set=["name"],
    )
    target_ref = None
    for ref, dp in dvs_props:
        if dp.get("name") == switch_name:
            target_ref = ref
            break
    if target_ref is None:
        return rows

    dvpg_props = _vc_pc_retrieve(
        content, vim, vim.dvs.DistributedVirtualPortgroup,
        path_set=["name", "config"],
    )
    for _pg_ref, p in dvpg_props:
        cfg = p.get("config")
        if cfg is None:
            continue
        if getattr(cfg, "uplink", False):
            continue
        parent = getattr(cfg, "distributedVirtualSwitch", None)
        if parent != target_ref:
            continue
        name = p.get("name") or getattr(cfg, "name", None)
        vlan_id = None
        vlan_kind = "unknown"
        try:
            spec = cfg.defaultPortConfig.vlan
            if isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec):
                vlan_kind = "trunk"
            elif isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.PvlanSpec):
                vlan_kind = "pvlan"
                vlan_id = getattr(spec, "pvlanId", None)
            elif isinstance(spec, vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec):
                vid = getattr(spec, "vlanId", 0) or 0
                vlan_id = vid if vid > 0 else None
                vlan_kind = "single" if vid > 0 else "none"
        except Exception:
            pass
        rows.append(
            {
                "name": name,
                "vlan": vlan_id,
                "vlanKind": vlan_kind,
                "switchKind": "DVS",
                "switchName": switch_name,
                "hosts": [],
            }
        )
    return rows


def _vss_pg_rows(
    vim, content, vswitch_name: str, host_filter: list[str] | None
) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, int | None, str], dict[str, Any]] = {}
    wanted = set(host_filter) if host_filter else None
    host_props = _vc_pc_retrieve(
        content, vim, vim.HostSystem,
        path_set=["name", "config.network"],
    )
    for _host_ref, p in host_props:
        host_name = p.get("name") or ""
        if wanted and host_name not in wanted:
            continue
        ns = p.get("config.network")
        if ns is None:
            continue
        for pg in getattr(ns, "portgroup", []) or []:
            spec = getattr(pg, "spec", None)
            if not spec:
                continue
            if getattr(spec, "vswitchName", None) != vswitch_name:
                continue
            name = getattr(spec, "name", None)
            vlan_raw = getattr(spec, "vlanId", 0) or 0
            if vlan_raw == 4095:
                vlan_id, vlan_kind = None, "trunk"
            elif vlan_raw == 0:
                vlan_id, vlan_kind = None, "none"
            else:
                vlan_id, vlan_kind = vlan_raw, "single"
            key = (name or "", vlan_id, vlan_kind)
            row = by_key.get(key)
            if row is None:
                row = {
                    "name": name,
                    "vlan": vlan_id,
                    "vlanKind": vlan_kind,
                    "switchKind": "vSwitch",
                    "switchName": vswitch_name,
                    "hosts": [],
                }
                by_key[key] = row
            row["hosts"].append(host_name)
    return list(by_key.values())


@app.post("/api/target/vcenter/portgroups")
def api_target_vcenter_portgroups():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_target_vcenter_session(body.get("target_token"))
    if not sess:
        return jsonify(error="target vCenter session not found (expired, or NP4M restarted) - reconnect in step 1"), 401
    switch_kind = (body.get("switch_kind") or "").strip().lower()
    switch_name = (body.get("switch_name") or "").strip()
    hosts = body.get("hosts") or []
    if switch_kind not in {"vss", "vds"}:
        return jsonify(error="switch_kind must be 'vss' or 'vds'"), 400
    if not switch_name:
        return jsonify(error="switch_name is required"), 400
    try:
        _, Disconnect, vim, si = _target_vcenter_connect_si(sess)
    except RuntimeError as exc:
        return jsonify(error=str(exc)), 503
    except Exception as exc:
        return jsonify(error=f"vCenter reconnect failed: {exc}"), 502
    try:
        content = si.content
        if switch_kind == "vds":
            rows = _vds_pg_rows(vim, content, switch_name)
        else:
            rows = _vss_pg_rows(vim, content, switch_name, hosts)
        rows.sort(
            key=lambda r: (
                r.get("vlan") if isinstance(r.get("vlan"), int) else 99999,
                (r.get("name") or "").lower(),
            )
        )
        return jsonify(rows=rows)
    except Exception as exc:
        return jsonify(error=f"vCenter portgroup list failed: {exc}"), 502
    finally:
        try:
            Disconnect(si)
        except Exception:
            pass


def _wait_for_vim_task(task, timeout_seconds: int = 120, poll_interval: float = 1.5):
    """Block until a pyvmomi task reaches success or error."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            state = task.info.state
        except Exception:
            state = None
        if str(state) in {"success", "TaskInfoState.success"}:
            return "success", None
        if str(state) in {"error", "TaskInfoState.error"}:
            err = None
            try:
                err = task.info.error.localizedMessage
            except Exception:
                pass
            return "error", err or "task reported error"
        time.sleep(poll_interval)
    return "timeout", f"task did not finish within {timeout_seconds}s"


@app.post("/api/target/vcenter/create")
def api_target_vcenter_create():
    body = request.get_json(force=True, silent=True) or {}
    sess = _get_target_vcenter_session(body.get("target_token"))
    if not sess:
        return jsonify(error="target vCenter session not found (expired, or NP4M restarted) - reconnect in step 1"), 401
    switch_kind = (body.get("switch_kind") or "").strip().lower()
    switch_name = (body.get("switch_name") or "").strip()
    hosts = body.get("hosts") or []
    networks = body.get("networks") or []
    pg_type = (body.get("pg_type") or "earlyBinding").strip()
    try:
        num_ports = int(body.get("num_ports") or 8)
    except (TypeError, ValueError):
        return jsonify(error="num_ports must be an integer"), 400
    task_timeout = 120
    try:
        task_timeout = int(body.get("task_timeout") or 120)
    except (TypeError, ValueError):
        pass

    if switch_kind not in {"vss", "vds"}:
        return jsonify(error="switch_kind must be 'vss' or 'vds'"), 400
    if not switch_name:
        return jsonify(error="switch_name is required"), 400
    if not isinstance(networks, list) or not networks:
        return jsonify(error="networks list is required"), 400
    if switch_kind == "vss" and not hosts:
        return jsonify(error="hosts is required for VSS create"), 400

    def emit(level: str, msg: str) -> str:
        return json.dumps({"level": level, "msg": msg}) + "\n"

    def gen():
        try:
            _, Disconnect, vim, si = _target_vcenter_connect_si(sess)
        except RuntimeError as exc:
            yield emit("error", str(exc))
            return
        except Exception as exc:
            yield emit("error", f"vCenter reconnect failed: {exc}")
            return
        try:
            content = si.content
            valid: list[tuple[str, int]] = []
            for net in networks:
                name = (net or {}).get("name")
                vlan = (net or {}).get("vlan")
                if not name or vlan is None:
                    yield emit("error", f"Skipping invalid entry: {net!r}")
                    continue
                try:
                    vlan_i = int(vlan)
                except (TypeError, ValueError):
                    yield emit("error", f"{name}: invalid VLAN {vlan!r}")
                    continue
                if vlan_i < 0 or vlan_i > 4094:
                    yield emit("error", f"{name}: VLAN out of range ({vlan_i})")
                    continue
                valid.append((name, vlan_i))

            if not valid:
                yield emit("error", "No valid networks to create.")
                return

            success = 0
            fail = 0

            if switch_kind == "vds":
                # Locate the DVS by name in a single PC fetch.
                dvs_props = _vc_pc_retrieve(
                    content, vim, vim.DistributedVirtualSwitch,
                    path_set=["name"],
                )
                target = None
                for ref, p in dvs_props:
                    if p.get("name") == switch_name:
                        target = ref
                        break
                if not target:
                    yield emit("error", f"DVS '{switch_name}' not found.")
                    return

                # Bulk-fetch existing port groups on *this* DVS in one
                # RPC. Pre-filter by parent DVS MORef so we ignore PGs
                # that belong to other DVS instances. (`==` works on
                # pyvmomi MORefs - they compare by managed-object id.)
                t0 = time.time()
                existing_lower: set[str] = set()
                try:
                    pg_props = _vc_pc_retrieve(
                        content, vim, vim.dvs.DistributedVirtualPortgroup,
                        path_set=["name", "config.distributedVirtualSwitch"],
                    )
                    for _ref, p in pg_props:
                        parent = p.get("config.distributedVirtualSwitch")
                        if parent != target:
                            continue
                        n = p.get("name")
                        if n:
                            existing_lower.add(n.lower())
                except Exception as exc:
                    yield emit(
                        "warn",
                        f"  preflight: could not list existing port groups: {exc}",
                    )
                yield emit(
                    "info",
                    f"DVS '{switch_name}' currently has {len(existing_lower)} "
                    f"port group(s) (preflight took "
                    f"{int((time.time() - t0) * 1000)} ms).",
                )

                specs: list[Any] = []
                queued: list[str] = []
                for name, vlan_i in valid:
                    if name.lower() in existing_lower:
                        yield emit(
                            "error",
                            f"Skipping '{name}': a dvportgroup with that name "
                            f"already exists on DVS '{switch_name}'.",
                        )
                        fail += 1
                        continue
                    vlan_spec = vim.dvs.VmwareDistributedVirtualSwitch.VlanIdSpec(
                        inherited=False, vlanId=vlan_i,
                    )
                    port_setting = (
                        vim.dvs.VmwareDistributedVirtualSwitch.VmwarePortConfigPolicy(
                            vlan=vlan_spec,
                        )
                    )
                    pg_spec = vim.dvs.DistributedVirtualPortgroup.ConfigSpec(
                        name=name,
                        numPorts=num_ports,
                        type=pg_type,
                        defaultPortConfig=port_setting,
                    )
                    specs.append(pg_spec)
                    queued.append(name)
                    yield emit(
                        "info",
                        f"Queueing dvportgroup '{name}' (VLAN {vlan_i}, "
                        f"type={pg_type}, ports={num_ports}).",
                    )

                if specs:
                    # Scale the task timeout with the batch size: small
                    # batches finish in seconds, but a single
                    # AddDVPortgroup_Task with hundreds of specs can run
                    # for a couple of minutes on a busy vCenter. Allow
                    # the body to override this; otherwise pick a
                    # reasonable per-spec budget bounded at 15 min.
                    effective_timeout = max(
                        task_timeout, min(900, 60 + 2 * len(specs))
                    )
                    yield emit(
                        "info",
                        f"Submitting AddDVPortgroup_Task with {len(specs)} "
                        f"spec(s) (timeout {effective_timeout}s)..."
                    )
                    try:
                        task = target.AddDVPortgroup_Task(specs)
                    except Exception as exc:
                        yield emit("error", f"AddDVPortgroup_Task failed: {exc}")
                        fail += len(specs)
                    else:
                        state, err = _wait_for_vim_task(task, timeout_seconds=effective_timeout)
                        if state == "success":
                            for n in queued:
                                yield emit("ok", f"  OK: dvportgroup '{n}' created.")
                                existing_lower.add(n.lower())
                                success += 1
                        else:
                            yield emit(
                                "error",
                                f"  FAIL: AddDVPortgroup_Task {state}: {err}",
                            )
                            fail += len(specs)
            else:
                # Bulk-fetch every host's name + network info +
                # networkSystem MORef in a single RPC. This replaces the
                # old "iterate hosts then poke .config.network and
                # .configManager.networkSystem per host" pattern, which
                # was 3 RPCs per host on top of the initial enumeration.
                wanted = set(hosts)
                t0 = time.time()
                host_props = _vc_pc_retrieve(
                    content, vim, vim.HostSystem,
                    path_set=["name", "config.network", "configManager.networkSystem"],
                )
                host_objs: dict[str, dict[str, Any]] = {}
                for _ref, p in host_props:
                    n = p.get("name")
                    if n and n in wanted:
                        host_objs[n] = {
                            "name": n,
                            "network": p.get("config.network"),
                            "networkSystem": p.get("configManager.networkSystem"),
                        }
                missing = sorted(wanted - set(host_objs.keys()))
                if missing:
                    yield emit("warn", f"  hosts not found in vCenter: {missing}")

                yield emit(
                    "info",
                    f"Resolved {len(host_objs)} host(s) for vSwitch "
                    f"'{switch_name}' (preflight took "
                    f"{int((time.time() - t0) * 1000)} ms).",
                )

                for host_name, host_info in host_objs.items():
                    net = host_info.get("network")
                    try:
                        existing = {
                            getattr(pg.spec, "name", None)
                            for pg in (getattr(net, "portgroup", []) or [])
                            if getattr(pg.spec, "vswitchName", None) == switch_name
                        } if net is not None else set()
                    except Exception:
                        existing = set()
                    existing_lower = {
                        (n or "").lower() for n in existing if n
                    }
                    ns = host_info["networkSystem"]
                    if ns is None:
                        yield emit(
                            "error",
                            f"  '{host_name}': no networkSystem available; skipping host.",
                        )
                        continue
                    for name, vlan_i in valid:
                        if name.lower() in existing_lower:
                            yield emit(
                                "warn",
                                f"  '{name}' already on '{host_name}': skipped.",
                            )
                            continue
                        yield emit(
                            "info",
                            f"Creating port group '{name}' (VLAN {vlan_i}) "
                            f"on host '{host_name}' / vSwitch '{switch_name}'...",
                        )
                        spec = vim.host.PortGroup.Specification(
                            name=name,
                            vlanId=vlan_i,
                            vswitchName=switch_name,
                            policy=vim.host.NetworkPolicy(),
                        )
                        try:
                            ns.AddPortGroup(spec)
                        except vim.fault.AlreadyExists:
                            yield emit(
                                "warn",
                                f"  '{name}' on '{host_name}': already exists.",
                            )
                            continue
                        except Exception as exc:
                            yield emit(
                                "error",
                                f"  FAIL: '{name}' on '{host_name}': {exc}",
                            )
                            fail += 1
                            continue
                        yield emit(
                            "ok",
                            f"  OK: '{name}' created on '{host_name}'.",
                        )
                        existing_lower.add(name.lower())
                        success += 1

            yield emit(
                "info",
                f"Done. {success} succeeded, {fail} failed.",
            )
        finally:
            try:
                Disconnect(si)
            except Exception:
                pass

    return Response(
        stream_with_context(gen()),
        mimetype="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _parse_cli_mode() -> str | None:
    """Tiny argv scan so we don't drag argparse in for one flag.
    `--mode probe` / `--mode master` flips the initial role at boot,
    overriding NP4M_MODE which mode.py read at import time."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg == "--mode" and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--mode="):
            return arg.split("=", 1)[1]
    return None


if __name__ == "__main__":
    cli_mode = _parse_cli_mode()
    if cli_mode:
        try:
            _mode.set_mode(cli_mode)
        except ValueError as _exc:
            print(f"warning: ignoring --mode {cli_mode!r}: {_exc}")
    host = os.environ.get("WEB_HOST", "127.0.0.1")
    default_port = "5050" if _mode.is_probe() else "5000"
    port = int(os.environ.get("WEB_PORT", default_port))
    debug = os.environ.get("WEB_DEBUG") == "1"
    print(
        f"NP4M v{__version__} build {BUILD} starting in {_mode.get_mode().value.upper()} mode "
        f"on http://{host}:{port}"
    )
    app.run(host=host, port=port, debug=debug, threaded=True)
