#!/usr/bin/env python3
"""
minidsp_core -- device-facing logic for the miniDSP tuning GUI.

Deliberately free of any GUI toolkit so it can be driven from a desktop app,
a script, or tests.

Three jobs:
  1. Filter design      (RBJ biquads, crossover alignments) and its inverse
  2. Device I/O         (minidspd REST for writes, minidsp CLI for readback)
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

import json
import math
import re
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

HERE = Path(__file__).resolve().parent
ADDRESS_MAPS = HERE / "address_maps"

BYPASS = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}

PEQ_TYPES = ("peaking", "lowshelf", "highshelf", "lowpass", "highpass",
             "notch", "allpass", "bandpass")

ALIGNMENTS = ("linkwitz-riley", "butterworth", "bessel")

# Q values of the 2nd-order sections, used both to design and to identify.
BESSEL_Q = {
    2: [0.5773],
    4: [0.5219, 0.8055],
    6: [0.5103, 0.6112, 1.0234],
    8: [0.5060, 0.5596, 0.7109, 1.2258],
}


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
    if order < 1:
        raise ValueError("order must be >= 1")
    qs = [1.0 / (2.0 * math.cos((2.0 * k + 1.0) * math.pi / (2.0 * order)))
          for k in range(order // 2)]
    return qs, order % 2 == 1


def design_crossover(mode: str, alignment: str, order: int, freq: float,
                     rate: int, max_biquads: int = 4) -> list[dict[str, float]]:
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
        if order not in BESSEL_Q:
            raise ValueError("bessel supports even orders 2-8")
        out += [design_biquad(mode, freq, q, 0.0, rate)
                for q in BESSEL_Q[order]]
    else:
        raise ValueError(f"unknown alignment: {alignment}")

    if len(out) > max_biquads:
        raise ValueError(
            f"{alignment} order {order} needs {len(out)} biquads but only "
            f"{max_biquads} slots exist per crossover group")
    return out


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


def unwrap_deg(degs: Iterable[float]) -> list[float]:
    """Remove the +/-360 jumps, leaving a continuous curve.

    Wrapped phase hides the thing worth looking at. Changing a crossover's
    alignment or order changes the slope of its phase, and a wrapped plot
    chops that slope into disconnected diagonals that all look alike.
    """
    out, offset, prev = [], 0.0, None
    for d in degs:
        if prev is not None:
            if d - prev > 180.0:
                offset -= 360.0
            elif d - prev < -180.0:
                offset += 360.0
        prev = d
        out.append(d + offset)
    return out


def log_freqs(n: int = 240, lo: float = 20.0, hi: float = 20000.0) -> list[float]:
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
    """Rough shape of a biquad from its numerator."""
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

    if abs(b1 - 2 * b0) < rel and abs(b2 - b0) < rel:
        return "lowpass"
    if abs(b1 + 2 * b0) < rel and abs(b2 - b0) < rel:
        return "highpass"
    if abs(b1 + 2 * b0) < rel and abs(b2 - b0) < rel * 10:
        return "notch"
    return "other"


def decode_biquad(bq: dict[str, float], rate: int) -> tuple[float, float] | None:
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
        # Mixed: an odd-order Butterworth is 1st-order plus biquads.
        bw, bw_first = butterworth_qs(order)
        if bw_first and n_first == 1 and matches(qs, bw):
            return "butterworth", order

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
    for line in text.splitlines():
        if line.strip().lower().startswith("biquad"):
            if len(cur) == 5:
                out.append(cur)
            cur = {}
            continue
        m = _REW_COEF.match(line)
        if m:
            cur[m.group(1)] = float(m.group(2))
    if len(cur) == 5:
        out.append(cur)
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

class AddressMap:
    """Per-device float addresses, generated by tools/gen_address_map.py."""

    def __init__(self, doc: dict):
        self.doc = doc
        self.device = doc.get("device", "unknown")
        self.rate = int(doc.get("internal_sampling_rate", 96000))
        self.inputs = doc.get("inputs", [])
        self.outputs = doc.get("outputs", [])

    @classmethod
    def load(cls, device: str) -> "AddressMap | None":
        path = ADDRESS_MAPS / f"{device.lower()}.json"
        if not path.is_file():
            return None
        return cls(json.loads(path.read_text()))

    @staticmethod
    def available() -> list[str]:
        if not ADDRESS_MAPS.is_dir():
            return []
        return sorted(p.stem for p in ADDRESS_MAPS.glob("*.json"))


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

    def session(self) -> requests.Session:
        # requests.Session is not thread-safe, and this object is shared by
        # the meter poller thread and background task threads, so each gets
        # its own session rather than sharing one connection pool.
        if self._local is None:
            self._local = threading.local()
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
            raise DeviceError(f"minidspd unreachable at {self.base}: {exc}")

    def _post(self, url: str, payload: dict):
        try:
            r = self.session().post(url, json=payload, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise DeviceError(f"write failed: {exc}")

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
            raise DeviceError(f"'{self.cli}' not found on PATH")
        except subprocess.TimeoutExpired:
            raise DeviceError("device read timed out")
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

    @staticmethod
    def _delay_ms(raw: float, rate: int) -> float:
        """Delay is a sample count stored in the float's bit pattern."""
        try:
            samples = struct.unpack("<I", struct.pack("<f", raw))[0]
        except (struct.error, OverflowError):
            return 0.0
        if samples > 1_000_000:          # not a plausible sample count
            return 0.0
        return samples * 1000.0 / rate

    def read_output(self, index: int) -> dict[str, Any]:
        spec = self.amap.outputs[index]
        rate = self.amap.rate
        out: dict[str, Any] = {"index": index}

        if "gain" in spec:
            out["gain"] = round(self.floats(spec["gain"], 1)[0], 3)
        if "delay" in spec:
            out["delay"] = round(self._delay_ms(
                self.floats(spec["delay"], 1)[0], rate), 4)

        peq_addrs = spec.get("peq", [])
        peq = []
        if peq_addrs:
            lo, hi = min(peq_addrs), max(peq_addrs)
            block = self.floats(lo, hi - lo + 5)
            for slot, addr in enumerate(peq_addrs):
                off = addr - lo
                coeff = self._biquads(block[off:off + 5])
                peq.append(self._describe(coeff[0] if coeff else dict(BYPASS),
                                          slot, rate))
        out["peq"] = peq

        groups = []
        for gi, base in enumerate(spec.get("xover_groups", [])):
            block = self.floats(base, 20)
            bqs = self._biquads(block)
            groups.append(self._describe_group(bqs, gi, rate))
        out["crossover"] = groups
        return out

    @staticmethod
    def _describe(bq: dict[str, float], slot: int, rate: int) -> dict[str, Any]:
        kind = classify_biquad(bq)
        entry: dict[str, Any] = {
            "index": slot, "coeff": bq, "shape": kind,
            "active": kind not in ("bypass", "unknown"),
        }
        d = decode_biquad(bq, rate)
        if d:
            entry["freq"] = round(d[0], 1)
            entry["q"] = None if d[1] is None else round(d[1], 4)
        return entry

    @staticmethod
    def _describe_group(bqs: list[dict[str, float]], gi: int,
                        rate: int) -> dict[str, Any]:
        sections, shapes = [], []
        for bq in bqs:
            kind = classify_biquad(bq)
            if kind in ("lowpass", "highpass"):
                d = decode_biquad(bq, rate)
                if d:
                    sections.append(d)
                    shapes.append(kind)
        entry: dict[str, Any] = {
            "index": gi,
            "coeff": bqs,
            "sections": len(sections),
            "active": bool(sections),
        }
        if sections:
            alignment, order = identify_alignment(sections)
            entry["mode"] = max(set(shapes), key=shapes.count)
            entry["alignment"] = alignment
            entry["order"] = order
            entry["freq"] = round(sum(f for f, _ in sections) / len(sections), 1)
            entry["qs"] = [None if q is None else round(q, 4)
                           for _, q in sections]
        return entry

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
                peq.append(self._describe(coeff[0] if coeff else dict(BYPASS),
                                          slot, rate))
        out["peq"] = peq
        return out

    def read_all(self, n_outputs: int | None = None) -> list[dict[str, Any]]:
        n = n_outputs if n_outputs is not None else len(self.amap.outputs)
        return [self.read_output(i) for i in range(min(n, len(self.amap.outputs)))]

    def read_inputs(self, n_inputs: int | None = None) -> list[dict[str, Any]]:
        n = n_inputs if n_inputs is not None else len(self.amap.inputs)
        return [self.read_input(i) for i in range(min(n, len(self.amap.inputs)))]


