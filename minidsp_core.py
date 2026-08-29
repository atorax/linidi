#!/usr/bin/env python3
"""
minidsp_core -- device-facing logic for the miniDSP tuning GUI.

Deliberately free of any GUI toolkit so it can be driven from a desktop app,
a script, or tests.

Three jobs:
  1. Filter design      (RBJ biquads, crossover alignments) and its inverse
  2. Device I/O         (the minidspd fallback; the direct USB path lives in
                         minidsp_protocol and minidsp_native)
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
import json
import math
import re
import struct
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

HERE = Path(__file__).resolve().parent
ADDRESS_MAPS = HERE / "address_maps"

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
# Derived from the reverse Bessel polynomial rather than transcribed: its roots
# give the poles, each conjugate pair becomes a section of Q = |p| / 2|Re p| at
# w0 = |p|, and the whole set is scaled so the cascade is -3 dB at 1. An
# earlier transcription had Q values that were right and ratios that were not,
# which passed a check at the corner and was up to 17 dB adrift in the
# stopband at 8th order -- so this is checked against the analog response
# across the band, not just at the corner.
BESSEL_SECTIONS: dict[int, list[tuple[float, float]]] = {
    2: [(0.5774, 1.2720)],
    3: [(0.6910, 1.3384)],
    4: [(0.5219, 1.5196), (0.8055, 1.3554)],
    5: [(0.5635, 1.5305), (0.9165, 1.3569)],
    6: [(0.5103, 1.6065), (0.6112, 1.5255), (1.0233, 1.3528)],
    7: [(0.5324, 1.6081), (0.6608, 1.5145), (1.1263, 1.3467)],
    8: [(0.5060, 1.6491), (0.5596, 1.6008),
        (0.7109, 1.5016), (1.2257, 1.3400)],
}

# An odd order has one real pole as well, which becomes a 1st-order section at
# this ratio.
BESSEL_FIRST: dict[int, float] = {3: 1.4648, 5: 1.5855, 7: 1.6386}

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
        out += [design_biquad(mode, bessel_section_freq(freq, ratio, mode),
                              q, 0.0, rate)
                for q, ratio in BESSEL_SECTIONS[order]]
        if order in BESSEL_FIRST:
            out.append(_first_order(
                mode, bessel_section_freq(freq, BESSEL_FIRST[order], mode),
                rate))
    else:
        raise ValueError(f"unknown alignment: {alignment}")

    if len(out) > max_biquads:
        raise ValueError(
            f"{alignment} order {order} needs {len(out)} biquads but only "
            f"{max_biquads} slots exist per crossover group")
    return out


def bessel_section_freq(corner: float, ratio: float, mode: str) -> float:
    """Where one Bessel section sits, relative to the cascade's corner.

    The low-pass prototype places each section above the corner by its ratio;
    the high-pass transformation inverts that, so the sections sit below it.
    """
    return corner / ratio if mode == "highpass" else corner * ratio


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
                mag = 0.0
                break
            mag *= abs((bq["b0"] + bq["b1"] * z1 + bq["b2"] * z2) / den)
        out.append(20.0 * math.log10(mag) if mag > 1e-12 else -120.0)
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
    return [lo * (hi / lo) ** (i / (n - 1)) for i in range(n)]


# ---------------------------------------------------------------------------
# Filter identification (the inverse problem)
# ---------------------------------------------------------------------------

def is_bypass(bq: dict[str, float], tol: float = 1e-9) -> bool:
    return (abs(bq["b0"] - 1.0) < tol
            and all(abs(bq[k]) < tol for k in ("b1", "b2", "a1", "a2")))


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


def biquad_gain_db(bq: dict[str, float], freq: float, rate: int) -> float:
    """Magnitude response of one section at one frequency, in dB."""
    return response_db([bq], [freq], rate)[0]


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
        # Purely 1st-order. Two cascaded at one corner is LR12; one alone is
        # a 6 dB/oct Butterworth.
        return ("linkwitz-riley", 2) if n_first == 2 else ("butterworth",
                                                           n_first)

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


def to_rew_text(biquads: Iterable[dict[str, float]]) -> str:
    parts = []
    for i, bq in enumerate(biquads, 1):
        parts.append(
            f"biquad{i},\nb0={bq['b0']:.10f},\nb1={bq['b1']:.10f},\n"
            f"b2={bq['b2']:.10f},\na1={bq['a1']:.10f},\na2={bq['a2']:.10f},\n")
    return "\n".join(parts)


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
    decoded = decode_peq(bq, rate)
    if decoded:
        entry["type"], entry["freq"], entry["q"], entry["gain"] = decoded
    return entry


def describe_crossover_group(bqs: list[dict[str, float]], index: int,
                             rate: int) -> dict[str, Any]:
    """One crossover group: which sections are real, and what they make."""
    sections, shapes = [], []
    for bq in bqs:
        kind = classify_biquad(bq)
        if kind in ("lowpass", "highpass"):
            decoded = decode_biquad(bq, rate)
            if decoded:
                sections.append(decoded)
                shapes.append(kind)
    entry: dict[str, Any] = {"index": index, "coeff": bqs,
                             "sections": len(sections),
                             "active": bool(sections)}
    if sections:
        alignment, order = identify_alignment(sections)
        mode = max(set(shapes), key=shapes.count)
        entry["mode"] = mode
        entry["alignment"] = alignment
        entry["order"] = order
        entry["freq"] = round(crossover_corner(sections, alignment, order,
                                               mode), 1)
        entry["qs"] = [None if q is None else round(q, 4) for _, q in sections]
    return entry


def crossover_corner(sections: list[tuple[float, float]], alignment: str,
                     order: int, mode: str) -> float:
    """The corner a set of sections was designed around.

    For Butterworth and Linkwitz-Riley every section sits at the corner, so
    the average is the corner. A Bessel's sections are spread by a fixed ratio
    each, so averaging them lands well above it -- an order 4 designed at
    1000 Hz averages 1505 -- and reading a crossover back would have moved it.
    Each section is put back where it came from first.
    """
    ratios = BESSEL_SECTIONS.get(order) if alignment == "bessel" else None
    if not ratios:
        return sum(f for f, _ in sections) / len(sections)

    corners = []
    for f0, q in sections:
        if q is None:
            continue
        # Pair each section with the ratio belonging to its Q.
        _, ratio = min(ratios, key=lambda rq: abs(rq[0] - q))
        corners.append(f0 * ratio if mode == "highpass" else f0 / ratio)
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
    _local: Any = None

    def __post_init__(self) -> None:
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
        self._post(self._url("/config"), payload)


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
# Four of the six do not read back, so a project is the only record of what
# was asked for -- the device will take the write and then decline to say
# what it holds. That is also why `enabled` defaults to False: a compressor
# sits in front of a driver, and nothing should put one into circuit except
# somebody deciding to.
def default_compressor() -> dict[str, Any]:
    return {"enabled": False, "threshold": -30.0, "makeup": 0.0,
            "ratio": 4.0, "knee": 20.0, "attack": 40.0, "release": 100.0,
            "bypass_source": "default"}


def default_output(index: int, n_peq: int) -> dict[str, Any]:
    return {"index": index, "name": f"Out {index + 1}", "gain": 0.0,
            "mute": False, "invert": False, "delay": 0.0,
            "peq": [default_peq_band(i, n_peq) for i in range(n_peq)],
            "crossover": [default_crossover_group(0, "highpass"),
                          default_crossover_group(1, "lowpass")],
            "compressor": default_compressor()}


def default_input(index: int, n_out: int, n_peq: int) -> dict[str, Any]:
    return {"index": index, "name": f"In {index + 1}", "gain": 0.0,
            "mute": False,
            "peq": [default_peq_band(i, n_peq) for i in range(n_peq)],
            "routing": [{"index": o, "enabled": o == index, "gain": 0.0}
                        for o in range(n_out)]}


def new_project(n_in: int, n_out: int, n_peq: int,
                rate: int) -> dict[str, Any]:
    return {"version": 1, "name": "untitled", "rate": rate,
            "inputs": [default_input(i, n_out, n_peq) for i in range(n_in)],
            "outputs": [default_output(i, n_peq) for i in range(n_out)]}


def peq_biquad(band: dict[str, Any], rate: int) -> dict[str, float]:
    """What a band contributes to the response *as things stand*.

    A band that is switched off contributes nothing, so this returns a
    passthrough for it. That makes this the right function for drawing and
    the wrong one for writing -- see peq_coeff, which returns the designed
    coefficients whether or not the band is currently in circuit.
    """
    if band.get("manual"):
        return dict(band["manual"])
    if not band.get("enabled"):
        return dict(BYPASS)
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
    if group.get("manual"):
        designed = [dict(b) for b in group["manual"]]
    elif not group.get("enabled"):
        designed = []
    else:
        designed = design_crossover(group["mode"], group["alignment"],
                                    int(group["order"]), group["freq"],
                                    rate, max_biquads=slots)
    designed += [dict(BYPASS)] * (slots - len(designed))
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
    except (ValueError, KeyError):
        return dict(BYPASS)


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
    else:
        try:
            designed = design_crossover(group["mode"], group["alignment"],
                                        int(group["order"]), group["freq"],
                                        rate, max_biquads=slots)
        except (ValueError, KeyError):
            designed = []
    designed += [dict(BYPASS)] * (slots - len(designed))
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
    return {"index": band["index"], "coeff": peq_coeff(band, rate),
            **_bypass_field(band)}


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
            "gain": float(out["gain"]),
            "mute": bool(out["mute"]),
            "invert": bool(out.get("invert", False)),
            "delay": ms_to_duration(out.get("delay", 0.0)),
            "peq": [_peq_entry(b, rate) for b in out["peq"]],
            "crossover": [_crossover_entry(g, rate)
                          for g in out.get("crossover", [])],
        }
        comp = out.get("compressor")
        if comp:
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
                         "gain": float(r.get("gain", 0.0))}
                        for r in inp.get("routing", [])],
        })
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
        if slot >= len(dst_bands):
            break
        dst = dst_bands[slot]
        unknown = dst.get("bypass_source") not in ("import", "user")
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
            dst["manual"] = None
            continue
        decoded = decode_peq(band["coeff"], rate)
        if decoded:
            kind, f0, q, gain = decoded
            dst.update(type=kind, freq=round(f0, 1), q=round(q, 4),
                       gain=round(gain, 2), manual=None)
        else:
            dst["manual"] = band["coeff"]
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
        if idx >= len(project["outputs"]):
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

        for gi, g in enumerate(r.get("crossover", [])):
            if gi >= len(out["crossover"]):
                break
            dst = out["crossover"][gi]
            # Coefficients are readable; bypass is not. Only mark it unknown
            # if the project has not already learned it from a config file --
            # a read should add knowledge, never destroy it.
            if dst.get("bypass_source") not in ("import", "user"):
                # We can read the coefficients but not whether the filter is
                # engaged. Do not claim it is: showing an unknown crossover as
                # active draws a band-pass that may not exist. Parameters are
                # still populated so they can be seen and resolved.
                dst["bypass_source"] = "unknown"
                dst["enabled"] = False
            if not g.get("active"):
                dst["manual"] = None
                continue
            if g.get("alignment") in ALIGNMENTS:
                dst.update(mode=g["mode"], alignment=g["alignment"],
                           order=g["order"], freq=g["freq"], manual=None)
            else:
                dst["alignment"] = "custom"
                dst["manual"] = g["coeff"]

        _apply_peq_readback(out["peq"], r.get("peq", []), rate)

    for r in (inputs or []):
        idx = r["index"]
        if idx >= len(project["inputs"]):
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
            if live and "gain" in live:
                route["gain"] = live["gain"]
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
        if slot >= len(dst_bands):
            break
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
            dst["manual"] = band["coeff"]
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
                if not same(r[key], s[key], tol):
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
                    if not same(cell["gain"], t["gain"], 0.02):
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
                            f"{label} {n} crossover {grp['index'] + 1} "
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
        if idx >= len(project["outputs"]):
            continue
        out = project["outputs"][idx]
        stats["outputs"] += 1
        if readable:
            for key in ("gain", "delay", "mute", "invert"):
                if key in src:
                    out[key] = src[key]

        for group in src.get("crossover", []):
            gi = group["index"]
            if gi >= len(out["crossover"]):
                break
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
                elif group.get("active"):
                    dst["alignment"] = "custom"
                    dst["manual"] = group["coeff"]
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
        if idx >= len(project["inputs"]):
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
            if readable and "gain" in found:
                route["gain"] = found["gain"]
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
    for chans in (project["outputs"], project["inputs"]):
        for ch in chans:
            for entry in ch.get("peq", []):
                _mark_imported(entry)
            for entry in ch.get("crossover", []):
                _mark_imported(entry)
            if ch.get("compressor"):
                ch["compressor"]["bypass_source"] = "import"
    return stats


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
            if "mode" in f:
                dst["mode"] = f["mode"]
                dst["alignment"] = f["alignment"]
                dst["order"] = f["order"]
                dst["freq"] = f["freq"]

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
            if kind:
                dst["type"] = kind
                dst["freq"] = f["freq"]
                dst["q"] = f["q"] or 0.7071
                dst["gain"] = f["gain"]
            elif "coeff" in f:
                dst["manual"] = f["coeff"]

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
            if kind:
                dst["type"] = kind
                dst["freq"] = f["freq"]
                dst["q"] = f["q"] or 0.7071
                dst["gain"] = f["gain"]
            elif "coeff" in f:
                dst["manual"] = f["coeff"]

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

    Rather than model it, this closes the loop: write, read, and re-write
    with the observed error subtracted. Three iterations usually land within
    a quantisation step of the target, which is the best the hardware can do.

    If none of them land, the device is left on whichever attempt came
    closest, not on whichever happened to be last: the correction can
    overshoot, so the final attempt is sometimes worse than one before it,
    and stopping there would leave the output further from its target than an
    earlier write already had it.

    Returns (achieved_db, request_db, writes_performed). The request is the
    value that had to be *written* to land on achieved, and it matters as
    much as the result: the device applies the same rounding when it loads a
    preset at power-on, so storing the target rather than the request means
    the gain comes back a step lower than it was tuned to. Whatever gets
    saved to flash should be this, not the target.
    """
    def put(db: float) -> float:
        daemon.set_config({"outputs": [{"index": output, "gain": db}]})
        return readback.read_output(output)["gain"]

    request = float(target_db)
    best_request = best_achieved = None
    writes = 0
    for _ in range(tries):
        achieved = put(request)
        writes += 1
        error = achieved - target_db
        if (best_achieved is None
                or abs(error) < abs(best_achieved - target_db)):
            best_request, best_achieved = request, achieved
        if abs(error) <= tol:
            return achieved, request, writes
        # Push the request the other way by the observed error.
        request = max(-127.0, min(0.0, request - error))

    if best_request is not None and abs(achieved - target_db) > abs(
            best_achieved - target_db):
        achieved = put(best_request)
        writes += 1
        return achieved, best_request, writes
    return achieved, request, writes


