# Fehlerbild: mHeard-Charts „Last Month" / „Last Year" gestaucht & lückenhaft

**Analyse-Datum:** 2026-06-02  
**System:** `mcapp.local`, DB `/var/lib/mcapp/messages.db` (Produktion, MCProxy)  
**Frontend:** webapp `MheardTable.vue` (Tabs 24h / 7d / 30d / 1y)  
**Status:** Ursache eingegrenzt, exakter Auslöser **nicht** final bewiesen (Rohquelle weggeprunt). Dieses Dokument ist das Fehlerbild + Evidenz für die Code-Analyse.

---

## 1. Symptom

In der mHeard-Ansicht zeigen die Charts **„Last Month"** und **„Last Year"** für alle Stationen dasselbe Muster:

- Zeitraum vor ca. **26. Mai 2026**: nur vereinzelte, senkrechte Balken mit großen Lücken dazwischen („sparse"), statt einer durchgehenden Linie.
- Zeitraum ab ca. **26. Mai 2026**: dichte, korrekte Daten (durchgehende Linie, gefüllte Count-Balken).
- Dazwischen eine harte Lücke (~22.–26. Mai).

Erwartung des Users: „mehr als ein ganzes Monat zurück" durchgehend sichtbar, **keine** Lücken.

---

## 2. Kernbefund (BEWIESEN)

### 2.1 Die Stauchung liegt in den gespeicherten `signal_buckets`, nicht im Chart

Rohe Zeilen aus `signal_buckets` (kein Chart, keine Laufzeit-Aggregierung dazwischen), Beispiel `DB0ED-99`:

```
bucket_size=3600000 (1h)   bucket_ts(UTC)      count
DB0ED-99   2026-05-20 00:00   85
DB0ED-99   2026-05-20 01:00   86
DB0ED-99   2026-05-20 02:00    6
DB0ED-99   2026-05-21 00:00   68
DB0ED-99   2026-05-21 01:00   92
DB0ED-99   2026-05-21 02:00    6
```

Der **ganze Tag** (~177 Pakete) liegt in 2–3 aufeinanderfolgenden Stunden-Buckets am Anfang des UTC-Tages. Der Chart rendert also korrekt, was in der DB steht → **`chart == DB`**, der Fehler ist in den Daten.

### 2.2 Es ist eine lineare Zeit-Stauchung (~Faktor 12), kein Offset

Die Tagespakete verteilen sich **nahezu gleich** auf UTC-Stunde 00 (85) und 01 (86), mit kleinem Rest in 02 (6). Das ist kein konstanter Uhren-Offset (der würde nur *verschieben*), sondern eine **lineare Stauchung der Tageszeit**: 24 h echte Zeit → ~2 h gespeicherte Zeit. Das **Datum** bleibt korrekt.

### 2.3 Das Muster ist systematisch über ALLE Stationen

Stunden-Buckets pro Callsign, distinct UTC-Stunden:

```
DB0ED-99   hours=[00,01,02]     DD7CV-1   hours=[00,01,02]
DL2JA-1    hours=[00,01,02]     DK5EN-90  hours=[00,01,02]
DL2JA-2    hours=[00,01,02]     DD7MH-55  hours=[02,03]   (Feb, CET)
```

Unabhängige Stationen an verschiedenen Standorten können nicht zufällig alle nur im selben 2-Stunden-Fenster gehört werden → **Timestamp-Artefakt, keine echte RF-Sparsity.** Das Fenster verschiebt sich mit der Sommerzeit (Feb/CET: UTC 02–03; Mai/CEST: UTC 00–02), d. h. eine lokale-Zeit-Interpretation ist im Spiel.

### 2.4 Die Aggregierung rechnet korrekt — der Bug ist UPSTREAM

- Die exakte `aggregate_hourly_buckets`-SELECT auf den **aktuellen** 5-Min-Buckets erzeugt **24 sauber über den Tag verteilte** Stunden-Buckets (Stunden 03,04,05…). Eine korrekte Eingabe wird also **nicht** gestaucht.
- Aktuelles `signal_log` (Rohmessungen) ist sauber über 00–23 UTC verteilt (~500/h) und matcht die Wanduhr.
- Aktuelle 5-Min-Buckets ebenfalls 00–23 UTC, gleichmäßig.

**Schlussfolgerung:** `aggregate_hourly_buckets`, `process_mheard_monthly/yearly`, das 5-Min-Bucketing und die Gap-Marker-Logik sind **korrekt**. Der Fehler sitzt in dem **Timestamp, der pro mHeard-Messung geschrieben wurde**, bevor aggregiert wurde — und zwar nur für die Altdaten.

---

## 3. Zusätzlicher Befund: harte Lücke 22.–26. Mai

```
letzter 1h-Bucket :  2026-05-22 04:00
erster 5min-Bucket:  2026-05-26 05:35   (Δ ≈ 97.6 h)
mcapp-Restart     :  2026-05-26 20:34 CEST
```

Die Lücke fällt mit dem Deploy/Restart am 26. Mai zusammen. Ab da sind die Daten korrekt (siehe 2.4). Das ist eine Betriebslücke, kein Code-Bug.

---

## 4. Datenstand (Kontext)

```
signal_buckets  bucket_size=300000  (5min): 10252 Zeilen,  2026-05-26 05:35 → 2026-06-02 07:35
signal_buckets  bucket_size=3600000 (1h)  :  1003 Zeilen,  2026-02-08 03:00 → 2026-05-22 04:00
signal_log (raw): 52221 Zeilen,            2026-05-26 05:39 → 2026-06-02 07:41   (8 Tage Retention)
```

Retention laut `doc/database-reference.md`: `signal_log` + 5-min-Buckets 8 Tage, 1h-Buckets 365 Tage. Nightly-Job um **04:00 CEST = 02:00 UTC** (`aggregate_hourly_buckets`, prune, ANALYZE).

**Die historischen Stunden-Buckets sind damit immutable:** die Rohquelle (`messages`/`signal_log` vor dem 26. Mai) ist weggeprunt; die gestauchten Aggregate lassen sich nicht „entstauchen".

---

## 5. Offene Frage für die Code-Analyse: woher kommt der gestauchte Timestamp?

Der mHeard-Mess-Timestamp kann aus zwei Quellen kommen:

| Pfad | Timestamp-Quelle | Datei | Kann einen Tag stauchen? |
|------|------------------|-------|--------------------------|
| **BLE** | Node-`DATE`/`TIME` | `ble_protocol.py:389` `transform_mh` → `:401` `"timestamp": node_timestamp` (via `timestamp_from_date_time` `:178`) | ja, falls Node-Tageszeit verfälscht |
| **UDP** | `int(time.time()*1000)` = Pi-Zeit | `udp_handler.py:177` / `:221` | nein (Pi-Uhr ist korrekt) |

Beobachtung: Aktuell kommen Daten stark über **UDP** rein (`src_type: lora/node` im Log). Das passt dazu, dass es *jetzt* korrekt ist. Für die **Altdaten** ist die Schreib-Quelle nicht mehr direkt einsehbar (Rohdaten geprunt).

**Wichtiger Hinweis (User):** Die Node-Uhr wird beim Boot per NTP gesetzt — ein simpler Node-Wandclock-Fehler ist also unwahrscheinlich. Das Stauch-Muster (Datum korrekt, Tageszeit linear ~12× gestaucht) deutet eher auf einen **Einheiten-/Konstruktionsfehler beim Bilden des Timestamps** als auf eine falsch gehende Uhr.

### Konkrete Hypothesen, die der Agent prüfen sollte

1. **`timestamp_from_date_time` (`ble_protocol.py:178`)**: `datetime.strptime(...)` liefert ein **naives** `datetime`; `dt.timestamp()` interpretiert es in der **lokalen TZ des Pi** (CET/CEST). Stimmt die Annahme über die TZ der Node-`DATE`/`TIME`-Felder (UTC vs. lokal)? Fallback bei Parse-Fehler ist `1970-01-01` → erzeugt aber 1970-Werte, nicht die beobachtete Stauchung.
2. **Format/Einheit der Node-`TIME`**: Welches Format senden `DATE`/`TIME` der mHeard-Beacons real (`mheardLine.mh_time = getTimeString()` in der Firmware)? Eine ~12×-Stauchung der Tageszeit entsteht z. B., wenn Stunden wie 5-Minuten-Einheiten behandelt würden (24 h × 5 min = 120 min = 2 h) — gibt es irgendwo so eine Verwechslung Stunde/Minute/5-Min-Slot?
3. **Welcher Pfad hat die Altdaten geschrieben?** BLE (`transform_mh`, Node-Zeit) vs. UDP (Pi-Zeit) vs. einmaliger Backfill `_backfill_new_tables` (`sqlite_storage.py:502`, der `signal_log` aus `messages.timestamp` rekonstruiert und dann 5-Min-Buckets bildet). Hat sich der aktive Pfad um den 26. Mai geändert?
4. **War der Schreib-Timestamp historisch schon in `messages.timestamp` gestaucht?** D. h. liegt der Fehler vor der Bucket-Bildung (in der Ingestion), nicht in der Aggregierung.

---

## 6. Relevante Code-Stellen

**MCProxy (`src/mcapp/`):**
- `sqlite_storage.py:29` `BUCKET_SECONDS = 5*60`; `:36` `HOURLY_BUCKET_MS`; `:37` `HOURLY_GAP_THRESHOLD = 6*3600`
- `sqlite_storage.py:502` `_backfill_new_tables` (Initial-Backfill; `:602-628` 5-min-Pre-Aggregation)
- `sqlite_storage.py:~892` `_accumulate_signal`, `:929` `_flush_completed_buckets` (Live-5-min-Bucketing)
- `sqlite_storage.py:~1217-1228` MHeard-Beacon → `signal_log`
- `sqlite_storage.py:1733` `aggregate_hourly_buckets` (Nightly-Rollup 5min→1h, **verifiziert korrekt**)
- `sqlite_storage.py:2314` `process_mheard_monthly`, `:2202` `process_mheard_yearly` (lesen Buckets + Gap-Marker)
- `ble_protocol.py:178` `timestamp_from_date_time`; `:387` `transform_mh` (`:401` setzt `timestamp = node_timestamp`)
- `udp_handler.py:177` / `:221` `time.time()`-Timestamp (Pi-Zeit, für `tele`/`msg`)
- `main.py:699-701` `--settime` beim BLE-Connect; `:285-291` Command-Routing; `:538` `_handle_mheard_dump_monthly_command`

**Firmware (`MeshCom-Firmware-DEV-Main/src/`):**
- `loop_functions.cpp:2396` `getTimeString()` → `"%02i:%02i:%02i"` aus `node_date_hour/minute/second` (Format **korrekt**)
- `lora_functions.cpp:493` `mheardLine.mh_time = getTimeString()`; `command_functions.cpp:5001` `pdoc["DATE"] = getDateString()+" "+getTimeString()`
- Verdächtige Commits: `e91a86e` (11.02. Feature/Backfill), `5ed54e7` (29.03. UTC-Offset bei `--settime`), `e3fa0fdb` (19.04. nur DATE-Format), `177909c6` („WEBService Time expired 4h")

---

## 7. Reproduktions-Queries (SQLite, Timestamps in **ms**)

```python
import sqlite3

con = sqlite3.connect("/var/lib/mcapp/messages.db")
cur = con.cursor()

# Stauchung sichtbar machen: Stunden-Buckets nur in 00-02 UTC trotz voller Tagescounts
cur.execute("""
SELECT callsign, strftime('%H', bucket_ts/1000,'unixepoch') h, SUM(count)
FROM signal_buckets WHERE bucket_size=3600000 GROUP BY callsign, h ORDER BY callsign,h""")

# Gegenprobe: aktuelles signal_log ist sauber 00-23 UTC verteilt
cur.execute("""
SELECT strftime('%H', timestamp/1000,'unixepoch') h, COUNT(*)
FROM signal_log GROUP BY h ORDER BY h""")

# Aggregierung ist korrekt: exakte Rollup-SELECT auf aktuelle 5min-Daten -> 24 Stunden verteilt
cur.execute("""
SELECT (bucket_ts/3600000)*3600000 AS hour_ts, COUNT(*), SUM(count)
FROM signal_buckets WHERE bucket_size=300000
GROUP BY callsign, hour_ts ORDER BY hour_ts LIMIT 30""")
```

---

## 8. Auswirkung & Optionen (zur Einordnung, nicht präskriptiv)

- **Selbstheilung:** Live-Pfad ist korrekt → Monats-Chart vollständig korrekt ~Ende Juni (gestauchter Teil wandert aus dem 30-Tage-Fenster), Jahres-Chart ~in 12 Monaten.
- **Altdaten reparieren:** nicht möglich (Rohquelle geprunt). Die 1h-Buckets enthalten aber **gültige Tages-Aggregate** (Counts + Ø-RSSI/SNR realistisch, z. B. `DB0ED-99` 8578 Pakete / 75 Tage, Ø-RSSI −112.5 dBm) — nur die Tageszeit ist verloren.
- **Fix-Optionen, die noch zu bewerten sind:**
  1. Region vor dem 26. Mai in **Tagesauflösung** rendern (1 count-gewichteter Punkt/Tag, keine Gap-Marker) → lückenloser, ehrlicher Monats-/Jahrestrend.
  2. Gestauchte 1h-Buckets **löschen** (`DELETE FROM signal_buckets WHERE bucket_size=3600000`, ~1003 Zeilen) → ehrlich, aber Historie weg bis Neuaufbau. Vorher `messages.db` sichern.
  3. Nichts tun (Selbstheilung).
- **Prävention:** sobald die Schreib-Timestamp-Quelle geklärt ist, ggf. Guard/Validierung gegen verfälschte Node-Tageszeit; oder mHeard konsequent mit Pi-Empfangszeit stempeln.
- **Diagnose zum endgültigen Beweis (Node-Zeit vs. Proxy-Zeit):** eine Log-Zeile, die beim Eintreffen einer mHeard-Messung **rohe Node-`DATE`/`TIME` vs. Pi-`time.time()`** loggt → beim nächsten Auftreten ist die Quelle eindeutig.

---

## 9. Was NICHT der Fehler ist

- Frontend `MheardTable.vue` / Gap-Marker-Logik (`HOURLY_GAP_THRESHOLD=6h`) — rendert die DB korrekt.
- `aggregate_hourly_buckets`, `process_mheard_monthly/yearly`, 5-min-Bucketing — Rechenweg verifiziert korrekt.
- Pi-Systemuhr — korrekt (aktuelle Daten matchen Wanduhr).

---

## 10. Code-Analyse: Ergebnis (2026-06-02)

Die Code-Analyse bestätigt das Fehlerbild und grenzt die Quelle **eindeutig** ein. Drei Punkte aus
Abschnitt 5 lassen sich jetzt beantworten, ein Punkt korrigiert eine Annahme des Dokuments.

### 10.1 BEWIESEN: Die Stauchung kommt aus der node-gelieferten mHeard-Zeit, 1:1 übernommen

Lückenlose Beweiskette im Proxy-Code:

1. **Nur der BLE-Pfad speist die Signal-Pipeline.** `store_message` schreibt `signal_log` und ruft
   `_accumulate_signal` **ausschließlich** wenn `is_mheard` wahr ist, und das verlangt
   `src_type == "ble"`:
   ```python
   # sqlite_storage.py:1213
   is_mheard = not msg_id and src_type == "ble" and msg_type == "pos"
   ```
   UDP-mHeard erreicht `signal_log`/`signal_buckets` im Live-Pfad **nie** (`udp_handler.py` setzt nur
   `tele`/`msg` ab, niemals `src_type=="ble"`). → **Korrektur zu Abschnitt 5/Tabelle**: Die aktuell
   korrekten Daten stammen **nicht** aus UDP=Pi-Zeit, sondern ebenfalls aus dem BLE-Pfad. Die relevante
   Variable ist **die Node**, nicht UDP-vs-BLE.

2. **Der geschriebene Timestamp ist die Node-Zeit, nicht die Pi-Empfangszeit.**
   ```python
   # sqlite_storage.py:1092
   timestamp = message.get("timestamp", int(time.time() * 1000))  # kein Reassign bis :1223
   # ble_protocol.py:401 (transform_mh) — message["timestamp"] = node_timestamp
   ```
   Für BLE-mHeard ist `message["timestamp"]` immer gesetzt (= `node_timestamp` aus
   `timestamp_from_date_time(DATE, TIME)`). Der Fallback `time.time()` greift hier nicht.
   `signal_log` (`:1223`) und `_accumulate_signal` (`:1227`) bekommen also die **Node-Tageszeit**.

3. **Der Proxy kann ×12 nicht erzeugen — und hat sich am Flip nicht geändert.**
   - `timestamp_from_date_time` (`ble_protocol.py:178`) baut strikt `"%Y-%m-%d %H:%M:%S"` und ruft
     `dt.timestamp()` — das ist ein TZ-Offset, **keine** Stauchung. Parse-Fehler → 1970, nicht ×12.
   - Backfill (`:602-628`), Live-Bucketing (`_accumulate_signal :899`) und Rollup
     (`aggregate_hourly_buckets :1750`, `(bucket_ts/3600000)*3600000`) rechnen alle korrekt auf
     bereits-korrekten Epoch-ms. Die einzige „12" im Proxy ist das Verhältnis 3600000/300000 der
     Bucket-Größen — sie wirkt auf korrekte Zeitstempel und erzeugt korrekte Buckets (in 2.4
     verifiziert).
   - **`git log` (src/mcapp): zwischen 2026-05-09 und 2026-05-30 kein Commit** an
     `sqlite_storage.py`, `ble_protocol.py`, `udp_handler.py`. Der Übergang gestaucht→korrekt am
     26. Mai ist also **kein Proxy-Codefix**, sondern ein **Input-Wechsel**.

Da derselbe (unveränderte) BLE-Pfad vor dem 26. Mai gestauchte und danach korrekte Zeitstempel
geschrieben hat, muss sich der **Input** geändert haben: die von der Node gelieferte `mh_time`.

### 10.2 Mechanik: ×12 ist ein Einheiten-/Konstruktionsfehler im Zeit-FELD, kein langsamer Takt

Das 85/86/6-Muster (Hypothese 2.2) ist exakt eine **lineare ×12-Stauchung der Tageszeit**: bei über
24 h gleichverteilten Paketen landet die erste Tageshälfte in UTC-Bucket 00, die zweite in Bucket 01,
ein kleiner Überlauf in 02 (12 h echte Zeit ⇒ 60 min gespeicherte Zeit). Faktor 12 = 60 min / 5 min
bzw. Stunde/5-Min — passt zu Hypothese 2 (Stunde wie 5-Min-Einheit behandelt).

**Vereinbar mit dem User-Hinweis (NTP):** Eine NTP-korrekte Uhr und ein fehlerhaft **konstruiertes**
`mh_time`-Feld schließen sich nicht aus. Die Evidenz spricht für einen **Skalierungs-/Einheitenfehler
beim Bilden des mHeard-Zeitfeldes in der Firmware**, nicht für eine langsam laufende Uhr. Der
DST-abhängige Versatz des Fensters (Feb/CET vs. Mai/CEST) ist konsistent damit, dass die (verfälschte)
Zeit node-seitig aus **Lokalzeit + `node_utcoff`** gebildet wird (`command_functions.cpp:293`).

### 10.3 Was nachweisbar ist — und was die User-Bestätigung braucht

- **Bewiesen:** Die Stauchung ist über node-geliefertes `mh_time` eingetreten und vom Proxy verbatim
  gespeichert. Proxy-Parse/Backfill/Bucketing/Rollup können ×12 nicht erzeugen und waren am Flip
  unverändert.
- **Entscheidender, vom User zu bestätigender Punkt:** ein **Node-Firmware-/Zeit-Config-Wechsel
  während der Betriebslücke 22.–26. Mai** (genau dort kippte der Input). Der Proxy-Restart am 26. Mai
  ist nur das sichtbare Ende dieser Lücke.
- **Nicht der Auslöser:** Firmware-Commit `e04d4194 "MHEARD epoch-Time fix"` ist **2025-03**, also
  *vor* dem Feb–Mai-2026-Fenster — nur relevant, falls die Node sehr alte Firmware fuhr. Die exakte
  deployte Firmware-Version ist von hier aus **nicht** feststellbar; daher keine Behauptung über eine
  konkrete Firmware-Zeile.

### 10.4 Latenter Proxy-Befund (separat, NICHT die Ursache des Symptoms)

`timestamp_from_date_time` erzeugt ein **naives** `datetime` und interpretiert es via `dt.timestamp()`
in der **Pi-Lokal-TZ**. Da die Node die Zeit bereits als Lokalzeit (`node_utcoff`) liefert und Node-TZ
== Pi-TZ (DE), **round-trippt das aktuell korrekt**. Es ist damit ein **latentes Robustheitsrisiko**
(beißt nur bei abweichender Node-/Pi-TZ oder falsch gesetztem `node_utcoff`), **nicht** die Ursache
der beobachteten Stauchung und auch nicht zwingend des DST-Versatzes.

### 10.5 Empfehlung (zur Bewertung, nicht implementiert)

- **Prävention (empfohlen):** mHeard-Messungen mit **Pi-Empfangszeit** (`int(time.time()*1000)`)
  stempeln statt mit Node-Zeit. Das entfernt in einem Schritt sowohl die Abhängigkeit von der
  Node-Tageszeit als auch das latente TZ-Risiko aus 10.4. Eingriffsstelle: der `is_mheard`-Block
  (`sqlite_storage.py:1216-1228`) bzw. `transform_mh` (`ble_protocol.py:401`).
- **Diagnose zum Endbeweis:** wie in Abschnitt 8 — eine Logzeile, die beim nächsten BLE-mHeard rohe
  Node-`DATE`/`TIME` gegen `time.time()` stellt; bestätigt die Node-Quelle zweifelsfrei.
- **Altdaten:** unverändert wie Abschnitt 8 (nicht entstauchbar; Selbstheilung über das wandernde
  30-Tage-/12-Monats-Fenster).

---

## 11. Datenbereinigung & Clean Slate (2026-06-02)

Auf `mcapp.local` durchgeführt. Vorgehen: erst Live-Inspektion (read-only), dann konsistentes Backup,
dann gezieltes Löschen, dann Verifikation.

### 11.1 Live-Befund (bestätigt die Analyse exakt)

```
signal_buckets size=3600000 (1h)  : 1003 Zeilen, 2026-02-08 → 2026-05-22
   Stunden-Verteilung (UTC)       : nur 00/01/02/03  (= DST-wanderndes Stauch-Fenster)
signal_buckets size=300000 (5min) : ~10320 Zeilen, 2026-05-26 → jetzt, Stunden 00–23  ✔ korrekt
signal_log                        : ~52520 Zeilen, 2026-05-26 → jetzt, Stunden 00–23  ✔ korrekt
messages (mHeard-artig)           : nur ab 2026-05-26  → KEINE gestauchten Alt-Rows mehr (geprunt)
messages (gesamt)                 : ab 2026-05-03  (Retention ~30 Tage)
```

→ Einziges korruptes Artefakt: die **1003 1h-Buckets** (gesamte Stauch-Ära Feb–22. Mai). Es existieren
noch **keine** korrekten 1h-Buckets (erster Rollup der korrekten 5min-Daten erst, wenn diese >8 Tage
alt sind, ~3.–4. Juni). Löschen aller 1h-Buckets entfernt daher **ausschließlich** Korruptes.

### 11.2 Durchgeführte Aktionen

1. **Backup** (konsistenter Online-Snapshot inkl. WAL, enthält die 1003 gestauchten Buckets — bei
   Bedarf wiederherstellbar):
   `/home/martin/db-backups/messages-20260602-pre-bucketwipe.db` (32 MB).
2. **Löschung** auf der Live-DB (`busy_timeout=15s`, Service lief weiter, WAL):
   `DELETE FROM signal_buckets WHERE bucket_size=3600000;` → **1003 → 0 Zeilen.**
3. **Verifikation:** 1h-Buckets = 0; 5min-Buckets und `signal_log` unangetastet und wachsen weiter.
   `messages`/`station_positions` **nicht** angefasst (keine korrupten mHeard-Rows mehr vorhanden;
   evtl. veraltete `signal_ts` einzelner inaktiver Stationen heilen beim nächsten Hören).

**Status: Clean Slate.** Ab sofort enthält die Signal-Historie nur noch korrekte Daten (ab 26. Mai).
Die mHeard-Charts „Last Month"/„Last Year" zeigen vor dem 26. Mai jetzt **leer** (ehrlich) statt
gestaucht; ab 26. Mai dichte, korrekte Daten.

### 11.3 Brauchen wir einen Code-Fix? — Nein zwingend, aber Hardening empfohlen

- **Kein Proxy-Bug hat das verursacht** (siehe §10): Die Stauchung kam node-seitig über `mh_time`; der
  Proxy hat verbatim gespeichert und sich am Flip nicht geändert. Es wurde **kein** Code geändert.
- Die Node liefert seit ~26. Mai korrekte Zeit. Solange das so bleibt, bleibt die DB sauber → **kein
  Pflicht-Fix.**
- **Empfohlenes Hardening (offen, Entscheidung des Users):** mHeard mit **Pi-Empfangszeit** stempeln
  statt Node-Zeit (`sqlite_storage.py:1216-1228` / `ble_protocol.py:401`). Entfernt die Abhängigkeit
  von unzuverlässiger Node-Tageszeit **und** das latente TZ-Risiko (§10.4) in einem Schritt; schützt
  gegen eine Node-Regression. Klein und risikoarm, aber bewusste Design-Entscheidung — **nicht**
  implementiert.

### 11.4 Was morgen (2026-06-03) zu prüfen ist

1. **Keine Regression:** `signal_log` und 5min-Buckets der letzten 24 h spannen weiterhin **00–23 UTC**
   (Node liefert weiter korrekte Zeit). Reproquery aus §7 (signal_log Stunden-Verteilung).
2. **Keine neuen gestauchten 1h-Buckets:** `SELECT strftime('%H',bucket_ts/1000,'unixepoch'), COUNT(*)
   FROM signal_buckets WHERE bucket_size=3600000 GROUP BY 1` — sollte, sobald der Nightly-Rollup
   (04:00 CEST) die ersten >8-Tage-alten 5min-Daten verarbeitet hat, **24 verteilte Stunden** zeigen,
   nicht nur 00–03.
3. **Chart-Sicht:** Monats-/Jahres-Chart — Lücke vor 26. Mai (ehrlich), dichte korrekte Daten danach.

### 11.5 Verifikation Tag 2 (2026-06-03, 11:20 UTC) — bestanden

```
signal_buckets size=3600000 (1h)  : 0 Zeilen                         ✔ keine korrupten zurück
signal_buckets size=300000 (5min) : 11826 Zeilen, 2026-05-26 → jetzt, Stunden 00–23  ✔
signal_log                        : 59910 Zeilen, 2026-05-26 → jetzt, Stunden 00–23  ✔ wächst normal
messages (mHeard-artig)           : ab 2026-05-26                    ✔ keine gestauchten Rows
```

- **Clean Slate hält:** 0 gestauchte Buckets, keine neuen aufgetaucht.
- **Keine Regression:** Node liefert weiter korrekte Zeit (`signal_log` + 5min-Buckets voll 00–23 UTC).
- **Noch keine 1h-Buckets — erwartet, kein Fehler.** Der Nightly-Rollup (04:00 CEST = 02:00 UTC) am
  3. Juni hatte noch nichts zu tun: Cutoff = `now − 8 Tage` = 26. Mai 02:00 UTC, das älteste
  5min-Bucket ist 26. Mai **03:35** UTC (jünger als Cutoff). Der **erste echte Rollup ist die Nacht
  auf den 4. Juni** (Cutoff 27. Mai 02:00 UTC → rollt 26. Mai 03:35 → 27. Mai 02:00 in 1h-Buckets).
- **Letzte Bestätigung am 4. Juni:** dann sollten die ersten korrekten 1h-Buckets mit Stunden-Verteilung
  **00–23** (nicht 00–03) erscheinen → Pipeline-Ende-zu-Ende verifiziert.

---

## 12. REZIDIV (2026-06-05): Stauchung in den 1h-Buckets zurück — Ursache geklärt

Der User meldet erneut Lücken/„sparse" in „Last Month"/„Last Year". Re-Analyse auf `mcapp.local`.

### 12.1 Live-Befund (DB)

```
signal_buckets 3600000 (1h)  : 32 Zeilen, 2026-05-27 00:00 → 2026-05-28 02:00
   Stunden-Verteilung (UTC)  : NUR 00 (538) / 01 (612) / 02 (58)   ← GESTAUCHT, ×12-Signatur zurück
signal_buckets 300000 (5min) : 11795 Zeilen, 2026-05-28 02:05 → jetzt, Stunden 00–23   ✔ korrekt
signal_log                   : 62422 Zeilen, 2026-05-28 → jetzt, Stunden 00–23          ✔ korrekt
messages (ble pos, rssi)     : ab 2026-05-28 00:01:38 (ältere Roh-Rows geprunt)
```

### 12.2 Beweis: Der Rollup ist NICHT der Bug (entscheidender Test)

Die exakte Rollup-Stunden-Extraktion `(bucket_ts/3600000)*3600000` auf **bekannt-saubere**
5min-Daten vom 4. Juni angewendet → **perfekte 00–23-Verteilung**. Der Rollup staucht nicht.
→ Die 32 gestauchten 1h-Buckets stammen aus **bereits gestauchten 5min-Quell-Buckets** (Reste der
Stauch-Ära, Tail bis ~28. Mai 02:00), die der **Nightly-Rollup** am 4./5. Juni zu 1h aggregiert hat.

### 12.3 Warum es ZURÜCKKAM (Kern-Antwort an den User)

Die Bereinigung vom 2. Juni (§11) hat **nur die 1h-Buckets** gelöscht (`bucket_size=3600000`). Die
**gestauchten 5min-Buckets** (Mai 26–28) blieben liegen. 5min-Retention = 8 Tage; sobald sie >8 Tage
alt wurden, hat der Nightly-Rollup (04:00 CEST) sie am 4./5. Juni in **neue gestauchte 1h-Buckets**
gerollt. `journalctl` 3.–5. Juni: **nur** normaler Rollup, **kein** Backfill, **kein** Restart
(durchgehend `uv[978]`) → das Rezidiv ist reines Aufrollen von Altlast, kein neuer Bug.

### 12.4 Node-Zeit ist inzwischen gesund

`timestamp` (Node) ≈ `created_at` (Pi, UTC-Text) für alle überlebenden Rows (Mai 28 → jetzt): Deltas
Sekunden bis wenige Minuten; nur **2 von 6282** Rows >2 h auseinander. Die ×12-Stauchung ist aus dem
Live-Feed verschwunden (seit ~28. Mai 02:00 UTC).

### 12.5 Was korrekt ist (unverändert)

- `process_mheard_monthly` (`:2314`) / `process_mheard_yearly` (`:2202`): **chart == DB.** Sie lesen
  1h-Buckets + rollen 5min on-the-fly, setzen Gap-Marker bei >6 h Lücke (`HOURLY_GAP_THRESHOLD`).
  Die sichtbaren „Löcher" sind der korrekt gerenderte 22-h-Sprung zwischen den 00–02-Stauch-Buckets.
- `aggregate_hourly_buckets` (`:1733`), Live-Bucketing, Backfill-Aggregation: rechnen korrekt.
- `created_at` ist **nicht** kaputt — es ist eine TEXT-Spalte (`CURRENT_TIMESTAMP`, UTC). Frühere
  Annahme „1970/leer" war ein Query-Artefakt (`text/1000`-Coercion).

### 12.6 PROBLEM STATEMENT

Der Proxy stempelt BLE-mHeard-Signaldaten (`signal_log`, `signal_buckets`) mit der **node-gelieferten
Zeit** (`transform_mh` → `timestamp = node_timestamp`, `sqlite_storage.py:1213-1228`). Wenn die Node
fehlerhafte Tageszeit liefert (×12-Stauchung, Feb–28. Mai), wird sie verbatim gespeichert und
verfälscht alle Zeit-aggregierten mHeard-Charts. Die Monats-/Jahres-Chart-Funktionen sind **korrekt**;
der Fehler sitzt in der **Ingestion** (Vertrauen in Node-Tageszeit) plus **Altlast-Reste**, die der
Rollup wieder hochspült. Es gibt keinen Schutz gegen eine Node-Zeit-Regression.

### 12.7 PLAN (zwei Teile)

**Teil A — Einmal-Bereinigung (Symptom, endgültig diesmal):**
- Backup `messages.db` (Online-Snapshot inkl. WAL).
- `DELETE FROM signal_buckets WHERE bucket_size=3600000 AND bucket_ts < <28.Mai 02:00 UTC ms>;`
  (gezielt, **nicht** blanket — saubere 1h-Buckets entstehen ab dem nächsten Rollup). Entfernt nur die
  32 gestauchten Reste. Regeneriert **nicht**, weil die gestauchte 5min-Quelle bereits weg ist.

**Teil B — Code-Hardening (Ursache, Regressionsschutz):**
- mHeard-Signaldaten mit **Pi-Empfangszeit** statt Node-Zeit stempeln. Eingriffsstelle: `is_mheard`-Block
  (`sqlite_storage.py:1216-1228`) — `signal_log.timestamp` und `_accumulate_signal` mit
  `int(time.time()*1000)` füttern; `messages.timestamp`/`node_timestamp` unverändert lassen
  (Node-Zeit als Referenz erhalten).
- Variante zur Wahl: **unbedingt** Pi-Zeit, **oder** Sanity-Guard (Node-Zeit behalten, wenn
  `|node−pi| < Schwelle`, sonst Pi-Zeit). Beleg, dass Pi-Zeit keinen Batch-Dump kollabiert: Signaldaten
  sind gleichmäßig ~2400/h über **jede** Stunde verteilt (kein Spike) → mHeard kommt als Live-Strom.
- Backfill-Pfad (`_backfill_new_tables :602`) nutzt ebenfalls `messages.timestamp` (= Node-Zeit) →
  beim Hardening mitdenken (gilt nur bei Reconstruct alter Daten).
- **Wahre Wurzel ist node-seitig** (Firmware, vom User DK5EN selbst gepflegt); das Proxy-Hardening ist
  der Regressionsschutz, nicht der eigentliche Firmware-Fix.

### 12.8b ⚠️ KORREKTUR — die Firmware-/×12-Theorie ist FALSCH (siehe §13)

Nach User-Einwand („wenn Tages-/Wochen-Chart ok sind, kann es keine Firmware-Zeit sein") neu
analysiert. Ergebnis: **Es gibt keine ×12-Stauchung und keinen Firmware-Zeitfehler.** Die alten
1h-Buckets enthalten **normale Stunden-Counts**, nur eben **ausschließlich für ein ~2-Stunden-Fenster
pro Tag** (Rest des Tages fehlt). Ursache ist ein **Bug im Nightly-Job** (Reihenfolge prune→aggregate
+ `datetime.utcnow().timestamp()`-TZ-Fehler), nicht die Node-Zeit. Volle Begründung in **§13**.
Abschnitte §2.2, §10.1–10.2 (×12-Stauchung, Node-Zeit) sind damit **überholt**.

### 12.8 Durchgeführt (2026-06-05)

User-Entscheidung: (1) Bereinigung ja, (2) **unbedingt Pi-Zeit**, (3) Deploy nach mcapp.local **nur**
via `scripts/release.sh` (rpizero.local hat keine relevanten Daten, dort nicht testbar).

- **Teil A — Bereinigung (mcapp.local) erledigt:**
  - Backup: `/home/martin/db-backups/messages-20260605-pre-bucketwipe2.db` (32.4 MB, enthält die 32
    gestauchten Buckets).
  - `DELETE FROM signal_buckets WHERE bucket_size=3600000 AND bucket_ts<=1779933600000` (= 28. Mai
    02:00 UTC) → **32 → 0**. 5min-Buckets (11814) und `signal_log` (62535) unangetastet, wachsen weiter.
- **Teil B — Code-Hardening (development, implementiert):** `sqlite_storage.py` `is_mheard`-Block:
  `signal_log` und `_accumulate_signal` bekommen `signal_ts = int(time.time()*1000)` (Pi-Empfangszeit)
  statt `timestamp` (Node-Zeit). `messages.timestamp`/`node_timestamp` unverändert. ruff grün.
  - **Noch offen (kleiner Scope, nicht im Fix):** `_upsert_station_position` (station_positions.signal_ts/
    last_seen) und der Backfill-Pfad `_backfill_new_tables:506` nutzen weiter Node-Zeit
    (`messages.timestamp`). Heilt bei station_positions selbst beim nächsten Hören; Backfill nur relevant
    bei Voll-Reconstruct. Bei Bedarf nachziehen (Backfill → `created_at` als Pi-Zeit).
  - **Deploy:** ausstehend — via `./scripts/release.sh` (interaktiv) nach mcapp.local.

---

## 13. NEUE, KORREKTE DIAGNOSE (2026-06-05): Nightly-Job zerstört die 1h-Buckets

User-Einwand: „Wenn Tages- und Wochen-Chart ok sind, ist die Firmware-Zeit-Theorie Unsinn." **Korrekt.**
Die Neu-Analyse widerlegt die ×12-/Node-Zeit-Theorie und findet die **echte** Ursache — rein backend,
im Nightly-Rollup.

### 13.1 Es gibt KEINE Stauchung — die alten Tage haben nur ~2 h Daten

Vergleich pro Station, gestauchter Tag vs. sauberer Tag (Counts aus Buckets):

```
DB0ED-99   27.05. (alte 1h-Buckets):  00→97  01→81  02→7      Tagessumme 185, NUR Stunden 00–02
DB0ED-99   04.06. (saubere 5min):     00→91  01→88  02→92 … 23→88   Tagessumme 2058, Stunden 00–23
```

Die **Stunden-Rate ist identisch** (~90/h in 00 und 01 an beiden Tagen). Eine ×12-Stauchung würde
einen **ganzen Tag (~2000)** in 2 h pressen → ~1000/Bucket. Tatsächlich stehen dort ~90/Bucket =
**eine normale Stunde**. Der 27.05. hat schlicht nur **2 Stunden Daten** (00:00–02:00 UTC), der Rest
des Tages **fehlt**. Keine Stauchung, sondern **Datenverlust 22 h/Tag**.

### 13.2 Der Bug: prune läuft VOR aggregate, plus `utcnow().timestamp()`-TZ-Fehler

Nightly-Job (`main.py:1458-1466`):
```python
remaining = await storage_handler.prune_messages(...)  # 1. löscht alte 5min-Buckets
await storage_handler.aggregate_hourly_buckets()  # 2. will sie DANACH zu 1h rollen
```

- `prune_messages` (`sqlite_storage.py:1672`) löscht 5min-Buckets älter als `prune_hours_pos` (Default
  **192 h = 8 Tage**) — **bevor** der Rollup sie sieht.
- **TZ-Bug** (`sqlite_storage.py:1611`): `now = datetime.utcnow()` ist **naiv**; `(now-…).timestamp()`
  interpretiert die UTC-Wandzeit als **Lokalzeit** → der prune-Cutoff ist um den lokalen UTC-Offset
  **älter** (CEST: −2 h, CET: −1 h). Empirisch verifiziert: `utcnow().timestamp()` vs `time.time()` =
  **7200 s** in CEST.
- `aggregate_hourly_buckets` (`:1742`) nutzt `time.time()` (korrekt), Cutoff = `now − 192 h`.

Folge — die Cutoffs klaffen genau um den lokalen Offset:
```
aggregate-Cutoff = now − 192 h            (korrekt)
prune-Cutoff     = now − 194 h (CEST)     (utcnow-Bug: 2 h zu alt)
```
prune läuft zuerst → es überleben **nur** 5min-Buckets im Alter 192–194 h = ein **2-Stunden-Fenster**,
das der Rollup danach aufrollt. Position des Fensters: Job läuft 04:00 lokal = **02:00 UTC** (CEST) →
Fenster **00:00–02:00 UTC**. In CET: Job 03:00 UTC, Offset 1 h → Fenster **02:00–03:00 UTC**.

**Das matcht die alte „DST-wandernde" Beobachtung exakt** (§2.3: Mai/CEST = UTC 00–02; Feb/CET =
UTC 02–03). Was als Node-/Firmware-Indiz galt, ist in Wahrheit die **Job-Laufzeit × DST**. Jede Nacht
wird nur ein ~2-h-Streifen pro Tag in 1h-Buckets gerettet; die übrigen ~22 h werden **ungerollt
geprunt und sind unwiederbringlich weg**.

### 13.3 Datenfluss: wie Live-Daten in 24h/7d/30d/1y aggregiert werden

```
mHeard-Beacon (RSSI/SNR)
   └─ store_message: is_mheard → INSERT signal_log (Rohzeile)
                              → _accumulate_signal → flush → signal_buckets (5-min, size=300000)
                                                                 │ Retention 8 Tage
   Nightly 04:00 lokal:  aggregate_hourly_buckets: 5-min (>8 Tage) → 1-h Buckets (size=3600000)
                                                                 │ Retention 365 Tage
Charts:
  24h + 7d  → process_mheard_store_parallel : liest NUR 5-min-Buckets der letzten 7 Tage
  30d       → process_mheard_monthly        : liest 1-h-Buckets + 5-min (on-the-fly zu 1h) der 30 Tage
  1y        → process_mheard_yearly         : liest 1-h-Buckets + 5-min der 365 Tage
```

**Warum 24h/7d ok, 30d/1y kaputt:**
- 24h/7d lesen **ausschließlich die 5-min-Buckets** (`process_mheard_store_parallel:2057`, Cutoff 7 d).
  Diese sind **vollständig** (00–23), weil sie gelesen werden **bevor** der Nightly-Job sie zerstört.
  → durchgehende Charts.
- 30d/1y lesen zusätzlich die **1-h-Buckets** (alles >~8 Tage). Genau die sind durch §13.2 auf ~2 h/Tag
  reduziert. → sparse + 22-h-Gap-Marker (`HOURLY_GAP_THRESHOLD=6h`). Der Übergang sparse→dicht im
  Chart liegt bei „heute − 8 Tagen" (dort wechselt die Quelle von 1h-Buckets auf 5min). Frontend rendert
  nur, was geliefert wird (`MHeardStore.ts:118` `{x: timestamp, y: …}`) → **chart == DB**, kein
  Frontend-Bug, kein TZ-Bug im Frontend.

### 13.4 Was damit überholt ist

- ×12-Stauchung (§2.2), Node-`mh_time` als Ursache (§5, §10.1–10.2), „Selbstheilung über wanderndes
  Fenster" — **alles hinfällig.** Die Node-Zeit war nie das Problem; `chart == DB` und „Aggregierung
  rechnet korrekt" bleiben richtig, aber die **Verlustquelle** ist der Nightly-Job, nicht die Ingestion.
- Die **Pi-Zeit-Stempelung** (§12.7 Teil B, bereits in `development` implementiert) ist **orthogonal**:
  sie behebt diesen Bug **nicht**. Sie bleibt als optionales Hardening sinnvoll, war aber auf die falsche
  Theorie gestützt → mit dem User klären, ob behalten oder zurücknehmen.
- Die Bereinigung (§12.8) hat keine „Artefakte" gelöscht, sondern die **echten (aber unvollständigen)
  2-h/Tag-Reste**. Vertretbar (sie rendern irreführend, Volldaten sowieso verloren), aber im Backup
  erhalten.

### 13.5 WAY FORWARD (echter Fix)

1. **Reihenfolge umdrehen (kritisch):** im Nightly-Job `aggregate_hourly_buckets()` **vor**
   `prune_messages()` aufrufen (`main.py:1465` vor `:1460`). Dann rollt der Rollup **alle** 5min-Buckets
   >8 Tage (volle 24-h-Tage) in 1h-Buckets und löscht sie; der anschließende prune findet nichts mehr.
2. **TZ-Bug fixen:** `datetime.utcnow()` → `datetime.now(timezone.utc)` (bzw. `time.time()`) in
   `prune_messages:1611`. Betrifft auch messages/signal_log/telemetry-Retention (aktuell um den lokalen
   Offset zu aggressiv). **Wichtig:** TZ-Bug **allein** (ohne Reihenfolge-Fix) macht es **schlimmer** —
   dann ist das Überlebensfenster ~0 und es entstehen **gar keine** 1h-Buckets. Reihenfolge zuerst.
3. **Verifikation:** nach Deploy einen Nightly-Lauf abwarten; neue 1h-Buckets müssen **00–23** über den
   ganzen Tag spannen (nicht 00–02). 30d/1y-Chart wird ab dann täglich vollständiger.
4. **Altdaten:** nicht reparierbar (Roh-5min >8 Tage geprunt). 30d-Chart heilt in ~3–4 Wochen voll,
   1y-Chart über das Jahr — diesmal **wirklich**, weil die Verlustursache behoben ist.
5. **Tests:** ein gezielter Test für die Job-Reihenfolge/Bucket-Promotion wäre wertvoll (heute deckt
   kein Test `aggregate_hourly_buckets` vs. prune ab).

### 13.6 Implementiert (2026-06-05, `development`, noch nicht deployed)

- **Pi-Zeit-Stempelung zurückgenommen** (§12.7 Teil B) — war auf die falsche Theorie gestützt.
- **`main.py:1458`:** `aggregate_hourly_buckets()` läuft jetzt **vor** `prune_messages()`.
- **`sqlite_storage.py:1611`:** `datetime.utcnow()` → `datetime.now(timezone.utc)` (Import ergänzt).
- ruff grün, Syntax ok. Empirisch: `now(timezone.utc).timestamp() == time.time()` (Δ 0 s).
- **Verifikation nach Deploy:** erster Nightly-Lauf (06.06. 04:00 CEST) rollt ~24 h (Stunden **00–23**)
  in 1h-Buckets; danach `SELECT strftime('%H',bucket_ts/1000,'unixepoch'),COUNT(*) FROM signal_buckets
  WHERE bucket_size=3600000 GROUP BY 1` → muss alle 24 Stunden zeigen.
- **Deploy:** ausstehend via `./scripts/release.sh` nach mcapp.local.

### 13.7 Deployed (2026-06-05 22:25) + Verifikations-Checkliste

Deployed nach mcapp.local (slot-2, Service-Restart 22:25). Verifiziert: `main.py:1464`
`aggregate_hourly_buckets()` **vor** `prune_messages()`; `sqlite_storage.py:1606`
`datetime.now(timezone.utc)`. „Next DB prune scheduled … 04:00". Aktueller DB-Stand:
**1h-Buckets = 0**, 5min-Buckets 2026-05-28 20:30 → jetzt (ältester genau 8.0 Tage alt).

**Wichtiger Kontext für die Erwartung:** Der alte (buggy) Job am 5. Juni 04:00 hat die früheren
5min-Buckets des 28. Mai bereits geprunt; jetzt beginnt die 5min-Historie erst **28. Mai 20:30**.
Daher rollt der **erste** Lauf unter neuem Code (6. Juni 04:00 CEST = 02:00 UTC, Cutoff = 29. Mai
02:00 UTC) nur ~5,5 h auf → 1h-Buckets für die Stunden **20–23 (28.5.) + 00–01 (29.5.)**. Ein
**voller 24-h-Tag** entsteht erst ab dem Lauf am **7. Juni** (rollt 29. Mai 02:00 → 30. Mai 02:00).

**Check 1 — nach 6. Juni 04:00 CEST (erstes Lebenszeichen):**
```sql
SELECT strftime('%H',bucket_ts/1000,'unixepoch') h, COUNT(*), SUM(count)
FROM signal_buckets WHERE bucket_size=3600000 GROUP BY h ORDER BY h;
```
→ **Erwartung:** Stunden **außerhalb 00–02** vorhanden (v. a. 20–23). Schon das beweist den Fix —
der alte Bug hätte **nur** 00–02 erzeugt. (Noch nicht 24 h, siehe Kontext oben.)

**Check 2 — nach 7. Juni 04:00 CEST (definitiv):** gleiche Query → **alle 24 Stunden 00–23**, je
Station grob gleichverteilt. Plus Gegenprobe der Rate (sollte der 5min-Rate eines sauberen Tages
entsprechen, z. B. DB0ED-99 ~90/h):
```sql
SELECT callsign, strftime('%H',bucket_ts/1000,'unixepoch') h, count
FROM signal_buckets WHERE bucket_size=3600000 AND callsign='DB0ED-99' ORDER BY bucket_ts;
```

**Check 3 — journalctl (Reihenfolge):** `sudo journalctl -u mcapp.service --since "2026-06-06 03:59"`
→ „Aggregated old 5-min buckets into hourly buckets" muss **vor** „Nightly prune complete" stehen.

**Check 4 — Charts (Frontend):** 24h/7d unverändert dicht; 30d/1y dicht für die letzten ~8 Tage,
davor leer (ehrlich, Altdaten gelöscht). Ab dem 6./7. Juni keine **neuen** 2-h-Streifen-Tage mehr.

**Rote Flaggen:**
- Neue 1h-Buckets **wieder nur 00–02** → Fix nicht aktiv (falscher Slot / kein Restart) → Code im
  aktiven Slot + Restart prüfen.
- **Gar keine** 1h-Buckets nach dem Lauf → aggregate fand nichts / prune zu aggressiv → 5min-Alter
  vs. Cutoff prüfen, journalctl auf „Nightly prune failed".

**Heilung:** 30d-Chart füllt sich täglich um einen vollen Tag, ~vollständig in 3–4 Wochen; 1y über
das Jahr. Altdaten >8 Tage bleiben verloren (Roh-5min geprunt), aber die Verlustursache ist behoben.

### 13.8 Check 1 BESTANDEN (2026-06-06, Lauf 04:00 CEST)

Erster Nightly-Lauf unter neuem Code. **Fix wirkt.**

```
1h-Buckets Stunden-Verteilung (UTC):  00→316  01→334  02→22  20→167  21→305  22→299  23→319
1h-Buckets Range: 2026-05-28 20:00 → 2026-05-29 02:00 (40 Zeilen, ~5,5 h wie vorhergesagt)
DB0ED-99:  20:00→39  21:00→94  22:00→70  23:00→74  00:00→63  01:00→82  02:00→6   (normale Rate, ~90/h)
journalctl 04:00: "Aggregated old 5-min buckets…" VOR "Nightly prune complete"  ✓ Reihenfolge korrekt
5min-Range jetzt: 2026-05-29 02:05 → jetzt (aufgerollte < 29.5. 02:00 wurden gelöscht)
```

- **Stunden 20–23 vorhanden** = außerhalb des alten 00–02-Streifens → der alte Bug ist weg.
- **Counts normal** (nicht gestaucht). Kein 2-h-Streifen mehr.
- Noch nicht 24 h — erwartet (Quellfenster durch alten Prune auf 28.5. 20:30 verkürzt).

**Check 2 (definitiv) — nach 7. Juni 04:00 CEST:** Lauf rollt 5min 29.5. 02:05 → 30.5. 02:00 (voller
Tag, intakt) → 1h-Buckets müssen **alle 24 Stunden 00–23** zeigen.

### 13.9 Check 2 BESTANDEN — Bug endgültig behoben (2026-06-07, Lauf 04:00 CEST)

```
1h-Buckets Stunden-Verteilung (UTC):  ALLE 24 (00–23) vorhanden, je ~300–700 Counts/Stunde
1h-Buckets Range: 2026-05-28 20:00 → 2026-05-30 02:00 (177 Zeilen)
DB0ED-99 am 29.5. (voller Tag): 24/24 Stunden, 61–103/h, gleichmäßig — wie ein sauberer Tag
journalctl 04:00: "Aggregated…" VOR "Nightly prune complete"  ✓
```

**Verdict:** Der Nightly-Rollup produziert jetzt **vollständige Tage** (00–23) mit normalen Counts.
Kein 2-h-Streifen, keine Stauchung, keine Datenverlust-Lücke mehr. **Pipeline end-to-end verifiziert.**
Frühere Tage (≤28.5.) bleiben unvollständig (Quellfenster vom alten Bug verkürzt); jeder neue Tag
ab 29.5. ist vollständig. 30d-Chart vervollständigt sich täglich, ~komplett in 3–4 Wochen.

**Saga abgeschlossen.** Ursache war §13 (Nightly-Job: prune vor aggregate + `utcnow().timestamp()`-TZ),
nicht Firmware/Node-Zeit (§2/§10 überholt). Fix: Reihenfolge + `datetime.now(timezone.utc)`.
