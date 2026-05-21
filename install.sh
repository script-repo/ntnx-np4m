#!/usr/bin/env bash
# NP4M one-shot installer for Linux. Distro-agnostic.
#
# Supported families:
#   * Debian / Ubuntu / Mint / Pop!_OS              (apt)
#   * RHEL / Rocky / Alma / Oracle / Fedora /
#     CentOS Stream / Amazon Linux                  (dnf, yum fallback)
#   * openSUSE Leap / Tumbleweed / SLES             (zypper)
#   * Arch / Manjaro / EndeavourOS                  (pacman)
#   * Alpine                                        (apk; no systemd, prints
#                                                    manual start instructions)
#
# Idempotent: safe to re-run; will git-pull, refresh the venv, and bounce the
# service.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/script-repo/ntnx-np4m/main/install.sh | sudo bash
#
# Env overrides (pass via 'sudo env VAR=value bash'):
#   NP4M_BIND       Bind address.        Default: 0.0.0.0
#                   (Loopback => plain HTTP. Anything else => self-signed HTTPS.)
#   NP4M_PORT       Listen port.         Default: 8443 (HTTPS) or 8080 (HTTP)
#   NP4M_USER       Service user.        Default: np4m
#   NP4M_HOME       Service user home.   Default: /home/<user>
#   NP4M_DIR        Install dir.         Default: <home>/ntnx-np4m
#   NP4M_REPO_URL   Repo to clone.       Default: https://github.com/script-repo/ntnx-np4m.git
#   NP4M_PY         Python binary.       Default: auto-detect highest 3.10+
#   NP4M_TLS_CERT   Path to TLS cert.    Default: self-signed at <dir>/tls/np4m.crt
#   NP4M_TLS_KEY    Path to TLS key.     Default: self-signed at <dir>/tls/np4m.key
#   NP4M_OPEN_FW    Open firewall port.  Default: yes (firewalld or ufw)
#   NP4M_SYSTEMD    Install systemd unit. Default: yes (auto-skipped if no systemd)

set -euo pipefail

NP4M_REPO_URL="${NP4M_REPO_URL:-https://github.com/script-repo/ntnx-np4m.git}"
NP4M_USER="${NP4M_USER:-np4m}"
NP4M_HOME="${NP4M_HOME:-/home/${NP4M_USER}}"
NP4M_DIR="${NP4M_DIR:-${NP4M_HOME}/ntnx-np4m}"
NP4M_BIND="${NP4M_BIND:-0.0.0.0}"
NP4M_OPEN_FW="${NP4M_OPEN_FW:-yes}"
NP4M_SYSTEMD="${NP4M_SYSTEMD:-yes}"

# TLS is on whenever we're not on a loopback address.
if [[ "$NP4M_BIND" == "127.0.0.1" || "$NP4M_BIND" == "::1" || "$NP4M_BIND" == "localhost" ]]; then
  NP4M_TLS="no"
  NP4M_PORT="${NP4M_PORT:-8080}"
else
  NP4M_TLS="yes"
  NP4M_PORT="${NP4M_PORT:-8443}"
fi

NP4M_TLS_CERT="${NP4M_TLS_CERT:-${NP4M_DIR}/tls/np4m.crt}"
NP4M_TLS_KEY="${NP4M_TLS_KEY:-${NP4M_DIR}/tls/np4m.key}"

log() { printf "\033[1;34m[np4m]\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m[np4m]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[np4m]\033[0m %s\n" "$*" >&2; }

if [[ $EUID -ne 0 ]]; then
  err "Must run as root. Example:"
  err "  curl -fsSL .../install.sh | sudo bash"
  exit 1
fi

# --- 1. Distro family detection -------------------------------------------
ID="" ; ID_LIKE="" ; PRETTY_NAME=""
if [[ -f /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
fi

FAMILY="unknown"
case "${ID:-}" in
  debian|ubuntu|linuxmint|pop|elementary|kali|raspbian|neon)         FAMILY="debian" ;;
  rhel|centos|rocky|almalinux|ol|fedora|amzn)                        FAMILY="rhel"   ;;
  opensuse|opensuse-leap|opensuse-tumbleweed|sles|suse|sled)         FAMILY="suse"   ;;
  arch|manjaro|endeavouros|garuda|cachyos)                           FAMILY="arch"   ;;
  alpine)                                                            FAMILY="alpine" ;;
