# ADR: Link Check (firmware `{ping}` / `{pong}`) in McApp

**Date:** 2026-08-13
**Status:** Accepted, not implemented
**Affects:** `storage/ingest.py`, `commands/linkcheck.py` (new), `sse_routes/linkcheck.py` (new), `main.py`, frontend `webapp`
**Implementation plan:** `doc/2026-08-13_1500-linkcheck-implementation-plan.md`
**Review:** `doc/2026-08-13_1500-linkcheck-verdict.md` — this ADR's first draft contained two
design-breaking protocol errors; both are corrected here and recorded there.
**Source:** <https://icssw.org/2026/07/24/ping-check/>, firmware `v4.35p.07.24.2`+

---

## 1. Context

MeshCom firmware `v4.35p.07.24.2` added a "PING check": a node can probe whether another node is
reachable **on direct RF**, and the operator gets sequence number, round-trip time and signal
quality on the node's own display.

The published article documents only the console commands. It does not document the wire format,
does not say the exchange is a plain DM, and does not mention that the result never reaches a
connected phone. Everything below was read out of the firmware source in
`/Users/martinwerner/WebDev/MeshCom-Firmware-DEV-Main` (branch `v4.35p_prio`).

**Scope of the upstream comparison:** the ping/pong logic and payload strings match
`upstream/oe1kbc_v4.35p` HEAD (`3e8ffccc`), and the four upstream commits we are behind touch GPS,
track mode and `--setlog` only. The **files** are not identical — `loop_functions.cpp`,
`lora_functions.cpp`, `extudp_functions.cpp` and `command_functions.cpp` differ from upstream by
26/42/20/5 added lines respectively. All line numbers below are **local**.

### 1.1 Console commands (node-local)

| Command             | Effect                                                  | Source                       |
| ------------------- | ------------------------------------------------------- | ---------------------------- |
| `--pingcall <call>` | target callsign; empty clears, non-empty arms the timer | `command_functions.cpp:3300` |
| `--pingtime <s>`    | repeat interval; `<15` or `>300` is coerced to 60       | `command_functions.cpp:3340` |
| `--pingmax <n>`     | max attempts, `1..5`; out of range coerced to 5         | `command_functions.cpp:3384` |
| `--pingmax max`     | sets 100                                                | `command_functions.cpp:3375` |
| `--ping start`      | `node_pingcount = node_pingmax`                         | `command_functions.cpp:3363` |
| `--ping stop`       | `node_pingcount = 0`                                    | `command_functions.cpp:3370` |

`PING_INTERVAL = 60`, `PING_MAX = 5` (`configuration_global.h:72-73`).

Two deviations from the article, both confirmed in source:

- The article gives the `--pingtime` range as 30-300. The setter accepts 15-300, but the scheduler
  only fires at `node_pingtime > 29` (`esp32_main.cpp:3707`). **Values 15..29 are accepted and then
  silently ignored.**
- `--pingmax max` yields 100, not 99.

### 1.2 Wire format

There is no new packet type. Both directions are ordinary `MSG_TYPE_TEXT` DMs with a magic
payload:

| Direction                | Payload on air          | Built at                  |
| ------------------------ | ----------------------- | ------------------------- |
| Ping, node's own timer   | `{ping}`                | `loop_functions.cpp:3103` |
| Ping, sent through McApp | `{ping}{NNN`            | `loop_functions.cpp:3395` |
| Pong                     | `{pong}{<ping_msg_id>}` | `loop_functions.cpp:3184` |

**The ACK suffix has no closing brace.** `sendMessage()` appends `"{" + %03i` to every DM
(`loop_functions.cpp:3395`), so a ping we originate is `{ping}{042` on the wire. All matching must
be prefix-based; an equality test against `"{ping}"` fails.

### 1.3 The correlation token — two representations, neither obvious

This is where the first draft of this ADR was wrong, twice. Both errors would have produced a
feature that silently never correlates.

**(a) The Extern-UDP `msg_id` field is hex; the pong payload is decimal.**
`extudp_functions.cpp:365` formats it as `snprintf(_msgId, sizeof(_msgId), "%08X", …)` and assigns
it at `:505`, so every Extern-UDP frame carries an **8-digit uppercase hex string**
(`"1AE1E0C4"`). The pong embeds the same 32-bit value in **decimal**. McApp already reflects this
elsewhere — `messages.msg_id` is `TEXT` (`migrations.py:557`).

**(b) About half of all nodes emit a negative pong id.** `SendPong(String, unsigned int msg_id)`
(`loop_functions.h:63`) is formatted with the **signed** `%i` (`loop_functions.cpp:3184`). The id
is `((_GW_ID & 0x3FFFFF) << 10) | (node_msgid & 0x3FF)`, occupying bits 10-31, so bit 31 is set
whenever bit 21 of `_GW_ID` is — deterministic per node, roughly half the fleet. Those nodes send
`{pong}{-1234567890}`.

