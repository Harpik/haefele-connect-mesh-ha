"""Pure helpers for choosing our BT Mesh source address (no HA imports)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .const import (
    LEGACY_SRC_ADDRESSES,
    ROTATION_SEARCH_TOP,
    SRC_ADDRESS_BASE,
    UNICAST_MAX,
)


def valid_ranges(raw: Any) -> list[tuple[int, int]]:
    """Normalise stored allocated_unicast_ranges, dropping malformed items."""
    out: list[tuple[int, int]] = []
    for rng in raw if isinstance(raw, list) else []:
        if (
            isinstance(rng, (list, tuple)) and len(rng) == 2
            and all(isinstance(v, int) for v in rng)
            and 0 < rng[0] <= rng[1] <= UNICAST_MAX
        ):
            out.append((rng[0], rng[1]))
    return out


def in_ranges(address: int, ranges: Iterable[tuple[int, int]]) -> bool:
    return any(lo <= address <= hi for lo, hi in ranges)


def _known_node_addresses(parsed: Mapping[str, Any]) -> set[int]:
    taken: set[int] = set()
    for r in parsed.get("reserved_unicasts") or []:
        if not isinstance(r, dict):
            continue
        unicast, count = r.get("unicast"), r.get("elements")
        if isinstance(unicast, int) and unicast > 0:
            n = count if isinstance(count, int) and count > 0 else 1
            taken.update(range(unicast, unicast + n))
    for n in parsed.get("nodes") or []:
        unicast = n.get("unicast") if isinstance(n, dict) else None
        if isinstance(unicast, int) and unicast > 0:
            taken.add(unicast)
    prov = parsed.get("provisioner_address")
    if isinstance(prov, int) and prov > 0:
        taken.add(prov)
    return taken


def pick_initial_src(parsed: Mapping[str, Any]) -> int:
    """SRC for a brand-new config entry.

    Keeps the historical SRC_ADDRESS_BASE when nothing says it is unsafe.
    When the export carries provisioner ranges and SRC_ADDRESS_BASE falls
    inside one (the H\u00e4fele app typically allocates 0x0001-0x1000), start
    instead at the first free address outside every range, searching down
    from ROTATION_SEARCH_TOP, so a later-provisioned node can never be
    given our SRC.
    """
    ranges = valid_ranges(parsed.get("allocated_unicast_ranges"))
    taken = _known_node_addresses(parsed) | set(LEGACY_SRC_ADDRESSES)
    if not ranges or (
        not in_ranges(SRC_ADDRESS_BASE, ranges) and SRC_ADDRESS_BASE not in taken
    ):
        return SRC_ADDRESS_BASE
    for candidate in range(ROTATION_SEARCH_TOP, 0, -1):
        if candidate not in taken and not in_ranges(candidate, ranges):
            return candidate
    return SRC_ADDRESS_BASE
