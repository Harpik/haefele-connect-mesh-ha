"""What each position of a Häfele remote publishes to, set from Home Assistant.

Pure Python: the Configuration messages (Mesh Profile 1.0.1 §4.3.2.15-17)
and the plan for each mode. The coordinator sends them while the remote is
awake, i.e. right after it was pressed.

Modes of a position:

* ``app``: what the Häfele app configured (from the .connect export).
* ``ha_only``: every control publishes to ``HA_ONLY_ADDRESS``. No light
  subscribes to it, so nothing reacts; Home Assistant still sees the
  presses (it identifies them by the sending element, not the address).
* ``disabled``: no publication; the position sends nothing.
"""

from __future__ import annotations

from typing import Any

MODE_APP = "app"
MODE_HA_ONLY = "ha_only"
MODE_DISABLED = "disabled"
MODES = (MODE_APP, MODE_HA_ONLY, MODE_DISABLED)

# Group address used by "ha_only" positions: the last group address, far
# from the ones the Häfele app hands out from 0xC000 upwards.
HA_ONLY_ADDRESS = 0xFEFF

OP_MODEL_PUBLICATION_SET = 0x03
OP_MODEL_PUBLICATION_GET = 0x8018
OP_MODEL_PUBLICATION_STATUS = 0x8019

# Publish TTL 0xFF: use the remote's Default TTL, as the app does.
_DEFAULT_TTL = 0xFF
# One retransmission 100 ms later: what the app configures (count 1,
# interval steps 1).
_DEFAULT_RETRANSMIT = 0x09


def publication_for(mode: str, client: dict[str, Any]) -> dict[str, int]:
    """Publication fields a client model gets in ``mode``.

    ``client`` is one entry of a position's ``clients`` from the parser.
    A model the app left without publication is disabled in ``app`` mode.
    """
    app = client.get("publish")
    if mode == MODE_APP and app:
        return dict(app)
    if mode == MODE_HA_ONLY:
        return {
            "address": HA_ONLY_ADDRESS,
            "app_key_index": (app or {}).get("app_key_index", 0),
            "ttl": _DEFAULT_TTL,
            "period": 0,
            "retransmit": (app or {}).get("retransmit", _DEFAULT_RETRANSMIT),
        }
    return {"address": 0, "app_key_index": 0, "ttl": 0, "period": 0, "retransmit": 0}


def encode_publication_set(element: int, model: int, publication: dict[str, int]) -> bytes:
    """Parameters of a Config Model Publication Set for a SIG model."""
    return (
        element.to_bytes(2, "little")
        + publication["address"].to_bytes(2, "little")
        # AppKeyIndex (12 bits), Credential Flag 0, RFU.
        + (publication["app_key_index"] & 0xFFF).to_bytes(2, "little")
        + bytes([
            publication["ttl"] & 0xFF,
            publication["period"] & 0xFF,
            publication["retransmit"] & 0xFF,
        ])
        + model.to_bytes(2, "little")
    )


def encode_publication_get(element: int, model: int) -> bytes:
    """Parameters of a Config Model Publication Get for a SIG model."""
    return element.to_bytes(2, "little") + model.to_bytes(2, "little")


def parse_publication_status(params: bytes) -> dict[str, int] | None:
    """Config Model Publication Status (SIG model), or None if malformed."""
    if len(params) != 12:
        return None
    return {
        "status": params[0],
        "element": int.from_bytes(params[1:3], "little"),
        "address": int.from_bytes(params[3:5], "little"),
        "app_key_index": int.from_bytes(params[5:7], "little") & 0xFFF,
        "ttl": params[7],
        "period": params[8],
        "retransmit": params[9],
        "model": int.from_bytes(params[10:12], "little"),
    }


def default_mode(module: dict[str, Any]) -> str:
    """Mode of a position before Home Assistant changed it."""
    return MODE_APP if module.get("enabled") else MODE_DISABLED


def available_modes(module: dict[str, Any]) -> list[str]:
    """``app`` only exists where the app configured the position."""
    if any(c.get("publish") for c in module.get("clients") or []):
        return list(MODES)
    return [MODE_HA_ONLY, MODE_DISABLED]
