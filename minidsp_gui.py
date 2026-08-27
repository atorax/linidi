#!/usr/bin/env python3
"""
LiniDi -- a desktop tuning front-end for miniDSP hardware on Linux.

    python3 minidsp_gui.py

Talks to the device directly over USB and needs nothing else installed -- no
daemon, no external binaries. If the USB device cannot be opened (usually a
missing udev rule) it falls back to minidspd's REST API and the `minidsp` CLI,
for anyone who already has minidsp-rs set up.

License: Apache-2.0
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import (QObject, QPointF, QRectF, QSize, QThread,
                            QTimer, Qt, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QListWidget, QListWidgetItem, QMainWindow,
    QMessageBox, QPlainTextEdit, QPushButton, QSlider, QStackedWidget,
    QStatusBar, QTableWidget, QTableWidgetItem, QTextBrowser, QVBoxLayout,
    QWidget,
)

import minidsp_core as core
import minidsp_native as native

# --------------------------------------------------------------------------
# Palette
# --------------------------------------------------------------------------

BG      = "#16181d"
PANEL   = "#1e2128"
PANEL2  = "#252932"
LINE    = "#333844"
MINOR_GRID = "#23262e"
FG      = "#e6e8ec"
MUTED   = "#8b93a3"
# Three states, one rule:
#   ACTIVE    orange   the thing you are on right now
#   ACCENT    blue     on, but not what you are looking at
#   MUTED     grey     off, or not applicable
ACTIVE  = "#f0883e"
ACCENT  = "#4f9cf9"
OK      = "#3fb950"
WARN    = "#d29922"
DANGER  = "#f0533f"

# One colour per PEQ band, so a band's row in the table, its own curve on the
# plot and the marker sitting on that curve are all obviously the same filter.
# Hues are spread rather than pretty: ten of these are on screen at once and
# the only thing that matters is telling them apart.
PEQ_COLOURS = [
    "#4f9cf9",  # blue
    "#f0533f",  # red
    "#3fb950",  # green
    "#d29922",  # amber
    "#a371f7",  # purple
    "#2dd4bf",  # teal
    "#f778ba",  # pink
    "#ff9f4a",  # orange
    "#9ad14b",  # lime
    "#7f8ea8",  # slate
]


def peq_colour(index: int) -> str:
    """Colour for band `index`, wrapping if there are more bands than hues."""
    return PEQ_COLOURS[index % len(PEQ_COLOURS)]


def _readable_on(colour: QColor) -> QColor:
    """Black or white, whichever stays legible on `colour`."""
    lum = (0.299 * colour.red() + 0.587 * colour.green()
           + 0.114 * colour.blue())
    return QColor("#12141a") if lum > 140 else QColor("#ffffff")


def peq_badge(index: int, size: int = 20, active: bool = True) -> QPixmap:
    """A numbered disc identifying one PEQ band.

    A band that is switched off is drawn hollow rather than in its colour, so
    the column doubles as a legend: filled discs are the filters actually in
    circuit, and you can see which at a glance without reading the checkboxes.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    colour = QColor(peq_colour(index))
    r = QRectF(1.0, 1.0, size - 2.0, size - 2.0)
    if active:
        p.setBrush(colour)
        p.setPen(Qt.NoPen)
        p.drawEllipse(r)
        p.setPen(_readable_on(colour))
    else:
        dim = QColor(colour)
        dim.setAlpha(90)
        p.setBrush(Qt.NoBrush)
        pen = QPen(dim); pen.setWidthF(1.4)
        p.setPen(pen)
        p.drawEllipse(r)
        p.setPen(QColor(MUTED))
    f = QFont()
    f.setPointSizeF(max(7.0, size * 0.5))
    f.setBold(active)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, str(index))
    p.end()
    return pm


# Vertical padding on a list item. Rows that hold a widget have to add this to
# their size hint, so both sides read it from here rather than from a literal
# in the stylesheet that nothing else can see.
LIST_ITEM_PAD_Y = 6

# The response plot's frequency axis, precomputed: _fx runs a few thousand
# times per repaint.
LOG_F_LO = math.log10(20.0)
LOG_F_SPAN = math.log10(20000.0) - LOG_F_LO

# Said in two places -- a crossover group and a PEQ band -- about the same
# condition, so it is written once.
UNKNOWN_BYPASS_TIP = (
    "Bypass state is unknown: it was read from the hardware, which cannot\n"
    "report it.\nLeft untouched on Apply. Click to set it explicitly.")

STYLE = f"""
/* Only real containers paint a background. Setting it on bare QWidget makes
   every label and checkbox draw the window colour over whatever panel it is
   sitting on, which shows up as a dark patch behind text on cards and inside
   group boxes. */
QWidget {{ color: {FG}; font-family: system-ui, sans-serif; font-size: 13px; }}
QMainWindow, QDialog {{ background: {BG}; }}
QLabel, QCheckBox, QGroupBox::title {{ background: transparent; }}
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
QPushButton#danger {{ background: {PANEL2}; color: #ffffff;
                      border: 1px solid {DANGER}; font-weight: 700;
                      letter-spacing: .04em; padding: 5px 12px; }}
QPushButton#danger:hover {{ background: #2c313c; }}
QPushButton#danger[spent="true"] {{ color: {DANGER}; }}
QPushButton:checked {{ background: {ACTIVE}; color: #1a1206;
                       border-color: {ACTIVE}; }}
QComboBox, QDoubleSpinBox, QPlainTextEdit, QListWidget, QTableWidget {{
    background: {PANEL2}; border: 1px solid {LINE}; border-radius: 4px;
    padding: 2px 4px; selection-background-color: {ACCENT};
}}
QListWidget::item {{ padding: {LIST_ITEM_PAD_Y}px 8px; border: 0; }}
QListWidget::item:selected {{ background: transparent; }}
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
QPushButton#tab {{ background: transparent; border: 0; padding: 3px 2px;
                   border-bottom: 2px solid transparent; color: {MUTED}; }}
QPushButton#tab:hover {{ color: {FG}; }}
QPushButton#tab:checked {{ color: {ACTIVE}; border-bottom-color: {ACTIVE};
                           font-weight: 600; }}
"""


# --------------------------------------------------------------------------
# Icons
# --------------------------------------------------------------------------

def bundle_dir() -> Path:
    """The root of the source tree, or of a frozen build's unpacked files."""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else Path(__file__).resolve().parent


def asset_dir() -> Path:
    """Where bundled assets live, in a source tree or inside a frozen build."""
    return bundle_dir() / "icons"


