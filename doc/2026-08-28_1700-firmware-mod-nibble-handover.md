# MHeard `MOD`: the "not from last hop" marker collides with country PL

**To:** MeshCom firmware maintainers
**From:** DK5EN (MCProxy / McApp)
**Date:** 2026-08-28
**Repo/branch examined:** `MeshCom-Firmware-DEV-Main` @ HEAD (this fork)
**Severity:** low impact today, but silently unfixable downstream — the wire cannot express the
difference. Not urgent; worth deciding before more consumers read the field.

This is a wire-contract report, not a crash report. Everything below is read out of the source; no
claim rests on observed traffic. Two issues, the first is the reason for this note.

---

## 1. `MOD` packs country and modulation into one byte

`aprs_functions.cpp:113` (and identically at `lora_functions.cpp:1232`, `udp_functions.cpp:224`,
`:314`, `nrf_eth.cpp:320`, `:430`):

```c
aprsmsg.msg_source_mod = (getMOD() & 0xF) | (meshcom_settings.node_country << 4);
```

| Nibble | Meaning           | Range                                         |
| ------ | ----------------- | --------------------------------------------- |
| low    | modulation preset | 3..8 (`getMOD()`, `lora_setchip.cpp:169-191`) |
| high   | country index     | 0..15 (`strCountry[]`, `lora_setchip.cpp:62`) |

The country table (`lora_setchip.cpp:62`, `max_country 17`):

```c
{"EU","UK","ON","EA","LA","868","915","MAN","EU8","UK8","US","VR2","435","436","442","PL","none"}
//  0    1    2    3    4    5     6     7    8     9     10   11    12    13    14    15    16
```

`--country` rejects only the literal `"none"` entry (`command_functions.cpp:4163`), so a node may be
set to any index **0..15 inclusive** — index 15 is `PL`. Index 16 (`none`) is rejected, so
`node_country << 4` never overflows the byte. That part is sound.

## 2. The collision

`lora_functions.cpp:583-587`, when writing the MHeard register:

```c
if((aprsmsg.msg_last_hw & 0x80) == 0x80)    // Last-Sending
    mheardLine.mh_mod = aprsmsg.msg_source_mod;
else
    mheardLine.mh_mod = aprsmsg.msg_source_mod | 0xF0;  // set mod not from last
```

The marker for "this modulation did not come from the last hop" is written **into the country
nibble**, by forcing it to `0xF`. But `0xF` is `strCountry[15]` — `PL`.

Consequently, for any MHeard entry:

- **`mh_mod >> 4 == 0xF` is ambiguous.** It means either "the station is in `PL`" or "the country is
  unknown because the modulation provenance is unknown". Nothing on the wire distinguishes them.
- **Marking destroys real data.** Every entry that takes the `else` branch loses the sender's actual
  country permanently — it is not recoverable, the original nibble is gone.
- **A `PL` node is silently mislabelled.** Its frames arrive with `mh_mod = 0xF3` and are
  indistinguishable from a marked entry.

This surfaces on the node's own displays, which already decode the two nibbles:

- `web_functions.cpp:939` — `printf("...%01X/%01X", (mh_mod >> 4), (mh_mod & 0x0f))`
- `mheard_functions.cpp:725` — `printfdeb("%01X/%01i | ", (mh_mod >> 4), (mh_mod & 0xf))`

and over BLE in the MHeard register, `mheard_functions.cpp:337`:

```c
mhdoc["MOD"] = mheardLine.mh_mod;
```

which is what MCApp receives. The value also round-trips through the node's own stored MHeard table
(`mheard_functions.cpp:325` writes it, `:114` reads it back, `:678` re-emits it on the `--mheard`
dump), so a marked entry stays marked for the lifetime of the table slot.

### Suggested fix

Any of these closes it; the first is smallest.

1. **Move the marker out of the value.** `struct mheardLine` (`aprs_structures.h:78`) is internal —
   add a `bool mh_mod_from_last;` beside `mh_mod`, set it from the `0x80` test, and leave `mh_mod`
   as the unmodified packed byte. Export it as a separate JSON key (e.g. `MODL: 0|1`) only if a
   consumer needs it; the register is already tight against `BLE_JSON_PAYLOAD_MAX` (244), so a
   one-character key or nothing at all.
