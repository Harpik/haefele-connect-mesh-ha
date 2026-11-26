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
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .addressing import valid_ranges
from .const import (
    CONF_SEQ_STORE,
    DOMAIN,
    HEARTBEAT_INTERVAL,
    LEGACY_SRC_ADDRESSES,
    LINK_DROP_TIMEOUT,
    LINK_FLAP_THRESHOLD,
    LINK_FLAP_WINDOW,
    LINK_PROBE_MAX_NODES,
    LINK_PROBE_TIMEOUT,
    LINK_RECONNECT_BACKOFF_MAX,
    LINK_REPLY_WINDOW,
    LINK_STALE_AFTER,
    NODE_ADDRESS_MARGIN,
    ROTATED_SRC_SEQ_START,
    ROTATION_SEARCH_TOP,
    SEQ_MAX,
    SEQ_PERSIST_BLOCK,
    SEQ_ROTATE_THRESHOLD,
    SEQ_SEED_MIN,
    SEQ_STORE_PER_ENTRY,
    SRC_ADDRESS_BASE,
    UNICAST_MAX,
)
from .gatt import MeshProxyConnection, MeshSession
from .remote import RemoteDecoder, RemoteEvent
from .remote_config import HA_ONLY_ADDRESS, OP_MODEL_PUBLICATION_STATUS
from .remote_configurator import RemoteConfigurator

_LOGGER = logging.getLogger(__name__)

