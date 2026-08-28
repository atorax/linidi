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

import minidsp_flash as mf
import minidsp_protocol as mp
from minidsp_protocol import MAX_FLOATS_PER_READ
from minidsp_core import (COEFF_KEYS, MAX_DELAY_SAMPLES,
                          XOVER_SLOTS, AddressMap, as_biquad,
                          delay_ms_from_raw,
                          describe_crossover_group,
                          describe_peq_band)

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
        # Located preset blocks, filled in on first use. None means "not
        # looked yet", which is different from an empty list meaning "looked
        # and this device has none".
        self._slots: list[mf.Slot] | None = None
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
        """Read `count` floats from filter memory, in whole biquads.

        Reads into filter memory are aligned down to a biquad boundary by the
        device: asking for base+14 returns the block starting at base+10.
        Chunking at the device's 14-float limit therefore asked for an address
        it would not honour, and the answer overlapped what had already been
        read -- a crossover group's third and fourth sections came back as a
        copy of its second. Nothing showed it while the only groups in use
        were two sections long and fitted inside the first chunk.

        Chunks are whole biquads, so every request is aligned by construction.
        `addr` is expected to be a block base, which is what the address maps
        record.
        """
        step = (MAX_FLOATS_PER_READ // BIQUAD_FLOATS) * BIQUAD_FLOATS
        out: list[float] = []
        while len(out) < count:
            n = min(step, count - len(out))
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
                out["gain"] = round(
                    self._dev.read_floats(spec["gain"], 1)[0], 3)
            if "delay" in spec:
                raw = self._dev.read_floats(spec["delay"], 1)[0]
                out["delay"] = round(delay_ms_from_raw(raw, self.rate), 4)
            if "enable" in spec:
                _set_gate(out, self._dev.read_ints(spec["enable"], 1)[0])
            if "invert" in spec:
                out["invert"] = bool(self._dev.read_ints(spec["invert"], 1)[0])
            peq_addrs = spec.get("peq", [])
            peq_blocks = {a: self._dev.read_floats(a, 5) for a in peq_addrs}
            xo_blocks = {a: self._floats(a, XOVER_SLOTS * 5)
                         for a in spec.get("xover_groups", [])}

        out["peq"] = [describe_peq_band(as_biquad(peq_blocks[a]), i, self.rate)
                      for i, a in enumerate(peq_addrs)]
        out["crossover"] = [
            describe_crossover_group(
                [as_biquad(xo_blocks[a][k * 5:(k + 1) * 5])
                 for k in range(XOVER_SLOTS)], gi, self.rate)
            for gi, a in enumerate(spec.get("xover_groups", []))
        ]
        return out

    def read_input(self, index: int) -> dict[str, Any]:
        spec = self.amap.inputs[index]
        out: dict[str, Any] = {"index": index}
        with self._lock:
            if "gain" in spec:
                out["gain"] = round(
                    self._dev.read_floats(spec["gain"], 1)[0], 3)
            if "enable" in spec:
                _set_gate(out, self._dev.read_ints(spec["enable"], 1)[0])
            peq_addrs = spec.get("peq", [])
            blocks = {a: self._dev.read_floats(a, 5) for a in peq_addrs}
        out["peq"] = [describe_peq_band(as_biquad(blocks[a]), i, self.rate)
                      for i, a in enumerate(peq_addrs)]
        return out

    # -- the stored preset -------------------------------------------------

    def preset_slots(self, force: bool = False,
                     progress: Any = None) -> list[mf.Slot]:
        """Where this device keeps its presets, located once and remembered.

        Locating means reading a header-sized window at every 256-byte
        boundary of the part, which takes about twenty-five seconds on a Flex
        8. The answer only changes if the firmware is rewritten, so it is kept
        under the device's own identity and reused.
        """
        key = mf.device_key(self.info)
        if not force:
            if self._slots is None:
                self._slots = mf.load_slots(key)
            if self._slots:
                return self._slots
        with self._lock:
            size = mf.flash_size(self._dev)
            blocks = mf.scan_blocks(self._dev, end=size, progress=progress)
        self._slots = mf.pair_slots(blocks)
        if self._slots:
            mf.save_slots(key, self._slots)
        return self._slots

    def preset_slots_known(self) -> bool:
        """Whether the block map is already in hand, so a read will be quick.

        Lets a caller warn before a first read, which has to scan the part,
        rather than appearing to hang for twenty-five seconds.
        """
        if self._slots:
            return True
        return bool(mf.load_slots(mf.device_key(self.info)))

    def read_stored_preset(self, index: int = 0, force: bool = False,
                           progress: Any = None) -> mf.StoredPreset:
        """The preset the device loads at power-on, decoded.

        Not the same question as read_output(): this is what is stored, and a
        parameter written live changes what is running without touching it.
        """
        slots = self.preset_slots(force=force)
        if not 0 <= index < len(slots):
            raise mp.ProtocolError(
                f"preset {index} was asked for, but {len(slots)} preset "
                f"slots were found in this device's flash")
        with self._lock:
            return mf.read_preset(self._dev, slots[index], progress)

    def stored_config(self, index: int = 0,
                      preset: mf.StoredPreset | None = None
                      ) -> dict[str, Any]:
        """A whole stored preset in the shape the rest of the app speaks.

        Carries three things a live read cannot produce at all: filter
        coefficients, which read back as zero; per-filter bypass flags, which
        live nowhere in parameter memory; and mixer gates, which answer with a
        constant whatever the routing really is.
        """
        p = preset if preset is not None else self.read_stored_preset(index)
        ins: list[dict[str, Any]] = []
        for i, spec in enumerate(self.amap.inputs):
            ch: dict[str, Any] = {"index": i}
            self._stored_common(ch, spec, p)
            routes = []
            gains = spec.get("routing", [])
            gates = spec.get("routing_status", [])
            for out_idx in range(max(len(gains), len(gates))):
                route: dict[str, Any] = {"index": out_idx}
                if out_idx < len(gains):
                    g = p.f32(gains[out_idx])
                    if g is not None:
                        route["gain"] = round(g, 3)
                if out_idx < len(gates):
                    raw = p.i32(gates[out_idx])
                    if raw in (GATE_MUTED, GATE_PASSING):
                        route["enabled"] = raw == GATE_PASSING
                routes.append(route)
            if routes:
                ch["routing"] = routes
            ins.append(ch)

        outs: list[dict[str, Any]] = []
        for i, spec in enumerate(self.amap.outputs):
            ch = {"index": i}
            self._stored_common(ch, spec, p)
            if "delay" in spec:
                raw = p.f32(spec["delay"])
                if raw is not None:
                    ch["delay"] = round(delay_ms_from_raw(raw, self.rate), 4)
            if "invert" in spec:
                raw = p.i32(spec["invert"])
                if raw is not None:
                    ch["invert"] = bool(raw)
            groups = []
            for gi, base in enumerate(spec.get("xover_groups", [])):
                vals = p.floats(base, XOVER_SLOTS * BIQUAD_FLOATS)
                group = describe_crossover_group(
                    [as_biquad(vals[k * BIQUAD_FLOATS:(k + 1) * BIQUAD_FLOATS])
                     for k in range(XOVER_SLOTS)], gi, self.rate)
                bypassed = p.is_bypassed(base)
                if bypassed is not None:
                    group["bypass"] = bypassed
                groups.append(group)
            ch["crossover"] = groups
            outs.append(ch)

        return {"preset": p.index, "inputs": ins, "outputs": outs,
                "source": "stored"}

    def _stored_common(self, ch: dict[str, Any], spec: dict[str, Any],
                       p: mf.StoredPreset) -> None:
        """Gain, gate and PEQ, which inputs and outputs record the same way."""
        if "gain" in spec:
            g = p.f32(spec["gain"])
            if g is not None:
                ch["gain"] = round(g, 3)
        if "enable" in spec:
            raw = p.i32(spec["enable"])
            if raw is not None:
                _set_gate(ch, raw)
        bands = []
        for band, addr in enumerate(spec.get("peq", [])):
            vals = p.floats(addr, BIQUAD_FLOATS)
            entry = describe_peq_band(as_biquad(vals), band, self.rate)
            bypassed = p.is_bypassed(addr)
            if bypassed is not None:
                entry["bypass"] = bypassed
            bands.append(entry)
        ch["peq"] = bands

    def read_all(self, n: int | None = None) -> list[dict[str, Any]]:
        total = len(self.amap.outputs)
        n = total if n is None else min(n, total)
        return [self.read_output(i) for i in range(n)]

    def read_inputs(self, n: int | None = None) -> list[dict[str, Any]]:
        total = len(self.amap.inputs)
        n = total if n is None else min(n, total)
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
                name = str(s).lower()
                idx = SOURCES.index(name) if name in SOURCES else int(s)
                self._dev.set_source(idx)
            if "preset" in fields:
                self._dev.set_preset(int(fields["preset"]))

    def set_config(self, payload: dict[str, Any]) -> None:
        """Apply a project payload, in the shape the app already builds.

        A mixer cell's on/off gate is written only where the address map
        records one. It used to be derived as in_index * outputs + out_index,
        which is true of the Flex 8 and of nothing much else: on five of the
        thirteen generated maps the real gates sit elsewhere, and on three
        there are none at all, so that arithmetic addressed whatever parameter
        happened to occupy the slot. On a 2x4HD it lands on the channel mute
        gates, meaning a routing change would have muted channels instead.
        """
        self._check_payload(payload)
        with self._lock:
            for out in payload.get("outputs", []):
                spec = self.amap.outputs[out["index"]]
                if "gain" in out and "gain" in spec:
                    self._dev.write_float(spec["gain"], float(out["gain"]))
                if "delay" in out and "delay" in spec:
                    self._dev.write_int(
                        spec["delay"],
                        _delay_samples(out["delay"], self.rate))
                if "invert" in out and "invert" in spec:
                    self._dev.write_int(spec["invert"],
                                        1 if out["invert"] else 0)
                if "mute" in out and "enable" in spec:
                    self._dev.write_int(spec["enable"], _gate(out["mute"]))

                peq_addrs = spec.get("peq", [])
                for band in out.get("peq", []):
                    if band["index"] >= len(peq_addrs):
                        continue
                    addr = peq_addrs[band["index"]]
                    self._dev.write_biquad(addr, _coeff_list(band["coeff"]))
                    if band.get("bypass") is not None:
                        self._dev.set_bypass(addr, bool(band["bypass"]))

                for group in out.get("crossover", []):
                    bases = spec.get("xover_groups", [])
                    if group["index"] >= len(bases):
                        continue
                    base = bases[group["index"]]
                    coeffs = group.get("coeff", [])[:XOVER_SLOTS]
                    for k, bq in enumerate(coeffs):
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
                    if band["index"] >= len(peq_addrs):
                        continue
                    addr = peq_addrs[band["index"]]
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
        n_out = len(self.amap.outputs)
        for key, specs in (("outputs", self.amap.outputs),
                           ("inputs", self.amap.inputs)):
            for ch in payload.get(key, []):
                idx = ch.get("index")
                if not isinstance(idx, int) or not 0 <= idx < len(specs):
                    raise mp.ProtocolError(
                        f"payload names {key[:-1]} {idx}, but this device has "
                        f"{len(specs)}. The project was probably saved "
                        f"against a different model.")
                for route in ch.get("routing", []):
                    dest = route.get("index")
                    if not isinstance(dest, int) or not 0 <= dest < n_out:
                        raise mp.ProtocolError(
                            f"routing on input {idx} names output {dest}, but "
                            f"this device has {n_out}.")

    def _set_route(self, in_idx: int, route: dict[str, Any]) -> None:
        """Mixer cell, which uses the same 1-off / 2-on gate as a channel."""
        out_idx = route["index"]
        spec = self.amap.inputs[in_idx]
        gates = spec.get("routing_status", [])
        gains = spec.get("routing", [])
        if out_idx < len(gates):
            self._dev.write_int(gates[out_idx],
                                _gate(not route.get("enabled")))
        if out_idx < len(gains):
            self._dev.write_float(gains[out_idx],
                                  float(route.get("gain", 0.0)))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Floats in one biquad, which is also the granularity the device
# aligns filter-memory reads to.
BIQUAD_FLOATS = 5

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


def _coeff_list(coeff: dict[str, float]) -> list[float]:
    """The five coefficients, in wire order, or an error.

    A missing key used to default to 0.0, and a section with b0 = 0 is not a
    gentle default: it passes nothing, so a malformed filter would have been
    written as silence on a driver. Every path that builds these produces all
    five, so an absent one is a bug worth hearing about.
    """
    missing = [k for k in COEFF_KEYS if k not in coeff]
    if missing:
        raise mp.ProtocolError(
            f"biquad is missing {', '.join(missing)}; refusing to write a "
            f"partial filter")
    return [float(coeff[k]) for k in COEFF_KEYS]


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
    samples = int(round(ms * rate / 1000.0))
    # Bounded on the way out as well as on the way in: no miniDSP offers
    # anything near this much delay, and writing a sample count past what the
    # buffer holds is not a defined thing to do.
    return max(0, min(samples, MAX_DELAY_SAMPLES))
