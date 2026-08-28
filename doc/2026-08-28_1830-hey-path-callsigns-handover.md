# HEY path: send the relay callsigns, or the chain stays unusable

**To:** MeshCom firmware maintainers
**From:** DK5EN (MCProxy / McApp)
**Date:** 2026-08-28
**Repo/branch examined:** `MeshCom-Firmware-DEV-Main` @ HEAD (this fork)
**Ask:** one new branch in `sendExtern()` for payload type `0x40`.
**Related:** `doc/2026-08-28_1700-firmware-mod-nibble-handover.md` (the `MOD` nibble collision —
separate issue, already filed).

---

## Summary

We adopted the `PP` link chain, built a UI for it, ran it on air for a day, and **removed the UI
again**. Not because the parsing was wrong — it works — but because the chain answers a question
nobody asked.

`PP` carries `ncnt,rssi,snr` per relay and **no callsigns**, so the best it can say is _"the third
link of five is weak."_ Which link is the third? Unknown. Which two stations are on either end of
it? Unknown. That is not actionable for an operator: you cannot call anyone, you cannot re-point an
antenna, you cannot decide anything. We shipped a "weakest hop 3 of 5" badge and then withdrew it,
because it turned out to be a number with no subject.

The chain becomes genuinely valuable the moment each link can be **named on both ends**. Everything
needed for that is already on the wire — it is simply never sent to the consumer.

## The gap, precisely

`queueExtern()` **is** already called for HEY frames — `lora_functions.cpp:771-775`:

```c
// txtmessage, position, hey
if(msg_type_b_lora == MSG_TYPE_TEXT || msg_type_b_lora == MSG_TYPE_POSITION || msg_type_b_lora == MSG_TYPE_HEY)
{
    if(bEXTUDP)
        queueExtern((char*)"lora", RcvBuffer, size, rssi, snr);
```

But `sendExtern()` only ever builds a JSON body for two payload types:

| Line                       | Guard                     | Emits                      |
| -------------------------- | ------------------------- | -------------------------- |
| `extudp_functions.cpp:402` | `msg_type_b_lora == 0x21` | `type: "pos"` (+ `"tele"`) |
| `extudp_functions.cpp:518` | `msg_type_b_lora == 0x3A` | `type: "msg"`              |

`MSG_TYPE_HEY` is `0x40`. It passes `decodeAPRS`, clears the `!= 0x00` guard at `:366` — and then
matches neither branch, so **the function returns having emitted nothing**. The frame is silently
dropped.

The BLE path does the same thing for its own reasons: `addBLEOutBuffer()` is called for ACK
(`lora_functions.cpp:290, 734, 906, 1074`), TEXT (`:936, 948, 1025, 1029`) and POSITION (`:1148`) —
**never for `MSG_TYPE_HEY`**. So the only representation of a HEY that reaches an app is the MHeard
register, which carries `CALL`, `SRC`, `PL` and `PP` but no path.

Net effect: we know a HEY travelled five hops, we know each link's RSSI/SNR, and we cannot name a
single station in between.

## What we are asking for

Add a `0x40` branch to `sendExtern()` that emits the HEY with the fields it already has to hand:

```c
if(msg_type_b_lora == 0x40)
{
    JsonDocument cJson;
    cJson["src_type"] = src_type;
    cJson["type"]     = "hey";
    cJson["src"]      = aprsmsg.msg_source_path.c_str();   // full relay path, comma separated
    cJson["msg"]      = aprsmsg.msg_payload.c_str();       // the PP chain, R<ncnt>;<n,r,s>;...
    cJson["msg_id"]   = _msgId;
    cJson["hw_id"]    = aprsmsg.msg_source_hw;
    // rssi/snr are already parameters of sendExtern() — our own reception of the last hop
}
```

Nothing here is new work: `cJson["src"] = aprsmsg.msg_source_path` is copied verbatim from the
existing `pos` branch (`:433`), and `msg_payload` of a `'@'` frame **is** the `PP` string.

### Why Extern-UDP and not the MH register

Putting the path into the BLE MHeard register would be the obvious alternative and it is the wrong
one. That register is capped at `BLE_JSON_PAYLOAD_MAX` = 244 chars, and it is already over budget:
`mheard_functions.cpp:366-374` drops `PP` first and then `DIST` to fit. Adding 5-10 callsigns would
make it drop `PP` on every deep chain — exactly the chains worth looking at.

`sendExtern` has none of that pressure: a 500-byte JSON buffer (`extudp_functions.cpp:373-380`) over
UDP. And the path is already flowing through it today for other frame types — live capture from
`mcapp.local`, 2026-08-28:

```
src: DL1RHS-14,DL1RHS-25,DL1RHS-24,DB0HOB-12,DB0ED-99   type=msg
src: DF2SI-12,DB0HOB-12,DB0ED-99                        type=pos
```

115 such frames in three hours. Only HEY is missing.

## Why the two lists line up

The same relay block writes both, in order — `lora_functions.cpp:1264-1275`:

```c
aprsmsg.msg_source_path.concat(',');
aprsmsg.msg_source_path.concat(meshcom_settings.node_call);   // who I am
...
appendHeySignalReport(aprsmsg, rssi, snr, getMheardCount());  // how I heard the previous hop
```

So for a path `P0,P1,…,Pn` and chain groups `g1…gn`, group `k` is the link `P(k-1) → P(k)`, measured
at `P(k)`. The final link is always `Pn → us`, and that one is not in `PP` at all — it is the
frame's own `rssi`/`snr`, which `sendExtern` already receives as parameters.

Worked example, captured on air 2026-08-28 (`DL2UD-1`, `PL 5`):

```
PP  R2;25,129,-10;7,122,-14;6,119,-8;4,125,-16;
```

| Link | With `src` we could say    | Signal     |
| ---- | -------------------------- | ---------- |
| 1    | originator → first relay   | −129 / −10 |
| 2    | first relay → second relay | −122 / −14 |
| 3    | second relay → third relay | −119 / −8  |
| 4    | third relay → `DL2UD-1`    | −125 / −16 |
| 5    | `DL2UD-1` → `DK5EN-98`     | −122 / −11 |

Today every entry in the left column reads "hop 1", "hop 2", … With `src` present, all five become
real station pairs.

## What we would build on it

**A signal report is an edge, not a node property** — it belongs to the ordered pair
(transmitter → receiver) and is always measured at the receiver. That invariant is already enforced
in MCProxy for the `CALL`/`SRC` split (schema migration v22 exists because it once was not), and it
is what makes the chain chartable: with callsigns, one HEY yields up to `n` independent link
measurements instead of one station measurement. A station heard by two different neighbours
produces two distinct edges, and they must stay distinct.

That turns the MHeard charts from "how well do I hear station X" into "how good is the link between
X and Y" — including links our own node cannot hear at all. That is the feature. Without callsigns
it cannot be built, and we would rather ship nothing than ship the unlabelled version again.

## Scope note

We are not asking for hop identities to be added to the RF frame — they are already there, in
`msg_source_path`. This is purely about forwarding to the local consumer over the link that has room
for it. No air-time cost, no protocol change, no impact on nodes that do not enable `bEXTUDP`.

Happy to test on `DK5EN-98` (mcapp.local) as soon as a build exists — we log every MHeard frame and
every Extern-UDP frame, so a before/after is a few minutes' work.
