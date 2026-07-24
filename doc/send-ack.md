# Send-ACK: Message Delivery Tracking

Technical and implementation spec for end-to-end delivery status in MCProxy + webapp.

## Problem

When a user sends a message via `POST /api/send`, the frontend shows no delivery feedback. The backend returns `{"status": "ok"}` immediately (meaning: BLE write succeeded), but there is no indication whether the firmware actually transmitted it over LoRa, or whether another node received it.

The firmware provides two signals that MCProxy already receives but does not fully exploit:

1. **Self-echo**: After the firmware transmits a message over LoRa, it echoes the full APRS packet back over BLE — including the firmware-assigned `msg_id`. This confirms "on air."
2. **Binary ACK (0x41)**: When another node receives and acknowledges the message, the firmware sends a `0x41` frame referencing the original `msg_id`. This confirms "delivered."

## Current State

### What works

- `ble_protocol.py:decode_binary_message()` decodes the self-echo and extracts `msg_id`, `path`, `src`
- `ble_protocol.py:transform_ack()` decodes `0x41` ACK frames and extracts `ack_id`
- `sqlite_storage.py:store_message()` sets `send_success = 1` when an ACK's `ack_id` matches a stored message's `msg_id`
- `MessageRouter.my_callsign` holds the node's own callsign (uppercase)
- Frontend `ChatBubbleV2.vue` already renders two checkmark states:
  - Single check (`msg_www === true`): "Sent to server"
  - Double check (`msg_ack === true`): "Delivered"

### What's missing

| Gap | Location | Issue |
|-----|----------|-------|
| No outgoing message stored at send time | `sse_handler.py:338-395` | `POST /api/send` returns immediately, nothing written to DB |
| Self-echo not detected as own message | `sqlite_storage.py:store_message()` | Echo is stored as a new incoming message, no `src == my_callsign` check |
| No `msg_id` correlation between send and echo | `ble_adapter.py:695-711` | `send_message()` returns `bool`, firmware assigns `msg_id` later |
| ACK DB update is silent | `sqlite_storage.py:982-994` | `send_success` is updated but no SSE event notifies the frontend |
| Frontend has no real-time status updates | `messages.ts` | `msg_www` / `msg_ack` only set from initial data load, not from live events |

## Delivery State Model

```
 ┌─────────┐    BLE write OK     ┌─────────┐    self-echo RX     ┌─────────┐    0x41 ACK RX     ┌───────────┐
 │ QUEUED  │ ──────────────────► │  SENT   │ ──────────────────► │ ON AIR  │ ──────────────────► │ DELIVERED │
 └─────────┘                     └─────────┘                     └─────────┘                     └───────────┘
   (local)                     (BLE accepted)                (firmware TX'd)                  (remote node ACK'd)
                                                             msg_id now known
```

| State | DB column | Frontend field | Checkmark |
|-------|-----------|----------------|-----------|
| QUEUED | row exists, `send_status = 0` | `msg_www = false, msg_ack = false` | None (spinner) |
| SENT | `send_status = 1` | `msg_www = true` | Single ✓ (gray) |
| ON AIR | `send_status = 2`, `msg_id` populated | `msg_www = true` | Single ✓ (blue) |
| DELIVERED | `send_status = 3` | `msg_ack = true` | Double ✓✓ (green) |

## Implementation

### Overview of changes

```
MCProxy backend (4 files):
  sse_handler.py        — store outgoing message at send time, return DB id
  sqlite_storage.py     — new column send_status, echo-detection, SSE notify on status change
  main.py               — pass my_callsign to storage handler
  ble_client_remote.py  — (no change, already passes own_callsign to dispatcher)

Webapp frontend (3 files):
  useSSEClient.ts       — listen for new "msg:status" SSE event
  messages.ts           — update message in store on status change
  ChatBubbleV2.vue      — map send_status to checkmark rendering
```

---

### Backend Change 1: Schema migration — add `send_status` column