# ---------------------------------------------------------------------------
# Project model -> coefficients
# ---------------------------------------------------------------------------

def default_peq_band(index: int) -> dict[str, Any]:
    return {"index": index, "enabled": False, "type": "peaking",
            "freq": 1000.0, "q": 1.0, "gain": 0.0, "manual": None,
            "bypass_source": "default"}


def default_crossover_group(index: int, mode: str) -> dict[str, Any]:
    return {"index": index, "enabled": False, "mode": mode,
            "alignment": "linkwitz-riley", "order": 4, "freq": 80.0,
            "manual": None, "bypass_source": "default"}


def default_output(index: int, n_peq: int) -> dict[str, Any]:
    return {"index": index, "name": f"Out {index + 1}", "gain": 0.0,
            "mute": False, "invert": False, "delay": 0.0,
            "peq": [default_peq_band(i) for i in range(n_peq)],
            "crossover": [default_crossover_group(0, "highpass"),
                          default_crossover_group(1, "lowpass")]}


def default_input(index: int, n_out: int, n_peq: int) -> dict[str, Any]:
    return {"index": index, "name": f"In {index + 1}", "gain": 0.0,
            "mute": False,
            "peq": [default_peq_band(i) for i in range(n_peq)],
            "routing": [{"index": o, "enabled": o == index, "gain": 0.0}
                        for o in range(n_out)]}


