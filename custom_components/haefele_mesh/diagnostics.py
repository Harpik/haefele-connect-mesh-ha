"""Diagnostics support for Häfele Connect Mesh.

Downloadable via Settings → Devices & Services → Häfele Connect Mesh
→ ⋮ → Download diagnostics.

Goal: give enough detail to debug real-world issues (proxy not
reachable, node not seen by the adapter, wrong service layout on
firmware update, …) without ever exposing cryptographic material.

What we dump:
  * Integration + config-entry metadata
  * Coordinator state: active proxy, candidate order, per-node
    availability, current IV Index, per-SRC SEQ snapshot
  * For every provisioned node: the latest BLE advertising data seen
    by HA's bluetooth stack (RSSI, advertised service UUIDs, a derived
    `advertises_mesh_proxy` flag, and seconds-since-last-seen)
  * For whichever node is currently the active mesh proxy: the full
    GATT service/characteristic tree, read from the already-connected
    bleak client's cached collection (zero extra BLE traffic)

What we **do not** dump: NetKey, AppKey, any DevKey, the raw `.connect`
JSON, or anything else that would let someone take over the mesh.
"""

from __future__ import annotations

import time
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import HaefeleCoordinator

# Anything that even smells like a key gets replaced with `**REDACTED**`
# before we return the payload to HA.
TO_REDACT = {
    "network_key",
    "app_key",
    "dev_key",
    "device_key",
    "devkey",
    "netkey",
    "appkey",
    "keys",
    # Raw .connect JSON can contain multiple keys nested arbitrarily.
    "connect_file",
    "raw_connect",
    "raw",
}

# Standard BT Mesh service UUIDs (lowercase short form + full 128-bit).
MESH_PROXY_UUIDS = {
    "1828",
    "00001828-0000-1000-8000-00805f9b34fb",
}
MESH_PROVISIONING_UUIDS = {
    "1827",
    "00001827-0000-1000-8000-00805f9b34fb",
}


def _normalise_uuid(uuid: Any) -> str:
    return str(uuid).lower()


def _advertises(service_uuids: list[str], targets: set[str]) -> bool:
    return any(_normalise_uuid(u) in targets for u in (service_uuids or []))


def _bluetooth_snapshot(hass: HomeAssistant, mac: str) -> dict[str, Any]:
    """Collect everything HA's bluetooth stack knows about `mac`."""
    info: dict[str, Any] = {
        "visible_to_ha": False,
        "connectable": False,
        "address_present": False,
    }

    try:
        info["address_present"] = bluetooth.async_address_present(
            hass, mac, connectable=True
        )
    except Exception as err:  # noqa: BLE001
        info["address_present_error"] = repr(err)

    try:
        device = bluetooth.async_ble_device_from_address(
            hass, mac, connectable=True
        )
    except Exception as err:  # noqa: BLE001
        device = None
        info["ble_device_error"] = repr(err)

    if device is not None:
        info["visible_to_ha"] = True
        info["connectable"] = True
        info["ble_device_name"] = getattr(device, "name", None)

    try:
        service_info = bluetooth.async_last_service_info(
            hass, mac, connectable=True
        )
    except Exception as err:  # noqa: BLE001
        service_info = None
        info["service_info_error"] = repr(err)

    if service_info is not None:
        now = time.monotonic()
        last_seen = getattr(service_info, "time", None)
        seconds_ago: float | None = None
        if isinstance(last_seen, (int, float)):
            seconds_ago = round(max(0.0, now - float(last_seen)), 2)

        advertised_uuids = list(getattr(service_info, "service_uuids", []) or [])
        info.update({
            "rssi": getattr(service_info, "rssi", None),
            "advertised_service_uuids": advertised_uuids,
            "advertises_mesh_proxy": _advertises(advertised_uuids, MESH_PROXY_UUIDS),
            "advertises_mesh_provisioning": _advertises(
                advertised_uuids, MESH_PROVISIONING_UUIDS,
            ),
            "last_seen_seconds_ago": seconds_ago,
            "source_adapter": getattr(service_info, "source", None),
            "manufacturer_data_keys": sorted(
                (getattr(service_info, "manufacturer_data", {}) or {}).keys()
            ),
            "advertisement_name": getattr(service_info, "name", None),
        })

    return info


