import re, subprocess, sys

MINIDSP = "/usr/local/bin/minidsp"
TCP = "127.0.0.1:5333"

def read_floats(start, count):
    """Return list of `count` floats starting at float-index `start`.
    dump_floats omits zeros, so absent addresses are 0.0."""
    end = start + count
    out = subprocess.run(
        [MINIDSP, "--tcp", TCP, "debug", "dump-float", f"{start:X}", f"{end:X}"],
        capture_output=True, text=True, timeout=40)
    vals = {}
    for line in out.stdout.splitlines():
        m = re.match(r"^\s*([0-9a-fA-F]{1,4}):\s*(-?[\d.eE+-]+)\s*$", line)
        if m:
            vals[int(m.group(1), 16)] = float(m.group(2))
    return [vals.get(start + i, 0.0) for i in range(count)]

def classify(bq):
    b0, b1, b2, a1, a2 = bq
    if abs(b0 - 1) < 1e-9 and not any(abs(x) > 1e-12 for x in (b1, b2, a1, a2)):
        return "bypass"
    if abs(b1 - 2 * b0) < abs(b0) * 1e-3 and abs(b2 - b0) < abs(b0) * 1e-3:
        return "lowpass"
    if abs(b1 + 2 * b0) < abs(b0) * 1e-3 and abs(b2 - b0) < abs(b0) * 1e-3:
        return "highpass"
    return "other"

def dc_gain(bq):
    b0, b1, b2, a1, a2 = bq
    den = 1 - a1 - a2
    return (b0 + b1 + b2) / den if abs(den) > 1e-12 else float("nan")

BASE, STRIDE, BPF_OFF = 4276, 103, 61
for out_i in [int(x) for x in sys.argv[1:]] or [0]:
    d = BASE + STRIDE * out_i
    print(f"\n===== OUTPUT {out_i}  (gain@0x{d:04X}) =====")
    print(f"  gain = {read_floats(d,1)[0]:.4f} dB")
    for grp in (0, 1):
        g = d + BPF_OFF + grp * 20
        block = read_floats(g, 20)
        print(f"  -- crossover group {grp} @0x{g:04X}")
        for k in range(4):
            bq = block[k*5:(k+1)*5]
            kind = classify(bq)
            extra = "" if kind == "bypass" else f"  dcgain={dc_gain(bq):.4f}"
            print(f"     bq{k+1}: {kind:8s} "
                  f"[{', '.join(f'{v: .6f}' for v in bq)}]{extra}")

import math
def decode(bq, rate=96000):
    """Invert RBJ: recover (f0, Q) from a 2nd-order section in miniDSP convention."""
    b0, b1, b2, a1, a2 = bq
    if abs(1 - a2) < 1e-12: return None
    alpha = (1 + a2) / (1 - a2)
    c = a1 * (1 + alpha) / 2.0
    if not -1 < c < 1: return None
    w0 = math.acos(c)
    f0 = rate * w0 / (2 * math.pi)
    q = math.sin(w0) / (2 * alpha) if alpha else float("nan")
    return f0, q
