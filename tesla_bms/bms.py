"""
High-level operations on Tesla BMS slave modules.

Wraps the byte-level transport in domain calls: scan(), reset_all(),
assign_addresses(), read_status(), read_values().
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

from .registers import (
    ADC_CONVERT_START,
    ADC_CTRL_ALL,
    ADDR_ASSIGNED_BIT,
    BROADCAST_ADDR,
    CELL_V_SCALE,
    IO_CTRL_TEMP_VSS,
    MODULE_V_SCALE,
    REG_ADC_CONVERT,
    REG_ADC_CTRL,
    REG_ADDR_CTRL,
    REG_ALERT_STATUS,
    REG_COV_FAULT,
    REG_CUV_FAULT,
    REG_DEV_STATUS,
    REG_FAULT_STATUS,
    REG_GPAI,
    REG_IO_CTRL,
    REG_RESET,
    RESET_MAGIC,
    TEMP_B,
    TEMP_R0,
    TEMP_R_REF,
    TEMP_T0,
    UNADDRESSED_ADDR,
)
from .transport import BMSError, BMSTransport, CRCError, TimeoutError_

log = logging.getLogger(__name__)


@dataclass
class ModuleStatus:
    address: int
    alerts: int = 0
    faults: int = 0
    cov: int = 0
    cuv: int = 0


@dataclass
class ModuleReading:
    address: int
    timestamp: float = field(default_factory=time.time)
    module_voltage: float = 0.0
    cell_voltages: List[float] = field(default_factory=list)   # 6 entries
    temperatures: List[float] = field(default_factory=list)    # 2 entries (°C)
    status: Optional[ModuleStatus] = None
    raw: bytes = b""

    @property
    def cell_min(self) -> float:
        return min(self.cell_voltages) if self.cell_voltages else 0.0

    @property
    def cell_max(self) -> float:
        return max(self.cell_voltages) if self.cell_voltages else 0.0

    @property
    def cell_delta(self) -> float:
        return self.cell_max - self.cell_min if self.cell_voltages else 0.0


def _thermistor_v_to_c(adc_raw: int) -> float:
    """Convert a 14-bit ADC reading to °C using the Beta equation.

    Circuit: Vcc -> R_ref(33046Ω) -> TS_pin -> NTC(10K) -> GND
    raw/16383 = NTC / (R_ref + NTC)
    Beta equation: 1/T = 1/T0 + (1/B) * ln(R/R0)

    Verified against Tesla BMB hardware:
      raw ~3740 = 25°C (room temperature)
      raw ~2650 = ~34°C (warm workshop bench)
    """
    if adc_raw <= 0 or adc_raw >= 16383:
        return float("nan")
    ratio = adc_raw / 16383.0
    if ratio >= 1.0:
        return float("nan")
    r_ntc = TEMP_R_REF * ratio / (1.0 - ratio)
    if r_ntc <= 0:
        return float("nan")
    try:
        temp_k = 1.0 / (1.0 / TEMP_T0 + (1.0 / TEMP_B) * math.log(r_ntc / TEMP_R0))
        return temp_k - 273.15
    except (ValueError, ZeroDivisionError):
        return float("nan")


class TeslaBMS:
    """High-level controller for one or more daisy-chained slave modules."""

    MAX_MODULES = 0x3E    # protocol allows 0x01..0x3E; Tesla pack uses 16

    def __init__(self, transport: BMSTransport):
        self.t = transport
        self.known: List[int] = []   # discovered addresses

    # ------------------------------------------------------------------
    # bring-up
    # ------------------------------------------------------------------
    def reset_all(self) -> None:
        """Broadcast a reset so every module forgets its address."""
        log.info("Broadcasting reset (RESET_MAGIC=0xA5)")
        try:
            self.t.write_register(BROADCAST_ADDR, REG_RESET, RESET_MAGIC)
        except BMSError as e:
            # Broadcasts often produce no clean reply; that's expected.
            log.debug("reset_all: %s (expected for broadcast)", e)
        time.sleep(0.05)

    def assign_addresses(self, count: int = 16) -> List[int]:
        """
        Walk the daisy-chain assigning sequential addresses 1..count.

        Each unaddressed module on the chain answers at address 0x00. When
        we write its address, only the first such module on the chain
        responds (the slave then propagates messages downstream), so we
        repeat for each module.
        """
        assigned: List[int] = []
        for addr in range(1, count + 1):
            try:
                self.t.write_register(
                    UNADDRESSED_ADDR,
                    REG_ADDR_CTRL,
                    addr | ADDR_ASSIGNED_BIT,
                )
                time.sleep(0.05)
                # Configure ADC and IO before attempting status read
                self.t.write_register(addr, REG_ADC_CTRL, ADC_CTRL_ALL)
                time.sleep(0.01)
                self.t.write_register(addr, REG_IO_CTRL, IO_CTRL_TEMP_VSS)
                time.sleep(0.01)
                # Confirm by reading device status from the new address.
                reply = self.t.read_register(addr, REG_DEV_STATUS, 1)
                log.info("Module at addr 0x%02X (status=0x%02X)", addr, reply.data[0])
                assigned.append(addr)
            except (TimeoutError_, CRCError) as e:
                log.info("No more modules after assigning %d (%s)", len(assigned), e)
                break
        self.known = assigned
        return assigned

    def configure_module(self, addr: int) -> None:
        """Set ADC + IO control on one module so reads are meaningful."""
        self.t.write_register(addr, REG_ADC_CTRL, ADC_CTRL_ALL)
        self.t.write_register(addr, REG_IO_CTRL, IO_CTRL_TEMP_VSS)
        # Clear any latched alert/fault bits.
        self.t.write_register(addr, REG_ALERT_STATUS, 0xFF)
        self.t.write_register(addr, REG_ALERT_STATUS, 0x00)
        self.t.write_register(addr, REG_FAULT_STATUS, 0xFF)
        self.t.write_register(addr, REG_FAULT_STATUS, 0x00)

    def configure_all(self) -> None:
        for addr in self.known:
            self.configure_module(addr)

    # ------------------------------------------------------------------
    # discovery
    # ------------------------------------------------------------------
    def scan(self, max_addr: int = 16, fresh: bool = True) -> List[int]:
        """
        Convenience: full bring-up. Resets the chain, walks addresses,
        configures everything. Returns list of discovered addresses.
        """
        if fresh:
            self.reset_all()
        addrs = self.assign_addresses(count=max_addr)
        self.configure_all()
        return addrs

    def probe(self, addr: int) -> bool:
        """Cheap aliveness check: read device status from `addr`."""
        try:
            self.t.read_register(addr, REG_DEV_STATUS, 1)
            return True
        except BMSError:
            return False

    # ------------------------------------------------------------------
    # measurements
    # ------------------------------------------------------------------
    def read_status(self, addr: int) -> ModuleStatus:
        st = self.t.read_register(addr, REG_DEV_STATUS, 1).data[0]
        al = self.t.read_register(addr, REG_ALERT_STATUS, 1).data[0]
        fl = self.t.read_register(addr, REG_FAULT_STATUS, 1).data[0]
        cov = self.t.read_register(addr, REG_COV_FAULT, 1).data[0]
        cuv = self.t.read_register(addr, REG_CUV_FAULT, 1).data[0]
        return ModuleStatus(
            address=addr, alerts=al, faults=fl, cov=cov, cuv=cuv
        )

    def read_module(self, addr: int) -> ModuleReading:
        """
        Trigger an ADC conversion and read 18 bytes starting at GPAI:
            2 bytes module voltage + 6 * 2 bytes cell voltages
            + 2 * 2 bytes thermistors
        Same layout as BMSModule::readModuleValues() in collin80/TeslaBMS.
        """
        # Kick a fresh conversion and wait for it to complete.
        self.t.write_register(addr, REG_ADC_CONVERT, ADC_CONVERT_START)
        time.sleep(0.01)

        reply = self.t.read_register(addr, REG_GPAI, 0x12)  # 18 bytes
        d = reply.data
        if len(d) != 18:
            raise BMSError(f"expected 18 bytes from GPAI, got {len(d)}")

        # Module voltage: bytes 0..1
        module_raw = (d[0] << 8) | d[1]
        module_v = module_raw * MODULE_V_SCALE

        # Cells: bytes 2..13, big-endian pairs
        cells: List[float] = []
        for i in range(6):
            hi = d[2 + i * 2]
            lo = d[3 + i * 2]
            raw = (hi << 8) | lo
            cells.append(raw * CELL_V_SCALE)

        # Temps: bytes 14..17
        t1_raw = (d[14] << 8) | d[15]
        t2_raw = (d[16] << 8) | d[17]
        temps = [_thermistor_v_to_c(t1_raw), _thermistor_v_to_c(t2_raw)]

        status = None
        try:
            status = self.read_status(addr)
        except BMSError as e:
            log.warning("could not read status for module 0x%02X: %s", addr, e)

        return ModuleReading(
            address=addr,
            module_voltage=module_v,
            cell_voltages=cells,
            temperatures=temps,
            status=status,
            raw=d,
        )

    def read_all(self, addrs: Optional[Iterable[int]] = None) -> List[ModuleReading]:
        """Read every known module in order."""
        out: List[ModuleReading] = []
        for a in (addrs if addrs is not None else self.known):
            try:
                out.append(self.read_module(a))
            except BMSError as e:
                log.error("read failed on module 0x%02X: %s", a, e)
        return out
