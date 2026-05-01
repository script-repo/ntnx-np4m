# NP4M

A small Flask web app for **bulk-creating unmanaged AHV subnets** on Nutanix
via the Prism Central v4 REST APIs, with optional **import of existing
networks** from another Prism Central, a VMware vCenter, or a standalone
VMware ESXi host.

NP4M was built for migration-style workflows where you need to recreate dozens
of VLAN-backed L2 networks on a target AHV cluster in one shot, mirroring what
already exists somewhere else.

---

## Features

- Connect to a target Prism Central via **basic auth** (`admin` + password) **or
  an API key** (`Authorization: Bearer <token>`).
- Pick a target AHV cluster (PE) registered to that PC.
- Pick a virtual switch on that cluster (filtered automatically).
- (Optional) Connect to a **source PC, vCenter, or standalone ESXi host**
  and browse its existing subnets / port-groups in a sortable, filterable
  table:
  - Nutanix source: clusters, virtual switches, subnets, VLAN, managed/IP
    config, per-host uplinks.
  - VMware source: port-group name, VLAN id / trunk / PVLAN, switch name,
    active/standby uplinks, teaming policy, failback.
    - Connecting to **vCenter** shows both Distributed Virtual Switches
      (DVS) and per-host standard vSwitches.
    - Connecting directly to **a single ESXi host** shows that host's
      standard vSwitches and port-groups (DVS objects are vCenter-managed
      and aren't visible from a standalone ESXi connection).
- Select rows + click "Add selected" to populate the create-list with their
  `name,vlan` pairs. Names are sanitized to AHV-legal characters; trunks /
  PVLANs / out-of-range VLANs are flagged and never auto-imported.
- Paste/edit additional networks freehand.
- Click **Create networks** and watch a streaming color-coded log: each
  subnet POST is followed by task polling until `SUCCEEDED` / `FAILED`.
- Pre-flight **duplicate-name check** against the target cluster (AHV doesn't
  enforce unique subnet names, so NP4M does it for you).

A standalone CLI helper (`create_subnets.py`) is included for scripted /
non-UI provisioning.

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

The UI is a single page with five (or six, if you import) numbered cards.
Each card unlocks the next one once it's set.

### 1. Target Prism Central

This is the PC that manages the AHV cluster you want subnets created on.

**Auth method radio:**

- **Username + password** — provide a PC user with permission to create
  subnets on the target cluster (typically `admin` or a service account
  with the `Network Admin` role).
- **API key (Bearer)** — paste a Prism Central API key. NP4M sends it as
  `Authorization: Bearer <key>`. The "Bearer " prefix is added
  automatically if you don't include it.

Fill in the host, leave the port at `9440`, fill the appropriate credential
fields, and click **Connect**. The status pill turns green and the log shows
the connection event.

> **Generating an API key on PC** (recommended for automation):
> Settings → Identity Providers → Local Directory → pick a service account →
> "Add API Key". Save the displayed key value somewhere safe — PC will not
> show it again.

### 2. Target cluster

Once connected, the cluster dropdown auto-populates with every PE cluster
registered to that PC (the PC itself is filtered out). Clusters are labeled
with their hypervisor types so you can confirm `[AHV]`. Pick the target.

Click **Refresh** if the cluster list was modified since you connected.

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

### 4. Networks to create

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

Constraints:

- VLAN must be an integer in `0..4094`.
- Names must be unique within the textarea (case-insensitive).
- Names are checked against the target cluster's existing subnets via a
  pre-flight `GET /subnets`; conflicts are skipped at create time with a
  log line.

### 5. Create networks

The button enables only when all of {connected, target cluster, virtual
switch, ≥1 valid network} are present.

Click it and the log streams in real-time:

```
[10:00:01] Starting creation of 3 subnet(s) on cluster ...
[10:00:01] Virtual switch: ...
[10:00:01] Cluster currently has 13 subnet(s)
[10:00:01] Creating 'network_VLAN_2099' (VLAN 2099)...
[10:00:01]   task ZXJnb24=:09fd... -- waiting...
[10:00:04]   OK: 'network_VLAN_2099' created.
[10:00:04] Done. 1 succeeded, 0 failed (of 1).
```

Each create is async on PC; NP4M polls the v4 task endpoint
(`/api/prism/v4.0/config/tasks/{extId}`) until it reaches `SUCCEEDED` or a
terminal error state.

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

| Method | Path                                | Purpose                                    |
|--------|-------------------------------------|--------------------------------------------|
| GET    | `/`                                 | The web UI                                 |
| POST   | `/api/connect`                      | Connect to target PC, returns `{token}`    |
| POST   | `/api/clusters`                     | List clusters reachable via target token   |
| POST   | `/api/virtual-switches`             | List VS, optionally filtered by cluster    |
| POST   | `/api/create`                       | Bulk create (NDJSON streaming response)    |
| POST   | `/api/source/pc/connect`            | Connect to a *source* PC                   |
| POST   | `/api/source/pc/inventory`          | Source PC inventory rows                   |
| POST   | `/api/source/vcenter/connect`       | Connect to a vCenter or ESXi host          |
| POST   | `/api/source/vcenter/inventory`     | vCenter / ESXi inventory rows              |

Auth bodies (target PC):

```json
// basic
{"host": "...", "auth_mode": "basic", "username": "admin", "password": "..."}

// token
{"host": "...", "auth_mode": "token", "api_key": "..."}
```

Sessions are kept in memory keyed by an opaque token returned by the
`connect` endpoints. Tokens last for one hour or until process restart.
Source and target sessions are in separate namespaces — they never collide.

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

- **Unmanaged subnets only** by design. NP4M does not currently set IPAM /
  DHCP / IP pools. If you need managed subnets, edit them in PC after
  creation, or extend `_create_unmanaged_subnet` to send `ipConfig`.
- **No overlay / VPC subnets**. Subnet type is hardcoded to `VLAN`.
- **Single virtual switch per batch**. The whole networks list goes onto the
  one VS you picked.
- **CLI does not support API-key auth** (web UI does).
- **VMware path live-tested only against a small lab**. Standard switches,
  DVS port-groups, PVLAN/Trunk decoding, and teaming policies are all
  coded but YMMV across vCenter / ESXi versions.
- **Standalone ESXi sources see only standard vSwitches**, never DVS
  port-groups (DVS is vCenter-managed). This is a VMware constraint.

---

## License

Released under the [MIT License](LICENSE). See the `LICENSE` file for the
full text.
