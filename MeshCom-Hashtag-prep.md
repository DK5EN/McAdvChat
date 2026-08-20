# MeshCom Hashtag Groups — Cross-Repo Preparation

Status: preparation / scouting result. Not a decision, not an implementation plan yet.
Date: 2026-08-20
Source RfC: `../MeshCom-Firmware-DEV-Main/docs/userstory-hashtag-filter.md` (FW 4.36 discussion draft, analysis against FW 4.35p)
Scope: `mc-chat` (mock service), `MCProxy` (backend), `webapp` (frontend). Firmware and the MeshCom
backbone server are named dependencies, not targets — see section 16.

## Contents

| #   | Section                                                                              |
| --- | ------------------------------------------------------------------------------------ |
| 0   | Executive summary                                                                    |
| 1   | What the RfC proposes — variants, decisions, and a verification of its own citations |
| 2   | Terminology — three name collisions                                                  |
| 3   | UI concept                                                                           |
| 4   | API contract changes                                                                 |
| 5   | Frontend changes (webapp)                                                            |
| 6   | Database changes                                                                     |
| 7   | Backend code changes — MCProxy                                                       |
| 8   | Backend code changes — mc-chat                                                       |
| 9   | The server-side prefix-filter engine                                                 |
| 10  | Variant B — tag as a payload prefix                                                  |
| 11  | Variant C — more slots plus a local alias table                                      |
| 12  | Variant comparison                                                                   |
| 13  | Border cases — master list                                                           |
| 14  | Bugs found during this survey                                                        |
| 15  | What is missing in the concept                                                       |
| 16  | Named dependencies outside our repos                                                 |
| 17  | Rollout ordering                                                                     |
| 18  | Test plan                                                                            |
| 19  | Open decisions                                                                       |
| 20  | Creative thinking — idea collection                                                  |

**How this was produced, and how to use it.** Findings come from reading the four repos, not from
inference off documentation. Claims marked "verified" were executed or read directly. The document was
then reviewed by an independent adversarial pass whose job was to refute it; corrections from that pass
are folded in and, where the original claim was wrong in a way worth knowing about, said so explicitly
rather than quietly edited.

**Every `path:line` citation here will rot.** This document demonstrates that at length — the RfC's own
citations had drifted 5 to 35 lines in under a few months, and these four repos move at least as fast.
Locate the symbol, not the line, and re-verify before implementing anything. The parts that will not
rot, and which are worth re-reading first, are the executed repro cases (for example
`compute_conversation_key("DK5EN-9", "#OE-SOTA")` returning `"#OE<>DK5EN"`), the matching-semantics
readings in 9.1, and the truth table.

> **Implementation status (2026-08-20).** The defensive slice of this document has been built. Eleven of
> the twelve defects in 14.1 and nine of the sixteen in 14.2 are fixed across all three repos —
> per-row commits in the Status columns. What was deliberately **not** built: prefix/subscription
> matching (9.1 is still unresolved), MCProxy answering hashtag-addressed commands (G6), and
> client-side destination validation in the picker. Those are feature work awaiting the RfC outcome.
> Everything else in this document still describes intent, not shipped state — read section 17 for
> what remains.

---

## 0. Executive summary

**The RfC is candid that this is a protocol change with flag-day character — its own section 6 says so
outright. What it cannot see from inside the firmware repo is that our three repos are not neutral
about it.** In all three, a `#TAG` destination is not rejected — it is silently misclassified as a
personal DM, at the identical decision point, by three independently written predicates. The bug the RfC
predicts for the firmware (`loop_functions.cpp:3361`, `bDM` misdetection) already exists, verbatim in
spirit, in `MCProxy/src/mcapp/commands/parsing.py:107`, `mc-chat/meshcom_mock/chat.py:86` and
`webapp/src/utils/callsignUtils.ts:109`. That is not a coincidence — all three implement the same
"group means numeric" contract, governed by a shared vector corpus — which is itself hand-copied
between repos with no hash pin and no drift detection (see 4.6).

Five findings dominate everything else:

1. **Conversation keying is the blocker, not the wire.** `compute_conversation_key()` sends a `#TAG`
   into its DM branch and then splits it on the first hyphen. Verified by direct execution:
   `compute_conversation_key("DK5EN-9", "#OE-SOTA")` returns `"#OE<>DK5EN"`. Two consequences, both
   silent: `#OE-SOTA` and `#OE-FIELD` collapse into one bucket, and the same tag from two senders
   fragments into two buckets. A hashtag "group chat" would present as a pile of fake DMs.
   Same defect, independently, in `mc-chat/meshcom_mock/storage.py:209`.
2. **The webapp cannot navigate to a tag at all.** The app runs `createWebHistory`, and every
   conversation link builds a raw template-string path (`` `/messages/${dst}` ``). vue-router's
   `parseURL` splits on the first `#` into a URL fragment — verified in the installed dependency at
   `node_modules/vue-router/dist/vue-router.cjs:502`. `/messages/#OE-SOTA` resolves to path
   `/messages/` with an empty `:dst?`, which the route guard redirects away. Eight call sites across
   two independent mechanisms (seven vue-router `to`/`push`, plus the service worker's real-URL
   `openWindow`).
   This is a prerequisite, not a polish item.
3. **Prefix matching does not exist anywhere in the stack.** Every group predicate in all three repos
   is exact-value. US-3's tag-boundary prefix rule (`#OE` matches `#OE1` and `#OE-SOTA`, not `#OEM`)
   is genuinely new code, needs a new normative spec, and — because Web Push fires with no client
   attached — must live server-side in both backends. See section 9.
4. **Blocked-sender traffic changes behaviour class.** Blocked traffic to a numeric group is
   quarantined to group `9999` and stays inspectable; the same traffic to a `#TAG` is dropped
   outright, because the quarantine gate asks `is_group(dst)`. Three repos, same gate, same result.
5. **Nothing in our stack validates a destination.** `MCProxy/src/mcapp/schemas.py:21` declares
   `dst: str = "*"` with no validator; `mc-chat/meshcom_mock/api.py:346` reads `body.get("dst", "*")`
   from raw JSON with no request model at all; `webapp` `DestinationPicker.vue`'s input has no
   `maxlength` and no charset rule. The RfC's own charset and 9-char cap have no enforcement point
   today. This is a pre-existing gap that hashtags merely make visible.

