# HEY path — fixes after the field review

**Status:** planned, not started
**Trigger:** UAT on `v2.0.2-dev.4` (2026-08-28). Live frames showed `Origin` values that make no
sense on the station they are rendered on, and `GW` flipping for one and the same originator.
**Predecessor:** `webapp/docs/archive/mheard-chain-verdict.md` — the eight findings already fixed.
This document covers what that review did **not** catch.
**Reviewed:** 2026-08-28, four independent advisors against `MeshCom-Firmware-DEV-Main` @ HEAD,
`MCProxy` @ `3b251c7` and `webapp` @ HEAD. Every firmware line, symbol and commit below was
re-verified; the corrections that review forced are marked inline.

---

## 1. What the firmware actually says

Everything below is read out of `/Users/martinwerner/WebDev/MeshCom-Firmware-DEV-Main` at HEAD, not
inferred from traffic. Line numbers are from that checkout.

**`SRC` is the first path element, `CALL` the last** — `aprs_functions.cpp:180-240`. `bSourceCall`
stops accumulating at the first comma (→ `msg_source_call`); `cConcat2` resets at every comma
(→ `msg_source_last`); `msg_last_path_cnt` starts at 1 (`:182`) and counts commas (`:213-215`).

**The MHeard register is written for almost every received frame** — the `mheardLine` is filled at
`lora_functions.cpp:574-602` and handed to `updateMheard()` at `:701`. The only gate at that point is
`!is_equ(aprsmsg.msg_source_last, node_call)` (`:562`) — text, position, ACK and HEY all land there,
each carrying its own `msg_source_call`. (An on-wire ACK is a `':'` text frame, `SendAckMessage`,
`loop_functions.cpp:4133`.)

There is an **earlier** gate the register never sees: `decodeAPRS` discards any frame whose
originator reports a firmware below 4.35 (`aprs_functions.cpp:485-490`,
`msg_source_fw_version > 0 && < 35`). Nothing from a pre-4.35 node reaches the MHeard path at all.

One line further down, `lora_functions.cpp:602`:

```c
mheardLine.mh_path_payload = (aprsmsg.payload_type == '@') ? aprsmsg.msg_payload : "";
```

**`H`/`HG` is written only by `sendHey()`** — `loop_functions.cpp:4217`, the destination set at
`:4235-4238`:

```c
if(bGATEWAY)
    aprsmsg.msg_destination_path = "HG";
else
    aprsmsg.msg_destination_path = "H";
```

The only other `"HG"` writer, `via_functions.cpp:111`, sits inside a commented-out block
(`/* 22.07.2026 - zum Test entfernt … */`) and is dead. `GW` is
`(mh_destinationpath == "HG") ? 1 : 0` (`mheard_functions.cpp:353`) over that frame's destination
(`lora_functions.cpp:581`).

**`PLT` is the payload type, and `'@'` means HEY.** `mhdoc["PLT"] = (uint8_t)mheardLine.mh_payload_type`
(`mheard_functions.cpp:335`). `decodeAPRS` admits only `0x3A` (`:`), `0x21` (`!`) and `0x40` (`@`)
(`aprs_functions.cpp:154`), and `initAPRS(aprsmsg, '@')` has exactly one call site — `sendHey`,
`loop_functions.cpp:4226`. `PLT` is set **before** the size-budget block (`:366-374`) and is never
removed by it.

**The neighbour count is written only from a HEY, and only into the originator's table slot.**
`updateHeyPath()` is called under `if(aprsmsg.payload_type == '@')` alone
(`lora_functions.cpp:706-713`) and writes `mheardNCount[imh]` for the slot whose key equals
`mh_sourcecallsign` — `SRC`, not `CALL` (`mheard_functions.cpp:432`, `:479-480`). `updateMheard()`
runs **first** (`lora_functions.cpp:701`) and fills `mhdoc["NCNT"]` from `mheardNCount[ipos]`, the slot
keyed by `mh_callsign` — `CALL` (`mheard_functions.cpp:313-321`, `:343`). That is why `NCNT` is a
property of `CALL` and is structurally at least one beacon old: it is the count `CALL` announced in an
earlier HEY of its own, replayed onto this frame.

