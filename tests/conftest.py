"""Pytest config.

Adds the integration module directory directly to sys.path so tests
can import `mesh_crypto` and `connect_parser` without triggering the
package __init__.py (which imports Home Assistant internals).

Tests that need to import `gatt` (MeshSession, MeshProxyConnection)
also go through this file — we stub `bleak`, `bleak_retry_connector`
and the bits of `homeassistant` that `gatt.py` pulls at module scope
so the import succeeds on a bare test environment (no HA, no bleak).
The stubs are minimal and only exist to let the module load; tests
that exercise BLE behaviour drive the proxy object directly via its
public API and monkeypatch the specific methods they care about.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_DIR = ROOT / "custom_components" / "haefele_mesh"

for p in (str(MODULE_DIR), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Register `custom_components.haefele_mesh` as a bare namespace package
# (no __init__.py execution) so modules inside it can use relative
# imports (`from .const import ...`) without pulling in the real
# package __init__.py, which imports Home Assistant runtime.
if "custom_components" not in sys.modules:
    _cc = types.ModuleType("custom_components")
    _cc.__path__ = [str(ROOT / "custom_components")]
    sys.modules["custom_components"] = _cc
if "custom_components.haefele_mesh" not in sys.modules:
    _pkg = types.ModuleType("custom_components.haefele_mesh")
    _pkg.__path__ = [str(MODULE_DIR)]
    sys.modules["custom_components.haefele_mesh"] = _pkg


def _install_stub(name: str, **attrs: object) -> types.ModuleType:
    """Register a minimal stub module in sys.modules if not already present."""
    if name in sys.modules:
        return sys.modules[name]
    mod = types.ModuleType(name)
    for attr, value in attrs.items():
        setattr(mod, attr, value)
    sys.modules[name] = mod
    return mod


# --- bleak stubs -----------------------------------------------------------
_install_stub("bleak")
_install_stub("bleak.backends")
_install_stub("bleak.backends.device", BLEDevice=type("BLEDevice", (), {}))


class _StubBleakClient:
    """Minimal shape of BleakClientWithServiceCache used by gatt.py."""

    def __init__(self, *a, **kw):
        self.is_connected = False
        self.services = None

    async def start_notify(self, *a, **kw):  # pragma: no cover - stub
        pass

    async def disconnect(self):  # pragma: no cover - stub
        self.is_connected = False


async def _stub_establish_connection(*args, **kwargs):  # pragma: no cover
    raise RuntimeError(
        "establish_connection() called in a unit test — tests must "
        "monkeypatch MeshProxyConnection._try_connect instead.",
    )


_install_stub(
    "bleak_retry_connector",
    BleakClientWithServiceCache=_StubBleakClient,
    establish_connection=_stub_establish_connection,
)


# --- homeassistant stubs ---------------------------------------------------
_install_stub("homeassistant")
_install_stub("homeassistant.core", HomeAssistant=type("HomeAssistant", (), {}))
_install_stub("homeassistant.components")


class _StubBluetoothModule:
    @staticmethod
    def async_ble_device_from_address(hass, mac, connectable=True):  # pragma: no cover
        return None


_bt_stub = types.ModuleType("homeassistant.components.bluetooth")
_bt_stub.async_ble_device_from_address = (
    _StubBluetoothModule.async_ble_device_from_address
)
sys.modules.setdefault("homeassistant.components.bluetooth", _bt_stub)
# And attach to the parent package so `from homeassistant.components import
# bluetooth` works.
sys.modules["homeassistant.components"].bluetooth = _bt_stub
