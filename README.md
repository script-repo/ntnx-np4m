# NP4M

A small Flask web app for **bulk-creating L2 networks on Nutanix AHV or
VMware vSphere**:

- **Nutanix target** — create unmanaged VLAN subnets on a Prism Central-managed
  AHV cluster via the Prism Central v4 REST APIs.
- **VMware target** — create port groups on a Distributed Virtual Switch (VDS)
  or on a Standard vSwitch (VSS) across one or more ESXi hosts, via the vSphere
  API (`pyvmomi`).

Both targets share the same UI, the same `name,vlan` textarea, and the same
optional **source import** from another Prism Central, a vCenter, or a
standalone ESXi host.

NP4M was built for migration-style workflows where you need to recreate dozens
of VLAN-backed L2 networks on a destination cluster (AHV or vSphere) in one
shot, mirroring what already exists somewhere else.

The current page shows a `vX.Y.Z build N` badge next to the title; the
constants live at the top of `app.py` and are bumped on every commit.

---

## Features

### Pick a target platform
- **Nutanix Prism Central** — basic auth (`admin` + password) or an API key
  (`Authorization: Bearer <token>`). Pick a target AHV cluster (PE) and a
  virtual switch on it.
- **VMware vCenter / ESXi** — username + password. Pick whether to target a
  Distributed Virtual Switch (VDS, vCenter only) or a Standard vSwitch (VSS).
  For VSS, pick one or more ESXi hosts; the port group is created on each.

### Optional: import from a source
Connect to **another PC, a vCenter, or a standalone ESXi host** and browse its
existing subnets / port-groups in a sortable, filterable table:
- Nutanix source: clusters, virtual switches, subnets, VLAN, managed/IP
  config, per-host uplinks.
- VMware source: port-group name, VLAN id / trunk / PVLAN, switch name,
  active/standby uplinks, teaming policy, failback.
  - vCenter exposes both DVS and per-host standard vSwitches.
  - Standalone ESXi exposes only that host's standard vSwitches (DVS objects
    are vCenter-managed).

Select rows + click "Add selected" to populate the create-list with their
`name,vlan` pairs. Names are sanitized; trunks / PVLANs / out-of-range VLANs
are flagged and never auto-imported.

### Create
- Paste/edit additional networks freehand.
- Click **Create networks** / **Create port groups** and watch a streaming
  color-coded log.
  - Nutanix: each subnet POST is followed by task polling until `SUCCEEDED` /
    `FAILED`. If the target cluster rejects `isAdvancedNetworking=true`, NP4M
    transparently retries with `false`.
  - VMware VDS: a single batched `AddDVPortgroup_Task` is submitted and polled
    to completion. VMware VSS: `AddPortGroup` is invoked per (host x port
    group); same-named port groups on a host are reported and skipped.
- Pre-flight **duplicate-name check** against the destination scope (cluster
  for Nutanix, DVS for VDS, each host for VSS).
- Live **"Existing networks / port groups on target"** panel auto-refreshes
  when you change cluster / switch and again after every Create run, so you
  can verify what actually landed.

### CLI extras
A standalone CLI helper (`create_subnets.py`) is included for scripted /
non-UI provisioning of Nutanix subnets.

---

## Repository layout

```
ntnx-np4m/
├── app.py               # Flask backend (target + source endpoints, streaming /api/create)
├── templates/
│   └── index.html       # Single-page UI (HTML + CSS + vanilla JS, no build step)
├── create_subnets.py    # Optional CLI: create N unmanaged VLAN subnets
├── requirements.txt     # Python deps
├── .gitignore
└── README.md
```

---

## Prerequisites

- **Python 3.10+** (3.12 is what NP4M was developed and tested on).
- Network reachability from your workstation to:
  - the **target** Prism Central on TCP/9440 (HTTPS),
  - the **source** Prism Central on TCP/9440, vCenter on TCP/443, and/or
    ESXi host on TCP/443 if you plan to use the import feature.
- Credentials for each system (or an API key for Prism Central).
- A modern browser (Chrome, Edge, Firefox, Safari).

> Prism Central usually presents a self-signed TLS certificate. NP4M disables
> certificate verification by default. Don't expose this app to untrusted
> networks.

---

## Install

Pick whichever path matches the box you're on. All three end up with the same
running app; the one-command installers just save you the manual steps.

### Install on Linux (one command)

Works on any modern Linux distro: the installer detects the family from
`/etc/os-release` and dispatches to the right package manager. Needs root
because it installs system packages, a service user, and (where systemd is
available) a service unit. Re-running it upgrades NP4M in place
(`git pull` + `pip install -U`).

