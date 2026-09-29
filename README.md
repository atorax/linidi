# LiniDi

A desktop tuning application for miniDSP hardware on Linux.

miniDSP ships its Device Console for Windows and macOS only. The excellent
[minidsp-rs](https://github.com/mrene/minidsp-rs) fills the gap on Linux, but
it's a command-line tool — fine for scripting a volume change, miserable for a
measure-and-adjust loop where you want to nudge a crossover and hear the result.

This is the missing front end: crossovers, parametric EQ, raw biquads, FIR,
gain, delay, polarity, routing, compressors, live meters and REW import, in a
native window. It speaks the device's own USB protocol directly — no daemon,
no minidsp-rs, nothing else installed.

![status](https://img.shields.io/badge/status-alpha-orange)

---

## Read this before you connect it to anything

**This software comes with no warranty of any kind, express or implied, and
you use it entirely at your own risk.** That is not a formality. Sections 7
and 8 of the [LICENSE](LICENSE) are the operative version of this paragraph;
what follows is what they mean in practice.

**It can destroy loudspeakers.** A DSP in an active system is the only thing
between an amplifier and a bare driver if there is no passive crossover
downstream to catch a mistake. A wrong coefficient, a crossover written to
the wrong channel, a high pass bypassed when it should not be, or a gain
applied to the wrong output can put full-range signal or full power into a
tweeter and finish it faster than you can reach the volume control. The same
mistakes can damage amplifiers and hearing.

**It is confirmed working on exactly one device.** My miniDSP Flex 8
(`hw_id 30`, `dsp_version 110`), on one machine, tested by me.
Address maps for other models ship with it, derived from published sources
and never tested against hardware; see Status below.

**The authors and contributors accept no liability** for damage to
equipment, hearing, property, or anything else arising from use or misuse of
this software — whether it behaves as documented or not.

If that isn't acceptable, don't use it. If it is:

- **Turn your amplifiers off** before you apply or save anything.
- **Read the device first**, so the app knows what is actually loaded.
- Bring the volume up slowly when you turn them back on.
- Do not assume a filter is doing what you intended until you have measured
  it.

---

**[MANUAL.md](MANUAL.md)** is the user manual — installation, signal path,
a reference for every processing block, and troubleshooting. This file is
about how the thing works inside and why it's built the way it is.

## Status

Alpha. Reading from the device and designing filters are well tested; writing a
full configuration has had limited testing on limited hardware.

Developed against a miniDSP Flex 8 (`hw_id 30`, `dsp_version 110`). That and
the Dirac variant (`dsp_version 111`, untested) are the only hardware it
recognises on its own; anything else is refused unless you name its map with
`--map`, and then you're the crash test dummy and should expect to find out
what's wrong with it yourself. `tools/check_maps.py` says which of the
shipped maps look incomplete.

---

## Requirements

Nothing but Python and a udev rule.

- Python 3.11+
- [PySide6](https://doc.qt.io/qtforpython/) (Qt bindings, LGPL v3)
- [pyusb](https://github.com/pyusb/pyusb) and libusb 1.0
- [hidapi](https://github.com/trezor/cython-hidapi) — the second transport
- A miniDSP device on USB

`requests` is optional: it's needed only for the minidspd fallback, and the
app runs and builds without it.

```sh
# Arch / CachyOS
sudo pacman -S pyside6 python-pyusb python-hidapi libusb

# Debian / Ubuntu
sudo apt install python3-pyside6.qtwidgets python3-usb python3-hid libusb-1.0-0
```

Give your user access to the device, otherwise it can only be opened as root:

```sh
sudo cp 99-minidsp.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then log out and back in if you weren't already in the `audio` group. The
control interface is separate from the USB Audio endpoint, so this doesn't
affect playback.

## Running

```sh
python3 -m linidi
```

That's all, for a Flex 8. The app identifies the device, loads the matching
address map and connects. Anything else has to be named with `--map`.

Command-line options are documented in the
[manual](MANUAL.md#options), or `--help`.

### A single file instead

`./build.sh` produces `dist/linidi`, one executable of about 120 MB carrying
its own Python and Qt. It needs the udev rule above and a glibc no older than
the machine it was built on. See [License](#license) for what bundling Qt asks
of you.

The build checks its dependencies first and names everything missing at once,
because PyInstaller bundles what it can import — a package absent from the
build machine isn't an error, it's quietly absent from the finished
executable.

`build.ps1` is the Windows equivalent; PyInstaller can't cross-compile, so a
Windows build has to run there. The platform differences are handled in the
source — no udev rule, hidapi leads instead of libusb, settings under
`%APPDATA%`. **Nobody has ever run it.** Treat it as a test rather than a
supported platform.

### What it sends

Nothing, and it makes no network requests of its own at any time. There is no
telemetry, no crash reporting and no startup check of any kind.

The single exception is **Check for updates**, in the help window, which asks
GitHub which version is the newest tagged one. It runs when you press it and
never otherwise, it sends nothing but the request, and it downloads nothing
whatever the answer is — a one-file build can't safely replace itself while
running, so it tells you and leaves the rest to you. The whole of that lives
in `linidi/update.py`, which is short enough to read before you trust it.

Everything else stays on the machine: the project file and the block cache in
`~/.config/linidi/`, and whatever you keep in `device/`.

---

## Applying and saving

**Apply changes what the device is doing now. It doesn't write the stored
preset.** Measured, not assumed: after applying a gain of -7.18 dB the running
parameter reads -7.18 and the stored one still reads -7.0, the value the
vendor's software last saved.

**Save Edits** writes the project into the stored preset, so it becomes
what the device loads at power-on. It writes whichever preset is active,
because that's the only one the device will write — the command names a
block, never an address, and the firmware puts it in the running preset.

Two things it does that Device Console doesn't. It reads the stored image
first and edits it rather than generating one, because the image covers every
parameter the DSP has and this app models only some of them — a generated
image would zero the rest. And it reads both blocks back afterwards and
compares them byte for byte.

There's no undo, so turn your amplifiers off first, and keep the project file.

## Reading the device

**Read** asks the hardware what it's set to and gets a complete answer,
including the parts that don't answer a parameter read at all: PEQ
coefficients, per-filter bypass, and mixer routing. Those come out of the
preset stored in flash.

Device Console can't do this. It never reads a preset back — it shows its own
settings file, so it can only display a tuning it made itself, on the machine
that made it. Nothing here needs that file.

The first read from a unit spends about twenty-five seconds finding where its
presets live in flash. That's remembered per device, so later reads are the
round trips plus about a fifth of a second of decoding.

Two limits. Gain, delay, polarity, mute and crossover coefficients are read
live; PEQ coefficients, bypass, mixer routing, compressor settings and FIR
taps come from the stored preset, so for those, anything applied but not saved
isn't in it. And coefficients come back exactly, but turning them back
into a frequency and a Q loses a little because the device stores float32 — a
band designed at 49 Hz reads back as 49.10 Hz. That loss happened when the
filter was stored, so no reader of this hardware can avoid it.

## Safety

**An active crossover has no passive network protecting your drivers.** A wrong
coefficient goes straight to a tweeter, so the design is defensive by default:
edits are staged rather than written as you type, Apply asks first and names
the channels when it would leave an active output with no crossover filtering
at all, unused crossover slots are written as explicit passthroughs so stale
coefficients can't linger, and Apply over a device that hasn't been read warns
before overwriting it.

What it deliberately doesn't do is guess what's on an output. It won't infer
that a channel called "Tweeter R" needs a high pass, and it won't warn you
about losing one while a low pass survives. Instead the channel list always
says what each output passes, so the configuration reads back at a glance.
Knowing what's safe to send a driver stays with whoever wired the system.

How to use those safely is in the [manual](MANUAL.md#9-safety).

---

## How it works

The app speaks the device's own protocol over libusb. Commands are framed as
`[size, cmd, args..., checksum]`, where the checksum is the plain 8-bit sum of
the preceding bytes, written to the HID interface padded to a 64-byte report.

What the hardware will and won't tell you shapes the whole design:

| | readable | writable |
|---|---|---|
| gain, delay, polarity, channel mute | yes | yes |
| crossover and PEQ coefficients | crossover only | yes |
| **whether a filter is bypassed** | **no** | yes |
| routing (mixer) | no | yes |
| mixer-cell polarity | yes | yes |
| compressor threshold | yes | yes |
| compressor makeup, ratio, attack, release | no | yes |
| compressor knee | no | accepted and ignored |
| FIR coefficients | **yes**, exactly | yes, via `0x3A` |
| FIR tap count | no | yes |
| whether a FIR is enabled | yes, once it has been | yes |
| master volume, source, preset | yes | yes |
| level meters | where the device has them | n/a |

**Bypass has no readable address**, so a filter sitting on the device fully
configured but switched off reads back looking exactly like a live one.
A filter whose state the app couldn't establish shows an *indeterminate*
enable box, and writes are refused until every one is resolved — clicking a
box makes it definite. Read Device recovers the real states from the stored
preset, which carries every bypass flag; importing a Device Console export is
the other way. The app can *write* bypass correctly regardless.

**Routing reads back a constant 1** on every cell regardless of what's
actually passing — measured with audio flowing through four outputs and all
sixteen cells reading "off". Writable and not readable, so routing also comes
from the stored preset.

### Address maps

Reading and writing both need the DSP memory address of every filter, and no
API exposes them. `tools/gen_address_map.py` parses minidsp-rs's generated
device definitions and emits JSON:

```sh
python3 tools/gen_address_map.py /path/to/minidsp-rs
```

That produces a map for every device minidsp-rs supports, so the tool isn't
limited to the hardware it was written on. Pre-generated maps are in
`linidi/address_maps/`.

minidsp-rs's profiles don't describe compressors, FIR blocks or mixer-cell
polarity; `tools/extend_map_from_export.py` adds those from a Device Console
export, and regenerating carries them across rather than dropping them.
`tools/check_maps.py` reports a map that gives one address two meanings, or
that looks like the generator missed a bank of filters — the Flex HTx map is
missing all of its output PEQ, and two maps have short mixer rows. Those need
the hardware to settle.

### What cost real debugging time

- **miniDSP's biquad sign convention is negated** relative to the RBJ cookbook:
  `y = b0x + b1x₁ + b2x₂ + a1y₁ + a2y₂`, with plus signs. Get it backwards and
  you produce an *unstable* filter, not merely a wrong-sounding one.
- **The parameter-write mode byte must be `0xa0`.** With `0x80` the device
  acknowledges the write and doesn't apply it to mixer cells: a disable does
  nothing, where `0xa0` drops the channel from −19.1 dB to silence at once.
  The separate biquad opcode is the exception and takes `0x80`.
- **Gain is truncated, not rounded to nearest**, at about five mantissa bits.
  The precision is a hardware limit; the truncation is a firmware defect.
  Every write lands low, so writing a gain back exactly as read moves it:
  −7.18 dB reads back −7.496, and repeating walks it down ~0.17 dB each time
  without settling. The device truncates again **when it loads a preset at
  power-on**, so what gets saved has to be the value that *lands* on the
  target, not the target itself. Every gain write is verified and corrected
  in a loop.
- **Delay is a sample count stored in the float's bit pattern**, not a float.
- **Channel mute and mixer cells use 1 = off, 2 = on**, not 0/1.
- **A routing cell's gate address isn't `input * outputs + output`.** That's
  right on a Flex 8 and wrong on most of the range — on a 2x4HD it produces
  the channel mute gates. Take them from the device profile.
- **Bypass is a separate opcode** (`0x19`) stored apart from the coefficients,
  so reading coefficients alone can't tell you what's in circuit.
- **A filter address is the base of a 5-float biquad block, and the device
  aligns reads down to one.** Asking for `base+14` returns the block at
  `base+10`, so reading a 20-float crossover group in 14-float chunks returns
  overlapping data. Read whole biquads.
- **PEQ band addresses descend.** Band 0 sits highest and the rest step down
  by 5.
- **Replies queue on the interrupt endpoint.** A late reply stays buffered and
  the next read returns the *previous* answer. Every reply starts with the
  command byte, so a stale one passes a naive check — match the echoed address
  too.
- **Bessel sections aren't all at the corner frequency**, and a Butterworth's
  section Qs depend on the parity of its order. Both produce filters that
  measure wrong at their own corner. Derive from the polynomial and check the
  result against the analog response across the band.
- **Compare responses, never coefficients.** A 100 Hz shelf and a peaking
  filter 5.5 dB apart at 20 Hz agree to 1e-5 in every coefficient.

### Deliberately not exposed

The app offers whatever the device supports. The exceptions:

- **Dirac.** Not driven. Use Device Console for it.
- **Master FIR bypass** (`0x3F`). A global bypass above the per-block FIR
  switches, untested here. The two per-block switches are on screen.
- **Noise generator** (`0x45`). Device Console never calls it, so the write
  protocol would have to be guessed. REW already generates noise.
- **OLED brightness and idle time** (`0x1A`, `0x1B`). Cosmetic, and
  brightness needs a contrast table from the vendor's support package.
- **DRE** (`0x1E`). A boolean whose effect on a Flex 8 is unknown.
- **CopyPreset** (`0x27`). Copies the active preset over *all* the others at
  once. Switch to a slot, import, and save does the same thing one slot at a
  time.
- **`ERASE_FLASH`, `ENTER_BOOTLOADER`, `COM_FW_UPGRADE`, `WRITE_DFLASH_ID`,
  `LOAD_DSP_PROGRAM`, `WRITE_FLASH_FULL_ADDR`.** Named in the command table
  for reference and never sent. `WRITE_DFLASH_ID` is one-time programmable.
  What the code does refuse is a flash write to any block but the two that
  hold a preset.

---

## Trademarks

miniDSP is a trademark of miniDSP Ltd. LiniDi is an independent project with
no affiliation to, sponsorship from, or endorsement by miniDSP Ltd. The name
appears throughout only to say which hardware this software talks to.

## AI disclosure

Code was written collaboratively with Claude Code. Everything produced was
reviewed by hand. All hardware claims were measured on an actual device,
rather than assumed.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Uses PySide6, which is LGPL v3. Run from source it's an ordinary dynamically
linked library and nothing further is asked of you.

**The single-file build is different.** `build.sh` packs Qt into the
executable, which LGPL v3 treats as combining rather than linking, so
redistributing that binary carries section 4's obligations: state the PySide6
version you built against, and let a recipient relink against their own copy.
`build.sh` prints the version on every build for exactly this reason.

Address maps in `linidi/address_maps/` are generated from
[minidsp-rs](https://github.com/mrene/minidsp-rs) (Apache-2.0).
