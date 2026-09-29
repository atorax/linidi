# Build a single-file executable on Windows.
#
# The companion to build.sh, which does the same job on Linux. PyInstaller
# cannot cross-compile, so a Windows executable has to be built on Windows;
# there is no way to produce one from the Linux side.
#
# Three things differ from build.sh, and all three are forced by the platform:
#
#   --add-data uses ';' as its separator here, where Linux uses ':'. Getting
#   this wrong does not fail the build -- it produces an executable whose
#   address maps and icons are silently missing.
#
#   --noconsole keeps a console window from opening behind the GUI.
#
#   --icon takes a .ico. The one in icons/ is generated from the same artwork
#   the application loads at runtime.
#
# Qt arrives through PySide6, which is LGPL v3. Record the version printed
# below in the release notes -- that, plus offering to relink, is what the
# licence asks of a single-file build.
#
# License: Apache-2.0

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Prefer a local venv, matching build.sh. Create one with:
#   py -m venv .venv
#   .venv\Scripts\pip install -r requirements.txt pyinstaller
if (Test-Path ".venv\Scripts\python.exe") {
    $PY = ".venv\Scripts\python.exe"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PY = "py"
} else {
    $PY = "python"
}

# Names everything that is missing, rather than dying on the first one. See
# tools/check_deps.py for why a missing module is worse than an error here --
# on Windows a build without hid produces an application that starts happily
# and then cannot reach the device at all.
& $PY tools\check_deps.py
if ($LASTEXITCODE -ne 0) { exit 1 }

# The package's own files keep their place inside it, because that is where
# the code looks for them; the project's documents sit beside it. run.py is
# the entry rather than linidi\__main__.py, which PyInstaller cannot use: it
# runs the entry as a top-level script, leaving a package's relative imports
# with no parent to resolve against.
& $PY -m PyInstaller --onefile --name linidi --noconfirm --noconsole `
    --icon "linidi/icons/LiniDi.ico" `
    --add-data "linidi/address_maps;linidi/address_maps" `
    --add-data "linidi/icons;linidi/icons" `
    --add-data "NOTICE;." `
    --add-data "LICENSE;." `
    --add-data "README.md;." `
    --add-data "MANUAL.md;." `
    run.py
if ($LASTEXITCODE -ne 0) { throw "build failed" }

$exe = "dist\linidi.exe"
$mb = [math]::Round((Get-Item $exe).Length / 1MB)
Write-Host ""
Write-Host "Built $exe  (${mb}M)"
