#!/usr/bin/env python3
"""Check every shipped address map, without opening a device.

Two questions, both of which have been wrong in a shipped map.

Does any DSP address carry two meanings? A generated map once put an input's
channel gain on the same address as its first mixer cell's, because the
generator's search for the channel's own fields ran over the routing array
too. Writing one moved the other and reading one returned the other. A map
like that is refused outright by AddressMap, so this reports it as REFUSED.

And does a channel have a hole where its filters should be? Most maps put an
output's PEQ bank immediately below its first crossover group, so a run of
whole biquads sitting there, on a channel that claims no PEQ at all and with
nothing else in the map using those addresses, means the generator missed
them rather than that the device lacks them. It cannot be settled from here:
filling the addresses in would be a guess about hardware nobody has tested,
and a wrong guess writes filter coefficients over whatever is really there.

License: Apache-2.0
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from linidi import core                                   # noqa: E402

WORDS_PER_BIQUAD = len(core.COEFF_KEYS)


def claimed(amap) -> set[int]:
    """Every address the whole map accounts for.

    The whole map, not one channel: a run that looks unclaimed from inside
    one output is often another channel's mixer row or the previous output's
    crossover, and checking per channel reported eight of those as holes on
    the one map this tool exists to describe.
    """
    out: set[int] = set()
    for spec in list(amap.inputs) + list(amap.outputs):
        for key in ("gain", "delay", "invert", "enable", "meter"):
            if isinstance(spec.get(key), int):
                out.add(spec[key])
        for base in spec.get("peq", []):
            out.update(range(base, base + WORDS_PER_BIQUAD))
        for base in spec.get("xover_groups", []):
            out.update(range(base,
                             base + core.XOVER_SLOTS * WORDS_PER_BIQUAD))
        for key in ("routing", "routing_status", "routing_polarity"):
            out.update(a for a in spec.get(key, []) if isinstance(a, int))
        for value in (spec.get("compressor") or {}).values():
            if isinstance(value, int):
                out.add(value)
        for key, value in (spec.get("fir") or {}).items():
            if isinstance(value, int) and key != "coeffs":
                out.add(value)
    return out


def bank_sizes() -> set[int]:
    """How many bands a PEQ bank holds, across every shipped map.

    Used as the yardstick below rather than a number written here: a hole is
    only worth reporting if it is the size of a bank some miniDSP actually
    has.
    """
    sizes = set()
    for name in core.AddressMap.available():
        try:
            amap = core.AddressMap.load(name)
        except ValueError:
            continue
        for spec in list(amap.inputs) + list(amap.outputs):
            if spec.get("peq"):
                sizes.add(len(spec["peq"]))
    return sizes


def missing_peq(spec: dict, taken: set[int],
                sizes: set[int]) -> list[tuple[int, int]]:
    """A bank-shaped hole where a channel's PEQ should be.

    Narrow on purpose. The run is followed down until it meets an address
    something else in the map owns, and reported only if it is exactly as
    many biquads as a bank on some real device -- otherwise every stretch of
    unallocated DSP memory under a crossover base looks like a finding.
    """
    if spec.get("peq"):
        return []
    out = []
    for base in spec.get("xover_groups", []):
        addr, n = base - 1, 0
        while addr >= 0 and addr not in taken:
            addr -= 1
            n += 1
        if n % WORDS_PER_BIQUAD == 0 and n // WORDS_PER_BIQUAD in sizes:
            out.append((addr + 1, n))
    return out


def main() -> int:
    bad = 0
    # The same check AddressMap runs when it loads one, over the whole set.
    conflicts = core.AddressMap.check_all()
    for name in core.AddressMap.available():
        try:
            amap = core.AddressMap.load(name)
        except ValueError as exc:
            print(f"{name:14} REFUSED  {exc}")
            bad += 1
            continue
        for addr, claims in conflicts.get(name, []):
            print(f"{name:14} ! {addr} is both {' and '.join(claims)}")
            bad += 1
        suspect, notes = [], []
        taken = claimed(amap)
        sizes = bank_sizes()
        for kind, specs in (("in", amap.inputs), ("out", amap.outputs)):
            for i, spec in enumerate(specs):
                for start, n in missing_peq(spec, taken, sizes):
                    suspect.append(
                        f"{kind} {i + 1}: no PEQ addresses, and "
                        f"{n // WORDS_PER_BIQUAD} biquads of unclaimed space "
                        f"at {start}, below its crossover -- which is a "
                        f"bank's worth, where most maps keep one")
        n_out = len(amap.outputs)
        for i, spec in enumerate(amap.inputs):
            cells = amap.routes(i)
            if cells and cells < n_out:
                suspect.append(f"in {i + 1}: {cells} mixer cells for "
                               f"{n_out} outputs")
            if not amap.route_gates(i):
                notes.append(f"in {i + 1}: no mixer on/off addresses")
            if "gain" not in spec:
                notes.append(f"in {i + 1}: no channel gain of its own")
        if suspect:
            bad += 1
        state = "CHECK" if suspect else ("note " if notes else "ok")
        print(f"{name:14} {state}")
        for line in suspect[:6]:
            print(f"               ! {line}")
        if len(suspect) > 6:
            print(f"               ! ... and {len(suspect) - 6} more")
        for line in notes[:4]:
            print(f"               - {line}")
        if len(notes) > 4:
            print(f"               - ... and {len(notes) - 4} more")
    print()
    print(f"{bad} of {len(core.AddressMap.available())} maps look like the "
          f"generator missed something.")
    print("Lines marked ! need a device to settle. Lines marked - are the "
          "map saying this device has no such control.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
