# TP-Link Smart Home (Custom)

A customized fork of the Home Assistant core **TP-Link** integration, hardened
for **stability with slower and flaky TP-Link devices** — in particular the IOT
**ES20M** dimmer and the SMART/KLAP-protocol devices (P1xx, KS2xx, L5xx and
Matter-capable models).

The core integration assumes devices respond quickly and hold a persistent
connection. Some TP-Link hardware doesn't, which shows up as repeated
`Unable to query the device ... TimeoutError`, entities flipping to
*unavailable*, and (on the ES20M) the device freezing. This fork addresses
that in the update coordinator.

## Attribution

This integration is **based on the Home Assistant core TP-Link integration**,
licensed under the **Apache License 2.0**. This is a modified redistribution;
all original copyright and license notices are retained. See `LICENSE` and
`NOTICE`.

Original source: https://github.com/home-assistant/core/tree/dev/homeassistant/components/tplink

## What's different from core

All behavioural changes are in `coordinator.py`. See `CHANGELOG.md` for full
detail. In short:

- **Overlapping-poll guard** — skips a new poll if the previous one is still
  running, instead of stacking requests on the device.
- **Fresh connection each cycle** — closes the protocol connection after every
  poll and after errors, avoiding stale-connection hangs on devices with a
  limited TCP stack.
- **Hard update timeout** (`UPDATE_TIMEOUT`, 25 s) — a hung connection can never
  hold the coordinator lock open indefinitely.
- **Retry-once on transient errors** — one quick in-cycle retry before marking a
  device unavailable, since many resets are transient.
- **Consecutive-failure backoff** — progressively skips cycles for a repeatedly
  failing device so it isn't hammered, and self-recovers on the first clean poll.
- **Clearer failure logging** — logs the real exception type/message.
- **Quieter logs** — demotes python-kasa's noisy per-module
  "... after first update" ERROR lines to DEBUG (genuine failures still logged).

Packaging: the domain is **`tplink_custom`** (so it can coexist with the core
`tplink` integration) and the manifest carries a `version`.

## Requirements

- `python-kasa[speedups]==0.10.2` (declared in the manifest; HA installs it
  automatically).

## Installation (via HACS)

1. HACS → **Integrations** → three-dot menu → **Custom repositories**.
2. Add this repository's URL, category **Integration**.
3. Install **"TP-Link Smart Home (Custom)"**, then **restart Home Assistant**.
4. Add it via **Settings → Devices & Services → Add Integration**, or let it
   auto-discover your TP-Link devices.

> If you also run the built-in TP-Link integration, note both may try to
> discover the same devices. Using the custom domain avoids a hard collision,
> but you may prefer to run only one for a given device.

## Tuning

The stability constants live at the top of `coordinator.py`
(`UPDATE_TIMEOUT`, `BACKOFF_THRESHOLD`, `BACKOFF_SKIP_COUNT`,
`MAX_BACKOFF_SKIPS`, `RETRY_BACKOFF_DELAY`). Adjust to taste for your devices
and poll interval.

## Maintenance notes

- This is a fork of core copied at a point in time; parts of the tree
  (e.g. `config_flow.py`) may lag current core. When syncing with upstream,
  re-apply the coordinator changes documented in `CHANGELOG.md`.
- Consider contributing the transient-retry / connection-close behaviour
  upstream if it proves broadly useful.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
Modifications © 2026 correiorafapc. Based on Home Assistant core
(© Home Assistant contributors).
