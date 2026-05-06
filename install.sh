#!/usr/bin/env bash
# MeshCore TCP-BLE Proxy installer
# Usage: curl -sSL https://raw.githubusercontent.com/zindello/meshcore-tcp-ble-proxy/dev/install.sh | sudo bash
set -euo pipefail

REPO_URL="https://github.com/zindello/meshcore-tcp-ble-proxy"
REPO_BRANCH="${MESHCORE_PROXY_BRANCH:-dev}"
INSTALL_DIR="/opt/meshcore-tcp-ble-proxy"
CONFIG_DIR="/etc/meshcore-tcp-ble-proxy"
SERVICE_USER="meshcore-tcp-ble-proxy"
SERVICE_NAME="meshcore-tcp-ble-proxy"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
DBUS_POLICY="/etc/dbus-1/system.d/meshcore-tcp-ble-proxy.conf"

# ── helpers ───────────────────────────────────────────────────────────────────

info()  { echo "  [+] $*"; }
warn()  { echo "  [!] $*" >&2; }
die()   { echo "  [✗] $*" >&2; exit 1; }

prompt() {
    local var="$1" prompt_text="$2" default="$3"
    # Read from /dev/tty directly so piped installs (curl|bash, ssh|bash)
    # don't accidentally consume the rest of the script from stdin.
    if [[ -e /dev/tty ]]; then
        read -r -p "      ${prompt_text} [${default}]: " "${var?}" </dev/tty || true
    fi
    : "${!var:=$default}"
}

# ── preflight ─────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Run with sudo: curl ... | sudo bash"
[[ "$(uname -s)" == "Linux" ]] || die "This installer is for Linux (Raspberry Pi OS / Debian)."

# ── system packages ───────────────────────────────────────────────────────────

info "Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    git python3 python3-venv python3-yaml bluetooth bluez pi-bluetooth rfkill >/dev/null

# On Pi Zero 2W, the BCM43430B0 BT firmware is loaded over UART by the
# hci_uart_bcm driver.  If the system booted without pi-bluetooth installed
# (firmware file absent), the driver attaches but fails to patch the chip.
# Unbinding and rebinding the driver now that the .hcd file exists reloads
# the firmware in place — no reboot needed.
# Gated on Pi Zero 2W only; the original Pi Zero W does not exhibit this race.
_pi_model=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || true)
if echo "$_pi_model" | grep -qi "zero 2" && \
   compgen -G "/sys/bus/serial/drivers/hci_uart_bcm/serial*-*" > /dev/null 2>&1; then
    info "Pi Zero 2W detected — reloading Bluetooth firmware without reboot…"
    # Capture names before unbinding (the symlinks disappear on unbind).
    _bt_devs=()
    for dev in /sys/bus/serial/drivers/hci_uart_bcm/serial*-*; do
        _bt_devs+=("$(basename "$dev")")
    done
    for devname in "${_bt_devs[@]}"; do
        echo "$devname" > /sys/bus/serial/drivers/hci_uart_bcm/unbind 2>/dev/null || true
    done
    sleep 1
    for devname in "${_bt_devs[@]}"; do
        echo "$devname" > /sys/bus/serial/drivers/hci_uart_bcm/bind 2>/dev/null || true
    done
    sleep 3
fi

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
    git -C "${INSTALL_DIR}" fetch --quiet origin
    git -C "${INSTALL_DIR}" checkout --quiet "${REPO_BRANCH}"
    git -C "${INSTALL_DIR}" pull --quiet origin "${REPO_BRANCH}"
else
    info "Cloning repository (branch: ${REPO_BRANCH}) into ${INSTALL_DIR}…"
    git clone --quiet --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

# ── D-Bus policy ──────────────────────────────────────────────────────────────

info "Installing D-Bus policy…"
cp "${INSTALL_DIR}/deploy/meshcore-tcp-ble-proxy-dbus.conf" "${DBUS_POLICY}"

# ── virtual environment ───────────────────────────────────────────────────────

info "Creating Python virtual environment…"
# --system-site-packages lets the venv use apt-installed python3-yaml,
# avoiding a from-source compile of PyYAML's C extension on ARM hardware.
python3 -m venv --system-site-packages "${INSTALL_DIR}/venv"
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