**On the RfC's three variants** (mapped at equal depth in sections 10-12): for _our three repos_,
Variant C (more slots plus a local alias table) is by far the cheapest to build and the only one
shippable unilaterally today; Variant B (tag as a payload prefix) needs no destination work at all but
costs **more** bytes on air than A (a 10-character prefix on every message, against A's 5), breaks
roughly 15 of 38 anchored classifier rules plus template fingerprinting, and carries an ambiguity no
code can fix — it cannot distinguish a filter tag from an ordinary chat hashtag like `#fieldday`;
Variant A (tag in the destination field) is the most expensive for us and the only one needing a
firmware flag day, but the only one that lets the backbone server filter without inspecting payloads.

**Recommendation to carry into the discussion, in three parts:**

1. **Fix the four defects in 14.1 now, unconditionally.** They are bugs in today's code, true
   regardless of which variant wins, regardless of what the community decides, and regardless of
   firmware timing. This is the only work in this document that needs no one's permission and no
   decision from anybody.
2. **Then ship Variant C** — but ship the alias editor together with its staleness/cleanup view, not
   as a follow-up. C is cheap, not risk-free: an alias is keyed by group number and survives that
   number being reassigned, so a stale label silently relabels unrelated traffic with a confident,
   human-chosen name. In a tool used for emergency traffic, a wrong name is worse than no name. See
   11.6. Worth saying plainly in the discussion that C is **the RfC author's own top recommendation**
   (its section 10, item 1); what this survey adds is that it is even cheaper on our side than the
   RfC assumed — a two-line change plus a small feature.
3. **Say yes to Variant A in principle**, conditional on firmware Stufe 1 landing first and on written
   commitments from the server and app sides. **Treat Variant B as the fallback** if those commitments
   do not materialise, and budget its classifier work explicitly — it is the part that looks free and
   is not.

---

## 1. What the RfC proposes

### 1.1 The proposal in one table

| Aspect                 | Today                               | RfC Variant A                                 |
| ---------------------- | ----------------------------------- | --------------------------------------------- |
| Group addressing       | numeric `1..99999` in the dst field | `#TAG` in the dst field                       |
| Tag charset            | n/a                                 | `A-Z`, `0-9`, `-`; max 9 chars including `#`  |
| Tags per message       | n/a                                 | exactly one                                   |
| Subscriptions per node | 6 (`node_gcb[6]`)                   | unbounded, limited by a 96-char filter buffer |
| Subscription syntax    | `--setgrc 9;9;9;9;9;9`              | `--setgrp #OE1#OE-SOTA#EMCOM`                 |
| Matching               | exact equality                      | tag-boundary-aware prefix                     |
| ACK behaviour          | group: gateway ACK, no peer ACK     | same as group                                 |
| Coexistence            | n/a                                 | numeric groups keep working, OR-ed            |

Forbidden inside a tag, per the RfC: `,` (VIA separator), `>` (path separator), `:` `!` `@`
(payload-type terminators), `0x00` (payload end).

### 1.2 The three variants the RfC itself offers

- **A — tag in the destination field.** `OE1KBC-24>#OE-SOTA:TEXT`. Clean, server-filterable, visible.
  Price: nodes below 4.36 discard the packet _and do not relay it_, so the mesh fragments into
  islands. The RfC proposes a two-stage rollout (4.35q relay tolerance, then 4.36 features).
- **B — tag as a payload prefix.** `OE1KBC-24>*:#OE-SOTA TEXT`. No protocol change, no relay hole,
  old nodes display it with a visible prefix. Price: `*` load rises, the backbone server cannot
  pre-filter without payload inspection, `--nomsgall on` hides everything on old nodes.
- **C — no protocol change.** Raise the 6 filter slots to 16-20, add a local alias table
  (`#OE-SOTA = 4711`). Solves the two things that are actually scarce. Does not solve hierarchy or
  spontaneous tag creation.

Section 12 compares them from _our_ side of the stack.

### 1.3 Decisions already taken for this document

- All three variants are mapped at equal depth.
- **Filter authority: backend as superset.** Both backends implement the prefix-match engine and
  per-subscriber tag subscriptions. Rationale: Web Push fires when no client is attached, so push
  filtering must be server-side regardless; and mc-chat is itself a node with the full UDP and
  interlink feed. See section 9.
- Scope is our three repos plus a named-dependency section for the backbone server and the phone apps.

---

### 1.4 Verification of the RfC's own source citations

Sections 7.1-7.10 of the RfC make roughly 25 claims about the firmware, each with a `file:line`
citation. Every one was re-checked against the current `MeshCom-Firmware-DEV-Main` tree. **The RfC's
conclusions hold. Its citations and two of its arithmetic derivations do not.** This matters because
three repos are about to be changed on the strength of those numbers.

| RfC section | Claim                                                               | Verdict                                              | Correction                                                                                                                                                                                                                                                                                                                                                                                         |
| ----------- | ------------------------------------------------------------------- | ---------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 7.1         | dest must pass `checkRegexCall()` when not a group                  | confirmed                                            | `aprs_functions.cpp:340` exact                                                                                                                                                                                                                                                                                                                                                                     |
| 7.1         | callsign regex excludes `#`                                         | confirmed                                            | `regex_functions.cpp:9`, exact text                                                                                                                                                                                                                                                                                                                                                                |
| 7.1         | `0x00` means no display, no BLE, no MHeard, no relay                | confirmed in substance                               | branch is at `lora_functions.cpp:512`, not `:517`                                                                                                                                                                                                                                                                                                                                                  |
| 7.2         | `CheckOwnGroup()` is default-open when no group is configured       | confirmed                                            | `aprs_functions.cpp:52`, falls through to `return true` at `:90`                                                                                                                                                                                                                                                                                                                                   |
| 7.2         | the same condition is duplicated three times (lora / udp / nrf_eth) | **wrong**                                            | `CheckOwnGroup()` has **exactly one** call site in the whole tree (`lora_functions.cpp:978`). The other two sites use `CheckGroup() > 0` — a different function meaning "is this a syntactically valid group number", not "is it one of mine". A synchronised three-site patch would apply the wrong fix at two of them.                                                                           |
| 7.3         | `iCall < 11` leaves 9 characters for the tag                        | **wrong**                                            | `substring(1, iCall)` with max `iCall = 10` yields **9 characters total** for the destination, so **8 remain for the tag body**. Note the RfC's own section 4 ("max 9 including `#`") is correct — it is the section 7.3 derivation that is wrong. Cited line `:3344` is inside a commented-out debug block; the real code is `loop_functions.cpp:3367`.                                           |
| 7.3         | `bDM` misdetection treats `#OE1` as a DM and appends an ACK request | confirmed                                            | `loop_functions.cpp:3384`, append at `:3413`                                                                                                                                                                                                                                                                                                                                                       |
| 7.4         | gateway ACK allow-list excludes tags                                | confirmed                                            | `lora_functions.cpp:1055`                                                                                                                                                                                                                                                                                                                                                                          |
| 7.5         | priority misclassification                                          | confirmed, name wrong                                | function is `getMessagePriority()`, `lora_functions.cpp:1466`. Nuance: `MSG_PRIO_CRITICAL` also holds real ACKs, and `CSMA_PRIO_BASE_1 == CSMA_PRIO_BASE_2` today, so the only real effect is TX-slot selection order, not CSMA timing.                                                                                                                                                            |
| 7.6         | KEEP fixed part is 22 chars, ~37 remain                             | **wrong**                                            | Fixed part is **26** characters (`KEEP` 4 + `%08X` 8 + `%-9.9s` 9 + `%-4.4s` 4 + `%-1.1s` 1). `keep_buffer[60]` leaves 59 printable bytes, so **33 remain**, not 37. Also the cited range `:1049-1061` is mostly a different function; `sendKEEP()` starts at `:1059` and its `snprintf` is at `:1079`. The conclusion (96 does not fit) stands, and the gap is 4 bytes worse than stated.         |
| 7.7         | `--setgrc`, JSON/info output, NVS keys, nRF52 migration             | confirmed in substance                               | line drift of 30-35 in `command_functions.cpp`; `FLASH_VERSION` is at `configuration_global.h:30`, not `:5`                                                                                                                                                                                                                                                                                        |
| 7.8         | all three UIs hard-wire six numeric fields                          | confirmed                                            | all three cited ranges correct                                                                                                                                                                                                                                                                                                                                                                     |
| 7.9         | header overflows a 20-char display                                  | confirmed arithmetic (22 chars), **but understated** | The same `CheckGroup()` that fails on a tag also gates the **`GM` versus `DM` header choice** at four further sites (`loop_functions.cpp:2314, 2348, 2405, 2457`). Unpatched, a tagged message renders as `DM <sender>` — a _wrong_ header, not merely a truncated one. Also, the 20-char truncation applies to three of the four board variants; the T-Deck-Pro path has no fixed 20-char buffer. |
| 8, item 6   | airtime ~9 ms per byte                                              | **doubtful**                                         | The firmware's own comment at `configuration_global.h:188` implies roughly 23.5 ms/byte average at SF11/BW250/CR6. A linear per-byte model is an approximation either way, but the discrepancy is large enough that the severity argument should be recomputed from a real time-on-air formula before it is used.                                                                                  |
| 8, item 12  | relay is independent of the filter, so no airtime saving            | confirmed exact                                      | `via_functions.cpp:49`; body references only `bMESH`, own-callsign and VIA-path — no group or tag check anywhere                                                                                                                                                                                                                                                                                   |

**Three things the RfC missed entirely, all of which change the plan:**

1. **`checkRegexCall()` already contains a literal allow-list** — `WLNK-1`, `APRS2SOTA`, `OE2YOTA-1`,
   `TEST`, `TESTER`, `BOT GATE`, `H`, `HG` are all whitelisted by plain string compare
   (`regex_functions.cpp:26-48`) _before_ the regex runs. The RfC's own "Stufe 1" relay tolerance is
   therefore **a one-line addition following an established pattern**
   (`if(callsign.startsWith("#")) return true;`), not a novel regex change. This materially lowers the
   risk estimate of the RfC's own recommendation 3.
2. **A per-message firmware-version signal already exists on the wire and is already exposed to
   MCProxy.** `aprsmsg.msg_source_fw_version` is a trailer byte written by `shortVERSION()`, and decode
   already rejects packets below version 35. It surfaces in the Extern-UDP JSON as the `firmware`
   field. **Two caveats, both verified in the source:** that field's type is unstable —
   `extudp_functions.cpp:508` emits the **string** `SOURCE_VERSION` (`"4.35"`) for self-originated
   (`src_type == "node"`) frames and the **numeric** `msg_source_fw_version` byte for relayed ones, so a
   bare `firmware >= 36` breaks on exactly the traffic we could test first; and the byte reports only the
   _originating_ sender's version, never a relay-only node's, so it makes partial rollout observable but
   does not solve the relay-hole problem. With those caveats it is a real, free, currently unused input.
   The RfC's
   entire "flag day, no capability signal" framing overlooks it.
3. **`sendExtern()` is gated by the same `decodeAPRS()` choke point.** `extudp_functions.cpp:337`
   calls `decodeAPRS()` and returns immediately on `0x00`. **Consequence: until the firmware gate is
   patched, MCProxy receives nothing at all for hashtag traffic — not even messages originated by its
   own attached node.** There is no looser local path. Every piece of MCProxy-side work for Variant A
   is therefore untestable against real hardware until firmware Stufe 1 ships. That is a scheduling
   fact, not a design one, and it is the single most important thing this verification turned up.

One more, for completeness: **no ACK is ever serialised to Extern-UDP.** `MSG_TYPE_ACK` (0x41) hits
the `else return;` in `sendExtern()`. Gateway and DM ACKs reach the phone over BLE only. A UDP-attached
MCProxy has no ACK channel at all — for hashtags or anything else.

---

## 2. Terminology — four collisions to settle before writing any code

These are not pedantry. Each one is a name already taken in our codebase, and the fourth is one this
survey nearly created.

| Term         | Already means                                                                                                                                                                                                                                                                                                                                                                                     | The RfC means                   | Proposal                                                                                                                                                                 |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **tag**      | classifier output: a free-form `tags` JSON array on every message, plus a `tags` column in both backends                                                                                                                                                                                                                                                                                          | the `#OE-SOTA` addressing token | Call the RfC concept a **hashtag** throughout. Never bare "tag".                                                                                                         |
| **group**    | numeric MeshCom group `1..99999`                                                                                                                                                                                                                                                                                                                                                                  | a superset including hashtags   | Keep **group** = numeric. Use **channel** for the union when one is needed.                                                                                              |
| **`!group`** | an existing device command, `!group on\|off`, controlling whether the node answers group commands from non-admins (`webapp/src/data/helpContent.ts:116`)                                                                                                                                                                                                                                          | nothing                         | Do not extend `!group`. A hashtag subscription command needs its own name.                                                                                               |
| **`!topic`** | **an existing, shipping admin command** meaning "recurring beacon to a numeric group" — `MCProxy/src/mcapp/commands/topic_beacon.py`, registered at `commands/handler.py:123`, throttled at `commands/constants.py:63`, admin-gated at `commands/simple_commands.py:101`, and documented to users at `webapp/src/data/helpContent.ts:143` with the example `!topic 2050 "Net every Sunday 20:00"` | nothing                         | **Do not use "topic" as the hashtag vocabulary.** An earlier draft of this document did, which would have put two unrelated meanings of the word in the same help panel. |

**Verified naming decision:** `hashtag` appears in none of the three repos, so it is free as an
identifier. Field and component names follow from that: `hashtag` (the wire-derived value),
`hashtag_filters` (the subscription list), `hashtags[]` (the push-contract field),
`HashtagFiltersCard.vue`, `is_hashtag()`, `kind-hashtag`. A hashtag column must **not** be called
`tags`, and nothing new should be called `topic`.

---

## 3. UI concept

### 3.1 Principles

1. **Do not implement `{#OE-SOTA}Text` bracket syntax in the web UI.** That syntax exists because a
   terminal or a phone command bar has one input line and no destination widget. The webapp already
   has a dedicated, structured destination field (`DestinationPicker.vue` bound to `ChatInput.vue`'s
   `inputDst`, persisted as a draft in `sessionStorage`). The correct mapping is: **type `#OE-SOTA`
   into the destination field, exactly like a callsign or a group number.** No new input mode, no new
   mental model. If terminal parity is wanted later, add it as a paste-detection convenience on the
   message textarea, never as the primary path.
2. **A hashtag is a destination, not a decoration.** It gets the same affordances a numeric group has
   today: a sidebar row, an unread badge, a hide/favourite toggle, a filter, a push filter entry.
3. **Subscription and view filter are different things.** The node's `--setgrp` list decides what
   comes off the air. The webapp's filter decides what the user looks at. They must be visibly
   distinct in the UI or users will believe the web UI changed their radio.
4. **Never silently truncate a tag.** Today three separate code paths would turn `#OE-SOTA` into
   `#OE`. The UI must show the full tag or nothing.

### 3.2 Sending to a hashtag

`DestinationPicker.vue` gains, in order of necessity:

1. A correct chip icon and kind. `kindOf()` delegates to `classifyDst()`, which returns `'person'`
   for a hashtag today (verified). Needs a new `DstKind` member — proposal: `'hashtag'`.
2. **Validation that does not exist today for any destination kind.** The input at
   `DestinationPicker.vue:255` has no `maxlength` and no pattern. New rules: charset `A-Z 0-9 -`
   after the leading `#`, total length at most 9 including `#`, reject the RfC's forbidden characters
   with a specific message rather than a generic one.
3. Uppercase normalisation on commit (blur/select/send), not per keystroke — per-keystroke uppercasing
   fights the caret. Mirrors what `normalizeCallsign` already does for callsigns.
4. A third autocomplete source. Today the picker merges **favourites**
   (`localStorage['meshcom_favoriteGroups']`) and **recents** — but `recentDestinations()` scans
   `src`, i.e. callsigns that have _sent_ something. A hashtag that has only ever been _received_
   never appears. Tags need a "recently seen destinations" source drawn from the message set's `dst`
   values, or tag discovery is impossible in the UI. This is a pre-existing gap for numeric groups
   too; hashtags make it acute because there is no central tag registry to consult instead.

Compose bar sketch:

```text
+--------------------------------------------------------------+
| To: [ #OE-SOTA            v ]  (hashtag)          8/9 chars     |
|     +------------------------------------------+             |
|     | * #OE-SOTA        favourite   142 msgs   |             |
|     |   #OE-CONTEST     seen 2h ago   17 msgs  |             |
|     |   #OE1            seen 5m ago    3 msgs  |             |
|     |   ------------------------------------   |             |
|     |   #                subscribe to all tags |             |
|     +------------------------------------------+             |
+--------------------------------------------------------------+
| Message ...                                    0/149          |
+--------------------------------------------------------------+
```

### 3.3 Subscribing — the filter list

Two different surfaces, deliberately separated:

**(a) Node subscription — `--setgrp`, in the Bluetooth node settings.** This writes to the radio.
See 3.5.

**(b) App subscription — what this browser/device wants pushed and shown.** New settings card beside
`GroupManager.vue`. Not inside `SpamFilterCard.vue`: that card is category/score based and never
inspects `dst`; folding hashtag filters in there would conflate two unrelated axes.

```text
+---- Hashtag filters ------------------------------------------+
| Show messages tagged with:                                  |
|                                                             |
|  [#OE ×] [#EMCOM ×] [#DL-SOTA ×]  [ + add filter        ]   |
|                                                             |
|  [ ] Show every tagged message  (equivalent to a bare #)     |
|                                                             |
|  #OE also matches #OE1 and #OE-SOTA — but not #OEM.          |
|  Filters end at a tag boundary.                              |
|                                                             |
|  Using 23 of 96 characters on the node.                      |
+-------------------------------------------------------------+
```

Chips rather than one free-text field, for three reasons: per-filter delete is a one-click action
instead of careful text surgery; each chip can be validated independently and shown invalid in place;
and the character budget can be attributed per chip. The underlying storage stays a single string
`#OE#EMCOM#DL-SOTA` so it round-trips to the node unchanged.

**The prefix rule must be stated in the UI, not just in the docs.** `#OE` matching `#OE-SOTA` but not
`#OEM` is not guessable. The helper line above is the minimum; a live preview ("this filter currently
matches 3 hashtags you have seen: #OE1, #OE-SOTA, #OE-CONTEST") is better and is cheap because the app
already holds the message set in memory.

### 3.4 Conversation list, badges, rendering

- **Sidebar.** `GroupManager.vue` needs no structural change to _list_ a hashtag — it already
  operates generically over `getAllDstData()`, so a `#OE-SOTA` row appears the moment one message
  arrives. What it needs is (a) the corrected icon, and (b) a deliberate ordering decision: `#` is
  ASCII 0x23 and sorts before digits, so with today's `localeCompare(..., {numeric: true})` every tag
  lands above every numeric group. Proposal: split into two labelled blocks, "Hashtags" and "Groups",
  reusing the same hide/favourite/search machinery.
- **Bubble.** Today a hashtag message renders with no group badge, a spurious "Directed" chip and an
  arrow route as if it were a personal reply target — all three follow from `classifyDst` returning
  `'person'`. Once fixed, decide whether a hashtag reuses the group badge style or gets its own. Worth
  its own: `ContactsSidebar.vue:218` sets `font-variant-numeric: tabular-nums` on `.kind-group .dst`
  with the comment "Group destinations are numeric". Applying number-column figures to `#OE-SOTA` is
  harmless but wrong; a `kind-hashtag` class is the honest fix.
- **Mobile header.** No change needed — `formatPairLabel()` passes non-pair strings through.
- **Unread counts.** These break today, not cosmetically: `sidebarKeyFor()` does not take the
  group branch for a hashtag and falls into the DM branches comparing hyphen-truncated values. Either
  the tag vanishes from counting or, worse, it is miscounted as a DM addressed to you.

### 3.5 Node settings — the `--setgrp` card

Current state: `NodeGroupsCard.vue` renders exactly six numeric fields, validates `0..99999`, commits
on blur with no dirty flag and no save button, and re-sends **all six slots** on any single edit. It
has **no component test at all** (its siblings `NodeAprsCard`, `NodeRadioCard`, `SymbolPickerModal`
all have one) — only the `setGroups()` string builder is pinned.

Proposal:

- Keep the six numeric fields. US-5 says numeric groups stay; the firmware keeps `node_gcb[6]`.
  Deleting them would be wrong.
- Add a **separate** hashtag-filter field (or the chip widget from 3.3) in the same card, visually
  below the numeric slots, with `maxlength=96` and the per-tag charset validation.
- **Make clearing explicit.** `--setgrp` with no argument clears every filter. The webapp already
  ships this footgun for `via` (`setVia(calls) => \`--via ${calls || 'NONE'}\`` firing on blur with no
  confirmation). Do not copy that pattern. A labelled "Clear all filters" button with a confirm, and
  an empty field that does nothing on blur.
- Show the budget inline: "23 / 96 characters, room for about 8 more filters".
- If the firmware exposes it, warn on the empty state: "No filters set — this node will show every
  tagged message." Whether it _can_ be warned about depends on the firmware's default-open decision,
  which is still open (RfC section 10, item 4).

### 3.6 Push notification settings

`PushNotificationsCard.vue` today has one field bound to `usrAttr.pushGroups`, placeholder `232, 9999`,
described as "Comma-separated group numbers". The transport is charset-agnostic (a plain
`split(',').map(trim)`), so a `#OE-SOTA` value already survives it — but the _matching_ is exact-only
and the copy hard-codes the numeric assumption. Needs: a parallel "Hashtags" field with prefix semantics,
new copy, and the same hydrate-before-POST discipline described in 5.5.

### 3.7 Help and onboarding copy

`webapp/src/data/helpContent.ts:28` currently states: _"Group: Send to numbered group (1-99999) or
TEST"_. That line becomes actively wrong. Several command examples also assume numeric group ids.
And the `!group on|off` device command sits in the same help panel — see the terminology collision in
section 2.

---

## 4. API contract changes

### 4.1 The wire (Extern-UDP and BLE)

No new field. The hashtag rides in the existing `dst`. Both backends already carry `dst` as an opaque
string end to end:

- `MCProxy/src/mcapp/udp_handler.py:143` — the datagram character allow-list accepts `0x20..0x5C`,
  and `#` is `0x23`. A `#TAG` survives untouched. No normalisation, no length cap, no charset check
  anywhere in this file.
- `MCProxy/src/mcapp/ble_protocol.py:134` — `dest` is the raw byte slice between the path terminator
  `>` and the payload-type terminator; `transform_msg()` at `:522` assigns `"dst": input_dict["dest"]`
  verbatim.
- `mc-chat/meshcom_mock/chat.py:25` — builds `f"{callsign}>{dst}:"` and `.encode("ascii")`. `#` is
  ASCII. `mc-chat/meshcom_mock/decoder.py:456` splits on the first `!`/`:` after `>`, so
  `OE1KBC-24>#OE-SOTA:TEXT` decodes correctly to `dest="#OE-SOTA"` today.

- `mc-chat/meshcom_mock/bridge.py` — a third, independent wire-to-JSON mapping, and the one the node's
  receive loop actually uses (`node.py:274`). `decoded_to_json` writes `"dst": header.dest` verbatim;
  `json_to_aprs` on the send path does `msg.get("dst", "*")`, the same unguarded default-to-broadcast
  pattern flagged as P10 for `api.py`.

**Conclusion: "Stufe 1" relay tolerance is already true in our Python codecs, by absence of a guard
rather than by design.** Both backends would pass a hashtag frame through the transport layer without
modification. Everything that breaks, breaks in the semantic layers above.

### 4.2 MCProxy REST and SSE

| Surface                             | File                                       | Change                                                                                                                                                                                                   |
| ----------------------------------- | ------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /api/send`                    | `sse_routes/stream.py:99`, `schemas.py:16` | `SendMessageRequest.dst` has **no validator at all** (`dst: str = "*"`). Add a `field_validator` if MCProxy is to be a validation boundary — see the open decision in 19.                                |
| `page_request` (via `/api/send`)    | `stream.py:118`                            | `dst` drives `get_messages_page()`; needs the hashtag branch or history is unreachable — see 6.1 and 7.                                                                                                  |
| `GET/POST /api/read_counts`         | `sse_routes/prefs.py`, `schemas.py:35`     | Keyed by literal `dst` string. Works mechanically for a hashtag; inherits the via-routing key problem groups already have.                                                                               |
| `GET/POST /api/hidden_destinations` | `prefs.py`, `schemas.py:42`                | Same — mechanically fine.                                                                                                                                                                                |
| `POST /api/delete_messages`         | `prefs.py:75`, `schemas.py:55`             | **Breaks.** Branches on `is_group(dst)`; a hashtag falls to the personal branch and computes a corrupted conversation key. Deleting a hashtag conversation deletes the wrong rows or none.               |
| `POST /api/push/subscribe`          | `sse_routes/push.py:72`                    | Filter shape `{dm, groups[], broadcast}`. Needs a hashtag dimension — see 4.4.                                                                                                                           |
| `POST /api/linkcheck`               | `sse_routes/linkcheck.py:38`               | `dst: Field(min_length=1, max_length=9)`. Would _accept_ `#OE-SOTA` (8 chars, non-empty) even though pinging a hashtag is meaningless. Should reject `#`-prefixed explicitly rather than silently no-op. |
| **New**                             | —                                          | An endpoint for hashtag subscriptions if they are stored server-side. See 9.                                                                                                                             |

**SSE frames.** `sse_handler.py` applies no schema to outbound JSON — every frame is `json.dumps` of a
dict. `mesh:message` carries the raw unresolved `dst`; `msg:status` carries `src`/`dst` for the
`send_failed` correlation triple. Nothing here breaks on a new dst shape; the risk is entirely in the
consumer. **No SSE frame change is required for Variant A** beyond whatever new frame a
server-side filter or subscription state needs.

### 4.3 mc-chat REST and SSE

- **`POST /api/send` has no request model at all.** `meshcom_mock/api.py:346` reads
  `body = await request.json()` and `body.get("dst", "*")` at `:444`. A missing `dst` silently becomes
  a broadcast. This is the one route that actually transmits to the real mesh and it is the one route
  with zero validation. Hashtags are the natural forcing function to add a `SendRequest` model.
- **`wire.py` is the parity surface.** `meshcom_mock/wire.py:60` maps `dst` into the MCProxy-compatible
  SSE frame with zero transformation, alongside a fixed core set (`msg_id`, `src`, `dst`, `msg`,
  `type`, `timestamp`, `src_type`) plus optional numeric/text fields, and two renames (`last_hw` →
  `last_hw_id`, `fw` → `firmware`). **If a derived hashtag field is ever added to the frame, `wire.py`
  and MCProxy's frame builder must ship it in the same release** or the webapp receives inconsistent
  frames depending on which backend it is talking to.
- MCP tools (`mcp_server.py`) need docstring updates only — `get_messages`, `count_messages`,
  `send_message` all already type `dst` as a plain `str`, consistent with the repo's rule that tool
  parameters never use `str | None`.

### 4.4 `push_contract.json` — the one contract that must change

Current: **v7**. The relevant clause is `match_semantics`, implemented identically in both backends:

- `MCProxy/src/mcapp/push_delivery.py:300` — `return target in {str(g).strip() for g in groups}`
- `mc-chat/meshcom_mock/push.py:163` — same predicate
- `webapp/src/pwa/pushFilter.ts:189` — `groups.some((g) => g.trim() === target)`

**Exact string membership. There is no prefix concept in the contract or in any of the three
implementations.** A `#OE` entry in `filter.groups` today matches only the literal four-character
string `#OE` — never `#OE1`, never `#OE-SOTA`. US-3 cannot be expressed in the current contract.

Required change, as a **v8** clause:

- A new filter dimension. Strong recommendation: a **separate** `hashtags: string[]` field rather than
  overloading `groups[]`. Overloading means existing subscribers with `groups: ["232","9999"]`
  silently acquire prefix semantics under a matcher change they never asked for, and it makes
  "is this entry a prefix or a literal?" a shape guess. A separate field is additive and back-compatible.
- A normative `hashtag_match_semantics` clause spelling out the boundary rule, with vectors for every
  case in the truth table in 9.1.
- Default for the new field on an existing subscription: empty list.

**Three implementations, two gates.** MCProxy and mc-chat are both gated by the sha256-pinned corpus.
The webapp's mirror (`pushFilter.ts`, plus `useForegroundPushSound.ts`) has no equivalent automated
parity check found in this survey. A v8 change therefore has one silently unguarded implementation.
Worth closing as part of this work.

### 4.5 `command_contract.json` and `dedup_contract.json`

- **`command_contract.json`** — `target_extraction` (13 vectors, none group-shaped), `suppression`
  (11 vectors, 7 of them `dst: "20"`), `format_for_lora`. No hashtag vectors. New vectors needed for the
  suppression decision on a hashtag destination — and that decision is itself open, because today a
  hashtag lands in the "always suppress" bucket even with an explicit remote `target:`, which diverges
  from the numeric-group behaviour. See 19.
- **`dedup_contract.json`** — v1, `content_fields: ["src","dst","text"]`. `dst` participates only as an
  opaque string in the msg_id-less fallback key. **No contract change is forced.** One substituted
  vector (`dst: "#OE-SOTA"` in place of `"232"`) is worth adding as insurance against a future change
  special-casing dst shape in the key function.
- **`command_contract.json` now has a sha256 pin** (added 2026-08-20, mutation-verified), closing the last gap of the three. Before that, an edit-in-place would pass
  the local parity suite trivially while diverging from mc-chat. Worth closing before new vectors land.

### 4.6 The manually vendored vector corpora

These are the real cross-repo contract. Unlike the subtree contracts they are **hand-copied**, with no
automated sync. **Correction after review:** an earlier draft claimed they have no drift detection at
all. That is wrong, and wrong in the direction that understates existing engineering — the webapp carries
a working `it.each(BACKEND_CONTRACT_PATHS)` drift check against **both** backends' copies for all four
corpora, and `push_contract.json` and `conversation_key_vectors.json` additionally carry an
`EXPECTED_SHA256` pin (`webapp/src/pwa/__tests__/pushFilter.spec.ts:55,120,163`). The two real gaps are
narrower: three of the four corpora have no local-edit sha256 pin, and **the drift check self-skips
whenever the sibling repo is not checked out — which is exactly the case in CI**, where the workflow does
a single `actions/checkout@v7` with no siblings. Drift is caught for any contributor using the standard
side-by-side layout, and never caught on CI.

| Corpus                            | Canonical in                           | Also lives in                                            | Hashtag vectors today        |
| --------------------------------- | -------------------------------------- | -------------------------------------------------------- | ---------------------------- |
| `group_dst_vectors.json`          | MCProxy `src/mcapp/commands/`          | mc-chat `tests/fixtures/`, webapp `src/utils/__tests__/` | 25 vectors, zero hashtag     |
| `conversation_key_vectors.json`   | MCProxy `src/mcapp/storage/`           | mc-chat `tests/fixtures/`, webapp `src/utils/__tests__/` | v3, 21 vectors, zero hashtag |
| `directed_dst_vectors.json`       | mc-chat (rides the classifier subtree) | MCProxy via subtree                                      | zero hashtag                 |
| `blocklist_decision_vectors.json` | MCProxy                                | webapp mirror                                            | zero hashtag                 |

Every one of these needs hashtag vectors, and every one must be hand-copied to two or three repos.
**This is the largest coordination risk in the feature** and it is worth deciding, up front, whether a new hashtag-matching corpus joins this
manual set or the disciplined subtree set.

### 4.7 Contract-change ordering

The subtree contracts are owned upstream by mc-chat. The ordering is not optional:

1. Edit the JSON in **mc-chat** `contract/`. Never in place in MCProxy.
2. Bump mc-chat's own `_EXPECTED_SHA256` in the same commit or its CI fails immediately.
3. `git subtree split --prefix=contract -b contract-subtree` in mc-chat.
4. `git subtree pull --prefix=src/mcapp/contract mc-chat contract-subtree --squash` in MCProxy.
5. Update MCProxy's `_EXPECTED_SHA256` (`push_tests.py:93`, `dedup_contract_tests.py:44`) **in the same
   change as the pull** — not before, not after.
6. Hand-copy the manually vendored corpora from 4.6 into all consumers.
7. Both gated runners green independently. Nothing cross-checks this automatically.

**Historical trap worth restating:** `push_contract.json` used to live outside the `contract/` prefix
in mc-chat, so every subtree pull silently _deleted_ MCProxy's copy and took the whole gated test
runner down with it. Fixed in v4 by moving the file inside the prefix. Any new contract artefact must
be placed inside the subtree prefix from day one.

---

## 5. Frontend changes (webapp)

### 5.1 The predicate layer — one file, four functions

All destination classification lives in `src/utils/callsignUtils.ts`. Note a naming drift: the
scope-named `groupDst.ts` and `directedDst.ts` no longer exist as files; only their spec files
survive and both import from `callsignUtils`.

| Function            | Line   | Behaviour on `#OE-SOTA` (verified)                                  | Change                                                                                                          |
| ------------------- | ------ | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| `normalizeCallsign` | `:13`  | returns `'#OE'` — splits on the first `-` to strip an SSID          | Must not be applied to a hashtag. Guard at the call sites, or introduce `normalizeDst` that dispatches on kind. |
| `isGroupDst`        | `:94`  | `false`                                                             | Leave alone (group means numeric). Add a sibling `isHashtagDst`.                                                |
| `classifyDst`       | `:109` | `'person'`                                                          | Add a `'hashtag'` member to `DstKind`, tested before the generic letter test.                                   |
| `isDirectedDst`     | `:138` | `true` — a hashtag is treated as a personal DM                      | Fixed automatically once `classifyDst` returns `'hashtag'`.                                                     |
| `matchesDst`        | `:158` | falls to the personal-DM branch, comparing hyphen-truncated `'#OE'` | Needs a hashtag branch mirroring the group branch.                                                              |

Downstream consumers that inherit the misclassification, all needing review once the predicates are
fixed: `stores/messages/predicates.ts:115` (`filterMessagesByDst` — a hashtag currently takes the branch
that _filters acks out_, where the group branch deliberately keeps them),
`stores/messages/sidebar.ts:28` (`sidebarKeyFor` — unread counts),
`services/messageProcessor.ts:143` (blocklist quarantine — a hashtag from a blocked sender is fully
suppressed instead of quarantined to `9999`), `services/messageProcessor/ackMatch.ts:58`,
`pwa/pushNotification.ts:124` (`conversationKey` — a hashtag push opens the sender's DM thread).

### 5.2 Routing — the blocker

The app uses `createWebHistory`. Every conversation link builds a raw template-string path.
vue-router's `parseURL` (`node_modules/vue-router/dist/vue-router.cjs:502`, verified in the installed
dependency) splits the string on the first `#` into path and hash. Result: `/messages/#OE-SOTA`
resolves to path `/messages/` with an empty `:dst?`, and the route's `beforeEnter` guard
(`src/router/index.ts:28`) then redirects to `lastMessagesDst`. **The hashtag conversation is
unreachable.**

Seven vue-router call sites, verified (the eighth follows):

```text
src/components/chat/ContactsSidebar.vue:107   :to="`/messages/${dstData.dst}`"
src/components/chat/ChatBubble.vue:153        router.push(`/messages/${props.message.src}`)
src/components/chat/ChatBubble.vue:197        :to="`/messages/${message.src}`"
src/components/chat/ChatBubble.vue:208        :to="`/messages/${personalDst}`"
src/components/chat/ChatBubble.vue:231        :to="`/messages/${effectiveDst(message.dst)}`"
src/router/index.ts:13                        redirect -> `/messages/${lastDst}`
src/router/index.ts:33                        beforeEnter -> `/messages/${lastDst}`
```

Plus an eighth, through a completely different mechanism: `src/pwa/pushNotification.ts:244` builds
`` `/webapp/messages/${dst}` `` as a real URL string, consumed by `src/sw.ts:220,227` via
`client.navigate()` / `clients.openWindow()`. Those go through the browser's WHATWG URL parser, where
`#` is equally a fragment. **Two independent mechanisms, same failure — this is systemic, not a
vue-router quirk.**

Fix options:

- (a) `encodeURIComponent(dst)` at all eight sites plus `decodeURIComponent` where `route.params.dst`
  is read. Minimal, but leaves the stringly-typed paths in place.
- (b) Switch to vue-router's **object** location form (`{ name: 'messages', params: { dst } }`), which
  encodes internally and never hash-splits, and `encodeURIComponent` only for the service-worker URL.
  **Recommended** — it removes the whole class of bug, not just the `#` case.

### 5.3 Components

| File                                         | Change                                                                                                                                                                                 |
| -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `chat/DestinationPicker.vue`                 | hashtag icon/kind, validation (charset + 9-char cap — none exists today), uppercase-on-commit, a "recently seen destinations" autocomplete source, and a bare-`#` "all hashtags" entry |
| `chat/ChatInput.vue`                         | no structural change; the 149-char UTF-8 budget is message-body only and untouched by Variant A                                                                                        |
| `chat/ChatBubble.vue`                        | hashtag badge instead of the spurious "Directed" chip and arrow route; router object form                                                                                              |
| `chat/ContactsSidebar.vue`                   | router object form; a `kind-hashtag` CSS class instead of reusing `kind-group`'s tabular figures                                                                                       |
| `chat/ChatFilterBar.vue`                     | placeholder copy ("Callsign, group or hashtag"); the free-text filter currently inherits the misclassification                                                                         |
| `settings/GroupManager.vue`                  | correct icon; split rendering into "Hashtags" and "Groups" blocks; no persistence change needed                                                                                        |
| `settings/PushNotificationsCard.vue`         | new Hashtags field, new copy                                                                                                                                                           |
| **new** `settings/HashtagFiltersCard.vue`    | the chip widget from 3.3                                                                                                                                                               |
| `bluetooth/node-settings/NodeGroupsCard.vue` | add the `--setgrp` field; keep the six numeric slots; explicit clear-all                                                                                                               |
| `bluetooth/node-settings/EditableField.vue`  | new `'hashtag'` editable branch with `maxlength=96` — note the existing `'group'` branch has no `maxlength` at all, so it is not a template to copy                                    |

### 5.4 Stores and persistence

- `types/userSettings.ts` — new field for the app-side hashtag filter list. Must be threaded through the
  existing dirty-flag / single-flight / hydration-guard discipline (there are dedicated spec files for
  each of those three).
- `stores/messages/sidebar.ts`, `predicates.ts` — hashtag branches.
- `stores/bleStore.ts` — a new `IRegister` field for the node's filter string. The register is JSON
  key-value with an index signature, so an unknown new key is already tolerated (see 5.6).
- **IndexedDB needs no schema change.** `offlineCacheDB` v3 keys messages by `msg_id`, not `dst`; there
  is no `dst` index; `viewedDst` is a flat object keyed by dst string. `#` is a valid key everywhere.

### 5.5 Push and the service worker

- `pwa/pushFilter.ts` — new `hashtags` field on `PushFilter` and a boundary-aware prefix matcher.
- `pwa/pushNotification.ts` — `conversationKey()` hashtag branch; `clickTargetPath()` URL encoding.
- `composables/usePushNotifications.ts` — the new field must follow the existing hydrate-before-read
  discipline in `postSubscribe()`: `await store.loadUserSettings()` and read `store.usrAttr`
  **after** that await, because the loader replaces the whole `usrAttr` object rather than mutating
  it. A pre-await destructure holds a stale reference. This is the exact shape of the 2026-08-17 incident (normative fix: contract v6, 2026-08-18)
  that wiped a live subscription's groups. **A subscribe POST replaces the stored filter wholesale**
  and the contract deliberately puts that ordering obligation on the client — do not attempt a
  server-side merge heuristic.

### 5.6 BLE node settings — cheaper than expected

The node's config registers arrive as **plain JSON**, ASCII-prefixed `D{...}`, not as the fixed-offset
binary `@` frame used for chat and position data. `MCProxy/src/mcapp/ble_protocol.py:594`
(`transform_ble`) does an unconditional `**input_dict` passthrough, and both
`webapp/src/stores/bleStore.ts`'s register interfaces carry an index signature (`[key: string]: unknown`).
Note `types/bleNodeSettings.ts` does **not** — its `editable?:` union is hand-enumerated and must be
extended by hand for a new field kind.

**Consequence: adding a filter-list key to the firmware's `I` register is a zero-effort,
backward-compatible change on our side.** No offset maths, no struct migration, no version
negotiation. Old clients ignore the key; new clients read it.

The real risk is elsewhere — see the MTU finding in 13.

### 5.7 Tests

New or extended: `callsignUtils` spec plus the vendored `group_dst_vectors.json` /
`conversation_key_vectors.json` copies, `messages.sidebar`, `messages.filter`, `predicates`,
`messageProcessor.blocklist`, `pushFilter`, `pushNotification`, `DestinationPicker`, and — currently
absent entirely — a first `NodeGroupsCard.spec.ts`.

---

## 6. Database changes

### 6.1 MCProxy

- **`messages.dst` needs no schema change.** `TEXT NOT NULL`, no CHECK, no length cap, no collation
  (`storage/constants.py:189`). SQLite binds any string.
- Existing indexes on `dst`: `idx_messages_dst`, `idx_messages_type_dst_timestamp`, and
  `idx_messages_convkey_ts` on `(conversation_key, timestamp DESC) WHERE type='msg'` — the last is the
  one that actually serves conversation paging.
- **`conversation_key` values change meaning** once the hashtag branch is added. Existing rows already
  carry the corrupted `#OE<>SENDER` keys if any hashtag traffic has been stored. A migration that
  re-keys history is the established pattern here — v18 re-keyed every via-routed message after
  `compute_conversation_key` was corrected (`storage/migrations.py:290`).
- **A new table is needed only if hashtag subscriptions are stored server-side** (which the "backend as
  superset" decision implies). Shape proposal: per-subscriber, not global. Note the existing prefs
  tables are singleton rows (`id INTEGER PRIMARY KEY CHECK (id = 1)`) or keyed by `dst`/`text` with no
  device dimension; the **only** per-device state that exists today is `push_subscriptions`, keyed by
  endpoint.
- Migration mechanics: append an `if current_version < 24:` block ending in `_set_schema_version(conn, 24)`
  in `storage/migrations.py`, and bump `LATEST_SCHEMA_VERSION` in `storage/constants.py:23` **and** the
  separately hand-copied `FINAL_SCHEMA_VERSION` in `storage/migration_chain_tests.py:41` in the same
  commit. See the naming bug in 14.
- No prefix-capable index exists. `dst LIKE '#OE%'` can range-seek `idx_messages_dst` under BINARY
  collation but has no notion of a tag boundary, so it would also match `#OEM`. A correct
  implementation needs a boundary check outside SQL, or a normalised hashtag column.

### 6.2 mc-chat

- Same conclusion for `messages.dst` — `TEXT NOT NULL`, no constraint, indexed by
  `idx_messages_dst_ts` and `idx_messages_src_dst`.
- **There is no schema-version counter at all.** No `PRAGMA user_version`, no `schema_version` table.
  Migration is `_migrate()` inspecting `PRAGMA table_info(messages)` and adding missing columns
  idempotently (`meshcom_mock/storage.py:545`). Adding a column is easy; there is no rollback path
  and no "reject on unexpected schema" gate. Worth noting as an asymmetry with MCProxy when the two
  need to move together.
- `read_counts` and `hidden_destinations` are keyed by raw `dst` — mechanically fine for a hashtag.
- `push_subscriptions.filter_json` already stores `{"dm":bool,"groups":[str],"broadcast":bool}` as a
  JSON blob, so a new `hashtags` key needs **no schema change at all** — only a contract and matcher
  change.
- **No table exists for node-level subscriptions.** `NodeConfig.groups` is a `list[int]` in
  `~/.meshcom-mock/nodes.json`, config-file only, with no DB presence, no history and no API-driven
  mutation path.

### 6.3 webapp (IndexedDB)

No change required. Detail in 5.4.

---

## 7. Backend code changes — MCProxy

Ordered by severity. Every one of these is reachable today by sending a `#`-prefixed destination.

1. **`compute_conversation_key()`** (`storage/constants.py:115`) — add a hashtag branch returning the
   raw tag, placed **before** the DM fallback, mirroring the group branch. Verified current behaviour:
   `("DK5EN-9", "#OE-SOTA")` returns `"#OE<>DK5EN"`; `("DK5EN-9", "#OE-FIELD")` returns the same key.
   Needs vectors in `conversation_key_vectors.json` (canonical here, vendored to two repos).
2. **A hashtag predicate.** Either widen `is_group()` or add `is_hashtag()` beside it. `is_group` is
   contract-pinned and mirrored in mc-chat and the webapp, so widening it is itself a three-repo flag
   day. Recommendation: **a sibling `is_hashtag()`**, OR-ed at each call site, plus a `dst_kind()` helper
   to stop the proliferation. See the open decision in 19.
3. **Command routing** (`commands/routing.py:226-254`) — a hashtag-addressed message from another
   station currently reaches no branch at all and falls off the end into `return False, None`.
   MCProxy is completely deaf to hashtag-addressed commands.
4. **Suppression** (`suppression.py:23`) — `is_valid_destination()` rejects `#` on both arms
   (`DST_CALLSIGN_RE` charset, and `is_group`), so every self-issued command to a hashtag is suppressed
   **even with an explicit remote `target:`**, unlike the numeric-group case. Today the end-to-end
   outcome happens to be right for replies, but only because two independently-false conditions cancel
   out. That should not survive into a real implementation without an explicit vector.
5. **Blocklist quarantine** (`main.py:372`) — `is_group(dst) or dst in ("*","ALL")` decides
   redirect-to-`9999` versus hard drop. Hashtag traffic from a blocked sender is dropped. Decide whether
   hashtags get quarantine, and if so whether a reserved `#`-sentinel is needed (there is no `#SPAM`
   analogue today).
6. **Query layer** (`storage/query.py`) — `get_messages_page()`'s dispatch tests `is_dm` before
   `is_group_dst`, and `is_dm` is true for a hashtag whenever `src` is supplied, so the group branch
   never gets a chance. Separately, `get_search_summary()` restricts distinct destinations with
   `AND dst GLOB '[0-9]*'`, making hashtags invisible to that endpoint entirely.
7. **`delete_messages_by_dst`** (`storage/prefs.py:140`) — same `is_group` branch; a hashtag deletion
   targets a corrupted key.
8. **Push matcher** (`push_delivery.py:285`) — the prefix engine. See 9.
9. **`SendMessageRequest` validation** (`schemas.py:16`) — optional, see 19.
10. **Via-resolution is hand-rolled in at least four places** (`storage/constants.py:135`,
    `classifier/types.py:58`, `push_delivery.py:105`, and implicitly in `storage/query.py:439`).
    A hashtag change touches all of them. This is the natural moment to consolidate into one
    `resolve_dst()` / `dst_kind()` helper rather than adding a fifth parallel implementation.

**Two things that need no change,** worth stating so nobody "fixes" them:

- **The ACK plumbing is destination-agnostic by construction.** `_handle_ack` keys on the 32-bit
  `msg_id`; the inline `:ackNNN` path keys on `echo_id`. Neither branches on dst shape. Once the
  firmware emits a gateway ACK for a hashtag send, the two-ACK contract works unmodified.
- **MCProxy never appends the `{NNN` ack-request suffix.** It only strips and matches it. The RfC's
  ack-suffix hazard is entirely firmware-side.

---

## 8. Backend code changes — mc-chat

The same defects, independently written. This is strong evidence that the shared contract is doing its
job and that a fix must be corpus-driven, not hand-applied per repo.

1. **`is_direct_dst()`** (`meshcom_mock/chat.py:86`) — `not is_group(dst)` means a hashtag is "direct",
   so `messaging.py:150` appends a `{NNN` ack-request to the message body of a hashtag send, and
   `node.py:328` never answers it because the ack-reply gate compares the destination to the node's own
   callsign. **This is the firmware's predicted defect number 2, reproduced in Python.** The existing
   test `tests/test_chat.py:426` (`test_out_of_range_digits_are_direct`) already documents the same
   tri-state gap for `"0"`.
2. **`is_relevant_message()`** (`meshcom_mock/chat.py:67`) — matches `dst` against
   `own_groups: list[int]`, so **every hashtag message is dropped before storage**. Unlike the
   firmware's `CheckOwnGroup()`, there is no default-open behaviour to inherit or debate: mc-chat's
   default node config already lists three numeric groups, so a hashtag is unconditionally irrelevant.
   Consequence: hashtag traffic would never reach `messages`, never be counted as an arrival, and the
   coverage view would show a permanent gap on the node paths while the scraper and interlink paths
   (which have no relevance filter by design) see it fine. That asymmetry would read as a network
   problem, not a code gap.
3. **`_conversation_key()`** (`meshcom_mock/storage.py:183`) — the hyphen-split bug, identical to
   MCProxy's, at `:209`.
4. **`should_suppress_command()` / `_is_valid_destination()`** (`meshcom_mock/commands.py:128`) —
   a hashtag is an invalid destination, so any `!command` to a hashtag is answered locally and never
   forwarded, unlike the numeric-group case.
5. **`push.py:matches()`** (`:143`) — the prefix engine, must stay byte-identical in behaviour to
   MCProxy's.
6. **KEEP.** `build_keep_packet()` (`meshcom_mock/protocol.py:40`) has **no size cap** — `group_string`
   is an unbounded Python string, unlike the firmware's `char keep_buffer[60]`. mc-chat would happily
   emit a KEEP that no real MeshCom server could parse. Pre-existing realism gap; becomes acute if a
   96-char filter list is bolted on. Note mc-chat never _decodes_ an inbound KEEP for its own logic —
   KEEP is send-only from its perspective.
7. **`SendRequest` model** — add one; see 4.3.
8. **`NodeConfig`** (`meshcom_mock/config.py:63`) — needs a hashtag-filter field. Whether it mirrors the
   firmware's single delimited string or a parsed list is an open decision; the string is more faithful
   to what is being mocked.

---

## 9. The server-side prefix-filter engine

Decision taken (1.3): both backends implement the matcher and per-subscriber hashtag subscriptions, as a
superset of whatever the attached node passes.

### 9.1 Matching semantics — specify before implementing

This is new behaviour with no precedent anywhere in the stack, and it will be implemented at least four
times (two backends, the webapp mirror, the firmware). It has to be pinned down first.

> **Do not implement from this section yet.** The RfC's stated rule and the RfC's own worked examples
> disagree with each other. Both readings are written out below so the disagreement is visible; which
> one is correct is open decision 1 in section 19 and must be settled with the RfC author first.

**Reading 1 — "the next character must be `-`".** This is the literal reading of US-3's phrase "the
prefix comparison stops at a tag boundary", and it is what a careful implementer would write:

```text
boundary_ok(hashtag, filter):
    rest = hashtag[len(filter):]
    return rest == "" or rest[0] == '-'
```

**This reading fails one of the RfC's own examples** — it rejects `#OE` matching `#OE1`, which US-3
explicitly requires. Kept here only to show what not to ship.

**Reading 2 — "the next character must not be a letter".** This is the only rule that satisfies all
three of US-3's worked examples (`#OE` matches `#OE1`, matches `#OE-SOTA`, does not match `#OEM`), and
is therefore the more likely intent:

```text
normalise(s):
    strip surrounding whitespace, uppercase

is_hashtag(dst):
    dst starts with '#'
    and 2 <= len(dst) <= 9
    and every char after '#' is in [A-Z0-9-]

boundary_ok(hashtag, filter):
    # a letter continues the current tag component; a digit or '-' starts a new one
    rest = hashtag[len(filter):]
    return rest == "" or not rest[0].isalpha()

matches(hashtag, filter):
    t = normalise(hashtag); f = normalise(filter)
    if f == '#':            return True          # the curious: everything tagged
    if not t.startswith(f): return False
    return boundary_ok(t, f)
```

Note that reading 2 is genuinely strange — it makes `#OE` match `#OE1` but not `#OEM`, so whether a tag
is "inside" a namespace depends on the character class of the next character rather than on an explicit
separator. It works for the RfC's examples and would surprise most users. A third option worth putting
to the author: require an explicit separator and change the example, so `#OE` matches `#OE-1` and
`#OE-SOTA` but not `#OE1` — which is reading 1 with the namespace convention made explicit.

Truth table the vectors must pin, in both backends and the webapp mirror, **once the rule is settled**:

| Filter       | Hashtag        | Expected                                            | Why                                                      |
| ------------ | -------------- | --------------------------------------------------- | -------------------------------------------------------- |
| `#OE`        | `#OE`          | match                                               | exact                                                    |
| `#OE`        | `#OE1`         | **match under reading 2, no match under reading 1** | the disputed case                                        |
| `#OE`        | `#OE-SOTA`     | match                                               | boundary at `-`, both readings agree                     |
| `#OE`        | `#OEM`         | **no match**                                        | the whole point of the boundary rule                     |
| `#OE-SOTA`   | `#OE-SOTA`     | match                                               | exact                                                    |
| `#OE-SOTA`   | `#OE`          | no match                                            | filter is longer than the hashtag                        |
| `#`          | `#ANYTHING`    | match                                               | bare `#` is the wildcard                                 |
| `#`          | `232`          | no match                                            | untagged traffic is not a hashtag                        |
| (empty list) | `#OE1`         | **undecided**                                       | default-open vs default-closed — see 15.6 and decision 4 |
| `#oe`        | `#OE-SOTA`     | match                                               | case-insensitive comparison                              |
| `#OE`        | `RELAY-1,#OE1` | match                                               | via-resolve to the last comma component first            |
| `#OE`        | `*`            | no match                                            | US-4: broadcast is never hashtag-filtered                |
| `NOTATAG`    | `#OE1`         | no match                                            | an invalid filter entry matches nothing                  |

The disagreement itself is one of the concrete gaps in the concept — see 15.1.

### 9.2 Where the engine plugs in

**Per-client identity is the missing primitive.** Both backends represent a connected SSE client as an
id plus a bounded queue and nothing else:

- MCProxy — `SSEClient(client_id, queue)` in `sse_handler.py:109`, registry
  `SSEManager.clients: dict[str, SSEClient]` at `:150`. The id is minted server-side per connect:
  `str(uuid.uuid4())[:8]` in `sse_routes/stream.py:37`.
- mc-chat — `EventBus._subscribers: dict[str, asyncio.Queue]` in `models.py:230`, same fresh-uuid model
  at `api.py:143`.

Both can tell two browsers apart **for the lifetime of one connection only**. Neither can recognise the
same device reconnecting. **The one durable per-device handle that exists today in either backend is
the Web Push subscription `endpoint`**, which is already the primary key of a real table. Every other
prefs table is a global singleton (`id INTEGER PRIMARY KEY CHECK (id = 1)`) or keyed by `dst`/`text`
with no owner column.

The webapp already round-trips the ephemeral `client_id` back to the server on POST bodies for
response targeting, so the plumbing exists — it just resets on reconnect.

**Smallest change that gives a durable identity:** the browser generates and persists a `device_id` in
localStorage and passes it as a query parameter on `GET /events` (which today takes none), and the
backend keys a per-device filter row on it — the same identity model push already uses, generalised to
browsers without push enabled.

**Fan-out.** Both backends already iterate every client per message, and the event string is serialised
once and shared:

- MCProxy — `SSEManager.broadcast_event`, `sse_handler.py:711`: snapshot the client list, format once,
  `asyncio.gather` the sends.
- mc-chat — `EventBus.publish`, `models.py:272`: loop over subscribers, `_offer` each.

A per-client predicate is a filter on the list that is already being iterated. It adds **no new O(n)
pass** and no re-serialisation — one string operation and a set membership test per client per message.

**Recommended siting.**

- **MCProxy** — add a `hashtags: list[str]` field to `PushFilter` (`sse_routes/push.py:52`) and a
  boundary-aware predicate beside `matches()` in `push_delivery.py`, reusing `_resolve_target` for
  via-routing. For the live stream, add an optional `hashtag_filter` to `SSEClient`
  (`sse_handler.py:106`), populated from a `device_id`-keyed table shaped like `push_subscriptions`,
  and gate the client list inside `broadcast_event` before the existing `gather`. **Do not touch
  `filter_prefs`** — it is a singleton row and the wrong shape.
- **mc-chat** — the same shape at `push.py`'s `FILTER_DEFAULTS`/`matches()` and at `EventBus.publish`.
  Critically, **the hashtag engine belongs in the SSE/push fan-out layer, not in `is_relevant_message`**.
  That function governs mc-chat's own virtual node's group subscription — a different, process-wide
  concept. Conflating "what mc-chat's node relays" with "what this browser wants to see" would be a
  design error.

**Existing precedents to imitate rather than invent:** the blocklist redirect rewrites `dst` on a
**shallow copy** and never mutates the shared routed dict (`sse_handler.py:657`); and the push pipeline
gates in a fixed order — eligibility, blocklist, dedup, then per-subscription match and coalesce — with
the gates running on the **unstripped** view. A hashtag engine should slot into that order, not beside it.

**Volumes and limits.** Per-client SSE queue 256 events (both backends, independently); push coalesce
window 5 s; push dedup window 3600 s; push delivery queue 1000; push connect/read timeouts 3 s / 5 s.
**There is no cap on SSE client count and no cap on push subscription rows in either backend.** The
latent cost is an unbounded subscription table on a Pi Zero 2W, not per-message CPU. Push subscriptions
are pruned only reactively, on a 401/403/404/410 delivery failure.

### 9.3 The MCProxy / mc-chat asymmetry — read this before promising anything

This is the most consequential operational finding in the survey.

**MCProxy is a true node.** It only ever sees what its attached hardware already decided to hand up.
Its browser-side "unlimited prefix filter" can therefore only ever be a **subset selector over whatever
arrived** — it can never show a hashtag the node itself dropped. And per 1.4, until the firmware
`decodeAPRS()` gate is patched, what arrives for hashtag traffic is _nothing at all_.

**mc-chat is a node in software, but its ingest is asymmetric.** Of its five ingest paths, only the two
UDP node paths apply a relevance filter (`is_relevant_message`); `interlink` and the two scraper paths
store everything unconditionally. So mc-chat genuinely can serve an unbounded per-browser hashtag filter
over the full backbone feed.

**These are not hypothetical machines.** `mcapp.local` is the production MCProxy box; `rpizero.local`
runs mc-chat. The comparison below is the one that will actually get made the first time this feature is
tested on both.

**The user-visible consequence.** A user subscribed to `#DL-EMCOM`:

- On an **MCProxy**-backed Pi whose attached node was never configured for that hashtag: **sees nothing,
  ever.** Not a filtering gap — a _reception_ gap, upstream of any code we write.
- On **mc-chat**: sees the traffic that reached the interlink/scraper feed, and it appears to just work.

Neither UI surfaces "your node did not relay this". Two operators comparing notes would conclude one
backend's filter engine is broken, when the real difference is hardware node configuration outside
either engine's control. **The design doc must scope MCProxy's engine explicitly as a subset selector
and surface the node-configuration gap in the UI**, or this becomes a recurring support conversation
with no diagnosable cause.

**A third feed makes it worse.** `webapp/src/services/internetWebSocket.ts` connects the browser
directly to oevsv.at, bypassing both backends entirely. It carries `dst` and feeds the same
`mesh:message` bus. **Server-side filtering is architecturally impossible for this feed** — there is no
server in the loop. A hashtag filter for it must be a client-side mirror, exactly as the blocklist
already is (`messageProcessor.ts:134`, with a comment explaining precisely this). And Web Push, which
fires with no client attached, **cannot ever cover internet-feed-only content**.

So the honest statement of scope is: **three feeds, two of which can be filtered server-side, one of
which can only be filtered in the browser, and one backend that is structurally blind to anything its
node dropped.**

---

## 10. Variant B — the tag as a payload prefix

`OE1KBC-24>*:#OE-SOTA TEXT`. No protocol change, no relay hole, old nodes display it with a visible
prefix. Mapped here at the same depth as Variant A.

### 10.1 What it costs on the wire

`#OE-SOTA ` is 9 characters (the tag is 8; the RfC's maximum tag is 9, so the worst case with its
separator is 10). That cost is paid on every tagged message, forever. Against the layer that bites first —
the webapp's compose-time budget of 149 UTF-8 bytes (`ChatInput.vue:19`, gates the Send button) —
that is **6.0 % of every message** for this example and 6.7 % at the maximum tag length. Against
MCProxy's bot-reply chunk size of 140 bytes it is 6.4 % to 7.1 %. Note this is a share of the
**compose-time character budget**, not a measured airtime share — see the caveat under the comparison
table in section 12.
The 120-char push truncation is a display cap, not an airtime cost.

Note that **no server-side length enforcement exists at all** in either backend's ingest path, so a
longer message from a non-webapp client is accepted and stored unmodified.

### 10.2 What survives the text path

Every in-flight text transform was inventoried. The good news is broad: **nothing in any of the three
repos strips, splits or eats a leading token today.** The ack-suffix strip is `$`-anchored
(`ACK_SUFFIX_RE = r"\{([0-9]+)$"`), the trims are edge-only, the push truncation takes the _first_
120 characters, the UDP charset allow-list already permits `#` (0x23 is inside 0x20-0x5C), and the
firmware-escape unescaper only touches `"` and `\`.

Two exceptions:

- **The response chunker eats the prefix from chunks 2..N.** `_transmit_chunks`
  (`MCProxy/src/mcapp/commands/response.py:127`) prepends a per-chunk `(n/m) ` header — that is the
  only thing it repeats. Anything at string index 0 lands in chunk 1 only. Moot today, because the
  chunker only ever handles bot replies and Variant B tags ride user chat; but if command output were
  ever auto-tagged, the tag would need threading into the per-chunk header alongside `(n/m)`.
  `MAX_CHUNKS = 3`, `MAX_RESPONSE_LENGTH = 140`.
- **`!wx text:` splices raw user text into an unrelated beacon.** `mc-chat/meshcom_mock/meteo.py:657`
  literally prepends the captured text. `!wx text:#OE-SOTA see you there` would prepend a
  syntactically valid tag onto a weather beacon, which a tag-aware client would then misfile.

### 10.3 The real cost: the classifier

This is where Variant B stops being cheap.

- **Template fingerprinting breaks.** `classifier/template.py:_normalize` hashes the whole text after
  a fixed normalisation (URL, emoji, digit-run, whitespace, lowercase) with no prefix stripping. Two
  otherwise-identical beacons that differ only by a `#OE-SOTA ` prefix produce **different
  `template_hash` values**. That silently splits one logical beacon into two rows in
  `beacon_templates`, halving each variant's count against every auto-promotion threshold (8 lifetime,
  5-in-24h, 3-in-72h). A beacon sent sometimes tagged and sometimes not never promotes either variant.
- **Anchored rules break, and worse.** **15 of 38** built-in classifier rules are `^`-anchored
  at literal string start — `^\{CET\}`, `^\s*ping\s*$`, `^\s*(—|–|--)[a-zA-Z]`, `^\s*MH\(\d+\):`.
  `re.search` still finds nothing, because the anchor demands position 0. A tagged timestamp beacon,
  bot-command echo or node advert all fall through to `category = "other"`, and `info_score` shifts
  with them.
- **A second, independent mechanism breaks the same way.** `MCProxy/src/mcapp/storage/ingest.py:1531`
  drops `{CET}` beacons from the messages table via `msg_content.startswith("{CET}")`. A tagged
  `{CET}` beacon would no longer be filtered and would land as a real stored row.

Fixing this means adding a tag-stripping normalisation step to the classifier's `_normalize()` and to
its rule-target computation — **in mc-chat, because that is the subtree's upstream**, then syncing to
MCProxy, plus a `classifier_ver` bump and a full backfill.

### 10.4 Dedup, commands, push, rendering

- **Dedup is safe when `msg_id` is present**, which is the normal LoRa case — the key is id-primary.
  In the msg_id-less fallback the key is `(src, dst, text)`, so a tagged and untagged retransmit of the
  same content produce two keys: no over-suppression, but no cross-variant dedup either. Same shape as
  the template-hash split.
- **`#` collides with nothing.** The full inventory of leading-token conventions across the three repos
  is `!` (command), `{ping}`, `{pong}{N}`, `{CET}`, `{MCP}`/`{SET}` (documented, firmware-side, no
  literal check in our source), and `:ackNNN` (which specifically _requires_ a preceding callsign, so a
  leading token is safe for it). `#` is not a reserved sigil anywhere. Worth noting that `{CET}` is the
  closest architectural analogue — a fixed leading brace-token treated as a semantic marker by plain
  `startswith` in three unsynchronised places. Exactly the fragile pattern a `#TAG ` implementation
  would have to replicate correctly at every site.
- **Push shows the tag verbatim.** Neither backend's payload builder strips a leading token. A strip
  would belong in `build_push_payload`'s text argument, **never** in `_build_gate_view` — the same
  gate-before-strip ordering the module already enforces for the ack suffix, and for the same reason:
  stripping before dedup widens the msg_id-less fallback key. This would need a parallel
  `payload_tag_prefix_semantics` contract clause or the two backends drift.
- **Rendering shows it verbatim too.** `ChatBubble.vue`'s `cleanMessage` is _only_
  `stripAckRequestSuffix`. There is no existing mechanism for hiding a leading payload token from
  display while keeping it in storage — but the ack-suffix strip is the exact template for one. (Also
  checked: there is no URL auto-linking anywhere in the chat components, so `#` cannot be mistaken for
  a markup token.)

### 10.5 The central ambiguity

**Variant B cannot distinguish a filter tag from an ordinary chat hashtag.** `#fieldday if you're free`
normalises to `#FIELDDAY` — 9 characters, valid charset, indistinguishable from a group tag. A
tag-aware client would reinterpret it as a filter tag, hide it from everyone not subscribed to
`#FIELDDAY`, and strip it from display. None of the three repos offer any disambiguation, and the RfC
proposes no escaping convention or registry.

Other border cases: a tag-only message with no text passes every non-empty check and would render with
an empty body once a display-strip is added (the `{ping}`/`{pong}` guards have no analogue here);
internal double spaces are never collapsed on the stored or displayed text, so whether the parser wants
exactly one separator or `\s+` is undefined.

### 10.6 Verdict for our three repos

**Cheaper than Variant A, but not free, and the cost is in the classifier and in ambiguity rather than
in plumbing.**

- Zero destination-parsing changes anywhere. Variant A touches `is_group`, `classifyDst`,
  `compute_conversation_key`, `DIRECTED_DST_RE`, the blocklist gate, the query dispatch and the router
  paths. Variant B touches none of them.
- The text path already leaves a leading token intact everywhere it matters.
- But: a classifier normalisation change in two repos plus a corpus and version bump and a backfill;
  a display-strip and a push-strip duplicated across three implementations; and an unsolved semantic
  ambiguity that no amount of code fixes.

---

## 11. Variant C — more slots plus a local alias table

The RfC's own section 10 recommends this as the immediate, risk-free step. For our stack it is
**dramatically cheaper than either protocol variant**, and it is the only one we can ship without
waiting for anybody.

### 11.1 Slot expansion 6 to 20 — a two-line change

Contrary to the RfC's framing, "six" is barely load-bearing on our side:

| Component                              | Status                                                                                                                                                                                                                          |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `NodeGroupsCard.vue:19` and `:40`      | **The only two functional hardcodes** — `Array.from({length: 6}, ...)` for field generation and for rebuilding the full array on any single edit.                                                                               |
| `firmwareCommands.ts:94` `setGroups()` | already length-agnostic: `` `--setgrc ${groups.join(';')};` ``                                                                                                                                                                  |
| its pinned test                        | asserts the string for a 6-element input; does not assert length. Stays green.                                                                                                                                                  |
| `bleStore.ts:47` `IRegister`           | declares `GCB0..GCB5` **plus** `[key: string]: unknown` at `:54`, and the card reads `I.value?.[\`GCB${i}\`]`dynamically. Resolves for`i` up to 19 with no type error. Adding named fields is documentation, not a requirement. |
| `NodeSettingsGroup.vue:29`             | renders `v-for="field in fields"` generically                                                                                                                                                                                   |
| BLE framing                            | `--setgrc ` (9) + 20 x 6 chars = **129 bytes**, inside the 253-byte 1-byte-length cap and the 247-byte MTU soft limit. No overflow.                                                                                             |

Firmware-side buffer sizing for parsing a 20-slot `--setgrc` is the one thing outside our control and
worth a footnote.

### 11.2 The alias table — and the mechanism that already exists

**`formatPairLabel()` (`callsignUtils.ts:74`) is already the display-versus-identity separator the app
uses**, and four of the render sites already route through it. An alias resolver is the same shape.

All sites rendering a destination as visible text — **5 files, 7 call sites**:

| Site                                     | Already via `formatPairLabel`? |
| ---------------------------------------- | ------------------------------ |
| `ContactsSidebar.vue:117`                | yes                            |
| `MobileChatHeader.vue:14`                | yes                            |
| `DestinationPicker.vue:300` (favourites) | yes                            |
| `DestinationPicker.vue:317` (recents)    | yes                            |
| `ChatBubble.vue:55,235`                  | **no** — raw `message.dst`     |
| `GroupManager.vue:149` (favourite chip)  | **no** — raw                   |
| `GroupManager.vue:230` (list label)      | **no** — raw                   |

Checked and excluded: the push notification title uses `resolveSrc(payload.src)`, never `dst`; there is
no `document.title` usage anywhere in `src/`.

Minimal client-only implementation: a `groupAliases?: Record<string,string>` field on `UserAttributes`,
a `resolveDstLabel(dst, aliases)` wrapping `formatPairLabel`, three template edits, and a small editor.
Persistence is free — `saveUserSettings()` already round-trips the whole `usrAttr` blob, and the
IndexedDB store's `usrAttr` is already `Record<string, unknown>`, so **no IndexedDB version bump**.

### 11.3 Alias versus identity — the trap map

The alias must appear **only** in text nodes. Everywhere `dst` is an identity it must stay raw:

router `:to` targets and `route.params.dst`; scroll-memory and `dstHasMore` keys;
`markDstAsViewed`; `getAllDstData` grouping; `msgSelect` (hidden) and `meshcom_favoriteGroups`
comparisons; `usrAttr.pushGroups` feeding the server-side push filter; the push notification `tag` and
`data.dst`; and server-side `compute_conversation_key` / `delete_messages_by_dst`.

**The sharpest trap is `DestinationPicker` to `ChatInput`.** `sendMessage()` sends `inputDst`
**verbatim** as the wire `dst`. If the dropdown ever emits the alias as the selected _value_ rather
than only rendering it as text, a user picking "OE-SOTA" transmits the literal string `OE-SOTA`, which
`CheckGroup()` rejects — a silently undeliverable message. The picker must keep the number as
`v-model` and use the alias only in the rendered label.

A second same-string trap: `GroupManager.vue` uses `group.dst` as both the `v-for` `:key` and the
rendered label. Easy to get right, equally easy for a later "simplification" to emit the alias from a
click handler.

### 11.4 Where the alias map should live

| Option                                             | Cost                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Trade-off                                                               |
| -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| **Client-only** (mirrors `meshcom_favoriteGroups`) | zero backend, zero migration, ships today                                                                                                                                                                                                                                                                                                                                                                                                                                                                | lost on a browser wipe, not shared across devices                       |
| **Backend-synced**                                 | ~40-60 frontend lines mirroring `updateSpamFilter`, plus a third `DirtySettingsFamily` member and its retry/toast/event-bus wiring. **Zero migration on either backend if piggybacked inside the existing `filter_prefs` JSON blob** — both backends already treat it as an untyped passthrough dict. A dedicated table instead costs one migration block plus a version bump on MCProxy, and a single `CREATE TABLE IF NOT EXISTS` on mc-chat (structurally cheaper there — it has no migration chain). | survives a browser wipe, shared across devices hitting the same backend |
| **On the node**                                    | firmware change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | contradicts the entire premise of Variant C                             |

Given "ship unilaterally", client-only is the only option needing neither a firmware nor a backend
release. Backend sync is a cheap follow-up, not a blocker.

### 11.5 Prior art

No user-editable alias mechanism exists in the served code paths. Two adjacent things worth knowing:

- `NON_PERSON_DST_ALIASES` (`callsignUtils.ts`) is a fixed two-entry set, not user-editable — the
  pattern to imitate, not a table to extend.
- **mc-chat already has a real `aliases` table** — `profile/knowledge_db.py:50,121`, with
  `add_alias(callsign, name, conf, evidence_msg_ids)`, `best_alias()`, `supersede_alias()`, used
  by the offline agentic profile builder to record inferred _real names for callsigns_. Wrong domain,
  wrong runtime (a batch CLI tool, not wired into the live service), confidence-scored rather than a
  user preference. Not reusable code — but it confirms the concept already has a home in this codebase
  family, just on the wrong shelf.
- `callsignColor()` is the closest UX precedent: a deterministic per-identifier client-side annotation.

### 11.6 Border cases

- **An alias survives deletion or reassignment of its group number.** Keyed by number, independent of
  subscription state. If `4711` is later reused for a different hashtag, the old, now-wrong name silently
  relabels it. No automatic invalidation is possible. This is the sharpest real risk in the feature; a
  manual "unused aliases" cleanup view is the practical mitigation.
- **An alias and a real hashtag can end up with the same label.** Once Variant A ships, a device can
  simultaneously hold a legacy alias `OE-SOTA` pointing at numeric group 4711 and a real `#OE-SOTA`
  hashtag — identically labelled, carrying different traffic, sorted into different UI sections by the
  split proposed in 3.4. This is a comprehension hazard, not merely dead data, and it is created by
  shipping C and A in sequence. Mitigation: render an alias with a visible marker distinguishing it from
  a real hashtag, and warn on save if an alias name collides with a hashtag already seen on air.
- Duplicate alias names are possible and visually indistinguishable — warn on save, do not block.
- An alias colliding with a real callsign is cosmetic only, **provided** the identity discipline holds.
- Alias text never reaches an attribute binding today, so escaping is safe by Vue's text interpolation
  — a guarantee that breaks the moment someone binds it into `:class` or a query string.
- Sorting: `GroupManager.vue:56` sorts by `dst` with `numeric: true`. Displaying aliases without a sort
  toggle makes the visible order stop matching the sort. Needs a decision, not a default.
- An alias for a group with no traffic is a harmless dead entry.

### 11.7 Forward compatibility with the hashtag future

- **The display side is already forward-compatible.** `resolveDstLabel(dst)` treats `dst` opaquely, so
  the same map keyed by the literal wire string works for `"4711"` and `"#OE-SOTA"` alike. No widening
  needed; only a UX change to offer the editor for tag-shaped destinations.
- **The alias data itself becomes dead weight, not dead code.** The number a user chose to call
  "OE-SOTA" has no relationship to whatever tag eventually gets used on the mesh for that hashtag.
- **The 20-slot UI is not a stepping stone.** Numeric slots stay numeric (US-5 keeps `node_gcb`), and
  the tag filter field is a separate, later widget in the same card. Expanding to 20 is not wasted —
  it ships value immediately — but do not plan it as phase one of the tag UI.

### 11.8 Effort ranking

1. **20 slots** — two lines plus a layout decision. Everything downstream is already length-agnostic.
2. **Alias table, client-only** — one type field, one resolver, two template edits, a small editor.
   Bounded entirely to `webapp/`.
3. **Alias table, backend-synced** — the above plus a third dirty-family and its wiring, plus either
   zero migration (piggyback on `filter_prefs`) or one small migration per backend.

---

## 12. Variant comparison, from our side of the stack

| Dimension                             | A: tag in dst                 | B: tag in payload                            | C: slots + alias |
| ------------------------------------- | ----------------------------- | -------------------------------------------- | ---------------- |
| Firmware release needed               | yes, two-stage                | no                                           | no               |
| Backbone server change needed         | yes (KEEP, distribution)      | no (but no pre-filtering either)             | no               |
| Phone apps must ship in step          | yes                           | no                                           | no               |
| Relay holes during rollout            | yes, severe                   | none                                         | none             |
| Our repos: destination predicates     | ~12 sites across 3 repos      | none                                         | none             |
| Our repos: classifier                 | one new dst rule              | **normalisation change + backfill**          | none             |
| Our repos: routing/URL                | 8 call sites, blocker         | none                                         | none             |
| Our repos: conversation keying        | new branch + re-key migration | none                                         | none             |
| Our repos: push contract              | v8, new filter dimension      | v8, new payload clause                       | none             |
| Text-budget cost per message          | +5 bytes vs a 3-digit group   | **+9 to +10 chars, 6.0-6.7 % of the budget** | 0                |
| Hierarchy (`#OE` matches `#OE1`)      | yes                           | yes                                          | no               |
| Spontaneous tag creation              | yes                           | yes                                          | no               |
| Server can pre-filter                 | yes                           | no, needs payload inspection                 | n/a              |
| Tag visible to old nodes              | n/a (they drop it)            | yes, as literal text                         | n/a              |
| Ambiguity with ordinary chat hashtags | none                          | **unsolved**                                 | none             |
| Can we ship it alone, today           | no                            | partly                                       | **yes**          |

**A caveat on the cost row.** Column A is a raw byte delta; column B is a share of the webapp's
compose-time character budget, which is not the same quantity as airtime. This document elsewhere calls
the RfC's linear ms/byte model doubtful, and the same scepticism applies here — neither figure is a
measured time-on-air, and the real marginal cost of a few extra bytes on a short message is smaller than
any average-per-byte model suggests. Treat the row as an ordering, not as a measurement. Worth noting the
RfC's own figure is internally inconsistent too: 5 bytes at its stated 9 ms/byte is 45 ms, not the 49 ms
it quotes.

**Reading of this table.**

Variant C is the only column we control end to end. It costs a two-line change plus a small feature and
delivers the two things the RfC identifies as actually scarce — filter slots and speaking names. It does
not deliver hierarchy or spontaneous tags.

Variant B is cheaper than A for us on plumbing but pays for it twice: a permanent 6-7 % text-budget tax on
every tagged message, and an ambiguity with ordinary chat hashtags that has no protocol answer. The
classifier work is real and lands in the subtree, meaning a mc-chat-first change plus a sync plus a
reclassify backfill.

Variant A is the most expensive for us and the only one whose timing we do not control — and per 1.4,
**we cannot even test it against real hardware until the firmware's `decodeAPRS()` gate is patched**,
because `sendExtern()` shares that gate. But it is also the only variant that gives the backbone server
something to filter on without inspecting payloads, and the only one whose tag never appears as literal
text in a message body.

**Recommendation to put into the discussion:** ship C now, unilaterally. Say yes to A in principle,
conditional on the firmware's Stufe 1 landing first and on a written commitment from the server and app
sides. Treat B as the fallback if that commitment does not materialise — and if B is chosen, budget
the classifier normalisation work explicitly, because it is the part that looks free and is not.

Independently of all three: **fix the defects in section 14.1.** They are bugs in today's code.

---

## 13. Border cases — master list

Grouped by where they bite. Every entry is either verified in code or a direct consequence of a
verified behaviour.

### 13.1 Tag shape and normalisation

| # | Case | Behaviour today | Note |
| --- | ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- | --- |
| B1 | Tag containing `-` (`#OE-SOTA`) | truncated to `#OE` by three independent SSID-stripping paths | The RfC's own flagship example. Highest-impact border case in the whole survey. |
| B2 | Lowercase tag `#oe-sota` | stored distinct from `#OE-SOTA`; two conversation keys, two read-count rows | RfC assumes the firmware uppercases on send. Nothing guarantees that for internet-WS or locally composed traffic. |
| B3 | Tag longer than 9 chars | accepted and stored everywhere; no cap in any layer | The RfC's cap has no enforcement point in our stack. |
| B4 | Tag with a forbidden char (`,` `>` `:` `!` `@` NUL) | accepted by both backends; a literal `:` or `>` would corrupt the `src>dst:msg` frame that our own decoder depends on | Pre-existing gap, not caused by hashtags. |
| B5 | Bare `#` as a destination (not a filter) | 1 char, passes every length check, classifies as `'person'` in the webapp | Is a bare `#` a legal destination or only a legal filter? Undecided in the RfC. |
| B6 | Empty tag `#` + nothing, or `##OE` | no validation anywhere | |
| B7 | Tag that is also a valid callsign shape after `#` is stripped | no collision today because `#` is never stripped | Worth keeping true. |

### 13.2 Routing and identity

| #   | Case                                                    | Behaviour today                                                                                                                                                         |
| --- | ------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B8  | Via-routed hashtag `RELAY-1,#OE-SOTA`                   | resolves correctly to `#OE-SOTA` in all four hand-rolled via-resolvers, because the RfC forbids `,` inside a tag. One place the design is genuinely safe.               |
| B9  | `#` in a URL path                                       | breaks navigation in the webapp at eight call sites, two mechanisms. See 5.2.                                                                                           |
| B10 | `#` as an IndexedDB key or JS object key                | safe — both accept arbitrary strings.                                                                                                                                   |
| B11 | `#` in a CSS selector                                   | not reachable: no selector is built from a `dst` value. `DestinationPicker` builds HTML `id` attributes from dst but consumes them by exact match, never as a selector. |
| B12 | `#` in rendered HTML                                    | safe — every dst render site uses Vue's escaping `{{ }}`; no `v-html` in the chat or settings scope.                                                                    |
| B13 | Same hashtag from two senders                           | fragments into two conversation buckets today (verified).                                                                                                               |
| B14 | Two hashtags sharing a prefix (`#OE-SOTA`, `#OE-FIELD`) | collapse into one bucket today (verified).                                                                                                                              |
| B15 | Sorting a mixed list                                    | `#` is ASCII 0x23, so all hashtags sort above all numeric groups under `localeCompare(..., {numeric:true})`.                                                            |

### 13.3 Messaging semantics

| #   | Case                                       | Behaviour today                                                                                                                                                                                                    |
| --- | ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| B16 | ACK expectation                            | a hashtag is classified as a DM, so mc-chat appends a `{NNN` ack-request to the body and then nothing ever answers it. Firmware has the same predicted defect.                                                     |
| B17 | `!command` addressed to a hashtag          | MCProxy: reaches no branch, silently dropped. mc-chat: answered locally, never forwarded. Both diverge from the numeric-group behaviour.                                                                           |
| B18 | Bot reply to a hashtag                     | expressible — the reply path treats the recipient as an opaque string. The blocker is upstream classification, not the wire.                                                                                       |
| B19 | Broadcast is never hashtag-filtered (US-4) | already true: `*` and `#` are disjoint branches everywhere.                                                                                                                                                        |
| B20 | Blocked sender posting to a hashtag        | dropped outright instead of quarantined to `9999`. Three repos, same gate.                                                                                                                                         |
| B21 | Link check targeting a hashtag             | MCProxy's `commands/linkcheck.py` rejects it cleanly via `CALLSIGN_STRICT_RE` ("Invalid target callsign"). Fail-closed, correct. But the REST endpoint's `max_length=9` would accept `#OE-SOTA` before that check. |
| B22 | Self-addressed hashtag                     | not reachable — the self-DM check is exact callsign equality.                                                                                                                                                      |

### 13.4 Subscription and filtering

| #   | Case                                          | Status                                                                                                                                                                                                                                                              |
| --- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B23 | Empty filter list                             | **undecided.** Default-open means every unconfigured node sees every tagged message; default-closed means the feature is invisible until configured. RfC section 10, item 4 flags this as open. Note mc-chat's current state is neither — it is an accidental drop. |
| B24 | `--setgrp` with no argument clears everything | a destructive command with no confirmation. The webapp already ships this footgun for `via`.                                                                                                                                                                        |
| B25 | Filter changed mid-stream                     | a subscribe POST replaces the stored filter wholesale, by design. The client must resolve stored prefs before POSTing.                                                                                                                                              |
| B26 | Backfill                                      | does a client that subscribes to `#OE` today see yesterday's `#OE-SOTA` messages? Storage keeps everything, so yes if history queries get a hashtag branch — but that is a decision, not a given.                                                                   |
| B27 | Filter matching nothing                       | no feedback anywhere. A live preview in the UI (3.3) is the mitigation.                                                                                                                                                                                             |
| B28 | Filter list exceeding the node buffer         | 96 chars on the node; the KEEP packet has **33** free bytes (recomputed in 1.4; the RfC's 37 is wrong). These two numbers do not agree — see 15.                                                                                                                    |

### 13.5 Infrastructure

| #   | Case                                                 | Status                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                             |
| --- | ---------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| B29 | BLE MTU on the inbound path                          | only **outbound** writes are length-checked in `ble_service`. Inbound notifications are forwarded as whatever D-Bus hands over, with one `json.loads()` per notification and **no multi-notification reassembly anywhere**. If a filter string grows the `I` register past the negotiated ATT MTU, the JSON is truncated at the GATT layer, `json.loads` throws, and **the entire register update is dropped** — not just the new field. Callsign, firmware version and the numeric groups all silently fail to update that cycle. |
| B30 | Outbound command framing                             | `_frame()` uses a 1-byte length field, so a command caps at 253 payload bytes. `--setgrp ` plus 96 chars is about 105 — fits. But `send_command` does no length pre-check (unlike `set_callsign`/`set_wifi`), so an oversized command raises `OverflowError` deep inside `int.to_bytes`.                                                                                                                                                                                                                                           |
| B31 | `#` needing escaping anywhere in our transport       | it does not. `#` is an ordinary byte to JSON, to the BLE framing and to the UDP allow-list.                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| B32 | Config commands are excluded from the offline outbox | a `--setgrp` sent while the phone briefly loses the Pi is discarded, not retried, with no distinct user feedback.                                                                                                                                                                                                                                                                                                                                                                                                                  |

---

## 14. Bugs found during this survey

These are defects in today's code. Most are independent of the hashtag decision and are worth fixing
whichever variant wins. Severity is my assessment, not the RfC's.

### 14.1 Reachable today with a `#`-prefixed destination

| #   | Severity | Bug                                                                                                                                                                                                                                                                                                          | Location                                                                       | Status                                               |
| --- | -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------ | ---------------------------------------------------- |
| G1  | high     | `compute_conversation_key` sends a hashtag to the DM branch and splits it on the first hyphen. Verified: `("DK5EN-9","#OE-SOTA")` → `"#OE<>DK5EN"`. Distinct hashtags collide; one hashtag fragments per sender.                                                                                             | `MCProxy/src/mcapp/storage/constants.py:146`                                   | **fixed** `ea15511`                                  |
| G2  | high     | Same defect, independently written.                                                                                                                                                                                                                                                                          | `mc-chat/meshcom_mock/storage.py:209`                                          | **fixed** `ab97405`                                  |
| G3  | high     | `normalizeCallsign` truncates any hyphenated destination. Feeds `matchesDst`, `sidebarKeyFor` and `findAckMessage`.                                                                                                                                                                                          | `webapp/src/utils/callsignUtils.ts:13`                                         | **fixed** `53cfac4`                                  |
| G4  | high     | Hashtag conversations are unreachable by navigation — vue-router splits `#` into a fragment. Eight call sites, two mechanisms.                                                                                                                                                                               | `webapp` — see 5.2                                                             | **fixed** `1ea2c3b`                                  |
| G5  | medium   | Blocked-sender traffic to a hashtag is dropped instead of quarantined to `9999`.                                                                                                                                                                                                                             | `MCProxy/src/mcapp/main.py:372`, `webapp/src/services/messageProcessor.ts:147` | **fixed** `ea15511` + `53cfac4`                      |
| G6  | medium   | MCProxy is completely deaf to hashtag-addressed commands — no branch is reached.                                                                                                                                                                                                                             | `MCProxy/src/mcapp/commands/routing.py:253`                                    | open — deliberate; new capability, not defect repair |
| G7  | medium   | mc-chat appends a `{NNN` ack-request to a hashtag send and then never sees an ack.                                                                                                                                                                                                                           | `mc-chat/meshcom_mock/chat.py:86` + `messaging.py:150`                         | **fixed** `ab97405`                                  |
| G8  | medium   | mc-chat drops every hashtag message before storage, so it never appears in coverage/arrivals on the node paths while the scraper and interlink paths would see it.                                                                                                                                           | `mc-chat/meshcom_mock/chat.py:67`                                              | **fixed** `ab97405`                                  |
| G9  | medium   | Hashtag messages lose their acks in the chat view — they take the branch that filters acks out, where the group branch deliberately keeps them.                                                                                                                                                              | `webapp/src/stores/messages/predicates.ts:149`                                 | **fixed** `53cfac4`                                  |
| G10 | medium   | `delete_messages` on a hashtag targets a corrupted conversation key.                                                                                                                                                                                                                                         | `MCProxy/src/mcapp/storage/prefs.py:140`                                       | **fixed** `ea15511`                                  |
| G11 | low      | `get_search_summary` restricts distinct destinations with `AND dst GLOB '[0-9]*'`, making hashtags invisible to that endpoint. User-facing symptom: the `!search` bot command prints a `Groups:` line from exactly this list (`commands/data_commands.py:39`), which would silently never mention a hashtag. | `MCProxy/src/mcapp/storage/query.py:943`                                       | **fixed** `ea15511`                                  |
| G12 | low      | A push for a hashtag opens the sender's DM thread.                                                                                                                                                                                                                                                           | `webapp/src/pwa/pushNotification.ts:124`                                       | **fixed** `53cfac4`                                  |

### 14.2 Pre-existing, independent of hashtags

| #   | Severity | Bug                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     | Location                                                                                  | Status                                                                                    |
| --- | -------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| P1  | medium   | **`CLAUDE.md` names a constant that does not exist in production code.** It instructs bumping `FINAL_SCHEMA_VERSION`; the real terminus constant is `LATEST_SCHEMA_VERSION` in `storage/constants.py:23`. `FINAL_SCHEMA_VERSION` exists only in `migration_chain_tests.py:41` as a hand-copied literal, re-exported to `connection_lifecycle_tests.py`. Three sync points, one of them a plain copy. Following the doc literally bumps the test constant and never touches the real one.                                                                                | `MCProxy/CLAUDE.md`, `storage/migration_chain_tests.py:41`                                | **fixed** `6ad9188`                                                                       |
| P2  | medium   | **`command_contract.json` has no sha256 pin**, unlike the other two contracts. An edit in place would pass the local parity suite trivially while diverging from mc-chat — defeating the entire purpose of the parity mechanism for that file.                                                                                                                                                                                                                                                                                                                          | `MCProxy/src/mcapp/contract_parity_tests.py:27`                                           | **fixed** `85ba4f4`                                                                       |
| P3  | low      | Both sides of the `command_contract.json` parity pair carry **stale docstrings pointing at paths that no longer exist** (`mc-chat/tests/fixtures/`, `src/mcapp/command_contract.json`). Both predate the subtree-prefix move that fixed the historical deletion trap.                                                                                                                                                                                                                                                                                                   | `MCProxy/src/mcapp/contract_parity_tests.py:9`, `mc-chat/tests/test_contract_parity.py:9` | **fixed** `6ad9188` (MCProxy side; mc-chat's copy still stale)                            |
| P4  | low      | `doc/database-reference.md` states schema version 21; the actual `LATEST_SCHEMA_VERSION` is 23.                                                                                                                                                                                                                                                                                                                                                                                                                                                                         | `MCProxy/doc/database-reference.md:97`                                                    | **fixed** `6ad9188`                                                                       |
| P5  | medium   | **No inbound BLE MTU guard.** Only outbound writes are length-checked; a truncated inbound `I` register drops the whole register update silently. See B29.                                                                                                                                                                                                                                                                                                                                                                                                              | `MCProxy/ble_service/src/ble_adapter.py:87` vs `:1229`                                    | **fixed** `1f5f2ee` — made loud; reassembly still absent by design                        |
| P6  | low      | `send_command` has no length pre-check, unlike `set_callsign`/`set_wifi`; an oversized command raises `OverflowError` inside `int.to_bytes`.                                                                                                                                                                                                                                                                                                                                                                                                                            | `MCProxy/ble_service/src/ble_adapter.py:360`                                              | **fixed** `1f5f2ee` (and `send_message`)                                                  |
| P7  | medium   | **`NodeGroupsCard.vue` has no component test.** CORRECTION: an earlier draft said "while all three sibling node-settings cards do" — that is wrong. Only **two of eleven** node-settings components have specs (`NodeAprsCard`, `NodeRadioCard`), so this card is not an anomaly; the area is broadly untested. It still matters more than its nine untested neighbours because it is the one card the RfC schedules for a rewrite (§7.8). Uncovered: the `0..99999` validation, revert-on-invalid, per-index ack wiring, and the resend-all-six-on-any-edit behaviour. | `webapp/src/components/bluetooth/node-settings/`                                          | tracked — webapp `docs/backlog.md` B3                                                     |
| P8  | medium   | **The `via` field ships an unconfirmed destructive clear**: emptying it and blurring sends `--via NONE` immediately, no confirmation. This is the pattern a naive `--setgrp` implementation would copy.                                                                                                                                                                                                                                                                                                                                                                 | `webapp/src/utils/firmwareCommands.ts:81`, `NodeRadioCard.vue:134`                        | **fixed** `ca259cd`                                                                       |
| P9  | low      | `recentDestinations()` scans `src`, never `dst`, so no destination that has only been _received_ ever appears in the picker's "Recent" list. Pre-existing for numeric groups.                                                                                                                                                                                                                                                                                                                                                                                           | `webapp/src/utils/recentDestinations.ts:40`                                               | open                                                                                      |
| P10 | low      | mc-chat's `POST /api/send` — the only route that transmits to the real mesh — has **no pydantic request model at all**; a missing `dst` silently becomes a broadcast.                                                                                                                                                                                                                                                                                                                                                                                                   | `mc-chat/meshcom_mock/api.py:346`                                                         | **fixed** `ab97405`                                                                       |
| P11 | low      | mc-chat's KEEP packet has no size cap where the firmware has `char keep_buffer[60]`; mc-chat can emit a KEEP no real server could parse, and neither its encoder nor its decoder would flag it.                                                                                                                                                                                                                                                                                                                                                                         | `mc-chat/meshcom_mock/protocol.py:40`                                                     | open                                                                                      |
| P12 | low      | `restore_database()` returns `False` silently with no log if a slot has no snapshot; `_do_rollback` has no `else` branch. Narrow reachability, but a silent failure.                                                                                                                                                                                                                                                                                                                                                                                                    | `MCProxy/scripts/update-runner.py:272`                                                    | open                                                                                      |
| P13 | info     | A manual rollback overwrites the live DB with the pre-update snapshot, silently discarding everything ingested since. Safe for schema, lossy for data, with no warning surfaced in the code path examined.                                                                                                                                                                                                                                                                                                                                                              | `MCProxy/scripts/update-runner.py:753`                                                    | open                                                                                      |
| P14 | low      | Prettier has zero automation in MCProxy — no `.prettierrc`, no root `package.json`, no CI step. It exists only as a `CLAUDE.md` instruction.                                                                                                                                                                                                                                                                                                                                                                                                                            | `MCProxy/.github/workflows/tests.yml`                                                     | open                                                                                      |
| P15 | info     | Via-resolution ("last comma component") is hand-rolled in at least four places with no shared helper.                                                                                                                                                                                                                                                                                                                                                                                                                                                                   | see 7, item 10                                                                            | partial — `resolve_dst_target` shared in both backends; `push_delivery.py` still separate |
| P16 | low      | The webapp's cross-repo drift checks (push mirror and all four vendored corpora) **self-skip on CI**, because the workflow checks out one repo with no siblings. They pass locally and are silently absent from the gate. Wiring them in is a small change, not a new mechanism.                                                                                                                                                                                                                                                                                        | `webapp/src/pwa/pushFilter.ts`                                                            | **fixed** `ca259cd` — unverified until it runs on CI                                      |

---

## 15. What is missing in the concept

The RfC is unusually honest — it lists twelve of its own side effects and four open questions. What
follows is what it does _not_ address. Ordered by how much trouble each one causes if left unanswered.

### 15.1 The prefix rule is self-contradictory as written

US-3 gives three worked examples: `#OE` must match `#OE1`, must match `#OE-SOTA`, must **not** match
`#OEM`. And it states the rule as "the prefix comparison stops at a tag boundary". But `#OE1` has no
boundary character — `1` follows `OE` directly, exactly as `M` does in `#OEM`. **The stated rule and
the stated examples disagree.**

The only rule satisfying all three examples is "the next character must not be a letter", which is a
strange rule and one nobody would guess. It also means `#OE` matches `#OE1` but not `#OEM`, while
`#DL` would match `#DL1` but not `#DLARC` — which happens to be the very collision the RfC cites as
its motivation, so the intent is probably right and the wording is wrong. **This must be settled before
any of the four implementations is written**, because all four will encode whatever guess their author
made.

### 15.2 No namespace governance at all

The RfC names squatting and typos as side effect 7 and then leaves it. Unanswered:

- Who owns `#EMCOM`? What stops a second station using `#EMCOMM`?
- Are country prefixes (`#OE`, `#DL`) reserved, and by whom?
- How does a user **discover** that `#OE-SOTA` exists? There is no registry, no directory, no
  announcement mechanism. Our own UI cannot help much — the destination picker's "recent" list only
  surfaces callsigns that have _sent_ something, never destinations.
- Is there a reserved namespace for system use? Our stack already needs one: the blocklist quarantine
  sentinel is group `9999`, and there is no `#`-equivalent.

Without any of this, the first month of the feature is a landgrab, and the second is a support load.

### 15.3 The capability signal already exists and the RfC does not use it

The RfC's entire rollout argument rests on "we cannot know which nodes are 4.36". But every APRS frame
already carries a firmware-version byte (`msg_source_fw_version`, written by `shortVERSION()`), decode
already rejects anything below version 35, and the byte is **already exposed to MCProxy** as the
`firmware` field of the Extern-UDP JSON — as a **string** for self-originated frames and a **number**
for relayed ones (`extudp_functions.cpp:508`), and only for the originating sender, never a relay-only
node. With those two caveats a backend could use it today with no new
protocol surface at all. This changes the rollout calculus materially and is not mentioned anywhere.

### 15.4 Stufe 1 is cheaper than the RfC thinks

`checkRegexCall()` already contains a literal allow-list — `WLNK-1`, `APRS2SOTA`, `OE2YOTA-1`, `TEST`,
`TESTER`, `BOT GATE`, `H`, `HG` — consulted before the regex runs. The relay-tolerance change is
therefore a one-line addition following an established pattern, not a regex modification. The RfC
quotes the regex in isolation and never mentions the dispatcher exists. This lowers the risk of its own
recommendation 3 substantially and should be in the proposal.

### 15.5 The KEEP problem is stated but not solved

The RfC correctly identifies that a 96-character filter list does not fit the KEEP packet, and then
offers "enlarge the buffer (server must follow) or define a separate registration packet" without
choosing. That is the single hardest dependency in the whole proposal, because it is the only part that
requires the backbone server team to write code. Also: the arithmetic is wrong — the fixed prefix is 26
bytes, not 22, so **33 bytes remain, not 37**. And there is no discussion of what happens when a node's
filter list exceeds whatever the KEEP can carry: silent truncation, refusal, or a partial registration?
And note that today's **numeric** `grc_ids` can already reach 36 bytes for six fully populated five-digit
groups (`udp_functions.cpp:1064`, `"<num>;"` per group) against those 33 free bytes — the buffer may
already be over-subscribable before hashtags enter the picture at all.

### 15.6 Default open or default closed is deferred, but it is not one decision

The RfC flags this as open question 1. It is actually at least three separate decisions:

- the **node's** behaviour with an empty filter list (the firmware's `CheckOwnGroup()` precedent is
  default-open);
- the **backend's** behaviour for a subscriber with an empty hashtag list (the existing push filter's
  shape is default-**closed** by construction — an empty `groups` list matches nothing);
- the **UI's** default for a new user.

If a hashtag filter is bolted onto the existing push `groups` field, it silently inherits default-closed
and contradicts the firmware precedent the RfC anchors on. Three surfaces, three defaults, one
undeclared decision.

### 15.7 Nothing is said about what a tag means over time

A numeric group is permanent by fiat. A tag is a string somebody typed once. The RfC has no position on:

- retiring a tag that nobody uses;
- two communities converging on the same tag for different purposes;
- whether a tag is case-normalised only on send (US-1) or also on receive — our stack would store
  `#oe-sota` and `#OE-SOTA` as two different conversations;
- whether a message can be re-tagged, forwarded under a different tag, or carry no tag at all.

### 15.8 The one-tag-per-message limit is asserted, not argued

US-1 states "exactly one tag" as an acceptance criterion with no rationale. It is presumably a wire
constraint (one destination field), but it is the design decision most likely to be regretted: a SOTA
activation in OE is plausibly both `#OE` and `#SOTA`, and the prefix mechanism only expresses hierarchy
in one dimension. Worth stating the reason so it can be revisited deliberately rather than accidentally.

### 15.9 Migration between the two worlds is left open

The RfC's open question 4 asks "parallel or migration" and stops. Concretely unanswered: does a station
posting to `#OE-SOTA` also reach subscribers of numeric group 232 who consider it the same channel? Is
there any bridging, aliasing or dual-posting? If not, the community fragments into two parallel
addressing schemes for the same hashtags — which is a worse outcome than either scheme alone.

### 15.9a Whole categories the concept does not touch

- **Accessibility.** The RfC proposes replacing six numeric fields with a free-text filter list in three
  firmware UIs, and our own proposal in 3.3 adds a chip widget and colour cues. Nothing anywhere says
  anything about screen readers or keyboard operation. Our side has no excuse for this: the component
  being extended, `DestinationPicker.vue`, is already a full ARIA combobox, so the pattern to follow
  exists — it just has to be followed.
- **Language.** The RfC is in German, the user base is largely German-speaking, and the webapp has no
  i18n infrastructure at all. Every string this feature adds — filter syntax help, the boundary rule,
  error messages for a malformed tag — would ship English-only. The boundary rule in particular is hard
  enough to explain in one's own language.
- **Performance on the actual hardware.** `mcapp.local` is a Pi Zero 2W, and this repo already contains
  a measurement of a full-table scan at 4.3 s (`doc/2026-03-14_sqlite-performance-analyse.md`). The
  hashtag-directory and live-preview ideas in section 20 assume aggregate queries over the message set
  are cheap. Under today's ten-to-twenty distinct groups that is probably true; under spontaneous,
  unbounded tag cardinality it needs re-measuring, not assuming. The same applies to index growth on
  `idx_messages_dst` and `idx_messages_convkey_ts`.
- **Free tag creation as an attack surface.** The RfC treats a bare `#` filter as "the curious". Combined
  with default-open and zero-cost spontaneous tag creation, it is also a zero-setup way to put content in
  front of every unconfigured node — something numeric groups made mildly awkward by requiring a number
  somebody had to learn. No threat model, no rate limiting, no abuse discussion appears anywhere.

### 15.10 Smaller gaps

- **The airtime severity figure is doubtful and should be recomputed.** The RfC's 9 ms/byte does not match the
  firmware's own SF11/BW250/CR6 comment, which implies about 23.5 ms/byte average. The severity
  argument should be recomputed from a real time-on-air formula.
- **The display header failure is worse than described.** The same `CheckGroup()` that fails on a tag
  also selects the `GM` versus `DM` header at four sites. Unpatched, a tagged message renders as
  `DM <sender>` — a _wrong_ header, not merely a truncated one.
- **`CheckOwnGroup()` and `CheckGroup()` are conflated.** The RfC says one condition is duplicated at
  three sites. It is not: `CheckOwnGroup()` has exactly one call site; the other two use `CheckGroup()`,
  a different function with different semantics. Any patch plan derived from that paragraph applies the
  wrong fix at two of three sites.
- **No ACK reaches Extern-UDP at all.** `MSG_TYPE_ACK` is never serialised there. A UDP-attached
  backend has no ACK channel for any message type. The RfC's discussion of server ACKs is BLE-only in
  practice, and that is not stated.
- **`#` is already in the wire format** in two other roles — as the default placeholder byte for the
  firmware sub-version trailer, and as the default APRS position symbol. Neither collides with the
  destination field, but both mean a raw-buffer scan for `#` would produce false positives.
- **Nothing about rate limiting.** Spontaneous tag creation plus a bare `#` "see everything" filter is
  an amplification path nobody has costed.

---

## 16. Named dependencies outside our three repos

Everything in this section is a question to put to another team, not work we can schedule.

### 16.1 Firmware — blocking, and blocking earlier than it looks

1. **`decodeAPRS()` is a single shared choke point.** Relay, BLE-to-phone, MHeard _and_ Extern-UDP all
   depend on it. `sendExtern()` calls it and returns on `0x00`. **Until it accepts `#`, MCProxy
   receives nothing for hashtag traffic — not even from its own attached node.** No MCProxy-side
   Variant A work can be end-to-end tested against hardware before that lands. This is the top
   scheduling constraint for the whole programme.
2. Will Stufe 1 (relay tolerance) actually be cut as its own maintenance release, and when? Note it is
   a one-line allow-list addition (15.4), not a regex change.
3. What is the final tag length — 9 including `#`, i.e. 8 body characters? The RfC's section 4 says one
   thing and its section 7.3 derives another; section 4 is the correct one.
4. Default open or default closed on an empty filter list?
5. What is the new BLE/`I`-register key name for the filter list? Our side is a verbatim passthrough,
   so the name has to be agreed, not discovered. **This is the cheapest possible question to answer and
   it blocks our node-settings UI.**
6. Will the `I` register with the filter string appended stay inside the negotiated ATT MTU? Our BLE
   layer has **no inbound reassembly** — an oversized register JSON is truncated at the GATT layer and
   the entire register update is lost, not just the new field. See B29.
7. Will `getMessagePriority()` and the gateway-ACK allow-list be fixed in the same release as the
   filter, or will tagged messages ship as CRITICAL-priority un-ACKed traffic for a window?

### 16.2 MeshCom backbone server — the hardest dependency

1. Will `sendKEEP()` carry the filter list, and how, given only 33 bytes remain in a 60-byte buffer?
   Enlarged buffer, or a new registration packet? Either way the server must move first or in step.
2. Without server-side tag support there is **no distribution filtering in the backbone at all** —
   every tagged message goes everywhere the gateway mesh reaches. Is that acceptable for a launch?
3. What happens to a node whose filter list exceeds what KEEP can carry — truncation, refusal, partial
   registration?
4. Does the dashboard need to display tags, and does anything there parse the destination field?

### 16.2a A scope ambiguity worth settling early

US-6's second acceptance criterion says "Web-UI und T-Deck/T-Deck-Pro-UI bieten ein Textfeld fuer die
Filterliste". Read together with RfC section 7.8, "Web-UI" there means **the ESP32 firmware's own
onboard configuration page** (`web_functions.cpp`), grouped with the two T-Deck UIs as one of three
firmware UIs. It does **not** mean our Vue webapp, which the RfC never mentions. Sections 3 and 5 of this
document therefore do **not** satisfy US-6 — they address different software. Worth confirming with the
author, because a reader could easily conclude otherwise.

### 16.3 Phone apps (iOS / Android)

1. They receive the raw APRS frame via `addBLEOutBuffer()` and parse the destination themselves.
   Without an app update they show tagged messages wrongly or not at all. Is there a commitment?
2. Would they adopt the same tag-boundary prefix rule, and from what specification? There is currently
   no normative text for it anywhere — section 9.1 of this document is a first attempt.

### 16.4 APRS-IS

`#` is not legal in an APRS callsign field. The RfC notes this and stops. What is the intended
behaviour for a gateway bridging tagged traffic to APRS-IS — drop, rewrite, or refuse to gate? No APRS-IS
integration exists in the firmware repo, so this is a question for whoever operates the gateways.

### 16.5 The oevsv.at internet feed

Not a team to ask, but a structural fact: the webapp connects to it directly, bypassing both backends.
**No server-side hashtag filter can ever apply to that feed**, and Web Push can never cover
internet-feed-only content. Any promise about filtering has to be qualified accordingly.

---

## 17. Rollout ordering

Dependency-ordered. Steps 1-3 are independent of the RfC's outcome and can start now.

**Phase 0 — pay down what is already broken (no decision needed) — DONE**

Completed 2026-08-20 across all three repos. Commits: MCProxy `ea15511`, `1f5f2ee`, `6ad9188`;
mc-chat `ab97405`; webapp `53cfac4`, `1ea2c3b`, `ca259cd`. All three gates green. Items 1 and 2 are
fully done; item 3 is done except the `command_contract.json` sha256 pin (P2) and the missing
`NodeGroupsCard.spec.ts` (P7), both still open.

1. Fix the identity defects: the conversation-key hyphen split in both backends, `normalizeCallsign`
   misuse in the webapp, the blocklist quarantine asymmetry. Add hashtag vectors to
   `conversation_key_vectors.json` and `group_dst_vectors.json` in the same change and hand-copy to all
   consumers.
2. Fix routing: move the **seven** vue-router sites to the object form, and `encodeURIComponent` the
   **eighth**, the service-worker URL in `pwa/pushNotification.ts` — it is a real URL string, not a
   vue-router location, so the object form does not apply to it.
3. Fix the infrastructure defects: the `FINAL_SCHEMA_VERSION` naming trap in `CLAUDE.md`, the missing
   sha256 pin on `command_contract.json`, the stale parity docstrings, the `database-reference.md`
   version drift, and add the missing `NodeGroupsCard.spec.ts`.

**Phase 1 — Variant C, shippable unilaterally — NOT STARTED**

4. Slot expansion 6 to 20 (`NodeGroupsCard.vue`, two lines plus layout).
5. Alias table, client-only. Ship. Decide later whether to sync it to the backend.

**Phase 2 — only once the firmware and server commitments exist — NOT STARTED**

Step 6 (settling the prefix rule) still gates everything here and remains open. What the defensive
slice shipped is **classification only**: a `#TAG` is recognised, keyed and routed correctly in all
three repos, but no subscription or prefix matching exists anywhere.

6. Settle the prefix rule (15.1) and write it down normatively. Nothing else starts before this.
7. **mc-chat first**: edit `push_contract.json` to v8 with the new filter dimension and the
   `hashtag_match_semantics` clause; bump its own `_EXPECTED_SHA256` in the same commit;
   `git subtree split --prefix=contract -b contract-subtree`.
8. **MCProxy**: `git subtree pull --prefix=src/mcapp/contract mc-chat contract-subtree --squash`, and
   update `_EXPECTED_SHA256` in `push_tests.py` **in the same change as the pull**.
9. Both backends: the hashtag predicate, the prefix matcher, the conversation-key branch, the routing and
   suppression branches, the schema migration for per-device subscriptions, the new test suite.
10. webapp: predicates, components, stores, push mirror, node-settings field.
11. Both gated runners green independently. Nothing cross-checks this — it is a manual coordination
    point.
12. Deploy. MCProxy and webapp ship as one combined tarball via `scripts/release.sh`; **mc-chat is a
    separate target with its own release process**, so the two are not atomically ordered. Only the
    contract-parity gates enforce that each side agrees with the shared corpus.

**Notes on deployment safety.** The DB migration runs automatically on service start — there is no
separate migration step. A schema change does **not** require a `SYSTEM_EPOCH` bump; that epoch covers
packages, firewall and the web front door only. Rollback is schema-safe by construction, because the
update runner restores the pre-update DB snapshot — but it silently discards everything ingested since
the update.

---

## 18. Test plan

### 18.1 New vectors in existing corpora

| Corpus                            | Add                                                                                                                    |
| --------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `group_dst_vectors.json`          | hashtag-shaped destinations against `is_group` (all should be false — `is_group` stays numeric)                        |
| a new hashtag corpus              | the full truth table from 9.1, including the disputed `#OE`/`#OE1` case                                                |
| `conversation_key_vectors.json`   | `("DK5EN-9","#OE-SOTA")`, `("OE3ABC-1","#OE-SOTA")` — same key; `#OE-FIELD` — different key; via-routed `RELAY-1,#OE1` |
| `directed_dst_vectors.json`       | hashtag destinations must classify as **not** directed                                                                 |
| `blocklist_decision_vectors.json` | blocked sender to a hashtag — redirect or drop, per the decision in 19                                                 |
| `command_contract.json`           | suppression with `dst: "#OE-SOTA"`, with and without an explicit `target:`                                             |
| `dedup_contract.json`             | one vector substituting a hashtag for `"232"` as insurance                                                             |
| `push_contract.json` v8           | the 9.1 truth table as `match_vectors`                                                                                 |

### 18.2 New suites

- A prefix-matcher suite in each backend, replaying the shared truth table. Register it in
  `scripts/run_startup_tests.py`'s `main()` — **a suite not wired in there is gated by nothing.**
- A `--setgrp` command-builder suite in the webapp.
- `NodeGroupsCard.spec.ts`, which does not exist today at all.

### 18.3 Regression tests for the defects in 14.1

Each gets a test that fails before and passes after. The conversation-key ones are the highest value —
they are the difference between "hashtag groups work" and "hashtag groups look like broken DMs".

### 18.4 Things that cannot be tested until firmware moves

Per 1.4: `sendExtern()` is gated by `decodeAPRS()`, so **no Variant A path can be exercised against
real hardware until the firmware allow-list change lands.** Until then, backend work is testable only
against synthetic frames and mc-chat. Plan for that explicitly rather than discovering it during
integration.

### 18.5 Gate reminders

Four gates, all mandatory: `uvx ruff@0.16.0 check .`, `uvx ruff@0.16.0 format --check .`,
`uv run mypy src/mcapp ble_service/src` (after `uv sync --all-packages`), and
`uv run python scripts/run_startup_tests.py`. Note that `ruff format` also formats fenced Python blocks
**inside `.md` files** — a docs-only commit has turned CI red twice for exactly that. Run prettier
first, then the ruff format check, on every file including documentation.

---

## 19. Open decisions — these need a human

Ordered by how much downstream work they block. Four of these already carry a worked recommendation
from the evidence in this document and need **confirmation, not deliberation** — they are marked
**[proposed]**. The rest are genuinely undecided. Leave a resolution line under each as the discussion
settles it, so this section stops being a snapshot: `resolved YYYY-MM-DD: <answer> <link>`.

1. **[proposed]** **The prefix rule.** `#OE` versus `#OE1` versus `#OEM`: is the boundary "next char is `-` or end",
   or "next char is not a letter"? The RfC's rule and its examples disagree. **Blocks everything.**
2. **Which variant.** A, B, C, or C-now-A-later. This document recommends C now and A conditionally.
3. **[proposed]** **One predicate or two.** Widen `is_group()` to mean "not a personal destination", or add
   `is_hashtag()` beside it? Widening changes a contract-pinned predicate mirrored in three repos —
   itself a flag day. Recommendation: a sibling predicate plus a `dst_kind()` helper, and consolidate
   the four hand-rolled via-resolvers at the same time.
4. **Default open or default closed** — separately for the node, the backend and the UI. See 15.6.
5. **Per-device identity for the live stream.** Does the hashtag filter apply only to push-enabled
   devices (simple, leaves live-tab filtering unsolved), or do we introduce a `device_id` on
   `GET /events`? The push `endpoint` is the only durable per-device handle either backend has today.
6. **Backfill on subscribe.** Does a client that adds `#OE` today see yesterday's `#OE-SOTA` messages?
   Push has no backfill by construction; the SSE initial burst has no per-client filter at all.
7. **[proposed]** **New filter field or overloaded `groups[]`** in the push contract. Recommendation: a separate
   `hashtags[]`, because overloading silently changes semantics for existing subscribers.
8. **Validation boundary.** Do the backends reject a malformed tag, or stay dumb pass-throughs? Today
   neither validates any destination at all. If validation is added, does it apply only to `#`-prefixed
   values, or does it finally constrain `dst` generally?
9. **Quarantine for blocked senders on a hashtag** — drop, or redirect to a sentinel? If redirect, a `#`
   sentinel has to be reserved, and that is a namespace decision the RfC does not cover.
10. **Normalisation on receive.** Do we uppercase `dst` on ingest? Today we deliberately do not, because
    of case-sensitive sentinels like `Time`. Without it, `#oe-sota` and `#OE-SOTA` are two conversations.
11. **[proposed]** **Where the alias map lives** if Variant C ships — client-only or backend-synced.
12. **New corpus placement.** Does a hashtag-matching corpus join the manually vendored set (no hash pin,
    no sync automation) or the subtree set? The manual set is where drift already has no detector.

---

## 20. Creative thinking — idea collection

Pure collection, no judgement, no prioritisation. Everything surfaced during the survey that is not
required by the RfC but might be worth something later. To be triaged separately.

### 20.1 Making tags discoverable

- **A tag directory built from observation.** Both backends already store every message. A "hashtags seen
  in the last 30 days, with message counts and last-heard" view needs no new protocol — it is a
  `GROUP BY` over data we already have. This is the answer to "how does anyone find out `#OE-SOTA`
  exists" that the RfC leaves open. Needs no protocol change.
- **Live filter preview.** When a user types a filter, show which hashtags it currently matches from the
  local message set. Turns the counter-intuitive boundary rule into something visible.
- **Suggest tags from content.** The classifier already computes categories and template hashes. A
  "messages like this are usually tagged X" hint is a small step from what exists.
- **Trending hashtags.** Sparkline per hashtag, reusing `BaseSparkline.vue`.
- **Import a tag list from a QR code or a shared link**, so a SOTA group can hand out its filter set at
  a meeting.

### 20.2 Using the capability signal we already have

- **A per-node firmware-version map.** The `firmware` field arrives on every Extern-UDP message. We
  could build "which nodes in my area are 4.36+" from passive observation alone, and show a coverage
  view, built from passive observation.
- **Warn before sending.** If the local node is below the tag-capable version, or if no 4.36+ node has
  been heard recently, tell the user their tagged message will go nowhere before they send it.
- **Automatic dual-posting during the transition.** Send tagged for new nodes and to the equivalent
  numeric group for old ones, deduplicated on receipt. Expensive in airtime, but it is a bridge.

### 20.3 Alternative and hybrid addressing schemes

- **Tag in the payload, number in the destination.** `OE1KBC-24>232:#OE-SOTA TEXT`. Old nodes see a
  normal group message and relay it; new nodes get a sub-channel inside an existing group. Combines B's
  compatibility with A's server-filterability at the group granularity, and sidesteps the ordinary-chat-
  hashtag ambiguity because a tag is only meaningful inside a group.
- **A community-run number-to-name registry**, published as a JSON file the way `sperrliste.json`
  already is — fetched over HTTPS, refreshed in the background, distributed to clients via SSE. This
  gives speaking names _and_ a shared namespace with zero protocol change, and we already have the
  entire delivery mechanism built and running for the blocklist.
- **Hierarchical numeric groups.** `2xx` = OE, `23x` = OE3. Pure convention, no code, delivers the
  hierarchy the RfC wants from prefix matching.
- **Tag as a short hash of a name.** `#OE-SOTA` hashed to a 5-digit number, so the wire stays numeric
  and clients display the name. Collision-prone, but it makes the wire format a non-issue.

### 20.4 Filtering ideas beyond the RfC

- **Filters with a time window.** Subscribe to `#CONTEST` for this weekend only, then auto-expire.
- **Negative filters.** `-#SPAM` alongside positive ones.
- **Per-hashtag notification levels** — some hashtags push, some only badge, some are silent. The push
  contract already has a per-subscription filter; this is a shape change, not a new mechanism.
- **Filter presets.** "Emergency only", "Everything local", "Contest weekend".
- **A saved-search concept** unifying hashtag filters, the classifier's spam filter and the free-text
  search into one "views" abstraction, since all three are predicates over the same message set.

### 20.5 Things the survey suggests we should build anyway

- **One `dst_kind()` helper per repo, replacing four hand-rolled via-resolvers and a scattering of
  ad-hoc predicates.** Every finding in section 7 and section 8 traces back to the same classification
  question being answered independently in a dozen places.
- **A shared "destination semantics" corpus** covering kind, conversation key, directedness and
  blocklist decision in one file, replacing four separate manually vendored corpora with no drift
  detection. Possibly moved into the subtree so it syncs automatically.
- **Wire the webapp's existing cross-repo drift checks into CI.** They already exist and pass locally;
  they self-skip on CI for want of sibling checkouts. Small change, real gate.
- **Contract-drift CI across repos**, so a forgotten hand-copy fails somewhere instead of nowhere.
- **An inbound BLE MTU guard and reassembly**, since today a truncated register silently loses the
  whole update.
- **Destination validation as a first-class concept**, given nothing anywhere validates one today.
- **A "why am I not seeing this" diagnostic view** — given the MCProxy/mc-chat/internet-feed asymmetry
  in 9.3, users will hit invisible reception gaps and have no way to tell a filter problem from a node
  configuration problem from a relay hole.

### 20.6 UI ideas parked for later

- Colour-code hashtags the way callsigns already are, deterministically from the tag string.
- A hashtag-based map layer: show stations that have posted to a hashtag recently.
- Per-hashtag mute with a snooze duration.
- A compose-time airtime meter showing what the tag costs, since under Variant B it is 6-7 % of the
  budget and users have no way to feel that.
- Show the node's filter list and the app's filter list side by side, with a diff, so the distinction
  between "my radio won't hear it" and "my app hides it" is visible rather than folklore.
- Let a user "adopt" a numeric group under a speaking name and have that name travel with them across
  devices via the existing prefs sync.

### 20.7 Wilder ideas, recorded without comment

- Tags as a lightweight pub/sub for telemetry and sensor data, not just chat.
- A well-known `#SOS` tag with a distinct notification treatment and no filter suppression, ever.
- Tag-scoped bot commands — `!wx` answered into the hashtag it was asked in.
- A per-hashtag message-retention policy, so a busy contest tag does not evict a year of DMs.
- Federating our hashtag directory between mcapp.local and rpizero.local so two boxes agree on what
  exists.
- Deriving suggested tags from the classifier's existing template clusters — the beacon templates are
  already, in effect, unnamed hashtags.
