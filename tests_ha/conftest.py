"""Tests that import the real Home Assistant package (see the `test-ha` CI job).

They are kept apart from tests/, whose conftest replaces Home Assistant with
stubs. Run them on their own: `pip install homeassistant && pytest tests_ha`.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The real bluetooth component pulls in USB/D-Bus requirements this test
# environment does not install; the code under test only needs its import.
try:
    import homeassistant.components as _components
except ImportError:  # pragma: no cover - HA missing, tests skip themselves
    _components = None
if _components is not None and "homeassistant.components.bluetooth" not in sys.modules:
    _bt = types.ModuleType("homeassistant.components.bluetooth")
    _bt.async_ble_device_from_address = lambda *a, **kw: None
    _bt.async_scanner_count = lambda *a, **kw: 1
    sys.modules["homeassistant.components.bluetooth"] = _bt
    _components.bluetooth = _bt
