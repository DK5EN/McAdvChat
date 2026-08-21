# BLE node registers never arrive after connect

**Status: FIXED** — MCProxy `dcf11da`, released `v1.6.14-dev.21`, verified live on `mcapp.local`.
Diagnosed 2026-08-01 against `v1.6.14-dev.20`; fixed the same day.

## 1. Symptom

Tapping a device in the webapp's Bluetooth modal established a working BLE link — messages and
positions flowed, the footer badge went green — but **not a single node register reached the
frontend**. `/webapp/bluetooth` showed `XX0XXX`, Node-ID `00:00:00:00`, firmware `0.0.0`, hardware
`--`, GPS `0.0000`. Every register chip stayed unlit. The condition was permanent for the life of
the connection and reproduced on every reconnect. The footer badge read `BLE` instead of the node
callsign, because the device name is carried by the `I` register.

## 2. Root cause (confirmed)

`POST /api/ble/ensure_connected` (`src/mcapp/sse_routes/deploy.py`) forwarded to `ble_service` and
returned. **It performed no register query and no info push.** The legacy path
`_handle_ble_connect_command` had done both inline, but the webapp stopped sending that command
when it moved to the REST route (webapp `4754dd2`, MCProxy `8d53c8d`), and nothing replaced it.

`G` was the only register that ever appeared, delivered by accident by `ble_service`'s 300 s
keepalive `--pos` — which is exactly, and only, what the live cache contained.

### What the original diagnosis got wrong

The first write-up hypothesised a **race** between `ble_service`'s `send_hello()` and mcapp's
notification drain. There is no race:

- `ble_service`'s notification queue is a `deque(maxlen=1000)` that **retains** events when no SSE
  client is attached, and replays the backlog on reattach. A detached-SSE window cannot destroy
  the burst.
- There is no `TYP` filtering anywhere on the path. `ROUTINE_JSON_TYPS` in `ble_protocol.py` is
  set-identical to `BLE_REGISTER_TYPES`.
- `send_hello()` fires exactly once on every connect path — the firmware genuinely was told to
  burst.

Nothing re-requested the registers. That was the whole bug.

## 3. The design question, answered empirically

The write-up left one question open — whether the firmware answers `--info` with the `I` register —
and called it the decision point between the two candidate designs. **It does.** Confirmed in
firmware source and then on the live node:

```
"TYP": "I", "FWVER": "4.35 p", "CALL": "DK5EN-98", "ID": 67549304, "HWID": 43
```

The post-hello batch (`esp32_main.cpp` / `nrf52_main.cpp` `config_cmds`) is nothing but ten ordinary
text commands run through the same parser the proxy can drive. The full mapping, verified both ways:

| command     | register  | command       | register  |
| ----------- | --------- | ------------- | --------- |
| `--info`    | `I`       | `--wifiset`   | `SW`+`S2` |
| `--nodeset` | `SN`      | `--wx`        | `W`       |
| `--pos`     | `G`       | `--analogset` | `AN`      |
| `--aprsset` | `SA`      | `--io`        | `IO`      |
| `--seset`   | `SE`+`S1` | `--tel`       | `TM`      |

**"Capture the burst" was ruled out, not merely deprioritised.** The firmware's `sendMheard()` dumps
one frame per heard station into the _same_ ring buffer immediately after the register frames, and
`addBLEComToOutBuffer` advances the write pointer with no read-pointer check. The burst can be
destroyed inside the node before it is ever transmitted, so capturing it can never be made reliable.

**Re-sending the `0x10` hello to force a re-burst is also not an option** and must not be
reintroduced: it re-runs `sendMheard()` into the same ring and re-runs PIN auth, where a wrong hash
drops the link.

## 4. What was implemented

- `_query_ble_registers` extended from 3 commands to the full 10, covering all 12 registers.
  `BLE_QUERY_DELAY_MULTIPART` (previously unused) now spaces the two two-frame replies.
- A **single-flight, bounded, never-raising background hydration** on `MessageRouter`, scheduled —
  not awaited — so no HTTP route or `build_app()` pays the ~9 s cost inline. Single-flight resolves
  as **skip, not supersede**: the sweep is idempotent, whereas superseding would let a user
  re-clicking "connect" restart the clock forever and never see a register.
- The sweep waits `BLE_HYDRATE_BURST_CLEAR_DELAY_S` (12 s) first. The node freezes its outbound
  queue for 3000 ms after a hello and then drains one frame per ≥300 ms, so the burst occupies
  roughly `T_hello+3s .. +9s`; commands issued inside that window are wasted. The old inline call
  fired at ~1–2 s, squarely inside it.
- Triggered from the `ensure_connected` route on **body** `success` — `ble_service` reports a failed
  connect as `success: false` inside an HTTP 200, so the status code is not the signal.
- `requery_reused_ble_connection` schedules instead of awaiting, so the longer sweep no longer
  delays the SSE server binding. `BLE_REQUERY_TIMEOUT_S` 10 s → 25 s.
- **A second, independent cause of the same symptom:** an mcapp↔ble_service SSE drop outliving
  `SSE_DISCONNECT_GRACE_S` publishes `("disconnect BLE", "lost")`, which wiped `cached_ble_registers`
  while the radio link was usually still up — and nothing refilled it. The stream now re-hydrates on
  recovery.
- Both legacy command paths moved onto the same scheduled sweep, keeping the fresh-connect
  `--settime` clock sync they owned.

## 5. Tests

`src/mcapp/ble_hydration_tests.py`, 44 assertions, wired into `run_startup_tests.py`'s `all_ok`
conjunction. Covers sweep order and coverage, multipart spacing, the route's body-not-status success
gate, single-flight, cancellation, never-raises, the link re-check and the SSE-recovery hook.

Verified to be a genuine regression test: reconstructing the pre-fix behaviour in memory makes it
fail. The end-to-end case specifically catches a **missing call site** — neutering only the call
site fails it while the unit cases still pass.

## 6. Verification

Live on `mcapp.local` after deploy: the full register set arrives, and the footer badge reads
`DK5EN-98` rather than `BLE` — i.e. the `I` register is landing.
