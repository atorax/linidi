#!/usr/bin/env python3
"""
linidi.core -- device-facing logic for the miniDSP tuning GUI.

Deliberately free of any GUI toolkit so it can be driven from a desktop app,
a script, or tests.

Three jobs:
  1. Filter design      (RBJ biquads, crossover alignments) and its inverse
  2. Device I/O         (the minidspd fallback; the direct USB path lives in
                         linidi.protocol and linidi.native)
  3. Project model      (local source of truth, snapshots, REW interchange)

Biquad sign convention
----------------------
miniDSP hardware uses the negated-feedback form

    y = b0*x + b1*x1 + b2*x2 + a1*y1 + a2*y2          <- plus signs

while the RBJ cookbook uses minus signs. Coefficients therefore have a1/a2
negated on the way out and un-negated on the way in. Verified against
minidsp-rs's own REW fixture, which asserts a *positive* a1 = 1.9973354, and
against live hardware where a low-pass read back with a1 = +1.7585379 and a
DC gain of exactly 1.0000.

License: Apache-2.0
"""

from __future__ import annotations

import copy
import inspect
import json
import math
import os
import re
import struct
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    import requests
except ImportError:                                    # pragma: no cover
    # Only the minidspd fallback needs it. The direct USB path -- which is
    # how the app normally runs -- must not require an HTTP library, and the
    # README promises exactly that. Daemon says so plainly if it is reached
    # without one.
    requests = None

# The maps ship inside the package, so they are found the same way whether
# this is a checkout or a frozen build: PyInstaller keeps the package
# structure when it unpacks.
from .protocol import ProtocolError, WRITE_CANCELLED

# Asked before a write that would leave a driver unfiltered. Installed by
# whatever can put the question to a person; with nothing installed the write
# is refused rather than assumed. It lives here so that both device layers
# reach the same one -- it used to exist only on the direct-USB path, so the
# minidspd fallback had no guard whatsoever.
_CONFIRM_DANGEROUS = None


def set_confirm_handler(fn) -> None:
    """Install something that can ask before a dangerous write."""
    global _CONFIRM_DANGEROUS
    _CONFIRM_DANGEROUS = fn


def channel_list(names: Sequence[str]) -> str:
    """"channel Out 3", or "channels Out 3 and Out 5", to sit in a sentence.

    The names are the labels the navigator shows, so a question names a
    channel the same way the window does. Nothing else is said about them:
    what an output is called is the only thing this program knows it by.

    Here rather than in the device layer because both guards ask the same
    question and only one of them can import that module. They used to build
    the sentence separately and disagreed about the last comma.
    """
    names = list(names)
    if len(names) == 1:
        return f"channel {names[0]}"
    return f"channels {', '.join(names[:-1])} and {names[-1]}"


def unfiltered_question(names: Sequence[str], caveat: str = "") -> str:
    """The body of the unfiltered-output question, for either write path.

    One wording, in one place. It says what the configuration will do and
    names the channels it will do it to, and it does not guess what is
    plugged into them -- this program has no way to know, and a check that
    guessed would be wrong in ways nobody can foresee.
    """
    body = (f"<b>UNFILTERED OUTPUT</b><br><br>"
            f"Operate active {channel_list(names)} unfiltered?")
    return f"{body}<br><br>{caveat}" if caveat else body


def ask_dangerous(title: str, detail: str, verb: str = "Continue") -> bool:
    """Put a question to whoever is driving, and return their answer.

    `verb` names the accept button after the action being confirmed -- Apply,
    rather than a generic Continue -- so the button says what pressing it
    does.
    """
    if _CONFIRM_DANGEROUS is None:
        raise ProtocolError(
            f"{title}\n\n{detail}\n\nNothing here can ask whether that "
            f"is intended, so it was not written. A caller that means it "
            f"can say so with linidi.core.set_confirm_handler().")
    return bool(_CONFIRM_DANGEROUS(title, detail, verb))

# The highest gain the app offers, on every kind of gain alike -- channel
# and mixer cell both. The correction loop has to be able to reach it, or a
# boosted cell gets corrected downwards to 0.
GAIN_MAX_DB = 12.0

HERE = Path(__file__).resolve().parent
ADDRESS_MAPS = HERE / "address_maps"