def new_project(n_in: int, n_out: int, n_peq: int, rate: int) -> dict[str, Any]:
    return {"version": 1, "name": "untitled", "rate": rate,
            "inputs": [default_input(i, n_out, n_peq) for i in range(n_in)],
            "outputs": [default_output(i, n_peq) for i in range(n_out)]}


def peq_biquad(band: dict[str, Any], rate: int) -> dict[str, float]:
    """Coefficients for one PEQ band: imported values win over designed ones."""
    if band.get("manual"):
        return dict(band["manual"])
    if not band.get("enabled"):
        return dict(BYPASS)
    return design_biquad(band["type"], band["freq"], band["q"],
                         band.get("gain", 0.0), rate)


def crossover_biquads(group: dict[str, Any], rate: int,
                      slots: int = 4) -> list[dict[str, float]]:
    """Coefficients for one crossover group, padded to `slots`.

    Unused slots are written as explicit passthroughs so stale coefficients
    from a previous tuning can never linger in the hardware.
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
    """Designed coefficients for a band, regardless of its enabled state.

    Whether the band is *in circuit* is carried by the separate bypass flag,
    so the coefficients are written either way. This mirrors how Device
    Console behaves and keeps a disable/re-enable cycle lossless.
    """
    if band.get("manual"):
        return dict(band["manual"])
    try:
        return design_biquad(band["type"], band["freq"], band["q"],
                             band.get("gain", 0.0), rate)
    except (ValueError, KeyError):
        return dict(BYPASS)


def crossover_coeffs(group: dict[str, Any], rate: int,
                     slots: int = 4) -> list[dict[str, float]]:
    """All biquads of one crossover group, padded to `slots`.

    Each biquad carries its own `index`, numbered 0..slots-1 *within the
    group*. minidspd rejects the payload outright without it ("biquad index
    not specified"), and numbering them absolutely across both groups is
    rejected as out of range.
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
                   inputs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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


def apply_device_console_xml(project: dict[str, Any], parsed: dict[str, Any],
                             amap: "AddressMap") -> dict[str, int]:
    """Fold a parsed Device Console export into a project.

    Matching is by DSP address, so this does not depend on any particular
    channel-naming convention.
    """
    filters = parsed["filters"]
    items = parsed["items"]
    stats = {"outputs": 0, "crossover": 0, "peq": 0, "bypassed": 0}

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
        for route in inp.get("routing", []):
            key = f"Mixer_{idx}_{route['index']}_status"
            if key in by_name:
                route["enabled"] = int(by_name[key]) == 2
                stats["routing"] = stats.get("routing", 0) + 1
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
                        tol: float = 0.05) -> tuple[float, int]:
    """Set an output gain and correct for the device's own quantisation.

    The Flex 8 dialect is Float32LE, so minidsp-rs writes the dB value
    verbatim -- yet the value that comes back is always snapped to a linear
    amplitude grid of 1/256, which puts it up to ~0.3 dB away from what was
    asked for. The transform happens inside the device firmware, below
    anything the protocol exposes.

    Rather than model that, this closes the loop: write, read, and re-write
    with the observed error subtracted. Two iterations land within a
    quantisation step of the target, which is the best the hardware can do.

    Returns (achieved_db, writes_performed).
    """
    achieved = None
    request = float(target_db)
    for attempt in range(1, tries + 1):
        daemon.set_config({"outputs": [{"index": output, "gain": request}]})
        achieved = readback.read_output(output)["gain"]
        error = achieved - target_db
        if abs(error) <= tol:
            return achieved, attempt
        # Push the request the other way by the observed error.
        request = max(-127.0, min(0.0, request - error))
    return achieved, tries


