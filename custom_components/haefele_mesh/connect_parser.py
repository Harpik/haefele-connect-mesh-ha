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

    # --- Provisioner address ---
    provisioner_addr = 0
    provs = data.get("provisioners", [])
    if isinstance(provs, list) and provs:
        ranges = provs[0].get("allocatedUnicastRange", [])
        if ranges and isinstance(ranges, list):
            try:
                provisioner_addr = int(ranges[0].get("highAddress", "0"), 16)
            except (ValueError, TypeError):
                pass
    # Also check tos_network
    tos_net = data.get("tos_network", {})
    if isinstance(tos_net, dict):
        pma = tos_net.get("provisionerMeshAddress")
        if pma is not None:
            try:
                provisioner_addr = int(pma)
            except (ValueError, TypeError):
                pass

    # --- Parse nodes ---
    nodes = []
    node_list = data.get("nodes", [])

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
        "nodes": nodes,
    }
