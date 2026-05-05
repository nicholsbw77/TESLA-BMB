#!/usr/bin/env python3
"""
Tesla BMS Cell Balancer CLI
============================
Simple command-line tool to balance cells in Tesla BMS modules.

Usage:
    python balance.py --port COM23 --addr 1
    python balance.py --port COM23 --addr 1 --minutes 30
    python balance.py --port COM23 --addr 1 --auto --threshold 10
    python balance.py --port COM23 --scan

Commands at the interactive prompt:
    r           - read and display all cell voltages
    b           - start balancing high cells automatically
    b 1 3 5     - balance specific cell groups (1-indexed)
    s           - stop all balancing
    t 30        - set timer for 30 minutes then stop
    n           - add a note to this module
    q           - quit
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
import threading
from typing import List, Optional

from tesla_bms.transport import BMSTransport
from tesla_bms.bms import TeslaBMS, ModuleReading
from tesla_bms.registers import REG_BAL_CTRL, REG_BAL_TIME


# ── ANSI colour helpers ──────────────────────────────────────────────────────
def _c(s: str, code: str) -> str:
    return f"\033[{code}m{s}\033[0m" if sys.stdout.isatty() else s

def red(s):    return _c(s, "31")
def yellow(s): return _c(s, "33")
def green(s):  return _c(s, "32")
def cyan(s):   return _c(s, "36")
def bold(s):   return _c(s, "1")


# ── Voltage display ──────────────────────────────────────────────────────────
CELL_WARN_LOW  = 3.0    # V
CELL_WARN_HIGH = 4.15   # V
TEMP_WARN      = 40.0   # °C

def _cell_bar(v: float, lo: float, hi: float, width: int = 20) -> str:
    """ASCII progress bar for a cell voltage (3.0V - 4.2V range)."""
    pct = max(0.0, min(1.0, (v - 3.0) / 1.2))
    filled = int(pct * width)
    bar = "█" * filled + "░" * (width - filled)
    return bar


def _fmt_cell(v: float, lo: float, hi: float, idx: int,
              balancing: int) -> str:
    bal_flag = _c(" ⚡", "33") if (balancing >> idx) & 1 else "  "
    bar = _cell_bar(v, lo, hi)
    txt = f"  Cell {idx+1}: {v:6.4f}V [{bar}]{bal_flag}"
    if v == lo and lo != hi:
        return red(txt)
    if v == hi and lo != hi:
        return yellow(txt)
    return txt


def print_reading(r: ModuleReading, balancing: int = 0) -> None:
    lo = r.cell_min
    hi = r.cell_max

    print()
    print(bold(f"── Module {r.address:02d} ─────────────────────────────────────────"))
    print(f"  Pack voltage : {r.module_voltage:7.3f} V")
    t1 = r.temperatures[0] if r.temperatures else float("nan")
    t2 = r.temperatures[1] if len(r.temperatures) > 1 else float("nan")
    t1s = red(f"{t1:.1f}°C") if t1 > TEMP_WARN else f"{t1:.1f}°C"
    t2s = red(f"{t2:.1f}°C") if t2 > TEMP_WARN else f"{t2:.1f}°C"
    print(f"  Temperatures : {t1s}  {t2s}")
    print(f"  Cell Δ       : {r.cell_delta*1000:.1f} mV  "
          f"(min={lo:.4f}V  max={hi:.4f}V)")
    print()
    for i, v in enumerate(r.cell_voltages):
        print(_fmt_cell(v, lo, hi, i, balancing))

    if r.status:
        if r.status.alerts:
            print(red(f"  ⚠ ALERTS : 0x{r.status.alerts:02X}"))
        if r.status.faults:
            print(red(f"  ⚠ FAULTS : 0x{r.status.faults:02X}"))
        if r.status.cov:
            print(red(f"  ⚠ OVER-VOLTAGE cells: 0x{r.status.cov:02X}"))
        if r.status.cuv:
            print(red(f"  ⚠ UNDER-VOLTAGE cells: 0x{r.status.cuv:02X}"))
    print()


# ── Balancing helpers ─────────────────────────────────────────────────────────
def calc_balance_mask(voltages: List[float],
                      threshold_mv: float = 10.0) -> int:
    """
    Return a bitmask of cells to balance.
    Balances any cell that is more than threshold_mv above the minimum.
    """
    if not voltages:
        return 0
    lo = min(voltages)
    mask = 0
    for i, v in enumerate(voltages):
        if (v - lo) * 1000 >= threshold_mv:
            mask |= (1 << i)
    return mask


def start_balance(t: BMSTransport, addr: int, mask: int,
                  duration_s: int = 0) -> None:
    """Write balancing mask to the module. duration_s=0 means run until stopped."""
    t.write_register(addr, REG_BAL_CTRL, mask & 0x3F)
    if duration_s > 0:
        # BAL_TIME register: value in minutes, max 63
        minutes = min(63, max(1, duration_s // 60))
        t.write_register(addr, REG_BAL_TIME, minutes)


def stop_balance(t: BMSTransport, addr: int) -> None:
    t.write_register(addr, REG_BAL_CTRL, 0x00)
    t.write_register(addr, REG_BAL_TIME, 0x00)


# ── Timer thread ──────────────────────────────────────────────────────────────
class BalanceTimer:
    """Runs a countdown and stops balancing when it expires."""

    def __init__(self, transport: BMSTransport, addr: int, seconds: int):
        self.transport = transport
        self.addr = addr
        self.seconds = seconds
        self.remaining = seconds
        self.active = False
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        self.active = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._stop_event.set()
        self.active = False

    def _run(self) -> None:
        start = time.monotonic()
        while not self._stop_event.is_set():
            elapsed = time.monotonic() - start
            self.remaining = max(0, self.seconds - int(elapsed))
            if self.remaining == 0:
                stop_balance(self.transport, self.addr)
                self.active = False
                print(f"\n{green('✓')} Timer expired — balancing stopped on module {self.addr:02d}")
                print("balance> ", end="", flush=True)
                break
            time.sleep(1)


# ── Interactive prompt ────────────────────────────────────────────────────────
def interactive_loop(transport: BMSTransport, bms: TeslaBMS,
                     addr: int, threshold_mv: float) -> None:
    balancing = 0
    timer: Optional[BalanceTimer] = None
    notes: List[str] = []

    print(cyan(f"\nConnected to module {addr:02d}. Type 'h' for help.\n"))

    # Initial read
    try:
        r = bms.read_module(addr)
        print_reading(r, balancing)
    except Exception as e:
        print(red(f"Initial read failed: {e}"))

    while True:
        # Show timer status in prompt
        timer_str = ""
        if timer and timer.active:
            m, s = divmod(timer.remaining, 60)
            timer_str = yellow(f" [{m:02d}:{s:02d}]")

        try:
            raw = input(f"balance{timer_str}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        parts = raw.split()
        cmd = parts[0].lower()

        # ── read ──────────────────────────────────────────────────────────────
        if cmd == "r":
            try:
                r = bms.read_module(addr)
                print_reading(r, balancing)
            except Exception as e:
                print(red(f"Read failed: {e}"))

        # ── balance ───────────────────────────────────────────────────────────
        elif cmd == "b":
            try:
                r = bms.read_module(addr)
            except Exception as e:
                print(red(f"Read failed before balance: {e}"))
                continue

            if len(parts) > 1:
                # Specific cells given (1-indexed)
                mask = 0
                for p in parts[1:]:
                    try:
                        cell = int(p) - 1
                        if 0 <= cell <= 5:
                            mask |= (1 << cell)
                    except ValueError:
                        pass
            else:
                # Auto: balance all cells above threshold
                mask = calc_balance_mask(r.cell_voltages, threshold_mv)

            if mask == 0:
                print(yellow("All cells within threshold — nothing to balance."))
                continue

            cells_balancing = [i+1 for i in range(6) if (mask >> i) & 1]
            print(f"Starting balance on cells: {cells_balancing}")
            start_balance(transport, addr, mask)
            balancing = mask
            print_reading(r, balancing)

        # ── stop ──────────────────────────────────────────────────────────────
        elif cmd == "s":
            stop_balance(transport, addr)
            balancing = 0
            if timer:
                timer.cancel()
                timer = None
            print(green("✓ Balancing stopped."))

        # ── timer ─────────────────────────────────────────────────────────────
        elif cmd == "t":
            if len(parts) < 2:
                print("Usage: t <minutes>")
                continue
            try:
                minutes = float(parts[1])
                seconds = int(minutes * 60)
            except ValueError:
                print(red("Invalid number of minutes."))
                continue

            if timer and timer.active:
                timer.cancel()

            if balancing == 0:
                print(yellow("Warning: no balancing active. Start balancing first with 'b'."))

            timer = BalanceTimer(transport, addr, seconds)
            timer.start()
            m, s = divmod(seconds, 60)
            print(green(f"✓ Timer set for {m:02d}:{s:02d}"))

        # ── auto-balance loop ─────────────────────────────────────────────────
        elif cmd == "auto":
            minutes = 60
            if len(parts) > 1:
                try:
                    minutes = float(parts[1])
                except ValueError:
                    pass
            seconds = int(minutes * 60)
            interval = 30  # re-read and update mask every 30 seconds

            print(cyan(f"Auto-balance for {minutes:.0f} min, "
                       f"re-checking every {interval}s. Ctrl-C to stop."))
            try:
                end_time = time.monotonic() + seconds
                while time.monotonic() < end_time:
                    r = bms.read_module(addr)
                    mask = calc_balance_mask(r.cell_voltages, threshold_mv)
                    if mask == 0:
                        stop_balance(transport, addr)
                        balancing = 0
                        print(green(f"\n✓ All cells balanced to within "
                                    f"{threshold_mv:.0f}mV. Stopping."))
                        break
                    start_balance(transport, addr, mask)
                    balancing = mask
                    remaining = int(end_time - time.monotonic())
                    m, s = divmod(remaining, 60)
                    print_reading(r, balancing)
                    print(cyan(f"  Remaining: {m:02d}:{s:02d} — "
                               f"sleeping {interval}s..."))
                    time.sleep(interval)
                else:
                    stop_balance(transport, addr)
                    balancing = 0
                    print(green("\n✓ Timer expired — balancing stopped."))
            except KeyboardInterrupt:
                stop_balance(transport, addr)
                balancing = 0
                print(green("\n✓ Interrupted — balancing stopped."))

        # ── switch module ─────────────────────────────────────────────────────
        elif cmd == "m":
            if len(parts) < 2:
                print(f"Known modules: {bms.known}")
                print("Usage: m <addr>")
                continue
            try:
                new_addr = int(parts[1])
                if new_addr not in bms.known:
                    print(red(f"Module {new_addr} not in known list: {bms.known}"))
                    continue
                # Stop balancing on old module first
                stop_balance(transport, addr)
                balancing = 0
                if timer:
                    timer.cancel()
                    timer = None
                addr = new_addr
                print(cyan(f"Switched to module {addr:02d}"))
                r = bms.read_module(addr)
                print_reading(r, 0)
            except ValueError:
                print(red("Invalid address."))

        # ── threshold ─────────────────────────────────────────────────────────
        elif cmd == "th":
            if len(parts) < 2:
                print(f"Current threshold: {threshold_mv:.1f} mV")
                print("Usage: th <mV>")
                continue
            try:
                threshold_mv = float(parts[1])
                print(green(f"✓ Threshold set to {threshold_mv:.1f} mV"))
            except ValueError:
                print(red("Invalid value."))

        # ── note ──────────────────────────────────────────────────────────────
        elif cmd == "n":
            note = " ".join(parts[1:]) if len(parts) > 1 else input("Note: ")
            if note:
                ts = time.strftime("%Y-%m-%d %H:%M:%S")
                notes.append(f"[{ts}] Module {addr:02d}: {note}")
                print(green("✓ Note saved."))
                # Append to a simple text file too
                log_path = Path("balance_notes.txt")
                with open(log_path, "a") as f:
                    f.write(f"[{ts}] Module {addr:02d}: {note}\n")

        # ── list notes ────────────────────────────────────────────────────────
        elif cmd == "ln":
            if not notes:
                print("No notes yet.")
            for n in notes[-20:]:
                print(f"  {n}")

        # ── list modules ──────────────────────────────────────────────────────
        elif cmd == "ls":
            print(f"Known modules: {bms.known}")

        # ── read all modules ──────────────────────────────────────────────────
        elif cmd == "ra":
            for a in bms.known:
                try:
                    r = bms.read_module(a)
                    print_reading(r, 0)
                except Exception as e:
                    print(red(f"  Module {a}: {e}"))

        # ── help ──────────────────────────────────────────────────────────────
        elif cmd in ("h", "help"):
            print(cyan("""