def config_dir() -> Path:
    """Where the program keeps its own files, by the platform's convention.

    Everything the program writes for itself -- the working project, the flash
    block map, the crash log -- lives here together, under the program's own
    name. Stated once because it was previously spelled out at each of those
    three call sites, which is how they drifted apart in the first place.

    A dot-directory in the home folder is a Unix habit; Windows keeps per-user
    application data under APPDATA. The Unix branch is written literally rather
    than through XDG_CONFIG_HOME: ~/.config is what XDG resolves to when that
    variable is unset, and honouring it would silently move the files of anyone
    who had set it for other reasons.
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        roaming = Path(base) if base else (Path.home() / "AppData" / "Roaming")
        return roaming / "LiniDi"
    return Path.home() / ".config" / "linidi"

# The five coefficients, in the order they go on the wire and are written
# everywhere else. Stated once so that dicts built in different places
# serialise identically -- a saved project should not differ between two runs
# because one of them happened to iterate a set.
COEFF_KEYS = ("b0", "b1", "b2", "a1", "a2")

BYPASS = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}

# Biquad slots in one crossover group. A property of the device format, so it
# is stated once rather than appearing as a 4 in every place that walks a
# group.
XOVER_SLOTS = 4

# What a crossover group is called on screen. Here rather than in the window
# because the warnings that name a group are raised down here, and a message
# that says "crossover 1" about the card labelled A is a message somebody has
# to translate.
XOVER_LABELS = ("A", "B")


def xover_label(index: int) -> str:
    if 0 <= index < len(XOVER_LABELS):
        return XOVER_LABELS[index]
    return str(index + 1)

PEQ_TYPES = ("peaking", "lowshelf", "highshelf", "lowpass", "highpass",
             "notch", "allpass", "bandpass")

ALIGNMENTS = ("linkwitz-riley", "butterworth", "bessel")

# Which orders each alignment offers, limited by the four biquad slots a
# crossover group has. Linkwitz-Riley is even orders only, being two cascaded
# Butterworths of half the order. Stated once because two things need it: the
# slope menu, and the plot's wheel, which steps through the same list.
CROSSOVER_ORDERS = {
    "linkwitz-riley": (2, 4, 6, 8),
    "butterworth": (1, 2, 3, 4, 5, 6, 7, 8),
    "bessel": (2, 3, 4, 5, 6, 7, 8),
}


def crossover_orders(alignment: str) -> tuple[int, ...]:
    """The orders this alignment can be built at, lowest first."""
    return CROSSOVER_ORDERS.get(alignment, ())

# Bessel sections as (Q, frequency ratio), normalised so the cascade is -3 dB
# at the corner. Unlike Butterworth and Linkwitz-Riley, whose sections all sit
# at the same frequency, a Bessel's are spread.
#
# Derived from the reverse Bessel polynomial: its roots give the poles, each
# conjugate pair becomes a section of Q = |p| / 2|Re p| at w0 = |p|, and the
# whole set is scaled so the cascade is -3 dB at 1.
#
# The ratios have been wrong twice, the same way both times: Q values right,
# ratios wrong, and the error invisible at the corner because every order is
# -3.01 dB there whatever the spread. The numbers below are checked against
# the analog Bessel response across 62 Hz to 16 kHz, not at the corner --
# the previous set measured 23.5 dB adrift at 8th order and 12.8 at 6th.
# Note that the ratios *increase* with section Q; a set that decreases is the
# old defect returning.
BESSEL_SECTIONS: dict[int, list[tuple[float, float]]] = {
    2: [(0.5774, 1.2720)],
    3: [(0.6910, 1.4476)],
    4: [(0.5219, 1.4302), (0.8055, 1.6034)],
    5: [(0.5635, 1.5563), (0.9165, 1.7554)],
    6: [(0.5103, 1.6039), (0.6112, 1.6892), (1.0233, 1.9047)],
    7: [(0.5324, 1.7164), (0.6608, 1.8224), (1.1263, 2.0495)],
    8: [(0.5060, 1.7785), (0.5596, 1.8321),
        (0.7109, 1.9532), (1.2257, 2.1887)],
}

# An odd order has one real pole as well, which becomes a 1st-order section at
# this ratio.
BESSEL_FIRST: dict[int, float] = {3: 1.3227, 5: 1.5023, 7: 1.6844}

BESSEL_Q = {order: [q for q, _ in secs]
            for order, secs in BESSEL_SECTIONS.items()}


# ---------------------------------------------------------------------------
# Filter design
# ---------------------------------------------------------------------------

def _emit(b0, b1, b2, a0, a1, a2) -> dict[str, float]:
    """Normalise by a0, then negate feedback terms for miniDSP convention."""
    if a0 == 0:
        raise ValueError("degenerate filter (a0 == 0)")
    return {"b0": b0 / a0, "b1": b1 / a0, "b2": b2 / a0,
            "a1": -(a1 / a0), "a2": -(a2 / a0)}


def design_biquad(kind: str, freq: float, q: float, gain_db: float,
                  rate: int) -> dict[str, float]:
    """One RBJ biquad, in miniDSP convention."""
    if not 0 < freq < rate / 2:
        raise ValueError(f"frequency {freq} out of range for rate {rate}")
    if q <= 0:
        raise ValueError("Q must be positive")

    w0 = 2.0 * math.pi * freq / rate
    cos_w0, sin_w0 = math.cos(w0), math.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    if kind in ("peaking", "lowshelf", "highshelf"):
        A = 10.0 ** (gain_db / 40.0)
        if kind == "peaking":
            return _emit(1 + alpha * A, -2 * cos_w0, 1 - alpha * A,
                         1 + alpha / A, -2 * cos_w0, 1 - alpha / A)
        beta = 2.0 * math.sqrt(A) * alpha
        if kind == "lowshelf":
            return _emit(
                A * ((A + 1) - (A - 1) * cos_w0 + beta),
                2 * A * ((A - 1) - (A + 1) * cos_w0),
                A * ((A + 1) - (A - 1) * cos_w0 - beta),
                (A + 1) + (A - 1) * cos_w0 + beta,
                -2 * ((A - 1) + (A + 1) * cos_w0),
                (A + 1) + (A - 1) * cos_w0 - beta)
        return _emit(
            A * ((A + 1) + (A - 1) * cos_w0 + beta),
            -2 * A * ((A - 1) + (A + 1) * cos_w0),
            A * ((A + 1) + (A - 1) * cos_w0 - beta),
            (A + 1) - (A - 1) * cos_w0 + beta,
            2 * ((A - 1) - (A + 1) * cos_w0),
            (A + 1) - (A - 1) * cos_w0 - beta)

    a = (1 + alpha, -2 * cos_w0, 1 - alpha)
    if kind == "lowpass":
        return _emit((1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2, *a)
    if kind == "highpass":
        return _emit((1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2, *a)
    if kind == "notch":
        return _emit(1, -2 * cos_w0, 1, *a)
    if kind == "allpass":
        return _emit(1 - alpha, -2 * cos_w0, 1 + alpha, *a)
    if kind == "bandpass":
        return _emit(alpha, 0, -alpha, *a)
    raise ValueError(f"unknown filter type: {kind}")


def _first_order(kind: str, freq: float, rate: int) -> dict[str, float]:
    k = math.tan(math.pi * freq / rate)
    if kind == "lowpass":
        return _emit(k, k, 0.0, k + 1.0, k - 1.0, 0.0)
    return _emit(1.0, -1.0, 0.0, k + 1.0, k - 1.0, 0.0)


def butterworth_qs(order: int) -> tuple[list[float], bool]:
    """Section Qs for a Butterworth of `order`, and whether one pole is real.

    The poles sit on a circle, and each conjugate pair becomes a section whose
    Q is 1 / (2 cos t), t being the pair's angle from the negative real axis.
    Where those angles fall depends on the parity of the order: an odd order
    puts one pole *on* the real axis and shifts the pairs around it.

    That parity term was missing, so odd orders came out with the angles of an
    even one. A 3rd-order Butterworth was built from Q = 0.577 instead of
    Q = 1.0, and measured -7.78 dB at its own corner where -3 dB is the
    definition. Even orders were unaffected and are unchanged.
    """
    if order < 1:
        raise ValueError("order must be >= 1")
    odd = order % 2
    qs = [1.0 / (2.0 * math.cos((2 * k - 1 + odd) * math.pi / (2 * order)))
          for k in range(1, order // 2 + 1)]
    return qs, bool(odd)


def design_crossover(mode: str, alignment: str, order: int, freq: float,
                     rate: int,
                     max_biquads: int = XOVER_SLOTS,
                     ) -> list[dict[str, float]]:
    """Crossover as a biquad cascade.

    Linkwitz-Riley order N is two cascaded Butterworths of order N/2, which is
    why LR24 is two Q=0.7071 sections and not the Butterworth-4 pair.
    """
    if mode not in ("highpass", "lowpass"):
        raise ValueError("mode must be highpass or lowpass")
    # Checked here rather than left to design_biquad. Three configurations
    # never reach it -- Bessel prewarps and clamps, and Butterworth 1 and
    # Linkwitz-Riley 2 are built entirely from _first_order, which validates
    # nothing -- so a corner of zero produced a flat cascade or an unstable
    # one and handed it back as a crossover.
    if not 0.0 < float(freq) < rate / 2.0:
        raise ValueError(
            f"a crossover corner of {freq} Hz is outside what a {rate} Hz "
            f"device can filter")

    out: list[dict[str, float]] = []
    if alignment == "linkwitz-riley":
        if order % 2:
            raise ValueError("Linkwitz-Riley order must be even")
        qs, first = butterworth_qs(order // 2)
        for _ in range(2):
            out += [design_biquad(mode, freq, q, 0.0, rate) for q in qs]
            if first:
                out.append(_first_order(mode, freq, rate))
    elif alignment == "butterworth":
        qs, first = butterworth_qs(order)
        out += [design_biquad(mode, freq, q, 0.0, rate) for q in qs]
        if first:
            out.append(_first_order(mode, freq, rate))
    elif alignment == "bessel":
        if order not in BESSEL_SECTIONS:
            raise ValueError("bessel supports orders 2-8")
        out += [design_biquad(mode,
                              bessel_section_freq(freq, ratio, mode, rate),
                              q, 0.0, rate)
                for q, ratio in BESSEL_SECTIONS[order]]
        if order in BESSEL_FIRST:
            out.append(_first_order(
                mode,
                bessel_section_freq(freq, BESSEL_FIRST[order], mode, rate),
                rate))
    else:
        raise ValueError(f"unknown alignment: {alignment}")

    if len(out) > max_biquads:
        raise ValueError(
            f"{alignment} order {order} needs {len(out)} biquads but only "
            f"{max_biquads} slots exist per crossover group")
    return out


def _prewarp(freq: float, rate: int) -> float:
    """A digital frequency as the analogue one the bilinear transform maps it
    from. Clamped below Nyquist, where the tangent runs away."""
    f = max(1e-6, min(float(freq), rate * 0.499999))
    return 2.0 * rate * math.tan(math.pi * f / rate)


def _unwarp(analogue: float, rate: int) -> float:
    """The inverse of _prewarp."""
    return rate / math.pi * math.atan(max(0.0, analogue) / (2.0 * rate))


def bessel_section_corner(f0: float, ratio: float, mode: str,
                          rate: int) -> float:
    """The cascade corner a Bessel section at `f0` was spread from.

    The exact inverse of bessel_section_freq, and it has to stay that way:
    one puts the sections where they go and the other reads them back.
    """
    a = _prewarp(f0, rate)
    a = a * ratio if mode == "highpass" else a / ratio
    return _unwarp(a, rate)


def bessel_section_freq(corner: float, ratio: float, mode: str,
                        rate: int) -> float:
    """Where one Bessel section sits, relative to the cascade's corner.

    The low-pass prototype places each section above the corner by its ratio;
    the high-pass transformation inverts that, so the sections sit below it.

    The ratios come from the analogue prototype, so they are applied in the
    analogue domain and mapped back. Applied to the digital frequency
    directly, the bilinear transform's warping moved the cascade's own
    corner as the sections approached Nyquist: a Bessel 8 low-pass asked for
    at 10 kHz on a 48 kHz device measured -1.28 dB at 10 kHz where every
    other alignment reads -3.01, and a Bessel 3 at 16 kHz read -0.44.
    Butterworth and Linkwitz-Riley never showed it because all of their
    sections sit at the corner itself, which RBJ already prewarps.
    """
    a = _prewarp(corner, rate)
    a = a / ratio if mode == "highpass" else a * ratio
    return _unwarp(a, rate)


def response_db(biquads: Iterable[dict[str, float]], freqs: Iterable[float],
                rate: int) -> list[float]:
    """Magnitude response of a cascade, in dB.

    Input is in miniDSP convention, so feedback terms are un-negated here.
    """
    bqs = list(biquads)
    out = []
    for f in freqs:
        w = 2.0 * math.pi * f / rate
        z1 = complex(math.cos(-w), math.sin(-w))
        z2 = z1 * z1
        mag = 1.0
        for bq in bqs:
            den = 1.0 - bq["a1"] * z1 - bq["a2"] * z2
            if abs(den) < 1e-20:
                # A vanishing denominator is a pole on the unit circle: a
                # section running away, not a silent one. This answered 0.0
                # and drew a runaway resonance as silence.
                mag = math.inf
                break
            mag *= abs((bq["b0"] + bq["b1"] * z1 + bq["b2"] * z2) / den)
        # 1e-6 is -120 dB. The threshold was 1e-12, which is -240, so
        # everything between the two came back verbatim and the stated floor
        # only applied below it.
        if mag == math.inf:
            out.append(RUNAWAY_DB)
        else:
            out.append(20.0 * math.log10(mag) if mag > 1e-6 else -120.0)
    return out


def response_phase(biquads: Iterable[dict[str, float]], freqs: Iterable[float],
                   rate: int, delay_ms: float = 0.0,
                   invert: bool = False) -> list[float]:
    """Phase of a cascade, in degrees, wrapped to (-180, 180].

    Delay and polarity are part of it, not extras: a pure delay of t seconds
    contributes -360*f*t degrees and is exactly what the output delay control
    is for, and an inverted output is 180 degrees away from one that is not.
    Leaving either out would draw a phase response the hardware does not have.
    """
    bqs = list(biquads)
    secs = delay_ms / 1000.0
    out = []
    for f in freqs:
        w = 2.0 * math.pi * f / rate
        z1 = complex(math.cos(-w), math.sin(-w))
        z2 = z1 * z1
        acc = complex(1.0, 0.0)
        for bq in bqs:
            den = 1.0 - bq["a1"] * z1 - bq["a2"] * z2
            if abs(den) < 1e-20:
                acc = 0j
                break
            acc *= (bq["b0"] + bq["b1"] * z1 + bq["b2"] * z2) / den
        deg = math.degrees(math.atan2(acc.imag, acc.real)) if acc != 0 else 0.0
        deg -= 360.0 * f * secs
        if invert:
            deg += 180.0
        out.append((deg + 180.0) % 360.0 - 180.0)
    return out


def log_freqs(n: int = 240, lo: float = 20.0,
              hi: float = 20000.0) -> list[float]:
    """n points from lo to hi, evenly spaced by ratio."""
    if n < 2:
        # One point has no spacing to divide by, and no caller wants a
        # division by zero out of a frequency axis.
        return [lo] * max(n, 0)
    return [lo * (hi / lo) ** (i / (n - 1)) for i in range(n)]


# ---------------------------------------------------------------------------
# Filter identification (the inverse problem)
# ---------------------------------------------------------------------------

def is_bypass(bq: dict[str, float], tol: float = 1e-9) -> bool:
    return (abs(bq["b0"] - 1.0) < tol
            and all(abs(bq[k]) < tol for k in ("b1", "b2", "a1", "a2")))


# How far a crossover group has to pull its own response down before it
# counts as filtering anything. An all-pass and a peaking filter at 0 dB are
# both built from coefficients nowhere near unity and both pass the whole
# band, so "the numbers are not 1, 0, 0, 0, 0" was the wrong question for a
# check about outputs left unfiltered.
FILTERING_MIN_DB = 3.0


def _cascade_span_db(bqs: Iterable[dict[str, float]]) -> float:
    """How far a cascade's magnitude travels across the whole band, in dB.

    In normalised frequency, so no sample rate is needed: a biquad's shape
    against a fraction of Nyquist is the same whatever the rate, and this
    only asks how far the response moves.
    """
    lo = hi = None
    for i in range(96):
        w = math.pi * 1e-4 * (1e4 ** (i / 95.0))
        z1 = complex(math.cos(-w), math.sin(-w))
        z2 = z1 * z1
        mag = 1.0
        for bq in bqs:
            try:
                b0, b1, b2, a1, a2 = (float(bq[k]) for k in COEFF_KEYS)
            except (KeyError, TypeError, ValueError):
                continue
            den = 1.0 - a1 * z1 - a2 * z2
            if abs(den) < 1e-20:
                return math.inf
            mag *= abs((b0 + b1 * z1 + b2 * z2) / den)
        db = 20.0 * math.log10(mag) if mag > 1e-12 else -240.0
        lo = db if lo is None else min(lo, db)
        hi = db if hi is None else max(hi, db)
    return 0.0 if lo is None else hi - lo


def group_filters(group: dict[str, Any]) -> bool:
    """Whether a payload's crossover group would filter anything.

    Two things have to hold, and each was once missing.

    The group must not be bypassed. A group whose filter has been switched
    off keeps its sections and moves only that flag, so judging it on
    coefficients alone calls a bypassed crossover a filter -- exactly
    backwards for a guard whose job is to notice an output about to run full
    range.

    And its response has to go somewhere. An all-pass at 2600 Hz, or a
    peaking filter at 0 dB, is flat to a thousandth of a dB across the band
    and made entirely of coefficients that are not unity, so the old test
    counted both as protection.

    Lives here rather than in the device layer because both guards need it
    and only one of them can import that module.
    """
    if group.get("bypass"):
        return False
    return _cascade_span_db(group.get("coeff") or []) >= FILTERING_MIN_DB


def is_first_order(bq: dict[str, float], tol: float = 1e-9) -> bool:
    """A 1st-order section stored as a biquad: second-order terms are zero.

    Odd-order Butterworths and Linkwitz-Riley 12 dB/oct (two cascaded
    1st-order sections) produce these, so they must be recognised or the
    filters read back as unidentifiable.
    """
    return abs(bq.get("b2", 0.0)) < tol and abs(bq.get("a2", 0.0)) < tol


def biquad_is_stable(bq: dict[str, float]) -> bool:
    """Whether the section's poles sit inside the unit circle.

    miniDSP adds the feedback terms rather than subtracting them, so the
    denominator is 1 - a1*z^-1 - a2*z^-2 and Jury's conditions come out as
    below. Worth checking before anything is written: an unstable section does
    not merely sound wrong, it runs away, and on an active crossover the
    output of that reaches a driver directly.
    """
    a1, a2 = float(bq["a1"]), float(bq["a2"])
    return abs(a2) < 1.0 and abs(a1) < 1.0 - a2


def classify_biquad(bq: dict[str, float]) -> str:
    """Rough shape of a section, from the relationships between its terms.

    The numerator alone cannot separate a notch from a high-pass: both have
    b2 == b0, and a notch's b1 = -2cos(w0)*b0 approaches -2*b0 as its centre
    frequency falls, which is exactly the high-pass form. What distinguishes
    the peaking family is that its numerator mirrors the feedback term,
    b1 == -a1, so that is tested first.

    An earlier version tried to catch notches with a test that was a strict
    superset of the high-pass test above it, so it could only ever fire on
    shapes that were not notches, and real notches fell through to "other".

    One degeneracy is real rather than a shortcoming here: as a notch's centre
    frequency falls, its coefficients converge on a high-pass's, the two
    differing by a term in (1 - cos w0) that vanishes. A notch placed very low
    will read as a high-pass. Pass filters are tested first deliberately,
    since decode_peq branches on that answer and a crossover section read as
    something else would be decoded down the wrong path.
    """
    b0, b1, b2 = bq["b0"], bq["b1"], bq["b2"]
    if is_bypass(bq):
        return "bypass"
    if abs(b0) < 1e-12:
        return "unknown"
    rel = abs(b0) * 1e-3

    if is_first_order(bq):
        # 1st-order low-pass has b1 == b0; high-pass has b1 == -b0.
        if abs(b1 - b0) < rel:
            return "lowpass"
        if abs(b1 + b0) < rel:
            return "highpass"
        return "other"

    if abs(b2 - b0) < rel:
        # A symmetric numerator is a pass filter or a notch, and they are
        # told apart by b1 alone.
        if abs(b1 - 2 * b0) < rel:
            return "lowpass"
        if abs(b1 + 2 * b0) < rel:
            return "highpass"
        return "notch"

    if abs(b1 + bq["a1"]) < max(rel, abs(bq["a1"]) * 1e-3):
        # Numerator mirrors the feedback term: the peaking family, which
        # includes the shelves.
        return "peaking"
    return "other"


def decode_biquad(bq: dict[str, float],
                  rate: int) -> "tuple[float, float | None] | None":
    """Recover (f0, Q) from a 2nd-order section. Inverse of the RBJ design.

    From the miniDSP-convention feedback terms:
        alpha    = (1 + a2) / (1 - a2)
        cos(w0)  = a1 * (1 + alpha) / 2
        Q        = sin(w0) / (2 * alpha)
    """
    a1, a2 = bq["a1"], bq["a2"]

    if is_first_order(bq):
        # 1st-order: a1 = (1 - k) / (1 + k) with k = tan(pi * f0 / rate),
        # so k = (1 - a1) / (1 + a1). Q is undefined for a single pole.
        if abs(1.0 + a1) < 1e-12:
            return None
        k = (1.0 - a1) / (1.0 + a1)
        if k <= 0:
            return None
        return rate * math.atan(k) / math.pi, None

    if abs(1.0 - a2) < 1e-12:
        return None
    alpha = (1.0 + a2) / (1.0 - a2)
    if alpha <= 0:
        return None
    c = a1 * (1.0 + alpha) / 2.0
    if not -1.0 < c < 1.0:
        return None
    w0 = math.acos(c)
    if w0 <= 0:
        return None
    return rate * w0 / (2.0 * math.pi), math.sin(w0) / (2.0 * alpha)


def identify_alignment(sections: list[tuple[float, float]]) -> tuple[str, int]:
    """Name the alignment behind a set of (f0, Q) sections.

    Returns (alignment, order). Falls back to ("custom", 2*len(sections)).
    """
    if not sections:
        return "custom", 0

    # A section with q of None is 1st-order and contributes one pole, not two.
    qs = sorted(q for _, q in sections if q is not None)
    n_first = sum(1 for _, q in sections if q is None)
    order = 2 * len(qs) + n_first

    def matches(a: list[float], b: list[float]) -> bool:
        return len(a) == len(b) and all(abs(x - y) < 0.02
                                        for x, y in zip(a, sorted(b)))

    if n_first and not qs:
        # Purely 1st-order. One alone is a 6 dB/oct Butterworth and two
        # cascaded at one corner is LR12. Three or four are neither: a
        # Butterworth of order 3 has one real pole and one complex pair, not
        # three real poles, and naming them that way rebuilt a filter 9 dB
        # shallower at the corner than the one the device was running.
        if n_first == 1:
            return "butterworth", 1
        if n_first == 2:
            return "linkwitz-riley", 2
        return "custom", n_first

    if n_first == 2 and order % 4 == 2:
        # A Linkwitz-Riley whose half-order is odd: each half is a Butterworth
        # with one real pole, so the pair contributes two 1st-order sections
        # and each of its Qs twice. LR36 is the case that matters, and it was
        # falling through to "custom" because the branch below only considered
        # halves with no real pole.
        half_qs, half_first = butterworth_qs(order // 2)
        if half_first and matches(qs, half_qs + half_qs):
            return "linkwitz-riley", order

    if not n_first:
        # Linkwitz-Riley of order N is Butterworth(N/2) cascaded twice, so
        # every Q appears exactly twice. That makes LR24 a pair of 0.7071
        # sections but LR48 a pair of 0.5412 *and* a pair of 1.3066 -- so
        # matching on "all 0.7071" only ever recognised LR24.
        if order % 4 == 0:
            half_qs, half_first = butterworth_qs(order // 2)
            if not half_first and matches(qs, half_qs + half_qs):
                return "linkwitz-riley", order

        bw, bw_first = butterworth_qs(order)
        if not bw_first and matches(qs, bw):
            return "butterworth", order

        if order in BESSEL_Q and matches(qs, BESSEL_Q[order]):
            return "bessel", order
    else:
        # Mixed: an odd-order Butterworth or Bessel is 1st-order plus biquads.
        bw, bw_first = butterworth_qs(order)
        if bw_first and n_first == 1 and matches(qs, bw):
            return "butterworth", order
        if (n_first == 1 and order in BESSEL_FIRST
                and matches(qs, BESSEL_Q[order])):
            return "bessel", order

    return "custom", order


# ---------------------------------------------------------------------------
# REW interchange
# ---------------------------------------------------------------------------

_REW_COEF = re.compile(r"^\s*([ab][012])\s*=\s*(-?[\d.eE+-]+)\s*,?\s*$")


# Where a comment starts in a coefficient file. rePhase writes none,
# Acourate and DRC-FIR write headers, and hand-edited files pick up all of
# these, so all of them are honoured.
_TAP_COMMENT = re.compile(r"[#;*]|//")
# Anything that separates two numbers on one line: whitespace or comma.
# Some exporters put the whole filter on a single line. A semicolon is not
# here: _TAP_COMMENT claims it first, so listing it as a separator was dead
# and the manual described behaviour the parser did not have.
_TAP_SPLIT = re.compile(r"[\s,]+")


def parse_fir_taps(data: bytes, name: str = "",
                   width: int | None = None) -> list[float]:
    """Read a FIR coefficient file, in whichever shape it arrived.

    There is no standard here. rePhase writes one decimal per line, Acourate
    and DRC-FIR write either text or raw little-endian floats, REW writes
    text, and anything hand-made picks up comments and blank lines. So the
    shape is worked out from the bytes rather than demanded of the user.

    Binary is detected by content rather than by extension -- a file naming
    itself .txt while holding raw floats is a real thing, and so is the
    reverse. See _looks_binary.

    Binary width comes from the strongest evidence available: an explicit
    `width`, then the extension, then a length that divides evenly for only
    one of them. Often none of those settle it, since every length suiting
    64-bit also suits 32-bit, and then it raises AmbiguousFirWidth carrying
    both readings for the caller to ask about. It never guesses from the
    numbers; see _parse_fir_binary.

    A caller loading into miniDSP hardware knows something this function does
    not: the manuals require IEEE 754 single precision, and rePhase's miniDSP
    export is 32-bit. FIR_DOCUMENTED_WIDTH is the right default there -- a
    default rather than an assumption, since rePhase writes 64-bit when asked.
    """
    if _looks_binary(data):
        return _parse_fir_binary(data, name, width)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("latin-1")
        except Exception as exc:                       # noqa: BLE001
            raise ValueError(
                f"{name or 'that file'} is neither text nor a whole number "
                f"of binary floats ({exc})") from exc

    # A byte-order mark is stripped by the codec when it leads the file,
    # and is just a character anywhere else -- which is where it lands in a
    # file that has been concatenated, re-saved, or exported twice. It
    # means nothing in a list of numbers wherever it appears.
    text = text.replace("\ufeff", "")

    taps: list[float] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _TAP_COMMENT.split(raw, 1)[0].strip()
        if not line:
            continue
        started = len(taps)
        for piece in _TAP_SPLIT.split(line):
            if not piece:
                continue
            try:
                taps.append(float(piece))
            except ValueError:
                # A header line of words is ordinary and is skipped. A bad
                # token *after* numbers on the same line is not: breaking
                # there abandons the rest of the line and silently shortens
                # the filter, which is the failure this is here to prevent.
                if any(c.isdigit() for c in piece) or len(taps) > started:
                    raise ValueError(
                        f"{name or 'that file'} line {lineno}: "
                        f"{piece!r} is not a number") from None
                break
    if not taps:
        raise ValueError(
            f"{name or 'that file'} holds no coefficients")
    return taps


def _looks_binary(data: bytes) -> bool:
    """Whether these bytes are raw floats rather than a list of numbers.

    A NUL settles it, but relying on one is not enough: a file of
    coefficients that all happen to encode without a zero byte contains
    none. Three thousand copies of 0.1 as float32 is CD CC CC 3D repeated,
    and that file was read as text and reported as holding no coefficients
    -- which is true of the text in it, and useless.

    So it counts bytes that cannot appear in a file of decimal numbers:
    control characters and anything above plain ASCII. A text export is
    entirely printable; raw floats are mostly not.
    """
    sample = data[:4096]
    # A byte-order mark is three bytes above ASCII, which in a short text
    # file is a fifth of the sample and enough to call it binary on its
    # own. It is the one high-byte sequence that means "this is text", and
    # it turns up more than once in files that have been concatenated or
    # saved twice -- so all of them go, not just a leading one, matching
    # what the text path does with them further down.
    sample = sample.replace(b"\xef\xbb\xbf", b"")
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    odd = sum(1 for b in sample
              if b < 9 or 13 < b < 32 or b > 126)
    return odd > len(sample) // 20


# What miniDSP's own manuals specify for a coefficient file, across the
# Flex, Flex Eight and 2x4 HD: IEEE 754 single precision. Four bytes.
FIR_DOCUMENTED_WIDTH = 4

# Extensions that name their own width. Anything else is worked out.
_FIR_WIDTH_BY_EXT = {".dbl": 8, ".f64": 8, ".double": 8,
                     ".f32": 4, ".flt": 4, ".float": 4}


class AmbiguousFirWidth(ValueError):
    """A binary file that reads as either width, with nothing to choose by.

    Carries both readings so a caller can put the question to whoever has
    the file, which is the only place the answer actually exists.
    """

    def __init__(self, message: str, options: list[tuple[int, int]]):
        super().__init__(message)
        self.options = options


def _parse_fir_binary(data: bytes, name: str = "",
                      width: int | None = None) -> list[float]:
    """Raw little-endian floats, at whichever width the evidence supports."""
    def read(w: int) -> list[float]:
        code = "f" if w == 4 else "d"
        return list(struct.unpack(f"<{len(data) // w}{code}", data))

    fits = [w for w in (4, 8) if len(data) % w == 0 and len(data) >= w]
    if not fits:
        raise ValueError(
            f"{name or 'that file'} is {len(data)} bytes, which is not a "
            f"whole number of 32-bit or 64-bit floats")

    if width is not None:
        if width not in fits:
            raise ValueError(
                f"{name or 'that file'} is {len(data)} bytes, which is not "
                f"a whole number of {width * 8}-bit floats")
        return read(width)

    ext = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    hinted = _FIR_WIDTH_BY_EXT.get(ext)
    if hinted in fits:
        return read(hinted)

    if len(fits) == 1:
        return read(fits[0])

    # No guessing from here. A first attempt scored the two readings on
    # whether the numbers looked like a filter, and a perfectly ordinary
    # 2048-tap 32-bit file read as 1024 64-bit values that were finite,
    # smooth and small -- it passed. Tightening the thresholds until that
    # one case failed would have been fitting the rule to the example.
    #
    # Nothing in the bytes says which width they are. The person holding
    # the file knows, so the question goes to them.
    raise AmbiguousFirWidth(
        f"{name or 'That file'} is {len(data)} bytes, which is "
        f"{len(data) // 4} coefficients at 32-bit or {len(data) // 8} at "
        f"64-bit. Nothing in the file says which, and read at the wrong "
        f"width it comes out as a filter rather than as an error.",
        [(4, len(data) // 4), (8, len(data) // 8)])


# Fewest taps a FIR block will hold. miniDSP's manual puts each input block
# at between 6 and 2048, and the floor is why an untouched block reads six
# rather than nothing: this hardware has no way to hold no filter at all.
FIR_MIN_TAPS = 6


def fir_passthrough() -> list[float]:
    """The shortest filter this hardware can hold that does nothing.

    Convolving a signal with a unit impulse returns the signal unchanged,
    so the block passes audio through untouched. At the minimum length, this is what an input that has
    never been loaded reads back -- and so it is also what clearing one means
    here, there being no way to write an absence.
    """
    return [1.0] + [0.0] * (FIR_MIN_TAPS - 1)


def fir_is_empty(taps: Sequence[float]) -> bool:
    """Whether a block holds nothing worth calling a filter.

    Either no coefficients at all, or the passthrough above. Reporting the
    latter as "6 taps, peak 1, 1 non-zero" is true and tells you nothing you
    wanted to know: it reads as a filter somebody loaded, on a block nobody
    has touched.

    Judged on the coefficients rather than on where they came from, because
    a block that has been deliberately cleared and one that was never used
    hold the same thing, and the window has no business claiming to know
    which. A filter somebody genuinely designed to be a unit impulse is a
    passthrough too, and saying so is not wrong.
    """
    if not taps:
        return True
    if len(taps) > FIR_MIN_TAPS:
        return False
    return taps[0] == 1.0 and not any(t != 0.0 for t in taps[1:])


def describe_fir_taps(taps: Sequence[float]) -> dict[str, Any]:
    """What a loaded filter looks like, for saying so before it is written.

    Peak magnitude is the one worth showing. A filter whose taps exceed 1
    can still be correct -- it is a convolution, not a gain -- but it is
    also what a file scaled for a different convention looks like, and the
    difference matters before it reaches a driver rather than after.
    """
    peak = max((abs(v) for v in taps), default=0.0)
    return {"count": len(taps), "peak": peak,
            "sum": sum(taps),
            "nonzero": sum(1 for v in taps if v != 0.0),
            "finite": all(math.isfinite(v) for v in taps)}


def parse_rew_biquads(text: str) -> list[dict[str, float]]:
    """Parse REW's miniDSP biquad export.

    REW already writes miniDSP's sign convention, so values pass through
    untouched -- the same thing minidsp-rs's rew.rs does.
    """
    out: list[dict[str, float]] = []
    cur: dict[str, float] = {}

    def flush() -> None:
        # Some exports carry an explicit a0=1 line. Requiring exactly five
        # entries silently dropped every one of those biquads; require the
        # five that matter and ignore anything else on the way past.
        if all(k in cur for k in COEFF_KEYS):
            out.append({k: cur[k] for k in COEFF_KEYS})

    for line in text.splitlines():
        if line.strip().lower().startswith("biquad"):
            flush()
            cur = {}
            continue
        m = _REW_COEF.match(line)
        if m:
            cur[m.group(1)] = float(m.group(2))
    flush()
    return out


# ---------------------------------------------------------------------------
# Address maps
# ---------------------------------------------------------------------------

# Display names, since a map name like "flex8" is not what is on the box.
# Kept here rather than in the device layer because AddressMap.load needs it:
# minidspd reports the name on the box, and it has to find the file.
PRODUCT_NAMES: dict[str, str] = {
    "flex8": "Flex 8", "flex": "Flex", "flexdl": "Flex DL",
    "flexhtx": "Flex HTx", "m2x4hd": "2x4 HD", "ddrc24": "DDRC-24",
    "ddrc88bm": "DDRC-88BM", "shd": "SHD", "c8x12v2": "C-DSP 8x12",
    "m10x10hd": "10x10 HD", "m4x10hd": "4x10 HD", "msharc4x8": "miniSHARC 4x8",
    "nanodigi2x8": "nanoDIGI 2x8", "m2x4": "2x4",
}


def product_name(map_name: str) -> str:
    return PRODUCT_NAMES.get(map_name, map_name)


def _squash(name: str) -> str:
    """A name reduced to what is comparable: lower case, letters and digits."""
    return re.sub(r"[^a-z0-9]", "", name.lower())


class AddressMap:
    """Per-device float addresses, generated by tools/gen_address_map.py."""

    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        self.device = doc.get("device", "unknown")
        self.rate = int(doc.get("internal_sampling_rate", 96000))
        self.inputs = doc.get("inputs", [])
        self.outputs = doc.get("outputs", [])
        clash = self.conflicts()
        if clash:
            addr, claims = clash[0]
            raise ValueError(
                f"address map '{self.device}' gives {len(clash)} DSP "
                f"address(es) two meanings -- {addr} is both "
                f"{' and '.join(claims)}. The app would write one over the "
                f"other and read one as the other, so the map has to be "
                f"corrected against real hardware before it can be used.")

    def routes(self, index: int) -> int:
        """How many mixer cells input `index` really has addresses for.

        Not the output count. Two of the shipped maps stop short of it -- a
        C-DSP 8x12 lists eight cells for twelve outputs -- and building a
        project to the output count meant the window offered cells with no
        address behind them, which every write dropped without a word.
        """
        if not 0 <= index < len(self.inputs):
            return 0
        spec = self.inputs[index]
        return max(len(spec.get("routing", [])),
                   len(spec.get("routing_status", [])))

    def route_gates(self, index: int) -> bool:
        """Whether this input's mixer cells have an on/off address at all.

        Three of the shipped maps have none. Their cells pass or not by gain
        alone, so an on/off switch on screen would be a control that cannot
        reach the device.
        """
        if not 0 <= index < len(self.inputs):
            return False
        return bool(self.inputs[index].get("routing_status"))

    def conflicts(self) -> list[tuple[int, list[str]]]:
        """Addresses this map gives more than one meaning, worst first.

        A generated map can put a channel's own gain on the same address as
        its first mixer cell's, which is not a subtlety: the two are separate
        controls in this app, so whichever is written second wins and neither
        reads back as itself.
        """
        owner: dict[int, list[str]] = {}

        def claim(addr: Any, what: str) -> None:
            if isinstance(addr, int):
                owner.setdefault(addr, []).append(what)

        for kind, specs in (("in", self.inputs), ("out", self.outputs)):
            for i, spec in enumerate(specs):
                where = f"{kind} {i + 1}"
                for key in ("gain", "delay", "invert", "enable", "meter"):
                    claim(spec.get(key), f"{where} {key}")
                for n, a in enumerate(spec.get("peq", [])):
                    for k in range(len(COEFF_KEYS)):
                        claim(a + k, f"{where} PEQ {n + 1}")
                for n, a in enumerate(spec.get("xover_groups", [])):
                    for k in range(XOVER_SLOTS * len(COEFF_KEYS)):
                        claim(a + k, f"{where} crossover {n + 1}")
                for key, label in (("routing", "cell gain"),
                                   ("routing_status", "cell gate"),
                                   ("routing_polarity", "cell polarity")):
                    for n, a in enumerate(spec.get(key, [])):
                        claim(a, f"{where} {label} to out {n + 1}")
                for key, a in (spec.get("compressor") or {}).items():
                    claim(a, f"{where} compressor {key}")
                for key, a in (spec.get("fir") or {}).items():
                    if key != "coeffs":
                        claim(a, f"{where} FIR {key}")
        return [(a, w) for a, w in sorted(owner.items()) if len(w) > 1]

    @classmethod
    def load(cls, device: str) -> "AddressMap | None":
        """Find a map by file name, or by the name on the box.

        Called with whatever names the device: the USB path passes a map name
        and matches directly, but minidspd reports a product name, and those
        do not match their file names -- "Flex 8" is flex8.json and "2x4 HD"
        is m2x4hd.json. Matching only the literal string meant the daemon
        fallback found no map for most devices, which quietly disabled reading
        from the hardware rather than reporting anything.
        """
        for name in cls._candidates(device):
            path = ADDRESS_MAPS / f"{name}.json"
            if path.is_file():
                return cls(json.loads(path.read_text(encoding="utf-8")))
        return None

    @staticmethod
    def check_all() -> dict[str, list[tuple[int, list[str]]]]:
        """Every shipped map that gives an address two meanings.

        Here so the check can be run over the whole set without opening a
        device -- see tools/check_maps.py.
        """
        bad = {}
        for name in AddressMap.available():
            doc = json.loads((ADDRESS_MAPS / f"{name}.json").read_text(
                encoding="utf-8"))
            probe = AddressMap.__new__(AddressMap)
            probe.doc = doc
            probe.device = doc.get("device", name)
            probe.inputs = doc.get("inputs", [])
            probe.outputs = doc.get("outputs", [])
            clash = probe.conflicts()
            if clash:
                bad[name] = clash
        return bad

    @staticmethod
    def _candidates(device: str) -> list[str]:
        wanted = _squash(device)
        names = [device.strip().lower(), wanted]
        names += [key for key, shown in PRODUCT_NAMES.items()
                  if _squash(shown) == wanted or _squash(key) == wanted]
        seen, out = set(), []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                out.append(n)
        return out

    @staticmethod
    def available() -> list[str]:
        if not ADDRESS_MAPS.is_dir():
            return []
        return sorted(p.stem for p in ADDRESS_MAPS.glob("*.json"))


# ---------------------------------------------------------------------------
# Describing what was read
# ---------------------------------------------------------------------------
#
# Both transports -- direct USB and the minidspd fallback -- turn raw floats
# into the same band and group descriptions, so the code that does it lives
# here rather than once in each. They had drifted: the daemon path recovered
# only frequency and Q, the USB path the full type and gain as well, so the
# same device described its own filters differently depending on how it was
# reached.

# No miniDSP offers anything near ten seconds of delay, so a sample count past
# this is a misread rather than a very long delay. Reporting zero beats
# reporting nonsense the UI would then draw.
MAX_DELAY_SAMPLES = 1_000_000


def delay_ms_from_raw(raw: float, rate: int) -> float:
    """Delay is a sample count living in the float's bit pattern."""
    try:
        samples = struct.unpack("<I", struct.pack("<f", raw))[0]
    except (struct.error, OverflowError):
        return 0.0
    return 0.0 if samples > MAX_DELAY_SAMPLES else samples * 1000.0 / rate


