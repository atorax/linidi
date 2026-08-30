#!/usr/bin/env python3
"""
Add the processors minidsp-rs does not model to an address map, by reading
their addresses out of a Device Console export.

The generated maps come from minidsp-rs device profiles, which cover gain,
delay, polarity, mute, PEQ, crossover and mixer gain. Several devices have
more than that -- a Flex 8 has eight compressors, two FIR blocks and a
polarity bit on every mixer cell -- and none of it is in those profiles.

An export names every parameter and gives its address, so it is a complete
map of the device it came from. This takes the addresses and nothing else:
no gains, no coefficients, no serial. What it writes is a description of
where things live on that model, which is a fact about the hardware rather
than anything about the tuning it was exported from.

    python3 tools/extend_map_from_export.py flex8 exported-....xml

License: Apache-2.0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

MAPS = Path(__file__).resolve().parent.parent / "linidi" / "address_maps"

# Fields of one compressor, in the order Device Console names them.
COMP_FIELDS = ("status", "threshold", "gain", "ratio", "knee", "atime",
               "rtime")
# What this app calls them. "gain" is makeup gain and would collide with the
# channel's own gain, so it is renamed on the way in.
COMP_RENAME = {"status": "enable", "gain": "makeup", "atime": "attack",
               "rtime": "release"}


def addresses(path: Path) -> dict[str, int]:
    """Every named parameter in an export, and where it lives."""
    root = ET.parse(path).getroot()
    out: dict[str, int] = {}
    for el in root.iter():
        name, addr = el.get("name"), el.get("addr")
        if name and addr is not None:
            out[name] = int(addr)
    return out


def compressors(items: dict[str, int]) -> dict[int, dict[str, int]]:
    """Compressor blocks, keyed by the channel number Device Console uses."""
    found: dict[int, dict[str, int]] = {}
    for name, addr in items.items():
        m = re.match(r"COMP_(\d+)_\d+_(\w+)$", name)
        if m and m.group(2) in COMP_FIELDS:
            found.setdefault(int(m.group(1)), {})[m.group(2)] = addr
    return {k: v for k, v in found.items() if len(v) == len(COMP_FIELDS)}


def firs(items: dict[str, int]) -> dict[int, dict[str, int]]:
    found: dict[int, dict[str, int]] = {}
    for name, addr in items.items():
        m = re.match(r"FIR_(\d+)_\d+(?:_(\w+))?$", name)
        if m:
            found.setdefault(int(m.group(1)),
                             {})[(m.group(2) or "coeffs").lower()] = addr
    return found


def polarities(items: dict[str, int], n_in: int, n_out: int
               ) -> dict[int, list[int]]:
    """Mixer-cell polarity, one per cell, grouped by input."""
    out: dict[int, list[int]] = {}
    for i in range(n_in):
        row = []
        for o in range(n_out):
            addr = items.get(f"Mixer_{i}_{o}_pol")
            if addr is None:
                return {}
            row.append(addr)
        out[i] = row
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.
                                 RawDescriptionHelpFormatter)
    ap.add_argument("map", help="address map name, e.g. flex8")
    ap.add_argument("export", help="a Device Console .xml export")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = MAPS / f"{args.map}.json"
    amap = json.loads(path.read_text())
    items = addresses(Path(args.export))
    n_in, n_out = len(amap["inputs"]), len(amap["outputs"])
    added: list[str] = []

    # -- compressors: matched to outputs by address order, then checked ----
    comp = compressors(items)
    if comp:
        blocks = [comp[k] for k in sorted(comp)]
        if len(blocks) != n_out:
            print(f"  compressors: found {len(blocks)}, map has {n_out} "
                  f"outputs -- skipping rather than guessing the pairing")
        else:
            # Every block must sit at the same offsets from its own output's
            # gain. If they do not, the two are not describing the same
            # channels and pairing them by order would be an invention.
            ref = {f: blocks[0][f] - amap["outputs"][0]["gain"]
                   for f in COMP_FIELDS}
            ok = all({f: b[f] - amap["outputs"][i]["gain"] for f in
                      COMP_FIELDS} == ref for i, b in enumerate(blocks))
            if not ok:
                print("  compressors: offsets differ between outputs -- "
                      "refusing to add a layout that is not uniform")
            else:
                for i, b in enumerate(blocks):
                    amap["outputs"][i]["compressor"] = {
                        COMP_RENAME.get(f, f): b[f] for f in COMP_FIELDS}
                added.append(f"compressor on {n_out} outputs, "
                             f"offsets {ref}")

    # -- FIR ---------------------------------------------------------------
    fir = firs(items)
    if fir:
        blocks = [fir[k] for k in sorted(fir)]
        if len(blocks) == n_in and all("status" in b for b in blocks):
            for i, b in enumerate(blocks):
                entry = {"enable": b["status"]}
                if "taps" in b:
                    entry["taps"] = b["taps"]
                if "coeffs" in b:
                    entry["coeffs"] = b["coeffs"]
                amap["inputs"][i]["fir"] = entry
            added.append(f"FIR on {n_in} inputs")
        else:
            print(f"  FIR: found {len(blocks)} blocks for {n_in} inputs -- "
                  f"skipping")

    # -- mixer cell polarity -----------------------------------------------
    pol = polarities(items, n_in, n_out)
    if pol:
        for i in range(n_in):
            amap["inputs"][i]["routing_polarity"] = pol[i]
        added.append(f"mixer polarity, {n_in * n_out} cells")

    if not added:
        print("nothing to add")
        return 0
    note = amap.get("note", "")
    stamp = ("compressor, FIR and mixer polarity addresses were taken from a "
             "Device Console export, which names every parameter; the "
             "minidsp-rs profiles these maps are generated from do not "
             "describe them.")
    if stamp not in note:
        amap["note"] = (note + " " + stamp).strip()
    print(f"{args.map}:")
    for a in added:
        print(f"  + {a}")
    if args.dry_run:
        print("  (dry run, nothing written)")
        return 0
    path.write_text(json.dumps(amap, indent=2) + "\n")
    print(f"  written to {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