> **Correction (advisor).** The previous draft cited `913f502d` (2026-07-07,
> _"NCount from Hey only `>= 4.35p`"_) and its `mh_fw_version > 35 || …` condition as a live firmware
> gate. **That gate was reverted the same afternoon** — weakened by `0432200e` (14:28) and removed
> entirely by `7f4f5e5a` (15:31). `mh_fw_version` does not exist anywhere in `src/` at HEAD, and
> `updateHeyPath()` has no version check. Do not cite it. What survives from that commit is the `/N`
> gate `charAt(itxt+2) >= '1' && <= '9'` (`aprs_functions.cpp:898`), which makes `/N0` unparseable by
> design — a **different channel**: position beacons carrying `/N` also feed `mheardNCount`
> (`lora_functions.cpp:662`, `:690`), so `NCNT` is not HEY-exclusive.

**One `PP` string is written by two different firmwares.** The leading `R<ncnt>;` comes from the
originator's `sendHey()` (`loop_functions.cpp:4241`, `"R" + String(getMheardCount()) + ";"`); each
group is appended by a relay's `appendHeySignalReport()` (`aprs_functions.cpp:1127`, called only from
`lora_functions.cpp:1205` and `:1275`).

> **Correction (advisor).** `appendHeySignalReport()` is **not** this fork's invention. The hop-group
> append is upstream Kurt's, `46939fd2` _"V4.34n Hey function"_ (2025-01-30). This fork's
> `d6e99ba7` (2026-08-21) **extracted** it into the named function and added the gateway-UDP call —
> the function symbol is ours, the mechanism is not. Every upstream 4.34n+ relay appends groups.

**The old wire shape is live right now.** `mheard_functions.cpp:443-447`, verbatim:

```c
// NeighborCount einfügen
// check new/old format
// new R99; R99;77,7 ...
// old R99,99,99;77,7 ... oder R99,77  ... oder R99
// old R99,99;.... kein NCount
```

The code below it counts commas in the leading token (`:457-462`) and accepts
`if(icomma == 0 || icomma == 2)` (`:471`) — 0 and 2 valid, **1 invalid**, exactly as the fourth
comment line says.

Observed on air 2026-08-28: `OE7FNH-99 → PP R3,115,-8;28,135,-16;17,120,-18;` — a 2-comma legacy
leading token from the originator, with 3-field groups appended by newer relays. Note this payload
**parses correctly on today's code**; it is evidence for the legacy shape, not for a parse failure.

### The consequence

The split is three-way, not two-way. `SRC` and `GW` each appear on both sides depending on the frame.

| Describes `CALL` (the measured station)            | Describes `SRC` (the originating station)                | Describes only this frame / route              |
| -------------------------------------------------- | -------------------------------------------------------- | ---------------------------------------------- |
| `RSSI`, `SNR`, `NCNT`, `DIST`, `HW`, `MOD`, `MESH` | `SRC` as identity + liveness; `GW` **when `PLT == '@'`** | `PP`, `PL`, `PLT`; `GW` on any non-`'@'` frame |

`SRC` is a station key and a frame value at once: as a value sitting on `CALL`'s row it is route
data, as a row key it licenses exactly two claims — identity and liveness. `MOD` is correctly on
`CALL`'s side — every relay rewrites `msg_source_mod` with its own value before forwarding
(`lora_functions.cpp:1232`) — but it is a **packed byte**, not a scalar; see **F6**.

`v2.0.2-dev.4` stores the third column on the station row keyed by `CALL`. `CALL` is a relay that
forwards a different originator on nearly every frame — `DB0ED-99` produced nine different origins in
six minutes — so the card shows whichever beacon that relay last happened to carry.

---

## 2. Fixes

### F1 — `gw` is only meaningful on a HEY frame (MCProxy)

`GW` is `0` on every non-HEY frame because the destination is something else entirely. Today's code
routes that `0` onto the originator's row, where it overwrites a genuine `1`. Observed: `DF2SI-12`
reads `GW 1` on its HEY frames (14:24:01, 14:27:17) and `GW 0` on its non-HEY frames (14:26:14,
14:27:02).

