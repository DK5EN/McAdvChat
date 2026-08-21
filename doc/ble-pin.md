# BLE PIN Authentication — Implementation Reference

Firmware commit: `bf05f9de` ("BLE Pin Checking"), upstream 2026-05-01.
MCProxy implementation: `ble_service/src/ble_adapter.py` + `ble_service/src/main.py`.

---

## 1. Overview

App-layer PIN authentication added to the BLE UART connection. After the physical
BLE link is established the client must prove knowledge of the PIN before the device
starts sending data. Failed auth → device forcibly disconnects the BLE link.

This is **not** BLE pairing-level security (NimBLE passkey). It is an additional
handshake inside the existing UART characteristic stream.

---

## 2. Default PIN after fresh flash

`bt_code = 0` — PIN disabled, open connection.

- ESP32: `preferences.getInt("bt_code", 0x000000)` — default 0.
- nRF52: struct field `int bt_code = 0`.

Device display shows `BLE-C: 000000` when no PIN is set.

---

## 3. Changing the PIN on the device

Serial / BLE command:

```
--btcode <number>
```

- Valid values: `100000`–`999999` to set a PIN; `0` or `000000` to disable.
- Persisted to flash immediately.
- **Always triggers an `I` JSON response** (`bInfo=true` is set unconditionally),
  including when disabling with `--btcode 0`.
- The `I` response contains the updated `BPIN` field confirming the new value.

---

## 4. Hello message protocol

### Without PIN (`bt_code == 0`)

```
Byte 0:  0x04   total length
Byte 1:  0x10   message type: Hello
Byte 2:  0x20   fixed
Byte 3:  0x30   fixed
```

### With PIN (`bt_code` 100000–999999)

```
Byte 0:  0x24   total length (36 = 4 header + 32 hash)
Byte 1:  0x10   message type: Hello
Byte 2:  0x20   fixed
Byte 3:  0x30   fixed
Byte 4–35:      SHA-256 hash (32 bytes)
```

Hash input: the PIN formatted as a zero-padded 6-digit ASCII decimal string.

```python
import hashlib

digest = hashlib.sha256(f"{pin:06d}".encode()).digest()
```

Example — PIN `123456`:
```
SHA-256(b"123456") = 8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92
```

The firmware (`phone_commands.cpp::hash_pin()`) uses identical logic:
```cpp
snprintf(pin_str, sizeof(pin_str), "%06u", (unsigned)pin_code);
// SHA-256 of pin_str[0..5]
```

On mismatch, or if the client sends the 4-byte hello without a hash when a PIN is
set (`msg_len < 35`), the firmware sets `ble_disconnect_requested = true` and
disconnects after the BLE RX callback returns.

---

## 5. `I` JSON — BPIN field

The device's `I` (info) JSON now includes `BPIN`:

```json
{ "TYP": "I", "CALL": "OE0XXX-9", ..., "BPIN": 123456 }
```

`BPIN` is the raw integer (`0` = disabled, `100000`–`999999` = active PIN).
The value is shown in the UI so users can reference it when configuring other clients.

---

## 6. Connection lifecycle (firmware change)

Before this commit `isPhoneReady = 1` and `config_to_phone_prepare = true` were set
on physical BLE connect — the device started sending data immediately. Now:

- **On BLE connect**: `isPhoneReady = 0`, `config_to_phone_prepare = false`.
  Device is silent until authenticated.
- **After successful hello auth**: `isPhoneReady = 1`, `config_to_phone_prepare = true`.
  Device begins sending config JSONs.
- **On disconnect**: both flags reset to `false/0` (fixes a prior bug where a stale
  push could occur after reconnect).

---

## 7. MCProxy implementation (done)

### `ble_service/src/ble_adapter.py`

`build_hello_bytes(pin: int) -> bytes` — public module-level function:

```python
def build_hello_bytes(pin: int) -> bytes:
    if pin > 0:
        digest = hashlib.sha256(f"{pin:06d}".encode()).digest()
        return bytes([0x24, 0x10, 0x20, 0x30]) + digest
    return b"\x04\x10\x20\x30"
```

`BLEAdapter.hello_bytes` is read on every `send_hello()` call — mutating it
takes effect on the next connection attempt with no restart needed.

### `ble_service/src/main.py`

**Persistence**: PIN is stored as `"ble_pin"` in `ble_state.json`
(at `BLE_STATE_FILE`, default `/var/lib/mcapp/ble_state.json`), the same file that
holds the last-connected MAC. `_save_ble_state()` preserves the `ble_pin` field
when it rewrites the file on every connect.