def help_icon(size: int = 18, colour: str = MUTED) -> QIcon:
    """A circled question mark, painted rather than shipped as a file."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(colour))
    pen.setWidthF(1.5)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(QRectF(1.0, 1.0, size - 2.0, size - 2.0))
    f = QFont()
    f.setPointSizeF(size * 0.52)
    f.setBold(True)
    p.setFont(f)
    p.drawText(pm.rect(), Qt.AlignCenter, "?")
    p.end()
    return QIcon(pm)


def doc_sections() -> list[tuple[str, str]]:
    """The README, split on its headings, for the help window's menu.

    Falls back to a short note if the file is not beside the program, so a
    build that forgot to bundle it opens a window that explains itself rather
    than an empty one.
    """
    path = bundle_dir() / "README.md"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [("About", "# LiniDi\n\nDocumentation was not bundled "
                          "with this build. The README is in the source "
                          "repository.")]
    sections, title, buf = [], "Overview", []
    for line in text.splitlines():
        if line.startswith("## "):
            sections.append((title, "\n".join(buf).strip()))
            title, buf = line[3:].strip(), []
        else:
            buf.append(line)
    sections.append((title, "\n".join(buf).strip()))
    return [(t, _strip_images(b)) for t, b in sections if b]


def _strip_images(md: str) -> str:
    """Drop image markup before rendering the README in the help window.

    The badges are for the repository page. Here they resolve to nothing --
    the widget will not fetch over the network, and should not -- so each one
    draws a broken-image square instead.
    """
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def logo_pixmap(height: int = 26) -> QPixmap:
    """The wordmark, scaled to sit in the device card.

    The artwork is white on transparent, which is why it looks blank in a
    file manager: thumbnailers paint transparency white. It is meant for the
    dark card it sits on. Returns an empty pixmap if the file is missing, and
    the caller hides the label rather than showing a broken image.
    """
    path = asset_dir() / "LiniDi.png"
    if not path.exists():
        return QPixmap()
    pm = QPixmap(str(path))
    if pm.isNull():
        return QPixmap()
    return pm.scaledToHeight(height, Qt.SmoothTransformation)


def speaker_icon(size: int = 22, muted: bool = False,
                 body: str = FG, slash: str = DANGER) -> QIcon:
    """The speaker glyph for the current state.

    icons/sound.png and icons/mute.png each depict one state; there is no
    third. Falls back to a painted glyph if the artwork is missing, so an
    unbundled asset degrades rather than breaking the window.
    """
    path = asset_dir() / ("mute.png" if muted else "sound.png")
    if path.is_file():
        pm = QPixmap(str(path))
        if not pm.isNull():
            return QIcon(pm.scaled(size, size, Qt.KeepAspectRatio,
                                   Qt.SmoothTransformation))
    return _painted_speaker_icon(size, muted, body, slash)


def led_icon(size: int = 12, colour: str = "#000000",
             ring: str = "#000000") -> QIcon:
    """A small round indicator, drawn like a panel LED.

    Dark when the device is passing sound and lit when it is muted, so the
    button reads as an indicator rather than as a second mute glyph beside the
    one that already shows state.
    """
    scale = 4
    n = size * scale
    pm = QPixmap(n, n)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(ring))
    pen.setWidthF(max(1.0, 0.10 * n))
    p.setPen(pen)
    p.setBrush(QColor(colour))
    inset = 0.14 * n
    p.drawEllipse(QRectF(inset, inset, n - 2 * inset, n - 2 * inset))
    p.end()
    return QIcon(pm.scaled(size, size, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation))


def _painted_speaker_icon(size: int, muted: bool, body: str,
                          slash: str) -> QIcon:
    """Fallback glyph, drawn rather than loaded."""
    scale = 4                          # supersample, then smooth-scale
    n = size * scale
    pm = QPixmap(n, n)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(body))

    # cabinet plus cone
    p.drawRect(int(0.14 * n), int(0.38 * n), int(0.14 * n), int(0.24 * n))
    cone = QPolygonF([
        QPointF(0.27 * n, 0.40 * n), QPointF(0.47 * n, 0.20 * n),
        QPointF(0.47 * n, 0.80 * n), QPointF(0.27 * n, 0.60 * n),
    ])
    p.drawPolygon(cone)

    if not muted:
        pen = QPen(QColor(body))
        pen.setWidthF(0.055 * n)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        for r in (0.13, 0.23, 0.33):
            box = QRectF((0.47 - r) * n, (0.5 - r) * n, 2 * r * n, 2 * r * n)
            p.drawArc(box, -55 * 16, 110 * 16)
    else:
        pen = QPen(QColor(slash))
        pen.setWidthF(0.10 * n)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawLine(QPointF(0.16 * n, 0.16 * n), QPointF(0.86 * n, 0.86 * n))

    p.end()
    return QIcon(pm.scaled(size, size, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation))


# --------------------------------------------------------------------------
# Background workers -- device I/O must never block the UI thread
# --------------------------------------------------------------------------

class Worker(QObject):
    """Runs one callable on a QThread and reports the outcome."""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
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

    def run(self, fn, on_done=None, on_error=None):
        """Run `fn` -- which takes no arguments -- off the UI thread.

        Arguments used to be forwarded, but sat after two optional callbacks,
        so a positional argument bound itself to on_done and was called as a
        callback instead. Every caller passes a closure; that is the contract
        now.
        """
        thread = QThread()
        worker = Worker(fn)
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
        """Stop and join everything still in flight, before the window dies.

        A thread that does not stop in time keeps its entry. Clearing the list
        regardless would drop the last reference to a running QThread, and the
        collector destroying one of those aborts the process -- the very thing
        this class is arranged to avoid. Better to leak a thread on the way
        out than to crash on the way out.
        """
        for thread, _worker in list(self._live):
            thread.quit()
        for pair in list(self._live):
            thread, worker = pair
            if thread.wait(5000):
                worker.deleteLater()
                thread.deleteLater()
                self._live.remove(pair)


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
        self.setFixedHeight(15)

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
            col = (DANGER if self.value > -3
                   else WARN if self.value > -12 else OK)
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
        self.bands: list[dict[str, Any]] = []
        self.phases: list[dict[str, Any]] = []
        self.freqs = core.log_freqs(280)
        self.setMinimumHeight(200)

    def set_curves(self, curves):
        self.curves = curves
        self.update()

    def set_phases(self, phases):
        """Phase traces, {degs, colour, label}, on their own right axis."""
        self.phases = phases
        self.update()

    def _py(self, deg, h):
        return h - (deg + 180.0) / 360.0 * h

    def set_bands(self, bands):
        """Individual PEQ curves, each tagged with its band number.

        Each entry is {index, colour, dbs, mark_f, mark_db}: the band's own
        response across the plot's frequencies, and where to put its marker.
        """
        self.bands = bands
        self.update()

    def _fx(self, f, w):
        return (math.log10(f) - LOG_F_LO) / (LOG_F_SPAN) * w

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
        minor = QPen(QColor(MINOR_GRID)); minor.setWidth(1)

        # 6 dB minor rules first, so the 12 dB majors draw over them.
        for db in range(int(self.DB_MIN), int(self.DB_MAX) + 1, 6):
            if db % 12 == 0:
                continue
            y = self._fy(db, h)
            p.setPen(minor)
            p.drawLine(QPointF(0, y), QPointF(w, y))

        label_bottom = h - 3                       # where frequencies sit
        for db in range(int(self.DB_MIN), int(self.DB_MAX) + 1, 12):
            y = self._fy(db, h)
            p.setPen(grid)
            p.drawLine(QPointF(0, y), QPointF(w, y))
            # The bottom rule shares its row with the frequency labels, so
            # skip its number rather than printing two strings on top of
            # each other.
            if y < label_bottom - 10:
                p.setPen(QColor(MUTED))
                p.drawText(QPointF(3, y - 3), f"{db:+d}")

        for f in (20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000):
            x = self._fx(f, w)
            p.setPen(grid)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            p.setPen(QColor(MUTED))
            p.drawText(QPointF(x + 3, label_bottom),
                       f"{f // 1000}k" if f >= 1000 else str(f))

        zero = QPen(QColor("#4a5163")); zero.setWidth(1)
        p.setPen(zero)
        y0 = self._fy(0.0, h)
        p.drawLine(QPointF(0, y0), QPointF(w, y0))

        # Individual bands sit under the summed response: they are context for
        # it, and a 2px sum drawn over them stays the thing you read first.
        for band in self.bands:
            colour = QColor(band["colour"])
            colour.setAlpha(170)
            pen = QPen(colour); pen.setWidth(1)
            p.setPen(pen)
            p.drawPath(self._trace(band["dbs"], w, h))

        for freqs, dbs, colour, dashed in self.curves:
            pen = QPen(QColor(colour))
            pen.setWidth(2)
            if dashed:
                pen.setStyle(Qt.DashLine)
                pen.setWidth(1)
            p.setPen(pen)
            # Break the trace where it leaves the window instead of clamping
            # it: a clamped curve draws a flat line along the bottom edge,
            # which reads as a response that is there rather than one that has
            # gone off-scale.
            p.drawPath(self._trace(dbs, w, h))

        self._draw_phase(p, w, h)
        self._draw_markers(p, w, h)

    def _draw_phase(self, p: QPainter, w: int, h: int):
        """Phase traces and the right-hand degree scale they are read against.

        Wrapped to +/-180 like every other phase plot, and the trace is broken
        where it wraps rather than drawn as a vertical line across the graph,
        which would read as a real feature.
        """
        if not self.phases:
            return
        p.setFont(QFont("monospace", 8))
        for deg in (-180, -90, 0, 90, 180):
            y = self._py(deg, h)
            if deg:                       # 0 already has the magnitude rule
                p.setPen(QPen(QColor(MINOR_GRID), 1, Qt.DotLine))
                p.drawLine(QPointF(0, y), QPointF(w, y))
            p.setPen(QColor(MUTED))
            txt = f"{deg:+d}\u00b0"
            p.drawText(QPointF(w - 4 - p.fontMetrics().horizontalAdvance(txt),
                               max(10, y - 3)), txt)

        for tr in self.phases:
            pen = QPen(QColor(tr["colour"]))
            pen.setWidth(1)
            pen.setStyle(Qt.DotLine)
            p.setPen(pen)
            path = QPainterPath()
            drawing = False
            prev = None
            mask = tr.get("mask") or [True] * len(tr["degs"])
            for f, deg, keep in zip(self.freqs, tr["degs"], mask):
                if not keep:
                    drawing = False
                    prev = None
                    continue
                if prev is not None and abs(deg - prev) > 180.0:
                    drawing = False           # a wrap, not a jump in phase
                prev = deg
                pt = QPointF(self._fx(f, w), self._py(deg, h))
                if drawing:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    drawing = True
            p.drawPath(path)

    def _trace(self, dbs, w, h) -> QPainterPath:
        """A curve, broken wherever it leaves the window.

        Clamping instead would draw a flat line along the bottom edge, which
        reads as a response that is there rather than one that has gone
        off-scale.
        """
        path = QPainterPath()
        drawing = False
        for f, db in zip(self.freqs, dbs):
            if self.DB_MIN <= db <= self.DB_MAX:
                pt = QPointF(self._fx(f, w), self._fy(db, h))
                if drawing:
                    path.lineTo(pt)
                else:
                    path.moveTo(pt)
                    drawing = True
            else:
                drawing = False
        return path

    def _draw_markers(self, p: QPainter, w: int, h: int):
        """Numbered discs pinning each band to its place on the plot."""
        if not self.bands:
            return
        r = 8.0
        font = QFont()
        font.setPointSizeF(8.0)
        font.setBold(True)
        p.setFont(font)
        placed: list[QPointF] = []
        for band in self.bands:
            x = self._fx(max(20.0, min(20000.0, band["mark_f"])), w)
            y = self._fy(band["mark_db"], h)
            # Keep the disc inside the plot even when its band runs off the
            # top or bottom, and nudge it clear of one already drawn in the
            # same spot so two filters at one frequency stay countable.
            y = max(r + 1, min(h - r - 1, y))
            for prev in placed:
                if abs(prev.x() - x) < 2 * r and abs(prev.y() - y) < 2 * r:
                    y = max(r + 1, min(h - r - 1, prev.y() - 2 * r - 1))
            pt = QPointF(x, y)
            placed.append(pt)

            colour = QColor(band["colour"])
            p.setPen(QPen(QColor(BG), 2))
            p.setBrush(colour)
            p.drawEllipse(pt, r, r)
            p.setPen(_readable_on(colour))
            p.drawText(QRectF(x - r, y - r, 2 * r, 2 * r),
                       Qt.AlignCenter, str(band["index"]))


class CrossoverGroup(QGroupBox):
    """Editor for one crossover group (4 biquad slots)."""

    changed = Signal()

    def __init__(self, title: str):
        super().__init__(title)
        self.data: dict[str, Any] = {}
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)

        self.enabled = QCheckBox("Enabled")
        self.enabled.setTristate(True)
        self.enabled.clicked.connect(self._enabled_clicked)
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

    def _enabled_clicked(self, _checked=False):
        """A click resolves an unknown bypass into a definite one."""
        if self.enabled.checkState() == Qt.PartiallyChecked:
            self.enabled.setCheckState(Qt.Checked)
        self.enabled.setTristate(False)
        self.enabled.setToolTip("")
        self.data["bypass_source"] = "user"

    def _on_alignment(self, *_):
        self._refresh_orders()
        self._emit()

    def _refresh_orders(self):
        align = self.alignment.currentText()
        prev = self.order.currentText()
        self.order.blockSignals(True)
        self.order.clear()
        # Every order that fits the four biquad slots a group has. LR36 and
        # the odd Butterworths above 3 were missing; Device Console offers
        # them, they fit, and they round-trip.
        if align == "linkwitz-riley":
            items = [(str(o), f"LR{o * 6}") for o in (2, 4, 6, 8)]
        elif align == "bessel":
            items = [(str(o), f"{o * 6} dB/oct") for o in range(2, 9)]
        elif align == "butterworth":
            items = [(str(o), f"{o * 6} dB/oct")
                     for o in (1, 2, 3, 4, 5, 6, 7, 8)]
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
        known = group.get("bypass_source", "default") != "unknown"
        self.enabled.setTristate(not known)
        if known:
            self.enabled.setCheckState(
                Qt.Checked if group.get("enabled") else Qt.Unchecked)
            self.enabled.setToolTip("")
        else:
            self.enabled.setCheckState(Qt.PartiallyChecked)
            self.enabled.setToolTip(UNKNOWN_BYPASS_TIP)
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
        if self.enabled.checkState() != Qt.PartiallyChecked:
            self.data["enabled"] = self.enabled.isChecked()
            self.data["bypass_source"] = "user"
        self.data["alignment"] = self.alignment.currentText()
        self.data["mode"] = self.mode.currentText()
        data = self.order.currentData()
        self.data["order"] = int(data) if data else 4
        self.data["freq"] = self.freq.value()
        return self.data


def provenance_item(b: dict[str, Any]) -> QTableWidgetItem:
    """Where a band's values came from, as a table cell.

    Both tabs show this, so the rules live in one place. Hand-typed is checked
    first: it is the most recent word on what the band is and overrides
    wherever it originally came from, since reporting "from config" after the
    numbers have been replaced would be stale.
    """
    state = b.get("read_state")
    manual = b.get("manual") is not None
    # Checked before anything else and regardless of where the band came
    # from. An unstable section is not a provenance question, it is a fault,
    # and it has to survive the table being rebuilt.
    if manual and b.get("enabled") and not core.biquad_is_stable(b["manual"]):
        item = QTableWidgetItem("UNSTABLE")
        item.setForeground(QColor(DANGER))
        item.setToolTip(
            "The poles are on or outside the unit circle. This section would "
            "run away rather than filter, and its output goes to a driver. "
            "Apply refuses to write it.")
        item.setFlags(Qt.ItemIsEnabled)
        return item
    if manual and b.get("manual_source") == "user":
        label, colour = "hand-typed", WARN
        tip = ("Coefficients typed in directly on the Biquad tab. Type, "
               "frequency, Q and gain no longer drive this band.")
    elif state == "unreadable":
        label, colour = "not readable", DANGER
        tip = ("The device does not report PEQ contents, so this band was not "
               "read.\nThe values shown are from the project, not from the "
               "hardware.\nImport a Device Console export to load the "
               "real ones.")
    elif state == "config":
        label, colour = "from config", ACCENT
        tip = ("Loaded from a Device Console export. The device cannot report "
               "PEQ, so a config file is the authoritative source.")
    elif manual:
        label, colour, tip = "imported", WARN, "Raw coefficients"
    elif state == "read":
        label, colour, tip = "from device", OK, "Decoded from hardware"
    else:
        label, colour, tip = "designed", MUTED, "Designed locally"
    item = QTableWidgetItem(label)
    item.setForeground(QColor(colour))
    item.setToolTip(tip)
    item.setFlags(Qt.ItemIsEnabled)
    return item


class TabStrip(QWidget):
    """Plain text tabs: the active one is orange and carries an underline."""

    selected = Signal(int)

    def __init__(self, labels: list[str]):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 2)
        lay.setSpacing(20)
        self.buttons: list[QPushButton] = []
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setObjectName("tab")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setFlat(True)
            b.clicked.connect(lambda _c=False, n=i: self.select(n))
            lay.addWidget(b)
            self.buttons.append(b)
        lay.addStretch(1)
        self.buttons[0].setChecked(True)

    def select(self, i: int):
        for n, b in enumerate(self.buttons):
            b.setChecked(n == i)
        self.selected.emit(i)


class PeqTable(QTableWidget):
    """Editor for a channel's PEQ bank."""

    changed = Signal()
    COLS = ["#", "On", "Type", "Freq (Hz)", "Q", "Gain (dB)", "Source"]
    C_NUM, C_ON, C_TYPE, C_FREQ, C_Q, C_GAIN, C_SRC = range(len(COLS))

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        # The badge column holds one glyph and should not share in the width.
        header.setSectionResizeMode(self.C_NUM, QHeaderView.Fixed)
        header.setMinimumSectionSize(26)   # else the header sets a floor
        self.setColumnWidth(self.C_NUM, 34)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.bands: list[dict[str, Any]] = []
        self.rate = 96000            # set from the project by ChannelEditor
        self._loading = False

    def load(self, bands: list[dict[str, Any]]):
        self._loading = True
        self.bands = bands
        self.setRowCount(len(bands))
        for r, b in enumerate(bands):
            idx = b.get("index", r)
            badge = QLabel()
            badge.setAlignment(Qt.AlignCenter)
            badge.setPixmap(peq_badge(idx, 20, bool(b.get("enabled"))))
            badge.setToolTip(
                f"Band {idx}"
                + ("" if b.get("enabled") else " - switched off, so it is not "
                   "drawn on the response"))
            self.setCellWidget(r, self.C_NUM, badge)

            on = QCheckBox()
            known = b.get("bypass_source", "default") != "unknown"
            on.setTristate(not known)
            if known:
                on.setCheckState(
                    Qt.Checked if b.get("enabled") else Qt.Unchecked)
            else:
                on.setCheckState(Qt.PartiallyChecked)
                on.setToolTip(UNKNOWN_BYPASS_TIP)
            on.clicked.connect(
                lambda _c=False, cb=on: self._resolve(cb))
            on.toggled.connect(lambda _c, row=r: self._sync_badge(row))
            on.toggled.connect(self._emit)
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0); hl.addWidget(on)
            hl.setAlignment(Qt.AlignCenter)
            self.setCellWidget(r, self.C_ON, holder)

            manual = b.get("manual") is not None

            kind = QComboBox(); kind.addItems(core.PEQ_TYPES)
            i = kind.findText(b.get("type", "peaking"))
            kind.setCurrentIndex(max(0, i))
            kind.setEnabled(not manual)
            kind.currentTextChanged.connect(self._emit)
            self.setCellWidget(r, self.C_TYPE, kind)

            for col, key, lo, hi, dec, step in [
                (self.C_FREQ, "freq", 10.0, 24000.0, 1, 10.0),
                (self.C_Q, "q", 0.1, 20.0, 3, 0.1),
                (self.C_GAIN, "gain", -24.0, 24.0, 2, 0.5),
            ]:
                sb = QDoubleSpinBox()
                sb.setRange(lo, hi)
                sb.setDecimals(dec)
                sb.setSingleStep(step)
                sb.setValue(float(b.get(key, 0.0)))
                sb.setEnabled(not manual)
                sb.valueChanged.connect(self._emit)
                self.setCellWidget(r, col, sb)

            self.setItem(r, self.C_SRC, provenance_item(b))
        self._loading = False

    def _sync_badge(self, r: int):
        """Fill or hollow a badge as its band is switched on or off."""
        if r >= len(self.bands):
            return
        b = self.bands[r]
        holder = self.cellWidget(r, self.C_ON)
        badge = self.cellWidget(r, self.C_NUM)
        if holder is None or badge is None:
            return
        active = holder.findChild(QCheckBox).checkState() == Qt.Checked
        badge.setPixmap(peq_badge(b.get("index", r), 20, active))

    @staticmethod
    def _resolve(cb):
        """A click turns an unknown bypass into a definite one."""
        if cb.checkState() == Qt.PartiallyChecked:
            cb.setCheckState(Qt.Checked)
        cb.setTristate(False)
        cb.setToolTip("")

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit()

    def store(self) -> list[dict[str, Any]]:
        for r, b in enumerate(self.bands):
            holder = self.cellWidget(r, self.C_ON)
            if holder is None:          # rows not built yet; nothing to read
                continue
            cb = holder.findChild(QCheckBox)
            if cb.checkState() != Qt.PartiallyChecked:
                b["enabled"] = cb.isChecked()
                b["bypass_source"] = "user"
            if b.get("manual") is None:
                b["type"] = self.cellWidget(r, self.C_TYPE).currentText()
                b["freq"] = self.cellWidget(r, self.C_FREQ).value()
                b["q"] = self.cellWidget(r, self.C_Q).value()
                b["gain"] = self.cellWidget(r, self.C_GAIN).value()
        return self.bands