Path confirmed: `ble_protocol.py:740` `"gw": _coerce_gw(input_dict.get("GW"))` (unconditional) →
`storage/ingest.py:1210-1213` `"heard"` upsert keyed by `mh_origin` → `:405`
`gw = COALESCE(excluded.gw, station_positions.gw)`.

- **File:** `src/mcapp/ble_protocol.py`, `transform_mh`
- Read `PLT` (currently unread — `PLT` appears nowhere in `src/` or `ble_service/`) and emit `gw`
  **only** when `PLT == 0x40` (`'@'`); otherwise `None`.
- **Gate on `PLT`, not on `PP` presence.** The firmware drops `PP` once the register JSON would
  exceed 244 chars (`mheard_functions.cpp:366-371`, then `DIST` at `:373-374`), so a deep HEY has no
  `PP` but is still a HEY — a `PP` check would throw away exactly the gateway flags that travelled
  furthest. `PLT` is written at `:335`, before that block, and is never removed.
- `PLT` absent → `None`, fail closed. Use a named `_MH_PAYLOAD_TYPE_HEY = 0x40` citing
  `mheard_functions.cpp:335`, and a coercer that cannot raise on `"@"` or `"64"` — `transform_mh`
  handles unauthenticated input and every sibling coercer tolerates the string form.
- **Gate `gw` only.** `mh_origin` / `mh_origin_at` stay ungated: `mhdoc["SRC"]` is set unconditionally
  (`mheard_functions.cpp:350`), and "this station originated something we heard" is true for a text
  message as much as for a HEY. `GW` is different only because its source field — the destination
  path — carries the gateway claim on `'@'` alone.
- **This REFINES CLAUDE.md's `GW` invariant; it does not overturn it.** `GW` still describes `SRC`,
  and a `0` is still authoritative — but only on a `'@'` frame. On any other payload type the
  destination is unrelated and `GW: 0` is not a claim about anything. The **same commit** must update:
  CLAUDE.md's MHeard section, the `elif update_type == "heard"` comment (`storage/ingest.py:396-402`),
  `transform_mh`'s docstring (`ble_protocol.py:704-709`), and an amendment under Decision 1 of
  `doc/2026-08-28_0900-firmware-4.35p.08.28-adoption.md`. All four state the current unconditional rule.
- **Fixing the writer does not fix the rows.** Every station whose last MH frame was non-HEY already
  has `gw = 0` stored. It self-heals on that station's next HEY — but HEY is trickle-suppressed, so
  the better-connected the gateway, the longer the wrong value persists. Ship a one-shot migration
  alongside (precedent: `55aa288`): set `gw = NULL` where `gw = 0`, so it is re-learned from the first
  `'@'` frame rather than asserted from a frame that never carried the claim. Accepted cost: after
  F1, `gw` is _unknown_ rather than `0` until a HEY arrives.
- **Second consumer:** the transformed dict is published as `mesh_message` and reaches SSE clients
  verbatim, so the live MH frame feed's `GW` column goes blank on non-HEY frames. That is correct —
  same wrong claim — but F4 puts that feed on screen, so note it in the wave-3 release notes rather
  than letting it be filed as a regression.
- **Tests** — file is `src/mcapp/ble_protocol_tests.py` (`_test_mh_transform` `:827`,
  `_test_mh_sentinel_coercion` `:999`); no `PLT` test exists anywhere.
  - **First add `"PLT": 64` to `MH_ORIGIN_DIRECT_DICT` (`:225-241`) and every dict derived from it**,
    or the fail-closed path turns four currently-green assertions red: `:844` (`direct["gw"] == 1`),
    the `MH_GW_VECTORS` loop (`:265-272`, driven `:962-970`), and `:1043-1048`
    (`"MH GW: 0 still yields gw == 0"`).
  - New vectors: `PLT` 64 + `GW` 1 → `1`; `PLT` 64 + `GW` 0 → `0` (a real "not a gateway"); `PLT` 58
    (`':'`) + `GW` 0 → `None`; `PLT` absent → `None`. Plus one pinning that a HEY whose `PP` was
    dropped by the size budget still yields its `gw`.
  - `MH_DUMP_DICT` (`:250-261`) should gain `PLT` too: the real `--mheard` dump builder does emit it
    (`mheard_functions.cpp:676`) while emitting no `GW` — so the dump still yields `gw = None`, but
    for the right reason.
  - `storage/mheard_attribution_tests.py` is unaffected — it feeds post-transform dicts with an
    explicit `gw=` kwarg and never calls `transform_mh`.

