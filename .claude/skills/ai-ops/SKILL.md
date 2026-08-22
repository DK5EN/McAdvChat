---
name: ai-ops
description: The "is McApp actually healthy on mcapp.local" check — services, active slot, database, live data flow, logs, host headroom and secret hygiene on the Raspberry Pi that runs production. Use for "daily check", "check the Pi", "is mcapp healthy", "check mcapp.local", "any issues with McApp", "do your ops job", or before claiming production is fine.
allowed-tools:
  - Bash
  - Read
  - Grep
  - Glob
---

# AI-Ops — the McApp production check

`mcapp.local` (Raspberry Pi Zero 2W) is **the** production target and the only host running
MCProxy. There is no staging box. `rpizero.local` used to be the integration target and no
longer runs MCProxy at all (verified 2026-07-25) — do not check it and do not "fix" it.

Work top to bottom. Each phase is cheap; stop and chase only what a phase actually flags.

## Ground rules

- **Read-only on the Pi.** Never edit code in a slot, never write to the production database.
  A slot is replaced wholesale by the next deploy, so an in-place edit is lost at best and
  invisible drift at worst. Fixes go into the repo and reach the Pi through `/dev-release`.
- **Never pipe the ssh through `tail`/`head`.** `ssh ... | tail -40` reports _tail's_ exit
  code, so a failed command reads as a clean one. Redirect to a file on the Pi, echo `$?`
  inside the remote shell, then tail the file.
- **Never print `/etc/mcapp/config.json` wholesale.** It contains `BLE_API_KEY` in clear.
  Select the keys you need.
- **The Pi's shell is German-localised.** `free` prints `Speicher:`, `df` prints `insgesamt`.
  Parse by column position or use `--output=`, never by matching an English label — a label
  match silently returns nothing, which reads exactly like a healthy zero.

## Phase 1 — Services and restart history

```bash
ssh mcapp.local '
  systemctl is-active mcapp mcapp-ble caddy lighttpd
  systemctl show mcapp     -p NRestarts -p ActiveEnterTimestamp --value
  systemctl show mcapp-ble -p NRestarts --value
'
```

`NRestarts` is the check that matters. **The app cannot report a crash loop about itself** —
a restarted service shows a short, healthy `uptime_seconds` in `/api/status` and looks fine.
Any non-zero `NRestarts` is a finding; get the reason from the journal (Phase 6), do not guess.

Baseline as of 2026-08-22: all four active, `NRestarts=0` on both McApp units.

## Phase 2 — The app's own status surface

```bash
curl -sk https://mcapp.local/health
curl -sk https://mcapp.local/api/status | python3 -m json.tool
```

`/api/status` carries four UDP-provenance fields that exist for a security reason — port 1799
is **unauthenticated**, so anything on the LAN can inject frames:

| field                           | read it as                                                               |
| ------------------------------- | ------------------------------------------------------------------------ |
| `udp_target_kind`               | `identified` = we know which node we talk to; anything else is a finding |
| `udp_multiple_sources`          | `true` means more than one host is sending — investigate, never ignore   |
| `udp_untrusted_source_ips`      | non-empty is a finding: someone else is injecting                        |
| `udp_suppressed_target_changes` | a climbing count means something is trying to steal the target           |

## Phase 3 — Which code is actually running

A green health banner says nothing about _which build_ is live.

```bash
ssh mcapp.local '
  readlink -f ~/mcapp-slots/current
  cat /var/lib/mcapp/system-epoch
  grep -h "^LATEST_SCHEMA_VERSION" ~/mcapp-slots/current/src/mcapp/storage/constants.py
'
```

- **Grep the ACTIVE slot for a symbol only the change you care about introduces.** Everything
  else can look healthy while the service runs last week's code.
- **Slot order is not fixed.** `get_target_slot` picks the empty-or-oldest of `slot-{0,1,2}`,
  and re-deploying a version the active slot already holds deploys _in place_ with no
  rotation — an unchanged `current` symlink is not by itself evidence of a failed deploy.
- **`version.html` lies about what the browser runs.** It reports what was deployed, not what
  the service worker is serving; confirm the bundle filename and `SKIP_WAITING` before
  believing a frontend fix is live.