| Family                                                          | Package manager | Notes                                                                                  |
|-----------------------------------------------------------------|-----------------|----------------------------------------------------------------------------------------|
| Debian, Ubuntu, Mint, Pop!_OS, elementary, Kali, Raspbian       | `apt-get`       | Installs `python3*-venv` automatically.                                                |
| RHEL, Rocky, Alma, Oracle Linux, Fedora, CentOS Stream, Amazon  | `dnf` (`yum`)   | On unsubscribed RHEL 8/9, drops a UBI repo so `dnf` still works.                       |
| openSUSE Leap, openSUSE Tumbleweed, SUSE Linux Enterprise       | `zypper`        | Prefers `python312` / `python311`, falls back to `python3`.                            |
| Arch, Manjaro, EndeavourOS, Garuda, CachyOS                     | `pacman`        | Uses the rolling `python` package.                                                     |
| Alpine                                                          | `apk`           | No systemd by default; the installer prints the manual `gunicorn` start command. |

If your distro isn't in the list above, set `NP4M_PY=/path/to/python3` (3.10+
with the `venv` module) and the script will skip the package step entirely.

```bash
curl -fsSL https://raw.githubusercontent.com/script-repo/ntnx-np4m/main/install.sh | sudo bash
```

With overrides (any env var the script understands, e.g. bind to loopback
only on port 8443):

```bash
curl -fsSL https://raw.githubusercontent.com/script-repo/ntnx-np4m/main/install.sh \
  | sudo env NP4M_BIND=127.0.0.1 NP4M_PORT=8443 bash
```

What it does (idempotent):

1. Reads `/etc/os-release`, picks one of `{debian, rhel, suse, arch, alpine}`,
   and dispatches package installs through `apt-get` / `dnf` / `zypper` /
   `pacman` / `apk` accordingly. On unsubscribed RHEL 8/9 also drops a UBI
   repo so `dnf install` works without a Red Hat subscription.
2. Finds the highest-numbered Python 3.10+ on the box that has the `venv`
   module (scans `python3.13`, `python3.12`, `python3.11`, `python3.10`,
   `python3`, `python`); installs one via the distro's package manager only
   if none is found.
3. Ensures `git` and `openssl` are present, installing them via the same
   package manager if missing.
4. Creates a system user `np4m` (via `useradd`, falling back to BusyBox
   `adduser` on Alpine) and clones the repo to `/home/np4m/ntnx-np4m`.
5. Builds a virtualenv with the detected Python and installs
   `requirements.txt` plus `gunicorn`.
6. **If `NP4M_BIND != 127.0.0.1`** (the default), generates a self-signed TLS
   cert at `<install-dir>/tls/np4m.{crt,key}` with the host's FQDN and
   primary IP in the SAN, then opens the port in `firewalld` or `ufw` if
   either is active.
7. If `systemd` is present, writes `/etc/systemd/system/np4m.service` and
   runs `systemctl enable --now np4m`. If not (e.g. Alpine + OpenRC), prints
   the manual `gunicorn` start line so you can wire it into your own init
   system.
8. Prints the URL, the `journalctl -u np4m -f` hint, and the result of a
   `curl` to `/api/version` so you can confirm the app is live.

| Env var          | Default                                          | Purpose                                                |
|------------------|--------------------------------------------------|--------------------------------------------------------|
| `NP4M_BIND`      | `0.0.0.0`                                        | Bind address. Loopback => plain HTTP; anything else => HTTPS. |
| `NP4M_PORT`      | `8443` when HTTPS, `8080` when loopback          | TCP port for gunicorn.                                 |
| `NP4M_TLS_CERT`  | `<install-dir>/tls/np4m.crt` (self-signed)       | Bring your own cert (skips self-signed generation).    |
| `NP4M_TLS_KEY`   | `<install-dir>/tls/np4m.key` (self-signed)       | Bring your own key.                                    |
| `NP4M_USER`      | `np4m`                                           | Service user.                                          |
| `NP4M_HOME`      | `/home/<user>`                                   | Service user home.                                     |
| `NP4M_DIR`       | `<home>/ntnx-np4m`                               | Install directory.                                     |
| `NP4M_REPO_URL`  | `https://github.com/script-repo/ntnx-np4m.git`   | Fork / mirror override.                                |
| `NP4M_PY`        | auto-detect highest 3.10+                        | Force a specific interpreter binary (full path or name on PATH). |
| `NP4M_OPEN_FW`   | `yes`                                            | Set to `no` to skip the firewalld / ufw step.          |
| `NP4M_SYSTEMD`   | `yes`                                            | Set to `no` to skip writing the systemd unit.          |

