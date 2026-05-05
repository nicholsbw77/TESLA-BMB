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
    # data_ready carries port name so the UI can key rows by (port, addr)
    data_ready   = pyqtSignal(str, dict)
    status_msg   = pyqtSignal(str)
    connect_done = pyqtSignal(str, list)  # port, list of hw addrs found
    error        = pyqtSignal(str, str)   # port, message

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
#  Test Module tab  (multi-port)
# ═══════════════════════════════════════════════════════════════════════════════
class TestTab(QWidget):
    # columns in the live table
    LIVE_COLS = ["Port", "HW Addr", "C1", "C2", "C3", "C4", "C5", "C6",
                 "Spread mV", "Avg V", "Module V", "Temp1 °C", "Temp2 °C", "Label", "Serial"]

    def __init__(self, parent=None):
        super().__init__(parent)
        # keyed by (port, addr) → {data, label, serial}
        self._modules: dict[tuple, dict] = {}
        # keyed by port → BMBWorker
        self._workers: dict[str, BMBWorker] = {}
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(12, 12, 12, 12)

        # ── Connection bar ────────────────────────────────────────────────────
        conn_box = QGroupBox("Connection  (Ctrl+click to select multiple ports)")
        conn_lay = QHBoxLayout(conn_box)

        from PyQt6.QtWidgets import QListWidget, QAbstractItemView
        self.lst_ports = QListWidget()
        self.lst_ports.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        self.lst_ports.setMaximumHeight(80)
        self.lst_ports.setMinimumWidth(320)
        self._refresh_ports()

        btn_refresh = QPushButton("↻")
        btn_refresh.setFixedWidth(32)
        btn_refresh.setToolTip("Refresh port list")
        btn_refresh.clicked.connect(self._refresh_ports)

        self.btn_connect = QPushButton("Connect All")
        self.btn_connect.setCheckable(True)
        self.btn_connect.clicked.connect(self._toggle_connect)
        self.btn_connect.setMinimumWidth(110)

        self.lbl_status = QLabel("Not connected")
        self.lbl_status.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        conn_lay.addWidget(QLabel("Ports:"))
        conn_lay.addWidget(self.lst_ports)
        conn_lay.addWidget(btn_refresh)
        conn_lay.addSpacing(16)
        conn_lay.addWidget(self.btn_connect)
        conn_lay.addSpacing(16)
        conn_lay.addWidget(self.lbl_status)
        conn_lay.addStretch()

        root.addWidget(conn_box)

        # ── Live module table ─────────────────────────────────────────────────
        live_box = QGroupBox("Live Readings")
        live_lay = QVBoxLayout(live_box)

        self.table = QTableWidget()
        self.table.setColumnCount(len(self.LIVE_COLS))
        self.table.setHorizontalHeaderLabels(self.LIVE_COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        # allow editing label/serial inline via double-click on those columns
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked |
            QTableWidget.EditTrigger.SelectedClicked
        )
        self.table.itemChanged.connect(self._on_table_edit)
        live_lay.addWidget(self.table)

        # Save row button
        btn_row = QHBoxLayout()
        self.btn_save_sel = QPushButton("💾  Save Selected to Log")
        self.btn_save_sel.setEnabled(False)
        self.btn_save_sel.setMinimumHeight(36)
        self.btn_save_sel.clicked.connect(self._save_selected)

        self.btn_save_all = QPushButton("💾  Save All to Log")
        self.btn_save_all.setEnabled(False)
        self.btn_save_all.setMinimumHeight(36)
        self.btn_save_all.clicked.connect(self._save_all)

        btn_row.addStretch()
        btn_row.addWidget(self.btn_save_sel)
        btn_row.addWidget(self.btn_save_all)
        live_lay.addLayout(btn_row)

        root.addWidget(live_box)

    # ── Ports ─────────────────────────────────────────────────────────────────
    def _refresh_ports(self):
        self.lst_ports.clear()
        ports = serial.tools.list_ports.comports()
        for p in ports:
            from PyQt6.QtWidgets import QListWidgetItem
            item = QListWidgetItem(f"{p.device}  {p.description[:50]}")
            item.setData(Qt.ItemDataRole.UserRole, p.device)
            self.lst_ports.addItem(item)
        if not ports:
            from PyQt6.QtWidgets import QListWidgetItem
            self.lst_ports.addItem(QListWidgetItem("No ports found"))

    def _selected_ports(self) -> list[str]:
        ports = []
        for item in self.lst_ports.selectedItems():
            p = item.data(Qt.ItemDataRole.UserRole)
            if p:
                ports.append(p)
        return ports

    # ── Connect / disconnect ──────────────────────────────────────────────────
    def _toggle_connect(self, checked: bool):
        if checked:
            ports = self._selected_ports()
            if not ports:
                self.btn_connect.setChecked(False)
                QMessageBox.warning(self, "No Port Selected",
                                    "Select at least one port from the list.")
                return
            self.btn_connect.setText("Disconnect All")
            for port in ports:
                self._start_worker(port)
        else:
            self._stop_all_workers()
            self.btn_connect.setText("Connect All")
            self.lbl_status.setText("Not connected")
            self._modules.clear()
            self.table.setRowCount(0)
            self.btn_save_sel.setEnabled(False)
            self.btn_save_all.setEnabled(False)

    def _start_worker(self, port: str):
        if port in self._workers:
            return
        w = BMBWorker(port)
        w.data_ready.connect(self._on_data)
        w.status_msg.connect(self._on_status)
        w.connect_done.connect(self._on_connected)
        w.error.connect(self._on_error)
        self._workers[port] = w
        w.start()

    def _stop_all_workers(self):
        for w in self._workers.values():
            w.stop()
            w.wait(3000)
        self._workers.clear()
        self.btn_save_sel.setEnabled(False)
        self.btn_save_all.setEnabled(False)

    # ── Signals from workers ──────────────────────────────────────────────────
    def _on_connected(self, port: str, addrs: list[int]):
        connected = sum(len(w.addrs) for w in self._workers.values())
        self.lbl_status.setText(
            f"{len(self._workers)} port(s) — {connected} module(s) found"
        )
        self.btn_save_sel.setEnabled(True)
        self.btn_save_all.setEnabled(True)

    def _on_data(self, port: str, data: dict):
        key = (port, data["hw_addr"])
        existing = self._modules.get(key, {})
        self._modules[key] = {
            "port":   port,
            "data":   data,
            "label":  existing.get("label", f"{port}-addr{data['hw_addr']}"),
            "serial": existing.get("serial", ""),
        }
        self._refresh_table()

    def _on_status(self, msg: str):
        window = self.window()
        if hasattr(window, "statusBar"):
            window.statusBar().showMessage(msg, 5000)

    def _on_error(self, port: str, msg: str):
        self._workers.pop(port, None)
        if not self._workers:
            self.btn_connect.setChecked(False)
            self.btn_connect.setText("Connect All")
        QMessageBox.critical(self, "Connection Error", msg)

    # ── Table rendering ───────────────────────────────────────────────────────
    def _refresh_table(self):
        self.table.blockSignals(True)
        keys = list(self._modules.keys())
        self.table.setRowCount(len(keys))

        # compute overall cell mean across all modules for deviation colouring
        all_cells = []
        for entry in self._modules.values():
            all_cells.extend(entry["data"]["cells"])
        global_mean = sum(all_cells) / len(all_cells) if all_cells else 0.0

        for row_i, key in enumerate(keys):
            entry = self._modules[key]
            data  = entry["data"]
            cells = data["cells"]
            mean  = sum(cells) / len(cells)
            spread_mv = (max(cells) - min(cells)) * 1000
            t1, t2 = data["temp1"], data["temp2"]

            def cell_item(text):
                it = QTableWidgetItem(str(text))
                it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                return it

            col_vals = [
                entry["port"],
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

            for col_i, val in enumerate(col_vals):
                it = cell_item(val)
                # only label/serial columns are editable
                if col_i < len(self.LIVE_COLS) - 2:
                    it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.table.setItem(row_i, col_i, it)

            # colour individual cell columns (C1-C6 = cols 2-7)
            for ci, v in enumerate(cells):
                dev = (v - mean) * 1000
                it  = self.table.item(row_i, 2 + ci)
                if dev < -CRIT_MV:
                    it.setBackground(QColor("#7f0000"))
                elif dev < -WARN_MV:
                    it.setBackground(QColor("#7f4000"))
                elif dev > HIGH_MV:
                    it.setBackground(QColor("#0d3b6e"))
                else:
                    it.setBackground(QColor("#1e4d2b"))

            # colour spread column (col 8)
            sp_it = self.table.item(row_i, 8)
            if spread_mv > SPREAD_WARN:
                sp_it.setBackground(QColor("#7f4000"))

        self.table.blockSignals(False)

    def _on_table_edit(self, item: QTableWidgetItem):
        col = item.column()
        row = item.row()
        keys = list(self._modules.keys())
        if row >= len(keys):
            return
        key = keys[row]
        if col == len(self.LIVE_COLS) - 2:   # Label
            self._modules[key]["label"] = item.text()
        elif col == len(self.LIVE_COLS) - 1: # Serial
            self._modules[key]["serial"] = item.text()

    # ── Save ──────────────────────────────────────────────────────────────────
    def _build_csv_row(self, key: tuple) -> dict:
        entry = self._modules[key]
        data  = entry["data"]
        cells = data["cells"]
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

    def _save_selected(self):
        sel_rows = {idx.row() for idx in self.table.selectedIndexes()}
        keys = list(self._modules.keys())
        saved = 0
        for row_i in sorted(sel_rows):
            if row_i < len(keys):
                append_csv(self._build_csv_row(keys[row_i]))
                saved += 1
        if saved:
            self._on_status(f"Saved {saved} module(s) to log.")
            main = self.window()
            if hasattr(main, "summary_tab"):
                main.summary_tab.refresh()
            QMessageBox.information(self, "Saved", f"Saved {saved} module(s) to log.")

    def _save_all(self):
        keys = list(self._modules.keys())
        for key in keys:
            append_csv(self._build_csv_row(key))
        self._on_status(f"Saved {len(keys)} module(s) to log.")
        main = self.window()
        if hasattr(main, "summary_tab"):
            main.summary_tab.refresh()
        QMessageBox.information(self, "Saved", f"Saved {len(keys)} module(s) to log.")


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
