# ACK attribution: "who acknowledged?" — implementation plan

Status 2026-09-05. Companion to the firmware proposal
`MeshCom-Firmware-DEV-Main/docs/ack-wer-hat-quittiert.md` (vocabulary aligned the same day).
This document is the McApp side: what MCProxy and the webapp do with the attribution once the
firmware sends it, what they do today without it, and what remains open on the firmware side.

## 1. Outcome

- MCProxy and the webapp are **attribution-ready and backwards compatible**. Every frame from
  today's firmware decodes and renders exactly as before; the new fields appear only when a frame
  carries them.
- The single-flag answers stay authoritative: `messages.send_success` / `msg_sent` (transport)
  and `messages.acked` / `msg_ack` (peer delivery). Attribution is the detail behind them, never a
  replacement.
- Vocabulary is fixed to `node` / `gateway` / `peer` (`ack_kind` on `msg:status`), the names the
  webapp and mc-chat already share. The proposal's "heard" and "server reached" are explanations,
  not identifiers.

## 2. Does it work with old firmware?

Yes, on every path, and the suites pin each case:

| Path                         | Old firmware sends                     | MCProxy behaviour                                                             | Pinned by                                |
| ---------------------------- | -------------------------------------- | ----------------------------------------------------------------------------- | ---------------------------------------- |
| BLE binary ACK               | 12/13-byte frame, byte 7 == `0x00`     | `parse_ack_appendix` returns `None`; decode identical to before               | `ble_protocol_tests._test_ack_appendix`  |
| BLE binary ACK, new firmware | byte 7 == n, callsign, timestamp       | `ack_from` set; bad appendix dropped, ACK kept                                | same                                     |
| extUDP `{"type":"ack"}`      | never (old firmware has no such frame) | new branch; absent before, so nothing changes for old nodes                   | `udp_parsing_tests._test_extudp_ack_*`   |
| `msg_status` SSE event       | n/a                                    | `from`/`via` keys present **only when known**; legacy payloads byte-identical | `ack_status_tests` cases 1-6 unchanged   |
| Repeated ACK frames          | only the first (firmware gate)         | idempotent: same `(msg_id, kind, from)` is a no-op row, one more SSE event    | `ack_status_tests` case 7, migration v29 |

The wire-side compatibility of a 22-byte LoRa ACK with old **nodes** is the firmware's question
(proposal §5.1) and is unaffected by anything here: MCProxy never sees raw LoRa frames.

## 3. What is in place (MCProxy)

- `ble_protocol.py`: `parse_ack_appendix`, `normalise_ack_callsign`, `ack_type_text`,
  `ACK_KIND_BY_TYPE`. `_decode_ack_frame` adds `ack_from` when the appendix validates.
  **Amendment to proposal §4.2:** the BLE frame uses a length byte at byte 7 (proposal's "byte 6"),
  mirroring the wire rule in §4.1, instead of "0x00 separator, timestamp at `len - 4`". The
  13-byte real-wire frame has a trailing pad and timestamp bytes can fall inside the callsign
  charset, so a separator-only rule cannot tell `DK5EN-98` from `DK5EN-98A` plus a shifted
  timestamp. Old firmware sends `0x00` there, which is the legacy format by construction.
- `udp_handler.py`: `normalize_extudp_ack` + `_handle_non_chat_frame`. The proposal's §6.3
  datagram `{"type":"ack","msg_id":"1A2B3C4D","status":1,"from":"OE1XYZ-12","via":"lora"}` is
  normalised to the same dict shape `transform_ack` emits, so ingest has one input contract.
  Before this branch every `msg`-less datagram fell into the DEBUG-only non-chat log and vanished.
- `storage/migrations.py` v29 + `LATEST_SCHEMA_VERSION = 29`: table `message_acks(msg_id, kind,
from_call, via, timestamp)` keyed on `(msg_id, kind, from_call)`. `from_call` is `''` (never
  NULL) for an unattributed frame so repeats collapse. Pruned with the ACK retention (8 days).
- `storage/ingest.py::_handle_ack`: accepts `ack_from` / `ack_via`, records a ledger row only when
  the original message matched, and adds `from` / `via` to the `msg_status` event only when known.
- `storage/query.py::get_message_acks` and `sse_routes/acks.py`:
  `GET /api/messages/{msg_id}/acks` → `{"msg_id", "acks": [{kind, from, via, timestamp}]}`.
  Unknown id is an empty list; malformed id is a 400.

## 4. What is in place (webapp)

- `types/message.ts`: `MessageAck` and `Message.acks`. `events/eventTypes.ts`: optional `from` /
  `via` on `MsgStatusAck`; the `useSSEClient` validator types them when present.
- `utils/messageAcks.ts`: `appendMessageAck` (same `(kind, from)` key as the ledger),
  `mergeMessageAcks`, `describeMessageAck`, `ackCheckTitle`.
- `stores/messages.ts`: the `msg:status` handler grows `acks` on both the `sent` and the `acked`
  branch. The ctcping ordering guard is untouched.
- `ChatBubble.vue`: the check-mark tooltip names the strongest attributed station (peer > gateway
  > node); the details popover lists every ack and lazily fetches the backend ledger the first
  > time it opens on an own message. Legacy wording is unchanged when nothing is attributed.

## 4a. Decisions taken 2026-09-05 (firmware side)

- **Wire: 22-bit node hash, 3 bytes**, not the callsign. The node resolves the hash against its
  MHeard list and forwards a callsign to the app; an unresolved hash goes out as an upper-case
  hex token, which passes `normalise_ack_callsign`.
- **App gate: `--ackinfo on`**, a volatile session flag on the node, reset on BLE disconnect.
  `ble_service` sends it first in the post-connect burst (`ACK_ATTRIBUTION_COMMAND` in
  `ble_adapter.py`, pinned by `_test_post_connect_burst_opts_into_ack_attribution`). Pre-attribution
  firmware answers `--wrong command --ackinfo on` on the command-back channel, which is harmless.

## 5. Firmware side (open, in order)

1. Parser for the wire appendix on all nodes, tolerant of old and new format; relays forward
   the full length (proposal §4.3 step 1, §5.2, §5.7).
2. BLE frame with the length byte at byte 6 (this plan's amendment to §4.2) for Node ACK and
   Peer ACK, where the callsign is already known. This alone makes attribution visible in McApp.
3. extUDP `{"type":"ack"}` datagram from the same emit site (§6.3), via `queueExtern()`.
4. Gateways send the 3-byte hash appendix; nodes resolve it; `--ackinfo on` lifts the "first
   ACK only" gates per session (§3, §5.5). McApp is idempotent for repeats.
5. Bench per §5.8, plus one McApp check: an attributed BLE frame on `mcapp.local` shows the
   callsign in the bubble tooltip and popover.

## 6. Not done, deliberately

- No change to the initial-load snapshot: attribution is fetched on demand, not embedded in
  `smart_initial`. Revisit only if the popover fetch proves too slow on the Pi.
- No mc-chat change. It must emit the same optional `from` / `via` keys once it has them, or the
  two backends diverge; the `msg_status` shape is shared by convention, no corpus pins it.
- No firmware code.