esac
if [[ "$FAMILY" == "unknown" ]]; then
  # Fall back to ID_LIKE for derivatives we don't list above.
  case " ${ID_LIKE:-} " in
    *" debian "*|*" ubuntu "*)              FAMILY="debian" ;;
    *" rhel "*|*" fedora "*|*" centos "*)   FAMILY="rhel"   ;;
    *" suse "*|*" opensuse "*)              FAMILY="suse"   ;;
    *" arch "*)                             FAMILY="arch"   ;;
  esac
fi

log "Detected: ${PRETTY_NAME:-${ID:-unknown}}  (family=${FAMILY})"

if [[ "$FAMILY" == "unknown" ]]; then
  err "Could not detect a supported package manager for this distro."
  err "Manually install: python3.10+ (with venv), git, openssl, then re-run with"
  err "  NP4M_PY=/path/to/python3 sudo -E bash install.sh"
  exit 1
fi

# --- 2. Package-manager dispatch ------------------------------------------
PKG_REFRESHED="no"
pkg_refresh() {
  [[ "$PKG_REFRESHED" == "yes" ]] && return 0
  case "$FAMILY" in
    debian) DEBIAN_FRONTEND=noninteractive apt-get update -y ;;
    rhel)   : ;;  # dnf/yum refresh metadata implicitly on each install
    suse)   zypper --non-interactive refresh ;;
    arch)   pacman -Sy --noconfirm >/dev/null ;;
    alpine) apk update ;;
  esac
  PKG_REFRESHED="yes"
}

pkg_install() {
  pkg_refresh
  case "$FAMILY" in
    debian) DEBIAN_FRONTEND=noninteractive apt-get install -y "$@" ;;
    rhel)
      if command -v dnf >/dev/null 2>&1; then dnf -y install "$@"
      else yum -y install "$@"; fi ;;
    suse)   zypper --non-interactive install --no-confirm --auto-agree-with-licenses "$@" ;;
    arch)   pacman -S --noconfirm --needed "$@" ;;
    alpine) apk add --no-cache "$@" ;;
  esac
}

# Try-install: returns non-zero instead of failing the script.
pkg_install_try() {
  if pkg_install "$@" >/dev/null 2>&1; then return 0; else return 1; fi
}

# --- 3. UBI repo (RHEL only, unsubscribed boxes) --------------------------
if [[ "${ID:-}" == "rhel" ]] && [[ ! -f /etc/yum.repos.d/ubi.repo ]]; then
  if ! command -v subscription-manager >/dev/null 2>&1 \
     || ! subscription-manager status >/dev/null 2>&1; then
    case "${VERSION_ID%%.*}" in
      9)
        log "Unsubscribed RHEL 9 detected. Adding UBI 9 repos..."
        cat > /etc/yum.repos.d/ubi.repo <<'EOF'
[ubi-9-baseos]
name=Red Hat UBI 9 BaseOS
baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/baseos/os/
enabled=1
gpgcheck=1
gpgkey=https://access.redhat.com/security/data/fd431d51.txt

[ubi-9-appstream]
name=Red Hat UBI 9 AppStream
baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/appstream/os/
enabled=1
gpgcheck=1
gpgkey=https://access.redhat.com/security/data/fd431d51.txt

[ubi-9-codeready-builder]
name=Red Hat UBI 9 CodeReady Builder
baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi9/9/x86_64/codeready-builder/os/
enabled=1
gpgcheck=1
gpgkey=https://access.redhat.com/security/data/fd431d51.txt
EOF
        ;;
      8)
        log "Unsubscribed RHEL 8 detected. Adding UBI 8 repos..."
        cat > /etc/yum.repos.d/ubi.repo <<'EOF'
[ubi-8-baseos]
name=Red Hat UBI 8 BaseOS
baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi8/8/x86_64/baseos/os/
enabled=1
gpgcheck=1
gpgkey=https://access.redhat.com/security/data/fd431d51.txt

[ubi-8-appstream]
name=Red Hat UBI 8 AppStream
baseurl=https://cdn-ubi.redhat.com/content/public/ubi/dist/ubi8/8/x86_64/appstream/os/
enabled=1
gpgcheck=1
gpgkey=https://access.redhat.com/security/data/fd431d51.txt
EOF
        ;;
    esac
  fi
fi

# --- 4. Find or install Python 3.10+ with venv ----------------------------
python_ok() {
  local bin="$1"
  command -v "$bin" >/dev/null 2>&1 || return 1
  "$bin" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null || return 1
  "$bin" -c 'import venv' 2>/dev/null || return 1
  return 0
}

