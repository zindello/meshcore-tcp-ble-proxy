# Design Justifications

This document records the non-obvious decisions made in this codebase: why a particular approach was chosen, what alternatives were ruled out, and what bugs were hit along the way. It exists because the code cannot always communicate the *why*, only the *what*.

---

## BLE advertisement name

### Why we do a pre-connect handshake to discover the node name

The proxy advertises as `MeshCore-<node_name>` rather than a static name like `MeshCore`. This matters because phone operating systems (iOS and Android) permanently cache the mapping from a BLE peripheral identifier to its advertisement name. If two different MeshCore companions were both advertising as `MeshCore`, a user who connected to the first one would see the wrong name forever when connecting to the second, because the OS serves the cached name instead of reading the advertisement again.

The node name is retrieved by opening a short-lived TCP connection, sending `APP_START`, reading the `SELF_INFO` response, and then closing that connection. The `Bridge` then opens a second, permanent connection for actual data traffic. The handshake connection is intentionally separate so the TCPClient's reconnect logic does not interfere.

### Why the SELF_INFO offset is 58, not 34

The first version of this code read the node name at `frame[34:]` based on the assumption:

```
type(1) + pubkey(32) + adv_type(1) = 34 bytes
```

The actual SELF_INFO layout is:

```
frame[0]      type            (1 B)
frame[1]      adv_type        (1 B)
frame[2]      tx_power        (1 B)
frame[3]      max_tx_power    (1 B)
frame[4:36]   pubkey          (32 B)
frame[36:40]  adv_lat         (4 B, int32 LE)
frame[40:44]  adv_lon         (4 B, int32 LE)
frame[44]     multi_acks      (1 B)
frame[45]     adv_loc_policy  (1 B)
frame[46]     telemetry_mode  (1 B)
frame[47]     manual_add_contacts (1 B)
frame[48:52]  radio_freq      (4 B, uint32 LE)
frame[52:56]  radio_bw        (4 B, uint32 LE)
frame[56]     radio_sf        (1 B)
frame[57]     radio_cr        (1 B)
frame[58:]    adv_name        (variable-length UTF-8)
```

The incorrect offset silently read 24 bytes of binary latitude, longitude, and radio parameters into the node name. BLE advertising received a string prefixed with raw binary data, which stalled the BLE stack silently. The correct layout was confirmed against the meshcore-cli Python reference implementation.

### Why APP_START byte 1 must be 0x03, not 0x00

The `APP_START` frame has a version/capability byte at offset 1. The companion uses this byte to determine which response format to send. Sending `0x00` caused the companion to respond with an older frame layout that placed the node name in the wrong field, causing MeshMapper to populate the wardrive API's `who` field with a public key instead of the node name. The reference implementation (`meshcore-cli`) sends `0x03`, so we do too.

### Why the fallback name is derived from the MAC address

If the TCP handshake fails (companion not yet running, network unreachable), the proxy falls back to `MeshCore-<8 hex chars>`. The 8 characters come from `uuid5(NAMESPACE_DNS, f"meshcore-tcp-ble-proxy.{mac_address}")`, where the MAC address is `uuid.getnode()`.

This satisfies three constraints simultaneously:

1. **Stable per host** — the same machine always advertises the same fallback name, preventing repeated entries in apps that list nearby devices.
2. **Unique per host** — two different machines (both without a reachable companion) produce different fallback names, keeping the phone BLE cache coherent.
3. **Non-reversible** — `uuid5` is a one-way hash; the MAC address is not recoverable from the advertisement.

A random UUID was rejected because it would produce a new name on every restart, creating duplicate phantom entries in apps' device lists.

---

## BLE peripheral implementation

### Why `run_coroutine_threadsafe` is used for write callbacks

The `bless` library calls the `write_request_func` callback from different threads depending on platform:

- **Linux/BlueZ**: callbacks arrive on the asyncio event loop thread.
- **macOS/CoreBluetooth**: callbacks arrive on the CoreBluetooth delegate thread, which is *not* the event loop thread.

Using `asyncio.run_coroutine_threadsafe` is safe from either thread, so the same code path works on both platforms without branching. Calling `asyncio.create_task` directly from the CoreBluetooth delegate thread would raise a `RuntimeError` because that thread has no running event loop.

### Why the re-advertising watchdog exists

On macOS, CoreBluetooth stops advertising the moment a central connects. It does not resume advertising automatically when the central disconnects. Without intervention, the proxy would become invisible to scanning apps after the first connection ends.

