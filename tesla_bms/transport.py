"""
Low-level transport for the Tesla BMS slave UART bus.

The bus runs at 612500 baud, 8N1. Most FTDI chips can hit this rate via
their fractional baud-rate divisor; pyserial passes the requested rate
straight through to the driver on Windows, and on Linux you may need to
use the FTDI D2XX library if the kernel rounds the rate.

Frames:
  Read:  [(addr<<1)|0, reg, length]                (no CRC sent on the
                                                    request itself in
                                                    collin80's code)
         reply: [(addr<<1), reg, length, ...data..., crc8]
  Write: [(addr<<1)|1, reg, value, crc8]
         reply: [(addr<<1)|1, reg, value, crc8]   (echoed back)

The 8051 wrapper on the slave board echoes write frames almost verbatim,
which lets us confirm a module is present.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import serial

from .crc import crc8, verify_crc
from .registers import RW_READ, RW_WRITE

log = logging.getLogger(__name__)


class BMSError(Exception):
    pass


class CRCError(BMSError):
    pass


class TimeoutError_(BMSError):
    pass


@dataclass
class Reply:
    addr: int
    reg: int
    data: bytes          # payload only, CRC stripped
    raw: bytes           # full frame including header + CRC


class BMSTransport:
    """Thin wrapper around a pyserial port.

    Two operating modes, both selected by `baud`:

    * Direct connection to a USB-to-TTL adapter that talks the Tesla bus
      at its native 612500 baud (genuine FTDI cable, etc.). Use baud=612500.

    * Through the ESP32 bridge firmware in firmware/esp32_bridge/. The PC
      side runs at 921600 (a standard rate the USB-CDC handles cleanly)
      and the ESP32 retransmits at 612500 on its UART2. Use baud=921600.

    The protocol bytes are identical in both modes — only the host-side
    serial rate differs.
    """

    DEFAULT_BAUD = 612500       # direct FTDI / Teensy / Due
    BRIDGE_BAUD  = 921600       # via ESP32 bridge firmware

    def __init__(
        self,
        port: str,
        baud: int = DEFAULT_BAUD,
        timeout: float = 0.5,
        inter_frame_delay: float = 0.010,
    ):
        self.port_name = port
        self.baud = baud
        self.timeout = timeout
        self.inter_frame_delay = inter_frame_delay
        self.ser: Optional[serial.Serial] = None

    # -- lifecycle ---------------------------------------------------------
    def open(self) -> None:
        log.info("Opening %s at %d baud", self.port_name, self.baud)
        self.ser = serial.Serial(
            port=self.port_name,
            baudrate=self.baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout,
            write_timeout=None,   # blocking write — avoids FTDI clone timeout errors
            rtscts=False,
            xonxoff=False,
        )
        # Some FTDI clones need a moment after open before the line is stable.
        time.sleep(0.05)
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()

    def close(self) -> None:
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- raw I/O -----------------------------------------------------------
    def _write(self, data: bytes) -> None:
        assert self.ser is not None, "transport not open"
        log.debug("TX %s", data.hex(" "))
        self.ser.write(data)
        self.ser.flush()

    def _read_exact(self, n: int) -> bytes:
        assert self.ser is not None, "transport not open"
        out = bytearray()
        deadline = time.monotonic() + self.timeout * 20
        while len(out) < n and time.monotonic() < deadline:
            chunk = self.ser.read(n - len(out))
            if not chunk:
                continue
            out.extend(chunk)
        log.debug("RX %s (wanted %d)", bytes(out).hex(" "), n)
        return bytes(out)

    # -- framed I/O --------------------------------------------------------
    def write_register(self, addr: int, reg: int, value: int) -> Reply:
        """Write one byte to a register on a module. Returns the echoed reply."""
        if self.ser is None:
            raise BMSError("transport not open")

        first = ((addr & 0x7F) << 1) | RW_WRITE
        frame = bytes([first, reg & 0xFF, value & 0xFF])
        frame_with_crc = frame + bytes([crc8(frame)])

        self.ser.reset_input_buffer()
        self._write(frame_with_crc)
        time.sleep(self.inter_frame_delay)

        # Broadcast writes: no reply is guaranteed (every module would
        # collide). Caller should treat that case specially.
        if addr == 0x3F:
            time.sleep(0.005)
            junk = self.ser.read(self.ser.in_waiting or 0)
            return Reply(addr=addr, reg=reg, data=bytes([value]), raw=junk)

        # Slave echoes our 4-byte frame back, but with bit 7 of the
        # address byte SET (the "blocking bit"). This confirms the module
        # received and accepted our write. We need to clear that bit
        # before validating the CRC.
        echo = self._read_exact(4)
        if len(echo) < 4:
            raise TimeoutError_(
                f"no echo from module 0x{addr:02X} when writing reg 0x{reg:02X}"
            )
        # Strip blocking bit from echo[0] before CRC check
        echo_clean = bytes([echo[0] & 0x7F]) + echo[1:]
        if not verify_crc(echo_clean):
            raise CRCError(f"bad CRC on write echo: {echo.hex(' ')}")
        return Reply(addr=addr, reg=reg, data=echo[2:3], raw=echo)

    def read_register(self, addr: int, reg: int, length: int) -> Reply:
        """Read `length` bytes starting at `reg` on the module at `addr`."""
        if self.ser is None:
            raise BMSError("transport not open")
        if not (1 <= length <= 0x4C):
            raise ValueError("length must be 1..76")

        # Read frame: address << 1 (NO R/W bit), reg, length, NO CRC.
        # Matches collin80/TeslaBMS BMSUtil sendData(..., false).
        frame = bytes([(addr & 0x7F) << 1, reg & 0xFF, length & 0xFF])
        self.ser.reset_input_buffer()
        self._write(frame)
        time.sleep(self.inter_frame_delay)

        # Reply layout is JUST [data bytes] [CRC byte] (no header echo).
        # The CRC covers (request + data).
        # On half-duplex bus our 3-byte TX echoes back first.
        expected = length + 1
        buf = self._read_exact(len(frame) + expected)

        # Strip our own TX echo if present (half-duplex)
        if buf[:len(frame)] == frame:
            log.debug("stripping TX echo from RX")
            buf = buf[len(frame):]
            if len(buf) < expected:
                buf += self._read_exact(expected - len(buf))
        else:
            buf = buf[:expected]

        if len(buf) < expected:
            raise TimeoutError_(
                f"short read from module 0x{addr:02X} reg 0x{reg:02X}: "
                f"got {len(buf)}/{expected} bytes"
            )

        data = buf[:length]
        rx_crc = buf[length]
        # CRC covers our original request + the data bytes
        expected_crc = crc8(frame + data)
        if rx_crc != expected_crc:
            raise CRCError(
                f"bad CRC on read reply: got 0x{rx_crc:02X} want 0x{expected_crc:02X} "
                f"(data={data.hex(' ')})"
            )
        return Reply(addr=addr, reg=reg, data=data, raw=buf)
