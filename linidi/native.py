#!/usr/bin/env python3
"""
linidi.native -- the device layer, spoken directly over USB.

Presents the same surface the application already used when it went through
minidspd, so the UI does not care which is underneath:

    status()        master state and live meters
    set_master()    volume, mute, source, preset
    set_config()    a whole project payload
    read_output()   coefficients, gain, delay for one output
    read_input()    gain and PEQ for one input

Parameter writes use MODE_APPLY (0xa0). The alternative 0x80 is acknowledged
by the device but a mixer disable written with it is silently discarded, which
is why routing changes appeared not to work at all. That finding is about
CMD_LOAD_DSP_PARAM; the separate biquad opcode takes 0x80, and says so where
it is sent.

License: Apache-2.0
"""

from __future__ import annotations

import struct
import threading
from typing import Any, Sequence

from . import flash as mf
from . import core as mc
from . import protocol as mp
from .protocol import MAX_FLOATS_PER_READ
from .core import (COEFF_KEYS, MAX_DELAY_SAMPLES,
                          XOVER_SLOTS, AddressMap, as_biquad,
                          delay_ms_from_raw,
                          describe_crossover_group,
                          describe_peq_band)

# (hw_id, dsp_version) -> address map name.
#
# The device reports numbers, not a product name, so this is how a map gets
# chosen without a daemon to ask. Entries are added as devices are confirmed
# against real hardware. A dsp_version of None would match every version of
# that hardware, and there is deliberately no such entry: an address map is a
# list of places to write, and the wrong one puts an 80 Hz crossover on
# whatever is wired to what it thinks is output 3. An unrecognised device is
# refused with the name of the flag that overrides this, which is a decision
# somebody makes rather than one that happens quietly.
DEVICE_MAPS: dict[tuple[int, int | None], str] = {
    (30, 110): "flex8",
    (30, 111): "flex8",      # Dirac variant, same DSP layout
}


def map_for(hw_id: int, dsp_version: int | None = None) -> str | None:
    """Address map name for a device that identified itself."""
    return DEVICE_MAPS.get((hw_id, dsp_version))


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
                f"no address map is known for hw_id {info.hw_id} dsp "
                f"{info.dsp_version}. Run with --map NAME to choose one, "
                f"after checking that it matches this device: "
                f"{', '.join(AddressMap.available())}")
        try:
            amap = AddressMap.load(name)
        except ValueError as exc:
            # A structurally broken map. Letting this out as ValueError meant
            # an unhandled traceback out of the window's constructor rather
            # than a message anyone could act on.
            raise mp.ProtocolError(str(exc)) from None
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
def set_confirm_handler(fn) -> None:
    """Install something that can ask before a dangerous write.

    `fn(title, detail)` returns True to go ahead. A caller with no handler
    installed cannot ask, so the write is refused and says how to allow it
    -- which makes permitting one a deliberate line of code rather than a
    thing that happens because nobody was looking.

    Kept here as well as in core because this is the module a script reaches
    for, but there is only one handler and core owns it: the minidspd path
    needs the same gate and cannot import this module.
    """
    mc.set_confirm_handler(fn)


