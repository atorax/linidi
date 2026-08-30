#!/usr/bin/env bash
# Build a single-file executable.
#
# The app talks to the device directly over USB, so nothing external is
# bundled: no minidsp-rs binaries, no daemon. What goes in is the Python, Qt,
# the address maps, the icons and the licence texts.
#
# Qt arrives through PySide6, which is LGPL v3. Record the version printed
# below in the release notes -- that, plus offering to relink, is what the
# licence asks of a single-file build.
set -euo pipefail

cd "$(dirname "$0")"

# Prefer a local venv, since distributions increasingly refuse to let pip
# install into the system Python. Create one with:
#   python3 -m venv --system-site-packages .venv
#   .venv/bin/pip install pyinstaller
if [[ -x .venv/bin/pyinstaller ]]; then
    PYI=.venv/bin/pyinstaller
    PY=.venv/bin/python
elif command -v pyinstaller >/dev/null; then
    PYI=pyinstaller
    PY=python3
else
    echo "pyinstaller not found. See the comment above for how to install it." >&2
    exit 1
fi

# Names everything that is missing, rather than dying on the first one. See
# tools/check_deps.py for why a missing module is worse than an error here.
"$PY" tools/check_deps.py

# The package's own files keep their place inside it, because that is where
# the code looks for them; the project's documents sit beside it, which is
# where the code looks for those. PyInstaller preserves both when it unpacks.
#
# run.py rather than linidi/__main__.py: PyInstaller executes its entry as a
# top-level script, and a package's __main__ has relative imports that would
# then have no parent package to resolve against.
"$PYI" --onefile --name linidi --noconfirm \
    --add-data "linidi/address_maps:linidi/address_maps" \
    --add-data "linidi/icons:linidi/icons" \
    --add-data "NOTICE:." \
    --add-data "LICENSE:." \
    --add-data "README.md:." \
    --add-data "MANUAL.md:." \
    run.py

echo
# --apparent-size, because on a delayed-allocation filesystem the blocks of a
# file this fresh are not on disk yet and plain du reports next to nothing.
echo "Built dist/linidi  ($(du -h --apparent-size dist/linidi | cut -f1))"
