# Spam Filter — Backend Handoff

Audience: backend developer working on the `meshcom_mock` Python package
(`/Users/martinwerner/WebDev/mc-chat`). This document is the execution
plan for the server-side classification + spam-filter system that the
Vue3 webapp consumes.

Companion document: `spam-filter-FE.md` (describes the webapp contract).

---

## 1. Goals

Tag every inbound message with:

- A single **primary category** (`timestamp_beacon`, `wx_beacon`,
  `node_advert`, `sw_advert`, `greeting`, `qso`, `alert`, `directed`,
  `other`).
- Zero or more free-form **tags** (`has_url`, `emoji_heavy`,
  `auto_beacon`, ...).
- An **info score** in `[0.0, 1.0]`.
- A **template hash** identifying messages that share a normalized shape.

The server never drops messages based on classification — it only
annotates. The webapp decides what to hide based on user preferences.
Deterministic rules live in the database so we can iterate without a
redeploy.

Design principles:

- **Additive schema** — existing API contracts keep working.
- **Flat SSE payloads** — every new event maps 1:1 to a `EventMap` entry
  in the webapp.
- **Rules are data**, not code.
- **Three independent layers** — each can be built, tested, and rolled
  out on its own.

---

## 2. Layers

### Layer 1 — Deterministic regex rules

A table of regex rules with a `category`, priority, scope (which field to
match against), and optional extra tags. First match wins for the primary
category; all matching rules contribute extra tags.

### Layer 2 — Template fingerprint

For each message compute a normalized template (digits → `#`, emojis →
`E`, URLs → `URL`, whitespace collapsed) and take a short SHA-1 hash
(12 hex chars). Maintain per-template statistics. When a template crosses
the auto-beacon threshold — default **≥ 5 messages from the same `src`
sharing the same `template_hash` within 24 h** — set `auto_beacon=1`
and emit an SSE event. Users can promote / demote individual templates.

### Layer 3 — Info score

Compute a 0..1 blended score that the UI uses for ranking and threshold
filtering. Positive: distinct word count, first-seen template, directed
message, group conversational density. Negative: emoji ratio, URL
ratio, template repetition count, known-beacon sender.

### Layer 4 — Future (not in this milestone)

Tiny local classifier trained on weak labels from Layers 1–3. Out of
scope for this PR; leave architecture hooks so a future classifier can
write additional `tags` without disrupting Layer 1's `category`.

---

## 3. Schema changes

One Alembic-free SQL migration run on startup (same pattern as the
existing tables in `storage.py`). All new columns are nullable →
backward-compatible.

```sql
-- messages: annotate every row
ALTER TABLE messages ADD COLUMN category        TEXT;
ALTER TABLE messages ADD COLUMN tags            TEXT;    -- JSON array string
ALTER TABLE messages ADD COLUMN info_score      REAL;
ALTER TABLE messages ADD COLUMN template_hash   TEXT;
ALTER TABLE messages ADD COLUMN classifier_ver  INTEGER;

CREATE INDEX IF NOT EXISTS idx_messages_category      ON messages(category);
CREATE INDEX IF NOT EXISTS idx_messages_template_hash ON messages(template_hash);

-- Layer 1 rules
CREATE TABLE IF NOT EXISTS classifier_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    pattern     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'msg',       -- 'msg' | 'src' | 'dst' | 'combined'
    category    TEXT NOT NULL,
    extra_tags  TEXT,                              -- JSON array
    priority    INTEGER NOT NULL DEFAULT 100,
    enabled     INTEGER NOT NULL DEFAULT 1,
    builtin     INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Layer 2 template stats
CREATE TABLE IF NOT EXISTS beacon_templates (
    template_hash TEXT PRIMARY KEY,
    example_msg   TEXT NOT NULL,
    example_src   TEXT NOT NULL,
    srcs          TEXT NOT NULL,                   -- JSON array
    count         INTEGER NOT NULL DEFAULT 0,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    auto_beacon   INTEGER NOT NULL DEFAULT 0,
    user_action   TEXT                             -- 'promote' | 'demote' | NULL
);

CREATE INDEX IF NOT EXISTS idx_beacon_templates_count      ON beacon_templates(count DESC);
CREATE INDEX IF NOT EXISTS idx_beacon_templates_last_seen  ON beacon_templates(last_seen DESC);
```