- **System epoch parity**: `/var/lib/mcapp/system-epoch` must match `REQUIRED_SYSTEM_EPOCH`
  in `src/mcapp/system_converge.py` (and `SYSTEM_EPOCH` in `bootstrap/mcapp.sh`). A stale
  epoch means the converge watchdog has system-level work pending.

## Phase 4 — Database

There is **no `sqlite3` CLI** on the Pi. Write a python3 heredoc, open read-only, delete it after.
**All timestamps are milliseconds** — divide by 1000 before `datetime.fromtimestamp()`, or you
get `ValueError: year 58089 is out of range`.

```bash
ssh mcapp.local "cat > /tmp/q.py << 'PYEOF'
import sqlite3, datetime
c = sqlite3.connect('file:/var/lib/mcapp/messages.db?mode=ro', uri=True)
q = lambda s, p=(): c.execute(s, p).fetchone()
f = lambda v: '-' if not v else datetime.datetime.fromtimestamp(v/1000).strftime('%Y-%m-%d %H:%M')
print('schema      :', q('SELECT version FROM schema_version')[0])
print('messages    :', q('SELECT COUNT(*) FROM messages')[0], 'newest', f(q('SELECT MAX(timestamp) FROM messages')[0]))
print('stations    :', q('SELECT COUNT(*) FROM station_positions')[0])
print('signal_log  :', q('SELECT COUNT(*) FROM signal_log')[0])
print('classifier  :', q(\"SELECT value FROM classifier_meta WHERE key='classifier_version'\"))
print('rules       :', q('SELECT COUNT(*) FROM classifier_rules')[0])
print('unclassified:', q(\"SELECT COUNT(*) FROM messages WHERE category IS NULL AND type='msg' AND timestamp > (SELECT MAX(timestamp)-3600000 FROM messages)\")[0], '(last 1h, expect 0)')
PYEOF
python3 /tmp/q.py; rm -f /tmp/q.py"
```

Judge:

- **schema version must equal `LATEST_SCHEMA_VERSION`** from Phase 3. A mismatch means a
  migration did not run — the loudest possible finding.
- **DB size** against the 1 GB hard limit (`MAX_DB_SIZE_MB`). Nightly prune runs at 04:00;
  a DB growing past ~900 MB means pruning is not keeping up.
- **A `-wal` file of a few MB is normal** (WAL mode); one that never checkpoints and grows
  into the hundreds of MB is not.
- **`{CET}` rows in `messages` should be ZERO.** They are dropped at ingest by
  `_should_filter_message`. Their absence is correct, not a broken feed.
- **The classifier's DB key is `classifier_version`, NOT `classifier_ver`.** The code and
  CLAUDE.md name the concept `classifier_ver`; the row in `classifier_meta` is
  `classifier_version`. Querying the former returns `None` with no error, which reads exactly
  like a dead classifier. It must match `classifier_ver` in the running code, and there should
  be a matching `backfill_done:v{N}` marker for it. Baseline 2026-08-22: version 3, 38 rules,
  markers through v3, 0 unclassified messages in the last hour.

## Phase 5 — Live data flow (the part that actually catches things)

A green suite and a green deploy still prove nothing about live behaviour. **Read the values,
not just the row counts.**

```bash
ssh mcapp.local "cat > /tmp/f.py << 'PYEOF'
import sqlite3, datetime, time
c = sqlite3.connect('file:/var/lib/mcapp/messages.db?mode=ro', uri=True)
now = int(time.time()*1000); h = 3600_000
for label, sql in [
    ('msgs last 1h',  \"SELECT COUNT(*) FROM messages WHERE type='msg' AND timestamp > ?\"),
    ('pos  last 1h',  \"SELECT COUNT(*) FROM messages WHERE type='pos' AND timestamp > ?\"),
    ('signal last 1h', 'SELECT COUNT(*) FROM signal_log WHERE timestamp > ?')]:
    print('%-15s: %s' % (label, c.execute(sql, (now-h,)).fetchone()[0]))
r = c.execute('SELECT first_observed_ms,last_beacon_ms,last_tick_ms FROM link_uptime_state').fetchone()
if r:
    age = (now - r[1])/1000 if r[1] else None
    print('last CET beacon:', ('%.0f s ago' % age) if age else 'never')
    print('heartbeat age  : %.0f s' % ((now - r[2])/1000))
    print('gap/dark rows  :', c.execute('SELECT COUNT(*) FROM link_uptime_segments').fetchone()[0])
PYEOF
python3 /tmp/f.py; rm -f /tmp/f.py"
```

