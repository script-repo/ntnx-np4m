#!/usr/bin/env bash
# NP4M one-shot installer for RHEL 9 (and Rocky 9 / Alma 9 / Oracle 9).
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
#   NP4M_PY         Python interpreter.  Default: python3.12 (falls back to python3.11)
#   NP4M_TLS_CERT   Path to TLS cert.    Default: self-signed at <dir>/tls/np4m.crt
#   NP4M_TLS_KEY   Path to TLS key.    Default: self-signed at <dir>/tls/np4m.key
#   NP4M_OPEN_FW    Open firewalld port. Default: yes
#   NP4M_SYSTEMD    Install systemd unit. Default: yes

set -euo pipefail

NP4M_REPO_URL="${NP4M_REPO_URL:-https://github.com/script-repo/ntnx-np4m.git}"
NP4M_USER="${NP4M_USER:-np4m}"
NP4M_HOME="${NP4M_HOME:-/home/${NP4M_USER}}"
NP4M_DIR="${NP4M_DIR:-${NP4M_HOME}/ntnx-np4m}"
NP4M_BIND="${NP4M_BIND:-0.0.0.0}"
NP4M_PY="${NP4M_PY:-python3.12}"
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
err() { printf "\033[1;31m[np4m]\033[0m %s\n" "$*" >&2; }

if [[ $EUID -ne 0 ]]; then
  err "Must run as root. Example:"
  err "  curl -fsSL .../install.sh | sudo bash"
  exit 1
fi

if [[ ! -f /etc/os-release ]]; then
  err "Cannot detect OS (no /etc/os-release)."
  exit 1
fi
# shellcheck disable=SC1091
. /etc/os-release
case "${ID:-}:${VERSION_ID%%.*}" in
  rhel:9|rocky:9|almalinux:9|centos:9|ol:9) : ;;
  *)
    err "Targets RHEL 9 / Rocky 9 / Alma 9 / Oracle 9. Detected: ${PRETTY_NAME:-?}"
    exit 1
    ;;
esac

# --- 1. UBI repos (so the box doesn't need a subscription) -----------------
if [[ ! -f /etc/yum.repos.d/ubi.repo ]]; then
  log "Adding UBI 9 repos..."
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
  dnf -y clean all
fi

# --- 2. System packages ----------------------------------------------------
log "Installing system packages (${NP4M_PY}, pip, git, openssl)..."
if ! dnf -y install "${NP4M_PY}" "${NP4M_PY}-pip" git openssl; then
  if [[ "$NP4M_PY" == "python3.12" ]]; then
    log "python3.12 unavailable on this minor release, trying python3.11..."
    NP4M_PY="python3.11"
    dnf -y install "${NP4M_PY}" "${NP4M_PY}-pip" git openssl
  else
    err "Failed to install Python via dnf."
    exit 1
  fi
fi

# --- 3. Service user -------------------------------------------------------
if ! id "$NP4M_USER" &>/dev/null; then
  log "Creating service user '${NP4M_USER}'..."
  useradd --system --create-home --shell /bin/bash --home-dir "$NP4M_HOME" "$NP4M_USER"
fi

# --- 4. Clone / update repo ------------------------------------------------
if [[ -d "$NP4M_DIR/.git" ]]; then
  log "Repo already present at ${NP4M_DIR}, pulling latest..."
  sudo -u "$NP4M_USER" git -C "$NP4M_DIR" pull --ff-only
else
  log "Cloning ${NP4M_REPO_URL} -> ${NP4M_DIR}"
  sudo -u "$NP4M_USER" git clone "$NP4M_REPO_URL" "$NP4M_DIR"
fi

# --- 5. Venv + Python deps -------------------------------------------------
log "Creating/refreshing virtualenv with ${NP4M_PY}..."
sudo -u "$NP4M_USER" bash -lc "
  set -euo pipefail
  cd '$NP4M_DIR'
  $NP4M_PY -m venv .venv
  ./.venv/bin/python -m pip install --upgrade pip
  ./.venv/bin/python -m pip install -r requirements.txt
  ./.venv/bin/python -m pip install gunicorn
"

# --- 6. TLS cert (self-signed, only if HTTPS branch and no BYO cert) -------
TLS_ARGS=""
if [[ "$NP4M_TLS" == "yes" ]]; then
  if [[ -f "$NP4M_TLS_CERT" && -f "$NP4M_TLS_KEY" ]]; then
    log "Using existing TLS cert/key at ${NP4M_TLS_CERT} / ${NP4M_TLS_KEY}"
  else
    log "Generating self-signed TLS cert at ${NP4M_TLS_CERT}..."
    sudo -u "$NP4M_USER" mkdir -p "$(dirname "$NP4M_TLS_CERT")"
    HOST_FQDN="$(hostname -f 2>/dev/null || hostname)"
    HOST_SHORT="$(hostname)"
    PRIMARY_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
    SAN="DNS:${HOST_FQDN},DNS:${HOST_SHORT}"
    [[ -n "$PRIMARY_IP" ]] && SAN="${SAN},IP:${PRIMARY_IP}"
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

# --- 7. Firewall -----------------------------------------------------------
if [[ "$NP4M_OPEN_FW" == "yes" && "$NP4M_TLS" == "yes" ]]; then
  if systemctl is-active --quiet firewalld; then
    log "Opening firewall port ${NP4M_PORT}/tcp..."
    firewall-cmd --permanent --add-port="${NP4M_PORT}/tcp" >/dev/null
    firewall-cmd --reload >/dev/null
  else
    log "firewalld not active, skipping firewall step."
  fi
fi

# --- 8. systemd unit -------------------------------------------------------
if [[ "$NP4M_SYSTEMD" == "yes" ]]; then
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
fi

# --- 9. Summary ------------------------------------------------------------
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
ver="$(curl ${CURL_ARGS} -fsS "${PROBE_URL}" 2>/dev/null || echo '(probe failed)')"
log "  Version: ${ver}"
log "  Logs:    journalctl -u np4m -f"
log "  Restart: systemctl restart np4m"
log "  Stop:    systemctl stop np4m"
if [[ "$NP4M_TLS" == "yes" && "$NP4M_TLS_CERT" == "${NP4M_DIR}/tls/np4m.crt" ]]; then
  log ""
  log "Note: served with a self-signed certificate. Your browser will warn on"
  log "first visit. Set NP4M_TLS_CERT / NP4M_TLS_KEY to use your own cert."
fi