### F2 — the parser must tolerate a missing terminator (MCProxy)

`parse_hey_chain` rejects any payload not ending in `;` (`hey_path.py:217`), and that rejection is
pinned as intended at `hey_path_tests.py:147`
(`"no terminator at all 'R12': -> None"`, inside `_test_structural_rejections`). A bare `R99` yields
`hey_chain: null` with `hey_chain_raw` set (`ble_protocol.py:744-745`), which is what the orange
"chain parse failed" badge keys off.

- **File:** `src/mcapp/hey_path.py`, `parse_hey_chain`
- Append `;` **when the payload does not already end with one**, then parse as today.
- **Do not "simplify" to an unconditional concat.** The firmware's
  `mh_path_payload.concat(";")` (`mheard_functions.cpp:450`) _is_ unconditional, and gets away with it
  because `updateHeyPath` only reads up to the first `;` via `indexOf`. We split on **every** `;`, so
  a literal mirror turns `R12;` into `R12;;` → a trailing empty group → `None`, rejecting every
  currently valid payload. Same intent, different mechanism — this is a deliberate divergence from
  the firmware, not a mirror of it.
- Apply the repair **after** the `_MAX_PAYLOAD_LEN` check (or re-check), or a max-length payload
  becomes one byte over.
- **Must not weaken anything else — two independent rules, do not fuse them:**
  (a) the **leading token** is judged by comma count, 0 and 2 valid, 1 invalid (`_parse_leading_token`);
  (b) every **hop group** must be exactly three integer fields (`_parse_hop_group`,
  `_HOP_GROUP_FIELDS = 3`). `R12;8,101,-7;JUNK` repairs to `…JUNK;`, whose final group has one field
  instead of three, so rule (b) rejects it — the leading token's comma count is not involved. Pin one
  test per rule. Verified by trace: the repaired string really does return `None`, and `R99,99` →
  `R99,99;` still returns `None` via the 1-comma branch (`hey_path.py:173-176`).
- Replace the `"no terminator at all"` test with its opposite, and cite `mheard_functions.cpp:450`.
- **Accepted cost:** a truncation landing exactly on a group boundary (`R12;8,101,-7;15,95,5`) now
  parses as a complete chain with no marker. Cuts mid-field still fail. The firmware makes the same
  trade; the `+` partial marker remains the only signal that a chain is short.
- **Framing:** no unterminated payload has been observed on air. This fork's `sendHey()` always
  terminates (`loop_functions.cpp:4241`) and every appended group ends in `;`, and pre-4.35 senders
  are discarded before the register (`aprs_functions.cpp:485-490`). F2 is **prophylactic parity** with
  the firmware's own repair, not a fix for a measured false alarm.
- **Why the repair is needed at all:** the `PP` we receive is genuinely unrepaired.
  `mheardLine.mh_path_payload` is set at `lora_functions.cpp:602` and `updateMheard()` builds the JSON
  at `:701` — both **before** `updateHeyPath()` at `:706-713` does the concat. The firmware's repair
  never touches the register copy.

### F3 — route fields come off the station surfaces (webapp)

`PP` / `PL` belong to a forwarded frame, not to the station whose row they sit on.

- `src/components/positions/PositionListPanel.vue` — drop the weakest-hop chip (`:557`) and its local
  `formatWeakestHopChip` (`:146`). Note it has **two** texts, `from ${mh_origin}: …` and
  `relayed path: …` (`:149`) — both go. Keep `no direct signal` and `NCNT`.
- `src/utils/positionHelpers.ts` — drop the Origin line (`:973-984`) and the hop ladder (`:999-1011`)
  from `buildPopupHtml` (`:828`).
- `src/components/positions/PositionsMap.vue` — drop the now-unused `.popup-origin` (`:569`) and
  `.popup-chain*` (`:579-651`) rules from the unscoped block (`:343`). `buildPopupHtml` is their sole
  producer; nothing else in `src/` emits those class names.
