# APRS symbol handling across all ingress routes

**Status: FIXED** — MCProxy `f77b2d7` + `7fbf4b4`, webapp `76ee412`, released `v1.6.14-dev.22`,
verified live on `mcapp.local`. Diagnosed and fixed 2026-08-01.

This file is cited from `util.py`, `storage/ingest.py`, `main.py` and `udp_parsing_tests.py` as the
evidence for the BLE exclusion and the exact-match rule.

## 1. The original bug

An APRS symbol is a (table-id, code) pair. The table id is `/` (primary), `\` (alternate), or a
single overlay character `0-9A-Z`.

MeshCom firmware hand-escapes a backslash in `sendExtern()`'s JSON builder
(`extudp_functions.cpp:379/385`) and then hands the already-escaped string to ArduinoJson, which
escapes it again. Every position beacon using the alternate table therefore arrived on Extern-UDP
:1799 with a **two-character** `\\` where the one-character `\` (`0x5C`) was meant. MCProxy stored
and re-served that verbatim, so the frontend drew a grey placeholder: `DL2JA-2` transmits `\-`
("House, HF antenna"), aprs.fi drew the blue house, the webapp drew `?`.

Only the UDP route was affected. The BLE path decodes the same beacon from raw APRS text via
`parse_aprs_position`, whose table-id group captures exactly one character — structurally incapable
of doubling. **Do not add de-escaping to the BLE path**; a second pass there would corrupt a
legitimate `\`.

### Fixed in `f77b2d7`

De-escape at the single UDP ingress point (`udp_handler.py`, immediately after the empty-message
guard, above both the `tele` and `msg` branches), plus a one-time marker-guarded backfill
(`aprs_escape_backfill_done:v1`) of `station_positions` and `messages.raw_json`.

Exact match on the two-character value, never `str.replace` — a blanket replace would mangle a
longer string that merely contains two backslashes. Both `aprs_symbol` and `aprs_symbol_group` are
handled; the code field had no failing live sample but is structurally exposed by the identical
hand-escape in `escape_symbol`.

Live backfill result: `positions_group_fixed: 4, raw_json_scanned: 198, raw_json_fixed: 198`.
The wire has carried a single `0x5C` ever since.

## 2. The icon still did not render — and why

With the wire provably correct, the webapp still drew grey. A full review across all three routes
(BLE, Extern-UDP, and the oevsv.at WSS feed that reaches the webapp directly and encodes the
alternate table as the literal `KFR`) established that **the escaping was no longer the problem at
all.** Both the backend wire and the frontend's sprite resolver were already correct.

### Claims refuted — do not re-investigate

- **The sprite index is missing the `\-` cell.** No. `aprsSymbols.ts` synthesises all 192 cells
  arithmetically from charCode 33, so it cannot have a hole. All 94 printable pairs resolve on both
  tables — verified against the deployed minified bundle and by cropping the sprite asset.
- **`mergePositions` clobbers a good stored symbol.** No. `||` preserves, and `processPosition`
  consults the existing item _before_ any fallback.
- **The v2 migration can resurrect the doubled value.** No. The backfill marker lives in
  `classifier_meta`, created by migration v16, so marker-present proves the DB reached v16+.
  Migrations always precede the backfill on the same start.
- **`load_dump` re-poisons production data.** No. It never writes `station_positions`, nothing in
  production reads `raw_json`'s symbol fields, and `save_dump` has zero callers — no `mcdump.json`
  is ever produced.
- **Overlays are ~8% of stations.** Measured 2.1% locally (2 of 97). The 8% figure refers to the
  oevsv.at population.
- **A malformed payload loses the row from both tables, silently.** Neither half holds: a
  half-populated row survives, and a full ERROR traceback is logged.

### What was actually wrong — absence handling

| #   | Defect                                                                                                                                                        | Where                                 |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------- |
| W1  | A missing symbol was fabricated into `('\', '')`, which cannot resolve. **19 of 116 live stations** were in this state.                                       | `messageProcessor.ts`                 |
| W2  | The 30 s position throttle discarded the corrective frame _whole_, so a row born symbol-less stayed grey for a beacon interval.                               | `positions.ts`                        |
| W3  | `KFR` was normalized on the group but not the code — oevsv sends it in the code slot too (observed on `DO1RL-13`).                                            | `aprsSymbols.ts`, `symbol-mapping.ts` |
| W4  | Only `KFR` was repaired; the doubled backslash passed through at ingest _and_ at the IndexedDB hydration door.                                                | `aprsSymbols.ts`, `offlineCache.ts`   |
| W5  | `parseLoraPayload.ts` — dead code whose `JSON.parse('"' + group + '"')` **throws** on the now-correct single backslash, discarding the entire frame. Deleted. | webapp                                |
| M1  | The BLE parser accepted only `/` and `\` as a table id, so an overlay id failed the whole regex and the station lost its **entire position**, silently.       | `ble_protocol.py`                     |
| M2  | `symbol or "?"` — `?` is a _valid_ APRS code (info kiosk), so absence became a confident wrong answer that overwrote stored symbols.                          | `ble_protocol.py`                     |
| M3  | Non-scalar values off the unauthenticated :1799 socket half-wrote a row.                                                                                      | `udp_handler.py`                      |
| M4  | The undouble helper existed twice with duplicated constants and divergent return types.                                                                       | now `util.py`                         |

Fixed in MCProxy `7fbf4b4` and webapp `76ee412`. Key rules that must survive future edits:

- **Absence stays absence.** Never fabricate a table id for a missing symbol, and never substitute
  `''` — falsy-but-present is the same bug one layer down. `messageProcessor`'s remaining
  `symbolGroup || '\\'` is correct: it fires only when a symbol _code_ actually arrived.
- **The BLE table-id class is the firmware's own accept-set** (`/ \ 0-9 A-Z`). Lowercase stays
  excluded — it denotes the compressed format the firmware itself rejects.
- **Read-time normalization, not a cache purge.** A purge buys at most the 24 h age floor while
  deleting correct rows from exactly the offline user it would be meant to help.

## 3. The permanent barrier

Both suites were green while the app was broken because **every test stopped at a seam**: MCProxy's
end-to-end ended at a Python dict, the webapp's began at an already-decoded JS object. Nothing could
see a value correct on one side of the serialization boundary and wrong on the other.

- **Shared corpus** — `src/mcapp/aprs_symbol_vectors.json` is canonical, vendored to
  `webapp/src/utils/__tests__/`, sha256-pinned on both sides. It deliberately does **not** live in
  `src/mcapp/contract/`, which is a git-subtree from mc-chat where in-place edits are reverted by
  the next pull.
- **Countable fixtures** — symbol values are `{"codepoints": [92], "len": 1}`, never string
  literals. `[92]` vs `[92,92]` is countable; `"\\"` vs `"\\\\"` is not, and that ambiguity is
  where this bug class hides — including inside its own tests.
- **Cross-seam byte pins** — the corpus stores the exact SSE `data:` line. MCProxy asserts it
  produces those bytes; the webapp asserts it consumes them, driven through the real path
  (`FakeEventSource → useSSEClient → ingest → store`). Verified bidirectional: editing one
  `data_line` turns _both_ repos red.
- **Anti-vacuity floors** — suites filter vectors by route and assert a minimum count before
  iterating, so a route-name typo fails loudly instead of passing an empty loop.
- **Honest mutation accounting** — each suite states its guard-vs-regression split rather than
  implying full coverage. MCProxy: 19 of 124 are fixture guards. webapp: of 191 added cases, 46 are
  killed by a production mutation, 10 by a corpus edit, 135 are no-op preservation by design.

Suites: `src/mcapp/aprs_symbol_tests.py` (124 cases, in `all_ok`), extended `ble_protocol_tests.py`
(51 → 69) and `udp_parsing_tests.py`; webapp `aprsSymbolContract.spec.ts` plus six extended specs.

An independent advisor applied 12 mutations by hand — including five neither implementer tried, such
as deleting the normalizer's _call site_ while leaving the function intact — and all 12 went red.

## 4. Verified end state

Live on `mcapp.local`, frontend matching backend exactly:

|                  | backend (SSE)         | frontend                 |
| ---------------- | --------------------- | ------------------------ |
| positions        | 116                   | 117 cards                |
| full symbol pair | 96                    | 96 resolved sprites      |
| missing a half   | 20                    | 20 fallback placeholders |
| alternate sheet  | 5 `\` + 2 overlay `G` | 7                        |

The 20 grey stations are precisely those for which the backend has no symbol data — correct
fail-closed behaviour. Before this work they were indistinguishable from genuinely broken ones,
because a fabricated `('\','')` rendered identically to real absence.

## 5. Known open items

- **Int coercion at the :1799 ingress.** An int `7` in `aprs_symbol_group` survives the scalar guard
  (an int _is_ a JSON scalar), lands in a TEXT column as `"7"` — a valid overlay id — and renders a
  plausible icon. Pinned in the corpus as _observed, not endorsed_. Tightening it is a behaviour
  change and has not been made.
- **One surviving mutation.** Rewriting the backfill's `CASE json_valid(...)` guard to a leading
  `json_valid(...) AND ...` is not observably different under SQLite's current evaluation order.
  That case is labelled a guard against a future wrong fix, not counted as regression coverage; the
  reasoning is in a comment at `storage/ingest.py`.
- **Two SSE serializers, two encodings.** `format_sse_event` uses `ensure_ascii=True` while
  `get_smart_initial_with_summary` uses `ensure_ascii=False`. Harmless today — both decode
  identically — and now pinned so it cannot drift silently.
- **CI one-sidedness.** The webapp's sibling-checkout drift check self-skips on CI; there,
  enforcement rests on the sha256 pin plus re-sync discipline.
