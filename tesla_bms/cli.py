#!/usr/bin/env python3
"""
Tesla BMS CLI scanner.

Subcommands:
    ports        - list serial ports the OS can see (helps pick the FTDI)
    scan         - reset chain, assign addresses, read every module once
    monitor      - keep reading every N seconds, print a compact table
    dump         - read all 76 registers from one module (hex dump)
    raw          - send a single read or write frame for debugging

Examples:
    python -m tesla_bms.cli ports
    python -m tesla_bms.cli scan --port COM5
    python -m tesla_bms.cli scan --port /dev/ttyUSB0 --modules 16
    python -m tesla_bms.cli monitor --port COM5 --interval 2
    python -m tesla_bms.cli dump --port COM5 --addr 1
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import List

from . import BMSError, BMSTransport, TeslaBMS
from .bms import ModuleReading

# ANSI helpers (cheap, no extra dep). Disabled if not a TTY.
def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(s: str, code: str) -> str:
    if not _supports_color():
        return s
    return f"\033[{code}m{s}\033[0m"


def _fmt_cell(v: float, lo: float, hi: float) -> str:
    """Color a cell voltage based on whether it's the min/max in the module."""
    txt = f"{v:5.3f}"
    if v == lo and v != hi:
        return _c(txt, "31")   # red — weakest
    if v == hi and v != lo:
        return _c(txt, "33")   # yellow — highest
    return txt


def _print_reading(r: ModuleReading) -> None:
    cells_str = " ".join(_fmt_cell(v, r.cell_min, r.cell_max) for v in r.cell_voltages)
    fault_str = ""
    if r.status:
        bits = []
        if r.status.alerts:
            bits.append(f"alerts=0x{r.status.alerts:02X}")
        if r.status.faults:
            bits.append(f"faults=0x{r.status.faults:02X}")
        if r.status.cov:
            bits.append(f"COV=0x{r.status.cov:02X}")
        if r.status.cuv:
            bits.append(f"CUV=0x{r.status.cuv:02X}")
        if bits:
            fault_str = "  " + _c(" ".join(bits), "31")
    print(
        f"  M{r.address:02d}  "
        f"V={r.module_voltage:6.3f}  "
        f"cells [{cells_str}]  "
        f"Δ={r.cell_delta*1000:5.1f}mV  "
        f"T1={r.temperatures[0]:5.1f}°C T2={r.temperatures[1]:5.1f}°C"
        f"{fault_str}"
    )


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------
def cmd_ports(_args) -> int:
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial is required: pip install pyserial", file=sys.stderr)
        return 2
    ports = list(list_ports.comports())
    if not ports:
        print("No serial ports found.")
        return 0
    print(f"{'Device':<25} {'Description':<40} VID:PID")
    for p in ports:
        vidpid = ""
        if p.vid is not None and p.pid is not None:
            vidpid = f"{p.vid:04x}:{p.pid:04x}"
        print(f"{p.device:<25} {(p.description or ''):<40} {vidpid}")
    return 0


def _open(args) -> tuple[BMSTransport, TeslaBMS]:
    baud = _resolve_baud(args)
    transport = BMSTransport(port=args.port, baud=baud, timeout=args.timeout)
    transport.open()
    bms = TeslaBMS(transport)
    return transport, bms


def cmd_scan(args) -> int:
    transport, bms = _open(args)
    try:
        print(f"Resetting chain and assigning up to {args.modules} addresses...")
        addrs = bms.scan(max_addr=args.modules, fresh=not args.no_reset)
        print(f"Found {len(addrs)} module(s): {addrs}")
        if not addrs:
            print("No modules responded. Check wiring, isolation circuit, and 5V supply.")
            return 1
        print()
        readings = bms.read_all()
        for r in readings:
            _print_reading(r)
        print()
        # Summary
        all_cells = [v for r in readings for v in r.cell_voltages]
        if all_cells:
            print(
                f"Pack: {sum(r.module_voltage for r in readings):.2f} V across "
                f"{len(readings)} modules,  "
                f"cell min={min(all_cells):.3f}V  "
                f"max={max(all_cells):.3f}V  "
                f"Δ={(max(all_cells)-min(all_cells))*1000:.1f}mV"
            )
        return 0
    finally:
        transport.close()


