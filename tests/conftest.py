"""Pytest config.

Adds the integration module directory directly to sys.path so tests
can import `mesh_crypto` and `connect_parser` without triggering the
package __init__.py (which imports Home Assistant internals).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT / "custom_components" / "haefele_mesh"

for p in (str(MODULE_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)
