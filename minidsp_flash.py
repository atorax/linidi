#!/usr/bin/env python3
"""
minidsp_flash -- the stored preset, read back out of the device's flash.

Why this exists
---------------
Three things on this hardware can be written but not read, and they were
established one at a time rather than assumed, by writing a known value and
reading it back:

  PEQ coefficients   answer zero. A distinctive probe written into an
                     unrouted channel's band still read back as five zeros,
                     so the address is right and the region simply does not
                     answer. Nor are they anywhere else: every one of the
                     65536 addresses the read command can express was read,
                     and 0..5098 is the whole parameter space. The other
                     60437 answer with the address itself plus 0x1000000 --
                     a synthesised reply, not storage -- and none of the
                     space holds a known coefficient.

  Mixer gates        answer a constant 1, which is the encoding for "off".
                     Writing 2 to a cell and reading it back still gives 1,
                     so this is not a stale value, and all sixteen answer 1
                     while audio is passing.

  Bypass flags       have no parameter address at all; they are set by a
                     command that takes the address as an argument.

Everything else does read back, including two that were long assumed not to:
crossover coefficients, and both the channel mute gate and polarity, each
confirmed by writing both states to an unrouted output and reading them.

Device Console never asks. It keeps its own settings file and shows that,
which is why it can only display a tuning it made itself on the machine that
made it.

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
    # The bypass payload exactly as it was read. Saving edits this rather
    # than rebuilding it from the decoded flags, so entry order and any
    # filter kind this app does not model survive a round trip untouched.
    bypass_raw: bytes = b""

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
        preset.bypass_raw = raw
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
# writing
# ---------------------------------------------------------------------------

def header_for(kind: str, payload_len: int) -> bytes:
    """The header the firmware expects ahead of a block's payload.

    Built the way DeviceFlash::writeFlashBlocks() builds it, and checked the
    only way that really settles it: the headers this produces are byte for
    byte the ones already sitting in front of the blocks in flash.
    """
    if kind == "vals":
        total = VALS_HEADER_LEN + payload_len
        b = total.to_bytes(4, "big")
        return bytes([(b[1] & 0xF0) | VALS_TAG, b[2], b[3]]) + VALS_MAGIC
    if kind == "bypass":
        total = BYPASS_HEADER_LEN + payload_len
        b = total.to_bytes(4, "big")
        return bytes([BYPASS_TAG, b[2], b[3]])
    raise ValueError(f"unknown block kind {kind!r}")


def replace_words(values: bytes, changes: dict[int, int]) -> bytes:
    """A values image with some parameters replaced, and the rest untouched.

    This is the whole reason a save reads before it writes. The image covers
    every parameter the DSP has, and the address maps describe a fraction of
    them -- compressor and FIR memory among the gaps. Building an image from
    only what this app models would write zeros over everything it does not,
    so what is stored is edited instead of regenerated.
    """
    buf = bytearray(values)
    for addr, word in changes.items():
        off = addr * WORD
        if off < 0 or off + WORD > len(buf):
            raise ValueError(
                f"parameter {addr} is outside the stored image "
                f"({len(buf) // WORD} words)")
        struct.pack_into("<I", buf, off, word & 0xFFFFFFFF)
    return bytes(buf)


def replace_bypass(raw: bytes, changes: dict[int, bool]) -> bytes:
    """A bypass payload with some flags flipped, and the rest untouched.

    Entries keep their order and their kind byte; only the bypass bit moves.
    An address the block does not already list cannot be added, because its
    position in the table is the firmware's to choose, not ours.
    """
    buf = bytearray(raw)
    seen = set()
    for i in range(0, len(buf) - 2, 3):
        addr = (buf[i + 1] << 8) | buf[i + 2]
        if addr in changes:
            seen.add(addr)
            if changes[addr]:
                buf[i] |= BYPASS_BIT
            else:
                buf[i] &= ~BYPASS_BIT
    missing = set(changes) - seen
    if missing:
        raise ValueError(
            f"the stored bypass table does not list "
            f"{sorted(missing)[:8]}; refusing to invent entries for it")
    return bytes(buf)


def write_block(dev: mp.MiniDSP, block_id: int, kind: str, payload: bytes,
                progress: Callable[[int, int], None] | None = None) -> None:
    """Write one whole flash block: header, payload, then finish."""
    dev.write_flash_block(block_id, header_for(kind, len(payload)),
                          header=True)
    sent = 0
    while sent < len(payload):
        sent += dev.write_flash_block(block_id, payload[sent:])
        if progress is not None:
            progress(sent, len(payload))
    dev.finish_flash_block(block_id)


def save_preset(dev: mp.MiniDSP, slot: Slot, values: bytes,
                bypass: bytes | None = None,
                progress: Callable[[int, int], None] | None = None) -> None:
    """Write a preset's two blocks to the device.

    The blocks go to whichever preset is *active*, not to `slot`: the write
    command names a block, never an address, and the firmware puts it in the
    running preset. `slot` says where to read the result back from, so it has
    to be the active one, and the caller is responsible for that being true.
    """
    if len(values) != slot.vals.payload_len:
        raise ValueError(
            f"values image is {len(values)} bytes but the block holds "
            f"{slot.vals.payload_len}")
    write_block(dev, mp.FLASH_BLOCK_PRESET_VALS, "vals", values, progress)
    if bypass is not None:
        if slot.bypass is None:
            raise ValueError("this slot has no bypass block to write")
        if len(bypass) != slot.bypass.payload_len:
            raise ValueError(
                f"bypass payload is {len(bypass)} bytes but the block holds "
                f"{slot.bypass.payload_len}")
        write_block(dev, mp.FLASH_BLOCK_PRESET_BYPASS, "bypass", bypass)


def verify_preset(dev: mp.MiniDSP, slot: Slot, values: bytes,
                  bypass: bytes | None = None,
                  progress: Callable[[int, int], None] | None = None) -> None:
    """Read both blocks back and insist they match, byte for byte.

    Device Console's own verify step reads a single EEPROM key and checks it
    against a constant, which says nothing about whether the preset arrived
    intact. Since the blocks can be read back, they are.
    """
    got = _read_span(dev, slot.vals.payload, slot.vals.payload_len, progress)
    if got != values:
        bad = next((i for i, (a, b) in enumerate(zip(got, values)) if a != b),
                   min(len(got), len(values)))
        raise mp.ProtocolError(
            f"the values block read back differently from what was written, "
            f"first at parameter {bad // WORD}: the preset on the device is "
            f"not what was meant to be saved")
    if bypass is not None and slot.bypass is not None:
        got = _read_span(dev, slot.bypass.payload, slot.bypass.payload_len)
        if got != bypass:
            raise mp.ProtocolError(
                "the bypass block read back differently from what was "
                "written: the preset on the device is not what was meant "
                "to be saved")


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
