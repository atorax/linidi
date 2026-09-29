#!/usr/bin/env python3
"""
linidi.protocol -- talk to miniDSP hardware directly over USB HID.

Packet format
-------------
Every command is framed as:

    [ size, cmd, arg0 .. argN-1, checksum ]

where ``size`` counts the command byte, the arguments and the checksum byte,
and ``checksum`` is the plain 8-bit sum of every preceding byte. The frame is
written to the HID interface padded to the report length.

Write mode byte
---------------
Parameter writes carry a mode byte ahead of the address. This app sends
**0xa0**, which takes effect immediately. Measured on a Flex 8: a mixer
disable written with 0xa0 drops the channel from -19.1 dB to silence at once,
and the same write with 0x80 changes nothing audible. Device Console calls
0x80 "withSave"; minidsp-rs also emits 0x80, by a different route. Reported
upstream in August 2026.

A parameter write changes what the device is doing now and leaves the stored
preset alone -- measured, not assumed: after applying -7.18 dB the running
parameter reads -7.18 and the stored one still reads -7.0. Applying, not
writing: a raw write of -7.18 lands on -7.496, and it is the correction loop
in core.apply_project that gets it to the number asked for. Outliving a power
cycle means writing the preset blocks, which is save_stored_preset().

Attribution
-----------
Protocol details were established by observing this hardware and by studying
miniDSP's Device Console, which ships as readable JavaScript. Command
numbering and framing are facts about the device, reimplemented here from
scratch. Address maps derive from minidsp-rs
(https://github.com/mrene/minidsp-rs, Apache-2.0); see NOTICE.

License: Apache-2.0
"""

from __future__ import annotations

import math
import struct
import sys
import time
from dataclasses import dataclass
from typing import Iterable, Sequence

try:
    import hid
except ImportError:                                    # pragma: no cover
    hid = None

try:
    import usb.core
    import usb.util
except ImportError:                                    # pragma: no cover
    usb = None

VENDOR_ID = 0x2752
REPORT_LEN = 64

# Command codes, as named by the vendor's own implementation. The whole table
# is recorded rather than only the codes this app sends: it is the useful
# artefact of reading Device Console, and the next person to extend this
# should not have to derive it again. Unused entries are reference, not dead
# code.
#
# Checked member by member against Device Console's own DspCmdCode enum: all
# 39 codes, every value matching.
CMD_RESET = 0x03
CMD_WRITE_FLASH = 0x04
CMD_READ_FLASH = 0x05
CMD_WRITE_FLASH_BLOCK = 0x06
CMD_FINISH_FLASH_BLOCK = 0x07
CMD_LOAD_DSP_PROGRAM = 0x11
CMD_LOAD_DSP_PARAM = 0x13
CMD_READ_DSP_PARAM = 0x14
CMD_MASTER_MUTE = 0x17
CMD_BYPASS_DSP_FILTER = 0x19
CMD_OLED_BRIGHTNESS = 0x1A
CMD_OLED_IDLE_TIME = 0x1B
CMD_DRE_STATUS = 0x1E
CMD_BLUETOOTH_AT_CMD = 0x21
CMD_RELOAD_E2PROM_HDR = 0x24
CMD_CHANGE_PRESET = 0x25
CMD_COPY_PRESET = 0x27
CMD_SET_DSP_FILTER_BIQUADS = 0x30
CMD_PIC_VERSION = 0x31
CMD_CHANGE_AUDIO_SRC = 0x34
# These three are one operation, not three commands. 0x39 opens a load and
# answers with the size the block will take, 0x3A carries coefficients, and
# 0x3B closes it. They were named GetNumFirTaps, WriteFirTapsToFlash and
# ReloadDspParam here, which reads as a query, a write and a refresh that can
# each be sent on their own -- and 0x39 was therefore sent to ask a question,
# from a read and from a bounds check, opening sessions that nothing closed.
# The names below are minidsp-rs's, confirmed against the binaries it installs.
CMD_FIR_LOAD_START = 0x39
CMD_FIR_LOAD_DATA = 0x3A
CMD_FIR_LOAD_END = 0x3B
CMD_WRITE_FLASH_FULL_ADDR = 0x3C
CMD_READ_FLASH_FULL_ADDR = 0x3D
CMD_ENTER_BOOTLOADER = 0x3E
CMD_BYPASS_FIR = 0x3F
CMD_DSP_VERSION = 0x40
CMD_MASTER_VOL = 0x42
CMD_GEN_NOISE_CH = 0x45
CMD_READ_DFLASH_ID = 0x46
CMD_WRITE_DFLASH_ID = 0x49
CMD_ERASE_FLASH = 0x50
CMD_COM_FW_UPGRADE = 0x52
CMD_EARC = 0x53
CMD_LOG_CTRL = 0x54
CMD_RESET_COMPLETED = 0xAA
CMD_PRESET_CHANGE_COMPLETED = 0xAB
CMD_WISA = 0xF0

# The first byte of a reply is its status, and the second echoes the command
# it answers. Anything else is a data reply whose first byte is the payload
# size. Taken from Device Console's own decoder and confirmed against a Flex 8:
# a no-op write answers 01, and an undefined opcode answers 00.
ACK_ERROR = 0x00
ACK_OK = 0x01
ACK_COMPLETED = 0x02      # ResetCompleted / PresetChangeCompleted follow
ACK_BAD_SIZE = 0xFF       # the device reporting a malformed payload size

