#!/usr/bin/env python3
"""Start LiniDi.

Equivalent to ``python3 -m linidi``, and there for two reasons. It is the
entry point both build scripts hand to PyInstaller, which cannot take a
package's __main__.py: it runs the entry as a top-level script, so the
relative imports inside the package would have no parent to resolve against.
And it is the obvious thing to double-click or type in a fresh clone.

License: Apache-2.0
"""

from linidi.gui import main

raise SystemExit(main())