The firmware contradicts itself on this: the ping display reads `cmsg[7..9]`
(`loop_functions.cpp:3138`) while the pong display reads `substring(14,17)` (`:2095`) — the two
disagree once the id is 11 characters.

**Consequence for us:** normalise both sides to unsigned 32-bit before comparing.

```
int(echo_msg_id_hex, 16) & 0xFFFFFFFF  ==  pong_decimal & 0xFFFFFFFF
```

Python's `&` on a negative int yields the two's-complement value, so this is exact for both signs.

### 1.4 Behaviour that matters for us

1. **Nearly every node is a responder — but not all.** `lora_functions.cpp:756` answers any
   `{ping}` addressed to our callsign with `SendPong()`. There is no configuration for it, but
   there **is** an opt-out: `SendPong()` early-returns when `bDisplayTrack` is set
   (`loop_functions.cpp:3163-3165`), a one-button toggle (`onebutton_functions.cpp:219`). A node in
   track mode never answers — and those are exactly the mobile stations a link test is most
   interesting for.
2. **RF-direct only.** Foreign ping/pong set `bMeshDestination = false` and
   `bSendAckGateway = false` (`lora_functions.cpp:883/889`), and `:1137` additionally suppresses
   meshing for a `{ping}` to the telemetry group `100001`. Nothing is forwarded into the mesh and
   nothing reaches the MeshCom server. Group, `*` and `100001` destinations are never answered.
3. **`queueExtern()` runs before any ping/pong filtering.** `lora_functions.cpp:701` queues
   received frames to Extern-UDP, behind three gates: `bEXTUDP`, `is_new_packet()`, and a
   TEXT/POS/HEY type check. Ping and pong therefore arrive at McApp as ordinary `type:"msg"`
   frames carrying `src`, `dst`, `msg_id`, `firmware`, **`rssi` and `snr`**.
4. **The phone never sees a pong.** The `{pong}` branch at `lora_functions.cpp:773` calls
   `queueDisplayText()` and clears `bPingSend`, but never `addBLEOutBuffer()`. On BLE the exchange
   is invisible; it exists only on the OLED.
5. **A ping we originate is echoed back to us with its `msg_id`.** The Extern-UDP inbound handler
   (`extudp_functions.cpp:282`) wraps the payload as `:{dst}payload` and calls `sendMessage()`,
   which echoes to Extern-UDP as `src_type:"node"` (`loop_functions.cpp:3502`). The echo is a
   **synchronous** call that completes before the frame is queued for TX, so the echo always
   precedes the pong — this is a property of the single-threaded main loop, not a protocol
   guarantee, but there is no reordering path.
6. **The node's own timer-driven ping is _not_ echoed.** Neither `sendPing()` nor `SendPong()`
   calls `sendExtern()`. We cannot observe pings the node sends on its own `--pingcall` timer, nor
   our own automatic pong replies.
7. **A ping we originate is retransmitted three further times.** `sendMessage()` arms
   retransmission for any text DM whose payload does not start with `{CET}`, `{MCP}` or `{SET}`
   (`loop_functions.cpp:3466-3472`); `{ping}` is not in that list. Retransmission is cancelled by a
   DM-ACK, and a responder answers with a pong, never an ACK — so nothing cancels it. **One attempt
   is about four keyings over two minutes, and can elicit up to four pongs sharing one correlation
   id.** The node's own `sendPing()` sets `0xFF` (no retransmission) and does not do this; only our
   path does. This block is local-fork code and may differ upstream.
8. **We cannot ping ourselves.** `sendMessage()` hard-refuses a DM to our own callsign
   (`loop_functions.cpp:3366-3372`, `[ERROR]...DM to own-all not allowed`). A self-test produces no
   transmission at all.

---

## 2. Problem

**(a) There is a small bug today.** Point 3 means a `{ping}`/`{pong}` exchange between two _other_
stations within our RF range is already ingested and stored, and shows up in the webapp as garbage
chat (`{pong}{1234567890}` from OE1XYZ-12 to OE3ABC-7). This is live now on every box with
Extern-UDP enabled, without anyone having asked for the feature.

**(b) There is a real feature available to us that the official app cannot have.** Points 3, 5 and
6 together mean McApp can run the whole exchange itself over Extern-UDP — transmit the ping, learn
its `msg_id` from the echo, match the pong, and report RTT plus the signal quality of the reply.
The node-side `--pingcall` mechanism is not needed and would not help.

---

## 3. Decision

Implement link check as an **McApp-driven** feature over Extern-UDP, in four stages, and explicitly
reject the node-side control path.

### 3.1 Stage 0 — drop ping/pong at the ingest filter

Hard-drop `{ping}`/`{pong}` payloads in `storage/ingest.py`'s `_should_filter_message()`
(`:1463`), exactly as `{CET}` is dropped there today (`:1469`). One function, one file, one repo.