### Why JSON in a TEXT column for `tags` / `srcs`
At the target volume (~700 messages / day, ~7k / 10 days) the simpler
storage wins: no join table, straightforward serialization. If we ever
need server-side tag-based queries beyond the primary `category`, we'll
promote to a join table in a follow-up migration.

### `classifier_ver`
Small monotonic integer. Bumped whenever rules are created / edited /
deleted or the classifier code itself changes its hashing / scoring
formula. Stored on each classified row so we can cheaply find rows that
need reclassifying.

---

## 4. Module layout

New files (all under `meshcom_mock/`):

```
classifier/
    __init__.py
    rules.py            # Layer 1: regex rules, load/match
    template.py         # Layer 2: normalize, hash, update stats, threshold
    score.py            # Layer 3: info score
    classify.py         # orchestrator — public API
    seed.py             # built-in default rules
```

Changes to existing files:

```
storage.py              # schema migration + store_message() hook + helpers
api.py                  # new REST endpoints + SSE extensions
main.py                 # start periodic classifier-stats broadcaster
```

Test files:

```
tests/test_classifier_rules.py
tests/test_classifier_template.py
tests/test_classifier_score.py
tests/test_classifier_api.py
```

---

## 5. Orchestrator API — `classify.py`

The public surface the rest of the codebase uses.

```python
@dataclass(frozen=True, slots=True)
class Classification:
    category: str  # always set, defaults to "other"
    tags: tuple[str, ...]  # sorted, deduplicated
    info_score: float  # 0..1
    template_hash: str  # always set (12 hex)
    classifier_version: int  # snapshot taken at classify time


class Classifier:
    def __init__(self, storage: MessageStorage) -> None: ...

    async def load(self) -> None:
        """Load rules from DB, compile regexes."""

    async def classify(self, msg: dict) -> Classification:
        """Layer 1 + Layer 2 (update + threshold) + Layer 3 (score)."""

    async def reclassify(
        self,
        since: int | None = None,
        category: str | None = None,
        progress_cb: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> str:
        """Return job_id; runs in a background task."""
```

### Flow on a single new message

1. `rules.match(msg, compiled_rules)` → `(category, extra_tags)`
2. `template.fingerprint(msg["msg"])` → `template_hash`
3. `template.update_stats(template_hash, msg)` → side effect
4. `template.auto_beacon_status(template_hash)` → bool
   → add `auto_beacon` tag if true
5. `score.compute(msg, category, tags, template_stats)` → float
6. Package into `Classification` and return.

### Integration with `store_message()`

In `storage.py`, call the classifier inline **before** the INSERT. The
classification fields are written as part of the same INSERT, so we
never have a half-classified row. If classification raises (regex
compile failure, anything), log a warning and fall back to `category =
"other", info_score = 0.5, template_hash = sha1(msg)[:12], tags = []` so
the pipeline never blocks on classifier bugs.

---

## 6. Layer 1 — rule matching

### Scope semantics

- `scope='msg'` — match `pattern` against `msg["msg"]`
- `scope='src'` — against `msg["src"]`
- `scope='dst'` — against `msg["dst"]`
- `scope='combined'` — against `f"{msg['src']}|{msg['dst']}|{msg['msg']}"`

### Compilation
Compile once, cache in memory (`re.Pattern`). Invalidate cache on
`POST/PATCH/DELETE /api/classifier/rules`.

### Evaluation
Sort by `(priority ASC, id ASC)` once at load time. For each enabled
rule in order, run `regex.search()`. The **first** match sets
`category`. **All** matching rules contribute `extra_tags`.

### Default rules (seeded on first run — `builtin=1`)