- `src/types/message.ts` — remove `hey_chain` (`:134`), `mh_origin` (`:86`) and `mh_dist` (`:125`)
  from `Position`. **Keep `mh_ncnt`** (`:120`, a real `CALL` property) and **`mh_origin_at`** (`:117`,
  the indirect-reach fact, consumed by `classifyStationReach`, `positionHelpers.ts:399-410`).
  > **Correction (advisor), blocking.** The previous draft also removed **`mh_path_len`**. Do not.
  > `hasMeasurement(pos.mh_path_len)` is the NCNT chip's **only** render gate
  > (`PositionListPanel.vue:570`); removing the field breaks `vue-tsc --noEmit` in `build:strict` and
  > silently deletes the chip that F3 and F5 both say to keep. `PositionListPanel.spec.ts:633` pins
  > exactly that gate. `mh_path_len` is a route property that survives without the chain — keep it,
  > or name a replacement gate first.
  > **Correction (advisor).** The rationale for dropping `mh_dist` was wrong on both halves. It is
  > not unrendered — `MheardLiveFrames.vue:98` renders a `DIST` column, but from
  > `MeshFeedFrame.mh_dist` (`stores/meshFeed.ts:32`), a **separate** type, so the removal is still
  > safe. And the popup does **not** compute the same quantity: `positionHelpers.ts:955-960` says so
  > explicitly — `mh_dist` is `src`→originator, the popup's `distance` is user→`src`. Drop it because
  > it has no attributable home on a station row, not because it is redundant.
- `src/services/messageProcessor.ts` (`:618-622`) / `src/stores/positions.ts` (`:333-337`) — stop
  copying and merging the removed fields. `processPosition` still reads **`raw.mh_origin`** to
  synthesize the originator row (`:660-664`, stamping `mh_origin_at` at `:677`); that is unchanged and
  is the one legitimate station-level use of `SRC`.
- Widen `RawDataElement.gw` (`messageProcessor.ts:86`) to `number | null | undefined` — F1 now emits
  `null`. Runtime is already safe (`:518`, `:682` both use `??`), the type is not.
- **Specs that must change — none were listed in the previous draft.** `tsconfig.json:22` includes
  `src/**/*.ts`, so `build:strict` type-checks them:
  - `src/utils/__tests__/positionHelpers.popupChain.spec.ts` — delete the file; it tests only the
    Origin line and ladder.
  - `src/components/positions/__tests__/PositionListPanel.spec.ts:499-590` — delete the weakest-hop
    block; keep `:609-645` (NCNT), whose fixtures rely on `mh_path_len` staying.
  - `src/stores/__tests__/positions.mheard.spec.ts:142-174` and
    `src/services/__tests__/messageProcessor.mheard.spec.ts:356-394` — drop the removed fields.
- **Dangling doc comments on kept code:** `types/message.ts:108-109`, `utils/presence.ts:61-72`,
  `types/heyChain.ts:27-30` all cite fields that no longer exist on `Position`.
- **Out of scope, state it so the next reader does not misread the deletion as a contract change:**
  the backend keeps emitting `mh_dist`, and the sentinel normalisation from `3b251c7` stays.

### F4 — the chain moves to the live feed, where the subject is right (webapp)

Rather than deleting the ladder, put it where one row is one beacon.

- `src/components/stats/MheardLiveFrames.vue` — render the hop ladder and the weakest-hop summary per
  frame, beside the raw `PP` it already shows (`:105-107`).
- **Also requires `src/stores/meshFeed.ts` and its spec — not listed in the previous draft.**
  `MeshFeedFrame` carries no parsed chain: only `hey_chain_raw` (`:35`) and `hopCount` (`:37`); the
  `HeyChain` from `normalizeHeyChain(data.hey_chain)` (`:82`) is discarded after `.hops.length`
  (`:96`). Both `heyChainLadder` and `formatWeakestHop` need that object, so add
  `hey_chain: HeyChain | undefined` to the frame and update `stores/__tests__/meshFeed.spec.ts`.
  `rssi` / `snr` / `mh_path_len` for the own rung are already on the frame (`:29-31`).
