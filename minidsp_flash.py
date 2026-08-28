#!/usr/bin/env python3
"""
minidsp_flash -- the stored preset, read back out of the device's flash.

Why this exists
---------------
Filter memory does not read back. Asking the device for a PEQ coefficient
returns zero, whatever is actually loaded, so the running curve cannot be
recovered through CMD_READ_DSP_PARAM. Bypass flags and mixer gates are no
better. Device Console solves this by not asking: it keeps its own settings
file and shows that, which is why it can only display a tuning it made itself
on a machine it made it on.

The data is on the device, though. Saving a preset writes two flash blocks,
and CMD_READ_FLASH_FULL_ADDR reaches the whole 24-bit space, so the stored
preset can be read back and decoded in full -- coefficients, bypass flags,
gains, delays and gates. Nothing in the vendor's software does this.

Block formats
-------------
Both come from DeviceFlash::writeFlashBlocks() and UserPresets::
valuesToMemoryBuffer(), and both were confirmed byte-for-byte against a
Device Console export: every one of the 1001 addressed words and all 116
bypass flags matched on three slots of a Flex 8.

  presetVals    header [len[1]&0xf0|0x04, len[2], len[3], 0x13, 0x0f, 0, 0]
                payload is an image of DSP parameter memory: the 32-bit word
                for parameter `addr` sits at byte offset addr*4, little
                endian. Fields keep the encoding the DSP uses -- a gain is a
                float in dB, a delay is an integer sample count, a gate is
                the integer 1 or 2.

  presetBypass  header [0x05, len[2], len[3]]
                payload is three bytes per filter:
                  [0]   0x01 peq, 0x04 bandpass, plus 0x80 when bypassed
                  [1:3] the filter's parameter address, uint16 big-endian

`len` is a uint32BE of header + payload, of which only the low 16 bits are
stored, so a block cannot declare more than 64 KiB.

What this is and is not
-----------------------
This reads the *stored* preset -- what the device loads at power-on. It is
not necessarily what the DSP is running now: a parameter written live changes
the running value and leaves flash alone. Where the two can disagree, callers
are told which one they asked for rather than the difference being smoothed
over.

How exact the answer is
-----------------------
The coefficients come back perfectly: all 100 words of a Flex 8's input PEQs
were bit-identical to the same filters in a Device Console export. Turning
them back into a frequency and a Q is where a little is lost, and it is lost
in the device rather than here. The device stores float32, and inverting a
float32 section is not quite the inverse of designing one: a band designed at
49 Hz Q 0.7 reads back as 49.10 Hz Q 0.701, and one at 15364 Hz reads back
exact. Quantising an exact design to float32 and inverting it reproduces
those same figures, so the loss happened when the filter was stored. Gains
are unaffected. Nothing reading this device can do better, the vendor's own
software included.

License: Apache-2.0
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import minidsp_protocol as mp

# Block headers were observed only ever at 256-byte boundaries, on every slot
# of every preset. Scanning at this stride is what makes locating them
# affordable; it is an alignment the firmware chose, not one we impose, so
# `scan_blocks` takes it as an argument rather than assuming it everywhere.
BLOCK_ALIGN = 0x100

VALS_HEADER_LEN = 7
BYPASS_HEADER_LEN = 3
VALS_MAGIC = b"\x13\x0f\x00\x00"    # header bytes 3..7
VALS_TAG_MASK, VALS_TAG = 0x0F, 0x04
BYPASS_TAG = 0x05

# Filter kinds in a bypass entry, and the bit that marks one bypassed.
FILTER_PEQ, FILTER_BPF = 0x01, 0x04
FILTER_KINDS = (FILTER_PEQ, FILTER_BPF)
BYPASS_BIT = 0x80

# Bytes per parameter word in the values image.
WORD = 4

# The largest read the device serves in one reply. Every scan and bulk read
# here is bound by round trips rather than bytes, so this is also the unit
# that decides how long a dump takes.
MAX_READ = 0x3A


@dataclass(frozen=True)
class Block:
    """One located flash block, before it has been read."""

    kind: str                 # "vals" or "bypass"
    header: int               # flash address of the header
    declared: int             # header + payload, as the block declares it

    @property
    def header_len(self) -> int:
        return VALS_HEADER_LEN if self.kind == "vals" else BYPASS_HEADER_LEN

    @property
    def payload(self) -> int:
        return self.header + self.header_len

    @property
    def payload_len(self) -> int:
        return max(0, self.declared - self.header_len)


@dataclass(frozen=True)
class Slot:
    """A preset slot: its values block and the bypass block that follows."""

    index: int
    vals: Block
    bypass: Block | None = None


@dataclass
class StoredPreset:
    """A decoded preset slot, addressed the way the address maps are."""

    index: int
    values: bytes = b""
    bypass: dict[int, bool] = field(default_factory=dict)
    kinds: dict[int, int] = field(default_factory=dict)

    # -- parameter access -------------------------------------------------

    def has(self, addr: int) -> bool:
        return 0 <= addr and (addr + 1) * WORD <= len(self.values)

    def word(self, addr: int) -> int | None:
        """The raw 32-bit word for a parameter address."""
        if not self.has(addr):
            return None
        off = addr * WORD
        return struct.unpack_from("<I", self.values, off)[0]

    def f32(self, addr: int) -> float | None:
        """A parameter read as a float, which is what gains are."""
        if not self.has(addr):
            return None
        return struct.unpack_from("<f", self.values, addr * WORD)[0]

    def i32(self, addr: int) -> int | None:
        """A parameter read as an integer, which is what gates and delays are.

        Same bytes as f32(); which one is right depends on the field, and the
        address maps are what say so.
        """
        return self.word(addr)

    def floats(self, addr: int, count: int) -> list[float]:
        """`count` consecutive parameters as floats, for a biquad block."""
        out = []
        for k in range(count):
            v = self.f32(addr + k)
            if v is None:
                break
            out.append(v)
        return out

    def is_bypassed(self, addr: int) -> bool | None:
        """Whether the filter at this address is bypassed, or None if the
        stored bypass table does not mention it."""
        return self.bypass.get(addr)


# ---------------------------------------------------------------------------
# locating
# ---------------------------------------------------------------------------

def _classify(head: bytes) -> tuple[str, int] | None:
    """A block kind and declared length, if these bytes start a block."""
    if len(head) >= VALS_HEADER_LEN:
        if (head[0] & VALS_TAG_MASK) == VALS_TAG and \
                head[3:7] == VALS_MAGIC:
            return "vals", (head[1] << 8) | head[2]
    if len(head) >= BYPASS_HEADER_LEN and head[0] == BYPASS_TAG:
        declared = (head[1] << 8) | head[2]
        # A bypass block is a whole number of three-byte entries and is never
        # empty. Without this a stray 0x05 matches roughly once per 64 KiB.
        payload = declared - BYPASS_HEADER_LEN
        if payload >= 3 and payload % 3 == 0:
            return "bypass", declared
    return None


def flash_size(dev: mp.MiniDSP, limit: int = 0x1000000) -> int:
    """Where the address space starts repeating itself.

    Reads above the end of the part come back as the low addresses again, so
    the real size is the smallest power of two whose contents match address
    zero. Scanning the aliased copies as well would multiply the cost of
    locating anything by four on a Flex 8.
    """
    head = dev.read_flash(0, MAX_READ)
    size = 0x40000
    while size < limit:
        if dev.read_flash(size, MAX_READ) == head:
            return size
        size <<= 1
    return limit


def _confirm(dev: mp.MiniDSP, block: Block, end: int) -> bool:
    """Read enough of a candidate to be sure it is really a block.

    A values header carries four magic bytes and is safe on its own. A bypass
    header is one byte and a length, which a stray 0x05 satisfies about once
    in three, so its payload is read back and every entry checked. That costs
    a handful of round trips on a handful of candidates and turns a guess into
    a decision.
    """
    if block.payload_len <= 0 or block.payload + block.payload_len > end:
        return False
    if block.kind == "vals":
        return True
    try:
        raw = _read_span(dev, block.payload, block.payload_len)
    except mp.ProtocolError:
        return False
    if len(raw) != block.payload_len:
        return False
    return all(raw[i] & ~BYPASS_BIT in FILTER_KINDS
               for i in range(0, len(raw) - 2, 3))


def scan_blocks(dev: mp.MiniDSP, end: int, start: int = 0,
                align: int = BLOCK_ALIGN,
                progress: Callable[[int, int], None] | None = None
                ) -> list[Block]:
    """Every preset block between `start` and `end`.

    Deliberately a flat scan of the whole part rather than anything cleverer.
    A coarse pass to find populated regions first would be four times quicker
    and would also miss blocks: a bypass block is under a kilobyte, and the
    one on this Flex 8 sits at 0x044c00, inside a 4 KiB page whose first bytes
    read as erased. The scan is the slow half of reading a preset, so callers
    are expected to do it once and keep the answer.
    """
    found: list[Block] = []
    head_len = max(VALS_HEADER_LEN, BYPASS_HEADER_LEN)
    total = max(1, (end - start) // align)
    for n, addr in enumerate(range(start, end, align)):
        try:
            head = dev.read_flash(addr, head_len)
        except mp.ProtocolError:
            continue
        hit = _classify(head)
        if hit is not None:
            block = Block(kind=hit[0], header=addr, declared=hit[1])
            if _confirm(dev, block, end):
                found.append(block)
        if progress is not None and n % 256 == 0:
            progress(n, total)
    if progress is not None:
        progress(total, total)
    return found


def pair_slots(blocks: Iterable[Block]) -> list[Slot]:
    """Group located blocks into slots, in address order.

    A slot's bypass block is the first one after its values block and before
    the next slot's, which is the only relationship the layout actually
    guarantees. The gap between slots is not constant -- on a Flex 8 the first
    is 0x1c200 and the rest are 0x19300 -- so nothing here computes an address
    from a stride.
    """
    ordered = sorted(blocks, key=lambda b: b.header)
    vals = [b for b in ordered if b.kind == "vals"]
    slots: list[Slot] = []
    for i, block in enumerate(vals):
        nxt = vals[i + 1].header if i + 1 < len(vals) else None
        mate = next(
            (b for b in ordered
             if b.kind == "bypass" and b.header > block.header
             and (nxt is None or b.header < nxt)),
            None)
        slots.append(Slot(index=i, vals=block, bypass=mate))
    return slots


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _read_span(dev: mp.MiniDSP, addr: int, length: int,
               progress: Callable[[int, int], None] | None = None) -> bytes:
    out = bytearray()
    while len(out) < length:
        n = min(MAX_READ, length - len(out))
        out += dev.read_flash(addr + len(out), n)
        if progress is not None:
            progress(len(out), length)
    return bytes(out)


def read_preset(dev: mp.MiniDSP, slot: Slot,
                progress: Callable[[int, int], None] | None = None
                ) -> StoredPreset:
    """Read and decode one preset slot."""
    values = _read_span(dev, slot.vals.payload, slot.vals.payload_len,
                        progress)
    preset = StoredPreset(index=slot.index, values=values)
    if slot.bypass is not None:
        raw = _read_span(dev, slot.bypass.payload, slot.bypass.payload_len)
        preset.bypass, preset.kinds = decode_bypass(raw)
    return preset


def decode_bypass(raw: bytes) -> tuple[dict[int, bool], dict[int, int]]:
    """A bypass payload as {address: bypassed} and {address: filter kind}."""
    flags: dict[int, bool] = {}
    kinds: dict[int, int] = {}
    for i in range(0, len(raw) - 2, 3):
        tag = raw[i]
        kind = tag & ~BYPASS_BIT
        if kind not in FILTER_KINDS:
            continue
        addr = (raw[i + 1] << 8) | raw[i + 2]
        flags[addr] = bool(tag & BYPASS_BIT)
        kinds[addr] = kind
    return flags, kinds


# ---------------------------------------------------------------------------
# remembering where the blocks were
# ---------------------------------------------------------------------------

# Bumped if the stored shape changes, so an old file is ignored rather than
# misread.
CACHE_VERSION = 1


def cache_path() -> Path:
    """Where located block addresses are kept between runs."""
    return Path.home() / ".config" / "linidi" / "flash-blocks.json"


def device_key(info: "mp.DeviceInfo") -> str:
    """What a cached block map belongs to.

    Serial is part of it because block placement is not uniform even within a
    model -- the gap between the first two slots on this Flex 8 differs from
    the rest -- and firmware is part of it because a firmware write is exactly
    the thing that would move them.
    """
    return (f"{info.hw_id}-{info.dsp_version}-{info.serial}"
            f"-{info.fw_major}.{info.fw_minor}")


def load_slots(key: str, path: Path | None = None) -> list[Slot] | None:
    """Slots located on an earlier run, or None if nothing usable is stored."""
    path = path or cache_path()
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if blob.get("version") != CACHE_VERSION:
        return None
    entry = (blob.get("devices") or {}).get(key)
    if not entry:
        return None
    try:
        slots = []
        for rec in entry:
            byp = rec.get("bypass")
            slots.append(Slot(
                index=rec["index"],
                vals=Block("vals", rec["vals"][0], rec["vals"][1]),
                bypass=Block("bypass", byp[0], byp[1]) if byp else None))
        return slots or None
    except (KeyError, TypeError, IndexError):
        return None


def save_slots(key: str, slots: list[Slot], path: Path | None = None) -> None:
    """Record located slots, leaving other devices' entries alone."""
    path = path or cache_path()
    try:
        blob = json.loads(path.read_text())
        if blob.get("version") != CACHE_VERSION:
            blob = {}
    except (OSError, ValueError):
        blob = {}
    devices = blob.get("devices")
    if not isinstance(devices, dict):
        devices = {}
    devices[key] = [
        {"index": s.index,
         "vals": [s.vals.header, s.vals.declared],
         "bypass": ([s.bypass.header, s.bypass.declared]
                    if s.bypass else None)}
        for s in slots
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(
            {"version": CACHE_VERSION, "devices": devices}, indent=2))
    except OSError:
        # A cache that cannot be written costs a rescan, nothing more.
        pass