| Priority | Name | Scope | Pattern | Category | Extra tags |
|---|---|---|---|---|---|
| 10 | CET timestamp | msg | `^\{CET\}\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}` | `timestamp_beacon` | `beacon` |
| 20 | WX emoji block | msg | `🌡.*(📊|💧)` | `wx_beacon` | `beacon,emoji_heavy` |
| 21 | WX text | msg | `(?i)(WX\s|Temp[:= ]).*(QNH|hPa)` | `wx_beacon` | `beacon` |
| 22 | WX short emoji | msg | `🌡️\d+.*\[` | `wx_beacon` | `beacon,emoji_heavy` |
| 30 | MeshCom WebDesk advert | msg | `MeshComWebDesk V\d` | `sw_advert` | `beacon` |
| 31 | MeshCom WebDesk banner | msg | `\*\*\*MeshCom WebDesk` | `sw_advert` | `beacon` |
| 40 | URL advert | msg | `https?://\S+` | `node_advert` | `has_url` |
| 50 | Greeting DE | msg | `(?i)^(73|hallo|servus|moin|nabend|ahoi|guten (morgen|abend|tag))` | `greeting` | — |
| 60 | Earthquake DE/EN | msg | `(?i)(erdbeben|earthquake|magnitude\s+\d)` | `alert` | — |
| 90 | Direct callsign | dst | `^[A-Z0-9]+-\d+$` | `directed` | — |

`builtin=1` rules cannot be deleted via API (returns 404), but can be
edited (pattern, priority, enabled). This lets the user tune defaults
without losing them.

---

## 7. Layer 2 — template fingerprint

### Normalization (`template.fingerprint()`)

```python
def fingerprint(text: str) -> str:
    t = text.strip()
    t = URL_RE.sub("URL", t)  # http(s) URLs first
    t = EMOJI_RE.sub("E", t)  # emoji ranges
    t = re.sub(r"\d+(?:[.,]\d+)?", "#", t)  # numbers (int/float)
    t = re.sub(r"\s+", " ", t)  # whitespace collapse
    t = t.lower()
    return hashlib.sha1(t.encode("utf-8")).hexdigest()[:12]
```

Regex defs:

- `URL_RE = re.compile(r"https?://\S+")`
- `EMOJI_RE` — union of `\U0001F300-\U0001FAFF` plus `\u2600-\u27BF` plus
  ZWJ sequence cleanup (see `protocol.py` style for ranges).

### Stats update (`template.update_stats()`)

UPSERT into `beacon_templates`:
- `count += 1`
- `last_seen = now`
- `first_seen = now` on insert
- `example_msg`, `example_src` refreshed to the most recent
- `srcs` — add `msg["src"]` to the JSON list (unique, capped at 20 entries)

### Auto-beacon threshold

Default: **`count_same_src_same_template_within_24h >= 5`** →
`auto_beacon = 1`.

Implementation: a small helper that counts matching recent messages
either via a lookup on the `messages` table (accurate, slightly slower)
or by tracking `(src, template_hash) → rolling_count` in a memo dict.
Recommend the table-based version first; it's simple, correct across
restarts, and the index on `template_hash` keeps it cheap.

When a template transitions 0 → 1 on `auto_beacon`, emit
`proxy:classifier_template_event` via the SSE hub.

Threshold values live in `classifier/template.py` as module constants so
they can be tweaked in code or exposed via config later:

```python
AUTO_BEACON_THRESHOLD = 5
AUTO_BEACON_WINDOW_SEC = 24 * 60 * 60
```

### User overrides

`user_action` has three values:
- `'promote'` — force this template to behave like an auto-beacon even
  if it hasn't crossed the threshold. UI hides it.
- `'demote'` — this template is **never** marked `auto_beacon`, even if
  the threshold is crossed. UI shows it.
- `NULL` — no override, automatic behavior.

The server respects the override when setting the `auto_beacon` tag at
classify time: promoted → tag set regardless of count; demoted → tag
never set.

---

## 8. Layer 3 — info score

Blend (cheap, transparent, tunable). All contributors are 0..1 then
weighted and clamped.

```python
def compute(msg: dict, category: str, tags: set[str], tpl_count: int) -> float:
    text = msg["msg"] or ""
    # Positive contributors
    word_count = len(re.findall(r"\w+", text))
    len_factor = min(word_count / 15.0, 1.0)  # caps at 15 words
    directed = 1.0 if category == "directed" else 0.0
    group_chat = 1.0 if category == "qso" else 0.0
    freshness = 1.0 / (1 + tpl_count)  # inverse of template freq
    # Negative contributors
    emoji_density = count_emoji(text) / max(len(text), 1)
    url_density = 1.0 if "has_url" in tags else 0.0
    known_beacon = 1.0 if "beacon" in tags or "auto_beacon" in tags else 0.0

    score = (
        0.30 * len_factor
        + 0.20 * directed
        + 0.20 * group_chat
        + 0.15 * freshness
        - 0.25 * emoji_density
        - 0.10 * url_density
        - 0.40 * known_beacon
    )
    return max(0.0, min(1.0, score + 0.5))  # center around 0.5
```

