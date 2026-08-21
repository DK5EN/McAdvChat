# BLE State Machine and Message Flow Documentation

**Date:** 2026-02-14
**Version:** 1.0
**Related Files:** `src/mcapp/ble_handler.py`, `src/mcapp/main.py`

This document provides visual documentation of the BLE connection lifecycle, message flows, and state transitions in McApp.

---

## Table of Contents

1. [Connection State Machine](#connection-state-machine)
2. [Connection Sequence Diagram](#connection-sequence-diagram)
3. [Register Query Flow](#register-query-flow)
4. [Multi-Part Response Flow](#multi-part-response-flow)
5. [Message Send Flow](#message-send-flow)
6. [Error Handling Flow](#error-handling-flow)
7. [Disconnect Flow](#disconnect-flow)

---

## Connection State Machine

The BLE connection goes through several states from initial discovery to ready for communication.

```mermaid
stateDiagram-v2
    [*] --> Disconnected

    Disconnected --> Scanning: User initiates scan
    Scanning --> Disconnected: Scan timeout
    Scanning --> DeviceFound: Device discovered

    DeviceFound --> Pairing: User selects device
    Pairing --> Disconnected: Pairing failed
    Pairing --> Connecting: Pairing successful

    Connecting --> Disconnected: Connection timeout (10s)
    Connecting --> Connected: Device connected

    Connected --> ServicesResolving: Wait for GATT services
    ServicesResolving --> Disconnected: Timeout (10s)
    ServicesResolving --> CharacteristicsFound: Services discovered

    CharacteristicsFound --> NotifyStarted: Enable notifications
    NotifyStarted --> HelloSent: Send 0x10 hello
    HelloSent --> WaitingHello: Delay 1.0s for handshake

    WaitingHello --> QueryingRegisters: Hello complete
    QueryingRegisters --> Ready: All queries done (~7s)

    Ready --> Ready: Send/receive messages
    Ready --> Disconnected: User disconnect / error

    Disconnected --> [*]

    note right of Connecting
        Timeout: BLE_CONNECT_TIMEOUT (10s)
        D-Bus call: Device1.Connect()
    end note

    note right of QueryingRegisters
        Critical: --info, --nodeset, --pos, --aprsset
        Extended: --seset, --wifiset, --weather, --analogset
        Total delay: ~7.2s
    end note

    note right of Ready
        Keep-alive: Every 30s
        Notifications active
        Can send A0/binary messages
    end note
```

### State Descriptions

| State | Description | Timeout | Implementation |
|-------|-------------|---------|----------------|
| **Disconnected** | No active connection | - | Initial state |
| **Scanning** | Discovering nearby BLE devices | User-defined | BlueZ adapter scan |
| **DeviceFound** | Device discovered, awaiting user action | - | Device MAC known |
| **Pairing** | Bluetooth pairing in progress | User-defined | BlueZ pairing agent |
| **Connecting** | Establishing BLE connection | 10.0s | `BLEClient.connect()` |
| **Connected** | Physical connection established | - | `Device1.Connected = true` |
| **ServicesResolving** | GATT service discovery | 10.0s | Polling `ServicesResolved` every 0.5s |
| **CharacteristicsFound** | Read/write UUIDs found | - | GATT characteristics cached |
| **NotifyStarted** | Notifications enabled | - | `GattCharacteristic1.StartNotify()` |
| **HelloSent** | 0x10 hello message sent | - | 4-byte handshake: `\x04\x10\x20\x30` |
| **WaitingHello** | Delay for firmware processing | 1.0s | `BLE_HELLO_WAIT` constant |
| **QueryingRegisters** | Fetching device config | ~7.2s | 8 register queries with delays |
| **Ready** | Fully operational | - | Can send/receive all message types |

---

## Connection Sequence Diagram

Detailed sequence of operations during BLE connection from user action to ready state.

```mermaid
sequenceDiagram
    actor User
    participant Frontend
    participant MessageRouter
    participant BLEClient
    participant BlueZ
    participant Device as ESP32 Device

    User->>Frontend: Click "Connect BLE"
    Frontend->>MessageRouter: POST /api/ble/connect
    MessageRouter->>BLEClient: connect(mac, retries=3)

    Note over BLEClient: Attempt 1/3

    BLEClient->>BlueZ: Device1.Connect()
    activate BlueZ
    BlueZ->>Device: BLE connection request
    Device-->>BlueZ: Connection accepted
    BlueZ-->>BLEClient: Connected = true
    deactivate BlueZ

    BLEClient->>BLEClient: Wait for services (10s timeout)
    loop Every 0.5s
        BLEClient->>BlueZ: Get ServicesResolved property
        BlueZ-->>BLEClient: ServicesResolved = true/false
    end

    Note over BLEClient: Services discovered

    BLEClient->>BlueZ: Find GATT characteristics
    BlueZ-->>BLEClient: Read UUID, Write UUID

    BLEClient->>BlueZ: StartNotify(Read UUID)
    BlueZ-->>BLEClient: Notifications enabled

    BLEClient->>Device: 0x10 Hello (4 bytes)
    Note over Device: Process hello handshake

    BLEClient->>BLEClient: Delay 1.0s (BLE_HELLO_WAIT)

    Note over BLEClient,Device: Register Query Phase (7.2s)

    BLEClient->>Device: --info (TYP: I)
    Device-->>BLEClient: Device info JSON
    BLEClient->>BLEClient: Delay 0.8s

    BLEClient->>Device: --nodeset (TYP: SN)
    Device-->>BLEClient: Node settings JSON
    BLEClient->>BLEClient: Delay 0.8s

    BLEClient->>Device: --pos (TYP: G)
    Device-->>BLEClient: GPS position JSON
    BLEClient->>BLEClient: Delay 0.8s

    BLEClient->>Device: --aprsset (TYP: SA)
    Device-->>BLEClient: APRS settings JSON
    BLEClient->>BLEClient: Delay 0.8s

    BLEClient->>Device: --seset (TYP: SE + S1)
    Device-->>BLEClient: Sensor settings (SE)
    Note over Device: 200ms internal delay
    Device-->>BLEClient: Extended sensor (S1)
    BLEClient->>BLEClient: Delay 1.2s

    BLEClient->>Device: --wifiset (TYP: SW + S2)
    Device-->>BLEClient: WiFi settings (SW)
    Note over Device: 200ms internal delay
    Device-->>BLEClient: Extended WiFi (S2)
    BLEClient->>BLEClient: Delay 1.2s

    BLEClient->>Device: --weather (TYP: W)
    Device-->>BLEClient: Weather data JSON
    BLEClient->>BLEClient: Delay 0.8s

    BLEClient->>Device: --analogset (TYP: AN)
    Device-->>BLEClient: Analog config JSON
    BLEClient->>BLEClient: Delay 0.8s

    Note over BLEClient: Connection Ready

    BLEClient->>MessageRouter: publish('ble_status', 'connected')
    MessageRouter->>Frontend: SSE: connection_state = CONNECTED
    Frontend->>User: Show "Connected" status

    loop Every 30s
        BLEClient->>Device: Keep-alive message
    end
```

### Timing Summary

| Phase | Duration | Details |
|-------|----------|---------|
| Connection | ~2-5s | BLE connection + service discovery |
| Hello Handshake | 1.0s | Mandatory delay before queries |
| Critical Queries | 3.2s | 4 queries × 0.8s each |
| Extended Queries | 4.0s | 2 multi-part (1.2s) + 2 standard (0.8s) |
| **Total** | **~7-11s** | From "Connect" click to ready |

---

## Register Query Flow

Detailed flow of the register query process showing critical vs extended queries.

```mermaid
flowchart TD
    Start([BLE Connected]) --> HelloWait{Wait for<br/>hello?}

    HelloWait -->|Yes| WaitDelay[Delay 1.0s<br/>BLE_HELLO_WAIT]
    HelloWait -->|No| TimeSync
    WaitDelay --> TimeSync

    TimeSync{Sync time?} -->|Yes| SendTime[Send --settime<br/>0x20 message]
    TimeSync -->|No| CriticalStart
    SendTime --> CriticalStart

    CriticalStart[Start Critical Queries] --> Q1

    Q1[--info<br/>TYP: I] --> D1[Delay 0.8s]
    D1 --> Q2[--nodeset<br/>TYP: SN]
    Q2 --> D2[Delay 0.8s]
    D2 --> Q3[--pos<br/>TYP: G]
    Q3 --> D3[Delay 0.8s]
    D3 --> Q4[--aprsset<br/>TYP: SA]
    Q4 --> D4[Delay 0.8s]

    D4 --> ExtendedStart[Start Extended Queries]

    ExtendedStart --> Q5[--seset<br/>TYP: SE + S1<br/>Multi-part]
    Q5 --> D5[Delay 1.2s<br/>BLE_QUERY_DELAY_MULTIPART]
    D5 --> Q6[--wifiset<br/>TYP: SW + S2<br/>Multi-part]
    Q6 --> D6[Delay 1.2s]
    D6 --> Q7[--weather<br/>TYP: W]
    Q7 --> D7[Delay 0.8s<br/>BLE_QUERY_DELAY_STANDARD]
    D7 --> Q8[--analogset<br/>TYP: AN]
    Q8 --> D8[Delay 0.8s]

    D8 --> Complete([Ready for Use])

    style Q1 fill:#90EE90
    style Q2 fill:#90EE90
    style Q3 fill:#90EE90
    style Q4 fill:#90EE90
    style Q5 fill:#FFD700
    style Q6 fill:#FFD700
    style Q7 fill:#87CEEB
    style Q8 fill:#87CEEB

    style Complete fill:#00FF00
```

**Legend:**
- 🟢 Green: Critical queries (always run)
- 🟡 Gold: Multi-part queries (SE+S1, SW+S2)
- 🔵 Blue: Extended queries (optional, can fail)

---

## Multi-Part Response Flow

Some queries trigger two separate notifications sent sequentially by the device.

```mermaid
sequenceDiagram
    participant Client as BLE Client
    participant Device as ESP32 Device
    participant Dispatcher
    participant Frontend

    Note over Client,Device: Multi-Part Query Example: --seset

    Client->>Device: --seset command (A0 message)

    Note over Device: Prepare SE response<br/>(sensor settings)
    Device->>Client: Notification 1: TYP: SE
    Client->>Dispatcher: decode_json_message(SE)
    Dispatcher->>Frontend: SSE event: SE data

    Note over Device: Internal delay ~200ms<br/>Prepare S1 response

    Device->>Client: Notification 2: TYP: S1
    Client->>Dispatcher: decode_json_message(S1)
    Dispatcher->>Frontend: SSE event: S1 data

    Client->>Client: Delay 1.2s total<br/>(BLE_QUERY_DELAY_MULTIPART)

    Note over Frontend: Frontend receives<br/>SE and S1 as<br/>separate events

    rect rgb(255, 240, 200)
    Note over Client,Frontend: Important: No correlation in backend!<br/>SE and S1 processed independently.<br/>Frontend must merge if needed.
    end
```

### Multi-Part Pairs

| Command | Part 1 | Part 2 | Delay | Description |
|---------|--------|--------|-------|-------------|
| `--seset` | SE | S1 | 1.2s | Sensor settings + extended sensor data |
| `--wifiset` | SW | S2 | 1.2s | WiFi settings + extended WiFi data |

**Key Points:**
1. Device sends TWO separate BLE notifications
2. ~200ms internal delay between parts
3. Backend processes each independently
4. Both published as separate SSE events
5. Frontend responsible for merging if needed
6. Client uses 1.2s delay (longer than single queries) to ensure both parts arrive

---

## Message Send Flow

Flow for sending messages from user to the mesh network.

```mermaid
flowchart TD
    Start([User sends message]) --> ValidateConn{BLE<br/>connected?}

    ValidateConn -->|No| Error1[Log: Not connected<br/>Return]
    ValidateConn -->|Yes| CheckConn[_check_conn<br/>Verify connection state]

    CheckConn --> DetermineType{Message<br/>type?}

    DetermineType -->|Text message| BuildA0[Build 0xA0 message<br/>group + text]
    DetermineType -->|Command| ParseCmd{Command<br/>type?}

    ParseCmd -->|--settime| Build20[Build 0x20<br/>timestamp]
    ParseCmd -->|--setcall| Build50[Build 0x50<br/>callsign]
    ParseCmd -->|--setssid/pwd| Build55[Build 0x55<br/>WiFi config]
    ParseCmd -->|--setlat| Build70[Build 0x70<br/>latitude + flag]
    ParseCmd -->|--setlon| Build80[Build 0x80<br/>longitude + flag]
    ParseCmd -->|--setalt| Build90[Build 0x90<br/>altitude + flag]
    ParseCmd -->|--setsym| Build95[Build 0x95<br/>APRS symbols]
    ParseCmd -->|--save/reboot| BuildA0Cmd[Build 0xA0<br/>text command]

    BuildA0 --> ValidateMTU
    Build20 --> ValidateMTU
    Build50 --> ValidateMTU
    Build55 --> ValidateMTU
    Build70 --> ValidateMTU
    Build80 --> ValidateMTU
    Build90 --> ValidateMTU
    Build95 --> ValidateMTU
    BuildA0Cmd --> ValidateMTU

    ValidateMTU{Length ><br/>247 bytes?} -->|Yes| ErrorMTU[Log error<br/>Publish status<br/>Raise ValueError]
    ValidateMTU -->|No| TrySend

    TrySend[Try: Write to GATT<br/>characteristic] --> WriteSuccess{Success?}

    WriteSuccess -->|Yes| PublishOK[Publish status: OK<br/>Log success]
    WriteSuccess -->|No| CatchError[Catch Exception<br/>Log error<br/>Publish status: error<br/>Raise]

    PublishOK --> End([Message sent])
    ErrorMTU --> End
    Error1 --> End
    CatchError --> End

    style ErrorMTU fill:#FF6B6B
    style CatchError fill:#FF6B6B
    style Error1 fill:#FF6B6B
    style PublishOK fill:#90EE90
```

### Message Format Reference

| Type | Msg ID | Format | Example |
|------|--------|--------|---------|
| Text Message | 0xA0 | `[LEN][0xA0][{GRP}MSG]` | Group chat |
| Set Time | 0x20 | `[LEN][0x20][TIMESTAMP]` | UNIX timestamp (4B LE) |
| Set Callsign | 0x50 | `[LEN][0x50][LEN][CALL]` | Length-prefixed string |
| WiFi Config | 0x55 | `[LEN][0x55][SSID_LEN][SSID][PWD_LEN][PWD]` | Two length-prefixed strings |
| Set Latitude | 0x70 | `[LEN][0x70][FLOAT][FLAG]` | 4B float (LE) + save flag |
| Set Longitude | 0x80 | `[LEN][0x80][FLOAT][FLAG]` | 4B float (LE) + save flag |
| Set Altitude | 0x90 | `[LEN][0x90][INT][FLAG]` | 4B signed int (LE) + flag |
| APRS Symbols | 0x95 | `[LEN][0x95][PRI][SEC]` | 2 bytes (table + symbol) |
| Save & Reboot | 0xF0 | `[LEN][0xF0]` | No payload |

---

## Error Handling Flow

Comprehensive error handling ensures frontend always receives status updates.

```mermaid
flowchart TD
    Start([BLE Operation]) --> TryBlock[Try:<br/>Execute operation]

    TryBlock --> OpSuccess{Operation<br/>succeeded?}

    OpSuccess -->|Yes| LogDebug[logger.debug<br/>Operation details]
    OpSuccess -->|No| Exception[Exception raised]

    Exception --> CatchBlock{Exception<br/>type?}

    CatchBlock -->|ValueError<br/>MTU error| AlreadyHandled[Already logged<br/>and published<br/>Re-raise]
    CatchBlock -->|DBusError<br/>Connection| LogError[logger.error<br/>Error details]
    CatchBlock -->|General| LogError

    LogError --> PublishStatus[_publish_status<br/>operation, 'error', msg]
    PublishStatus --> NotifyFrontend[SSE event to frontend<br/>Error notification]
    NotifyFrontend --> Reraise[Raise exception<br/>to caller]

    LogDebug --> Success([Operation complete])
    AlreadyHandled --> Caller([Return to caller])
    Reraise --> Caller

    style Exception fill:#FF6B6B
    style LogError fill:#FFA500
    style PublishStatus fill:#FFD700
    style Success fill:#90EE90
```

### Error Handling Pattern

All BLE operations follow this pattern:

```python
try:
    # BLE operation (write, read, etc.)
    await self.write_char_iface.call_write_value(byte_array, {})
    logger.debug("Operation succeeded: %s", context)

except ValueError:
    # MTU validation error - already logged
    raise

except Exception as e:
    logger.error("Operation failed: %s", e)
    await self._publish_status("operation", "error", f"❌ Failed: {e}")
    raise
```

### Status Publishing

All errors are published to the frontend via SSE:

| Operation | Status Types | Messages |
|-----------|--------------|----------|
| Connect | `info`, `error` | Connection progress, failures |
| Send Hello | `info`, `error` | Handshake status |
| Send Message | `ok`, `error` | Message sent or failed |
| Send Command | `ok`, `error` | Command execution status |

---

## Disconnect Flow

Graceful disconnection with cleanup.

```mermaid
sequenceDiagram
    actor User
    participant Frontend
    participant BLEClient
    participant BlueZ
    participant Device

    User->>Frontend: Click "Disconnect"
    Frontend->>BLEClient: disconnect()

    Note over BLEClient: Stop keep-alive task
    BLEClient->>BLEClient: Stop time sync task

    alt Notifications active
        BLEClient->>BlueZ: StopNotify(Read UUID)
        BlueZ-->>BLEClient: Notifications stopped
        BLEClient->>Frontend: Publish status: "Notify stopped"
    end

    BLEClient->>BLEClient: Delay 1.0s (disconnect cleanup)

    alt Connected to D-Bus
        BLEClient->>BlueZ: Device1.Disconnect()
        BlueZ->>Device: BLE disconnect
        Device-->>BlueZ: Disconnected
        BlueZ-->>BLEClient: Disconnected
    end

    BLEClient->>BLEClient: bus.disconnect()
    BLEClient->>BLEClient: Clear cached interfaces

    BLEClient->>Frontend: Publish status: "Disconnected"
    Frontend->>User: Show "Disconnected" status

    Note over BLEClient: State reset to Disconnected
```

### Cleanup Steps

1. Stop keep-alive task (if running)
2. Stop time sync task (if running)
3. Stop GATT notifications
4. Delay 1.0s for cleanup
5. Disconnect from device
6. Disconnect from D-Bus
7. Clear cached interfaces
8. Publish disconnection status
9. Reset state

---

## Implementation Reference

### Key Files

| File | Purpose | Key Functions |
|------|---------|---------------|
| `ble_handler.py` | BLE connection management | `connect()`, `send_hello()`, `send_message()` |
| `main.py` | Register query orchestration | `_query_ble_registers()`, `_send_ble_command_with_retry()` |
| `ble_client.py` | Abstraction interface | `create_ble_client()` factory |
| `ble_client_local.py` | Local D-Bus implementation | Wrapper around `ble_handler.py` |
| `ble_client_remote.py` | Remote HTTP/SSE client | For distributed deployments |

### Timing Constants

All timing constants are centralized:

**`ble_handler.py`:**
```python
BLE_CONNECT_TIMEOUT = 10.0  # Connection timeout
BLE_SERVICES_CHECK_INTERVAL = 0.5  # Service polling interval
BLE_KEEPALIVE_INTERVAL = 30.0  # Keep-alive frequency
BLE_HELLO_DELAY = 1.0  # Post-hello delay
BLE_DISCONNECT_DELAY = 2.0  # Pre-disconnect delay
```

**`main.py`:**
```python
BLE_HELLO_WAIT = 1.0  # Wait after hello
BLE_QUERY_DELAY_STANDARD = 0.8  # Standard query delay
BLE_QUERY_DELAY_MULTIPART = 1.2  # Multi-part query delay
BLE_RETRY_BASE_DELAY = 0.5  # Retry backoff base
```

---

## Troubleshooting

### Common Issues

| Issue | State | Cause | Solution |
|-------|-------|-------|----------|
| Connection timeout | Connecting | Device too far, interference | Retry, move closer |
| Services not resolved | ServicesResolving | Slow device, BLE stack issue | Increase timeout, restart BlueZ |
| Hello timeout | WaitingHello | Device not ready | Increase `BLE_HELLO_WAIT` |
| Query failures | QueryingRegisters | Command not supported | Check firmware version |
| MTU exceeded | Ready | Message too long | Split message or shorten |
| Connection drops | Ready | Signal weak, device reboot | Auto-reconnect on next send |

### Debug Tips

1. **Enable verbose logging**: Set `MCAPP_ENV=dev`
2. **Monitor D-Bus**: `dbus-monitor --system`
3. **Check BlueZ logs**: `journalctl -u bluetooth -f`
4. **Check service logs**: `journalctl -u mcapp.service -f`
5. **Frontend SSE stream**: Browser DevTools → Network → EventStream

---

## Future Enhancements

Potential improvements to the state machine:

1. **Automatic Reconnection**: Retry on connection loss
2. **State Persistence**: Resume state after app restart
3. **Queue Management**: Queue messages during disconnection
4. **Connection Pooling**: Multiple simultaneous devices
5. **Smart Retry**: Exponential backoff on connection failures
6. **Health Monitoring**: Periodic connection quality checks

---

**Document Maintenance:**
- Update diagrams when adding new states or transitions
- Keep timing constants synchronized with code
- Document any protocol changes from firmware updates
- Add new message types as implemented

**Related Documents:**
- `doc/ble-challenges.md` - Gap analysis and implementation status
- `doc/a0-commands.md` - Firmware protocol specification
- `doc/phase3-summary.md` - Phase 3 implementation details
