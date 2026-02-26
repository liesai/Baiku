"""Terminal CLI entrypoint for Velox Engine MVP."""

from __future__ import annotations

import argparse
import asyncio

from backend.ble.ftms_client import FTMSClient
from backend.core.engine import VeloxEngine


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Velox Engine terminal MVP")
    parser.add_argument("--scan", action="store_true", help="Scan BLE devices")
    parser.add_argument(
        "--ui",
        action="store_true",
        help="Launch Linux desktop app (scan/connect/workout ERG)",
    )
    parser.add_argument(
        "--ui-web",
        action="store_true",
        help="Launch web UI (NiceGUI) for responsive dashboard",
    )
    parser.add_argument(
        "--web-host",
        default="127.0.0.1",
        help="Host bind for --ui-web",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8088,
        help="Port for --ui-web",
    )
    parser.add_argument(
        "--web-start-delay",
        type=int,
        default=10,
        help="Countdown in seconds before sending first workout target in web UI",
    )
    parser.add_argument(
        "--connect",
        nargs="?",
        const="auto",
        default=None,
        help="Connect to first FTMS device or the provided BLE address/name",
    )
    parser.add_argument("--erg", type=int, default=None, help="Set fixed ERG target in watts")
    parser.add_argument(
        "--startup-wait",
        type=float,
        default=30.0,
        help="Seconds to wait for first trainer signal before sending ERG command",
    )
    parser.add_argument(
        "--debug-ftms",
        action="store_true",
        help="Print raw FTMS payload/flags parsing for each notification",
    )
    parser.add_argument(
        "--debug-sim-ht",
        action="store_true",
        help="Simulate a home trainer (no BLE required) for debug/testing",
    )
    return parser


async def run_scan(simulate_ht: bool = False) -> int:
    client = FTMSClient(simulate_ht=simulate_ht)
    devices = await client.scan(timeout=5.0)

    if not devices:
        print("No BLE devices found")
        return 0

    for device in devices:
        ftms_flag = "FTMS" if device.has_ftms else "-"
        print(f"{device.name:<24} {device.address} RSSI={device.rssi:>4} [{ftms_flag}]")
    return 0


async def run_connect(
    connect_target: str | None,
    erg_watts: int | None,
    debug_ftms: bool,
    simulate_ht: bool,
    startup_wait: float,
) -> int:
    engine = VeloxEngine(
        debug_ftms=debug_ftms,
        simulate_ht=simulate_ht,
        startup_wait_seconds=startup_wait,
    )

    try:
        await engine.run(target=connect_target, erg_watts=erg_watts)
    except KeyboardInterrupt:
        engine.stop()
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.ui:
        from backend.ui.app import run_ui

        return run_ui(simulate_ht=args.debug_sim_ht)
    if args.ui_web:
        from backend.ui.web_app import run_web_ui

        return run_web_ui(
            simulate_ht=args.debug_sim_ht,
            host=args.web_host,
            port=args.web_port,
            start_delay_sec=max(0, int(args.web_start_delay)),
        )

    if args.scan:
        return asyncio.run(run_scan(args.debug_sim_ht))

    connect_target = args.connect
    if args.erg is not None and connect_target is None:
        connect_target = "auto"

    if connect_target is None:
        parser.print_help()
        return 1

    return asyncio.run(
        run_connect(
            connect_target,
            args.erg,
            args.debug_ftms,
            args.debug_sim_ht,
            args.startup_wait,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
