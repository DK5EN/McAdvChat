# McApp BLE Service

Standalone BLE service that exposes MeshCom BLE device functionality via HTTP REST API and Server-Sent Events (SSE).

## Purpose

This service enables the McApp "brain" to run on hardware without Bluetooth support (e.g., Mac, cloud server, OrbStack VM) while the BLE service runs on a Raspberry Pi with Bluetooth hardware.

## Quick Start

### On Raspberry Pi (with Bluetooth)

```bash
cd ~/mcapp/ble_service
uv sync

export BLE_SERVICE_API_KEY=your-secret-key
uv run uvicorn ble_service.src.main:app --host 0.0.0.0 --port 8081
```

### As systemd service

The service file runs as non-root user, binds to `127.0.0.1:8081`, and automatically unblocks Bluetooth radio and powers on the adapter before starting.

```bash
# Copy service file
sudo cp mcapp-ble.service /etc/systemd/system/

# Set API key
sudo systemctl edit mcapp-ble
# Add: Environment=BLE_SERVICE_API_KEY=your-secret-key

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable --now mcapp-ble
```

## Authentication

API key is passed via the `X-API-Key` header. Behavior depends on the `BLE_SERVICE_API_KEY` environment variable:

| Value               | Behavior                                              |
| ------------------- | ----------------------------------------------------- |
| `"your-secret-key"` | All API endpoints require matching `X-API-Key` header |
| `""` (empty/unset)  | Unauthenticated mode (startup warning logged)         |
| `"disabled"`        | Explicitly disable authentication (no warning)        |

The `/health` endpoint never requires authentication.

```bash
curl -H "X-API-Key: your-secret-key" http://pi.local:8081/api/ble/status
```

## API Endpoints

### Health Check

| Method | Endpoint  | Auth | Description  |
| ------ | --------- | ---- | ------------ |
| GET    | `/health` | No   | Health check |

Response:

```json
{ "status": "healthy", "ble_connected": false, "timestamp": 1706100000000 }
```

### Status & Discovery

| Method | Endpoint           | Description                                          |
| ------ | ------------------ | ---------------------------------------------------- |
| GET    | `/api/ble/status`  | Connection status, state, device info, last activity |
| GET    | `/api/ble/devices` | Scan for BLE devices                                 |

**`GET /api/ble/devices`** query parameters:

| Param     | Default | Description                         |
| --------- | ------- | ----------------------------------- |
| `timeout` | 5.0     | Scan duration in seconds (1.0-30.0) |
| `prefix`  | `MC-`   | Device name prefix filter           |

Cannot scan while connected (returns 409).

### Connection Management

| Method | Endpoint              | Description                             |
| ------ | --------------------- | --------------------------------------- |
| POST   | `/api/ble/connect`    | Connect to device                       |
| POST   | `/api/ble/disconnect` | Disconnect (also resets ERROR state)    |
| POST   | `/api/ble/pair`       | Pair with device (must be disconnected) |
| POST   | `/api/ble/unpair`     | Remove pairing                          |

**`POST /api/ble/connect`** accepts JSON body with either `device_address` (MAC) or `device_name`. If only `device_name` is given, the service scans for 5 seconds to resolve the MAC address (returns 404 if not found).