def _coordinator_snapshot(coordinator: HaefeleCoordinator) -> dict[str, Any]:
    proxy = coordinator.proxy
    session = coordinator.session

    proxy_state: dict[str, Any] = {
        "is_connected": False,
        "active_mac": None,
        "active_name": None,
        "candidates_order": [],
        "filter_addresses": [],
    }
    if proxy is not None:
        proxy_state["is_connected"] = proxy.is_connected
        proxy_state["active_mac"] = proxy.active_mac
        proxy_state["active_name"] = proxy.active_name
        # Internal ordering of last-success-first candidates.
        proxy_state["candidates_order"] = [
            {"mac": mac, "name": name}
            for mac, name in (proxy._candidates or [])  # noqa: SLF001
        ]
        proxy_state["filter_addresses"] = [
            f"0x{a:04X}" for a in (proxy._filter_addresses or [])  # noqa: SLF001
        ]

    session_state: dict[str, Any] = {}
    if session is not None:
        session_state = {
            "src_address": f"0x{session.src:04X}",
            "iv_index": session.iv_index,
            "nid": f"0x{session.nid:02X}",
            "aid": f"0x{session.aid:02X}",
        }

    # SEQ snapshot — just the numeric state, keyed by hex SRC for readability.
    seq_snapshot = {
        f"0x{src:04X}": seq
        for src, seq in (coordinator._seq_state or {}).items()  # noqa: SLF001
    }

    return {
        "availability": dict(coordinator.availability),
        "session": session_state,
        "proxy": proxy_state,
        "seq_state": seq_snapshot,
        "persisted_iv_index": coordinator._persisted_iv_index,  # noqa: SLF001
    }


def _nodes_snapshot(
    hass: HomeAssistant, coordinator: HaefeleCoordinator
) -> list[dict[str, Any]]:
    nodes_cfg = coordinator._nodes_cfg or []  # noqa: SLF001
    active_mac = (
        coordinator.proxy.active_mac if coordinator.proxy is not None else None
    )
    active_mac_upper = active_mac.upper() if active_mac else None

    out: list[dict[str, Any]] = []
    for node in nodes_cfg:
        mac = str(node.get("mac") or "").upper()
        unicast = node.get("unicast")
        groups = node.get("groups") or []
        entry: dict[str, Any] = {
            "name": node.get("name"),
            "mac": mac,
            "unicast": f"0x{unicast:04X}" if isinstance(unicast, int) else unicast,
            "groups": [
                f"0x{g:04X}" if isinstance(g, int) else g for g in groups
            ],
            "type": node.get("type"),
            "is_active_proxy": bool(active_mac_upper and mac == active_mac_upper),
            "bluetooth": _bluetooth_snapshot(hass, mac) if mac else {},
        }
        out.append(entry)
    return out


def _redacted_config(entry: ConfigEntry) -> dict[str, Any]:
    data = dict(entry.data)
    # Scrub any per-node DevKey hiding inside the node list before the
    # generic redactor runs.
    for node in data.get("nodes", []) or []:
        if isinstance(node, dict):
            for k in list(node.keys()):
                if "key" in k.lower():
                    node[k] = "**REDACTED**"
    return async_redact_data(data, TO_REDACT)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for the Häfele Connect Mesh config entry."""
    coordinator: HaefeleCoordinator | None = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id)
    )

    payload: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "version": entry.version,
            "source": entry.source,
            "domain": entry.domain,
            "options": dict(entry.options),
        },
        "config": _redacted_config(entry),
    }

    if coordinator is None:
        payload["coordinator"] = None
        payload["nodes"] = []
        payload["active_proxy_gatt"] = None
        return async_redact_data(payload, TO_REDACT)

    payload["coordinator"] = _coordinator_snapshot(coordinator)
    payload["nodes"] = _nodes_snapshot(hass, coordinator)

    # Full GATT tree of whichever node is currently acting as proxy —
    # read from the cached bleak client, so no extra BLE traffic.
    active_gatt = None
    if coordinator.proxy is not None:
        try:
            active_gatt = coordinator.proxy.dump_active_gatt_tree()
        except Exception as err:  # noqa: BLE001
            active_gatt = {"error": repr(err)}
    payload["active_proxy_gatt"] = active_gatt

    # One more safety pass — redact anything we might have missed.
    return async_redact_data(payload, TO_REDACT)
