# MeshCore TCP-BLE Proxy

> **Disclaimer:** This project was written with [Claude](https://claude.ai) (Anthropic AI). The code should be treated with appropriate scrutiny — review carefully before deploying. Bug reports and PRs are very welcome.

Bridges a remote **MeshCore TCP companion** to a local **Bluetooth Low Energy peripheral**, so that BLE-only apps (e.g. MeshMapper) can reach a MeshCore node anywhere on your network.

```
[MeshMapper / BLE app]
        ↕  BLE (Nordic UART Service)
[Raspberry Pi — this proxy]
        ↕  TCP (MeshCore companion protocol)
[MeshCore node — anywhere on the network]
```

---

## Installation

### Raspberry Pi / Linux (recommended)

```bash
curl -sSL https://raw.githubusercontent.com/zindello/meshcore-tcp-ble-proxy/main/install.sh | sudo bash
```

The installer will:
- Install system dependencies (Python 3, BlueZ, git)
- Create a dedicated `meshcore-proxy` service account
- Clone the repo into `/opt/meshcore-tcp-ble-proxy/`
- Create a virtual environment and install dependencies
- Prompt for the companion host and port (defaults: `localhost`, `5050`)
- Write `/etc/meshcore-tcp-ble-proxy/config.yaml`
- Enable and start the `meshcore-tcp-ble-proxy` systemd service

After installation:

```bash
systemctl status meshcore-tcp-ble-proxy
journalctl -u meshcore-tcp-ble-proxy -f
```

### Manual install (Linux)

```bash
sudo apt install bluetooth bluez
git clone https://github.com/zindello/meshcore-tcp-ble-proxy
cd meshcore-tcp-ble-proxy
python3 -m venv .venv && source .venv/bin/activate
pip install ".[linux]"
```

### macOS (for testing)

```bash
git clone https://github.com/zindello/meshcore-tcp-ble-proxy
cd meshcore-tcp-ble-proxy
python3 -m venv .venv && source .venv/bin/activate
pip install ".[macos]"
```

> **Bluetooth permission** — on macOS 12+ grant your terminal Bluetooth access:
> System Settings → Privacy & Security → Bluetooth.

---

## Configuration

The proxy reads `/etc/meshcore-tcp-ble-proxy/config.yaml` at startup.
A template is at `deploy/config.yaml.example`.

```yaml
tcp:
  host: localhost
  port: 5050
  handshake_timeout: 10.0

logging:
  debug: false
```

Restart the service after editing:

```bash
sudo systemctl restart meshcore-tcp-ble-proxy
```

### CLI overrides

Any setting can be overridden at the command line without editing the config file:

```
meshcore-proxy [--config PATH] [--tcp HOST:PORT] [-v]

  --config PATH     Config file path (default: /etc/meshcore-tcp-ble-proxy/config.yaml)
  --tcp HOST:PORT   Override companion address from config
  -v / --verbose    Enable debug logging (overrides config)
```

Examples:

```bash
# Use config file defaults
meshcore-proxy

# Override companion address
meshcore-proxy --tcp 192.168.1.50:5050

# Debug logging
meshcore-proxy -v
```

---

## Architecture

```
proxy/
├── __main__.py        CLI entry point — config loading, asyncio event loop
├── bridge.py          Wires TCP ↔ BLE; routes frames in both directions
├── handshake.py       One-shot TCP handshake to resolve the BLE advertisement name
├── tcp_client.py      Persistent TCP connection with automatic reconnect
└── ble_peripheral.py  GATT server (Nordic UART Service) via bless / BlueZ
```

All components run inside a single asyncio event loop.

- **TCPClient** maintains a persistent connection with automatic reconnect. Frames from the node arrive via an `on_frame` callback; frames from the app are sent with `send()`.
- **BLEPeripheral** registers a GATT application with the platform BLE stack (BlueZ on Linux, CoreBluetooth on macOS) and starts advertising. Write callbacks are dispatched with `asyncio.run_coroutine_threadsafe` so they are safe regardless of which OS thread bless uses to deliver them.
- **Bridge** holds both and cross-wires their callbacks.

### BLE advertisement name

The BLE peripheral advertises as `MeshCore-<node_name>`, where `<node_name>` is the companion's configured node name retrieved during the TCP handshake. If the handshake fails, the name falls back to `MeshCore-<8 hex chars>` derived from the machine's MAC address.

---

## Protocol details

### TCP companion framing

| Direction | Byte 0 | Bytes 1–2 | Bytes 3… |
|-----------|--------|-----------|----------|
| app → device | `0x3C` (`<`) | uint16 LE length | payload |
| device → app | `0x3E` (`>`) | uint16 LE length | payload |

Maximum payload: 300 bytes. Multi-byte integers are little-endian.

### BLE (Nordic UART Service)

| Role | UUID |
|------|------|
| Service | `6E400001-B5A3-F393-E0A9-E50E24DCCA9E` |
| RX char (central writes) | `6E400002-B5A3-F393-E0A9-E50E24DCCA9E` |
| TX char (central subscribes) | `6E400003-B5A3-F393-E0A9-E50E24DCCA9E` |

Each characteristic write or notification carries exactly one protocol frame. Apps should request MTU ≥ 512 to handle larger frames.

---

## Troubleshooting

### Linux / Raspberry Pi

**D-Bus errors on start**
Ensure BlueZ is running: `sudo systemctl start bluetooth`

**Permission denied on the Bluetooth adapter**
The installer adds the service account to the `bluetooth` group automatically.
For manual installs: `sudo usermod -aG bluetooth $USER` then log out and back in.

### macOS

**`CBManagerStateUnauthorized` / "not authorized"**
System Settings → Privacy & Security → Bluetooth → grant access to your terminal. Re-run after granting.

**`CBManagerStatePoweredOff`**
Enable Bluetooth in System Settings → Bluetooth.

**`bless` hangs at `await server.start()`**
CoreBluetooth is waiting for a permission prompt that may be behind other windows.

### General

**App connects but receives no data**
Enable verbose logging (`-v`) and confirm frames are flowing on the TCP side.

**Large frames (>20 bytes) are truncated**
The connecting app must negotiate MTU ≥ 512. MeshMapper does this automatically.

---

## Sources

- [MeshCore companion protocol docs](https://github.com/meshcore-dev/MeshCore/blob/main/docs/companion_protocol.md)
- [MeshCore companion protocol wiki](https://github.com/meshcore-dev/MeshCore/wiki/Companion-Radio-Protocol)
- [meshcore-cli (Python reference implementation)](https://github.com/meshcore-dev/meshcore-cli)
- [bless — BLE GATT server library](https://github.com/kevincar/bless)