2. **Use a bit that is not part of either field.** There is no spare bit in this byte — both nibbles
   are fully used — so this only works together with widening the field.
3. **Drop the modulation instead of the country.** If the point of the marker is "do not trust this
   modulation", clearing the _low_ nibble to `0` (an impossible `getMOD()` value, since the range is
   3..8) says exactly that without destroying anything real. This is a one-character change to
   `lora_functions.cpp:587` and needs no protocol change:

   ```c
   mheardLine.mh_mod = aprsmsg.msg_source_mod & 0xF0;   // country kept, modulation marked unknown
   ```

Option 3 is our preference if a struct change is unwelcome: `0` is already outside the valid
modulation range, so every existing consumer that reads the low nibble gets a value it can recognise
as "unset", and the country stays intact.

### What MCApp will do meanwhile

We currently store the raw byte and decode neither nibble — our bug, being fixed on our side. Until
the marker is separated we will render `country == 15` as **ambiguous**, never as `PL` and never as
`unknown`, because the wire genuinely does not say which it is.

---

## 3. Second, smaller issue: absent optional fields fall back to the receiver's own identity

Reported here because it feeds the same `if((msg_last_hw & 0x80) == 0x80)` test above.

`decodeAPRS` starts by calling `initAPRS(aprsmsg, 0x00)` (`aprs_functions.cpp:126`), which fills the
struct with **this node's own** values (`:110-118`):

```c
aprsmsg.msg_source_hw          = BOARD_HARDWARE;
aprsmsg.msg_source_mod         = (getMOD() & 0xF) | (meshcom_settings.node_country << 4);
aprsmsg.msg_source_fw_version  = shortVERSION();
aprsmsg.msg_last_hw            = 0x80 | BOARD_HARDWARE;   // mit lastHeard Bit
```

Two of those are then decoded from the frame **only if the bytes are present** (`:447-455`):

```c
if(inext < rsize) { aprsmsg.msg_source_fw_version = RcvBuffer[inext]; inext++; }
if(inext < rsize) { aprsmsg.msg_last_hw           = RcvBuffer[inext]; inext++; }
```

So a frame that ends after the FCS keeps the **receiver's own** firmware version and hardware id, with
the last-hop bit already set. Three consequences:

1. `mheardLine.mh_hw = aprsmsg.msg_last_hw & 0x7F` (`lora_functions.cpp:582`) records **our own board
   type** as the heard station's hardware.
2. The `0x80` test always passes for such a frame, so the "not from last" marker in §2 never fires for
   exactly the older frames it exists to mark.
3. The pre-4.35 discard gate (`aprs_functions.cpp:485-490`,
   `msg_source_fw_version > 0 && < 35`) cannot fire either — the defaulted value is our own version,
   which is ≥ 35.

This is not memory corruption (the struct is fully initialised); it is a defaulting choice. The fix is
to default the _optional_ fields to a sentinel rather than to own values, after `initAPRS` inside
`decodeAPRS`:

```c
initAPRS(aprsmsg, 0x00);        // decode init
aprsmsg.msg_source_fw_version = 0;   // 0 = not supplied by sender
aprsmsg.msg_last_hw           = 0;   // 0 = not supplied by sender
```

`0` is already the "unknown" value the fw-version gate expects (`> 0 && < 35`), and `mh_hw == 0`
reads as "unknown hardware" rather than as a wrong board type. The `0x80` test then correctly takes
the `else` branch for these frames — which is what §2's marker was written for.

---

## Summary

| #   | Where                             | Issue                                                                      | Suggested fix                                               |
| --- | --------------------------------- | -------------------------------------------------------------------------- | ----------------------------------------------------------- |
| 1   | `lora_functions.cpp:587`          | `\| 0xF0` marker overwrites the country nibble and collides with `PL` (15) | mark the modulation nibble instead: `msg_source_mod & 0xF0` |
| 2   | `aprs_functions.cpp:126, 447-455` | optional trailing fields default to the receiver's own hw/fw               | default them to `0` after `initAPRS` in `decodeAPRS`        |

Happy to test either on `DK5EN-98` (mcapp.local) and report back from the BLE register — we log every
MHeard frame, so a before/after on the `MOD` byte is a few minutes' work.