Commands:
  r              Read current module voltages and temps
  b              Auto-balance (cells above threshold)
  b 1 3 5        Balance specific cells (1-indexed)
  s              Stop all balancing
  t <min>        Set timer (stops balancing after N minutes)
  auto [min]     Auto-balance loop for N minutes (default 60)
  m <addr>       Switch to a different module
  th <mV>        Set balance threshold in mV (default 10)
  ls             List all known module addresses
  ra             Read all modules
  n [text]       Add a note
  ln             List notes
  q / quit       Exit (stops balancing first)
"""))

        # ── quit ──────────────────────────────────────────────────────────────
        elif cmd in ("q", "quit", "exit"):
            break

        else:
            print(f"Unknown command '{cmd}'. Type 'h' for help.")

    # Cleanup on exit
    print("\nStopping balancing before exit...")
    try:
        stop_balance(transport, addr)
    except Exception:
        pass
    if timer:
        timer.cancel()
    print(green("✓ Done. Goodbye."))


# ── Entry point ───────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="balance",
        description="Tesla BMS cell balancer CLI",
    )
    ap.add_argument("--port", required=True,
                    help="Serial port (e.g. COM23, /dev/ttyUSB0)")
    ap.add_argument("--addr", type=int, default=1,
                    help="Module address to connect to (default 1)")
    ap.add_argument("--modules", type=int, default=16,
                    help="Max modules to scan for (default 16)")
    ap.add_argument("--baud", type=int,
                    default=BMSTransport.DEFAULT_BAUD,
                    help="Baud rate (default 612500)")
    ap.add_argument("--threshold", type=float, default=10.0,
                    help="Balance threshold in mV (default 10)")
    ap.add_argument("--minutes", type=float, default=0,
                    help="Auto-start balancing for N minutes then quit")
    ap.add_argument("--no-reset", action="store_true",
                    help="Skip broadcast reset on startup")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Show debug output")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s"
    )

    print(bold(cyan("\n  Tesla BMS Cell Balancer\n")))

    transport = BMSTransport(port=args.port, baud=args.baud)
    transport.open()
    bms = TeslaBMS(transport)

    try:
        print(f"Scanning for modules on {args.port}...")
        addrs = bms.scan(max_addr=args.modules, fresh=not args.no_reset)

        if not addrs:
            print(red("No modules found. Check wiring and power."))
            return 1

        print(green(f"✓ Found {len(addrs)} module(s): {addrs}"))

        # Quick health summary
        print()
        for a in addrs:
            try:
                r = bms.read_module(a)
                status = ""
                if r.cell_delta * 1000 > 50:
                    status = red(f"  ⚠ HIGH DELTA {r.cell_delta*1000:.0f}mV")
                elif r.cell_delta * 1000 > 20:
                    status = yellow(f"  △ delta {r.cell_delta*1000:.0f}mV")
                else:
                    status = green(f"  ✓ delta {r.cell_delta*1000:.0f}mV")
                print(f"  Module {a:02d}:  {r.module_voltage:.3f}V  "
                      f"min={r.cell_min:.4f}V  max={r.cell_max:.4f}V{status}")
            except Exception as e:
                print(red(f"  Module {a:02d}: read failed — {e}"))

        # If --minutes given, run non-interactively then exit
        if args.minutes > 0:
            addr = args.addr
            seconds = int(args.minutes * 60)
            print(f"\nAuto-balance module {addr} for {args.minutes:.0f} min...")
            try:
                r = bms.read_module(addr)
                mask = calc_balance_mask(r.cell_voltages, args.threshold)
                if mask == 0:
                    print(green("All cells within threshold — nothing to do."))
                    return 0
                start_balance(transport, addr, mask, seconds)
                print(green(f"✓ Balancing started. Will stop after "
                             f"{args.minutes:.0f} min."))
                timer = BalanceTimer(transport, addr, seconds)
                timer.start()
                while timer.active:
                    time.sleep(5)
                    r = bms.read_module(addr)
                    lo, hi = r.cell_min, r.cell_max
                    print(f"  {time.strftime('%H:%M:%S')}  "
                          f"min={lo:.4f}V  max={hi:.4f}V  "
                          f"Δ={r.cell_delta*1000:.1f}mV  "
                          f"remaining={timer.remaining}s")
            except KeyboardInterrupt:
                stop_balance(transport, addr)
                print(green("\n✓ Interrupted — balancing stopped."))
            return 0

        # Interactive mode
        addr = args.addr if args.addr in addrs else addrs[0]
        interactive_loop(transport, bms, addr, args.threshold)

    finally:
        transport.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
