# Contributing to Häfele Connect Mesh for Home Assistant

Thanks for considering a contribution! 🐧 This integration is small and
personal-project-shaped, but it's open source and community patches are
very welcome.

## Ways to help

- **Report bugs** with the [Bug report form](./.github/ISSUE_TEMPLATE/bug_report.yml).
  Please always attach the **Download diagnostics** JSON (keys are auto-redacted).
- **Request features** via the [Feature request form](./.github/ISSUE_TEMPLATE/feature_request.yml).
- **Add device support.** If you own a Häfele Connect Mesh model that
  isn't well-supported yet (RGB, dimmer-only, LED strip, …), a PR with
  a tested mapping plus a redacted sample `.connect` node block is
  extremely valuable — see [_Adding a new device type_](#adding-a-new-device-type).
- **Improve docs** — the README and troubleshooting section welcome
  real-world "what worked / what didn't" notes.

## Development setup

```bash
git clone https://github.com/Harpik/haefele-connect-mesh-ha.git
cd haefele-connect-mesh-ha
pip install -r requirements_test.txt
pytest -v tests/
```

The unit tests run without Home Assistant or a BLE stack — `conftest.py`
stubs the bits of `homeassistant.*` and `bleak*` that `gatt.py` imports
at module load. If you add a module that pulls more HA runtime, extend
the stubs there instead of adding HA to the test requirements.

For manual end-to-end testing, install the component into a real HA
instance (e.g. via HACS with a custom repository pointing at your
fork / branch) and pair it against a physical mesh.

## Coding style

- **Commits:** conventional commits — `feat(...)`, `fix(...)`, `docs(...)`,
  `test(...)`, `refactor(...)`, `chore(...)`. Keep the subject ≤ 72
  chars; add a body when the change isn't self-evident.
- **Type hints** everywhere in production code. Tests can be looser.
- **No new runtime dependencies** without discussion — BT Mesh is a
  crypto-heavy domain and every extra package enlarges the threat
  surface of the integration.
- **Logging:** `_LOGGER.debug(…)` for anything frame-level, `info` for
  lifecycle events, `warning` for recoverable issues, `error` for
  real failures. Avoid `print`.
- **Async first:** never block the HA event loop. File / crypto work
  goes through `hass.async_add_executor_job` when it's non-trivial.
- **No secrets in code or tests.** Use synthetic values. The
  `diagnostics.py` redactor is belt-and-braces — don't rely on it.

## Tests

- New behavior needs tests. If writing a test requires real BLE
  hardware, split the patch: add the plumbing first with unit tests
  around the pure-logic parts (framing, state machines, conversions)
  and mark the hardware-dependent piece as follow-up.
- Keep tests offline. No external network, no real Bluetooth, no HA
  runtime.

## Adding a new device type

1. Update `connect_parser.py` so the new type is detected from
   `tos_node.type` or `tos_devices[*].name`. Add the detection pattern
   to `device_type` (`"rgb"`, `"dimmable"`, `"tunable_white"`, or a
   new value you introduce).
2. If new opcodes are needed (Light HSL Set, vendor opcodes, …), add
   helpers to `gatt.py` on `MeshProxyConnection`, with a matching
   status-decoding path in `light.py`.
3. Route the `LightEntity` capabilities (`supported_color_modes`,
   brightness/temperature limits) off `node_cfg["device_type"]`.
4. Add unit tests for the parser change, the new opcode framing
   (build/decode round-trip), and the entity capability mapping.
5. Note in the PR which physical model you verified against, firmware
   version, and any quirks you hit.

## Security

- The integration stores BT Mesh keys in the HA config entry. Never
  log raw key bytes, never include them in error messages.
- Persisted SEQ / IV Index must monotonically increase — if you touch
  that logic, add a replay-resistance test.
- Treat every `.connect` blob as potentially untrusted: validate
  structure before using it.

## License

By contributing you agree that your contributions are licensed under
the MIT license (see [LICENSE](LICENSE)).
