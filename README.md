# LiniDi

A desktop tuning application for miniDSP hardware on Linux.

miniDSP ships its Device Console for Windows and macOS only. The excellent
[minidsp-rs](https://github.com/mrene/minidsp-rs) fills the gap on Linux, but
it is a command-line tool — fine for scripting a volume change, miserable for a
measure-and-adjust loop where you want to nudge a crossover and hear the result.

This is the missing front end: crossovers, parametric EQ, raw biquads, gain,
delay, polarity, routing, live meters and REW import, in a native window. It
speaks the device's own USB protocol directly, so minidsp-rs is not required
to run it.

![status](https://img.shields.io/badge/status-alpha-orange)

---

## Status

Alpha. Reading from the device and designing filters are well tested; writing a
full configuration has had limited testing on limited hardware. **Read the
safety notes before you point this at an active crossover feeding real drivers.**

Developed against a miniDSP Flex 8 (`hw_id 30`, `dsp_version 110`). Other
devices should work — the app derives its channel layout from the device — but
they are untested.

---

## Requirements

Nothing but Python and a udev rule. The app opens the device itself over USB.

- Python 3.10+
- [PySide6](https://doc.qt.io/qtforpython/) (Qt bindings, LGPL v3)
- [pyusb](https://github.com/pyusb/pyusb) and libusb 1.0
- A miniDSP device on USB

```sh
# Arch / CachyOS
sudo pacman -S python-pyside6 python-pyusb libusb

# Debian / Ubuntu
sudo apt install python3-pyside6.qtwidgets python3-usb libusb-1.0-0
```

`requests` is needed only for the optional minidspd fallback described under
[Running](#running); the normal path does not use it.

Give your user access to the device, otherwise it can only be opened as root:

```sh
sudo tee /etc/udev/rules.d/99-minidsp.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2752", MODE="0660", GROUP="audio"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2752", MODE="0660", GROUP="audio"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Then log out and back in if you were not already in the `audio` group. The
control interface is separate from the USB Audio endpoint, so this does not
affect playback.

### A single file instead

`./build.sh` produces `dist/linidi`, one executable of about 120 MB that
carries its own Python, Qt and libusb. It needs the udev rule above and a
glibc no older than the machine it was built on; nothing else, and no
minidsp-rs. See [License](#license) for what bundling Qt asks of you.

---

## Running

```sh
python3 minidsp_gui.py          # or ./dist/linidi
```

That is all. The app finds the device, identifies it, loads the matching
address map and connects. The title bar of the device card shows which
transport it got: `[usb]` is the normal one.

Useful flags: `--project` to keep more than one working file, `--map` to force
an address map for a device that is not recognised yet, `--console-dir` to
point at a Device Console settings folder somewhere unusual.

### Fallback: minidspd

If the USB device cannot be opened -- almost always a missing udev rule -- the
app falls back to [minidsp-rs](https://github.com/mrene/minidsp-rs), using
`minidspd`'s REST API for writes and the `minidsp` CLI for readback. This
exists so an existing minidsp-rs setup keeps working; it is slower, it needs
the daemon running, and routing writes through it are subject to the mode-byte
bug noted below. Fix the udev rule instead if you can.

```sh
cat > ~/.config/minidsp.toml <<'EOF'
[http_server]
bind_address = "127.0.0.1:5380"

[[tcp_server]]
bind_address = "127.0.0.1:5333"
EOF

minidspd -c ~/.config/minidsp.toml &
python3 minidsp_gui.py --daemon http://127.0.0.1:5380 --tcp 127.0.0.1:5333
```

Note that Flex 8 support is not yet in upstream minidsp-rs; see
[PR #768](https://github.com/mrene/minidsp-rs/pull/768). The direct USB path
does not depend on it.

---

## Safety

**An active crossover has no passive network protecting your drivers.** A wrong
coefficient goes straight to a tweeter. Some deliberate design choices follow
from that:

- **Read before you write.** The app cannot know what is loaded in the hardware
  until you press **Read Device**. Until you do, it warns that Apply would
  overwrite your configuration, and asks for confirmation.
- **Nothing is written until you press Apply.** Editing filters only changes
  the local project.
- **MUTE ALL** is always visible and issues a master mute immediately.
- Unused crossover slots are written as explicit passthroughs, so stale
  coefficients from a previous tuning cannot linger.
- **Turn your amplifiers off the first time you apply a configuration.**

---

## How it works

```
   LiniDi  ──USB HID──>  device        everything: writes, readback, meters
```

The app speaks the device's own protocol directly, over libusb. Commands are
framed as `[size, cmd, args..., checksum]`, where the checksum is the plain
8-bit sum of the preceding bytes, and written to the HID interface padded to a
64-byte report.

What the hardware will and will not tell you shapes the whole design:

| | readable | writable |
|---|---|---|
| gain, delay, polarity, channel mute | yes | yes |
| crossover and PEQ coefficients | crossover only | yes |
| **whether a filter is bypassed** | **no** | yes |
| routing (mixer) | no | yes |
| master volume, source, preset | yes | yes |
| level meters | where the device has them | n/a |

Bypass has no readable address at all, so a filter sitting on the device fully
configured but switched off reads back looking exactly like a live one. That
is why importing a Device Console export matters, and why the app refuses to
guess -- see [On bypass](#on-bypass).

Routing is the same story for a different reason. The mixer cells *do* have
addresses, and writing to them works, but reading them back returns a constant
1 on every cell regardless of what is actually passing -- measured on a Flex 8
with audio flowing through four outputs and all sixteen cells reading "off".
They are writable and not readable, so routing also comes from the config
file.

Three of the generated maps have no meter addresses at all, so those devices
report no levels. The channel counts come from the address map rather than
from how many levels arrive, which is the honest source for the question
"how many outputs does this have".

### Address maps

Reading and writing both need the DSP memory address of every filter, and no
API exposes them. `tools/gen_address_map.py` parses minidsp-rs's generated
device definitions and emits JSON:

```sh
python3 tools/gen_address_map.py /path/to/minidsp-rs
```

This produces a map for every device minidsp-rs supports, so the tool is not
limited to the hardware it was written on. Pre-generated maps are in
`address_maps/`.

### Notes for anyone building on this

Things that cost real debugging time:

- **miniDSP's biquad sign convention is negated** relative to the RBJ cookbook:
  `y = b0x + b1x₁ + b2x₂ + a1y₁ + a2y₂`, with plus signs. Get it backwards and
  you produce an *unstable* filter, not merely a wrong-sounding one. REW's
  miniDSP export already uses this convention.
- **The parameter-write mode byte must be `0xa0`.** With `0x80` the device
  acknowledges the write and silently discards it for mixer cells, which
  presents as routing changes that never take effect. Verified on a Flex 8:
  with `0x80` a disable does nothing; with `0xa0` the channel drops from
  −19.1 dB to silence immediately.
- **Gain is snapped to a linear n/256 grid, and not to the nearest step.**
  Writing a gain back exactly as read moves it: −7.18 dB reads back −7.496,
  and repeating the write walks it down ~0.17 dB each time without settling.
  Any write of a gain has to be verified and corrected in a loop.
- **Delay is a sample count stored in the float's bit pattern**, not a float.
- **Channel mute and mixer cells use 1 = off, 2 = on**, not 0/1. A plain zero
  means something else at those addresses. A channel's own mute gate reads
  back correctly; a mixer cell's does not, and returns 1 whatever the state.
- **A routing cell's gate address is not `input * outputs + output`.** That
  arithmetic happens to be right on a Flex 8 and is wrong on most of the
  range: five of the thirteen device profiles put the gates elsewhere, three
  have none, and on a 2x4HD the addresses that formula produces are the
  channel mute gates. Take them from the device profile.
- **Bessel sections are not all at the corner frequency.** Each has its own
  ratio, and designing them all at the corner gives a cascade that is -4.8 dB
  at its own corner at 2nd order and -12.2 dB at 8th.
- **Gain writes are not idempotent.** Reading a gain and writing it straight
  back moves it further down, about 0.17 dB a time, without converging.
- **Bypass is a separate opcode** (`0x19`) stored apart from the coefficients,
  so a bypassed filter keeps its old coefficients. Reading coefficients alone
  cannot tell you what is actually in circuit.
- **Replies queue on the interrupt endpoint.** An abandoned or late reply stays
  buffered and the next read returns the *previous* answer. Since every reply
  starts with the command byte, a stale one passes a naive check — match the
  echoed address as well.
- Low-frequency sections are numerically ill-conditioned: a 100 Hz shelf and a
  peaking filter that sounds 5.5 dB different at 20 Hz agree to 1e-5 in every
  coefficient. Compare responses, never coefficients.

On the minidspd fallback path only:

- **The `minidsp` CLI parses address arguments as hex.** Symbol addresses are
  decimal. `dump-float 4287` reads `0x4287`.
- **`dump-float` prints only non-zero values.** Absent addresses are `0.0`;
  empty output means "all zeros", not "the read failed".
- The CLI cannot open USB while minidspd holds the device — route through
  `--tcp`.

---

## Roadmap

- [x] Read coefficients back off the hardware
- [x] Bypass state, via Device Console import (the hardware cannot report it)
- [x] Write a full configuration, verified as a byte-level round trip
- [x] Correct for the device's gain quantisation
- [x] Input routing matrix in the UI
- [x] Pure-Python USB transport, dropping the minidsp-rs dependency
- [x] Single-file executable
- [ ] Compressor — the Flex 8 exposes none, so this needs other hardware
- [ ] FIR — likewise

### On bypass

Filters read from the hardware show an **indeterminate** enable box, and Apply
omits the `bypass` field for them entirely, so the device keeps whatever it
already had. Clicking the box resolves it into a definite state that will be
written. This matters: a filter can sit on the device fully configured but
bypassed, and writing a guessed `bypass: false` would switch it on.

Bypass is set by command `0x19` and has no readable address — the device
profile has `_STATUS` symbols for `COMP`, `DGain`, `FIR` and `Mixer`, but none
for `PEQ` or `BPF`. Coefficients also survive being bypassed, so a dormant
filter reads back looking exactly like a live one. Importing a Device Console
export is the only way to recover that state; the app can *write* bypass
correctly either way.

---

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).

Uses PySide6, which is LGPL v3. Run from source it is an ordinary dynamically
linked library and nothing further is asked of you.

**The single-file build is different.** `build.sh` packs Qt into the
executable, which LGPL v3 treats as combining rather than linking, so
redistributing that binary carries section 4's obligations: state the PySide6
version you built against, and let a recipient relink against their own copy.
`build.sh` prints the version on every build for exactly this reason. See
NOTICE.

Nothing else is bundled — no minidsp-rs binaries — so no other project's
licence travels with the executable.

Address maps in `address_maps/` are generated from
[minidsp-rs](https://github.com/mrene/minidsp-rs) (Apache-2.0).