class BiquadTable(QTableWidget):
    """The same bands as the PEQ tab, as the coefficients they compile to.

    miniDSP adds the feedback terms rather than subtracting them, so a1 and a2
    carry the opposite sign to the textbook form. REW's miniDSP export already
    uses this convention, which is why its numbers paste across untouched.

    Typing here makes a band manual: its coefficients stop being derived from
    type, frequency, Q and gain, and are written as given. Designed bands show
    what they currently compile to, so the tab doubles as a way to see what a
    filter actually is.
    """

    changed = Signal()
    COLS = ["#", "On", "b0", "b1", "b2", "a1", "a2", "Source"]
    C_NUM, C_ON, C_B0, C_B1, C_B2, C_A1, C_A2, C_SRC = range(len(COLS))
    KEYS = ("b0", "b1", "b2", "a1", "a2")

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        header = self.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Stretch)
        header.setMinimumSectionSize(26)
        header.setSectionResizeMode(self.C_NUM, QHeaderView.Fixed)
        self.setColumnWidth(self.C_NUM, 34)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.bands: list[dict[str, Any]] = []
        self.rate = 96000
        self._loading = False

    def load(self, bands: list[dict[str, Any]]):
        self._loading = True
        self.bands = bands
        self.setRowCount(len(bands))
        for r, b in enumerate(bands):
            idx = b.get("index", r)
            badge = QLabel()
            badge.setAlignment(Qt.AlignCenter)
            badge.setPixmap(peq_badge(idx, 20, bool(b.get("enabled"))))
            badge.setToolTip(f"Band {idx}")
            self.setCellWidget(r, self.C_NUM, badge)

            on = QCheckBox()
            known = b.get("bypass_source", "default") != "unknown"
            on.setTristate(not known)
            on.setCheckState(
                (Qt.Checked if b.get("enabled") else Qt.Unchecked) if known
                else Qt.PartiallyChecked)
            on.clicked.connect(lambda _c=False, cb=on: PeqTable._resolve(cb))
            on.toggled.connect(lambda _c, row=r: self._enabled_changed(row))
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0); hl.addWidget(on)
            hl.setAlignment(Qt.AlignCenter)
            self.setCellWidget(r, self.C_ON, holder)

            # What this band compiles to today, designed or manual alike.
            coeffs = core.peq_biquad(b, self.rate)
            for col, key in zip((self.C_B0, self.C_B1, self.C_B2,
                                 self.C_A1, self.C_A2), self.KEYS):
                sb = QDoubleSpinBox()
                sb.setRange(-64.0, 64.0)
                sb.setDecimals(7)
                sb.setSingleStep(0.0001)
                sb.setValue(float(coeffs.get(key, 0.0)))
                sb.setFont(QFont("monospace", 9))
                # Connected after setValue: loading is not editing.
                sb.valueChanged.connect(lambda _v, row=r: self._edited(row))
                self.setCellWidget(r, col, sb)

            self.setItem(r, self.C_SRC, provenance_item(b))
        self._loading = False

    def row_coeffs(self, r: int) -> dict[str, float]:
        return {k: self.cellWidget(r, c).value()
                for c, k in zip((self.C_B0, self.C_B1, self.C_B2,
                                 self.C_A1, self.C_A2), self.KEYS)}

    def _enabled_changed(self, r: int):
        if self._loading or r >= len(self.bands):
            return
        b = self.bands[r]
        cb = self.cellWidget(r, self.C_ON).findChild(QCheckBox)
        if cb.checkState() != Qt.PartiallyChecked:
            b["enabled"] = cb.isChecked()
            b["bypass_source"] = "user"
        self.cellWidget(r, self.C_NUM).setPixmap(
            peq_badge(b.get("index", r), 20, cb.checkState() == Qt.Checked))
        self.changed.emit()

    def _edited(self, r: int):
        """A typed coefficient makes the band manual, and flags a runaway."""
        if self._loading or r >= len(self.bands):
            return
        b = self.bands[r]
        coeffs = self.row_coeffs(r)
        b["manual"] = coeffs
        b["manual_source"] = "user"
        self.setItem(r, self.C_SRC, provenance_item(b))
        self.changed.emit()

    def store(self) -> list[dict[str, Any]]:
        """Edits are written as they are made, so there is nothing to flush."""
        return self.bands


