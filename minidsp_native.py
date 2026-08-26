#!/usr/bin/env python3
"""
minidsp_native -- the device layer, spoken directly over USB.

Presents the same surface the application already used when it went through
minidspd, so the UI does not care which is underneath:

    status()        master state and live meters
    set_master()    volume, mute, source, preset
    set_config()    a whole project payload
    read_output()   coefficients, gain, delay for one output
    read_input()    gain and PEQ for one input

Writes use MODE_APPLY (0xa0). The alternative 0x80 is acknowledged by the
device but a mixer disable written with it is silently discarded, which is why
routing changes appeared not to work at all.

License: Apache-2.0
"""

from __future__ import annotations

import threading
from typing import Any

import minidsp_protocol as mp
from minidsp_core import (AddressMap, as_biquad,
                          describe_crossover_group, describe_peq_band,
                          delay_ms_from_raw)

# (hw_id, dsp_version) -> address map name.
#
# The device reports numbers, not a product name, so this is how a map gets
# chosen without a daemon to ask. dsp_version None matches any version of that
# hardware. Entries are added as devices are confirmed against real hardware;
# an unknown device still works if a map is named explicitly.
DEVICE_MAPS: dict[tuple[int, int | None], str] = {
    (30, 110): "flex8",
    (30, 111): "flex8",      # Dirac variant, same DSP layout
    (30, None): "flex8",
}


# Display names, since a map name like "flex8" is not what is on the box.
PRODUCT_NAMES: dict[str, str] = {
    "flex8": "Flex 8", "flex": "Flex", "flexdl": "Flex DL",
    "flexhtx": "Flex HTx", "m2x4hd": "2x4 HD", "ddrc24": "DDRC-24",
    "ddrc88bm": "DDRC-88BM", "shd": "SHD", "c8x12v2": "C-DSP 8x12",
    "m10x10hd": "10x10 HD", "m4x10hd": "4x10 HD", "msharc4x8": "miniSHARC 4x8",
    "nanodigi2x8": "nanoDIGI 2x8", "m2x4": "2x4",
}


def product_name(map_name: str) -> str:
    return PRODUCT_NAMES.get(map_name, map_name)


def map_for(hw_id: int, dsp_version: int | None = None) -> str | None:
    """Address map name for a device that identified itself."""
    for key in ((hw_id, dsp_version), (hw_id, None)):
        if key in DEVICE_MAPS:
            return DEVICE_MAPS[key]
    return None


# Source ordering as the device numbers them. Index is what goes on the wire.
SOURCES = ["analog", "toslink", "spdif", "usb", "bluetooth"]


def open_device(map_name: str | None = None, product_id: int | None = None,
                timeout_ms: int = 1000) -> "NativeDevice":
    """Open whichever miniDSP is attached and load its address map.

    Identification comes from the hardware itself, so no daemon is involved.
    """
    dev = mp.MiniDSP(product_id=product_id, timeout_ms=timeout_ms)
    try:
        info = dev.device_info()
        name = map_name or map_for(info.hw_id, info.dsp_version)
        if name is None:
            raise mp.ProtocolError(
                f"no address map for hw_id {info.hw_id} dsp "
                f"{info.dsp_version}; generate one and pass its name "
                f"explicitly")
        amap = AddressMap.load(name)
        if amap is None:
            raise mp.ProtocolError(f"address map '{name}' not found")
    except Exception:
        dev.close()
        raise
    # The identified connection is handed on rather than closed and reopened.
    # Only one process may hold this interface, so releasing it between
    # identifying the device and using it leaves a gap for something else to
    # claim it -- and it asked the device who it was twice.
    return NativeDevice(amap, connection=dev, info=info)


