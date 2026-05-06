"""
Initial TCP handshake to discover the MeshCore node's identity.

Sequence:
  1. Open a short-lived TCP connection.
  2. Send APP_START (0x01) with our proxy app name.
  3. The device automatically replies with SELF_INFO (0x05).
  4. Parse the node name from SELF_INFO and return a "MeshCore-<name>" BLE name.
  5. Close the connection; the main TCPClient will open its own connection.

The BLE advertisement name is "MeshCore-" + the companion's configured node name
(e.g. "MeshCore-MeshCoreBridgeTest2").  This matches real MeshCore device naming
and ensures uniqueness per companion — important because phone OSes cache the
Bluetooth peripheral identifier → name mapping, so two different companions must
present distinct advertisement names.
"""
from __future__ import annotations

import asyncio
import logging
import struct

from . import protocol as P

log = logging.getLogger(__name__)

_TX_MARKER = 0x3C   # '<'  app → device
_RX_MARKER = 0x3E   # '>'  device → app
_MAX_FRAME = 300
_MAX_BUF   = _MAX_FRAME * 4   # abort handshake if peer floods us with garbage
_MAX_NAME  = 20               # cap node name to keep BLE advert packet well within limits

# APP_START payload: type(1) + version(1) + padding(6 spaces) + app_name(utf-8)
# Byte 1 must be 0x03 — the companion uses it as a protocol version/capability
# flag and may send a different (older) frame format if it sees 0x00 here.
# Reference: meshcore-cli sends b"\x01\x03      mccli"
_APP_START = bytes([P.CMD_APP_START]) + b"\x03" + b" " * 6 + b"meshcore-tcp-ble-proxy"


async def fetch_device_name(
    host: str,
    port: int,
    timeout: float = 10.0,
) -> str | None:
    """
    Connect to the MeshCore TCP companion, send APP_START, and return a BLE
    advertisement name of the form "MeshCore-<node_name>".

    Returns None if the connection fails or the response doesn't arrive in time.
    The caller should fall back to a configured default name.
    """
    log.info("handshake: connecting to %s:%d to discover node identity…", host, port)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=timeout,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        log.warning("handshake: connect %s:%d failed: %s", host, port, exc)
        return None

    try:
        # Send APP_START
        pkt = struct.pack("<BH", _TX_MARKER, len(_APP_START)) + _APP_START
        writer.write(pkt)
        await writer.drain()
        log.debug(
            "handshake: sent APP_START (%d B):\n%s",
            len(_APP_START),
            P.hexdump(_APP_START),
        )

        # Read responses until we see SELF_INFO or we time out
        buf = b""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                log.warning("handshake: timed out waiting for SELF_INFO")
                return None
            try:
                chunk = await asyncio.wait_for(reader.read(512), timeout=remaining)
            except asyncio.TimeoutError:
                log.warning("handshake: timed out waiting for SELF_INFO")
                return None
            if not chunk:
                log.warning("handshake: TCP connection closed before SELF_INFO arrived")
                return None
            buf += chunk
            if len(buf) > _MAX_BUF:
                log.warning("handshake: receive buffer overflow — aborting")
                return None

            while True:
                frame, buf = _extract_frame(buf)
                if frame is None:
                    break
                log.debug(
                    "handshake: received %s (%d B):\n%s",
                    P.resp_name(frame[0]),
                    len(frame),
                    P.hexdump(frame),
                )
                name = _parse_ble_name(frame)
                if name is not None:
                    log.info("handshake: BLE name → %r", name)
                    return name
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


def _extract_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    while buf and buf[0] != _RX_MARKER:
        buf = buf[1:]
    if len(buf) < 3:
        return None, buf
    (size,) = struct.unpack_from("<H", buf, 1)
    if size > _MAX_FRAME:
        return None, buf[1:]
    if len(buf) < 3 + size:
        return None, buf
    return buf[3 : 3 + size], buf[3 + size :]


def _parse_ble_name(frame: bytes) -> str | None:
    """
    Derive the BLE advertisement name from a SELF_INFO (0x05) frame.

    SELF_INFO layout (little-endian multi-byte fields):
      frame[0]      = 0x05  packet type
      frame[1]      = adv_type
      frame[2]      = tx_power (dBm)
      frame[3]      = max_tx_power (dBm)
      frame[4:36]   = pubkey (32 bytes)
      frame[36:40]  = adv_lat (int32)
      frame[40:44]  = adv_lon (int32)
      frame[44]     = multi_acks
      frame[45]     = adv_loc_policy
      frame[46]     = telemetry_mode
      frame[47]     = manual_add_contacts
      frame[48:52]  = radio_freq (uint32)
      frame[52:56]  = radio_bw (uint32)
      frame[56]     = radio_sf
      frame[57]     = radio_cr
      frame[58:]    = adv_name (variable-length UTF-8)

    Returns "MeshCore-<node_name>" matching the companion's configured device
    name and the naming convention used by real MeshCore firmware.
    """
    if not frame or frame[0] != P.RESP_SELF_INFO:
        return None
    if len(frame) < 58:
        log.warning(
            "handshake: SELF_INFO too short (%d B, need ≥ 58)",
            len(frame),
        )
        return None
    node_name = frame[58:].rstrip(b"\x00").decode("utf-8", errors="replace").strip()
    if not node_name:
        log.warning("handshake: SELF_INFO contains empty node name")
        return None
    return "MeshCore-" + node_name[:_MAX_NAME]
