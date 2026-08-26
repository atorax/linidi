#!/usr/bin/env python3
"""
minidsp-gui -- a desktop tuning front-end for miniDSP hardware on Linux.

    python3 minidsp_gui.py

Requires `minidspd` (from minidsp-rs) to be running. All DSP writes go through
its REST API; readback drives the `minidsp` CLI, which is the only path that
exposes the protocol's ReadFloats.

License: Apache-2.0
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import (QObject, QPointF, QThread, QTimer, Qt, Signal)
from PySide6.QtGui import (QAction, QColor, QFont, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QSlider,
    QSplitter, QStatusBar, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

import minidsp_core as core

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

BG      = "#16181d"
PANEL   = "#1e2128"
PANEL2  = "#252932"
LINE    = "#333844"
FG      = "#e6e8ec"
MUTED   = "#8b93a3"
ACCENT  = "#4f9cf9"
OK      = "#3fb950"
WARN    = "#d29922"
DANGER  = "#f0533f"

STYLE = f"""
QWidget {{ background: {BG}; color: {FG};
           font-family: system-ui, sans-serif; font-size: 13px; }}
QGroupBox {{ background: {PANEL}; border: 1px solid {LINE};
             border-radius: 6px; margin-top: 14px; padding-top: 6px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px;
                    color: {MUTED}; font-size: 11px; }}
QPushButton {{ background: {PANEL2}; border: 1px solid {LINE};
               border-radius: 4px; padding: 5px 10px; }}
QPushButton:hover {{ border-color: {ACCENT}; }}
QPushButton:disabled {{ color: {MUTED}; }}
QPushButton#primary {{ background: {ACCENT}; color: #06101f; font-weight: 600;
                       border-color: {ACCENT}; }}
QPushButton#danger {{ background: {DANGER}; color: white; font-weight: 700;
                      border-color: {DANGER}; }}
QPushButton:checked {{ background: {WARN}; color: #201800; border-color: {WARN}; }}
QComboBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTableWidget {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 4px;
    padding: 2px 4px; selection-background-color: {ACCENT};
}}
QListWidget::item {{ padding: 6px 8px; border-left: 3px solid transparent; }}
QListWidget::item:selected {{ background: {PANEL2}; color: {FG};
                              border-left-color: {ACCENT}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED};
                        border: 0; border-bottom: 1px solid {LINE};
                        padding: 4px; font-size: 11px; }}
QTableWidget {{ gridline-color: {LINE}; }}
QLabel#muted {{ color: {MUTED}; font-size: 11px; }}
QSlider::groove:horizontal {{ height: 4px; background: {PANEL2};
                              border-radius: 2px; }}
QSlider::handle:horizontal {{ width: 12px; margin: -5px 0; border-radius: 6px;
                              background: {ACCENT}; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; }}
"""


# --------------------------------------------------------------------------
# Background workers -- device I/O must never block the UI thread
# --------------------------------------------------------------------------

class Worker(QObject):
    """Runs one callable on a QThread and reports the outcome."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self._fn, self._args, self._kwargs = fn, args, kwargs

    def run(self):
        try:
            self.done.emit(self._fn(*self._args, **self._kwargs))
        except Exception as exc:                       # noqa: BLE001
            self.failed.emit(str(exc))


