---
name: dev-release
description: Cut a McApp dev pre-release and install it on mcapp.local, end to end — gate both repos, commit and push, publish with scripts/release.sh, then curl-bash the box and verify the change is actually live. Use for "cut a dev release", "release and deploy", "release.sh", "get this onto the Pi", "deploy to mcapp.local", "ship it".
---

# Dev release → mcapp.local

Two repos, one release. `scripts/release.sh` tags **MCProxy + webapp** together, builds one
combined tarball, and publishes it as a GitHub pre-release. `bootstrap/mcapp.sh --dev` then pulls
that pre-release onto the Pi into a fresh slot.

`mcapp.local` (Pi Zero 2W) is **the** production target and the only host running MCProxy. There is
no staging box — a dev release goes straight onto the box the operator uses. The slot system is the
safety net: a failed deploy leaves the previous slot active and the service untouched.

## The one-screen version

```bash
# 0. both repos on `development`, tags fresh
git fetch --tags && git -C ../webapp fetch --tags

# 1. gate BOTH repos yourself — release.sh does not do this for you
uvx ruff check && uvx ruff format --check . \
  && uv run mypy src/mcapp ble_service/src \
  && uv run python scripts/run_startup_tests.py          # exit 0 = all suites pass
cd ../webapp && npm run typecheck && npm run lint && npm run format:check && npm test

# 2. commit + push BOTH repos (explicit paths, never `git add -A`)
git add <paths> && git commit && git push origin development     # in each repo

# 3. publish (non-interactive: 1 = dev, 2 = production)
./scripts/release.sh 1

# 4. let the asset settle, then verify it is fetchable FROM THE PI (see the curl 56 trap)
ssh mcapp.local 'curl -fsSL -o /tmp/t.tar.gz <asset-url> && sha256sum /tmp/t.tar.gz; rm -f /tmp/t.tar.gz'

# 5. install — log on the Pi, never pipe the ssh through `tail`
ssh mcapp.local 'curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/development/bootstrap/mcapp.sh \
  | sudo bash -s -- --dev > /tmp/deploy.log 2>&1; echo "EXIT=$?"; tail -45 /tmp/deploy.log'

# 6. verify the change is in the ACTIVE slot, not just on disk
```

## Step 1 — Gate both repos

`release.sh` runs `npm run build:strict` (vue-tsc + vite build) and **nothing else**. It never runs
the backend suite, never runs ruff or mypy, never runs eslint, prettier, or vitest. A red tree
releases silently. Run the full gate yourself, in both repos, before you commit.

Not verified until lint, typecheck, format-check and the real suite have all run — for dependency
changes too, not just code.

## Step 2 — Commit and push, both repos, push first

Each repo commits independently (`[type] description`). Stage explicit paths.

**Push before releasing.** `gh release create` pushes only the _tag_; it never pushes the branch. A
tag pointing at commits that exist nowhere else on the remote is a mess to unpick later.

**Expect the push to be rejected.** Dependabot opens PRs against this repo and they land on
`development` while you work. Then:

```bash
git fetch origin development
git log --oneline development..origin/development   # look at what landed before you touch anything
git rebase origin/development
```

If the rebase pulled in a **dependency** change (uv.lock, pyproject.toml), re-run the whole gate —
`uv sync --all-packages` first. A dependency bump is a change like any other; the tree you release
is not the tree you tested until you have re-tested it.

## Step 3 — `./scripts/release.sh 1`

`1` = dev pre-release, `2` = production. With an argument the dev path is fully non-interactive
(production still stops to wait for `doc/release-history.md`).

The script refuses to start unless **both** repos are on `development` and **both** working trees
are clean. It needs `git gh shasum tar sed npm jq` on PATH.

The next tag comes from `pyproject.toml`'s version plus the highest **local** `vX.Y.Z-dev.N` tag —
so `git fetch --tags` in both repos first, or a release cut elsewhere gets a number reused.

What it does, in order: build webapp → tag both repos (lightweight) → build combined tarball →
sha256 → `gh release create --prerelease` (which pushes the MCProxy tag) → push the webapp tag →
clean up local artefacts. On any failure a trap rolls back tags, the GitHub release, and artefacts.

## Step 4 — The `curl: (56)` trap

**Do not deploy in the same breath as the release.** GitHub's asset CDN
(`release-assets.githubusercontent.com`) needs a moment after `gh release create`. Deploying
immediately gets:

```
curl: (56) Connection died, tried 5 times before giving up
[ERROR]   Failed to download release tarball
```

This is transient, not a broken release. Confirm the asset is real and fetchable **from the Pi**
before spending ten minutes on a deploy that cannot succeed:

```bash
gh release view <tag> --repo DK5EN/McApp --json assets --jq '.assets[] | "\(.name) \(.size) \(.state)"'
ssh mcapp.local 'curl -fsSL -o /tmp/t.tar.gz \
  https://github.com/DK5EN/McApp/releases/download/<tag>/mcapp-<tag>.tar.gz \
  && sha256sum /tmp/t.tar.gz; rm -f /tmp/t.tar.gz'
```

The sha256 must match the one `release.sh` printed. If curl still dies, wait and retry — do not
start diagnosing the Pi's network until `gh release view` has confirmed both assets are `uploaded`.

## Step 5 — Install on the Pi

```bash
ssh mcapp.local 'curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/development/bootstrap/mcapp.sh \
  | sudo bash -s -- --dev > /tmp/deploy.log 2>&1; echo "EXIT=$?"; tail -45 /tmp/deploy.log'
```

**Never pipe the ssh command through `tail`/`head`.** `ssh ... | tail -80` reports _tail's_ exit
code, so a failed deploy looks like a clean one — and the pipe buffers everything, so you watch a
blank screen for ten minutes. Redirect to a log **on the Pi**, echo `$?` inside the remote shell,
and tail the file afterwards.

Run it with `run_in_background: true`; on a Pi Zero 2W the full run is 5–15 minutes (apt upgrade +
`uv sync --all-packages`). To watch progress, poll `tail -1 /tmp/deploy.log` over a second ssh
rather than trying to stream the first one.

Other flags worth knowing: `--check` (dry run), `--skip` (deploy only, no system setup),
`--converge` (system epoch only, deploy nothing), `--tag vX.Y.Z` (pin an exact release),
`--force`. See `bootstrap/README.md`.

## Step 6 — Verify

A "complete" banner is not verification. Check three things:

```bash
ssh mcapp.local '
  readlink -f ~/mcapp-slots/current                      # which slot is live now
  systemctl is-active mcapp mcapp-ble                    # both active
  systemctl show mcapp -p ActiveEnterTimestamp --value   # restarted just now, not days ago
  grep -c "<a symbol from your change>" ~/mcapp-slots/current/src/mcapp/<file>.py'
```

The last one is the check that actually matters: grep the **active slot** for a symbol only your
change introduces. Everything else can look healthy while the service still runs the old code.

Do not expect a fixed slot order. `get_target_slot` picks the empty-or-oldest of `slot-{0,1,2}`,
and a re-deploy of a version the active slot already holds deploys **in place** with no rotation at
all — so an unchanged `current` symlink is not by itself evidence of failure.

The health-check block in the log should show 14 `[OK]` lines, ending with `webapp version:` at the
new tag and `active slot:` at whichever slot was used.

### Then watch live traffic — this is not optional

A green suite plus a green deploy is still not proof the change is correct. For anything on the
mesh-ingest path, watch real rows land and **read the values**, not just the row count. Beacons
arrive every ~30 min per station, so give it 20–30 minutes.

This has already paid for itself once. A QFE fix deployed clean, all suites green, and the first
live rows showed the pressure arriving correctly — **and** two rows per station one second apart
with different values (962.6 derived vs 966.5 measured), because the new code made a previously
unreachable dedup branch reachable. Nothing in the suite or the health checks could have caught it;
it only existed where two transports of the same beacon met. Look at the data.

## When it fails

A failed deploy is normally **safe**: the tarball lands in the _inactive_ slot, so a download or
extract failure leaves the previous slot symlinked and the service running the old code. Confirm
that rather than assuming it:

```bash
ssh mcapp.local 'readlink -f ~/mcapp-slots/current; systemctl is-active mcapp mcapp-ble'
```

Report the failure plainly and retry — do not re-cut a release to work around a transient download.

## Pi gotchas that cost round-trips

- No `sqlite3` CLI. Write a python3 script, `scp` it, run it, delete it. All DB timestamps are in
  **milliseconds**.
- No `tcpdump`. For wire capture write a `socket.AF_PACKET` sniffer and run it under
  `sudo nohup setsid` — a bare `cmd &` inside an ssh one-liner dies with the session.
- Never open a second consumer on the BLE service's `/api/ble/notifications` SSE stream: the queue
  is single-consumer (`popleft()`), so a debugging client steals frames from production.
- Slots live at `~/mcapp-slots/slot-{0,1,2}`; DB at `/var/lib/mcapp/messages.db`; logs via
  `sudo journalctl -u mcapp.service -f`.

## Related

- `doc/version-logic.md` — how dev/production versions and tags resolve
- `bootstrap/README.md` — installer flags and what the bootstrap actually does
- `doc/operations-reference.md` — deploy, config, health, troubleshooting
- `CLAUDE.md` § System Epoch — bump `SYSTEM_EPOCH` + `REQUIRED_SYSTEM_EPOCH` together when
  system-level state changes; a startup test enforces the parity
