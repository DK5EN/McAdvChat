# CLAUDE.md

## Project Overview

McApp is a message proxy service for MeshCom (LoRa mesh network for ham radio operators). It bridges MeshCom nodes with web clients via SSE/REST (FastAPI), supporting both UDP and Bluetooth Low Energy (BLE) connections. Runs on Raspberry Pi, serves a Vue.js web app through lighttpd.

Companion frontend: `/Users/martinwerner/WebDev/webapp` (separate git repo — commit each repo independently).

## Architecture

Entry point: `src/mcapp/main.py` → `MessageRouter` (central pub/sub hub connecting UDP, BLE, SSE, and command handlers). All source in `src/mcapp/`. The `commands/` package uses mixin-based architecture assembled in `handler.py`.

See `doc/dataflow.md` for data flow diagrams and `doc/2026-02-11_1400-position-signal-architecture-ADR.md` for position/signal table design.

## Package Management

- **Python**: `uv` only — NEVER use `pip` or `venv`
- **Frontend** (webapp repo): `npm`

## Development Commands

```bash
export MCAPP_ENV=dev       # Enables verbose logging
uv run mcapp               # Run locally
uvx ruff check             # Lint (must pass before committing)
uvx ruff check --fix       # Auto-fix
uvx ruff format            # Format (must be clean before committing)
./scripts/release.sh       # Create release (interactive, run from development branch)
```

## Code Quality

- `uvx ruff check` is mandatory — zero tolerance for errors and warnings
- `uvx ruff format` is the mandatory formatter — run it before committing (`uvx ruff format --check .` must be clean)
- **Ruff config** in `pyproject.toml`: `line-length = 100`, `target-version = "py311"`, strict rule set (bugbear, security, pylint, pyupgrade, simplify, datetime-TZ, async, pathlib, naming, …) — see the `[tool.ruff.lint]` section for the full list and documented ignores
- **Keep all `[tool.ruff*]` sections identical** across `pyproject.toml`, `ble_service/pyproject.toml` and mc-chat's `pyproject.toml` — the classifier subtree must lint clean under the same rules in both repos
- New `# noqa` markers need a trailing reason comment and should stay rare — prefer a real fix
- **Git branches**: `development` (default), `main` (production)
- **Commit format**: `[type] description` — types: feat, fix, perf, refactor, chore, docs, test

## Testing

No pytest — tests are built into the app and run at startup when `has_console()` is true.
Headless (no TTY, no `/etc/mcapp` needed): `uv run python scripts/run_startup_tests.py` — exit 0 = all passed (needs network for weather APIs).
Suites:
- `message_router.test_suppression_logic()`
- `command_handler.run_all_tests()` (in `src/mcapp/commands/tests.py`)
- `classifier.run_all_tests()` (in `src/mcapp/classifier/tests.py`) — uses an ephemeral tempfile SQLite so the live DB is untouched

## Classifier Subtree

`src/mcapp/classifier/` is a **git subtree** pulled from mc-chat (`meshcom_mock/classifier/`).
**Do not edit files in this directory directly** — edits belong in mc-chat and are synced here.

Every inbound message is annotated inline in `store_message()` with a primary `category`, free-form `tags` (JSON array), `info_score ∈ [0, 1]`, and a 12-char `template_hash`. Messages are never dropped — the webapp decides what to hide.

- Layer 1 (`classifier/rules.py` + `seed.py`): data-driven regex rules in the `classifier_rules` table. First match by `(priority, id)` sets category; all matches contribute tags. Seed defaults are `builtin=1` — editable via REST but never deleted.
- Layer 2 (`classifier/template.py`): fingerprint normalizes URLs/emojis/numbers and hashes sha1[:12]. `beacon_templates` tracks count/srcs/auto_beacon per fingerprint. Multi-threshold: 8 lifetime, 5 in 24 h, 3 in 72 h. `user_action='promote'|'demote'` overrides.
- Layer 3 (`classifier/score.py`): blended info score with tunable weights.
- Orchestrator (`classifier/classify.py`): `Classifier.classify(msg)` combines all three layers. Never blocks ingestion.

Rule mutations bump `classifier_ver` via `storage.bump_classifier_version()` + `classifier.load()`. Startup auto-backfills once per version via a `backfill_done:v{N}` marker in `classifier_meta`.

SSE events: `proxy:classifier_rules` (on connect + after mutations), `proxy:classifier_stats` (60 s), `proxy:classifier_template_event` (threshold crossing), `proxy:reclassify_progress` (batch updates).

### How to sync (after mc-chat classifier changes are committed)

```bash
cd /Users/martinwerner/WebDev/mc-chat
git subtree split --prefix=meshcom_mock/classifier -b classifier   # update split branch

cd /Users/martinwerner/WebDev/MCProxy
git subtree pull --prefix=src/mcapp/classifier mc-chat classifier --squash
```

The `mc-chat` remote is a local path remote already configured in this repo.

Schema migrations: add new columns to `messages` or new tables via a `current_version < N` block in `sqlite_storage.initialize()` and bump `SCHEMA_VERSION`. Current schema: v16.

## Configuration

Config file: `/etc/mcapp/config.json` (dev: `/etc/mcapp/config.dev.json`, auto-selected via `MCAPP_ENV=dev`).
BLE mode: `remote` or `disabled` (`MCAPP_BLE_MODE` env override). See `ble_service/README.md` for BLE service API.

## Key Gotchas

- **All DB timestamps are in milliseconds** (not seconds). Divide by 1000 for `datetime.fromtimestamp()`. Forgetting this causes `ValueError: year 58089 is out of range`.
- **SSH + python3 -c quoting**: Use single quotes for Python code, `\"` for strings inside. Never use f-strings with dict key access — use `%` formatting. Or write a temp script with `cat > /tmp/q.py << 'PYEOF'`.
- **MHeard beacons** (RSSI/SNR, no coordinates) and **position beacons** (lat/lon, no signal) are disjoint packet types. `station_positions` merges them per callsign with independent field-group updates.

## Deployment

Two Raspberry Pi Zero 2W targets: `mcapp.local` (production) and `rpizero.local` (integration).

**On-device layout (both Pis, same structure):**
- Slot system: `~/mcapp-slots/slot-0`, `slot-1`, `slot-2`; `~/mcapp-slots/current` symlink points to active slot
- Service: `systemctl status mcapp` — `ExecStart=/home/martin/.local/bin/uv run mcapp`
- Source: `~/mcapp-slots/current/src/mcapp/`
- Config: `/etc/mcapp/config.json`
- DB: `/var/lib/mcapp/messages.db` (SQLite, WAL mode, schema v16)
- Logs: `sudo journalctl -u mcapp.service -f`

See `bootstrap/README.md` for installation, `doc/tls-architecture.md` for TLS setup, `doc/tls-maintenance-SOP.md` for maintenance.
