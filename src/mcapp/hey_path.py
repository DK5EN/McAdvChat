"""Pure parser for the per-hop signal-report chain in a MeshCom HEY beacon's
`PP` field (BLE `TYP: "MH"` register, `payload_type == '@'`).

Wire format, verbatim from firmware (do not re-derive — these citations are
the source of truth):

    R<ncnt>;                  written by the originating station: its own
                               neighbour count (`sendHey()`, loop_functions.cpp:4242)
    <ncnt>,<rssi>,<snr>;      appended by each relay, one group per hop, in
                               transmission order (`appendHeySignalReport()`,
                               aprs_functions.cpp:1127)

Example: `R12;8,101,-7;15,95,5;`

Five deliberate firmware behaviours a naive parser gets wrong, all honoured
here:

1. **RSSI is a POSITIVE MAGNITUDE on the wire.** `appendHeySignalReport()`
   emits `String(rssi*-1.0, 0)` — `-101 dBm` appears as `101`. `HeyHop.rssi`
   is the NEGATED value (real dBm); trusting the wire sign inverts every
   reading in a way that still looks physically plausible. This is the
   single most important thing this module gets right.
2. **Legacy-format detection is by COMMA COUNT in the leading token, not by
   field count** (`mheard_functions.cpp:436-451`): the firmware counts the
   commas between `R` and the first `;`. **0 commas** (`R99;`) and
   **2 commas** (`R99,99,99;`) are both valid; **1 comma** (`R99,99;`) is
   INVALID and rejected — that is the firmware's own rule, not an inferred
   one.
3. In the 2-comma legacy shape, the firmware keeps only the leading integer
   as the neighbour count and DISCARDS the other two values. This module
   mirrors that: `HeyChain.legacy` records that the shape was seen, and the
   two discarded values never become a `HeyHop` (nor are they validated —
   they are thrown away exactly as the firmware throws them away).
4. **The chain stops before our own hop.** It describes the path up to and
   including the last relay; the link from that last relay to us is carried
   in the MH register's own top-level `RSSI`/`SNR` fields, not in `PP`.
   Callers must not double-count: appending the MH register's own
   RSSI/SNR as an implicit final hop is a caller error, not something this
   module does for you.
5. **The chain carries no callsigns.** The result is an ordered list of
   `(ncnt, rssi, snr)` and nothing that identifies which station each hop
   was. A caller can say at which POSITION in the path a link was weak,
   never which station — do not attribute a hop to any callsign.

Two more facts about how this field arrives in practice (both new as of
firmware 2026-08-28), neither of which this module can or should work
around:

* The firmware caps the on-air chain at `HEY_PATH_PAYLOAD_MAX` = 106 chars;
  `appendHeySignalReport()` returns early instead of appending a group that
  would exceed it. So a real chain can legitimately be SHORTER than
  `origin_ncnt` implies, and on the wire it always ends cleanly at a group
  boundary — but this parser does not rely on that guarantee for its own
  correctness, since the input is unauthenticated.
* The firmware drops the whole `PP` field from the MH register when the
  register's JSON would exceed 244 chars, which starts happening from
  roughly 5 relay hops onward. So an ABSENT `PP` field means nothing about
  the real hop count — callers must not read "no chain" as "no relays".
  Handling that absence is the caller's problem; this module only parses
  the string when one is present.

**Out-of-range values (e.g. `abs(rssi) > 200`, `abs(snr) > 60`) are kept,
not rejected.** This is a decode layer, not a validity gate: MCProxy's
storage ingest already range-guards signal values before persisting them
(`VALID_RSSI_RANGE` / `VALID_SNR_RANGE` in `src/mcapp/storage/constants.py`).
Adding a second, differently-tuned range check here would only create a
second place that can disagree with the first — a physically absurd
`HeyHop` is a valid parse of the string, and it is up to a caller that
persists or displays it to apply whichever range policy is authoritative
there.

No I/O, no storage, no global state: `parse_hey_chain()` only turns a `PP`
string into a `HeyChain` or `None`. This decodes data taken straight off an
unauthenticated RF link, so it must never raise for any input, however
malformed, hostile, or pathologically large, and the work it does is bounded
regardless of input size (see `_MAX_PAYLOAD_LEN`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Generous over any real on-air payload: the firmware caps the chain itself
# at HEY_PATH_PAYLOAD_MAX = 106 chars, and the whole MH register JSON (of
# which PP is one field) is dropped past 244 chars. This cap exists purely
# to bound the work done on an adversarial string — parsing itself is
# already linear in input length (one split, one pass), so this is
# defense-in-depth against a hostile multi-megabyte payload, not a fix for
# an algorithmic blowup.
_MAX_PAYLOAD_LEN = 4096

# Comma count in the leading `R<ncnt>...` token that marks the legacy shape
# (`mheard_functions.cpp:436-451`): `R<ncnt>,<discarded>,<discarded>;`.
_LEGACY_LEADING_COMMAS = 2

# Every hop group is exactly `<ncnt>,<rssi>,<snr>` — three fields.
_HOP_GROUP_FIELDS = 3

# Strict integer token: optional leading '-', one or more ASCII digits only.
# Deliberately NOT Python's int()/`\d` directly: int() also accepts a
# leading '+', internal '_' digit-group separators, and surrounding
# whitespace, and `\d` matches every Unicode Nd digit (e.g. Arabic-Indic
# ٠-٩) that the firmware never emits — linkcheck.py's `_PONG_RE` makes the
# identical choice for the identical reason.
_INT_RE = re.compile(r"^-?[0-9]+$")


@dataclass(frozen=True, slots=True)
class HeyHop:
    """One relay's report of the hop it heard, in transmission order."""

    ncnt: int
    """That relay's own neighbour count."""
    rssi: int
    """dBm, ALREADY NEGATED from the wire's positive magnitude (see module
    docstring, point 1)."""
    snr: int
    """dB, as transmitted — the firmware does not sign-invert SNR."""


