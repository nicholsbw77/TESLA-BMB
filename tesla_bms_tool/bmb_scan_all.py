"""
Tesla BMB Full Pack Scanner
===========================
Prompts you to connect each module in turn (M01–M16), reads voltages,
then produces a final comparison table across all modules.

Usage:
    python bmb_scan_all.py [--port COM3]
"""

import sys
import time
import csv
import os
import datetime
import argparse

from bmb_monitor import BMBProtocol, display_all, crc8, save_csv, pick_port

import serial


def scan_module(proto: BMBProtocol, mod_id: str) -> dict | None:
    """Wake, discover, and do a single read from the connected module."""
    proto.bus.ser.reset_input_buffer()
    proto.wake()
    addrs = proto.discover()
    if not addrs:
        return None
    results = {}
    for addr in addrs:
        data = proto.read_voltages_temps(addr)
        if data:
            data["module_id"] = mod_id
            results[addr] = data
    return results if results else None


def summary_table(all_results: dict[str, list[float]]):
    """Print a comparison table. all_results: {mod_id: [cell1..6]}"""
    all_cells = [v for cells in all_results.values() for v in cells]
    if not all_cells:
        return
    pack_mean = sum(all_cells) / len(all_cells)

    print("\n" + "=" * 72)
    print("FULL PACK SUMMARY")
    print("=" * 72)
    print(f"{'Module':<8}", end="")
    for i in range(1, 7):
        print(f"  Cell{i}  ", end="")
    print(f"  Spread  Avg")
    print("-" * 72)

    for mod_id in sorted(all_results):
        cells = all_results[mod_id]
        spread_mv = (max(cells) - min(cells)) * 1000
        avg = sum(cells) / len(cells)
        dev_from_pack = (avg - pack_mean) * 1000

        print(f"{mod_id:<8}", end="")
        for v in cells:
            dev = v - pack_mean
            marker = "!" if dev < -0.012 else ("*" if dev < -0.006 else " ")
            print(f"  {v:.4f}{marker}", end="")
        spread_flag = " <<<" if spread_mv > 15 else "    "
        print(f"  {spread_mv:5.1f}mV{spread_flag}  {avg:.4f}V ({dev_from_pack:+.1f}mV)")

    print("-" * 72)
    print(f"\nPack mean: {pack_mean:.4f}V   "
          f"Global spread: {(max(all_cells)-min(all_cells))*1000:.1f}mV")
    print("Legend:  ! = >12mV below mean   * = >6mV below mean   <<< = >15mV module spread")


def save_summary_csv(all_data: dict, out_dir: str):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = os.path.join(out_dir, f"bmb_full_pack_scan_{ts}.csv")
    all_cells = [v for cells in all_data.values() for v in cells]
    pack_mean = sum(all_cells) / len(all_cells)

    with open(fname, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["module_id", "cell1_V", "cell2_V", "cell3_V",
                    "cell4_V", "cell5_V", "cell6_V",
                    "spread_mV", "avg_V", "dev_from_pack_mV"])
        for mod_id in sorted(all_data):
            cells = all_data[mod_id]
            spread_mv = (max(cells) - min(cells)) * 1000
            avg = sum(cells) / len(cells)
            dev = (avg - pack_mean) * 1000
            w.writerow([mod_id] + [f"{v:.6f}" for v in cells] +
                       [f"{spread_mv:.2f}", f"{avg:.6f}", f"{dev:.2f}"])
    print(f"\nSaved: {fname}")
    return fname


def main():
    parser = argparse.ArgumentParser(description="Scan all 16 Tesla BMB modules")
    parser.add_argument("--port",    help="COM port (e.g. COM3). Auto-detected if omitted.")
    parser.add_argument("--outdir",  default=r"C:\Users\admin\tesla_bms_logs",
                        help="Directory for CSV output")
    parser.add_argument("--start",   type=int, default=1,
                        help="Starting module number (default 1, resume from partway through)")
    args = parser.parse_args()

    port = args.port or pick_port()

    print(f"\nConnecting to {port}...")
    try:
        proto = BMBProtocol(port)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print("\nTesla BMB Full Pack Scanner")
    print("─" * 50)
    print("Connect each module's BMB connector in turn.")
    print("The tool will read it, then ask for the next one.")
    print("Press Ctrl+C at any time to stop and see results.\n")

    all_results: dict[str, list[float]] = {}

    try:
        for mod_num in range(args.start, 17):
            mod_id = f"M{mod_num:02d}"
            input(f"Connect module {mod_id} then press Enter (or Ctrl+C to stop)...")

            data = scan_module(proto, mod_id)
            if not data:
                print(f"  !! No response from {mod_id}. Check connections.")
                retry = input("  Retry? [y/N]: ").strip().lower()
                if retry == "y":
                    data = scan_module(proto, mod_id)

            if data:
                cells = []
                for addr, d in sorted(data.items()):
                    cells.extend(d["cells_v"])
                    print(f"  {mod_id} addr={addr}  cells: "
                          + "  ".join(f"{v:.4f}" for v in d["cells_v"])
                          + f"  T1={d['temp1_c']:.1f}°C  T2={d['temp2_c']:.1f}°C")
                spread = (max(cells) - min(cells)) * 1000
                print(f"  Spread: {spread:.1f}mV")
                all_results[mod_id] = cells
            else:
                print(f"  Skipping {mod_id}.")
                all_results[mod_id + "_NORESP"] = [0.0] * 6

    except KeyboardInterrupt:
        print("\nScan interrupted.")

    proto.close()

    if all_results:
        summary_table({k: v for k, v in all_results.items() if not k.endswith("_NORESP")})
        save_summary_csv({k: v for k, v in all_results.items() if not k.endswith("_NORESP")},
                         args.outdir)


if __name__ == "__main__":
    main()