> The self-signed cert will trip a browser warning on first visit. To use a
> trusted cert, copy it onto the box and re-run the installer with
> `NP4M_TLS_CERT=/path/to/server.crt NP4M_TLS_KEY=/path/to/server.key`.

Service management once installed:

```bash
systemctl status np4m
systemctl restart np4m
journalctl -u np4m -f
```

#### In-app self-update

The header pill in the UI checks `raw.githubusercontent.com/.../app.py` for
the upstream `BUILD` number every 5 minutes. When upstream is ahead, the
pill turns green and reads "Update available (build N)". Click it to
update in place — NP4M streams the steps into the log pane:

1. `git fetch origin --prune` (`git reset --hard origin/main` lands the new code)
2. `pip install --upgrade -r requirements.txt`
3. SIGTERM the gunicorn master so systemd respawns it with the new code

The page then polls `/api/version` for up to 60 seconds, detects the new
build, and hard-reloads itself. This only works under the systemd
deployment created by `install.sh`. Other setups (Windows, `python app.py`
dev runs, missing `Restart=always`, etc.) get a clean preflight refusal
and the pill falls back to opening the upstream repo for a manual
update — no harm done.

> **Why `Restart=always` matters:** `/api/self-update` SIGTERMs the
> gunicorn master after the pull/install steps. With `Restart=on-failure`
> systemd would treat that clean exit as "stopped on purpose" and leave
> the service down. The unit shipped by `install.sh` uses `Restart=always`
> + `StartLimitBurst=5` (rate-limited so a crash loop doesn't spin
> forever).

### Run on Windows (one command, self-contained)

No admin, no service, no persistence, nothing outside the folder you run it
in. The installer drops an embeddable Python 3.12 inside the folder, so the
box doesn't even need Python pre-installed. **To uninstall: delete the
folder.**

In a PowerShell window:

```powershell
mkdir C:\Tools\NP4M
cd C:\Tools\NP4M
iwr -useb https://raw.githubusercontent.com/script-repo/ntnx-np4m/main/install.ps1 | iex
```

(Pick whatever folder you like — `C:\Tools\NP4M` is just an example. You can
also keep it on a USB stick or under your home directory.)

What it does:

1. Downloads the **embeddable Python 3.12** distribution from python.org and
   unzips it to `./python/`. Uncomments `import site` in `python*._pth` so
   pip will work on the embeddable build.
2. Downloads `get-pip.py` and bootstraps pip into `./python/Lib/site-packages/`.
3. Downloads the repo zip from GitHub and extracts it to `./ntnx-np4m-main/`
   (no `git` required).
4. Runs `pip install -r requirements.txt waitress` against the embedded
   Python.
5. Writes `np4m.cmd` and `_run_np4m.py` at the top of the folder. The runner
   adds `./ntnx-np4m-main` to `sys.path`, imports the Flask app, and serves
   it with waitress on `127.0.0.1:5000`. The launcher pops your browser at
   that URL and starts the server.
6. Launches NP4M for you. Set `$env:NP4M_NO_START='1'` before the one-liner
   if you don't want auto-launch.

Folder layout after install:

```
C:\Tools\NP4M\
├── install.ps1            (only if you saved it; not required)
├── np4m.cmd               <-- double-click to launch
├── _run_np4m.py
├── python\                embedded Python + pip + site-packages
└── ntnx-np4m-main\        repo source
```

| Env var             | Default                                                         | Purpose                                                                |
|---------------------|-----------------------------------------------------------------|------------------------------------------------------------------------|
| `NP4M_DIR`          | current directory                                               | Override the install folder.                                           |
| `NP4M_PORT`         | `5000`                                                          | Listen port (always on `127.0.0.1`).                                   |
| `NP4M_NO_START`     | (unset)                                                         | Set to `1` to skip auto-launch after install.                          |
| `NP4M_PY_VERSION`   | `3.12.7`                                                        | Embeddable Python version pulled from python.org.                      |
| `NP4M_REPO_ZIP`     | `https://github.com/script-repo/ntnx-np4m/archive/.../main.zip` | Fork / mirror override.                                                |

Re-running the one-liner from the same folder is the upgrade path — it
re-downloads the source zip, blows away `./ntnx-np4m-main/`, and refreshes
the Python dependencies. Python itself is only downloaded the first time.

> Why embeddable Python: it's a ~12 MB unzip with no installer and no
> registry entries. The folder is fully relocatable; copy it to another
> Windows box and it runs there too. There's no system Python to clash with
> and nothing to uninstall via Control Panel.