def as_biquad(vals: list[float]) -> dict[str, float]:
    """Five consecutive floats as a section, or a passthrough if short."""
    if len(vals) < len(COEFF_KEYS):
        return dict(BYPASS)
    return dict(zip(COEFF_KEYS, vals))


def describe_peq_band(bq: dict[str, float], slot: int,
                      rate: int) -> dict[str, Any]:
    """One PEQ slot as the project model wants it.

    The decoded type, frequency, Q and gain are added only when the section
    really is one the app can express that way; decode_peq checks its own
    answer, so anything else stays as coefficients.
    """
    kind = classify_biquad(bq)
    entry: dict[str, Any] = {"index": slot, "coeff": bq, "shape": kind,
                             "active": kind not in ("bypass", "unknown")}
    # A passthrough is not decoded. It genuinely satisfies the response check
    # as a 0 dB peaking filter -- flat is flat -- so every empty slot came
    # back named "peaking, 24000 Hz, Q 0.5" and a read rewrote every unused
    # band's design with it. An unnameable shape is still offered to
    # decode_peq: that is what it is for.
    decoded = None if kind == "bypass" else decode_peq(bq, rate)
    if decoded:
        entry["type"], entry["freq"], entry["q"], entry["gain"] = decoded
    return entry


def describe_crossover_group(bqs: list[dict[str, float]], index: int,
                             rate: int) -> dict[str, Any]:
    """One crossover group: which sections are real, and what they make."""
    sections, shapes = [], []
    # Sections that are neither a pass filter nor a passthrough. A group can
    # only be named by (mode, alignment, order, freq) when there is nothing
    # else in it, because rebuilding from those four numbers throws the other
    # slots away. An LR4 pair with a -12 dB cut beside it read back as a
    # plain LR4, and the rebuild put those 12 dB back into the driver.
    strays = 0
    for bq in bqs:
        kind = classify_biquad(bq)
        decoded = (decode_biquad(bq, rate)
                   if kind in ("lowpass", "highpass") else None)
        if decoded:
            sections.append(decoded)
            shapes.append(kind)
        elif not is_bypass(bq):
            strays += 1
    entry: dict[str, Any] = {"index": index, "coeff": bqs,
                             "sections": len(sections),
                             "active": bool(sections)}
    if sections:
        # Naming an alignment means the caller will rebuild the group from
        # (mode, alignment, order, freq) and throw the coefficients away. That
        # is only safe if the sections really are one filter. Two checks, both
        # of which used to be missing:
        #
        # Every section must be the same mode. A high pass at 100 Hz and a low
        # pass at 8 kHz in one group -- an ordinary midrange band -- was read
        # as "linkwitz-riley 4 at 4050 Hz", and rebuilding that dropped the
        # high pass entirely: +28 dB at 20 Hz into a driver that had been band
        # limited. Picking the commoner mode also made the answer depend on
        # set iteration order, so the same input could read either way.
        #
        # And every section must agree on a corner. Two high passes at 40 Hz
        # and 4 kHz are both high passes, and averaged to a filter at neither.
        alignment, order = identify_alignment(sections)
        if not strays and len(set(shapes)) == 1:
            mode = shapes[0]
            freq = crossover_corner(sections, alignment, order, mode, rate)
            if _sections_share_corner(sections, alignment, order, mode,
                                      freq, rate):
                entry["mode"] = mode
                entry["alignment"] = alignment
                entry["order"] = order
                entry["freq"] = round(freq, 1)
        entry.setdefault("alignment", "custom")
        entry["qs"] = [None if q is None else round(q, 4) for _, q in sections]
    return entry


def _sections_share_corner(sections: list[tuple[float, float]],
                           alignment: str, order: int, mode: str,
                           corner: float, rate: int,
                           tol: float = 0.05) -> bool:
    """Whether every section really belongs to one filter at `corner`.

    Each section is put back where the alignment says it came from -- the
    same un-spreading crossover_corner does -- and the results have to agree
    within a few per cent. Sections that disagree are not a crossover this app
    can name, whatever their Q values happen to match.
    """
    ratios = BESSEL_SECTIONS.get(order) if alignment == "bessel" else None
    first = BESSEL_FIRST.get(order) if alignment == "bessel" else None
    implied = []
    for f0, q in sections:
        if q is None:
            # An odd-order Bessel's real pole is spread from the corner the
            # same way its biquads are, so it has to be un-spread the same
            # way. Taken raw it landed 32% out and no Bessel 3, 5 or 7 could
            # ever name itself -- every one of them fell to "custom".
            implied.append(bessel_section_corner(f0, first, mode, rate)
                           if first else f0)
            continue
        if ratios:
            _, ratio = min(ratios, key=lambda rq: abs(rq[0] - q))
            implied.append(bessel_section_corner(f0, ratio, mode, rate))
        else:
            implied.append(f0)
    if not implied or corner <= 0:
        return False
    return all(abs(f - corner) / corner <= tol for f in implied)


def crossover_corner(sections: list[tuple[float, float]], alignment: str,
                     order: int, mode: str, rate: int) -> float:
    """The corner a set of sections was designed around.

    For Butterworth and Linkwitz-Riley every section sits at the corner, so
    the average is the corner. A Bessel's sections are spread by a fixed ratio
    each, so averaging them lands well above it -- an order 4 designed at
    1000 Hz averages 1505 -- and reading a crossover back would have moved it.
    Each section is put back where it came from first.
    """
    if not sections:
        # No section, no corner. Every caller checks first, but a divisor
        # that can be zero is not something to leave to callers.
        return 0.0
    ratios = BESSEL_SECTIONS.get(order) if alignment == "bessel" else None
    if not ratios:
        return sum(f for f, _ in sections) / len(sections)

    corners = []
    for f0, q in sections:
        if q is None:
            continue
        # Pair each section with the ratio belonging to its Q.
        _, ratio = min(ratios, key=lambda rq: abs(rq[0] - q))
        corners.append(bessel_section_corner(f0, ratio, mode, rate))
    return sum(corners) / len(corners) if corners else sections[0][0]


# ---------------------------------------------------------------------------
# Device I/O
# ---------------------------------------------------------------------------

class DeviceError(RuntimeError):
    pass