# How many address bytes each read command echoes back at the head of its
# reply. Matching them is what distinguishes a real answer from a stale one
# left in the endpoint's queue, since every reply also starts with the command
# byte and so passes a naive check.
READ_ECHO_BYTES = {
    CMD_READ_FLASH: 2,
    CMD_READ_DSP_PARAM: 2,
    CMD_READ_FLASH_FULL_ADDR: 3,
    # The tap-count reply echoes nothing but the command, so there is no
    # argument prefix to match against; one byte of echo would be the
    # command byte the framing already checks.
}

# Taps carried by one FirLoadData packet. Device Console caps its own
# packets at this and truncates anything longer, which is what makes the
# reply's count meaningful: it says how many of the ones offered were
# taken.
#
# Fourteen is Device Console's cap, not the report's. Fourteen float32
# plus the opcode, the index and the framing make 60 bytes and fifteen
# make 64, which still fits -- _check_frame refuses only past 64. The
# arithmetic here used to say 61 and 65 and blame the report for a limit
# that is really somebody else's convention. minidsp-rs sends fifteen.
# Fourteen is kept because the device answers with what it took, so the
# conservative number costs one extra packet per 2048 taps and nothing
# else.
MAX_FIR_TAPS_PER_PACKET = 14

# How long to wait for a write to be acknowledged, and how many unacknowledged
# writes in a row mean the device has stopped listening rather than merely
# being slow.
#
# Two seconds because this device is unhurried about several operations -- a
# master mute is the obvious one -- and the cost of waiting is paid only when
# an ack is genuinely late, while the cost of not waiting is counting a slow
# reply as a lost one. Eight of those in a row used to raise "the device
# stopped acknowledging writes" at a device that was working perfectly.
ACK_TIMEOUT_MS = 2000

# The device serves at most this many floats in one reply.
MAX_FLOATS_PER_READ = 14
MAX_UNACKED_WRITES = 8

# How many replies command() will look past while hunting the one that
# answers it. A late acknowledgement from a slow write sits in front of the
# one wanted; two is enough for the runs this app sends, and a third is
# cheap insurance.
STALE_REPLIES = 3

# The second byte of a preset change. Device Console distinguishes all three:
# it switches without a reset while loading configurations into slots, reloads
# when re-selecting the slot already active, and switches when the user picks
# a different one.
PRESET_NO_RESET = 0
PRESET_RELOAD = 1
PRESET_SWITCH = 2

# Parameter-write modes. See the module docstring: 0xa0 is the one that works.
MODE_APPLY = 0xA0
MODE_ALT = 0x80

# Flash blocks, as Device Console's FlashBlockId enum numbers them. Block 0 is
# the DSP firmware and is named here only so that it can be refused: nothing
# in this app has any business writing it, and a stray 0 would otherwise be a
# valid-looking argument that starts a firmware write.
FLASH_BLOCK_DSP_FW = 0x00
FLASH_BLOCK_PRESET_VALS = 0x01
FLASH_BLOCK_PRESET_BYPASS = 0x02
WRITABLE_FLASH_BLOCKS = (FLASH_BLOCK_PRESET_VALS, FLASH_BLOCK_PRESET_BYPASS)

# The block id is sent bare on the header chunk and with this bit set on every
# chunk of payload after it, which is how the device tells the two apart.
FLASH_BLOCK_DATA = 0x80

# The device takes at most this much block payload per command.
MAX_FLASH_BLOCK_CHUNK = 32

# EEPROM addresses (byte-addressed space, reachable via CMD_READ_FLASH).
# Every one of these matches the address Device Console reads for the same
# thing, checked against its own source.
#
# "EEPROM" is a name, not a separate part. This window is the last 64 KiB of
# the same flash the presets live in: read 0x0000..0xffff with CMD_READ_FLASH
# and 0x3f0000..0x3fffff with CMD_READ_FLASH_FULL_ADDR and every one of the
# 65536 bytes matches. Two commands, one region, differing only in how wide
# an address they take.
EE_DSP_ID = 0xFFA1            # Device Console calls this the DSP id
EE_DEFAULT_CONFIG = 0xFFA3    # 9 bytes; byte 0 is the key below
EE_VERIFY_KEY = 0xFFA3        # "DSP Program Verification Key"; vendor
                              # rejects anything above 13. Reads 3 here.
EE_MOD_TOKEN = 0xFFC8         # 4 bytes per preset, four presets
EE_MOD_TOKEN_SIZE = 4
EE_PRESET = 0xFFD8
EE_SOURCE = 0xFFD9
EE_MASTER_VOLUME = 0xFFDA
EE_MUTE = 0xFFDB
EE_MASTER_FIR_BYPASS = 0xFFE0
EE_SERIAL32 = 0xFFFC          # u32 variant
EE_SERIAL16 = 0xFFFE          # u16, big-endian; used by the Flex family


class ProtocolError(RuntimeError):
    pass


# The message a write raises when the person at the keyboard declined it. A
# declined write is not a failure and must not be reported as one, and the
# worker signal that carries an error to the window carries a string rather
# than the exception -- so this is what the window matches on. A constant
# because four places have to agree on it exactly: raised from here and
# from core, and matched in the window.
WRITE_CANCELLED = "write cancelled"