class TaskRunner(QObject):
    """Runs callables on background threads and reaps them safely.

    Qt object lifetime here is fussy and worth spelling out, because getting
    it wrong aborts the process rather than raising:

      * Callbacks must not tear the thread down. A plain lambda connected to a
        worker signal has no receiver QObject, so Qt uses a *direct* connection
        and runs it in the worker thread -- where calling QThread.wait() means
        a thread waiting on itself.
      * References must outlive the thread. Dropping the last Python reference
        to a still-running QThread lets the garbage collector destroy it, and
        Qt aborts with "QThread: Destroyed while thread is still running".

    So: the worker only ever asks the thread to quit, and reaping happens on
    the main thread once QThread.finished has actually fired. TaskRunner is a
    QObject owned by the window, so that connection is queued to the main
    thread rather than run inline.
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._live: list[tuple[QThread, Worker]] = []

    def run(self, fn, on_done=None, on_error=None, *args, **kwargs):
        thread = QThread()
        worker = Worker(fn, *args, **kwargs)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)

        # Queued: these receivers live on the main thread.
        if on_done:
            worker.done.connect(on_done)
        if on_error:
            worker.failed.connect(on_error)

        # quit() is thread-safe and merely asks the event loop to stop.
        worker.done.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(self._reap)

        self._live.append((thread, worker))
        thread.start()

    def _reap(self):
        """Drop finished threads. Always runs on the main thread."""
        for pair in list(self._live):
            thread, worker = pair
            if thread.isFinished():
                thread.wait()
                worker.deleteLater()
                thread.deleteLater()
                self._live.remove(pair)

    def shutdown(self):
        """Stop and join everything still in flight, before the window dies."""
        for thread, _worker in list(self._live):
            thread.quit()
        for thread, _worker in list(self._live):
            thread.wait(5000)
        self._live.clear()


# --------------------------------------------------------------------------
# Widgets
# --------------------------------------------------------------------------

class MeterBar(QWidget):
    """A level meter with instant attack, decaying release and peak hold.

    Ballistics matter more than raw poll rate for how a meter *reads*: rising
    instantly and falling at a fixed dB/second looks smooth even when the
    underlying samples arrive at a modest rate, whereas an unsmoothed bar
    flickers no matter how fast you poll.
    """

    DECAY_DB_PER_SEC = 90.0     # release rate of the bar
    PEAK_HOLD_SEC = 1.2         # how long the peak tick stays put

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.value = -120.0         # newest sample
        self.display = -120.0       # what is actually drawn
        self.peak = -120.0
        self._peak_at = 0.0
        self._last = time.monotonic()
        self.setMinimumHeight(15)
        self.setMaximumHeight(15)

    def set_value(self, db: float):
        self.value = db
        now = time.monotonic()
        if db > self.peak or now - self._peak_at > self.PEAK_HOLD_SEC:
            self.peak, self._peak_at = db, now
        # Driven by the poll for now. If a repaint timer is added later for
        # smoother release, call animate() from that instead and drop this.
        self.animate()

    def animate(self):
        """Advance ballistics one frame."""
        now = time.monotonic()
        dt = max(0.0, now - self._last)
        self._last = now
        if self.value >= self.display:
            self.display = self.value                  # instant attack
        else:
            self.display = max(self.value,
                               self.display - self.DECAY_DB_PER_SEC * dt)
        self.update()

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        p.setPen(QColor(MUTED))
        p.setFont(QFont("monospace", 8))
        p.drawText(0, 0, 22, h, Qt.AlignVCenter | Qt.AlignLeft, self.label)
        p.drawText(w - 42, 0, 42, h, Qt.AlignVCenter | Qt.AlignRight,
                   "-inf" if self.value <= -119 else f"{self.value:.1f}")

        x0, x1 = 24, w - 46
        bar_w = max(1, x1 - x0)
        p.fillRect(x0, 4, bar_w, h - 8, QColor(PANEL2))

        def frac(db):
            return max(0.0, min(1.0, (db + 60.0) / 60.0))

        fill = int(bar_w * frac(self.display))
        if fill > 0:
            col = DANGER if self.value > -3 else WARN if self.value > -12 else OK
            p.fillRect(x0, 4, fill, h - 8, QColor(col))
        if self.peak > -119:
            px = x0 + int(bar_w * frac(self.peak))
            p.fillRect(min(px, x1 - 1), 4, 1, h - 8, QColor(FG))


class ResponsePlot(QWidget):
    """Log-frequency magnitude plot, painted directly (no plotting library)."""

    DB_MIN, DB_MAX = -36.0, 18.0

    def __init__(self):
        super().__init__()
        self.curves: list[tuple[list[float], list[float], str, bool]] = []
        self.freqs = core.log_freqs(280)
        self.setMinimumHeight(200)

    def set_curves(self, curves):
        self.curves = curves
        self.update()

    def _fx(self, f, w):
        import math
        lo, hi = math.log10(20.0), math.log10(20000.0)
        return (math.log10(f) - lo) / (hi - lo) * w

    def _fy(self, db, h):
        db = max(self.DB_MIN, min(self.DB_MAX, db))
        return h - (db - self.DB_MIN) / (self.DB_MAX - self.DB_MIN) * h

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(PANEL))

        p.setFont(QFont("monospace", 8))
        grid = QPen(QColor(LINE)); grid.setWidth(1)
        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = self._fx(f, w)
            p.setPen(grid)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.setPen(QColor(MUTED))
            p.drawText(QPointF(x + 3, h - 3),
                       f"{f // 1000}k" if f >= 1000 else str(f))
        for db in range(int(self.DB_MIN), int(self.DB_MAX) + 1, 12):
            y = self._fy(db, h)
            p.setPen(grid)
            p.drawLine(QPointF(0, y), QPointF(w, y))
            p.setPen(QColor(MUTED))
            p.drawText(QPointF(3, y - 3), f"{db:+d}")

        zero = QPen(QColor("#4a5163")); zero.setWidth(1)
        p.setPen(zero)
        y0 = self._fy(0.0, h)
        p.drawLine(QPointF(0, y0), QPointF(w, y0))

        for freqs, dbs, colour, dashed in self.curves:
            pen = QPen(QColor(colour))
            pen.setWidth(2)
            if dashed:
                pen.setStyle(Qt.DashLine)
                pen.setWidth(1)
            p.setPen(pen)
            path = QPainterPath()
            for i, (f, db) in enumerate(zip(freqs, dbs)):
                pt = QPointF(self._fx(f, w), self._fy(db, h))
                path.moveTo(pt) if i == 0 else path.lineTo(pt)
            p.drawPath(path)


class CrossoverGroup(QGroupBox):
    """Editor for one crossover group (4 biquad slots)."""

    changed = Signal()

    def __init__(self, title: str):
        super().__init__(title)
        self.data: dict[str, Any] = {}
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)

        self.enabled = QCheckBox("Enabled")
        self.enabled.toggled.connect(self._emit)
        lay.addWidget(self.enabled, 0, 0, 1, 2)

        self.mode = QComboBox()
        self.mode.addItems(["highpass", "lowpass"])
        self.mode.currentTextChanged.connect(self._emit)

        self.alignment = QComboBox()
        self.alignment.addItems(list(core.ALIGNMENTS) + ["custom"])
        self.alignment.currentTextChanged.connect(self._on_alignment)

        self.order = QComboBox()
        self.order.currentTextChanged.connect(self._emit)

        self.freq = QDoubleSpinBox()
        self.freq.setRange(10.0, 24000.0)
        self.freq.setDecimals(1)
        self.freq.setSingleStep(10.0)
        self.freq.setSuffix(" Hz")
        self.freq.valueChanged.connect(self._emit)

        for row, (label, widget) in enumerate([
            ("Mode", self.mode), ("Alignment", self.alignment),
            ("Slope", self.order), ("Frequency", self.freq),
        ], start=1):
            lab = QLabel(label); lab.setObjectName("muted")
            lay.addWidget(lab, row, 0)
            lay.addWidget(widget, row, 1)

        self.note = QLabel(""); self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        lay.addWidget(self.note, 5, 0, 1, 2)
        self._refresh_orders()

    def _on_alignment(self, *_):
        self._refresh_orders()
        self._emit()

    def _refresh_orders(self):
        align = self.alignment.currentText()
        prev = self.order.currentText()
        self.order.blockSignals(True)
        self.order.clear()
        if align == "linkwitz-riley":
            items = [("2", "LR12"), ("4", "LR24"), ("8", "LR48")]
        elif align == "bessel":
            items = [(str(o), f"{o * 6} dB/oct") for o in (2, 4, 6, 8)]
        elif align == "butterworth":
            items = [(str(o), f"{o * 6} dB/oct") for o in (1, 2, 3, 4, 6, 8)]
        else:
            items = []
        for value, label in items:
            self.order.addItem(label, value)
        idx = self.order.findText(prev)
        if idx >= 0:
            self.order.setCurrentIndex(idx)
        self.order.blockSignals(False)
        custom = align == "custom"
        for wdg in (self.mode, self.order, self.freq):
            wdg.setEnabled(not custom)
        self.note.setText(
            "Coefficients read from the device do not match a standard "
            "alignment; they will be written back unchanged."
            if custom else "")

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit()

    _loading = False

    def load(self, group: dict[str, Any]):
        self._loading = True
        self.data = group
        self.enabled.setChecked(bool(group.get("enabled")))
        align = group.get("alignment", "linkwitz-riley")
        idx = self.alignment.findText(align)
        self.alignment.setCurrentIndex(max(0, idx))
        self._refresh_orders()
        idx = self.mode.findText(group.get("mode", "highpass"))
        self.mode.setCurrentIndex(max(0, idx))
        want = str(group.get("order", 4))
        for i in range(self.order.count()):
            if self.order.itemData(i) == want:
                self.order.setCurrentIndex(i)
                break
        self.freq.setValue(float(group.get("freq", 80.0)))
        self._loading = False

    def store(self) -> dict[str, Any]:
        self.data["enabled"] = self.enabled.isChecked()
        self.data["alignment"] = self.alignment.currentText()
        self.data["mode"] = self.mode.currentText()
        data = self.order.currentData()
        self.data["order"] = int(data) if data else 4
        self.data["freq"] = self.freq.value()
        return self.data


class PeqTable(QTableWidget):
    """Editor for a channel's PEQ bank."""

    changed = Signal()
    COLS = ["On", "Type", "Freq (Hz)", "Q", "Gain (dB)", "Source"]

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.bands: list[dict[str, Any]] = []
        self._loading = False

    def load(self, bands: list[dict[str, Any]]):
        self._loading = True
        self.bands = bands
        self.setRowCount(len(bands))
        for r, b in enumerate(bands):
            on = QCheckBox()
            on.setChecked(bool(b.get("enabled")))
            on.toggled.connect(self._emit)
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0); hl.addWidget(on)
            hl.setAlignment(Qt.AlignCenter)
            self.setCellWidget(r, 0, holder)

            manual = b.get("manual") is not None

            kind = QComboBox(); kind.addItems(core.PEQ_TYPES)
            i = kind.findText(b.get("type", "peaking"))
            kind.setCurrentIndex(max(0, i))
            kind.setEnabled(not manual)
            kind.currentTextChanged.connect(self._emit)
            self.setCellWidget(r, 1, kind)

            for col, key, lo, hi, dec, step in [
                (2, "freq", 10.0, 24000.0, 1, 10.0),
                (3, "q", 0.1, 20.0, 3, 0.1),
                (4, "gain", -24.0, 24.0, 2, 0.5),
            ]:
                sb = QDoubleSpinBox()
                sb.setRange(lo, hi); sb.setDecimals(dec); sb.setSingleStep(step)
                sb.setValue(float(b.get(key, 0.0)))
                sb.setEnabled(not manual)
                sb.valueChanged.connect(self._emit)
                self.setCellWidget(r, col, sb)

            src = QTableWidgetItem("imported" if manual else "designed")
            src.setForeground(QColor(WARN if manual else MUTED))
            src.setFlags(Qt.ItemIsEnabled)
            self.setItem(r, 5, src)
        self._loading = False

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit()

    def store(self) -> list[dict[str, Any]]:
        for r, b in enumerate(self.bands):
            holder = self.cellWidget(r, 0)
            b["enabled"] = holder.findChild(QCheckBox).isChecked()
            if b.get("manual") is None:
                b["type"] = self.cellWidget(r, 1).currentText()
                b["freq"] = self.cellWidget(r, 2).value()
                b["q"] = self.cellWidget(r, 3).value()
                b["gain"] = self.cellWidget(r, 4).value()
        return self.bands


