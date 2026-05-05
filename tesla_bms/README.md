# ESP32 USB-to-UART Bridge for Tesla BMS — MicroPython version

Same job as the Arduino sketch in `../esp32_bridge/`, but written in
MicroPython so you don't need Arduino IDE or PlatformIO.

```
PC (Python CLI)  ──USB──▶  ESP32  ──UART──▶  isolation circuit  ──▶  Tesla module
                  921600          612500
```

The script auto-detects whether you're on a classic ESP32, S3, S2,
or C3 and picks the correct UART pins for each.

---

## ⚠️ ESP32-S3-DevKitC-1 specifics — read this first

The S3-DevKitC-1 has **two USB-C ports**, side by side, labeled:

| Label | What it is | Use for this project? |
|-------|-----------|----------------------|
| **"UART"** (left) | CP2102 USB-serial chip → ESP32 UART0 (GPIO 43/44) | **YES — use this one** |
| **"USB"** (right) | Native USB peripheral on the S3 chip itself (GPIO 19/20) | No |

**Plug your USB-C cable into the port labeled "UART"** for both
flashing and running the bridge. The right port is the chip's native
USB and takes a different code path; this script targets the UART port.

If you're not sure which is which, look for the small CP2102 chip on
the PCB — it's nearer the "UART" port.

### Wiring on the S3

For the BMS side, the script uses UART1 on its dedicated pins:

