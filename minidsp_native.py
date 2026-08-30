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

import struct
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
                timeout_ms: int = 2000) -> "NativeDevice":
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


# Asked before a write that would take a crossover out of circuit on an
# output still carrying signal. Set by whatever can put the question to a
# person; when nothing has, the answer is no.
#
# This lives here rather than in the window because the window is not the
# only way to reach the device. Every test script, every one-off probe,
# talks to this layer directly -- and a guard that only exists in the UI is
# one that every script goes around. That is not hypothetical: a script
# doing exactly that wrote default 80 Hz crossovers over a pair of
# tweeters, with the amplifiers live.
_CONFIRM_DANGEROUS = None


def set_confirm_handler(fn) -> None:
    """Install something that can ask before a dangerous write.

    `fn(title, detail)` returns True to go ahead. A caller with no handler
    installed cannot ask, so the write is refused and says how to allow it
    -- which makes permitting one a deliberate line of code rather than a
    thing that happens because nobody was looking.
    """
    global _CONFIRM_DANGEROUS
    _CONFIRM_DANGEROUS = fn


def _ask_dangerous(title: str, detail: str) -> bool:
    if _CONFIRM_DANGEROUS is None:
        raise mp.ProtocolError(
            f"{title}\n\n{detail}\n\nNothing here can ask whether that "
            f"is intended, so it was not written. A caller that means it "
            f"can say so with minidsp_native.set_confirm_handler().")
    return bool(_CONFIRM_DANGEROUS(title, detail))