def apply_project(daemon: "Daemon", project: dict[str, Any],
                  readback: "Readback | None" = None,
                  verify_gains: bool = True,
                  tol: float = 0.05) -> dict[str, Any]:
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
    daemon.set_config(payload)
    result: dict[str, Any] = {"outputs": len(payload["outputs"]),
                              "corrected": [], "gain_requests": {}}
    if not (verify_gains and readback):
        return result

    for out in project["outputs"]:
        idx = out["index"]
        if idx >= len(readback.amap.outputs):
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
    return result


def unstable_filters(project: dict[str, Any]) -> list[str]:
    """Bands whose coefficients describe a runaway rather than a filter.

    Only enabled bands are reported: a bypassed one is not in circuit, and
    refusing to write the rest of a configuration over a filter that is
    switched off would be unhelpful.
    """
    bad = []
    for kind in ("inputs", "outputs"):
        for ch in project.get(kind, []):
            name = ch.get("name", kind)
            for i, band in enumerate(ch.get("peq", [])):
                if not band.get("enabled"):
                    continue
                bq = band.get("manual")
                if bq and not biquad_is_stable(bq):
                    bad.append(f"{name} band {band.get('index', i)}")
            # Crossover groups can hold manual coefficients too -- anything
            # read off the device that did not match a standard alignment is
            # kept verbatim -- and those were not being checked at all, so an
            # unstable one would have gone to a driver unchallenged.
            for i, group in enumerate(ch.get("crossover", [])):
                if not group.get("enabled"):
                    continue
                for k, bq in enumerate(group.get("manual") or []):
                    if not biquad_is_stable(bq):
                        bad.append(
                            f"{name} crossover {group.get('index', i) + 1} "
                            f"section {k + 1}")
    return bad


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
                    out.append(f"{name} crossover {g.get('index', 0) + 1}")
            for b in chan.get("peq", []):
                if b.get("bypass_source", "default") == "unknown":
                    out.append(f"{name} PEQ {b.get('index', 0) + 1}")
    return out