def _ask_dangerous(title: str, detail: str, verb: str = "Continue") -> bool:
    return mc.ask_dangerous(title, detail, verb)




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
            if self._slots is not None:
                # Including an empty list, which means "looked, and this
                # device has none". Treating that as "not looked yet" rescanned
                # the whole part on every call, twenty-five seconds each time.
                return self._slots
            if self._slots is None:
                remembered = mf.load_slots(key)
                if remembered:
                    # Confirmed before it is trusted: eight short reads
                    # against a twenty-five-second rescan.
                    with self._lock:
                        if mf.confirm_slots(self._dev, remembered):
                            self._slots = remembered
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
        if progress is not None and (force or not self.preset_slots_known()):
            # The scan is the slow half and had no way to say so: progress
            # went to the block read only, so the first read of a device sat
            # on an empty bar for twenty-five seconds before anything moved.
            phase = self._phases(progress, 2)
            slots = self.preset_slots(force=force, progress=phase())
            progress = phase()
        else:
            slots = self.preset_slots(force=force)
        if not 0 <= index < len(slots):
            raise mp.ProtocolError(
                f"preset {index + 1} was asked for, but {len(slots)} "
                f"preset "
                f"slots were found in this device's flash")
        with self._lock:
            return mf.read_preset(self._dev, slots[index], progress)

    def stored_config(self, index: int = 0,
                      preset: mf.StoredPreset | None = None
                      ) -> dict[str, Any]:
        """A whole stored preset in the shape the rest of the app speaks.

        Carries three things a live read cannot produce at all: PEQ
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
                # Only as many taps as the image says the filter has, and
                # only a state the image actually holds. Reading the whole
                # block reported 2048 taps for a six-tap filter, and an
                # address past the end of the image reported "off" rather
                # than saying nothing -- which is what _stored_common and
                # the compressor both take care to do.
                held = p.i32(fir["taps"]) if "taps" in fir else None
                usable = isinstance(held, int) and 0 < held <= FIR_TAPS
                count = held if usable else FIR_TAPS
                block: dict[str, Any] = {
                    "taps": p.floats(fir["coeffs"], count),
                    "source": "device", "pending": False,
                }
                state = p.i32(fir["enable"]) if "enable" in fir else None
                if state in (FIR_ENABLED, FIR_BYPASSED):
                    block["enabled"] = state == FIR_ENABLED
                ch["fir"] = block
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

    def _unfiltered_outputs(self, payload: dict[str, Any]) -> list[str]:
        """Outputs this payload would leave active with nothing filtering them.

        One question, and it is answered from the payload alone: does this
        write leave an output passing unfiltered while it is still fed and
        unmuted. Nothing is read off the device and nothing is compared
        against what is there now, because what was there before does not
        change the answer to that question.

        Nothing is inferred about what is connected, either. This program does
        not know whether an output feeds a tweeter, a plate amp or a patch
        bay, and a check that guessed would be wrong in ways nobody can
        foresee. It reports the state the configuration will be in. Knowing
        what is safe to do with that belongs to whoever wired the system, and
        an active DSP can destroy a driver by design.

        Says nothing about an output that will be muted or unrouted: no
        signal reaches it, and refusing that would block the ordinary
        business of clearing a preset.

        A payload that says nothing about routing is treated as feeding
        everything. Silence is not evidence that an output is quiet, and the
        guard used to read it that way: a payload with no inputs produced an
        empty `fed`, so nothing was ever at risk and the write went through
        unasked. That is weakest for exactly the hand-built payloads the guard
        was moved down here to catch.
        """
        # An output is fed unless this payload can prove otherwise, and it
        # can only prove that by speaking for every input that is able to
        # feed it. Taking one input's word for all of them left a hole: a
        # payload unrouting input 1 says nothing about a tweeter fed by
        # input 2. Inputs whose map has no gate address are left out of the
        # arithmetic entirely -- the write cannot switch those cells off
        # whatever the payload says, so they prove nothing either way.
        can_gate = {i for i, spec in enumerate(self.amap.inputs)
                    if spec.get("routing_status")}
        cut, fed_by = {}, set()
        for inp in payload.get("inputs", []):
            src = inp.get("index")
            if src not in can_gate:
                continue
            for r in inp.get("routing", []):
                dest = r.get("index")
                if "enabled" not in r or not isinstance(dest, int):
                    continue
                if r["enabled"]:
                    fed_by.add(dest)
                else:
                    cut.setdefault(dest, set()).add(src)

        def unfed(dest: int) -> bool:
            return (dest not in fed_by and can_gate
                    and cut.get(dest, set()) >= can_gate)

        unfiltered = []
        for out in payload.get("outputs", []):
            idx = out["index"]
            if unfed(idx) or out.get("mute"):
                continue
            groups = out.get("crossover")
            if groups is None:
                # This write is not touching this output's crossover, so it
                # cannot take one out of circuit. Absent is not empty: a
                # mute-only payload names no crossover at all, and a
                # gain-verification payload names one output and nothing
                # else. Reading either as "no filter" put the tweeter dialog
                # in front of every Load, Import, unmute and gain correction
                # -- which is how a warning stops being read.
                continue
            named = {g.get("index"): g for g in groups
                     if isinstance(g.get("index"), int)}
            if len(named) < len(self.amap.outputs[idx].get("xover_groups",
                                                          [])):
                # This write does not speak for every group the output has,
                # so a group it leaves out may still be filtering. Claiming
                # otherwise would take the device read this check does
                # without. The window's own payloads always name them all.
                continue
            # group_filters is what "filtering" means: not bypassed, and a
            # response that actually goes somewhere. An all-pass or a 0 dB
            # peaking filter is made of coefficients that are not unity and
            # protects nothing, so counting either as a filter would silence
            # the question in exactly the case worth asking about.
            if not any(mc.group_filters(g) for g in named.values()):
                unfiltered.append(out.get("name") or f"Out {idx + 1}")
        return unfiltered

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
        # Asked here and only here. This is the write that changes what the
        # device is doing to whatever is plugged into it, so it is the write
        # worth a question. Storing the same configuration to flash asks
        # nothing: it changes what comes back at the next power-on, after the
        # fact, and Save is already a separate deliberate action.
        unfiltered = self._unfiltered_outputs(payload)
        if unfiltered and not _ask_dangerous(
                "Unfiltered output",
                mc.unfiltered_question(unfiltered),
                verb="Apply"):
            raise mp.ProtocolError(mp.WRITE_CANCELLED)
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
                # A mute goes first and an unmute goes last. Unmuting ahead
                # of the twenty-odd filter writes below ran the driver on
                # whatever crossover was there before for about eighty
                # milliseconds -- and indefinitely if any write in between
                # raised.
                #
                # Both directions require the acknowledgement. The driver
                # guard stands down over a muted output, so a mute that was
                # lost and tolerated would have it stand down over a live
                # one; and a lost unmute leaves a channel silent with the
                # Apply reporting success.
                unmute_at = None
                if "mute" in out and "enable" in spec:
                    if out["mute"]:
                        self._dev.write_int(spec["enable"], _gate(True),
                                            require_ack=True)
                    else:
                        unmute_at = spec["enable"]
                self._write_compressor(spec, out)

                peq_addrs = spec.get("peq", [])
                for band in out.get("peq", []):
                    if not 0 <= band["index"] < len(peq_addrs):
                        continue
                    addr = peq_addrs[band["index"]]
                    self._dev.write_biquad(addr, _coeff_list(band["coeff"]))
                    if band.get("bypass") is not None:
                        self._dev.set_bypass(addr, bool(band["bypass"]))

                for group in out.get("crossover", []):
                    bases = spec.get("xover_groups", [])
                    if not 0 <= group["index"] < len(bases):
                        continue
                    base = bases[group["index"]]
                    coeffs = group.get("coeff", [])[:XOVER_SLOTS]
                    for k, bq in enumerate(coeffs):
                        self._dev.write_biquad(base + k * 5, _coeff_list(bq))
                    if group.get("bypass") is not None:
                        self._dev.set_bypass(base, bool(group["bypass"]))

                if unmute_at is not None:
                    self._dev.write_int(unmute_at, _gate(False),
                                        require_ack=True)

            for inp in payload.get("inputs", []):
                spec = self.amap.inputs[inp["index"]]
                if "gain" in inp and "gain" in spec:
                    self._dev.write_float(spec["gain"], float(inp["gain"]))
                # Mute first, unmute last, the same discipline the output
                # loop keeps: unmuting ahead of this channel's filters ran it
                # through the old ones for the rest of the sequence, and
                # indefinitely if anything in between raised.
                unmute_in = None
                if "mute" in inp and "enable" in spec:
                    if inp["mute"]:
                        self._dev.write_int(spec["enable"], _gate(True),
                                            require_ack=True)
                    else:
                        unmute_in = spec["enable"]
                peq_addrs = spec.get("peq", [])
                for band in inp.get("peq", []):
                    if not 0 <= band["index"] < len(peq_addrs):
                        continue
                    addr = peq_addrs[band["index"]]
                    self._dev.write_biquad(addr, _coeff_list(band["coeff"]))
                    if band.get("bypass") is not None:
                        self._dev.set_bypass(addr, bool(band["bypass"]))
                for route in inp.get("routing", []):
                    self._set_route(inp["index"], route)
                if unmute_in is not None:
                    self._dev.write_int(unmute_in, _gate(False),
                                        require_ack=True)

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
            elif fir and "enabled" in fir:
                # A payload carrying no taps but an explicit state is asking
                # to switch the block, not to load one. Skipping it outright
                # made "disable this FIR" a write that did nothing.
                spec = self.amap.inputs[inp["index"]].get("fir") or {}
                if "enable" in spec:
                    with self._lock:
                        self._dev.write_int(
                            spec["enable"],
                            FIR_ENABLED if fir["enabled"] else FIR_BYPASSED)

    @staticmethod
    def _phases(cb: Any, count: int):
        """Turn several byte-counted round trips into one progress line.

        Storing is three trips over the wire -- read what is there, write
        the edited image, read it back to check it -- and each one counts
        its own bytes from zero. Reported raw, the bar would fill and
        restart three times, which reads as three failures rather than one
        operation. This gives each phase its own band of a single total.

        The band is a fraction of the phase rather than a byte count,
        because the phases are not the same length: a flash scan counts four
        megabytes and a block read counts twenty kilobytes, and measuring
        both in bytes against one assumed length sent the bar backwards.
        """
        state = {"i": -1}
        steps = 1000

        def phase():
            state["i"] += 1
            base = state["i"]

            def report(done: int, total: int) -> None:
                if cb is None:
                    return
                frac = min(done / total, 1.0) if total else 1.0
                cb(int((base + frac) * steps), count * steps)
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
        active = int(self.status()["master"]["preset"])
        slots = self.preset_slots()
        if not 0 <= active < len(slots):
            raise mp.ProtocolError(
                f"the device reports preset {active + 1}, but {len(slots)} "
                f"preset slots were found in its flash")
        slot = slots[active]
        phase = self._phases(progress, 3)
        stored = self.read_stored_preset(active, progress=phase())

        # No unfiltered-output question here. Apply is where a configuration
        # starts driving what is connected, and that is where it is asked.
        # Storing only changes what the device loads at the next power-on, and
        # reaching this at all took a second, separate, deliberate button.
        words, flags = self._preset_changes(payload)
        values = mf.replace_words(stored.values, words)
        bypass = None
        if flags:
            if not stored.bypass_raw:
                # Reported rather than dropped. The flags used to fall on the
                # floor here while the summary counted them as written, so a
                # filter switched out stayed switched in at power-on and the
                # dialog said it had been stored.
                raise mp.ProtocolError(
                    f"{len(flags)} filter bypass flag(s) had to be written, "
                    f"but "
                    f"preset {active + 1} has no bypass block in this "
                    f"device's flash to write them into")
            bypass = mf.replace_bypass(stored.bypass_raw, flags)

        with self._lock:
            # Re-checked here, inside the lock, immediately before the write.
            # The preset was read seconds ago and a dialog may have been on
            # screen since; the firmware puts a block in whatever preset is
            # active, so a front-panel change in that window would commit
            # this preset's edited image into a different one.
            still = int(self._dev.master_status().get("preset", active))
            if still != active:
                raise mp.ProtocolError(
                    f"the device moved from preset {active + 1} to "
                    f"{still + 1} while this was being prepared. Nothing was "
                    f"written -- read it again and repeat the save.")
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
            # Checked here as well as in the protocol layer. That guard sits
            # on the commands that write floats to the device; this path
            # packs words straight into a flash image and never passes
            # through them. A NaN stored here is worse than a NaN applied
            # live: it survives a power cycle, and verify_preset cannot catch
            # it because a NaN read back compares equal to the NaN written.
            checked, = mp.finite([value], f"a value for 0x{addr:04x}")
            words[addr] = struct.unpack("<I", struct.pack("<f", checked))[0]

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
                if not 0 <= band["index"] < len(peq_addrs):
                    continue
                addr = peq_addrs[band["index"]]
                put_biquad(addr, band["coeff"])
                if band.get("bypass") is not None:
                    flags[addr] = bool(band["bypass"])
            bases = spec.get("xover_groups", [])
            for group in out.get("crossover", []):
                if not 0 <= group["index"] < len(bases):
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
                if not 0 <= band["index"] < len(peq_addrs):
                    continue
                addr = peq_addrs[band["index"]]
                put_biquad(addr, band["coeff"])
                if band.get("bypass") is not None:
                    flags[addr] = bool(band["bypass"])
            fir_spec, fir = spec.get("fir"), inp.get("fir")
            if (fir_spec and fir and "enable" in fir_spec
                    and fir.get("enabled") is not None):
                # The taps live in their own flash block and cannot go in
                # here, but the flag that puts them in circuit is an ordinary
                # parameter word. Leaving it out meant a FIR switched on,
                # applied and stored came back bypassed at power-on, with the
                # save summary saying nothing about it.
                put_int(fir_spec["enable"],
                        FIR_ENABLED if fir["enabled"] else FIR_BYPASSED)
            gains = spec.get("routing", [])
            gates = spec.get("routing_status", [])
            pols = spec.get("routing_polarity", [])
            for route in inp.get("routing", []):
                dest = route["index"]
                if dest < len(gains) and "gain" in route:
                    put_float(gains[dest], route["gain"])
                if "enabled" in route and dest < len(gates):
                    put_int(gates[dest], _gate(not route["enabled"]))
                if dest < len(pols) and "polarity" in route:
                    put_int(pols[dest], 1 if route["polarity"] else 0)
        return words, flags

    def _close_load_quietly(self) -> None:
        """End a FIR load on the way out of a failure, and say nothing.

        Every path that leaves write_fir after the load is open goes through
        here. The close is best-effort on purpose: whatever went wrong has an
        exception of its own on its way up, and a second one raised while
        tidying would replace the explanation with the clean-up.
        """
        try:
            self._dev.fir_load_end()
        except Exception:                              # noqa: BLE001
            pass

    def fir_capacity(self, index: int) -> int:
        """How many taps input `index`'s FIR block will hold.

        Answered from FIR_TAPS rather than from the device. Asking meant
        sending 0x39, which opens a load on the block -- so a question about
        capacity disturbed the thing it was asking about, and the two callers
        that only wanted a number (a readback, and the check that a
        coefficient file is not too long) each left a load open behind them.

        The number is a constant on this hardware: both blocks answer 2048
        whatever is in them. A device whose blocks are a different size would
        be told wrong here, which is worth less than a read that damages what
        it reads. write_fir still asks the device, because opening the load is
        what it is there to do, and uses the size in the reply.
        """
        return FIR_TAPS

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
        enabled it reads 2, the value it was set to.
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
        # The length is checked twice, and this is the cheap half: against
        # what this side believes, so an over-long filter is refused before
        # the block is touched at all. The other half is against the size the
        # device reports when the load is opened, which is the authority --
        # the constant here is only a copy of it.
        capacity = self.fir_capacity(index)
        if len(taps) > capacity:
            raise mp.ProtocolError(
                f"input {index + 1}'s FIR holds {capacity} taps and "
                f"{len(taps)} were offered. The device would take the first "
                f"{capacity} and drop the rest, which is a different filter, "
                f"so nothing was written.")
        if len(taps) < mc.FIR_MIN_TAPS:
            raise mp.ProtocolError(
                f"a FIR block takes at least {mc.FIR_MIN_TAPS} taps and "
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
        # Every tap checked before the block is touched. finite() used to
        # raise from inside the tap loop, which left the block bypassed, the
        # count reading the full filter length and only a fraction of the
        # taps written -- and no reload, so the DSP had been told about a
        # filter that was not there.
        mp.finite(list(taps), f"a FIR tap for input {index + 1}")
        sent = 0
        with self._lock:
            self._dev.write_int(spec["enable"], FIR_BYPASSED)
            # Opening the load is the first thing on the wire that touches
            # the block, and it is the only place this command is sent. The
            # size it answers with is the device's own, so the filter is
            # measured against that rather than against the constant above.
            try:
                size = self._dev.fir_load_start(index)
                if len(taps) > size:
                    raise mp.ProtocolError(
                        f"input {index + 1}'s FIR reports room for {size} "
                        f"taps and {len(taps)} were offered, so nothing was "
                        f"written")
            except Exception:
                # The command went out, so assume the load opened even when
                # the reply did not make sense. An open load outlives this
                # call and the next command runs into it, so it is closed on
                # the way out of every path from here on -- including the one
                # where the failure was fir_load_start itself, which is the
                # case that most certainly did open one.
                self._close_load_quietly()
                raise
            # The device's own size, not the constant: it is the authority,
            # and padding to the larger of the two would send a full block to
            # a device that had just said it holds less.
            payload = list(taps) + [0.0] * (size - len(taps))
            # The count stays the real length, not the padded one: it is
            # what the DSP is told the filter is, and an integer, like
            # delay's sample count. The stored preset reads 6 through i32;
            # as a float that word would read 1086324736. minidsp-rs writes
            # no such parameter -- it takes the length from the load itself --
            # but the stored preset is where this app reads a tap count back
            # from, so it is written here the way Device Console writes it.
            self._dev.write_int(spec["taps"], len(taps))
            try:
                while sent < len(payload):
                    sent += self._dev.write_fir_taps(index, payload[sent:])
                    if progress is not None:
                        progress(sent, len(payload))
            except Exception:
                # The taps that did land are a filter nobody designed. Close
                # the load, then put the count back to nothing so the block
                # does not describe a filter, and leave it bypassed.
                self._close_load_quietly()
                try:
                    self._dev.write_int(spec["taps"], 0)
                except Exception:                      # noqa: BLE001
                    pass
                raise
            self._dev.fir_load_end()
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
        if "enable" in comp and want.get("enabled") is None:
            # This routine bypasses the compressor before touching its
            # settings, and five of the six cannot be read back, so there is
            # no way to discover what to put it back to. A payload that does
            # not say leaves it switched off -- which is how adjusting a
            # limiter's threshold turned the limiter off.
            raise mp.ProtocolError(
                f"a compressor payload has to say whether the compressor "
                f"ends up enabled: it is bypassed while its settings are "
                f"written, and this device cannot be asked what it was")
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
        all -- which is a claim about shapes and numbers as well as indices,
        so coefficients are checked here for the shape the write path wants,
        for completeness, for finiteness and for stability.
        """
        n_out = len(self.amap.outputs)
        for key, specs in (("outputs", self.amap.outputs),
                           ("inputs", self.amap.inputs)):
            for ch in payload.get(key, []):
                idx = ch.get("index")
                if not isinstance(idx, int) or not 0 <= idx < len(specs):
                    raise mp.ProtocolError(
                        f"payload names {key[:-1]} {idx}, but this "
                        f"device has "
                        f"{len(specs)}. The project was probably saved "
                        f"against a different model.")
                cells = max(len(specs[idx].get("routing", [])),
                            len(specs[idx].get("routing_status", [])))
                for route in ch.get("routing", []):
                    dest = route.get("index")
                    if not isinstance(dest, int) or not 0 <= dest < n_out:
                        raise mp.ProtocolError(
                            f"routing on input {idx + 1} names output "
                            f"{dest}, but this device has {n_out}.")
                    # Against the map, not just the output count. Cells past
                    # what the map addresses were dropped by the write and
                    # counted as written -- the same treatment filter slots
                    # were hardened against a few lines below.
                    if cells and dest >= cells:
                        raise mp.ProtocolError(
                            f"routing on input {idx + 1} names output "
                            f"{dest + 1}, but this device's address map has "
                            f"{cells} mixer cell(s) for that input.")
                # Filter slots, for the same reason and with the same
                # consequence. These used to be dropped where they ran past
                # the map, so a project with more bands than the device has
                # was written short and reported as written whole -- and a
                # negative slot number indexed the address list from the end
                # and put the filter on a band nobody had named.
                spec = specs[idx]
                for field, what, addrs in (
                        ("peq", "PEQ band", spec.get("peq", [])),
                        ("crossover", "crossover group",
                         spec.get("xover_groups", []))):
                    for item in ch.get(field, []):
                        slot = item.get("index")
                        if not isinstance(slot, int) or not (
                                0 <= slot < len(addrs)):
                            raise mp.ProtocolError(
                                f"{key[:-1]} {idx} names {what} {slot}, but "
                                f"this device has {len(addrs)}.")
                        self._check_coeffs(item, f"{key[:-1]} {idx} "
                                                  f"{what} {slot}")
                        coeff = item.get("coeff")
                        if (isinstance(coeff, list)
                                and len(coeff) > XOVER_SLOTS):
                            # Truncating is the "written short and reported
                            # whole" failure the slot checks above exist to
                            # prevent, one level down.
                            raise mp.ProtocolError(
                                f"{key[:-1]} {idx} {what} {slot} carries "
                                f"{len(coeff)} sections but a group on this "
                                f"device has {XOVER_SLOTS} slots")
                self._check_numbers(ch, f"{key[:-1]} {idx}", self.rate)

    @staticmethod
    def _check_coeffs(item: dict[str, Any], where: str) -> None:
        """Every biquad in one payload entry, complete and finite."""
        coeff = item.get("coeff")
        if coeff is None:
            raise mp.ProtocolError(
                f"{where} carries no coefficients, so there is nothing to "
                f"write for it")
        # The write path wants a list for a crossover group and a dict for a
        # PEQ band, and accepting either here meant the wrong one raised
        # inside the lock with earlier channels already on the device --
        # which is what the docstring above says cannot happen.
        want_list = "crossover" in where
        if isinstance(coeff, list) != want_list:
            raise mp.ProtocolError(
                f"{where} carries "
                f"{'a list of sections' if not want_list else 'one section'}"
                f" where {'one section' if not want_list else 'a list'} was "
                f"expected")
        sections = coeff if want_list else [coeff]
        for k, bq in enumerate(sections):
            try:
                vals = _coeff_list(bq)
            except mp.ProtocolError as exc:
                raise mp.ProtocolError(f"{where}: {exc}") from None
            mp.finite(vals, f"{where} section {k}")
            # A section whose poles are on or outside the unit circle runs
            # away rather than filtering, and its output goes to a driver.
            # The window refuses one; a script went straight past.
            if not mc.biquad_is_stable(mc.as_biquad(vals)):
                raise mp.ProtocolError(
                    f"{where} section {k} has its poles on or outside the "
                    f"unit circle. That section would run away rather than "
                    f"filter, so nothing was written.")

    @staticmethod
    def _check_numbers(ch: dict[str, Any], where: str, rate: int) -> None:
        """The plain numbers on one channel, before any of them is written.

        The docstring above promises a write that either happens completely
        or not at all, and index checks alone did not deliver it: a NaN gain
        or a biquad missing a coefficient raised inside the lock, with
        earlier channels already on the device and no record of where it
        stopped. A NaN delay escaped as a bare ValueError besides.
        """
        if "gain" in ch:
            mp.finite([float(ch["gain"])], f"{where} gain")
        if "delay" in ch:
            # Either shape _delay_samples takes: milliseconds, or the
            # {secs, nanos} struct build_config_payload emits. Range-checked
            # here too, because _delay_samples raises rather than clamping
            # now -- and doing that inside the lock left earlier outputs on
            # the device with no record of where it stopped.
            value = ch["delay"]
            parts = ([value.get("secs", 0), value.get("nanos", 0)]
                     if isinstance(value, dict) else [value])
            mp.finite([float(v) for v in parts], f"{where} delay")
            _delay_samples(value, rate)
        for route in ch.get("routing", []):
            if "gain" in route:
                mp.finite([float(route["gain"])],
                          f"{where} routing gain to output "
                          f"{route.get('index')}")
        comp = ch.get("compressor") or {}
        for field, value in comp.items():
            if field == "enabled":
                continue
            mp.finite([float(value)], f"{where} compressor {field}")
        if comp and comp.get("enabled") is None:
            # _write_compressor refuses this, and refusing it there means
            # refusing it mid-write with earlier outputs already applied.
            raise mp.ProtocolError(
                f"{where} carries compressor settings but does not say "
                f"whether the compressor ends up enabled; it is bypassed "
                f"while its settings are written and this device cannot be "
                f"asked what it was")

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
        # Both only where the payload said so, as with polarity above. A
        # route entry carrying a gain and nothing else used to switch the
        # cell off on the way past -- "enabled" absent read as disabled --
        # and one carrying only a gate reset the cell's gain to 0 dB.
        # Gain first, then the gate. The other way round, switching a cell
        # on passed one write at whatever gain it happened to hold -- the
        # same window the mute, compressor and FIR paths all order around.
        if "gain" in route and out_idx < len(gains):
            self._dev.write_float(gains[out_idx], float(route["gain"]))
        # The gate carries the same weight as a mute: the driver guard proves
        # an output unfed from the cells this write switches off, so a gate
        # whose ack was lost and tolerated would stand the guard down over an
        # output that is still being fed.
        if "enabled" in route and out_idx < len(gates):
            self._dev.write_int(gates[out_idx],
                                _gate(not route["enabled"]),
                                require_ack=True)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# Floats in one biquad, which is also the granularity the device
# aligns filter-memory reads to.
BIQUAD_FLOATS = 5

GATE_MUTED, GATE_PASSING = 1, 2

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
# you like across the two *input* channels, each between 6 and 2048. Both
# ends of that distribution are inputs -- there is no output-side block to
# move taps into, and the export names exactly two FIR blocks against a
# compressor on all eight outputs. Since the two maxima add up to the
# total there is nothing to trade either: both hold 2048 at once, which is
# what GetNumFirTaps reports for each of them.
#
# The floor -- why an unused block reads 6, rather than nothing -- is
# core.FIR_MIN_TAPS, where the window can reach it too.
FIR_TAPS = 2048

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
    if not 0 <= samples <= MAX_DELAY_SAMPLES:
        # Said rather than clamped. No miniDSP offers anything near this much
        # delay and a negative one is not a thing at all, so a request
        # outside the range is a mistake somewhere upstream -- and quietly
        # writing a different number is how it stays hidden.
        raise mp.ProtocolError(
            f"a delay of {ms:g} ms is {samples} samples, and this device "
            f"which is outside the 0 to {MAX_DELAY_SAMPLES} this app "
            f"will write")
    return samples