**File:** `MCProxy/src/mcapp/sqlite_storage.py`

**Where:** In the schema migration list (around line 560, where `send_success` was added).

Add migration:

```python
(("send_status", "INTEGER DEFAULT 0"),)
```

`send_status` values: `0` = queued, `1` = sent (BLE write OK), `2` = on air (echo received, `msg_id` known), `3` = delivered (ACK received).

This replaces the boolean `send_success` for new messages. Keep `send_success` for backward compat — set `send_success = 1` whenever `send_status` reaches 3.

---

### Backend Change 2: Store outgoing message at send time

**File:** `MCProxy/src/mcapp/sse_handler.py`

**Where:** In the `send_message` endpoint handler (lines 338-395), after the BLE send succeeds.

**Current flow:**
```python
# line ~382
await message_router.publish("ble", "ble_message", {"msg": msg, "dst": dst})
return {"status": "ok", "message": "Message queued for delivery"}
```

**New flow:**
```python
# After BLE send succeeds:
outgoing = {
    "src": message_router.my_callsign,
    "dst": dst,
    "msg": msg,
    "type": "msg",
    "timestamp": int(time.time() * 1000),
    "src_type": "self",
    "send_status": 1,  # SENT (BLE accepted)
}
db_id = await storage.store_outgoing_message(outgoing)

# Broadcast to frontend so message appears immediately
outgoing["id"] = db_id
await sse_manager.broadcast_event("mesh:message", outgoing)

return {"status": "ok", "id": db_id}
```

This gives the frontend an immediate message to render (with single gray checkmark), and a `db_id` to track status updates.

---

### Backend Change 3: Detect self-echo, correlate `msg_id`, update status

**File:** `MCProxy/src/mcapp/sqlite_storage.py`

**Where:** In `store_message()` (around line 933), before the INSERT.

**Logic:** When a BLE notification arrives where `src == my_callsign` and `type == "msg"`:
- This is a self-echo, not a new incoming message
- Extract the firmware-assigned `msg_id` from the decoded packet
- Find the most recent outgoing message with matching `dst` and `msg` text and `send_status < 2`
- UPDATE that row: set `msg_id` and `send_status = 2` (ON AIR)
- Do NOT insert a new row
- Publish status change via SSE

```python
async def _handle_self_echo(self, message: dict[str, Any]) -> int | None:
    """Detect self-echo and update outgoing message with firmware msg_id.

    Returns the DB row id if matched, None otherwise.
    """
    msg_id = message.get("msg_id")
    msg_text = message.get("msg", "")
    dst = message.get("dst", "")

    # Find the most recent outgoing message matching text + destination
    row = await self._execute(
        "SELECT id FROM messages "
        "WHERE src_type = 'self' AND send_status < 2 "
        "AND dst = ? AND msg = ? "
        "ORDER BY timestamp DESC LIMIT 1",
        (dst, msg_text),
    )
    if not row:
        return None

    db_id = row[0]["id"]
    await self._execute(
        "UPDATE messages SET msg_id = ?, send_status = 2 WHERE id = ?",
        (msg_id, db_id),
        fetch=False,
    )
    return db_id
```

**Call site** in `store_message()`:

```python
# At the top of store_message(), after extracting fields:
src = message.get("src", "")
if src.upper() == self._my_callsign.upper() and msg_type == "msg":
    db_id = await self._handle_self_echo(message)
    if db_id is not None:
        # Notify frontend of status change
        await self._publish_status_update(db_id, 2, message.get("msg_id"))
        return  # Don't insert duplicate row
```

