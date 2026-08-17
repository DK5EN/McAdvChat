# Release History

## Unreleased

### Backend (MCProxy)

- **[feat]** **Link Check** — probe whether a station answers on direct RF, using the MeshCom
  `v4.35p.07.24.2` firmware's `{ping}`/`{pong}` exchange, driven entirely from McApp over
  Extern-UDP (the official app cannot do this: a pong is never forwarded to BLE clients).
  `POST/DELETE/GET /api/linkcheck*` plus `proxy:linkcheck_*` SSE events. Reports **reachability and
  the reply's RSSI/SNR** — deliberately not a round-trip time: measured on air at 21-43 s, dominated
  by the node's TX queue and the firmware's 40 s retransmit steps rather than propagation. Caps are
  enforced server-side (≤5 attempts, one session per target, ≤3 concurrent, 60 s cooldown) because
  every attempt is ~4 keyings under the operator's licence. See
  `doc/2026-08-13_1500-linkcheck-ping-pong-ADR.md`.
- **[fix]** `{ping}`/`{pong}` protocol frames no longer appear as chat. They were being stored and
  rendered as garbage messages (`{pong}{1234567890}`) whenever two other stations in RF range
  probed each other. The suppression is placed after signal ingestion, so the pong's RSSI/SNR still
  reaches `signal_log`.
- **[fix]** `store_message()` no longer loses an entire frame when `msg` is not a string. Reachable
  from unauthenticated UDP port 1799 via the telemetry branch, which publishes without
  `udp_handler`'s `isinstance` guard; a crafted `{"type":"tele","msg":123}` raised `AttributeError`
  and dropped the frame.
- **[fix]** `ctcping`'s background tasks are now cancelled at shutdown. `_ping_bg_tasks` was
  populated at four sites and cancelled nowhere, so shutdown could tear down transports under a
  sleeping 30 s ACK timeout or a 300 s test monitor.

- **[fix]** Piped installs (`curl | sudo bash`) now pin bootstrap libs, templates, and the app to one resolved release tag for the whole run, instead of always pulling libs from the `development` branch tip regardless of the app version being installed. `--tag` is now a real time machine for libs+templates+app (previously app-only); a new `--ref`/`MCAPP_BOOTSTRAP_REF` forces just the bootstrap tree ref, independently of the app version, for developing bootstrap changes without cutting a release and as a one-line field rollback. A skew guard aborts cleanly if a pinned tag's libs predate a function the running script needs, instead of installing a mismatched pair. See `doc/2026-08-09_1600-bootstrap-tag-pinning-plan.md`.

### Frontend (webapp)

- **[fix]** Push notification settings no longer wipe themselves. Opening Settings on a cold boot —
  which is what a service-worker update reload produces — re-POSTed the push subscription with the
  built-in defaults before the settings store had finished loading from IndexedDB. A subscribe POST
  replaces the **whole** server-side filter, so the user's group list was silently cleared on the
  backend and group notifications stopped. Observed in the field after the 2026-08-17 update, with
  the subscription itself provably intact the whole time.
- **[fix]** Push status is no longer reported from the outcome of a network round trip. An intact
  subscription rendered as "not enabled yet" — hiding the DM / groups / broadcast fields, which
  reads as data loss — for as long as the VAPID fetch and subscribe POST took, worst right after a
  deploy while the backend is still restarting. It is now committed from `getSubscription()` alone,
  before any network call, with a distinct "checking" state for the pre-resolution unknown.
- **[fix]** User settings can no longer be persisted before they were loaded, which would have
  written an empty callsign, proxy host and coordinates over the stored record.

## v1.6.13 (2026-06-20)

Maintenance release: reduces journal log noise and rolls up dependency updates. No functional changes.

### Backend (MCProxy)

- **[perf]** High-frequency INFO log lines for UDP telemetry, ACK receipt, and UDP send are demoted to DEBUG. All three are confirmed to land in the database (`telemetry` table, `messages.send_success`, and echo-back ingest respectively), so logging them at INFO produced constant journald noise with no diagnostic value. Error and warning paths are untouched.
- **[chore]** `uv lock --upgrade` dependency sweeps.

### Frontend (webapp)

- **[feat]** **Link Check** button and result row per station in the station list, with a
  `LinkCheckStore` driven by the four `proxy:linkcheck_*` SSE events. Copy is deliberately careful:
  "response time" in whole seconds (never RTT), RSSI/SNR attributed to the pinged station only when
  the reply arrived direct (`hops === 0`), and a timeout described as "no direct-RF answer" rather
  than "station down".
- **[fix]** The station card no longer nests interactive controls inside a `role="button"`
  ancestor — the callsign is now a real button and the card keeps a mouse-only click, matching
  `MheardListPanel`/`WxListPanel`. Pinned with a regression test.

- **[chore]** `npm update` — minor and patch dependency bumps (vue, vue-tsc, vite-plugin-vue, typescript-eslint, transitive patches).
