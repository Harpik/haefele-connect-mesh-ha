"""Parser for Häfele Connect Mesh .connect export files.

Extracts network keys, app keys, device keys, IV index and device list
from the JSON configuration exported by the Häfele Connect app.

The .connect format follows the Bluetooth Mesh CDB 1.0.1 schema with
Häfele-specific extensions under tos_* and tos_node/tos_devices per node.
"""

import json
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

# Häfele device type prefixes that are lights (not remotes/sensors)
_LIGHT_TYPE_PREFIXES = (
    "com.haefele.meshbox",
    "com.haefele.driver",
    "com.haefele.led",
)

_REMOTE_TYPE_PREFIXES = (
    "com.haefele.remote",
    "com.haefele.sensor",
    "com.haefele.switch",
)

MODEL_GENERIC_ONOFF_SERVER = 0x1000
MODEL_LIGHT_LIGHTNESS_SERVER = 0x1300
MODEL_LIGHT_CTL_SERVER = 0x1303
MODEL_LIGHT_CTL_TEMPERATURE_SERVER = 0x1306
MODEL_LIGHT_HSL_SERVER = 0x1307

_MODEL_ID_KEYS = ("modelId", "modelID", "model_id", "id")


def _parse_model_id(raw: Any) -> int | None:
    """Parse a Bluetooth Mesh model ID from CDB JSON."""
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        try:
            # CDB model IDs are hex strings: "1000" means 0x1000, not decimal 1000.
            return int(raw.strip(), 16)
        except ValueError:
            return None
    return None


def _model_id_from_entry(model: Any) -> int | None:
    """Extract one model ID from a CDB model entry."""
    if isinstance(model, dict):
        for key in _MODEL_ID_KEYS:
            if key in model:
                return _parse_model_id(model[key])
        return None
    return _parse_model_id(model)


def detect_device_type_from_models(models: list[Any]) -> str:
    """Detect a light device type from Bluetooth Mesh SIG server models."""
    model_ids = {
        model_id
        for model in models
        if (model_id := _model_id_from_entry(model)) is not None
    }

    if MODEL_LIGHT_HSL_SERVER in model_ids:
        return "rgb"
    if (
        MODEL_LIGHT_CTL_SERVER in model_ids
        or MODEL_LIGHT_CTL_TEMPERATURE_SERVER in model_ids
    ):
        return "tunable_white"
    if MODEL_LIGHT_LIGHTNESS_SERVER in model_ids:
        return "dimmable"
    if MODEL_GENERIC_ONOFF_SERVER in model_ids:
        return "onoff"
    return "unknown"


def _detect_device_type_from_node_models(node: dict[str, Any]) -> str:
    """Detect a light device type from all element models in a node."""
    node_models: list[Any] = []
    elements = node.get("elements", [])
    if not isinstance(elements, list):
        return "unknown"

    for element in elements:
        if not isinstance(element, dict):
            continue
        models = element.get("models", [])
        if isinstance(models, list):
            node_models.extend(models)

    return detect_device_type_from_models(node_models)


def _detect_device_type_from_tos_type(node_type: str, name: str) -> str | None:
    """Fallback device type detection from Häfele product metadata."""
    if node_type:
        type_lower = node_type.lower()
        if any(type_lower.startswith(p) for p in _REMOTE_TYPE_PREFIXES):
            _LOGGER.debug("Skipping non-light node %s (%s)", name, node_type)
            return None
        if "tw" in type_lower or "tunable" in type_lower:
            return "tunable_white"
        if "rgb" in type_lower or "hsl" in type_lower or "color" in type_lower:
            return "rgb"
        if "relay" in type_lower or "onoff" in type_lower or "switch_out" in type_lower:
            return "onoff"
        if "dim" in type_lower:
            return "dimmable"
        if any(type_lower.startswith(p) for p in _LIGHT_TYPE_PREFIXES):
            return "dimmable"
        return "unknown"

    # Fallback: guess from name
    name_lower = name.lower()
    if "remote" in name_lower or "sensor" in name_lower or "switch" in name_lower:
        return None
    return "tunable_white"  # safe default for Häfele lights


def _extract_key_from_list(key_list: Any) -> str | None:
    """Extract first key value from a netKeys/appKeys list."""
    if isinstance(key_list, list) and key_list:
        obj = key_list[0]
        if isinstance(obj, dict):
            return obj.get("key") or obj.get("netKey") or obj.get("appKey") or obj.get("value")
        if isinstance(obj, str):
            return obj
    if isinstance(key_list, str):
        return key_list
    return None


def _parse_unicast(raw: Any) -> int:
    """Parse unicast address from string or int."""
    if raw is None:
        return 0
    try:
        if isinstance(raw, str):
            return int(raw, 16)
        return int(raw)
    except (ValueError, TypeError):
        return 0