class NativeDevice:
    """One miniDSP, driven directly.

    All access is serialised: the device has a single command/reply endpoint
    pair, so overlapping requests from the UI thread and a worker thread would
    interleave replies.
    """

    def __init__(self, amap: AddressMap, product_id: int | None = None,
                 timeout_ms: int = 2000,
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

    def _meter_spans(self, specs: list[dict[str, Any]]
                     ) -> list[tuple[int, int]]:
        """Meter addresses grouped into consecutive runs.

        They are laid out contiguously on every map seen so far -- a Flex 8
        keeps its two input meters at 48-49 and its eight output meters at
        58-65 -- so the whole set costs two reads rather than ten. That is
        what makes polling them often enough to look like a meter affordable.
        Falls back to one read per meter if a map ever scatters them.
        """
        addrs = sorted(a for a in (s.get("meter") for s in specs)
                       if a is not None)
        spans: list[tuple[int, int]] = []
        for a in addrs:
            if spans and a == spans[-1][1] + 1 and \
                    (spans[-1][1] - spans[-1][0] + 1) < MAX_FLOATS_PER_READ:
                spans[-1] = (spans[-1][0], a)
            else:
                spans.append((a, a))
        return spans

    def _read_meters(self) -> tuple[list[float], list[float]]:
        """Both meter banks, caller holds the lock."""
        out = []
        for specs in (self.amap.inputs, self.amap.outputs):
            vals: list[float] = []
            for lo, hi in self._meter_spans(specs):
                vals.extend(self._dev.read_floats(lo, hi - lo + 1))
            out.append(vals)
        return out[0], out[1]

    def compressor_meters(self) -> list[float]:
        """Gain reduction per output, if the map records those meters.

        A separate call from meters() because the level meters are polled
        many times a second for the bars and this is only wanted while a
        compressor panel is on screen.
        """
        addrs = [s["compressor"]["meter"] for s in self.amap.outputs
                 if "compressor" in s and "meter" in s["compressor"]]
        if not addrs:
            return []
        lo, hi = min(addrs), max(addrs)
        if hi - lo + 1 != len(addrs) or len(addrs) > MAX_FLOATS_PER_READ:
            with self._lock:
                return [self._dev.read_floats(a, 1)[0] for a in addrs]
        with self._lock:
            return self._dev.read_floats(lo, hi - lo + 1)

    def meters(self) -> tuple[list[float], list[float]]:
        """Input and output levels, and nothing else.

        Separate from status() so the bars can be polled quickly without
        re-reading the master block, which changes far more slowly.
        """
        with self._lock:
            return self._read_meters()

    def status(self) -> dict[str, Any]:
        with self._lock:
            m = self._dev.master_status()
            ins, outs = self._read_meters()
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
        """One output, from live parameter memory only.

        Gain, delay, polarity, the mute gate and the crossover blocks all
        read back and are decoded here. The PEQ blocks are read too and
        come back as five zeros apiece, which is what filter memory
        answers -- they are decoded anyway so the caller sees a band of the
        right shape, and stored_config supplies the real ones.

        The compressor is deliberately absent. Only its threshold reads
        back; the rest answer zero, and a zero that looks like a setting is
        worse than no setting at all.
        """
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
        """One input, from live parameter memory only.

        Gain, the mute gate, the PEQ blocks and each mixer cell's gain and
        polarity. The cells' on/off gates are not here: see the note in the
        body for why a readable-looking address is left unread.
        """
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
            # A mixer cell's gain reads back; its on/off gate does not. The
            # gate answers a constant 1 on every cell, which is the encoding
            # for "off" -- on this device all 16 answer 1 while audio is
            # passing through four outputs, and the stored preset holds a
            # mixture of 1 and 2 at the same addresses. So the gain is read
            # and the gate is left to the stored preset, which is the only
            # place the real routing can be had.
            #
            # Worth stating because 1 is a plausible answer rather than an
            # obviously absent one: taking it at face value would draw every
            # output as unrouted, and writing it back would silence them.
            route_gain = [round(self._dev.read_floats(a, 1)[0], 3)
                          for a in spec.get("routing", [])]
            # Polarity does read back, unlike the gate beside it: written 0
            # or 1 it answers 0 or 1, and anything else saturates to 1.
            route_pol = [bool(self._dev.read_ints(a, 1)[0])
                         for a in spec.get("routing_polarity", [])]
        out["peq"] = [describe_peq_band(as_biquad(blocks[a]), i, self.rate)
                      for i, a in enumerate(peq_addrs)]
        if route_gain:
            out["routing"] = [
                {"index": i, "gain": g,
                 **({"polarity": route_pol[i]}
                    if i < len(route_pol) else {})}
                for i, g in enumerate(route_gain)]
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
            fir = spec.get("fir")
            if fir and "coeffs" in fir:
                # The taps are inside the image already, so this costs
                # nothing beyond decoding them -- and it is the only way
                # the panel can show what the device is actually holding
                # rather than what this project last put there.
                ch["fir"] = {
                    "taps": p.floats(fir["coeffs"], FIR_TAPS),
                    "enabled": p.i32(fir["enable"]) == FIR_ENABLED
                    if "enable" in fir else False,
                    "source": "device", "pending": False,
                }
            routes = []
            gains = spec.get("routing", [])
            gates = spec.get("routing_status", [])
            pols = spec.get("routing_polarity", [])
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
                if out_idx < len(pols):
                    route["polarity"] = bool(p.i32(pols[out_idx]))
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
            comp = spec.get("compressor")
            if comp:
                entry: dict[str, Any] = {}
                for key in ("threshold", "makeup", "ratio", "knee",
                            "attack", "release"):
                    if key in comp:
                        v = p.f32(comp[key])
                        if v is not None:
                            entry[key] = round(v, 3)
                raw = p.i32(comp["enable"]) if "enable" in comp else None
                if raw in (COMP_BYPASSED, COMP_ENABLED):
                    entry["enabled"] = raw == COMP_ENABLED
                if entry:
                    ch["compressor"] = entry

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

    def _crossovers_at_risk(self, payload: dict[str, Any]) -> list[str]:
        """Outputs this payload would leave carrying signal unfiltered.

        Compares against the crossover coefficients the device is running,
        which do read back. An output that has a real filter now, would
        have none after, and is still fed and unmuted, is the one shape on
        this hardware that destroys something.

        Says nothing about an output that will be muted or unrouted: no
        signal reaches a driver there, and refusing that would block the
        ordinary business of clearing a preset.
        """
        fed = {r["index"] for inp in payload.get("inputs", [])
               for r in inp.get("routing", []) if r.get("enabled")}
        at_risk = []
        for out in payload.get("outputs", []):
            idx = out["index"]
            if idx not in fed or out.get("mute"):
                continue
            wants = any(_group_filters(g) for g in out.get("crossover", []))
            if wants:
                continue
            spec = self.amap.outputs[idx]
            live = False
            for base in spec.get("xover_groups", []):
                vals = self._floats(base, XOVER_SLOTS * BIQUAD_FLOATS)
                for k in range(XOVER_SLOTS):
                    bq = vals[k * BIQUAD_FLOATS:(k + 1) * BIQUAD_FLOATS]
                    if any(abs(a - b) > 1e-6 for a, b in zip(bq, _UNITY)):
                        live = True
                        break
                if live:
                    break
            if live:
                at_risk.append(out.get("name") or f"Out {idx + 1}")
        return at_risk

    def set_config(self, payload: dict[str, Any],
                   fir_progress: Any = None) -> None:
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
        risk = self._crossovers_at_risk(payload)
        if risk and not _ask_dangerous(
                "This would leave a driver unfiltered",
                f"{', '.join(risk)} would carry full range after this "
                f"write, and each is unmuted and fed. If a tweeter is on "
                f"one of them, that destroys it."):
            raise mp.ProtocolError("write cancelled")
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
                self._write_compressor(spec, out)

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

        # Outside that lock, because write_fir takes it for itself and this
        # one is not reentrant -- doing it inside deadlocks the worker and
        # the window with it. Last, so a filter that fails to land leaves
        # everything before it applied.
        for inp in payload.get("inputs", []):
            fir = inp.get("fir")
            if fir and fir.get("taps"):
                self.write_fir(inp["index"], fir["taps"],
                               enabled=bool(fir.get("enabled")),
                               progress=fir_progress)

    @staticmethod
    def _phases(cb: Any, count: int, each: int):
        """Turn several byte-counted round trips into one progress line.

        Storing is three trips over the wire -- read what is there, write
        the edited image, read it back to check it -- and each one counts
        its own bytes from zero. Reported raw, the bar would fill and
        restart three times, which reads as three failures rather than one
        operation. This gives each phase its own band of a single total.
        """
        state = {"i": 0}

        def phase():
            base = state["i"] * each
            state["i"] += 1

            def report(done: int, _total: int) -> None:
                if cb is not None:
                    cb(min(base + done, count * each), count * each)
            return report
        return phase

    def save_stored_preset(self, payload: dict[str, Any],
                           progress: Any = None) -> dict[str, int]:
        """Write a project into the device's stored preset, and check it.

        This is the one operation here that changes what the device does at
        power-on. Everything else is live only.

        Three things make it safer than rebuilding the preset from scratch,
        which is what the vendor's software does:

          * It edits the stored image rather than generating one. The image
            holds every parameter the DSP has and the address maps describe
            only some of them, so a generated image would zero whatever this
            app does not model.
          * It writes to the active preset because that is the only one the
            device will write, and it says so rather than letting an index
            argument imply otherwise.
          * It reads both blocks back afterwards and compares them byte for
            byte. The vendor's verify step reads one EEPROM key and checks it
            against a constant, which cannot tell whether the preset arrived.
        """
        self._check_payload(payload)
        risk = self._crossovers_at_risk(payload)
        if risk and not _ask_dangerous(
                "This would leave a driver unfiltered",
                f"{', '.join(risk)} would carry full range after this "
                f"write, and each is unmuted and fed. If a tweeter is on "
                f"one of them, that destroys it."):
            raise mp.ProtocolError("write cancelled")
        active = int(self.status()["master"]["preset"])
        slots = self.preset_slots()
        if not 0 <= active < len(slots):
            raise mp.ProtocolError(
                f"the device reports preset {active}, but {len(slots)} "
                f"preset slots were found in its flash")
        slot = slots[active]
        phase = self._phases(progress, 3, slot.vals.payload_len)
        stored = self.read_stored_preset(active, progress=phase())

        words, flags = self._preset_changes(payload)
        values = mf.replace_words(stored.values, words)
        bypass = (mf.replace_bypass(stored.bypass_raw, flags)
                  if flags and stored.bypass_raw else None)

        with self._lock:
            mf.save_preset(self._dev, slot, values, bypass, phase())
            mf.verify_preset(self._dev, slot, values, bypass, phase())
        return {"preset": active, "parameters": len(words),
                "bypass_flags": len(flags)}

    def _preset_changes(self, payload: dict[str, Any]
                        ) -> tuple[dict[int, int], dict[int, bool]]:
        """What a payload changes, as raw words and bypass flags.

        Every value is encoded exactly as the DSP stores it, which is not
        uniform: a gain is a float, a delay is an integer sample count, and a
        gate is the integer 1 or 2.
        """
        words: dict[int, int] = {}
        flags: dict[int, bool] = {}

        def put_float(addr: int, value: float) -> None:
            words[addr] = struct.unpack("<I", struct.pack("<f",
                                                          float(value)))[0]

        def put_int(addr: int, value: int) -> None:
            words[addr] = int(value) & 0xFFFFFFFF

        def put_biquad(addr: int, coeff: dict[str, float]) -> None:
            for k, c in enumerate(_coeff_list(coeff)):
                put_float(addr + k, c)

        for out in payload.get("outputs", []):
            spec = self.amap.outputs[out["index"]]
            if "gain" in out and "gain" in spec:
                put_float(spec["gain"], out["gain"])
            if "delay" in out and "delay" in spec:
                put_int(spec["delay"], _delay_samples(out["delay"], self.rate))
            if "invert" in out and "invert" in spec:
                put_int(spec["invert"], 1 if out["invert"] else 0)
            if "mute" in out and "enable" in spec:
                put_int(spec["enable"], _gate(out["mute"]))
            comp, want = spec.get("compressor"), out.get("compressor")
            if comp and want:
                for key in COMP_FIELDS:
                    if key in want and key in comp:
                        put_float(comp[key], float(want[key]))
                if "enable" in comp and want.get("enabled") is not None:
                    put_int(comp["enable"], COMP_ENABLED if want["enabled"]
                            else COMP_BYPASSED)
            peq_addrs = spec.get("peq", [])
            for band in out.get("peq", []):
                if band["index"] >= len(peq_addrs):
                    continue
                addr = peq_addrs[band["index"]]
                put_biquad(addr, band["coeff"])
                if band.get("bypass") is not None:
                    flags[addr] = bool(band["bypass"])
            bases = spec.get("xover_groups", [])
            for group in out.get("crossover", []):
                if group["index"] >= len(bases):
                    continue
                base = bases[group["index"]]
                for k, bq in enumerate(group.get("coeff", [])[:XOVER_SLOTS]):
                    put_biquad(base + k * BIQUAD_FLOATS, bq)
                if group.get("bypass") is not None:
                    flags[base] = bool(group["bypass"])

        for inp in payload.get("inputs", []):
            spec = self.amap.inputs[inp["index"]]
            if "gain" in inp and "gain" in spec:
                put_float(spec["gain"], inp["gain"])
            if "mute" in inp and "enable" in spec:
                put_int(spec["enable"], _gate(inp["mute"]))
            peq_addrs = spec.get("peq", [])
            for band in inp.get("peq", []):
                if band["index"] >= len(peq_addrs):
                    continue
                addr = peq_addrs[band["index"]]
                put_biquad(addr, band["coeff"])
                if band.get("bypass") is not None:
                    flags[addr] = bool(band["bypass"])
            gains = spec.get("routing", [])
            gates = spec.get("routing_status", [])
            pols = spec.get("routing_polarity", [])
            for route in inp.get("routing", []):
                dest = route["index"]
                if dest < len(gains) and "gain" in route:
                    put_float(gains[dest], route["gain"])
                if dest < len(gates):
                    put_int(gates[dest], _gate(not route.get("enabled")))
                if dest < len(pols) and "polarity" in route:
                    put_int(pols[dest], 1 if route["polarity"] else 0)
        return words, flags

    def fir_capacity(self, index: int) -> int:
        """How many taps input `index`'s FIR block will hold."""
        with self._lock:
            return self._dev.get_num_fir_taps(index)

    def read_fir(self, index: int, count: int | None = None
                 ) -> dict[str, Any]:
        """One FIR block, read from the device.

        The coefficients genuinely read back, which no other filter on this
        hardware does: a PEQ biquad answers five zeros however it is set,
        while these come back exactly as written. So a FIR is the one filter
        here that can be verified rather than trusted.

        `taps` does not: it reads 0 whatever it holds, so the tap count
        comes from the stored preset the way the compressor's settings do.

        `enable` does read back, which took a while to establish. It
        answers 1 on a block that has never been switched on, and every
        read of it here was in that state -- so it looked like another
        write-only field reporting a constant. Once a block is actually
        enabled it reads 2, the value it was set to. The same shape of
        mistake as the compressor's makeup gain, which read 0 and was
        called unreadable when the stored value was also 0.
        """
        spec = self.amap.inputs[index].get("fir")
        if not spec:
            raise mp.ProtocolError(f"input {index + 1} has no FIR block")
        want = count if count is not None else self.fir_capacity(index)
        out: dict[str, Any] = {"index": index, "capacity": want}
        with self._lock:
            out["coeff"] = self._floats(spec["coeffs"], want)
        return out

    def write_fir(self, index: int, taps: Sequence[float],
                  enabled: bool = False,
                  progress: Any = None) -> dict[str, Any]:
        """Load a filter into one input's FIR block.

        The order is Device Console's, with one deliberate difference.

        Theirs writes the status field first, carrying the value it means to
        end on, and only then the tap count and the taps. So enabling a
        filter walks the device through a window where the block is live and
        its coefficients are half written -- the same shape as their
        compressor write. Here the block is bypassed for the whole write and
        switched on at the end, after the reload.

        Enabling a block that was loaded this way is uneventful; measured,
        with the DSP answering throughout and the field reading back as 2.
        The time this DSP stopped answering altogether, it had been asked to
        run coefficients written straight to their addresses with
        LoadDspParam and never reloaded -- a filter it had never been told
        about. That is what the order below is really guarding against.

        Taps go by WriteFirTapsToFlash, never by a parameter write. The
        coefficient addresses accept one and read it back, which makes the
        wrong thing look like it worked; the device does its own bookkeeping
        around that memory and a filter poked into it directly is not one
        the DSP has been told about.
        """
        spec = self.amap.inputs[index].get("fir")
        if not spec:
            raise mp.ProtocolError(f"input {index + 1} has no FIR block")
        taps = list(taps)
        capacity = self.fir_capacity(index)
        if len(taps) > capacity:
            raise mp.ProtocolError(
                f"input {index + 1}'s FIR holds {capacity} taps and "
                f"{len(taps)} were offered. The device would take the first "
                f"{capacity} and drop the rest, which is a different filter, "
                f"so nothing was written.")
        if len(taps) < FIR_MIN_TAPS:
            raise mp.ProtocolError(
                f"a FIR block takes at least {FIR_MIN_TAPS} taps and "
                f"{len(taps)} were offered")

        # Padded to the full block. Writing only the filter's own taps
        # leaves whatever was there beyond them: a 32-tap filter followed by
        # a 6-tap one reads back as six taps and then the tail of the first,
        # which is measurably what this device does. Device Console writes
        # only its own rows and trusts the tap count to stop the DSP reading
        # past them -- and that may well be true, but the count cannot be
        # read back to check, so trusting it would mean a filter that reads
        # back as something other than what was written. Since reading back
        # is the one thing FIR can do that no other filter here can, it is
        # worth the packets to keep it meaningful.
        payload = list(taps) + [0.0] * (capacity - len(taps))
        sent = 0
        with self._lock:
            self._dev.write_int(spec["enable"], FIR_BYPASSED)
            # The count stays the real length, not the padded one: it is
            # what the DSP is told the filter is, and an integer, like
            # delay's sample count. The stored preset reads 6 through i32;
            # as a float that word would read 1086324736.
            self._dev.write_int(spec["taps"], len(taps))
            while sent < len(payload):
                sent += self._dev.write_fir_taps(index, payload[sent:])
                if progress is not None:
                    progress(sent, len(payload))
            self._dev.reload_dsp_param()
            if enabled:
                self._dev.write_int(spec["enable"], FIR_ENABLED)
        return {"index": index, "taps": len(taps), "written": sent,
                "enabled": bool(enabled)}

    def _write_compressor(self, spec: dict[str, Any],
                          out: dict[str, Any]) -> None:
        """One output's compressor, switched off while it is changed.

        The order is deliberate: bypass it, write the settings, then put it
        back into circuit if that is what was asked for. Writing a live
        compressor's parameters underneath it left this device computing
        against a half-updated set and emitting NaN from that channel -- a
        NaN that then propagated and did not clear by itself.

        Device Console writes the status field *first*, with the value it
        wants to end on, and then writes the settings underneath it. So a
        vendor write that switches a compressor on walks through exactly the
        state that produced the NaN here. Bypassing first costs one extra
        write and removes that window, which is why this does not copy them.

        Five of the six settings cannot be read back, so there is no way to
        confirm what landed. That is a reason to be careful about the order,
        not a reason to skip the write.
        """
        comp = spec.get("compressor")
        want = out.get("compressor")
        if not comp or not want:
            return
        if "enable" in comp:
            self._dev.write_int(comp["enable"], COMP_BYPASSED)
        for key in COMP_FIELDS:
            if key in want and key in comp:
                self._dev.write_float(comp[key], float(want[key]))
        if "enable" in comp and want.get("enabled") is not None:
            self._dev.write_int(
                comp["enable"],
                COMP_ENABLED if want["enabled"] else COMP_BYPASSED)

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
        pols = spec.get("routing_polarity", [])
        if "polarity" in route and out_idx < len(pols):
            # 0 passes, 1 inverts -- the same encoding a channel's own
            # polarity uses, and it saturates: anything else reads back 1.
            self._dev.write_int(pols[out_idx],
                                1 if route["polarity"] else 0)
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

# A biquad that does nothing: the shape a crossover slot holds when there
# is no filter in it.
_UNITY = [1.0, 0.0, 0.0, 0.0, 0.0]


def _is_unity(bq: Any) -> bool:
    """Whether one biquad passes its input through untouched."""
    try:
        vals = _coeff_list(bq)
    except Exception:                                  # noqa: BLE001
        return True          # nothing usable in it is nothing filtering
    return all(abs(a - b) <= 1e-6 for a, b in zip(vals, _UNITY))


def _group_filters(group: dict[str, Any]) -> bool:
    """Whether a payload's crossover group would filter anything.

    A group is four biquad slots. It filters if it is not bypassed and at
    least one slot holds something other than a pass-through -- which is
    what a group looks like when its filter has been switched off, since
    the slots keep their coefficients and only the bypass flag moves.
    """
    if group.get("bypass"):
        return False
    return any(not _is_unity(bq) for bq in (group.get("coeff") or []))

# A compressor's on/off field, which does not use the 1/2 gate convention.
# From Device Console's own audioProcessingDefn: bypass ? 0x3 : 0x2, and it
# reads the field back with `parseInt(...) === 3` for bypassed. Live reads of
# this address return 1 on every output regardless, so a read that is neither
# 3 nor 2 is discarded rather than guessed at.
COMP_BYPASSED, COMP_ENABLED = 3, 2

# A FIR block's on/off field. Same encoding as the compressor's, but
# unlike that one it does read back -- 2 once a block has been enabled.
# A block that has never been switched on answers 1, which is neither
# value and is what made this look unreadable for as long as nothing had
# been enabled.
FIR_BYPASSED, FIR_ENABLED = 3, 2

# How many coefficients a block holds on the hardware this was written
# against. Only used to decode a stored preset, where asking the device
# would mean a round trip for a number that is already implied by the
# block's size in the image.
#
# miniDSP's manual puts it as a pool: 4096 taps in total, distributed as
# you like across the two inputs, each between 6 and 2048. Since the two
# maxima add up to the total there is never anything to trade -- both
# blocks can hold 2048 at once, which is what GetNumFirTaps reports for
# each of them.
#
# The floor is why an unused block reads 6: that is the documented
# minimum, not an arbitrary leftover.
FIR_TAPS = 2048
FIR_MIN_TAPS = 6

# The order these are written in matters, so it is stated once. Everything
# the compressor computes with goes down before it is switched on, and it is
# switched off before any of it changes -- see _write_compressor.
#
# `knee` is written because it is part of the block and the stored preset
# carries it, but this DSP ignores it. Measured by giving four outputs the
# same signal at the same instant and identical compressors differing only
# in knee -- 0, 12, 24 and 40 all returned a gain reduction of -10.99 dB and
# a level of -54.2 dBFS, identical to the last digit, while the same method
# resolved a ratio sweep across 7.3 dB. Device Console does not offer a knee
# control and minidsp-rs has no knee field; this is why.
COMP_FIELDS = ("threshold", "makeup", "ratio", "knee", "attack", "release")


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