Known-good shapes, so you do not report normal behaviour as a fault:

- **The `{CET}` uplink beacon arrives every ~303 s** (measured 2026-08-21, stable to the
  second). A last-beacon age under ~6 min is healthy; `GAP_TOLERANCE_MS` is 6 min precisely
  because anything tighter marks a perfect link as silent. See CLAUDE.md § Gateway Uptime.
- **Heartbeat age must stay under ~60 s** (30 s tick). A stale `last_tick_ms` with a live
  service means the heartbeat task died — the coverage metric is blind from that point on.
- **`gap` and `dark` are different claims.** `gap` = proxy up, no beacon (counts against
  UPTIME). `dark` = proxy not running (counts against COVERAGE). A `dark` row after a deploy
  is correct and expected, not an outage.
- **`state: "unknown"` on a fresh ledger is expected** until the first beacon lands (≤ ~5 min
  after a restart). Since 2026-08-22 the API reports `uptime_pct: null` inside the tolerance
  (too early to judge, and the stretch renders `dark`, not `up`) and `0.0` once the tolerance
  has passed with nothing ever heard. A fresh ledger reporting **100.0 % is the old bug** and
  would mean the fix regressed.
- **MHeard beacons are throttled to one row per station per 2 min** and run ~98/hour/station.
- **RSSI/SNR are only real for `src_type == "lora"`.** `node` and `udp` send a `0/0`
  sentinel — exclude by an explicit `src_type` check, never by a range check. Both values are
  final on the wire: RSSI is dBm as-is, SNR is already divided by 4 in firmware. **Never
  re-scale either.**

Known open items — confirm they have not changed, do not re-litigate them:

- 169 pre-2026-08-13 duplicate telemetry pairs, still unexplained, different in shape from the
  dev.30 ones that were fixed.
- `fcs_ok` field-data verdict is due after 2026-09-20 (see `doc/backlog.md`).

## Phase 6 — Log triage

```bash
ssh mcapp.local 'sudo journalctl -u mcapp --since "-24h" --no-pager -p warning' > /tmp/mcapp-warn.log
wc -l /tmp/mcapp-warn.log
```

Aggregate by message rather than reading the tail — one flapping upstream otherwise looks like
dozens of distinct problems:

```bash
python3 -c "
import collections, re, sys
c = collections.Counter()
for line in open('/tmp/mcapp-warn.log'):
    m = re.search(r'\[(WARNING|ERROR|CRITICAL)\]\s*(\S+)?[: ]\s*(.*)', line)
    if m: c[(m.group(1), (m.group(2) or '')[:40], m.group(3)[:80])] += 1
for (lvl, name, msg), n in c.most_common(25): print('%5d  %-8s %s %s' % (n, lvl, name, msg))
"
```

Baseline 2026-08-22: **1 line** in 24 h. A sudden jump into the hundreds is the signal; the
exact line matters more than the count.

## Phase 7 — Host headroom

```bash
ssh mcapp.local '
  df -h --output=source,size,used,avail,pcent /
  free -m | awk "NR==2 {print \"mem total=\" \$2 \" used=\" \$3 \" avail=\" \$7}"
  grep -E "^(SwapTotal|SwapFree):" /proc/meminfo
  uptime
  vcgencmd measure_temp 2>/dev/null || cat /sys/class/thermal/thermal_zone0/temp
'
```

**This is a Pi Zero 2W with ~415 MB of usable RAM** — memory is the scarce resource, not disk.
Baseline 2026-08-22 07:47: disk 7 % of 59 G, ~181 MB available RAM, and **~142 MB actually
swapped out** (SwapFree 273 of 415 MB). Steady swap usage is normal for this box under zram and
is a WATCH, not a finding — but SwapFree trending toward zero, MemAvailable in the low tens of
MB, or disk above 85 % is a finding. Three slots plus a growing DB is what fills the card.

