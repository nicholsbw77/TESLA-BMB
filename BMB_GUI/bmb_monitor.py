"""
Tesla Model S/X Gen1 (2012-2016) BMB Monitor
============================================
Reads cell voltages, temperatures, and fault status from Tesla battery
modules directly via the BMB connector using a FTDI FT232R USB-UART adapter.

Hardware needed:
  - FTDI FT232R 5V USB-UART adapter (SparkFun DEV-09716 or similar)
  - Molex 15-97-5101 connector (10-pin) + pins 39-00-0038
  - Wire map to module BMB connector:
      Red  (pin 1) = 5V      -> FTDI VCC (5V)
      Green(pin 3) = GND     -> FTDI GND
      Yellow(pin 2)= TX_out  -> FTDI RXD  (module transmits, FTDI receives)
      Blue (pin 9) = RX_in   -> FTDI TXD  (FTDI transmits, module receives)
      Gray (pin 6) = FAULT   -> optional, pull to 5V with 10kΩ

Protocol: BQ76PL536A UART bridge at 612,500 baud
  Sources: collin80/TeslaBMS (github), hackaday.io/project/10098
"""

import sys
import time
import math
import csv
import os
import datetime
import argparse

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ERROR: pyserial not installed.")
    print("  Run: pip install pyserial")
    sys.exit(1)

# ── Protocol constants ────────────────────────────────────────────────────────
BAUD_RATE    = 612500
MAX_MODULES  = 16

# Cell voltage: raw_16bit * 6.250 / 16383
CELL_V_FACTOR    = 6.250 / 16383.0
# Module voltage: raw_16bit * 33.333 / 16383
MODULE_V_FACTOR  = 33.333 / 16383.0


# ── Protocol layer — uses the verified working tesla_bms package ──────────────
from tesla_bms.transport import BMSTransport
from tesla_bms.bms import TeslaBMS


class BMBSerial:
    """Minimal compatibility wrapper. Delegates to BMSTransport for real I/O."""
    def __init__(self, port: str):
        self.transport = BMSTransport(port=port, baud=BAUD_RATE)
        self.transport.open()
        self.ser = self.transport.ser

    def close(self):
        try:
            self.transport.close()
        except Exception:
            pass


class BMBProtocol:
    """High-level protocol layer wrapping the verified tesla_bms package."""

    def __init__(self, port: str):
        self.bus = BMBSerial(port)
        self.bms = TeslaBMS(self.bus.transport)
        self.found_addrs: list[int] = []

    def close(self):
        self.bus.close()

    def wake(self):
        """Broadcast reset to wake all modules."""
        self.bms.reset_all()

    def discover(self) -> list[int]:
        """Reset + assign addresses + configure ADC/IO."""
        self.found_addrs = self.bms.scan(max_addr=MAX_MODULES, fresh=True)
        return self.found_addrs

    def read_voltages_temps(self, addr: int) -> dict | None:
        """Read 6 cell voltages + module voltage + 2 temperatures."""
        try:
            r = self.bms.read_module(addr)
            return {
                "module_v":  r.module_voltage,
                "cells_v":   list(r.cell_voltages),
                "temp1_c":   r.temperatures[0] if r.temperatures else float("nan"),
                "temp2_c":   r.temperatures[1] if len(r.temperatures) > 1 else float("nan"),
                "cells_raw": [],
            }
        except Exception as e:
            print(f"read_voltages_temps {addr}: {e}")
            return None

    def read_faults(self, addr: int) -> dict | None:
        """Read alert and fault status registers."""
        try:
            s = self.bms.read_status(addr)
            return {"alert": s.alerts, "fault": s.faults}
        except Exception:
            return None


# ── CRC kept as no-op stub for backward compatibility ─────────────────────────
def crc8(data: bytes) -> int:
    """Deprecated — protocol now handled by tesla_bms package."""
    from tesla_bms.crc import crc8 as _crc8
    return _crc8(data)


# ── Display ───────────────────────────────────────────────────────────────────
CELL_LOW_WARN  = 0.006   # flag if >6mV below pack mean
CELL_LOW_CRIT  = 0.012   # flag if >12mV below pack mean

def fmt_cell(v: float, mean: float) -> str:
    dev = v - mean
    flag = ""
    if dev < -CELL_LOW_CRIT:
        flag = " !!!"
    elif dev < -CELL_LOW_WARN:
        flag = " *  "
    return f"{v:.4f}V ({dev:+.1f}mV){flag}"

def print_header():
    print("\033[2J\033[H", end="")   # clear screen
    print("╔══════════════════════════════════════════════════════════╗")
    print("║       Tesla BMB Module Monitor  —  bench tester         ║")
    print("╚══════════════════════════════════════════════════════════╝")