class ChainBar(QWidget):
    """Where this channel sits in the signal path, as controls not prose.

    Names only. Each stage is a button that navigates -- to the channel
    feeding this one, or to the control that owns that part of the chain.
    The stages used to carry their current values as well, which was a second
    copy of settings already on the page the bar sits above, and wide enough
    to need a scrollbar of its own.
    """

    navigate = Signal(str, int)      # (kind, index) -> select that channel
    focus_stage = Signal(str)        # a stage within the current channel

    STAGE_CSS = f"""
        QPushButton {{
            background: {PANEL2}; border: 1px solid {LINE};
            border-radius: 5px; padding: 4px 9px; text-align: left;
            color: {FG};
        }}
        QPushButton:hover {{ border-color: {ACCENT}; background: #2c313c; }}
        QPushButton:disabled {{ color: {MUTED}; background: transparent;
                                border-color: transparent; }}
    """
    CURRENT_CSS = f"""
        QPushButton {{
            background: {ACTIVE}; border: 1px solid {ACTIVE};
            border-radius: 5px; padding: 4px 9px; color: #1a1206;
            font-weight: 700;
        }}
    """

    def __init__(self):
        super().__init__()
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)
        self.setSizePolicy(self.sizePolicy().horizontalPolicy(),
                           self.sizePolicy().Policy.Fixed)

    def _clear(self):
        # Unparent as well as delete: deleteLater() is deferred, so a widget
        # merely taken out of the layout stays a child and keeps rendering,
        # which stacks every previous chain on top of the current one.
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

    def set_stages(self, stages: list[dict[str, Any]]):
        """Rebuild the bar.

        Each stage is {label, sizer, target, current, tooltip}, all optional
        but `label`. `sizer` is the widest text that stage can ever hold, so
        the row keeps its geometry instead of jumping when a name changes;
        `target` makes the stage clickable, either "input:N"/"output:N" to
        navigate or "focus:name" to jump to a control on this page.
        """
        self._clear()
        for i, st in enumerate(stages):
            if i:
                arrow = QLabel("→")
                arrow.setStyleSheet(f"color: {LINE}; font-size: 15px;")
                self._lay.addWidget(arrow)

            text = st["label"]
            btn = QPushButton(text)
            # Fixed width per stage, measured from the widest value that stage
            # can ever hold. Sizing to the current text makes the whole bar
            # jump every time a number changes.
            sizer = st.get("sizer") or text
            fm = btn.fontMetrics()
            btn.setFixedWidth(fm.horizontalAdvance(sizer) + 26)
            btn.setCursor(Qt.PointingHandCursor if st.get("target")
                          else Qt.ArrowCursor)
            btn.setFlat(True)
            if st.get("current"):
                btn.setStyleSheet(self.CURRENT_CSS)
                btn.setEnabled(False)
            else:
                btn.setStyleSheet(self.STAGE_CSS)
                btn.setEnabled(bool(st.get("target")))
            if st.get("tooltip"):
                btn.setToolTip(st["tooltip"])
            target = st.get("target")
            if target:
                btn.clicked.connect(
                    lambda _=False, t=target: self._activate(t))
            self._lay.addWidget(btn)
        self._lay.addStretch(1)

    def _activate(self, target: str):
        kind, _, value = target.partition(":")
        if kind in ("input", "output"):
            self.navigate.emit(kind, int(value))
        else:
            self.focus_stage.emit(value)


class RoutingTable(QTableWidget):
    """Which outputs an input feeds, and at what gain.

    This is the mixer matrix: on the device, one `Mixer_<in>_<out>_status`
    flag and one `Mixer_<in>_<out>` gain per pair. It is edited per input
    rather than as a full grid because that matches the direction signal
    actually travels -- one source fanning out to several drivers.
    """

    changed = Signal()
    COLS = ["To output", "On", "Gain (dB)"]

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        hh = self.horizontalHeader()
        # Only the destination name absorbs slack; the checkbox and the gain
        # field have a fixed natural width and stretching them just pads air.
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.setTextElideMode(Qt.ElideRight)
        self.setSelectionMode(QTableWidget.NoSelection)
        self.routes: list[dict[str, Any]] = []
        self._loading = False

    def load(self, routes: list[dict[str, Any]],
             outputs: list[dict[str, Any]]):
        self._loading = True
        self.routes = routes
        self.setRowCount(len(routes))
        for r, route in enumerate(routes):
            idx = route.get("index", r)
            name = next((o["name"] for o in outputs
                         if o.get("index") == idx), f"Out {idx + 1}")
            summary = ""
            target = next((o for o in outputs if o.get("index") == idx), None)
            if target:
                active = [g for g in target.get("crossover", [])
                          if g.get("enabled")]
                if active:
                    summary = " · " + "/".join(
                        f"{'HP' if g['mode'] == 'highpass' else 'LP'}"
                        f"{g['freq']:.0f}" for g in active)

            item = QTableWidgetItem(name + summary)
            item.setToolTip(f"{name}{summary.replace(chr(183), '')}".strip()
                            or name)
            item.setFlags(Qt.ItemIsEnabled)
            if not route.get("enabled"):
                item.setForeground(QColor(MUTED))
            self.setItem(r, 0, item)

            on = QCheckBox()
            on.setChecked(bool(route.get("enabled")))
            on.toggled.connect(self._emit)
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0); hl.addWidget(on)
            hl.setAlignment(Qt.AlignCenter)
            self.setCellWidget(r, 1, holder)

            sb = QDoubleSpinBox()
            sb.setRange(-127.0, 12.0); sb.setDecimals(1)
            sb.setSingleStep(0.5); sb.setSuffix(" dB")
            sb.setValue(float(route.get("gain", 0.0)))
            sb.valueChanged.connect(self._emit)
            self.setCellWidget(r, 2, sb)
        self._loading = False

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit()

    def store(self) -> list[dict[str, Any]]:
        for r, route in enumerate(self.routes):
            holder = self.cellWidget(r, 1)
            if holder is None:
                continue
            route["enabled"] = holder.findChild(QCheckBox).isChecked()
            route["gain"] = self.cellWidget(r, 2).value()
        return self.routes


def format_hz(f: float) -> str:
    """A corner frequency, short enough to sit in the navigator column."""
    if f >= 10000:
        return f"{f / 1000:.0f}k"
    if f >= 1000:
        return f"{f / 1000:.1f}k".replace(".0k", "k")
    return f"{f:.0f}"


def passband(chan: dict[str, Any]) -> str:
    """What band of audio this output actually passes.

    Listing the filters ("HP2600/LP4500") describes how the crossover is
    built; the band describes what comes out of the jack, which is what you
    are asking when you scan the channel list. Two highpasses on one output
    pass the higher corner, two lowpasses the lower one, so the band is the
    intersection rather than a list.
    """
    hp = lp = None
    for g in chan.get("crossover", []):
        if not g.get("enabled"):
            continue
        f = float(g.get("freq", 0) or 0)
        if f <= 0:
            continue
        if g.get("mode") == "highpass":
            hp = f if hp is None else max(hp, f)
        else:
            lp = f if lp is None else min(lp, f)
    if hp and lp:
        return f"{format_hz(hp)}–{format_hz(lp)}"
    if hp:
        return f"≥{format_hz(hp)}"
    if lp:
        return f"≤{format_hz(lp)}"
    return ""


class ChannelRow(QWidget):
    """One channel in the navigator: its name, what it does, and its mute.

    A single line, with the mute at the trailing edge. The name column is a
    fixed width so the summaries line up rather than starting wherever the
    name happened to end.
    """

    toggled = Signal(bool)

    ROW_HEIGHT = 28

    def __init__(self, name: str, detail: str, muted: bool, dim: bool,
                 name_width: int):
        super().__init__()
        self.channel_name = name
        self.full_detail = detail
        self.dim = dim
        self.setFixedHeight(self.ROW_HEIGHT)
        self.setObjectName("navRow")
        # A bare QWidget ignores a stylesheet background unless asked to draw
        # one, which is why the cards were invisible at first.
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.set_selected(False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 0, 6, 0)
        lay.setSpacing(6)

        self.name = QLabel(name)
        self.name.setFixedWidth(name_width)
        lay.addWidget(self.name)

        self.detail = QLabel(detail)
        self.detail.setObjectName("muted")
        lay.addWidget(self.detail, 1)

        self.mute = QPushButton()
        self.mute.setCheckable(True)
        self.mute.setFixedSize(24, 24)
        self.mute.setIconSize(QSize(16, 16))
        self.mute.setCursor(Qt.PointingHandCursor)
        self.mute.setStyleSheet(
            "QPushButton { background: transparent; border: 0; }"
            f"QPushButton:hover {{ background: {PANEL2};"
            f" border-radius: 4px; }}")
        # Clicking the mute must not also change which channel is selected.
        self.mute.clicked.connect(
            lambda: self.toggled.emit(self.mute.isChecked()))
        lay.addWidget(self.mute, 0, Qt.AlignVCenter)
        self.set_muted(muted)

    def set_muted(self, muted: bool):
        """Show a channel as muted, without rebuilding the row.

        Flipping one icon used to go through a full list rebuild, which also
        re-selected a row and so reloaded the whole editor for a channel that
        had not changed.
        """
        self.mute.setChecked(muted)
        self.mute.setIcon(speaker_icon(16, muted=muted))
        self.mute.setToolTip(
            f"{'Unmute' if muted else 'Mute'} {self.channel_name}")
        self.name.setStyleSheet(
            f"color: {MUTED};" if muted or self.dim else "")

    def set_selected(self, on: bool):
        """Each row is a card, darker than the panel it sits on.

        Selection is drawn by the card rather than by the list item behind it:
        the widget covers that item completely, so anything the view painted
        there would be hidden.
        """
        edge = ACTIVE if on else "#2b3140"
        fill = PANEL2 if on else BG
        self.setStyleSheet(
            f"#navRow {{ background: {fill}; border: 1px solid {edge};"
            f" border-radius: 6px; }}")

    def elide_detail(self, text: str):
        """Shorten the summary to whatever width is left for it."""
        fm = self.detail.fontMetrics()
        self.detail.setText(
            fm.elidedText(text, Qt.ElideRight, max(20, self.detail.width())))
        self.detail.setToolTip(text)