class ChannelEditor(QWidget):
    """Everything for one output (or input) channel."""

    changed = Signal()

    def __init__(self):
        super().__init__()
        self.chan: dict[str, Any] | None = None
        self.project: dict[str, Any] | None = None
        self.is_output = True
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # Where this channel sits in the signal path.
        self.chain = QLabel("")
        self.chain.setTextFormat(Qt.RichText)
        self.chain.setStyleSheet(
            f"background: {PANEL}; border: 1px solid {LINE}; "
            f"border-radius: 6px; padding: 7px 10px; font-size: 12px;")
        root.addWidget(self.chain)

        basics = QGroupBox("Channel")
        bl = QHBoxLayout(basics)
        self.gain = QDoubleSpinBox()
        self.gain.setRange(-127.0, 12.0); self.gain.setDecimals(2)
        self.gain.setSingleStep(0.5); self.gain.setSuffix(" dB")
        self.gain.valueChanged.connect(self._emit)
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.0, 80.0); self.delay.setDecimals(4)
        self.delay.setSingleStep(0.01); self.delay.setSuffix(" ms")
        self.delay.valueChanged.connect(self._emit)
        self.mute = QPushButton("Mute"); self.mute.setCheckable(True)
        self.mute.toggled.connect(self._emit)
        self.invert = QPushButton("Invert"); self.invert.setCheckable(True)
        self.invert.toggled.connect(self._emit)

        for label, wdg in [("Gain", self.gain), ("Delay", self.delay)]:
            lab = QLabel(label); lab.setObjectName("muted")
            bl.addWidget(lab); bl.addWidget(wdg)
        bl.addWidget(self.mute); bl.addWidget(self.invert)
        bl.addStretch(1)
        root.addWidget(basics)

        self.plot = ResponsePlot()
        root.addWidget(self.plot, 2)
        self.legend = QLabel("")
        self.legend.setObjectName("muted")
        root.addWidget(self.legend)

        xo = QHBoxLayout()
        self.xo_groups = [CrossoverGroup("Crossover group 1"),
                          CrossoverGroup("Crossover group 2")]
        for g in self.xo_groups:
            g.changed.connect(self._emit)
            xo.addWidget(g)
        self.xo_holder = QWidget(); self.xo_holder.setLayout(xo)
        root.addWidget(self.xo_holder)

        peq_box = QGroupBox("Parametric EQ")
        pl = QVBoxLayout(peq_box)
        self.peq = PeqTable()
        self.peq.changed.connect(self._emit)
        pl.addWidget(self.peq)
        root.addWidget(peq_box, 3)

    def _emit(self, *_):
        if not self._loading:
            self.store()
            self.changed.emit()

    def load(self, chan: dict[str, Any], is_output: bool):
        self._loading = True
        self.chan, self.is_output = chan, is_output
        self.gain.setValue(float(chan.get("gain", 0.0)))
        self.delay.setValue(float(chan.get("delay", 0.0)))
        self.delay.setVisible(is_output)
        self.invert.setVisible(is_output)
        self.mute.setChecked(bool(chan.get("mute")))
        self.invert.setChecked(bool(chan.get("invert")))
        self.xo_holder.setVisible(is_output)
        if is_output:
            for widget, group in zip(self.xo_groups, chan.get("crossover", [])):
                widget.load(group)
        self.peq.load(chan.get("peq", []))
        self._update_chain()
        self._loading = False
        self.refresh_plot()

    def _update_chain(self):
        """Render the signal path, highlighting the current stage."""
        if self.chan is None:
            self.chain.setText("")
            return

        def step(text, here=False):
            if here:
                return (f"<span style='color:{ACCENT}; font-weight:600'>"
                        f"{text}</span>")
            return f"<span style='color:{MUTED}'>{text}</span>"

        arrow = f"<span style='color:{LINE}'> &#8594; </span>"
        name = self.chan.get("name", "")

        if self.is_output:
            feeding = self._feeding_inputs()
            src = (", ".join(i["name"] for i in feeding) if feeding
                   else "no input routed")
            n_eq = sum(core.count_effective_peq(i.get("peq", []))
                       for i in feeding)
            parts = [
                step(src),
                step(f"EQ ({n_eq})" if n_eq else "EQ"),
                step("routing"),
                step(name, here=True),
                step("crossover"),
                step("PEQ"),
                step("gain / delay"),
                step("driver"),
            ]
        else:
            idx = self.chan.get("index")
            outs = [o["name"] for o in (self.project or {}).get("outputs", [])
                    for r in self.chan.get("routing", [])
                    if r.get("index") == o.get("index")
                    and r.get("enabled") and self.chan.get("index") == idx]
            dest = ", ".join(outs) if outs else "not routed"
            parts = [
                step("source"),
                step(name, here=True),
                step("EQ"),
                step("routing"),
                step(dest),
                step("crossover"),
                step("driver"),
            ]
        self.chain.setText(arrow.join(parts))

    def store(self):
        if self.chan is None:
            return
        self.chan["gain"] = self.gain.value()
        self.chan["mute"] = self.mute.isChecked()
        if self.is_output:
            self.chan["delay"] = self.delay.value()
            self.chan["invert"] = self.invert.isChecked()
            self.chan["crossover"] = [w.store() for w in self.xo_groups]
        self.chan["peq"] = self.peq.store()
        self.refresh_plot()

    def _feeding_inputs(self) -> list[dict[str, Any]]:
        """Inputs routed to the currently selected output.

        Input PEQ is applied *before* the crossover split, so it is part of
        what the driver on this output actually receives. A common house style
        is to flatten response at the input and keep the outputs as pure
        crossover, which makes the output-only curve an incomplete picture.
        """
        if not self.is_output or not self.project or self.chan is None:
            return []
        idx = self.chan.get("index")
        feeding = []
        for inp in self.project.get("inputs", []):
            for route in inp.get("routing", []):
                if route.get("index") == idx and route.get("enabled"):
                    feeding.append(inp)
                    break
        return feeding

    def refresh_plot(self, rate: int = 96000):
        if self.chan is None:
            return
        freqs = self.plot.freqs
        try:
            own = [core.peq_biquad(b, rate) for b in self.chan.get("peq", [])]
            if self.is_output:
                for g in self.chan.get("crossover", []):
                    own += [b for b in core.crossover_biquads(g, rate)
                            if not core.is_bypass(b)]

            curves = [(freqs, core.response_db(own, freqs, rate), ACCENT, False)]

            # Dashed: everything the driver sees, input EQ included.
            feeding = self._feeding_inputs()
            if feeding:
                upstream: list[dict[str, float]] = []
                for inp in feeding[:1]:      # one input's chain; sums are not modelled
                    upstream += [core.peq_biquad(b, rate)
                                 for b in inp.get("peq", [])]
                if any(not core.is_bypass(b) for b in upstream):
                    curves.append((freqs,
                                   core.response_db(upstream + own, freqs, rate),
                                   WARN, True))
            self.plot.set_curves(curves)

            if not self.is_output:
                self.legend.setText(
                    "Input EQ, applied before the crossover split - it reaches "
                    "every output this input is routed to.")
            elif len(curves) > 1:
                self.legend.setText(
                    "solid: this output's own chain     "
                    "dashed: what the driver receives, input EQ included")
            else:
                self.legend.setText("solid: this output's own chain")
        except Exception:                                # noqa: BLE001
            self.plot.set_curves([])
            self.legend.setText("")