find_python() {
  if [[ -n "${NP4M_PY:-}" ]] && python_ok "$NP4M_PY"; then
    echo "$NP4M_PY"; return 0
  fi
  local cand
  for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if python_ok "$cand"; then echo "$cand"; return 0; fi
  done
  return 1
}

install_python() {
  log "Installing Python (3.10+) via the system package manager..."
  case "$FAMILY" in
    debian)
      # Pick the newest python3.X that the distro ships; fall back to python3.
      if pkg_install_try python3.12 python3.12-venv python3-pip; then :
      elif pkg_install_try python3.11 python3.11-venv python3-pip; then :
      else pkg_install python3 python3-venv python3-pip
      fi
      ;;
    rhel)
      if pkg_install_try python3.12 python3.12-pip; then :
      elif pkg_install_try python3.11 python3.11-pip; then :
      else pkg_install python3 python3-pip
      fi
      ;;
    suse)
      if pkg_install_try python312 python312-pip; then :
      elif pkg_install_try python311 python311-pip; then :
      else pkg_install python3 python3-pip
      fi
      ;;
    arch)
      pkg_install python python-pip
      ;;
    alpine)
      pkg_install python3 py3-pip
      ;;
  esac
}

if ! PY=$(find_python); then
  install_python
  if ! PY=$(find_python); then
    err "Python 3.10+ with venv module not available after install."
    err "On Debian/Ubuntu try: apt-get install -y python3-venv"
    err "Set NP4M_PY=/path/to/python3 and re-run."
    exit 1
  fi
fi
log "Using Python: ${PY} ($($PY --version 2>&1))"