The watchdog (`_advertise_watchdog`) polls `is_advertising()` and `is_connected()` every 2 seconds. When both are false (central has left and advertising has stopped), it tears down the `BlessServer` completely and builds a fresh one. A full rebuild is necessary because there is no `BlessServer.start_advertising()` method independent of construction.

On Linux/BlueZ, advertising typically survives connect/disconnect cycles, so the watchdog runs but never triggers in normal operation.

### Why `prioritize_local_name=False` is passed to `server.start()`

The BLE advertisement packet has limited space. By default, bless will fill it with the local name first, potentially crowding out the service UUID. Apps like MeshMapper scan by service UUID (the Nordic UART Service UUID `6E400001-…`) rather than by name. If the service UUID is absent from the advertisement, those apps cannot find the peripheral.

Setting `prioritize_local_name=False` ensures the service UUID always appears in the advertisement packet regardless of name length.

### Why the macOS local name restriction is documented but not worked around

On macOS 12 and later, CoreBluetooth ignores `CBAdvertisementDataLocalNameKey` when the peripheral is the local machine. Scanning apps see the system Bluetooth device name (e.g. "Joshua's MacBook Air") instead of `MeshCore-<node_name>`. This is an OS-enforced restriction with no API escape hatch. The proxy still works correctly — connections succeed and data flows — the only impact is the visible name in the scanning app. The expected deployment target is Raspberry Pi, where this restriction does not apply.

---

## Security and robustness

### Why receive buffers are capped (`_MAX_BUF`)

Both `tcp_client.py` and `handshake.py` cap their receive buffers at a multiple of the maximum frame size. Without this cap, a buggy or malicious TCP server that sends a continuous stream of garbage bytes would cause the buffer to grow without bound until the process was killed by the OOM killer. When the cap is exceeded the buffer is discarded and reading continues; for `handshake.py` the connection is closed and `None` is returned.

The cap in `tcp_client.py` is `_MAX_FRAME * 8` (a more generous multiple) because the persistent connection legitimately buffers frames while the event loop is busy. The handshake connection uses `_MAX_FRAME * 4` because only one response is expected.

### Why `asyncio.get_running_loop()` is used instead of `get_event_loop()`

`asyncio.get_event_loop()` emits a `DeprecationWarning` in Python 3.10+ when called from a coroutine without a running loop, and is scheduled for removal. `get_running_loop()` raises `RuntimeError` if called outside a running loop, making bugs immediately visible rather than silently returning a wrong loop. Both call sites in this codebase are inside coroutines, so `get_running_loop()` is always correct.

---

## Deployment and installation

### Why a dedicated service account (`meshcore-tcp-ble-proxy`) is used

The proxy is a long-running network service that also has access to the system Bluetooth stack. Running it as root would give any bug in the code or its dependencies root-level access to the host. The dedicated account has exactly the permissions it needs: membership in the `bluetooth` group (for BlueZ D-Bus access) and read access to its config file. It cannot write to the install directory or elsewhere on the filesystem.

### Why `ExecStartPre=+` is used for rfkill and bluetoothctl

The service runs as `meshcore-tcp-ble-proxy`, which does not have permission to manipulate the Bluetooth radio state. On Raspberry Pi OS, the Bluetooth adapter often starts in a soft-blocked state (rfkill) or powered off. Without explicitly unblocking and powering the adapter before starting, the proxy fails immediately with `Failed to register advertisement`.

The `+` prefix instructs systemd to run that specific command as root, regardless of the `User=` directive. The main `ExecStart` command still runs as the unprivileged service account.

### Why `pi-bluetooth` must be installed on Raspberry Pi OS

The `pi-bluetooth` package provides two things that are not present by default:

1. **`BCM43430B0.hcd`** — the firmware patch file for the Pi Zero 2W's BT chip. Without it, the chip runs unpatched (`build 0000`) and the kernel's UART baudrate negotiation commands (`0xfc18`) time out with error -110. `hci0` appears in `/sys/class/bluetooth/` with a valid BD address but cannot be brought up — `hciconfig hci0 up` returns "Connection timed out".

2. **`hciuart.service`** — the systemd service that attaches the UART to BlueZ and triggers firmware loading at the right point in the boot sequence.

