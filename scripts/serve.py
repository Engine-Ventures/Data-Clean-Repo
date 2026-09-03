#!/usr/bin/env python
"""Run the local workbench.

    python scripts/serve.py        # then open http://127.0.0.1:8765

A launcher rather than `python -m evpipeline.server`, because the package is
not installed — `src/` has to go on sys.path before the import, and with `-m`
the interpreter resolves the module name before any code in it runs. Same
pattern as scripts/build_db.py and scripts/build_ui.py.

Localhost only, no authentication. See src/evpipeline/server.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from evpipeline.server import main

if __name__ == "__main__":
    raise SystemExit(main())