On successful connect, the service automatically: starts notifications, waits ~0.7s (`HELLO_SETTLE_DELAY_S` — mirrors how long a phone app waits between subscribing to notifications and sending hello; sending hello immediately was suspected of racing the firmware's connect-callback setup and suppressing its post-hello register burst), sends hello, waits 1s, then queries extended registers (`--io`, `--tel`).

**`POST /api/ble/disconnect`** cancels any pending auto-reconnect attempt.

**`POST /api/ble/pair`** and **`POST /api/ble/unpair`** require `device_address` in the JSON body.

### Communication

| Method | Endpoint                 | Description                        |
| ------ | ------------------------ | ---------------------------------- |
| POST   | `/api/ble/send`          | Send data to device                |
| POST   | `/api/ble/settime`       | Sync device clock to current time  |
| GET    | `/api/ble/notifications` | SSE notification stream            |
| GET    | `/api/ble/registers`     | Cached register values (see below) |

**`POST /api/ble/send`** supports four mutually-exclusive input modes (priority order):

| Field(s)            | Purpose               | Example                                |
| ------------------- | --------------------- | -------------------------------------- |
| `command`           | Device command (0xA0) | `{"command": "--pos"}`                 |
| `message` + `group` | Mesh message to group | `{"message": "Hello!", "group": "20"}` |
| `data_base64`       | Raw bytes (base64)    | `{"data_base64": "BBAQMA=="}`          |
| `data_hex`          | Raw bytes (hex)       | `{"data_hex": "04102030"}`             |

**`POST /api/ble/settime`** sends the current Unix timestamp to the device. No request body needed.

**`GET /api/ble/registers`** returns every register value cached from a cleanly-parsed `D{...}` notification frame so far:

```json
{
  "registers": {
    "I": { "TYP": "I", "callsign": "DK5EN-98" },
    "SN": { "TYP": "SN", "fw": "4.35p.07.24" }
  },
  "count": 2
}
```

- Always `200`. `registers: {}` / `count: 0` when nothing has been cached yet.
- Keyed by `TYP`; each value is the exact parsed JSON object last received for that `TYP` (the `TYP` field itself included), last-write-wins.
- Only registers the device actually sends a config value for are cached: `I`, `SN`, `G`, `SA`, `SE`, `S1`, `SW`, `S2`, `W`, `IO`, `TM`, `AN` (see "Extended Register Queries" below for which are auto-sent vs. query-only). `CONFFIN` (a burst-terminator marker, not a value) and `MH` (a rolling mheard list, not a stable register) are never cached.
- **Staleness is deliberate.** The cache is _not_ cleared on a plain disconnect — a value from before a dropped link is more useful than nothing, especially since this cache exists precisely to soften the case where the device's automatic post-hello register burst does not arrive at all (see `HELLO_SETTLE_DELAY_S` above). A value here can therefore be from any point since the last connect to this device, not necessarily the current connection.
- **The cache IS cleared the moment the connect target changes to a different MAC** (before the new connect attempt even starts) — a cached value from one node must never be attributed to another. Reconnecting to the _same_ device (auto-reconnect, or re-tapping the node you were already on) keeps the cache.
- Exists because the notification SSE stream (`/api/ble/notifications`) alone is not durable memory: its queue is bounded (`NOTIFICATION_QUEUE_SIZE`) and is lost across an mcapp restart, so a register that only arrives once per connect could be gone before anything ever reads it.

### Device Configuration

All config endpoints require an active BLE connection (409 if not connected). Changes are **not persisted** until `config/save` is called.

| Method | Endpoint                   | Params                                                                                 | Description                         |
| ------ | -------------------------- | -------------------------------------------------------------------------------------- | ----------------------------------- |
| POST   | `/api/ble/config/callsign` | `callsign` (1-9 chars)                                                                 | Set node callsign                   |
| POST   | `/api/ble/config/wifi`     | `ssid` (1-32 chars), `password` (0-63 chars)                                           | Set WiFi credentials                |
| POST   | `/api/ble/config/position` | `lat` (-90..90), `lon` (-180..180), `alt` (-1000..10000), `save` (bool, default false) | Set GPS position                    |
| POST   | `/api/ble/config/aprs`     | `primary` (1 char), `secondary` (1 char)                                               | Set APRS symbol                     |
| POST   | `/api/ble/config/save`     | none                                                                                   | Save to flash and **reboot device** |

Parameters are passed as query parameters.

**Warning:** `config/save` reboots the device immediately and disconnects BLE. `config/position` sends three separate BLE writes (lat, lon, alt) with 200ms delays between them.

```bash
# Set callsign
curl -X POST -H "X-API-Key: secret" \
  "http://pi.local:8081/api/ble/config/callsign?callsign=DL4GLE-10"

# Set position
curl -X POST -H "X-API-Key: secret" \
  "http://pi.local:8081/api/ble/config/position?lat=48.1234&lon=11.5678&alt=520"

# Save and reboot
curl -X POST -H "X-API-Key: secret" \
  http://pi.local:8081/api/ble/config/save
```

### SSE Notification Stream

Connect to `GET /api/ble/notifications` for real-time BLE data.

**Event types:**

| Event          | When                      | Data                                                   |
| -------------- | ------------------------- | ------------------------------------------------------ |
| `status`       | On initial SSE connection | `{"connected": bool, "state": "...", "timestamp": ms}` |
| `notification` | BLE data received         | See formats below                                      |
| `ping`         | Every 30s if idle         | `{"timestamp": ms}`                                    |

**Notification formats:**

All notifications include `timestamp`, `raw_base64`, and `raw_hex`. The `format` field indicates how the data was decoded:

- **`json`** — Data starting with `D{`. The `parsed` field contains the decoded JSON (e.g., `{"TYP": "MH", "CALL": "OE5HWN-12"}`).
- **`binary`** — Data starting with `@`. Includes `prefix` (first 2 bytes as ASCII).
- **`unknown`** — Anything else that doesn't match a known frame prefix; still forwarded as-is.
- **`raw`** — Two distinct cases. A `D{` register-update frame that failed to decode (truncated by the firmware's register-JSON producer clamp, or malformed) is logged loudly (ERROR for a truncated frame, WARNING for malformed) and **dropped** — never queued for SSE delivery, since there is no partial-register concept and nothing downstream consumes a discarded register update. Any other notification whose decoding raised an unexpected exception is still queued with `format: "raw"` so the raw bytes remain inspectable.

```bash
curl -N -H "X-API-Key: secret" \
  http://pi.local:8081/api/ble/notifications
```

## Connection Behavior

### Auto-Reconnect

On unexpected disconnect (detected during a failed write), the service automatically attempts to reconnect with exponential backoff: 5s, 10s, 20s, 60s (4 attempts). Auto-reconnect is cancelled if the user explicitly calls `/api/ble/disconnect`.

### Keepalive

While connected, the service sends a `--pos` command every 5 minutes to prevent the device from entering sleep mode.

### Extended Register Queries

After connecting, the service automatically queries `--io` (GPIO status) and `--tel` (telemetry config). The device auto-sends all other registers on BLE connect: I, SN, G, SA, SE+S1, SW+S2, W, AN.

### Connection States

The `state` field in status responses reflects the current connection lifecycle:

| State           | Description                                       |
| --------------- | ------------------------------------------------- |
| `disconnected`  | No active connection                              |
| `connecting`    | Connection attempt in progress                    |
| `connected`     | Active connection to device                       |
| `disconnecting` | Disconnect in progress                            |
| `error`         | Connection failed (cleared by calling disconnect) |

### Status/reason wire vocabulary (BLE-10)

Two additional values are layered on top of the five connection states above,
used only in SSE `status` events (`event: status`) pushed to `/api/ble/notifications`
and in the `reason` field of 409 responses. They are **not** part of the
`ConnectionState` enum — they describe the auto-reconnect process itself, not
BlueZ's connection state:

| Value                 | Where it appears                 | Meaning                                                                                          |
| --------------------- | -------------------------------- | ------------------------------------------------------------------------------------------------ |
| `reconnecting`        | SSE `status.state`, 409 `reason` | An auto-reconnect attempt is in progress (`attempt`/`max_attempts`/`next_retry_in` accompany it) |
| `reconnect_exhausted` | SSE `status.state`               | All reconnect attempts failed (`attempts`/`device_name` accompany it)                            |
| `busy`                | 409 `reason`                     | Another BLE operation (scan/connect/pair) holds the operation lock — not a reconnect             |

⚠ These are string constants (`STATUS_RECONNECTING`, `STATUS_RECONNECT_EXHAUSTED`,
`REASON_BUSY`) mirrored — not imported — between `ble_service/src/main.py` and
`src/mcapp/ble_client_remote.py`, since the two are separate processes with no
shared code. Changing a value on one side without the other is a wire-format
break.

## Error Codes

| Code | Meaning        | Example                                                                       |
| ---- | -------------- | ----------------------------------------------------------------------------- |
| 200  | Success        | Request completed                                                             |
| 400  | Bad Request    | Callsign too long, missing parameters                                         |
| 401  | Unauthorized   | Invalid or missing API key                                                    |
| 404  | Not Found      | Device name not found during connect scan                                     |
| 409  | Conflict       | Not connected, already connected, scan while connected, operation in progress |
| 500  | Internal Error | BLE adapter or D-Bus failure                                                  |

## Examples

### Scan for devices

```bash
curl -H "X-API-Key: secret" \
  "http://pi.local:8081/api/ble/devices?timeout=10&prefix=MC-"
```

### Connect to device

```bash
# By MAC address
curl -X POST -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d '{"device_address": "AA:BB:CC:DD:EE:FF"}' \
  http://pi.local:8081/api/ble/connect

# By name (auto-scans to resolve MAC)
curl -X POST -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d '{"device_name": "MC-ABCDEF"}' \
  http://pi.local:8081/api/ble/connect
```

### Send message

```bash
curl -X POST -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello Mesh!", "group": "20"}' \
  http://pi.local:8081/api/ble/send
```

### Send device command

```bash
curl -X POST -H "X-API-Key: secret" \
  -H "Content-Type: application/json" \
  -d '{"command": "--pos"}' \
  http://pi.local:8081/api/ble/send
```

## Environment Variables

| Variable                   | Default      | Description                                                         |
| -------------------------- | ------------ | ------------------------------------------------------------------- |
| `BLE_SERVICE_API_KEY`      | `""` (empty) | API key. Empty = unauthenticated. `"disabled"` = explicitly no auth |
| `BLE_SERVICE_PORT`         | `8081`       | Server port (only when running via `__main__`)                      |
| `BLE_SERVICE_CORS_ORIGINS` | `*`          | Comma-separated allowed CORS origins                                |

## Security Notes

- Always use a strong, unique API key in production
- The systemd service binds to `127.0.0.1` only — use a reverse proxy for remote access
- Consider using HTTPS via Caddy or similar reverse proxy
- Limit CORS origins in production
- The service requires D-Bus access to BlueZ