def _normalize_hex_key(key: str) -> str:
    """Normalize a hex key string (remove spaces/dashes, lowercase)."""
    return key.replace(" ", "").replace("-", "").lower()


def parse_connect_file(content: str) -> dict:
    """
    Parse a .connect export file and return structured config.

    The .connect file is Bluetooth Mesh CDB JSON with Häfele extensions.
    Keys are at top-level (netKeys, appKeys) following the CDB schema.
    Node metadata (name, MAC, type) lives in tos_node/tos_devices per node.

    Returns:
        {
            "network_key": "hex string",
            "app_key": "hex string",
            "iv_index": int,
            "provisioner_address": int,
            "allocated_unicast_ranges": [[low, high]],
            "reserved_unicasts": [{"unicast": int, "elements": int}],
            "nodes": [
                {
                    "name": str,
                    "uuid": str,
                    "mac": str,
                    "unicast": int,
                    "device_key": str,
                    "groups": [int],
                    "device_type": str,
                    "firmware": str,
                }
            ]
        }
    """
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in .connect file: {e}")

    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object at top level of .connect file")

    # --- Extract keys (CDB schema: top-level netKeys/appKeys) ---
    net_key = _extract_key_from_list(data.get("netKeys"))
    app_key = _extract_key_from_list(data.get("appKeys"))

    # Fallback: look inside meshNetwork wrapper (some older exports)
    if not net_key or not app_key:
        mesh_net = data.get("meshNetwork") or data.get("network") or data.get("mesh")
        if isinstance(mesh_net, dict):
            if not net_key:
                net_key = _extract_key_from_list(
                    mesh_net.get("netKeys") or mesh_net.get("networkKeys")
                )
            if not app_key:
                app_key = _extract_key_from_list(
                    mesh_net.get("appKeys") or mesh_net.get("applicationKeys")
                )

    if not net_key or not app_key:
        raise ValueError(
            "Could not find network key or application key in .connect file. "
            "Make sure you exported a valid Häfele Connect Mesh configuration."
        )

    net_key = _normalize_hex_key(net_key)
    app_key = _normalize_hex_key(app_key)

    if len(net_key) != 32 or len(app_key) != 32:
        raise ValueError("Network key or application key has invalid length (must be 32 hex chars)")

    # --- IV Index ---
    iv_index = 0
    iv_raw = data.get("ivIndex") or data.get("IVIndex")
    if iv_raw is not None:
        try:
            iv_index = int(iv_raw)
        except (ValueError, TypeError):
            pass

    # --- Provisioner address + allocated unicast ranges ---
    # The Häfele app stores its own unicast address in
    # tos_network.provisionerMeshAddress as a HEX string (e.g. "7FF9").
    # Older builds fed that to int() as decimal, which always failed and left
    # the highAddress of the first allocated range here instead (not an
    # address anyone uses). The highAddress heuristic is kept only as a
    # fallback for exports without tos_network.
    provisioner_addr = 0
    allocated_unicast_ranges: list[list[int]] = []
    provs = data.get("provisioners", [])
    if isinstance(provs, list):
        for prov in provs:
            if not isinstance(prov, dict):
                continue
            prov_ranges = prov.get("allocatedUnicastRange")
            if not isinstance(prov_ranges, list):
                continue
            for rng in prov_ranges:
                if not isinstance(rng, dict):
                    continue
                lo = _parse_unicast(rng.get("lowAddress"))
                hi = _parse_unicast(rng.get("highAddress"))
                if 0 < lo <= hi <= 0x7FFF:
                    allocated_unicast_ranges.append([lo, hi])
        if provs and isinstance(provs[0], dict):
            first_ranges = provs[0].get("allocatedUnicastRange")
            first = first_ranges[0] if isinstance(first_ranges, list) and first_ranges else None
            if isinstance(first, dict):
                provisioner_addr = _parse_unicast(first.get("highAddress"))
    tos_net = data.get("tos_network", {})
    if isinstance(tos_net, dict):
        pma = _parse_unicast(tos_net.get("provisionerMeshAddress"))
        if 0 < pma <= 0x7FFF:
            provisioner_addr = pma

    # --- Parse nodes ---
    nodes = []
    node_list = data.get("nodes", [])

    # Every provisioned unicast range in the network, including the nodes
    # skipped below (remotes, sensors, wall switches). They never get an
    # entity, but they do emit with their own SRC, so the coordinator must
    # never pick one of these addresses as its own SRC.
    reserved_unicasts: list[dict[str, int]] = []
    for node in node_list if isinstance(node_list, list) else []:
        if not isinstance(node, dict):
            continue
        addr = _parse_unicast(node.get("unicastAddress"))
        if addr == 0:
            continue
        elements = node.get("elements")
        count = len(elements) if isinstance(elements, list) else 0
        reserved_unicasts.append({"unicast": addr, "elements": max(count, 1)})

    for node in node_list:
        if not isinstance(node, dict):
            continue

        uuid = node.get("UUID") or node.get("uuid") or ""
        unicast = _parse_unicast(node.get("unicastAddress"))
        device_key = node.get("deviceKey", "")

        if unicast == 0:
            continue

        # --- Häfele metadata from tos_node / tos_devices ---
        tos_node = node.get("tos_node", {}) or {}
        tos_devices = node.get("tos_devices", []) or []

        node_type = tos_node.get("type", "")
        mac = tos_node.get("proxyBleAddress") or tos_node.get("provisionerBleAddress") or ""
        firmware = tos_node.get("firmwareVersion", "")

        # Get name from tos_devices (first device entry)
        name = "Unknown"
        device_type = "unknown"
        if isinstance(tos_devices, list) and tos_devices:
            dev = tos_devices[0]
            if isinstance(dev, dict):
                name = dev.get("name", "").strip() or "Unknown"

        type_lower = node_type.lower() if isinstance(node_type, str) else ""
        if any(type_lower.startswith(p) for p in _REMOTE_TYPE_PREFIXES):
            _LOGGER.debug("Skipping non-light node %s (%s)", name, node_type)
            continue

        # Prefer Bluetooth Mesh SIG server models when present. The
        # tos_node.type string is a vendor product ID and is only a
        # compatibility fallback for exports without useful model data.
        device_type = _detect_device_type_from_node_models(node)
        if device_type == "unknown":
            fallback_type = _detect_device_type_from_tos_type(node_type, name)
            if fallback_type is None:
                continue
            device_type = fallback_type

        # Fallback MAC: not in tos_node (shouldn't happen, but be safe)
        if not mac:
            mac = node.get("macAddress") or ""

        # --- Group addresses from element subscriptions ---
        groups = []
        elements = node.get("elements", [])
        if isinstance(elements, list):
            for element in elements:
                if not isinstance(element, dict):
                    continue
                models = element.get("models", [])
                if not isinstance(models, list):
                    continue
                for model in models:
                    if not isinstance(model, dict):
                        continue
                    subs = model.get("subscribe", [])
                    if not isinstance(subs, list):
                        continue
                    for sub in subs:
                        try:
                            addr = int(sub, 16) if isinstance(sub, str) else int(sub)
                            if addr >= 0xC000 and addr not in groups:
                                groups.append(addr)
                        except (ValueError, TypeError):
                            pass

        nodes.append({
            "name": name,
            "uuid": uuid,
            "mac": mac,
            "unicast": unicast,
            "device_key": _normalize_hex_key(device_key) if device_key else "",
            "groups": groups,
            "device_type": device_type,
            "firmware": firmware,
        })

    if not nodes:
        raise ValueError(
            "No light nodes found in .connect file. "
            "Make sure the file contains provisioned Häfele Mesh devices."
        )

    return {
        "network_key": net_key,
        "app_key": app_key,
        "iv_index": iv_index,
        "provisioner_address": provisioner_addr,
        "allocated_unicast_ranges": allocated_unicast_ranges,
        "reserved_unicasts": reserved_unicasts,
        "nodes": nodes,
        "remotes": _parse_remotes(node_list, data.get("tos_groups")),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Client models a remote position publishes from: Generic OnOff, Generic
# Level and Scene client.
REMOTE_CLIENT_MODELS = (0x1001, 0x1003, 0x1205)
# Elements per remote position (module) on the Häfele remote v2.
_ELEMENTS_PER_MODULE = 4
_PERIOD_RESOLUTIONS_MS = {100: 0, 1000: 1, 10000: 2, 600000: 3}


def _publish_fields(publish: Any) -> dict[str, int] | None:
    """The app's publication of one client model, as Config message fields."""
    if not isinstance(publish, dict):
        return None
    try:
        address = int(str(publish.get("address")), 16)
    except (TypeError, ValueError):
        return None
    if not 0 < address <= 0xFFFF:
        return None
    ttl = _int_or_none(publish.get("ttl"))
    index = _int_or_none(publish.get("index"))
    period = publish.get("period") if isinstance(publish.get("period"), dict) else {}
    steps = _int_or_none(period.get("numberOfSteps")) or 0
    resolution = _PERIOD_RESOLUTIONS_MS.get(_int_or_none(period.get("resolution")) or 100, 0)
    retransmit = publish.get("retransmit") if isinstance(publish.get("retransmit"), dict) else {}
    count = _int_or_none(retransmit.get("count")) or 0
    interval = _int_or_none(retransmit.get("interval")) or 50
    return {
        "address": address,
        "app_key_index": index if index is not None and 0 <= index <= 0xFFF else 0,
        "ttl": ttl if ttl is not None and 0 <= ttl <= 0xFF else 0xFF,
        "period": (steps & 0x3F) | (resolution << 6),
        "retransmit": (min(max(count, 0), 7)) | (min(max(interval // 50 - 1, 0), 31) << 3),
    }


def _remote_clients(elements: Any, unicast: int) -> dict[int, list[dict[str, Any]]]:
    """Client models of each remote position, with the app's publication.

    ``{position: [{"element", "model", "publish"}]}``; ``publish`` is None
    where the app left the model without one (a disabled position).
    """
    clients: dict[int, list[dict[str, Any]]] = {}
    for index, element in enumerate(elements if isinstance(elements, list) else []):
        if not isinstance(element, dict):
            continue
        for model in element.get("models") or []:
            if not isinstance(model, dict):
                continue
            try:
                model_id = int(str(model.get("modelId") or model.get("modelIdentifier")), 16)
            except (TypeError, ValueError):
                continue
            if model_id not in REMOTE_CLIENT_MODELS:
                continue
            clients.setdefault(index // _ELEMENTS_PER_MODULE, []).append({
                "element": unicast + index,
                "model": model_id,
                "publish": _publish_fields(model.get("publish")),
            })
    return clients


def _parse_remotes(node_list: Any, tos_groups: Any) -> list[dict[str, Any]]:
    """Describe every Häfele remote: name, address range and its modules.

    A remote has N selectable positions ("modules", 6 on the remote v2),
    each with the same number of consecutive elements. Each module controls
    one group from the app; ``tos_groups`` gives that group's name.
    """
    # Best effort: an odd field on a remote must never reject the whole
    # import (the lights are what matters), so bad values are dropped.
    group_names: dict[str, str] = {}
    group_addresses: dict[str, int] = {}
    if isinstance(tos_groups, list):
        for g in tos_groups:
            if isinstance(g, dict) and g.get("id"):
                group_names[str(g["id"])] = str(g.get("name") or "").strip()
                addr = _int_or_none(g.get("multicastAddress"))
                if addr is not None and 0xC000 <= addr <= 0xFEFF:
                    group_addresses[str(g["id"])] = addr

    remotes: list[dict[str, Any]] = []
    for node in node_list if isinstance(node_list, list) else []:
        if not isinstance(node, dict):
            continue
        tos_node = node.get("tos_node")
        tos_node = tos_node if isinstance(tos_node, dict) else {}
        node_type = tos_node.get("type", "")
        if not (isinstance(node_type, str) and node_type.lower().startswith("com.haefele.remote")):
            continue
        try:
            unicast = _parse_unicast(node.get("unicastAddress"))
        except (TypeError, ValueError):
            continue
        elements = node.get("elements")
        element_count = len(elements) if isinstance(elements, list) else 0
        element_count = max(element_count, _int_or_none(tos_node.get("elementCount")) or 0)
        if not 0 < unicast <= 0x7FFF or element_count == 0:
            continue
        tos_devices = node.get("tos_devices") or []
        dev = tos_devices[0] if isinstance(tos_devices, list) and tos_devices else {}
        dev = dev if isinstance(dev, dict) else {}
        modules_raw = dev.get("modules") if isinstance(dev.get("modules"), list) else []
        clients = _remote_clients(elements, unicast)
        modules: list[dict[str, Any]] = []
        for pos, mod in enumerate(modules_raw):
            if not isinstance(mod, dict):
                continue
            settings = mod.get("controlSettings") or {}
            settings = settings if isinstance(settings, dict) else {}
            ctrl_type = settings.get("type")
            scenes = mod.get("sceneIdList") if isinstance(mod.get("sceneIdList"), list) else []
            group_id = str(settings.get("groupId"))
            modules.append({
                "index": pos,
                "enabled": ctrl_type not in (None, "disabled"),
                "name": group_names.get(group_id, "") or None,
                "group_address": group_addresses.get(group_id),
                "scenes": [s if isinstance(s, int) else 0 for s in scenes],
                "clients": clients.get(pos, []),
            })
        firmware = tos_node.get("firmwareVersion")
        mac = tos_node.get("proxyBleAddress") or tos_node.get("provisionerBleAddress")
        dev_key = node.get("deviceKey")
        dev_key = _normalize_hex_key(dev_key) if isinstance(dev_key, str) else ""
        if len(dev_key) != 32 or any(c not in "0123456789abcdef" for c in dev_key):
            dev_key = ""
        remotes.append({
            "name": str(dev.get("name") or "").strip() or "Remote",
            "uuid": str(node.get("UUID") or node.get("uuid") or ""),
            "mac": mac if isinstance(mac, str) else "",
            "unicast": unicast,
            "elements": element_count,
            "type": node_type,
            "firmware": firmware if isinstance(firmware, str) else "",
            "device_key": dev_key,
            "modules": modules,
        })
    return remotes