- Keeps `heyChainLadder` (`src/utils/heyChain.ts:212`), `formatWeakestHop` (`:143`) and `weakestHop`
  (`:92`) alive and tested — including the own-hop rung and the `— ours` case (`:156`, `:183`,
  pinned at `src/utils/__tests__/heyChain.spec.ts:173-227`), which remain correct per-frame.
  Note there are **two** `heyChain.ts`: `src/utils/heyChain.ts` (functions) and `src/types/heyChain.ts`
  (interfaces). This is the former.
- The `+` partial marker keeps its meaning, and now has **two** mechanisms: a relay without
  `appendHeySignalReport` (an older node in the path), and — since firmware `66aed467` (2026-08-28) —
  a relay that skipped the append because the payload would exceed `HEY_PATH_PAYLOAD_MAX` = 106
  (`configuration_global.h:215-223`). The second is rare by design but not impossible, so `+` means
  "chain shorter than `PL`", never "an old node was here".

### F5 — `NCNT` unknown means never announced, not stale (webapp)

The current tooltip (`PositionListPanel.vue:572`) reads:

> This station's neighbour count, as last announced on this relay beacon — the register is built one
> beacon behind the fresh count, and a well-connected station announces it least often. Not a live
> reading.

> **Correction (advisor).** The previous draft justified the reword with _"for an originator below
> firmware 4.35p, `updateHeyPath()` never runs and it never will"_. **That is false at HEAD** — the
> version gate was reverted (see §1), and `updateHeyPath()` runs for every `'@'` frame regardless of
> the originator's firmware. Worse, the reword it prescribed ("nodes below firmware 4.35p never do")
> is not even derivable from the data the chip renders: the MH register carries **no firmware field
> for `SRC`** (`mheard_functions.cpp:331-353`).

The real reasons a count stays unknown: the station has not announced one we received — neither in a
HEY (`R<ncnt>`) nor in a position beacon's `/N` — or it was not yet in `mheardCalls[]` when one
arrived. And per §1, `NCNT` is `CALL`'s own earlier count replayed onto this frame, so it is
structurally at least one beacon old even when present.

- `src/components/positions/PositionListPanel.vue` — reword along these lines: _"This station has not
  announced a neighbour count. The count is carried only in a station's own beacon; one we have only
  ever heard relayed never announces one to us. When a station does report, the value is at least one
  beacon behind."_ Keep the staleness note, demote it to secondary.
- `PositionListPanel.spec.ts:639` pins the title's meaning
  (`'the NCNT chip title conveys staleness — as last announced, not a live reading'`) and must be
  updated with it.

### F6 — `MOD` is a packed byte and we store it raw (MCProxy)

**Found by this review, not by UAT.** Independent of F1/F2, and much narrower than the first draft of
this section claimed.

