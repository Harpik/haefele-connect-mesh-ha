"""
DataUpdateCoordinator for Häfele Connect Mesh.

Owns a single shared MeshProxyConnection (one GATT link into the mesh)
and exposes typed send helpers for entities. Nodes with no Proxy
feature are reached through whichever node currently holds the active
proxy role, via advertising bearer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, Callable

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    CONF_SEQ_STORE,
    DOMAIN,
    HEARTBEAT_INTERVAL,
    LEGACY_SRC_ADDRESSES,
    NODE_ADDRESS_MARGIN,
    ROTATED_SRC_SEQ_START,
    SEQ_MAX,
    SEQ_PERSIST_BLOCK,
    SEQ_ROTATE_THRESHOLD,
    SEQ_SEED_MIN,
    SEQ_STORE_PER_ENTRY,
    SRC_ADDRESS_BASE,
    UNICAST_MAX,
)
from .gatt import MeshProxyConnection, MeshSession

_LOGGER = logging.getLogger(__name__)

SEQ_STORAGE_VERSION = 1
SEQ_STORAGE_KEY = f"{DOMAIN}_seq"
SEQ_STARTUP_JUMP = 200
# Reserved keys inside the SEQ store (everything else is "<src>": seq).
STORE_KEY_IV_INDEX = "_iv_index"
STORE_KEY_ACTIVE_SRC = "_active_src"


class SeqExhaustedError(RuntimeError):
    """The active SRC ran out of SEQ space and no fresh SRC was available.

    Raised instead of wrapping to 0: a wrapped SEQ would be dropped as a
    replay by every lamp, silently.
    """

# How often we poll each node for its current state. External control
# (wall remotes, Häfele app) doesn't reliably publish Status to groups
# we subscribe to, so polling is the simplest way to keep HA in sync.
STATE_POLL_INTERVAL = 15  # seconds
# Small gap between the Gets we send to different nodes to avoid
# flooding the proxy link.
STATE_POLL_PER_NODE_GAP = 0.2


class HaefeleCoordinator(DataUpdateCoordinator):
    """Owns mesh session + single proxy connection for all nodes."""

    def __init__(
        self, hass: HomeAssistant, config: dict, entry_id: str | None = None,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=HEARTBEAT_INTERVAL),
        )
        self._config = config
        # {unicast_address -> list[callback(opcode, params)]}
        self._status_handlers: dict[int, list[Callable[[int, bytes], None]]] = {}
        # SEQ store. Keyed by SRC: the active one plus every SRC we have
        # retired (kept so we never reuse an address whose SEQ space we
        # already burnt).
        #
        # One file per config entry: a shared file let two meshes overwrite
        # each other's SEQ / active SRC (last writer wins).
        #
        # Entries created before per-entry storage (no CONF_SEQ_STORE marker)
        # migrate once from the shared file and keep mirroring to it, so a
        # downgrade to a release that only knows the shared file resumes
        # from a current SEQ instead of a stale one. Entries created with
        # the marker never touch the shared file, so a new network can't
        # inherit another network's SEQ state.
        self._legacy_seq_store: Store = Store(
            hass, SEQ_STORAGE_VERSION, SEQ_STORAGE_KEY,
        )
        self._uses_legacy_store = (
            config.get(CONF_SEQ_STORE) != SEQ_STORE_PER_ENTRY
        )
        if entry_id:
            self._seq_store: Store = Store(
                hass, SEQ_STORAGE_VERSION, f"{SEQ_STORAGE_KEY}_{entry_id}",
            )
        else:
            self._seq_store = self._legacy_seq_store
        # Last SEQ handed out, per SRC.
        self._seq_state: dict[int, int] = {}
        # Persisted ceiling per SRC (>= last SEQ handed out). This is what
        # goes to disk; see SEQ_PERSIST_BLOCK.
        self._seq_reserved: dict[int, int] = {}
        self._seq_lock = asyncio.Lock()
        # SRC we emit from. Persisted so an automatic rotation survives
        # restarts; falls back to the config entry value.
        self._persisted_active_src: int | None = None
        self._active_src: int | None = None
        self._rotation_exhausted_logged = False
        self._rotation_save_failed_logged = False
        # Fire-and-forget tasks we own; kept referenced (the event loop only
        # holds weak references) and cancelled on shutdown.
        self._background_tasks: set[asyncio.Task] = set()
        # Persisted IV Index from last session (auto-updated by Secure
        # Network Beacons). None until _load_seq runs.
        self._persisted_iv_index: int | None = None

        self.session: MeshSession | None = None
        self.proxy: MeshProxyConnection | None = None
        self._nodes_cfg: list[dict] = config.get("nodes", [])
        self._poll_task: asyncio.Task | None = None
        # Per-node availability (True means the mesh is reachable *and* we
        # have no reason to believe the node specifically is offline; we
        # don't currently track per-node liveness beyond "mesh is up").
        self.availability: dict[str, bool] = {
            _node_id(n): False for n in self._nodes_cfg
        }

    # ------------------------------------------------------------------
    # Status routing
    # ------------------------------------------------------------------

    def register_status_handler(
        self, src_address: int, callback: Callable[[int, bytes], None]
    ) -> Callable[[], None]:
        self._status_handlers.setdefault(src_address, []).append(callback)

        def _unsub() -> None:
            handlers = self._status_handlers.get(src_address, [])
            if callback in handlers:
                handlers.remove(callback)
            if not handlers:
                self._status_handlers.pop(src_address, None)

        return _unsub

    def _dispatch_status(self, src_address: int, opcode: int, params: bytes) -> None:
        for cb in self._status_handlers.get(src_address, ()):
            try:
                cb(opcode, params)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Status handler failed for %04X", src_address)

    # ------------------------------------------------------------------
    # SEQ management
    # ------------------------------------------------------------------

    async def _load_seq(self) -> None:
        raw = await self._seq_store.async_load()
        if (
            raw is None
            and self._uses_legacy_store
            and self._seq_store is not self._legacy_seq_store
        ):
            # First start with per-entry storage: migrate from the shared
            # file written by earlier releases.
            raw = await self._legacy_seq_store.async_load()
            if raw is not None:
                _LOGGER.info("Migrating SEQ state from the shared store")
        state: dict[int, int] = {}
        iv: int | None = None
        active: int | None = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k == STORE_KEY_IV_INDEX:
                    try:
                        iv = int(v)
                    except (TypeError, ValueError):
                        pass
                    continue
                if k == STORE_KEY_ACTIVE_SRC:
                    try:
                        candidate = int(v)
                    except (TypeError, ValueError):
                        continue
                    if 0 < candidate <= UNICAST_MAX:
                        active = candidate
                    continue
                try:
                    state[int(k)] = min(int(v), SEQ_MAX)
                except (TypeError, ValueError):
                    continue
        # Whatever is on disk is a ceiling (or, for stores written before
        # block persistence, the exact last SEQ) — either way it is >= the
        # last SEQ actually emitted, so it is safe to resume from.
        self._seq_state = state
        self._seq_reserved = dict(state)
        self._persisted_iv_index = iv
        self._persisted_active_src = active

    def _store_payload(
        self,
        reserved: dict[int, int] | None = None,
        active_src: int | None = None,
    ) -> dict[str, int]:
        """Build what goes to disk. Overrides let callers persist a state
        *before* committing it in memory (save-then-commit)."""
        if reserved is None:
            reserved = self._seq_reserved
        if active_src is None:
            active_src = self._active_src
        payload: dict[str, int] = {str(k): v for k, v in reserved.items()}
        if self.session is not None:
            payload[STORE_KEY_IV_INDEX] = int(self.session.iv_index)
        elif self._persisted_iv_index is not None:
            payload[STORE_KEY_IV_INDEX] = int(self._persisted_iv_index)
        if active_src is not None:
            payload[STORE_KEY_ACTIVE_SRC] = active_src
        return payload

    async def _write_store(self, payload: dict[str, int]) -> None:
        """Persist payload. Raises if the per-entry (authoritative) write fails."""
        await self._seq_store.async_save(payload)
        if self._uses_legacy_store and self._seq_store is not self._legacy_seq_store:
            # The mirror never carries the active SRC: older releases ignore
            # it, and a newly added entry migrating from the shared file must
            # not inherit another network's rotated SRC.
            mirror = {k: v for k, v in payload.items() if k != STORE_KEY_ACTIVE_SRC}
            try:
                await self._legacy_seq_store.async_save(mirror)
            except Exception:
                _LOGGER.debug("Legacy SEQ store mirror write failed", exc_info=True)

    async def _save_seq(self) -> None:
        await self._write_store(self._store_payload())

    async def next_seq(self, src_address: int) -> int:
        rotated_to: int | None = None
        async with self._seq_lock:
            current = self._seq_state.get(src_address)
            if current is None:
                current = max(SEQ_SEED_MIN, int(time.time()) & SEQ_MAX)
                _LOGGER.info(
                    "Seeding fresh SEQ for SRC 0x%04X at %d", src_address, current,
                )
            seq = current + 1
            if seq > SEQ_MAX:
                # Never wrap: a wrapped SEQ is a silent replay drop.
                raise SeqExhaustedError(
                    f"SEQ space for SRC 0x{src_address:04X} is exhausted and "
                    "no fresh SRC could be allocated"
                )
            if seq > self._seq_reserved.get(src_address, -1):
                # Save-then-commit: a SEQ is only handed out once a ceiling
                # >= it is on disk. If the write fails, nothing is committed
                # and the exception reaches the caller, so no frame goes out
                # with an unpersisted SEQ and the next call retries the save.
                new_reserved = dict(self._seq_reserved)
                new_reserved[src_address] = min(seq + SEQ_PERSIST_BLOCK, SEQ_MAX)
                await self._write_store(self._store_payload(reserved=new_reserved))
                self._seq_reserved = new_reserved
            self._seq_state[src_address] = seq
            if src_address == self._active_src:
                rotated_to = await self._maybe_rotate_src_locked()
        if rotated_to is not None:
            self._on_src_rotated(rotated_to)
        return seq

    # ------------------------------------------------------------------
    # SRC rotation
    # ------------------------------------------------------------------

    def _reserved_addresses(self) -> set[int]:
        """Addresses a rotation must never pick."""
        taken: set[int] = {0x0000}
        # Exact ranges of every provisioned node, lights *and* the remotes /
        # switches the parser skips. Only present in entries created or
        # reconfigured with a release that records them.
        for r in self._config.get("reserved_unicasts") or []:
            if not isinstance(r, dict):
                continue
            unicast = r.get("unicast")
            count = r.get("elements")
            if isinstance(unicast, int) and unicast > 0:
                n = count if isinstance(count, int) and count > 0 else 1
                taken.update(range(unicast, unicast + n))
        # Lights: conservative margin, since older entries carry neither
        # element counts nor the skipped nodes.
        for n in self._nodes_cfg:
            unicast = n.get("unicast")
            if isinstance(unicast, int) and unicast > 0:
                taken.update(range(unicast, unicast + NODE_ADDRESS_MARGIN))
        prov = self._config.get("provisioner_address")
        if isinstance(prov, int) and prov > 0:
            taken.add(prov)
        taken.update(LEGACY_SRC_ADDRESSES)
        # Every SRC we have ever emitted from (active + retired).
        taken.update(self._seq_state)
        taken.update(self._seq_reserved)
        if self._active_src is not None:
            taken.add(self._active_src)
        return taken

    def _pick_rotation_src(self) -> int | None:
        """Next free unicast address above the current SRC (wrapping once)."""
        taken = self._reserved_addresses()
        start = (self._active_src or SRC_ADDRESS_BASE) & 0xFFFF
        for offset in range(1, UNICAST_MAX + 1):
            candidate = ((start - 1 + offset) % UNICAST_MAX) + 1
            if candidate not in taken:
                return candidate
        return None

    async def _maybe_rotate_src_locked(self) -> int | None:
        """Switch to a fresh SRC if the active one is near SEQ exhaustion.

        Must be called with ``_seq_lock`` held. Returns the new SRC, or
        None if no rotation happened.
        """
        old = self._active_src
        if old is None or self._seq_state.get(old, 0) < SEQ_ROTATE_THRESHOLD:
            return None
        new = self._pick_rotation_src()
        if new is None:
            if not self._rotation_exhausted_logged:
                _LOGGER.error(
                    "SRC 0x%04X is close to SEQ exhaustion (%d) but no free "
                    "unicast address is left to rotate to. Commands will "
                    "stop working once SEQ reaches %d.",
                    old, self._seq_state.get(old, 0), SEQ_MAX,
                )
                self._rotation_exhausted_logged = True
            return None
        # Save-then-commit: persist the new SRC and its first block before
        # switching. If the write fails we keep emitting from the old SRC
        # (still far below SEQ_MAX) and retry on the next SEQ.
        new_reserved = dict(self._seq_reserved)
        new_reserved[new] = ROTATED_SRC_SEQ_START + SEQ_PERSIST_BLOCK
        try:
            await self._write_store(
                self._store_payload(reserved=new_reserved, active_src=new),
            )
        except Exception:
            if not self._rotation_save_failed_logged:
                _LOGGER.warning(
                    "Could not persist SRC rotation 0x%04X -> 0x%04X; staying "
                    "on 0x%04X and retrying", old, new, old, exc_info=True,
                )
                self._rotation_save_failed_logged = True
            return None
        self._rotation_save_failed_logged = False
        self._seq_reserved = new_reserved
        self._seq_state[new] = ROTATED_SRC_SEQ_START
        self._active_src = new
        if self.session is not None:
            self.session.src = new
        _LOGGER.warning(
            "SRC 0x%04X reached SEQ %d (limit %d); switched to fresh SRC "
            "0x%04X so the lamps keep accepting our frames",
            old, self._seq_state.get(old, 0), SEQ_MAX, new,
        )
        return new

    def _on_src_rotated(self, new_src: int) -> None:
        """Make sure Status replies addressed to the new SRC reach us."""
        if self.proxy is None:
            return
        self.proxy.set_filter_addresses(self._filter_addresses())
        if self.proxy.is_connected:
            # Fire-and-forget: add_filter_addresses consumes a SEQ itself,
            # so it must not run while the caller still holds _seq_lock.
            self._spawn_background(
                self._push_filter_address(new_src),
                "haefele-src-rotation-filter",
            )

    def _spawn_background(self, coro, name: str) -> asyncio.Task:
        """Start a task we keep a reference to and cancel on shutdown."""
        create = getattr(self.hass, "async_create_background_task", None)
        if callable(create):
            task = create(coro, name)
        else:  # bare test harness without a real hass
            task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _push_filter_address(self, address: int) -> None:
        if self.proxy is None:
            return
        try:
            await self.proxy.add_filter_addresses([address])
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not add rotated SRC 0x%04X to the proxy filter (%s); "
                "it will be added on the next reconnect", address, err,
            )

    # ------------------------------------------------------------------
    # Setup / teardown
    # ------------------------------------------------------------------

    async def async_setup(self) -> None:
        await self._load_seq()
        # Jump SEQ forward on startup for every known SRC. With block
        # persistence the stored value is already a ceiling; the jump is
        # kept for stores written by older releases (exact last SEQ).
        # Clamp instead of masking: wrapping would be a silent replay drop.
        for src, seq in list(self._seq_state.items()):
            jumped = min(seq + SEQ_STARTUP_JUMP, SEQ_MAX)
            self._seq_state[src] = jumped
            self._seq_reserved[src] = jumped

        net_key = self._config["network_key"]
        app_key = self._config["app_key"]
        # Prefer the IV Index we last saw on a Secure Network Beacon; fall
        # back to whatever the casa-2.connect import recorded. The beacon
        # parser will correct us live on the first beacon regardless, but
        # starting close to the truth keeps the first second of traffic
        # decryptable (including the Proxy Filter Status, if any firmware
        # ever starts honouring it).
        iv_index = self._persisted_iv_index
        if iv_index is None:
            iv_index = self._config.get("iv_index", 1)
        _LOGGER.info("Starting mesh session with IV Index = %d (source=%s)",
                     iv_index,
                     "persisted" if self._persisted_iv_index is not None else "config")

        # One SRC for the whole integration. SRC_ADDRESS_BASE is chosen to
        # be fresh vs the Haefele app (provisioner address, usually 0x7FFD)
        # and any earlier gateway implementation.
        #
        # config_flow persists this under "src_address_base"; older builds
        # mistakenly read "src_address" here, so the stored override never
        # took effect and the SRC was pinned to the constant. Accept both
        # keys (new name first) and fall back to the constant.
        #
        # An automatic SEQ-exhaustion rotation (persisted as _active_src)
        # takes precedence over the config value.
        src_address = (
            self._persisted_active_src
            or self._config.get("src_address_base")
            or self._config.get("src_address")
            or SRC_ADDRESS_BASE
        ) & 0xFFFF
        self._active_src = src_address

        self.session = MeshSession(
            net_key_hex=net_key,
            app_key_hex=app_key,
            src_address=src_address,
            iv_index=iv_index,
            seq_provider=self.next_seq,
        )
        # Rotate *before* the proxy exists if the stored SEQ is already past
        # the threshold (e.g. first start after upgrading), so the filter
        # list computed below already carries the fresh SRC.
        async with self._seq_lock:
            await self._maybe_rotate_src_locked()
        await self._save_seq()
        self.proxy = MeshProxyConnection(
            hass=self.hass,
            session=self.session,
            message_handler=self._dispatch_status,
            reconnect_callback=self._on_proxy_reconnect,
        )
        self.proxy.set_candidates([
            (n["mac"], n["name"]) for n in self._nodes_cfg if n.get("mac")
        ])
        self.proxy.set_filter_addresses(self._filter_addresses())

        ok = await self.proxy.connect_any()
        # Mark every node as available if *any* proxy is up — they're all
        # reachable through the mesh from there.
        for nid in self.availability:
            self.availability[nid] = ok

        # Start the state-polling loop.
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(
                self._state_poll_loop(), name="haefele-state-poll",
            )

    async def async_shutdown(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._poll_task = None
        for task in list(self._background_tasks):
            task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()
        if self.proxy is not None:
            try:
                await self.proxy.disconnect()
            except Exception:  # noqa: BLE001
                pass
        # Persist the latest IV Index (beacons may have moved it since the
        # last block write).
        try:
            await self._save_seq()
        except Exception:
            _LOGGER.debug("Final SEQ store save failed", exc_info=True)

    async def _on_proxy_reconnect(self) -> None:
        """Called by MeshProxyConnection after an auto-reconnect attempt.

        We refresh availability for every node off the proxy's current
        connection state so entities recover (or report unavailable)
        without waiting for the next 60 s heartbeat.
        """
        if self.proxy is None:
            return
        ok = self.proxy.is_connected
        for nid in self.availability:
            self.availability[nid] = ok
        # Push the new state to every listener (light entities) and also
        # schedule a regular refresh so the next heartbeat stays aligned.
        self.async_set_updated_data(
            {nid: {"available": ok} for nid in self.availability},
        )

    # ------------------------------------------------------------------
    # Heartbeat — just keep the single connection alive
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if self.proxy is None:
            return {nid: {"available": False} for nid in self.availability}

        ok = self.proxy.is_connected
        if not ok:
            _LOGGER.debug("Proxy disconnected, trying to reconnect...")
            ok = await self.proxy.connect_any()

        for nid in self.availability:
            self.availability[nid] = ok
        return {nid: {"available": ok} for nid in self.availability}

    def _filter_addresses(self) -> list[int]:
        """Compute the set of DST addresses the proxy should forward.

        Includes:
          * our own SRC (so unicast Status replies make it back)
          * every lamp unicast (replies overheard via relay)
          * every group configured on the lamps (so publications from
            the physical remote or the Häfele app come through)
        """
        addrs: set[int] = set()
        if self.session is not None:
            addrs.add(self.session.src & 0xFFFF)
        for n in self._nodes_cfg:
            unicast = n.get("unicast")
            if isinstance(unicast, int):
                addrs.add(unicast & 0xFFFF)
            for g in n.get("groups", []) or []:
                if isinstance(g, int):
                    addrs.add(g & 0xFFFF)
        return sorted(addrs)

    def is_available(self, node_id: str) -> bool:
        return self.availability.get(node_id, False)

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------

    async def _state_poll_loop(self) -> None:
        """Poll each node for its current on/off + CTL state.

        Keeps HA in sync with physical remote presses and Häfele-app
        changes — the lamps don't reliably publish status to groups
        that we subscribe to, so active polling is the most robust path.
        """
        # Small initial delay so initial_sync gets to run first.
        await asyncio.sleep(3.0)
        while True:
            try:
                await asyncio.sleep(STATE_POLL_INTERVAL)
                if self.proxy is None or not self.proxy.is_connected:
                    continue
                await self._poll_all_nodes()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _LOGGER.exception("State poll loop crashed, continuing")

    async def _poll_all_nodes(self) -> None:
        """Send exactly one state Get per node.

        Every Get costs one SEQ, so we only ask for the status the light
        entity actually consumes (see light._apply_status):

          * tunable_white -> CTL Get. CTL Status carries present lightness,
            from which is_on is derived, so an extra OnOff Get would be
            overwritten anyway.
          * everything else -> OnOff Get. CTL Status is ignored for these
            capability tiers, so the old CTL Get was pure SEQ burn.
        """
        for node_cfg in self._nodes_cfg:
            unicast = node_cfg.get("unicast")
            if not unicast or self.proxy is None:
                continue
            try:
                if _polls_ctl(node_cfg):
                    await self.proxy.get_ctl(unicast)
                else:
                    await self.proxy.get_onoff(unicast)
                await asyncio.sleep(STATE_POLL_PER_NODE_GAP)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug(
                    "State poll for %s failed: %s",
                    node_cfg.get("name", "?"), err,
                )


def _polls_ctl(node_cfg: dict) -> bool:
    """True for nodes whose entity runs in the colour-temperature tier.

    Mirrors light.resolve_capability ("tunable_white" -> color_temp);
    kept local because light.py imports this module.
    """
    return (node_cfg.get("device_type") or "").lower() == "tunable_white"


def _node_id(node_cfg: dict) -> str:
    mac = node_cfg["mac"].replace(":", "").lower()
    return f"haefele_{mac}"
