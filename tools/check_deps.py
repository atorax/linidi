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
    ("PySide6", "PySide6", "pyside6", "python3-pyside6.qtwidgets",
     "Qt bindings -- the entire interface"),
    ("usb", "pyusb", "python-pyusb", "python3-usb",
     "USB transport -- the normal path on Linux"),
    ("hid", "hidapi", "python-hidapi", "python3-hid",
     "HID transport -- the fallback on Linux, the only one on Windows"),
    ("PyInstaller", "pyinstaller", None, "pyinstaller",
     "the tool that builds the executable"),
]

# Wanted, not required. The app imports these optionally, so a build without
# one works and simply lacks that path. Reported, never fatal -- listing
# requests here as required failed the build on any machine that had followed
# the README, which does not ask for it.
OPTIONAL = [
    ("requests", "requests", "python-requests", "python3-requests",
     "the minidspd fallback, for when USB cannot be opened"),
]


def _version(module, dist: str) -> str:
    """Whatever this package is willing to say about its version."""
    try:
        return md.version(dist)
    except md.PackageNotFoundError:
        # Installed by the distribution rather than pip, so there is no
        # metadata to read. The module itself may still know.
        return getattr(module, "__version__", "present")


def usb_backend() -> str | None:
    """Whether pyusb can actually find a libusb to talk through.

    pyusb is pure Python and imports without one, so checking the import says
    nothing. PyInstaller's own pyusb hook decides what to bundle by calling
    usb.core.find() at build time -- with no backend it bundles nothing, with
    no error, and the finished executable raises NoBackendError on somebody
    else's machine. That is the failure this whole file exists to prevent.
    """
    try:
        import usb.backend.libusb1 as libusb1
    except ImportError:
        return None
    try:
        return "present" if libusb1.get_backend() is not None else None
    except Exception:                                      # noqa: BLE001
        return None


def main() -> int:
    missing = []
    for mod, pip_name, arch, debian, why in OPTIONAL:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            print(f"{pip_name:12} not installed -- {why}")
        else:
            print(f"{pip_name:12} {_version(m, pip_name)}")

    for mod, pip_name, arch, debian, why in REQUIRED:
        try:
            m = importlib.import_module(mod)
        except ImportError:
            missing.append((mod, pip_name, arch, debian, why))
        else:
            note = ("  (LGPL v3 -- record this version)"
                    if mod == "PySide6" else "")
            print(f"{pip_name:12} {_version(m, pip_name)}{note}")

    backend = usb_backend()
    if backend:
        print(f"{'libusb':12} {backend} (pyusb has a backend to use)")
    else:
        print(f"{'libusb':12} NOT FOUND -- pyusb imports without it and "
              f"fails at run time")
        missing.append(("usb.backend", "libusb", "libusb", "libusb-1.0-0",
                        "the library pyusb talks through; without it the "
                        "build bundles no backend at all"))

    if not missing:
        return 0

    print()
    print(f"Cannot build: {len(missing)} of {len(REQUIRED) + 1} "
          f"requirements are missing.")
    print()
    for _, pip_name, _, _, why in missing:
        print(f"  {pip_name:12} {why}")
    print()
    print("Install them with one of:")
    print()
    by_pip = [m[1] for m in missing if m[1] not in ("libusb",)]
    if by_pip:
        print(f"  pip              pip install {' '.join(by_pip)}")
    # A None in the Arch column means the package is not in Arch's
    # repositories. Printing a pacman command that cannot work is worse than
    # saying so.
    packaged = [m[2] for m in missing if m[2]]
    if packaged:
        print(f"  Arch / CachyOS   sudo pacman -S {' '.join(packaged)}")
    unpackaged = [m[1] for m in missing if not m[2]]
    if unpackaged:
        print(f"  {'' if packaged else 'Arch / CachyOS   '}"
              f"{'' if not packaged else '                 '}"
              f"{', '.join(unpackaged)}: not in Arch's repositories, "
              f"pip install it")
    print(f"  Debian / Ubuntu  sudo apt install "
          f"{' '.join(m[3] for m in missing)}")
    print()
    # Only when it would actually help. libusb is not a pip package and
    # pyinstaller is commented out of that file, so offering it as the
    # one-line fix for either was offering something that cannot work.
    by_pip = [m[1] for m in missing
              if m[1] not in ("libusb", "pyinstaller")]
    if by_pip:
        print()
        print("or, for the runtime ones at once:  "
              "pip install -r requirements.txt")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