@dataclass(frozen=True, slots=True)
class HeyChain:
    """A parsed `PP` signal-report chain. See module docstring for the five
    firmware behaviours this decodes (positive-magnitude RSSI, legacy-shape
    detection, discarded legacy fields, chain-stops-before-us, no
    callsigns)."""

    origin_ncnt: int
    """The leading `R<ncnt>` — the originating station's own neighbour count."""
    hops: tuple[HeyHop, ...]
    """One per relay, transmission order. May be empty (`R12;` alone)."""
    legacy: bool
    """True iff the leading token was the 2-comma legacy shape
    (`R<ncnt>,<discarded>,<discarded>;`)."""

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable rendering, e.g. for an SSE/REST payload."""
        return {
            "origin_ncnt": self.origin_ncnt,
            "legacy": self.legacy,
            "hops": [{"ncnt": h.ncnt, "rssi": h.rssi, "snr": h.snr} for h in self.hops],
        }


def _parse_int(token: str) -> int | None:
    """Strict integer parse: `None` for anything `_INT_RE` rejects, never raises."""
    if not _INT_RE.fullmatch(token):
        return None
    return int(token)


def _parse_leading_token(token: str) -> tuple[int, bool] | None:
    """Parse the `R<ncnt>...` token. Returns `(origin_ncnt, legacy)` or
    `None` for: no leading `R`, an invalid comma count (module docstring,
    point 2), or a non-numeric neighbour count."""
    if not token.startswith("R"):
        return None
    remainder = token[1:]
    comma_count = remainder.count(",")
    legacy: bool
    origin_token: str
    if comma_count == 0:
        origin_token = remainder
        legacy = False
    elif comma_count == _LEGACY_LEADING_COMMAS:
        # Legacy shape: keep only the leading integer, discard the other two
        # fields verbatim — the firmware does not validate them either
        # (module docstring, point 3), so neither do we.
        origin_token = remainder.split(",", 1)[0]
        legacy = True
    else:
        # Includes the invalid 1-comma shape (module docstring, point 2) and
        # any other comma count, which the firmware never emits.
        return None

    origin_ncnt = _parse_int(origin_token)
    if origin_ncnt is None:
        return None
    return origin_ncnt, legacy


def _parse_hop_group(group: str) -> HeyHop | None:
    """Parse one `<ncnt>,<rssi>,<snr>` hop group, negating the wire's
    positive-magnitude RSSI (module docstring, point 1). `None` for anything
    that is not exactly three integer fields."""
    fields = group.split(",")
    if len(fields) != _HOP_GROUP_FIELDS:
        return None
    ncnt = _parse_int(fields[0])
    wire_rssi = _parse_int(fields[1])
    snr = _parse_int(fields[2])
    if ncnt is None or wire_rssi is None or snr is None:
        return None
    return HeyHop(ncnt=ncnt, rssi=-wire_rssi, snr=snr)


def parse_hey_chain(payload: str) -> HeyChain | None:
    """Parse a HEY beacon's `PP` field into a `HeyChain`.

    Returns `None` — never raises, never returns a partial object — for:
    empty/blank input, a non-`str`, no leading `R`, a non-numeric neighbour
    count, the 1-comma leading shape, a group that is not exactly three
    integers, a non-numeric field in a group, or any trailing garbage.

    This parses unauthenticated data straight off the air: every input is
    treated as hostile, and the work done is bounded regardless of input
    size (see `_MAX_PAYLOAD_LEN`).
    """
    if not isinstance(payload, str):
        return None
    payload = payload.strip()
    # The grammar terminates every token (leading and hop groups alike) with
    # ';'. Anything after the last ';' — including a payload with no ';' at
    # all — is trailing garbage.
    if not payload or len(payload) > _MAX_PAYLOAD_LEN or not payload.endswith(";"):
        return None

    # Drop only the final terminator: parts[0] is the leading R<ncnt> token,
    # parts[1:] are hop groups. A malformed group (wrong field count, a
    # non-numeric field, or an empty group from a doubled ';;') is rejected
    # by `_parse_hop_group` — no separate case is needed for it here.
    parts = payload[:-1].split(";")

    leading = _parse_leading_token(parts[0])
    if leading is None:
        return None
    origin_ncnt, legacy = leading

    hops: list[HeyHop] = []
    for group in parts[1:]:
        hop = _parse_hop_group(group)
        if hop is None:
            return None
        hops.append(hop)

    return HeyChain(origin_ncnt=origin_ncnt, hops=tuple(hops), legacy=legacy)