class ChannelEditor(QWidget):
    """Everything for one output (or input) channel."""

    changed = Signal()
    navigate = Signal(str, int)      # (kind, index) from a chain link

    def __init__(self):
        super().__init__()
        self.chan: dict[str, Any] | None = None
        self.project: dict[str, Any] | None = None
        self.is_output = True
        self._loading = False

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        # Where this channel sits in the signal path.
        self.chain = ChainBar()
        self.chain.navigate.connect(self.navigate.emit)
        self.chain.focus_stage.connect(self._focus_stage)
        chain_frame = QFrame()
        chain_frame.setStyleSheet(
            f"background: {PANEL}; border: 1px solid {LINE}; "
            f"border-radius: 6px;")
        cf = QHBoxLayout(chain_frame)
        cf.setContentsMargins(8, 6, 8, 6)
        cf.addWidget(self.chain)
        root.addWidget(chain_frame)

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
        self.invert = QPushButton("Invert"); self.invert.setCheckable(True)
        self.invert.toggled.connect(self._emit)

        self.show_phase = QCheckBox("Phase")
        self.show_phase.setToolTip(
            "Overlay phase on the response, against a right-hand scale.\n"
            "On an output this also draws whatever it crosses over with, so "
            "the two can be compared through the overlap.")
        self.show_phase.toggled.connect(lambda _v: self.refresh_plot())
        bl.addWidget(self.show_phase)
        bl.addSpacing(14)

        gain_label = QLabel("Gain"); gain_label.setObjectName("muted")
        # The delay label is held so it can be hidden along with its box.
        # Inputs have no delay address on this hardware, and a label with
        # nothing beside it reads as a control that has stopped working.
        self.delay_label = QLabel("Delay")
        self.delay_label.setObjectName("muted")
        bl.addWidget(gain_label); bl.addWidget(self.gain)
        bl.addWidget(self.delay_label); bl.addWidget(self.delay)
        bl.addWidget(self.invert)
        bl.addStretch(1)
        root.addWidget(basics)

        self.plot = ResponsePlot()
        root.addWidget(self.plot, 2)
        self.legend = QLabel("")
        self.legend.setObjectName("muted")
        root.addWidget(self.legend)

        # Crossover and routing live in the right-hand column, not here:
        # vertical space is the scarce dimension on a widescreen display, and
        # the plot and PEQ table are what actually benefit from height.
        self.side = QWidget()
        side_l = QVBoxLayout(self.side)
        side_l.setContentsMargins(0, 0, 0, 0)

        xo = QVBoxLayout()          # stacked, the side column is narrow
        self.xo_groups = [CrossoverGroup("Crossover group 1"),
                          CrossoverGroup("Crossover group 2")]
        for g in self.xo_groups:
            g.changed.connect(self._emit)
            xo.addWidget(g)
        self.xo_holder = QWidget(); self.xo_holder.setLayout(xo)
        side_l.addWidget(self.xo_holder)

        self.routing_box = QGroupBox("Routing - outputs this input feeds")
        rl = QVBoxLayout(self.routing_box)
        self.routing = RoutingTable()
        self.routing.changed.connect(self._emit)
        rl.addWidget(self.routing)
        side_l.addWidget(self.routing_box, 1)
        side_l.addStretch(0)

        # Two views of one set of bands: the parameters, or the coefficients
        # they compile to. Tabs rather than a second panel, because it is the
        # same ten filters either way.
        peq_box = QGroupBox()
        pl = QVBoxLayout(peq_box)
        self.peq_tabs = TabStrip(["Parametric EQ", "Biquad"])
        self.peq_tabs.selected.connect(self._show_peq_tab)
        pl.addWidget(self.peq_tabs)

        self.peq = PeqTable()
        self.peq.changed.connect(self._emit)
        self.bq = BiquadTable()
        self.bq.changed.connect(self._emit)
        self.peq_stack = QStackedWidget()
        self.peq_stack.addWidget(self.peq)
        self.peq_stack.addWidget(self.bq)
        pl.addWidget(self.peq_stack, 1)
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
        self.delay_label.setVisible(is_output)
        self.invert.setVisible(is_output)
        self.invert.setChecked(bool(chan.get("invert")))
        self.xo_holder.setVisible(is_output)
        self.routing_box.setVisible(not is_output)
        if is_output:
            groups = chan.get("crossover", [])
            for widget, group in zip(self.xo_groups, groups):
                widget.load(group)
        else:
            self.routing.load(chan.get("routing", []),
                              (self.project or {}).get("outputs", []))
        rate = int((self.project or {}).get("rate", 96000))
        self.peq.rate = self.bq.rate = rate
        self._active_peq().load(chan.get("peq", []))
        self._update_chain()
        self._loading = False
        self.refresh_plot()

    def _active_peq(self):
        """Whichever of the two tabs is currently showing."""
        return self.peq_stack.currentWidget()

    def _show_peq_tab(self, i: int):
        """Swap tabs, carrying edits across rather than losing them."""
        if self.chan is not None:
            self.store()                       # capture the tab being left
        self.peq_stack.setCurrentIndex(i)
        if self.chan is not None:
            self._active_peq().load(self.chan.get("peq", []))

    def _focus_stage(self, stage: str):
        """Jump to the control that owns a stage of the chain."""
        target = {
            "crossover": self.xo_groups[0].freq if self.xo_groups else None,
            "peq": self._active_peq(),
            "routing": self.routing,
            "basics": self.gain,
        }.get(stage)
        if target is not None:
            target.setFocus(Qt.OtherFocusReason)

    def _update_chain(self):
        """The signal path this channel sits in, as a breadcrumb.

        Names only. The bar used to carry each stage's current value as well,
        which was a second copy of settings already on screen, in a strip too
        narrow to hold them.
        """
        if self.chan is None:
            self.chain.set_stages([])
            return

        name = self.chan.get("name", "")
        proj = self.project or {}
        outputs = proj.get("outputs", [])
        inputs = proj.get("inputs", [])
        stages: list[dict[str, Any]] = []

        # Only the stages naming channels can change width, and only when a
        # channel is renamed, so those are the only ones that need sizing.
        # The rest are constant words.
        all_in = ", ".join(i.get("name", "In") for i in inputs) or "In 1"
        all_out = ", ".join(o.get("name", "Out") for o in outputs) or "Out 1"
        widest_name = max((c.get("name", "") for c in outputs + inputs),
                          key=len, default="Out 8")

        if self.is_output:
            feeding = self._feeding_inputs()
            if feeding:
                first = feeding[0]
                stages.append({
                    "label": ", ".join(i["name"] for i in feeding),
                    "sizer": all_in,
                    "target": f"input:{first['index']}",
                    "tooltip": "Go to the input feeding this output",
                })
                stages.append({
                    "label": "EQ",
                    "target": f"input:{first['index']}",
                    "tooltip": "Input EQ, applied before the crossover split",
                })
                stages.append({
                    "label": "routing",
                    "target": f"input:{first['index']}",
                    "tooltip": "Which outputs each input feeds",
                })
            else:
                stages.append({"label": "no input routed", "sizer": all_in,
                               "tooltip": "Nothing is routed to this output"})

            stages.append({"label": name, "sizer": widest_name,
                           "current": True})

            stages.append({"label": "crossover",
                           "target": "focus:crossover"})

            stages.append({"label": "PEQ",
                           "target": "focus:peq"})

            stages.append({"label": "out", "target": "focus:basics"})
            stages.append({"label": "driver"})
        else:
            stages.append({"label": "source",
                           "tooltip": "Selected on the master strip above"})
            stages.append({"label": name, "sizer": widest_name,
                           "current": True})

            stages.append({"label": "EQ",
                           "target": "focus:peq"})

            dests = [o for o in outputs
                     for r in self.chan.get("routing", [])
                     if r.get("index") == o.get("index") and r.get("enabled")]
            stages.append({"label": "routing",
                           "target": "focus:routing"})
            if dests:
                stages.append({
                    "label": ", ".join(o["name"] for o in dests),
                    "sizer": all_out,
                    "target": f"output:{dests[0]['index']}",
                    "tooltip": "Go to the first output this input feeds",
                })
            else:
                stages.append({"label": "not routed", "sizer": all_out})
            stages.append({"label": "driver"})

        self.chain.set_stages(stages)

    def store(self):
        if self.chan is None:
            return
        self.chan["gain"] = self.gain.value()
        if self.is_output:
            self.chan["delay"] = self.delay.value()
            self.chan["invert"] = self.invert.isChecked()
            self.chan["crossover"] = [w.store() for w in self.xo_groups]
        else:
            self.chan["routing"] = self.routing.store()
        # Only the visible tab is read. The hidden one holds widgets from
        # whenever it was last shown, and flushing those would write stale
        # values over edits made on the tab actually in front of the user.
        self.chan["peq"] = self._active_peq().store()
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

    def refresh_plot(self):
        """Redraw the response for the loaded channel.

        The rate comes from the project rather than from an argument. It used
        to be a parameter defaulting to 96000, and only the caller that runs
        on channel selection passed the real one -- so on a device with a
        different internal rate every edit redrew the plot against the wrong
        one, and selecting a channel corrected it again.
        """
        if self.chan is None:
            return
        rate = int((self.project or {}).get("rate", 96000))
        freqs = self.plot.freqs
        try:
            own = [core.peq_biquad(b, rate) for b in self.chan.get("peq", [])]
            if self.is_output:
                for g in self.chan.get("crossover", []):
                    own += [b for b in core.crossover_biquads(g, rate)
                            if not core.is_bypass(b)]

            curves = [(freqs, core.response_db(own, freqs, rate),
                       ACTIVE, False)]

            # Dashed: everything the driver sees, input EQ included.
            feeding = self._feeding_inputs()
            if feeding:
                upstream: list[dict[str, float]] = []
                # One input's chain; summing is not modelled.
                for inp in feeding[:1]:
                    upstream += [core.peq_biquad(b, rate)
                                 for b in inp.get("peq", [])]
                if any(not core.is_bypass(b) for b in upstream):
                    curves.append((freqs,
                                   core.response_db(upstream + own, freqs,
                                                    rate),
                                   ACCENT, True))
            phases = (self._phase_curves(freqs, rate)
                      if self.show_phase.isChecked() else [])
            self.plot.set_curves(curves)
            self.plot.set_bands(self._band_curves(freqs, rate))
            self.plot.set_phases(phases)

            if not self.is_output:
                self.legend.setText(
                    "Input EQ, applied before the crossover split - it "
                    "reaches "
                    "every output this input is routed to.")
            elif len(curves) > 1:
                self.legend.setText(
                    "solid: this output's own chain     "
                    "dashed: what the driver receives, input EQ included")
            else:
                self.legend.setText("solid: this output's own chain")

            if phases:
                # Reuses what was just computed: building these again for the
                # legend meant designing and evaluating every filter on the
                # channel, and its crossover partner, a second time on every
                # repaint.
                names = [t["label"] for t in phases]
                extra = "     dotted: phase, right-hand scale"
                if len(names) > 1:
                    extra += " - " + " vs ".join(names)
                self.legend.setText(self.legend.text() + extra)
        except Exception:                                # noqa: BLE001
            # A paint path: a channel mid-edit can hold values no filter can
            # be designed from, and blanking the plot is better than letting
            # the exception reach the event loop.
            self.plot.set_curves([])
            self.plot.set_bands([])
            self.plot.set_phases([])
            self.legend.setText("")

    def _chain_biquads(self, chan: dict[str, Any], rate: int) -> list[dict]:
        """Every section this channel actually applies, PEQ and crossover."""
        out = [core.peq_biquad(b, rate) for b in chan.get("peq", [])]
        for g in chan.get("crossover", []):
            out += [b for b in core.crossover_biquads(g, rate)
                    if not core.is_bypass(b)]
        return [b for b in out if not core.is_bypass(b)]

    def _crossover_partners(self) -> list[dict[str, Any]]:
        """Outputs this one crosses over with.

        Anything fed by an input that also feeds this output: those are the
        drivers whose passbands meet this one, and whose phase through the
        overlap decides whether they sum or fight.
        """
        if not self.is_output or not self.project or self.chan is None:
            return []
        idx = self.chan.get("index")
        feeders = [i for i in self.project.get("inputs", [])
                   for r in i.get("routing", [])
                   if r.get("index") == idx and r.get("enabled")]
        partners, seen = [], {idx}
        for inp in feeders:
            for r in inp.get("routing", []):
                if not r.get("enabled") or r.get("index") in seen:
                    continue
                for o in self.project.get("outputs", []):
                    if o.get("index") == r.get("index"):
                        seen.add(o["index"])
                        partners.append(o)
        return partners

    def _phase_curves(self, freqs, rate: int) -> list[dict[str, Any]]:
        out = []
        for chan, colour in [(self.chan, ACTIVE)] + [
                (p, ACCENT) for p in self._crossover_partners()]:
            bqs = self._chain_biquads(chan, rate)
            out.append({
                "degs": core.response_phase(
                    bqs, freqs, rate,
                    delay_ms=float(chan.get("delay", 0.0) or 0.0),
                    invert=bool(chan.get("invert"))),
                # Phase where a channel passes nothing is not wrong, it is
                # meaningless, and drawing it fills the plot with sweeps
                # through bands the driver never sees. Keep the part of each
                # trace that is in its own passband; what is left is the
                # overlap, which is the region the question is about.
                "mask": self._passband_mask(bqs, freqs, rate),
                "colour": colour,
                "label": chan.get("name", "?"),
            })
        return out

    @staticmethod
    def _passband_mask(bqs, freqs, rate: int, floor_db: float = 30.0):
        """True where a chain is within `floor_db` of its own peak."""
        mags = core.response_db(bqs, freqs, rate)
        peak = max(mags) if mags else 0.0
        return [m > peak - floor_db for m in mags]

    def _band_curves(self, freqs, rate: int) -> list[dict[str, Any]]:
        """Each PEQ band on its own, so a filter can be found on the plot.

        Only bands that are switched on: a bypassed one is a flat line at
        0 dB, and ten of those stacked on the zero rule with markers on top
        would bury the bands that are doing something.
        """
        out = []
        for r, b in enumerate(self.chan.get("peq", [])):
            if not b.get("enabled"):
                continue
            bq = core.peq_biquad(b, rate)
            if core.is_bypass(bq):
                continue
            dbs = core.response_db([bq], freqs, rate)
            idx = b.get("index", r)
            if b.get("manual"):
                # Typed coefficients leave the frequency field describing
                # whatever the band used to be, so mark where the filter
                # actually does the most instead of where that field points.
                nearest = max(range(len(dbs)), key=lambda i: abs(dbs[i]))
                f0 = freqs[nearest]
            else:
                # Mark the band at its own corner, on its own curve, rather
                # than at its nominal gain: for a shelf or a pass filter those
                # are not the same point, and the marker has to sit on the
                # line it labels.
                f0 = float(b.get("freq", 1000.0))
                nearest = min(range(len(freqs)),
                              key=lambda i: abs(freqs[i] - f0))
            out.append({
                "index": idx,
                "colour": peq_colour(idx),
                "dbs": dbs,
                "mark_f": f0,
                "mark_db": dbs[nearest],
            })
        return out


