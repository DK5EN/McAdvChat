# Backlog

Deferred tasks with a due date or a data-collection dependency. One section per item; move
resolved items to `doc/archive/` with their outcome.

## B1 — fcs_ok field-data verdict (due: after 2026-09-20)

**Question:** does the LoRa chip ever deliver a frame with a broken FCS? Martin's claim during
the 2026-08-21 wire audit: no — the chip drops bad frames itself, and the firmware's FCS
handling is unused/possibly broken. The full M2 gate (reject trailer-less/truncated frames)
was declined on that basis; instead, schema v24 stores per-frame `fcs_ok` on BLE data frames
(NULL for UDP/MH rows by design) so the claim can be tested against field data.

**Snapshot at creation (2026-08-21 21:45, mcapp.local, v2.0.0 slot-0):**

| fcs_ok | rows  |
| ------ | ----- |
| NULL   | 18593 |
| 1      | 8     |
| 0      | 0     |

Non-NULL window: 2026-08-21 18:54 – 21:21 (column live since the dev.48 deploy). 8 frames is
not a verdict — the point of the due date is to accumulate a few hundred non-NULL rows first.

**Check (run on the Pi; no sqlite3 CLI, timestamps in ms):**

```
SELECT fcs_ok, COUNT(*) FROM messages GROUP BY fcs_ok;
SELECT timestamp, src, dst, substr(msg,1,60) FROM messages WHERE fcs_ok = 0
  ORDER BY timestamp DESC LIMIT 20;
```

**Decision rule:**

- Still zero `fcs_ok=0` rows over a meaningfully larger sample → claim confirmed; close M2
  for good and record the result in the audit trail
  (`project_audit_fixes_dev48` memory / audit report artifact).
- Any `fcs_ok=0` rows → inspect the frames: real corruption reaching the host would reopen
  the M2 trailer/FCS gate question, this time with evidence.