def _check_frame(data: bytes) -> None:
    """Refuse a frame too long for one report.

    Both transports used to pad with a negative repeat count, which yields an
    empty string, and then slice back to the report length -- so an oversized
    frame was quietly truncated and sent as a malformed packet. Nothing this
    app builds comes close to the limit, but silently corrupting a write to
    audio hardware is not a failure mode worth leaving in place.
    """
    if len(data) > REPORT_LEN:
        raise ProtocolError(
            f"frame of {len(data)} bytes exceeds the {REPORT_LEN}-byte report")


def frame(cmd: int, args: Iterable[int] = ()) -> bytes:
    """Wrap a command and its arguments in the device's packet format."""
    body = bytes(args)
    size = 1 + len(body) + 1                 # cmd + args + checksum
    buf = bytearray(1 + size)
    buf[0] = size
    buf[1] = cmd
    buf[2:2 + len(body)] = body
    buf[2 + len(body)] = sum(buf) & 0xFF
    return bytes(buf)


def addr_bytes(addr: int) -> bytes:
    """Parameter addresses go on the wire big-endian."""
    return struct.pack(">H", addr)


def finite(values: Iterable[float], what: str) -> list[float]:
    """Numbers on their way to the DSP, checked for being numbers.

    struct.pack encodes NaN and infinity without complaint -- NaN becomes
    0000c07f and goes out like any other coefficient -- so nothing below this
    point would refuse them. What the DSP then does with a NaN in a filter is
    not documented anywhere readable, and this is not hardware to find out on.

    They are not hard to produce. Python's json accepts NaN and Infinity by
    default, though strict JSON has neither, so a hand-edited project file is
    enough; so is a malformed REW export, or arithmetic on an empty band.

    The check is here rather than at the point each number is designed
    because they arrive by several routes -- designed filters, imported REW
    and Device Console files, raw biquads typed into the Biquad tab -- and
    every command that writes to the running DSP passes through here.

    It is not the only way out. Writing the stored preset builds a flash
    image word by word and never reaches these commands, so native's
    put_float calls this too.
    """
    out = []
    for v in values:
        f = float(v)
        if not math.isfinite(f):
            raise ProtocolError(
                f"refusing to send {f} to the device as {what}")
        out.append(f)
    return out


@dataclass
class DeviceInfo:
    path: bytes
    vendor_id: int
    product_id: int
    serial: int | None = None
    fw_major: int | None = None
    fw_minor: int | None = None
    hw_id: int | None = None
    dsp_version: int | None = None

    def __str__(self) -> str:
        return (f"miniDSP {self.product_id:#06x} serial {self.serial} "
                f"hw_id {self.hw_id} dsp {self.dsp_version} "
                f"fw {self.fw_major}.{self.fw_minor}")


def discover() -> list[DeviceInfo]:
    """Every miniDSP HID interface currently attached."""
    if hid is None:
        raise ProtocolError("hidapi is not installed")
    seen = {}
    for d in hid.enumerate(VENDOR_ID, 0):
        # A device exposes several interfaces; the control one accepts our
        # reports. Prefer the highest interface number, which is where the
        # vendor-specific endpoint lives on the devices seen so far.
        key = (d["vendor_id"], d["product_id"], d.get("serial_number"))
        cur = seen.get(key)
        if (cur is None or d.get("interface_number", -1)
                > cur.get("interface_number", -1)):
            seen[key] = d
    return [DeviceInfo(path=d["path"], vendor_id=d["vendor_id"],
                       product_id=d["product_id"]) for d in seen.values()]


class LibUsbTransport:
    """Raw HID over libusb.

    Used in preference to hidraw because the HID interface often has no kernel
    driver bound: anything that has driven this device through libusb detaches
    usbhid and does not put it back, so /dev/hidrawN simply does not exist.
    Going through libusb works either way, and can reattach the kernel driver
    on the way out.
    """

    def __init__(self, product_id: int | None = None, timeout_ms: int = 2000):
        if usb is None:
            raise ProtocolError("pyusb is not installed")
        kwargs = {"idVendor": VENDOR_ID}
        if product_id is not None:
            kwargs["idProduct"] = product_id
        self.dev = usb.core.find(**kwargs)
        if self.dev is None:
            raise ProtocolError("no miniDSP device found on USB")
        self.timeout_ms = timeout_ms
        self.product_id = int(self.dev.idProduct)
        self.ep_in = self.ep_out = None
        self.interface = None
        self._detached = False

        for cfg in self.dev:
            for intf in cfg:
                if intf.bInterfaceClass != 3:          # HID
                    continue
                ins = [e for e in intf
                       if usb.util.endpoint_direction(e.bEndpointAddress)
                       == usb.util.ENDPOINT_IN]
                outs = [e for e in intf
                        if usb.util.endpoint_direction(e.bEndpointAddress)
                        == usb.util.ENDPOINT_OUT]
                if ins and outs:
                    self.interface = intf.bInterfaceNumber
                    self.ep_in, self.ep_out = ins[0], outs[0]
                    break
            if self.interface is not None:
                break
        if self.interface is None:
            raise ProtocolError("no HID interface with both endpoints")

        try:
            if self.dev.is_kernel_driver_active(self.interface):
                self.dev.detach_kernel_driver(self.interface)
                self._detached = True
        except (NotImplementedError, usb.core.USBError):
            pass
        # Only one process can hold the control interface. Say so plainly
        # rather than letting the caller block on a device that will never
        # answer -- a second copy of the app is the usual cause.
        try:
            usb.util.claim_interface(self.dev, self.interface)
        except usb.core.USBError as exc:
            # Put the kernel driver back and let go of the handle before
            # raising. The exception escapes __init__, so this object is
            # discarded and close() -- the only other place that reattaches --
            # never runs: /dev/hidrawN would stay gone for everyone until the
            # device was replugged, which also denies the hidapi fallback the
            # node it is about to look for.
            self.close()
            raise ProtocolError(
                "could not claim the device. Usually another program has it "
                "-- another copy of this app, or minidspd -- but a missing "
                "udev rule looks the same here. "
                f"[{exc}]") from exc

    def write(self, data: bytes) -> None:
        _check_frame(data)
        pkt = bytes(data).ljust(REPORT_LEN, b"\x00")
        self.ep_out.write(pkt, self.timeout_ms)

    def read(self, timeout_ms: int | None = None) -> bytes:
        # `is None` rather than `or`: a caller asking for a 0 ms read means
        # "do not block", and `or` would quietly give it the full default.
        wait = self.timeout_ms if timeout_ms is None else timeout_ms
        try:
            return bytes(self.ep_in.read(REPORT_LEN, wait))
        except usb.core.USBTimeoutError:
            # Nothing arrived in time. That is what an empty read means here,
            # and it is what the hidraw transport already returns, so the
            # layers above can retry rather than see an exception from a
            # library they know nothing about. Letting it escape meant a slow
            # reply bypassed every retry in exchange() and surfaced as a raw
            # usb error -- which is what a master mute did, the device taking
            # its time over that one and several others.
            return b""

    def close(self) -> None:
        try:
            usb.util.release_interface(self.dev, self.interface)
            if self._detached:
                self.dev.attach_kernel_driver(self.interface)
        except Exception:                              # noqa: BLE001
            pass
        finally:
            usb.util.dispose_resources(self.dev)


