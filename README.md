# Tesla BMB Module Monitor

A PyQt6 GUI for reading, logging, and balancing Tesla Gen1 (2012–2016) battery modules via FTDI USB-UART adapters at 612,500 baud (BQ76PL536A protocol).

## Download (no Python required)

Pre-built Windows executables are attached to every [GitHub Release](https://github.com/nicholsbw77/TESLA-BMB/releases). Download `TeslaBMBMonitor.exe` and double-click — no install needed.

## Run from source

```bash
pip install -r requirements.txt
python bmb_gui.py
```

Requires Python 3.10+ and an FTDI USB-Serial adapter wired to the BMB daisy-chain.

## Creating a release

Tag any commit with a version number to trigger an automatic build and publish:

```bash
git tag v1.0.0
git push origin v1.0.0
```

GitHub Actions will build `TeslaBMBMonitor.exe` and attach it to the release automatically.

## Branches

### `master` — Single adapter, full-featured
The main branch. Connect one FTDI adapter, test modules one at a time.

**Features:**
- Single COM port dropdown
- Live cell voltage tiles (colour-coded by deviation from mean)
- Module stats: pack voltage, spread, temps, avg cell voltage
- Label & serial number fields — editable while live, never overwritten by polling
- ⚡ Balance tab — auto-balance or manually select cells, configurable threshold and timer
- 📊 Pack Summary — persistent CSV log comparing all modules across sessions

### `claude/setup-local-testing-Qx5hQ` — Multi-adapter (two or more FTDI adapters simultaneously)
Use this branch if you have **two or more FTDI adapters** monitoring separate module strings at the same time.

**Additional features over master:**
- Multi-select port list (Ctrl+click to select multiple COM ports)
- One background worker thread per adapter — all run in parallel
- All discovered modules merged into a single live table keyed by port + HW address
- Save Selected / Save All to log simultaneously across all adapters
- ⚡ Balance tab works across all connected adapters

**To use:**
```bash
git checkout claude/setup-local-testing-Qx5hQ
python bmb_gui.py
```

## Hardware

- FTDI USB-UART adapter (FT232R or clone) wired to the BMB daisy-chain TX/RX/GND
- Baud rate: 612,500 (set automatically)
- Tested on Windows with Thonny-bundled Python and system Python

## Files

| Path | Purpose |
|---|---|
| `bmb_gui.py` | Main GUI — run this |
| `BMB_GUI/` | Copies of GUI scripts + Windows `.bat` launchers |
| `tesla_bms/` | Core BMS protocol library (transport, registers, CRC) |
| `tesla_bms_tool/` | CLI tools: `balance.py`, `brick_check.py` |
| `tesla_bms_logs/` | Auto-created on first save; holds `bmb_module_log.csv` |
