#!/usr/bin/env bash
# MeshCore TCP-BLE Proxy installer
# Usage: curl -sSL https://raw.githubusercontent.com/zindello/meshcore-tcp-ble-proxy/main/install.sh | sudo bash
set -euo pipefail

REPO_URL="https://github.com/zindello/meshcore-tcp-ble-proxy"
INSTALL_DIR="/opt/meshcore-tcp-ble-proxy"
CONFIG_DIR="/etc/meshcore-tcp-ble-proxy"
SERVICE_USER="meshcore-proxy"
SERVICE_NAME="meshcore-tcp-ble-proxy"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DBUS_POLICY="/etc/dbus-1/system.d/meshcore-proxy.conf"

# ── helpers ───────────────────────────────────────────────────────────────────

info()  { echo "  [+] $*"; }
warn()  { echo "  [!] $*" >&2; }
die()   { echo "  [✗] $*" >&2; exit 1; }

prompt() {
    local var="$1" prompt_text="$2" default="$3"
    read -r -p "      ${prompt_text} [${default}]: " "${var?}"
    : "${!var:=$default}"
}

# ── preflight ─────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Run with sudo: curl ... | sudo bash"
[[ "$(uname -s)" == "Linux" ]] || die "This installer is for Linux (Raspberry Pi OS / Debian)."

# ── system packages ───────────────────────────────────────────────────────────

info "Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    git python3 python3-venv bluetooth bluez >/dev/null

# ── service user ──────────────────────────────────────────────────────────────

info "Creating service user '${SERVICE_USER}'…"
if ! id "${SERVICE_USER}" &>/dev/null; then
    useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
usermod -aG bluetooth "${SERVICE_USER}"
info "User '${SERVICE_USER}' is a member of the 'bluetooth' group."

# ── clone / update repo ───────────────────────────────────────────────────────

if [[ -d "${INSTALL_DIR}/.git" ]]; then
    info "Updating existing installation at ${INSTALL_DIR}…"
    git -C "${INSTALL_DIR}" pull --quiet
else
    info "Cloning repository into ${INSTALL_DIR}…"
    git clone --quiet "${REPO_URL}" "${INSTALL_DIR}"
fi

# ── D-Bus policy ──────────────────────────────────────────────────────────────

info "Installing D-Bus policy…"
cp "${INSTALL_DIR}/deploy/meshcore-proxy-dbus.conf" "${DBUS_POLICY}"

# ── virtual environment ───────────────────────────────────────────────────────

info "Creating Python virtual environment…"
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${INSTALL_DIR}/venv/bin/pip" install --quiet "${INSTALL_DIR}[linux]"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/venv"

# ── configuration ─────────────────────────────────────────────────────────────

mkdir -p "${CONFIG_DIR}"

if [[ -f "${CONFIG_DIR}/config.yaml" ]]; then
    info "Config already exists at ${CONFIG_DIR}/config.yaml — skipping."
else
    echo ""
    echo "  Configure the MeshCore TCP companion connection."
    echo "  Press Enter to accept the default shown in brackets."
    echo ""
    prompt tcp_host "Companion host" "localhost"
    prompt tcp_port "Companion port" "5050"
    echo ""

    # Validate port is numeric
    [[ "${tcp_port}" =~ ^[0-9]+$ ]] || die "Port must be a number, got: ${tcp_port}"

    cat > "${CONFIG_DIR}/config.yaml" <<YAML
tcp:
  host: "${tcp_host}"
  port: ${tcp_port}
  handshake_timeout: 10.0

logging:
  debug: false
YAML
    chmod 640 "${CONFIG_DIR}/config.yaml"
    chown root:"${SERVICE_USER}" "${CONFIG_DIR}/config.yaml"
    info "Config written to ${CONFIG_DIR}/config.yaml"
fi

# ── systemd service ───────────────────────────────────────────────────────────

info "Installing systemd service…"
cp "${INSTALL_DIR}/deploy/meshcore-tcp-ble-proxy.service" "${SERVICE_FILE}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

# ── done ──────────────────────────────────────────────────────────────────────

echo ""
info "Installation complete."
echo ""
echo "      Service:  systemctl status ${SERVICE_NAME}"
echo "      Logs:     journalctl -u ${SERVICE_NAME} -f"
echo "      Config:   ${CONFIG_DIR}/config.yaml"
echo ""