def apply_project(daemon: "Daemon", project: dict[str, Any],
                  readback: "Readback | None" = None,
                  verify_gains: bool = False,
                  tol: float = 0.05) -> dict[str, Any]:
    """Push a project to the device, optionally correcting gain quantisation.

    `verify_gains` costs two or three extra round-trips per output that misses
    its target, so it is opt-in: it is worth doing when the user has just
    changed a gain, and wasted when the project's gains came from readback and
    already reflect what the hardware holds.
    """
    payload = build_config_payload(project)
    daemon.set_config(payload)
    result: dict[str, Any] = {"outputs": len(payload["outputs"]),
                              "corrected": []}
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
        got, writes = write_gain_verified(daemon, readback, idx, target,
                                          tol=tol)
        result["corrected"].append(
            {"output": idx, "target": target, "achieved": got,
             "writes": writes})
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
            for i, band in enumerate(ch.get("peq", [])):
                if not band.get("enabled"):
                    continue
                bq = band.get("manual")
                if bq and not biquad_is_stable(bq):
                    bad.append(f"{ch.get('name', kind)} band {band.get('index', i)}")
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


def decode_peq(bq: dict[str, float], rate: int):
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
#   <documents>/miniDSP/MiniDSP Device Console/<Model>/SN<nnnnn>/setting/setting<N>.xml
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
        for lvl1 in b.iterdir() if b.is_dir() else []:
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

    Matches on serial when given: the store is keyed by the last digits of the
    board serial, so 123456 lives under SN23456.
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
    """The file for a preset. Device Console numbers them from one."""
    candidate = settings_dir / f"setting{preset + 1}.xml"
    if candidate.is_file():
        return candidate
    files = sorted(settings_dir.glob("setting*.xml"))
    return files[0] if files else None