class NativeDevice:
    """One miniDSP, driven directly.

    All access is serialised: the device has a single command/reply endpoint
    pair, so overlapping requests from the UI thread and a worker thread would
    interleave replies.
    """

    def __init__(self, amap: AddressMap, product_id: int | None = None,
                 timeout_ms: int = 1000,
                 connection: "mp.MiniDSP | None" = None,
                 info: "mp.DeviceInfo | None" = None):
        self.amap = amap
        self.rate = amap.rate
        self._lock = threading.Lock()
        # open_device() passes the connection it already identified; opening
        # one here is the path for a caller that knows which map it wants.
        self._dev = connection or mp.MiniDSP(product_id=product_id,
                                             timeout_ms=timeout_ms)
        self.info = info or self._dev.device_info()

    def close(self) -> None:
        with self._lock:
            self._dev.close()

    # -- reads ------------------------------------------------------------

    def _floats(self, addr: int, count: int) -> list[float]:
        """Read `count` floats, chunked to the device's 14-per-call limit."""
        out: list[float] = []
        while len(out) < count:
            n = min(14, count - len(out))
            out.extend(self._dev.read_floats(addr + len(out), n))
        return out

    def status(self) -> dict[str, Any]:
        with self._lock:
            m = self._dev.master_status()
            ins = [self._dev.read_floats(a, 1)[0]
                   for a in (s.get("meter") for s in self.amap.inputs)
                   if a is not None]
            outs = [self._dev.read_floats(a, 1)[0]
                    for a in (s.get("meter") for s in self.amap.outputs)
                    if a is not None]
        src = m["source"]
        return {
            "master": {
                "preset": m["preset"],
                "source": SOURCES[src].capitalize() if src < len(SOURCES)
                          else str(src),
                "volume": m["volume"],
                "mute": m["mute"],
            },
            "input_levels": ins,
            "output_levels": outs,
            "available_sources": SOURCES,
        }

    def read_output(self, index: int) -> dict[str, Any]:
        spec = self.amap.outputs[index]
        out: dict[str, Any] = {"index": index}
        with self._lock:
            if "gain" in spec:
                out["gain"] = round(self._dev.read_floats(spec["gain"], 1)[0], 3)
            if "delay" in spec:
                raw = self._dev.read_floats(spec["delay"], 1)[0]
                out["delay"] = round(delay_ms_from_raw(raw, self.rate), 4)
            if "enable" in spec:
                _set_gate(out, self._dev.read_ints(spec["enable"], 1)[0])
            if "invert" in spec:
                out["invert"] = bool(self._dev.read_ints(spec["invert"], 1)[0])
            peq_addrs = spec.get("peq", [])
            peq_blocks = {a: self._dev.read_floats(a, 5) for a in peq_addrs}
            xo_blocks = {a: self._floats(a, 20)
                         for a in spec.get("xover_groups", [])}

        out["peq"] = [describe_peq_band(as_biquad(peq_blocks[a]), i, self.rate)
                      for i, a in enumerate(peq_addrs)]
        out["crossover"] = [
            describe_crossover_group([as_biquad(xo_blocks[a][k * 5:(k + 1) * 5])
                             for k in range(4)], gi, self.rate)
            for gi, a in enumerate(spec.get("xover_groups", []))
        ]
        return out

    def read_input(self, index: int) -> dict[str, Any]:
        spec = self.amap.inputs[index]
        out: dict[str, Any] = {"index": index}
        with self._lock:
            if "gain" in spec:
                out["gain"] = round(self._dev.read_floats(spec["gain"], 1)[0], 3)
            if "enable" in spec:
                _set_gate(out, self._dev.read_ints(spec["enable"], 1)[0])
            peq_addrs = spec.get("peq", [])
            blocks = {a: self._dev.read_floats(a, 5) for a in peq_addrs}
        out["peq"] = [describe_peq_band(as_biquad(blocks[a]), i, self.rate)
                      for i, a in enumerate(peq_addrs)]
        return out

    def read_all(self, n: int | None = None) -> list[dict[str, Any]]:
        n = len(self.amap.outputs) if n is None else min(n, len(self.amap.outputs))
        return [self.read_output(i) for i in range(n)]

    def read_inputs(self, n: int | None = None) -> list[dict[str, Any]]:
        n = len(self.amap.inputs) if n is None else min(n, len(self.amap.inputs))
        return [self.read_input(i) for i in range(n)]

    # -- writes -----------------------------------------------------------

    def set_master(self, **fields) -> None:
        with self._lock:
            if "volume" in fields:
                self._dev.set_master_volume(float(fields["volume"]))
            if "mute" in fields:
                self._dev.set_master_mute(bool(fields["mute"]))
            if "source" in fields:
                s = fields["source"]
                idx = SOURCES.index(str(s).lower()) if str(s).lower() in SOURCES \
                    else int(s)
                self._dev.set_source(idx)
            if "preset" in fields:
                self._dev.set_preset(int(fields["preset"]))

    def set_config(self, payload: dict[str, Any]) -> None:
        """Apply a project payload, in the shape the app already builds."""
        self._check_payload(payload)
        with self._lock:
            for out in payload.get("outputs", []):
                spec = self.amap.outputs[out["index"]]
                if "gain" in out and "gain" in spec:
                    self._dev.write_float(spec["gain"], float(out["gain"]))
                if "delay" in out and "delay" in spec:
                    self._dev.write_int(spec["delay"],
                                        _delay_samples(out["delay"], self.rate))
                if "invert" in out and "invert" in spec:
                    self._dev.write_int(spec["invert"], 1 if out["invert"] else 0)
                if "mute" in out and "enable" in spec:
                    self._dev.write_int(spec["enable"], _gate(out["mute"]))

                peq_addrs = spec.get("peq", [])
                for band in out.get("peq", []):
                    addrs = peq_addrs
                    if band["index"] >= len(addrs):
                        continue
                    addr = addrs[band["index"]]
                    self._dev.write_biquad(addr, _coeff_list(band["coeff"]))
                    if band.get("bypass") is not None:
                        self._dev.set_bypass(addr, bool(band["bypass"]))

                for group in out.get("crossover", []):
                    bases = spec.get("xover_groups", [])
                    if group["index"] >= len(bases):
                        continue
                    base = bases[group["index"]]
                    for k, bq in enumerate(group.get("coeff", [])[:4]):
                        self._dev.write_biquad(base + k * 5, _coeff_list(bq))
                    if group.get("bypass") is not None:
                        self._dev.set_bypass(base, bool(group["bypass"]))

            for inp in payload.get("inputs", []):
                spec = self.amap.inputs[inp["index"]]
                if "gain" in inp and "gain" in spec:
                    self._dev.write_float(spec["gain"], float(inp["gain"]))
                if "mute" in inp and "enable" in spec:
                    self._dev.write_int(spec["enable"], _gate(inp["mute"]))
                peq_addrs = spec.get("peq", [])
                for band in inp.get("peq", []):
                    addrs = peq_addrs
                    if band["index"] >= len(addrs):
                        continue
                    addr = addrs[band["index"]]
                    self._dev.write_biquad(addr, _coeff_list(band["coeff"]))
                    if band.get("bypass") is not None:
                        self._dev.set_bypass(addr, bool(band["bypass"]))
                for route in inp.get("routing", []):
                    self._set_route(inp["index"], route)

    def _check_payload(self, payload: dict[str, Any]) -> None:
        """Reject a payload that does not fit this device, before writing.

        A project saved against one model and applied to a smaller one used to
        raise partway through, leaving the device half configured -- some
        channels written, the rest not, and no record of where it stopped.
        Checking first means the write either happens completely or not at
        all.
        """
        for key, specs in (("outputs", self.amap.outputs),
                           ("inputs", self.amap.inputs)):
            for ch in payload.get(key, []):
                idx = ch.get("index")
                if not isinstance(idx, int) or not 0 <= idx < len(specs):
                    raise mp.ProtocolError(
                        f"payload names {key[:-1]} {idx}, but this device has "
                        f"{len(specs)}. The project was probably saved "
                        f"against a different model.")

    def _set_route(self, in_idx: int, route: dict[str, Any]) -> None:
        """Mixer cell, which uses the same 1-off / 2-on gate as a channel."""
        out_idx = route["index"]
        status_addr = _mixer_status_addr(self.amap, in_idx, out_idx)
        gain_addrs = self.amap.inputs[in_idx].get("routing", [])
        self._dev.write_int(status_addr, _gate(not route.get("enabled")))
        if out_idx < len(gain_addrs):
            self._dev.write_float(gain_addrs[out_idx],
                                  float(route.get("gain", 0.0)))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

