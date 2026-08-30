#!/usr/bin/env python3
"""Drive the app the way a person does: real mouse and key events.

Not a test suite -- the thing tests are written against. Import it from a
script, build a MainWindow, and click at it.

Everything here goes through Qt's event queue, so a click reaches whatever
the widget actually connected -- which is the difference between testing
the handler and testing the app. Calling win.on_read() proves on_read
works; clicking Read Device proves the button is wired to it, is enabled,
and is not covered by something else.

The distinction has already cost this project twice. A confirmation
checkbox was connected to `toggled` rather than `clicked`, so it fired on
any programmatic change -- invisible to a test that called the handler.
And a reset test guarded its own call with hasattr(), passed for weeks,
and had never run.

Modal dialogs need the answer armed before they open, because opening one
blocks the thread that did it. See answer_next_dialog.

    driver = Driver(app)
    driver.click(win.read_btn)
    driver.wait_until(lambda: win._busy_dlg is None, 120, "the read")

License: Apache-2.0
"""
import sys
import time
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog,
                               QMessageBox, QRadioButton)


class Driver:
    def __init__(self, app):
        self.app = app
        self.answered = []

    # -- input ------------------------------------------------------------
    def click(self, widget, button=Qt.LeftButton):
        """A real press and release somewhere the widget will accept one.

        The centre is not always that place. QCheckBox and QRadioButton
        override hitButton to the indicator and its label, so a box laid
        out 248 pixels wide with the word "Enabled" in it ignores a click
        at x=124 -- which looks exactly like a broken signal connection and
        cost an hour of looking at the wrong end. Those get clicked near
        the left edge, where the indicator is.
        """
        if isinstance(widget, (QCheckBox, QRadioButton)):
            r = widget.rect()
            point = QPoint(min(10, max(2, r.width() // 4)),
                           r.height() // 2)
        else:
            point = widget.rect().center()
        QTest.mouseClick(widget, button, Qt.NoModifier, point)
        self.pump(0.05)

    def key(self, widget, key, text=""):
        QTest.keyClick(widget, key)
        self.pump(0.02)

    def type_into(self, widget, text):
        widget.selectAll() if hasattr(widget, "selectAll") else None
        QTest.keyClicks(widget, text)
        QTest.keyClick(widget, Qt.Key_Return)
        self.pump(0.05)

    def wheel(self, widget, notches):
        from PySide6.QtGui import QWheelEvent
        from PySide6.QtCore import QPointF
        pt = QPointF(widget.rect().center())
        ev = QWheelEvent(pt, widget.mapToGlobal(pt), QPoint(0, 0),
                         QPoint(0, 120 * notches), Qt.NoButton,
                         Qt.NoModifier, Qt.NoScrollPhase, False)
        self.app.sendEvent(widget, ev)
        self.pump(0.05)

    # -- time -------------------------------------------------------------
    def pump(self, secs=0.05):
        end = time.time() + secs
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def wait_until(self, cond, timeout=60.0, why=""):
        end = time.time() + timeout
        while time.time() < end:
            self.app.processEvents()
            if cond():
                return True
            time.sleep(0.02)
        raise TimeoutError(f"timed out waiting for {why or cond}")

    # -- dialogs ----------------------------------------------------------
    def answer_next_dialog(self, button_text, timeout=10.0):
        """Click a button by its label on whatever modal appears next.

        Modal dialogs block the thread that opened them, so the answer has
        to be armed beforehand and fired from a timer.
        """
        def fire():
            end = time.time() + timeout
            while time.time() < end:
                w = self.app.activeModalWidget()
                if w is not None:
                    for b in w.findChildren(type(QMessageBox().addButton(
                            "x", QMessageBox.AcceptRole))):
                        if button_text.lower() in b.text().lower().replace(
                                "&", ""):
                            self.answered.append((w.windowTitle(), b.text()))
                            QTest.mouseClick(b, Qt.LeftButton)
                            return
                    # a plain QDialog: accept or reject it
                    if isinstance(w, QDialog):
                        self.answered.append((w.windowTitle(), button_text))
                        (w.accept if button_text.lower() in
                         ("ok", "import", "continue", "yes")
                         else w.reject)()
                        return
                self.app.processEvents()
                time.sleep(0.02)
        QTimer.singleShot(0, fire)

    def dismiss_any_dialog(self):
        w = self.app.activeModalWidget()
        if w is not None:
            w.reject()
            self.pump(0.05)
            return True
        return False
