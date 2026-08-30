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
import faulthandler
import json
import math
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import (QtMsgType, qInstallMessageHandler,
                            QObject, QPointF, QRectF, QSize, QThread,
                            QTimer, Qt, Signal)
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath,
                           QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QDoubleSpinBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QProgressBar, QSizePolicy,
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
# Phase gets a colour of its own. It shares the plot with magnitude curves in
# orange and blue and with ten PEQ discs, and reusing any of those made a
# dotted trace look like a variant of whatever it borrowed from. Teal belongs
# to nothing else here, so the colour carries the meaning by itself.
PHASE   = "#2dd4bf"
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


def _install_crash_log() -> None:
    """Leave a Python stack behind if the process dies at the C++ level.

    A segfault inside Qt prints nothing a traceback would catch: the
    interpreter is gone before any Python handler runs. faulthandler writes
    the stack of every thread from a signal handler instead, which is the
    only way to find out what this app was doing when it died. It goes to a
    file rather than stderr because the terminal that started it is usually
    not around to read.
    """
    try:
        path = Path.home() / ".config" / "linidi"
        path.mkdir(parents=True, exist_ok=True)
        # Kept on the module so the handle outlives this function; a closed
        # file would leave faulthandler writing to a dead descriptor.
        global _CRASH_LOG
        _CRASH_LOG = open(path / "crash.log", "a", buffering=1)
        _CRASH_LOG.write(f"\n--- started {datetime.now():%Y-%m-%d %H:%M:%S}"
                         f" ---\n")
        faulthandler.enable(file=_CRASH_LOG, all_threads=True)
        qInstallMessageHandler(_qt_message)
    except OSError:
        faulthandler.enable(all_threads=True)


def _qt_message(mode, _context, message) -> None:
    """Send Qt's own complaints to the log, and to stderr as before.

    Qt says so when it is misused -- "Timers cannot be stopped from another
    thread" is the warning for the fault that took four crashes to find --
    and it says it on stderr, which for a windowed app launched from a
    terminal nobody is watching goes nowhere. It costs nothing to keep.
    """
    label = {QtMsgType.QtDebugMsg: "debug",
             QtMsgType.QtInfoMsg: "info",
             QtMsgType.QtWarningMsg: "WARNING",
             QtMsgType.QtCriticalMsg: "CRITICAL",
             QtMsgType.QtFatalMsg: "FATAL"}.get(mode, "?")
    line = f"[Qt {label}] {message}"
    print(line, file=sys.stderr)
    if _CRASH_LOG is not None:
        try:
            _CRASH_LOG.write(line + "\n")
        except ValueError:
            pass


_CRASH_LOG = None


def card_heading(text: str) -> QLabel:
    """A card's name, drawn inside it rather than on its border.

    A QGroupBox title sits on the frame, which puts it outside the card and
    makes a long one look like a caption floating above the panel. Inside,
    it reads as part of the thing it names. Every card here does it this way,
    and in the same colour, so the panels read as one family rather than
    several -- a heading that identifies itself by hue is a badge, and the
    cards that need one have a badge already.
    """
    lab = QLabel(text)
    lab.setObjectName("cardHeading")
    return lab


def disc_badge(text: str, colour_name: str, size: int = 20,
               active: bool = True) -> QPixmap:
    """A small filled disc with a character in it.

    The same mark the response plot puts on a filter, so a card and its
    curve are identifiably the same thing. Drawn hollow when the filter is
    switched off, which turns any column of these into a legend: the filled
    ones are what is actually in circuit.
    """
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    colour = QColor(colour_name)
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
    p.drawText(pm.rect(), Qt.AlignCenter, text)
    p.end()
    return pm


def peq_badge(index: int, size: int = 20, active: bool = True) -> QPixmap:
    """The numbered disc identifying one PEQ band."""
    return disc_badge(str(index), peq_colour(index), size, active)


# The two crossover groups are lettered, not numbered, so a corner is never
# read as a band. These are the colours their markers take on the plot.
XOVER_LABELS = ("A", "B")
XOVER_COLOURS = (ACCENT, WARN)


def xover_label(index: int) -> str:
    return XOVER_LABELS[index] if index < len(XOVER_LABELS) else str(index + 1)


def xover_colour(index: int) -> str:
    return XOVER_COLOURS[index % len(XOVER_COLOURS)]


# How wide the panel beside the filter table is -- crossover on an output,
# routing on an input. Shared so the table keeps one width across both.
SIDE_PANEL_W = 300

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
    "report it.\nLeft untouched when saving. Click to set it explicitly.")

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
/* The two writes. Outlined rather than filled: both carry an indicator, and
   a solid fill fights the lamp for attention. Blue is the reversible one,
   orange the one that commits -- the same ranking used everywhere else. */
QLabel#cardHeading {{ color: {MUTED}; font-weight: 600;
                      letter-spacing: .03em; padding: 0 0 2px 0; }}
QPushButton#rowReset {{ background: transparent; border: 1px solid {LINE};
                        border-radius: 3px; padding: 0; }}
QPushButton#rowReset:hover {{ background: {PANEL2}; border-color: {ACCENT}; }}
QPushButton#resetAll {{ background: {PANEL2}; color: {ACCENT};
                        border: 1px solid {ACCENT}; padding: 3px 10px; }}
QPushButton#resetAll:hover {{ background: #2c313c; }}
QPushButton#applyEdits {{ background: {PANEL2}; color: {FG};
                          border: 1px solid {ACCENT}; font-weight: 600;
                          padding: 5px 12px; }}
QPushButton#applyEdits:hover {{ background: #2c313c; }}
QPushButton#saveEdits {{ background: {PANEL2}; color: {FG};
                         border: 1px solid {ACTIVE}; font-weight: 600;
                         padding: 5px 12px; }}
QPushButton#saveEdits:hover {{ background: #2c313c; }}
QPushButton#applyEdits:disabled, QPushButton#saveEdits:disabled {{
    color: {MUTED}; border-color: {LINE}; }}
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

def default_project_path() -> Path:
    """Where the working project lives.

    Under the program's own name, but the previous name is honoured if a
    project is already there and the new location is empty. Renaming the
    program should not hide someone's tuning behind a path they never chose
    and would have no reason to look for.
    """
    current = Path.home() / ".config" / "linidi" / "project.json"
    previous = Path.home() / ".config" / "minidsp-gui" / "project.json"
    if not current.exists() and previous.is_file():
        return previous
    return current


def device_dir() -> Path:
    """Where things read off a particular unit are kept.

    Flash images and Device Console exports carry a serial number and
    somebody's tuning, so they live beside the program rather than in it,
    and the repository ignores the folder. Opening file dialogs here saves
    hunting for them, which is the only reason a program should have an
    opinion about where a dialog starts.

    Falls back to the home directory if the folder is not there, rather than
    opening somewhere that does not exist.
    """
    here = bundle_dir() / "device"
    return here if here.is_dir() else Path.home()


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


def reset_icon(size: int = 14, colour: str = ACCENT) -> QIcon:
    """A circular arrow, painted so it does not depend on a glyph font.

    Blue: undoing an edit is a secondary action, not the destructive red of
    a mute or the orange of the thing that writes to the device.
    """
    pm = QPixmap(size * 4, size * 4)          # drawn large, scaled down
    pm.fill(Qt.transparent)
    n = size * 4
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(colour))
    pen.setWidthF(n * 0.11)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    inset = n * 0.20
    box = QRectF(inset, inset, n - 2 * inset, n - 2 * inset)
    # An arc with a gap at the top right, and an arrowhead closing it.
    p.drawArc(box, int(60 * 16), int(280 * 16))
    tip_r = box.width() / 2.0
    cx, cy = box.center().x(), box.center().y()
    ang = math.radians(60.0)
    tx, ty = cx + tip_r * math.cos(ang), cy - tip_r * math.sin(ang)
    head = n * 0.20
    path = QPainterPath()
    path.moveTo(tx - head * 0.1, ty - head)
    path.lineTo(tx + head * 0.85, ty + head * 0.15)
    path.lineTo(tx - head * 0.75, ty + head * 0.5)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(colour))
    p.drawPath(path)
    p.end()
    return QIcon(pm.scaled(size, size, Qt.KeepAspectRatio,
                           Qt.SmoothTransformation))


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


