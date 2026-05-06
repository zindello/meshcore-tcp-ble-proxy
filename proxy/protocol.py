"""
MeshCore companion protocol constants and debug helpers.

Direction conventions used in log messages:
  ble→tcp   frame received from BLE central, forwarded to TCP device
  tcp→ble   frame received from TCP device,  forwarded to BLE central
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Packet type bytes
# Commands (app → device, i.e. ble→tcp direction)
CMD_APP_START     = 0x01
CMD_SEND_MSG      = 0x03
CMD_GET_MSG       = 0x0A
CMD_GET_BATTERY   = 0x14
CMD_DEVICE_QUERY  = 0x16  # always followed by 0x03
CMD_GET_CHANNEL   = 0x1F
CMD_SET_CHANNEL   = 0x20
CMD_SEND_DATAGRAM = 0x3E

# Responses (device → app, i.e. tcp→ble direction)
RESP_OK             = 0x00
RESP_ERROR          = 0x01
RESP_SELF_INFO      = 0x05
RESP_MSG_SENT       = 0x06
RESP_CONTACT_MSG    = 0x07
RESP_CHAN_MSG        = 0x08
RESP_EXPORT_CONTACT = 0x0B
RESP_BATTERY        = 0x0C
RESP_DEVICE_INFO    = 0x0D
RESP_CONTACT_MSG_V3 = 0x10
RESP_CHAN_MSG_V3    = 0x11
RESP_CHANNEL_INFO   = 0x12
RESP_MSGS_WAITING   = 0x83

_CMD_NAMES: dict[int, str] = {
    CMD_APP_START:     "APP_START",
    CMD_SEND_MSG:      "SEND_MSG",
    CMD_GET_MSG:       "GET_MSG",
    CMD_GET_BATTERY:   "GET_BATTERY",
    CMD_DEVICE_QUERY:  "DEVICE_QUERY",
    CMD_GET_CHANNEL:   "GET_CHANNEL",
    CMD_SET_CHANNEL:   "SET_CHANNEL",
    CMD_SEND_DATAGRAM: "SEND_DATAGRAM",
}

_RESP_NAMES: dict[int, str] = {
    RESP_OK:             "OK",
    RESP_ERROR:          "ERROR",
    RESP_SELF_INFO:      "SELF_INFO",
    RESP_MSG_SENT:       "MSG_SENT",
    RESP_CONTACT_MSG:    "CONTACT_MSG",
    RESP_CHAN_MSG:        "CHAN_MSG",
    RESP_EXPORT_CONTACT: "EXPORT_CONTACT",
    RESP_BATTERY:        "BATTERY",
    RESP_DEVICE_INFO:    "DEVICE_INFO",
    RESP_CONTACT_MSG_V3: "CONTACT_MSG_V3",
    RESP_CHAN_MSG_V3:     "CHAN_MSG_V3",
    RESP_CHANNEL_INFO:   "CHANNEL_INFO",
    RESP_MSGS_WAITING:   "MSGS_WAITING",
}


def cmd_name(type_byte: int) -> str:
    return _CMD_NAMES.get(type_byte, f"0x{type_byte:02X}")


def resp_name(type_byte: int) -> str:
    return _RESP_NAMES.get(type_byte, f"0x{type_byte:02X}")


def hexdump(data: bytes, indent: str = "    ") -> str:
    """Return a multi-line hex + ASCII dump, e.g. for debug logging."""
    if not data:
        return f"{indent}(empty)"
    lines: list[str] = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_col = " ".join(f"{b:02X}" for b in chunk)
        asc_col = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in chunk)
        lines.append(f"{indent}{i:04X}  {hex_col:<47}  {asc_col}")
    return "\n".join(lines)