> **Correction (self).** The first version of F6 said _"Both branches carry the **originator's**
> modulation … `MOD` is not [`CALL`'s]"_. **That is wrong.** Every relay rewrites
> `aprsmsg.msg_source_mod` with its own modulation and country before forwarding
> (`lora_functions.cpp:1231-1232`, alongside `msg_last_hw = BOARD_HARDWARE | 0x80`), so the value we
> receive describes the **last hop** — `CALL`. Storing it on `CALL`'s row is correct, and `MOD` stays
> in the `CALL` column. There is no misattribution here. What is real is the encoding.

`MOD` is two nibbles, not a number (`aprs_functions.cpp:113`):

```c
aprsmsg.msg_source_mod = (getMOD() & 0xF) | (meshcom_settings.node_country << 4);
```

Low nibble = modulation preset, `getMOD()` ∈ 3..8 (`lora_setchip.cpp:169-191`). High nibble = country
index, 0..15 (`strCountry`, `lora_setchip.cpp:62`). The firmware's own surfaces decode it as two
nibbles — `web_functions.cpp:939` (`%01X/%01X`) and `mheard_functions.cpp:725`.

MCProxy passes the byte through raw (`ble_protocol.py:735`) into `CALL`'s
`station_positions.lora_mod` (`storage/ingest.py:286`) and decodes **neither** nibble. So a node in,
say, `EU8` (country 8) stores `lora_mod = 0x83 = 131` where the modulation is `3`. Every value we
have shown or compared as "modulation" is wrong for every node whose country index is non-zero.

- Store `lora_mod = MOD & 0x0F` (the modulation, 3..8) and expose the country as a separate
  `lora_country = MOD >> 4` if we want it — the data is already on the wire, we simply never split it.
- **Do not** treat a high nibble of `0xF` as "unknown" without reading the firmware handover below:
  `0xF` is a legitimate country (`strCountry[15] == "PL"`) **and** the value the firmware ORs in to
  mark "modulation not from the last hop" (`lora_functions.cpp:587`). The two are indistinguishable on
  the wire. Until the firmware separates them, `country == 15` must be rendered as ambiguous, never as
  `PL` and never as `unknown`.
- Existing `station_positions.lora_mod` rows hold undecoded bytes and cannot be repaired in place
  (the country nibble is real data, so a blind mask would be fine — but a stored `0xF3` is either a
  Polish node or an unmarked one). Mask on read, backfill on next observation.
- Sequence after wave 1. It mis-labels a modulation, not a gateway.
- Handover to the firmware team for the `0xF` collision: `doc/2026-08-28_1700-firmware-mod-nibble-handover.md`.

---

## 3. Waves

| Wave | Scope                                                                                              | Repo    |
| ---- | -------------------------------------------------------------------------------------------------- | ------- |
| 0    | Commit this plan                                                                                   | MCProxy |
| 1    | F1 `gw` HEY gate (+ fixture `PLT`, `gw` scrub) · F2 terminator tolerance                           | MCProxy |
| 1b   | Docs: CLAUDE.md MHeard `GW` bullet, `ingest.py` / `transform_mh` comments, adoption-note amendment | MCProxy |
| 2    | F3 route fields off station surfaces · F5 NCNT wording                                             | webapp  |
| 3    | F4 ladder into the live feed                                                                       | webapp  |
| 4    | F6 `MOD` nibble decoding                                                                           | MCProxy |
| 5    | Docs: correct the FE design doc and B4; archive                                                    | webapp  |

> **Correction (advisor).** The previous draft claimed _"Wave 3 depends on 2 — both touch
> `heyChain.ts` consumers and `MheardLiveFrames.vue` sits downstream of the type changes."_ **Neither
> half holds.** `MheardLiveFrames.vue` imports `MeshFeedFrame` from `@/stores/meshFeed` (`:7-8`) and
> never `Position`; F3's file set and F4's are **disjoint**, and neither edits `utils/heyChain.ts`.
> Between the waves `formatWeakestHop` / `heyChainLadder` are briefly test-only exports — harmless:
> eslint's `no-unused-vars` (`eslint.config.js:64`) does not flag exports, and `vitest.config.ts` sets
> no `src/utils/**` coverage floor. **Waves 2 and 3 can run in parallel.**

Waves 1 and 2 are independent — but **not** merely because they are different repos. Six webapp specs
read MCProxy paths directly (`groupDst.spec.ts:61`, `hashtagDst.spec.ts:72`, `callsignUtils.spec.ts:432`,
`predicates.spec.ts:233`, `aprsSymbolContract.spec.ts:120`) and `dedupContract.spec.ts:55` pins a
sha256. They are independent here because **no shared corpus covers `transform_mh` or `hey_path.py`**;
the webapp references both only in doc comments.

Advisor gate after each wave, as in the previous campaign.

On completion this document is superseded, not archived to the webapp: it stays in MCProxy `doc/`
with a `**Status:** shipped in vX.Y.Z` header, in the shape of the adoption note.

## 4. Verification

Per wave, and neither suite is the check that matters here:

```
MCProxy:  uv sync --all-packages && uvx ruff@0.16.0 check . && uvx ruff@0.16.0 format --check . \
          && uv run mypy src/mcapp ble_service/src && uv run python scripts/run_startup_tests.py
webapp:   npm run lint && npm run format:check && npm run test:coverage && npm run build:strict \
          && npm run check:sw
```

The ruff pin and the `uv sync` are what CI actually runs (`.github/workflows/tests.yml:38`, `:41`,
`:48`); an unpinned `uvx ruff` resolves the newest release at run time and can disagree with the gate.
`run_startup_tests.py` gates both affected suites — `hey_path_ok` (`:155`) and `ble_protocol_ok`
(`:171`), both in the `all_ok` conjunction at `:227`.

**On live data**, after deploying:

1. **Smoke:** the weakest-hop chip is absent everywhere (`from …` and `relayed path: …` alike),
   confirming the bundle is live — and the chips F3 keeps, `no direct signal` and `NCNT`, still render
   on a station that had both.
2. **F1.** Precondition: the `gw` scrub has run and at least one HEY from `DF2SI-12` has been ingested
   since deploy. Then, over a window containing at least one non-HEY MH frame from it (visible in the
   live feed, where `GW` now reads blank on non-`'@'` frames), `station_positions.gw` stays `1` —
   checked in the DB, not by icon. The icon is `pos.gw === 1 ? GateWayIcon : MeshIcon`
   (`PositionListPanel.vue:495`), so a scrubbed-but-not-yet-relearned station legitimately renders as
   a plain node.
3. **F2 is pinned by unit tests, not by live traffic** — no unterminated payload has been observed and
   none can be induced. Live check instead: the legacy 2-comma shape (`OE7FNH-99`,
   `R3,115,-8;…`) still parses, and the count of `chain parse failed` badges over 24 h does not rise.
   A regression guard on F2, not a demonstration of it.
4. **F4.** The live feed shows a ladder per frame, and `+` appears on at least one path whose `PL`
   exceeds `hops + 1`. Which relay was old is **not** determinable — the chain carries no callsigns.
5. **F5.** `NCNT: —` persists across at least three frames for a station we have only ever heard
   relayed, and the tooltip states the two reasons in F5's wording. "Pre-4.35p originator" is **not**
   an observable: the register carries no firmware field for `SRC`.

## 5. What this withdraws

- **B4 increment 3 in full** (`webapp/docs/backlog.md:294-297`) — both halves: the weakest-hop badge
  (FE-doc **P2**) and the popup hop ladder (FE-doc **P1**, `docs/mheard-link-chain-FE.md:154`; `P1` is
  overloaded across `webapp/docs/`, so qualify it). The badge was the most actionable thing in the
  chain, but it is unattributable on a station row. It survives on the live feed. `backlog.md:216`
  reads `**Status:** done 2026-08-28` — wave 5 must amend that status, not append to it.
- **Predecessor Finding 6.** It concluded the `src` card was the defensible home for the chain,
  because chain and measurement are one observation. That held for a single observation; it does not
  survive a relay carrying nine different originators in six minutes. Findings 3 and 4 of that review
  become unreachable when the Origin line is deleted (Finding 4 is in any case superseded by the
  backend sentinel normalisation in `3b251c7`) — delete their now-dead specs rather than leaving them
  pinning removed code.
- Wave 5 must correct `webapp/docs/mheard-link-chain-FE.md` in **two** places: the status line (`:3`)
  and the §5 landing table (`:265`).

The link-chain data is real and useful. It describes **traffic passing through** a station, not the
station — and the station list is the wrong shape for it. A per-frame or topology surface is the right
home; `future-experiments.md` E1 (`:172`) is where that belongs.

## 6. Why the first pass missed all of this

`MeshCom-Firmware-DEV-Main/docs/issue-mh-json-size-budget-20260828.md` §5 documents four traps:
positive-magnitude RSSI, `GW` describes `SRC`, `PP` ends before our own hop, and the comma-count rule.
The FE design doc was written from MCProxy's adoption note and a reading of the firmware source,
without opening the firmware repo's own documentation.

Three of those four we later re-derived independently. The fourth is more uncomfortable than the
previous draft admitted: `GW beschreibt SRC, nicht CALL` is stated there **without a payload-type
qualifier**, MCProxy implemented exactly that, and that is precisely what produces the flip F1 fixes.
That §5 was right about attribution and **silent about applicability** — and the silence is what
shipped the bug. Only the terminator divergence (F2) is genuinely absent from it; `MOD` (F6) is absent
from both documents.

**Rule, to be added to both repos' campaign checklists:** before adopting a new firmware field, read
`MeshCom-Firmware-DEV-Main/docs/issue-*.md` and `docs/adr-*.md` for that field, not only `src/` — and
check whether a cited gate is still live at HEAD. This review found one (`913f502d`) that had been
reverted 91 minutes after it landed, and cited as live evidence seven weeks later.