class HidRawTransport:
    """Raw HID through hidapi.

    On Linux this reaches the device through a /dev/hidrawN node, and is the
    fallback: see LibUsbTransport for why libusb is tried first there. On
    Windows it goes through the operating system's own HID API and is the only
    transport that can work at all, so the order is reversed -- see MiniDSP's
    constructor.
    """

    def __init__(self, path: bytes | None = None, timeout_ms: int = 2000):
        if hid is None:
            raise ProtocolError("hidapi is not installed")
        if path is None:
            found = discover()
            if not found:
                raise ProtocolError("no miniDSP hidraw node")
            path = found[0].path
        self.timeout_ms = timeout_ms
        self.product_id = 0
        self.dev = hid.device()
        self.dev.open_path(path)
        self.dev.set_nonblocking(0)

    def write(self, data: bytes) -> None:
        _check_frame(data)
        # hidapi expects a leading report id byte.
        self.dev.write(b"\x00" + bytes(data).ljust(REPORT_LEN, b"\x00"))

    def read(self, timeout_ms: int | None = None) -> bytes:
        wait = self.timeout_ms if timeout_ms is None else timeout_ms
        return bytes(self.dev.read(REPORT_LEN, wait))

    def close(self) -> None:
        try:
            self.dev.close()
        except Exception:                              # noqa: BLE001
            pass