**Startup** (inside `lifespan`):
```python
_ble_pin = _load_ble_pin() or _BLE_PIN_ENV  # persisted state beats env var
ble_adapter = BLEAdapter(
    notification_callback=notification_callback,
    hello_bytes=build_hello_bytes(_ble_pin),
)
```

`BLE_SERVICE_BLE_PIN` env var sets a one-time default; after the first `PATCH /api/ble/pin`
call the persisted value in `ble_state.json` takes over permanently.

**Endpoint**:
```
PATCH /api/ble/pin
Content-Type: application/json

{ "pin": 123456 }
```

- `pin = 0` disables auth; `100000`–`999999` enables it.
- Protected by `Depends(verify_api_key)` (no-op when `BLE_SERVICE_API_KEY` not set —
  consistent with all other endpoints; the webapp does not send an auth header).
- Updates `_ble_pin`, persists via `_save_ble_pin()`, hot-reloads `ble_adapter.hello_bytes`.
- Returns `{"ok": true}` or HTTP 400 on invalid value.

---

## 8. Webapp frontend — what still needs to be built

### 8.1 Where BPIN lives

`bleStore.ts` stores the full `I` message object in `I: ref<BleMessage>({})`.
`BleMessage` has `[key: string]: unknown`, so `I.value.BPIN` already contains the
value as soon as the first `I` JSON arrives after connect. No store changes needed.

```typescript
// In BtNodeSettings.vue or a computed
const blePin = computed(() => {
  const v = I.value?.BPIN
  return typeof v === 'number' ? v : null
})
// blePin.value === 0  → disabled
// blePin.value > 0    → active, show the number
// blePin.value === null → not yet received (device not connected / old firmware)
```

### 8.2 How to send the command to the device

Use the existing pattern from `BtNodeSettings.vue`:
```typescript
sendQueueStore.enqueueMessage({ type: 'command', dst: 'TEST', msg: `--btcode ${pin}` })
```
For disable: `msg: '--btcode 0'`.

### 8.3 How to sync the proxy after device confirms

```typescript
const { patch } = useProxyAPI()
await patch('/api/ble/pin', { pin })
```

`useProxyAPI().patch()` already exists in `src/composables/useProxyAPI.ts` and needs
no changes. The endpoint is `/api/ble/pin` (note the `/api/` prefix — all BLE
service endpoints use this prefix).

### 8.4 Where to add the UI — `BtNodeSettings.vue`

Add a **BLE PIN section** as a separate `<div>` block **inside `.node-settings-card`**,
below the `<div class="settings-grid">` block and above the symbol picker overlay.
Do **not** put it inside `settingsGroups` — those render as flat field rows and have
no room for multi-button actions.

Structure to add in `<script setup>`:

```typescript
// PIN state
const pinInputValue = ref('')
const pinSaving = ref(false)
const showDisablePinConfirm = ref(false)

const blePin = computed(() => {
  const v = I.value?.BPIN
  return typeof v === 'number' ? v : null
})

async function onSetPin() {
  const pin = parseInt(pinInputValue.value, 10)
  if (!Number.isInteger(pin) || pin < 100000 || pin > 999999) {
    showToast('PIN must be a 6-digit number (100000–999999)', 'warning', 3000)
    return
  }
  pinSaving.value = true
  try {
    sendQueueStore.enqueueMessage({ type: 'command', dst: 'TEST', msg: `--btcode ${pin}` })
    // Device will respond with updated I JSON (BPIN updates automatically in store).
    // Then sync the proxy:
    const { patch } = useProxyAPI()
    await patch('/api/ble/pin', { pin })
    pinInputValue.value = ''
    showToast(`BLE PIN set to ${pin}`, 'success', 3000)
  } catch {
    showToast('PIN set on device, but proxy sync failed — reconnect may fail', 'error', 5000)
  } finally {
    pinSaving.value = false
  }
}

async function onDisablePin() {
  showDisablePinConfirm.value = false
  pinSaving.value = true
  try {
    sendQueueStore.enqueueMessage({ type: 'command', dst: 'TEST', msg: '--btcode 0' })
    const { patch } = useProxyAPI()
    await patch('/api/ble/pin', { pin: 0 })
    showToast('BLE PIN disabled', 'success', 3000)
  } catch {
    showToast('PIN disabled on device, but proxy sync failed — reconnect may fail', 'error', 5000)
  } finally {
    pinSaving.value = false
  }
}
```

Template block (add after `</div>` closing the `settings-grid`, before the symbol picker overlay):

