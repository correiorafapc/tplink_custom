# Changelog

All notable changes to this custom integration, relative to the upstream
Home Assistant core **TP-Link** integration it is based on.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/).
This project is a modified redistribution of HA core (Apache License 2.0).

---

## [0.5.0]

Customized fork focused on **stability for slower / flaky TP-Link devices**
(notably the IOT **ES20M** dimmer and the SMART/KLAP-protocol P1xx, KS2xx,
L5xx and Matter-capable devices). All changes are in the update coordinator
(`coordinator.py`) unless noted.

### Packaging
- Renamed integration domain `tplink` → **`tplink_custom`** so it can be
  installed alongside (without colliding with) the built-in core integration.
- Set integration `name` to **"TP-Link Smart Home (Custom)"** and added a
  `version` field (required for custom integrations).

### Added — polling stability fixes

- **FIX 1 — Overlapping-poll guard.**
  Added an `asyncio.Lock` around the update cycle. If a previous poll is still
  running when the next interval fires, the new poll is skipped instead of
  stacking concurrent requests on the device — a primary cause of the ES20M
  freezing.

- **FIX 2 — Close connection after every poll / on error.**
  The device protocol connection is now explicitly closed after each
  successful poll and after any Kasa error. The ES20M's limited TCP stack
  cannot reliably hold a persistent idle connection between cycles; forcing a
  clean reconnect prevents stale-connection hangs and device freezes.

- **FIX 3 — Hard timeout on `device.update()`.**
  The update call is wrapped in `asyncio.wait_for(..., timeout=UPDATE_TIMEOUT)`
  so a hung connection can never hold the update lock open indefinitely.

- **FIX 4 — Consecutive-failure backoff.**
  After `BACKOFF_THRESHOLD` (3) consecutive failures the coordinator skips
  extra poll cycles, doubling the skip count with each further failure up to
  `MAX_BACKOFF_SKIPS` (8, ~4 min at a 30 s interval). This gives a struggling
  device breathing room instead of hammering it. The failure counter resets on
  the first clean poll (logged at INFO).

- **FIX 6 — Clearer failure logging.**
  On failure the coordinator now logs the actual exception type and message
  (e.g. `TimeoutError: ...`) instead of only the generic translated
  `device_error`, so the root cause is visible.

- **FIX 7 — Retry once before failing.**
  Many ES20M connection resets are transient — the device drops one connection
  but answers cleanly on an immediate second attempt. The update now performs
  one quick retry (after closing the stale connection and pausing
  `RETRY_BACKOFF_DELAY` = 1.0 s) within the same poll cycle before flipping the
  entity to unavailable. Authentication errors are **not** retried.

### Changed — logging noise reduction

- **Demote python-kasa per-module query errors.**
  Added a logging filter (`_ModuleQueryErrorFilter`) on
  `kasa.smart.smartdevice` that demotes the library's own per-module
  "... after first update" ERROR lines to DEBUG. These are emitted inside
  `device.update()` for each module (Time, AutoOff, DeviceModule, Matter…)
  that times out, never propagate as exceptions when the overall update still
  succeeds, and otherwise just spam the log. Genuine failures remain visible
  via the coordinator's own WARNING/UpdateFailed messages.

### Tuning constants (top of `coordinator.py`)

| Constant              | Value | Purpose                                              |
|-----------------------|-------|------------------------------------------------------|
| `UPDATE_TIMEOUT`      | 25 s  | Max wait for one `device.update()` (raised from 15 s; SMART/KLAP devices query several modules over an encrypted handshake and are slower than the IOT ES20M) |
| `BACKOFF_THRESHOLD`   | 3     | Consecutive failures before backing off              |
| `BACKOFF_SKIP_COUNT`  | 2     | Base skip cycles at threshold (doubles each extra)   |
| `MAX_BACKOFF_SKIPS`   | 8     | Hard cap on skipped cycles                           |
| `RETRY_BACKOFF_DELAY` | 1.0 s | Pause before the single in-cycle retry               |

### Note on other files
Files other than `coordinator.py` (`config_flow.py`, `__init__.py`, etc.) may
differ from current core simply because this fork was copied from an earlier
core version. The intentional behavioural changes above are confined to the
update coordinator.

---

## Upstream

Based on the Home Assistant core TP-Link integration:
https://github.com/home-assistant/core/tree/dev/homeassistant/components/tplink

Requires `python-kasa[speedups]==0.10.2`.
