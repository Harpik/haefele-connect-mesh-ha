# Changelog

All notable changes to this integration are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **SEQ exhaustion no longer locks the integration out.** The 24-bit BT Mesh
  sequence counter used to wrap to 0 with `& 0xFFFFFF`; every lamp then
  treats our frames as replays and drops them silently (the same symptom as
  the 0.4.2 lockout). With the 15 s state polling a small network burns
  about 11.5k SEQ per light per day, so a counter seeded at `0x800000` ran
  out in well under a year. The coordinator now switches to a fresh,
  never-used source address once the active one crosses `0xF00000`,
  persists that choice, and adds the new address to the proxy filter on
  the live link. Candidates skip every node (with a 16-address margin per
  node), the provisioner, legacy SRCs and any SRC used before. The
  `.connect` import now also records the unicast range of every
  provisioned node, including the remotes and switches that get no entity,
  and rotation avoids those too; existing entries pick this up on the next
  Reconfigure. If no
  address is free it logs a single error and refuses to emit rather than
  wrapping.
- PDUs are built with the SRC captured before the SEQ is allocated, so a
  rotation can never produce a frame mixing SRC and SEQ from different
  address spaces.

### Changed

- **Half the SEQ burn from polling.** Each poll cycle now sends one Get per
  light instead of two: CTL Get for tunable-white lights (CTL Status already
  carries lightness, from which on/off is derived) and OnOff Get for the
  rest (their entities ignore CTL Status, so that Get was wasted). Resulting
  HA state is unchanged.
- **SEQ is persisted in blocks of 256** instead of on every frame, cutting
  `.storage` writes from tens of thousands per day to a few hundred (matters
  on SD-card installs). The stored value is a ceiling, always ahead of the
  last SEQ sent, so an unclean shutdown still never rewinds the counter. The
  IV Index is also saved on shutdown. Stores written by older releases are
  read as before.
- Dropped `cryptography` from the manifest requirements. It ships with Home
  Assistant core, and hassfest now rejects custom integrations that list it.

## [0.4.4] — 2026-08-26

### Changed