class MasterStrip(QFrame):
    """Master volume / mute / source / preset, plus the panic control."""

    master_changed = Signal(dict)
    panic = Signal()

    def __init__(self):
        super().__init__()
        self._loading = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)

        self.device_label = QLabel("connecting...")
        self.device_label.setObjectName("muted")
        lay.addWidget(self.device_label)
        lay.addSpacing(20)

        lab = QLabel("Volume"); lab.setObjectName("muted")
        lay.addWidget(lab)
        self.volume = QSlider(Qt.Horizontal)
        self.volume.setRange(-1270, 0)          # tenths of a dB
        self.volume.setFixedWidth(220)
        self.volume.sliderReleased.connect(self._volume_committed)
        self.volume.valueChanged.connect(self._volume_preview)
        lay.addWidget(self.volume)
        self.volume_label = QLabel("--")
        self.volume_label.setFixedWidth(64)
        self.volume_label.setFont(QFont("monospace", 10))
        lay.addWidget(self.volume_label)

        self.mute = QPushButton("Mute"); self.mute.setCheckable(True)
        self.mute.clicked.connect(
            lambda: self._emit({"mute": self.mute.isChecked()}))
        lay.addWidget(self.mute)

        lab = QLabel("Source"); lab.setObjectName("muted")
        lay.addWidget(lab)
        self.source = QComboBox()
        self.source.currentTextChanged.connect(
            lambda t: self._emit({"source": t}) if t else None)
        lay.addWidget(self.source)

        lab = QLabel("Preset"); lab.setObjectName("muted")
        lay.addWidget(lab)
        self.preset = QComboBox()
        # Device presets are 0-indexed; miniDSP's own UI numbers them from 1.
        for i in range(4):
            self.preset.addItem(str(i + 1), i)
        self.preset.currentIndexChanged.connect(
            lambda i: self._emit({"preset": self.preset.itemData(i)}))
        lay.addWidget(self.preset)

        lay.addStretch(1)
        self.panic_btn = QPushButton("MUTE ALL")
        self.panic_btn.setObjectName("danger")
        self.panic_btn.clicked.connect(self.panic.emit)
        lay.addWidget(self.panic_btn)

    def _volume_preview(self, v):
        self.volume_label.setText(f"{v / 10.0:.1f} dB")

    def _volume_committed(self):
        self._emit({"volume": self.volume.value() / 10.0})

    def _emit(self, payload):
        if not self._loading:
            self.master_changed.emit(payload)

    def update_status(self, status: dict):
        self._loading = True
        m = status.get("master", {})
        if not self.volume.isSliderDown():
            self.volume.setValue(int(round(float(m.get("volume", 0.0)) * 10)))
            self._volume_preview(self.volume.value())
        self.mute.setChecked(bool(m.get("mute")))

        sources = status.get("available_sources") or []
        if not sources and self.source.count() == 0:
            # Some minidspd builds omit available_sources; fall back.
            sources = ["analog", "toslink", "spdif", "usb"]
        if sources and self.source.count() != len(sources):
            self.source.clear()
            self.source.addItems([s.lower() for s in sources])
        cur = str(m.get("source", "")).lower()
        idx = self.source.findText(cur)
        if idx >= 0 and idx != self.source.currentIndex():
            self.source.setCurrentIndex(idx)

        pi = self.preset.findData(int(m.get("preset", 0)))
        if pi >= 0:
            self.preset.setCurrentIndex(pi)
        self._loading = False


