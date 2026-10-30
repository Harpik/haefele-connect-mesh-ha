"""Turn messages published by a Häfele remote into button events.

A Häfele remote has several selectable positions ("modules"). Each module
owns ``ELEMENTS_PER_MODULE`` consecutive elements starting at
``unicast + module * ELEMENTS_PER_MODULE``, and each element publishes to
one address of the module's group (verified on a remote v2):

  offset 0: Generic OnOff (centre button) and Generic Level (ring up/down)
            to the group's on/off/lightness address
  offset 1: Generic Level to the group's saturation address
  offset 2: Generic Level to the group's colour-temperature address
            (ring left/right)
  offset 3: Generic Level to the group's hue address

The scene buttons A/B/C publish Scene Recall from offsets 0/1/2.

The ring sends a Level *Set* to the extreme of the range rather than a
step: 0x7FFF for up/right, 0x8000/0x8001 for down/left.

Each press arrives twice (the remote retransmits with the same TID), so
events are de-duplicated on (source, opcode, TID).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .const import DOMAIN

ELEMENTS_PER_MODULE = 4
# Fired on the Home Assistant bus for every press; device triggers listen to it.
REMOTE_BUS_EVENT = f"{DOMAIN}_remote_event"
DEDUP_WINDOW = 6.0  # seconds; a TID is reused only after 256 presses

OP_ONOFF_SET = 0x8202
OP_ONOFF_SET_UNACK = 0x8203
OP_LEVEL_SET = 0x8206
OP_LEVEL_SET_UNACK = 0x8207
OP_SCENE_RECALL = 0x8242
OP_SCENE_RECALL_UNACK = 0x8243

EVENT_TURN_ON = "turn_on"
EVENT_TURN_OFF = "turn_off"
EVENT_BRIGHTNESS_MAX = "brightness_max"
EVENT_BRIGHTNESS_MIN = "brightness_min"
EVENT_COLOR_TEMP_COLD = "color_temp_cold"
EVENT_COLOR_TEMP_WARM = "color_temp_warm"
EVENT_SATURATION_MAX = "saturation_max"
EVENT_SATURATION_MIN = "saturation_min"
EVENT_HUE_MAX = "hue_max"
EVENT_HUE_MIN = "hue_min"
EVENT_SCENE_A = "scene_a"
EVENT_SCENE_B = "scene_b"
EVENT_SCENE_C = "scene_c"

EVENT_TYPES = [
    EVENT_TURN_ON,
    EVENT_TURN_OFF,
    EVENT_BRIGHTNESS_MAX,
    EVENT_BRIGHTNESS_MIN,
    EVENT_COLOR_TEMP_COLD,
    EVENT_COLOR_TEMP_WARM,
    EVENT_SATURATION_MAX,
    EVENT_SATURATION_MIN,
    EVENT_HUE_MAX,
    EVENT_HUE_MIN,
    EVENT_SCENE_A,
    EVENT_SCENE_B,
    EVENT_SCENE_C,
]

# Level events by element offset: (event for the top of the range, bottom).
_LEVEL_EVENTS = {
    0: (EVENT_BRIGHTNESS_MAX, EVENT_BRIGHTNESS_MIN),
    1: (EVENT_SATURATION_MAX, EVENT_SATURATION_MIN),
    2: (EVENT_COLOR_TEMP_COLD, EVENT_COLOR_TEMP_WARM),
    3: (EVENT_HUE_MAX, EVENT_HUE_MIN),
}
_SCENE_EVENTS = {0: EVENT_SCENE_A, 1: EVENT_SCENE_B, 2: EVENT_SCENE_C}


@dataclass(frozen=True)
class RemoteEvent:
    """One de-duplicated press on a remote module."""

    remote_unicast: int
    module: int
    event_type: str
    attributes: dict[str, Any] = field(default_factory=dict)
    # Group the pressed position controls, when the export names it.
    group_address: int | None = None


def remote_device_identifier(remote_cfg: dict) -> tuple[str, str]:
    """Stable device-registry identifier for a remote."""
    key = remote_cfg.get("uuid") or f"{remote_cfg.get('unicast', 0):04x}"
    return (DOMAIN, f"remote_{str(key).lower()}")


def module_info(remote_cfg: dict, module: int) -> dict:
    """The export's description of one position (defaults when absent)."""
    modules = remote_cfg.get("modules") or []
    if module < len(modules):
        return modules[module]
    return {"index": module, "enabled": True, "name": None, "scenes": []}


def bus_event_data(device_id: str, event: RemoteEvent) -> dict[str, Any]:
    """Payload of REMOTE_BUS_EVENT; device triggers match on these keys."""
    return {
        **event.attributes,
        "device_id": device_id,
        "module": event.module + 1,
        "type": event.event_type,
    }


def module_count(remote_cfg: dict) -> int:
    """Number of modules: the larger of the export's list and the element count."""
    modules = remote_cfg.get("modules") or []
    from_elements = int(remote_cfg.get("elements") or 0) // ELEMENTS_PER_MODULE
    return max(len(modules), from_elements, 1)


class RemoteDecoder:
    """Map (src, opcode, params) from any known remote to a RemoteEvent."""

    def __init__(self, remotes_cfg: list[dict]) -> None:
        self._ranges: list[tuple[int, int, dict]] = []
        for remote in remotes_cfg or []:
            unicast = remote.get("unicast")
            elements = remote.get("elements")
            if isinstance(unicast, int) and isinstance(elements, int) and elements > 0:
                self._ranges.append((unicast, unicast + elements, remote))
        self._seen: dict[tuple[int, int, int], float] = {}

    def owns(self, src: int) -> bool:
        return any(lo <= src < hi for lo, hi, _r in self._ranges)

    def decode(
        self, src: int, opcode: int, params: bytes, now: float | None = None,
    ) -> RemoteEvent | None:
        for lo, hi, remote in self._ranges:
            if lo <= src < hi:
                break
        else:
            return None
        module, offset = divmod(src - lo, ELEMENTS_PER_MODULE)
        if module >= module_count(remote):
            return None

        event = _event_for(opcode, params, offset)
        if event is None:
            return None
        event_type, tid, attributes = event

        now = time.monotonic() if now is None else now
        self._seen = {k: t for k, t in self._seen.items() if now - t < DEDUP_WINDOW}
        key = (src, opcode, tid)
        if key in self._seen:
            return None
        self._seen[key] = now

        modules = remote.get("modules") or []
        group = modules[module].get("group_address") if module < len(modules) else None
        return RemoteEvent(lo, module, event_type, attributes, group)


def _event_for(opcode: int, params: bytes, offset: int) -> tuple[str, int, dict] | None:
    """(event_type, tid, attributes) for one message, or None if not a press."""
    if opcode in (OP_ONOFF_SET, OP_ONOFF_SET_UNACK) and offset == 0 and len(params) >= 2:
        return (EVENT_TURN_ON if params[0] else EVENT_TURN_OFF), params[1], {}
    if opcode in (OP_LEVEL_SET, OP_LEVEL_SET_UNACK) and len(params) >= 3:
        level = int.from_bytes(params[0:2], "little", signed=True)
        top, bottom = _LEVEL_EVENTS[offset]
        return (top if level >= 0 else bottom), params[2], {"level": level}
    if opcode in (OP_SCENE_RECALL, OP_SCENE_RECALL_UNACK) and len(params) >= 3:
        event_type = _SCENE_EVENTS.get(offset)
        if event_type is None:
            return None
        scene = int.from_bytes(params[0:2], "little")
        return event_type, params[2], {"scene": scene}
    return None