class MasterStrip(QFrame):
    """Master volume / mute / source / preset, plus the panic control."""

    master_changed = Signal(dict)
    panic = Signal()

    def __init__(self):
        super().__init__()
        self._loading = False
        self.setObjectName("deviceCard")
        self.setStyleSheet(
            f"#deviceCard {{ background: {PANEL}; border: 1px solid {LINE};"
            f" border-radius: 7px; }}")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 7, 12, 7)
        self._lay = lay

        self.logo = QLabel()
        # The card is 44px with 7px margins, so 30 is the most the
        # wordmark can take; the artwork carries its own small margin
        # inside that.
        pm = logo_pixmap(30)
        if pm.isNull():
            self.logo.hide()
        else:
            self.logo.setPixmap(pm)
            self.logo.setFixedWidth(pm.width())
        lay.addWidget(self.logo, 0, Qt.AlignVCenter)
        lay.addSpacing(16)

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
        self.volume.valueChanged.connect(self._volume_changed)
        lay.addWidget(self.volume)
        self.volume_label = QLabel("--")
        self.volume_label.setFixedWidth(64)
        self.volume_label.setFont(QFont("monospace", 10))
        lay.addWidget(self.volume_label)

        # Master mute sits here, as MUTE ALL. There used to be a speaker
        # button in this slot as well, but it toggled the same master mute --
        # two controls for one function, each able to look like it disagreed
        # with the other.
        lay.addSpacing(8)
        self.muted = False
        self.panic_btn = QPushButton("  MUTE ALL")
        self.panic_btn.setObjectName("danger")
        self.panic_btn.setIcon(led_icon(12, "#000000"))
        self.panic_btn.setIconSize(QSize(12, 12))
        self.panic_btn.setToolTip("Mute the device immediately")
        self.panic_btn.clicked.connect(self.panic.emit)
        lay.addWidget(self.panic_btn)
        lay.addSpacing(14)

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

    def add_trailing(self, *widgets, spacing: int = 0):
        """Append controls to the right-hand end of the card."""
        if spacing:
            self._lay.addSpacing(spacing)
        for wdg in widgets:
            self._lay.addWidget(wdg)

    def _volume_preview(self, v):
        self.volume_label.setText(f"{v / 10.0:.1f} dB")

    def _volume_changed(self, v):
        """Show every change, and send the ones that are not mid-drag.

        Only the end of a drag used to be sent, so the arrow keys, the mouse
        wheel and a click on the groove all moved the slider and updated the
        readout without the device ever being told. A drag still sends once,
        on release, rather than on every pixel.
        """
        self._volume_preview(v)
        if not self.volume.isSliderDown():
            self._volume_committed()

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
        muted = bool(m.get("mute"))
        self.muted = muted
        # MUTE ALL keeps its label and signals state by colour alone: a
        # dark LED and white text while sound is passing, both red once
        # the device is muted. The label never changes, so the button
        # never looks like a different control.
        self.panic_btn.setIcon(led_icon(12, DANGER if muted else "#000000"))
        self.panic_btn.setProperty("spent", muted)
        self.panic_btn.setToolTip("Click to unmute" if muted
                                  else "Mute the device immediately")
        self.panic_btn.style().unpolish(self.panic_btn)
        self.panic_btn.style().polish(self.panic_btn)

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
        buttons = QDialogButtonBox(QDialogButtonBox.Ok
                                   | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def biquads(self):
        return core.parse_rew_biquads(self.text.toPlainText())



class OfflineBrowser(QTextBrowser):
    """A text view that cannot fetch anything off the machine.

    Rich text will happily resolve remote images and stylesheets while
    rendering. Nothing here needs that, and an application that controls
    audio hardware has no business making requests on its own, so the
    resource loader refuses anything that is not a local file.
    """

    def loadResource(self, kind, url):
        if url.isLocalFile() or url.scheme() in ("", "qrc", "data"):
            return super().loadResource(kind, url)
        return None


class HelpDialog(QDialog):
    """The README, with its headings as a menu down the side."""

    def __init__(self, parent, subtitle: str = ""):
        super().__init__(parent)
        self.setWindowTitle("LiniDi - help")
        self.resize(880, 620)
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        logo = QLabel()
        pm = logo_pixmap(26)
        if not pm.isNull():
            logo.setPixmap(pm)
        head.addWidget(logo)
        if subtitle:
            lab = QLabel(subtitle)
            lab.setObjectName("muted")
            head.addSpacing(12)
            head.addWidget(lab)
        head.addStretch(1)
        root.addLayout(head)

        body = QHBoxLayout()
        self.menu = QListWidget()
        self.menu.setFixedWidth(180)
        self.text = OfflineBrowser()
        # A clicked link hands off to the system browser, which is the user
        # asking for it; the window itself still fetches nothing.
        self.text.setOpenExternalLinks(True)
        # Rendered markdown carries no colours of its own, so it lands in the
        # widget's default near-black on this theme's dark background.
        self.text.setStyleSheet(
            f"QTextBrowser {{ background: {BG}; color: {FG};"
            f" border: 1px solid {LINE}; border-radius: 6px;"
            f" padding: 10px; }}")
        self.text.document().setDefaultStyleSheet(
            f"a {{ color: {ACCENT}; }}"
            f"code, pre {{ color: {ACTIVE}; }}"
            f"h1, h2, h3 {{ color: {FG}; }}")
        body.addWidget(self.menu)
        body.addWidget(self.text, 1)
        root.addLayout(body, 1)

        self.sections = doc_sections()
        for title, _ in self.sections:
            self.menu.addItem(QListWidgetItem(title))
        self.menu.currentRowChanged.connect(self._show)
        self.menu.setCurrentRow(0)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _show(self, row: int):
        if 0 <= row < len(self.sections):
            title, body = self.sections[row]
            # Rendered as Markdown rather than dumped as plain text: the
            # README is full of lists, code and tables that are unreadable
            # raw.
            self.text.setMarkdown(f"## {title}\n\n{body}"
                                  if title != "Overview" else body)


class MainWindow(QMainWindow):
    def __init__(self, opts):
        super().__init__()
        self.opts = opts
        self.tasks = TaskRunner(self)
        self.daemon = None        # set by connect_device()
        self.amap: core.AddressMap | None = None
        self.readback: core.Readback | None = None
        self.project: dict[str, Any] | None = None
        self.project_path = Path(opts.project).expanduser()
        self.dirty = False
        self.have_read = False
        self._name_col = 36
        self._topology_dsp: int | None = None
        self._last_config = None

        self.setWindowTitle("LiniDi")
        self.resize(1560, 1044)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.master = MasterStrip()
        self.master.master_changed.connect(self.on_master_change)
        self.master.panic.connect(self.on_panic)

        self.warn_label = QLabel("")
        self.warn_label.setObjectName("muted")
        # Apply writes to the hardware; keep it at the far end of the card,
        # away from anything pressed routinely.
        self.apply_btn = QPushButton("Apply to device")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.setToolTip(
            "Write this project to the hardware, overwriting what is loaded.")
        self.apply_btn.clicked.connect(self.on_apply)
        self.help_btn = QPushButton()
        self.help_btn.setIcon(help_icon(18))
        self.help_btn.setIconSize(QSize(18, 18))
        self.help_btn.setFixedWidth(34)
        self.help_btn.setCursor(Qt.PointingHandCursor)
        self.help_btn.setToolTip("What this is, how it works, and what to be "
                                 "careful with")
        self.help_btn.clicked.connect(self.on_help)
        self.master.add_trailing(self.apply_btn, spacing=8)
        self.master.add_trailing(self.help_btn, spacing=8)

        card_wrap = QWidget()
        cw = QHBoxLayout(card_wrap)
        cw.setContentsMargins(10, 8, 10, 4)
        cw.addWidget(self.master)
        root.addWidget(card_wrap)

        bar = QHBoxLayout()
        bar.setContentsMargins(10, 6, 10, 6)
        # Safe, frequently-used actions live together on the left.
        # One width across the row: these are equal in weight, and a ragged
        # edge of differently sized buttons just reads as clutter.
        BTN_W = 150
        self.read_btn = QPushButton("Read Device")
        self.read_btn.setToolTip(
            "Read live coefficients off the hardware and load them here.\n"
            "This only reads; nothing is written.")
        self.read_btn.clicked.connect(self.on_read)

        self.xml_btn = QPushButton("Import XML")
        self.xml_btn.setToolTip(
            "Load a preset exported from miniDSP Device Console.\n"
            "This is the only source of bypass state and PEQ contents --\n"
            "the hardware reports neither.")
        self.xml_btn.clicked.connect(self.on_import_xml)

        self.rew_btn = QPushButton("Import REW")
        self.rew_btn.setToolTip("Load a REW biquad export into this channel.")
        self.rew_btn.clicked.connect(self.on_rew)

        self.save_btn = QPushButton("Save project")
        self.save_btn.clicked.connect(self.on_save)
        self.load_btn = QPushButton("Load project")
        self.load_btn.clicked.connect(self.on_load)

        for b in (self.read_btn, self.xml_btn, self.rew_btn,
                  self.save_btn, self.load_btn):
            b.setFixedWidth(BTN_W)
            bar.addWidget(b)

        bar.addStretch(1)
        bar.addWidget(self.warn_label)
        holder = QWidget(); holder.setLayout(bar)
        root.addWidget(holder)

        # Both side columns are a fixed width and only the editor stretches,
        # so there is nothing for a splitter to split. It also had a bug in
        # it: given slack, it grew the navigator's slot past the width the
        # list is allowed to be, and the leftover showed as a gap that came
        # and went depending on whether the selected channel's editor asked
        # for more room. A plain row cannot do that.
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self.chan_list = QListWidget()
        self.chan_list.setFixedWidth(190)
        self.chan_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.chan_list.setTextElideMode(Qt.ElideRight)
        self.chan_list.currentRowChanged.connect(self.on_select)
        body.addWidget(self.chan_list)

        self.editor = ChannelEditor()
        self.editor.changed.connect(self.on_edit)
        self.editor.navigate.connect(self.on_navigate)
        body.addWidget(self.editor, 1)

        right = QWidget()
        ml = QVBoxLayout(right)
        ml.setContentsMargins(8, 8, 8, 8)
        levels = QGroupBox("Levels")
        lv = QVBoxLayout(levels)
        lab = QLabel("Inputs"); lab.setObjectName("muted")
        lv.addWidget(lab)
        self.in_meters: list[MeterBar] = []
        self.in_box = QVBoxLayout(); lv.addLayout(self.in_box)
        lv.addSpacing(8)
        lab = QLabel("Outputs"); lab.setObjectName("muted")
        lv.addWidget(lab)
        self.out_meters: list[MeterBar] = []
        self.out_box = QVBoxLayout(); lv.addLayout(self.out_box)
        ml.addWidget(levels)
        ml.addWidget(self.editor.side, 1)
        right.setFixedWidth(286)
        body.addWidget(right)

        holder2 = QWidget(); holder2.setLayout(body)
        root.addWidget(holder2, 1)
        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self.poll = QTimer(self)
        self.poll.timeout.connect(self.tick)
        self.connect_device()

    # ---- setup ----

    def connect_device(self):
        """Open the device directly over USB, falling back to minidspd.

        The native path needs no daemon and no external binaries, and it is
        the one that writes routing correctly. The daemon path is kept as a
        fallback for when USB permissions are not in place.
        """
        self.transport = "usb"
        try:
            dev = native.open_device(map_name=self.opts.map,
                                     timeout_ms=self.opts.timeout)
            # NativeDevice satisfies both roles the app used to split between
            # a daemon (writes, status) and a readback helper (coefficients).
            self.daemon = self.readback = dev
            self.amap = dev.amap
            info = dev.info
            name = core.product_name(self.amap.device)
            self._topology_dsp = info.dsp_version
            serial = info.serial
            rate = self.amap.rate
        except Exception as usb_exc:                      # noqa: BLE001
            self.transport = "daemon"
            self.daemon = core.Daemon(self.opts.daemon, self.opts.device)
            try:
                devices = self.daemon.devices()
            except core.DeviceError as exc:
                QMessageBox.critical(
                    self, "No device",
                    f"Could not open the device over USB:\n  {usb_exc}\n\n"
                    f"and minidspd is not reachable either:\n  {exc}\n\n"
                    "Check that the device is connected and that your user "
                    "has access to it (see the udev rule in the README).")
                QTimer.singleShot(0, self.close)
                return
            if not devices:
                QMessageBox.critical(self, "No device",
                                     "minidspd reports no connected devices.")
                QTimer.singleShot(0, self.close)
                return
            d = devices[min(self.opts.device, len(devices) - 1)]
            name = d.get("product_name", "unknown")
            ver = d.get("version", {})
            self._topology_dsp = ver.get("dsp_version")
            serial = ver.get("serial")
            self.amap = core.AddressMap.load(name)
            rate = self.amap.rate if self.amap else self.opts.rate
            if self.amap:
                self.readback = core.Readback(self.amap, cli=self.opts.cli,
                                              tcp=self.opts.tcp)
            else:
                self.readback = None
                self.read_btn.setEnabled(False)

        status = self.daemon.status()
        # Channel counts come from the address map, which is what describes
        # the device. They used to be taken from the number of meter levels
        # reported, but three of the generated maps have no meter addresses at
        # all -- those devices reported no levels, and the app built a project
        # with no channels and showed an empty navigator.
        if self.amap:
            n_in = len(self.amap.inputs)
            n_out = len(self.amap.outputs)
        else:
            n_in = len(status.get("input_levels", []))
            n_out = len(status.get("output_levels", []))

        self.master.device_label.setText(
            f"{name}  sn {serial}  {n_in}in/{n_out}out  {rate} Hz  "
            f"[{self.transport}]")

        n_peq = (len(self.amap.outputs[0].get("peq", []))
                 if self.amap and self.amap.outputs else self.opts.peq)
        self.project = self.load_project(n_in, n_out, n_peq or 10, rate)
        self.build_meters(n_in, n_out)
        self.refresh_list()
        self.chan_list.setCurrentRow(1)
        self.update_warning()
        self.poll.start(500)
        self.statusBar().showMessage(
            f"Connected to {name} over {self.transport}", 4000)

    def load_project(self, n_in, n_out, n_peq, rate):
        if self.project_path.is_file():
            try:
                data = json.loads(
                    self.project_path.read_text(encoding="utf-8"))
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
    def _summarise_output(out: dict[str, Any], fed: bool) -> str:
        """One-line description of what an output is actually doing."""
        bits = []
        band = passband(out)
        if band:
            bits.append(band)
        pq = core.count_effective_peq(out.get("peq", []))
        if pq:
            bits.append(f"{pq} EQ")
        if bits:
            return " · ".join(bits)
        # No filters at all means two very different things, and the
        # difference matters when you are looking for a silent driver.
        return "full range" if fed else "unused"

    def _add_header(self, text: str):
        item = QListWidgetItem(text)
        item.setFlags(Qt.NoItemFlags)              # not selectable
        item.setForeground(QColor(MUTED))
        font = item.font()
        font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
        font.setCapitalization(QFont.AllUppercase)
        item.setFont(font)
        self.chan_list.addItem(item)

    def _outputs_fed(self) -> set[int]:
        """Outputs some input is routed to."""
        fed = set()
        for inp in self.project["inputs"]:
            for r in inp.get("routing", []):
                if r.get("enabled"):
                    fed.add(r.get("index"))
        return fed

    def _name_column(self) -> int:
        """Width of the name column: the widest channel name, measured once."""
        names = [c.get("name", "") for c in
                 self.project["inputs"] + self.project["outputs"]]
        fm = self.chan_list.fontMetrics()
        widest = max((fm.horizontalAdvance(n) for n in names), default=36)
        return max(36, widest)

    def _add_channel_row(self, kind: str, i: int, chan: dict[str, Any],
                         detail: str, dim: bool):
        item = QListWidgetItem()
        item.setData(Qt.UserRole, (kind, i))
        row = ChannelRow(chan.get("name", ""), detail,
                         bool(chan.get("mute")), dim, self._name_col)
        row.toggled.connect(
            lambda muted, k=kind, n=i: self.on_channel_mute(k, n, muted))
        # The stylesheet pads list items, and that padding is not taken out of
        # the rect an item widget is laid into. Sizing the row to the widget
        # exactly left the icon standing in the padding with its bottom
        # clipped, so ask for the padding on top of the row's own height.
        item.setSizeHint(QSize(row.sizeHint().width(),
                               ChannelRow.ROW_HEIGHT + 2 * LIST_ITEM_PAD_Y))
        self.chan_list.addItem(item)
        self.chan_list.setItemWidget(item, row)

    def on_channel_mute(self, kind: str, index: int, muted: bool):
        """Mute one channel, straight to the device.

        This is a live control rather than an edit staged for Apply: reaching
        for mute during a measurement means you want that driver quiet now.
        """
        key = "outputs" if kind == "output" else "inputs"
        chan = self.project[key][index]
        chan["mute"] = muted
        self._show_mute(kind, index, muted)
        name = chan.get("name", kind)
        payload = {key: [{"index": index, "mute": muted}]}

        def failed(msg):
            # The device did not take it, so put the icon back rather than
            # leaving the screen claiming a driver is quiet when it is not.
            chan["mute"] = not muted
            self._show_mute(kind, index, not muted)
            self.statusBar().showMessage(f"{name}: mute failed - {msg}", 8000)

        self.tasks.run(
            lambda: self.daemon.set_config(payload),
            on_done=lambda _: self.statusBar().showMessage(
                f"{name} {'muted' if muted else 'unmuted'}", 3000),
            on_error=failed)

    def _show_mute(self, kind: str, index: int, muted: bool):
        """Update one navigator row in place."""
        for r in range(self.chan_list.count()):
            if self.chan_list.item(r).data(Qt.UserRole) == (kind, index):
                row = self.chan_list.itemWidget(self.chan_list.item(r))
                if row is not None:
                    row.set_muted(muted)
                return

    def refresh_list(self):
        """Channel list ordered by signal flow: inputs first, then outputs."""
        prev = self.chan_list.currentItem()
        prev_key = prev.data(Qt.UserRole) if prev else None

        self.chan_list.blockSignals(True)
        self.chan_list.clear()
        self._name_col = self._name_column()

        self._add_header("Inputs · voicing")
        for i, inp in enumerate(self.project["inputs"]):
            pq = core.count_effective_peq(inp.get("peq", []))
            self._add_channel_row("input", i, inp,
                                  f"{pq} EQ" if pq else "flat", False)

        self._add_header("Outputs · crossover")
        fed = self._outputs_fed()
        for i, out in enumerate(self.project["outputs"]):
            summary = self._summarise_output(out, i in fed)
            self._add_channel_row("output", i, out, summary,
                                  summary == "unused")

        self.chan_list.blockSignals(False)

        target = 1        # first real row, skipping the header
        if prev_key:
            for r in range(self.chan_list.count()):
                if self.chan_list.item(r).data(Qt.UserRole) == prev_key:
                    target = r
                    break
        self.chan_list.setCurrentRow(target)
        self._paint_selection()
        # Summaries can only be trimmed once the rows have been laid out and
        # the labels know how much width they were actually given.
        QTimer.singleShot(0, self._elide_details)

    def _paint_selection(self):
        """Tell each card whether it is the selected one.

        The row widget covers its list item completely, so the selection the
        view would paint behind it never shows; the card has to draw it.
        """
        current = self.chan_list.currentRow()
        for r in range(self.chan_list.count()):
            row = self.chan_list.itemWidget(self.chan_list.item(r))
            if row is not None:
                row.set_selected(r == current)

    def _elide_details(self):
        for r in range(self.chan_list.count()):
            row = self.chan_list.itemWidget(self.chan_list.item(r))
            if row is not None:
                row.elide_detail(row.full_detail)

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
        self._paint_selection()
        chan, is_out = self.current_channel()
        if chan is not None:
            self.editor.project = self.project
            self.editor.load(chan, is_out)
            self.editor.refresh_plot()

    def on_navigate(self, kind: str, index: int):
        """Select a channel because a chain stage was clicked."""
        for row in range(self.chan_list.count()):
            if self.chan_list.item(row).data(Qt.UserRole) == (kind, index):
                self.chan_list.setCurrentRow(row)
                return

    def on_edit(self):
        self.dirty = True
        self.refresh_list()
        self.update_warning()

    def update_warning(self):
        unknown = core.unknown_bypass(self.project) if self.project else []
        if unknown:
            self.warn_label.setText(
                f"{len(unknown)} filter state(s) unknown - import your config "
                "to enable Apply")
            self.warn_label.setStyleSheet(f"color: {DANGER};")
            self.apply_btn.setEnabled(False)
            return
        self.apply_btn.setEnabled(True)
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

    def on_help(self):
        info = ""
        if self.amap is not None:
            info = self.master.device_label.text()
        HelpDialog(self, info).exec()

    def on_panic(self):
        """Toggle master mute.

        The button shows state, so it has to act on state: leaving it as a
        one-way mute would mean the lit indicator could not be cleared from
        the control that lit it.
        """
        target = not self.master.muted
        self.tasks.run(
            lambda: self.daemon.set_master(mute=target),
            on_done=lambda _: self.statusBar().showMessage(
                "MUTED" if target else "unmuted", 5000),
            on_error=lambda e: self.statusBar().showMessage(e, 6000))

    def on_read(self):
        if self.readback is None:
            return
        self.read_btn.setEnabled(False)
        self.statusBar().showMessage("Reading coefficients from device...")
        n_out = len(self.project["outputs"])
        n_in = len(self.project["inputs"])
        serial = getattr(getattr(self.daemon, "info", None), "serial", None)
        preset = 0
        try:
            preset = int(self.daemon.status()["master"].get("preset", 0))
        except Exception:                                  # noqa: BLE001
            pass

        def work():
            outs = self.readback.read_all(n_out)
            ins = self.readback.read_inputs(n_in)
            # The device cannot report PEQ, routing or bypass. Device Console
            # keeps those beside the serial, in the same format as an export,
            # so read them from there rather than leaving the panels empty.
            cfg = None
            for d in core.find_console_settings(serial, self.opts.console_dir):
                f = core.console_setting_file(d, preset)
                if f:
                    cfg = f
                    break
            return outs, ins, cfg

        self.tasks.run(work, on_done=self._read_done,
                       on_error=self._read_failed)

    def _read_done(self, result):
        readings, input_readings, cfg = result
        core.apply_readback(self.project, readings, input_readings)
        self._last_config = None
        if cfg is not None:
            try:
                parsed = core.parse_device_console_xml(
                    cfg.read_text(encoding="utf-8", errors="replace"))
                core.apply_device_console_xml(self.project, parsed, self.amap)
                self._last_config = cfg
            except Exception as exc:                       # noqa: BLE001
                self.statusBar().showMessage(
                    f"could not read {cfg.name}: {exc}", 8000)
        self.have_read = True
        self.dirty = False
        self.read_btn.setEnabled(True)
        self.refresh_list()
        self.on_select(self.chan_list.currentRow())
        self.update_warning()
        active = sum(1 for r in readings
                     for g in r.get("crossover", []) if g.get("active"))
        every = self.project["outputs"] + self.project["inputs"]
        unread = len([b for ch in every
                      for b in ch.get("peq", [])
                      if b.get("read_state") == "unreadable"])
        msg = (f"Read {len(readings)} outputs and "
               f"{len(input_readings)} inputs, "
               f"{active} crossover groups, from the device")
        if self._last_config is not None:
            msg += (f"  -  PEQ, routing and bypass loaded from "
                    f"{self._last_config.name} (the device does not report "
                    "them)")
        elif unread:
            msg += (f"  -  {unread} PEQ bands unavailable: this device "
                    "reports neither PEQ nor routing, and no Device "
                    "Console settings file was found. Use Import XML.")
        self.statusBar().showMessage(msg, 15000)

    def _read_failed(self, msg):
        self.read_btn.setEnabled(True)
        self.statusBar().showMessage(f"Read failed: {msg}", 8000)
        QMessageBox.warning(self, "Read failed", msg)

    def on_apply(self):
        # An unstable section does not filter, it runs away, and its output
        # goes to a driver. Never write one, whatever else is in the payload.
        unstable = core.unstable_filters(self.project)
        if unstable:
            QMessageBox.warning(
                self, "Unstable filter",
                "These bands have coefficients whose poles are on or outside "
                "the unit circle:\n\n  "
                + "\n  ".join(unstable[:10])
                + "\n\nA section like that does not filter, it runs away, and "
                  "its output goes straight to a driver. Correct them on the "
                  "Biquad tab, or switch those bands off.")
            return

        # Bypass cannot be read back from the hardware, so if any filter's
        # state is still unknown the app does not know what it would be
        # writing. Refuse rather than guess: a wrong guess switches a filter
        # on or off, and on an active crossover that reaches a driver.
        unknown = core.unknown_bypass(self.project)
        if unknown:
            shown = "\n".join(f"  \u2022 {u}" for u in unknown[:10])
            more = (f"\n  ... and {len(unknown) - 10} more"
                    if len(unknown) > 10 else "")
            QMessageBox.warning(
                self, "Filter states unknown",
                f"{len(unknown)} filter(s) were read from the hardware, which "
                "cannot report whether a filter is bypassed.\n\n"
                f"{shown}{more}\n\n"
                "Import your Device Console export to load the real states, "
                "or click each filter's enable box to set it explicitly. "
                "Applying now would switch filters on or off at random.")
            return

        if not self.have_read:
            resp = QMessageBox.warning(
                self, "Overwrite device configuration?",
                "You have not read the current configuration from this "
                "device.\n\n"
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
        # Gain writes are always verified. The device snaps gain to a linear
        # grid, not to the nearest step, so writing back the value it just
        # reported moves it further down -- an unverified Apply attenuates
        # every output a little, every time.
        project = copy.deepcopy(self.project)
        self.tasks.run(
            lambda: core.apply_project(self.daemon, project,
                                       readback=self.readback),
            on_done=self._apply_done, on_error=self._apply_failed)

    def _apply_done(self, _):
        self.apply_btn.setEnabled(True)
        self.dirty = False
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
                f"reports {mine}.\n\nAddresses may not line up. "
                "Import anyway?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if resp != QMessageBox.Yes:
                return

        stats = core.apply_device_console_xml(self.project, parsed, self.amap)
        self.have_read = True
        self.dirty = True
        self.refresh_list()
        self.on_select(self.chan_list.currentRow())
        self.update_warning()
        self.statusBar().showMessage(
            f"Imported {stats['peq']} PEQ bands and {stats['crossover']} "
            f"crossover groups across {stats['inputs']} inputs and "
            f"{stats['outputs']} outputs, plus {stats['routing']} routing "
            f"cells  -  {stats['bypassed']} of those filters are bypassed",
            12000)

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
            self, "Load project", str(self.project_path.parent),
            "JSON (*.json)")
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
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
            self.project_path.write_text(
                json.dumps(self.project, indent=2) + "\n",
                encoding="utf-8")
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
    ap.add_argument("--console-dir", default=None,
                    help="where Device Console keeps its settings, if it is "
                         "not found automatically")
    ap.add_argument("--map", default=None,
                    help="address map name to use, if auto-detection fails")
    ap.add_argument("--timeout", type=int, default=1000,
                    help="USB command timeout in milliseconds")
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