The installer (`install.sh`) includes `pi-bluetooth` in the `apt-get install` list and then performs a driver unbind/rebind to load the firmware immediately without rebooting. After unbinding the `hci_uart_bcm` device from the serial driver, the kernel re-probes on rebind and this time finds the `.hcd` file, completing firmware initialisation in place. The device names must be captured before unbinding because the sysfs symlinks disappear the moment unbind completes.

### Why `hciuart.service` is listed in `After=` and `Wants=`

`hciuart.service` (provided by `pi-bluetooth`) loads the BT firmware over UART and registers `hci0` with bluetoothd. The proxy must start after this has completed, otherwise bluetoothd reports "No default controller available".

`Wants=` rather than `Requires=` is intentional: on systems without `pi-bluetooth` (e.g. Pi Zero W where the original hciuart mechanism differs, or macOS), the unit will simply not exist and systemd will proceed without it rather than failing.

### Why bluetoothctl power on uses a wait loop

On the Pi Zero 2W, even after `hciuart.service` completes, bluetoothd may not have registered the controller yet. A bare `bluetoothctl power on` in this window fails with `No default controller available` and exits with code 1, aborting the service start.

The wait loop polls `bluetoothctl show` for the string `Controller` up to 15 times (30 seconds), then runs `power on` once the controller appears. The Pi Zero (original) does not exhibit this race, but the Zero 2W does — the wait loop is harmless on both.

### Why `--system-site-packages` is used for the virtual environment

The `dbus-fast` and `PyYAML` packages both have C or Cython extension modules. On 32-bit ARM hardware (Raspberry Pi Zero W, armv6l), PyPI does not publish binary wheels for these architectures, so pip falls back to compiling from source. Compilation of PyYAML's C extension takes several minutes on a Pi Zero.

Raspberry Pi OS packages `python3-yaml` as an apt package with a pre-compiled binary. Creating the virtual environment with `--system-site-packages` allows the venv to use the apt-installed `python3-yaml`, eliminating the compile step entirely. The venv still installs everything else in isolation.

### Why `bleak<0.20.0` is pinned in the Linux extras

`bless` (our BLE GATT server library) imports from `bleak`'s internal BlueZ/D-Bus backend at runtime, even though it does not always declare it as a dependency. `bleak` 0.20.0 switched its D-Bus transport from `dbus-next` (pure Python) to `dbus-fast` (Cython). `dbus-fast` does not publish binary wheels for 32-bit ARM (armhf/armv6l). On a Pi Zero W, installing `dbus-fast` requires compiling 9 Cython C extension modules from source, which takes 15–20 minutes.

Pinning `bleak<0.20.0` forces pip to resolve `bleak` to a 0.19.x release, which uses `dbus-next` as its D-Bus backend. `dbus-next` is pure Python and installs in seconds on any platform.

The pin does not affect macOS installs, where `bleak` is not installed at all (the `[macos]` extra uses CoreBluetooth directly).

### Why `read </dev/tty` is used in the install script's `prompt()` function

The installer is designed to be run as `curl ... | sudo bash`. In this pattern, bash reads its input (the script) from the pipe connected to `curl`. A bare `read` command inside the script would consume the next line of the *script itself* from that pipe, storing it as the user's answer, and the rest of the script would be silently skipped.

Redirecting `read` to `/dev/tty` forces it to read from the terminal directly, leaving the pipe untouched. The `|| true` absorbs the error that would occur if `/dev/tty` is unavailable (e.g. in a fully non-interactive environment), in which case the default value is used.

---

## Protocol implementation

### Why the Nordic UART Service (NUS) UUIDs are used

MeshCore firmware advertises using the Nordic UART Service profile. Apps that connect to MeshCore devices — including MeshMapper — scan for and connect using the NUS service UUID. Using the same UUIDs means existing apps require no modification to connect to the proxy; it is indistinguishable from a real MeshCore BLE companion from the app's perspective.

### Why no additional framing is added at the BLE layer

The MeshCore companion protocol already frames each message with a marker byte and a 16-bit length prefix at the TCP layer. When bridging to BLE, each BLE characteristic write or notification carries exactly one complete protocol frame. This works because MeshMapper (and similar apps) already handle one-frame-per-operation semantics. Adding a second framing layer would have required changes to every client app.

The one constraint this creates is MTU: BLE's default MTU is 23 bytes (20 bytes usable), which is too small for most MeshCore frames. Apps must negotiate MTU ≥ 512. MeshMapper does this automatically during connection setup.