### Manual install (any OS)

If you'd rather wire this up by hand:

```bash
git clone https://github.com/script-repo/ntnx-np4m.git
cd ntnx-np4m

# Recommended: use a virtualenv
python -m venv .venv

# Activate the venv
#   Windows PowerShell:
.\.venv\Scripts\Activate.ps1
#   Windows cmd:
#   .\.venv\Scripts\activate.bat
#   macOS / Linux:
#   source .venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

`requirements.txt` pulls:

| Package    | Purpose                                              |
|------------|------------------------------------------------------|
| `Flask`    | Web framework for the UI + REST endpoints            |
| `requests` | HTTPS client for the Nutanix v4 REST API             |
| `urllib3`  | Used for warning suppression around self-signed TLS  |
| `pyvmomi`  | VMware SDK (only required if you import from vCenter or ESXi)|

If you don't intend to import from a VMware source, `pyvmomi` is still
installed but unused. Removing it is safe; the VMware endpoints will return
a clean `503 - pyvmomi is not installed` if it's missing.

---

## Run

```bash
python app.py
```

By default the app listens on `http://127.0.0.1:5000`. Open that URL in your
browser.

### Configuration via environment variables

| Variable    | Default     | Effect                                       |
|-------------|-------------|----------------------------------------------|
| `WEB_HOST`  | `127.0.0.1` | Bind address. Set to `0.0.0.0` to expose on the LAN. |
| `WEB_PORT`  | `5000`      | TCP port.                                    |
| `WEB_DEBUG` | (unset)     | If set to `1`, runs Flask in debug mode (auto-reload, full tracebacks). Do **not** use in production. |

```powershell
# Example: bind to all interfaces on port 8080
$env:WEB_HOST = "0.0.0.0"
$env:WEB_PORT = "8080"
python app.py
```

> The bundled server is Flask's development server. For anything beyond a
> personal/lab use case, run NP4M behind a real WSGI server such as Waitress
> (`pip install waitress`, `waitress-serve --host 0.0.0.0 --port 8080 app:app`).

---

## Walkthrough

The UI is a single page with six numbered cards (seven if you use the optional
source-import card).

### Step 0 — Pick a target platform

At the top of card 1, choose **Nutanix Prism Central** or **VMware vCenter /
ESXi**. Toggling the radio re-renders cards 2-6 in place; the source-import
card (3.5) and the streaming log work for either platform. The rest of this
walkthrough is split into a Nutanix flow and a vCenter flow — pick the one
that matches your target.

## Walkthrough — Nutanix AHV target

### 1. Target Prism Central

This is the PC that manages the AHV cluster you want subnets created on.

**Auth method radio:**

- **Username + password** — provide a PC user with permission to create
  subnets on the target cluster (typically `admin` or a service account
  with the `Network Admin` role).
- **API key (Bearer)** — paste a Prism Central API key. NP4M sends it as
  `Authorization: Bearer <key>`. The "Bearer " prefix is added
  automatically if you don't include it.

Fill in the host, fill the appropriate credential fields, and click
**Connect**. NP4M always uses port `9440` for the target Prism Central
(the field is intentionally not exposed in the UI). The status pill turns
green and the log shows the connection event.

> **Generating an API key on PC** (recommended for automation):
> Settings → Identity Providers → Local Directory → pick a service account →
> "Add API Key". Save the displayed key value somewhere safe — PC will not
> show it again.

### 2. Target cluster

Once connected, the cluster dropdown auto-populates with every PE cluster
registered to that PC (the PC itself is filtered out). Clusters are labeled
with their hypervisor types so you can confirm `[AHV]`. Pick the target.

To refresh the cluster list (for example, after registering a new PE),
re-click **Connect** in step 1 — it re-runs the cluster query as part of
the reconnect.

### 3. Virtual switch

When a cluster is picked, NP4M queries the v4 networking API for virtual
switches and filters them to the cluster you chose. The default `vs0` is
listed first. Pick the VS that the new subnets should live on.

### 3.5. Import from source (optional)

This whole card is optional. Skip it if you'll be typing networks manually.

**Source type:**

