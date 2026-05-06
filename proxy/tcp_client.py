"""
TCP companion client.

Frame format (MeshCore companion protocol):
  app  → device: 0x3C ('<') + uint16-LE length + payload
  device → app:  0x3E ('>') + uint16-LE length + payload
"""
from __future__ import annotations

import asyncio
import logging
import struct
from collections.abc import Awaitable, Callable

from . import protocol as P

log = logging.getLogger(__name__)

_TX_MARKER = 0x3C   # '<'  we send to device
_RX_MARKER = 0x3E   # '>'  device sends to us
_MAX_FRAME = 300
_MAX_BUF   = _MAX_FRAME * 8   # drop and reset if a buggy/malicious peer fills the buffer
_RECONNECT_DELAY = 5.0
_READ_TIMEOUT    = 120.0  # seconds; catches half-open connections where peer accepted but went silent


class TCPClient:
    """Connects to a MeshCore TCP companion and delivers raw protocol frames."""

    def __init__(
        self,
        host: str,
        port: int,
        on_frame: Callable[[bytes], Awaitable[None]],
    ) -> None:
        self._host = host
        self._port = port
        self._on_frame = on_frame
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()

    def start(self) -> None:
        """Start the background connect/reconnect loop."""
        asyncio.get_running_loop().create_task(self._connect_loop(), name="tcp-connect")

    async def send(self, frame: bytes) -> None:
        """Send one raw protocol frame to the MeshCore node."""
        async with self._write_lock:
            if self._writer is None:
                log.warning("tcp: not connected — frame dropped (%d B)", len(frame))
                return
            header = struct.pack("<BH", _TX_MARKER, len(frame))
            wire = header + frame
            log.debug(
                "tcp: wire →  %d B (0x3C + len=%d)\n%s",
                len(wire),
                len(frame),
                P.hexdump(wire),
            )
            self._writer.write(wire)
            await self._writer.drain()

    # ------------------------------------------------------------------

    async def _connect_loop(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_connection(self._host, self._port)
            except OSError as exc:
                log.warning(
                    "tcp: connect %s:%d failed: %s — retry in %.0fs",
                    self._host, self._port, exc, _RECONNECT_DELAY,
                )
                await asyncio.sleep(_RECONNECT_DELAY)
                continue

            log.info("tcp: connected to %s:%d", self._host, self._port)
            async with self._write_lock:
                self._writer = writer

            try:
                await self._read_loop(reader)
            except Exception as exc:
                log.error("tcp: read error: %s", exc)
            finally:
                async with self._write_lock:
                    self._writer = None
                writer.close()
                log.info("tcp: disconnected — retry in %.0fs", _RECONNECT_DELAY)
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        buf = b""
        while True:
            try:
                chunk = await asyncio.wait_for(reader.read(512), timeout=_READ_TIMEOUT)
            except asyncio.TimeoutError:
                log.warning("tcp: read timeout after %.0fs — reconnecting", _READ_TIMEOUT)
                return
            if not chunk:
                return
            log.debug(
                "tcp: wire ←  %d B\n%s",
                len(chunk),
                P.hexdump(chunk),
            )
            buf += chunk
            if len(buf) > _MAX_BUF:
                log.warning("tcp: receive buffer overflow (%d B) — discarding", len(buf))
                buf = b""
            while True:
                frame, buf = _extract_frame(buf)
                if frame is None:
                    break
                log.debug(
                    "tcp: recv  %s  (%d B)\n%s",
                    P.resp_name(frame[0]) if frame else "?",
                    len(frame),
                    P.hexdump(frame),
                )
                await self._on_frame(frame)


def _extract_frame(buf: bytes) -> tuple[bytes | None, bytes]:
    """Return (frame_payload, remaining_buf) or (None, buf) if incomplete."""
    # Scan forward to the next RX marker; log anything skipped (framing drift)
    skipped = 0
    while buf and buf[0] != _RX_MARKER:
        buf = buf[1:]
        skipped += 1
    if skipped:
        log.warning("tcp: skipped %d non-marker byte(s) — framing drift", skipped)
    if len(buf) < 3:
        return None, buf
    (size,) = struct.unpack_from("<H", buf, 1)
    if size > _MAX_FRAME:
        log.warning(
            "tcp: bad frame size %d at marker — skipping (max %d)", size, _MAX_FRAME
        )
        return None, buf[1:]        # bad size — skip marker and re-scan
    if len(buf) < 3 + size:
        return None, buf            # incomplete frame — wait for more data
    return buf[3 : 3 + size], buf[3 + size :]
