# Tech Debt: Komplexe Funktionen & Refactoring-Kandidaten

Stand: 2026-07-06 (refreshed nach fable-verdict.md Wellen 1-7 + Track M; siehe dort
für den vollständigen Audit-Trail)

## Ziel

Code wartbar machen: keine tiefen Verschachtelungen, keine Spaghetti-Logik,
alles erwartbar und einfach verständlich.

---

## Bereits erledigt

### `commands/parsing.py` — parse_command_v2 (Dispatch-Table)
- **Was war:** `_parse_command_v1()` in `routing.py` — 130-Zeilen if/elif-Kette,
  topic-Parsing mit 5 Verschachtelungsebenen
- **Was ist:** Dispatch-Table `_COMMAND_PARSERS` + je eine kleine Funktion pro Command
- **Nächster Schritt:** Shadow-Vergleich bestätigen, dann v1 entfernen

### `main.py: route_command()` — Kein Problem
- Analysiert und für sauber befunden. 16 Commands, jeder Branch ein Einzeiler-Dispatch.
  `startswith`-Checks für Device-Commands passen nicht in ein Dict. Bleibt so.

### `commands/routing.py: _message_handler()` — Extrahiert + Logger
- **Was war:** 152 Zeilen, `has_console`/`print`-Blocks, Exception-Handling inline
- **Was ist:** 79 Zeilen. `_parse_and_execute()` und `_error_response_text()` extrahiert.
  Alle prints durch `logger.debug()`/`logger.warning()` ersetzt.

### `commands/data_commands.py: handle_search()` — SQL-Aggregation
- **Was war:** 108 Zeilen — `search_messages()` holte ALLE Messages (ignorierte
  callsign/search_type komplett), Filtering + Counting + MAX-Tracking + SID-Extraktion
  alles in Python-Loop
- **Was ist:** `get_search_summary()` im Storage-Layer macht 3 gezielte SQL-Queries
  (COUNT/MAX/GROUP BY, DISTINCT destinations, SID-Gruppierung). `handle_search()`
  ist jetzt 52 Zeilen reines Response-Formatting.

### `commands/ctcping.py: _handle_ack_message()` — Erledigt
- **Was war:** 124 Zeilen, 4 Verschachtelungsebenen, vermischte Verantwortlichkeiten
- **Was ist:** 54 Zeilen, flache Early-Returns, Logik in `_record_ack_result()` extrahiert
- **Dual-Tracking behoben** (fable-verdict.md Legibility-Audit, 2026-07-06): die
  Reconciliation-Block, der `results`-abgeleitete Counts gegen `test_summary.completed`/
  `.timeouts` verglich und bei Abweichung warnte, wurde entfernt — CMD-03s
  Single-Increment-Design macht die beiden Counts durch Konstruktion gleich, der Block
  konnte also nur "diese können auseinanderlaufen" für einen Fall behaupten, der nicht
  eintreten kann. `test_summary.completed`/`.timeouts` sind jetzt die einzige Quelle.
  Zusätzlich: das Completion-State-Machine-Invariant (kein `await` zwischen
  Completion-Check und Guard-Set) ist jetzt direkt im Code dokumentiert
  (`_trigger_completion_if_done`-Docstring), nicht mehr nur in fable-verdict.md.

### `commands/routing.py: _should_execute_command()` — Kein Problem
- 42 Zeilen, Early Returns, sauber strukturiert. Bleibt so.

### `sqlite_storage.py: _migrate_v3_to_v4()` — Kein Problem
- Strikt sequentielle Migration, klar kommentiert. Bleibt so.

### `sqlite_storage.py: _backfill_new_tables()` — Kein Problem
- 5 eigenständige SQL-Statements mit Kommentaren. Bleibt so.

---

## Erledigt (fable-verdict.md Wellen 1-7)

### `main.py: _udp_message_handler()` / `_ble_message_handler()` — Erledigt
- **Shadow-Logik** (`compare_outbound_decision()`) vollständig entfernt (Wave 1/Track U-Ära;
  `doc/check-and-remove-outbound-shadow.md` existiert nicht mehr).
- **Logger statt print/has_console** — seit Wave 4 (CO-09) durchgehend `logger.debug/info`.
- **CO-02 (Wave 5):** beide Handler sind jetzt ~90% identische dünne Wrapper um einen
  gemeinsamen `_handle_outbound(routed_message, protocol, send)` (main.py:864) —
  ~70 Zeilen Duplikation entfernt.

### `ble_protocol.py: decode_binary_message()` — Erledigt
- **BLE-05/06 (Wave 6.5):** in `_decode_ack_frame()`/`_decode_data_frame()` extrahiert
  (ble_protocol.py:78, 112); `locals()`-Dict-Comprehension durch explizites Dict-Bauen ersetzt;
  Rückgabetyp `dict | None` statt `dict | str` (kein Bare-Error-String mehr); benannte
  Frame-Offset-Konstanten statt Magic Numbers.

---

## Offen

Keine offenen Punkte aus diesem Dokument mehr. Für laufende/zukünftige Code-Qualitätsarbeit
siehe `fable-verdict.md` (Waves 1-7 abgeschlossen; Section 9 "Open decisions" und die
"Discovered during waves"-Liste dort für alles, was seither neu aufgefallen ist).

---

## Zusammenfassung

| Funktion | Status |
|----------|--------|
| `_udp_message_handler()`/`_ble_message_handler()` (main) | Erledigt |
| `decode_binary_message()` (ble_protocol) | Erledigt |
| `_message_handler()` (routing) | Erledigt |
| `_handle_ack_message()` (ctcping) | Erledigt |
| `handle_search()` (data_commands) | Erledigt |
| `_should_execute_command()` (routing) | Kein Problem |
| `_migrate_v3_to_v4()` (sqlite_storage) | Kein Problem |
| `_backfill_new_tables()` (sqlite_storage) | Kein Problem |