# --- 5. git + openssl ------------------------------------------------------
NEED_TOOLS=()
command -v git     >/dev/null 2>&1 || NEED_TOOLS+=(git)
command -v openssl >/dev/null 2>&1 || NEED_TOOLS+=(openssl)
if [[ ${#NEED_TOOLS[@]} -gt 0 ]]; then
  log "Installing tools: ${NEED_TOOLS[*]}"
  pkg_install "${NEED_TOOLS[@]}"
fi

# --- 6. Service user -------------------------------------------------------
if ! id "$NP4M_USER" &>/dev/null; then
  log "Creating service user '${NP4M_USER}'..."
  if command -v useradd >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash --home-dir "$NP4M_HOME" "$NP4M_USER"
  elif command -v adduser >/dev/null 2>&1; then
    # Alpine / BusyBox adduser
    adduser -S -D -h "$NP4M_HOME" -s /bin/sh "$NP4M_USER"
  else
    err "Neither useradd nor adduser available; cannot create service user."
    exit 1
  fi
fi
# Make sure $NP4M_HOME exists (adduser -D doesn't always create it).
[[ -d "$NP4M_HOME" ]] || { mkdir -p "$NP4M_HOME" && chown "$NP4M_USER:" "$NP4M_HOME"; }

# --- 7. Clone / update repo ------------------------------------------------
if [[ -d "$NP4M_DIR/.git" ]]; then
  log "Repo already present at ${NP4M_DIR}, pulling latest..."
  sudo -u "$NP4M_USER" git -C "$NP4M_DIR" pull --ff-only
else
  log "Cloning ${NP4M_REPO_URL} -> ${NP4M_DIR}"
  sudo -u "$NP4M_USER" git clone "$NP4M_REPO_URL" "$NP4M_DIR"
fi

# --- 8. Venv + Python deps -------------------------------------------------
log "Creating/refreshing virtualenv with ${PY}..."
sudo -u "$NP4M_USER" env "PY=$PY" bash -lc '
  set -euo pipefail
  cd '"'$NP4M_DIR'"'
  "$PY" -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip
  ./.venv/bin/python -m pip install -r requirements.txt
  ./.venv/bin/python -m pip install gunicorn
'

# --- 9. TLS cert (self-signed, only if HTTPS branch and no BYO cert) ------
TLS_ARGS=""
if [[ "$NP4M_TLS" == "yes" ]]; then
  if [[ -f "$NP4M_TLS_CERT" && -f "$NP4M_TLS_KEY" ]]; then
    log "Using existing TLS cert/key at ${NP4M_TLS_CERT} / ${NP4M_TLS_KEY}"
  else
    log "Generating self-signed TLS cert at ${NP4M_TLS_CERT}..."
    sudo -u "$NP4M_USER" mkdir -p "$(dirname "$NP4M_TLS_CERT")"
    HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
    HOST_SHORT="$(hostname)"
    PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || true)"
    SAN="DNS:${HOST_FQDN},DNS:${HOST_SHORT}"
    [[ -n "${PRIMARY_IP:-}" ]] && SAN="${SAN},IP:${PRIMARY_IP}"
    sudo -u "$NP4M_USER" openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout "$NP4M_TLS_KEY" \
      -out "$NP4M_TLS_CERT" \
      -subj "/CN=${HOST_FQDN}" \
      -addext "subjectAltName=${SAN}" >/dev/null 2>&1
    chmod 600 "$NP4M_TLS_KEY"
    chown "$NP4M_USER:$NP4M_USER" "$NP4M_TLS_CERT" "$NP4M_TLS_KEY"
  fi
  TLS_ARGS="--certfile=${NP4M_TLS_CERT} --keyfile=${NP4M_TLS_KEY}"
fi

# --- 10. Firewall ----------------------------------------------------------
open_firewall() {
  local port="$1"
  if command -v firewall-cmd >/dev/null 2>&1 \
     && systemctl is-active --quiet firewalld 2>/dev/null; then
    log "Opening firewall port ${port}/tcp (firewalld)..."
    firewall-cmd --permanent --add-port="${port}/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
    return 0
  fi
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi 'Status: active'; then
    log "Opening firewall port ${port}/tcp (ufw)..."
    ufw allow "${port}/tcp" >/dev/null
    return 0
  fi
  log "No active firewalld/ufw detected; skipping firewall step."
}
if [[ "$NP4M_OPEN_FW" == "yes" && "$NP4M_TLS" == "yes" ]]; then
  open_firewall "$NP4M_PORT"
fi

# --- 11. systemd unit (or manual-start fallback) --------------------------
HAS_SYSTEMD="no"
if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
  HAS_SYSTEMD="yes"
fi

if [[ "$NP4M_SYSTEMD" == "yes" && "$HAS_SYSTEMD" == "yes" ]]; then
  log "Installing systemd unit /etc/systemd/system/np4m.service..."
  cat > /etc/systemd/system/np4m.service <<EOF
[Unit]
Description=NP4M
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${NP4M_USER}
WorkingDirectory=${NP4M_DIR}
Environment=WEB_HOST=${NP4M_BIND}
Environment=WEB_PORT=${NP4M_PORT}
ExecStart=${NP4M_DIR}/.venv/bin/gunicorn --workers 2 --bind ${NP4M_BIND}:${NP4M_PORT} ${TLS_ARGS} app:app
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable np4m >/dev/null 2>&1 || true
  systemctl restart np4m
elif [[ "$NP4M_SYSTEMD" == "yes" && "$HAS_SYSTEMD" == "no" ]]; then
  warn "systemd not detected on this distro. NP4M was installed but no service was registered."
  warn "Start it manually with:"
  warn "  sudo -u ${NP4M_USER} ${NP4M_DIR}/.venv/bin/gunicorn --workers 2 \\"
  warn "    --bind ${NP4M_BIND}:${NP4M_PORT} ${TLS_ARGS} app:app"
fi

# --- 12. Summary -----------------------------------------------------------
sleep 1
SCHEME="http"
CURL_ARGS=""
if [[ "$NP4M_TLS" == "yes" ]]; then
  SCHEME="https"
  CURL_ARGS="-k"
fi
URL="${SCHEME}://${NP4M_BIND}:${NP4M_PORT}/"
PROBE_URL="${SCHEME}://127.0.0.1:${NP4M_PORT}/api/version"
log ""
log "NP4M installed."
log "  URL:     ${URL}"
if [[ "$HAS_SYSTEMD" == "yes" && "$NP4M_SYSTEMD" == "yes" ]]; then
  ver="$(curl ${CURL_ARGS} -fsS "${PROBE_URL}" 2>/dev/null || echo '(probe failed)')"
  log "  Version: ${ver}"
  log "  Logs:    journalctl -u np4m -f"
  log "  Restart: systemctl restart np4m"
  log "  Stop:    systemctl stop np4m"
fi
if [[ "$NP4M_TLS" == "yes" && "$NP4M_TLS_CERT" == "${NP4M_DIR}/tls/np4m.crt" ]]; then
  log ""
  log "Note: served with a self-signed certificate. Your browser will warn on"
  log "first visit. Set NP4M_TLS_CERT / NP4M_TLS_KEY to use your own cert."
fi