- **Scanner-only Bluetooth gateways are now called out explicitly instead of
  failing as "no proxy visible"**
  ([#7](https://github.com/Harpik/haefele-connect-mesh-ha/issues/7)). Shelly
  BLE gateways, BTHome-style bridges and ESPHome proxies without
  `active: true` forward advertisements to Home Assistant but never open
  outbound GATT connections, so they can't carry the mesh proxy link. The
  connection path now checks whether HA has *any* connectable scanner before
  attempting discovery and, when it doesn't, logs a single explicit error
  naming that as the cause — the previous generic warning read like a range
  problem and sent users looking for a Häfele Proxy-feature setting that
  wasn't involved. The requirements and troubleshooting sections of the
  README now state the limitation up front.

## [0.4.3] — 2026-07-18

### Fixed

- **Light capabilities are now detected from the Bluetooth Mesh SIG server
  models a node advertises, not only from `tos_node.type`**
  ([#6](https://github.com/Harpik/haefele-connect-mesh-ha/pull/6)). The
  Häfele product-type string (e.g. `com.haefele.meshbox.esp.mw.1c`) can be
  too generic to describe what a node actually implements, so tunable-white
  distributors were mis-detected (the type exported without `tw`/`tunable`
  even though the node advertises Light CTL models). The `.connect` parser
  now resolves the capability from the advertised SIG models — Light HSL →
  `rgb`, Light CTL / CTL Temperature → `tunable_white`, Light Lightness →
  `dimmable`, Generic OnOff → `onoff` — and keeps the `tos_node.type` string
  match as a backward-compatible fallback for older exports.
  Remote/sensor/switch nodes are still filtered by an early guard before
  model detection, so they never produce spurious light entities.

## [0.4.2] — 2026-06-22

### Fixed

- **Commands silently dropped after a SEQ store reset (replay-cache
  lockout).** The mesh SRC override persisted by the config flow under
  `src_address_base` was read from the wrong key in the coordinator, so
  the SRC was always pinned to the shipped constant (`0x00C0`). When the
  persisted SEQ store (`.storage/haefele_mesh_seq`) was deleted, the SEQ
  counter for that SRC rewound *below* the replay-protection watermark
  the lamps had already cached, so every Set/Get we emitted looked like a
  replay and was discarded by the nodes (no Status replies, no
  actuation) while RX/decryption kept working perfectly. Fixed by:
  - bumping `SRC_ADDRESS_BASE` to a fresh `0x00C8` (empty replay list on
    every lamp -> accepted from the first frame);
  - reading the override from `src_address_base` (with a legacy
    `src_address` fallback) so the value is honoured for real;
  - a v1->v2 config-entry migration that rewrites any legacy/default SRC
    (`0x0060`/`0x0080`/`0x00C0`) to the current `SRC_ADDRESS_BASE`,
    leaving a genuinely custom SRC untouched.

## [0.4.1] — 2026-05-26

### Fixed

- **Proxy connection no longer depends on stored MAC addresses**
  ([#1](https://github.com/Harpik/haefele-connect-mesh-ha/issues/1)).
  The integration now locates the mesh proxy by scanning live BLE
  advertisements for the Mesh Proxy service UUID (`0x1828`) with a
  Service Data payload of `0x00 || NetworkID(8 bytes)` matching
  `k3(NetKey)` for your session — the standard BT Mesh discovery
  pattern. Stored candidate MACs (when valid) are still used as an
  ordering hint to keep reconnect stickiness, but stale or
  non-MAC-shaped identifiers in the `.connect` file no longer block
  recovery: any proxy-capable node on your network that's currently
  in BLE range is now reachable.

### Tests

- `tests/test_proxy_candidates.py` rewritten against the new
  discovery internals (`_discover_proxy_candidates` +
  `_try_connect_device`); adds coverage for the empty-discovery
  branch and for the "stored MAC stale, different proxy advertising"
  recovery case.
- `tests/test_proxy_discovery_recovery.py` adds an end-to-end-style
  test that drives the *real* `_discover_proxy_candidates` against a
  mocked HA bluetooth layer and asserts the MAC-stale → Network-ID
  recovery path: stale stored MAC is never probed, the advertising
  node is connected through, and the
  `Discovered N proxy candidate(s) on our network (0 matched stored
  MACs)` debug line is emitted.

### Docs

- README architecture and troubleshooting sections updated to
  describe Network-ID-based discovery and the two distinct
  log-message cases for unreachable mesh.

## [0.4.0] — 2026-04-27

Big quality-of-life release: more device types, recover automatically
from BLE drops, re-import your `.connect` without losing mesh state,
and a downloadable diagnostics bundle for bug reports.

### Added

- **Capability tiers — support for more Häfele models.** The parser
  now detects four light tiers from the `.connect` export and the
  light entity picks the matching BT Mesh opcodes automatically:
    - `tunable_white` → on/off + brightness + color temp (CTL) _[verified]_
    - `dimmable` → on/off + brightness (Light Lightness) _[plausible]_
    - `rgb` → on/off + brightness + hue/saturation via
      Light HSL Set Unack `0x8277` _[experimental, standard spec
      opcode; vendor-opcode-only fixtures not yet supported]_
    - `onoff` → on/off only (Generic OnOff) _[plausible]_
- **Reconfigure flow.** Integration three-dot menu → _Reconfigure_
  lets you re-import an updated `.connect` file after adding /
  renaming / removing lights in the Häfele app. Preserves the
  persisted BT Mesh SEQ counters and the live IV Index so the mesh
  keeps accepting our frames; shows an added / removed / kept diff
  before applying. Rejects a `.connect` with a different NetKey.
- **Downloadable diagnostics.** Settings → Devices & Services →
  Häfele Connect Mesh → ⋮ → Download diagnostics. Ships the
  coordinator state, per-node BLE visibility (RSSI, service
  UUIDs, last-seen, mesh proxy / mesh provisioning flags) and the
  full GATT service tree of the active proxy. All keys redacted
  via triple-layer scrubbing; safe to attach to a bug report.
- **Immediate auto-reconnect on unsolicited BLE disconnect.** The
  proxy relinks within ~3 s of a bluez drop instead of waiting up
  to 60 s for the next heartbeat. The heartbeat remains as the
  long-term safety net.
- **Issue / PR templates + `CONTRIBUTING.md`.** Structured bug
  report form that includes a `diagnostics.json` drop zone and
  sanity checkboxes for the two most common root causes. Dev-setup
  recipe for offline tests, security rules, and a concrete
  "add a new device type" walkthrough.

### Security

- **New secret-scanning infrastructure.** `.gitleaks.toml` with
  BT-Mesh-aware detectors (NetKey / AppKey / DevKey), pre-commit
  hook, and a CI workflow running on every push / PR plus a
  weekly full-history scan. Triggered by a real-key leak that
  was patched within ~80 min on 2026-04-27; history rewritten.
  Contributors: run `pre-commit install` after cloning.

### Tests

- `tests/test_mesh_session.py`: PDU build / decode round-trips
  for access and proxy-config frames, rejection of foreign
  NetKey / AppKey / IV Index, SEQ counter plumbing.
- `tests/test_proxy_candidates.py`: iteration order, winner
  promotion, empty list, already-connected short-circuit.
- `tests/test_connect_parser_models.py`: device-type detection
  for each capability tier plus remote-skip.
- `tests/test_models_capability.py`: `resolve_capability`
  parametric table (12 cases) + Light HSL Set opcode / payload
  framing (0x8277) + value masking.
- `tests/test_diagnostics.py`: `dump_active_gatt_tree` service
  tree extraction, disconnected / missing-services handling.

### Docs

- README: single-proxy architecture, Proxy-feature requirement,
  15 s polling, troubleshooting section (including diagnostics
  usage), capability-tier table.

### Breaking changes

- _None for end users._ SEQ / IV-Index storage layout unchanged,
  config-entry shape backward-compatible.

## [0.3.0]

Initial tagged release after the single-proxy refactor
(`MeshSession` + `MeshProxyConnection` + `HaefeleCoordinator`,
with the legacy per-node `MeshGattNode` removed).

[0.4.4]: https://github.com/Harpik/haefele-connect-mesh-ha/releases/tag/v0.4.4
[0.4.3]: https://github.com/Harpik/haefele-connect-mesh-ha/releases/tag/v0.4.3
[0.4.0]: https://github.com/Harpik/haefele-connect-mesh-ha/releases/tag/v0.4.0
[0.3.0]: https://github.com/Harpik/haefele-connect-mesh-ha/releases/tag/v0.3.0
