# minidsp-gui

A desktop tuning application for miniDSP hardware on Linux.

miniDSP ships its Device Console for Windows and macOS only. The excellent
[minidsp-rs](https://github.com/mrene/minidsp-rs) fills the gap on Linux, but
it is a command-line tool — fine for scripting a volume change, miserable for a
measure-and-adjust loop where you want to nudge a crossover and hear the result.

This is the missing front end: crossovers, parametric EQ, gain, delay, polarity,
live meters, and REW import, in a native window.

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

- Python 3.10+
- PySide6, requests
- [minidsp-rs](https://github.com/mrene/minidsp-rs) — both `minidspd` and the
  `minidsp` CLI
- A miniDSP device on USB

Note that Flex 8 support is not yet in upstream minidsp-rs; see
[PR #768](https://github.com/mrene/minidsp-rs/pull/768).

```sh
# Arch / CachyOS
sudo pacman -S python-pyside6 python-requests

# Debian / Ubuntu
sudo apt install python3-pyside6.qtwidgets python3-requests
```

Give your user access to the device, otherwise minidspd needs root:

```sh
sudo tee /etc/udev/rules.d/99-minidsp.rules >/dev/null <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="2752", MODE="0660", GROUP="audio"
SUBSYSTEM=="usb", ATTRS{idVendor}=="2752", MODE="0660", GROUP="audio"
EOF
sudo udevadm control --reload-rules && sudo udevadm trigger
```

---

## Running

Start the daemon first — it owns the USB connection, and this app talks to it:

```sh
cat > ~/.config/minidsp.toml <<'EOF'
[http_server]
bind_address = "127.0.0.1:5380"

[[tcp_server]]
bind_address = "127.0.0.1:5333"
EOF

minidspd -c ~/.config/minidsp.toml &
python3 minidsp_gui.py
```

Useful flags: `--daemon`, `--tcp`, `--cli`, `--device`, `--project`.

### Running the daemon as a service

So it is simply always there:

```sh
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/minidspd.service <<'EOF'
[Unit]
Description=miniDSP control daemon (minidsp-rs)
After=sound.target

[Service]
Type=simple
ExecStart=/usr/local/bin/minidspd -c %h/.config/minidsp.toml
Restart=on-failure
RestartSec=3
StartLimitIntervalSec=0

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now minidspd.service
```

A *user* service is enough because the udev rule above grants the `audio`
group access; it does not need root. `StartLimitIntervalSec=0` keeps the unit
from being disabled after repeated restarts when the device is unplugged. If
you want it running without logging in, `sudo loginctl enable-linger $USER`.

---

## Safety

**An active crossover has no passive network protecting your drivers.** A wrong
coefficient goes straight to a tweeter. Some deliberate design choices follow
from that:

- **Read before you write.** The app cannot know what is loaded in the hardware
  until you press *Read from device*. Until you do, it warns that Apply would
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
   minidsp-gui  ──REST──>  minidspd  ──USB──>  device      writes, status, meters
        │
        └───────CLI───────>  minidsp  ──TCP──> minidspd    readback
```

Writes and live status go through minidspd's HTTP API. Readback needs a
different path, because that API is **write-only for DSP configuration**:
`GET /devices/N` returns master status and meter levels, and there is no way to
ask it what coefficients are loaded. The underlying protocol *can* read
(`ReadFloats`, opcode `0x14`), and the `minidsp` CLI exposes it as
`debug dump-float`, so readback drives the CLI over the daemon's TCP port.

### Address maps

Readback needs the DSP memory address of every filter, which no API exposes.
`tools/gen_address_map.py` parses minidsp-rs's generated device definitions and
emits JSON:

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
- **The `minidsp` CLI parses address arguments as hex.** Symbol addresses are
  decimal. `dump-float 4287` reads `0x4287`.
- **`dump-float` prints only non-zero values.** Absent addresses are `0.0`;
  empty output means "all zeros", not "the read failed".
- **Delay is a sample count stored in the float's bit pattern**, not a float.
- **Bypass is a separate opcode** (`WriteBiquadBypass`) stored apart from the
  coefficients, so a bypassed filter keeps its old coefficients. Reading
  coefficients alone cannot tell you what is actually in circuit.
- The CLI cannot open USB while minidspd holds the device — route through
  `--tcp`.

---

## Roadmap

- [x] Read coefficients back off the hardware
- [x] Bypass state, via Device Console import (the hardware cannot report it)
- [x] Write a full configuration, verified as a byte-level round trip
- [x] Correct for the device's gain quantisation
- [ ] Input routing matrix and compressor in the UI
- [ ] Single-file executable bundling the minidsp-rs binaries
- [ ] Pure-Python USB HID transport, dropping the minidsp-rs dependency entirely

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

Uses PySide6 under LGPL v3 as a dynamically linked library. If you redistribute
a bundled binary, see the NOTICE file for what LGPL v3 section 4 asks of you.

Address maps in `address_maps/` are generated from
[minidsp-rs](https://github.com/mrene/minidsp-rs) (Apache-2.0).