Weights are constants; iterate once we have real data. Document them in
`classifier/score.py` with a short commentary block so tuning has an
obvious home.

---

## 9. REST endpoints — implementation notes

All live in `api.py`. Use the existing FastAPI app and the existing auth
middleware (if any).

### Rules
```
GET    /api/classifier/rules
POST   /api/classifier/rules
PATCH  /api/classifier/rules/{id}
DELETE /api/classifier/rules/{id}        # 404 when builtin
POST   /api/classifier/rules/test        # dry-run over last 500 messages
```

- `test` body: `{pattern, scope?, sample_msg?}`. Response:
  `{matches: bool, sample_matches: Message[]}`. Implementation:
  compile pattern → run over the last 500 rows of `messages` (LIMIT 500
  ORDER BY id DESC) → return up to 10 matching rows.
- On any mutation (POST / PATCH / DELETE): bump `classifier_ver`,
  invalidate compiled-rule cache, emit `proxy:classifier_rules` event
  with the full list.

### Templates
```
GET    /api/classifier/templates?min_count=5&auto_only=false&limit=100
PATCH  /api/classifier/templates/{hash}                 # {user_action}
POST   /api/classifier/templates/{hash}/preview         # last 20 messages
```

### Reclassify
```
POST /api/classifier/reclassify           # {since?, category?}
GET  /api/classifier/status
```

`reclassify` returns immediately with
`{job_id, estimated_rows}` and runs as an asyncio task. It:
1. Selects target rows (`classifier_ver < current_ver` or all, filtered).
2. Iterates in batches of 500.
3. Runs the classifier on each row, UPDATEs the row.
4. Emits `proxy:reclassify_progress` every batch and once more when
   `done=True`.

### Backward compatibility

`GET /api/messages` response already includes every column. Because we
added new columns, they just appear on every row. Old clients ignore
them.

### Flask-level details

Use the existing `SSEEvent(name, data)` emit pattern. Payloads must be
JSON-serializable dicts / lists that **exactly** match the types in
`spam-filter-FE.md` §2. No outer wrapping, no `{data: ...}` shell
(except for the existing `mesh:message` which already has it).

---

## 10. SSE events (emit points)

| Event | When | Payload |
|---|---|---|
| `proxy:classifier_rules` | on connect + after any rule mutation | `ClassifierRule[]` |
| `proxy:classifier_stats` | on connect + every 60 s | `{counts, recent_24h, top_templates}` |
| `proxy:classifier_template_event` | a template transitions to `auto_beacon=1` | `BeaconTemplate` |
| `proxy:reclassify_progress` | every batch while reclassify job runs + final | `{job_id, processed, total, done}` |

### Periodic stats

Add an asyncio task in `main.py` that calls
`classifier.collect_stats()` every 60 s and broadcasts via the SSE hub.
`collect_stats()` returns:

```python
{
  "counts": {cat: n for each category in last 30d},
  "recent_24h": {cat: n for each category in last 24h},
  "top_templates": [
    {"template_hash", "example_msg", "count", "auto_beacon"}
    # top 10 by count, last 7d
  ],
}
```

---

## 11. Migration & backfill

On startup:

1. Run schema migration (idempotent `CREATE TABLE IF NOT EXISTS` +
   safe `ALTER TABLE` — check `PRAGMA table_info(messages)` first).
2. If `classifier_rules` is empty → seed builtins (`seed.py`).
3. Check how many messages have `classifier_ver IS NULL`. If non-zero
   and the user config flag `auto_backfill_on_start = True` (default
   **True**), kick off a reclassify job in the background. Log the
   job_id.
4. Subscribe the periodic stats task.

The auto-backfill is opt-out so a fresh Pi deploy lights up with
correct counts immediately without user action. It runs as a normal
reclassify job so the frontend sees progress as usual.

---

## 12. Test strategy

- **`test_classifier_rules.py`** — unit tests per default rule with
  samples drawn from the real DB (the categories identified in the
  previous analysis). Include a rule-priority test so edits don't
  reshuffle accidentally.
- **`test_classifier_template.py`** — normalization test (golden
  strings → golden hashes), stats update test, auto-beacon threshold
  test (3 messages → off, 5 → on), user_action overrides.
