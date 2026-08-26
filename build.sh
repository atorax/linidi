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

"$PY" - <<'EOF'
import PySide6, usb, requests
print(f"PySide6 {PySide6.__version__}  (LGPL v3 -- record this version)")
print(f"pyusb   {usb.__version__}")
EOF

"$PYI" --onefile --name minidsp-gui --noconfirm \
    --add-data "address_maps:address_maps" \
    --add-data "icons:icons" \
    --add-data "NOTICE:." \
    --add-data "LICENSE:." \
    --add-data "README.md:." \
    minidsp_gui.py

echo
echo "Built dist/minidsp-gui  ($(du -h dist/minidsp-gui | cut -f1))"