@dataclass
class Daemon:
    """Writes and live status go through minidspd's REST API.

    Uses a persistent session: meter polling is dominated by round-trip
    latency (~6.7 ms median on loopback), so avoiding a fresh TCP connection
    per poll matters once the poll rate goes up.
    """

    base: str = "http://127.0.0.1:5380"
    index: int = 0
    timeout: float = 5.0
    # How many inputs this model has, once the window has loaded a map for
    # the name minidspd reported. The driver guard needs it to prove an
    # output unfed: without it, a payload naming one input was allowed to
    # speak for every input that exists. None means not known yet, and the
    # guard then proves nothing.
    n_inputs: int | None = None
    _local: Any = None

    def _refuse_unfiltered(self, payload: dict[str, Any]) -> None:
        """Ask before a write that would leave a fed output with no filter.

        The same question the USB path asks, answered the same way: does this
        write leave a fed, unmuted output with no crossover filtering on it at
        all. Both are answered from the payload alone, and neither infers
        anything about what is connected.

        What it does *not* catch is a two-group output losing one of its
        groups -- wipe the highpass and the surviving lowpass still travels
        the whole band, so the output reads as filtered and nothing is asked,
        which is measured rather than assumed. On this hardware that case is
        what destroys a tweeter, and no check here sees it: catching it needs
        a before-state to compare against, and the window shows what the
        configuration will be rather than warning about every step towards it.
        The channel list is what carries that, continuously.

        One difference from the USB path remains, and it is in the routing
        rather than the filtering: there the map says how many inputs exist,
        so an output can be proved unfed. Here that comes from n_inputs, and
        without it nothing is provable and the question gets asked.

        It errs towards asking: more often than necessary, never silently.
        """
        # Fed unless this payload says otherwise, the same rule the USB
        # guard uses. Taking "the payload mentioned routing at all" as having
        # spoken for every output left a hole: a payload routing input 1 says
        # nothing about a driver fed by input 2. A routing entry naming no
        # destination says nothing about anything -- and subscripting it
        # raised KeyError out of a guard, which a caller's except turns into
        # a question that never got asked.
        fed_by, cut = set(), {}
        for inp in payload.get("inputs", []):
            src = inp.get("index")
            for r in inp.get("routing", []):
                dest = r.get("index")
                if "enabled" not in r or not isinstance(dest, int):
                    continue
                if r["enabled"]:
                    fed_by.add(dest)
                else:
                    cut.setdefault(dest, set()).add(src)

        def unfed(dest: Any) -> bool:
            # Every input this model has must cut it, which needs to know how
            # many there are. Measuring against the inputs the payload
            # happens to name is not the same test and reads as the same one:
            # a payload that cuts input 1 and says nothing about input 2 had
            # its output counted as unfed, and the guard stood down over a
            # driver input 2 was still feeding. Unknown count proves nothing,
            # so the output stays fed and the question gets asked.
            if dest in fed_by or self.n_inputs is None:
                return False
            every = set(range(self.n_inputs))
            return bool(every) and cut.get(dest, set()) >= every

        at_risk = []
        for out in payload.get("outputs", []):
            if unfed(out.get("index")) or out.get("mute"):
                continue
            groups = out.get("crossover")
            if groups is None:            # not being changed by this write
                continue
            # The live groups as one cascade, because that is what reaches
            # the driver. This is not what rescues a wiped highpass, and it
            # was once claimed to be: the surviving lowpass travels the whole
            # band, so the union reads as filtered exactly as asking each
            # group in turn did -- measured, both ways round. The docstring
            # says why nothing available here can catch that case.
            cascade = []
            for g in groups:
                if g.get("bypass"):
                    continue
                cascade += [dict(b) for b in (g.get("coeff") or [])]
            if _cascade_span_db(cascade) >= FILTERING_MIN_DB:
                continue
            at_risk.append(out.get("name") or f"Out {out.get('index', 0) + 1}")
        if at_risk and not ask_dangerous(
                "Unfiltered output",
                unfiltered_question(
                    at_risk,
                    "Over minidspd the device's own coefficients cannot be "
                    "read, so this is what the write says rather than what "
                    "the hardware is doing."),
                verb="Apply"):
            raise ProtocolError(WRITE_CANCELLED)

    def __post_init__(self) -> None:
        if requests is None:
            raise RuntimeError(
                "the minidspd fallback needs the 'requests' package, which is "
                "not installed. The direct USB path does not; if you meant to "
                "use that, the udev rule is what is missing.")
        # Built here rather than lazily in session(): two threads arriving at
        # once would each have seen it missing and made their own, and one of
        # the two would then have been discarded along with its connections.
        self._local = threading.local()

    def session(self) -> requests.Session:
        # requests.Session is not thread-safe, and this object is shared by
        # the meter poller thread and background task threads, so each gets
        # its own session rather than sharing one connection pool.
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = requests.Session()
            self._local.session = sess
        return sess

    def _url(self, suffix: str = "") -> str:
        return f"{self.base.rstrip('/')}/devices/{self.index}{suffix}"

    def _get(self, url: str):
        try:
            r = self.session().get(url, timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise DeviceError(
                f"minidspd unreachable at {self.base}: {exc}") from exc

    def _post(self, url: str, payload: dict):
        try:
            r = self.session().post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise DeviceError(f"write failed: {exc}") from exc

    def devices(self) -> list[dict]:
        return self._get(f"{self.base.rstrip('/')}/devices")

    def status(self) -> dict:
        return self._get(self._url())

    def set_master(self, **fields) -> None:
        self._post(self._url(), fields)

    def set_config(self, payload: dict) -> None:
        self._refuse_unfiltered(payload)
        self._post(self._url("/config"), _without_names(payload))


class Readback:
    """Reads live coefficients off the hardware.

    minidspd's REST API is write-only for DSP config -- `GET /devices/N`
    returns master status and meters, nothing else. The underlying protocol
    *can* read (`ReadFloats`, opcode 0x14), and the `minidsp` CLI exposes it
    as `debug dump-float`, so that is what we drive.

    Two traps this class exists to hide:
      * the CLI parses address arguments as HEX
      * `dump_floats` prints only non-zero values, so absent addresses are 0.0
        and silence means "all zeros", not "failed"
    """

    def __init__(self, amap: AddressMap, cli: str = "minidsp",
                 tcp: str = "127.0.0.1:5333", timeout: float = 40.0):
        self.amap = amap
        self.cli = cli
        self.tcp = tcp
        self.timeout = timeout

    def _run(self, start: int, count: int) -> dict[int, float]:
        end = start + count
        try:
            proc = subprocess.run(
                [self.cli, "--tcp", self.tcp, "debug", "dump-float",
                 f"{start:X}", f"{end:X}"],
                capture_output=True, text=True, timeout=self.timeout)
        except FileNotFoundError:
            raise DeviceError(f"'{self.cli}' not found on PATH") from None
        except subprocess.TimeoutExpired:
            raise DeviceError("device read timed out") from None
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip().splitlines()
            raise DeviceError(f"read failed: {err[-1] if err else '?'}")

        vals: dict[int, float] = {}
        for line in proc.stdout.splitlines():
            m = re.match(r"^\s*([0-9a-fA-F]{1,4}):\s*(-?[\d.eE+-]+)\s*$", line)
            if m:
                vals[int(m.group(1), 16)] = float(m.group(2))
        return vals

    def floats(self, start: int, count: int) -> list[float]:
        vals = self._run(start, count)
        return [vals.get(start + i, 0.0) for i in range(count)]

    @staticmethod
    def _biquads(block: list[float]) -> list[dict[str, float]]:
        out = []
        for i in range(len(block) // 5):
            b0, b1, b2, a1, a2 = block[i * 5:(i + 1) * 5]
            out.append({"b0": b0, "b1": b1, "b2": b2, "a1": a1, "a2": a2})
        return out

    def read_output(self, index: int) -> dict[str, Any]:
        spec = self.amap.outputs[index]
        rate = self.amap.rate
        out: dict[str, Any] = {"index": index}

        if "gain" in spec:
            out["gain"] = round(self.floats(spec["gain"], 1)[0], 3)
        if "delay" in spec:
            out["delay"] = round(delay_ms_from_raw(
                self.floats(spec["delay"], 1)[0], rate), 4)

        peq_addrs = spec.get("peq", [])
        peq = []
        if peq_addrs:
            lo, hi = min(peq_addrs), max(peq_addrs)
            block = self.floats(lo, hi - lo + 5)
            for slot, addr in enumerate(peq_addrs):
                off = addr - lo
                coeff = self._biquads(block[off:off + 5])
                peq.append(describe_peq_band(
                    coeff[0] if coeff else dict(BYPASS), slot, rate))
        out["peq"] = peq

        groups = []
        for gi, base in enumerate(spec.get("xover_groups", [])):
            block = self.floats(base, 20)
            bqs = self._biquads(block)
            groups.append(describe_crossover_group(bqs, gi, rate))
        out["crossover"] = groups
        return out

    def read_input(self, index: int) -> dict[str, Any]:
        """Gain and PEQ for one input.

        Inputs carry the voicing in a common house style -- flatten at the
        input, keep the outputs as pure crossover -- so reading only outputs
        misses everything that shapes the response.
        """
        spec = self.amap.inputs[index]
        rate = self.amap.rate
        out: dict[str, Any] = {"index": index}
        if "gain" in spec:
            out["gain"] = round(self.floats(spec["gain"], 1)[0], 3)

        peq_addrs = spec.get("peq", [])
        peq = []
        if peq_addrs:
            lo, hi = min(peq_addrs), max(peq_addrs)
            block = self.floats(lo, hi - lo + 5)
            for slot, addr in enumerate(peq_addrs):
                off = addr - lo
                coeff = self._biquads(block[off:off + 5])
                peq.append(describe_peq_band(
                    coeff[0] if coeff else dict(BYPASS), slot, rate))
        out["peq"] = peq
        return out

    def read_all(self, n_outputs: int | None = None) -> list[dict[str, Any]]:
        n = n_outputs if n_outputs is not None else len(self.amap.outputs)
        total = len(self.amap.outputs)
        return [self.read_output(i) for i in range(min(n, total))]

    def read_inputs(self, n_inputs: int | None = None) -> list[dict[str, Any]]:
        n = n_inputs if n_inputs is not None else len(self.amap.inputs)
        total = len(self.amap.inputs)
        return [self.read_input(i) for i in range(min(n, total))]


# ---------------------------------------------------------------------------
# Project model -> coefficients
# ---------------------------------------------------------------------------

# The ISO 1/1-octave centre frequencies. Ten of them span 31.5 Hz to 16 kHz,
# which is the layout of every ten-band equaliser ever built, so the numbers
# are ones people already recognise.
ISO_OCTAVE_CENTRES = (16.0, 31.5, 63.0, 125.0, 250.0, 500.0, 1000.0,
                      2000.0, 4000.0, 8000.0, 16000.0)


def stock_peq_freqs(count: int) -> list[float]:
    """Evenly spaced starting points for a bank of `count` bands.

    Spread evenly in log frequency between 31.5 Hz and 16 kHz, then snapped
    to an ISO centre wherever one is within a few percent. For the usual ten
    that lands exactly on 31.5 / 63 / 125 ... 16k; for any other count it
    degrades to plain log spacing rather than to a table that does not fit.

    All ten bands defaulting to 1 kHz was the alternative, and it meant
    switching a second band on stacked it invisibly on the first.
    """
    if count <= 0:
        return []
    if count == 1:
        return [1000.0]
    lo, hi = 31.5, 16000.0
    ratio = (hi / lo) ** (1.0 / (count - 1))
    out = []
    for k in range(count):
        f = lo * ratio ** k
        near = min(ISO_OCTAVE_CENTRES, key=lambda c: abs(math.log(c / f)))
        out.append(near if abs(math.log(near / f)) < 0.03 else round(f, 1))
    return out


def stock_peq_q(count: int) -> float:
    """A Q that makes `count` bands tile the range they are spread over.

    A peaking filter is N octaves wide at Q = sqrt(2**N) / (2**N - 1), so
    octave spacing wants Q 1.41 rather than the 1.0 this used to default to,
    which is nearer an octave and a half and overlaps its neighbours.
    """
    if count < 2:
        return 1.41
    ratio = (16000.0 / 31.5) ** (1.0 / (count - 1))
    n = math.log2(ratio)
    return round(math.sqrt(2 ** n) / (2 ** n - 1), 3)


def default_peq_band(index: int, count: int = 10) -> dict[str, Any]:
    freqs = stock_peq_freqs(count)
    return {"index": index, "enabled": False, "type": "peaking",
            "freq": freqs[index] if index < len(freqs) else 1000.0,
            "q": stock_peq_q(count), "gain": 0.0, "manual": None,
            "bypass_source": "default"}


def reset_peq_band(band: dict[str, Any], count: int = 10) -> None:
    """Put one band back to its starting point, in place.

    Left switched off, which is what the stock band is. A reset that also
    put the band into circuit would be switching a filter on by itself, and
    on an active crossover nothing should do that except somebody deciding
    to. It costs one click to enable, and the band contributes nothing
    either way until it is.
    """
    stock = default_peq_band(band.get("index", 0), count)
    band.update(stock)
    # These values are nobody's but this app's now. Leaving read_state as it
    # was left the provenance column saying "from device" for a band whose
    # numbers had just been thrown away and replaced with stock ones.
    band["read_state"] = "config"
    # Not "default": somebody chose this, and the difference matters to the
    # warning about filters whose state is unknown.
    band["bypass_source"] = "user"


def default_crossover_group(index: int, mode: str) -> dict[str, Any]:
    return {"index": index, "enabled": False, "mode": mode,
            "alignment": "linkwitz-riley", "order": 4, "freq": 80.0,
            "manual": None, "bypass_source": "default"}


# One compressor's settings. The values are the ones a Flex 8 ships with,
# read out of its own flash rather than invented.
#
# Five of the six do not read back, so a project is the only record of what
# was asked for -- the device will take the write and then decline to say
# what it holds. That is also why `enabled` defaults to False: a compressor
# sits in front of a driver, and nothing should put one into circuit except
# somebody deciding to.
def default_compressor() -> dict[str, Any]:
    return {"enabled": False, "threshold": -30.0, "makeup": 0.0,
            "ratio": 4.0, "knee": 20.0, "attack": 40.0, "release": 100.0,
            "bypass_source": "default"}


def default_output(index: int, n_peq: int,
                   n_groups: int = 2) -> dict[str, Any]:
    """A fresh output, with as many crossover groups as the device has.

    The count used to be two, which is right for every shipped map but the
    Flex HTx, whose outputs have four. Groups 3 and 4 were invisible in the
    window and never written -- the thing the rule below this forbids. They
    alternate high, low, high, low: a pair of them is a band.
    """
    return {"index": index, "name": f"Out {index + 1}", "gain": 0.0,
            "mute": False, "invert": False, "delay": 0.0,
            "peq": [default_peq_band(i, n_peq) for i in range(n_peq)],
            "crossover": [
                default_crossover_group(
                    i, "highpass" if i % 2 == 0 else "lowpass")
                for i in range(n_groups)],
            "compressor": default_compressor()}


# The app offers whatever the device supports.
#
# Not a slogan: a rule with a failure mode behind it. Mixer-cell polarity
# was in the address map for weeks, read out of a Device Console export by
# tools/extend_map_from_export.py, and referenced by nothing. The hardware
# keeps sixteen of them, they read back, they persist, and the app had no
# way to set one. An address map that describes more than the app offers is
# a list of things quietly withheld from whoever owns the device.
#
# So when a capability is found, it gets built or it gets written down as a
# decision not to. See "Deliberately not exposed" in the README for those.


# Design is staged; state is enforced.
#
# Most of what this app edits is a design -- a gain, a filter, a crossover.
# Those are staged: you change them, the plot shows what you would get, and
# Apply is what makes them real. The gap between the two is honest, because
# the control was never claiming to describe the hardware.
#
# Mute is not a design. It reports whether a driver is making sound, and
# there is no such thing as an intended-but-not-yet-real mute. So it is
# enforced: clicking one writes it, and so does loading a project or
# importing a preset. Master volume, source and preset work the same way
# and never needed saying, because they are not stored in a project file --
# mute is, because the device stores it in the preset too.
#
# The alternative was to ignore a loaded mute, which is worse in a way that
# is not obvious: Apply would still write the routing that config specifies
# while dropping the mute that made it safe. Half a configuration, and the
# half that makes noise.
def default_fir() -> dict[str, Any]:
    """An input's FIR block, with nothing loaded.

    An empty tap list means "leave whatever is on the device alone", not
    "load an empty filter". A project that has never been given a filter
    should not wipe one somebody loaded from elsewhere, and a filter of no
    taps is not a thing the hardware can be asked for anyway.

    `pending` says the taps here have not been written yet. Two thousand
    and forty-eight coefficients is a hundred and forty-seven packets, so
    an Apply that rewrote them every time would spend most of itself on a
    filter nobody had touched.
    """
    return {"enabled": False, "taps": [], "source": "", "pending": False}


def default_input(index: int, n_routes: int,
                  n_peq: int) -> dict[str, Any]:
    """A fresh input: routed nowhere.

    This used to send input N straight to output N, which is what the
    hardware itself does with a slot nobody has configured. On a device
    wired to a passive speaker that is the dangerous arrangement, not the
    neutral one: a new project has no crossovers either, so output 1 would
    carry full-range programme at 0 dB into whatever is on it -- and on a
    two-way that is the tweeter.

    Every other default here is off: a PEQ band, a crossover group and a
    compressor all arrive switched out until someone switches them in.
    Routing was the one exception, and it was the one that could put bass
    into a driver that cannot take it. A new project is silent now, which
    is a thing you notice and fix in seconds, rather than loud, which is a
    thing you notice once.

    `n_routes` is how many mixer cells the address map has addresses for,
    which is not always the output count: a C-DSP 8x12 lists eight for
    twelve outputs and an mSHARC 4x8 four for eight. Built to the output
    count instead, the window offered cells with nothing behind them and
    every write dropped them without a word.
    """
    return {"index": index, "name": f"In {index + 1}", "gain": 0.0,
            "mute": False,
            "peq": [default_peq_band(i, n_peq) for i in range(n_peq)],
            "fir": default_fir(),
            "routing": [{"index": o, "enabled": False, "gain": 0.0,
                         "polarity": False} for o in range(n_routes)]}


def new_project(n_in: int, n_out: int, n_peq: int, rate: int,
                n_peq_in: int | None = None, n_groups: int = 2,
                n_routes: int | None = None) -> dict[str, Any]:
    """A blank project shaped like the device.

    Inputs get their own band count because four of the shipped address maps
    give their inputs none at all. Built to the output count, those devices
    showed ten input bands with no address behind them: editable, drawn on
    the response, and dropped without a word by every write.
    """
    if n_peq_in is None:
        n_peq_in = n_peq
    if n_routes is None:
        n_routes = n_out
    return {"version": 1, "name": "untitled", "rate": rate,
            "inputs": [default_input(i, n_routes, n_peq_in)
                       for i in range(n_in)],
            "outputs": [default_output(i, n_peq, n_groups)
                        for i in range(n_out)]}


def peq_biquad(band: dict[str, Any], rate: int) -> dict[str, float]:
    """What a band contributes to the response *as things stand*.

    A band that is switched off contributes nothing, so this returns a
    passthrough for it. That makes this the right function for drawing and
    the wrong one for writing -- see peq_coeff, which returns the designed
    coefficients whether or not the band is currently in circuit.
    """
    # Switched off first. Checking manual first drew a bypassed band at full
    # strength, so the plot could show a tweeter protected by a highpass the
    # device has out of circuit.
    if not band.get("enabled"):
        return dict(BYPASS)
    if band.get("manual"):
        return dict(band["manual"])
    return design_biquad(band["type"], band["freq"], band["q"],
                         band.get("gain", 0.0), rate)


def crossover_biquads(group: dict[str, Any], rate: int,
                      slots: int = XOVER_SLOTS) -> list[dict[str, float]]:
    """What a crossover group contributes to the response as things stand.

    A group that is switched off contributes nothing. Padded to `slots` with
    passthroughs, so a caller always gets a fixed-length cascade.

    The counterpart for writing is crossover_coeffs, which designs the group
    regardless of whether it is engaged and numbers each section for the
    payload.
    """
    if not group.get("enabled"):
        designed = []               # see peq_biquad: off is off, manual too
    elif group.get("manual"):
        designed = [dict(b) for b in group["manual"]]
    else:
        designed = design_crossover(group["mode"], group["alignment"],
                                    int(group["order"]), group["freq"],
                                    rate, max_biquads=slots)
    # A comprehension rather than [dict(BYPASS)] * n, which would hand back
    # the same object several times. See crossover_coeffs.
    designed += [dict(BYPASS) for _ in range(slots - len(designed))]
    return designed[:slots]


def ms_to_duration(ms: float) -> dict[str, int]:
    """Milliseconds -> the {secs, nanos} struct minidspd expects for delay."""
    ms = max(0.0, float(ms))
    total_ns = int(round(ms * 1_000_000))
    return {"secs": total_ns // 1_000_000_000,
            "nanos": total_ns % 1_000_000_000}


def peq_coeff(band: dict[str, Any], rate: int) -> dict[str, float]:
    """What a band should be *written* as, in or out of circuit.

    Whether the band is in circuit is carried by the separate bypass flag, so
    the coefficients are written either way. This mirrors how Device Console
    behaves and keeps a disable/re-enable cycle lossless: switching a filter
    off and on again gets the same filter back rather than a passthrough.

    The counterpart is peq_biquad, which answers what the band contributes
    right now and is what the response plot uses.
    """
    if band.get("manual"):
        return dict(band["manual"])
    try:
        return design_biquad(band["type"], band["freq"], band["q"],
                             band.get("gain", 0.0), rate)
    except (ValueError, KeyError) as exc:
        # Said out loud rather than answered with a passthrough. A flat
        # section paired with a bypass flag that says "in circuit" is the
        # outcome crossover_coeffs was changed to refuse, for the same
        # reason: the payload claims a filter the device will not have.
        raise ValueError(
            f"PEQ band {band.get('index', 0) + 1} asks for "
            f"{band.get('type', '?')} at {band.get('freq', '?')} Hz "
            f"Q {band.get('q', '?')}, which cannot be built at "
            f"{rate} Hz: {exc}") from None


def crossover_coeffs(group: dict[str, Any], rate: int,
                     slots: int = XOVER_SLOTS) -> list[dict[str, float]]:
    """All biquads of one crossover group, padded to `slots`.

    Each biquad carries its own `index`, numbered 0..slots-1 *within the
    group*. minidspd rejects the payload outright without it ("biquad index
    not specified"), and numbering them absolutely across both groups is
    rejected as out of range.

    Unused slots become explicit passthroughs so that coefficients from a
    previous tuning can never linger in hardware the current one does not
    reach. The counterpart for drawing is crossover_biquads.
    """
    if group.get("manual"):
        designed = [dict(b) for b in group["manual"]]
        if len(designed) > slots:
            # Refused rather than trimmed. An import can carry more sections
            # than the group holds, and dropping the surplus silently threw
            # away 6 dB of a highpass while reporting the write as complete.
            raise ValueError(
                f"crossover group {group.get('index', 0) + 1} holds "
                f"{len(designed)} sections but this device's group has "
                f"{slots} slots, so it cannot be written as it stands")
    else:
        try:
            designed = design_crossover(group["mode"], group["alignment"],
                                        int(group["order"]), group["freq"],
                                        rate, max_biquads=slots)
        except KeyError:
            # An entry that does not describe a filter at all. Nothing was
            # asked for, so bypass is the honest answer.
            designed = []
        except ValueError as exc:
            # A filter that was asked for and cannot be built -- a Bessel 8
            # low-pass near Nyquist puts its top section above it. Padding
            # with passthrough here produced the worst possible outcome: four
            # flat slots written while _bypass_field still reported the group
            # engaged, so a woofer took full range and the app called it a
            # crossover. Say so instead.
            raise ValueError(
                f"{group.get('alignment', '?')} {group.get('order', '?')} "
                f"{group.get('mode', '?')} at {group.get('freq', '?')} Hz "
                f"cannot be built at {rate} Hz: {exc}") from None
    # A comprehension, not [dict(BYPASS)] * n. The multiplication repeats one
    # object by reference, so the loop below stamped every padded slot into the
    # same dict and they all came out numbered 3: a two-section group produced
    # indices 0, 1, 3, 3 and slot 2 was never written at all. That is exactly
    # the lingering-coefficient case this padding exists to prevent -- change
    # a Butterworth 8 to an LR4 and the old third section stayed in circuit.
    designed += [dict(BYPASS) for _ in range(slots - len(designed))]
    designed = designed[:slots]
    for i, bq in enumerate(designed):
        bq["index"] = i
    return designed


def _bypass_field(entry: dict[str, Any]) -> dict[str, Any]:
    """The bypass key for a payload entry, or nothing if it is unknown.

    Hardware readback cannot recover bypass state -- it has no readable
    address -- so a filter read off the device has coefficients but no idea
    whether it is in circuit. `bypass` is optional in minidspd's API and only
    fields that are present cause a change, so omitting it leaves the device's
    own setting alone. Writing a guess here would silently switch filters on.
    """
    if entry.get("bypass_source", "default") != "unknown":
        return {"bypass": not entry.get("enabled", False)}
    return {}


def _peq_entry(band: dict[str, Any], rate: int) -> dict[str, Any]:
    try:
        coeff = peq_coeff(band, rate)
    except ValueError:
        if band.get("enabled"):
            raise
        # Switched off, so what goes in the slot is a passthrough either way
        # and the flag beside it says the band is out of circuit. No reason
        # to refuse a whole write over a band that is not in the signal.
        coeff = dict(BYPASS)
    return {"index": band["index"], "coeff": coeff, **_bypass_field(band)}


def _crossover_entry(group: dict[str, Any], rate: int) -> dict[str, Any]:
    return {"index": group["index"], "coeff": crossover_coeffs(group, rate),
            **_bypass_field(group)}


def build_config_payload(project: dict[str, Any]) -> dict[str, Any]:
    """Whole project -> one minidspd `POST /devices/N/config` body.

    Three shapes here are not obvious and were established against the live
    schema rather than guessed:

      * `delay` is a Duration struct, not a float.
      * A `crossover` entry is a whole *group*: `coeff` is an array of the
        group's biquads, indexed by group, not one entry per biquad.
      * `bypass` is writable on both crossover groups and PEQ bands. It must
        follow the project's enabled state -- hardcoding `false` silently
        switches on filters the user deliberately disabled.
    """
    rate = int(project.get("rate", 96000))
    outputs = []
    for out in project["outputs"]:
        entry: dict[str, Any] = {
            "index": out["index"],
            # For this program, not for the device: the hardware has nowhere
            # to keep a channel name, and the unfiltered-output question has
            # to be able to call an output what the window calls it. Stripped
            # again in Daemon.set_config before the payload goes on the wire,
            # because that endpoint's shape was established against the live
            # schema and an extra field there is untested.
            "name": out.get("name", ""),
            "gain": float(out["gain"]),
            "mute": bool(out["mute"]),
            "invert": bool(out.get("invert", False)),
            "delay": ms_to_duration(out.get("delay", 0.0)),
            "peq": [_peq_entry(b, rate) for b in out["peq"]],
            "crossover": [_crossover_entry(g, rate)
                          for g in out.get("crossover", [])],
        }
        comp = out.get("compressor")
        # Only when something established it. Five of its six settings cannot
        # be read back, so over minidspd -- which never reads the stored
        # preset -- an untouched compressor would have carried this app's
        # stock numbers onto the device on every Apply.
        if comp and comp.get("bypass_source", "default") != "default":
            entry["compressor"] = {
                k: (bool(comp[k]) if k == "enabled" else float(comp[k]))
                for k in ("enabled", "threshold", "makeup", "ratio", "knee",
                          "attack", "release") if k in comp}
        outputs.append(entry)

    inputs = []
    for inp in project["inputs"]:
        inputs.append({
            "index": inp["index"],
            "gain": float(inp["gain"]),
            "mute": bool(inp["mute"]),
            "peq": [_peq_entry(b, rate) for b in inp["peq"]],
            "routing": [{"index": r["index"], "enabled": bool(r["enabled"]),
                         "gain": float(r.get("gain", 0.0)),
                         "polarity": bool(r.get("polarity", False))}
                        for r in inp.get("routing", [])],
        })
        # A FIR filter rides along only when there is one to send and it
        # has not been sent. Two thousand and forty-eight coefficients is a
        # hundred and forty-seven packets a channel, so including it every
        # time would make every Apply pay for a filter nobody touched --
        # and an input with no taps loaded must not wipe the one already on
        # the device.
        fir = inp.get("fir") or {}
        if fir.get("taps") and fir.get("pending"):
            inputs[-1]["fir"] = {"taps": list(fir["taps"]),
                                 "enabled": bool(fir.get("enabled"))}
    return {"inputs": inputs, "outputs": outputs}


def _apply_peq_readback(dst_bands: list[dict[str, Any]],
                        read_bands: list[dict[str, Any]], rate: int) -> None:
    """Fold PEQ bands read from the hardware into a project.

    Coefficients are decoded into editable parameters where the shape can be
    inverted; raw coefficients are kept only as a fallback. Enablement is left
    alone unless the state is unknown, in which case the band is shown as off
    rather than asserted to be active.
    """
    for band in read_bands:
        slot = band["index"]
        if not 0 <= slot < len(dst_bands):
            # Skipped, not stopped. These carry their own slot number rather
            # than arriving in order, so one out of range used to abandon
            # every band after it -- and a negative one indexed from the end
            # and rewrote a band nobody asked about.
            continue
        dst = dst_bands[slot]
        # "device" is the most authoritative source there is: it came out
        # of the hardware's own flash. Downgrading it to unknown -- and
        # switching the filter off -- destroys what an earlier read
        # established, which is the opposite of what this is for.
        unknown = dst.get("bypass_source") not in ("import", "user",
                                                   "device")
        if unknown:
            dst["bypass_source"] = "unknown"

        # An all-zero block means the device did not report this band at all.
        # PEQ addresses are not served by the hardware, so the values on
        # screen are whatever the project already held -- say so rather than
        # letting invented defaults pass for measurements.
        coeff = band.get("coeff") or {}
        if all(abs(float(coeff.get(k, 0.0))) < 1e-12
               for k in ("b0", "b1", "b2", "a1", "a2")):
            dst["read_state"] = "unreadable"
        else:
            dst["read_state"] = "read"

        if not band.get("active"):
            if unknown:
                dst["enabled"] = False
            if dst["read_state"] != "unreadable":
                # Only when the slot actually answered with a passthrough. A
                # slot that answered nothing has said nothing about what is
                # in it, and throwing the coefficients away on that basis is
                # the opposite of what the line above just recorded -- and
                # PEQ never answers on a Flex 8, so this fired on every read.
                dst["manual"] = None
            continue
        decoded = decode_peq(band["coeff"], rate)
        if decoded:
            kind, f0, q, gain = decoded
            dst.update(type=kind, freq=round(f0, 1), q=round(q, 4),
                       gain=round(gain, 2), manual=None)
        else:
            dst["manual"] = dict(band["coeff"])
        if unknown:
            dst["enabled"] = False


def apply_readback(project: dict[str, Any],
                   readings: list[dict[str, Any]],
                   inputs: "list[dict[str, Any]] | None" = None,
                   ) -> dict[str, Any]:
    """Fold live device readings into a project.

    Filters that match a standard alignment become editable designs; anything
    else is kept verbatim as manual coefficients so a round trip cannot alter
    what the hardware is doing.
    """
    rate = int(project.get("rate", 96000))
    for r in readings:
        idx = r["index"]
        if not 0 <= idx < len(project["outputs"]):
            continue
        out = project["outputs"][idx]
        if "gain" in r:
            out["gain"] = r["gain"]
        if "delay" in r:
            out["delay"] = r["delay"]
        # Unlike bypass, the channel gate and polarity have readable
        # addresses, so these are the device's own answer and not a guess.
        if "mute" in r:
            out["mute"] = r["mute"]
        if "invert" in r:
            out["invert"] = r["invert"]

        for g in r.get("crossover", []):
            # By the group's own index, and skipping rather than stopping --
            # the same rule every sibling loop follows. Positional worked
            # only because both readback paths happen to emit dense ordered
            # lists today.
            gi = g.get("index")
            if not isinstance(gi, int) or not 0 <= gi < len(out["crossover"]):
                continue
            dst = out["crossover"][gi]
            # Coefficients are readable; bypass is not. Only mark it unknown
            # if the project has not already learned it from a config file --
            # a read should add knowledge, never destroy it.
            if dst.get("bypass_source") not in ("import", "user", "device"):
                # We can read the coefficients but not whether the filter is
                # engaged. Do not claim it is: showing an unknown crossover as
                # active draws a band-pass that may not exist. Parameters are
                # still populated so they can be seen and resolved.
                dst["bypass_source"] = "unknown"
                dst["enabled"] = False
            held = [b for b in (g.get("coeff") or []) if not is_bypass(b)]
            if not held:
                # Every slot a passthrough: the group holds no filter, and
                # the design on screen stands rather than being invented
                # from unity coefficients.
                dst["manual"] = None
                continue
            if not g.get("active"):
                # Real sections, none of them a pass filter -- a notch, an
                # all-pass, something hand-entered. There is no alignment to
                # name, so they are kept verbatim, which is what "custom"
                # means everywhere else here. Dropping them replaced a filter
                # the device was running with whatever the card still said.
                dst["alignment"] = "custom"
                # Copied. Stored by reference, the project and the reading it
                # came from shared one list -- and a preset import applies
                # the same cfg to a preview project and then to the real one.
                dst["manual"] = [dict(b) for b in g["coeff"]]
                continue
            if g.get("alignment") in ALIGNMENTS:
                dst.update(mode=g["mode"], alignment=g["alignment"],
                           order=g["order"], freq=g["freq"], manual=None)
            else:
                dst["alignment"] = "custom"
                dst["manual"] = [dict(b) for b in g["coeff"]]

        _apply_peq_readback(out["peq"], r.get("peq", []), rate)

    for r in (inputs or []):
        idx = r["index"]
        if not 0 <= idx < len(project["inputs"]):
            continue
        inp = project["inputs"][idx]
        if "gain" in r:
            inp["gain"] = r["gain"]
        if "mute" in r:
            inp["mute"] = r["mute"]
        # A mixer cell's gain reads back; its gate does not, and comes from
        # the stored preset instead. Only what was actually read is folded
        # in here.
        found = {x["index"]: x for x in r.get("routing", [])}
        for route in inp.get("routing", []):
            live = found.get(route["index"])
            if not live:
                continue
            if "gain" in live:
                route["gain"] = live["gain"]
            # Polarity reads back beside the gain and was thrown away here,
            # so the stored preset's copy won unopposed a moment later.
            if "polarity" in live:
                route["polarity"] = live["polarity"]
        _apply_peq_readback(inp["peq"], r.get("peq", []), rate)

    return project


# ---------------------------------------------------------------------------
# miniDSP Device Console XML import
# ---------------------------------------------------------------------------
#
# Device Console exports a complete preset as XML: every filter with its
# coefficients *and* its bypass flag, plus gains, delays, polarity and
# routing. That bypass flag is the one thing hardware readback cannot recover,
# because bypass is set by command 0x19 and has no readable address -- so an
# export is the only way to know which filters are actually in circuit.
#
# Filters carry an `addr` attribute matching the addresses in address_maps/,
# so entries are matched by address rather than by channel naming convention.
# That keeps the importer device-agnostic.

_XML_FILTER = re.compile(
    r'<filter\s+name="(?P<name>[^"]+)"\s+addr="(?P<addr>\d+)"\s*>'
    r'(?P<body>.*?)</filter>', re.S)
_XML_ITEM = re.compile(
    r'<item\s+name="(?P<name>[^"]+)"\s+addr="(?P<addr>\d+)"\s*>\s*'
    r'<dec>(?P<dec>[^<]*)</dec>', re.S)

# Device Console filter type -> (our type, alignment, order) where relevant.
_XML_PEQ_TYPES = {"PK": "peaking", "SL": "lowshelf", "SH": "highshelf",
                  "LP": "lowpass", "HP": "highpass", "NO": "notch",
                  "AP": "allpass", "BP": "bandpass"}
_XML_XOVER = re.compile(r"^(BW|LR|BE)(LPF|HPF)_(\d+)$")
_XML_ALIGN = {"BW": "butterworth", "LR": "linkwitz-riley", "BE": "bessel"}


def _xml_tag(body: str, tag: str) -> str:
    m = re.search(rf"<{tag}>(.*?)</{tag}>", body, re.S)
    return m.group(1).strip() if m else ""


def _xml_float(text: str, default: float = 0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def parse_device_console_xml(text: str) -> dict[str, Any]:
    """Parse a Device Console preset export into address-keyed data.

    Returns::

        {"filters": {addr: {...}}, "items": {addr: (name, value)},
         "dsp_version": int | None}
    """
    filters: dict[int, dict[str, Any]] = {}
    for m in _XML_FILTER.finditer(text):
        body = m.group("body")
        raw_type = _xml_tag(body, "type")
        coeffs = [c for c in _xml_tag(body, "dec").split(",") if c.strip()]
        entry: dict[str, Any] = {
            "name": m.group("name"),
            "type": raw_type,
            "freq": _xml_float(_xml_tag(body, "freq")),
            "q": _xml_float(_xml_tag(body, "q"), 0.7071),
            "gain": _xml_float(_xml_tag(body, "boost")),
            "bypass": _xml_tag(body, "bypass") == "1",
        }
        if len(coeffs) == 5:
            b0, b1, b2, a1, a2 = (float(c) for c in coeffs)
            entry["coeff"] = {"b0": b0, "b1": b1, "b2": b2, "a1": a1, "a2": a2}
        elif coeffs and len(coeffs) % 5 == 0:
            # A crossover carries every section of the group in one entry --
            # twenty numbers for the four slots of a Flex 8. Kept, because
            # without them a shape the type string does not describe imports
            # as nothing at all.
            vals = [float(c) for c in coeffs]
            entry["coeffs"] = [
                dict(zip(COEFF_KEYS, vals[i:i + 5]))
                for i in range(0, len(vals), 5)]
        xm = _XML_XOVER.match(raw_type)
        if xm:
            entry["alignment"] = _XML_ALIGN.get(xm.group(1), "butterworth")
            entry["mode"] = "lowpass" if xm.group(2) == "LPF" else "highpass"
            entry["order"] = int(xm.group(3))
        filters[int(m.group("addr"))] = entry

    items: dict[int, tuple[str, float]] = {}
    for m in _XML_ITEM.finditer(text):
        items[int(m.group("addr"))] = (m.group("name"),
                                       _xml_float(m.group("dec")))

    ver = re.search(r"<dspversion>(\d+)</dspversion>", text)
    return {"filters": filters, "items": items,
            "dsp_version": int(ver.group(1)) if ver else None}


def _apply_stored_bands(dst_bands: list[dict[str, Any]],
                        src_bands: list[dict[str, Any]],
                        stats: dict[str, int]) -> None:
    """Fold one channel's stored PEQ bank into the project's."""
    for band in src_bands:
        slot = band["index"]
        if not 0 <= slot < len(dst_bands):
            continue
        dst = dst_bands[slot]
        stats["peq"] += 1
        bypassed = band.get("bypass")
        if bypassed is not None:
            dst["bypass_source"] = "device"
            dst["enabled"] = not bypassed
            if bypassed:
                stats["bypassed"] += 1
        dst["read_state"] = "stored"
        if "type" in band:
            dst.update(type=band["type"], freq=round(band["freq"], 1),
                       q=round(band["q"], 4), gain=round(band["gain"], 2),
                       manual=None)
        elif band.get("active"):
            # A real section the inverse does not cover. Kept verbatim so a
            # round trip cannot quietly redesign what the device is running.
            dst["manual"] = dict(band["coeff"])
        else:
            # Unity coefficients: the slot holds no filter. The design on
            # screen is left as it was rather than inventing one from a
            # passthrough.
            dst["manual"] = None


def compare_live_stored(readings: list[dict[str, Any]],
                        inputs: list[dict[str, Any]],
                        cfg: dict[str, Any]) -> dict[str, Any]:
    """How the running parameters differ from the stored preset.

    Only the fields the hardware actually reports can be compared -- gain,
    delay, polarity and the channel gates. Coefficients cannot, because they
    do not read back, so a filter that was changed live and not stored is
    invisible here. "Agrees" therefore means no reason to think otherwise,
    not proof of identity, and `unreadable` says how much was out of reach.

    It is still the difference between a silent surprise and a visible one:
    a device that has been applied to but not saved shows up the moment it is
    read, rather than the next time it is powered on.

    Returns {compared, unreadable, differences: [text, ...]}. Callers treat
    an empty `differences` with `compared` of zero as "nothing to say".
    """
    # One quantisation step, with room to spare. An output's stored gain is
    # not the value it runs at and is not meant to be: the device truncates
    # when it loads a preset, so what gets stored is the request that lands
    # on the tuned value afterwards. The request therefore sits above the
    # running value by less than a step, and reporting that as a difference
    # would mean flagging every tuned output on every read -- for the one
    # thing that is arranged so a power cycle changes nothing.
    GAIN_REQUEST_HEADROOM = 0.35

    stored_out = {c["index"]: c for c in cfg.get("outputs", [])}
    stored_in = {c["index"]: c for c in cfg.get("inputs", [])}
    out: dict[str, Any] = {"compared": 0, "unreadable": 0, "differences": []}

    def same(a: Any, b: Any, tol: float) -> bool:
        if isinstance(a, bool) or isinstance(b, bool):
            return bool(a) == bool(b)
        try:
            return abs(float(a) - float(b)) <= tol
        except (TypeError, ValueError):
            return a == b

    def stored_request(running: Any, stored: Any) -> bool:
        """Whether the stored value is the request that produced the running
        one.

        store_gain_requests does this for output, input and mixer-cell gains
        alike, so all three sit a step above what is running. Exempting only
        outputs made 191 of 200 simulated saves report a disagreement that
        was not one, immediately after a successful save -- which is the
        message that would otherwise catch a real unsaved change.
        """
        try:
            gap = float(stored) - float(running)
        except (TypeError, ValueError):
            return False
        return 0 < gap <= GAIN_REQUEST_HEADROOM

    def show(v: Any) -> str:
        if isinstance(v, bool):
            return "on" if v else "off"
        try:
            return f"{float(v):g}"
        except (TypeError, ValueError):
            return str(v)

    for live, stored, label, fields in (
            (readings, stored_out, "out",
             (("gain", 0.02), ("delay", 0.002), ("mute", 0), ("invert", 0))),
            (inputs, stored_in, "in", (("gain", 0.02), ("mute", 0)))):
        for r in live:
            s = stored.get(r.get("index"))
            if not s:
                continue
            n = r["index"] + 1
            for key, tol in fields:
                if key not in r or key not in s:
                    continue
                out["compared"] += 1
                if same(r[key], s[key], tol):
                    continue
                # A gain is the one field stored deliberately unequal to
                # what is running. Accept the gap only in the direction and
                # size truncation produces; anything else is a real
                # disagreement and still gets said.
                if key == "gain" and stored_request(r[key], s[key]):
                    continue
                out["differences"].append(
                    f"{label} {n} {key}: running {show(r[key])}, "
                    f"stored {show(s[key])}")

            # Mixer cells read back, so routing is comparable too.
            sr = {x["index"]: x for x in s.get("routing", [])}
            for cell in r.get("routing", []):
                t = sr.get(cell["index"])
                if not t:
                    continue
                # Gain only: the gate does not read back, so a live value
                # for it would be a constant being compared with the truth.
                if "gain" in cell and "gain" in t:
                    out["compared"] += 1
                    if (not same(cell["gain"], t["gain"], 0.02)
                            and not stored_request(cell["gain"], t["gain"])):
                        out["differences"].append(
                            f"{label} {n} -> out {cell['index'] + 1} gain: "
                            f"running {show(cell['gain'])}, "
                            f"stored {show(t['gain'])}")

            # So do crossover coefficients. Compared as coefficients rather
            # than as a decoded corner, because two different designs can
            # round to the same frequency and this is meant to catch a real
            # difference in what the device is computing.
            sx = {g["index"]: g for g in s.get("crossover", [])}
            for grp in r.get("crossover", []):
                t = sx.get(grp["index"])
                if not t:
                    continue
                for k, (a, b) in enumerate(zip(grp.get("coeff", []),
                                               t.get("coeff", []))):
                    out["compared"] += 1
                    if any(not same(a.get(c), b.get(c), 1e-6)
                           for c in COEFF_KEYS):
                        out["differences"].append(
                            f"{label} {n} crossover "
                            f"{xover_label(grp['index'])} "
                            f"section {k + 1}: coefficients differ")

            out["unreadable"] += len(s.get("peq", []))
    return out


def apply_stored_preset(project: dict[str, Any], cfg: dict[str, Any],
                        readable: bool = False) -> dict[str, int]:
    """Fold a preset read out of the device's flash into a project.

    The counterpart of apply_device_console_xml for data that came from the
    hardware instead of a file. Addresses have already been resolved through
    the address map by the device layer, so this matches on channel index.

    Everything here is the device's own answer, including the three things a
    live read cannot produce: coefficients, per-filter bypass, and mixer
    gates. What it is not is a reading of what the DSP is running this
    instant -- it is what the device loads at power-on, which is the same
    thing unless something has been written live since.

    Which is why most of it is applied only when `readable` is set. Measured
    class by class against a Flex 8, gains, delays, mixer-cell gains and
    every crossover block read back and match, so a live read is the better
    answer for those: it says what the device is doing now, where this says
    what it would come back as, and taking the stored value would hide
    exactly the disagreement worth seeing.

    Three things cannot be read at any price, and those are supplied
    unconditionally: PEQ coefficients, which answer zero; mixer-cell gates,
    which answer a constant 1 whatever the routing is; and the bypass flag
    on every filter, which has no parameter address at all.
    """
    stats = {"outputs": 0, "inputs": 0, "crossover": 0, "peq": 0,
             "bypassed": 0, "routing": 0}

    for src in cfg.get("outputs", []):
        idx = src["index"]
        if not 0 <= idx < len(project["outputs"]):
            continue
        out = project["outputs"][idx]
        stats["outputs"] += 1
        if readable:
            for key in ("gain", "delay", "mute", "invert"):
                if key in src:
                    out[key] = src[key]

        for group in src.get("crossover", []):
            gi = group["index"]
            if not 0 <= gi < len(out["crossover"]):
                continue
            dst = out["crossover"][gi]
            stats["crossover"] += 1
            # The coefficients read back, so the live pass has already put
            # the right ones here. Only the bypass flag is taken from store,
            # because it has no address to read.
            bypassed = group.get("bypass")
            if bypassed is not None:
                dst["bypass_source"] = "device"
                dst["enabled"] = not bypassed
                if bypassed:
                    stats["bypassed"] += 1
            if readable:
                dst["read_state"] = "stored"
                if group.get("alignment") in ALIGNMENTS:
                    dst.update(mode=group["mode"],
                               alignment=group["alignment"],
                               order=group["order"], freq=group["freq"],
                               manual=None)
                elif any(not is_bypass(b)
                         for b in (group.get("coeff") or [])):
                    # Real sections, whether or not any of them is a pass
                    # filter. Keying on "active" -- which counts pass filters
                    # only -- discarded a group of notches and left the card's
                    # old design asserted as the device's, with the stored
                    # bypass flag switching it on. The same shape was fixed in
                    # apply_readback and missed here.
                    dst["alignment"] = "custom"
                    dst["manual"] = [dict(b) for b in group["coeff"]]
                else:
                    dst["manual"] = None

        comp = src.get("compressor")
        if comp:
            dst = out.setdefault("compressor", default_compressor())
            for k, v in comp.items():
                dst[k] = v
            # Five of its six settings cannot be read back, so this is the
            # only place they come from. Recording it as the device's own
            # answer rather than a default keeps it out of the "state
            # unknown" warning.
            dst["bypass_source"] = "device"
            stats["compressor"] = stats.get("compressor", 0) + 1

        _apply_stored_bands(out["peq"], src.get("peq", []), stats)

    for src in cfg.get("inputs", []):
        idx = src["index"]
        if not 0 <= idx < len(project["inputs"]):
            continue
        inp = project["inputs"][idx]
        stats["inputs"] += 1
        if readable:
            for key in ("gain", "mute"):
                if key in src:
                    inp[key] = src[key]
        # A cell's gate is one of the three things the hardware will not
        # report, so it always comes from here. Its gain does read back, so
        # that is left to the live pass unless there was not one.
        routes = {r["index"]: r for r in src.get("routing", [])}
        for route in inp.get("routing", []):
            found = routes.get(route["index"])
            if not found:
                continue
            if "enabled" in found:
                route["enabled"] = found["enabled"]
                stats["routing"] += 1
            # A cell's polarity reads back live, so the stored value is only
            # right when there was nothing better. Taken unconditionally, a
            # live reading of a reverted cell polarity was overwritten by the
            # stored one and would have been written back -- a driver out of
            # phase at the crossover.
            if "polarity" in found and not (readable and "polarity" in route):
                route["polarity"] = found["polarity"]
            if readable and "gain" in found:
                route["gain"] = found["gain"]
        fir = src.get("fir")
        if fir and fir.get("taps"):
            # Already on the device, so not pending: this is what it holds,
            # not something waiting to be sent to it.
            inp["fir"] = {"taps": list(fir["taps"]),
                          "enabled": bool(fir.get("enabled")),
                          "source": "device", "pending": False}
            stats["fir"] = stats.get("fir", 0) + 1
        _apply_stored_bands(inp["peq"], src.get("peq", []), stats)

    return stats


# What travels when one channel is imported onto another, and what does not.
#
# The rule is that tuning travels and identity stays. A channel's identity is
# its index, its name, and -- for an input -- its routing, which is the thing
# that makes it the left one rather than the right one. Copy an input's
# routing onto its partner and both inputs feed the same pair of outputs: the
# other pair goes silent and the first pair sums to mono. Everything else on a
# channel is work someone did, which is the whole reason for importing it.
IMPORT_FIELDS_OUTPUT = ("gain", "mute", "invert", "delay")
IMPORT_FIELDS_INPUT = ("gain", "mute")

# Copied band and group fields. `index` is position and stays put; the
# provenance keys are rewritten afterwards rather than inherited, because a
# band that came off the device for output 1 is not a reading of output 3.
_BAND_FIELDS = ("type", "freq", "q", "gain", "enabled", "manual",
                "manual_source")
_GROUP_FIELDS = ("mode", "alignment", "order", "freq", "enabled", "manual")


def _mark_imported(entry: dict[str, Any]) -> None:
    """Say where a filter's values came from, now that they have moved."""
    entry["bypass_source"] = "import"
    entry["read_state"] = "imported"


def import_channel(dst: dict[str, Any], src: dict[str, Any],
                   is_output: bool) -> dict[str, int]:
    """Copy one channel's tuning onto another, in place.

    Both sides are in project shape, which is what makes this one code path
    rather than two: a channel from another preset is turned into a project
    channel first, so importing from the next output and importing from
    preset 3 differ only in where `src` came from.

    Enabled state travels with everything else. It is visible on the strip
    and on the channel, so an imported mute explains itself, and leaving it
    behind would mean the one thing you could not carry over is the one the
    hardware makes easiest to see.
    """
    stats = {"peq": 0, "crossover": 0, "compressor": 0, "fields": 0}
    for key in (IMPORT_FIELDS_OUTPUT if is_output else IMPORT_FIELDS_INPUT):
        if key in src:
            dst[key] = copy.deepcopy(src[key])
            stats["fields"] += 1

    for slot, band in enumerate(src.get("peq", [])):
        if slot >= len(dst.get("peq", [])):
            break
        d = dst["peq"][slot]
        for k in _BAND_FIELDS:
            if k in band:
                d[k] = copy.deepcopy(band[k])
        _mark_imported(d)
        stats["peq"] += 1

    if not is_output:
        return stats

    for gi, group in enumerate(src.get("crossover", [])):
        if gi >= len(dst.get("crossover", [])):
            break
        d = dst["crossover"][gi]
        for k in _GROUP_FIELDS:
            if k in group:
                d[k] = copy.deepcopy(group[k])
        _mark_imported(d)
        stats["crossover"] += 1

    comp = src.get("compressor")
    if comp:
        d = dst.setdefault("compressor", default_compressor())
        d.update(copy.deepcopy(comp))
        d["bypass_source"] = "import"
        stats["compressor"] += 1
    return stats


def import_preset(project: dict[str, Any],
                  cfg: dict[str, Any]) -> dict[str, int]:
    """Fold a whole stored preset into a project, as an import.

    Routing travels here, unlike a channel import. Copying one input's
    routing onto another rearranges the signal flow; bringing a whole
    preset's routing is the signal flow, and leaving it behind would import
    a configuration that cannot work.

    `readable` is set because there is no live read to prefer: every value
    is the device's own, and the point is to see that preset rather than
    the one that is running.
    """
    stats = apply_stored_preset(project, cfg, readable=True)
    # apply_stored_preset marks its work as read from the device, which is
    # true of where the bytes came from and false about what they describe:
    # these are another preset's values, not this channel's current state.
    #
    # Only the channels the preset actually described. Walking all of them
    # relabelled filters nobody imported: a band whose state was genuinely
    # unknown became "import", which drops it out of the unknown_bypass
    # warning and, worse, makes _bypass_field assert a bypass flag on the
    # write -- so a tweeter highpass the app had only ever guessed was off
    # would be switched out for real.
    for key in ("outputs", "inputs"):
        named = {c["index"] for c in cfg.get(key, []) if "index" in c}
        for ch in project[key]:
            if ch["index"] not in named:
                continue
            for entry in ch.get("peq", []):
                _mark_imported(entry)
            for entry in ch.get("crossover", []):
                _mark_imported(entry)
            if ch.get("compressor"):
                ch["compressor"]["bypass_source"] = "import"
    return stats


def _usable_freq(freq: Any, rate: int) -> bool:
    """Whether an imported corner is one this device could filter at.

    An export with no <freq> tag parses as 0.0, and taking that as a design
    put a band or a group in the project that nothing can build: the drawing
    path raised out of a repaint and every write raised too. The coefficients
    in the same entry are still used -- see the branches below -- so nothing
    is lost by declining the parameters.
    """
    try:
        return 0.0 < float(freq) < rate / 2.0
    except (TypeError, ValueError):
        return False


def _xml_sections(entry: dict[str, Any]) -> list[dict[str, float]]:
    """A parsed filter's sections, however many the entry carried.

    The parser records five numbers as `coeff` and any other whole multiple
    of five as `coeffs`, never both, so a caller that reads only the plural
    misses a one-section group.
    """
    if entry.get("coeffs"):
        return list(entry["coeffs"])
    return [entry["coeff"]] if "coeff" in entry else []


def apply_device_console_xml(project: dict[str, Any], parsed: dict[str, Any],
                             amap: "AddressMap") -> dict[str, int]:
    """Fold a parsed Device Console export into a project.

    Matching is by DSP address, so this does not depend on any particular
    channel-naming convention.
    """
    filters = parsed["filters"]
    items = parsed["items"]
    stats = {"outputs": 0, "inputs": 0, "crossover": 0, "peq": 0,
             "bypassed": 0, "routing": 0}

    for idx, spec in enumerate(amap.outputs):
        if idx >= len(project["outputs"]):
            break
        out = project["outputs"][idx]
        stats["outputs"] += 1

        if "gain" in spec and spec["gain"] in items:
            out["gain"] = items[spec["gain"]][1]
        if "delay" in spec and spec["delay"] in items:
            out["delay"] = items[spec["delay"]][1]
        if "invert" in spec and spec["invert"] in items:
            out["invert"] = bool(items[spec["invert"]][1])
        # Mute, mixer-cell polarity and the compressor are all in the export,
        # at addresses the map already names, and none of them was read. The
        # payload then wrote the app's own defaults over them: importing an
        # export unmuted a muted driver, un-inverted a deliberately inverted
        # cell and switched a limiter out -- on Apply, with nothing said.
        if "enable" in spec and spec["enable"] in items:
            raw = int(items[spec["enable"]][1])
            if raw in (XML_GATE_MUTED, XML_GATE_PASSING):
                out["mute"] = raw == XML_GATE_MUTED
        comp_spec, comp = spec.get("compressor"), out.get("compressor")
        if comp_spec and comp is not None:
            got = False
            for field in COMPRESSOR_FIELDS:
                addr = comp_spec.get(field)
                if addr in items:
                    comp[field] = round(float(items[addr][1]), 4)
                    got = True
            raw = items.get(comp_spec.get("enable"), (None, None))[1]
            if raw is not None and int(raw) in (XML_COMP_BYPASSED,
                                                XML_COMP_ENABLED):
                comp["enabled"] = int(raw) == XML_COMP_ENABLED
                got = True
            if got:
                comp["bypass_source"] = "import"

        for gi, base in enumerate(spec.get("xover_groups", [])):
            if gi >= len(out["crossover"]):
                break
            f = filters.get(base)
            dst = out["crossover"][gi]
            if not f:
                continue
            stats["crossover"] += 1
            if f["bypass"]:
                stats["bypassed"] += 1
            dst["bypass_source"] = "import"
            dst["read_state"] = "config"
            dst["enabled"] = not f["bypass"]
            dst["manual"] = None
            if "mode" in f and _usable_freq(f["freq"], amap.rate):
                dst["mode"] = f["mode"]
                dst["alignment"] = f["alignment"]
                dst["order"] = f["order"]
                dst["freq"] = f["freq"]
            elif any(not is_bypass(b) for b in _xml_sections(f)):
                # A shape _XML_XOVER does not describe. Kept verbatim, the
                # same as an unrecognised PEQ band: the group used to be
                # marked imported and switched on while still describing
                # whatever happened to be on screen before the import.
                dst["alignment"] = "custom"
                dst["manual"] = [dict(b) for b in _xml_sections(f)]

        for slot, addr in enumerate(spec.get("peq", [])):
            if slot >= len(out["peq"]):
                break
            f = filters.get(addr)
            if not f:
                continue
            dst = out["peq"][slot]
            stats["peq"] += 1
            if f["bypass"]:
                stats["bypassed"] += 1
            dst["bypass_source"] = "import"
            dst["read_state"] = "config"
            dst["enabled"] = not f["bypass"]
            dst["manual"] = None
            kind = _XML_PEQ_TYPES.get(f["type"])
            if kind and _usable_freq(f["freq"], amap.rate):
                dst["type"] = kind
                dst["freq"] = f["freq"]
                dst["q"] = f["q"] or 0.7071
                dst["gain"] = f["gain"]
            elif "coeff" in f:
                dst["manual"] = dict(f["coeff"])

    # Routing is matched by symbol name rather than address: the generated
    # maps record the mixer *gain* addresses, while the on/off flag lives in
    # a separate Mixer_<in>_<out>_status entry. Device Console encodes that
    # flag as 2 = enabled, 1 = disabled (not 1/0).
    by_name = {name: value for name, value in items.values()}
    for idx, spec in enumerate(amap.inputs):
        if idx >= len(project["inputs"]):
            break
        inp = project["inputs"][idx]
        stats["inputs"] += 1
        for route in inp.get("routing", []):
            key = f"Mixer_{idx}_{route['index']}_status"
            if key in by_name:
                route["enabled"] = int(by_name[key]) == 2
                stats["routing"] += 1
            gkey = f"Mixer_{idx}_{route['index']}"
            if gkey in by_name:
                route["gain"] = by_name[gkey]
            pols = spec.get("routing_polarity", [])
            dest = route["index"]
            if dest < len(pols) and pols[dest] in items:
                route["polarity"] = bool(items[pols[dest]][1])
        if "gain" in spec and spec["gain"] in items:
            inp["gain"] = items[spec["gain"]][1]
        for slot, addr in enumerate(spec.get("peq", [])):
            if slot >= len(inp["peq"]):
                break
            f = filters.get(addr)
            if not f:
                continue
            dst = inp["peq"][slot]
            # Counted alongside the output bands. Leaving them out meant an
            # import reported nothing for an input-voiced setup, where every
            # band that matters lives on the inputs.
            stats["peq"] += 1
            if f["bypass"]:
                stats["bypassed"] += 1
            dst["bypass_source"] = "import"
            dst["read_state"] = "config"
            dst["enabled"] = not f["bypass"]
            dst["manual"] = None
            kind = _XML_PEQ_TYPES.get(f["type"])
            if kind and _usable_freq(f["freq"], amap.rate):
                dst["type"] = kind
                dst["freq"] = f["freq"]
                dst["q"] = f["q"] or 0.7071
                dst["gain"] = f["gain"]
            elif "coeff" in f:
                dst["manual"] = dict(f["coeff"])

    return stats


def peq_is_effective(band: dict[str, Any]) -> bool:
    """Whether a PEQ band actually alters the signal.

    Device Console leaves unused bands un-bypassed but flat, so "enabled" is
    not the same as "doing something": a peaking or shelving filter at 0 dB is
    a no-op. Filters whose shape does not depend on gain (pass, notch, allpass)
    always count.
    """
    if not band.get("enabled"):
        return False
    manual = band.get("manual")
    if manual:
        return not is_bypass(manual)
    if band.get("type") in ("peaking", "lowshelf", "highshelf"):
        return abs(float(band.get("gain", 0.0))) > 1e-6
    return True


def count_effective_peq(bands: Iterable[dict[str, Any]]) -> int:
    return sum(1 for b in bands if peq_is_effective(b))


def write_gain_verified(daemon: "Daemon", readback: "Readback", output: int,
                        target_db: float, tries: int = 3,
                        tol: float = 0.05) -> tuple[float, float, int]:
    """Set an output gain and correct for the device's own quantisation.

    The Flex 8 dialect is Float32LE, so minidsp-rs writes the dB value
    verbatim -- yet the value that comes back is not the value sent. The
    error is ~0.2-0.3 dB at every level from -0.1 dB down to -100, which is
    relative rather than absolute precision: roughly five mantissa bits
    (2^-5 ~ 3% ~ 0.27 dB). The transform happens inside the device firmware,
    below anything the protocol exposes.

    Finite precision is not the problem. The problem is that it *truncates*
    instead of rounding to nearest, so every write lands low, and
    `write(read(x))` is not `read(x)` -- reading a gain and writing it
    straight back moves it down again, and repeating never settles. Rounding
    to nearest would halve the worst-case error and make the operation a
    fixed point. This is a firmware defect, not a limit of the hardware.

    Rather than model it, this closes the loop through _verify_gain.

    Returns (achieved_db, request_db, writes_performed). The request is the
    value that had to be *written* to land on achieved, and it matters as much
    as the result: the device applies the same rounding when it loads a preset
    at power-on, so storing the target rather than the request means the gain
    comes back a step lower than it was tuned to. Whatever gets saved to flash
    should be this, not the target.
    """
    def put(db: float) -> float:
        daemon.set_config({"outputs": [{"index": output, "gain": db}]})
        return readback.read_output(output)["gain"]

    return _verify_gain(put, target_db, tries, tol)


def write_input_gain_verified(daemon: "Daemon", readback: "Readback",
                              index: int, target_db: float, tries: int = 3,
                              tol: float = 0.05) -> tuple[float, float, int]:
    """The same closed loop, for an input's gain."""
    def put(db: float) -> float:
        daemon.set_config({"inputs": [{"index": index, "gain": db}]})
        return readback.read_input(index)["gain"]

    return _verify_gain(put, target_db, tries, tol)


def write_route_gain_verified(daemon: "Daemon", readback: "Readback",
                              index: int, dest: int, target_db: float,
                              tries: int = 3,
                              tol: float = 0.05) -> tuple[float, float, int]:
    """The same closed loop, for one mixer cell's gain."""
    def put(db: float) -> float:
        daemon.set_config({"inputs": [{"index": index, "routing": [
            {"index": dest, "gain": db}]}]})
        cells = readback.read_input(index).get("routing", [])
        for c in cells:
            if c.get("index") == dest:
                return c.get("gain")
        raise ProtocolError(
            f"input {index + 1} reported no gain for its cell to output "
            f"{dest + 1}, so the write could not be checked")

    return _verify_gain(put, target_db, tries, tol)


def _verify_gain(put, target_db: float, tries: int,
                 tol: float) -> tuple[float, float, int]:
    """Write, read, correct, and report the request that landed.

    Shared by every gain on the device. `put` writes one value and returns
    what the hardware reports afterwards; everything else about which gain
    it is belongs to the caller.

    Not every target is reachable. The grid is about 0.18 dB wide around
    -8 dB, so a target landing between two steps cannot be hit: asking for
    -8.25 gives -8.1648 or -8.3403 and nothing between, and the correction
    swings between the two indefinitely. That is what `best_request` is
    for -- the loop ends on whichever attempt came closest rather than on
    whichever came last, and the error that remains is the step size.

    Measured on a Flex 8, inputs, outputs and mixer cells share one grid:
    the same request lands on the same value whichever kind of gain it is.
    """
    request = float(target_db)
    best_request = best_achieved = None
    written = request
    writes = 0
    if tries < 1:
        # Zero attempts still has to answer. `achieved` was only bound
        # inside the loop, so a caller passing tries=0 got UnboundLocalError
        # out of a write path rather than a value.
        raise ValueError("a gain write needs at least one attempt")
    for _ in range(tries):
        # What gets returned has to be what was written, not what would have
        # been written next. `request` is advanced at the bottom of the loop,
        # so returning it after the last pass hands back a value nobody sent
        # -- and store_gain_requests puts that in flash, so power-on restores
        # a gain that was never measured.
        written = request
        achieved = put(request)
        writes += 1
        error = achieved - target_db
        if (best_achieved is None
                or abs(error) < abs(best_achieved - target_db)):
            best_request, best_achieved = request, achieved
        if abs(error) <= tol:
            return achieved, written, writes
        # Push the request the other way by the observed error. The ceiling is
        # +12 dB because a mixer cell goes that high; clamping to 0 turned a
        # correction on a boosted cell into a write of 0 dB.
        request = max(-127.0, min(GAIN_MAX_DB, request - error))

    if best_request is not None and abs(achieved - target_db) > abs(
            best_achieved - target_db):
        achieved = put(best_request)
        writes += 1
        return achieved, best_request, writes
    return achieved, written, writes


def store_gain_requests(payload: dict[str, Any],
                        applied: dict[str, Any]) -> None:
    """Put the values that landed into a payload bound for flash.

    Every gain here is stored as the *request* that produced the tuned
    value, not the value itself: the device truncates again when it loads a
    preset, so storing what was wanted means getting a step less of it back
    at power-on. Three classes of gain need this and for a long time only
    one was getting it.
    """
    for out in payload.get("outputs", []):
        req = (applied.get("gain_requests") or {}).get(out.get("index"))
        if req is not None:
            out["gain"] = req
    for inp in payload.get("inputs", []):
        idx = inp.get("index")
        req = (applied.get("input_gain_requests") or {}).get(idx)
        if req is not None:
            inp["gain"] = req
        for route in inp.get("routing", []):
            req = (applied.get("route_gain_requests") or {}).get(
                f"{idx},{route.get('index')}")
            if req is not None:
                route["gain"] = req


def _without_names(payload: dict[str, Any]) -> dict[str, Any]:
    """The payload as minidspd expects it: no channel names.

    A name is in the payload so a dialog can call an output what the window
    calls it. `POST /config` never asked for one, and the three shapes in
    build_config_payload that are not obvious were established against the
    live schema rather than guessed -- so an untested extra field is not sent
    there on the strength of an assumption about how strictly it validates.
    """
    outs = payload.get("outputs")
    if not outs:
        return payload
    return {**payload,
            "outputs": [{k: v for k, v in o.items() if k != "name"}
                        for o in outs]}


def apply_project(daemon: "Daemon", project: dict[str, Any],
                  readback: "Readback | None" = None,
                  verify_gains: bool = True,
                  tol: float = 0.05,
                  fir_progress: Any = None) -> dict[str, Any]:
    """Push a project to the device, correcting gain quantisation.

    Verification used to be opt-in, on the reasoning that a gain which came
    from readback already matched the hardware and so did not need checking.
    That reasoning is wrong, and measurably so: writing back the value the
    device just reported does not reproduce it. Writing -7.18 dB to an output
    reading -7.18 dB leaves it at -7.496, and repeating the write walks it
    down about 0.17 dB each time without converging:

        wrote  -7.180 -> read  -7.496
        wrote  -7.496 -> read  -7.659
        wrote  -7.659 -> read  -7.824
        wrote  -7.824 -> read  -7.993

    So an Apply that skipped verification quietly attenuated every output, and
    doing it five times cost a decibel. The closed loop below lands within a
    quantisation step and stays there, which is worth two or three extra
    round-trips per output.
    """
    payload = build_config_payload(project)
    # Asked, not attempted. Catching TypeError could not tell a missing
    # argument from one raised part-way through a write that had already put
    # hundreds of parameters on the device -- and the answer to that was to
    # send the whole payload again, from the top.
    if "fir_progress" in inspect.signature(daemon.set_config).parameters:
        daemon.set_config(payload, fir_progress=fir_progress)
    else:
        # Over the daemon there is no FIR path and no such argument.
        daemon.set_config(payload)
    result: dict[str, Any] = {
        "outputs": len(payload["outputs"]), "corrected": [],
        "gain_requests": {}, "input_gain_requests": {},
        "route_gain_requests": {},
        "fir": [i["index"] for i in payload["inputs"] if "fir" in i]}
    if not (verify_gains and readback):
        return result

    for out in project["outputs"]:
        idx = out["index"]
        if not 0 <= idx < len(readback.amap.outputs):
            continue
        target = float(out.get("gain", 0.0))
        achieved = readback.read_output(idx).get("gain")
        if achieved is None or abs(achieved - target) <= tol:
            continue
        got, request, writes = write_gain_verified(daemon, readback, idx,
                                                   target, tol=tol)
        result["corrected"].append(
            {"output": idx, "target": target, "achieved": got,
             "request": request, "writes": writes})
        # What a save should put in flash for this output. Storing `target`
        # means the device rounds it down again at power-on and the gain
        # comes back a step below where it was tuned.
        result["gain_requests"][idx] = request

    # Inputs and mixer cells hold gains in the same format and truncate them
    # the same way. Only the outputs were being corrected, so those two
    # drifted exactly as the outputs used to -- measured, an input asked for
    # -8.25 dB landed at -8.34 and a cell asked for -6.75 landed at -6.876.
    for inp in project["inputs"]:
        idx = inp["index"]
        if not 0 <= idx < len(readback.amap.inputs):
            continue
        live = readback.read_input(idx)
        target = float(inp.get("gain", 0.0))
        achieved = live.get("gain")
        if achieved is not None and abs(achieved - target) > tol:
            got, request, writes = write_input_gain_verified(
                daemon, readback, idx, target, tol=tol)
            result["corrected"].append(
                {"input": idx, "target": target, "achieved": got,
                 "request": request, "writes": writes})
            result["input_gain_requests"][idx] = request

        cells = {c.get("index"): c.get("gain")
                 for c in live.get("routing", [])}
        for route in inp.get("routing", []):
            dest = route["index"]
            target = float(route.get("gain", 0.0))
            achieved = cells.get(dest)
            if achieved is None or abs(achieved - target) <= tol:
                continue
            got, request, writes = write_route_gain_verified(
                daemon, readback, idx, dest, target, tol=tol)
            result["corrected"].append(
                {"route": [idx, dest], "target": target, "achieved": got,
                 "request": request, "writes": writes})
            result["route_gain_requests"][f"{idx},{dest}"] = request
    return result


def unstable_filters(project: dict[str, Any]) -> list[str]:
    """Bands whose coefficients describe a runaway rather than a filter.

    Only enabled bands are reported: a bypassed one is not in circuit, and
    refusing to write the rest of a configuration over a filter that is
    switched off would be unhelpful.
    """
    rate = int(project.get("rate", 96000))
    bad = []
    for kind in ("inputs", "outputs"):
        for ch in project.get(kind, []):
            name = ch.get("name", kind)
            for i, band in enumerate(ch.get("peq", [])):
                if not band.get("enabled"):
                    continue
                # What would be written, not only what was typed. Checking
                # `manual` alone meant a designed band was never examined,
                # and a design can come out unstable too.
                try:
                    bq = peq_coeff(band, rate)
                except (ValueError, KeyError):
                    bad.append(f"{name} band {band.get('index', i)}")
                    continue
                if not biquad_is_stable(bq):
                    bad.append(f"{name} band {band.get('index', i)}")
            # Crossover groups can hold manual coefficients too -- anything
            # read off the device that did not match a standard alignment is
            # kept verbatim -- and those were not being checked at all, so an
            # unstable one would have gone to a driver unchallenged.
            for i, group in enumerate(ch.get("crossover", [])):
                if not group.get("enabled"):
                    continue
                label = xover_label(group.get("index", i))
                where = f"{name} crossover {label}"
                try:
                    sections = crossover_coeffs(group, rate)
                except (ValueError, KeyError):
                    bad.append(where)
                    continue
                for k, bq in enumerate(sections):
                    if not biquad_is_stable(bq):
                        bad.append(f"{where} section {k + 1}")
    return bad


def demote_device_bypass(project: dict[str, Any]) -> int:
    """Downgrade "read from the device" to "unknown", and say how many.

    A bypass_source of "device" means the stored preset said so, and it is
    saved into the project file like everything else. That makes it a claim
    about a device this session may never have read -- over minidspd the
    stored preset cannot be read at all -- and a live read leaves it
    standing, so a flag learned weeks ago is asserted back at the hardware
    as fact. Called when a read could not confirm it.
    """
    n = 0
    for kind in ("inputs", "outputs"):
        for ch in project.get(kind, []):
            for entry in list(ch.get("peq", [])) + list(
                    ch.get("crossover", [])):
                if entry.get("bypass_source") == "device":
                    entry["bypass_source"] = "unknown"
                    n += 1
            comp = ch.get("compressor")
            if comp and comp.get("bypass_source") == "device":
                comp["bypass_source"] = "unknown"
                n += 1
    return n


def unknown_bypass(project: dict[str, Any]) -> list[str]:
    """Filters whose bypass state the app does not actually know.

    Bypass cannot be read from the hardware -- it is a command with no stored,
    retrievable parameter, which is why miniDSP's own Device Console tracks it
    in a config file rather than querying the device. Anything listed here must
    be resolved before writing, because guessing switches filters on or off.
    """
    out: list[str] = []
    for chan_kind, key in (("output", "outputs"), ("input", "inputs")):
        for chan in project.get(key, []):
            name = chan.get("name", f"{chan_kind} {chan.get('index')}")
            for g in chan.get("crossover", []):
                if g.get("bypass_source", "default") == "unknown":
                    out.append(
                        f"{name} crossover {xover_label(g.get('index', 0))}")
            for b in chan.get("peq", []):
                if b.get("bypass_source", "default") == "unknown":
                    out.append(f"{name} PEQ {b.get('index', 0)}")
            comp = chan.get("compressor")
            if comp and comp.get("bypass_source", "default") == "unknown":
                # A compressor sits in front of a driver and five of its six
                # settings cannot be read back, so a state nobody has
                # established is exactly the thing this list is for.
                out.append(f"{name} compressor")
    return out


# What a section with a pole on the unit circle reads as. It has no finite
# magnitude; this is high enough to read as a fault on any plot and finite
# enough not to poison an average.
RUNAWAY_DB = 120.0


# Where the decoded answer is checked against the original, and how far apart
# they may be. The sweep is spread across the audible band rather than
# concentrated, since the shapes that fool the inverse diverge at the
# extremes. Its steps are a third of an octave, so each check adds a cluster
# around the candidate's own corner: a high-Q band is narrower than one step,
# and comparing two filters only at frequencies where neither of them does
# anything is not a comparison.
DECODE_CHECK_FREQS = [20.0 * (1000.0 ** (i / 31.0)) for i in range(32)]
DECODE_CHECK_TOL_DB = 0.1
# Phase as well as magnitude. Reversing b0 and b2 mirrors a section's zeros
# through the unit circle, which leaves the magnitude response untouched and
# moves the phase by up to eighty degrees -- a different filter that a
# magnitude-only check calls the same one, and phase is what matters at a
# crossover point.
DECODE_CHECK_TOL_DEG = 1.0


def _f32(bq: dict[str, float]) -> dict[str, float]:
    """The section as the device stores it: single precision.

    Coefficients read back have been through float32 already, so comparing
    them against a double-precision redesign measures the rounding and not
    the match. Around a low-frequency high-Q peak that rounding is worth most
    of a dB -- enough at 25 Hz and Q 5 to reject the filter that produced the
    coefficients in the first place.
    """
    return {k: struct.unpack("<f", struct.pack("<f", v))[0]
            for k, v in bq.items()}


def _decode_check_freqs(f0: float, rate: int) -> list[float]:
    """The sweep, plus twelfth-octave steps either side of one corner."""
    nyquist = rate / 2.0
    near = (f0 * 2.0 ** (k / 12.0) for k in range(-12, 13))
    return DECODE_CHECK_FREQS + [f for f in near if 0.0 < f < nyquist]


def _next_f32(value: float, direction: int) -> float:
    """The adjacent single-precision number, above or below.

    Single precision is sign-and-magnitude, so its bit patterns only count
    upwards on the positive side: stepping the raw pattern of a negative
    number walks the exponent instead of the mantissa, and -1.0 becomes -4.0
    rather than -0.99999994. The patterns are mapped to a signed ordering,
    stepped there, and mapped back.
    """
    if value != value or value in (float("inf"), float("-inf")):
        return value
    bits, = struct.unpack("<i", struct.pack("<f", value))
    order = bits if bits >= 0 else -(bits & 0x7FFFFFFF)
    order += direction
    raw = order if order >= 0 else (0x80000000 | -order)
    return struct.unpack("<f", struct.pack("<I", raw & 0xFFFFFFFF))[0]


def _mag_gap(a: dict[str, float], b: dict[str, float],
             freqs: list[float], rate: int) -> float:
    """Worst magnitude difference between two sections, in dB."""
    return max(abs(x - y) for x, y in zip(response_db([a], freqs, rate),
                                          response_db([b], freqs, rate)))


def _phase_gap(a: dict[str, float], b: dict[str, float],
               freqs: list[float], rate: int,
               window_db: float = 20.0) -> float:
    """Worst phase difference in degrees, where either section is audible.

    Phase is undefined at a zero and swings through 180 degrees crossing one,
    so a notch compared against an exact copy of itself disagrees violently
    at whichever grid point lands nearest its null -- and the disagreement
    saturates at the 180 degree wrap, which makes the comparison say nothing.
    Points more than `window_db` below the louder of the two responses are
    left out: twenty decibels into a null nobody is listening to the phase,
    and a shelf mirrored through the unit circle -- the case this check is
    for -- has no null to hide in.
    """
    ma = response_db([a], freqs, rate)
    mb = response_db([b], freqs, rate)
    pa = response_phase([a], freqs, rate)
    pb = response_phase([b], freqs, rate)
    floor = max(max(ma), max(mb)) - window_db
    worst = 0.0
    for x, y, u, v in zip(ma, mb, pa, pb):
        if x < floor or y < floor:
            continue
        worst = max(worst, abs(((u - v + 180.0) % 360.0) - 180.0))
    return worst


def _quantisation_spread(bq: dict[str, float], freqs: list[float],
                         rate: int, phase: bool = False) -> float:
    """How far the response moves when one coefficient moves one step.

    Below a couple of hundred hertz a section's poles crowd up against z = 1,
    and single precision runs out of room to place them: at 25 Hz and Q 10 the
    smallest representable change to a coefficient is worth 2.8 dB at the
    peak. Two sections that far apart are the same section as far as the
    device is concerned, so the decode cannot insist on better agreement than
    that. Higher up it collapses to nothing -- a thousandth of a dB at 1 kHz
    -- which is why this widens the tolerance only where the ambiguity is
    real, and grants a shelf pretending to be a peaking filter under a tenth
    of a dB of slack.
    """
    gap = _phase_gap if phase else _mag_gap
    worst = 0.0
    for key in COEFF_KEYS:
        for direction in (1, -1):
            alt = dict(bq)
            alt[key] = _next_f32(bq[key], direction)
            worst = max(worst, gap(bq, alt, freqs, rate))
    return worst


def _standard_pole(a1: float, a2: float):
    """(cos w0, alpha) for the denominator every non-shelf RBJ type shares.

    Stored coefficients are miniDSP's, whose feedback terms are negated
    against RBJ's: A1 = -a1 and A2 = -a2. Working in RBJ terms from there,
    A2 = (1 - alpha) / (1 + alpha) inverts to alpha directly, and the
    cosine falls out of A1.
    """
    u = -a2
    if abs(1.0 + u) < 1e-12:
        return None
    alpha = (1.0 - u) / (1.0 + u)
    if alpha <= 0:
        return None
    c = a1 / (1.0 + u)
    if not -1.0 < c < 1.0:
        return None
    return c, alpha


def _shelf_candidate(bq: dict[str, float], rate: int, kind: str):
    """(kind, f0, Q, gain) for a shelf, or None.

    A shelf's denominator carries the gain in it -- 2*sqrt(A)*alpha rather
    than alpha -- so the pole cannot be read without knowing A first. It
    can: a low shelf's gain is its response at DC and a high shelf's is its
    response at Nyquist, both of which are one division. With A in hand the
    two normalised feedback terms are two equations in the corner and the
    width, and they solve.
    """
    b0, b1, b2 = bq["b0"], bq["b1"], bq["b2"]
    a1, a2 = bq["a1"], bq["a2"]
    A1, A2 = -a1, -a2
    den = (1.0 + A1 + A2) if kind == "lowshelf" else (1.0 - A1 + A2)
    num = (b0 + b1 + b2) if kind == "lowshelf" else (b0 - b1 + b2)
    if abs(den) < 1e-12:
        return None
    h = num / den
    if h <= 0:
        return None
    gain = 20.0 * math.log10(h)
    if abs(gain) < 1e-6:
        return None                      # a flat shelf is not a shelf
    A = 10.0 ** (gain / 40.0)
    P, M, S = A + 1.0, A - 1.0, 2.0 * math.sqrt(A)
    if abs(M) < 1e-12:
        return None
    # The two shelves do not share a denominator. RBJ's low shelf carries
    # +(A-1)cos and a negated a1; the high shelf carries -(A-1)cos and a
    # positive one. Deriving both from the low-shelf form gave a high shelf
    # that never once matched its own response.
    if kind == "lowshelf":
        bottom = P * (1.0 + A2) + A1 * M
        if abs(bottom) < 1e-12:
            return None
        D = 8.0 * A / bottom
        c = (D * (1.0 + A2) / 2.0 - P) / M
    else:
        bottom = P * (1.0 + A2) - A1 * M
        if abs(bottom) < 1e-12:
            return None
        D = 8.0 * A / bottom
        c = (P - D * (1.0 + A2) / 2.0) / M
    if not -1.0 < c < 1.0:
        return None
    alpha = D * (1.0 - A2) / (2.0 * S)
    if alpha <= 0:
        return None
    w0 = math.acos(c)
    if w0 <= 0:
        return None
    q = math.sin(w0) / (2.0 * alpha)
    if q <= 0:
        return None
    return kind, rate * w0 / (2.0 * math.pi), q, gain


def _peq_candidates(bq: dict[str, float], rate: int):
    """Every reading of this biquad worth checking, best guess first.

    Several RBJ shapes share the b1 == -a1 relationship the original
    inverse keyed on -- a shelf and an all-pass both do -- so a single
    answer was either right or confidently wrong. Offering candidates and
    letting the caller check each against the actual response turns that
    into a search with a verdict.
    """
    out = []
    first = _decode_peq_raw(bq, rate)
    if first:
        out.append(first)

    b0, b1, b2, a1, a2 = (bq["b0"], bq["b1"], bq["b2"], bq["a1"], bq["a2"])
    pole = _standard_pole(a1, a2)
    if pole:
        c, alpha = pole
        w0 = math.acos(c)
        if w0 > 0:
            f0 = rate * w0 / (2.0 * math.pi)
            q = math.sin(w0) / (2.0 * alpha)
            if q > 0:
                # All-pass: unity magnitude, numerator the denominator
                # reversed. Bandpass: no middle term, and the outer two
                # equal and opposite.
                if abs(b2 - 1.0) < 1e-4 and abs(b0 + a2) < 1e-4:
                    out.append(("allpass", f0, q, 0.0))
                if abs(b1) < 1e-6 and abs(b0 + b2) < 1e-6:
                    out.append(("bandpass", f0, q, 0.0))
                out.append(("notch", f0, q, 0.0))

    for kind in ("lowshelf", "highshelf"):
        cand = _shelf_candidate(bq, rate, kind)
        if cand:
            out.append(cand)

    # The peaking reading, offered whatever classify_biquad made of the
    # shape. It decides which branch _decode_peq_raw takes, and at 30 Hz it
    # calls a deep peaking cut a highpass -- so the peaking inverse never
    # ran and the band came back as nothing at all. Guessing wrong is free
    # now that every candidate is checked against the response.
    peak = _peaking_candidate(bq, rate)
    if peak and peak not in out:
        out.append(peak)
    return out


def _peaking_candidate(bq: dict[str, float], rate: int):
    """The peaking/notch reading of a biquad, without asking what shape it
    looks like.

    Writing t = alpha/A and p = alpha*A, both fall out of the feedback and
    numerator terms, and A and alpha follow from their product and ratio.
    """
    b0, b1, b2, a1, a2 = (bq["b0"], bq["b1"], bq["b2"], bq["a1"], bq["a2"])
    if abs(b1 + a1) > max(1e-6, abs(a1) * 1e-4):
        return None
    if abs(1.0 - a2) < 1e-12:
        return None
    t = (1.0 + a2) / (1.0 - a2)
    if t <= 0:
        return None
    c = a1 * (1.0 + t) / 2.0
    if not -1.0 < c < 1.0:
        return None
    w0 = math.acos(c)
    if w0 <= 0:
        return None
    if abs(b0 + b2) < 1e-12:
        return None
    p = (b0 - b2) / (b0 + b2)
    if p <= 0:
        return None
    A = math.sqrt(p / t)
    alpha = math.sqrt(p * t)
    if alpha <= 0 or A <= 0:
        return None
    q = math.sin(w0) / (2.0 * alpha)
    if q <= 0:
        return None
    return ("peaking", rate * w0 / (2.0 * math.pi), q,
            40.0 * math.log10(A))


def _decode_peq_raw(bq: dict[str, float], rate: int):
    """Recover (type, f0, Q, gain_db) from a PEQ biquad, or None.

    Inverts the RBJ design. For a peaking section in miniDSP convention the
    numerator's middle term mirrors the feedback one (b1 == -a1), which
    identifies the family; then, writing t = alpha/A and p = alpha*A:

        t = (1 + a2) / (1 - a2)          from the feedback terms
        p = (b0 - b2) / (b0 + b2)        from the numerator
        A = sqrt(p / t)                  gain,  dB = 40*log10(A)
        alpha = sqrt(p * t)              width, Q = sin(w0) / (2*alpha)

    Without this, filters read off the hardware can only be shown as raw
    coefficients, which is useless for editing.
    """
    if is_bypass(bq):
        return None
    b0, b1, b2, a1, a2 = (bq["b0"], bq["b1"], bq["b2"], bq["a1"], bq["a2"])

    shape = classify_biquad(bq)
    if shape in ("lowpass", "highpass"):
        d = decode_biquad(bq, rate)
        if d:
            return shape, d[0], (d[1] if d[1] is not None else 0.7071), 0.0
        return None

    # Peaking and notch both satisfy b1 == -a1; they differ in whether the
    # numerator is symmetric (notch) or not (peaking with gain).
    if abs(b1 + a1) > max(1e-6, abs(a1) * 1e-4):
        return None
    if abs(1.0 - a2) < 1e-12:
        return None
    t = (1.0 + a2) / (1.0 - a2)
    if t <= 0:
        return None
    c = a1 * (1.0 + t) / 2.0
    if not -1.0 < c < 1.0:
        return None
    w0 = math.acos(c)
    if w0 <= 0:
        return None
    f0 = rate * w0 / (2.0 * math.pi)

    denom = b0 + b2
    if abs(denom) < 1e-15:
        return None
    p = (b0 - b2) / denom
    if p <= 0:
        return "notch", f0, math.sin(w0) / (2.0 * t), 0.0
    A = math.sqrt(p / t)
    alpha = math.sqrt(p * t)
    if alpha <= 0:
        return None
    return "peaking", f0, math.sin(w0) / (2.0 * alpha), 40.0 * math.log10(A)


def decode_peq(bq: dict[str, float], rate: int):
    """(type, f0, Q, gain_db) for a section, or None if it is not one of ours.

    Wraps the inversion in a check: design a filter from each candidate
    reading and compare it with what came in. Several RBJ shapes satisfy the
    b1 == -a1 relationship the inverse keys on -- a shelf and an all-pass both
    do -- so a single answer was either right or confidently wrong, carrying
    parameters that described neither.

    The comparison is of responses, not coefficients. Low-frequency sections
    put their poles and zeros so close to z = 1 that agreeing to five decimal
    places means nothing: a 100 Hz shelf and the peaking filter this inverse
    mistakes it for match to about 1e-5 in every coefficient and still differ
    by 5.5 dB at 20 Hz. What matters is whether the two describe the same
    curve, so that is what is checked -- magnitude and phase both, on both
    sides rounded to the single precision the device stores, and each
    tolerance widened wherever that rounding is worth more than the tolerance
    is.

    Returning None is the honest answer for a section the app cannot express
    as type, frequency, Q and gain; the caller keeps it as raw coefficients,
    which is exactly what the Biquad tab is for.
    """
    ref = _f32(bq)
    best, best_err = None, None
    for guess in _peq_candidates(bq, rate):
        kind, f0, q, gain = guess
        if not (0 < f0 < rate / 2) or not q or q <= 0:
            continue
        try:
            check = design_biquad(kind, f0, q, gain, rate)
        except (ValueError, KeyError):
            continue
        freqs = _decode_check_freqs(f0, rate)
        shape = _f32(check)
        # Magnitude first and phase only if it passes: the widening costs ten
        # more sweeps each time, and magnitude alone throws out most
        # candidates before either is needed.
        err = _mag_gap(ref, shape, freqs, rate)
        if err > DECODE_CHECK_TOL_DB and err > _quantisation_spread(
                ref, freqs, rate):
            continue
        skew = _phase_gap(ref, shape, freqs, rate)
        if skew > DECODE_CHECK_TOL_DEG and skew > _quantisation_spread(
                ref, freqs, rate, phase=True):
            continue
        if best_err is None or err < best_err:
            best, best_err = guess, err
    return best


# ---------------------------------------------------------------------------
# Device Console's own settings store
# ---------------------------------------------------------------------------
#
# The hardware does not report PEQ, routing or bypass to anyone -- verified by
# scanning the entire 16-bit parameter space and the populated flash regions
# for a filter written moments earlier, which appears nowhere. Device Console
# has no privileged access either; it simply always has a config file, written
# beside the device serial:
#
#   <documents>/miniDSP/MiniDSP Device Console/<Model>/SN<nnnnn>/
#       setting/setting<N>.xml
#
# Those files are the same format as a manual export, so finding one lets the
# app populate everything the device cannot report.

# What Device Console writes for the gate and compressor fields it exports.
# Same encodings the device uses, restated here because this module does not
# import the device layer.
XML_GATE_MUTED, XML_GATE_PASSING = 1, 2
XML_COMP_BYPASSED, XML_COMP_ENABLED = 3, 2
COMPRESSOR_FIELDS = ("threshold", "makeup", "ratio", "knee", "attack",
                     "release")

CONSOLE_DIR_NAMES = ("MiniDSP Device Console",)


def _candidate_roots() -> list[Path]:
    """Places a Device Console store might live, including mounted Windows.

    A Windows volume keeps it at <mount>/Users/<name>/Documents, which is one
    level deeper than this used to reach: it offered <mount>/<dir>/Users,
    a path that only matches if somebody is called "Users", so the branch
    could never find the layout it was written for.
    """
    roots = [Path.home() / "Documents", Path.home()]

    def each(path: Path):
        try:
            return [p for p in path.iterdir() if p.is_dir()]
        except (PermissionError, OSError):
            return []

    for base in ("/run/media", "/media", "/mnt"):
        b = Path(base)
        if not b.is_dir():
            continue
        # The mount itself, then one level of user or label directory under
        # it -- and under either, a Windows Users tree.
        for lvl1 in dict.fromkeys([b] + each(b)):
            roots.append(lvl1 / "Documents")
            for name in each(lvl1 / "Users"):
                roots.append(name / "Documents")
            for lvl2 in each(lvl1):
                roots.append(lvl2 / "Documents")
                for name in each(lvl2 / "Users"):
                    roots.append(name / "Documents")
    return roots