GATE_MUTED, GATE_PASSING = 1, 2


def _gate(muted: Any) -> int:
    """Channel mute, in the 1-off / 2-on encoding the gate address uses.

    Same convention as a mixer cell, and deliberately not 0/1: a plain zero
    means something else at these addresses.
    """
    return GATE_MUTED if muted else GATE_PASSING


def _set_gate(out: dict[str, Any], raw: int) -> None:
    """Record a gate reading, but only when it says something we understand.

    Leaving the key out is what tells the merge layer the device did not
    report, which is different from it reporting "not muted". Guessing here
    would put a channel's real state and the screen out of step.
    """
    if raw in (GATE_MUTED, GATE_PASSING):
        out["mute"] = raw == GATE_MUTED


def _mixer_status_addr(amap: AddressMap, in_idx: int, out_idx: int) -> int:
    """Where a mixer cell's on/off flag lives.

    The status flags occupy their own block starting at address 0, one per
    cell, laid out input-major: Mixer_0_4_status is 4, Mixer_1_2_status is 10
    on an 8-output device. The generated maps only record the mixer *gain*
    addresses, which sit in the block immediately after.
    """
    return in_idx * len(amap.outputs) + out_idx


def _coeff_list(coeff: dict[str, float]) -> list[float]:
    return [float(coeff.get(k, 0.0)) for k in ("b0", "b1", "b2", "a1", "a2")]


def _delay_samples(value: Any, rate: int) -> int:
    """Delay in samples, from either milliseconds or a Duration mapping.

    build_config_payload emits {'secs', 'nanos'} because that is what the REST
    API demands; the device itself wants a sample count, so both shapes are
    accepted here rather than forking the payload builder.
    """
    if isinstance(value, dict):
        ms = value.get("secs", 0) * 1000.0 + value.get("nanos", 0) / 1e6
    else:
        ms = float(value)
    return max(0, int(round(ms * rate / 1000.0)))