Rejected alternative: classifier rules. The first draft proposed that, which would have meant
editing the mc-chat subtree, splitting, pulling, bumping `classifier_ver`, backfilling and
re-running a parity corpus across two repos — to categorise frames that are protocol, not chat.
`_should_filter_message` is where this codebase already puts firmware magic payloads. Dropping
rather than tagging also removes the disk-growth vector in §4.2 entirely.

Filtering suppresses **persistence only**; the router still publishes the frame, which is what
Stage 2 subscribes to.

### 3.2 Stage 1 — feed pong signal into the existing signal architecture

A pong's `rssi`/`snr` is precisely the "signal at which we heard station X" measurement that
`signal_log` already models, including the `source TEXT` column added for exactly this kind of
distinction (`migrations.py:322-325`, values `'mheard'`/`'lora'`). Record link-check replies there
with `source='linkcheck'`.

**No new table and no schema migration.** `doc/UDP-2.0-impl.md`'s own principle — "generalize,
don't fork; the same physical measurement must feed the same tables" — applies directly. A separate
`link_checks` table, as first drafted, would also have been invisible to the existing map and
mHeard views.

The first draft justified that table as "storage Stage 2 needs anyway". It does not: Stage 2's
correlation state is in-memory.

### 3.3 Stage 2 — active ping from McApp

A new `CommandHandler` mixin alongside `commands/ctcping.py`, reusing its session machinery rather
than reimplementing it. `POST /api/linkcheck` starts a session; the ping goes out via
`message_router.publish(...)`; the `src_type:"node"` echo supplies the `msg_id`; the inbound pong
is matched per §1.3; results stream over SSE.

`CTCPingMixin` already provides the shape — `ActivePing`/`PingTest`, a `PingStatus` state machine,
a session registry, an injectable `ping_timeout`, tracked background tasks, repeat caps, callsign
and blocklist validation — including fixes for races that already bit once in production. Building
a second registry from scratch was the first draft's plan and is rejected.

### 3.4 Stage 3 — webapp UI

"Link check" action per station, live result strip, and a view of link-check signal history. Lands
in the separate `webapp` repo.

### 3.5 Rejected — remote control of the node's own ping mode

Driving `--pingcall` / `--ping start` from McApp is rejected. The Extern-UDP inbound path only
emits LoRa DMs (`extudp_functions.cpp:282` hardcodes `:{dst}payload`); it cannot carry console
commands. Even over BLE, where commands could be sent, the results would be unreachable: §1.4
point 4 (no pong to the phone) and point 6 (no Extern-UDP echo of timer-driven pings). Stage 2
delivers strictly more for less.

---

## 4. Consequences

### 4.1 Positive

- Works with the firmware already deployed on both ends, with no firmware change.
- The responder side needs no configuration.
- We get RSSI/SNR of the reply, a better link metric than "message delivered".
- Removes existing chat noise (§2a) as a side effect of Stage 0.
- Reuses two existing subsystems (`_should_filter_message`, `signal_log`) and one existing session
  engine (`CTCPingMixin`) instead of adding a table, a migration and a parallel registry.

### 4.2 Negative / accepted limitations

- **Four keyings per attempt, not one** (§1.4 point 7). Airtime caps must be set against the real
  multiplier, and duplicate pongs are the normal case.
- **UDP mode only.** BLE clients cannot see pongs (§1.4 point 4). Note that UDP is always on and
  BLE is additive, so this is not a mode the box is "in" — the constraint is that a BLE-only client
  view cannot show this data, not that the endpoint should be refused.
- **One direction only.** RSSI/SNR is measured by the receiver and the pong carries no signal data,
  so we learn how well _we_ hear _them_ and never the reverse. This is not a symmetric link test
  and must not be presented as one.
- **Timeout has three meanings**: not in direct range, station off, or station in track mode
  (§1.4 point 1).
- **We cannot test against ourselves** (§1.4 point 8) — every end-to-end check needs a second
  station or a simulated responder.
- **We transmit under the operator's licence**, from an endpoint with no authentication. Caps must
  be enforced server-side, not in the UI.
- **Incomplete self-view**: we see pings addressed to us but not the pong our node auto-sends
  (§1.4 point 6).
- A future firmware could change the pong payload shape. Correlation must fail soft.

### 4.3 Neutral

- No MeshCom-server interaction whatsoever; this never leaves the local RF neighbourhood.
- No schema change (§3.2).

---

## 5. Firmware sync note

Before implementing Stage 2, re-verify against the firmware actually running on the target node
that `loop_functions.cpp:3103/3184/3395/3466`, `lora_functions.cpp:701/756/773` and
`extudp_functions.cpp:365` are unchanged — the correlation scheme and the airtime estimate depend
on all of them, and §1.4 point 7 in particular is local-fork code. New upstream tags at time of
writing: `v4.35p.08.03`, `v4.35p.08.06`, `v4.35p.08.10.2`.
