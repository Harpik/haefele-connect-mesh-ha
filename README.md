# Häfele Connect Mesh — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/Harpik/haefele-connect-mesh-ha/actions/workflows/tests.yml/badge.svg)](https://github.com/Harpik/haefele-connect-mesh-ha/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Control your **Häfele Connect Mesh** Bluetooth Mesh lights directly from Home Assistant over BLE — **no cloud, no Häfele gateway hardware, no MQTT broker required**.

Home Assistant itself plays the role of a BT Mesh **GATT Proxy client**. It connects to a nearby mesh proxy node, encrypts messages with the keys from your `.connect` export, and drives the lights over the mesh.

## Features

- 🔵 Native BLE via Home Assistant's `bluetooth` integration
- 📡 **ESPHome Bluetooth Proxy compatible** — extend range without extra software
- 💡 On/Off, brightness, color temperature (Tunable White)
- 🔁 Automatic reconnection with `bleak-retry-connector`
- 💾 Persistent BT Mesh sequence numbers (survives HA restarts — no replay risk)
- 🧩 HACS compatible, setup 100% via UI
- 🔐 No cloud. No phone. Keys stay on your HA install.

## Architecture

```
┌──────────────────────┐        BLE GATT         ┌──────────────────┐
│ Home Assistant       │ ──────(service 0x1828)──▶│  Mesh Proxy Node │
│ (bluetooth adapter   │                          │  (Häfele light)  │
│  or ESPHome proxy)   │                          └─────────┬────────┘
└──────────────────────┘                                    │ BT Mesh
                                                            ▼
                                              ┌───────────────────────────┐
                                              │ Other Häfele Mesh lights  │
                                              └───────────────────────────┘
```

HA opens one GATT connection to the closest Häfele node (the "proxy"), then every command flows through the mesh to the target node or group.

## Requirements

- Home Assistant **2024.1** or newer
- A Bluetooth adapter reachable by Home Assistant — either:
  - Local HCI adapter on the HA host (Pi built-in, USB dongle, …), **or**
  - An [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) on the same network
- Häfele Connect Mesh lights provisioned via the Häfele Connect app
- A `.connect` export file from that app

## Installation

### HACS (recommended)

1. HACS → **Integrations** → ⋮ → **Custom repositories**
2. Repository URL: `https://github.com/Harpik/haefele-connect-mesh-ha`
   Category: **Integration**
3. Install **Häfele Connect Mesh**
4. Restart Home Assistant

### Manual

```bash
# From your HA config directory
cd config/custom_components
git clone https://github.com/Harpik/haefele-connect-mesh-ha.git haefele_tmp
mv haefele_tmp/custom_components/haefele_mesh .
rm -rf haefele_tmp
```

Restart Home Assistant.

## Setup

1. **Settings → Devices & Services → + Add Integration** → search **Häfele Connect Mesh**
2. Paste the full contents of your `.connect` export file
3. Confirm discovered devices — one HA device per light is created

HA picks the Bluetooth adapter automatically via the core `bluetooth` integration (configure the preferred adapter there if you have more than one).

### Exporting the `.connect` file

1. Open the **Häfele Connect** app
2. **Menu → Settings → Export Configuration**
3. Share the file to yourself (email, Files app, …) and open it in a text editor
4. Copy the entire contents and paste into the HA setup form

## Supported Devices

Tested with Häfele Meshbox Tunable White spots. Other Häfele Connect Mesh light nodes (RGB, dimmable drivers, LED strips) are recognised by the parser and should work, but have not been verified end-to-end — reports welcome.

Remotes, sensors and switches are parsed but **skipped** (they don't expose writable light models).

## How it works

- `.connect` is parsed as Bluetooth Mesh CDB JSON with Häfele extensions (`tos_node`, `tos_devices`).
- Each light becomes a `light` entity inside an HA device keyed on MAC.
- The coordinator keeps one GATT connection open to the mesh proxy and maintains a per-source BT Mesh sequence counter persisted in `.storage/haefele_mesh_seq`.
- Commands are built as BT Mesh Network PDUs (encrypted with AES-CCM, obfuscated with AES-ECB) and segmented over the Mesh Proxy PDU bearer (spec § 6.6.2).
- A 60-second heartbeat verifies connectivity and reconnects dropped links.

See [`custom_components/haefele_mesh/gatt.py`](custom_components/haefele_mesh/gatt.py) and [`mesh_crypto.py`](custom_components/haefele_mesh/mesh_crypto.py) for the mesh implementation, and [`connect_parser.py`](custom_components/haefele_mesh/connect_parser.py) for the import format.

## Troubleshooting

**The integration setup says "No light nodes found"**
Your `.connect` file only contains remotes/sensors, or the export is from a non-Häfele mesh. Re-export from the Häfele Connect app after provisioning at least one light.

**Lights stay "unavailable"**
HA can't reach any mesh proxy over BLE. Check:
1. `Settings → System → Hardware → Bluetooth` shows an active adapter or ESPHome proxy.
2. Move the HA host / proxy closer to one light (≤ 10 m line of sight is a good test).
3. Look for `haefele_mesh` entries in `Settings → System → Logs`.

**Commands fail after a while**
BT Mesh requires monotonically increasing sequence numbers. The integration persists them on every emission, but if you restore an HA snapshot you may need to wait a few minutes for the network to accept new SEQ values (or re-provision).

## Development

```bash
# Run tests
pip install -r requirements_test.txt
pytest -v tests/
```

CI runs `pytest` on Python 3.11 / 3.12, plus [HACS action](https://github.com/hacs/action) and [hassfest](https://developers.home-assistant.io/docs/creating_integration_manifest/#hassfest) on every push.

## Roadmap

- [ ] Brand logo/icon in Home Assistant (PR to [`home-assistant/brands`](https://github.com/home-assistant/brands))
- [ ] RGB light support
- [ ] Status feedback (parse incoming OnOff/CTL status messages)
- [ ] Scene/group pass-through for Häfele scenes defined in the `.connect` file
- [ ] Config options flow (tweak heartbeat interval, TTL, etc.)

## Acknowledgements

- [Bluetooth Mesh Profile 1.0](https://www.bluetooth.com/specifications/specs/mesh-profile-1-0-1/) — the spec that made this possible
- [`bleak`](https://github.com/hbldh/bleak) + [`bleak-retry-connector`](https://github.com/Bluetooth-Devices/bleak-retry-connector) — the BLE stack HA uses under the hood
- The Häfele firmware team for keeping the BT Mesh implementation standards-compliant

## License

MIT — see [LICENSE](LICENSE).

> Not affiliated with, endorsed by, or supported by Häfele. Use at your own risk.
