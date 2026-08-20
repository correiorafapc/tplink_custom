# TP-Link Smart Home (Custom)

A customized version of the Home Assistant core **TP-Link** integration,
modified for improved stability with certain Kasa/Tapo SMART-protocol devices.

## Attribution

This integration is **based on the Home Assistant core TP-Link integration**,
which is licensed under the Apache License 2.0. This is a modified redistribution.
All original copyright and license notices are retained. See `LICENSE`.

Original source: https://github.com/home-assistant/core/tree/dev/homeassistant/components/tplink

## Changes from the original

<!-- Document EXACTLY what you changed and why. Keep this current. -->
- Adjusted the update coordinator interval to reduce query timeouts on
  slower SMART-protocol devices (e.g. repeated
  `Unable to query the device ... TimeoutError` on some Kasa switches).
- (add any other changes here)

## Installation (via HACS)

1. In HACS, go to **Integrations**.
2. Click the three-dot menu → **Custom repositories**.
3. Add this repository's URL, category **Integration**.
4. Search for "TP-Link Smart Home (Custom)" and install.
5. Restart Home Assistant.

## Configuration

Add the integration via **Settings → Devices & Services → Add Integration**,
or it may auto-discover your TP-Link devices.

## Maintenance notes

- This is a fork of core; when the upstream integration changes, review the
  diff and re-apply the changes listed above as needed.
- Domain is set to `tplink_custom` to avoid collision with the core integration.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
Modifications © 2026 correiorafapc. Based on Home Assistant core (© Home Assistant contributors).