def display_module(mod_num: int, data: dict, pack_mean: float):
    cells = data["cells_v"]
    spread_mv = (max(cells) - min(cells)) * 1000
    spread_flag = "  <<<" if spread_mv > 15 else ""
    print(f"\n  Module {mod_num:2d}  |  Pack V: {data['module_v']:.3f}V  "
          f"|  Spread: {spread_mv:.1f}mV{spread_flag}")
    print(f"           |  T1: {data['temp1_c']:5.1f}°C   T2: {data['temp2_c']:5.1f}°C")
    print( "  ─────────┼──────────────────────────────────────")
    for i, v in enumerate(cells):
        print(f"    Cell {i+1}  │  {fmt_cell(v, pack_mean)}")

def display_all(results: dict[int, dict]):
    """results: {module_addr: data_dict}"""
    all_cells = [v for d in results.values() for v in d["cells_v"]]
    if not all_cells:
        return
    pack_mean = sum(all_cells) / len(all_cells)
    pack_min  = min(all_cells)
    pack_max  = max(all_cells)

    print_header()
    print(f"\n  Pack mean: {pack_mean:.4f}V   Min: {pack_min:.4f}V   "
          f"Max: {pack_max:.4f}V   Spread: {(pack_max-pack_min)*1000:.1f}mV")
    print(f"  Modules: {len(results)}")

    for addr in sorted(results):
        display_module(addr, results[addr], pack_mean)

    print("\n  Legend:  * = >6mV low    !!! = >12mV low    <<< = >15mV intra-module spread")
    print(f"\n  Last updated: {datetime.datetime.now().strftime('%H:%M:%S')}  "
          f"(Ctrl+C to stop / press Enter to save CSV)")


# ── CSV logging ───────────────────────────────────────────────────────────────
def save_csv(results: dict[int, dict], module_label: str, out_dir: str):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(out_dir, f"bmb_module_{module_label}_{ts}.csv")
    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["timestamp", "module_addr", "cell", "voltage_V",
                    "module_V", "temp1_C", "temp2_C"])
        ts_str = datetime.datetime.now().isoformat()
        for addr, data in sorted(results.items()):
            for i, v in enumerate(data["cells_v"]):
                w.writerow([ts_str, addr, i + 1, f"{v:.6f}",
                             f"{data['module_v']:.4f}",
                             f"{data['temp1_c']:.2f}",
                             f"{data['temp2_c']:.2f}"])
    print(f"\n  Saved: {fname}")
    return fname


# ── Port picker ───────────────────────────────────────────────────────────────
def pick_port() -> str:
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("No COM ports found. Check USB connection.")
        sys.exit(1)
    print("\nAvailable COM ports:")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p.device}  —  {p.description}")
    if len(ports) == 1:
        print(f"\nAuto-selecting {ports[0].device}")
        return ports[0].device
    choice = input("\nEnter number: ").strip()
    return ports[int(choice)].device


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Tesla BMB Module Monitor — reads cell voltages via FTDI USB-UART"
    )
    parser.add_argument("--port",    help="COM port (e.g. COM3). Auto-detected if omitted.")
    parser.add_argument("--label",   default="test", help="Module label for CSV filename")
    parser.add_argument("--outdir",  default=r"C:\Users\admin\tesla_bms_logs",
                        help="Directory to save CSV results")
    parser.add_argument("--once",    action="store_true",
                        help="Read once and exit (no live loop)")
    parser.add_argument("--interval", type=float, default=2.0,
                        help="Refresh interval in seconds (default 2.0)")
    args = parser.parse_args()

    port = args.port or pick_port()
    label = args.label

    print(f"\nConnecting to {port} at {BAUD_RATE} baud...")
    try:
        proto = BMBProtocol(port)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("Waking modules...")
    proto.wake()

    print("Discovering modules...")
    addrs = proto.discover()
    if not addrs:
        print("\nNo modules responded. Check wiring:")
        print("  Yellow -> FTDI RXD,  Blue -> FTDI TXD")
        print("  Red -> 5V,  Green -> GND")
        print("  Module must be powered (at least 1 brick above 3V).")
        proto.close()
        sys.exit(1)

    print(f"Found {len(addrs)} module(s) at address(es): {addrs}")
    time.sleep(0.2)

    results: dict[int, dict] = {}

    def poll():
        for addr in addrs:
            data = proto.read_voltages_temps(addr)
            if data:
                results[addr] = data
            else:
                print(f"  Warning: no response from module address {addr}")

    if args.once:
        poll()
        display_all(results)
        save_csv(results, label, args.outdir)
        proto.close()
        return

    print("Starting live monitor. Press Enter to save CSV, Ctrl+C to quit.\n")
    import threading

    save_requested = threading.Event()

    def wait_for_enter():
        while True:
            input()
            save_requested.set()

    t = threading.Thread(target=wait_for_enter, daemon=True)
    t.start()

    try:
        while True:
            poll()
            display_all(results)
            if save_requested.is_set():
                save_csv(results, label, args.outdir)
                save_requested.clear()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        if results:
            ans = input("Save final readings to CSV? [y/N]: ").strip().lower()
            if ans == "y":
                save_csv(results, label, args.outdir)
    finally:
        proto.close()


if __name__ == "__main__":
    main()