def app_icon() -> QIcon:
    """The program icon, at the sizes a window manager asks for.

    Qt will scale a single pixmap, but it does it once per request and with no
    say in how; adding the sizes explicitly keeps the monogram crisp in a
    task bar and a title bar rather than leaving 16px to a generic downscale.
    Returns an empty icon if the artwork is missing, which Qt treats as "no
    icon set" rather than drawing a blank.
    """
    path = asset_dir() / "LDicon.png"
    if not path.is_file():
        return QIcon()
    source = QPixmap(str(path))
    if source.isNull():
        return QIcon()
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(source.scaled(size, size, Qt.KeepAspectRatio,
                                     Qt.SmoothTransformation))
    return icon


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

      * Callbacks must not tear the thread down, and must not run on the
        worker thread at all. A plain lambda connected to a worker signal has
        no receiver QObject, so Qt uses a *direct* connection and runs it
        there -- where calling QThread.wait() means a thread waiting on
        itself, and where touching a widget or a timer is undefined. Every
        callback is routed back through this object instead.
      * References must outlive the thread. Dropping the last Python reference
        to a still-running QThread lets the garbage collector destroy it, and
        Qt aborts with "QThread: Destroyed while thread is still running".

    So: the worker only ever asks the thread to quit, and reaping happens on
    the main thread once QThread.finished has actually fired. TaskRunner is a
    QObject owned by the window, so that connection is queued to the main
    thread rather than run inline.
    """

    # Carries a finished task's callback and its result back to the main
    # thread. Emitting is thread-safe from anywhere; the connection below is
    # what decides where the callback actually runs.
    _deliver = Signal(object, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._live: list[tuple[QThread, Worker]] = []
        # Queued to this object, which the window owns and which therefore
        # lives on the main thread. That is what makes the guarantee below
        # hold for every kind of callable, rather than only for the ones Qt
        # can find a receiver for.
        self._deliver.connect(self._invoke, Qt.QueuedConnection)

    @staticmethod
    def _invoke(callback, value):
        callback(value)

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

        # Callbacks are delivered through this object rather than connected
        # to the worker directly. Qt picks a connection type from the
        # receiver, and a plain closure or lambda has no receiver QObject to
        # pick from -- so it gets a direct connection and runs on the worker
        # thread. Most callers here pass closures, and several of them touch
        # widgets or timers. Stopping a QTimer from the wrong thread corrupts
        # the timer list, and the process then dies inside activateTimers
        # some time later with nothing of ours on the stack.
        #
        # These two lambdas do run on the worker thread, which is safe
        # because emitting a signal is, and the emit is queued to the main
        # thread where the real callback is finally called.
        if on_done:
            worker.done.connect(
                lambda value, cb=on_done: self._deliver.emit(cb, value))
        if on_error:
            worker.failed.connect(
                lambda msg, cb=on_error: self._deliver.emit(cb, msg))

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

    # Bottom of the bar, in dB. Linear in dB from here to 0, which is what
    # most digital peak meters do; a scale with a compressed bottom end is a
    # deliberate choice rather than a convention, and this does not need one.
    #
    # -60 was too high: a tweeter behind a 2.6 kHz high pass and 7 dB of cut
    # carries far less energy than the woofer beside it and sat below the
    # floor while plainly audible. Lower this further if a channel still
    # reads empty when you can hear it.
    SCALE_FLOOR = -90.0

    def __init__(self, label: str):
        super().__init__()
        self.label = label
        self.value = -120.0         # newest sample
        self.display = -120.0       # what is actually drawn
        self.peak = -120.0
        self._peak_at = 0.0
        self._last = time.monotonic()
        self.setFixedHeight(15)

    # A level below which there is nothing to show. Also where anything
    # unrepresentable is sent.
    FLOOR_DB = -120.0

    def set_value(self, db: float):
        # A meter cannot display "not a number", and must not try. This
        # device emits NaN from a channel whose compressor is running
        # against digital silence, and one such sample used to be permanent:
        # every comparison against NaN is False, so the ballistics below
        # latch, display stays NaN for ever, and the bar sits full whatever
        # arrives afterwards. Floor it and the meter recovers on its own.
        if not math.isfinite(db):
            db = self.FLOOR_DB
        self.value = db
        if not math.isfinite(self.display):
            self.display = self.FLOOR_DB
        if not math.isfinite(self.peak):
            self.peak = self.FLOOR_DB
        now = time.monotonic()
        if db > self.peak or now - self._peak_at > self.PEAK_HOLD_SEC:
            self.peak, self._peak_at = db, now
        # Ballistics are advanced by the window's animation timer, not here.
        # Driving them from the sample meant the release only moved when a
        # sample arrived, so a 90 dB/s decay rendered at the poll rate and
        # the bar stepped instead of falling.

    def animate(self):
        """Advance ballistics one frame."""
        if not math.isfinite(self.display):
            self.display = self.FLOOR_DB
        if not math.isfinite(self.value):
            self.value = self.FLOOR_DB
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
        # The number is the held peak, not the newest sample. A sample
        # arrives every 60 ms, and a figure changing sixteen times a second
        # cannot be read -- it is only good for seeing that something is
        # moving, which the bar already shows better. Held for PEAK_HOLD_SEC,
        # it is a number you can actually take down, and it is the same value
        # as the peak tick on the bar.
        p.drawText(w - 42, 0, 42, h, Qt.AlignVCenter | Qt.AlignRight,
                   "-inf" if self.peak <= -119 else f"{self.peak:.1f}")

        x0, x1 = 24, w - 46
        bar_w = max(1, x1 - x0)
        p.fillRect(x0, 4, bar_w, h - 8, QColor(PANEL2))

        def frac(db):
            """Where a level sits along the bar, 0 at the floor, 1 at 0 dB."""
            span = -self.SCALE_FLOOR
            return max(0.0, min(1.0, (db - self.SCALE_FLOOR) / span))

        fill = int(bar_w * frac(self.display))
        if fill > 0:
            # Coloured by the held peak, like the number. Following the
            # newest sample instead meant a transient into the red showed for
            # a single frame and was gone before it could be seen; held, it
            # stays lit long enough to notice, which is the point of marking
            # it at all.
            col = (DANGER if self.peak > -3
                   else WARN if self.peak > -12 else OK)
            p.fillRect(x0, 4, fill, h - 8, QColor(col))
        if self.peak > -119:
            px = x0 + int(bar_w * frac(self.peak))
            p.fillRect(min(px, x1 - 1), 4, 1, h - 8, QColor(FG))


class ResponsePlot(QWidget):
    """Log-frequency magnitude plot, painted directly (no plotting library).

    The band markers are grabbable: drag one to move its frequency and gain,
    and turn the wheel over it to change Q. Editing a filter on the curve it
    draws is the point -- you aim at the shape you want rather than working
    out which numbers produce it.

    Dragging emits band_changed continuously so the curve and the table keep
    up with the pointer, and band_committed once on release. The split is
    what keeps a drag from marking the project dirty sixty times a second.
    """

    DB_MIN, DB_MAX = -36.0, 18.0
    GRAB_PX = 12.0              # how near the pointer must be to a marker
    Q_PER_NOTCH = 1.12          # wheel step, multiplicative
    Q_MIN, Q_MAX = 0.1, 20.0    # matches the spin boxes in PeqTable
    F_MIN, F_MAX = 10.0, 24000.0
    G_MIN, G_MAX = -24.0, 24.0

    band_changed = Signal(int, dict)
    xover_changed = Signal(int, dict)
    band_committed = Signal()

    def __init__(self):
        super().__init__()
        self.curves: list[tuple[list[float], list[float], str, bool]] = []
        self.bands: list[dict[str, Any]] = []
        self.phases: list[dict[str, Any]] = []
        self.freqs = core.log_freqs(280)
        self.setMinimumHeight(200)
        # Where each marker was actually drawn, so hit-testing matches what
        # is on screen rather than where the band nominally sits -- markers
        # get nudged apart when they overlap.
        self._marks: list[tuple[str, int, QPointF, float | None]] = []
        self._drag: tuple[str, int] | None = None
        self._drag_scale: float | None = None
        self.setMouseTracking(True)

    # -- coordinates, and their inverses ---------------------------------

    def _xf(self, x, w):
        """Pointer x back to a frequency."""
        span = max(1, w)
        return 10.0 ** (LOG_F_LO + max(0.0, min(1.0, x / span)) * LOG_F_SPAN)

    def _yd(self, y, h):
        """Pointer y back to a level in dB."""
        span = max(1, h)
        frac = max(0.0, min(1.0, (span - y) / span))
        return self.DB_MIN + frac * (self.DB_MAX - self.DB_MIN)

    # -- grabbing --------------------------------------------------------

    def _hit(self, pos):
        """The draggable marker under the pointer, nearest first.

        Returns (kind, index, gain_scale) or None.
        """
        best = None
        for kind, index, pt, scale in self._marks:
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d <= self.GRAB_PX and (best is None or d < best[0]):
                best = (d, kind, index, scale)
        return None if best is None else (best[1], best[2], best[3])

    def mousePressEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            return
        hit = self._hit(ev.position())
        if hit is None:
            return
        self._drag = (hit[0], hit[1])
        self._drag_scale = hit[2]
        self._emit_from(ev.position())

    def mouseMoveEvent(self, ev):
        if self._drag is None:
            self.setCursor(Qt.PointingHandCursor if self._hit(ev.position())
                           else Qt.ArrowCursor)
            return
        self._emit_from(ev.position())

    def mouseReleaseEvent(self, ev):
        if self._drag is not None:
            self._drag = None
            self.band_committed.emit()

    def _emit_from(self, pos):
        kind, index = self._drag
        fields = {"freq": round(max(self.F_MIN, min(
            self.F_MAX, self._xf(pos.x(), self.width()))), 1)}
        if self._drag_scale:
            db = self._yd(pos.y(), self.height()) * self._drag_scale
            fields["gain"] = round(max(self.G_MIN, min(self.G_MAX, db)), 2)
        if kind == "xover":
            self.xover_changed.emit(index, fields)
        else:
            self.band_changed.emit(index, fields)

    def wheelEvent(self, ev):
        """Q, on the band under the pointer.

        Multiplicative so a notch feels the same at Q 0.5 and at Q 8, which
        a fixed step does not: 0.1 is a fifth of the former and a eightieth
        of the latter.
        """
        hit = self._hit(ev.position())
        if hit is None:
            ev.ignore()
            return
        kind, index, _scale = hit
        entry = next((b for b in self.bands
                      if b.get("index") == index
                      and b.get("kind", "peq") == kind), None)
        steps = ev.angleDelta().y() / 120.0
        if entry is None or not steps:
            ev.ignore()
            return

        if kind == "xover":
            # Order, stepped through what this alignment actually offers
            # rather than by arithmetic: Linkwitz-Riley has no odd orders,
            # and landing on one would be a slope the device cannot build.
            orders = core.crossover_orders(entry.get("alignment"))
            if not orders:
                ev.ignore()
                return
            try:
                at = orders.index(int(entry.get("order")))
            except (TypeError, ValueError):
                at = 0
            nxt = max(0, min(len(orders) - 1, at + int(round(steps))))
            if orders[nxt] != entry.get("order"):
                self.xover_changed.emit(index, {"order": orders[nxt]})
                self.band_committed.emit()
            ev.accept()
            return

        if entry.get("q") is None:
            ev.ignore()
            return
        q = float(entry["q"]) * (self.Q_PER_NOTCH ** steps)
        self.band_changed.emit(
            index, {"q": round(max(self.Q_MIN, min(self.Q_MAX, q)), 3)})
        self.band_committed.emit()
        ev.accept()

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
        self._marks = []
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
            # Hit-testing uses the drawn position, nudge included, so the
            # grab area is where the disc actually is rather than where the
            # band would have been without the anti-overlap shuffle.
            if band.get("grab"):
                self._marks.append((band.get("kind", "peq"), band["index"],
                                    pt, band.get("gain_scale")))

            colour = QColor(band["colour"])
            p.setPen(QPen(QColor(BG), 2))
            p.setBrush(colour)
            p.drawEllipse(pt, r, r)
            p.setPen(_readable_on(colour))
            p.drawText(QRectF(x - r, y - r, 2 * r, 2 * r),
                       Qt.AlignCenter,
                       str(band.get("label", band["index"])))


class CompressorPanel(QGroupBox):
    """One output's compressor, with its gain-reduction meter.

    The gain-reduction meter reads NaN whenever the channel is silent,
    whether or not the compressor is switched on. That is the resting state
    of an output with nothing routed to it, not a fault: routing signal to
    out5 made its GR meter read a number, and removing the routing put it
    back to NaN. MeterBar guards against it, so it shows as no bar rather
    than latching.

    Five of its six settings cannot be read back from the device, so what is
    shown for those comes from the stored preset and from this project --
    there is no way to ask the hardware to confirm them. The provenance line
    at the bottom says so rather than letting the numbers imply they were
    measured. Only threshold answers a live read; makeup, ratio, knee, attack
    and release all come back as zero, and the enable field reads 1, which is
    neither of the two values it is ever written with.
    """

    changed = Signal()

    # Ranges are not published anywhere we can read, and four of these
    # cannot be read back to probe them, so they are conventional limits wide
    # enough to cover anything the vendor's own editor offers. The values a
    # Flex 8 ships with -- 4:1, 40 ms, 100 ms -- sit comfortably inside them.
    #
    # There is no knee here on purpose. The field exists, the DSP accepts the
    # write, and it changes nothing: four outputs given the same signal at
    # the same instant, with knees of 0, 12, 24 and 40, reported the same
    # gain reduction to the last digit. A control that cannot affect anything
    # is not a control, so the preset keeps carrying the value and the UI
    # does not offer it.
    FIELDS = (
        ("threshold", "Threshold", -90.0, 0.0, 1, 0.5, " dB"),
        ("ratio", "Ratio", 1.0, 100.0, 1, 0.5, ":1"),
        ("attack", "Attack", 0.1, 1000.0, 1, 1.0, " ms"),
        ("release", "Release", 1.0, 5000.0, 1, 10.0, " ms"),
        ("makeup", "Makeup", -20.0, 20.0, 2, 0.5, " dB"),
    )

    def __init__(self):
        super().__init__()
        self.data: dict[str, Any] = {}
        self._loading = False
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.addWidget(card_heading("Compressor"), 0, 0, 1, 2)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setToolTip(
            "Put the compressor into circuit on this output.\n"
            "It sits in front of a driver, so it is left off unless you "
            "switch it on.")
        self.enabled.toggled.connect(self._emit)
        head.addWidget(self.enabled)
        head.addStretch(1)
        self.gr = MeterBar("GR")
        self.gr.setToolTip("Gain reduction: how far the compressor is "
                           "pulling this output down right now")
        head.addWidget(self.gr, 1)
        holder = QWidget(); holder.setLayout(head)
        lay.addWidget(holder, 1, 0, 1, 2)

        self.boxes: dict[str, QDoubleSpinBox] = {}
        for row, (key, label, lo, hi, dec, step, suffix) in enumerate(
                self.FIELDS, start=2):
            lab = QLabel(label); lab.setObjectName("muted")
            sb = QDoubleSpinBox()
            sb.setRange(lo, hi); sb.setDecimals(dec)
            sb.setSingleStep(step); sb.setSuffix(suffix)
            sb.valueChanged.connect(self._emit)
            self.boxes[key] = sb
            lay.addWidget(lab, row, 0)
            lay.addWidget(sb, row, 1)

        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        lay.addWidget(self.note, len(self.FIELDS) + 2, 0, 1, 2)

    def _emit(self, *_):
        if not self._loading:
            self.store()
            self.changed.emit()

    def load(self, comp: dict[str, Any] | None):
        self._loading = True
        self.data = comp if comp is not None else {}
        self.enabled.setChecked(bool(self.data.get("enabled")))
        for key, sb in self.boxes.items():
            if self.data.get(key) is not None:
                sb.setValue(float(self.data[key]))
        src = self.data.get("bypass_source")
        self.note.setText(
            "Makeup, ratio, attack and release do not read back from the "
            "device. These came from its stored preset."
            if src == "device" else
            "Makeup, ratio, attack and release do not read back from the "
            "device, so these are this project's values, not measured ones.")
        self._loading = False

    def store(self):
        if self.data is None:
            return
        self.data["enabled"] = self.enabled.isChecked()
        for key, sb in self.boxes.items():
            self.data[key] = sb.value()
        self.data["bypass_source"] = "user"

    def set_reduction(self, db: float):
        self.gr.set_value(db)


class FirPanel(QGroupBox):
    """One input's FIR block: what is loaded, and how to load something.

    A Flex 8 puts FIR on its two inputs and nowhere else -- 2048 taps each,
    ahead of the crossover. That is the whole signal, so it suits room
    correction and linear-phase EQ; it cannot make a linear-phase crossover,
    which needs a FIR per output and this hardware does not have one.

    A filter is written with the block bypassed throughout and read back
    afterwards to prove it landed -- coefficients are the one thing on this
    hardware that can be verified rather than trusted. Enabling still asks
    first, because it is the step that changes what comes out of the
    speakers rather than because it is dangerous in itself.
    """

    changed = Signal()
    load_requested = Signal()

    def __init__(self):
        super().__init__()
        self.data: dict[str, Any] = {}
        self._loading = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(6)
        lay.addWidget(card_heading("FIR"))

        self.enabled = QCheckBox("Enabled")
        self.enabled.setToolTip(
            "Put the filter into circuit on this input.\n"
            "Sits ahead of the crossover, so it colours both drivers.")
        # clicked, not toggled: this handler asks a question, and toggled
        # also fires when the box is set from code -- so loading a channel
        # whose filter is switched on would put the dialog in front of
        # somebody who had only changed page.
        self.enabled.clicked.connect(self._on_toggle)
        lay.addWidget(self.enabled)

        self.summary = QLabel("No filter loaded")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)

        self.plot = FirPlot()
        lay.addWidget(self.plot)

        self.load_btn = QPushButton("Load taps...")
        self.load_btn.setToolTip(
            "Read a coefficient file: one number per line, or raw floats.\n"
            "rePhase, REW, Acourate and DRC-FIR exports all work.")
        self.load_btn.clicked.connect(self.load_requested.emit)
        lay.addWidget(self.load_btn)

        self.note = QLabel("")
        self.note.setObjectName("muted")
        self.note.setWordWrap(True)
        lay.addWidget(self.note)

    def _on_toggle(self, on: bool) -> None:
        if on and not self.data.get("taps"):
            QMessageBox.warning(
                self, "Nothing to switch on",
                "No filter has been loaded into this input, so there is "
                "nothing for the block to do. Load taps first.")
            self.enabled.setChecked(False)
            return
        if on:
            box = QMessageBox(self)
            box.setIcon(QMessageBox.Warning)
            box.setWindowTitle("Switch this FIR into circuit?")
            box.setText(
                "This puts the filter into circuit on this input, ahead of "
                "the crossover, so it colours everything the input "
                "feeds.\n\n"
                "The filter was written the way Device Console writes one "
                "and read back to check, which is what makes this safe: "
                "the time this DSP stopped answering, it had been asked to "
                "run coefficients poked straight into its memory that it "
                "was never told to reload.\n\n"
                "Worth a listen at low volume first.")
            go = box.addButton("Enable", QMessageBox.AcceptRole)
            box.addButton("Cancel", QMessageBox.RejectRole)
            box.setDefaultButton(box.buttons()[-1])
            box.exec()
            if box.clickedButton() is not go:
                self.enabled.setChecked(False)
                return
        self.store()
        self.changed.emit()

    def load(self, fir: dict[str, Any] | None):
        self._loading = True
        self.data = fir if fir is not None else {}
        taps = self.data.get("taps") or []
        self.enabled.setChecked(bool(self.data.get("enabled")))
        self.enabled.setEnabled(bool(taps))
        self.plot.set_taps(taps)
        if taps:
            d = core.describe_fir_taps(taps)
            src = self.data.get("source") or "loaded"
            self.summary.setText(
                f"{d['count']} taps from {src}\n"
                f"peak {d['peak']:.4g}, {d['nonzero']} non-zero")
        else:
            self.summary.setText("No filter loaded")
        pending = bool(self.data.get("pending"))
        self.note.setText(
            "Not written to the device yet -- Apply or Save sends it."
            if pending else
            "Coefficients read back from this hardware, so what is written "
            "here can be checked. The tap count and the on/off state "
            "cannot, and come from the stored preset.")
        self._loading = False

    def store(self):
        if self.data is None:
            return
        self.data["enabled"] = self.enabled.isChecked()

    def set_taps(self, taps: list[float], source: str) -> None:
        """Take a filter that has just been read from a file."""
        self.data["taps"] = taps
        self.data["source"] = source
        self.data["pending"] = True
        self.data["enabled"] = False
        self.load(self.data)
        self.changed.emit()


class FirPlot(QWidget):
    """The impulse response, drawn as it is: tap against tap number.

    Not a frequency response. Deriving one means an FFT and a choice of
    window, and both would be this app's opinion of a filter somebody else
    designed. The taps are what was loaded, and a glance at them catches
    the things that actually go wrong with a coefficient file -- a filter
    that is all zeros, one that is clipped flat, one that arrived at the
    wrong width and looks like noise.
    """

    def __init__(self):
        super().__init__()
        self.taps: list[float] = []
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_taps(self, taps: list[float]) -> None:
        self.taps = list(taps)
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        p.fillRect(r, QColor(PANEL2))
        mid = r.center().y()
        p.setPen(QPen(QColor(MINOR_GRID), 1))
        p.drawLine(r.left(), mid, r.right(), mid)
        if not self.taps:
            p.setPen(QColor(MUTED))
            p.drawText(r, Qt.AlignCenter, "no filter loaded")
            return
        peak = max((abs(v) for v in self.taps if math.isfinite(v)),
                   default=0.0)
        if peak <= 0:
            p.setPen(QColor(MUTED))
            p.drawText(r, Qt.AlignCenter, "all coefficients are zero")
            return
        n = len(self.taps)
        half = (r.height() / 2.0) - 2
        p.setPen(QPen(QColor(ACCENT), 1))
        # One column per pixel, drawn as the extremes falling in it, so a
        # 2048-tap filter in 300 pixels still shows its shape rather than
        # every seventh coefficient.
        for x in range(r.width()):
            lo_i = n * x // r.width()
            hi_i = max(lo_i + 1, n * (x + 1) // r.width())
            seg = [v for v in self.taps[lo_i:hi_i] if math.isfinite(v)]
            if not seg:
                continue
            top = mid - (max(seg) / peak) * half
            bot = mid - (min(seg) / peak) * half
            p.drawLine(r.left() + x, int(top), r.left() + x, int(bot))
        p.setPen(QColor(MUTED))
        p.drawText(r.adjusted(4, 0, -4, 0), Qt.AlignLeft | Qt.AlignTop,
                   f"peak {peak:.3g}")


class CrossoverGroup(QGroupBox):
    """Editor for one crossover group (4 biquad slots)."""

    changed = Signal()

    def __init__(self, index: int):
        self.index = index
        letter = xover_label(index)
        super().__init__()
        self.data: dict[str, Any] = {}
        lay = QGridLayout(self)
        lay.setContentsMargins(10, 6, 10, 8)

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self.badge = QLabel()
        self.badge.setPixmap(disc_badge(letter, xover_colour(index), 20))
        self.badge.setToolTip(
            f"Group {letter} on the response plot. Drag its marker to move "
            f"the corner, or turn the wheel over it to change the slope.")
        head.addWidget(self.badge)
        # Plain text. The badge beside it already carries the colour its
        # marker wears on the plot, so tinting the words as well said the
        # same thing twice and made one card's title look unlike every
        # other card's for no additional meaning.
        head.addWidget(card_heading(f"Crossover {letter}"))
        head.addStretch(1)
        self.enabled = QCheckBox("Enabled")
        self.enabled.setTristate(True)
        self.enabled.clicked.connect(self._enabled_clicked)
        self.enabled.toggled.connect(self._emit)
        head.addWidget(self.enabled)
        holder = QWidget()
        holder.setLayout(head)
        lay.addWidget(holder, 0, 0, 1, 2)

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
        orders = core.crossover_orders(align)
        if align == "linkwitz-riley":
            items = [(str(o), f"LR{o * 6}") for o in orders]
        else:
            items = [(str(o), f"{o * 6} dB/oct") for o in orders]
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
        # Filled when the group is in circuit, hollow when it is not, which
        # is exactly when its marker is on the plot and when it is not.
        self.badge.setPixmap(disc_badge(
            xover_label(self.index), xover_colour(self.index), 20,
            bool(group.get("enabled"))))
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
            "Saving refuses to write it.")
        item.setFlags(Qt.ItemIsEnabled)
        return item
    if manual and b.get("manual_source") == "user":
        label, colour = "hand-typed", WARN
        tip = ("Coefficients typed in directly on the Biquad tab. Type, "
               "frequency, Q and gain no longer drive this band.")
    elif state == "unreadable":
        label, colour = "not readable", DANGER
        tip = ("Filter memory does not answer a parameter read, so this band "
               "was not read.\nThe values shown are from the project, not "
               "from the hardware.\nRead the stored preset to load the real "
               "ones from the device.")
    elif state == "stored":
        label, colour = "from device", OK
        tip = ("Read out of the device's stored preset, coefficients and "
               "bypass together.\nThis is what the device loads at power-on. "
               "Anything written live since\nthen has changed what is running "
               "without changing this.")
    elif state == "imported":
        label, colour = "from import", ACCENT
        tip = ("Copied from another channel or another preset.\n"
               "These are not this channel's device values until they are "
               "applied.")
    elif state == "config":
        label, colour = "from config", ACCENT
        tip = ("Loaded from a Device Console export. Used only where the "
               "device's own stored preset could not be read.")
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
    COLS = ["#", "On", "Type", "Freq (Hz)", "Q", "Gain (dB)", "Source", ""]
    (C_NUM, C_ON, C_TYPE, C_FREQ, C_Q, C_GAIN, C_SRC,
     C_RESET) = range(len(COLS))

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
        header.setSectionResizeMode(self.C_RESET, QHeaderView.Fixed)
        self.setColumnWidth(self.C_RESET, 30)
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

            rb = QPushButton()
            rb.setObjectName("rowReset")
            rb.setIcon(reset_icon(13))
            rb.setIconSize(QSize(13, 13))
            rb.setFixedSize(24, 20)
            rb.setCursor(Qt.PointingHandCursor)
            rb.setToolTip(
                f"Put band {idx} back to its starting point: "
                f"{core.stock_peq_freqs(len(bands))[r]:g} Hz, no boost or "
                f"cut, and switched off")
            rb.clicked.connect(lambda _c=False, row=r: self.reset_row(row))
            self.setCellWidget(r, self.C_RESET, rb)

        self._loading = False

    def reset_row(self, row: int):
        """One band back to stock."""
        if not 0 <= row < len(self.bands):
            return
        core.reset_peq_band(self.bands[row], len(self.bands))
        self.load(self.bands)
        self.changed.emit()

    def reset_all(self):
        """Every band back to stock, spread across the range."""
        for b in self.bands:
            core.reset_peq_band(b, len(self.bands))
        self.load(self.bands)
        self.changed.emit()

    def refresh_values(self):
        """Push the bands' numbers into the spin boxes, nothing else.

        load() rebuilds every widget in the table, which is fine when a
        channel is selected and far too heavy to do on each frame of a drag.
        This moves the three numbers and leaves the widgets alone.
        """
        self._loading = True
        try:
            for r, b in enumerate(self.bands):
                for col, key in ((self.C_FREQ, "freq"), (self.C_Q, "q"),
                                 (self.C_GAIN, "gain")):
                    sb = self.cellWidget(r, col)
                    if sb is not None and b.get(key) is not None:
                        sb.setValue(float(b[key]))
        finally:
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
    """Which outputs an input feeds, at what gain, and in which polarity.

    This is the mixer matrix: on the device, one `Mixer_<in>_<out>_status`
    flag and one `Mixer_<in>_<out>` gain per pair. It is edited per input
    rather than as a full grid because that matches the direction signal
    actually travels -- one source fanning out to several drivers.
    """

    changed = Signal()
    # On first: it is the thing you are setting, and the destination beside
    # it is what you are setting it for. Reading "on / Out 3" is the order
    # the decision is made in.
    # The circle-slash is what a polarity invert is marked with on mixing
    # desks and on miniDSP's own front end; spelling it out would cost more
    # width than the column has in a card this narrow.
    COLS = ["On", "To output", "Gain (dB)", "\u00f8"]
    C_ON, C_DEST, C_GAIN, C_POL = range(len(COLS))

    def __init__(self):
        super().__init__(0, len(self.COLS))
        self.setHorizontalHeaderLabels(self.COLS)
        self.verticalHeader().setVisible(False)
        hh = self.horizontalHeader()
        # Only the destination name absorbs slack; the checkbox and the gain
        # field have a fixed natural width and stretching them just pads air.
        # Fixed widths for the two narrow columns rather than sizing them to
        # their contents: a spin box with a " dB" suffix asks for far more
        # room than the number needs, and letting it have that left the
        # destination name -- the column you actually read -- squeezed out.
        hh.setMinimumSectionSize(30)
        hh.setSectionResizeMode(self.C_ON, QHeaderView.Fixed)
        hh.setSectionResizeMode(self.C_DEST, QHeaderView.Stretch)
        hh.setSectionResizeMode(self.C_GAIN, QHeaderView.Fixed)
        hh.setSectionResizeMode(self.C_POL, QHeaderView.Fixed)
        self.setColumnWidth(self.C_ON, 32)
        self.setColumnWidth(self.C_GAIN, 62)
        self.setColumnWidth(self.C_POL, 30)
        hh.setToolTip("On: does this input feed that output.  "
                      "\u00f8: invert this path's polarity.")
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
            target = next((o for o in outputs if o.get("index") == idx), None)
            # Described exactly as the navigator describes it, so the same
            # output reads the same way in both places.
            summary = ((" \u00b7 " + summarise_output(target, True))
                       if target else "")

            item = QTableWidgetItem(name + summary)
            item.setToolTip(f"{name}{summary}".strip() or name)
            item.setFlags(Qt.ItemIsEnabled)
            if not route.get("enabled"):
                item.setForeground(QColor(MUTED))
            self.setItem(r, self.C_DEST, item)

            on = QCheckBox()
            on.setChecked(bool(route.get("enabled")))
            on.toggled.connect(self._emit)
            holder = QWidget(); hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0); hl.addWidget(on)
            hl.setAlignment(Qt.AlignCenter)
            self.setCellWidget(r, self.C_ON, holder)

            sb = QDoubleSpinBox()
            sb.setRange(-127.0, 12.0); sb.setDecimals(1)
            sb.setSingleStep(0.5)
            # No " dB" suffix here. The column header already says it, and
            # repeating it on every row cost more width than the numbers do
            # in a card this narrow.
            sb.setMinimumWidth(0)
            sb.setButtonSymbols(QDoubleSpinBox.NoButtons)
            sb.setValue(float(route.get("gain", 0.0)))
            sb.valueChanged.connect(self._emit)
            self.setCellWidget(r, self.C_GAIN, sb)

            # One flag per cell, not per output: the same driver can take a
            # normal feed from one input and an inverted one from another,
            # which is the whole reason the hardware keeps sixteen of these
            # rather than eight.
            pol = QCheckBox()
            pol.setChecked(bool(route.get("polarity")))
            pol.setToolTip(
                f"Invert the polarity of {name} as fed from this input.\n"
                f"Separate from the output's own polarity, which inverts "
                f"it whatever is feeding it.")
            pol.toggled.connect(self._emit)
            ph = QWidget(); pl = QHBoxLayout(ph)
            pl.setContentsMargins(0, 0, 0, 0); pl.addWidget(pol)
            pl.setAlignment(Qt.AlignCenter)
            self.setCellWidget(r, self.C_POL, ph)
        self._loading = False

    def _emit(self, *_):
        if not self._loading:
            self.changed.emit()

    def store(self) -> list[dict[str, Any]]:
        for r, route in enumerate(self.routes):
            holder = self.cellWidget(r, self.C_ON)
            if holder is None:
                continue
            route["enabled"] = holder.findChild(QCheckBox).isChecked()
            route["gain"] = self.cellWidget(r, self.C_GAIN).value()
            ph = self.cellWidget(r, self.C_POL)
            if ph is not None:
                route["polarity"] = ph.findChild(QCheckBox).isChecked()
        return self.routes


def format_hz(f: float) -> str:
    """A corner frequency, short enough to sit in the navigator column."""
    if f >= 10000:
        return f"{f / 1000:.0f}k"
    if f >= 1000:
        return f"{f / 1000:.1f}k".replace(".0k", "k")
    return f"{f:.0f}"


def summarise_output(out: dict[str, Any], fed: bool = True) -> str:
    """One line describing what an output is actually doing.

    Shared by the navigator and the routing table so an output is described
    the same way wherever it is named. Listing an output's filters twice in
    two different dialects made the same channel look like two channels.
    """
    bits = []
    band = passband(out)
    if band:
        bits.append(band)
    pq = core.count_effective_peq(out.get("peq", []))
    if pq:
        bits.append(f"{pq} EQ")
    if bits:
        return " · ".join(bits)
    # No filters at all means two very different things, and the difference
    # matters when you are looking for a silent driver.
    return "full range" if fed else "unused"


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

    def update_contents(self, name: str, detail: str, muted: bool,
                        dim: bool) -> None:
        """Re-say what this row says, without replacing the row.

        The alternative is destroying it and building another, which is how
        the list used to answer every edit -- and a widget torn down while Qt
        still has an event in flight for it is a crash rather than an error.
        """
        self.channel_name = name
        self.full_detail = detail
        self.dim = dim
        self.name.setText(name)
        self.elide_detail(detail)
        self.set_muted(muted)

    def set_muted(self, muted: bool):
        """Show a channel as muted, without rebuilding the row.

        Flipping one icon used to go through a full list rebuild, which also
        re-selected a row and so reloaded the whole editor for a channel that
        had not changed.

        The icon needs no hedging about whether the device agrees, because
        the device is made to agree: mute is enforced rather than staged.
        See default_fir's neighbour in minidsp_core for why.
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
    fir_load_requested = Signal()    # the FIR panel wants a file

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

        basics = QGroupBox()
        bl = QHBoxLayout(basics)
        bl.addWidget(card_heading("Channel"))
        bl.addSpacing(10)
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
        self.show_phase.setStyleSheet(f"color: {PHASE};")
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
        self.plot.band_changed.connect(self._on_band_dragged)
        self.plot.xover_changed.connect(self._on_xover_dragged)
        self.plot.band_committed.connect(self._on_band_committed)
        root.addWidget(self.plot, 2)
        self.legend = QLabel("")
        self.legend.setObjectName("muted")
        root.addWidget(self.legend)

        # Crossover and routing sit beside the filter table rather than in
        # the right-hand column. Both are things you move while watching the
        # curve, so they belong next to it; the column is left for the
        # processors you set and leave. They go alongside the table rather
        # than above or below it because height is the scarce dimension on a
        # widescreen display and the plot and table are what benefit from it
        # -- the width was going spare.
        xo = QVBoxLayout()
        xo.setContentsMargins(0, 0, 0, 0)
        self.xo_groups = [CrossoverGroup(0), CrossoverGroup(1)]
        for g in self.xo_groups:
            g.changed.connect(self._emit)
            xo.addWidget(g)
        xo.addStretch(1)
        self.xo_holder = QWidget(); self.xo_holder.setLayout(xo)

        self.routing_box = QGroupBox()
        rl = QVBoxLayout(self.routing_box)
        rl.addWidget(card_heading("Routing"))
        self.routing = RoutingTable()
        self.routing.setToolTip("Which outputs this input feeds, and at "
                                "what gain")
        self.routing.changed.connect(self._emit)
        rl.addWidget(self.routing)

        # The right-hand column: the per-channel processors that are set and
        # left. Compressor for an output, FIR for an input, mirroring how
        # crossover and routing swap beside the table.
        self.side = QWidget()
        side_l = QVBoxLayout(self.side)
        side_l.setContentsMargins(0, 0, 0, 0)
        self.comp = CompressorPanel()
        self.comp.changed.connect(self._emit)
        side_l.addWidget(self.comp)
        self.fir = FirPanel()
        self.fir.changed.connect(self._emit)
        self.fir.load_requested.connect(self.fir_load_requested.emit)
        side_l.addWidget(self.fir)
        side_l.addStretch(1)

        # Two views of one set of bands: the parameters, or the coefficients
        # they compile to. Tabs rather than a second panel, because it is the
        # same ten filters either way.
        peq_box = QGroupBox()
        pl = QVBoxLayout(peq_box)
        self.peq_tabs = TabStrip(["Parametric EQ", "Biquad"])
        self.peq_tabs.selected.connect(self._show_peq_tab)
        # The tab strip already ends in a stretch, so the reset sits at the
        # far right of the same row rather than taking a row of its own.
        self.reset_all_btn = QPushButton("  Reset all bands")
        self.reset_all_btn.setObjectName("resetAll")
        self.reset_all_btn.setIcon(reset_icon(13))
        self.reset_all_btn.setIconSize(QSize(13, 13))
        self.reset_all_btn.setCursor(Qt.PointingHandCursor)
        self.reset_all_btn.setToolTip(
            "Put every band back to its starting point: spread across the "
            "range, no boost or cut, all switched off")
        self.reset_all_btn.clicked.connect(self._on_reset_all)
        tabs_row = QWidget()
        trl = QHBoxLayout(tabs_row)
        trl.setContentsMargins(0, 0, 0, 0)
        trl.addWidget(self.peq_tabs, 1)
        trl.addWidget(self.reset_all_btn)
        pl.addWidget(tabs_row)

        self.peq = PeqTable()
        self.peq.changed.connect(self._emit)
        self.bq = BiquadTable()
        self.bq.changed.connect(self._emit)
        self.peq_stack = QStackedWidget()
        self.peq_stack.addWidget(self.peq)
        self.peq_stack.addWidget(self.bq)
        pl.addWidget(self.peq_stack, 1)

        # The filter table and whatever this channel's other curve-adjacent
        # controls are, side by side.
        lower = QWidget()
        low_l = QHBoxLayout(lower)
        low_l.setContentsMargins(0, 0, 0, 0)
        low_l.setSpacing(8)
        low_l.addWidget(peq_box, 1)
        # One width for both, even though only one is ever on screen. They
        # take their room from the same row as the filter table, so letting
        # them differ resized the table when you moved between an input and
        # an output -- the table is the thing you are reading, and it should
        # not change shape underneath you. The figure is what routing needs:
        # its longest destination is a name plus a passband.
        self.xo_holder.setFixedWidth(SIDE_PANEL_W)
        self.routing_box.setFixedWidth(SIDE_PANEL_W)
        low_l.addWidget(self.xo_holder)
        low_l.addWidget(self.routing_box)
        root.addWidget(lower, 3)

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
        # The compressor is an output-only processor on this hardware, and
        # FIR an input-only one. They take the same place.
        self.comp.setVisible(is_output)
        self.fir.setVisible(not is_output)
        if is_output:
            groups = chan.get("crossover", [])
            for widget, group in zip(self.xo_groups, groups):
                widget.load(group)
            self.comp.load(chan.get("compressor"))
        else:
            self.routing.load(chan.get("routing", []),
                              (self.project or {}).get("outputs", []))
            self.fir.load(chan.setdefault("fir", core.default_fir()))
        rate = int((self.project or {}).get("rate", 96000))
        self.peq.rate = self.bq.rate = rate
        self._active_peq().load(chan.get("peq", []))
        self._update_chain()
        self._loading = False
        self.refresh_plot()

    def _on_reset_all(self):
        """Every band in this channel's bank back to stock.

        Asked first: this throws away the whole bank, and on a channel read
        from the device that is a tuning somebody made.
        """
        if self.chan is None:
            return
        bands = self.chan.get("peq", [])
        touched = [b for b in bands
                   if b.get("enabled") or b.get("manual")
                   or abs(float(b.get("gain") or 0.0)) > 1e-9]
        if touched:
            resp = QMessageBox.question(
                self, "Reset all bands?",
                f"{len(touched)} of {len(bands)} bands on "
                f"{self.chan.get('name', 'this channel')} are in use.\n\n"
                "Resetting spreads every band back across the range at no "
                "boost or cut and switches them all off. The device is not "
                "written until you save.",
                QMessageBox.Reset | QMessageBox.Cancel, QMessageBox.Cancel)
            if resp != QMessageBox.Reset:
                return
        table = self._active_peq()
        if hasattr(table, "reset_all"):
            table.reset_all()
        else:
            for b in bands:
                core.reset_peq_band(b, len(bands))
            table.load(bands)
            self._emit()

    def _on_band_dragged(self, index: int, fields: dict):
        """A band was dragged or scrolled on the plot.

        Updates the band, the table and the curve, but does not announce a
        change: a drag produces these continuously, and marking the project
        dirty on every frame would queue an autosave per pixel. The
        announcement comes once, on release.
        """
        if self.chan is None:
            return
        band = next((b for b in self.chan.get("peq", [])
                     if b.get("index") == index), None)
        if band is None:
            return
        band.update(fields)
        # Dragging is an explicit statement about where the band should be,
        # so it counts as the user setting it -- the same as typing in the
        # table.
        band["enabled"] = True
        band["bypass_source"] = "user"
        table = self._active_peq()
        if hasattr(table, "refresh_values"):
            table.refresh_values()
        self.refresh_plot()

    def _on_xover_dragged(self, index: int, fields: dict):
        """A crossover marker was dragged or scrolled on the plot."""
        if self.chan is None:
            return
        group = next((g for g in self.chan.get("crossover", [])
                      if g.get("index") == index), None)
        if group is None:
            return
        group.update(fields)
        group["manual"] = None
        group["enabled"] = True
        group["bypass_source"] = "user"
        for widget, g in zip(self.xo_groups, self.chan.get("crossover", [])):
            if g is group:
                widget.load(group)
        self.refresh_plot()

    def _on_band_committed(self):
        """The drag ended. Now the project has changed."""
        if self.chan is None:
            return
        self._active_peq().load(self.chan.get("peq", []))
        self._emit()

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
            # store() writes into the dict the panel was loaded with, which
            # is this channel's own, so there is nothing to assign back.
            self.comp.store()
        else:
            self.chan["routing"] = self.routing.store()
            self.fir.store()
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
        # This channel's phase in teal; whatever it crosses over with in
        # grey. The comparison is between one trace and its context, and
        # giving the context its own bright colour made two equals out of
        # what is really a subject and a backdrop.
        for chan, colour in [(self.chan, PHASE)] + [
                (p, MUTED) for p in self._crossover_partners()]:
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
                "q": b.get("q"),
                # A band with typed coefficients has no frequency or gain to
                # move -- its marker sits where the response peaks, not where
                # a design parameter points -- so it is shown but not
                # grabbable. Dragging it would have to invent a design and
                # throw away what was typed.
                "grab": not b.get("manual"),
                # How the marker's height relates to the band's gain, so a
                # drag can set a gain that puts the marker back under the
                # pointer. A peaking filter reaches its full gain at f0; a
                # shelf is only half way up there, so dragging one to +6
                # means a gain of +12. None means the shape has no level to
                # set and the drag is horizontal only -- dragging a
                # high-pass vertically would write a gain its type ignores.
                "gain_scale": {"peaking": 1.0, "lowshelf": 2.0,
                               "highshelf": 2.0}.get(b.get("type")),
            })

        # The crossover groups, lettered rather than numbered so a corner is
        # never mistaken for a PEQ band. The marker sits on the group's own
        # curve at its corner, which for a pass filter is some way down from
        # 0 dB, so it lands on the line it labels.
        for gi, group in enumerate(self.chan.get("crossover", [])):
            if not group.get("enabled"):
                continue
            bqs = [b for b in core.crossover_biquads(group, rate)
                   if not core.is_bypass(b)]
            if not bqs:
                continue
            dbs = core.response_db(bqs, freqs, rate)
            f0 = float(group.get("freq", 1000.0))
            nearest = min(range(len(freqs)),
                          key=lambda i: abs(freqs[i] - f0))
            out.append({
                "index": gi,
                "label": "AB"[gi] if gi < 2 else str(gi + 1),
                "kind": "xover",
                "colour": ACCENT if gi == 0 else WARN,
                "dbs": dbs,
                "mark_f": f0,
                "mark_db": dbs[nearest],
                "q": None,
                # No level to set, so a drag is horizontal only; the wheel
                # steps the order rather than a Q.
                "gain_scale": None,
                "order": group.get("order"),
                "alignment": group.get("alignment"),
                # A group whose coefficients were typed has no corner to
                # move, the same as a hand-typed PEQ band.
                "grab": group.get("alignment") in core.ALIGNMENTS,
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
        was_muted, self.muted = self.muted, muted
        # MUTE ALL keeps its label and signals state by colour alone: a
        # dark LED and white text while sound is passing, both red once
        # the device is muted. The label never changes, so the button
        # never looks like a different control.
        # Only when it actually changes. This ran on every poll, which
        # built a fresh icon and pushed the button through the style engine
        # twice a second for the life of the session, to arrive at the
        # appearance it already had.
        if was_muted != muted or self.panic_btn.icon().isNull():
            self.panic_btn.setIcon(
                led_icon(12, DANGER if muted else "#000000"))
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



class BusyDialog(QDialog):
    """What the device is doing, while the window is held for it.

    Shown, never exec()'d. exec() runs a nested event loop, which is a
    second place for timers to be dispatched from and was the context one of
    this app's segfaults landed in. show() on a modal dialog blocks input to
    the rest of the window without one.

    No buttons. Nothing here can be cancelled: a flash write stopped halfway
    leaves a preset that is neither what it was nor what it was going to be,
    and there is no way to ask the device to undo the blocks already sent.
    Saying so with an absent button is better than offering one that lies.
    """

    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        # No close button: the dialog goes away when the work does.
        self.setWindowFlags(Qt.Dialog | Qt.CustomizeWindowHint
                            | Qt.WindowTitleHint)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(10)
        self.label = QLabel(title)
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.bar = QProgressBar()
        # Indeterminate until something reports a byte count. Several
        # phases here have none to report -- the live parameter reads, the
        # gain verification loop -- and a bar inventing a percentage for
        # those would be worse than one that just says "working".
        self.bar.setRange(0, 0)
        self.bar.setTextVisible(False)
        lay.addWidget(self.bar)
        self.note = QLabel("The window is held until this finishes.")
        self.note.setObjectName("muted")
        lay.addWidget(self.note)
        self.setFixedWidth(380)

    def set_progress(self, done: int, total: int) -> None:
        if total <= 0:
            self.bar.setRange(0, 0)
            return
        self.bar.setRange(0, total)
        self.bar.setValue(done)

    def closeEvent(self, event):
        """Only closable from code -- there is nothing to cancel."""
        event.ignore()


class ImportDialog(QDialog):
    """Pick a source to import from. The destination is where you already are.

    One direction, so there is nothing to remember between two actions. The
    page underneath is the destination, which means the source list only ever
    offers like for like -- on an output you are shown outputs -- and there is
    no wrong pairing to detect and refuse, because none can be expressed.

    Nothing is read here. The read happens after this closes, so arrowing
    through the preset list does not fire one flash read per keystroke, and
    the result is visible the moment it lands: the destination is the page
    you are looking at.
    """

    def __init__(self, parent, kind: str, n_presets: int,
                 current_preset: int | None, names: list[str],
                 here: int | None):
        super().__init__(parent)
        self.setWindowTitle(f"Import {kind}")
        self.setMinimumWidth(360)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(8)

        what = "settings" if here is None else f"into {names[here]}"
        hint = QLabel(f"Choose where to bring {what} from. Nothing is "
                      f"written to the device -- the import lands in this "
                      f"project, and Apply or Save is still yours to press.")
        hint.setObjectName("muted"); hint.setWordWrap(True)
        lay.addWidget(hint)

        form = QGridLayout()
        form.setColumnStretch(1, 1)
        lab = QLabel("Preset"); lab.setObjectName("muted")
        form.addWidget(lab, 0, 0)
        self.preset = QComboBox()
        for i in range(n_presets):
            mark = "  (this one)" if i == current_preset else ""
            self.preset.addItem(f"Preset {i + 1}{mark}", i)
        if current_preset is not None:
            self.preset.setCurrentIndex(current_preset)
        form.addWidget(self.preset, 0, 1)

        self.chan = None
        if here is not None:
            lab = QLabel(kind.capitalize()); lab.setObjectName("muted")
            form.addWidget(lab, 1, 0)
            self.chan = QComboBox()
            form.addWidget(self.chan, 1, 1)
        lay.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.go = buttons.addButton("Import", QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        # After the button box: _refill enables and disables Import, so it
        # cannot run before there is an Import button to speak of.
        self._names, self._here = names, here
        self._current_preset = current_preset
        if self.chan is not None:
            self.preset.currentIndexChanged.connect(self._refill)
            self._refill()

    def _refill(self):
        """Offer every like channel, minus the one we would import onto.

        Importing a channel onto itself is a no-op, so it is not offered --
        but only when the source preset is the one on screen. From another
        preset the same index is the most useful entry in the list, because
        it is how you get a channel's earlier tuning back.
        """
        same = self.preset.currentData() == self._current_preset
        keep = self.chan.currentData()
        self.chan.blockSignals(True)
        self.chan.clear()
        for i, name in enumerate(self._names):
            if same and i == self._here:
                continue
            self.chan.addItem(name, i)
        idx = self.chan.findData(keep)
        self.chan.setCurrentIndex(max(0, idx))
        self.chan.blockSignals(False)
        self.go.setEnabled(self.chan.count() > 0)

    def choice(self) -> tuple[int, int | None]:
        """(preset index, channel index or None for a whole preset)."""
        return (self.preset.currentData(),
                self.chan.currentData() if self.chan is not None else None)


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
    # Emitted from the worker thread while a flash read runs, so the bar
    # updates on the main thread by queued connection rather than by a
    # widget being touched from the wrong one.
    read_progress = Signal(int, int)

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
        self._last_stored = None
        # The stored-preset warning is shown once per session, not on every
        # write: the button says what it does, and a modal on every press
        # during a tuning pass teaches people to dismiss it unread.
        self._warned_store = False
        # Whether the device's stored preset matches the project. Starts
        # false because nothing is known before a read, and a lamp that
        # claims "stored" without having looked is worse than no lamp.
        self.stored_current = False
        # The dialog raised while the device is busy, so a second operation
        # cannot stack another one on top of the first.
        self._busy_dlg: "BusyDialog | None" = None
        # The last read's comparison of running against stored: what was
        # compared, what could not be, and what differed. None before a read.
        self._live_vs_stored = None
        # Which preset the project on screen came from, and which one the
        # device is actually running. Changing preset switches the device and
        # leaves the project showing the old one, so without these two the
        # app can display preset 1 while preset 3 is playing and say nothing.
        self._project_preset: int | None = None
        self._active_preset: int | None = None
        # Last folder used per kind of file dialog.
        self._dirs: dict[str, Path] = {}
        # True while a write is in flight. Both writes share the one command
        # endpoint, and update_warning() runs from several places, so the
        # buttons' enabled state is derived from this rather than set by
        # whichever handler ran last.
        self._writing = False

        self.setWindowTitle("LiniDi")
        self.setWindowIcon(app_icon())
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
        # The two writes, at the far end of the card away from anything
        # pressed routinely. Apply is the reversible one -- it changes what
        # the device is doing, and a power cycle undoes it. Save also stores
        # the preset, which is what the device loads at power-on, and nothing
        # undoes that. Each carries a lamp for its own half of the state.
        self.apply_btn = QPushButton("  Apply Edits")
        self.apply_btn.setObjectName("applyEdits")
        self.apply_btn.setIcon(led_icon(10, MUTED))
        self.apply_btn.setIconSize(QSize(10, 10))
        self.apply_btn.setToolTip(
            "Write the edits to the device so you can hear them. Undone by a "
            "power cycle, which makes it the safe one to experiment with.")
        self.apply_btn.clicked.connect(self.on_apply)
        self.save_device_btn = QPushButton("  Save Edits")
        self.save_device_btn.setObjectName("saveEdits")
        self.save_device_btn.setIcon(led_icon(10, MUTED))
        self.save_device_btn.setIconSize(QSize(10, 10))
        self.save_device_btn.setToolTip(
            "Write the edits and store them in the device, so they are what "
            "it loads when powered on. There is no undo.")
        self.save_device_btn.clicked.connect(self.on_save_device)
        self.help_btn = QPushButton()
        self.help_btn.setIcon(help_icon(18))
        self.help_btn.setIconSize(QSize(18, 18))
        self.help_btn.setFixedWidth(34)
        self.help_btn.setCursor(Qt.PointingHandCursor)
        self.help_btn.setToolTip("What this is, how it works, and what to be "
                                 "careful with")
        self.help_btn.clicked.connect(self.on_help)
        self.master.add_trailing(self.apply_btn, spacing=8)
        self.master.add_trailing(self.save_device_btn, spacing=8)
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
        SIDE_BTN_W = 124
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

        # Import runs one way, into where you already are, so these say what
        # they will land on rather than naming a source. The channel one is
        # relabelled on every selection because the page is the destination.
        self.import_preset_btn = QPushButton("Import preset")
        self.import_preset_btn.setToolTip(
            "Bring another preset's whole configuration into this project.\n"
            "Reads that preset out of the device's flash. Nothing is "
            "written.")
        self.import_preset_btn.clicked.connect(self.on_import_preset)

        self.import_chan_btn = QPushButton("Import output")
        self.import_chan_btn.clicked.connect(self.on_import_channel)

        for b in (self.read_btn, self.xml_btn, self.rew_btn,
                  self.save_btn, self.load_btn):
            b.setFixedWidth(BTN_W)
            bar.addWidget(b)

        # Set apart, and smaller. The five before these act on the device or
        # on a file; these two only move values around inside the project,
        # which is a lighter thing to be doing. Equal size would have said
        # they carry equal weight, and the gap says they are a different
        # kind of action rather than the sixth and seventh of a series.
        bar.addSpacing(22)
        for b in (self.import_preset_btn, self.import_chan_btn):
            b.setFixedWidth(SIDE_BTN_W)
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
        self.editor.fir_load_requested.connect(self.on_fir_load)
        body.addWidget(self.editor, 1)

        right = QWidget()
        ml = QVBoxLayout(right)
        ml.setContentsMargins(8, 8, 8, 8)
        levels = QGroupBox()
        lv = QVBoxLayout(levels)
        lv.addWidget(card_heading("Levels"))
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
        # The device layer refuses a write that would strip a crossover off
        # a live output unless something can ask about it. This is that
        # something. A script talking to the device directly installs no
        # handler and so gets the refusal, which is the point: the guard
        # used to live here and every test script went around it.
        native.set_confirm_handler(self._confirm_dangerous)
        self.setStatusBar(QStatusBar())
        # Flash reads take seconds and the layer underneath has always
        # reported how far along it is; nothing ever displayed it.
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(180)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.read_progress.connect(self._on_progress)

        # Three rates, because three things change at three speeds.
        #
        # The bars are redrawn at screen rate so their decay actually
        # animates. That is what made them look choppy: the ballistics were
        # only advanced when a sample arrived, so a 90 dB/s release rendered
        # at two frames a second. Repainting costs no USB traffic at all.
        #
        # Levels are sampled often, which is affordable because the meters
        # are contiguous and cost two reads. The master block -- preset,
        # source, volume, mute -- changes when somebody touches something,
        # so it stays slow.
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.tick)
        self.meter_poll = QTimer(self)
        self.meter_poll.timeout.connect(self.tick_meters)
        self.meter_anim = QTimer(self)
        self.meter_anim.timeout.connect(self.tick_animate)
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
        # Only the direct-USB path can read meters on their own; over the
        # daemon they arrive with the status block, so the fast timer would
        # have nothing cheap to ask for.
        if hasattr(self.daemon, "meters"):
            self.meter_poll.start(60)
        self.meter_anim.start(16)
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
        return summarise_output(out, fed)

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

        This is a live control rather than an edit staged for saving:
        reaching for mute during a measurement means you want that
        driver quiet now.
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
        """Update one navigator row in place. The device has just spoken."""
        for r in range(self.chan_list.count()):
            if self.chan_list.item(r).data(Qt.UserRole) == (kind, index):
                row = self.chan_list.itemWidget(self.chan_list.item(r))
                if row is not None:
                    row.set_muted(muted)
                return

    def _row_contents(self):
        """What every channel row should say, in list order."""
        rows = []
        for i, inp in enumerate(self.project["inputs"]):
            pq = core.count_effective_peq(inp.get("peq", []))
            rows.append(("input", i, inp, f"{pq} EQ" if pq else "flat",
                         False))
        fed = self._outputs_fed()
        for i, out in enumerate(self.project["outputs"]):
            summary = self._summarise_output(out, i in fed)
            rows.append(("output", i, out, summary, summary == "unused"))
        return rows

    def _refresh_rows_in_place(self, wanted) -> bool:
        """Update the existing rows, or say the list has to be rebuilt.

        clear() destroys every row widget, and a widget destroyed while Qt
        still has an event in flight for it is a dangling receiver -- which
        is what a segfault inside activateTimers looks like, with no Python
        frame running because the fault happens before any slot is entered.

        Rebuilding was never needed for the common case anyway. Editing a
        channel changes what a row *says*, not which rows exist, and the
        same reasoning already applies to mute: flipping that icon stopped
        going through a rebuild for its own reasons. This extends it to the
        rest of the row. A rebuild still happens when the shape really does
        change -- a different device, a project with other channel counts.
        """
        keys = [(k, i) for k, i, _c, _d, _dim in wanted]
        have = []
        for r in range(self.chan_list.count()):
            key = self.chan_list.item(r).data(Qt.UserRole)
            if key:
                have.append((r, key))
        if [k for _r, k in have] != keys:
            return False
        for (r, _key), (_k, _i, chan, detail, dim) in zip(have, wanted):
            row = self.chan_list.itemWidget(self.chan_list.item(r))
            if row is None:
                return False
            row.update_contents(chan.get("name", ""), detail,
                                bool(chan.get("mute")), dim)
        return True

    def refresh_list(self):
        """Channel list ordered by signal flow: inputs first, then outputs."""
        wanted = self._row_contents()
        if self._refresh_rows_in_place(wanted):
            self._paint_selection()
            QTimer.singleShot(0, self._elide_details)
            return

        prev = self.chan_list.currentItem()
        prev_key = prev.data(Qt.UserRole) if prev else None

        self.chan_list.blockSignals(True)
        self.chan_list.clear()
        self._name_col = self._name_column()

        self._add_header("Inputs · voicing")
        for kind, i, chan, detail, dim in wanted:
            if kind == "output":
                continue
            self._add_channel_row(kind, i, chan, detail, dim)

        self._add_header("Outputs · crossover")
        for kind, i, chan, detail, dim in wanted:
            if kind == "input":
                continue
            self._add_channel_row(kind, i, chan, detail, dim)

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
        self._import_buttons_state()
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

    def _update_leds(self) -> None:
        """The two lamps: is it running, and is it stored.

        Green means the device agrees with the project. Amber means work
        outstanding -- deliberately not red, which is reserved here for
        things that can damage a driver. Amber also differs from green in
        brightness as well as hue, so the pair survives colour blindness,
        and neither lamp is the only place its state is written: the label
        beside them says the same thing in words.

        They fill in left to right, because storing implies applying.
        """
        applied = bool(self.have_read) and not self.dirty
        stored = applied and bool(self.stored_current)
        self.apply_btn.setIcon(led_icon(10, OK if applied else WARN))
        self.save_device_btn.setIcon(led_icon(10, OK if stored else WARN))

        cmp_ = self._live_vs_stored or {}
        unread = cmp_.get("unreadable", 0)
        caveat = (f"\nCompared {cmp_.get('compared', 0)} parameters; "
                  f"{unread} filters cannot be read back and are taken from "
                  f"the stored preset." if cmp_ else "")
        self.apply_btn.setToolTip(
            ("The device is running these edits. A power cycle undoes it."
             if applied else
             "Edits are not on the device yet. Click to hear them; a power "
             "cycle undoes it.") + caveat)

        if stored:
            tip = "Stored: this is what the device loads at power-on."
        else:
            diffs = cmp_.get("differences") or []
            detail = ""
            if diffs and not self.dirty:
                detail = ("\n\nWhat is running differs from what is "
                          "stored:\n  " + "\n  ".join(diffs[:6]))
                if len(diffs) > 6:
                    detail += f"\n  ... and {len(diffs) - 6} more"
            tip = ("Not stored. The device would come back to something else "
                   "after a power cycle. There is no undo once stored."
                   + detail)
        self.save_device_btn.setToolTip(tip)

    def update_warning(self):
        unknown = core.unknown_bypass(self.project) if self.project else []
        self._update_leds()
        # Ahead of everything else: if the screen is showing one preset and
        # the device is running another, nothing else on the strip means what
        # it appears to mean.
        if (self._project_preset is not None
                and self._active_preset is not None
                and self._project_preset != self._active_preset):
            self.warn_label.setText(
                f"Showing preset {self._project_preset + 1}, device is on "
                f"preset {self._active_preset + 1} - read to catch up")
            self.warn_label.setStyleSheet(f"color: {DANGER};")
            self._enable_writes(False)
            return
        # Which preset this is leads every other message on the strip. It is
        # the thing that decides what all of it means, and it was previously
        # nowhere on screen except a combo box that shows the device's preset
        # rather than the one being displayed.
        if self._project_preset is not None:
            where = f"Preset {self._project_preset + 1}"
        elif self._active_preset is not None:
            where = f"Preset {self._active_preset + 1} (imported, not read)"
        else:
            where = "No preset read"
        if unknown:
            self.warn_label.setText(
                f"{where}  -  {len(unknown)} filter state(s) unknown, "
                "read from device to enable writing")
            self.warn_label.setStyleSheet(f"color: {DANGER};")
            self._enable_writes(False)
            return
        self._enable_writes(not self._writing)
        if not self.have_read:
            self.warn_label.setText(
                f"{where}  -  not yet read from device, writing would "
                f"overwrite it")
            self.warn_label.setStyleSheet(f"color: {WARN};")
        elif self.dirty:
            self.warn_label.setText(f"{where}  -  edits not applied")
            self.warn_label.setStyleSheet(f"color: {ACCENT};")
        elif not self.stored_current:
            self.warn_label.setText(f"{where}  -  applied, not stored")
            self.warn_label.setStyleSheet(f"color: {ACTIVE};")
        else:
            self.warn_label.setText(f"{where}  -  in sync")
            self.warn_label.setStyleSheet(f"color: {MUTED};")

    def _suspended(self) -> bool:
        """Whether a background tick should do nothing this time.

        A modal dialog runs its own event loop, so these timers keep firing
        underneath one -- reaching the device and repainting widgets while
        the user is being asked a question about those same widgets, and
        while the handler that opened the dialog is still part-way through
        its own work. Nobody can see the window behind a modal anyway, so
        there is nothing to update and nothing lost by waiting.
        """
        return QApplication.activeModalWidget() is not None

    def tick(self):
        """The slow poll: master state, and levels only if nothing faster is
        supplying them."""
        if self._suspended():
            return
        try:
            status = self.daemon.status()
        except Exception:                                  # noqa: BLE001
            # Includes a protocol timeout, which the device
            # produces whenever it is busy. Not worth a dialog.
            return
        self.master.update_status(status)
        was = self._active_preset
        try:
            self._active_preset = int(status["master"]["preset"])
        except (KeyError, TypeError, ValueError):
            self._active_preset = None
        if was != self._active_preset:
            self.update_warning()
        if self.meter_poll.isActive():
            return
        for m, v in zip(self.in_meters, status.get("input_levels", [])):
            m.set_value(v)
        for m, v in zip(self.out_meters, status.get("output_levels", [])):
            m.set_value(v)

    def _tick_compressor_meter(self):
        """Gain reduction for the output on screen, if it has a compressor.

        Only the selected channel's, and only while that panel is showing:
        reading all eight every frame would cost more than the level meters
        do, for a number nobody is looking at.
        """
        ed = self.editor
        if ed.chan is None or not ed.is_output or not ed.comp.isVisible():
            return
        fn = getattr(self.daemon, "compressor_meters", None)
        if fn is None:
            return
        try:
            vals = fn()
        except Exception:                                  # noqa: BLE001
            return
        idx = ed.chan.get("index", 0)
        if idx < len(vals):
            ed.comp.set_reduction(vals[idx])

    def tick_meters(self):
        """The fast poll: levels only. Two reads, whatever the channel count.

        A failure here is ignored rather than reported: this runs many times
        a second, and the device is busy during an apply or a save, so a
        missed sample is normal and a dialog about it would be intolerable.
        """
        if self._suspended():
            return
        try:
            ins, outs = self.daemon.meters()
        except Exception:                                  # noqa: BLE001
            return
        for m, v in zip(self.in_meters, ins):
            m.set_value(v)
        for m, v in zip(self.out_meters, outs):
            m.set_value(v)
        self._tick_compressor_meter()

    def tick_animate(self):
        """Advance every bar's ballistics one frame. No device access."""
        if self._suspended():
            return
        for m in self.in_meters:
            m.animate()
        for m in self.out_meters:
            m.animate()
        if self.editor.comp.isVisible():
            self.editor.comp.gr.animate()

    def on_master_change(self, payload):
        """Volume, mute, source or preset, straight to the device.

        A preset change is followed by a read. Switching preset used to move
        the device and leave the screen showing the preset before it, with
        nothing to say so -- and since a stock preset is flat with no
        crossovers, that reads exactly like a device that has lost its
        configuration. The display now follows the device.
        """
        switching = "preset" in payload

        def done(_):
            if switching:
                self.on_read()

        self.tasks.run(
            lambda: self.daemon.set_master(**payload),
            on_done=done,
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
        self.set_device_busy(True, "Reading the device")
        self.statusBar().showMessage("Reading from device...")
        n_out = len(self.project["outputs"])
        n_in = len(self.project["inputs"])
        # Which preset is active decides which stored slot gets read and
        # which one everything on screen is then labelled with. Falling back
        # to 0 meant that a failed status read showed preset 1's stored
        # configuration while the device ran preset 3, with nothing saying
        # so -- and a preset's worth of unfamiliar settings appearing for no
        # visible reason is how an afternoon gets lost. Better to not read.
        try:
            preset = int(self.daemon.status()["master"].get("preset", 0))
        except Exception as exc:                           # noqa: BLE001
            self.set_device_busy(False)
            self.statusBar().showMessage(
                f"Read stopped: the device did not report which preset is "
                f"active ({exc})", 10000)
            QMessageBox.warning(
                self, "Cannot tell which preset is active",
                "The device did not answer when asked which preset it is "
                "running.\n\nReading anyway would mean showing one preset's "
                "settings while the device runs another, so nothing was "
                "read. Try again.")
            return
        self._reading_preset = preset

        # Only the direct-USB path can reach flash; over the daemon there is
        # no way to issue a raw flash read.
        native = self.readback if hasattr(self.readback,
                                          "stored_config") else None
        if native is not None and not native.preset_slots_known():
            self.statusBar().showMessage(
                "Reading from device -- first time on this unit, so its "
                "flash is being mapped. About 25 seconds.")

        def work():
            outs = self.readback.read_all(n_out)
            ins = self.readback.read_inputs(n_in)
            # PEQ coefficients, mixer gates and bypass flags do not answer a
            # parameter read. They are in the stored preset, which is also
            # the device, so a read is answered entirely by the hardware.
            # Nothing here falls back to a settings file: a value on screen
            # after a read came from the device or it is not there at all.
            stored = stored_error = None
            if native is not None:
                try:
                    stored = native.stored_config(
                        preset,
                        preset=native.read_stored_preset(
                            preset,
                            progress=lambda d, t:
                                self.read_progress.emit(d, t)))
                except Exception as exc:               # noqa: BLE001
                    stored_error = str(exc)
            return outs, ins, stored, stored_error

        self.tasks.run(work, on_done=self._read_done,
                       on_error=self._read_failed)

    def _read_done(self, result):
        self.progress.hide()
        self.set_device_busy(False)
        readings, input_readings, stored, stored_error = result
        core.apply_readback(self.project, readings, input_readings)
        self._last_stored = None
        if stored is not None:
            # Applied after the live readings, deliberately: it supplies the
            # three things a parameter read cannot, and leaves the ones it
            # can to the live answer.
            self._last_stored = core.apply_stored_preset(self.project, stored)
            # Whether the device is running what it would come back as. A
            # disagreement means someone applied without saving -- possibly
            # this app, on an earlier run -- and the lamp should say so, and
            # say which parameters.
            cmp_ = core.compare_live_stored(readings, input_readings, stored)
            self._live_vs_stored = cmp_
            self.stored_current = (cmp_["compared"] > 0
                                   and not cmp_["differences"])
        if stored_error:
            self.statusBar().showMessage(
                f"could not read the stored preset: {stored_error}", 8000)
        self.have_read = True
        self.dirty = False
        self._project_preset = getattr(self, "_reading_preset", None)
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
        if self._last_stored is not None:
            s = self._last_stored
            msg += (f"  -  PEQ, routing and bypass read from the device's "
                    f"stored preset: {s['peq']} bands, {s['routing']} mixer "
                    f"cells, {s['bypassed']} bypassed")
            diffs = (self._live_vs_stored or {}).get("differences") or []
            if diffs:
                msg += (f"  -  {len(diffs)} parameter(s) differ between what "
                        f"it is running and what it has stored, so a power "
                        f"cycle would change it: {diffs[0]}"
                        + (f" (+{len(diffs) - 1} more)"
                           if len(diffs) > 1 else ""))
        elif unread:
            msg += (f"  -  {unread} PEQ bands unavailable: they do not "
                    "answer a parameter read and no preset was found in this "
                    "device's flash. Import XML can load them from a file.")
        self.statusBar().showMessage(msg, 15000)

    def _read_failed(self, msg):
        self.progress.hide()
        self.set_device_busy(False)
        self.statusBar().showMessage(f"Read failed: {msg}", 8000)
        QMessageBox.warning(self, "Read failed", msg)

    def _write_preflight(self) -> bool:
        """The refusals both writes share. True if it is safe to go on.

        Identical for Apply and Save because the danger is in what gets
        written, not in how long it lasts: an unstable section reaching a
        driver is just as bad for the minute before you power cycle.
        """
        # An unstable section does not filter, it runs away, and its output
        # goes to a driver. Never write one, whatever else is in the payload.
        unstable = core.unstable_filters(self.project)
        if unstable:
            QMessageBox.warning(
                self, "Unstable filter",
                "These bands have coefficients whose poles are on or outside "
                "the unit circle:\n\n  "
                + "\n  ".join(unstable[:10])
                + "\n\nA section like that does not filter, it runs away, "
                  "and its output goes straight to a driver. Correct them on "
                  "the Biquad tab, or switch those bands off.")
            return False

        # If any filter's state is still unknown the app does not know what
        # it would be writing. Refuse rather than guess: a wrong guess
        # switches a filter on or off, and on an active crossover that
        # reaches a driver.
        unknown = core.unknown_bypass(self.project)
        if unknown:
            shown = "\n".join(f"  \u2022 {u}" for u in unknown[:10])
            more = (f"\n  ... and {len(unknown) - 10} more"
                    if len(unknown) > 10 else "")
            QMessageBox.warning(
                self, "Filter states unknown",
                f"{len(unknown)} filter(s) have an unknown bypass state: a "
                "parameter read cannot report whether a filter is "
                "bypassed.\n\n"
                f"{shown}{more}\n\n"
                "Read from device to load the real states -- the stored "
                "preset carries every one -- or click each filter's enable "
                "box to set it explicitly. Writing now would switch filters "
                "on or off at random.")
            return False

        # A write that takes a crossover out of circuit on an output that
        # will still carry signal is worth saying out loud, once, with the
        # outputs named. It is not worth refusing: whoever is looking at
        # this knows what is wired to their own amplifier, and an app that
        # will not let them clear a preset is not keeping anybody safe.
        # Checked against what the device actually holds, not against what
        # the app last thought, because an import replaces the project
        # wholesale.
        if not self.have_read:
            resp = QMessageBox.warning(
                self, "Overwrite device configuration?",
                "You have not read the current configuration from this "
                "device.\n\n"
                "Writing now replaces whatever is loaded, including any "
                "crossover you set up elsewhere.\n\n"
                "Read from device first?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Yes)
            if resp == QMessageBox.Yes:
                self.on_read()
                return False
            if resp == QMessageBox.Cancel:
                return False
        return True

    def _confirm_dangerous(self, title: str, detail: str) -> bool:
        """Put a dangerous write to the person about to make it."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle(title)
        box.setText(detail)
        go = box.addButton("Continue", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        return box.clickedButton() is go

    def on_apply(self):
        """Write the edits to working memory, so they can be heard.

        Reversible: the device reloads its stored preset at power-on, so a
        power cycle undoes whatever this wrote. That is what makes it the one
        to experiment with.
        """
        if not self._write_preflight():
            return
        self._set_writing(True, "Applying edits to the device")
        self.statusBar().showMessage("Applying edits...")
        # Gain writes are always verified. The device snaps gain to a linear
        # grid, not to the nearest step, so writing back the value it just
        # reported moves it further down -- an unverified write attenuates
        # every output a little, every time.
        project = copy.deepcopy(self.project)
        self.tasks.run(
            lambda: core.apply_project(
                self.daemon, project, readback=self.readback,
                fir_progress=lambda d, t: self.read_progress.emit(d, t)),
            on_done=self._apply_done, on_error=self._write_failed)

    def _confirm_enforcement(self, what: str,
                             incoming: dict[str, Any]) -> bool:
        """Say what loading will do to the device, and let it be refused.

        Confirm, not block. Whoever is running a DSP can decide whether
        their amplifiers should be on for this; what they cannot do is
        decide about a write nobody told them was coming.

        The unmute direction leads, because that is the one that makes
        noise. A channel this config leaves passing, which the device
        currently has muted, will start carrying signal the moment the file
        opens.
        """
        if self.daemon is None or self.project is None:
            return True
        now = {}
        for kind in ("inputs", "outputs"):
            for c in self.project.get(kind, []):
                now[(kind, c["index"])] = bool(c.get("mute"))
        will_mute, will_pass = [], []
        for kind in ("inputs", "outputs"):
            for c in incoming.get(kind, []):
                was = now.get((kind, c["index"]))
                new_state = bool(c.get("mute"))
                if was is None or was == new_state:
                    continue
                (will_mute if new_state else will_pass).append(
                    c.get("name", f"{kind[:-1]} {c['index'] + 1}"))
        if not will_mute and not will_pass:
            return True

        lines = []
        if will_pass:
            lines.append("These are muted now and this config leaves them "
                         "passing, so they will start carrying signal:\n  "
                         + ", ".join(will_pass))
        if will_mute:
            lines.append("These will be muted:\n  " + ", ".join(will_mute))

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Loading will change the device")
        box.setText(
            f"{what}\n\n" + "\n\n".join(lines)
            + "\n\nMute is enforced on load rather than staged: it "
              "describes whether a driver is making sound, so it takes "
              "effect now and not at Apply. Everything else in this config "
              "waits for Apply as usual.\n\n"
              "Worth being sure this config suits the drivers that are "
              "connected -- or having the amplifiers down while you find "
              "out.")
        go = box.addButton("Continue", QMessageBox.AcceptRole)
        box.addButton("Cancel", QMessageBox.RejectRole)
        box.setDefaultButton(box.buttons()[-1])
        box.exec()
        return box.clickedButton() is go

    def _enforce_mutes(self, what: str) -> None:
        """Make the device's mutes match the ones just loaded, and say so.

        Mute reports whether a driver is making sound, so an icon showing
        it has to be true rather than pending -- see the policy note beside
        default_fir in minidsp_core. Loading a project or importing a
        preset therefore writes them straight away.

        This can start sound: a config that leaves a channel unmuted will
        unmute it. That is the point, and it is said out loud rather than
        hidden, because the alternative -- applying a configuration's
        routing while quietly dropping the mutes that made it safe -- is
        the more dangerous half.

        Nothing else in the payload goes: only the mute of every channel.
        """
        if self.daemon is None or self.project is None:
            return
        payload = {
            "outputs": [{"index": o["index"], "mute": bool(o.get("mute"))}
                        for o in self.project.get("outputs", [])],
            "inputs": [{"index": i["index"], "mute": bool(i.get("mute"))}
                       for i in self.project.get("inputs", [])],
        }
        muted = [c.get("name", "?")
                 for c in (self.project.get("inputs", [])
                           + self.project.get("outputs", []))
                 if c.get("mute")]
        try:
            self.daemon.set_config(payload)
        except Exception as exc:                           # noqa: BLE001
            self.statusBar().showMessage(
                f"{what}: the mute states could not be written ({exc}). "
                f"What is on screen may not be what the device is doing.",
                12000)
            return
        self.statusBar().showMessage(
            f"{what}. Mute enforced on the device: "
            + (f"{len(muted)} channel(s) muted -- {', '.join(muted)}"
               if muted else "every channel passing"), 12000)

    def _clear_fir_pending(self) -> None:
        """A filter that has been written is no longer waiting to be.

        Cleared on the project rather than on the payload, because the
        payload was a copy: leaving it set means every later Apply resends
        two thousand coefficients nobody asked for again.
        """
        for inp in (self.project or {}).get("inputs", []):
            fir = inp.get("fir")
            if fir and fir.get("taps"):
                fir["pending"] = False

    def _apply_done(self, _):
        self._set_writing(False)
        self._clear_fir_pending()
        self.dirty = False
        # Working memory now matches the project; the stored preset does not,
        # and saying so is the whole point of the second lamp.
        self.stored_current = False
        self.save_project()
        self.update_warning()
        self.statusBar().showMessage(
            "Applied. This is undone by a power cycle -- Save Edits to make "
            "it what the device loads.", 10000)

    def on_save_device(self):
        """Write the edits and store them, so they outlive a power cycle."""
        if not self._write_preflight():
            return
        native = self.daemon if hasattr(self.daemon,
                                        "save_stored_preset") else None
        if native is None:
            QMessageBox.warning(
                self, "Cannot store the preset",
                "Storing writes the device's flash directly, which needs the "
                "USB connection. This session is going through minidspd, so "
                "only Apply Edits is available.")
            return
        if not self._warned_store:
            resp = QMessageBox.warning(
                self, "Save to device?",
                "This stores the project in the device's own memory, "
                "replacing what it loads at power-on -- including a tuning "
                "made in Device Console.\n\n"
                "The stored blocks are read back and checked afterwards, but "
                "there is no undo, and a power cycle will no longer bring "
                "the old settings back. Turn your amplifiers off first.\n\n"
                "Asked once per session.",
                QMessageBox.Save | QMessageBox.Cancel, QMessageBox.Cancel)
            if resp != QMessageBox.Save:
                return
            self._warned_store = True

        self._set_writing(True, "Applying edits, then storing them "
                          "in the device's flash")
        self.statusBar().showMessage("Applying and storing edits...")
        project = copy.deepcopy(self.project)
        payload = core.build_config_payload(copy.deepcopy(self.project))

        def work():
            applied = core.apply_project(
                self.daemon, project, readback=self.readback,
                fir_progress=lambda d, t: self.read_progress.emit(d, t))
            # Store what had to be written, not what was asked for. The
            # device rounds a gain down when it applies one, and it does the
            # same when it loads a preset at power-on -- so storing the
            # target means the gain comes back a step below where it was
            # tuned. Storing the request means power-on reproduces it.
            core.store_gain_requests(payload, applied)
            return native.save_stored_preset(
                payload,
                progress=lambda d, t: self.read_progress.emit(d, t))

        self.tasks.run(work, on_done=self._save_device_done,
                       on_error=self._write_failed)

    def _save_device_done(self, stats):
        self.progress.hide()
        self._set_writing(False)
        self._clear_fir_pending()
        self.dirty = False
        self.stored_current = True
        self.save_project()
        self.update_warning()
        self.statusBar().showMessage(
            f"Saved to preset {stats['preset']}: {stats['parameters']} "
            f"parameters and {stats['bypass_flags']} bypass flags, read back "
            f"and verified. This is what the device now loads at power-on.",
            15000)

    def _write_failed(self, msg):
        self.progress.hide()
        self._set_writing(False)
        self.update_warning()
        self.statusBar().showMessage(f"Write failed: {msg}", 10000)
        QMessageBox.warning(self, "Write to device failed", msg)

    def _set_writing(self, busy: bool, what: str = "") -> None:
        """Mark a write in flight. Both go down the one command endpoint, so
        neither may start while the other is running."""
        self._writing = busy
        self._enable_writes(not busy)
        self.set_device_busy(busy, what)

    def set_device_busy(self, busy: bool, what: str = "") -> None:
        """Hold every other conversation while one is in progress.

        There is a single command endpoint. Anything sent while a read or a
        write is running interleaves with it, and some of it is worse than
        noise: the master strip sends immediately rather than waiting for
        Apply, so a preset change made during a save would move the device
        to a different slot part-way through storing to the one it was on.

        So the whole window stands down, not a chosen list of controls. A
        list is a thing to keep in step with every control added later, and
        the first one forgotten is a bug that only appears while the device
        is mid-write. The rule is simpler than the list: the program is
        either running or talking to the device, never both.

        All three timers stop, including the animation, which touches no
        hardware. Under a rule this blunt it should not be the exception,
        and a bar holding its last value reads as paused, which is what is
        happening.

        The costs, stated rather than discovered. MUTE ALL is unavailable
        for the ten seconds a save takes -- it sends a command like
        everything else. And if an operation ever hung, the window would
        stay locked; what makes that survivable is that the transport times
        out rather than blocking forever, and the title bar is outside the
        central widget, so the window can still be closed.
        """
        self.set_polling(not busy)
        if busy:
            self.meter_anim.stop()
        else:
            self.meter_anim.start(16)
        central = self.centralWidget()
        if central is not None:
            central.setEnabled(not busy)
        if busy:
            # A greyed window on its own says something is wrong at least as
            # readily as it says something is happening. This says which.
            if self._busy_dlg is None:
                self._busy_dlg = BusyDialog(self, what or "Working")
                self._busy_dlg.show()
        else:
            if self._busy_dlg is not None:
                self._busy_dlg.accept()
                self._busy_dlg.deleteLater()
                self._busy_dlg = None
            self._import_buttons_state()
            self.read_btn.setEnabled(self.readback is not None)

    def set_polling(self, on: bool) -> None:
        """Run the device pollers, or stand them down while it is busy.

        Both pollers ask the device from the UI thread and take the same lock
        a read or a write holds. While one of those runs -- ten seconds for a
        save -- the main thread was blocking on that lock every 60 ms,
        freezing the window and stacking timer events behind it, and there
        was never anything to sample: the device is busy, so those reads fail
        and are thrown away.

        The animation timer is deliberately left running. It touches no
        hardware, and stopping it would freeze the bars mid-decay rather than
        letting them fall.
        """
        if not on:
            self.poll.stop()
            self.meter_poll.stop()
            return
        if self.daemon is None:
            return
        self.poll.start(500)
        if hasattr(self.daemon, "meters"):
            self.meter_poll.start(60)

    def _enable_writes(self, enabled: bool) -> None:
        self.apply_btn.setEnabled(enabled)
        self.save_device_btn.setEnabled(enabled)

    def _last_dir(self, kind: str) -> Path:
        """Where a file dialog of this kind should open.

        The folder last used for that kind of file, or the sensible default
        for it: exports and flash images live in device/, projects wherever
        the current one does. Remembered per kind so that importing an export
        does not then send the project dialog off to the same place.
        """
        remembered = self._dirs.get(kind)
        if remembered and remembered.is_dir():
            return remembered
        return device_dir() if kind == "xml" else self.project_path.parent

    def _remember_dir(self, kind: str, path: str) -> None:
        parent = Path(path).expanduser().parent
        if parent.is_dir():
            self._dirs[kind] = parent

    def on_import_xml(self):
        """Load a Device Console preset export.

        Bypass has no readable parameter address, so this was once the only
        way to know which filters were really in circuit. Reading the stored
        preset now recovers the same thing from the device itself, and this
        remains for loading a tuning from a file -- one made on another
        machine, or for a device whose flash holds nothing recognisable.
        Either way it counts as having read the device.
        """
        if self.amap is None:
            QMessageBox.warning(
                self, "No address map",
                "Importing needs an address map for this device.\n"
                "Generate one with tools/gen_address_map.py.")
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Device Console preset", str(self._last_dir("xml")),
            "Device Console export (*.xml);;All files (*)")
        if not path:
            return
        self._remember_dir("xml", path)
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

        # Say what the file contains before replacing anything with it. A
        # stock export is a hundred flat bands, and importing one over a
        # tuning removes every filter in it -- which is a reasonable thing
        # to want and a terrible thing to do by accident. The two exports a
        # device ships with look identical in a file dialog.
        incoming = sum(1 for f in parsed["filters"].values()
                       if abs(float(f.get("gain") or 0.0)) > 1e-9)
        current = sum(1 for ch in (self.project["outputs"]
                                   + self.project["inputs"])
                      for b in ch.get("peq", [])
                      if b.get("enabled")
                      and abs(float(b.get("gain") or 0.0)) > 1e-9)
        if current and incoming < current:
            resp = QMessageBox.warning(
                self, "This import removes filters",
                f"{Path(path).name}\n\n"
                f"It carries {incoming} band(s) with any boost or cut. This "
                f"project currently has {current}.\n\n"
                + ("Importing it will flatten the equalisation entirely."
                   if incoming == 0 else
                   "Importing it will replace what is here with fewer "
                   "filters.")
                + "\n\nNothing reaches the device until you save.",
                QMessageBox.Ok | QMessageBox.Cancel, QMessageBox.Cancel)
            if resp != QMessageBox.Ok:
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
        # Imported from a file, so the project is nobody's preset now. Saying
        # it came from the one that happens to be active would be a lie, and
        # it is exactly the lie that makes a stock export look like a
        # cleared-out device.
        self._project_preset = None
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

    def _on_progress(self, done: int, total: int) -> None:
        if self._busy_dlg is not None:
            self._busy_dlg.set_progress(done, total)
        if total <= 0 or done >= total:
            self.progress.hide()
            return
        if not self.progress.isVisible():
            self.progress.show()
        self.progress.setMaximum(total)
        self.progress.setValue(done)

    def _n_presets(self) -> int:
        native = self._native()
        if native is not None:
            try:
                return max(1, len(native.preset_slots()))
            except Exception:                              # noqa: BLE001
                pass
        return 4

    def _native(self):
        """The direct-USB readback, or None when running over the daemon.

        Only the USB path can reach flash, so importing from another preset
        is offered only when there is one.
        """
        rb = self.readback
        return rb if rb is not None and hasattr(rb, "stored_config") else None

    def _import_buttons_state(self) -> None:
        """Label the channel button for what it will write onto."""
        chan, is_out = self.current_channel()
        kind = "output" if is_out else "input"
        self.import_chan_btn.setText(f"Import {kind}")
        self.import_chan_btn.setEnabled(chan is not None)
        self.import_chan_btn.setToolTip(
            f"Replace this {kind}'s settings with another {kind}'s.\n"
            f"Everything but its number and its "
            f"{'name' if is_out else 'name and routing'} is brought over.\n"
            f"Lands in this project; nothing is written to the device.")
        self.import_preset_btn.setEnabled(self._native() is not None)

    def _read_preset_async(self, index: int, then) -> None:
        """Read one stored preset off the device, then hand it to `then`."""
        native = self._native()
        if native is None:
            QMessageBox.warning(
                self, "No flash access",
                "Reading another preset needs the direct USB connection.")
            return
        self.set_device_busy(True, f"Reading preset {index + 1} out of "
                                   f"the device's flash")
        self.statusBar().showMessage(f"Reading preset {index + 1} from "
                                     f"device...")

        def work():
            return native.stored_config(
                index,
                preset=native.read_stored_preset(
                    index, progress=lambda d, t: self.read_progress.emit(d,
                                                                         t)))

        def done(cfg):
            self.progress.hide()
            self.set_device_busy(False)
            then(cfg)

        def failed(msg):
            self.progress.hide()
            self.set_device_busy(False)
            self.statusBar().showMessage(f"Could not read preset "
                                         f"{index + 1}: {msg}", 8000)

        self.tasks.run(work, on_done=done, on_error=failed)

    def _project_from_stored(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """That preset, as a project, so an import has one shape to read."""
        n_in = len(self.project["inputs"])
        n_out = len(self.project["outputs"])
        n_peq = len(self.project["outputs"][0]["peq"])
        tmp = core.new_project(n_in, n_out, n_peq, self.project["rate"])
        core.import_preset(tmp, cfg)
        return tmp

    def on_fir_load(self):
        """Read a coefficient file into the input on screen.

        Lands in the project like every other import; Apply or Save is what
        sends it. The block is not switched on by loading one -- a filter
        arriving is not the same event as deciding to hear it.
        """
        chan, is_out = self.current_channel()
        if chan is None or is_out:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, f"Load FIR taps into {chan.get('name', 'this input')}",
            str(self._last_dir("fir")),
            "Coefficients (*.txt *.dat *.bin *.dbl *.f32 *.flt);;"
            "All files (*)")
        if not path:
            return
        self._remember_dir("fir", path)
        name = Path(path).name
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            QMessageBox.warning(self, "Could not read that file", str(exc))
            return

        width = None
        while True:
            try:
                taps = core.parse_fir_taps(data, name, width=width)
                break
            except core.AmbiguousFirWidth as exc:
                width = self._ask_fir_width(exc, name)
                if width is None:
                    return
            except ValueError as exc:
                QMessageBox.warning(self, "Not a coefficient file", str(exc))
                return

        cap = self._fir_capacity()
        if cap and len(taps) > cap:
            QMessageBox.warning(
                self, "That filter is too long",
                f"{name} holds {len(taps)} coefficients and this input's "
                f"FIR block takes {cap}.\n\nThe device would keep the "
                f"first {cap} and drop the rest, which is a different "
                f"filter rather than a shorter one, so nothing was loaded.")
            return
        d = core.describe_fir_taps(taps)
        if not d["finite"]:
            QMessageBox.warning(
                self, "That filter has values that are not numbers",
                f"{name} contains an infinity or a NaN. A filter like that "
                f"does not attenuate anything, it propagates, and every "
                f"sample after it is lost. Nothing was loaded.")
            return

        self.editor.fir.set_taps(taps, name)
        self.on_edit()
        extra = ("" if d["peak"] <= 1.0 else
                 f"  Peak coefficient is {d['peak']:.4g}, above 1 -- which "
                 f"a convolution may legitimately be, but is also what a "
                 f"file scaled for another convention looks like.")
        self.statusBar().showMessage(
            f"Loaded {d['count']} taps from {name} into "
            f"{chan.get('name', 'this input')}. Not written to the device "
            f"yet.{extra}", 15000)

    def _fir_capacity(self) -> int | None:
        """What the device says the block holds, or None if it cannot say."""
        native = self._native()
        if native is None:
            return None
        chan, _is_out = self.current_channel()
        try:
            return native.fir_capacity(chan["index"])
        except Exception:                                  # noqa: BLE001
            return None

    def _ask_fir_width(self, exc, name: str) -> int | None:
        """Put the 32-or-64-bit question to the person holding the file."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Question)
        box.setWindowTitle("How wide are these coefficients?")
        box.setText(
            str(exc)
            + "\n\nminiDSP's manuals specify IEEE 754 single precision -- "
              "32-bit -- and that is what rePhase's miniDSP export writes. "
              "It is the answer nearly every time. rePhase will write "
              "64-bit if it is asked to, which is why this is a question "
              "rather than an assumption.")
        buttons = {}
        default = None
        for width, count in exc.options:
            label = f"{width * 8}-bit  ({count} taps)"
            if width == core.FIR_DOCUMENTED_WIDTH:
                label += "  - what miniDSP documents"
            b = box.addButton(label, QMessageBox.AcceptRole)
            buttons[b] = width
            if width == core.FIR_DOCUMENTED_WIDTH:
                default = b
        box.addButton("Cancel", QMessageBox.RejectRole)
        if default is not None:
            box.setDefaultButton(default)
        box.exec()
        return buttons.get(box.clickedButton())

    def on_import_channel(self):
        chan, is_out = self.current_channel()
        if chan is None:
            return
        kind = "output" if is_out else "input"
        here = chan["index"]
        pool = self.project["outputs" if is_out else "inputs"]
        names = [c["name"] for c in pool]
        n_presets = self._n_presets() if self._native() is not None else 1
        dlg = ImportDialog(self, kind, n_presets, self._project_preset,
                           names, here)
        if dlg.exec() != QDialog.Accepted:
            return
        preset, src_idx = dlg.choice()
        if src_idx is None:
            return

        def land(source_project):
            src = source_project["outputs" if is_out else "inputs"][src_idx]
            if bool(src.get("mute")) != bool(chan.get("mute")):
                one = {"outputs" if is_out else "inputs": [
                    {"index": chan["index"], "name": chan.get("name", "?"),
                     "mute": bool(src.get("mute"))}]}
                if not self._confirm_enforcement(
                        f"Importing {names[src_idx]} into "
                        f"{chan.get('name', 'this channel')}.", one):
                    self.statusBar().showMessage(
                        "Import cancelled; nothing changed.", 6000)
                    return
            stats = core.import_channel(chan, src, is_out)
            self.on_select(self.chan_list.currentRow())
            self.on_edit()
            where = ("" if preset == self._project_preset
                     else f" of preset {preset + 1}")
            self._enforce_mutes(
                f"Imported {names[src_idx]}{where} into {chan['name']} -- "
                f"{stats['peq']} PEQ bands"
                + (f", {stats['crossover']} crossover groups"
                   if stats["crossover"] else "")
                + ", nothing else written yet")

        if preset == self._project_preset or self._native() is None:
            land(self.project)
        else:
            self._read_preset_async(preset, lambda cfg: land(
                self._project_from_stored(cfg)))

    def on_import_preset(self):
        if self.project is None:
            return
        dlg = ImportDialog(self, "preset", self._n_presets(),
                           self._project_preset, [], None)
        if dlg.exec() != QDialog.Accepted:
            return
        preset, _ = dlg.choice()

        def land(cfg):
            # Built first, so the question can name the channels before
            # anything on screen or on the device has moved.
            preview = core.new_project(
                len(self.project["inputs"]), len(self.project["outputs"]),
                len(self.project["outputs"][0]["peq"]),
                self.project["rate"])
            core.import_preset(preview, cfg)
            if not self._confirm_enforcement(
                    f"Importing preset {preset + 1}.", preview):
                self.statusBar().showMessage(
                    "Import cancelled; nothing changed.", 6000)
                return
            stats = core.import_preset(self.project, cfg)
            self._project_preset = preset
            self.on_select(self.chan_list.currentRow())
            self.refresh_list()
            self.on_edit()
            self.update_warning()
            self._enforce_mutes(
                f"Imported preset {preset + 1}: {stats['outputs']} outputs, "
                f"{stats['inputs']} inputs, {stats['peq']} PEQ bands, "
                f"nothing else written yet")

        self._read_preset_async(preset, land)

    def on_save(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Save project", str(self._last_dir("project")),
            "JSON (*.json)")
        if path:
            self._remember_dir("project", path)
            self.project_path = Path(path)
            self.save_project()
            self.statusBar().showMessage(f"Saved {path}", 5000)

    def on_load(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Load project", str(self._last_dir("project")),
            "JSON (*.json)")
        if not path:
            return
        self._remember_dir("project", path)
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
        if not self._confirm_enforcement(
                f"Loading {Path(path).name}.", data):
            self.statusBar().showMessage("Load cancelled; nothing changed.",
                                         6000)
            return
        self.project = data
        self.project_path = Path(path)
        self.dirty = True
        self._enforce_mutes(f"Loaded {Path(path).name}")
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
    _install_crash_log()
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
    ap.add_argument("--timeout", type=int, default=2000,
                    help="USB command timeout in milliseconds; the device is "
                         "unhurried about some operations and a short one "
                         "reads a slow reply as a lost one")
    ap.add_argument("--rate", type=int, default=96000,
                    help="fallback DSP rate if no address map is available")
    ap.add_argument("--peq", type=int, default=10,
                    help="fallback PEQ band count")
    ap.add_argument("--project", default=str(default_project_path()),
                    help="working project file")
    opts = ap.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("LiniDi")
    app.setApplicationDisplayName("LiniDi")
    # Set on the application as well as the window: some window managers take
    # the task-bar entry's icon from here rather than from the window.
    app.setWindowIcon(app_icon())
    app.setStyleSheet(STYLE)
    win = MainWindow(opts)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
