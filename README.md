# Häfele Connect Mesh — Home Assistant Integration

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Tests](https://github.com/Harpik/haefele-connect-mesh-ha/actions/workflows/tests.yml/badge.svg)](https://github.com/Harpik/haefele-connect-mesh-ha/actions/workflows/tests.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Control your **Häfele Connect Mesh** Bluetooth Mesh lights directly from Home Assistant over BLE — **no cloud, no Häfele gateway hardware, no MQTT broker required**.

Home Assistant itself plays the role of a BT Mesh **GATT Proxy client**. It connects to a nearby mesh proxy node, encrypts messages with the keys from your `.connect` export, and drives the lights over the mesh.

## Features

- 🔵 Native BLE via Home Assistant's `bluetooth` integration
- 📡 **ESPHome Bluetooth Proxy compatible** — extend range without extra software
- 💡 On/Off, brightness, color temperature (Tunable White), hue/saturation (RGB, experimental)
- 🔁 Automatic reconnection with `bleak-retry-connector` — immediate retry on disconnect plus a 60 s heartbeat as a safety net
- 🎛️ **Physical remote changes are reflected in HA** — the coordinator polls every light every 15 s so wall-switch / Häfele-app presses show up in HA within seconds
- 💾 Persistent BT Mesh sequence numbers and IV Index (survives HA restarts — no replay risk)
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

HA opens **one** GATT connection to a single Häfele node that has the BT Mesh **Proxy feature** enabled. That node forwards every command and status update between HA and the rest of the mesh — so only one proxy-capable node needs to be reachable over BLE, regardless of how many lights are in the network.

> ⚠️ **Your network must contain at least one node with the Proxy feature active** (this is the Häfele app default for mains-powered lights). Nodes that only implement the GATT Proxy *service* without the Proxy feature (e.g. some battery-powered spots with it disabled for power reasons) will advertise UUID `0x1828`, accept a GATT connection, but never forward mesh traffic. The integration probes for a real Secure Network Beacon on connect and skips those automatically.

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

Tested end-to-end with Häfele Meshbox Tunable White spots. The integration detects four capability tiers from the `.connect` export:

| Capability tier   | Triggers on `tos_node.type` containing | HA surface               | Status       |
|-------------------|-----------------------------------------|--------------------------|--------------|
| `tunable_white`   | `tw`, `tunable`                         | brightness + color_temp  | ✅ verified   |
| `dimmable`        | `dim`, any `com.haefele.driver/led.*`   | brightness               | ⚠️ likely     |
| `rgb`             | `rgb`, `hsl`, `color`                   | brightness + hs_color    | 🧪 experimental |
| `onoff`           | `relay`, `onoff`, `switch_out`          | on/off                   | ⚠️ likely     |

RGB support uses the standard BT Mesh **Light HSL Set Unacknowledged** opcode (`0x8277`). If your RGB fixture uses a vendor-specific opcode instead, colour changes won't apply — please open an issue with a diagnostics dump and we'll add a per-model override.

Remotes, sensors and wall switches are recognised and deliberately skipped: they're battery-powered, don't act as proxies, and don't expose an entity on the HA side.

Remotes, sensors and switches are parsed but **skipped** (they don't expose writable light models).

## How it works

- `.connect` is parsed as Bluetooth Mesh CDB JSON with Häfele extensions (`tos_node`, `tos_devices`).
- Each light becomes a `light` entity inside an HA device keyed on MAC.
- The coordinator keeps **one** GATT connection open to a functional mesh proxy node and maintains a per-source BT Mesh sequence counter (and last-seen IV Index) persisted in `.storage/haefele_mesh_seq`.
- Commands are built as BT Mesh Network PDUs (encrypted with AES-CCM, obfuscated with AES-ECB) and sent over the Mesh Proxy PDU bearer (spec § 6.6.2).
- An immediate reconnect is attempted whenever the BLE link drops; a 60 s heartbeat also verifies connectivity as a safety net.
- Every 15 s the coordinator polls each light for On/Off + CTL state so manual changes from the physical remote or the Häfele app are reflected in HA.
- Secure Network Beacons from the mesh are parsed live; the IV Index is auto-updated if the mesh advances it.

See [`custom_components/haefele_mesh/gatt.py`](custom_components/haefele_mesh/gatt.py) and [`mesh_crypto.py`](custom_components/haefele_mesh/mesh_crypto.py) for the mesh implementation, and [`connect_parser.py`](custom_components/haefele_mesh/connect_parser.py) for the import format.

## Troubleshooting

**The integration setup says "No light nodes found"**
Your `.connect` file only contains remotes/sensors, or the export is from a non-Häfele mesh. Re-export from the Häfele Connect app after provisioning at least one light.

**Lights stay "unavailable"**
HA can't reach any mesh proxy over BLE. Check:
1. `Settings → System → Hardware → Bluetooth` shows an active adapter or ESPHome proxy.
2. Move the HA host / proxy closer to one light (≤ 10 m line of sight is a good test).
3. At least one mains-powered light must have the BT Mesh **Proxy feature** enabled (default in the Häfele app).
4. Look for `haefele_mesh` entries in `Settings → System → Logs`. A line saying `No Häfele node reachable as a mesh proxy` means none of your nodes emitted a Secure Network Beacon within 5 s of connecting — the proxy candidates are either out of range, powered off, or have the Proxy feature disabled.

**Physical remote presses aren't reflected in HA**
The coordinator polls every 15 s, so expect up to ~15 s of lag after a wall-switch press. If it never catches up, check HA logs for `State poll for ... failed` entries.

**Need to see what's really going on**
Download the integration's diagnostics from **Settings → Devices & Services → Häfele Connect Mesh → ⋮ → Download diagnostics**. The JSON includes:
- Coordinator state (active proxy, candidate order, per-node availability, current IV Index, SEQ snapshot)
- Latest BLE advertising data for every provisioned node (RSSI, advertised service UUIDs, `advertises_mesh_proxy` flag, seconds since last seen)
- Full GATT service/characteristic tree of whichever node is currently acting as the mesh proxy (read from the cached bleak client — zero extra BLE traffic)

All network/app/device keys are redacted before export, so the file is safe to share in a bug report.

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
