"""
Tesla BMB Module Monitor — Qt6 GUI
===================================
Reads cell voltages from Tesla Gen1 (2012-2016) battery modules via
FTDI USB-UART at 612,500 baud (BQ76PL536A protocol).

Results are appended to a persistent CSV log so every module tested
across all sessions is compared in the Pack Summary tab.

Run:  python bmb_gui.py
"""

import sys
import os
import csv
import math
import time
import datetime
import threading

import queue
import serial
import serial.tools.list_ports

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLabel, QPushButton, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QStatusBar, QSizePolicy, QFrame,
    QMessageBox, QStackedWidget,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QSize,
)
from PyQt6.QtGui import QColor, QFont, QPalette

# ── Config ────────────────────────────────────────────────────────────────────
LOG_CSV   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tesla_bms_logs", "bmb_module_log.csv")
BAUD_RATE = 612500

WARN_MV   =  6   # yellow flag threshold (mV below mean)
CRIT_MV   = 12   # red flag threshold
HIGH_MV   =  6   # blue flag (above mean)
SPREAD_WARN = 15  # module spread warning (mV)

CSV_FIELDS = [
    "timestamp", "module_label", "serial_num", "hw_addr",
    "cell1_V", "cell2_V", "cell3_V", "cell4_V", "cell5_V", "cell6_V",
    "module_V", "temp1_C", "temp2_C", "spread_mV", "avg_V",
]

# ── Colors ────────────────────────────────────────────────────────────────────
C_GOOD    = QColor("#1e7d34")   # dark green — within ±6mV
C_HIGH    = QColor("#1565c0")   # blue — above mean
C_WARN    = QColor("#f57f17")   # amber — 6-12mV low
C_CRIT    = QColor("#b71c1c")   # red — >12mV low
C_NEUTRAL = QColor("#37474f")   # dark grey — no comparison yet
C_BEST    = QColor("#1b5e20")   # best module row
C_WORST   = QColor("#b71c1c")   # worst module row
C_BG      = QColor("#1a1a2e")   # window background
C_PANEL   = QColor("#16213e")   # panel background
C_TEXT    = QColor("#e0e0e0")


# ═══════════════════════════════════════════════════════════════════════════════
#  BMB Protocol — uses the verified working tesla_bms package
# ═══════════════════════════════════════════════════════════════════════════════
from tesla_bms.transport import BMSTransport
from tesla_bms.bms import TeslaBMS
from tesla_bms.registers import REG_BAL_CTRL, REG_BAL_TIME

CELL_V_FACTOR    = 6.250  / 16383.0
MODULE_V_FACTOR  = 33.333 / 16383.0
SH_A, SH_B, SH_C = 0.0007610373573, 0.0002728524832, 0.0000001022822735
TEMP_REF_R = 10000.0
TEMP_ADC_MAX = 16383.0


def raw_to_celsius(raw: int) -> float:
    if raw <= 0 or raw >= TEMP_ADC_MAX:
        return float("nan")
    ntc_r = TEMP_REF_R * raw / (TEMP_ADC_MAX - raw)
    try:
        ln_r = math.log(ntc_r)
        return 1.0 / (SH_A + SH_B * ln_r + SH_C * ln_r ** 3) - 273.15
    except (ValueError, ZeroDivisionError):
        return float("nan")


