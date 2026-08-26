#!/usr/bin/env python3
"""
minidsp-gui — a browser front-end for minidsp-rs.

Talks to the `minidspd` REST API, which in turn drives a miniDSP device over
USB. Nothing here speaks USB directly.

    browser <--> minidsp-gui (this) <--> minidspd <--> USB <--> device

Why a local project file?
    minidspd's REST API is *write-only* for DSP configuration: you can POST
    filter coefficients, but `GET /devices/N` returns only master status and
    live meter levels. There is no way to ask the hardware what crossover it
    is currently running. Every miniDSP tool, the official Device Console
    included, therefore treats a config file as the source of truth and pushes
    it to the device. This does the same.

Biquad sign convention (important):
    miniDSP hardware uses the *negated* feedback convention

        y = b0*x + b1*x1 + b2*x2 + a1*y1 + a2*y2        <- note the + signs

    whereas the standard RBJ cookbook uses

        y = b0*x + b1*x1 + b2*x2 - a1*y1 - a2*y2

    So a1/a2 are negated on the way out. This is verified against minidsp-rs's
    own REW test fixture, which asserts a *positive* a1 of 1.9973354 for a
    filter whose textbook a1 would be -1.9973354. Getting this backwards
    produces an unstable filter, not merely a wrong-sounding one.

License: MIT
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from flask import Flask, jsonify, request, send_from_directory

# --------------------------------------------------------------------------
# Biquad filter design
# --------------------------------------------------------------------------

# Filter types that take (freq, q, gain_db) and produce one biquad.
PEQ_TYPES = (
    "peaking",
    "lowshelf",
    "highshelf",
    "lowpass",
    "highpass",
    "notch",
    "allpass",
    "bandpass",
)

# A passthrough biquad: unity gain, no poles.
BYPASS = {"b0": 1.0, "b1": 0.0, "b2": 0.0, "a1": 0.0, "a2": 0.0}


def _emit(b0: float, b1: float, b2: float,
          a0: float, a1: float, a2: float) -> dict[str, float]:
    """Normalise by a0 and convert to miniDSP's negated-feedback convention."""
    if a0 == 0:
        raise ValueError("degenerate filter: a0 == 0")
    return {
        "b0": b0 / a0,
        "b1": b1 / a0,
        "b2": b2 / a0,
        # Negated relative to the RBJ cookbook -- see module docstring.
        "a1": -(a1 / a0),
        "a2": -(a2 / a0),
    }


