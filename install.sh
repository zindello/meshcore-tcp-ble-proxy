#!/usr/bin/env bash
# MeshCore TCP-BLE Proxy — install / upgrade / uninstall
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/zindello/meshcore-tcp-ble-proxy/dev/install.sh | sudo bash -s -- install
#   curl -sSL https://raw.githubusercontent.com/zindello/meshcore-tcp-ble-proxy/dev/install.sh | sudo bash -s -- upgrade
#   sudo bash install.sh uninstall
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
step()  { echo ""; echo "  ── $* ──────────────────────────────────────────"; echo ""; }

prompt() {
    local var="$1" prompt_text="$2" default="$3"
    # Read from /dev/tty directly so piped installs (curl|bash, ssh|bash)
    # don't accidentally consume the rest of the script from stdin.
    if [[ -e /dev/tty ]]; then
        read -r -p "      ${prompt_text} [${default}]: " "${var?}" </dev/tty || true
    fi
    : "${!var:=$default}"
}

confirm() {
    local answer
    prompt answer "$1" "n"
    [[ "${answer,,}" =~ ^(y|yes)$ ]]
}

# ── preflight ─────────────────────────────────────────────────────────────────

[[ $EUID -eq 0 ]] || die "Run with sudo."
[[ "$(uname -s)" == "Linux" ]] || die "This installer is for Linux (Raspberry Pi OS / Debian)."

# ── shared steps ──────────────────────────────────────────────────────────────

do_packages() {
    step "System packages"
    apt-get update -qq
    apt-get install -y --no-install-recommends \
        git python3 python3-venv python3-yaml bluetooth bluez pi-bluetooth rfkill
}

do_bt_firmware_reload() {
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
        info "Bluetooth firmware reloaded."
    fi
}

do_service_user() {
    step "Service user"
    if ! id "${SERVICE_USER}" &>/dev/null; then
        info "Creating service user '${SERVICE_USER}'…"
        useradd --system --no-create-home --shell /usr/sbin/nologin "${SERVICE_USER}"
    else
        info "Service user '${SERVICE_USER}' already exists."
    fi
    usermod -aG bluetooth "${SERVICE_USER}"
    info "User '${SERVICE_USER}' is a member of the 'bluetooth' group."
}

do_clone() {
    step "Repository"
    if [[ -d "${INSTALL_DIR}/.git" ]]; then
        info "Updating existing installation at ${INSTALL_DIR}…"
        git -C "${INSTALL_DIR}" fetch origin
        git -C "${INSTALL_DIR}" checkout "${REPO_BRANCH}"
        git -C "${INSTALL_DIR}" pull origin "${REPO_BRANCH}"
    else
        info "Cloning repository (branch: ${REPO_BRANCH}) into ${INSTALL_DIR}…"
        git clone --branch "${REPO_BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
    fi
}

do_dbus_policy() {
    step "D-Bus policy"
    cp "${INSTALL_DIR}/deploy/meshcore-tcp-ble-proxy-dbus.conf" "${DBUS_POLICY}"
    info "D-Bus policy installed at ${DBUS_POLICY}."
}

do_venv() {
    step "Python environment"
    # --system-site-packages lets the venv use apt-installed python3-yaml,
    # avoiding a from-source compile of PyYAML's C extension on ARM hardware.
    python3 -m venv --system-site-packages "${INSTALL_DIR}/venv"
    "${INSTALL_DIR}/venv/bin/pip" install "${INSTALL_DIR}[linux]"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/venv"
}

do_venv_upgrade() {
    step "Python environment"
    "${INSTALL_DIR}/venv/bin/pip" install --upgrade "${INSTALL_DIR}[linux]"
    chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_DIR}/venv"
}

do_config() {
    step "Configuration"
    mkdir -p "${CONFIG_DIR}"
    if [[ -f "${CONFIG_DIR}/config.yaml" ]]; then
        info "Config already exists at ${CONFIG_DIR}/config.yaml — skipping."
    else
        echo "  Configure the MeshCore TCP companion connection."
        echo "  Press Enter to accept the default shown in brackets."
        echo ""
        prompt tcp_host "Companion host" "localhost"
        prompt tcp_port "Companion port" "5050"
        echo ""

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
}

