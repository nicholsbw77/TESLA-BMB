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

import serial
import serial.tools.list_ports

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QLabel, QPushButton, QComboBox, QLineEdit,
    QTableWidget, QTableWidgetItem, QHeaderView,
    QGroupBox, QStatusBar, QSizePolicy, QFrame,
    QMessageBox,
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
    data_ready   = pyqtSignal(dict)
    status_msg   = pyqtSignal(str)
    connect_done = pyqtSignal(list)   # list of hw addrs found
    error        = pyqtSignal(str)

    def __init__(self, port: str):
        super().__init__()
        self.port    = port
        self.running = False
        self.bus: BMBBus | None = None
        self.addrs: list[int] = []

    def run(self):
        try:
            self.bus = BMBBus(self.port)
        except Exception as e:
            self.error.emit(f"Cannot open {self.port}: {e}")
            return

        self.status_msg.emit("Waking modules…")
        self.bus.wake()

        self.status_msg.emit("Discovering modules…")
        self.addrs = self.bus.discover()
        if not self.addrs:
            self.error.emit("No BMB modules responded. Check wiring.")
            self.bus.close()
            return

        self.connect_done.emit(self.addrs)
        self.status_msg.emit(f"Connected — {len(self.addrs)} module(s) at addr {self.addrs}")
        self.running = True

        while self.running:
            for addr in self.addrs:
                data = self.bus.read_module(addr)
                if data:
                    self.data_ready.emit(data)
                else:
                    self.status_msg.emit(f"No response addr {addr}")
            time.sleep(2.0)

    def stop(self):
        self.running = False
        if self.bus:
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
# ═══════════════════════════════════════════════════════════════════════════════
class TestTab(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker: BMBWorker | None = None
        self._last_data: dict | None = None
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Connection bar ────────────────────────────────────────────────────
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

        # ── Module identity ───────────────────────────────────────────────────
        id_box = QGroupBox("Module Identity")
        id_lay = QHBoxLayout(id_box)

        self.txt_label  = QLineEdit()
        self.txt_label.setPlaceholderText("e.g. M05  or  spare-1")
        self.txt_label.setMinimumWidth(130)

        self.txt_serial = QLineEdit()
        self.txt_serial.setPlaceholderText("serial / part number (optional)")
        self.txt_serial.setMinimumWidth(200)

        id_lay.addWidget(QLabel("Label:"))
        id_lay.addWidget(self.txt_label)
        id_lay.addSpacing(20)
        id_lay.addWidget(QLabel("Serial / Notes:"))
        id_lay.addWidget(self.txt_serial)
        id_lay.addStretch()

        root.addWidget(id_box)

        # ── Cell voltage grid ─────────────────────────────────────────────────
        cells_box = QGroupBox("Cell Voltages")
        cells_lay = QHBoxLayout(cells_box)
        cells_lay.setSpacing(8)

        self.cell_widgets = []
        for i in range(1, 7):
            cw = CellWidget(i)
            self.cell_widgets.append(cw)
            cells_lay.addWidget(cw)

        root.addWidget(cells_box)

        # ── Module stats ──────────────────────────────────────────────────────
        stats_box = QGroupBox("Module Stats")
        stats_lay = QHBoxLayout(stats_box)
        stats_lay.setSpacing(30)

        def stat_pair(label):
            lbl = QLabel(label + ":")
            val = QLabel("—")
            f = val.font()
            f.setPointSize(13)
            f.setBold(True)
            val.setFont(f)
            return lbl, val

        lbl_mv, self.lbl_mod_v   = stat_pair("Pack V")
        lbl_sp, self.lbl_spread  = stat_pair("Spread")
        lbl_t1, self.lbl_temp1   = stat_pair("Temp 1")
        lbl_t2, self.lbl_temp2   = stat_pair("Temp 2")
        lbl_av, self.lbl_avg     = stat_pair("Avg Cell")

        for lbl, val in [(lbl_mv, self.lbl_mod_v), (lbl_sp, self.lbl_spread),
                         (lbl_t1, self.lbl_temp1), (lbl_t2, self.lbl_temp2),
                         (lbl_av, self.lbl_avg)]:
            pair = QVBoxLayout()
            pair.addWidget(lbl)
            pair.addWidget(val)
            stats_lay.addLayout(pair)

        stats_lay.addStretch()

        # Save button in stats bar
        self.btn_save = QPushButton("💾  Save Reading to Log")
        self.btn_save.setEnabled(False)
        self.btn_save.setMinimumHeight(40)
        self.btn_save.setMinimumWidth(180)
        self.btn_save.clicked.connect(self._save_reading)
        stats_lay.addWidget(self.btn_save)

        root.addWidget(stats_box)
        root.addStretch()

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
            for cw in self.cell_widgets:
                cw.clear()

    def _start_worker(self, port: str):
        self._worker = BMBWorker(port)
        self._worker.data_ready.connect(self._on_data)
        self._worker.status_msg.connect(self._on_status)
        self._worker.connect_done.connect(self._on_connected)
        self._worker.error.connect(self._on_error)
        self._worker.start()

    def _stop_worker(self):
        if self._worker:
            self._worker.stop()
            self._worker.wait(3000)
            self._worker = None
        self.btn_save.setEnabled(False)

    # ── Signals from worker ───────────────────────────────────────────────────
    def _on_connected(self, addrs: list[int]):
        self.lbl_hw_addr.setText(f"HW addr: {addrs}")
        self.btn_save.setEnabled(True)
        if self.txt_label.text() == "" and addrs:
            self.txt_label.setText(f"addr{addrs[0]}")

    def _on_data(self, data: dict):
        self._last_data = data
        cells = data["cells"]
        mean  = sum(cells) / len(cells)

        for i, (cw, v) in enumerate(zip(self.cell_widgets, cells)):
            cw.update_value(v, (v - mean) * 1000)

        spread_mv = (max(cells) - min(cells)) * 1000
        spread_color = "#e53935" if spread_mv > SPREAD_WARN else "#43a047"
        self.lbl_spread.setText(f"{spread_mv:.1f} mV")
        self.lbl_spread.setStyleSheet(f"color: {spread_color};")

        self.lbl_mod_v.setText(f"{data['module_v']:.3f} V")
        self.lbl_avg.setText(f"{mean:.4f} V")

        t1, t2 = data["temp1"], data["temp2"]
        self.lbl_temp1.setText("—" if math.isnan(t1) else f"{t1:.1f} °C")
        self.lbl_temp2.setText("—" if math.isnan(t2) else f"{t2:.1f} °C")

    def _on_status(self, msg: str):
        window = self.window()
        if hasattr(window, "statusBar"):
            window.statusBar().showMessage(msg, 5000)

    def _on_error(self, msg: str):
        self.btn_connect.setChecked(False)
        self.btn_connect.setText("Connect")
        self._stop_worker()
        QMessageBox.critical(self, "Connection Error", msg)

    # ── Save ──────────────────────────────────────────────────────────────────
    def _save_reading(self):
        if not self._last_data:
            return
        label  = self.txt_label.text().strip() or "unlabeled"
        serial_n = self.txt_serial.text().strip()
        cells  = self._last_data["cells"]
        spread = (max(cells) - min(cells)) * 1000
        avg    = sum(cells) / len(cells)
        t1, t2 = self._last_data["temp1"], self._last_data["temp2"]

        row = {
            "timestamp":    datetime.datetime.now().isoformat(timespec="seconds"),
            "module_label": label,
            "serial_num":   serial_n,
            "hw_addr":      self._last_data["hw_addr"],
            "cell1_V":      f"{cells[0]:.6f}",
            "cell2_V":      f"{cells[1]:.6f}",
            "cell3_V":      f"{cells[2]:.6f}",
            "cell4_V":      f"{cells[3]:.6f}",
            "cell5_V":      f"{cells[4]:.6f}",
            "cell6_V":      f"{cells[5]:.6f}",
            "module_V":     f"{self._last_data['module_v']:.4f}",
            "temp1_C":      "nan" if math.isnan(t1) else f"{t1:.2f}",
            "temp2_C":      "nan" if math.isnan(t2) else f"{t2:.2f}",
            "spread_mV":    f"{spread:.2f}",
            "avg_V":        f"{avg:.6f}",
        }
        append_csv(row)
        msg = f"Saved module '{label}' to log."
        self._on_status(msg)

        # tell summary tab to refresh
        main = self.window()
        if hasattr(main, "summary_tab"):
            main.summary_tab.refresh()

        QMessageBox.information(self, "Saved", msg)


# ═══════════════════════════════════════════════════════════════════════════════
#  Pack Summary tab
# ═══════════════════════════════════════════════════════════════════════════════
class SummaryTab(QWidget):
    COLS = ["Label", "Serial", "HW Addr", "C1", "C2", "C3", "C4", "C5", "C6",
            "Spread mV", "Avg V", "Dev mV", "Temp1 °C", "Temp2 °C", "Saved"]

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

            col_data = [
                r.get("module_label", ""),
                r.get("serial_num", ""),
                r.get("hw_addr", ""),
                *[f"{v:.4f}" for v in cells_v],
                f"{spread:.1f}",
                f"{avg_v:.4f}",
                f"{dev_mv:+.1f}",
                r.get("temp1_C", ""),
                r.get("temp2_C", ""),
                r.get("timestamp", ""),
            ]
            for col_i, val in enumerate(col_data):
                item = cell(val, Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row_i, col_i, item)

            # Row background for best / worst
            if row_i == best_idx:
                row_color = QColor("#1b5e20")
            elif row_i == worst_idx:
                row_color = QColor("#7f0000")
            else:
                row_color = None

            # Per-cell coloring in C1-C6 columns (cols 3-8)
            for ci, v in enumerate(cells_v):
                item = self.table.item(row_i, 3 + ci)
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
                for col_i in [0, 1, 2, 9, 10, 11, 12, 13, 14]:
                    it = self.table.item(row_i, col_i)
                    if it:
                        it.setBackground(row_color)

            # Color spread column
            sp_item = self.table.item(row_i, 9)
            if sp_item and spread > SPREAD_WARN:
                sp_item.setBackground(QColor("#7f4000"))

            # Color dev column
            dev_item = self.table.item(row_i, 11)
            if dev_item:
                if dev_mv < -CRIT_MV:
                    dev_item.setBackground(QColor("#7f0000"))
                elif dev_mv < -WARN_MV:
                    dev_item.setBackground(QColor("#7f4000"))
                elif dev_mv > HIGH_MV:
                    dev_item.setBackground(QColor("#0d3b6e"))

        self.table.setSortingEnabled(True)


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
        self.summary_tab = SummaryTab(self)
        tabs.addTab(self.test_tab,    "🔌  Test Module")
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
            QComboBox, QLineEdit       { background: #16213e; color: #e0e0e0;
                                         border: 1px solid #37474f; border-radius: 4px;
                                         padding: 3px 6px; }
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