- **Nutanix Prism Central** — connect to a *different* PC (or even the same
  PC you're targeting, if you want to copy subnets within the same PC) using
  basic or token auth.
- **VMware (vCenter or ESXi)** — connect with username/password. Default
  port is 443. The same code path (`pyvmomi`'s `SmartConnect`) talks to
  either a vCenter Server or a standalone ESXi host; pick whichever has the
  port-groups you want to import. SSL verification is disabled by default
  (typical lab setup); flip the `ignore_ssl` flag in code if you want to
  enforce it.

  - When you point this at **vCenter**, the inventory walks the whole
    rootFolder: every Distributed Virtual Switch (DVS) and every standard
    vSwitch on every connected ESXi host shows up in one table.
  - When you point this at **a standalone ESXi host**, only that host's
    **standard vSwitches and port-groups** are listed. DVS objects are
    managed by vCenter and are not exposed via a direct ESXi
    connection — that's a VMware constraint, not an NP4M one. If you need
    DVS port-groups, point at the vCenter that owns them.

  Local ESXi credentials work (typically `root` + the host password, or a
  user with at least the `Read-only` role on the host). For vCenter,
  `Read-only` on the inventory you want to browse is enough.

After **Connect & list**, the inventory table renders one row per network
with these columns:

| Column            | Notes                                              |
|-------------------|----------------------------------------------------|
| ☐                 | Checkbox. Disabled for trunk / PVLAN / VLAN-0 rows.|
| Name              | Source name. If non-AHV-legal characters are present, the sanitized name is shown directly underneath. |
| VLAN              | Numeric VLAN id, or a red `TRUNK` / `PVLAN` badge. |
| Switch            | Switch name + a kind badge (`DVS`, `vSwitch`, `AHV-VS`). |
| Cluster           | (PC source only) Source cluster name.              |
| Uplinks / teaming | `act:`, `sby:`, and `teaming:` summary if available.|
| IP / status       | Managed-subnet IP/gateway, or `in target list` badge if a same-named subnet would already exist on the target. |

**Filter** the table by typing in the search box (substring match across
name, switch, cluster, VLAN, and teaming).

Use **Select all visible** + **Clear selection** to manage which rows you
want, then click **Add selected to networks list**. Each accepted row is
appended to the textarea below as `sanitized_name,vlan`. Skipped rows are
logged with the reason (`trunk port-group`, `PVLAN spec`, `already in target
networks list`, etc.).

Click **Disconnect** when you're done with the source. Source sessions are
isolated from your target PC session, so you can connect to either side
independently.

### 4. Existing networks on target cluster

A read-only panel that shows every subnet currently on the selected
cluster, sorted by VLAN ascending then name. Columns:

| Column           | Notes                                                     |
|------------------|-----------------------------------------------------------|
| Name             | Subnet name as PC sees it.                                |
| VLAN             | `networkId` from the v4 subnet object (or `—` for overlays). |
| Type             | `VLAN` / `OVERLAY` / `EXTERNAL` etc. — straight from `subnetType`. |
| Virtual switch   | Resolved by joining `virtualSwitchReference` against the VS list — falls back to a short extId if PC doesn't return a name. |
| Advanced         | `yes` / `no` from `isAdvancedNetworking`.                 |
| IP / description | First entry's `ipv4` block formatted as `<ip>/<prefix> gw <gateway>` if the subnet is managed; otherwise the subnet's description, or `—`. |

The panel auto-refreshes:

- when you pick a cluster in step 2 (so you can see what's already there
  before typing into the create list, and avoid duplicates by sight); and
- at the tail of every **Create networks** run (so successful creates pop
  in immediately and you can verify them without leaving the page).

There is also a **Refresh** button for ad-hoc reloads. The endpoint
underneath is `POST /api/target-subnets`. Like the rest of NP4M, it caps
at 100 results and surfaces a `(truncated at 100)` tail in the panel
header if the cluster has more — extend `_pc_paginated_get` callers in
`app.py` if you need full pagination.

### 5. Networks to create

The textarea accepts one network per line. Format options (mix and match):

```
network_VLAN_1001,1001
network_VLAN_1002 1002
network_VLAN_1003	1003

# Lines starting with '#' are ignored.
```

Whitespace, comma, and tab are all valid separators. The line directly under
the textarea live-validates and tells you how many entries are accepted.
Each subnet will be created **unmanaged** (no IPAM, no DHCP — pure VLAN-
backed L2).

NP4M sends `"isAdvancedNetworking": true` by default so the resulting
subnets can later participate in Flow Network Security / VPC features
without a one-shot migration. If the target cluster rejects the advanced
flag (e.g. Flow Network Security is not licensed or enabled), NP4M
**logs the specific PC error in amber, then transparently retries the
same subnet with `"isAdvancedNetworking": false`**. The streaming summary
counts these as successes and notes how many fell back.

Constraints:

- VLAN must be an integer in `0..4094`.
- Names must be unique within the textarea (case-insensitive).
- Names are checked against the target cluster's existing subnets via a
  pre-flight `GET /subnets`; conflicts are skipped at create time with a
  log line.

### 6. Create networks

The button enables only when all of {connected, target cluster, virtual
switch, ≥1 valid network} are present.

Click it and the log streams in real-time:

```
[10:00:01] Starting creation of 3 subnet(s) on cluster ...
[10:00:01] Virtual switch: ...
[10:00:01] Cluster currently has 13 subnet(s)
[10:00:01] Creating 'network_VLAN_2099' (VLAN 2099) with isAdvancedNetworking=true...
[10:00:01]   task ZXJnb24=:09fd... -- waiting...
[10:00:04]   OK: 'network_VLAN_2099' created [isAdvancedNetworking=true].
[10:00:04] Done. 1 succeeded, 0 failed (of 1).
```

If the target cluster doesn't support / license the advanced flag, the
log shows the fallback path explicitly:

```
[10:00:05] Creating 'network_VLAN_2100' (VLAN 2100) with isAdvancedNetworking=true...
[10:00:05]   'network_VLAN_2100' could not be created with isAdvancedNetworking=true.
[10:00:05]     reason: HTTP 400: Advanced networking is not enabled on this cluster
[10:00:05]   Retrying 'network_VLAN_2100' with isAdvancedNetworking=false...
[10:00:05]   task ZXJnb24=:0a01... -- waiting...
[10:00:08]   OK: 'network_VLAN_2100' created [isAdvancedNetworking=false].
[10:00:08] Done. 1 succeeded (1 via isAdvancedNetworking=false fallback), 0 failed (of 1).
```

Each create is async on PC; NP4M polls the v4 task endpoint
(`/api/prism/v4.0/config/tasks/{extId}`) until it reaches `SUCCEEDED` or a
terminal error state.

---

## Walkthrough — VMware vSphere target

When the platform toggle is set to **VMware vCenter / ESXi**, cards 1-6 host
the vCenter flow.

### 1. Target vCenter / ESXi

Provide host, port (default 443), username, and password. NP4M uses
`pyvmomi`'s `SmartConnect`, which accepts either a vCenter Server or a
standalone ESXi host. vCenter does not support Bearer-style API keys, so this
step is username/password only. TLS verification is disabled by default — see
the security notes below.

> If you connect to a **standalone ESXi host**, only that host's **Standard
> vSwitches** are visible. DVS objects are vCenter-managed and are not
> exposed via a direct ESXi connection.

### 2. Target switch type

Pick:

- **Distributed (VDS)** — port groups are created at switch level on a
  vCenter-managed DVS and propagate to every member host in one task.
- **Standard (VSS)** — port groups are created per host. You'll pick which
  hosts get them in step 3.

### 3. Destination switch (+ hosts for VSS)

The dropdown lists every switch of the chosen type visible in this vCenter.
For VSS, a checklist of hosts that already have that vSwitch is shown
underneath (with **Select all** / **Clear** shortcuts). NP4M will create the
port group on every checked host.

### 4. Existing port groups on destination switch

A read-only panel that lists every port group currently on the selected
switch:

| Column         | Notes                                                            |
|----------------|------------------------------------------------------------------|
| Name           | Port group name.                                                 |
| VLAN           | Numeric VLAN id, or a `TRUNK` / `PVLAN` badge.                   |
| Switch         | Switch kind badge (`DVS` / `vSwitch`) + switch name.             |
| Host coverage  | For VSS: list of hosts that already have this port group on this vSwitch. For VDS: `(DVS-wide)`. |

The panel auto-refreshes when you change the switch or host selection, and
again after every Create run, so successful creates pop in immediately.
Underneath: `POST /api/target/vcenter/portgroups`.

### 5. Networks to create

Same textarea as the Nutanix flow — one `name,vlan` per line. The same VLAN
range (0..4094) and duplicate-name rules apply. For VMware targets the lines
become VDS port group names or VSS port group names, depending on what you
picked in step 2.

### 6. Create port groups

The button label switches to **Create port groups** in vCenter mode and
enables once you have {connected, switch picked, ≥1 host for VSS, ≥1 valid
network}.

**VDS path:** NP4M builds one `DVPortgroupConfigSpec` per requested name and
submits them in a single `AddDVPortgroup_Task` call. The task is polled to
`success` or `error`. Default port group type is `earlyBinding` and default
`numPorts` is 8 (configurable in the request body if you call the endpoint
directly).

**VSS path:** for each (host x port group), NP4M calls
`HostNetworkSystem.AddPortGroup` with a `HostPortGroupSpec`. Pre-existing
port groups on a host raise `vim.fault.AlreadyExists`, which is reported as
an amber "already exists" line and the run continues.

Sample log:

```
[10:00:01] Submitting 4 port group(s) to VDS 'DSwitch'.
[10:00:01] DVS 'DSwitch' currently has 7 port group(s).
[10:00:01] Queueing dvportgroup 'VMW_VLAN_4001' (VLAN 4001, type=earlyBinding, ports=8).
[10:00:01] Queueing dvportgroup 'VMW_VLAN_4002' (VLAN 4002, type=earlyBinding, ports=8).
[10:00:01] Submitting AddDVPortgroup_Task with 4 spec(s)...
[10:00:04]   OK: dvportgroup 'VMW_VLAN_4001' created.
[10:00:04]   OK: dvportgroup 'VMW_VLAN_4002' created.
[10:00:04] Done. 4 succeeded, 0 failed.
```

Constraints / notes:

- VLAN must be an integer in `0..4094` (VSS treats `4095` as VGT/trunk; NP4M
  does not create VGT port groups from the UI).
- For VSS, when a port group with the same name already exists on a host on
  the same vSwitch, that host is skipped (amber log line) and others
  continue.
- For VDS, if any spec in the batch is rejected, the whole task fails — read
  the localized error in the log, fix the spec, and re-run only the
  remaining names.

---

## Optional: CLI mode

If you'd rather drive this from a script (CI, lab provisioning, etc.) without
the web UI, `create_subnets.py` is a standalone helper for the most common
case: a contiguous range of VLAN-numbered subnets on a single cluster.

```bash
# env-var auth
$env:PC_HOST = "10.0.0.10"
$env:PC_USERNAME = "admin"
$env:PC_PASSWORD = "<password>"

python create_subnets.py --cluster-name MyCluster --vlan-start 1001 --vlan-end 1010

# or by UUID, with a custom prefix:
python create_subnets.py \
    --cluster-uuid 0005f1ab-1234-5678-9abc-def012345678 \
    --name-prefix net_VLAN_ --vlan-start 2000 --vlan-end 2009
```

`python create_subnets.py --help` lists every flag. Same v4 API,
same async task polling, same `NTNX-Request-Id` idempotency header.

> The CLI currently supports basic-auth only. The web app is the
> recommended surface if you need API-key auth.

---

## REST surface (in case you want to integrate)

All endpoints use JSON.

| Method | Path                                  | Purpose                                  |
|--------|---------------------------------------|------------------------------------------|
| GET    | `/`                                   | The web UI                               |
| GET    | `/api/version`                        | `{version, build}` of the running app    |
| POST   | `/api/connect`                        | Connect to target PC, returns `{token}`  |
| POST   | `/api/clusters`                       | List clusters reachable via target token |
| POST   | `/api/virtual-switches`               | List VS, optionally filtered by cluster  |
| POST   | `/api/target-subnets`                 | Existing subnets on a target cluster     |
| POST   | `/api/create`                         | Bulk-create AHV subnets (NDJSON stream)  |
| POST   | `/api/source/pc/connect`              | Connect to a *source* PC                 |
| POST   | `/api/source/pc/inventory`            | Source PC inventory rows                 |
| POST   | `/api/source/vcenter/connect`         | Connect to a *source* vCenter / ESXi     |
| POST   | `/api/source/vcenter/inventory`       | Source vCenter / ESXi inventory rows     |
| POST   | `/api/target/vcenter/connect`         | Connect to a *target* vCenter / ESXi     |
| POST   | `/api/target/vcenter/switches`        | List VDS or VSS switches on the target   |
| POST   | `/api/target/vcenter/hosts`           | List hosts (optionally filtered by VSS)  |
| POST   | `/api/target/vcenter/portgroups`      | Existing port groups on the target switch|
| POST   | `/api/target/vcenter/create`          | Bulk-create vSphere port groups (NDJSON) |

Auth bodies (target PC):

```json
// basic
{"host": "...", "auth_mode": "basic", "username": "admin", "password": "..."}

// token
{"host": "...", "auth_mode": "token", "api_key": "..."}
```

Sessions are kept in memory keyed by an opaque token returned by the
`connect` endpoints. Tokens last for one hour or until process restart.
Nutanix target, source, and vCenter target sessions all live in separate
namespaces (`SESSIONS`, `SOURCE_SESSIONS`, `TARGET_VCENTER_SESSIONS`) so they
never collide.

`POST /api/target/vcenter/create` accepts:

```json
{
  "target_token": "...",
  "switch_kind": "vds",
  "switch_name": "DSwitch",
  "hosts": ["esx-01.example.com"],
  "networks": [{"name": "VMW_VLAN_4001", "vlan": 4001}],
  "pg_type": "earlyBinding",
  "num_ports": 8
}
```

`hosts` is only used for `switch_kind: "vss"`. `pg_type` and `num_ports` are
only used for `switch_kind: "vds"` (defaults: `earlyBinding`, `8`).

---

## Security notes

- TLS verification against PC, vCenter, and ESXi is **disabled by default**,
  since all three commonly run with self-signed certificates. Run NP4M only
  on a trusted network. If you need verification, search `verify=False` in
  `app.py` and `_ssl.CERT_NONE` for the VMware path and flip them.
- Passwords and API keys are kept in the running Flask process's memory.
  They are never written to disk, never logged, and never sent back to the
  browser. They evaporate when the process exits.
- The bundled server is Flask's development server. Put a real WSGI server
  in front of it (Waitress, Gunicorn behind nginx, etc.) for anything
  multi-user.
- Don't commit credentials. The included `.gitignore` blocks common patterns
  (`.env`, `*.pem`, `secrets.json`, `credentials.json`) for safety.

---

## Troubleshooting

**"invalid credentials" or "invalid API key" on connect**
The target PC rejected the auth header. PC sometimes wraps this as a 5xx
response; NP4M normalizes those to a 401 here. For API keys, double-check
that you copied the full key and that the underlying user has the v4 API
permissions.

**Cluster dropdown shows nothing**
The user/key has no permission to list clusters, or the only entity returned
was the PC itself (which NP4M filters out). Try a more privileged account.

**"PC returned HTTP 400 ... `$limit` ... maximum 100"**
NP4M caps page size at 100 (PC's hard maximum for v4). If you somehow have
more than 100 clusters or virtual switches, the listing endpoints will
truncate. Add pagination in `app.py`'s `_pc_paginated_get` callers or open
an issue.

**"a subnet with that name already exists on this cluster"**
That's the duplicate-name pre-flight check protecting you. AHV doesn't
enforce unique subnet names; if you really want a duplicate, rename it
slightly or delete the existing one first.

**VMware import returns 503 "pyvmomi is not installed"**
Run `python -m pip install -r requirements.txt` (the venv where you start
`app.py` must have `pyvmomi`).

**ESXi connection succeeds but no DVS port-groups appear**
This is expected. Distributed Virtual Switches are vCenter-managed objects
and are not visible from a direct ESXi host connection — only the host's
standard vSwitches and port-groups are. Point NP4M at the vCenter that owns
the DVS if you need those.

**Teaming / uplink columns are blank**
Some vCenter / ESXi versions expose teaming under different attribute
paths. Open an issue with your version (`vmware -v` on the host, or
About → Build for vCenter) and a sanitized sample of the row JSON.

**Streaming log freezes mid-create**
NP4M polls each create task every 2s for up to 120s. If a PC task takes
longer, raise `timeout_seconds` in `_wait_for_task` (or `--task-timeout` for
the CLI).

---

## Limitations

### Nutanix target
- **Unmanaged subnets only** by design. NP4M does not currently set IPAM /
  DHCP / IP pools. If you need managed subnets, edit them in PC after
  creation, or extend `_create_unmanaged_subnet` to send `ipConfig`.
- **No overlay / VPC subnets**. Subnet type is hardcoded to `VLAN`.
- **Single virtual switch per batch**. The whole networks list goes onto the
  one VS you picked.
- **CLI does not support API-key auth** (web UI does).

### VMware target
- **VLAN port groups only**. NP4M sends a `VlanIdSpec`; trunk and PVLAN port
  groups are not created from the UI.
- **VDS defaults**: `type=earlyBinding`, `numPorts=8`. Override via the JSON
  body of `/api/target/vcenter/create` if you need different values.
- **VSS is per-host**: NP4M iterates the host list you selected and creates
  the port group on each. Same-named port groups on a host are reported and
  skipped; others continue.
- **No edit / delete from the UI**. NP4M is a creator; manage existing port
  groups in vCenter / Host Client.
- **API-key auth is not supported for vCenter** — vCenter's `SmartConnect`
  only accepts SSO username/password.
- **VMware path live-tested only against a small lab**. Standard switches,
  DVS port-groups, PVLAN/Trunk decoding, and teaming policies are all
  coded but YMMV across vCenter / ESXi versions.
- **Standalone ESXi connections only see standard vSwitches**, never DVS
  port-groups (DVS is vCenter-managed). This is a VMware constraint.

---

## License

Released under the [MIT License](LICENSE). See the `LICENSE` file for the
full text.