do_service() {
    step "Systemd service"
    cp "${INSTALL_DIR}/deploy/meshcore-tcp-ble-proxy.service" "${SERVICE_FILE}"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}"
    systemctl restart "${SERVICE_NAME}"
    info "Service enabled and started."
}

# ── commands ──────────────────────────────────────────────────────────────────

cmd_install() {
    echo ""
    echo "  Installing MeshCore TCP-BLE Proxy"
    echo "  Branch: ${REPO_BRANCH}"

    do_packages
    do_bt_firmware_reload
    do_service_user
    do_clone
    do_dbus_policy
    do_venv
    do_config
    do_service

    echo ""
    info "Installation complete."
    echo ""
    echo "      Service:  systemctl status ${SERVICE_NAME}"
    echo "      Logs:     journalctl -u ${SERVICE_NAME} -f"
    echo "      Config:   ${CONFIG_DIR}/config.yaml"
    echo ""
}

cmd_upgrade() {
    [[ -d "${INSTALL_DIR}/.git" ]] \
        || die "No existing installation found at ${INSTALL_DIR}. Run 'install' first."

    echo ""
    echo "  Upgrading MeshCore TCP-BLE Proxy"
    echo "  Branch: ${REPO_BRANCH}"

    do_packages
    do_bt_firmware_reload
    do_clone
    do_dbus_policy
    do_venv_upgrade
    do_service

    echo ""
    info "Upgrade complete."
    echo ""
    echo "      Logs:  journalctl -u ${SERVICE_NAME} -f"
    echo ""
}

cmd_uninstall() {
    echo ""
    echo "  Uninstalling MeshCore TCP-BLE Proxy"
    echo ""

    step "Service"
    if systemctl is-active --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Stopping ${SERVICE_NAME}…"
        systemctl stop "${SERVICE_NAME}"
    fi
    if systemctl is-enabled --quiet "${SERVICE_NAME}" 2>/dev/null; then
        info "Disabling ${SERVICE_NAME}…"
        systemctl disable "${SERVICE_NAME}"
    fi
    if [[ -f "${SERVICE_FILE}" ]]; then
        rm -f "${SERVICE_FILE}"
        info "Removed ${SERVICE_FILE}."
    fi
    systemctl daemon-reload

    step "D-Bus policy"
    if [[ -f "${DBUS_POLICY}" ]]; then
        rm -f "${DBUS_POLICY}"
        info "Removed ${DBUS_POLICY}."
    fi

    step "Installation directory"
    if [[ -d "${INSTALL_DIR}" ]]; then
        rm -rf "${INSTALL_DIR}"
        info "Removed ${INSTALL_DIR}."
    fi

    step "Configuration"
    if [[ -d "${CONFIG_DIR}" ]]; then
        if confirm "Remove config directory ${CONFIG_DIR}? Your settings will be lost. [y/N]"; then
            rm -rf "${CONFIG_DIR}"
            info "Removed ${CONFIG_DIR}."
        else
            info "Config kept at ${CONFIG_DIR}."
        fi
    fi

    step "Service user"
    if id "${SERVICE_USER}" &>/dev/null; then
        if confirm "Remove service user '${SERVICE_USER}'? [y/N]"; then
            userdel "${SERVICE_USER}"
            info "Removed user '${SERVICE_USER}'."
        else
            info "User '${SERVICE_USER}' kept."
        fi
    fi

    echo ""
    info "Uninstall complete."
    echo ""
}

# ── main ──────────────────────────────────────────────────────────────────────

case "${1:-install}" in
    install)   cmd_install   ;;
    upgrade)   cmd_upgrade   ;;
    uninstall) cmd_uninstall ;;
    *) die "Unknown command '${1}'. Usage: bash install.sh [install|upgrade|uninstall]" ;;
esac