# Where the decoded answer is checked against the original, and how far apart
# they may be. Spread across the audible band rather than concentrated, since
# the shapes that fool the inverse diverge at the extremes.
DECODE_CHECK_FREQS = [20.0 * (1000.0 ** (i / 31.0)) for i in range(32)]
DECODE_CHECK_TOL_DB = 0.1


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

    Wraps the inversion in a check: design a filter from the answer and
    compare it with what came in. The inverse only covers the pass filters and
    the peaking/notch pair, but several shapes it does not cover satisfy the
    same b1 == -a1 relationship it keys on -- a shelf and an all-pass both do
    -- so without this it reported them confidently as peaking or notch, with
    parameters that describe neither.

    The comparison is of responses, not coefficients. Low-frequency sections
    put their poles and zeros so close to z = 1 that agreeing to five decimal
    places means nothing: a 100 Hz shelf and the peaking filter this inverse
    mistakes it for match to about 1e-5 in every coefficient and still differ
    by 5.5 dB at 20 Hz. What matters is whether the two describe the same
    curve, so that is what is checked.

    Returning None is the honest answer for a section the app cannot express
    as type, frequency, Q and gain; the caller keeps it as raw coefficients,
    which is exactly what the Biquad tab is for.
    """
    guess = _decode_peq_raw(bq, rate)
    if guess is None:
        return None
    kind, f0, q, gain = guess
    if not (0 < f0 < rate / 2) or not q or q <= 0:
        return None
    try:
        check = design_biquad(kind, f0, q, gain, rate)
    except ValueError:
        return None
    a = response_db([bq], DECODE_CHECK_FREQS, rate)
    b = response_db([check], DECODE_CHECK_FREQS, rate)
    if max(abs(x - y) for x, y in zip(a, b)) > DECODE_CHECK_TOL_DB:
        return None
    return guess


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

CONSOLE_DIR_NAMES = ("MiniDSP Device Console",)


def _candidate_roots() -> list[Path]:
    """Places a Device Console store might live, including mounted Windows."""
    roots = [Path.home() / "Documents", Path.home()]
    for base in ("/run/media", "/media", "/mnt"):
        b = Path(base)
        if not b.is_dir():
            continue
        for lvl1 in b.iterdir():
            try:
                if not lvl1.is_dir():
                    continue
                roots.append(lvl1 / "Documents")
                for lvl2 in lvl1.iterdir():
                    if lvl2.is_dir():
                        roots.append(lvl2 / "Documents")
                        roots.append(lvl2 / "Users")
            except (PermissionError, OSError):
                continue
    return roots


def find_console_settings(serial: int | None = None,
                          extra: Path | None = None) -> list[Path]:
    """Settings directories Device Console has written, newest first.

    Matches on serial when given: the store is keyed by the last five digits
    of the board serial, so a unit with serial 123456 lives under SN23456.
    """
    found: list[Path] = []
    roots = _candidate_roots()
    if extra:
        roots.insert(0, Path(extra))
    tail = f"{serial % 100000:05d}" if serial else None

    for root in roots:
        if not root.is_dir():
            continue
        for name in CONSOLE_DIR_NAMES:
            for base in (root / "miniDSP" / name, root / name):
                if not base.is_dir():
                    continue
                try:
                    for model in base.iterdir():
                        if not model.is_dir():
                            continue
                        for sn in model.iterdir():
                            if not sn.is_dir() or not sn.name.startswith("SN"):
                                continue
                            if tail and not sn.name.endswith(tail):
                                continue
                            setting = sn / "setting"
                            if setting.is_dir() and any(setting.glob("*.xml")):
                                found.append(setting)
                except (PermissionError, OSError):
                    continue
    found.sort(key=lambda p: max((f.stat().st_mtime
                                  for f in p.glob("*.xml")), default=0),
               reverse=True)
    return found


def console_setting_file(settings_dir: Path, preset: int) -> Path | None:
    """The file for one preset, or None. Device Console numbers them from one.

    Only the file for the preset asked for. There used to be a fallback to
    the first file in the directory, which meant a device sitting on preset 3
    with only settings 1 and 2 saved quietly loaded preset 1 -- and what is
    loaded from here is bypass state and PEQ contents, which the app treats as
    authoritative precisely because the hardware cannot report them. Applying
    afterwards would then have written one preset's tuning onto another.

    Returning None is the honest answer; the caller says no settings file was
    found and offers Import XML, which is a choice the user makes knowingly.
    """
    candidate = settings_dir / f"setting{preset + 1}.xml"
    return candidate if candidate.is_file() else None
