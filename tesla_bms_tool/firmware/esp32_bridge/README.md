# ESP32 USB-to-UART Bridge for Tesla BMS

This sketch turns any common ESP32 dev board into a transparent baud-rate
translator between your PC's USB port and the Tesla BMS daisy-chain bus.

```
PC (Python CLI)  ──USB──▶  ESP32  ──UART2──▶  Isolation circuit  ──▶  Tesla module
                  921600           612500
```

The ESP32 has zero protocol knowledge. It just reads bytes from one
side and writes them to the other. All BMS logic lives in the Python
`tesla_bms` package.

---

## Wiring

### ESP32 side

For a typical 30-pin "DOIT ESP32 DEVKIT v1" or any board exposing UART2:

| ESP32 pin   | Connect to                              | Notes                          |
|-------------|------------------------------------------|--------------------------------|
| GPIO 16 (RX2) | Output of isolation circuit (module's TX side)  | input to ESP32 |
| GPIO 17 (TX2) | Input of isolation circuit (module's RX side)  | output from ESP32 (3.3V TTL) |
| GND         | Common ground with module 5V supply, isolation circuit | **all grounds tied together** |
| 5V (Vin/USB) | Optional: power for isolation circuit  | only if it needs 5V |
| 3.3V         | Optional: power for isolation circuit  | only if it needs 3.3V |

**ESP32 GPIO is 3.3V tolerant, NOT 5V tolerant.** If your isolation
circuit puts out 5V on its TTL side, add a 1k/2k voltage divider or a
proper 5V→3.3V level shifter on the line into GPIO 16. Most isolation
circuits for this project (Tom's reference design, Si8642-based) are
3.3V capable so this isn't usually an issue.

### Module side (Tesla Molex 15-97-5101, 10-pin)

For a single bench module:

| Molex pin | Tesla wire | Connect to                  |
|-----------|------------|----------------------------|
| 1         | Yellow or Blue | isolation circuit "module RX" side |
| 2         | Red        | +5V from bench supply      |
| 3         | Blue or Yellow | isolation circuit "module TX" side |
| 5         | Gray       | 4.7k pull-up to +5V (optional, fault line) |
| 10        | Green      | GND (tied to ESP32 GND and bench 5V GND) |

Pins 4, 6, 7, 8, 9 — leave open for a single module. They form the
downstream side of the daisy-chain.

**Critical: the +5V on Molex pin 2 is required.** It powers the
communication side of the module's onboard isolator (Si8642). Without
it, the module is electrically deaf even if the cells are connected.

---

## Flashing

### Option A: Arduino IDE

1. **Install ESP32 board support**
   File → Preferences → Additional boards URL:
   `https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json`
   Then Tools → Board → Boards Manager → search "esp32" → install.

2. **Open** `esp32_bridge.ino` in this folder.

3. **Select your board**
   Tools → Board → ESP32 Arduino → "ESP32 Dev Module" (or whatever
   matches your specific board — most clones use this entry).

4. **Settings**
   - Upload Speed: **921600**
   - CPU Frequency: 240 MHz
   - Flash Frequency: 80 MHz
   - Partition Scheme: Default
   - Port: pick the COM/tty your ESP32 enumerates as

5. **Hold the BOOT button** on the ESP32 if your board doesn't have
   auto-bootloader, then click Upload. Release BOOT after upload starts.

6. **Verify**: open the Arduino Serial Monitor at 921600. You should
   see no output (the firmware deliberately doesn't print anything,
   so it doesn't pollute the binary protocol). The on-board LED stays
   off until bytes flow.

### Option B: arduino-cli (one-shot from a terminal)

```bash
# install ESP32 core (one time)
arduino-cli core install esp32:esp32

# from this directory
arduino-cli compile --fqbn esp32:esp32:esp32 esp32_bridge.ino
arduino-cli upload --fqbn esp32:esp32:esp32 --port /dev/ttyUSB0 esp32_bridge.ino
```

On Windows replace `/dev/ttyUSB0` with the COM port (e.g. `COM5`).

---

## First test (no Tesla module yet — loopback)

Before connecting the module, prove the bridge itself works.

1. **Loopback the ESP32**: jumper GPIO 16 directly to GPIO 17. Now
   anything the ESP32 sends on UART2 comes right back into UART2.

2. **Open a terminal at 921600** to the ESP32's USB port. PuTTY,
   minicom, or `python -m serial.tools.miniterm /dev/ttyUSB0 921600`.

3. **Type characters.** Each one should echo back to you, and the
   on-board LED should blink.

4. If that works, **remove the loopback jumper** and you're ready to
   connect the isolation circuit.

---

## Using with the Python tool

Once flashed, point the CLI at the ESP32's USB serial port and add the
`--bridge` flag — that's it:

```bash
# list ports to find the ESP32 (look for CP210x, CH340, or similar)
python -m tesla_bms.cli ports

# scan with the bridge
python -m tesla_bms.cli scan --port COM5 --bridge

# verbose - shows raw TX/RX bytes
python -m tesla_bms.cli scan --port COM5 --bridge -v

# continuous monitoring
python -m tesla_bms.cli monitor --port COM5 --bridge --interval 2
```

The `--bridge` flag tells the host to use 921600 baud instead of the
direct 612500. The actual Tesla bus rate is unchanged — the ESP32
handles that translation invisibly.

---

## Troubleshooting

**Sketch compiles but ESP32 doesn't enumerate as serial port.**
Driver issue. Most ESP32 boards use a CP2102 (Silicon Labs) or CH340
(WCH) USB-serial bridge. Install the driver from the manufacturer's
site if Windows doesn't auto-pick it up.

**Loopback test echoes garbage characters.**
Bad ground or interference between GPIO 16 and 17. Use a short jumper.

**Loopback test echoes correctly but nothing happens with the module.**
Run `python -m tesla_bms.cli scan --port ... --bridge -v` and watch
the TX bytes. If you see TX going out but no RX coming back:
- Isolation circuit power: confirm both sides are powered
- Module +5V: measure voltage at Molex pin 2 — should be 5.0V ± 0.25V
- Module ground: continuity from Molex pin 10 to ESP32 GND
- TX/RX swap: try swapping which side of the isolation circuit goes to
  GPIO 16 vs 17; "TX from module" labelling can be confusing

**Random CRC errors / partial frames.**
The default Arduino-ESP32 USB CDC buffer is small. The firmware bumps
it to 1KB which should be enough for 18-byte register reads, but if
you're still dropping bytes, try lowering the host poll rate or
running `monitor --interval 0.5` instead of faster.

**ESP32 resets when I plug in the 5V to the module.**
You're feeding the same supply or you have a short between the
isolation circuit's two domains. The whole point of isolation is that
the ESP32 side and the module side are *electrically separate* —
they should share GND only through the isolation chip, not directly.

---

## Files in this folder

```
firmware/esp32_bridge/
├── README.md           this file
└── esp32_bridge.ino    the sketch (~80 lines, mostly comments)
```