def cmd_monitor(args) -> int:
    transport, bms = _open(args)
    try:
        addrs = bms.scan(max_addr=args.modules, fresh=not args.no_reset)
        if not addrs:
            print("No modules responded.")
            return 1
        try:
            while True:
                ts = time.strftime("%H:%M:%S")
                print(f"\n[{ts}] {len(addrs)} modules")
                for r in bms.read_all():
                    _print_reading(r)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nstopped.")
        return 0
    finally:
        transport.close()


def cmd_dump(args) -> int:
    transport, bms = _open(args)
    try:
        if not args.no_scan:
            bms.scan(max_addr=args.modules)
        print(f"Reading 76 registers from module 0x{args.addr:02X}...")
        reply = transport.read_register(args.addr, 0x00, 0x4C)
        data = reply.data
        for i in range(0, len(data), 16):
            chunk = data[i:i + 16]
            hex_part = " ".join(f"{b:02X}" for b in chunk)
            print(f"  0x{i:02X}: {hex_part}")
        return 0
    finally:
        transport.close()


def cmd_raw(args) -> int:
    transport, _ = _open(args)
    try:
        if args.op == "read":
            r = transport.read_register(args.addr, args.reg, args.length)
            print(f"reply: {r.raw.hex(' ')}")
            print(f"data:  {r.data.hex(' ')}")
        else:
            r = transport.write_register(args.addr, args.reg, args.value)
            print(f"echo:  {r.raw.hex(' ')}")
        return 0
    finally:
        transport.close()


# ---------------------------------------------------------------------------
# argparse
# ---------------------------------------------------------------------------
def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--port", required=True, help="serial port (e.g. COM5, /dev/ttyUSB0)")
    p.add_argument("--baud", type=int, default=None,
                   help="baud rate. Default: 612500 direct, or 921600 with --bridge")
    p.add_argument("--bridge", action="store_true",
                   help="connecting through the ESP32 bridge firmware "
                        "(sets host baud to 921600 unless --baud overrides)")
    p.add_argument("--timeout", type=float, default=0.1, help="serial timeout (s)")
    p.add_argument("--modules", type=int, default=16, help="max modules to look for")
    p.add_argument("--no-reset", action="store_true",
                   help="skip the broadcast reset before scanning")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="enable debug logging (shows TX/RX bytes)")


def _resolve_baud(args) -> int:
    if args.baud is not None:
        return args.baud
    return BMSTransport.BRIDGE_BAUD if args.bridge else BMSTransport.DEFAULT_BAUD


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="tesla-bms",
        description="Tesla BMS slave-board diagnostic CLI.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ports", help="list serial ports").set_defaults(func=cmd_ports)

    p_scan = sub.add_parser("scan", help="one-shot scan + read of all modules")
    _common(p_scan)
    p_scan.set_defaults(func=cmd_scan)

    p_mon = sub.add_parser("monitor", help="continuously read every N seconds")
    _common(p_mon)
    p_mon.add_argument("--interval", type=float, default=2.0, help="seconds between reads")
    p_mon.set_defaults(func=cmd_monitor)

    p_dump = sub.add_parser("dump", help="dump all 76 registers from one module")
    _common(p_dump)
    p_dump.add_argument("--addr", type=lambda x: int(x, 0), required=True,
                        help="module address (decimal or 0x..)")
    p_dump.add_argument("--no-scan", action="store_true",
                        help="skip address-assignment, assume module is already at addr")
    p_dump.set_defaults(func=cmd_dump)

    p_raw = sub.add_parser("raw", help="send a single raw read or write frame")
    _common(p_raw)
    p_raw.add_argument("op", choices=["read", "write"])
    p_raw.add_argument("--addr", type=lambda x: int(x, 0), required=True)
    p_raw.add_argument("--reg",  type=lambda x: int(x, 0), required=True)
    p_raw.add_argument("--length", type=int, default=1, help="bytes to read")
    p_raw.add_argument("--value", type=lambda x: int(x, 0), default=0,
                       help="byte to write")
    p_raw.set_defaults(func=cmd_raw)

    args = ap.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if getattr(args, "verbose", False) else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except BMSError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