SEQ_STORAGE_VERSION = 1
SEQ_STORAGE_KEY = f"{DOMAIN}_seq"
REMOTE_STORAGE_VERSION = 1
REMOTE_STORAGE_KEY = f"{DOMAIN}_remote_positions"
SEQ_STARTUP_JUMP = 200
# Reserved keys inside the SEQ store (everything else is "<src>": seq).
STORE_KEY_IV_INDEX = "_iv_index"
# 1 once the stored IV Index was confirmed by an authenticated beacon. The IV
# Index itself is saved from the first start, so its presence alone says
# nothing about where it came from.
STORE_KEY_IV_CONFIRMED = "_iv_confirmed"
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
# Delay between a remote press and the state read it triggers.
QUICK_POLL_DELAY = 0.8  # seconds
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
        # Set when shutdown starts; no new background work after that.
        self._closing = False
        # Persisted IV Index from last session (auto-updated by Secure
        # Network Beacons). None until _load_seq runs.
        self._persisted_iv_index: int | None = None
        self._persisted_iv_confirmed = False
        # Set once _load_seq has read the store. Until then the in-memory
        # state is empty, and saving it would wipe the stored SEQ counters.
        self._seq_loaded = False

        self.session: MeshSession | None = None
        self.proxy: MeshProxyConnection | None = None
        self._nodes_cfg: list[dict] = config.get("nodes", [])
        self._poll_task: asyncio.Task | None = None
        # Presses on Häfele remotes, routed to their event entities.
        self.remotes_cfg: list[dict] = config.get("remotes") or []
        self._remote_decoder = RemoteDecoder(self.remotes_cfg)
        # Modes of remote positions set from Home Assistant.
        self.remote_config = RemoteConfigurator(
            self.remotes_cfg,
            Store(
                hass, REMOTE_STORAGE_VERSION,
                f"{REMOTE_STORAGE_KEY}_{entry_id}" if entry_id else REMOTE_STORAGE_KEY,
            ),
            self._send_devkey,
            self._spawn_background,
        )
        self.remote_config.register_listener(self._on_remote_config_change)
        # Whether 0xFEFF is in the filter list we keep for the proxy.
        self._ha_only_filtered = False
        self._remote_listeners: list[Callable[[RemoteEvent], None]] = []
        self._quick_poll_task: asyncio.Task | None = None
        # Groups whose lights a quick poll still has to read (None = all).
        self._quick_poll_groups: set[int | None] = set()
        # One poll at a time on the shared proxy link, periodic or quick.
        self._poll_lock = asyncio.Lock()
        # Per-node availability (True means the mesh is reachable *and* we
        # have no reason to believe the node specifically is offline; we
        # don't currently track per-node liveness beyond "mesh is up").
        self.availability: dict[str, bool] = {
            _node_id(n): False for n in self._nodes_cfg
        }

        # Stuck-link watchdog (see _async_update_data). A link can stay
        # open while carrying nothing; only replies from the lights prove
        # it works both ways. A remote's frames are not counted: hearing
        # the mesh does not mean the mesh hears us.
        self._entry_id = entry_id
        self._light_unicasts: set[int] = {
            n["unicast"] & 0xFFFF for n in self._nodes_cfg
            if isinstance(n.get("unicast"), int)
        }
        # time.monotonic() of the last message from a light, and of the
        # moment the current link came up.
        self._last_light_rx: float | None = None
        self._link_up_at: float | None = None
        # link_generation of the link _link_up_at refers to.
        self._link_seen_generation = 0
        # Lights we sent a Get to (poll or probe) -> time.monotonic().
        self._awaiting_reply: dict[int, float] = {}
        # Set when a probe went unanswered; the lights stay unavailable
        # until one of them talks again.
        self._link_suspect = False
        # Lights a probe is waiting to hear from.
        self._probe_waiters: dict[int, asyncio.Future] = {}
        self._forced_reconnects: list[float] = []
        self._forced_reconnects_total = 0
        self._forced_backoff = 0.0
        self._next_forced_reconnect_at = 0.0
        self._last_forced_reconnect: dict[str, Any] | None = None
        self._flap_notified = False

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

    def register_remote_listener(
        self, callback: Callable[[RemoteEvent], None]
    ) -> Callable[[], None]:
        """Call ``callback`` with every de-duplicated remote press."""
        self._remote_listeners.append(callback)

        def _unsub() -> None:
            if callback in self._remote_listeners:
                self._remote_listeners.remove(callback)

        return _unsub

    def _dispatch_status(self, src_address: int, opcode: int, params: bytes) -> None:
        if src_address in self._light_unicasts:
            self._note_light_frame(src_address)
        for cb in self._status_handlers.get(src_address, ()):
            try:
                cb(opcode, params)
            except Exception:
                _LOGGER.exception("Status handler failed for %04X", src_address)
        if self._remote_decoder.owns(src_address):
            if opcode == OP_MODEL_PUBLICATION_STATUS:
                self.remote_config.on_publication_status(src_address, params)
                return
            self._dispatch_remote(src_address, opcode, params)

    def _dispatch_remote(self, src_address: int, opcode: int, params: bytes) -> None:
        event = self._remote_decoder.decode(src_address, opcode, params)
        if event is None:
            return
        # The remote is awake right after a press: send what is pending.
        if not self._closing:
            self.remote_config.on_remote_activity(event.remote_unicast)
        _LOGGER.debug("Remote %04X module %d: %s %s", event.remote_unicast,
                      event.module + 1, event.event_type, event.attributes)
        for cb in list(self._remote_listeners):
            try:
                cb(event)
            except Exception:
                _LOGGER.exception("Remote listener failed")
        # The press changed the lights it controls: read their state now
        # instead of waiting for the next periodic poll. A position set to
        # "Home Assistant only" changes no light.
        if not self.remote_config.is_ha_only(event.remote_unicast, event.module):
            self._schedule_quick_poll(event.group_address)

    async def _send_devkey(self, dst: int, opcode: int, params: bytes) -> bool:
        if self.proxy is None:
            raise ConnectionError("Mesh not set up")
        return await self.proxy.send_devkey(dst, opcode, params)

    def _on_remote_config_change(self) -> None:
        """Refresh the filter when 0xFEFF joins or leaves it.

        Called after every change of a remote position, so only act when
        whether any position is "HA only" actually changed. The proxy's
        list is rebuilt, not just added to: an accept list holds only a
        few addresses and is usually full already.
        """
        uses = self.remote_config.uses_ha_only_address()
        if uses == self._ha_only_filtered:
            return
        self._ha_only_filtered = uses
        if self.proxy is None:
            return
        self.proxy.set_filter_addresses(self._filter_addresses())
        if self.proxy.is_connected:
            self._spawn_background(
                self._refresh_proxy_filter(), "haefele-ha-only-filter",
            )

    async def _refresh_proxy_filter(self) -> None:
        if self.proxy is None:
            return
        try:
            await self.proxy.refresh_filter()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Could not update the proxy filter (%s); it will be set on "
                "the next reconnect", err,
            )

    def _schedule_quick_poll(self, group: int | None) -> None:
        """Read the lights of ``group`` soon (None: every light).

        Presses that arrive while a quick poll is running are queued and
        read in another pass, never dropped.
        """
        if self._closing:
            return
        self._quick_poll_groups.add(group)
        if self._quick_poll_task is not None and not self._quick_poll_task.done():
            return
        self._quick_poll_task = self._spawn_background(
            self._quick_poll(), "haefele-quick-poll",
        )

    async def _quick_poll(self) -> None:
        while self._quick_poll_groups:
            # Let the lights apply the command (and the remote's
            # retransmission arrive) before asking for their state.
            await asyncio.sleep(QUICK_POLL_DELAY)
            groups, self._quick_poll_groups = self._quick_poll_groups, set()
            # Like the periodic poll: no link, no Gets. Each one would run a
            # full reconnect while holding the poll lock; the periodic poll
            # catches up once the link is back.
            if self.proxy is None or not self.proxy.is_connected:
                continue
            await self._poll_all_nodes(None if None in groups else groups)

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
        iv_confirmed = False
        active: int | None = None
        if isinstance(raw, dict):
            for k, v in raw.items():
                if k == STORE_KEY_IV_CONFIRMED:
                    iv_confirmed = v is True or v == 1
                    continue
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
        # Stores from 0.4.6 and earlier have no flag: their IV may have come
        # from the export or an unauthenticated beacon, so treat it as
        # unconfirmed.
        self._persisted_iv_confirmed = iv is not None and iv_confirmed
        self._persisted_active_src = active
        self._seq_loaded = True

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
            iv_confirmed = bool(getattr(self.session, "iv_confirmed", False))
        elif self._persisted_iv_index is not None:
            payload[STORE_KEY_IV_INDEX] = int(self._persisted_iv_index)
            iv_confirmed = self._persisted_iv_confirmed
        else:
            iv_confirmed = False
        if iv_confirmed:
            payload[STORE_KEY_IV_CONFIRMED] = 1
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

    def _allocated_ranges(self) -> list[tuple[int, int]]:
        """Unicast ranges provisioners will hand out to future nodes."""
        return valid_ranges(self._config.get("allocated_unicast_ranges"))

    def _pick_rotation_src(self) -> int | None:
        """Pick a fresh SRC for rotation.

        When the entry knows the provisioners' allocated unicast ranges
        (imported or reconfigured from a release that records them), prefer
        an address *outside* all of them, searching down from
        ROTATION_SEARCH_TOP: the provisioning app will never assign those to
        a new node. Otherwise, or if every such address is taken, fall back
        to the next free address above the current SRC.
        """
        taken = self._reserved_addresses()
        ranges = self._allocated_ranges()
        if ranges:
            for candidate in range(ROTATION_SEARCH_TOP, 0, -1):
                if candidate in taken:
                    continue
                if any(lo <= candidate <= hi for lo, hi in ranges):
                    continue
                return candidate
        start = (self._active_src or SRC_ADDRESS_BASE) & 0xFFFF
        for offset in range(1, UNICAST_MAX + 1):
            candidate = ((start - 1 + offset) % UNICAST_MAX) + 1
            if candidate not in taken:
                return candidate
        return None

    def _active_src_collides_with_node(self) -> bool:
        """True if our active SRC is a known node/element/provisioner address.

        Two emitters sharing a SRC confuse every lamp's replay cache, and a
        node discards frames whose SRC is one of its own element addresses.
        An entry created before the provisioner ranges were known can end up
        here once the app hands our SRC to a new node and a Reconfigure
        records it.
        """
        src = self._active_src
        if src is None:
            return False
        for r in self._config.get("reserved_unicasts") or []:
            if not isinstance(r, dict):
                continue
            unicast, count = r.get("unicast"), r.get("elements")
            if isinstance(unicast, int) and unicast > 0:
                n = count if isinstance(count, int) and count > 0 else 1
                if unicast <= src < unicast + n:
                    return True
        if any(n.get("unicast") == src for n in self._nodes_cfg if isinstance(n, dict)):
            return True
        return self._config.get("provisioner_address") == src

    async def _maybe_rotate_src_locked(self, force: bool = False) -> int | None:
        """Switch to a fresh SRC if the active one is near SEQ exhaustion.

        ``force`` rotates regardless of SEQ (used when the active SRC turned
        out to collide with a node). Must be called with ``_seq_lock`` held.
        Returns the new SRC, or None if no rotation happened.
        """
        old = self._active_src
        if old is None:
            return None
        if not force and self._seq_state.get(old, 0) < SEQ_ROTATE_THRESHOLD:
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

    def _spawn_background(self, coro, name: str) -> asyncio.Task | None:
        """Start a task we keep a reference to and cancel on shutdown.

        Returns None once shutdown has started: a task created then could
        outlive it and reconnect the proxy.
        """
        if self._closing:
            coro.close()
            return None
        create = getattr(self.hass, "async_create_background_task", None)
        if callable(create):
            task = create(coro, name)
        else:  # bare test harness without a real hass
            task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

    async def _cancel_background_tasks(self) -> None:
        while self._background_tasks:
            tasks = list(self._background_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._background_tasks.difference_update(tasks)

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
        await self.remote_config.async_load()
        self._ha_only_filtered = self.remote_config.uses_ha_only_address()
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
        # Only an IV Index that an authenticated beacon confirmed counts as
        # known; one from the export can still be corrected by a big jump.
        self.session.iv_confirmed = self._persisted_iv_confirmed
        for remote in self.remotes_cfg:
            if remote.get("device_key"):
                self.session.device_keys[remote["unicast"]] = bytes.fromhex(
                    remote["device_key"]
                )
        # Rotate *before* the proxy exists if the stored SEQ is already past
        # the threshold (e.g. first start after upgrading), so the filter
        # list computed below already carries the fresh SRC.
        collides = self._active_src_collides_with_node()
        if collides:
            _LOGGER.error(
                "SRC 0x%04X is used by a node in this network (per the last "
                ".connect import); switching to a fresh SRC",
                self._active_src,
            )
        async with self._seq_lock:
            await self._maybe_rotate_src_locked(force=collides)
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
        self._track_link()
        # Mark every node as available if *any* proxy is up — they're all
        # reachable through the mesh from there.
        self._set_availability(ok)

        # Start the state-polling loop.
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(
                self._state_poll_loop(), name="haefele-state-poll",
            )

    async def async_shutdown(self) -> None:
        # Frames keep arriving until proxy.disconnect() stops the rx pump; a
        # remote press in that window must not start new work.
        self._closing = True
        if self._poll_task is not None and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._poll_task = None
        await self._cancel_background_tasks()
        if self.proxy is not None:
            try:
                await self.proxy.disconnect()
            except Exception:  # noqa: BLE001
                pass
        # Persist the latest IV Index (beacons may have moved it since the
        # last block write). Skipped if setup failed before the store was
        # read: the empty in-memory state would overwrite the stored SEQ
        # counters and the nodes would drop every frame as a replay.
        if not self._seq_loaded:
            return
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
        # A link the watchdog found silent stays unavailable until a light
        # answers, even if a real drop and auto-reconnect happened since.
        self._track_link()
        ok = self.proxy.is_connected and not self._link_suspect
        self._set_availability(ok)
        # Push the new state to every listener (light entities) and also
        # schedule a regular refresh so the next heartbeat stays aligned.
        self.async_set_updated_data(
            {nid: {"available": ok} for nid in self.availability},
        )

    # ------------------------------------------------------------------
    # Heartbeat — keep the single connection alive *and working*
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        if self.proxy is None:
            return {nid: {"available": False} for nid in self.availability}

        ok = self.proxy.is_connected
        if not ok:
            _LOGGER.debug("Proxy disconnected, trying to reconnect...")
            ok = await self.proxy.connect_any()
            if self._closing:
                return {nid: {"available": False} for nid in self.availability}
            self._track_link()
            # Still unproven if the lights were unavailable: the next
            # heartbeat probes the new link.
            ok = ok and not self._link_suspect
        elif self._link_suspect or self._link_idle_for() >= LINK_STALE_AFTER:
            # Open is not the same as working: on 26-11 a link stayed open
            # for hours while no frame went through it.
            ok = await self._check_link()

        self._set_availability(ok)
        return {nid: {"available": ok} for nid in self.availability}

    # ------------------------------------------------------------------
    # Stuck-link watchdog
    # ------------------------------------------------------------------

    def _set_availability(self, ok: bool) -> None:
        for nid in self.availability:
            self.availability[nid] = ok

    def _expect_reply(self, dst: int) -> None:
        """Remember that we just asked light ``dst`` for its state."""
        self._awaiting_reply[dst & 0xFFFF] = time.monotonic()

    def _note_light_frame(self, src_address: int) -> None:
        """A light spoke. Only a reply to our own Get proves the link.

        A light also publishes its state on its own (wall switch, Häfele
        app): that proves we hear the mesh, not that the mesh hears us.
        """
        sent = self._awaiting_reply.pop(src_address, None)
        if sent is None or time.monotonic() - sent > LINK_REPLY_WINDOW:
            return
        self._note_light_reply(src_address)

    def _note_light_reply(self, src_address: int) -> None:
        self._last_light_rx = time.monotonic()
        # The link works: a later stuck link starts from a clean backoff.
        self._forced_backoff = 0.0
        self._next_forced_reconnect_at = 0.0
        waiter = self._probe_waiters.get(src_address)
        if waiter is not None and not waiter.done():
            waiter.set_result(True)
        if self._link_suspect:
            # A light answered: the link works again, no need to wait for
            # the next heartbeat to bring the entities back.
            self._link_suspect = False
            _LOGGER.info(
                "Mesh link answering again (light %04X); lights available",
                src_address,
            )
            if self.proxy is not None and self.proxy.is_connected:
                self._set_availability(True)
                self.async_set_updated_data(
                    {nid: {"available": True} for nid in self.availability},
                )

    def _link_idle_for(self) -> float:
        """Seconds since the link last proved it works (or came up).

        Every new link is noticed by its generation, whoever opened it
        (setup, heartbeat, auto-reconnect, a light command), and gets the
        full grace period instead of inheriting the old link's silence.
        """
        if self._track_link():
            return 0.0
        ref = max(self._last_light_rx or 0.0, self._link_up_at or 0.0)
        return time.monotonic() - ref

    def _track_link(self) -> bool:
        """Note when a new link shows up; True if this one is new."""
        if self.proxy is None:
            return False
        generation = self.proxy.link_generation
        if generation == self._link_seen_generation:
            return False
        self._link_seen_generation = generation
        self._link_up_at = time.monotonic()
        return True

    async def _probe_link(self) -> bool:
        """Ask a few lights for their on/off state; True if one replies.

        Only a reply from the light we asked counts: it proves our frame
        went out through the proxy and the answer came back.

        Only the current link is probed. If it goes away mid-probe the
        probe stops instead of letting a Get reconnect: the probe timeout
        would cancel that connect half-way.
        """
        if self.proxy is None or not self.proxy.is_connected:
            return False
        generation = self.proxy.link_generation
        targets = [
            n["unicast"] & 0xFFFF for n in self._nodes_cfg
            if isinstance(n.get("unicast"), int)
        ][:LINK_PROBE_MAX_NODES]
        if not targets:
            return True  # nothing to ask, nothing to judge the link by
        loop = asyncio.get_running_loop()
        for dst in targets:
            if (
                not self.proxy.is_connected
                or self.proxy.link_generation != generation
            ):
                return False
            waiter = loop.create_future()
            self._probe_waiters[dst] = waiter
            self._expect_reply(dst)
            try:
                await asyncio.wait_for(
                    self.proxy.get_onoff(dst), LINK_PROBE_TIMEOUT,
                )
                await asyncio.wait_for(waiter, LINK_PROBE_TIMEOUT)
            except (TimeoutError, ConnectionError) as err:
                _LOGGER.debug(
                    "Link probe: no reply from %04X (%s)", dst,
                    type(err).__name__,
                )
                continue
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Link probe to %04X failed: %s", dst, err)
                continue
            finally:
                if self._probe_waiters.get(dst) is waiter:
                    del self._probe_waiters[dst]
            return True
        return False

    async def _check_link(self) -> bool:
        """Probe an idle link; drop it and reconnect if nothing answers.

        Returns whether the lights are reachable now.
        """
        idle = self._link_idle_for()
        # The link we are judging. Another one may replace it while we wait
        # (auto-reconnect after a real drop); that one must not be dropped.
        generation = self.proxy.link_generation
        if await self._probe_link():
            # The reply went through _note_light_reply, which cleared the
            # suspect flag and the backoff.
            return True
        if self._closing:
            return False
        if self.proxy.link_generation != generation:
            # The link went away during the probe and another one is up
            # (auto-reconnect): nothing to drop. The new link gets its
            # grace period; if the lights were already unavailable, the
            # next heartbeat probes it.
            return self.proxy.is_connected and not self._link_suspect

        self._link_suspect = True
        # Entities go unavailable now, not after the reconnect below (which
        # can take a while with several candidates).
        self._set_availability(False)
        self.async_set_updated_data(
            {nid: {"available": False} for nid in self.availability},
        )
        now = time.monotonic()
        if now < self._next_forced_reconnect_at:
            _LOGGER.debug(
                "Mesh link still silent; next forced reconnect in %.0f s",
                self._next_forced_reconnect_at - now,
            )
            return False

        name = self.proxy.active_name
        mac = self.proxy.active_mac
        _LOGGER.warning(
            "Mesh proxy link via %s (%s) is open but silent: no reply from "
            "any light for %.0f s, nor to a state probe. Dropping it and "
            "reconnecting", name or "?", mac or "?", idle,
        )
        self._record_forced_reconnect(now, name, mac)
        await self.proxy.drop_link(
            timeout=LINK_DROP_TIMEOUT, expected_generation=generation,
        )
        if self._closing:
            return False
        ok = await self.proxy.connect_any(avoid=mac)
        if self._closing:
            # Unloaded while connecting: leave no link behind.
            await self.proxy.drop_link(timeout=LINK_DROP_TIMEOUT)
            return False
        self._track_link()
        if ok:
            ok = await self._probe_link()
        if ok:
            _LOGGER.warning(
                "Mesh proxy link restored via %s (%s)",
                self.proxy.active_name, self.proxy.active_mac,
            )
        else:
            self._forced_backoff = min(
                max(self._forced_backoff * 2, HEARTBEAT_INTERVAL),
                LINK_RECONNECT_BACKOFF_MAX,
            )
            self._next_forced_reconnect_at = (
                time.monotonic() + self._forced_backoff - 1
            )
            _LOGGER.warning(
                "Mesh proxy reconnect %s, but no light answers yet; lights "
                "stay unavailable (next forced reconnect in %.0f s)",
                f"via {self.proxy.active_name}" if self.proxy.is_connected
                else "failed",
                self._forced_backoff,
            )
        return ok

    def _record_forced_reconnect(
        self, now: float, name: str, mac: str | None,
    ) -> None:
        self._forced_reconnects_total += 1
        self._forced_reconnects = [
            t for t in self._forced_reconnects if now - t < LINK_FLAP_WINDOW
        ] + [now]
        self._last_forced_reconnect = {
            "at": time.time(), "from_name": name, "from_mac": mac,
        }
        recent = len(self._forced_reconnects)
        if recent < LINK_FLAP_THRESHOLD:
            self._flap_notified = False
            return
        if self._flap_notified:
            return
        self._flap_notified = True
        self._notify(
            "Häfele Connect Mesh: unstable proxy link",
            f"The Bluetooth link to the mesh got stuck {recent} times in the "
            f"last {LINK_FLAP_WINDOW // 60} minutes and had to be "
            f"reconnected (last via {name or '?'}). Check the Bluetooth "
            "adapter or proxy placement; download the integration "
            "diagnostics for details.",
        )

    def _notify(self, title: str, message: str) -> None:
        try:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass, message, title=title,
                notification_id=f"{DOMAIN}_{self._entry_id or 'entry'}_link",
            )
        except Exception:
            _LOGGER.debug("Could not create the notification", exc_info=True)

    def link_health(self) -> dict[str, Any]:
        """Watchdog state, for diagnostics."""
        now = time.monotonic()
        return {
            "seconds_since_light_reply": (
                None if self._last_light_rx is None
                else round(now - self._last_light_rx, 1)
            ),
            "seconds_since_link_up": (
                None if self._link_up_at is None
                else round(now - self._link_up_at, 1)
            ),
            "suspect": self._link_suspect,
            "forced_reconnects_total": self._forced_reconnects_total,
            "forced_reconnects_recent": len([
                t for t in self._forced_reconnects if now - t < LINK_FLAP_WINDOW
            ]),
            "last_forced_reconnect": self._last_forced_reconnect,
            "backoff_seconds": self._forced_backoff,
        }

    def _filter_addresses(self) -> list[int]:
        """Compute the set of DST addresses the proxy should forward.

        Includes:
          * our own SRC (so unicast Status replies make it back)
          * every lamp unicast (replies overheard via relay)
          * every group configured on the lamps (so publications from
            the physical remote or the Häfele app come through)

        Ordered by priority, because a proxy may keep only the first few
        when it falls back to an accept list: our SRC, then groups, then
        lamp unicasts (replies to our SRC already reach us).
        """
        groups: set[int] = set()
        unicasts: set[int] = set()
        for n in self._nodes_cfg:
            unicast = n.get("unicast")
            if isinstance(unicast, int):
                unicasts.add(unicast & 0xFFFF)
            for g in n.get("groups", []) or []:
                if isinstance(g, int):
                    groups.add(g & 0xFFFF)
        ordered: list[int] = []
        if self.session is not None:
            ordered.append(self.session.src & 0xFFFF)
        # Presses of positions set to "Home Assistant only". Only when one
        # is: a proxy may keep just 4 addresses in its accept list.
        if self.remote_config.uses_ha_only_address():
            ordered.append(HA_ONLY_ADDRESS)
        for addr in [*sorted(groups), *sorted(unicasts)]:
            if addr not in ordered:
                ordered.append(addr)
        return ordered

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
            except Exception:
                _LOGGER.exception("State poll loop crashed, continuing")

    async def _poll_all_nodes(self, groups: set[int] | None = None) -> None:
        """Poll every light, or only those subscribed to one of ``groups``."""
        async with self._poll_lock:
            await self._poll_nodes_locked(groups)

    async def _poll_nodes_locked(self, groups: set[int] | None) -> None:
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
            if groups is not None and not groups & set(node_cfg.get("groups") or []):
                continue
            try:
                self._expect_reply(unicast)
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
