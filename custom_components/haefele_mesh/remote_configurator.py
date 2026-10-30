"""Apply remote position modes while the remote is awake.

A Häfele remote runs on batteries and only listens for a few seconds after
it was pressed. Changing a position's mode therefore only records it as
pending; the change is sent on the next press and kept pending, model by
model, until the remote confirms each one with a Publication Status.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .remote import remote_device_identifier
from .remote_config import (
    MODE_DISABLED,
    MODE_HA_ONLY,
    OP_MODEL_PUBLICATION_SET,
    available_modes,
    default_mode,
    encode_publication_set,
    parse_publication_status,
    publication_for,
)

_LOGGER = logging.getLogger(__name__)

# How long to wait for the Publication Status of one Set.
STATUS_TIMEOUT = 3.0
# Unanswered Sets in a row after which the remote is taken to be asleep
# again; the rest waits for the next press.
MAX_SILENT = 2
# Pause after each answered Set before sending the next one.
SETTLE_DELAY = 0.3

# Returns False when a segmented message never got through.
SendDevkey = Callable[[int, int, bytes], Awaitable[bool | None]]
Spawn = Callable[[Awaitable[None], str], Any]


def _position_key(remote_cfg: dict, module: int) -> str:
    return f"{remote_device_identifier(remote_cfg)[1]}:{module}"


def _discard(position: dict, element: int, model: int) -> None:
    if [element, model] in position["pending"]:
        position["pending"].remove([element, model])


class RemoteConfigurator:
    """Modes of every remote position, and the job that applies them."""

    def __init__(
        self,
        remotes_cfg: list[dict],
        store: Any,
        send_devkey: SendDevkey,
        spawn: Spawn,
    ) -> None:
        self._remotes = {r["unicast"]: r for r in remotes_cfg}
        self._store = store
        self._send = send_devkey
        self._spawn = spawn
        # {position key: {"mode": str, "pending": [[element, model], ...],
        #  "error": bool}}
        self._positions: dict[str, dict[str, Any]] = {}
        self._jobs: dict[int, Any] = {}
        # {(element, model): (future, publication address that was Set)}
        self._waiters: dict[tuple[int, int], tuple[asyncio.Future, int]] = {}
        self._listeners: list[Callable[[], None]] = []

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        positions = data.get("positions") if isinstance(data, dict) else None
        if isinstance(positions, dict):
            self._positions = {
                k: v for k, v in positions.items() if isinstance(v, dict)
            }

    async def _save(self) -> None:
        await self._store.async_save({"positions": self._positions})

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    def can_configure(self, remote_cfg: dict) -> bool:
        """Only with the remote's device key and the app's configuration of
        its controls, both stored by a Reconfigure since this feature."""
        return bool(remote_cfg.get("device_key")) and any(
            m.get("clients") for m in remote_cfg.get("modules") or []
        )

    def mode(self, remote_cfg: dict, module: int) -> str:
        stored = self._positions.get(_position_key(remote_cfg, module))
        modes = self.modes(remote_cfg, module)
        if stored and stored.get("mode") in modes:
            return stored["mode"]
        default = default_mode(self._module(remote_cfg, module))
        return default if default in modes else modes[-1]

    def modes(self, remote_cfg: dict, module: int) -> list[str]:
        return available_modes(self._module(remote_cfg, module))

    def is_pending(self, remote_cfg: dict, module: int) -> bool:
        stored = self._positions.get(_position_key(remote_cfg, module)) or {}
        return bool(stored.get("pending"))

    def has_error(self, remote_cfg: dict, module: int) -> bool:
        stored = self._positions.get(_position_key(remote_cfg, module)) or {}
        return bool(stored.get("error"))

    def is_ha_only(self, unicast: int, module: int) -> bool:
        """True once the remote confirmed the position as "HA only".

        While the change is pending (or was refused) the position may
        still switch lights, so their state must still be read.
        """
        remote_cfg = self._remotes.get(unicast)
        return (
            remote_cfg is not None
            and self.mode(remote_cfg, module) == MODE_HA_ONLY
            and not self.is_pending(remote_cfg, module)
            and not self.has_error(remote_cfg, module)
        )

    def uses_ha_only_address(self) -> bool:
        """Whether any position is (or is becoming) "HA only"."""
        return any(
            self.mode(remote_cfg, module) == MODE_HA_ONLY
            for remote_cfg in self._remotes.values()
            for module in range(len(remote_cfg.get("modules") or []))
        )

    def register_listener(self, callback: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(callback)

        def _unsub() -> None:
            if callback in self._listeners:
                self._listeners.remove(callback)

        return _unsub

    def _notify(self) -> None:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception:
                _LOGGER.exception("Remote configuration listener failed")

    @staticmethod
    def _module(remote_cfg: dict, module: int) -> dict:
        modules = remote_cfg.get("modules") or []
        return modules[module] if module < len(modules) else {}

    # ------------------------------------------------------------------
    # Changes
    # ------------------------------------------------------------------

    async def async_set_mode(self, remote_cfg: dict, module: int, mode: str) -> None:
        """Record ``mode`` for a position; it is sent on the next press."""
        if mode not in self.modes(remote_cfg, module):
            raise ValueError(f"mode {mode!r} not available for this position")
        if mode == MODE_DISABLED and not any(
            self.mode(remote_cfg, m) != MODE_DISABLED
            for m in range(len(remote_cfg.get("modules") or []))
            if m != module
        ):
            # Only a press on a position that sends something wakes the
            # remote up for us; with none left it could not be changed again.
            raise ValueError("at least one position must stay enabled")
        clients = self._module(remote_cfg, module).get("clients") or []
        self._positions[_position_key(remote_cfg, module)] = {
            "mode": mode,
            "pending": [[c["element"], c["model"]] for c in clients],
            "error": False,
        }
        await self._save()
        self._notify()

    def on_remote_activity(self, unicast: int) -> None:
        """The remote sent something, so it is awake: apply what is pending."""
        remote_cfg = self._remotes.get(unicast)
        if remote_cfg is None or not self.can_configure(remote_cfg):
            return
        if not any(
            self.is_pending(remote_cfg, m)
            for m in range(len(remote_cfg.get("modules") or []))
        ):
            return
        job = self._jobs.get(unicast)
        if job is not None and not job.done():
            return
        task = self._spawn(self._apply(remote_cfg), "haefele-remote-config")
        if task is not None:
            self._jobs[unicast] = task

    def on_publication_status(self, src: int, params: bytes) -> None:
        status = parse_publication_status(params)
        remote_cfg = self._remotes.get(src)
        if status is None or remote_cfg is None:
            return
        _LOGGER.debug(
            "Remote %04X publication status %d element %04X model %04X -> %04X",
            src, status["status"], status["element"], status["model"], status["address"],
        )
        waiter, expected = self._waiters.get(
            (status["element"], status["model"]), (None, None),
        )
        # A success for another address answers an earlier Set, not this one.
        if (
            waiter is not None and not waiter.done()
            and (status["status"] != 0 or status["address"] == expected)
        ):
            waiter.set_result(status)
            return
        # A late answer (its Set already timed out) still confirms the model.
        if self._confirm(remote_cfg, status):
            self._spawn(self._save_and_notify(), "haefele-remote-config-save")

    def _confirm(self, remote_cfg: dict, status: dict[str, int]) -> bool:
        """Mark the model of a successful status as done; True if it was pending."""
        if status["status"] != 0:
            return False
        item = [status["element"], status["model"]]
        for module in range(len(remote_cfg.get("modules") or [])):
            position = self._positions.get(_position_key(remote_cfg, module)) or {}
            if item not in (position.get("pending") or []):
                continue
            client = next(
                (c for c in self._module(remote_cfg, module).get("clients") or []
                 if [c["element"], c["model"]] == item),
                None,
            )
            if client is None or (
                publication_for(position["mode"], client)["address"] != status["address"]
            ):
                return False
            position["pending"].remove(item)
            return True
        return False

    async def _save_and_notify(self) -> None:
        await self._save()
        self._notify()

    async def _apply(self, remote_cfg: dict) -> None:
        # A mode changed while sending is picked up in another round.
        for _ in range(3):
            if not await self._apply_once(remote_cfg):
                return

    async def _apply_once(self, remote_cfg: dict) -> bool:
        """Send what is pending once; True if a mode changed meanwhile."""
        unicast = remote_cfg["unicast"]
        silent = 0
        replaced = False
        keys = [
            _position_key(remote_cfg, module)
            for module in range(len(remote_cfg.get("modules") or []))
        ]
        started = {key: self._positions.get(key) for key in keys}
        for module in range(len(remote_cfg.get("modules") or [])):
            key = _position_key(remote_cfg, module)
            position = self._positions.get(key)
            if not position or not position.get("pending"):
                continue
            clients = {
                (c["element"], c["model"]): c
                for c in self._module(remote_cfg, module).get("clients") or []
            }
            for element, model in list(position["pending"]):
                if self._positions.get(key) is not position:
                    replaced = True
                    break
                client = clients.get((element, model))
                if client is None:
                    _discard(position, element, model)
                    continue
                publication = publication_for(position["mode"], client)
                status = await self._set_publication(unicast, element, model, publication)
                if status is None:
                    silent += 1
                    if silent >= MAX_SILENT:
                        _LOGGER.debug(
                            "Remote %04X stopped answering; the rest waits for "
                            "the next press", unicast,
                        )
                        await self._save()
                        self._notify()
                        return False
                    continue
                silent = 0
                # Let our Segment Ack for that Status go out (and the remote
                # take it) before the next Set lands on top of it.
                await asyncio.sleep(SETTLE_DELAY)
                # A late or repeated Status may have confirmed it meanwhile.
                _discard(position, element, model)
                if status["status"] != 0 or status["address"] != publication["address"]:
                    _LOGGER.error(
                        "Remote %04X refused publication of model %04X on element "
                        "%04X (status %d)", unicast, model, element, status["status"],
                    )
                    position["error"] = True
                await self._save()
                self._notify()
        # Also a mode changed during the last Set of a position, or for one
        # already sent: the remote is still awake, so send it now.
        return replaced or any(
            self._positions.get(key) is not position
            and (self._positions.get(key) or {}).get("pending")
            for key, position in started.items()
        )

    async def _set_publication(
        self, unicast: int, element: int, model: int, publication: dict[str, int],
    ) -> dict[str, int] | None:
        loop = asyncio.get_running_loop()
        waiter: asyncio.Future = loop.create_future()
        self._waiters[(element, model)] = (waiter, publication["address"])
        try:
            delivered = await self._send(
                unicast, OP_MODEL_PUBLICATION_SET,
                encode_publication_set(element, model, publication),
            )
            if delivered is False and not waiter.done():
                return None
            return await asyncio.wait_for(waiter, STATUS_TIMEOUT)
        except TimeoutError:
            return None
        except Exception:
            _LOGGER.debug("Publication Set to %04X failed", unicast, exc_info=True)
            return None
        finally:
            self._waiters.pop((element, model), None)