def design_biquad(kind: str, freq: float, q: float, gain_db: float,
                  rate: int) -> dict[str, float]:
    """Design a single biquad, returned in miniDSP convention.

    Formulas are the Robert Bristow-Johnson audio EQ cookbook.
    """
    if freq <= 0 or freq >= rate / 2:
        raise ValueError(f"frequency {freq} out of range for rate {rate}")
    if q <= 0:
        raise ValueError("Q must be positive")

    w0 = 2.0 * math.pi * freq / rate
    cos_w0 = math.cos(w0)
    sin_w0 = math.sin(w0)
    alpha = sin_w0 / (2.0 * q)

    if kind == "peaking":
        A = 10.0 ** (gain_db / 40.0)
        return _emit(
            1 + alpha * A, -2 * cos_w0, 1 - alpha * A,
            1 + alpha / A, -2 * cos_w0, 1 - alpha / A,
        )

    if kind == "lowshelf":
        A = 10.0 ** (gain_db / 40.0)
        two_sqrtA_alpha = 2.0 * math.sqrt(A) * alpha
        return _emit(
            A * ((A + 1) - (A - 1) * cos_w0 + two_sqrtA_alpha),
            2 * A * ((A - 1) - (A + 1) * cos_w0),
            A * ((A + 1) - (A - 1) * cos_w0 - two_sqrtA_alpha),
            (A + 1) + (A - 1) * cos_w0 + two_sqrtA_alpha,
            -2 * ((A - 1) + (A + 1) * cos_w0),
            (A + 1) + (A - 1) * cos_w0 - two_sqrtA_alpha,
        )

    if kind == "highshelf":
        A = 10.0 ** (gain_db / 40.0)
        two_sqrtA_alpha = 2.0 * math.sqrt(A) * alpha
        return _emit(
            A * ((A + 1) + (A - 1) * cos_w0 + two_sqrtA_alpha),
            -2 * A * ((A - 1) + (A + 1) * cos_w0),
            A * ((A + 1) + (A - 1) * cos_w0 - two_sqrtA_alpha),
            (A + 1) - (A - 1) * cos_w0 + two_sqrtA_alpha,
            2 * ((A - 1) - (A + 1) * cos_w0),
            (A + 1) - (A - 1) * cos_w0 - two_sqrtA_alpha,
        )

    if kind == "lowpass":
        return _emit(
            (1 - cos_w0) / 2, 1 - cos_w0, (1 - cos_w0) / 2,
            1 + alpha, -2 * cos_w0, 1 - alpha,
        )

    if kind == "highpass":
        return _emit(
            (1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2,
            1 + alpha, -2 * cos_w0, 1 - alpha,
        )

    if kind == "notch":
        return _emit(
            1, -2 * cos_w0, 1,
            1 + alpha, -2 * cos_w0, 1 - alpha,
        )

    if kind == "allpass":
        return _emit(
            1 - alpha, -2 * cos_w0, 1 + alpha,
            1 + alpha, -2 * cos_w0, 1 - alpha,
        )

    if kind == "bandpass":
        return _emit(
            alpha, 0, -alpha,
            1 + alpha, -2 * cos_w0, 1 - alpha,
        )

    raise ValueError(f"unknown filter type: {kind}")


def _first_order(kind: str, freq: float, rate: int) -> dict[str, float]:
    """A 1st-order low/high pass, expressed as a biquad with zeroed 2nd terms."""
    k = math.tan(math.pi * freq / rate)
    denom = k + 1.0
    if kind == "lowpass":
        return _emit(k, k, 0.0, denom, k - 1.0, 0.0)
    return _emit(1.0, -1.0, 0.0, denom, k - 1.0, 0.0)


def butterworth_qs(order: int) -> tuple[list[float], bool]:
    """Q values for the 2nd-order sections of a Butterworth of given order.

    Returns (qs, has_first_order). Odd orders need one extra 1st-order section.
    """
    if order < 1:
        raise ValueError("order must be >= 1")
    qs = [
        1.0 / (2.0 * math.cos((2.0 * k + 1.0) * math.pi / (2.0 * order)))
        for k in range(order // 2)
    ]
    return qs, order % 2 == 1


def design_crossover(mode: str, alignment: str, order: int, freq: float,
                     rate: int, max_biquads: int = 4) -> list[dict[str, float]]:
    """Design a crossover as a cascade of biquads.

    mode:      "highpass" | "lowpass"
    alignment: "butterworth" | "linkwitz-riley" | "bessel"
    order:     filter order (2 = 12 dB/oct, 4 = 24 dB/oct, ...)

    Linkwitz-Riley of order N is two cascaded Butterworths of order N/2, which
    is why LR24 is two Q=0.7071 sections rather than one Q=0.5412 pair.
    """
    if mode not in ("highpass", "lowpass"):
        raise ValueError("mode must be highpass or lowpass")

    sections: list[dict[str, float]] = []

    if alignment == "linkwitz-riley":
        if order % 2 != 0:
            raise ValueError("Linkwitz-Riley order must be even")
        half = order // 2
        qs, first = butterworth_qs(half)
        # Cascade the half-order Butterworth twice.
        for _ in range(2):
            for q in qs:
                sections.append(design_biquad(mode, freq, q, 0.0, rate))
            if first:
                sections.append(_first_order(mode, freq, rate))

    elif alignment == "butterworth":
        qs, first = butterworth_qs(order)
        for q in qs:
            sections.append(design_biquad(mode, freq, q, 0.0, rate))
        if first:
            sections.append(_first_order(mode, freq, rate))

    elif alignment == "bessel":
        # Bessel Q values for orders 2, 4, 6, 8 (2nd-order sections).
        table = {
            2: [0.5773],
            4: [0.5219, 0.8055],
            6: [0.5103, 0.6112, 1.0234],
            8: [0.5060, 0.5596, 0.7109, 1.2258],
        }
        if order not in table:
            raise ValueError("bessel supports even orders 2-8")
        for q in table[order]:
            sections.append(design_biquad(mode, freq, q, 0.0, rate))

    else:
        raise ValueError(f"unknown alignment: {alignment}")

    if len(sections) > max_biquads:
        raise ValueError(
            f"{alignment} order {order} needs {len(sections)} biquads, "
            f"but only {max_biquads} are available per crossover group"
        )
    return sections


def response_db(biquads: list[dict[str, float]], freqs: list[float],
                rate: int) -> list[float]:
    """Magnitude response in dB of a cascade, at the given frequencies.

    Coefficients arrive in miniDSP convention, so the feedback terms are
    negated back to textbook form before evaluating.
    """
    out = []
    for f in freqs:
        w = 2.0 * math.pi * f / rate
        z1 = complex(math.cos(-w), math.sin(-w))
        z2 = z1 * z1
        mag = 1.0
        for bq in biquads:
            num = bq["b0"] + bq["b1"] * z1 + bq["b2"] * z2
            den = 1.0 - bq["a1"] * z1 - bq["a2"] * z2
            if abs(den) < 1e-20:
                mag = 0.0
                break
            mag *= abs(num / den)
        out.append(20.0 * math.log10(mag) if mag > 1e-12 else -120.0)
    return out


# --------------------------------------------------------------------------
# REW interchange
# --------------------------------------------------------------------------

_REW_LINE = re.compile(r"^\s*([ab][012])\s*=\s*(-?[\d.eE+-]+)\s*,?\s*$")


def parse_rew_biquads(text: str) -> list[dict[str, float]]:
    """Parse REW's miniDSP biquad export.

        biquad1,
        b0=1.0000,
        b1=-1.9808,
        ...

    REW already emits miniDSP's sign convention, so values pass through
    untouched -- matching how minidsp-rs's own rew.rs parser behaves.
    """
    filters: list[dict[str, float]] = []
    current: dict[str, float] = {}
    for line in text.splitlines():
        if line.strip().lower().startswith("biquad"):
            if len(current) == 5:
                filters.append(current)
            current = {}
            continue
        m = _REW_LINE.match(line)
        if m:
            current[m.group(1)] = float(m.group(2))
    if len(current) == 5:
        filters.append(current)
    return filters


def to_rew_text(biquads: list[dict[str, float]]) -> str:
    """Serialise biquads back to REW's format."""
    chunks = []
    for i, bq in enumerate(biquads, start=1):
        chunks.append(
            f"biquad{i},\n"
            f"b0={bq['b0']:.10f},\n"
            f"b1={bq['b1']:.10f},\n"
            f"b2={bq['b2']:.10f},\n"
            f"a1={bq['a1']:.10f},\n"
            f"a2={bq['a2']:.10f},\n"
        )
    return "\n".join(chunks)


# --------------------------------------------------------------------------
# minidspd client
# --------------------------------------------------------------------------

class DaemonError(RuntimeError):
    pass


@dataclass
class Daemon:
    base: str
    index: int
    timeout: float = 5.0

    def _url(self, suffix: str = "") -> str:
        return f"{self.base}/devices/{self.index}{suffix}"

    def list_devices(self) -> list[dict[str, Any]]:
        try:
            r = requests.get(f"{self.base}/devices", timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise DaemonError(f"cannot reach minidspd at {self.base}: {exc}")

    def status(self) -> dict[str, Any]:
        try:
            r = requests.get(self._url(), timeout=self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise DaemonError(f"status read failed: {exc}")

    def set_master(self, payload: dict[str, Any]) -> None:
        try:
            r = requests.post(self._url(), json=payload, timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise DaemonError(f"master write failed: {exc}")

    def set_config(self, payload: dict[str, Any]) -> None:
        try:
            r = requests.post(self._url("/config"), json=payload,
                              timeout=self.timeout)
            r.raise_for_status()
        except requests.RequestException as exc:
            raise DaemonError(f"config write failed: {exc}")


# --------------------------------------------------------------------------
# Project model
# --------------------------------------------------------------------------

def default_peq_band(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "enabled": False,
        "type": "peaking",
        "freq": 1000.0,
        "q": 1.0,
        "gain": 0.0,
        "manual": None,   # raw biquad dict overrides the designed one
    }


def default_crossover_group(index: int, mode: str) -> dict[str, Any]:
    return {
        "index": index,
        "enabled": False,
        "mode": mode,
        "alignment": "linkwitz-riley",
        "order": 4,
        "freq": 80.0,
        "manual": None,   # list of raw biquads overriding the design
    }


def default_output(index: int, n_peq: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": f"Out {index + 1}",
        "gain": 0.0,
        "mute": False,
        "invert": False,
        "delay": 0.0,
        "peq": [default_peq_band(i) for i in range(n_peq)],
        "crossover": [
            default_crossover_group(0, "highpass"),
            default_crossover_group(1, "lowpass"),
        ],
        "compressor": {
            "enabled": False,
            "threshold": -20.0,
            "ratio": 4.0,
            "attack": 10.0,
            "release": 100.0,
        },
    }


def default_input(index: int, n_out: int, n_peq: int) -> dict[str, Any]:
    return {
        "index": index,
        "name": f"In {index + 1}",
        "gain": 0.0,
        "mute": False,
        "peq": [default_peq_band(i) for i in range(n_peq)],
        # Routing matrix: one entry per output.
        "routing": [
            {"index": o, "enabled": o % max(n_out, 1) == index, "gain": 0.0}
            for o in range(n_out)
        ],
    }


def new_project(n_in: int, n_out: int, n_peq: int, rate: int) -> dict[str, Any]:
    return {
        "version": 1,
        "name": "untitled",
        "rate": rate,
        "inputs": [default_input(i, n_out, n_peq) for i in range(n_in)],
        "outputs": [default_output(i, n_peq) for i in range(n_out)],
    }


class ProjectStore:
    """Holds the working project plus on-disk snapshots."""

    def __init__(self, path: Path, snapshot_dir: Path):
        self.path = path
        self.snapshot_dir = snapshot_dir
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] | None = None

    def load_or_create(self, n_in: int, n_out: int, n_peq: int,
                       rate: int) -> dict[str, Any]:
        if self._data is not None:
            return self._data
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                # Reconcile with the device we actually found.
                if (len(data.get("outputs", [])) == n_out
                        and len(data.get("inputs", [])) == n_in):
                    self._data = data
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        self._data = new_project(n_in, n_out, n_peq, rate)
        self.save()
        return self._data

    @property
    def data(self) -> dict[str, Any]:
        if self._data is None:
            raise RuntimeError("project not loaded")
        return self._data

    def replace(self, data: dict[str, Any]) -> None:
        self._data = data
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2))
        tmp.replace(self.path)

    def snapshot(self, name: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "snapshot"
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.snapshot_dir / f"{stamp}_{safe}.json"
        target.write_text(json.dumps(self._data, indent=2))
        return target.name

    def snapshots(self) -> list[str]:
        return sorted((p.name for p in self.snapshot_dir.glob("*.json")),
                      reverse=True)

    def restore(self, name: str) -> dict[str, Any]:
        target = self.snapshot_dir / name
        if not target.is_file():
            raise FileNotFoundError(name)
        self._data = json.loads(target.read_text())
        self.save()
        return self._data


# --------------------------------------------------------------------------
# Project -> device payload
# --------------------------------------------------------------------------

def peq_to_biquad(band: dict[str, Any], rate: int) -> dict[str, float]:
    if band.get("manual"):
        return dict(band["manual"])
    if not band.get("enabled"):
        return dict(BYPASS)
    return design_biquad(band["type"], band["freq"], band["q"],
                         band["gain"], rate)


def crossover_to_biquads(group: dict[str, Any], rate: int,
                         slots: int = 4) -> list[dict[str, float]]:
    if group.get("manual"):
        designed = [dict(b) for b in group["manual"]]
    elif not group.get("enabled"):
        designed = []
    else:
        designed = design_crossover(group["mode"], group["alignment"],
                                    int(group["order"]), group["freq"], rate,
                                    max_biquads=slots)
    # Pad unused slots with passthrough so stale coefficients never linger.
    while len(designed) < slots:
        designed.append(dict(BYPASS))
    return designed[:slots]


def build_config_payload(project: dict[str, Any]) -> dict[str, Any]:
    """Translate the whole project into one minidspd config POST body."""
    rate = int(project.get("rate", 96000))
    outputs = []
    for out in project["outputs"]:
        entry: dict[str, Any] = {
            "index": out["index"],
            "gain": float(out["gain"]),
            "mute": bool(out["mute"]),
            "invert": bool(out["invert"]),
            "delay": float(out["delay"]),
            "peq": [
                {"index": b["index"], "bypass": False,
                 "coeff": peq_to_biquad(b, rate)}
                for b in out["peq"]
            ],
        }
        xover = []
        for group in out["crossover"]:
            coeffs = crossover_to_biquads(group, rate)
            for slot, coeff in enumerate(coeffs):
                xover.append({
                    "index": group["index"] * 4 + slot,
                    "bypass": False,
                    "coeff": coeff,
                })
        entry["crossover"] = xover
        comp = out.get("compressor") or {}
        if comp.get("enabled"):
            entry["compressor"] = {
                "bypass": False,
                "threshold": float(comp["threshold"]),
                "ratio": float(comp["ratio"]),
                "attack": float(comp["attack"]),
                "release": float(comp["release"]),
            }
        else:
            entry["compressor"] = {"bypass": True}
        outputs.append(entry)

    inputs = []
    for inp in project["inputs"]:
        inputs.append({
            "index": inp["index"],
            "gain": float(inp["gain"]),
            "mute": bool(inp["mute"]),
            "peq": [
                {"index": b["index"], "bypass": False,
                 "coeff": peq_to_biquad(b, rate)}
                for b in inp["peq"]
            ],
            "routing": [
                {"index": r["index"], "enabled": bool(r["enabled"]),
                 "gain": float(r.get("gain", 0.0))}
                for r in inp["routing"]
            ],
        })

    return {"inputs": inputs, "outputs": outputs}


# --------------------------------------------------------------------------
# Flask app
# --------------------------------------------------------------------------

STATIC_DIR = Path(__file__).parent / "static"

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False

_daemon: Daemon
_store: ProjectStore
_topology: dict[str, Any] = {}


def discover_topology() -> dict[str, Any]:
    """Derive channel counts from the device rather than hardcoding a model."""
    devices = _daemon.list_devices()
    if not devices:
        raise DaemonError("minidspd reports no devices")
    if _daemon.index >= len(devices):
        raise DaemonError(
            f"device index {_daemon.index} out of range "
            f"({len(devices)} device(s) present)"
        )
    info = devices[_daemon.index]
    status = _daemon.status()
    return {
        "product_name": info.get("product_name", "unknown"),
        "serial": info.get("version", {}).get("serial"),
        "hw_id": info.get("version", {}).get("hw_id"),
        "dsp_version": info.get("version", {}).get("dsp_version"),
        "n_inputs": len(status.get("input_levels", [])),
        "n_outputs": len(status.get("output_levels", [])),
        "sources": status.get("available_sources", []),
    }


def _err(exc: Exception, code: int = 502):
    return jsonify({"error": str(exc)}), code


@app.get("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.get("/static/<path:name>")
def static_files(name: str):
    return send_from_directory(STATIC_DIR, name)


@app.get("/api/device")
def api_device():
    try:
        topo = dict(_topology)
        topo["rate"] = _store.data.get("rate")
        topo["n_peq"] = len(_store.data["outputs"][0]["peq"]) \
            if _store.data["outputs"] else 0
        return jsonify(topo)
    except Exception as exc:            # noqa: BLE001 - surfaced to the UI
        return _err(exc)


@app.get("/api/status")
def api_status():
    try:
        return jsonify(_daemon.status())
    except DaemonError as exc:
        return _err(exc)


@app.post("/api/master")
def api_master():
    payload = request.get_json(silent=True) or {}
    allowed = {"volume", "mute", "source", "preset"}
    body = {k: v for k, v in payload.items() if k in allowed}
    if not body:
        return jsonify({"error": "no recognised master fields"}), 400
    try:
        _daemon.set_master(body)
        return jsonify({"ok": True})
    except DaemonError as exc:
        return _err(exc)


@app.post("/api/mute-all")
def api_mute_all():
    """Panic control: master mute on, immediately."""
    try:
        _daemon.set_master({"mute": True})
        return jsonify({"ok": True})
    except DaemonError as exc:
        return _err(exc)


@app.get("/api/project")
def api_project_get():
    return jsonify(_store.data)


@app.post("/api/project")
def api_project_post():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "expected a project object"}), 400
    try:
        _store.replace(payload)
        return jsonify({"ok": True})
    except Exception as exc:            # noqa: BLE001
        return _err(exc, 400)


@app.post("/api/preview")
def api_preview():
    """Magnitude response for a channel's filter chain, for plotting."""
    payload = request.get_json(silent=True) or {}
    rate = int(_store.data.get("rate", 96000))
    try:
        biquads: list[dict[str, float]] = []
        for band in payload.get("peq", []):
            biquads.append(peq_to_biquad(band, rate))
        for group in payload.get("crossover", []):
            biquads.extend(
                bq for bq in crossover_to_biquads(group, rate)
                if bq != BYPASS
            )
        n = 240
        freqs = [20.0 * (20000.0 / 20.0) ** (i / (n - 1)) for i in range(n)]
        freqs = [f for f in freqs if f < rate / 2]
        return jsonify({
            "freqs": freqs,
            "db": response_db(biquads, freqs, rate),
        })
    except Exception as exc:            # noqa: BLE001
        return _err(exc, 400)


@app.post("/api/apply")
def api_apply():
    """Push the entire project to the device."""
    try:
        payload = build_config_payload(_store.data)
    except Exception as exc:            # noqa: BLE001
        return _err(exc, 400)
    try:
        _daemon.set_config(payload)
        return jsonify({"ok": True, "outputs": len(payload["outputs"])})
    except DaemonError as exc:
        return _err(exc)


@app.post("/api/import-rew")
def api_import_rew():
    """Load a REW biquad export into a channel's PEQ bank as manual coefficients."""
    payload = request.get_json(silent=True) or {}
    text = payload.get("text", "")
    target = payload.get("target", "output")
    idx = int(payload.get("index", 0))
    filters = parse_rew_biquads(text)
    if not filters:
        return jsonify({"error": "no biquads found in that text"}), 400

    bank = _store.data["outputs" if target == "output" else "inputs"]
    if idx >= len(bank):
        return jsonify({"error": f"{target} {idx} does not exist"}), 400
    peq = bank[idx]["peq"]
    applied = 0
    for slot, coeff in enumerate(filters):
        if slot >= len(peq):
            break
        peq[slot]["manual"] = coeff
        peq[slot]["enabled"] = True
        applied += 1
    _store.save()
    return jsonify({"ok": True, "applied": applied,
                    "skipped": max(0, len(filters) - applied)})


@app.get("/api/snapshots")
def api_snapshots():
    return jsonify({"snapshots": _store.snapshots()})


@app.post("/api/snapshots")
def api_snapshot_create():
    payload = request.get_json(silent=True) or {}
    name = payload.get("name", "snapshot")
    return jsonify({"ok": True, "name": _store.snapshot(name)})


@app.post("/api/snapshots/restore")
def api_snapshot_restore():
    payload = request.get_json(silent=True) or {}
    try:
        data = _store.restore(payload.get("name", ""))
        return jsonify({"ok": True, "project": data})
    except FileNotFoundError:
        return jsonify({"error": "no such snapshot"}), 404


# --------------------------------------------------------------------------

def main() -> int:
    global _daemon, _store, _topology

    ap = argparse.ArgumentParser(
        description="Browser front-end for minidsp-rs (minidspd).")
    ap.add_argument("--daemon", default="http://127.0.0.1:5380",
                    help="minidspd HTTP API base URL")
    ap.add_argument("--device", type=int, default=0,
                    help="device index as reported by minidspd")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to bind the UI to")
    ap.add_argument("--port", type=int, default=8777,
                    help="port to serve the UI on")
    ap.add_argument("--rate", type=int, default=96000,
                    help="DSP internal sample rate (Flex/Flex 8: 96000, "
                         "2x4HD and many others: 48000)")
    ap.add_argument("--peq", type=int, default=10,
                    help="PEQ bands per channel")
    ap.add_argument("--project",
                    default=str(Path.home() / ".config" / "minidsp-gui"
                                / "project.json"),
                    help="path to the project file")
    args = ap.parse_args()

    _daemon = Daemon(args.daemon.rstrip("/"), args.device)

    try:
        _topology = discover_topology()
    except DaemonError as exc:
        print(f"error: {exc}")
        print("\nIs minidspd running? Try:  minidspd -c /path/to/config.toml")
        return 1

    project_path = Path(args.project)
    _store = ProjectStore(project_path, project_path.parent / "snapshots")
    _store.load_or_create(_topology["n_inputs"], _topology["n_outputs"],
                          args.peq, args.rate)

    print(f"Device : {_topology['product_name']} "
          f"(serial {_topology['serial']}, hw_id {_topology['hw_id']}, "
          f"dsp {_topology['dsp_version']})")
    print(f"Channels: {_topology['n_inputs']} in / "
          f"{_topology['n_outputs']} out, {args.peq} PEQ each")
    print(f"Rate   : {args.rate} Hz")
    print(f"Project: {project_path}")
    print(f"UI     : http://{args.host}:{args.port}")

    app.run(host=args.host, port=args.port, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
