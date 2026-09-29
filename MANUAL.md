# LiniDi User Manual

For the miniDSP Flex 8, on Linux.

---

## Warning

**No warranty, express or implied. You use this entirely at your own risk.**
Sections 7 and 8 of the LICENSE are the operative text; this is what they
mean at your speakers.

**This software can destroy loudspeakers.** In an active system the DSP is
the only thing between an amplifier and a bare driver — nothing downstream
catches a mistake. A high pass bypassed, a crossover on the wrong channel,
or a gain on the wrong output can put full range or full power into a
tweeter and end it in an instant. Amplifiers and hearing are at risk too.

**It is confirmed working on one device only** — a Flex 8 (`hw_id 30`,
`dsp_version 110`), on one machine. Other models are supported in code and
untested against real hardware.

**The authors and contributors accept no liability** for any damage to
equipment, hearing or property arising from use or misuse of this software,
whether or not it behaves as documented.

Turn your amplifiers off before applying or saving. Read the device before
writing to it. Bring the volume up slowly. See [Safety](#9-safety) for the
specific failure this hardware punishes.

---

## Contents

1. [Introduction](#1-introduction)
2. [Installation](#2-installation)
3. [Basic operation](#3-basic-operation)
4. [Signal flow](#4-signal-flow)
5. [DSP reference](#5-dsp-reference)
6. [Presets](#6-presets)
7. [Applying and saving](#7-applying-and-saving)
8. [Files](#8-files)
9. [Safety](#9-safety)
10. [Troubleshooting](#10-troubleshooting)
11. [What this app doesn't do](#11-what-this-app-doesnt-do)

---

## 1. Introduction

LiniDi configures a miniDSP Flex 8 from Linux over USB. No daemon, no vendor
software, no Wine.

It does routing, crossovers, parametric EQ, delay, polarity, compression and
FIR. It reads the device's real state back, and verifies what it writes.

### Requirements

- A miniDSP Flex 8 on USB
- Linux, with permission to open the device (see [Installation](#2-installation))
- Python 3.11+ and PySide6 — or the single-file build, which needs neither

---

## 2. Installation

### Permissions

Out of the box only root may open the device. This is the one step needing
administrator rights, and it's needed once.

If you have the repository:

```
sudo cp 99-minidsp.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

If you only have the program, it's two lines:

```
sudo tee /etc/udev/rules.d/99-minidsp.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2752", MODE="0660", GROUP="audio"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2752", MODE="0660", GROUP="audio"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Unplug and replug the device. You also need to be in the `audio` group —
`id -nG` will tell you, and a group you were just added to doesn't apply
until you log out and back in.

Without this, opening the device fails — usually as a permissions error,
sometimes as *"could not claim the device"*, because a refused interface and
a claimed one look alike at that layer. If nothing else is holding the
device, suspect the rule.

### Starting it

```
python3 -m linidi
```

Or run the executable, if you have one.

### Options

None are needed for normal use. `--help` lists everything.

| | |
|---|---|
| `--project FILE` | Work on a different project file — a second system, or a known-good tuning beside an experiment. |
| `--map NAME` | Force an address map when the device isn't recognised. See [Troubleshooting](#10-troubleshooting). |
| `--console-dir DIR` | Where the Import XML dialog opens, if your Device Console exports aren't in the usual place. |
| `--device-dir DIR` | Where the dialogs for files that came off a device open — exports and FIR coefficient files. Defaults to `device/` beside the program. |
| `--device N` | Which device to use when more than one is attached, or which minidspd index to talk to. |
| `--daemon URL` | Where minidspd is listening, for the fallback path. Default `http://127.0.0.1:5380`. |
| `--tcp HOST:PORT` | minidspd's TCP bridge, used by the fallback. Default `127.0.0.1:5333`. |
| `--cli NAME` | The `minidsp` binary the fallback shells out to. Default `minidsp`. |
| `--timeout MS` | USB command timeout, default 2000. Raise it before concluding the device has stopped answering. |
| `--rate HZ` | Assumed sample rate when no address map is available. |
| `--peq N` | Assumed PEQ band count when no address map is available. |

---

## 3. Basic operation

### Starting up

**LiniDi reads the device as it opens.** The window is unusable while that
runs — there's one command endpoint on this hardware, so the app is either
running or talking to the device, never both — and the status bar carries a
progress bar. The first read of a session is the slow one, because it scans
flash for the preset block layout; that's cached afterwards.

This isn't optional and there's no way round it, on purpose. The device is the
source of truth. Until it's been read, everything on screen is the last
project, which looks exactly like live state and isn't — and writing it would
replace whatever's actually on the device, including a crossover you set up
somewhere else.

If there's nothing to read — no device, or minidspd without the CLI to read
coefficients through — the read is skipped, both lamps stay amber and the
strip says so.

### The window

- **Navigator**, left: every channel in signal order, inputs first. Each row
  shows what that channel does and carries its mute. **Double-click a
  channel's name to rename it** — `Tweeter R` reads better than `Out 3`, and
  it's the name the unfiltered-output question uses too. Return keeps it,
  Escape abandons it, and a blank leaves the old name alone. Names live in the
  project file; there's nowhere on the hardware to keep them, so they don't
  follow the device and Device Console won't see them. Renaming isn't an edit
  and needs no Apply.
- **Editor**, centre: the selected channel — level controls, filters, and the
  frequency-response plot.
- **Right column**: level meters, and either the compressor or the FIR panel
  depending on the channel.
- **Master strip**, above: volume, source, preset, MUTE ALL, and the two
  write buttons.
- **Action row**, below: reading, importing, and project files.

### The master strip

These act on the device **immediately**. They aren't staged, and Apply
doesn't affect them.

| Control | Effect |
|---|---|
| Volume | Master volume, after everything else |
| Source | Which physical input the device listens to |
| Preset | Which of the four stored configurations is running |
| MUTE ALL | Mutes every output at once |

Changing the preset re-reads the device.

### Meters

The number is a peak hold; the bar follows the signal.

**Green** is below −12 dBFS, **amber** above it, **red** approaching
clipping. Green means quiet, not "good".

---

## 4. Signal flow

```
  input 1 ──┬─ gain ─ mute ─ PEQ ×10 ─ FIR ──┬──▶ mixer ──┬──▶ output 1 ─ ... ─▶
            │                                │            │
  input 2 ──┴─ gain ─ mute ─ PEQ ×10 ─ FIR ──┘            ├──▶ output 2 ─ ... ─▶
                                                          │        ⋮
                                                          └──▶ output 8 ─ ... ─▶

  each output:  gain ─ mute ─ polarity ─ delay ─ PEQ ×10 ─ crossover ×2 ─ compressor
```

**Inputs.** Two. Each has gain, mute, ten parametric bands, and one FIR block.

**The mixer.** A 2 × 8 matrix. Every input can feed every output, each cell
with its own **on/off**, **gain** and **polarity**. The routing card edits one
input's row at a time.

A cell's polarity inverts only that path; an output's polarity inverts
everything feeding it.

**Outputs.** Eight. Each has gain, mute, polarity, delay, ten parametric
bands, two crossover groups, and a compressor.

---

## 5. DSP reference

### Gain

−127 to +12 dB, on inputs, outputs and mixer cells alike.

The device holds gains on a coarse grid and always rounds **down**, so a gain
lands near what you asked for rather than exactly on it — ask for −7.0 and you
may get −7.03. The app writes, reads back and corrects to land as close as the
grid allows.

A stored gain reads a little higher than the running one. That's also
deliberate — it's what makes the gain come back where you set it after a power
cycle.

The rounding is firmware behaviour rather than anything this app can fix. It
was reported to miniDSP in August 2026.

### Delay

Outputs only, 0 to 80 ms, in steps of one sample.

### Polarity

Inverts the channel. Per output, and per mixer cell.

### Parametric EQ

Ten bands per channel, inputs and outputs alike. Each band has a type,
frequency, Q and gain — or raw coefficients on the **Biquad** tab, which is
the same ten filters seen the other way.

Types: peaking, low shelf, high shelf, low pass, high pass, notch, all pass,
band pass.

Bands reset individually or as a bank. A reset band returns to a stock
frequency at 0 dB, Q 1.416, and **switched off**. Resetting never switches a
filter into circuit.

### Crossover

Two groups per output, each four biquads. A group is a mode (high pass or low
pass), an alignment, and an order:

| Alignment | Orders | Notes |
|---|---|---|
| Linkwitz-Riley | 2, 4, 6, 8 | Sums flat with its partner at the corner |
| Butterworth | 1–8 | Flat on its own |
| Bessel | 2–8 | Best phase behaviour, gentlest slope |

A typical two-way uses one group on the tweeter — a high pass — and both on
the woofer: a low pass, and a high pass to keep everything below the driver's
useful range out of it.

On the response plot each group has a lettered marker, **A** and **B**.
Drag it to change the corner frequency; scroll it to change the order. The
same letters appear on the crossover cards.

> A filter whose on/off state the device can't report shows a **partly
> filled** checkbox. Click it to make it definite.

### Compressor

One per output. Threshold, ratio, attack, release, makeup gain, and a
gain-reduction meter.

| Field | Range | Reads back? |
|---|---|---|
| Threshold | −90 to 0 dB | yes |
| Ratio | 1:1 to 100:1 | no |
| Attack | 0.1 to 1000 ms | no |
| Release | 1 to 5000 ms | no |
| Makeup | −20 to +20 dB | no |

The four that don't read back come from the stored preset, and the panel says
so.

**There's no knee control.**

> The gain-reduction meter reads NaN on a silent channel. That's its resting
> state, not a fault.

### FIR

Two blocks, one per **input**, ahead of the mixer. Good for room correction
and linear-phase EQ across the whole signal.

**It can't make a linear-phase crossover.** That needs a FIR on each output,
and this device has none there.

The budget is 4096 taps across the two inputs, each between 6 and 2048. Both
inputs can hold 2048 at once.

**Loading a filter.** *Load taps…* on an input's FIR panel reads a
coefficient file:

- **Text** — one number per line, or several separated by spaces or commas.
  A `#`, `;`, `*` or `//` starts a comment and the rest of that line is
  ignored, as are header lines of words. rePhase and REW exports work as they
  are.
- **Binary** — raw little-endian floats. A `.f32` or `.dbl` extension settles
  the width; a plain `.bin` can't be told apart, so the app asks.

The plot below is the **impulse response** — taps against tap number, not a
frequency response. It's there to catch what goes wrong with a coefficient
file: all zeros, clipped flat, or read at the wrong width.

Loading doesn't switch the filter on. Enabling asks first.

**Clearing one.** *Clear* puts the block back to passing signal through
unchanged. There's no way to hold *no* filter — the minimum is six taps — so
an empty block is a six-tap passthrough, which is also what an input that's
never been loaded reads back. That's why an untouched block shows **No filter
loaded** rather than describing six coefficients: it's the absence of a
filter, expressed the only way this hardware can express it.

Clear is staged like a load. Apply or Save sends it, and the coefficient file
it came from is untouched.

---

## 6. Presets

The device holds four and runs one. The strip always shows which.

**Save Edits writes the running preset only.** To write another slot, switch
to it first.

**Import preset** reads any of the four into your project without switching.
That's how to bring settings across from a preset you aren't running.

> Switching presets changes what the device is doing immediately, including
> what its outputs carry. A preset holding a configuration that doesn't match
> your drivers is a hazard whether or not you ever edit it.

---

## 7. Applying and saving

Most edits are staged. Two buttons write them:

| | Writes | Survives power off |
|---|---|---|
| **Apply Edits** | live parameters | no |
| **Save Edits** | live parameters **and** flash | yes |

The lamps beside them say where things stand. **Green** means the device
agrees with what's on screen; **amber** means work outstanding. They fill left
to right, because storing implies applying.

A save takes about ten seconds and is verified afterwards.

### What isn't staged

Three kinds of control act immediately, because they describe a **state**
rather than a design:

- the master strip — volume, source, preset, MUTE ALL
- a channel's mute, wherever you click it
- mute states carried in by loading a project or importing a preset

Loading a configuration that changes a mute asks first.

### While a write is happening

The window is held and a panel says what's running. Nothing else may talk to
the device while a read or write is in flight.

---

## 8. Files

| Action | Reads |
|---|---|
| Load / Save project | this app's own format, everything it models |
| Import XML | a Device Console export |
| Import REW | REW's biquad text, into one channel |
| Import preset / output / input | from the device, or another preset |
| Load taps | a FIR coefficient file |

**Import** always writes into where you're standing, and offers only
like-for-like sources.

---

## 9. Safety

The one mistake this hardware punishes is **an output carrying full range into
a driver that can't take it** — most often a tweeter that has lost its high
pass.

Apply asks first when it would leave an active output — fed and unmuted — with
no crossover filtering on it at all, and names the channels. That's the whole
of it. In particular it can't tell you that a tweeter has lost its high pass
while its low pass survives, because that output still has a filter on it, and
nothing here knows what's wired to which jack.

What tells you is the channel list, continuously: it shows what each output
actually passes — `≥2600`, `2600–4500`, `unfiltered`, `unused`. A channel
reading `≤2600` where you expect `≥2600` is a tweeter about to be a paperweight.
Reading that list is the habit that catches what one dialog can't.

Save doesn't ask. Apply is where a configuration starts driving what's
connected, so that's where the question belongs.

Two habits worth keeping:

- Have the amplifiers down while making structural changes — routing,
  crossovers, preset switches.
- A preset you've never configured isn't empty. It holds whatever the factory
  left, typically a full-range passthrough.

---

## 10. Troubleshooting

**"Could not claim the device."**
Usually true: another copy of LiniDi, or `minidspd`, has the USB interface,
and only one program may hold it. If nothing else is running, the udev rule is
the other cause — a refused interface and a claimed one look the same at that
layer. See [Installation](#2-installation).

**No sound, and the meters show input but no output.**
Check the routing card for the input feeding those outputs, then the output
mutes, then MUTE ALL.

**No sound, and the device's own screen says MUTE.**
Something other than this app muted it — the front panel or the remote. The
app reads a commanded mute correctly, but a mute the device applies for its
own reasons isn't always visible to it.

**A band shows as "hand-typed".**
Its coefficients aren't something the app can express as a type, frequency, Q
and gain. The filter is correct and running; edit it on the Biquad tab.

**A Bessel crossover reads back at a different frequency than it was set to.**
Bessel section placement was wrong in builds before September 2026, so a
Bessel crossover written by one of those reads back up to 29% off — high-pass
high, low-pass low, above 2nd order. It was corrected again later that month,
which shifts a Bessel of any order by a few per cent above a few kilohertz.
Set the frequency again and re-apply.

**A gain reads slightly different from what was set.**
Expected. See [Gain](#gain). A stored gain reading higher than the running one
is also correct.

**The crossover enable is partly filled.**
The app doesn't know whether that filter is in circuit, because nothing on the
device reports it. Click it to make it definite.

**Something changed the device outside the app.**
Read Device. It fetches the running parameters and the stored preset and
replaces what's on screen.

**The meters update about twice a second, and the strip beside the buttons says "via minidspd".**
minidspd is holding the device. Stop it — `systemctl --user disable --now
minidspd` — and restart LiniDi for direct USB.

**The device isn't recognised, or the channel layout looks wrong.**
Force a map with `--map NAME` — `flex8`, `m2x4hd` and so on, after the files
in `linidi/address_maps/`. A map for the wrong model reads and writes the
wrong addresses, so do this with the amplifiers off.

---

## 11. What this app doesn't do

- **Dirac.** Use Device Console for it.
- **The noise generator.** REW already generates noise.
- **Front-panel display settings.**
- **Firmware updates, flash erase, bootloader.** Never sent.
- **Writing a preset without switching to it.** Switch to a slot and it writes
  like any other.

---

*LiniDi isn't a miniDSP product and isn't endorsed by miniDSP. Apache-2.0.*