```html
<!-- BLE PIN Management -->
<div class="pin-section">
  <div class="group-header">
    <span class="group-icon">🔐</span>
    <span class="group-title">BLE PIN</span>
  </div>
  <div class="pin-status">
    <span v-if="blePin === null" class="field-value placeholder">—</span>
    <span v-else-if="blePin === 0" class="field-value placeholder">not set (open)</span>
    <span v-else class="field-value pin-value">{{ blePin }}</span>
  </div>
  <div v-if="connected" class="pin-actions">
    <input
      v-model="pinInputValue"
      class="field-input pin-input"
      type="text"
      inputmode="numeric"
      pattern="[0-9]*"
      maxlength="6"
      placeholder="100000–999999"
    />
    <button class="pin-btn" :disabled="pinSaving" @click="onSetPin">
      {{ blePin && blePin > 0 ? 'Change' : 'Set PIN' }}
    </button>
    <button
      v-if="blePin && blePin > 0"
      class="pin-btn pin-btn--danger"
      :disabled="pinSaving"
      @click="showDisablePinConfirm = true"
    >
      Disable
    </button>
  </div>
</div>
```

Add a second `<BaseConfirmModal>` alongside the existing WiFi TX Power one:

```html
<BaseConfirmModal
  :visible="showDisablePinConfirm"
  title="Disable BLE PIN"
  message="Remove PIN protection? Any client will be able to connect without authentication."
  confirm-label="Disable PIN"
  variant="danger"
  @confirm="onDisablePin"
  @cancel="showDisablePinConfirm = false"
/>
```

Minimal CSS to add in `<style scoped>` (follow existing `.settings-group` / `.field-row` patterns):

```css
.pin-section {
  border-top: 1px solid var(--chat-bubble-received-border);
  padding: var(--spacing-sm) var(--spacing-md);
}

.pin-status {
  margin-bottom: var(--spacing-sm);
}

.pin-value {
  font-family: 'Courier New', Courier, monospace;
  font-weight: 600;
  letter-spacing: 0.1em;
}

.pin-actions {
  display: flex;
  gap: var(--spacing-sm);
  align-items: center;
  flex-wrap: wrap;
}

.pin-input {
  width: 130px;
  flex-shrink: 0;
}

.pin-btn {
  padding: var(--spacing-xs) var(--spacing-sm);
  background: var(--button-bg, rgba(255,255,255,0.08));
  border: 1px solid var(--chat-bubble-received-border);
  border-radius: var(--radius-sm);
  color: var(--chat-sender-color);
  cursor: pointer;
  font-size: 0.8rem;
  white-space: nowrap;
}

.pin-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.pin-btn--danger {
  color: var(--color-error, #e53935);
  border-color: var(--color-error, #e53935);
}
```

### 8.5 Timing note — proxy sync vs. device confirmation

The proxy `PATCH /api/ble/pin` call is made **concurrently** with enqueueing `--btcode`.
This is intentional: the proxy needs the new hash ready before any reconnect attempt,
and the device-side `--btcode` command is fire-and-forget from the webapp's perspective
(the `I` JSON confirmation updates the store automatically via the SSE stream, no
manual `await` needed). If the `PATCH` fails, the toast makes the out-of-sync state
visible to the user.

---

## 9. File reference

### Firmware (`bf05f9de`)

| File | Change |
|------|--------|
| `src/phone_commands.cpp` | `hash_pin()`, hello auth logic, `ble_disconnect_requested` |
| `src/esp32/esp32_main.cpp` | `g_ble_conn_handle`, state reset on connect/disconnect, disconnect loop |
| `src/nrf52/nrf52_ble.cpp` | state reset on connect/disconnect, disconnect after RX callback |
| `src/loop_functions.cpp` | `ble_disconnect_requested` declaration, `BLE-C:` display line |
| `src/loop_functions_extern.h` | extern declaration |
| `src/command_functions.cpp` | `--btcode 0` allowed, `BPIN` in info JSON, `bInfo` on btcode change |
| `src/esp32/esp32_flash.h` / `src/nrf52/WisBlock-API.h` | `int bt_code = 0` default |
| `src/esp32/esp32_flash.cpp` | persist/load `bt_code` via NVS, default `0x000000` |

### MCProxy

| File | Change |
|------|--------|
| `ble_service/src/ble_adapter.py` | `import hashlib`, `build_hello_bytes()` function |
| `ble_service/src/main.py` | `build_hello_bytes` import, `_BLE_PIN_ENV`, `_ble_pin` global, `_load_ble_pin()`, `_save_ble_pin()`, `_save_ble_state()` PIN preservation, lifespan PIN init, `SetPinRequest` model, `PATCH /api/ble/pin` endpoint |

### Webapp (pending)

| File | Change |
|------|--------|
| `src/components/bluetooth/BtNodeSettings.vue` | `blePin` computed, `onSetPin()`, `onDisablePin()`, PIN section template + CSS, second `BaseConfirmModal` |