Swap is read from `/proc/meminfo` rather than `free`, because `/proc` is English regardless of
the shell's locale and needs no `$field` references — `free`'s own labels are German here.

## Phase 8 — Secret and TLS hygiene

```bash
ssh mcapp.local '
  stat -c "%a %n" /var/lib/mcapp/vapid.json /etc/mcapp/config.json
  head -c 40 /var/lib/mcapp/vapid.json; echo
'
curl -skI https://mcapp.local/ | head -3
```

- **`vapid.json` must be `0600`.** A world-readable raw private scalar lets any local account
  forge VAPID JWTs as this node. `load_or_create_vapid` re-tightens a wider file on load, so a
  wide mode here means it has not been reloaded since someone changed it.
- It must hold the **raw base64url scalar, not PEM** — pywebpush's `Vapid.from_string` dies on
  a PEM. And it must not be regenerating on every restart: an ephemeral key silently kills
  every stored push subscription.
- Web Push needs outbound internet from the Pi and **degrades silently without it**.
- TLS is Caddy on :80/:443 in front of lighttpd on :8082. For certificate work follow
  `doc/tls-maintenance-SOP.md` rather than improvising.
- **A near-term `notAfter` on this host is NOT an expiry finding.** Caddy's internal CA
  (`issuer=CN=Caddy Local Authority - ECC Intermediate`) issues **12-hour** leaf certs and
  renews them automatically, so a spot check almost always shows a cert expiring within hours —
  observed 2026-08-22: `notBefore Aug 21 23:39 → notAfter Aug 22 11:39`, checked at 05:48, i.e.
  mid-life and healthy. Always read `notBefore` and the issuer before judging `notAfter`; only a
  cert that has _actually_ expired, or a public-CA cert inside its last days, is a finding.

```bash
echo | openssl s_client -connect mcapp.local:443 2>/dev/null \
  | openssl x509 -noout -issuer -dates -ext subjectAltName
```

## Pi gotchas that cost round-trips

- **No `tcpdump`.** For wire capture write a `socket.AF_PACKET` sniffer and run it under
  `sudo nohup setsid` — a bare `cmd &` in an ssh one-liner dies with the session.
- **Never open a second consumer on the BLE service's `/api/ble/notifications` SSE stream.**
  The queue is single-consumer (`popleft()`), so a debugging client _steals frames from
  production_.
- `sudo` is passwordless here, so `ssh -o BatchMode=yes` works for the whole sweep.

## Reporting

Verdict first, then the numbers. State explicitly which signals are **absent** — for most of
these checks absence _is_ the result, so quote the zero rather than staying silent about it.
Separate genuine findings from noise you classified and dismissed, and say why you dismissed it.

For every finding give the evidence (log line, callsign, counter) and say whether it is ours or
upstream. Anything needing a code change goes into the repo and ships via `/dev-release` — never
patched on the box. If the sweep finds nothing, "all green" is a valid result, but only after
the phases above actually ran, not from `/health` alone.

### Then append to the log — this is not optional

**[`doc/ops-mcapp-health-log.md`](../../../doc/ops-mcapp-health-log.md) is the source of truth
for observed baselines**, and the point of the whole exercise is the time series, not any single
run. Append one new dated section following the shape of the existing ones — anchors table,
verdict, measured values, absent signals, watch points `W<n>`, findings `F<n>` continuing the
series across runs — and update the banner at the top to name the newest run. **Append, never
edit a previous section**: a run that rewrites history destroys the comparison it exists to make.

Compare against the **rate table in §1** and re-measure it. Raw counters cannot be compared
across runs with different uptimes — a box six hours past a deploy and the same box six days
later produce very different totals while being equally healthy. Record rates with their window;
record totals only as context.

The inline "Baseline 2026-08-22" values in the phases above are a starting point, not the
record. When they and the log disagree, the log wins — and say so in the run.

Finish with:

```bash
npx --yes prettier@3 --write doc/ops-mcapp-health-log.md && uvx ruff format --check .
```

Prettier and ruff both touch markdown, so run prettier first, then the ruff check — a docs-only
commit has turned CI red here twice. Commit only if the user asks.
