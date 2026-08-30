# LiniDi User Manual

For the miniDSP Flex 8 and Flex 8 Eight-N, on Linux.

This manual follows the shape of miniDSP's own product manuals, because
that is the vocabulary the hardware is described in everywhere else. Where
this app behaves differently from Device Console, it says so and says why.

Everything here was measured on a Flex 8. Where something is inferred
rather than measured, it is marked as such.

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
11. [What this app does not do](#11-what-this-app-does-not-do)

---

## 1. Introduction

LiniDi configures a miniDSP Flex 8 from Linux, over USB, speaking the
device's own protocol directly. No daemon, no vendor software, no Wine.

It does what Device Console does — routing, crossovers, parametric EQ,
delay, polarity, compression, FIR — and a few things it does not: it reads
the device's real state back rather than trusting a settings file, and it
verifies what it writes.

### Requirements

- A miniDSP Flex 8 or Flex 8 Eight-N connected over USB
- Linux, with permission to open the USB device (see [Installation](#2-installation))
- Python 3.11 or newer, and PySide6 — or the single-file build, which needs neither

---

## 2. Installation

### Permissions

The device is a USB HID. Out of the box only root may open it. Install the
udev rule:

```
sudo cp 99-minidsp.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Unplug and replug the device. Without this you will get *"the device is
already in use by another program"* or a permissions error.

### Running from source

```
pip install -r requirements.txt
python3 minidsp_gui.py
```

### A single file instead

`./build.sh` produces one executable with Python and Qt inside it. It needs
nothing installed to run.

---

## 3. Basic operation

### The window

Three regions, left to right:

- **The navigator** lists every channel in signal order — inputs first,
  then outputs. Each row shows what that channel does and carries its mute.
- **The editor** shows the selected channel: its level controls, its
  filters, and the frequency-response plot.
- **The right column** holds the level meters and, depending on the
  channel, the compressor or the FIR panel.

Above them is the **master strip** — volume, source, preset, MUTE ALL, and
the two write buttons. Below is the **action row** — reading, importing,
and project files.

### The master strip

These controls act on the device **immediately**. They are not staged and
Apply does not affect them.

| Control | Effect |
|---|---|
| Volume | Master volume, after everything else |
| Source | Which physical input the device listens to |
| Preset | Which of the four stored configurations is running |
| MUTE ALL | Mutes every output at once |

Changing the preset re-reads the device, because the screen must describe
the preset that is actually running.

### Meters

Input and output levels, updated many times a second. The numeric value is
a peak hold; the bar follows the signal.

A **green** bar is below −12 dBFS, **amber** above it, **red** approaching
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

### Input channels

Two. Each has gain, mute, ten parametric bands, and one FIR block.

### The mixer

A 2 × 8 matrix. Every input can feed every output, each cell with its own
**on/off**, **gain**, and **polarity**.

The routing card edits one input's row at a time, because that matches the
direction signal travels: one source fanning out to several drivers.

Per-cell polarity is separate from an output's own polarity. The output's
inverts whatever is feeding it; a cell's inverts only that path. That is
why the hardware keeps sixteen of them rather than eight.

### Output channels

Eight. Each has gain, mute, polarity, delay, ten parametric bands, two
crossover groups, and a compressor.

---

## 5. DSP reference

### Gain

−127 to 0 dB on outputs and inputs; mixer cells go to +12 dB.

The device stores gains coarsely — about five significant bits — and
**truncates rather than rounding**, so a written value always lands a
little low. This app closes the loop: it writes, reads back, and adjusts
until it lands on what you asked for.

Two consequences worth knowing:

- Not every value is reachable. Around −8 dB the steps are about 0.18 dB
  apart, so a target between two of them lands on the nearer one.
- What gets **saved** is not what you see. The device truncates again when
  it loads a preset at power-on, so the app stores the value that lands on
  your target rather than the target itself. A stored gain reading a little
  higher than the running one is correct.

### Delay

Outputs only, 0 to about 80 ms. Stored as a whole number of samples, so it
moves in steps of one sample — about 0.0104 ms at 96 kHz.

### Polarity

Inverts the channel. Available per output, and per mixer cell.

### Parametric EQ

Ten bands per channel, on inputs and outputs alike. Each band has a type,
frequency, Q and gain; or raw biquad coefficients on the **Biquad** tab —
the same ten filters, seen the other way.

Types: peaking, low shelf, high shelf, low pass, high pass, notch, all
pass, band pass.

Bands can be reset individually or as a bank. A reset band returns to a
stock frequency — spread across the range on ISO octave centres — at 0 dB,
Q 1.416, and **switched off**. Resetting never switches a filter into
circuit.

> **Coefficients do not read back.** Filter memory answers zero to a
> parameter read whatever is loaded, on this device. The app reads them out
> of the stored preset instead, which is why a read fetches both.

### Crossover

Two groups per output, each four biquads. A group is a mode (high pass or
low pass), an alignment, and an order:

| Alignment | Orders | Notes |
|---|---|---|
| Linkwitz-Riley | 2, 4, 6, 8 | Sums flat with its partner at the corner |
| Butterworth | 1–8 | Flat on its own |
| Bessel | 2–8 | Best phase behaviour, gentlest slope |

A typical two-way uses both groups on each output: a high pass on the
tweeter, a low pass and a protective high pass on the woofer.

On the response plot each group has a lettered marker — **ⓐ** and **ⓑ**.
Drag it to change the corner frequency; scroll it to change the order. The
same letters appear on the crossover cards.

> **Bypass state does not read back either.** No parameter address reports
> whether a filter is in circuit. It comes from the stored preset, and a
> band whose state is genuinely unknown shows a partly filled checkbox
> rather than guessing.

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

The five that do not read back come from the stored preset, and the panel
says so.

**There is no knee control.** The device has the parameter and ignores it:
four outputs given the same signal with knees of 0, 12, 24 and 40 returned
identical gain reduction to the last digit. Device Console offers no knee
control either.

> The gain-reduction meter reads NaN on a silent channel — that is its
> resting state, not a fault. It reads a number as soon as signal arrives.

### FIR

Two blocks, one per **input**, ahead of the crossover. Good for room
correction and linear-phase EQ across the whole signal.

**It cannot make a linear-phase crossover.** That needs a FIR per output,
and a Flex 8 does not have one — the device exposes exactly two FIR blocks,
both on inputs.

miniDSP's manual gives the budget as 4096 taps in total, distributed across
the two inputs, each between 6 and 2048. Since the two maxima add up to the
total, both can hold 2048 at once and there is nothing to trade.

**Loading a filter.** *Load taps…* on an input's FIR panel reads a
coefficient file:

- **Text** — one number per line, or several per line separated by spaces,
  commas or semicolons. Comments after `#`, `;`, `*` or `//` are ignored,
  as are header lines of words. rePhase and REW exports work as they are.
- **Binary** — raw little-endian floats. miniDSP's manuals specify IEEE 754
  single precision, and rePhase's miniDSP export is 32-bit. A `.f32` or
  `.dbl` extension settles the width; a plain `.bin` cannot be told apart,
  so the app asks.

The plot below is the **impulse response** — taps against tap number, not a
frequency response. It is there to catch what actually goes wrong with a
coefficient file: all zeros, clipped flat, or read at the wrong width.

Loading does not switch the filter on. Enabling asks first, because it
changes what comes out of the speakers.

> Unlike every other filter here, **FIR coefficients read back exactly**.
> This is the one filter the app can verify rather than trust.

---

## 6. Presets

The device holds four, and runs one. The strip always shows which.

**Save Edits writes the running preset only.** The flash write command
names a block rather than an address, and the firmware puts it in whatever
preset is active — so there is no way to write an inactive slot without
switching to it first.

**Import preset** reads any of the four into your project without
switching. That is the safe direction, and it is how to bring settings
across from a preset you are not running.

> Switching presets changes what the device is doing immediately, including
> what its outputs carry. A preset holding a configuration that does not
> match your drivers is a hazard whether or not you ever edit it.

---

## 7. Applying and saving

This is the part that differs most from Device Console, which writes as you
type. Here, most edits are staged.

| | Writes | Survives power off |
|---|---|---|
| **Apply Edits** | live parameters | no |
| **Save Edits** | live parameters **and** flash | yes |

The two lamps beside them say where things stand. **Green** means the
device agrees with what is on screen; **amber** means work outstanding.
They fill left to right, because storing implies applying.

### What is not staged

Three kinds of control act immediately, because they describe a **state**
rather than a design:

- the master strip — volume, source, preset, MUTE ALL
- a channel's mute, wherever you click it
- mute states carried in by loading a project or importing a preset

A mute is a claim about whether a driver is making sound. There is no such
thing as an intended-but-not-yet-real mute, so it is enforced rather than
staged, and loading a configuration that changes one asks first.

### While a write is happening

The window is held and a panel says what is running. Nothing else may talk
to the device while a read or a write is in flight — there is one command
endpoint, and anything else sent would interleave.

A save takes about ten seconds. It is three passes: read the slot's current
image, write the edited one, read it back and compare **byte for byte**.

---

## 8. Files

| Action | Reads |
|---|---|
| Load / Save project | this app's own format, everything it models |
| Import XML | a Device Console export |
| Import REW | REW's biquad text, into one channel |
| Import preset / output / input | from the device, or another preset |
| Load taps | a FIR coefficient file |

**Import** always writes into where you are standing and offers only
like-for-like sources. There is no clipboard: choosing the source and doing
the import are one action, so there is nothing to remember in between.

---

## 9. Safety

The one mistake this hardware punishes is **an output carrying full range
into a driver that cannot take it** — most often a tweeter that has lost
its high pass.

The app refuses to make that write quietly. Any write that would leave a
fed, unmuted output with no filter in circuit asks first, naming the
outputs. It asks rather than refuses, because the person reading it can see
their own speakers.

Two habits worth keeping:

- Have the amplifiers down while making structural changes — routing,
  crossovers, preset switches.
- A preset you have never configured is not empty. It holds whatever the
  factory left, which is typically a full-range passthrough.

---

## 10. Troubleshooting

**"The device is already in use by another program."**
Another copy of LiniDi, or `minidspd`, has the USB interface. Only one
program may hold it.

**Permission denied opening the device.**
The udev rule is not installed. See [Installation](#2-installation).

**No sound, and the meters show input but no output.**
Check the routing card for the input feeding those outputs, then the output
mutes, then MUTE ALL.

**No sound, and the device's own screen says MUTE.**
The device is muted by something other than this app — its front panel or
remote. The app reads and reports a commanded mute correctly, but a mute
the device applies for its own reasons is not always visible to it.

**A band shows as "hand-typed" or the type is blank.**
Its coefficients are not something the app can express as a type,
frequency, Q and gain. The filter is correct and running; the app declines
to describe it wrongly. Edit it on the Biquad tab.

**A gain reads slightly different from what was set.**
Expected. See [Gain](#gain). A stored gain reading higher than the running
one is also correct.

**The crossover enable is partly filled.**
The app does not know whether that filter is in circuit, because nothing on
the device reports it. Click it to make it definite.

**Something changed the device outside the app.**
Read Device. It fetches the running parameters and the stored preset and
replaces what is on screen.

---

## 11. What this app does not do

- **Dirac.** Not on this hardware, and nothing here can test it.
- **The noise generator.** The device has one; Device Console never uses
  it, so there is no protocol to read and building it would mean guessing.
- **Front-panel display settings.**
- **Firmware updates, flash erase, bootloader.** These commands are
  recognised so they can be refused.
- **Writing an inactive preset.** Import reads from one; only the running
  preset can be written.

See the README's *Deliberately not exposed* for the reasoning.

---

*LiniDi is not a miniDSP product and is not endorsed by miniDSP. Apache-2.0.
See NOTICE for what it builds on.*
