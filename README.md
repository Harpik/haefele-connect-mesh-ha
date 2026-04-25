# Häfele Connect Mesh - Home Assistant Integration

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Control your **Häfele Connect Mesh** Bluetooth lights directly from Home Assistant via BLE GATT Proxy — no cloud, no gateway hardware required.

## Features

- 🔵 Direct BLE GATT connection (no hub needed)
- 💡 On/Off, Brightness, Color Temperature (Tunable White)
- 📡 Automatic availability monitoring (heartbeat)
- 🔧 Setup via UI — just upload your `.connect` export file
- 🔌 Supports multiple Bluetooth adapters (USB dongles)

## Requirements

- Home Assistant 2024.1.0 or newer
- Bluetooth adapter accessible from HAOS (built-in or USB dongle)
- Häfele Connect Mesh lights provisioned with the Häfele Connect app
- Export file from the Häfele Connect app (`.connect` format)

## Installation via HACS

1. In HACS, click **Custom repositories**
2. Add `https://github.com/YOUR_USERNAME/haefele-connect-mesh-ha` as **Integration**
3. Install **Häfele Connect Mesh**
4. Restart Home Assistant

## Manual Installation

1. Copy the `custom_components/haefele_mesh` folder to your HA `config/custom_components/` directory
2. Restart Home Assistant

## Setup

1. Go to **Settings → Integrations → Add Integration**
2. Search for **Häfele Connect Mesh**
3. Paste the contents of your `.connect` export file
   - Export from the Häfele Connect app: **Menu → Settings → Export Configuration**
4. Select your Bluetooth adapter
5. Confirm the discovered devices

## How to export your .connect file

1. Open the **Häfele Connect** app on your phone
2. Go to **Settings** (gear icon)
3. Tap **Export Configuration**
4. Save or share the `.connect` file
5. Open it with a text editor and copy the entire contents

## Supported Devices

- Häfele Connect Mesh Tunable White spots (Meshbox TW)
- Other Häfele Connect Mesh light nodes

## Technical Notes

This integration communicates directly with Häfele Mesh nodes via **BLE GATT Proxy** (Bluetooth Mesh Profile, service UUID `0x1828`). It uses the cryptographic keys from your exported configuration to build and encrypt BT Mesh PDUs, bypassing the need for the Häfele gateway hardware or cloud service.

The integration was developed and tested with:
- Häfele Meshbox TW 1C spots
- Raspberry Pi 5 running HAOS
- Raspberry Pi 3 for development/testing

## License

MIT License
