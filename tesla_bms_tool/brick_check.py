#!/usr/bin/env python3
"""
Tesla BMB Cell Logger & Balancer
=================================
Read cell voltages with timestamps, log to CSV, optionally auto-balance.

Use cases:
    1. Quick read — see all 6 cell voltages now
       python brick_check.py --port COM23

    2. Log a reading — append to CSV with timestamp
       python brick_check.py --port COM23 --log
       python brick_check.py --port COM23 --log --label "module_05"

    3. Auto-balance to within 2mV
       python brick_check.py --port COM23 --balance
       python brick_check.py --port COM23 --balance --target 2

    4. Continuous monitoring
       python brick_check.py --port COM23 --monitor

    5. View history of a specific module
       python brick_check.py --history --label module_05

The CSV log makes it easy to spot bricks that drift over time —
a sign of a shorted or weak cell.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from tesla_bms.transport import BMSTransport
from tesla_bms.bms import TeslaBMS
from tesla_bms.registers import REG_BAL_CTRL, REG_BAL_TIME

# ── Defaults ─────────────────────────────────────────────────────────────────
DEFAULT_LOG = Path.home() / "tesla_bms_logs" / "brick_log.csv"
DEFAULT_TARGET_MV = 2.0      # auto-balance until spread is below this
DEFAULT_BALANCE_INTERVAL = 30 # seconds between re-reads during balance
DEFAULT_BALANCE_DURATION = 60 # default minutes for balance session

CSV_HEADERS = [
    "timestamp", "label", "module_addr",
    "module_v", "cell1_v", "cell2_v", "cell3_v",
    "cell4_v", "cell5_v", "cell6_v",
    "min_v", "max_v", "spread_mv",
    "temp1_c", "temp2_c", "notes"
]


# ── ANSI colors ──────────────────────────────────────────────────────────────
def _c(s, code): return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s
def red(s):    return _c(s, "31")
def yellow(s): return _c(s, "33")
def green(s):  return _c(s, "32")
def cyan(s):   return _c(s, "36")
def bold(s):   return _c(s, "1")
def dim(s):    return _c(s, "2")


# ── CSV log file ─────────────────────────────────────────────────────────────
def ensure_log_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def append_log(path: Path, label: str, addr: int,
               cells, module_v: float,
               t1: float, t2: float, notes: str = "") -> None:
    ensure_log_file(path)
    lo, hi = min(cells), max(cells)
    spread = (hi - lo) * 1000
    row = [
        datetime.now().isoformat(timespec="seconds"),
        label,
        addr,
        f"{module_v:.4f}",
        *[f"{v:.4f}" for v in cells],
        f"{lo:.4f}",
        f"{hi:.4f}",
        f"{spread:.1f}",
        f"{t1:.1f}" if t1 == t1 else "",
        f"{t2:.1f}" if t2 == t2 else "",
        notes,
    ]
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def read_log(path: Path, label_filter: str | None = None) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if label_filter and row["label"] != label_filter:
                continue
            rows.append(row)
    return rows


# ── Display ──────────────────────────────────────────────────────────────────
def display_cells(addr: int, cells, module_v: float, t1: float, t2: float,
                  balancing_mask: int = 0, label: str = "") -> None:
    lo, hi = min(cells), max(cells)
    spread_mv = (hi - lo) * 1000

    header = f"Module {addr}"
    if label:
        header += f"  ({label})"
    print()
    print(bold(header))
    print(f"  Pack: {module_v:.3f}V    Spread: {spread_mv:.1f}mV    "
          f"T1={t1:.1f}°C  T2={t2:.1f}°C")
    print()

    for i, v in enumerate(cells):
        bal = " ⚡ BAL" if (balancing_mask >> i) & 1 else ""
        # Bar across 3.0 - 4.2V range
        pct = max(0.0, min(1.0, (v - 3.0) / 1.2))
        bar_w = 30
        filled = int(pct * bar_w)
        bar = "█" * filled + "░" * (bar_w - filled)
        line = f"  Cell {i+1}:  {v:.4f}V  [{bar}]{bal}"
        if v == lo and v != hi:
            print(red(line))
        elif v == hi and v != lo:
            print(yellow(line))
        else:
            print(line)
    print()


# ── Balancing primitives ─────────────────────────────────────────────────────
def calc_balance_mask(cells, target_mv: float) -> int:
    """Bitmask of cells more than target_mv above the lowest cell."""
    lo = min(cells)
    mask = 0
    for i, v in enumerate(cells):
        if (v - lo) * 1000 > target_mv:
            mask |= (1 << i)
    return mask


def start_balance(transport, addr: int, mask: int, minutes: int = 2) -> None:
    """Enable balancing on the masked cells.
    Keep minutes short (2) so we refresh it every 30s check cycle
    rather than risk the hardware timer expiring and stopping silently."""
    transport.write_register(addr, REG_BAL_TIME, min(minutes, 63))
    transport.write_register(addr, REG_BAL_CTRL, mask & 0x3F)


def stop_balance(transport, addr: int) -> None:
    """Stop all balancing on this module."""
    transport.write_register(addr, REG_BAL_CTRL, 0x00)
    transport.write_register(addr, REG_BAL_TIME, 0x00)


# ── Operations ───────────────────────────────────────────────────────────────
def cmd_read(args, bms: TeslaBMS, addrs: list[int]) -> int:
    """Single-shot read of all modules."""
    log_path = Path(args.logfile)

    for addr in addrs:
        try:
            r = bms.read_module(addr)
        except Exception as e:
            print(red(f"Module {addr}: read failed — {e}"))
            continue

        t1 = r.temperatures[0] if r.temperatures else float("nan")
        t2 = r.temperatures[1] if len(r.temperatures) > 1 else float("nan")
        display_cells(addr, r.cell_voltages, r.module_voltage, t1, t2,
                      label=args.label)

        if args.log:
            label = args.label or f"M{addr:02d}"
            append_log(log_path, label, addr,
                       r.cell_voltages, r.module_voltage, t1, t2,
                       notes=args.note)
            print(green(f"  ✓ Logged to {log_path}"))
    return 0


def cmd_monitor(args, bms: TeslaBMS, addrs: list[int]) -> int:
    """Continuous read loop; press Ctrl-C to stop."""
    log_path = Path(args.logfile)
    interval = args.interval
    print(cyan(f"Monitoring every {interval}s. Ctrl-C to stop."))

    try:
        while True:
            ts = datetime.now().strftime("%H:%M:%S")
            print(dim(f"\n─── {ts} ───"))
            for addr in addrs:
                try:
                    r = bms.read_module(addr)
                    t1 = r.temperatures[0] if r.temperatures else float("nan")
                    t2 = r.temperatures[1] if len(r.temperatures) > 1 else float("nan")
                    display_cells(addr, r.cell_voltages, r.module_voltage,
                                  t1, t2, label=args.label)
                    if args.log:
                        label = args.label or f"M{addr:02d}"
                        append_log(log_path, label, addr,
                                   r.cell_voltages, r.module_voltage, t1, t2)
                except Exception as e:
                    print(red(f"  Module {addr} read failed: {e}"))
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


def cmd_balance(args, bms: TeslaBMS, addrs: list[int]) -> int:
    """Auto-balance until all cells are within target_mv of each other."""
    log_path = Path(args.logfile)
    target = args.target
    duration_s = args.minutes * 60

    print(cyan(f"\nAuto-balance: target spread ≤ {target}mV, "
               f"max duration {args.minutes} min"))
    print(cyan("Re-checking every 30s. Ctrl-C to stop early."))
    print()

    end_time = time.monotonic() + duration_s
    iteration = 0
    label = args.label or f"M{addrs[0]:02d}"

    try:
        while time.monotonic() < end_time:
            iteration += 1
            ts = datetime.now().strftime("%H:%M:%S")
            remaining_s = int(end_time - time.monotonic())
            rm, rs = divmod(remaining_s, 60)
            print(bold(f"\n─── pass {iteration} @ {ts}  "
                       f"(remaining {rm:02d}:{rs:02d}) ───"))

            done = True

            for addr in addrs:
                try:
                    r = bms.read_module(addr)
                except Exception as e:
                    print(red(f"  Module {addr} read failed: {e}"))
                    continue

                cells = r.cell_voltages
                lo = min(cells)
                spread_mv = (max(cells) - lo) * 1000

                t1 = r.temperatures[0] if r.temperatures else float("nan")
                t2 = r.temperatures[1] if len(r.temperatures) > 1 else float("nan")

                if spread_mv <= target:
                    # This module done — make sure it's not balancing
                    try:
                        stop_balance(bms.t, addr)
                    except Exception:
                        pass
                    print(green(
                        f"  Module {addr}: ✓ spread {spread_mv:.1f}mV "
                        f"(within {target}mV target)"))
                    if args.log:
                        append_log(log_path, label, addr, cells,
                                   r.module_voltage, t1, t2,
                                   notes=f"balanced (spread {spread_mv:.1f}mV)")
                    continue

                done = False
                mask = calc_balance_mask(cells, target)
                cells_to_balance = [i+1 for i in range(6) if (mask >> i) & 1]

                display_cells(addr, cells, r.module_voltage, t1, t2,
                              balancing_mask=mask, label=label)
                print(yellow(f"  Spread {spread_mv:.1f}mV > {target}mV, "
                             f"balancing cells {cells_to_balance}"))

                try:
                    start_balance(bms.t, addr, mask, minutes=5)
                except Exception as e:
                    print(red(f"  Failed to start balance: {e}"))

                if args.log:
                    append_log(log_path, label, addr, cells,
                               r.module_voltage, t1, t2,
                               notes=f"balancing {cells_to_balance}")

            if done:
                print(green(f"\n✓ All modules within {target}mV target. Done."))
                break

            time.sleep(30)
        else:
            print(yellow(f"\n⏱  Time limit ({args.minutes} min) reached."))

    except KeyboardInterrupt:
        print(yellow("\nInterrupted by user."))

    finally:
        # Always stop balancing on every module before exit
        print("\nStopping all balancing...")
        for addr in addrs:
            try:
                stop_balance(bms.t, addr)
            except Exception:
                pass
        print(green("✓ All balancing stopped."))

    return 0


def cmd_history(args) -> int:
    """Print the log file history for a label."""
    log_path = Path(args.logfile)
    rows = read_log(log_path, label_filter=args.label)

    if not rows:
        if args.label:
            print(f"No entries for label '{args.label}' in {log_path}")
        else:
            print(f"No entries in {log_path}")
        return 1

    print(bold(f"\n{len(rows)} entries"
               + (f" for label '{args.label}'" if args.label else "")))
    print()

    if not args.label:
        # Show one line per entry
        print(f"{'Timestamp':<20}  {'Label':<12}  Mod  Spread  Min     Max")
        print("─" * 70)
        for row in rows:
            print(f"{row['timestamp']:<20}  {row['label']:<12}  "
                  f"{row['module_addr']:>3}  "
                  f"{row['spread_mv']:>5}mV  "
                  f"{row['min_v']:>6}  {row['max_v']:>6}")
    else:
        # Detailed view per entry
        print(f"{'Timestamp':<20}  Cell1   Cell2   Cell3   Cell4   Cell5   Cell6   "
              f"Spread  Note")
        print("─" * 110)
        for row in rows:
            cells_str = "  ".join(f"{row[f'cell{i}_v']}" for i in range(1, 7))
            note = row.get("notes", "")[:30]
            print(f"{row['timestamp']:<20}  {cells_str}  "
                  f"{row['spread_mv']:>5}mV  {note}")

        # Drift analysis
        print()
        print(bold("Drift analysis:"))
        first = rows[0]
        last = rows[-1]
        for i in range(1, 7):
            v_first = float(first[f"cell{i}_v"])
            v_last = float(last[f"cell{i}_v"])
            drift_mv = (v_last - v_first) * 1000
            symbol = ""
            if abs(drift_mv) > 20:
                symbol = red(" ⚠ SIGNIFICANT DRIFT")
            elif abs(drift_mv) > 10:
                symbol = yellow(" △")
            print(f"  Cell {i}: {v_first:.4f}V → {v_last:.4f}V  "
                  f"({drift_mv:+.1f}mV){symbol}")
    return 0


# ── Entry point ──────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="brick_check",
        description="Tesla BMB cell logger and balancer",
    )
    ap.add_argument("--port", help="Serial port (e.g. COM23)")
    ap.add_argument("--label", default="",
                    help="Label for this module/session (used in CSV)")
    ap.add_argument("--logfile", default=str(DEFAULT_LOG),
                    help=f"CSV log path (default: {DEFAULT_LOG})")
    ap.add_argument("--note", default="",
                    help="Optional note added to CSV row")
    ap.add_argument("--addr", type=int, default=0,
                    help="Specific module address (0=all found)")

    # Mode flags (mutually exclusive)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--log", action="store_true",
                   help="Read once and append to CSV (default action)")
    g.add_argument("--monitor", action="store_true",
                   help="Continuous read loop")
    g.add_argument("--balance", action="store_true",
                   help="Auto-balance until cells are within target_mv")
    g.add_argument("--history", action="store_true",
                   help="Show history from CSV log (no module needed)")

    # Balance options
    ap.add_argument("--target", type=float, default=DEFAULT_TARGET_MV,
                    help=f"Balance target spread in mV (default {DEFAULT_TARGET_MV})")
    ap.add_argument("--minutes", type=float, default=DEFAULT_BALANCE_DURATION,
                    help=f"Max balance duration in minutes (default {DEFAULT_BALANCE_DURATION})")
    ap.add_argument("--interval", type=float, default=5.0,
                    help="Seconds between reads in monitor mode (default 5)")

    args = ap.parse_args(argv)

    # History mode: no hardware needed
    if args.history:
        return cmd_history(args)

    # Everything else needs a port
    if not args.port:
        print(red("--port is required (e.g. --port COM23)"), file=sys.stderr)
        return 2

    # Connect
    print(f"Opening {args.port}...")
    transport = BMSTransport(port=args.port, baud=BMSTransport.DEFAULT_BAUD)
    transport.open()
    bms = TeslaBMS(transport)

    try:
        print("Scanning for modules...")
        found = bms.scan(max_addr=16, fresh=True)
        if not found:
            print(red("No modules responded. Check wiring and 5V supply."))
            return 1

        # Filter to single addr if requested
        addrs = [args.addr] if args.addr else found
        if args.addr and args.addr not in found:
            print(red(f"Module {args.addr} not found. Available: {found}"))
            return 1

        print(green(f"✓ Found {len(found)} module(s): {found}"))

        # Dispatch to the right mode
        if args.balance:
            return cmd_balance(args, bms, addrs)
        elif args.monitor:
            return cmd_monitor(args, bms, addrs)
        else:
            # Default: single read (with --log if requested)
            return cmd_read(args, bms, addrs)

    finally:
        # Safety: always stop balancing before disconnect
        for a in addrs if 'addrs' in dir() else []:
            try:
                stop_balance(transport, a)
            except Exception:
                pass
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
