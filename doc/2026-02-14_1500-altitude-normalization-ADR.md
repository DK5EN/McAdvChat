# ADR: Normalize Altitude to Meters at UDP Ingestion

**Date:** 2026-02-14
**Status:** Accepted & Implemented
**Affects:** `udp_handler.py`, `sqlite_storage.py`, `ble_handler.py`

---

## 1. Problem Statement

The frontend positions page displayed many stations at ~3.28x their real altitude (e.g., DK5EN-12 showed 1637m instead of ~499m). The raw APRS `/A=` altitude value is in **feet**, but the frontend displays it as meters with no conversion.

The bug only manifested for stations that received a real-time WebSocket position update after the initial SSE load. Stations without a recent update showed the correct DB value.

---

## 2. Root Cause

Three ingestion paths feed altitude data into the system:

| Path | Source | Conversion before publish | DB conversion |
|------|--------|--------------------------|---------------|
| **UDP gateway** | LoRa mesh via MCProxy | None (raw feet) | `sqlite_storage.py` converted to meters |
| **BLE handler** | Direct Bluetooth to ESP32 | Converted to meters | Already meters |
| **Internet WS** | `wss://mcmap.oevsv.at/ws` | N/A (frontend only) | N/A |

The UDP path was the problem. In `udp_handler.py`, the raw message dict was published to all WebSocket/SSE subscribers unchanged. `sqlite_storage.py` created a **copy** of the dict and converted feet to meters for DB storage, but the **original dict** broadcast to real-time clients still contained feet.

**Data flow showing the bug:**

```
UDP packet (alt=1637 feet)
  ├─→ message_router.publish() → WebSocket/SSE → Frontend shows 1637m (WRONG)
  └─→ sqlite_storage.store()   → DB stores 499m (correct, converted on copy)
```

The frontend's initial SSE load retrieved the correct DB value (499m), but any subsequent real-time position update overwrote it with the unconverted feet value (1637).

---

## 3. Decision

**Normalize altitude from feet to meters in `udp_handler.py` before publishing**, so all downstream consumers (WebSocket, SSE, DB storage) receive meters consistently.

### Rejected alternatives

- **Convert in the frontend**: Would require the frontend to know which source sends feet vs meters. Violates single-responsibility — the backend should deliver consistent units.
- **Convert in `sqlite_storage.py` on the original dict**: Would mutate shared state and could cause side effects if other subscribers read the dict before storage runs.

---

## 4. Implementation

Added a module-level helper in `udp_handler.py`:

```python
def _normalize_altitude_to_meters(message: dict) -> None:
    """Convert APRS altitude from feet to meters in-place."""
    if message.get("alt"):
        message["alt"] = round(message["alt"] * 0.3048)
```

Called before both `publish()` sites:
- **Telemetry path** (type `tele`) — before `message_router.publish()`
- **Chat/position path** (has `msg` field) — before `message_router.publish()`

### Altitude conversion across all ingestion paths (after fix)

| Path | Conversion point | Unit at publish |
|------|-----------------|-----------------|
| UDP gateway | `udp_handler._normalize_altitude_to_meters()` | meters |
| BLE handler | `ble_handler.py` line 363 | meters |
| DB storage | `sqlite_storage.py` fallback (only if `alt` missing) | meters |

The `sqlite_storage.py` fallback conversion (parsing `/A=` from APRS text) is retained as a safety net — it only triggers when `alt` is absent in the dict, so there is no double-conversion risk.

---

## 5. Verification

| Station | Before (feet shown as m) | After (correct meters) |
|---------|--------------------------|----------------------|
| DK5EN-12 | 1637m | ~499m |
| DG6TOM-11 | 1752m | ~534m |
| DO1PIT-99 | 1903m | ~580m |

Stations that had no recent real-time update (e.g., DL2JA-2 at 539m) were unaffected — they already showed the correct DB value.

---

## 6. Follow-up: Double Conversion Cleanup (Schema v9)

**Date:** 2026-02-14

### Problem

Between commit `9f85f42` (09:22, added `_normalize_altitude_to_meters()` in `udp_handler.py`) and commit `417dc52` (10:11, removed duplicate conversion in `sqlite_storage.py`), altitude values were converted **twice** (feet→meters→meters×0.3048). This left stale double-converted values in `station_positions`:

| Station | Stored alt | Expected alt | Ratio |
|---------|-----------|-------------|-------|
| DK5EN-99 | 146m | ~480m | 0.304 |
| DK5EN-12 | 152m | ~500m | 0.304 |
| DL2JA-1 | 152m | ~500m | 0.304 |

Stations updated after the service restart at 10:12 were correct (e.g., DL7OSX-1 at 516m, DB0ED-99 at 526m).

### Fix

Schema v9 migration resets all altitude values to NULL:

```python
if current_version < 9:
    updated = conn.execute("UPDATE station_positions SET alt = NULL WHERE alt IS NOT NULL").rowcount
```

Altitudes self-correct within ~30 minutes as new position beacons arrive from all three ingestion paths (UDP, BLE local, BLE remote), all of which now deliver meters correctly.
