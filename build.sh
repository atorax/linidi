#!/usr/bin/env bash
# Build a single-file executable.
#
# minidsp-gui shells out to the `minidsp` CLI and talks to `minidspd` over
# HTTP. Neither is a Python import, so PyInstaller cannot discover them --
# they are bundled explicitly here so the result really is one file.
#
# minidsp-rs is Apache-2.0; redistributing its binaries requires the
# attribution in NOTICE, which is bundled too.
set -euo pipefail

MINIDSP="${MINIDSP:-$(command -v minidsp || true)}"
MINIDSPD="${MINIDSPD:-$(command -v minidspd || true)}"

args=(--onefile --name minidsp-gui --add-data "address_maps:address_maps"
      --add-data "icons:icons"
      --add-data "NOTICE:." --add-data "LICENSE:.")

if [[ -n "$MINIDSP" && -n "$MINIDSPD" ]]; then
    echo "bundling $MINIDSP and $MINIDSPD"
    args+=(--add-binary "$MINIDSP:." --add-binary "$MINIDSPD:.")
else
    echo "WARNING: minidsp/minidspd not found; the build will require them"
    echo "         to be installed separately. Set MINIDSP= and MINIDSPD=."
fi

python3 -c 'import PySide6; print("PySide6", PySide6.__version__)'
pyinstaller "${args[@]}" minidsp_gui.py
echo
echo "Built dist/minidsp-gui"
echo "Record the PySide6 version above in your release notes (LGPL v3)."
