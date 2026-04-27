# Changelog

All notable changes to this integration are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.4.0]: https://github.com/Harpik/haefele-connect-mesh-ha/releases/tag/v0.4.0
[0.3.0]: https://github.com/Harpik/haefele-connect-mesh-ha/releases/tag/v0.3.0
