"""Entry point for ``python3 -m linidi``.

The same main() that run.py calls. Two ways in, one implementation: this one
for a source tree, run.py for PyInstaller, which cannot use a package's
__main__ as its entry -- it executes the entry as a top-level script, and the
relative imports below would have no parent package to resolve against.

License: Apache-2.0
"""

from .gui import main

raise SystemExit(main())