- **`test_classifier_score.py`** — a small table of sample messages
  with expected score ranges (not exact, since weights are tunable).
- **`test_classifier_api.py`** — integration tests for each endpoint:
  CRUD, dry-run, reclassify progress via SSE captured by a test
  client.
- **Integration with `store_message()`** — one test that inserts a
  message and verifies `category`, `tags`, `info_score`,
  `template_hash`, `classifier_ver` are non-null on readback.

Per project conventions: `uv run pytest tests/ -m "not live"` must
remain green. No live tests needed for the classifier.

---

## 13. Implementation order (recommended)

1. Schema migration + `classifier_ver` column + `classifier_rules` +
   `beacon_templates` tables. Smoke-test with empty classifier.
2. `rules.py` + `seed.py` + Layer-1 integration in `store_message()`.
3. Layer-2 `template.py` + stats update + auto-beacon event emit.
4. Layer-3 `score.py`.
5. REST endpoints (rules CRUD + dry-run).
6. REST endpoints (templates + reclassify).
7. SSE events + periodic stats task.
8. Backfill on startup.
9. Tests per step.
10. Deploy to `rpizero.local`, watch live data for a day, tune weights
    and thresholds.

---

## 14. Future iterations

Parked for after the first rollout.

- **Callsign reputation** — track per-`src` ratio of `auto_beacon` vs
  non-beacon messages. A dedicated `callsign_profile` table can power a
  "known beacon sender" tag and feed the score as a negative
  contributor.
- **Alert pipeline** — `category='alert'` messages can emit a dedicated
  SSE event (`proxy:alert`) and integrate with the existing webapp
  Alerts card. USGS / EMSC earthquake formats are distinctive enough
  for regex detection.
- **Template decay** — templates unseen for N days move to a
  `beacon_templates_archive` table to keep the hot table small.
- **Server-side pre-filter option** — for very constrained clients, add
  an `?exclude_category=` param to `/api/messages` (spec already
  reserves it; not implemented in Milestone 1).
- **Language detection** — a lightweight language tag (`lang=de|en`) in
  `tags`, useful for per-language scoring and greeting rules.
- **Small classifier** — once we have weeks of labeled data, train a
  tiny linear / logistic classifier (bag-of-words + features from
  Layer 1/2/3) and have it contribute an additional `ml_*` tag without
  disturbing the deterministic `category`.
- **Config-driven thresholds** — expose `AUTO_BEACON_THRESHOLD` and
  `AUTO_BEACON_WINDOW_SEC` via `config.py` / environment.
- **Audit trail** — record rule-mutation history for accountability.

---

## 15. Defaults that were chosen for this milestone

These are sensible first-pass defaults, not edicts. Change any of them
if real data shows they are wrong.

| Decision | Default | Where to tune |
|---|---|---|
| Multi-tag storage | JSON array in `messages.tags` TEXT column | schema revision if query load grows |
| When classification runs | at `store_message()` + on-demand reclassify endpoint | — |
| Auto-beacon threshold | 5 messages / same src / same template / 24 h | `classifier/template.py` constants |
| Default-hidden category (FE pref) | `timestamp_beacon` only | webapp `useUserSettingsStore` defaults |
| Backfill on first migration | automatic, in background, progress via SSE | `auto_backfill_on_start` config flag |

---

## 16. Deliverables checklist

- [ ] Schema migration with new columns on `messages` + two new tables
- [ ] `classifier/rules.py` — load, compile, match
- [ ] `classifier/seed.py` — default `builtin=1` rules
- [ ] `classifier/template.py` — normalize, hash, stats, threshold
- [ ] `classifier/score.py` — info score
- [ ] `classifier/classify.py` — orchestrator
- [ ] `storage.store_message()` — calls classifier pre-INSERT
- [ ] REST endpoints: rules CRUD + dry-run
- [ ] REST endpoints: templates list / preview / action
- [ ] REST endpoints: reclassify + status
- [ ] SSE events: rules, stats, template_event, reclassify_progress
- [ ] Periodic `proxy:classifier_stats` task in `main.py`
- [ ] Auto-backfill on startup
- [ ] Tests covering rules, template, score, API
- [ ] Documentation in `CLAUDE.md` under a new "Classifier" section
