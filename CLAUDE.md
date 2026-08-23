# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A remote monitor for the Medtronic Minimed 770G/780G insulin pump, for caregivers of a Type-1 Diabetes patient. It polls an external **Carelink proxy** (a separately-hosted instance of the [Carelink Python Client](https://github.com/ondrej1024/carelink-python-client)'s `carelink_client_proxy.py`, providing a simple REST API) and displays glucose/pump/sensor status. There are three independent frontends sharing this data-fetching logic conceptually, but **not sharing code**:

- `minimed-mon.py` — runs on an **M5Stack Fire** device under MicroPython (UIFlow2 firmware).
- `minimed-mon-pc.py` — a desktop clone using Tkinter, for faster iteration without physical hardware.
- `minimed-mon-inkplate.py` — runs on a **Soldered Inkplate 2** (2.13" 3-color e-paper, classic ESP32) under [Inkplate-micropython](https://github.com/SolderedElectronics/Inkplate-micropython). A redesign, not a direct port, of `minimed-mon.py` — see that file's header for the full rationale and its `/home/frederik/.claude/plans/investigate-if-the-micropython-shimmying-whale.md` for the on-hardware debugging history behind it.

There is no build step, package manifest, or test suite in this repo — all three are single-file scripts.

## Running

**PC version** (primary way to iterate on logic/UI without a device):
```
./run-pc-monitor.sh
# or: ./.venv/bin/python3 minimed-mon-pc.py
```
Dependencies (not pinned anywhere — install manually into `.venv`): `requests`, `playsound` (needs a GStreamer binding, e.g. `apt install python3-gst-1.0`, for alarm sounds on Linux).

Before running, point it at your Carelink proxy by editing near the top of `minimed-mon-pc.py`:
```python
proxyaddr = "0.0.0.0"  # IP/hostname of your carelink_client_proxy instance
proxyport = 8081
```

**M5Stack Fire version**: deploy by copying `res/` to `/flash/res/` and `minimed-mon.py` to the device's flash (e.g. `/flash/apps/`) via a MicroPython-capable editor (Thonny, VS Code + M5Stack plugin). Requires UIFlow2 (`uiflow_micropython`) firmware. First boot (or failed WiFi) drops it into AP config mode (SSID `M5_MINIMED_MON`, password `123456789`, config UI at `http://192.168.4.1`) to set WiFi credentials, NTP server, timezone, and the proxy address/port; config is persisted as JSON at `/flash/minimed_config.json`.

**Inkplate 2 version**: flash `firmware/inkplate-firmware.bin` from [Inkplate-micropython](https://github.com/SolderedElectronics/Inkplate-micropython) via `esptool` (`erase-flash` then `write-flash -z 0x1000`), then `mpremote mip install github:SolderedElectronics/Inkplate-micropython/boards/inkplate2` for the board driver package, then copy `minimed-mon-inkplate.py` to the device's flash root as `main.py` via `mpremote cp` so it runs automatically on boot/wake. No `res/` deployment needed — this frontend uses procedural drawing and text glyphs instead of PNG icons (see its header for why). First boot (or a WiFi failure with no stored credentials) drops it into AP config mode (SSID `INKPLATE_MINIMED_MON`, password `123456789`, config UI at `http://192.168.4.1`); config is persisted as JSON at `/minimed_config.json` (this firmware's root is plain `/`, not `/flash/...`). Runs a single poll/draw cycle then calls `machine.deepsleep()` — a transient WiFi failure on an already-configured device just retries next cycle rather than re-entering AP mode (see `wlan_connect()`'s comment in the file for why that matters here specifically).

There's no linter/formatter/test command configured — validate changes by running the PC version and, when touching Fire-specific code, by deploying and observing on real hardware.

## Architecture

### `minimed-mon.py` (M5Stack Fire / UIFlow2)

Everything lives in one file, in this order: fault-code tables → small widget-tracking helper classes → config/NVS/NTP/AP-mode helpers → screen/button handlers → data-formatting helpers (`time_delta`, `reservoir_level`, `time_to_calib_progress`, `sensor_age_*`) → `handle_*` update handlers → init → a single polling `while True` main loop at the bottom (no OS-level task scheduler; periodic work is done via `time.ticks_ms()`/`ticks_diff()` deadline checks each iteration, see `TIMER*_PERIOD_S` constants).

Three logical "screens" (pump status, time-in-range stats, config/info) are emulated with `TrackedLabel`/`TrackedImage` helper classes that group widgets per screen and replay their last-known value on `show_screen(n)`, since UIFlow2's `Widgets.Image`/`Widgets.Label` draw straight to the LCD and never repaint themselves automatically. `show_screen(1)` **must** run before the NTP-sync wait at startup, or all three screens' widgets are visibly overlaid (every widget draws itself immediately at construction time).

This file was ported from an M5Stack Core2 (UIFlow1) original to the Fire (UIFlow2) — **read the "Fire port notes" comment block at the top of the file before changing GUI, timer, HTTP, NTP, storage, or speaker code**; it documents the API mapping for every subsystem that changed (e.g. `m5stack`/`m5stack_ui` → `M5`/`Widgets`, `urequests` → `requests2`, `nvs` → a JSON config file, `@timerSch.event` → manual polling, touch-based screen dimming → physical-button-based). Getting this wrong silently reintroduces UIFlow1 APIs that don't exist on this firmware.

Fonts are limited to UIFlow2's built-in sizes (12/14/16/18/24/40/44/48) — sizes from the original design (20/22/26/28) were rounded to the nearest available one; don't assume arbitrary font sizes are available.

### `minimed-mon-pc.py` (desktop)

Still emulates the **original UIFlow1** API surface locally (`M5Screen`, `M5Img`, `M5Label`, `M5Msgbox`, `timerSch`, `lcd`, `ntpclient`, `speaker` classes near the top of the file) on top of Tkinter, rather than the `M5`/`Widgets` API `minimed-mon.py` now uses post-Fire-port. **The two files have diverged and are not kept in sync automatically** — a change to data handling, alarm logic, or fault tables in one needs to be manually ported to the other if it should apply to both. Structurally it otherwise mirrors `minimed-mon.py`'s helper functions (`handle_alarm`, `handle_pumpdataupdate`, `time_to_calib_progress`, `sensor_age_*`, fault-code tables, etc.) and uses `timerSch`-driven periodic callbacks (`ttimer0/1/2`) instead of a manual polling loop.

### `minimed-mon-inkplate.py` (Soldered Inkplate 2)

A single-cycle script, not a loop: `main()` connects WiFi, resyncs NTP, fetches/parses pump data, redraws the whole 212×104 3-color panel via `draw_screen()`, then calls `machine.deepsleep()`, which re-runs the entire script from scratch on the next wake — there is no in-memory state carried between cycles by design (notably, no `lastAlarmId` dedup like the other two files' `handle_alarm()` — see the "Alarm handling" comment in the file for why that's safe here). **Read the file's own header before changing GUI, timing, HTTP, NTP, storage, or alarm code** — it documents the hardware/firmware quirks this design works around (fixed 16px-tall proportional font, no partial refresh, no `requests`/`urequests` module, a 2000-01-01 time epoch instead of the standard 1970 Unix epoch, no text auto-wrap). Layout is drawn procedurally (`fill_rect`/`print`/text glyphs) rather than from PNG icons — there's no `res/` dependency for this frontend at all.

### Shared concepts across all three files

- **Data source**: `handle_pumpdataupdate()` polls `http://<proxyaddr>:<proxyport>/carelink/nohistory` on a timer and drives all on-screen state from the JSON response (glucose, pump battery %, reservoir level, sensor state, calibration countdown, active insulin, banner state, alarms). The proxy serving that endpoint (`carelink_client2_proxy.py`) lives in the sibling repo `~/carelink-python-client` — if a field's shape/name changes and this monitor breaks, that's where to check the actual response format, not just the upstream [carelink-python-client](https://github.com/ondrej1024/carelink-python-client) README.
- **Fault codes**: `faultIdMapping` (raw Carelink fault ID → canonical ID, many-to-one) and `faultIdTable` (canonical ID → human-readable message) are large, mostly-static lookup tables reverse-engineered from Carelink pump alarm data — treat them as data, not something to restructure, and extend by adding entries rather than changing their shape.
- **Alarm handling**: `handle_alarm()` (Fire/PC) — dedupes on `lastAlarmId`/`lastAlarmMsg` so a repeated alarm isn't re-announced/re-sounded every poll; this exists specifically to avoid replaying the audible WAV alarm repeatedly. The Inkplate version's `get_alarm_text()` deliberately skips this dedup — it has no speaker, so re-showing the same visual banner every redraw while an alarm stays recent is desired, not something to suppress.
- **DST/timezone**: handled manually via a `dstDelta` offset applied in `local_now()`/`time_delta()` rather than a timezone library (MicroPython has none) — this has had multiple regression fixes historically (see changelog at the top of `minimed-mon.py`), so treat DST edge cases here as easy to get subtly wrong.
- **Pump battery vs. device battery**: `pumpBatteryLevelPercent` from the Carelink JSON (icon-based, `mm_batt{0,25,50,75,100,unk}.png`) is the insulin pump's own battery, distinct from `M5.Power.getBatteryLevel()`, which reads the M5Stack Fire's own battery via its IP5306 PMIC (stepped 0/25/50/75/100 readout, not a continuous fuel gauge) — don't conflate the two when working on battery display code.

## Investigated: direct-to-Carelink from the M5 device (skip the proxy)

Explored (2026-08-21) whether `minimed-mon.py` could import `carelink_client2.py` directly and poll Medtronic's Carelink Cloud from the M5Stack Fire itself, dropping the LAN-hosted proxy dependency for true anywhere-with-WiFi portability. **Conclusion: not fully achievable, and not worth attempting as a rewrite.**

The blocker isn't the ongoing polling — `carelink_client2.py`'s runtime calls (refresh token, fetch data) are plain HTTPS REST, and UIFlow2's `requests2` does support HTTPS. The blocker is **token bootstrap**: `carelink_carepartner_api_login.py` (in `~/carelink-python-client`) drives a real Firefox browser via Selenium through Medtronic's OAuth/social-login flow, requires a **human to solve a reCAPTCHA**, and generates a 2048-bit RSA keypair + X.509 CSR for Medtronic's device-registration step. None of that can run on ESP32 MicroPython — no browser, no CAPTCHA-solving on this hardware, no CSR/X.509 tooling. Even the proxy's own "recover without redeploy" web GUI (`carelink_client2_proxy.py`'s `do_POST`) just accepts a *pasted* `logindata.json` produced by that same PC+Selenium flow — the whole project already assumes a PC is needed periodically for auth, so there's no shortcut being carried unused.

A hybrid is technically possible — bootstrap/re-auth stays a manual PC+Selenium step, but the device polls Medtronic directly once it has a token, so it isn't tied to a specific LAN. That would still require porting ~300 lines of `carelink_client2.py` logic to MicroPython, verifying ESP32 mbedTLS behaves against three separate Medtronic hosts, and storing a live CareLink account session in flash on a small, physically-loseable device (a materially bigger exposure than the WiFi password already stored there today). Not pursued given the effort and the fact that it doesn't remove the human/PC dependency, just makes it rarer.
