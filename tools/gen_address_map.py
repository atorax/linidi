#!/usr/bin/env python3
"""
Generate JSON address maps from minidsp-rs device definitions.

minidsp-rs describes each supported device in `protocol/src/device/<name>.rs`,
a generated file containing a `sym` module of symbol->address constants plus a
`DEVICE` static laying out which symbol drives each input/output parameter.
Nothing in the REST API exposes those addresses, but readback needs them, so
we parse them out of the source.

Usage:
    python3 gen_address_map.py /path/to/minidsp-rs [device ...]

With no device names, every device file found is converted. Output lands in
`address_maps/<device>.json` next to this script's parent directory.

Why parse rather than hardcode: it works for every device minidsp-rs supports,
and it stays correct when upstream regenerates a profile.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

SYM_RE = re.compile(r"pub const (\w+): u16 = (\d+);")

# Each `Output { ... }` / `Input { ... }` block, non-greedy up to the closing
# brace that sits at the same indentation the block opened at.
BLOCK_RE = re.compile(
    r"\n(?P<indent>\s*)(?P<kind>Output|Input)\s*\{"
    r"(?P<body>.*?)\n(?P=indent)\}",
    re.DOTALL,
)

FIELD_PATTERNS = {
    "gain": re.compile(r"gain:\s*Some\((\w+)\)"),
    "enable": re.compile(r"enable:\s*(\w+)"),
    "meter": re.compile(r"meter:\s*Some\((\w+)\)"),
    "delay": re.compile(r"delay_addr:\s*Some\((\w+)\)"),
    "invert": re.compile(r"invert_addr:\s*(\w+)"),
}

PEQ_RE = re.compile(r"peq:\s*&\[(.*?)\]", re.DOTALL)
XOVER_RE = re.compile(r"xover:\s*Some\(Crossover\s*\{\s*peqs:\s*&\[(.*?)\]",
                      re.DOTALL)
ROUTING_RE = re.compile(r"routing:\s*&\[(.*?)\n\s*\],", re.DOTALL)
SYMBOL_LIST_RE = re.compile(r"\b([A-Z][A-Z0-9_]*)\b")


def parse_symbols(src: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in SYM_RE.finditer(src)}


def resolve(names: list[str], syms: dict[str, int]) -> list[int]:
    out = []
    for n in names:
        if n in syms:
            out.append(syms[n])
    return out


def parse_device(src: str, syms: dict[str, int]) -> dict:
    """Extract per-channel address layout from the DEVICE static."""
    inputs, outputs = [], []

    for m in BLOCK_RE.finditer(src):
        body = m.group("body")
        kind = m.group("kind")
        entry: dict = {}

        for field, pat in FIELD_PATTERNS.items():
            fm = pat.search(body)
            if fm and fm.group(1) in syms:
                entry[field] = syms[fm.group(1)]

        pm = PEQ_RE.search(body)
        if pm:
            names = SYMBOL_LIST_RE.findall(pm.group(1))
            entry["peq"] = resolve(names, syms)

        xm = XOVER_RE.search(body)
        if xm:
            names = SYMBOL_LIST_RE.findall(xm.group(1))
            entry["xover_groups"] = resolve(names, syms)

        if kind == "Input":
            rm = ROUTING_RE.search(body)
            if rm:
                names = SYMBOL_LIST_RE.findall(rm.group(1))
                # Routing alternates enable/gain symbols; keep gains only.
                gains = [n for n in names if not n.endswith("_STATUS")]
                entry["routing"] = resolve(gains, syms)
            inputs.append(entry)
        else:
            outputs.append(entry)

    return {"inputs": inputs, "outputs": outputs}


def find_rate(src: str) -> int:
    m = re.search(r"internal_sampling_rate:\s*Some\((\d+)\)", src)
    return int(m.group(1)) if m else 96000


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 1

    repo = Path(argv[1])
    devdir = repo / "protocol" / "src" / "device"
    if not devdir.is_dir():
        print(f"error: {devdir} not found -- is that a minidsp-rs checkout?")
        return 1

    wanted = set(argv[2:])
    outdir = Path(__file__).resolve().parent.parent / "address_maps"
    outdir.mkdir(exist_ok=True)

    skip = {"mod.rs", "probe.rs"}
    count = 0
    for path in sorted(devdir.glob("*.rs")):
        if path.name in skip:
            continue
        name = path.stem
        if wanted and name not in wanted:
            continue

        src = path.read_text()
        syms = parse_symbols(src)
        if not syms:
            print(f"  {name}: no symbols found, skipping")
            continue

        layout = parse_device(src, syms)
        n_in = len(layout["inputs"])
        n_out = len(layout["outputs"])
        if not n_out:
            print(f"  {name}: no outputs parsed, skipping")
            continue

        doc = {
            "device": name,
            "internal_sampling_rate": find_rate(src),
            "generated_from": f"minidsp-rs protocol/src/device/{path.name}",
            "note": (
                "Addresses are float indices. The minidsp CLI parses address "
                "arguments as HEX, so convert before shelling out to "
                "`minidsp debug dump-float`."
            ),
            **layout,
        }
        target = outdir / f"{name}.json"
        target.write_text(json.dumps(doc, indent=2))
        peq_n = len(layout["outputs"][0].get("peq", []))
        print(f"  {name}: {n_in} in / {n_out} out, {peq_n} PEQ "
              f"-> {target.name}")
        count += 1

    print(f"\n{count} device map(s) written to {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
