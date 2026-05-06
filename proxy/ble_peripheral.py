"""
BLE peripheral — exposes a MeshCore companion over Bluetooth Low Energy.

Uses the Nordic UART Service (NUS) which is what MeshCore firmware advertises:
  Service UUID : 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
  RX char UUID : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (central writes here)
  TX char UUID : 6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (we notify central here)

Each characteristic write / notification carries exactly one protocol frame —
no additional framing is required at the BLE layer.

Platform notes:
  Linux  — requires BlueZ ≥ 5.50; bless uses D-Bus (dbus-next).
           Callbacks arrive on the asyncio event loop thread.
  macOS  — requires pyobjc-framework-CoreBluetooth; bless uses CoreBluetooth.
           Callbacks arrive on the CoreBluetooth delegate thread, NOT the
           event loop thread — always use run_coroutine_threadsafe here.

macOS advertisement name note:
  On macOS 12+, CoreBluetooth ignores CBAdvertisementDataLocalNameKey when the
  peripheral is the local machine, so scanning apps see the system Bluetooth
  device name (e.g. "Joshua's MacBook Air") instead of the name we set.
  This is a platform restriction — the connection still works correctly.
  For a correctly-named peripheral, deploy to a Raspberry Pi.

Re-advertising after disconnect:
  On macOS CoreBluetooth, advertising stops when a central connects and does
  NOT resume automatically on disconnect. A background watchdog task polls
  is_advertising() / is_connected() every 2 s and rebuilds the BlessServer
  whenever the central has gone and advertising is no longer running.
  On Linux/BlueZ advertising typically continues through connect/disconnect
  cycles, so the watchdog is a no-op there in normal operation.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from bless import (
    BlessGATTCharacteristic,
    BlessServer,
    GATTAttributePermissions,
    GATTCharacteristicProperties,
)

from . import protocol as P

log = logging.getLogger(__name__)

_SERVICE_UUID = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
_RX_UUID      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"   # central → proxy
_TX_UUID      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"   # proxy → central

_WATCHDOG_INTERVAL = 2.0   # seconds between advertising-state checks


class BLEPeripheral:
    """GATT server that makes the proxy look like a native MeshCore BLE companion."""

    def __init__(
        self,
        name: str,
        on_frame: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._name = name
        self._on_frame = on_frame
        self._server: BlessServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._watchdog: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self._setup_server()
        self._watchdog = self._loop.create_task(
            self._advertise_watchdog(), name="ble-watchdog"
        )

    async def stop(self) -> None:
        """Stop advertising and clean up the GATT server."""
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except asyncio.CancelledError:
                pass
            self._watchdog = None
        await self._teardown_server()

    # ------------------------------------------------------------------

    async def _setup_server(self) -> None:
        """Build and start a fresh BlessServer."""
        server = BlessServer(name=self._name, loop=self._loop)
        server.read_request_func = self._handle_read
        server.write_request_func = self._handle_write

        await server.add_new_service(_SERVICE_UUID)

        # RX: app writes inbound protocol frames here
        await server.add_new_characteristic(
            _SERVICE_UUID,
            _RX_UUID,
            GATTCharacteristicProperties.write
            | GATTCharacteristicProperties.write_without_response,
            None,
            GATTAttributePermissions.writeable,
        )

        # TX: proxy pushes outbound protocol frames here as notifications
        await server.add_new_characteristic(
            _SERVICE_UUID,
            _TX_UUID,
            GATTCharacteristicProperties.notify | GATTCharacteristicProperties.read,
            None,
            GATTAttributePermissions.readable,
        )

        # prioritize_local_name=False ensures the NUS service UUID is always
        # included in the advertisement packet regardless of name length.
        # On macOS 12+ the local name is ignored by the OS anyway (scanning
        # devices see the system BT name), so omitting UUID priority would
        # make the peripheral invisible to apps that scan by service UUID.
        await server.start(prioritize_local_name=False)
        self._server = server
        log.info("ble: advertising as %r (Nordic UART Service)", self._name)
        log.info("ble: service  %s", _SERVICE_UUID)
        log.info("ble: RX char  %s  (central writes here)", _RX_UUID)
        log.info("ble: TX char  %s  (we notify here)", _TX_UUID)

    async def _teardown_server(self) -> None:
        """Stop and discard the current BlessServer, if any."""
        server, self._server = self._server, None
        if server is not None:
            try:
                await server.stop()
            except Exception as exc:
                log.debug("ble: server.stop() raised %s (ignored)", exc)

    async def _advertise_watchdog(self) -> None:
        """
        Restart advertising after a central disconnects.

        CoreBluetooth stops advertising when a central connects.  It does not
        resume automatically on disconnect, so we rebuild the BlessServer
        whenever we detect that advertising has stopped and no central is
        currently connected.
        """
        while True:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL)
            except asyncio.CancelledError:
                return

            if self._server is None:
                return

            try:
                advertising = await self._server.is_advertising()
                connected   = await self._server.is_connected()
            except Exception as exc:
                log.debug("ble: watchdog state check failed: %s", exc)
                continue

            if not advertising and not connected:
                log.info("ble: central disconnected — restarting advertisement")
                await self._teardown_server()
                try:
                    await self._setup_server()
                except Exception as exc:
                    log.error("ble: failed to restart advertisement: %s", exc)

    # ------------------------------------------------------------------

    def send(self, frame: bytes) -> None:
        """Push one protocol frame to the connected BLE central via notification."""
        if self._server is None:
            log.warning("ble: server not started — frame dropped (%d B)", len(frame))
            return
        log.debug(
            "ble: notify  %s  (%d B)\n%s",
            P.resp_name(frame[0]) if frame else "?",
            len(frame),
            P.hexdump(frame),
        )
        char = self._server.get_characteristic(_TX_UUID)
        char.value = bytearray(frame)
        ok = self._server.update_value(_SERVICE_UUID, _TX_UUID)
        if ok is False:
            log.warning(
                "ble: update_value returned False for %s (%d B) — "
                "central may not be subscribed or TX queue is full",
                P.resp_name(frame[0]) if frame else "?",
                len(frame),
            )

    # ------------------------------------------------------------------
    # bless callbacks
    # On Linux these are called from the asyncio event loop thread.
    # On macOS they are called from the CoreBluetooth delegate thread.
    # run_coroutine_threadsafe is safe from either thread.

    def _handle_read(self, characteristic: BlessGATTCharacteristic, **_: object) -> bytearray:
        log.debug("ble: read request on %s", characteristic.uuid)
        return characteristic.value or bytearray()

    def _handle_write(
        self,
        characteristic: BlessGATTCharacteristic,
        value: bytearray,
        **_: object,
    ) -> None:
        if self._loop is None:
            return
        frame = bytes(value)
        log.debug(
            "ble: write  %s  (%d B)\n%s",
            P.cmd_name(frame[0]) if frame else "?",
            len(frame),
            P.hexdump(frame),
        )
        asyncio.run_coroutine_threadsafe(self._on_frame(frame), self._loop)
