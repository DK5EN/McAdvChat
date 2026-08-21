Check whether DF8RD-1 has resumed beaconing on mcapp.local, and whether its
telemetry is being stored correctly.

CONTEXT

- mcapp.local (Raspberry Pi Zero 2W) runs McApp; SQLite DB at
  /var/lib/mcapp/messages.db. There is NO sqlite3 CLI on the box — write a
  python3 script, scp it over, run it, delete it. All DB timestamps are in
  MILLISECONDS (divide by 1000 for datetime.fromtimestamp).
- Running version at time of writing: v1.6.14-dev.32, deployed 2026-08-13 13:53
  CEST. It contains a large telemetry-ingest fix set (dedup/merge rewrite,
  APRS key mapping /O=->temp2, /G=->gas, /C=->co2, and pressure-vs-altitude
  discrimination by src_type).

WHAT HAPPENED
DF8RD-1 is a weather station whose APRS beacon carries /P= (pressure), /T=
(temp1), /O= (temp2) and /Q= (QNH) — but NO /H=, so a NULL hum is correct and
expected for it. Its median beacon interval is ~32 min (observed range 22-92
min). Its last weather telemetry row was 2026-08-13 12:20:56 with
temp1=37.5, temp2=29.1, qfe=968.5. It then went silent — no frames of any kind
— and was still silent 110 minutes later. It went quiet BEFORE the 13:53
deploy, so this was not deploy-related; most likely propagation or the station
being off.

WHAT TO REPORT

1. Has DF8RD-1 sent any frame since 2026-08-13 12:20? Distinguish MHeard
   beacons (src_type='ble', type='pos', msg_id IS NULL, no sensor data — these
   correctly produce no telemetry row) from real weather beacons.
2. If it has weather rows again: for the most recent few, print timestamp,
   temp1, temp2, hum, qfe, gas, extras. Then judge:
   - temp2 populated (~~/O= working) and qfe populated (~~/P= working) = healthy
   - temp2 NULL while the beacon's raw_json msg contains "/O=" = PARSER
     REGRESSION, report it loudly
   - a bare "O" key inside `extras` instead of the temp2 column = the same
     regression in its original form
   - two rows for the same callsign less than 60 s apart = DEDUP REGRESSION
3. Sanity-check the whole table: total telemetry row count should only ever
   grow. A drop is a row-destruction regression (a replayed BLE frame deleting
   newer rows) and is critical — report immediately with numbers.
4. If DF8RD-1 is still silent, say so plainly and do not speculate about
   causes beyond noting whether other stations are still being received (check
   for recent rows from DL2JA-2, DF8RD-10, DM6CS-12 as a propagation control).

Report actual values, not just row counts. Do not conclude "working" from a
row existing — read the fields.
