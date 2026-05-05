"""Entry point: python -m proxy [--config PATH] [--tcp HOST:PORT] [-v]"""
from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import signal
import sys

import yaml

from .bridge import Bridge

_DEFAULT_CONFIG = pathlib.Path("/etc/meshcore-tcp-ble-proxy/config.yaml")


def _load_config(path: pathlib.Path) -> dict:
    try:
        return yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "MeshCore TCP-BLE Proxy\n\n"
            "Connects to a MeshCore TCP companion node and re-exposes it as a "
            "BLE peripheral (Nordic UART Service), allowing apps like MeshMapper "
            "to connect over Bluetooth to a remote MeshCore node.\n\n"
            f"Config file: {_DEFAULT_CONFIG}"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        metavar="PATH",
        type=pathlib.Path,
        default=_DEFAULT_CONFIG,
        help=f"Config file path (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--tcp",
        metavar="HOST:PORT",
        default=None,
        help="Override companion address from config (e.g. 192.168.0.89:5050)",
    )
    parser.add_argument(
        "--handshake-timeout",
        metavar="SECS",
        type=float,
        default=None,
        help="Override handshake timeout from config",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        default=False,
        help="Enable debug-level logging (overrides config)",
    )
    args = parser.parse_args()

    cfg = _load_config(args.config)
    tcp_cfg = cfg.get("tcp", {})
    log_cfg = cfg.get("logging", {})

    # Resolve settings: CLI flag > config file > built-in default
    if args.tcp:
        host_raw, _, port_raw = args.tcp.rpartition(":")
        host = host_raw or port_raw
        try:
            port = int(port_raw) if host_raw else 5050
        except ValueError:
            parser.error(f"invalid port in --tcp: {port_raw!r}")
    else:
        host = str(tcp_cfg.get("host", "localhost"))
        try:
            port = int(tcp_cfg.get("port", 5050))
        except (ValueError, TypeError):
            parser.error("config tcp.port must be an integer")

    try:
        handshake_timeout = (
            args.handshake_timeout
            if args.handshake_timeout is not None
            else float(tcp_cfg.get("handshake_timeout", 10.0))
        )
    except (ValueError, TypeError):
        parser.error("config tcp.handshake_timeout must be a number")

    debug = args.verbose or bool(log_cfg.get("debug", False))
    log_level = logging.DEBUG if debug else logging.INFO

    # When stdout/stderr is not a TTY (running as a systemd service) journald
    # already adds timestamps and severity, so strip them from our format.
    if sys.stderr.isatty():
        log_fmt  = "%(asctime)s.%(msecs)03d  %(levelname)-7s  %(name)s: %(message)s"
        log_date = "%H:%M:%S"
    else:
        log_fmt  = "%(levelname)-7s  %(name)s: %(message)s"
        log_date = None

    logging.basicConfig(level=log_level, format=log_fmt, datefmt=log_date, stream=sys.stderr)

    bridge = Bridge(host, port, handshake_timeout=handshake_timeout)

    async def run() -> None:
        await bridge.start()
        loop = asyncio.get_running_loop()
        stop: asyncio.Future[None] = loop.create_future()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set_result, None)
        logging.info("proxy running — Ctrl-C to stop")
        await stop
        logging.info("shutting down")
        await bridge.stop()

    asyncio.run(run())


if __name__ == "__main__":
    main()