class BMBBus:
    """Adapter that exposes the GUI's expected interface but delegates to
    the verified working tesla_bms package underneath."""

    def __init__(self, port: str):
        self.transport = BMSTransport(port=port, baud=BAUD_RATE)
        self.transport.open()
        self.bms = TeslaBMS(self.transport)
        self.ser = self.transport.ser  # for code that pokes ser directly

    def close(self):
        try:
            self.transport.close()
        except Exception:
            pass

    def balance(self, addr: int, mask: int, duration_s: int = 0) -> None:
        self.transport.write_register(addr, REG_BAL_CTRL, mask & 0x3F)
        if duration_s > 0:
            self.transport.write_register(addr, REG_BAL_TIME, min(63, max(1, duration_s // 60)))

    def stop_balance(self, addr: int) -> None:
        self.transport.write_register(addr, REG_BAL_CTRL, 0x00)
        self.transport.write_register(addr, REG_BAL_TIME, 0x00)

    def wake(self):
        # Broadcast reset wakes everything cleanly
        self.bms.reset_all()

    def discover(self) -> list[int]:
        # scan() does reset + assign + configure all in one call
        return self.bms.scan(max_addr=16, fresh=True)

    def read_module(self, addr: int) -> dict | None:
        try:
            r = self.bms.read_module(addr)
            return {
                "hw_addr":  addr,
                "module_v": r.module_voltage,
                "cells":    list(r.cell_voltages),
                "temp1":    r.temperatures[0] if r.temperatures else float("nan"),
                "temp2":    r.temperatures[1] if len(r.temperatures) > 1 else float("nan"),
            }
        except Exception as e:
            print(f"read_module {addr}: {e}")
            return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Worker thread
# ═══════════════════════════════════════════════════════════════════════════════
class BMBWorker(QThread):
    # data_ready carries port name so the UI can key rows by (port, addr)
    data_ready       = pyqtSignal(str, dict)
    status_msg       = pyqtSignal(str)
    connect_done     = pyqtSignal(str, list)   # port, list of hw addrs found
    error            = pyqtSignal(str, str)    # port, message
    balancing_changed = pyqtSignal(str, int, int)  # port, addr, mask

    def __init__(self, port: str):
        super().__init__()
        self.port      = port
        self.running   = False
        self.bus: BMBBus | None = None
        self.addrs: list[int] = []
        self._cmd_queue: queue.Queue = queue.Queue()
        self._balancing: dict[int, int] = {}   # addr -> active mask

    # ── commands (safe to call from any thread) ───────────────────────────────
    def cmd_balance(self, addr: int, mask: int, duration_s: int = 0) -> None:
        self._cmd_queue.put(("balance", addr, mask, duration_s))

    def cmd_stop_balance(self, addr: int) -> None:
        self._cmd_queue.put(("stop", addr))

    def cmd_stop_all_balance(self) -> None:
        for addr in list(self._balancing.keys()):
            self._cmd_queue.put(("stop", addr))

    def _process_commands(self) -> None:
        while not self._cmd_queue.empty():
            try:
                cmd = self._cmd_queue.get_nowait()
            except queue.Empty:
                break
            if cmd[0] == "balance":
                _, addr, mask, duration_s = cmd
                try:
                    self.bus.balance(addr, mask, duration_s)
                    self._balancing[addr] = mask
                    self.balancing_changed.emit(self.port, addr, mask)
                except Exception as e:
                    self.status_msg.emit(f"[{self.port}] Balance error addr {addr}: {e}")
            elif cmd[0] == "stop":
                _, addr = cmd
                try:
                    self.bus.stop_balance(addr)
                    self._balancing[addr] = 0
                    self.balancing_changed.emit(self.port, addr, 0)
                except Exception as e:
                    self.status_msg.emit(f"[{self.port}] Stop-balance error addr {addr}: {e}")

    def run(self):
        try:
            self.bus = BMBBus(self.port)
        except Exception as e:
            self.error.emit(self.port, f"Cannot open {self.port}: {e}")
            return

        self.status_msg.emit(f"[{self.port}] Waking modules…")
        self.bus.wake()

        self.status_msg.emit(f"[{self.port}] Discovering modules…")
        self.addrs = self.bus.discover()
        if not self.addrs:
            self.error.emit(self.port, f"[{self.port}] No BMB modules responded. Check wiring.")
            self.bus.close()
            return

        self.connect_done.emit(self.port, self.addrs)
        self.status_msg.emit(f"[{self.port}] Connected — {len(self.addrs)} module(s) at addr {self.addrs}")
        self.running = True

        while self.running:
            self._process_commands()
            for addr in self.addrs:
                data = self.bus.read_module(addr)
                if data:
                    self.data_ready.emit(self.port, data)
                else:
                    self.status_msg.emit(f"[{self.port}] No response addr {addr}")
            time.sleep(2.0)

    def stop(self):
        self.running = False
        if self.bus:
            # stop any active balancing before closing
            for addr in list(self._balancing.keys()):
                try:
                    self.bus.stop_balance(addr)
                except Exception:
                    pass
            self.bus.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  CSV helpers
# ═══════════════════════════════════════════════════════════════════════════════
def ensure_csv():
    if not os.path.exists(LOG_CSV):
        os.makedirs(os.path.dirname(LOG_CSV), exist_ok=True)
        with open(LOG_CSV, "w", newline="") as f:
            csv.writer(f).writerow(CSV_FIELDS)


def append_csv(row: dict):
    ensure_csv()
    with open(LOG_CSV, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writerow(row)


def load_csv() -> list[dict]:
    ensure_csv()
    with open(LOG_CSV, newline="") as f:
        return list(csv.DictReader(f))


# ═══════════════════════════════════════════════════════════════════════════════
#  Cell voltage display widget
# ═══════════════════════════════════════════════════════════════════════════════
class CellWidget(QFrame):
    def __init__(self, cell_num: int):
        super().__init__()
        self.cell_num = cell_num
        self.setMinimumSize(QSize(130, 80))
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet("border-radius: 6px; padding: 4px;")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        lay.setSpacing(2)

        self.lbl_num = QLabel(f"Cell {cell_num}")
        self.lbl_num.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self.lbl_num.font()
        f.setPointSize(9)
        self.lbl_num.setFont(f)

        self.lbl_volt = QLabel("—")
        self.lbl_volt.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fv = self.lbl_volt.font()
        fv.setPointSize(16)
        fv.setBold(True)
        self.lbl_volt.setFont(fv)

        self.lbl_dev = QLabel("")
        self.lbl_dev.setAlignment(Qt.AlignmentFlag.AlignCenter)
        fd = self.lbl_dev.font()
        fd.setPointSize(9)
        self.lbl_dev.setFont(fd)

        lay.addWidget(self.lbl_num)
        lay.addWidget(self.lbl_volt)
        lay.addWidget(self.lbl_dev)

        self._set_color(C_NEUTRAL)

    def _set_color(self, color: QColor):
        r, g, b = color.red(), color.green(), color.blue()
        self.setStyleSheet(
            f"background-color: rgb({r},{g},{b}); border-radius: 6px; "
            f"color: white; border: 1px solid rgba(255,255,255,0.15);"
        )

    def update_value(self, voltage: float, deviation_mv: float):
        self.lbl_volt.setText(f"{voltage:.4f}V")
        sign = "+" if deviation_mv >= 0 else ""
        self.lbl_dev.setText(f"{sign}{deviation_mv:.1f} mV")

        if deviation_mv > HIGH_MV:
            self._set_color(C_HIGH)
        elif deviation_mv < -CRIT_MV:
            self._set_color(C_CRIT)
        elif deviation_mv < -WARN_MV:
            self._set_color(C_WARN)
        else:
            self._set_color(C_GOOD)

    def clear(self):
        self.lbl_volt.setText("—")
        self.lbl_dev.setText("")
        self._set_color(C_NEUTRAL)


# ═══════════════════════════════════════════════════════════════════════════════
#  Test Module tab
#  • 1 module  → tile view (original look)
#  • 2+ modules on one chain → table view (one row per module)
# ═══════════════════════════════════════════════════════════════════════════════
class TestTab(QWidget):
    MULTI_COLS = ["HW Addr", "C1", "C2", "C3", "C4", "C5", "C6",
                  "Spread mV", "Avg V", "Module V", "Temp1 °C", "Temp2 °C",
                  "Label", "Serial"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: BMBWorker | None = None
        self._last_data: dict | None = None
        self._modules: dict[tuple, dict] = {}   # (port, addr) → entry
        self._multi_mode = False
        self._build_ui()

    def get_workers(self) -> dict[str, BMBWorker]:
        return {self._worker.port: self._worker} if self._worker else {}

    def get_modules(self) -> dict[tuple, dict]:
        return self._modules

    # ── UI construction ───────────────────────────────────────────────────────
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Connection bar (always visible) ───────────────────────────────────
        conn_box = QGroupBox("Connection")
        conn_lay = QHBoxLayout(conn_box)

        self.cmb_port = QComboBox()
        self.cmb_port.setMinimumWidth(120)
        self._refresh_ports()

        btn_refresh = QPushButton("↻")
        btn_refresh.setFixedWidth(32)
        btn_refresh.setToolTip("Refresh port list")
        btn_refresh.clicked.connect(self._refresh_ports)

        self.btn_connect = QPushButton("Connect")
        self.btn_connect.setCheckable(True)
        self.btn_connect.clicked.connect(self._toggle_connect)
        self.btn_connect.setMinimumWidth(90)

        self.lbl_hw_addr = QLabel("HW addr: —")
        self.lbl_hw_addr.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        conn_lay.addWidget(QLabel("Port:"))
        conn_lay.addWidget(self.cmb_port)
        conn_lay.addWidget(btn_refresh)
        conn_lay.addSpacing(16)
        conn_lay.addWidget(self.btn_connect)
        conn_lay.addSpacing(16)
        conn_lay.addWidget(self.lbl_hw_addr)
        conn_lay.addStretch()
        root.addWidget(conn_box)

        # ── Stacked widget: page 0 = single, page 1 = multi ──────────────────
        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_single_page())
        self.stack.addWidget(self._build_multi_page())
        root.addWidget(self.stack)

    def _build_single_page(self) -> QWidget:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.setContentsMargins(0, 0, 0, 0)

        # Module identity
        id_box = QGroupBox("Module Identity")
        id_lay = QHBoxLayout(id_box)
        self.txt_label = QLineEdit()
        self.txt_label.setPlaceholderText("e.g. M05  or  spare-1")
        self.txt_label.setMinimumWidth(130)
        self.txt_label.textEdited.connect(self._on_label_edited)
        self.txt_serial = QLineEdit()
        self.txt_serial.setPlaceholderText("serial / part number (optional)")
        self.txt_serial.setMinimumWidth(200)
        self.txt_serial.textEdited.connect(self._on_serial_edited)
        id_lay.addWidget(QLabel("Label:"))
        id_lay.addWidget(self.txt_label)
        id_lay.addSpacing(20)
        id_lay.addWidget(QLabel("Serial / Notes:"))
        id_lay.addWidget(self.txt_serial)
        id_lay.addStretch()
        lay.addWidget(id_box)

        # Cell voltage tiles
        cells_box = QGroupBox("Cell Voltages")
        cells_lay = QHBoxLayout(cells_box)
        cells_lay.setSpacing(8)
        self.cell_widgets = []
        for i in range(1, 7):
            cw = CellWidget(i)
            self.cell_widgets.append(cw)
            cells_lay.addWidget(cw)
        lay.addWidget(cells_box)

        # Module stats + save button
        stats_box = QGroupBox("Module Stats")
        stats_lay = QHBoxLayout(stats_box)
        stats_lay.setSpacing(30)

        def stat_pair(label):
            lbl = QLabel(label + ":")
            val = QLabel("—")
            f = val.font(); f.setPointSize(13); f.setBold(True)
            val.setFont(f)
            return lbl, val

        lbl_mv, self.lbl_mod_v  = stat_pair("Pack V")
        lbl_sp, self.lbl_spread = stat_pair("Spread")
        lbl_t1, self.lbl_temp1  = stat_pair("Temp 1")
        lbl_t2, self.lbl_temp2  = stat_pair("Temp 2")
        lbl_av, self.lbl_avg    = stat_pair("Avg Cell")

        for lbl, val in [(lbl_mv, self.lbl_mod_v), (lbl_sp, self.lbl_spread),
                         (lbl_t1, self.lbl_temp1), (lbl_t2, self.lbl_temp2),
                         (lbl_av, self.lbl_avg)]:
            pair = QVBoxLayout()
            pair.addWidget(lbl); pair.addWidget(val)
            stats_lay.addLayout(pair)
        stats_lay.addStretch()

        self.btn_save = QPushButton("💾  Save Reading to Log")
        self.btn_save.setEnabled(False)
        self.btn_save.setMinimumHeight(40)
        self.btn_save.setMinimumWidth(180)
        self.btn_save.clicked.connect(self._save_single)
        stats_lay.addWidget(self.btn_save)
        lay.addWidget(stats_box)
        lay.addStretch()
        return page

    def _build_multi_page(self) -> QWidget:
        page = QWidget()
        lay  = QVBoxLayout(page)
        lay.setSpacing(10)
        lay.setContentsMargins(0, 0, 0, 0)

        tbl_box = QGroupBox("Live Readings — multiple modules detected on chain")
        tbl_lay = QVBoxLayout(tbl_box)

        self.multi_table = QTableWidget()
        self.multi_table.setColumnCount(len(self.MULTI_COLS))
        self.multi_table.setHorizontalHeaderLabels(self.MULTI_COLS)
        self.multi_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        self.multi_table.horizontalHeader().setStretchLastSection(True)
        self.multi_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.multi_table.setAlternatingRowColors(True)
        self.multi_table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked |
            QTableWidget.EditTrigger.SelectedClicked)
        self.multi_table.itemChanged.connect(self._on_multi_table_edit)
        tbl_lay.addWidget(self.multi_table)

        btn_row = QHBoxLayout()
        self.btn_save_sel = QPushButton("💾  Save Selected")
        self.btn_save_sel.setEnabled(False)
        self.btn_save_sel.setMinimumHeight(36)
        self.btn_save_sel.clicked.connect(self._save_selected)
        self.btn_save_all = QPushButton("💾  Save All")
        self.btn_save_all.setEnabled(False)
        self.btn_save_all.setMinimumHeight(36)
        self.btn_save_all.clicked.connect(self._save_all)
        btn_row.addStretch()
        btn_row.addWidget(self.btn_save_sel)
        btn_row.addWidget(self.btn_save_all)
        tbl_lay.addLayout(btn_row)
        lay.addWidget(tbl_box)
        return page

    # ── Ports ─────────────────────────────────────────────────────────────────
    def _refresh_ports(self):
        self.cmb_port.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            self.cmb_port.addItem(f"{p.device}  {p.description[:40]}", p.device)
        if not ports:
            self.cmb_port.addItem("No ports found", "")

    # ── Connect / disconnect ──────────────────────────────────────────────────
    def _toggle_connect(self, checked: bool):
        if checked:
            port = self.cmb_port.currentData()
            if not port:
                self.btn_connect.setChecked(False)
                return
            self._start_worker(port)
            self.btn_connect.setText("Disconnect")
        else:
            self._stop_worker()
            self.btn_connect.setText("Connect")
            self.lbl_hw_addr.setText("HW addr: —")
            self._modules.clear()
            self._multi_mode = False
            self.stack.setCurrentIndex(0)
            self.multi_table.setRowCount(0)
            for cw in self.cell_widgets:
                cw.clear()

    def _start_worker(self, port: str):
        self._worker = BMBWorker(port)
        self._worker.data_ready.connect(self._on_data)
        self._worker.status_msg.connect(self._on_status)
        self._worker.connect_done.connect(self._on_connected)
        self._worker.error.connect(self._on_error)
        self._worker.balancing_changed.connect(self._on_balancing_changed)
        self._worker.start()

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        self.btn_save.setEnabled(False)
        self.btn_save_sel.setEnabled(False)
        self.btn_save_all.setEnabled(False)

    # ── Worker signals ────────────────────────────────────────────────────────
    def _on_connected(self, port: str, addrs: list[int]):
        self.lbl_hw_addr.setText(f"HW addr: {addrs}")
        if len(addrs) > 1:
            self._multi_mode = True
            self.stack.setCurrentIndex(1)
            self.multi_table.setRowCount(len(addrs))
            self.btn_save_sel.setEnabled(True)
            self.btn_save_all.setEnabled(True)
        else:
            self._multi_mode = False
            self.stack.setCurrentIndex(0)
            self.btn_save.setEnabled(True)
            if self.txt_label.text() == "" and addrs:
                self.txt_label.setText(f"addr{addrs[0]}")

    def _on_label_edited(self, text: str):
        for entry in self._modules.values():
            entry["label"] = text

    def _on_serial_edited(self, text: str):
        for entry in self._modules.values():
            entry["serial"] = text

    def _on_data(self, port: str, data: dict):
        # update shared module state
        key = (port, data["hw_addr"])
        existing = self._modules.get(key, {})
        self._modules[key] = {
            "port":     port,
            "data":     data,
            "label":    existing.get("label", f"addr{data['hw_addr']}"),
            "serial":   existing.get("serial", ""),
            "bal_mask": existing.get("bal_mask", 0),
        }

        if self._multi_mode:
            self._refresh_multi_row(key)
        else:
            self._last_data = data
            self._refresh_single(data)

        main = self.window()
        if hasattr(main, "balance_tab"):
            main.balance_tab.refresh_row(port, data["hw_addr"])

    def _refresh_single(self, data: dict):
        cells = data["cells"]
        mean  = sum(cells) / len(cells)
        for cw, v in zip(self.cell_widgets, cells):
            cw.update_value(v, (v - mean) * 1000)
        spread_mv = (max(cells) - min(cells)) * 1000
        self.lbl_spread.setText(f"{spread_mv:.1f} mV")
        self.lbl_spread.setStyleSheet(
            f"color: {'#e53935' if spread_mv > SPREAD_WARN else '#43a047'};")
        self.lbl_mod_v.setText(f"{data['module_v']:.3f} V")
        self.lbl_avg.setText(f"{mean:.4f} V")
        t1, t2 = data["temp1"], data["temp2"]
        self.lbl_temp1.setText("—" if math.isnan(t1) else f"{t1:.1f} °C")
        self.lbl_temp2.setText("—" if math.isnan(t2) else f"{t2:.1f} °C")

    def _refresh_multi_row(self, key: tuple):
        keys = list(self._modules.keys())
        if key not in keys:
            return
        row_i = keys.index(key)
        if self.multi_table.rowCount() < len(keys):
            self.multi_table.setRowCount(len(keys))

        self.multi_table.blockSignals(True)
        entry = self._modules[key]
        data  = entry["data"]
        cells = data["cells"]
        mean  = sum(cells) / len(cells)
        spread_mv = (max(cells) - min(cells)) * 1000
        t1, t2 = data["temp1"], data["temp2"]

        def mk(text, editable=False):
            it = QTableWidgetItem(str(text))
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if not editable:
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            return it

        col_vals = [
            str(data["hw_addr"]),
            *[f"{v:.4f}" for v in cells],
            f"{spread_mv:.1f}",
            f"{mean:.4f}",
            f"{data['module_v']:.3f}",
            "—" if math.isnan(t1) else f"{t1:.1f}",
            "—" if math.isnan(t2) else f"{t2:.1f}",
            entry["label"],
            entry["serial"],
        ]
        editable_cols = {len(col_vals) - 2, len(col_vals) - 1}  # Label, Serial
        for col_i, val in enumerate(col_vals):
            self.multi_table.setItem(row_i, col_i, mk(val, col_i in editable_cols))

        # colour cells C1-C6 (cols 1-6)
        for ci, v in enumerate(cells):
            dev = (v - mean) * 1000
            it  = self.multi_table.item(row_i, 1 + ci)
            if dev < -CRIT_MV:
                it.setBackground(QColor("#7f0000"))
            elif dev < -WARN_MV:
                it.setBackground(QColor("#7f4000"))
            elif dev > HIGH_MV:
                it.setBackground(QColor("#0d3b6e"))
            else:
                it.setBackground(QColor("#1e4d2b"))

        # colour spread col (7)
        sp_it = self.multi_table.item(row_i, 7)
        if sp_it and spread_mv > SPREAD_WARN:
            sp_it.setBackground(QColor("#7f4000"))

        self.multi_table.blockSignals(False)

    def _on_multi_table_edit(self, item: QTableWidgetItem):
        col  = item.column()
        row  = item.row()
        keys = list(self._modules.keys())
        if row >= len(keys):
            return
        key = keys[row]
        label_col  = len(self.MULTI_COLS) - 2
        serial_col = len(self.MULTI_COLS) - 1
        if col == label_col:
            self._modules[key]["label"] = item.text()
        elif col == serial_col:
            self._modules[key]["serial"] = item.text()

    def _on_balancing_changed(self, port: str, addr: int, mask: int):
        key = (port, addr)
        if key in self._modules:
            self._modules[key]["bal_mask"] = mask
        main = self.window()
        if hasattr(main, "balance_tab"):
            main.balance_tab.on_balancing_changed(port, addr, mask)

    def _on_status(self, msg: str):
        window = self.window()
        if hasattr(window, "statusBar"):
            window.statusBar().showMessage(msg, 5000)

    def _on_error(self, port: str, msg: str):
        self.btn_connect.setChecked(False)
        self.btn_connect.setText("Connect")
        self._stop_worker()
        QMessageBox.critical(self, "Connection Error", msg)

    # ── Save helpers ──────────────────────────────────────────────────────────
    def _build_csv_row(self, key: tuple) -> dict:
        entry  = self._modules[key]
        data   = entry["data"]
        cells  = data["cells"]
        spread = (max(cells) - min(cells)) * 1000
        avg    = sum(cells) / len(cells)
        t1, t2 = data["temp1"], data["temp2"]
        return {
            "timestamp":    datetime.datetime.now().isoformat(timespec="seconds"),
            "module_label": entry["label"] or "unlabeled",
            "serial_num":   entry["serial"],
            "hw_addr":      data["hw_addr"],
            "cell1_V":      f"{cells[0]:.6f}",
            "cell2_V":      f"{cells[1]:.6f}",
            "cell3_V":      f"{cells[2]:.6f}",
            "cell4_V":      f"{cells[3]:.6f}",
            "cell5_V":      f"{cells[4]:.6f}",
            "cell6_V":      f"{cells[5]:.6f}",
            "module_V":     f"{data['module_v']:.4f}",
            "temp1_C":      "nan" if math.isnan(t1) else f"{t1:.2f}",
            "temp2_C":      "nan" if math.isnan(t2) else f"{t2:.2f}",
            "spread_mV":    f"{spread:.2f}",
            "avg_V":        f"{avg:.6f}",
        }

    def _notify_summary(self):
        main = self.window()
        if hasattr(main, "summary_tab"):
            main.summary_tab.refresh()

    def _save_single(self):
        if not self._last_data:
            return
        keys = list(self._modules.keys())
        if not keys:
            return
        # single mode always has exactly one module
        row = self._build_csv_row(keys[0])
        # honour whatever is in the label/serial fields right now
        row["module_label"] = self.txt_label.text().strip() or "unlabeled"
        row["serial_num"]   = self.txt_serial.text().strip()
        append_csv(row)
        msg = f"Saved module '{row['module_label']}' to log."
        self._on_status(msg)
        self._notify_summary()
        QMessageBox.information(self, "Saved", msg)

    def _save_selected(self):
        sel_rows = {idx.row() for idx in self.multi_table.selectedIndexes()}
        keys = list(self._modules.keys())
        saved = 0
        for row_i in sorted(sel_rows):
            if row_i < len(keys):
                append_csv(self._build_csv_row(keys[row_i]))
                saved += 1
        if saved:
            self._on_status(f"Saved {saved} module(s) to log.")
            self._notify_summary()
            QMessageBox.information(self, "Saved", f"Saved {saved} module(s) to log.")

    def _save_all(self):
        keys = list(self._modules.keys())
        for key in keys:
            append_csv(self._build_csv_row(key))
        self._on_status(f"Saved {len(keys)} module(s) to log.")
        self._notify_summary()
        QMessageBox.information(self, "Saved", f"Saved {len(keys)} module(s) to log.")


# ═══════════════════════════════════════════════════════════════════════════════
#  Pack Summary tab
# ═══════════════════════════════════════════════════════════════════════════════
class SummaryTab(QWidget):
    COLS = ["Label", "Date / Time", "Serial", "HW Addr",
            "C1", "C2", "C3", "C4", "C5", "C6",
            "Spread mV", "Avg V", "Dev mV", "Temp1 °C", "Temp2 °C"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        # Stats bar
        self.lbl_stats = QLabel("No data yet.")
        self.lbl_stats.setAlignment(Qt.AlignmentFlag.AlignCenter)
        f = self.lbl_stats.font()
        f.setPointSize(11)
        self.lbl_stats.setFont(f)
        lay.addWidget(self.lbl_stats)

        # Legend
        legend = QHBoxLayout()
        for color, text in [
            ("#1b5e20", "Best module"),
            ("#b71c1c", "Worst module"),
            ("#f57f17", f">{WARN_MV}mV low"),
            ("#b71c1c", f">{CRIT_MV}mV low"),
            ("#1565c0", f">{HIGH_MV}mV high"),
        ]:
            dot = QLabel("■ " + text)
            dot.setStyleSheet(f"color: {color}; font-size: 10px;")
            legend.addWidget(dot)
        legend.addStretch()

        btn_refresh = QPushButton("⟳  Refresh")
        btn_refresh.clicked.connect(self.refresh)
        legend.addWidget(btn_refresh)
        lay.addLayout(legend)

        # Table
        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        lay.addWidget(self.table)

    def refresh(self):
        rows = load_csv()
        if not rows:
            self.lbl_stats.setText("No modules logged yet.")
            self.table.setRowCount(0)
            return

        # Compute pack mean across all saved readings
        all_avgs = []
        for r in rows:
            try:
                all_avgs.append(float(r["avg_V"]))
            except (ValueError, KeyError):
                pass
        pack_mean = sum(all_avgs) / len(all_avgs) if all_avgs else 0.0

        # Find best/worst by avg_V
        valid = [(i, float(r["avg_V"])) for i, r in enumerate(rows)
                 if r.get("avg_V")]
        best_idx  = max(valid, key=lambda x: x[1])[0] if valid else -1
        worst_idx = min(valid, key=lambda x: x[1])[0] if valid else -1

        spread_all = []
        for r in rows:
            try:
                spread_all.append(float(r["spread_mV"]))
            except (ValueError, KeyError):
                pass

        self.lbl_stats.setText(
            f"Modules logged: {len(rows)}   |   "
            f"Pack mean: {pack_mean:.4f} V   |   "
            f"Avg spread: {sum(spread_all)/len(spread_all):.1f} mV" if spread_all else ""
        )

        # Sort rows by label then timestamp so same module's readings are grouped
        rows.sort(key=lambda r: (r.get("module_label", "").lower(),
                                  r.get("timestamp", "")))

        # Re-derive best/worst after sort
        valid = [(i, float(r["avg_V"])) for i, r in enumerate(rows) if r.get("avg_V")]
        best_idx  = max(valid, key=lambda x: x[1])[0] if valid else -1
        worst_idx = min(valid, key=lambda x: x[1])[0] if valid else -1

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))

        for row_i, r in enumerate(rows):
            try:
                avg_v  = float(r.get("avg_V", 0))
                dev_mv = (avg_v - pack_mean) * 1000
                spread = float(r.get("spread_mV", 0))
            except ValueError:
                avg_v, dev_mv, spread = 0.0, 0.0, 0.0

            def cell(text, align=Qt.AlignmentFlag.AlignCenter) -> QTableWidgetItem:
                item = QTableWidgetItem(str(text))
                item.setTextAlignment(align)
                return item

            cells_v = []
            for cn in range(1, 7):
                try:
                    cells_v.append(float(r.get(f"cell{cn}_V", 0)))
                except ValueError:
                    cells_v.append(0.0)
            cell_mean = sum(cells_v) / len(cells_v) if cells_v else avg_v

            # Format timestamp as "YYYY-MM-DD HH:MM" — readable at a glance
            ts_raw = r.get("timestamp", "")
            try:
                ts = datetime.datetime.fromisoformat(ts_raw).strftime("%Y-%m-%d  %H:%M")
            except ValueError:
                ts = ts_raw

            # Columns: Label, Date/Time, Serial, HW Addr, C1-C6, Spread, Avg, Dev, T1, T2
            col_data = [
                r.get("module_label", ""),
                ts,
                r.get("serial_num", ""),
                r.get("hw_addr", ""),
                *[f"{v:.4f}" for v in cells_v],
                f"{spread:.1f}",
                f"{avg_v:.4f}",
                f"{dev_mv:+.1f}",
                r.get("temp1_C", ""),
                r.get("temp2_C", ""),
            ]
            for col_i, val in enumerate(col_data):
                self.table.setItem(row_i, col_i, cell(val))

            # Row background for best / worst
            if row_i == best_idx:
                row_color = QColor("#1b5e20")
            elif row_i == worst_idx:
                row_color = QColor("#7f0000")
            else:
                row_color = None

            # Per-cell coloring — C1-C6 are now cols 4-9
            for ci, v in enumerate(cells_v):
                item = self.table.item(row_i, 4 + ci)
                dev = (v - cell_mean) * 1000
                if dev < -CRIT_MV:
                    bg = QColor("#7f0000")
                elif dev < -WARN_MV:
                    bg = QColor("#7f4000")
                elif dev > HIGH_MV:
                    bg = QColor("#0d3b6e")
                elif row_color:
                    bg = row_color
                else:
                    bg = None
                if bg:
                    item.setBackground(bg)

            # Apply row color to non-cell columns
            if row_color:
                for col_i in [0, 1, 2, 3, 10, 11, 12, 13, 14]:
                    it = self.table.item(row_i, col_i)
                    if it:
                        it.setBackground(row_color)

            # Spread column is now col 10
            sp_item = self.table.item(row_i, 10)
            if sp_item and spread > SPREAD_WARN:
                sp_item.setBackground(QColor("#7f4000"))

            # Dev column is now col 12
            dev_item = self.table.item(row_i, 12)
            if dev_item:
                if dev_mv < -CRIT_MV:
                    dev_item.setBackground(QColor("#7f0000"))
                elif dev_mv < -WARN_MV:
                    dev_item.setBackground(QColor("#7f4000"))
                elif dev_mv > HIGH_MV:
                    dev_item.setBackground(QColor("#0d3b6e"))

        self.table.setSortingEnabled(True)


# ═══════════════════════════════════════════════════════════════════════════════
#  Balance tab
# ═══════════════════════════════════════════════════════════════════════════════
class BalanceTab(QWidget):
    COLS = ["Port", "HW Addr", "C1", "C2", "C3", "C4", "C5", "C6",
            "Spread mV", "Avg V", "Balancing Cells", "Label"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Settings bar ──────────────────────────────────────────────────────
        cfg_box = QGroupBox("Balance Settings")
        cfg_lay = QHBoxLayout(cfg_box)

        cfg_lay.addWidget(QLabel("Threshold (mV):"))
        self.spin_threshold = QLineEdit("10")
        self.spin_threshold.setFixedWidth(60)
        self.spin_threshold.setToolTip("Balance cells this many mV above the minimum")
        cfg_lay.addWidget(self.spin_threshold)
        cfg_lay.addSpacing(20)

        cfg_lay.addWidget(QLabel("Timer (min, 0=∞):"))
        self.spin_timer = QLineEdit("0")
        self.spin_timer.setFixedWidth(60)
        self.spin_timer.setToolTip("Auto-stop after N minutes (0 = run until stopped manually)")
        cfg_lay.addWidget(self.spin_timer)
        cfg_lay.addStretch()

        self.lbl_cfg_info = QLabel("Connect modules on the Test tab first.")
        self.lbl_cfg_info.setStyleSheet("color: #90caf9;")
        cfg_lay.addWidget(self.lbl_cfg_info)
        root.addWidget(cfg_box)

        # ── Module table ──────────────────────────────────────────────────────
        tbl_box = QGroupBox("Modules  (select rows, then use buttons below)")
        tbl_lay = QVBoxLayout(tbl_box)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        tbl_lay.addWidget(self.table)
        root.addWidget(tbl_box)

        # ── Action buttons ────────────────────────────────────────────────────
        act_lay = QHBoxLayout()

        self.btn_bal_sel = QPushButton("⚡  Auto-Balance Selected")
        self.btn_bal_sel.setMinimumHeight(38)
        self.btn_bal_sel.clicked.connect(self._balance_selected)

        self.btn_stop_sel = QPushButton("■  Stop Selected")
        self.btn_stop_sel.setMinimumHeight(38)
        self.btn_stop_sel.clicked.connect(self._stop_selected)

        self.btn_bal_all = QPushButton("⚡  Balance All")
        self.btn_bal_all.setMinimumHeight(38)
        self.btn_bal_all.setStyleSheet("background: #1b5e20;")
        self.btn_bal_all.clicked.connect(self._balance_all)

        self.btn_stop_all = QPushButton("■  Stop All")
        self.btn_stop_all.setMinimumHeight(38)
        self.btn_stop_all.setStyleSheet("background: #7f0000;")
        self.btn_stop_all.clicked.connect(self._stop_all)

        act_lay.addWidget(self.btn_bal_sel)
        act_lay.addWidget(self.btn_stop_sel)
        act_lay.addStretch()
        act_lay.addWidget(self.btn_bal_all)
        act_lay.addWidget(self.btn_stop_all)
        root.addLayout(act_lay)

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _threshold_mv(self) -> float:
        try:
            return float(self.spin_threshold.text())
        except ValueError:
            return 10.0

    def _duration_s(self) -> int:
        try:
            mins = float(self.spin_timer.text())
            return int(mins * 60)
        except ValueError:
            return 0

    def _test_tab(self):
        main = self.window()
        return main.test_tab if hasattr(main, "test_tab") else None

    def _modules(self) -> dict:
        tt = self._test_tab()
        return tt.get_modules() if tt else {}

    def _workers(self) -> dict:
        tt = self._test_tab()
        return tt.get_workers() if tt else {}

    def _keys_for_selected_rows(self) -> list[tuple]:
        sel_rows = {idx.row() for idx in self.table.selectedIndexes()}
        keys = list(self._modules().keys())
        return [keys[r] for r in sorted(sel_rows) if r < len(keys)]

    # ── Table rendering ───────────────────────────────────────────────────────
    def refresh_row(self, port: str, addr: int):
        """Called from TestTab whenever new data arrives for (port, addr)."""
        modules = self._modules()
        keys = list(modules.keys())
        key = (port, addr)
        if key not in modules:
            return
        # ensure row exists
        if key not in keys:
            keys.append(key)
        row_i = keys.index(key)
        if self.table.rowCount() != len(keys):
            self.table.setRowCount(len(keys))
        self._render_row(row_i, key, modules[key])
        active = sum(1 for e in modules.values() if e.get("bal_mask", 0))
        self.lbl_cfg_info.setText(
            f"{len(modules)} module(s) live — {active} balancing"
        )

    def on_balancing_changed(self, port: str, addr: int, mask: int):
        """Called when a worker reports a balance state change."""
        modules = self._modules()
        keys = list(modules.keys())
        key = (port, addr)
        if key in modules and key in keys:
            self._render_row(keys.index(key), key, modules[key])

    def _render_row(self, row_i: int, key: tuple, entry: dict):
        self.table.blockSignals(True)
        data     = entry["data"]
        cells    = data["cells"]
        mean     = sum(cells) / len(cells)
        spread   = (max(cells) - min(cells)) * 1000
        bal_mask = entry.get("bal_mask", 0)

        balancing_cells = [str(i + 1) for i in range(6) if (bal_mask >> i) & 1]
        bal_str = ", ".join(balancing_cells) if balancing_cells else "—"

        def mk(text):
            it = QTableWidgetItem(str(text))
            it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            return it

        col_vals = [
            entry["port"],
            str(data["hw_addr"]),
            *[f"{v:.4f}" for v in cells],
            f"{spread:.1f}",
            f"{mean:.4f}",
            bal_str,
            entry.get("label", ""),
        ]
        for col_i, val in enumerate(col_vals):
            it = mk(val)
            it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_i, col_i, it)

        # colour cell columns (C1-C6 = cols 2-7)
        # balancing cells get amber, others use normal spread colouring
        for ci, v in enumerate(cells):
            it = self.table.item(row_i, 2 + ci)
            if (bal_mask >> ci) & 1:
                it.setBackground(QColor("#7f4000"))  # amber = actively balancing
            else:
                dev = (v - mean) * 1000
                if dev < -CRIT_MV:
                    it.setBackground(QColor("#7f0000"))
                elif dev < -WARN_MV:
                    it.setBackground(QColor("#7f4000"))
                elif dev > HIGH_MV:
                    it.setBackground(QColor("#0d3b6e"))
                else:
                    it.setBackground(QColor("#1e4d2b"))

        # highlight entire row if any cell is balancing
        if bal_mask:
            for col_i in [0, 1, 8, 9, 10, 11]:
                it = self.table.item(row_i, col_i)
                if it:
                    it.setBackground(QColor("#3d2800"))

        self.table.blockSignals(False)

    # ── Actions ───────────────────────────────────────────────────────────────
    def _do_balance(self, keys: list[tuple]):
        modules = self._modules()
        workers = self._workers()
        threshold = self._threshold_mv()
        duration_s = self._duration_s()
        for key in keys:
            entry = modules.get(key)
            if not entry:
                continue
            port, addr = key
            worker = workers.get(port)
            if not worker:
                continue
            cells = entry["data"]["cells"]
            lo = min(cells)
            mask = 0
            for i, v in enumerate(cells):
                if (v - lo) * 1000 >= threshold:
                    mask |= (1 << i)
            if mask == 0:
                self.window().statusBar().showMessage(
                    f"[{port}] addr {addr}: all cells within threshold — nothing to balance.", 4000
                )
                continue
            worker.cmd_balance(addr, mask, duration_s)

    def _balance_selected(self):
        self._do_balance(self._keys_for_selected_rows())

    def _balance_all(self):
        self._do_balance(list(self._modules().keys()))

    def _do_stop(self, keys: list[tuple]):
        workers = self._workers()
        for port, addr in keys:
            w = workers.get(port)
            if w:
                w.cmd_stop_balance(addr)

    def _stop_selected(self):
        self._do_stop(self._keys_for_selected_rows())

    def _stop_all(self):
        for w in self._workers().values():
            w.cmd_stop_all_balance()


# ═══════════════════════════════════════════════════════════════════════════════
#  Main window
# ═══════════════════════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Tesla BMB Module Monitor")
        self.resize(1000, 620)
        self._apply_dark_theme()

        tabs = QTabWidget()
        self.test_tab    = TestTab(self)
        self.balance_tab = BalanceTab(self)
        self.summary_tab = SummaryTab(self)
        tabs.addTab(self.test_tab,    "🔌  Test Module")
        tabs.addTab(self.balance_tab, "⚡  Balance")
        tabs.addTab(self.summary_tab, "📊  Pack Summary")
        self.setCentralWidget(tabs)

        bar = QStatusBar()
        self.setStatusBar(bar)
        bar.showMessage("Ready — connect a module via FTDI adapter")

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow, QWidget       { background-color: #1a1a2e; color: #e0e0e0; }
            QGroupBox                  { border: 1px solid #37474f; border-radius: 6px;
                                         margin-top: 6px; padding-top: 6px; color: #90caf9; }
            QGroupBox::title           { subcontrol-origin: margin; left: 10px; }
            QTabWidget::pane           { border: 1px solid #37474f; }
            QTabBar::tab               { background: #16213e; color: #90caf9;
                                         padding: 6px 18px; border-radius: 4px; }
            QTabBar::tab:selected      { background: #0f3460; color: white; }
            QPushButton                { background: #0f3460; color: #e0e0e0;
                                         border: 1px solid #37474f; border-radius: 4px;
                                         padding: 5px 12px; }
            QPushButton:hover          { background: #1565c0; }
            QPushButton:checked        { background: #b71c1c; }
            QComboBox, QLineEdit, QListWidget { background: #16213e; color: #e0e0e0;
                                         border: 1px solid #37474f; border-radius: 4px;
                                         padding: 3px 6px; }
            QListWidget::item:selected { background: #1565c0; }
            QTableWidget               { background: #16213e; color: #e0e0e0;
                                         gridline-color: #37474f; border: none; }
            QHeaderView::section       { background: #0f3460; color: #90caf9;
                                         border: 1px solid #37474f; padding: 4px; }
            QTableWidget::item:selected { background: #1565c0; }
            QStatusBar                 { color: #90caf9; }
        """)


# ═══════════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    ensure_csv()
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