The `_my_callsign` needs to be set on the storage instance. Pass it from `main.py` when creating the storage handler (it's already available as `message_router.my_callsign`).

---

### Backend Change 4: Update ACK handling to use `send_status` and notify frontend

**File:** `MCProxy/src/mcapp/sqlite_storage.py`

**Where:** The existing ACK handler (lines 982-994).

**Current:** Silently updates `send_success = 1`, no SSE event.

**New:**

```python
if msg_type == "ack":
    ack_id = message.get("ack_id")
    if ack_id:
        row = await self._execute(
            "SELECT id FROM messages WHERE msg_id = ? AND type = 'msg' "
            "ORDER BY timestamp DESC LIMIT 1",
            (ack_id,),
        )
        if row:
            db_id = row[0]["id"]
            await self._execute(
                "UPDATE messages SET send_status = 3, send_success = 1 WHERE id = ?",
                (db_id,),
                fetch=False,
            )
            await self._publish_status_update(db_id, 3, ack_id)
    return
```

---

### Backend Change 5: SSE status update event

**File:** `MCProxy/src/mcapp/sqlite_storage.py`

Add a method that publishes status changes through the message router:

```python
async def _publish_status_update(self, db_id: int, send_status: int, msg_id: str | None = None):
    """Publish delivery status change to SSE clients."""
    if self._message_router:
        await self._message_router.publish(
            "storage",
            "msg_status",
            {
                "id": db_id,
                "send_status": send_status,
                "msg_id": msg_id,
            },
        )
```

**File:** `MCProxy/src/mcapp/sse_handler.py`

Subscribe to the new event type (alongside existing subscriptions around line 99):

```python
message_router.subscribe("msg_status", self._status_handler)
```

Add handler:

```python
async def _status_handler(self, routed_message: dict[str, Any]) -> None:
    await self.broadcast_event("msg:status", routed_message["data"])
```

This sends a named SSE event:

```
event: msg:status
data: {"id": 42, "send_status": 2, "msg_id": "a3f8bc12"}
```

---

### Frontend Change 1: Listen for `msg:status` SSE event

**File:** `webapp/src/composables/useSSEClient.ts`

**Where:** In the event listener setup (around line 242-249), add:

```typescript
eventSource.addEventListener("msg:status", (e: MessageEvent) => {
    const data = JSON.parse(e.data)
    eventBus.emit("msg:status", data)
})
```

---

### Frontend Change 2: Update message store on status change

**File:** `webapp/src/stores/messages.ts`

**Where:** In the event bus listener setup, add handler for `msg:status`:

```typescript
eventBus.on("msg:status", (data: { id: number; send_status: number; msg_id?: string }) => {
    const msg = msgData.value.find(m => m.msg_id === data.id)
    if (msg) {
        msg.msg_www = data.send_status >= 1
        msg.msg_ack = data.send_status >= 3
        // Optional: store send_status directly for finer-grained UI
        msg.send_status = data.send_status
    }
})
```

Note: The `find` matches on the **database row id** (`msg_id` in the frontend Message type corresponds to the DB `id` column), not the firmware msg_id.

---

### Frontend Change 3: Render delivery states

**File:** `webapp/src/components/chat/ChatBubbleV2.vue`

**Where:** The existing checkmark rendering (lines 76-86).

**Current:**
- `msg_ack === true` → double check (green)
- `msg_www === true` → single check (gray)

**New** (using `send_status` if available, falling back to existing booleans):

```vue
<template>
  <!-- DELIVERED: double check, green -->
  <span v-if="message.msg_ack || message.send_status === 3"
        class="check delivered" title="Zugestellt">✓✓</span>
  <!-- ON AIR: single check, blue -->
  <span v-else-if="message.send_status === 2"
        class="check on-air" title="Gesendet (on air)">✓</span>
  <!-- SENT: single check, gray -->
  <span v-else-if="message.msg_www || message.send_status === 1"
        class="check pending" title="Gesendet">✓</span>
  <!-- QUEUED: clock icon or spinner -->
  <span v-else-if="message.send_status === 0"
        class="check queued" title="In Warteschlange">⏳</span>
</template>
```

Add CSS for the new `on-air` state:

```css
.check.on-air {
    color: var(--chat-check-onair, #2196F3); /* blue */
}
.check.queued {
    color: var(--chat-check-queued, #9E9E9E); /* gray */
}
```

---

### Frontend Change 4: Add `send_status` to Message type

**File:** `webapp/src/types/message.ts`

Add to the `Message` interface:

```typescript
send_status?: number  // 0=queued, 1=sent, 2=on-air, 3=delivered
```

## Data flow summary

```
User clicks Send
    │
    ▼
POST /api/send ──► BLE write ──► {"status": "ok", "id": 42}
    │
    ├──► INSERT messages (src_type='self', send_status=1)
    │
    ├──► SSE broadcast "mesh:message" {id:42, send_status:1, ...}
    │         │
    │         ▼
    │    Frontend adds message to store, shows gray ✓
    │
    ▼
Firmware TX's over LoRa, echoes back via BLE
    │
    ▼
BLE notification received, decoded, src == my_callsign
    │
    ├──► UPDATE messages SET msg_id=X, send_status=2 WHERE id=42
    │
    ├──► SSE broadcast "msg:status" {id:42, send_status:2}
    │         │
    │         ▼
    │    Frontend updates message, shows blue ✓
    │
    ▼
Remote node receives, sends 0x41 ACK referencing msg_id X
    │
    ▼
BLE notification received, decoded as ACK, ack_id matches msg_id X
    │
    ├──► UPDATE messages SET send_status=3, send_success=1 WHERE id=42
    │
    ├──► SSE broadcast "msg:status" {id:42, send_status:3}
    │         │
    │         ▼
    │    Frontend updates message, shows green ✓✓
    │
    ▼
Done — user sees full delivery confirmation
```

## Edge cases

| Case | Behavior |
|------|----------|
| BLE write fails | `send_message()` returns `False`. Don't insert row. Return error to frontend. |
| Echo never arrives (firmware crash) | Message stays at `send_status=1` (gray ✓). No timeout needed — user sees it's stuck. |
| ACK never arrives (remote node offline) | Message stays at `send_status=2` (blue ✓). User knows it was transmitted but not confirmed. |
| Echo text doesn't match (firmware modified payload) | `_handle_self_echo` match fails. Echo is stored as new incoming message (existing behavior). Outgoing stays at status 1. Acceptable — rare edge case. |
| Multiple messages with same text + dst | Match by most recent `send_status < 2` row. If ambiguous, the wrong row gets updated — acceptable for chat use case. Could add a nonce to the message text for stricter matching. |
| Mesh mode ON — message echoed by relay node | The relay echo has a different `src` (the relay node's callsign, not ours). Only the first path entry matches `my_callsign`. The `src` field from `split_path()` extracts the origin, so `src == my_callsign` still works. |
| Legacy messages without `send_status` | Frontend falls back to `msg_www` / `msg_ack` booleans (existing behavior). |

## Migration

The `send_status` column is added via the existing schema migration mechanism in `sqlite_storage.py`. Default value `0` means all existing messages show no delivery status (same as before). The `send_success` column is kept and set to `1` whenever `send_status` reaches `3`, preserving backward compat with any code that reads `send_success`.

## Testing

1. Send a message via `POST /api/send`. Verify:
   - Row inserted with `src_type='self'`, `send_status=1`
   - SSE event `mesh:message` received by frontend
   - Gray ✓ displayed
2. Wait for firmware echo (~2-5 s). Verify:
   - Same row updated: `send_status=2`, `msg_id` populated
   - SSE event `msg:status` with `send_status=2`
   - Blue ✓ displayed
3. If mesh mode ON or addressed message: wait for ACK. Verify:
   - Same row updated: `send_status=3`, `send_success=1`
   - SSE event `msg:status` with `send_status=3`
   - Green ✓✓ displayed
4. Send with BLE disconnected. Verify error returned, no DB row created.
5. Send two identical messages quickly. Verify each gets its own echo match.