class RewDialog(QDialog):
    """Paste a REW biquad export and load it into a channel's PEQ bank."""

    def __init__(self, parent, channel_name: str):
        super().__init__(parent)
        self.setWindowTitle(f"Import REW filters into {channel_name}")
        self.resize(520, 380)
        lay = QVBoxLayout(self)
        hint = QLabel(
            "Paste REW's miniDSP biquad export. REW already writes miniDSP's "
            "sign convention, so coefficients are used verbatim.")
        hint.setObjectName("muted"); hint.setWordWrap(True)
        lay.addWidget(hint)
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText(
            "biquad1,\nb0=1.0000000,\nb1=-1.9808...,\nb2=...,\n"
            "a1=...,\na2=...,\n")
        self.text.setFont(QFont("monospace", 10))
        lay.addWidget(self.text, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def biquads(self):
        return core.parse_rew_biquads(self.text.toPlainText())


class MainWindow(QMainWindow):
    def __init__(self, opts):
        super().__init__()
        self.opts = opts
        self.tasks = TaskRunner(self)
        self.daemon = core.Daemon(opts.daemon, opts.device)
        self.amap: core.AddressMap | None = None
        self.readback: core.Readback | None = None
        self.project: dict[str, Any] | None = None
        self.project_path = Path(opts.project).expanduser()
        self.dirty = False
        self.have_read = False
        self._topology_dsp: int | None = None
        self._gains_at_read: dict[int, float] = {}

        self.setWindowTitle("minidsp-gui")
        self.resize(1220, 840)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.master = MasterStrip()
        self.master.master_changed.connect(self.on_master_change)
        self.master.panic.connect(self.on_panic)
        root.addWidget(self.master)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        # Safe, frequently-used actions live together on the left.
        self.read_btn = QPushButton("Read from device")
        self.read_btn.setToolTip(
            "Read live coefficients off the hardware and load them here.\n"
            "This only reads; nothing is written.")
        self.read_btn.clicked.connect(self.on_read)
        bar.addWidget(self.read_btn)

        self.xml_btn = QPushButton("Import Device Console XML...")
        self.xml_btn.setToolTip(
            "Load a preset exported from miniDSP Device Console.\n"
            "This is the only source of bypass state -- hardware readback\n"
            "cannot tell an active filter from a bypassed one.")
        self.xml_btn.clicked.connect(self.on_import_xml)
        bar.addWidget(self.xml_btn)

        self.rew_btn = QPushButton("Import REW...")
        self.rew_btn.clicked.connect(self.on_rew)
        bar.addWidget(self.rew_btn)

        for text, slot in (("Save project", self.on_save),
                           ("Load project", self.on_load)):
            b = QPushButton(text); b.clicked.connect(slot); bar.addWidget(b)

        bar.addStretch(1)
        self.warn_label = QLabel("")
        self.warn_label.setObjectName("muted")
        bar.addWidget(self.warn_label)

        # Apply writes to the hardware. Keep it well away from everything
        # else so it cannot be hit by accident while tuning.
        bar.addSpacing(28)
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {LINE};")
        bar.addWidget(sep)
        bar.addSpacing(28)

        self.apply_btn = QPushButton("Apply to device")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.setToolTip(
            "Write this project to the hardware, overwriting what is loaded.")
        self.apply_btn.clicked.connect(self.on_apply)
        bar.addWidget(self.apply_btn)
        holder = QWidget(); holder.setLayout(bar)
        root.addWidget(holder)

        splitter = QSplitter(Qt.Horizontal)

        self.chan_list = QListWidget()
        self.chan_list.setFixedWidth(190)
        self.chan_list.currentRowChanged.connect(self.on_select)
        splitter.addWidget(self.chan_list)

        self.editor = ChannelEditor()
        self.editor.changed.connect(self.on_edit)
        splitter.addWidget(self.editor)

        meters = QWidget()
        ml = QVBoxLayout(meters)
        ml.setContentsMargins(8, 8, 8, 8)
        ml.addWidget(QLabel("Inputs"))
        self.in_meters: list[MeterBar] = []
        self.in_box = QVBoxLayout(); ml.addLayout(self.in_box)
        ml.addSpacing(10)
        ml.addWidget(QLabel("Outputs"))
        self.out_meters: list[MeterBar] = []
        self.out_box = QVBoxLayout(); ml.addLayout(self.out_box)
        ml.addStretch(1)
        meters.setFixedWidth(210)
        splitter.addWidget(meters)

        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self.poll = QTimer(self)
        self.poll.timeout.connect(self.tick)
        self.connect_device()

    # ---- setup ----

    def connect_device(self):
        try:
            devices = self.daemon.devices()
        except core.DeviceError as exc:
            QMessageBox.critical(
                self, "Cannot reach minidspd",
                f"{exc}\n\nStart it with:\n    minidspd -c <config.toml>\n\n"
                "The daemon owns the USB connection; this app talks to it.")
            QTimer.singleShot(0, self.close)
            return
        if not devices:
            QMessageBox.critical(self, "No device",
                                 "minidspd reports no connected devices.")
            QTimer.singleShot(0, self.close)
            return

        info = devices[min(self.opts.device, len(devices) - 1)]
        name = info.get("product_name", "unknown")
        ver = info.get("version", {})
        self._topology_dsp = ver.get("dsp_version")
        status = self.daemon.status()
        n_in = len(status.get("input_levels", []))
        n_out = len(status.get("output_levels", []))

        self.amap = core.AddressMap.load(name)
        rate = self.amap.rate if self.amap else self.opts.rate
        if self.amap:
            self.readback = core.Readback(
                self.amap, cli=self.opts.cli, tcp=self.opts.tcp)
        else:
            self.read_btn.setEnabled(False)
            self.read_btn.setToolTip(
                f"No address map for '{name}'. Generate one with\n"
                "tools/gen_address_map.py <minidsp-rs checkout>")

        self.master.device_label.setText(
            f"{name}  sn {ver.get('serial')}  {n_in}in/{n_out}out  {rate} Hz")

        n_peq = len(self.amap.outputs[0].get("peq", [])) if self.amap else self.opts.peq
        self.project = self.load_project(n_in, n_out, n_peq or 10, rate)
        self.build_meters(n_in, n_out)
        self.refresh_list()
        self.chan_list.setCurrentRow(1)     # row 0 is a section header
        self.update_warning()
        self.poll.start(500)
        self.statusBar().showMessage(f"Connected to {name}", 4000)

    def load_project(self, n_in, n_out, n_peq, rate):
        if self.project_path.is_file():
            try:
                data = json.loads(self.project_path.read_text())
                if (len(data.get("outputs", [])) == n_out
                        and len(data.get("inputs", [])) == n_in):
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return core.new_project(n_in, n_out, n_peq, rate)

    def build_meters(self, n_in, n_out):
        for i in range(n_in):
            m = MeterBar(str(i + 1)); self.in_meters.append(m)
            self.in_box.addWidget(m)
        for i in range(n_out):
            m = MeterBar(str(i + 1)); self.out_meters.append(m)
            self.out_box.addWidget(m)

    @staticmethod
    def _summarise_output(out: dict[str, Any]) -> str:
        """One-line description of what an output is actually doing."""
        bits = []
        for g in out.get("crossover", []):
            if not g.get("enabled"):
                continue
            tag = "HP" if g.get("mode") == "highpass" else "LP"
            bits.append(f"{tag} {g.get('freq', 0):.0f}")
        pq = core.count_effective_peq(out.get("peq", []))
        if pq:
            bits.append(f"{pq} PEQ")
        return " / ".join(bits) if bits else "unused"

    def _add_header(self, text: str):
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)              # not selectable
        item.setForeground(QColor(MUTED))
        font = item.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        font.setCapitalization(QFont.AllUppercase)
        item.setFont(font)
        self.chan_list.addItem(item)

    def refresh_list(self):
        """Channel list ordered by signal flow: inputs first, then outputs."""
        prev = self.chan_list.currentItem()
        prev_key = prev.data(Qt.UserRole) if prev else None

        self.chan_list.blockSignals(True)
        self.chan_list.clear()

        self._add_header("Inputs · voicing")
        for i, inp in enumerate(self.project["inputs"]):
            pq = core.count_effective_peq(inp.get("peq", []))
            item = QListWidgetItem(
                f"{inp['name']}      {pq} PEQ" if pq else inp["name"])
            item.setData(Qt.UserRole, ("input", i))
            if inp.get("mute"):
                item.setForeground(QColor(MUTED))
            self.chan_list.addItem(item)

        self._add_header("Outputs · crossover")
        for i, out in enumerate(self.project["outputs"]):
            summary = self._summarise_output(out)
            item = QListWidgetItem(f"{out['name']}      {summary}")
            item.setData(Qt.UserRole, ("output", i))
            if out.get("mute") or summary == "unused":
                item.setForeground(QColor(MUTED))
            self.chan_list.addItem(item)

        self.chan_list.blockSignals(False)

        target = 1        # first real row, skipping the header
        if prev_key:
            for r in range(self.chan_list.count()):
                if self.chan_list.item(r).data(Qt.UserRole) == prev_key:
                    target = r
                    break
        self.chan_list.setCurrentRow(target)

    def current_channel(self):
        item = self.chan_list.currentItem()
        key = item.data(Qt.UserRole) if item else None
        if not key:
            return None, True
        kind, idx = key
        if kind == "output":
            return self.project["outputs"][idx], True
        return self.project["inputs"][idx], False

    # ---- events ----

    def on_select(self, _row):
        chan, is_out = self.current_channel()
        if chan is not None:
            self.editor.project = self.project
            self.editor.load(chan, is_out)
            self.editor.refresh_plot(int(self.project.get("rate", 96000)))

    def on_edit(self):
        self.dirty = True
        self.refresh_list()
        self.update_warning()

    def _remember_gains(self):
        """Record gains as they currently stand on the device."""
        self._gains_at_read = {o["index"]: float(o.get("gain", 0.0))
                               for o in self.project["outputs"]}

    def _gains_changed(self, tol: float = 0.005) -> bool:
        """Has the user edited any gain since the last read or import?"""
        for out in self.project["outputs"]:
            known = self._gains_at_read.get(out["index"])
            if known is None:
                return True
            if abs(float(out.get("gain", 0.0)) - known) > tol:
                return True
        return False

    def update_warning(self):
        if not self.have_read:
            self.warn_label.setText(
                "Not yet read from device - Apply would overwrite it")
            self.warn_label.setStyleSheet(f"color: {WARN};")
        elif self.dirty:
            self.warn_label.setText("Unapplied changes")
            self.warn_label.setStyleSheet(f"color: {ACCENT};")
        else:
            self.warn_label.setText("In sync")
            self.warn_label.setStyleSheet(f"color: {MUTED};")

    def tick(self):
        try:
            status = self.daemon.status()
        except core.DeviceError:
            return
        self.master.update_status(status)
        for m, v in zip(self.in_meters, status.get("input_levels", [])):
            m.set_value(v)
        for m, v in zip(self.out_meters, status.get("output_levels", [])):
            m.set_value(v)

    def on_master_change(self, payload):
        self.tasks.run(
            lambda: self.daemon.set_master(**payload),
            on_error=lambda e: self.statusBar().showMessage(e, 6000))

    def on_panic(self):
        self.tasks.run(
            lambda: self.daemon.set_master(mute=True),
            on_done=lambda _: self.statusBar().showMessage("MUTED", 5000),
            on_error=lambda e: self.statusBar().showMessage(e, 6000))

    def on_read(self):
        if self.readback is None:
            return
        self.read_btn.setEnabled(False)
        self.statusBar().showMessage("Reading coefficients from device...")
        n_out = len(self.project["outputs"])
        self.tasks.run(
            lambda: self.readback.read_all(n_out),
            on_done=self._read_done, on_error=self._read_failed)

    def _read_done(self, readings):
        core.apply_readback(self.project, readings)
        self._remember_gains()
        self.have_read = True
        self.dirty = False
        self.read_btn.setEnabled(True)
        self.refresh_list()
        self.on_select(self.chan_list.currentRow())
        self.update_warning()
        active = sum(1 for r in readings
                     for g in r.get("crossover", []) if g.get("active"))
        self.statusBar().showMessage(
            f"Read {len(readings)} outputs, {active} active crossover groups",
            8000)

    def _read_failed(self, msg):
        self.read_btn.setEnabled(True)
        self.statusBar().showMessage(f"Read failed: {msg}", 8000)
        QMessageBox.warning(self, "Read failed", msg)

    def on_apply(self):
        if not self.have_read:
            resp = QMessageBox.warning(
                self, "Overwrite device configuration?",
                "You have not read the current configuration from the device.\n\n"
                "Applying now writes this project over whatever is loaded, "
                "including any crossover you set up elsewhere.\n\n"
                "Read from device first?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes)
            if resp == QMessageBox.Yes:
                self.on_read()
                return
            if resp == QMessageBox.Cancel:
                return

        self.apply_btn.setEnabled(False)
        # The device snaps gain to a 1/256 linear grid, so a plain write can
        # land up to ~0.3 dB off. Closing the loop costs extra round-trips, so
        # only do it for gains the user has actually changed since the last
        # read -- gains that came from the device are already where they land.
        verify = self._gains_changed()
        project = copy.deepcopy(self.project)
        self.tasks.run(
            lambda: core.apply_project(self.daemon, project,
                                       readback=self.readback,
                                       verify_gains=verify),
            on_done=self._apply_done, on_error=self._apply_failed)

    def _apply_done(self, _):
        self.apply_btn.setEnabled(True)
        self.dirty = False
        self._remember_gains()
        self.update_warning()
        self.save_project()
        self.statusBar().showMessage("Applied to device", 5000)

    def _apply_failed(self, msg):
        self.apply_btn.setEnabled(True)
        self.statusBar().showMessage(f"Apply failed: {msg}", 8000)
        QMessageBox.warning(self, "Apply failed", msg)

    def on_import_xml(self):
        """Load a Device Console preset export.

        Readback recovers coefficients but not bypass state, because bypass is
        set by command 0x19 and has no readable address. An export carries it,
        so importing one is the only way to know which filters are really in
        circuit -- which is why this counts as having read the device.
        """
        if self.amap is None:
            QMessageBox.warning(
                self, "No address map",
                "Importing needs an address map for this device.\n"
                "Generate one with tools/gen_address_map.py.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Device Console preset", str(Path.home()),
            "Device Console export (*.xml);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
            parsed = core.parse_device_console_xml(text)
        except OSError as exc:
            QMessageBox.warning(self, "Import failed", str(exc))
            return
        if not parsed["filters"]:
            QMessageBox.warning(
                self, "Nothing imported",
                "No <filter> elements found. Is that a Device Console export?")
            return

        dsp = parsed.get("dsp_version")
        mine = self._topology_dsp
        if dsp is not None and mine is not None and dsp != mine:
            resp = QMessageBox.warning(
                self, "Different DSP version",
                f"That export is for dsp_version {dsp}, but this device "
                f"reports {mine}.\n\nAddresses may not line up. Import anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return

        stats = core.apply_device_console_xml(self.project, parsed, self.amap)
        self._remember_gains()
        self.have_read = True
        self.dirty = True
        self.refresh_list()
        self.on_select(self.chan_list.currentRow())
        self.update_warning()
        self.statusBar().showMessage(
            f"Imported {stats['crossover']} crossover groups and "
            f"{stats['peq']} PEQ bands across {stats['outputs']} outputs "
            f"({stats['bypassed']} bypassed)", 10000)

    def on_rew(self):
        chan, _ = self.current_channel()
        if chan is None:
            return
        dlg = RewDialog(self, chan["name"])
        if dlg.exec() != QDialog.Accepted:
            return
        filters = dlg.biquads()
        if not filters:
            QMessageBox.warning(self, "Nothing imported",
                                "No biquads found in that text.")
            return
        applied = 0
        for slot, coeff in enumerate(filters):
            if slot >= len(chan["peq"]):
                break
            chan["peq"][slot]["manual"] = coeff
            chan["peq"][slot]["enabled"] = True
            applied += 1
        self.on_select(self.chan_list.currentRow())
        self.on_edit()
        extra = (f", {len(filters) - applied} did not fit"
                 if len(filters) > applied else "")
        self.statusBar().showMessage(
            f"Imported {applied} biquad(s){extra}", 6000)

    def on_save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", str(self.project_path), "JSON (*.json)")
        if path:
            self.project_path = Path(path)
            self.save_project()
            self.statusBar().showMessage(f"Saved {path}", 5000)

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load project", str(self.project_path.parent), "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            QMessageBox.warning(self, "Load failed", str(exc))
            return
        if len(data.get("outputs", [])) != len(self.project["outputs"]):
            QMessageBox.warning(
                self, "Wrong shape",
                "That project was made for a device with a different "
                "number of outputs.")
            return
        self.project = data
        self.project_path = Path(path)
        self.dirty = True
        self.refresh_list()
        self.on_select(self.chan_list.currentRow())
        self.update_warning()

    def save_project(self):
        try:
            self.project_path.parent.mkdir(parents=True, exist_ok=True)
            self.project_path.write_text(json.dumps(self.project, indent=2))
        except OSError as exc:
            self.statusBar().showMessage(f"Could not save: {exc}", 6000)

    def closeEvent(self, ev):
        self.poll.stop()
        # Join background threads before teardown; a QThread destroyed while
        # still running aborts the process.
        self.tasks.shutdown()
        if self.project:
            self.save_project()
        ev.accept()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--daemon", default="http://127.0.0.1:5380",
                    help="minidspd HTTP API base URL")
    ap.add_argument("--tcp", default="127.0.0.1:5333",
                    help="minidspd TCP server, used for readback")
    ap.add_argument("--cli", default="minidsp",
                    help="path to the minidsp CLI binary")
    ap.add_argument("--device", type=int, default=0)
    ap.add_argument("--rate", type=int, default=96000,
                    help="fallback DSP rate if no address map is available")
    ap.add_argument("--peq", type=int, default=10,
                    help="fallback PEQ band count")
    ap.add_argument("--project",
                    default=str(Path.home() / ".config" / "minidsp-gui"
                                / "project.json"))
    opts = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("minidsp-gui")
    app.setStyleSheet(STYLE)
    win = MainWindow(opts)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