class MiniDSP:
    """A direct connection to one device."""

    def __init__(self, transport=None, product_id: int | None = None,
                 timeout_ms: int = 2000):
        if transport is None:
            def _libusb():
                return LibUsbTransport(product_id, timeout_ms)

            def _hidapi():
                return HidRawTransport(timeout_ms=timeout_ms)

            # Which transport leads depends on who owns the HID interface.
            # On Linux nothing need own it, and libusb wins for the reason in
            # LibUsbTransport's docstring. On Windows the operating system's
            # own HID driver always owns it, and libusb cannot claim it away
            # without replacing that driver -- which would break the vendor's
            # software too -- so hidapi leads and libusb becomes the fallback.
            first, second = ((_hidapi, _libusb) if sys.platform == "win32"
                             else (_libusb, _hidapi))
            # Catching ProtocolError alone was not enough: pyusb raises
            # NoBackendError, which is a ValueError, and USBError, which is an
            # OSError, while hidapi's open_path raises a bare OSError. So a
            # missing libusb backend skipped straight past the working hidraw
            # path, and an OSError from the second transport destroyed the
            # first one's message on the way out.
            try:
                transport = first()
            except (ProtocolError, OSError, ValueError) as primary_exc:
                # The second transport is for when the first is unavailable,
                # not an explanation for why it failed. If it cannot help
                # either, report the first reason -- "already in use" is
                # actionable, "no hidraw node" is not.
                try:
                    transport = second()
                except (ProtocolError, OSError, ValueError):
                    raise primary_exc from None
        self._t = transport
        self.timeout_ms = timeout_ms
        self._unacked = 0
        # Set when a command times out, cleared once the queue has been
        # drained again. See command().
        self._resync = False

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "MiniDSP":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- raw exchange -----------------------------------------------------

    def send(self, cmd: int, args: Iterable[int] = ()) -> None:
        self._t.write(frame(cmd, args))

    def recv_raw(self, timeout_ms: int | None = None) -> bytes:
        """One report, exactly as it arrived."""
        data = self._t.read(timeout_ms)
        if not data:
            raise ProtocolError("timed out waiting for a reply")
        return bytes(data)

    def recv(self, timeout_ms: int | None = None) -> bytes:
        """The body of a data reply: the command byte and its payload.

        A status reply -- an acknowledgement or an error, whose first byte is
        a status rather than a length -- comes back whole, since there is no
        body to take out of it. command() is what interprets those.
        """
        buf = self.recv_raw(timeout_ms)
        size = buf[0]
        if size == 0 or size > len(buf):
            return buf
        return buf[1:size]                              # drop size, keep body

    def drain(self, limit: int = 8) -> int:
        """Discard replies left over from earlier traffic.

        The device answers on an interrupt endpoint that keeps queueing, so an
        abandoned or late reply stays buffered and every subsequent read
        returns the *previous* answer. A reply carries the command it answers
        but not which address was asked for, so a stale one for a different
        address of the same command passes a naive check -- which shows up as
        values that are correct but belong to the request before. That is what
        the address echo in exchange() is matched against; draining is what
        stops the queue getting one behind in the first place.
        """
        dropped = 0
        for _ in range(limit):
            try:
                if not self._t.read(1):
                    break
            except Exception:                          # noqa: BLE001
                break
            dropped += 1
        return dropped

    def command(self, cmd: int, args: Iterable[int] = (),
                require_ack: bool = False) -> bytes:
        """Send a write and read its acknowledgement.

        A write is answered by a status reply: a byte saying accepted or
        refused, then the command it answers. Both halves are used -- the
        status for the verdict, the echo to be sure the verdict belongs to
        this command and is not a late one from the last.

        A single missing ack is tolerated: the device is occasionally slow and
        the write itself usually landed. A long run of them is not, because
        every write after that point would also be silently accepted and the
        caller would finish an Apply believing a configuration had been
        written that never left the machine.

        `require_ack` withdraws that tolerance where "usually landed" is
        not good enough. It is set on the bypass flag and the biquad write,
        neither of which can be read back to check; on the flash-block
        finish, which is what commits a stored preset; and on the two writes
        the driver guard stands down over -- a channel mute and a mixer
        gate, which are how it proves an output silent or unfed.

        A reply that says the command was *rejected* raises. The device
        distinguishes the two -- a no-op write answers 01, an undefined
        opcode answers 00 -- and that verdict used to be discarded, so a
        refused write and an accepted one were indistinguishable.
        """
        if self._resync:
            # A previous command timed out, so its acknowledgement may still
            # be in flight. Left there, the next write of the same opcode
            # consumes it -- the echo matches, because the echo is the
            # opcode -- and every ack after that answers the write before it.
            # A rejection then lands against the wrong command, and the last
            # write of the run is never acknowledged at all.
            self.drain(limit=8)
            self._resync = False
        self.send(cmd, args)
        # The echo is what says which command a reply answers, so a reply
        # carrying somebody else's is a late ack from an earlier one --
        # discarded and looked past, rather than taken as this command's or
        # drained along with the one actually wanted. Accepting a stale ack
        # used to reset the counter below and leave the queue permanently one
        # behind, after which every rejection arrived against the wrong write
        # and was swallowed for the rest of an Apply.
        reply = None
        for _ in range(STALE_REPLIES):
            try:
                got = self.recv_raw(timeout_ms=ACK_TIMEOUT_MS)
            except ProtocolError:
                break
            if len(got) >= 2 and got[1] == cmd:
                reply = got
                break
        if reply is None:
            # Either nothing arrived in time, or nothing that answered this
            # command did. Make sure a later one cannot be mistaken for the
            # answer to whatever is asked next: draining here catches one
            # already waiting, _resync catches one still in flight.
            self.drain(limit=2)
            self._resync = True
            self._unacked += 1
            if self._unacked >= MAX_UNACKED_WRITES:
                raise ProtocolError(
                    f"the device stopped acknowledging writes after "
                    f"{self._unacked} in a row; it may have been unplugged or "
                    f"stopped responding. Nothing further was written."
                ) from None
            if require_ack:
                raise ProtocolError(
                    f"command 0x{cmd:02x} was not acknowledged, and this one "
                    f"cannot be read back to find out whether it landed")
            return b""
        status = reply[0]
        self._unacked = 0
        # The reply is this command's -- the loop above accepted nothing
        # else -- so a refusal in it is a refusal of this command.
        if status in (ACK_ERROR, ACK_BAD_SIZE):
            raise ProtocolError(
                f"the device rejected command {cmd:#04x}"
                + (" (malformed payload size)" if status == ACK_BAD_SIZE
                   else ""))
        return reply

    def exchange(self, cmd: int, args: Iterable[int] = (),
                 retries: int = 2) -> bytes:
        """Send a command and return the reply body, minus the size byte.

        Retries cover a lost or stale reply. A reply that says the device
        refused the command is not retried: asking again gets the same answer,
        and reporting it as "unexpected" hid what had actually happened.
        """
        echo = READ_ECHO_BYTES.get(cmd)
        expect = bytes(args)[:echo] if echo else None
        last = None
        for attempt in range(retries + 1):
            if attempt:
                self.drain()
            self.send(cmd, args)
            try:
                reply = self.recv()
            except ProtocolError as exc:
                last = exc
                continue
            if (len(reply) > 1 and reply[0] in (ACK_ERROR, ACK_BAD_SIZE)
                    and reply[1] == cmd):
                # An explicit refusal. Retrying it would only ask again and
                # be told the same thing.
                raise ProtocolError(
                    f"the device rejected command {cmd:#04x}"
                    + (" (malformed payload size)"
                       if reply[0] == ACK_BAD_SIZE else ""))
            if reply and reply[0] == cmd:
                # Reads echo the address; matching it rejects a stale reply
                # that happens to share the command byte.
                got = reply[1:1 + len(expect)] if expect else b""
                if expect is None or got == expect:
                    return reply
                last = ProtocolError(
                    f"reply for {got.hex()} while asking for "
                    f"{expect.hex()} (stale packet)")
            else:
                last = ProtocolError(
                    f"unexpected reply "
                    f"{reply[:4].hex() if reply else '(empty)'} "
                    f"to command {cmd:#04x}")
            time.sleep(0.01)
        raise last or ProtocolError("no reply")

    # -- identity ---------------------------------------------------------

    def hardware_id(self) -> tuple[int, int, int]:
        """(fw_major, fw_minor, hw_id)."""
        r = self.exchange(CMD_PIC_VERSION)
        if len(r) < 4:
            raise ProtocolError(f"short hardware id reply: {r.hex()}")
        return r[1], r[2], r[3]

    def dsp_version(self) -> int:
        """The DSP id, which is what picks an address map.

        Read from EEPROM rather than with CMD_DSP_VERSION, because that opcode
        answers a different number: on a Flex 8 it returns 33 where the id at
        0xFFA1 is 110, and 110 is the value the device profiles are keyed on.
        Device Console reads the same address and calls it the DSP id, which
        is the name used here.
        """
        return self.read_memory(EE_DSP_ID, 1)[0]

    def serial(self) -> int:
        """Board serial, stored with 900000 subtracted.

        Two encodings exist. The Flex family uses the 16-bit one at 0xFFFE,
        big-endian; the 32-bit slot at 0xFFFC holds something else here and
        yields nonsense if read as the serial.
        """
        raw = self.read_memory(EE_SERIAL16, 2)
        return struct.unpack(">H", raw)[0] + 900000

    def device_info(self) -> DeviceInfo:
        fw_major, fw_minor, hw_id = self.hardware_id()
        return DeviceInfo(path=b"", vendor_id=VENDOR_ID,
                          product_id=getattr(self._t, "product_id", 0),
                          serial=self.serial(), fw_major=fw_major,
                          fw_minor=fw_minor, hw_id=hw_id,
                          dsp_version=self.dsp_version())

    def master_status(self) -> dict:
        """Preset, source, volume and mute, read from EEPROM in one go."""
        blk = self.read_memory(EE_PRESET, 4)
        return {"preset": blk[0], "source": blk[1],
                "volume": -(blk[2] / 2.0), "mute": bool(blk[3])}

    # -- memory -----------------------------------------------------------

    def read_memory(self, addr: int, size: int) -> bytes:
        """Byte-addressed read (EEPROM/settings space).

        A short answer is an error here rather than a shorter result: callers
        unpack fixed-width fields out of this, so returning what arrived would
        surface as a struct error or an index error somewhere else entirely.
        """
        r = self.exchange(CMD_READ_FLASH, addr_bytes(addr) + bytes([size]))
        body = r[3:3 + size]
        if len(body) < size:
            raise ProtocolError(
                f"short read at {addr:#06x}: wanted {size} bytes, got "
                f"{len(body)}")
        return body

    def read_flash(self, addr: int, size: int) -> bytes:
        """Read the 24-bit flash space, where stored presets live.

        Distinct from read_memory(), which reaches only the 16-bit settings
        window. 58 bytes per call, matching the vendor's own limit; a larger
        request is clamped to it rather than refused, so a caller asking for
        more gets fewer bytes than it asked for and has to loop. flash.py
        does. Note the difference from read_floats(), which raises instead.
        """
        size = max(1, min(int(size), 0x3A))
        args = bytes([(addr >> 16) & 0xFF, (addr >> 8) & 0xFF, addr & 0xFF,
                      size])
        r = self.exchange(CMD_READ_FLASH_FULL_ADDR, args)
        body = r[4:4 + size]
        if len(body) < size:
            raise ProtocolError(
                f"short flash read at {addr:#08x}: wanted {size} bytes, got "
                f"{len(body)}")
        return body

    # -- flash blocks -----------------------------------------------------
    #
    # How a preset is stored. The host never says where a block goes: it
    # names the block, and the firmware places it, which is why finding one
    # again means searching for it. A block is written as a header chunk,
    # then payload chunks, then a finish.

    def write_flash_block(self, block_id: int, data: bytes,
                          header: bool = False) -> int:
        """One chunk of a flash block. Returns how many bytes were sent.

        Refuses any block but the two that hold a preset. The firmware block
        shares this command and this argument position, and the difference
        between saving a tuning and starting a firmware write is one byte.
        """
        if block_id not in WRITABLE_FLASH_BLOCKS:
            raise ProtocolError(
                f"refusing to write flash block {block_id}: only the preset "
                f"blocks {WRITABLE_FLASH_BLOCKS} may be written from here")
        chunk = bytes(data)[:MAX_FLASH_BLOCK_CHUNK]
        tag = block_id if header else (block_id | FLASH_BLOCK_DATA)
        if not self.command(CMD_WRITE_FLASH_BLOCK, bytes([tag]) + chunk):
            # command() answers b"" for a write that was never acknowledged.
            # The caller advances its cursor by this return value, so counting
            # an unacknowledged chunk as written mis-assembles the rest of the
            # block. verify_preset would catch it, but only after the stored
            # preset had been overwritten, and there is no rollback.
            raise ProtocolError(
                "a flash chunk went unacknowledged; the block would be "
                "assembled wrong, so nothing further was written")
        return len(chunk)

    def finish_flash_block(self, block_id: int) -> None:
        """Close a block, which is what commits it."""
        if block_id not in WRITABLE_FLASH_BLOCKS:
            raise ProtocolError(
                f"refusing to finish flash block {block_id}")
        # Every chunk's verdict is checked; this is the one that commits
        # them, and it used to be the one nobody looked at. A dropped ack
        # here produced a clean "saved" from a block that was never closed.
        self.command(CMD_FINISH_FLASH_BLOCK, bytes([block_id]),
                     require_ack=True)

    def read_floats(self, addr: int, count: int) -> list[float]:
        """DSP parameter read, up to the device's per-reply limit.

        Reads into filter memory are aligned down to a biquad boundary,
        so an address that is not a block base returns the block that
        contains it rather than the floats asked for.
        """
        if not 1 <= count <= MAX_FLOATS_PER_READ:
            raise ValueError(
                f"count must be 1..{MAX_FLOATS_PER_READ}")
        r = self.exchange(CMD_READ_DSP_PARAM,
                          addr_bytes(addr) + bytes([count]))
        body = r[3:3 + count * 4]
        if len(body) < count * 4:
            raise ProtocolError(f"short float reply ({len(body)} bytes)")
        return list(struct.unpack("<" + "f" * count, body))

    def read_ints(self, addr: int, count: int) -> list[int]:
        """Read parameters that hold integers rather than floats.

        The device has one parameter-read command and always hands back four
        raw bytes per address; only the caller knows how they are meant to be
        read. Flags like channel mute and polarity are small integers, which
        as a float come out denormal (2 reads as 2.8e-45), so reinterpret the
        same bytes instead of converting them.
        """
        return [struct.unpack("<I", struct.pack("<f", f))[0]
                for f in self.read_floats(addr, count)]

    def write_float(self, addr: int, value: float,
                    mode: int = MODE_APPLY) -> None:
        value, = finite([value], f"the parameter at 0x{addr:04x}")
        self.command(CMD_LOAD_DSP_PARAM,
                     bytes([mode]) + addr_bytes(addr)
                     + struct.pack("<f", value))

    def fir_load_start(self, index: int) -> int:
        """Open a FIR load on one block, and learn how much it will take.

        **This is not a query.** The size in the reply makes it look like one
        -- it answers 2048 for either block on a Flex 8, whatever is loaded --
        and it was called GetNumFirTaps here on that reading, then sent from a
        read and from a file-size check that wanted nothing but the number.
        Both left a load open on the device that nothing ever closed, and a
        block whose load has been started is not a block that still holds what
        it held.

        So it is sent from one place now: the beginning of a real write, which
        finishes with fir_load_end(). Anything that only wants the capacity
        takes it from the constant the device layer keeps, which is the same
        number and costs nothing.
        """
        body = self.exchange(CMD_FIR_LOAD_START, bytes([index & 0xFF]))
        if len(body) < 3:
            raise ProtocolError(
                f"the FIR load on block {index} was not acknowledged with a "
                f"size ({len(body)} bytes came back), so nothing was sent to "
                f"it")
        return int.from_bytes(body[1:3], "big")

    def write_fir_taps(self, index: int, taps: Sequence[float]) -> int:
        """Send one packet of taps, and say how many went.

        There is no offset on the wire. The device keeps its own cursor for
        the block and answers with the number it accepted, so the caller
        advances by that and sends the rest -- which is how Device Console
        does it. Anything past the packet limit is dropped here rather than
        by the device, so the count returned is always one this side chose.
        """
        chunk = finite(list(taps)[:MAX_FIR_TAPS_PER_PACKET], "a FIR tap")
        if not chunk:
            return 0
        payload = bytes([index & 0xFF]) + b"".join(
            struct.pack("<f", float(v)) for v in chunk)
        reply = self.command(CMD_FIR_LOAD_DATA, payload)
        # The device answers with how many taps it took, and the caller
        # advances its cursor by the return value. Returning len(chunk)
        # regardless meant an unacknowledged packet -- command() returns b""
        # for one -- still counted as written, and every remaining tap landed
        # fourteen positions early with the block ending short. Nothing reads
        # FIR taps back afterwards, so a wrong filter would go into circuit
        # with no error anywhere.
        if not reply:
            raise ProtocolError(
                "a FIR tap packet went unacknowledged; the filter would be "
                "written shifted, so nothing further was sent")
        took = reply[2] if len(reply) > 2 else None
        if took is not None and took > len(chunk):
            raise ProtocolError(
                f"the device reported taking {took} taps from a packet of "
                f"{len(chunk)}; the filter would be written wrong, so "
                f"nothing further was sent")
        if took:
            return took
        # A count of zero is not "no count": HID reports are zero-padded, so
        # a device that sends no count byte and one that says it took none
        # look alike. Advancing the cursor by the whole packet on either
        # reading is the shifted-filter failure again, so neither is assumed.
        raise ProtocolError(
            "the device did not say how many FIR taps it accepted, so the "
            "rest of the filter was not sent")

    def fir_load_end(self) -> None:
        """Close the load opened by fir_load_start, committing the filter.

        Issued once after a whole filter is in. Coefficients written without
        it are in the device and not in the running program, and switching
        the block on in that state stopped this DSP answering parameter
        commands at all until a preset change reloaded it.

        Called reload_dsp_param here, which read as a general "pick up what
        was written" that any write might want. It is the closing half of a
        FIR load and belongs to nothing else, so it is sent from one place:
        write_fir, immediately after the last packet of taps.
        """
        self.command(CMD_FIR_LOAD_END, b"")

    def write_int(self, addr: int, value: int,
                  mode: int = MODE_APPLY,
                  require_ack: bool = False) -> None:
        """One integer parameter.

        `require_ack` is for the writes something else is relying on. The
        driver guard skips an output the payload mutes, and one it can prove
        every input cuts, on the grounds that no signal reaches it -- so a
        mute or a mixer gate whose acknowledgement was lost, and tolerated,
        would have that guard stand down over a driver that is still live.
        The callers that set it are those two writes; everything else this
        method sends keeps the ordinary tolerance.
        """
        self.command(CMD_LOAD_DSP_PARAM,
                     bytes([mode]) + addr_bytes(addr)
                     + struct.pack("<I", int(value) & 0xFFFFFFFF),
                     require_ack=require_ack)

    # -- filters ----------------------------------------------------------

    def write_biquad(self, addr: int, coeffs: list[float]) -> None:
        """Five coefficients, b0 b1 b2 a1 a2, in miniDSP convention.

        Sent with MODE_ALT rather than MODE_APPLY, which is the exception to
        what the module docstring says about write modes. The 0x80 finding
        there is about CMD_LOAD_DSP_PARAM, a different opcode: a mixer gate
        written that way is acknowledged and discarded. This opcode takes
        0x80 the way Device Console sends it, and the crossover
        coefficients it writes do read back, so a discarded write here would
        have shown up long ago.
        """
        if len(coeffs) != 5:
            raise ValueError("a biquad takes exactly 5 coefficients")
        coeffs = finite(coeffs, f"a biquad coefficient at 0x{addr:04x}")
        payload = (bytes([MODE_ALT]) + addr_bytes(addr) + struct.pack(">H", 0)
                   + b"".join(struct.pack("<f", c) for c in coeffs))
        # The ack is required here for the same reason it is on set_bypass:
        # a PEQ biquad answers five zeros however it is set, so a tolerated
        # lost ack would leave the band holding its previous coefficients
        # with nothing anywhere able to notice. Crossover coefficients do
        # read back, but this one command writes both.
        self.command(CMD_SET_DSP_FILTER_BIQUADS, payload, require_ack=True)

    def set_bypass(self, addr: int, bypassed: bool) -> None:
        """Bypass one filter: the flag rides in the mode byte, not the payload.

        The payload is the flag, the address, and a trailing zero -- byte for
        byte what Device Console sends for a single filter.

        The same opcode also takes a list of addresses, all switched the same
        way, which would collapse the hundred-odd bypass writes an Apply makes
        into two. That is not used here on purpose: bypass is what puts a
        filter into circuit on an active crossover, and this form is the one
        verified against the hardware.

        The saving was measured rather than guessed, so the trade is on the
        record. A bypass write takes 6.6 ms against 1.5 ms for a parameter or
        a biquad -- the slowest thing an Apply does -- and there are 116 of
        them on a Flex 8, which is 766 ms of a 1.1 s Apply. Batching would
        take back most of that and would swap a verified form for an
        unverified one to do it. Deliberately not taken.

        The ack is required here rather than tolerated. This flag has no
        parameter address at all, so nothing can read back whether it
        landed. A lost ack on `set_bypass(base, False)` --
        the write that puts a crossover *into* circuit -- would leave the
        group bypassed, the driver running full range, and nothing anywhere
        able to notice.
        """
        payload = (bytes([0x80 if bypassed else 0x00]) + addr_bytes(addr)
                   + struct.pack(">H", 0))
        self.command(CMD_BYPASS_DSP_FILTER, payload, require_ack=True)

    # -- master -----------------------------------------------------------

    def set_master_volume(self, db: float) -> None:
        """Master volume, in 0.5 dB steps as the device expects.

        The control only attenuates, and the wire value is the number of
        half-decibels below unity. A positive argument is clamped to 0 rather
        than run through abs(), which used to turn a request for +6 dB into
        6 dB of cut -- the opposite of what was asked for, silently.
        """
        # Through finite() like every other write: min(0.0, nan) is 0.0, so
        # a NaN request used to send zero attenuation -- full output.
        wanted, = finite([float(db)], "the master volume")
        cut = -min(0.0, wanted)
        steps = max(0, min(255, int(round(cut * 2))))
        self.command(CMD_MASTER_VOL, bytes([steps]))

    def set_master_mute(self, muted: bool) -> None:
        self.command(CMD_MASTER_MUTE, bytes([1 if muted else 0]))

    def set_source(self, index: int) -> None:
        self.command(CMD_CHANGE_AUDIO_SRC, bytes([index & 0xFF]))

    def set_preset(self, index: int, mode: int = PRESET_SWITCH) -> None:
        """Switch preset, reloading the DSP so the new one takes effect.

        The second byte is not a flag but one of three modes, which is what
        Device Console's own call sites show: it changes preset without a
        reset while writing a configuration into each slot in turn, asks for
        PRESET_RELOAD when re-selecting the slot already active, and asks for
        PRESET_SWITCH from the handler behind its preset control -- the case
        this is. Its bulk-import path picks between the last two on exactly
        that distinction.

        This was originally sent as 0, which changed the preset without the
        DSP reloading it.
        """
        self.command(CMD_CHANGE_PRESET, bytes([index & 0xFF, mode & 0xFF]))
