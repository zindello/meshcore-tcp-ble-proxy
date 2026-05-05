"""
Bridge — wires the TCP companion client to the BLE peripheral.

Startup sequence
  1. Run the TCP handshake to discover the node's advertisement name.
  2. Bring up the BLE peripheral with that name (or the fallback name).
  3. Start the TCP reconnect loop.

Frame flow
  BLE central (app) → write to RX char → bridge → TCP device
  TCP device        → notify TX char   → bridge → BLE central (app)
"""
from __future__ import annotations

import logging
import uuid

from . import protocol as P
from .ble_peripheral import BLEPeripheral
from .handshake import fetch_device_name
from .tcp_client import TCPClient

log = logging.getLogger(__name__)

# Fallback BLE name when the companion identity cannot be retrieved.
# Derived from the machine's MAC address so it's stable per host but unique
# across machines — preventing phone BLE cache collisions between hosts.
_node_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"meshcore-proxy.{uuid.getnode():012x}")
_FALLBACK_NAME = "MeshCore-" + _node_uuid.hex[:8].upper()


class Bridge:
    """Bidirectional raw-frame bridge between a MeshCore TCP companion and a BLE peripheral."""

    def __init__(
        self,
        tcp_host: str,
        tcp_port: int,
        handshake_timeout: float = 10.0,
    ) -> None:
        self._tcp_host = tcp_host
        self._tcp_port = tcp_port
        self._handshake_timeout = handshake_timeout
        self._tcp = TCPClient(tcp_host, tcp_port, on_frame=self._tcp_to_ble)
        self._ble: BLEPeripheral | None = None

    async def stop(self) -> None:
        """Shut down BLE advertising and close the TCP connection."""
        if self._ble is not None:
            await self._ble.stop()

    async def start(self) -> None:
        # Step 1 — discover the node name before advertising
        name = await fetch_device_name(
            self._tcp_host,
            self._tcp_port,
            timeout=self._handshake_timeout,
        )
        if name:
            device_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, f"meshcore-proxy.{name}")
            log.info("bridge: BLE advertisement name: %r  device-id: %s", name, device_uuid)
        else:
            name = _FALLBACK_NAME
            log.warning("bridge: could not retrieve companion identity — falling back to %r", name)

        # Step 2 — bring up the BLE peripheral with the resolved name
        self._ble = BLEPeripheral(name, on_frame=self._ble_to_tcp)
        await self._ble.start()

        # Step 3 — start the TCP reconnect loop (runs in background)
        self._tcp.start()
        log.info("bridge: running")

    async def _tcp_to_ble(self, frame: bytes) -> None:
        log.debug(
            "bridge: tcp→ble  %s\n%s",
            P.resp_name(frame[0]) if frame else "?",
            P.hexdump(frame),
        )
        if self._ble is not None:
            self._ble.send(frame)

    async def _ble_to_tcp(self, frame: bytes) -> None:
        log.debug(
            "bridge: ble→tcp  %s\n%s",
            P.cmd_name(frame[0]) if frame else "?",
            P.hexdump(frame),
        )
        await self._tcp.send(frame)
