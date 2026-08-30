#!/usr/bin/env python3
"""Report everything the build is missing, in one pass.

PyInstaller bundles what it can import. A module missing from the build box is
not an error -- it is simply absent from the finished executable, which then
fails at run time on someone else's machine for a reason that has nothing to
do with their machine. So the build checks first.

It checks all of them before saying anything, because learning about one
missing package per attempt is a poor way to spend an afternoon. Both build.sh
and build.ps1 call this, so the list of what the program needs is written down
once rather than kept in step across two shell scripts.

`hid` is the one that motivated this. The code imports it optionally, so
nothing anywhere fails loudly when it is absent -- the program merely loses
its second transport, and on Windows that is the only transport there is.

License: Apache-2.0
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import sys

# What to import, what pip calls it, what the distributions call it, and why
# it is here. The "why" is printed on failure: a bare package name does not
# tell you whether you can skip it.
REQUIRED = [
    ("PySide6", "PySide6", "python-pyside6", "python3-pyside6.qtwidgets",
     "Qt bindings -- the entire interface"),
    ("usb", "pyusb", "python-pyusb", "python3-usb",
     "USB transport -- the normal path on Linux"),
    ("hid", "hidapi", "python-hidapi", "python3-hidapi",
     "HID transport -- the fallback on Linux, the only one on Windows"),
    ("requests", "requests", "python-requests", "python3-requests",
     "the optional minidspd fallback"),
    ("PyInstaller", "pyinstaller", "pyinstaller", "pyinstaller",
     "the tool that builds the executable"),
]


def _version(module, dist: str) -> str:
    """Whatever this package is willing to say about its version."""
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        # Installed by the distribution rather than pip, so there is no
        # metadata to read. The module itself may still know.
        return getattr(module, "__version__", "present")


def main() -> int:
    missing = []
    for mod, pip_name, arch, debian, why in REQUIRED:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            missing.append((mod, pip_name, arch, debian, why))
        else:
            note = ("  (LGPL v3 -- record this version)"
                    if mod == "PySide6" else "")
            print(f"{pip_name:12} {_version(m, pip_name)}{note}")

    if not missing:
        return 0

    print()
    print(f"Cannot build: {len(missing)} of {len(REQUIRED)} requirements are "
          f"missing.")
    print()
    for _, pip_name, _, _, why in missing:
        print(f"  {pip_name:12} {why}")
    print()
    print("Install them with one of:")
    print()
    print(f"  pip              pip install "
          f"{' '.join(m[1] for m in missing)}")
    print(f"  Arch / CachyOS   sudo pacman -S "
          f"{' '.join(m[2] for m in missing)}")
    print(f"  Debian / Ubuntu  sudo apt install "
          f"{' '.join(m[3] for m in missing)}")
    print()
    print("or, for all of them at once:  pip install -r requirements.txt")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
