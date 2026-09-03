# Delivery status flags — RCA of the `1AE1E12B` "did it go out?" case

Date: 2026-09-03. Host: mcapp.local, v2.0.2, slot-1. Status: finding, no code changed.

## 0. Summary

A broadcast sent from the webapp at 21:32:52 looked, to its author, like it had never gone out.
Every artefact on the box says the send path worked. **The box cannot say more than that** — and
the single check mark the webapp draws next to the bubble is not evidence of anything beyond
"our own node echoed the frame back to us".

Two independent flaws produce that blind spot, one per repo:

1. **MCProxy** collapses the firmware's Node ACK and Gateway ACK into a single
   `send_success = 1` bit and discards `ack_kind`. Only the Gateway ACK carries the fact that a
   gateway took the frame onto the backbone.
2. **webapp** sets `msg_www` unconditionally for every own local echo, which lights the ✓ before
   any confirmation exists and permanently disables the one code path that was designed to mark
   real WWW delivery.

Net effect: a message the node queued but never got onto the mesh renders identically to a
delivered one, and leaves no forensic trace that could separate the two after the fact.

## 1. The case

`msg_id 1AE1E12B`, `DK5EN-98 → *`, `Hallo Wolf-Ruediger, grüße Rüber zum Leuchturm`,
2026-09-03 21:32:52. Reported as "seems not to have gone out"; the addressee did not react.

### Evidence chain

| Stage            | Artefact                                                                                                                                                            |
| ---------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Proxy to BLE svc | `POST /api/ble/send` returned 200 OK at 21:32:52, logged by both `mcapp.service` and `mcapp-ble.service`                                                            |
| Node accepted it | node echoed the frame on **both** transports: row `360686` `src_type=node` (Extern-UDP, ts 1788463972082) and row `360687` `src_type=ble_remote` (ts 1788463972154) |
| Frame shape      | `max_hop=2`, `mesh_info=1`, `path="DK5EN-98>"`, text intact including the umlauts; identical in shape to the 17 preceding own messages that day                     |
| Firmware ACK     | `send_success = 1`                                                                                                                                                  |
| Node server link | `link_uptime_state`: one open `up` segment since 18:19:28, no `gap` row covering 21:32; `last_beacon_ms` 21:43:25                                                   |
| Errors           | none in either journal in that window                                                                                                                               |

The predecessor `1AE1E12A` (21:32:06, considered fine by the author) differs in exactly one
respect: it has no `ble_remote` echo row, only the `node` one. Every other stored column matches.

### One artefact that looks alarming and is not

Row `360686` (`node`) carries `send_success = 0` while row `360687` (`ble_remote`) carries `1`.
That is not a partial failure. `_handle_ack` (`src/mcapp/storage/ingest.py:770`) updates
`ORDER BY timestamp DESC LIMIT 1` for the msg_id, and our own outbound message is stored twice —
once per transport. Which of the two duplicate rows receives the flag is decided by a 72 ms
timestamp difference. Read `MAX(send_success)` per `msg_id`, never a single row.

### Circumstantial, deliberately not part of the verdict

`signal_log` recorded 8 RF events in the 40 s after `1AE1E12A` (DL2JA-2 five times, 6-10 s apart
— relay traffic) and 2 in the 40 s after `1AE1E12B`. Consistent with the frame not being
relayed, and equally consistent with a quiet minute. Separately, every gateway this node hears is
marginal — DL2JA-2 −116 dBm, DB0ED-99 −120/−121 dBm, DF2SI-12 −124 dBm — while the only strong
neighbour is the operator's own T-Deck DK5EN-14 at −60 dBm, which is not a gateway. Neither
observation can carry a conclusion.

## 2. Blind spot 1 — `send_success` discards the only distinction that matters

`_handle_ack` receives the firmware's 7-byte binary ACK and derives `ack_kind`
(`src/mcapp/storage/ingest.py:833-838`):

- `0x00` Node ACK — **my own node queued the frame**
- `0x01` Gateway ACK — **a gateway heard it and put it on the backbone**
- `0x02` Peer ACK — the addressee answered (handled earlier, sets `acked`)

`ack_kind` is published on SSE as `msg_status` (`ingest.py:840-848`) and **never persisted**. The
DB write is `UPDATE messages SET send_success = 1` for all three types, by explicit design
(monotonic "transport confirmed"). The consequence was not intended: after the SSE event has
been consumed, nothing distinguishes "the node took it" from "the network took it", which is
exactly the question asked here.

Two further losses fall out of the same write:

- **No ack arrival timestamp.** We cannot tell whether the ACK came 200 ms or 40 s after the send.
- **A second ACK leaves no trace.** The UPDATE is idempotent, so a Node ACK followed by a Gateway
  ACK is indistinguishable from a Node ACK alone.

## 3. Blind spot 2 — the webapp ✓ is unconditional for own messages

`ChatBubble.vue:285` renders the single ✓ on `isOwn && (msg_sent || msg_www)`, titled
`"Sent to server"` when only `msg_www` is set. `msg_sent` comes from `send_success` and is
therefore already ambiguous per section 2. `msg_www` is worse:

```text
messageProcessor.ts:305
  msg_www:
    context.source === 'local' && ['node', 'ble', 'ble_remote'].includes(raw.src_type || ''),
```

`source: 'local'` means "arrived over the local proxy SSE" (`useSSEClient.ts:346`). Every own
message comes back over that SSE with `src_type` `node` and/or `ble_remote` within ~100 ms of the
send. So `msg_www` is true for every own message the node echoes, whether or not it ever reached
a server.

That also disables the path that was built to mean what the flag is named after. `messages.ts:791`
sets `patch.msg_www = true` when a **duplicate arrives from the oevsv.at internet firehose**
(`src_type === undefined`) for a message whose stored row is `src_type === 'node'` — i.e. "our
node-originated message came back from the WWW", the one genuine end-to-end delivery proof the
webapp has access to. It can never contribute anything, because the flag is already true.

## 4. What would have answered the question in seconds

- Persist `ack_kind` (and the ack arrival instant) instead of folding everything into one bit, so
  history can distinguish node receipt from gateway receipt.
- Make `msg_www` mean what its name and its tooltip claim: set it only from an internet-sourced
  duplicate, never from the local echo. The local echo already has its own signal (`msg_sent`).
- Give the two states distinct rendering. Today three different facts — "node queued it",
  "gateway took it", "it came back from the WWW" — share one glyph.

Tracked as backlog **B2**. The delivery-status representation spans both repos and the webapp's
existing ✓ / ✓✓ semantics (see the 2026-08 ctcping bug, `storage/ack_status_tests.py`), so it
needs one design pass across both rather than two local patches.

## 5. Verdict

No fault on mcapp.local. The proxy, the BLE service, the node handover and the node's server
uplink all behaved normally and are documented above. Whether the frame reached the mesh is
**unknowable from the stored data** — that is the finding, not the message.