| ESP32-S3 pin    | Connect to                                | Notes        |
|-----------------|-------------------------------------------|--------------|
| GPIO 17 (U1TXD) | Isolation circuit input (module's RX side) | output       |
| GPIO 18 (U1RXD) | Isolation circuit output (module's TX side)| input        |
| GND             | Common ground with isolation + 5V supply   | tie all GNDs |

For the loopback test, jumper **GPIO 17 to GPIO 18** (not 16 to 17 as
in earlier docs — that was wrong). On the S3-DevKitC-1, GPIO 16 is
U0CTS, which is reserved for UART0 flow control.

---

## What you need

* The ESP32-S3-DevKitC-1 (or any ESP32 family board)
* A USB-C cable that does **data**, not just charging — most cheap
  cables don't transmit data. If `esptool` can't find your board, this
  is the first thing to suspect.
* Python 3 on your PC
* About 5 minutes for one-time setup

You do **not** need Arduino IDE, PlatformIO, or any C toolchain.

---

## Step 1 — install esptool and mpremote (one time)

```bash
pip install esptool mpremote
```

* `esptool` flashes the MicroPython firmware to the chip.
* `mpremote` copies your `main.py` onto the chip's filesystem.

---

## Step 2 — download MicroPython firmware for ESP32-S3

For the S3-DevKitC-1, go to:
https://micropython.org/download/ESP32_GENERIC_S3/

Download the latest `.bin` (filename like
`ESP32_GENERIC_S3-<version>.bin`).

For non-S3 boards, use the matching page (`ESP32_GENERIC`,
`ESP32_GENERIC_S2`, `ESP32_GENERIC_C3`).

Save it somewhere you can find — e.g. `~/Downloads/esp32s3-firmware.bin`.

---

## Step 3 — find your ESP32's serial port

Plug the ESP32 into the **UART** port (left port on most S3 boards).
Then:

```bash
# any platform
python -m serial.tools.list_ports
```

Look for `CP2102`, `CP210x`, or `Silicon Labs CP210x`. Note the port
name — `COM5`, `/dev/ttyUSB0`, etc. We'll call it `<PORT>` below.

If you see *two* CP210x devices appear, one is for UART and one for
JTAG. The lower-numbered one is usually UART. You can confirm by
unplugging and re-plugging the cable to see which disappears.

---

## Step 4 — erase and flash MicroPython

For the **ESP32-S3**:

```bash
# erase the chip (only needed first time, or if something goes wrong)
esptool --chip esp32s3 --port <PORT> erase_flash

# flash MicroPython — note offset 0x0 for S3, NOT 0x1000
esptool --chip esp32s3 --port <PORT> --baud 460800 write_flash 0x0 ~/Downloads/esp32s3-firmware.bin
```

For the **classic ESP32**, the offset is `0x1000` instead of `0x0`:
```bash
esptool --chip esp32 --port <PORT> erase_flash
esptool --chip esp32 --port <PORT> --baud 460800 write_flash -z 0x1000 ~/Downloads/esp32-firmware.bin
```

If `esptool` can't connect, **hold the BOOT button on the ESP32 while
running the command**, release it after the dots start. On the
S3-DevKitC-1, BOOT is the small black button next to RST.

You should see something like:

```
Hash of data verified.
Leaving...
Hard resetting via RTS pin...
```

---

## Step 5 — quick REPL sanity check

After flashing, the ESP32 reboots into MicroPython. Talk to it:

```bash
mpremote connect <PORT>
```

You should see:

```
MicroPython v1.24.0 on 2024-10-25; ESP32-S3 module with ESP32-S3
Type "help()" for more information.
>>>
```

Try `2+2`, then press `Ctrl-X` to disconnect. If that worked,
MicroPython is installed correctly.

While you're here, take note of the exact line shown — `os.uname()`
will show the same string. The bridge script auto-detects from this.

---

## Step 6 — copy the bridge script

From this folder (`firmware/esp32_bridge_micropython/`):

```bash
mpremote connect <PORT> cp main.py :main.py
```

That copies `main.py` to the ESP32 as `/main.py`. MicroPython runs
`main.py` automatically at boot.

Press the EN/RST button on the board to start the bridge running.

---

## Step 7 — loopback test (do this BEFORE connecting any module)

Prove the bridge works end-to-end before risking battery hardware.

1. **Power off** the ESP32 (unplug USB-C).
2. Install a jumper wire between the two BMS-side UART pins for your board:
   * **ESP32-S3**: jumper **GPIO 17 to GPIO 18**
   * **Classic ESP32 / S2 / C3**: jumper the BMS_TX_PIN to BMS_RX_PIN
     listed in the pin reference table below.
3. Plug USB-C back into the **UART** port (left port on S3-DevKitC-1).
4. From the project root:

```bash
python -m tesla_bms.cli ports
```

Confirm the ESP32 still shows up.

5. Open a quick terminal at 921600:

```bash
python -m serial.tools.miniterm <PORT> 921600 --raw
```

6. Type any character. It should echo back, and the on-board LED
   should blink with each keystroke. Press `Ctrl-]` to exit.

If that works, **remove the loopback jumper** and you're ready to
connect the isolation circuit.

If it doesn't work, see "If the loopback test fails" below.

---

## Step 8 — use it

The Python CLI takes care of everything from here. Just add `--bridge`:

```bash
# from the project root
python -m tesla_bms.cli scan --port <PORT> --bridge

# verbose mode — shows raw TX/RX bytes
python -m tesla_bms.cli scan --port <PORT> --bridge -v

# continuous monitoring
python -m tesla_bms.cli monitor --port <PORT> --bridge --interval 2
```

---

## Pin reference (where each board's UARTs land)

| Board         | USB UART | USB TX/RX     | BMS UART | BMS TX/RX     |
|---------------|----------|---------------|----------|---------------|
| Classic ESP32 | UART0    | GPIO 1 / 3    | UART2    | GPIO 17 / 16  |
| ESP32-S3      | UART0    | GPIO 43 / 44  | UART1    | GPIO 17 / 18  |
| ESP32-S2      | UART0    | GPIO 43 / 44  | UART1    | GPIO 17 / 18  |
| ESP32-C3      | UART0    | GPIO 21 / 20  | UART1    | GPIO 5 / 4    |

(The script auto-picks these based on `os.uname().machine`. You can
override by editing the constants at the top of `main.py` if your
board uses different pins.)

---

## Troubleshooting

**"esptool can't connect" / "Failed to connect to ESP32"**
- Hold the BOOT button while starting the command, release after it begins.
- On the S3, make sure you're plugged into the **UART** port (left), not USB.
- Check it's a data USB-C cable, not charge-only.

**"mpremote connect" hangs or shows nothing**
The bridge script is running and has detached the REPL — that's expected
once `main.py` is on the chip. To get the REPL back temporarily:

1. Hold BOOT, press EN/RST, release EN/RST, release BOOT — drops into
   the bootloader and skips `main.py`.
2. Or rename `main.py` so it doesn't auto-run:
   ```bash
   mpremote connect <PORT> cp :main.py :main.py.bak
   mpremote connect <PORT> rm :main.py
   ```

**Loopback test echoes garbage characters**
Bad ground or intermittent jumper contact. Use a short, solid jumper
right on the headers.

**Loopback test fails entirely**
The bridge auto-detected the wrong board variant. Connect to the REPL
(see above) and run:
```python
import os
print(os.uname())
```
Send me what it prints and I'll fix the detection.

**Loopback works but real module reads fail**
Run with `-v` to see the actual bytes:
```bash
python -m tesla_bms.cli scan --port <PORT> --bridge -v
```
If you see TX bytes leaving but no RX coming back, the issue is on the
isolation-circuit-or-module side, not the bridge:
- Confirm 5V on Molex pin 2 (Red wire)
- Confirm continuity of GND between ESP32, isolation circuit, and module
- Try swapping the TX/RX wires going into the isolation circuit

**ESP32 resets randomly during heavy traffic**
The watchdog can fire if `main.py` is busy-looping. The script includes
`time.sleep_us(200)` in the idle path to prevent this; if you've
modified the script, make sure that's still there.

**Want to update the bridge script**
Same as install:
```bash
mpremote connect <PORT> cp main.py :main.py
```
Then hit reset on the board.

---

## Files in this folder

```
firmware/esp32_bridge_micropython/
├── README.md       this file
└── main.py         the bridge script — auto-detects board variant
```
