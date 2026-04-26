# Changelog

## 0.3.0 — 2026-04-26

A complete rewrite of the BLE transport after a deep debug session on real
Häfele hardware. The integration now talks to the mesh through **one
shared GATT Proxy connection** and understands Häfele's firmware quirks.

### Highlights
- Fully controllable from Home Assistant (on/off, brightness, color
  temperature) even on nodes that don't have the Proxy feature enabled.
- External-change feedback: physical wall remote and the Häfele app both
  reflect in HA within ≈15 s via active polling.
- Bulk off/on automations (presence triggers, etc.) now target multiple
  lamps reliably, without the "only one lamp responds" race.

### Added
- **Single shared `MeshProxyConnection`** (one GATT link for the whole
  mesh). Candidates are tried in order and the first functional proxy
  is kept; successful candidates move to the head of the list for the
  next startup.
- **Functional-proxy health check**: wait for a Secure Network Beacon
  within 5 s of GATT connect. Nodes that advertise the Mesh Proxy
  service but don't actually route (`Cocina fuegos` style) are skipped.
- **Live IV Index auto-sync** from Secure Network Beacons (k3-authenticated
  against our own Network ID). Survives network-wide IV Updates without
  touching the config.
- **Persisted IV Index** in the SEQ store, so subsequent restarts begin
  with the correct IV instead of the stale value in `casa-2.connect`.
- **Explicit accept-list proxy filter** with our SRC, every lamp unicast,
  and every subscribed group — required because Häfele firmware ignores
  `Set Filter Type` silently.
- **State polling** (every 15 s) as a robust fallback for external
  state changes when lamps' publish configuration is unpredictable.
- **Outbound write serialisation** (`_send_lock`) so concurrent
  `turn_off` / `turn_on` calls can't interleave SAR chunks on the
  characteristic.
- **Richer debug logging** at every RX early-return (NID mismatch,
  Net MIC failure, AID mismatch, non-access PDUs) and on every
  outbound operation.

### Changed
- `turn_on` now sends a single **Light Lightness Set Unack (0x824D)**
  instead of the old OnOff + Level two-step. Lightness > 0 implies
  OnOff=1 on the subscribed server, so this replaces both commands
  atomically.
- Default brightness on `turn_on` without `ATTR_BRIGHTNESS` is now 255.
  Previously the integration started at 128 which the lamp translated
  to ~0.4 % visible brightness.
- `HaefeleCoordinator` owns `MeshSession` + `MeshProxyConnection`
  directly. The old `MeshGattNode` class (one BLE connection per lamp)
  is gone.
- Availability is now mesh-wide (true if any proxy is reachable)
  instead of per-lamp ping.

### Fixed
- `set_ctl` used CTL Status opcode `0x8260` instead of CTL Set Unack
  `0x825F`, so color-temperature changes were silently dropped.
- `OnOff Set` was being addressed to groups, but Häfele Generic OnOff
  Server (model `0x1000`) is **not subscribed to any group**. OnOff is
  now always sent to unicast.
- Preferred group selection skipped shared groups (`0xC002`, `0xC003`,
  `0xC006`, `0xC007`, `0xC017`, `0xC018`) to avoid cross-lamp triggers.
- SEQ seeding uses `max(time.time() & 0xFFFFFF, 0x800000)` to dodge
  anti-replay caches left by the old Raspberry Pi 3 gateway script.
- Race between two near-simultaneous sends that caused only one of
  two "turn off both lamps" commands to land.

### Known quirks (not bugs on our side)
- Häfele firmware does not send `Filter Status` in response to
  `Set Filter Type`, contrary to Mesh Profile 1.0 §6.5.3. We no longer
  rely on that ACK.
- Some provisioned nodes (`Cocina fuegos` in Jose's network) advertise
  the Mesh Proxy service but are not functional proxies. They are
  detected and skipped automatically.

## 0.2.0

Initial public drop.
