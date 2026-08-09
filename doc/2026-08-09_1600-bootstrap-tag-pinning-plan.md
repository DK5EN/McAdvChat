# Pin piped-install bootstrap libs per tag — implementation plan

Status: **signed off 2026-08-09** (decisions D1-D5 in §4), not started
Date: 2026-08-09
Scope: `bootstrap/mcapp.sh`, `bootstrap/lib/{packages,deploy,detect}.sh`, one new startup test suite, docs
Ships as: its own release (no other feature riding along), VM smoke matrix before the Pi

---

## 1. The bug

`bootstrap/mcapp.sh:45-46` hardcodes the lib source:

```bash
readonly GITHUB_REPO_BRANCH_DEFAULT="development"
GITHUB_RAW_BASE="https://raw.githubusercontent.com/DK5EN/McApp/${GITHUB_REPO_BRANCH_DEFAULT}"
```

In piped mode (`curl … | sudo bash`, `PIPED_MODE=true`) `source_libs()` calls `download_libs()`
(`mcapp.sh:216-234`), which pulls all six `lib/*.sh` from that branch tip — **regardless of which
branch the script itself came from, and regardless of which release is about to be installed**.

Three consequences:

| Path                                 | Script from | Libs from         | Templates from    | App from       |
| ------------------------------------ | ----------- | ----------------- | ----------------- | -------------- |
| `curl …/main/…mcapp.sh \| sudo bash` | `main`      | `development` tip | `development` tip | latest release |
| `… \| sudo bash -s -- --dev`         | `main`      | `development` tip | `development` tip | latest prerel. |
| `… \| sudo bash -s -- --tag v1.6.13` | `main`      | `development` tip | `development` tip | `v1.6.13`      |

1. **Stable fresh installs run current development lib code.** A push to `development` changes what
   every "stable" fresh install does, instantly, with no release cut. Installs are time-dependent,
   not version-pinned.
2. **`--tag` is not a time machine.** It pins only the app tarball. This is what burned us on
   Saturday: the "v1.6.13 baseline" attempt ran June's script with August's libs, which installed
   Caddy into a supposedly-old install and destroyed the baseline. `doc/update-converge.md:95-105`
   already carries the workaround ("do NOT use piped mode for this step") — that note exists because
   of this bug and gets deleted by this fix.
3. **Templates have the same flaw.** `configure_lighttpd()` (`packages.sh:253`) and
   `configure_caddy()` (`packages.sh:558`) fall back to `${GITHUB_RAW_BASE}/bootstrap/templates/…`,
   and `download_webapp()` (`deploy.sh:572-573`) fetches the legacy webapp archive from the same
   branch tip.

Not affected: non-piped runs (`SCRIPT_DIR` set → local `lib/`, local `templates/`), which is why
the deployed-slot update path via `scripts/update-runner.py` has been consistent all along.

## 2. Goal

For a single installer invocation, **script, libs, templates and app all come from one release
tag**. A piped install is reproducible: the same command on the same tag produces the same box
next month.

Non-goals for this release: signing/verifying beyond what GitHub already gives us; changing the
release process; touching `ssl-tunnel-setup.sh` or `sd-card.sh`.

## 3. Design

### 3.1 Resolve the ref before the libs are sourced

The tag is currently resolved inside `deploy.sh` (`resolve_target_version()`, `deploy.sh:148-158`),
which is only available **after** `source_libs()`. So the resolver must be duplicated, minimally,
in `mcapp.sh` itself, before `source_libs()`:

```
resolve_install_ref()        # new, in mcapp.sh, ~25 lines
  --tag TAG   → TAG                      (no API call at all)
  --dev       → latest prerelease tag     (GET /releases?per_page=100)
  default     → latest release tag        (GET /releases/latest)
  API failure → "" (caller falls back, see 3.4)
```

Two constraints on this function:

- **No `jq`.** `jq` is installed by `install_packages()` (`packages.sh:126`), which runs much later;
  a fresh Pi OS Lite has none. Parse `tag_name` with `grep -o`/`sed`, and validate the result
  against `^v[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9]+)?$` before it is ever interpolated into a URL.
- **Resolve once.** Export the result (`MCAPP_INSTALL_REF`) and thread it into `deploy_app()` so the
  app deploy does not make a second, independently-resolved API call. Without this, a release cut
  between the two calls gives you libs from tag A and app from tag B — a narrower version of the
  same bug. `deploy_release()` already accepts a pre-resolved `remote_version` as `$5`
  (`deploy.sh:352-363`); `deploy_app()` (`deploy.sh:220`) needs the same parameter added.

### 3.2 Fetch the whole `bootstrap/` tree, not six files

Replace `download_libs()` with `fetch_bootstrap_tree <ref>`, which downloads **one** tarball,
extracts it, and returns the path to its `bootstrap/` directory:

1. Primary: the release asset `mcapp-<ref>.tar.gz` + `mcapp-<ref>.tar.gz.sha256` from
   `https://github.com/DK5EN/McApp/releases/download/<ref>/`. `scripts/release.sh:515-517` copies
   the entire `bootstrap/` directory into that tarball, and unlike raw/codeload URLs it ships a
   **published checksum** — same artifact, same verification as the app deploy already does
   (`deploy.sh:432-442`).
2. Fallback: `https://codeload.github.com/DK5EN/McApp/tar.gz/refs/tags/<ref>` (no checksum, warn) —
   covers a tag without assets. This is literally the command the runbook tells humans to run today.

Why a tarball instead of per-file raw URLs at the pinned ref:

- The hardcoded six-file list in a **new** script would break against an **old** tag that has a
  different set of libs. A tarball carries whatever that release actually had.
- Templates come along for free — see 3.3.
- One request instead of six, on a Pi Zero's WiFi.

### 3.3 Point `SCRIPT_DIR` at the extracted tree

`SCRIPT_DIR` and `PIPED_MODE` are `readonly` at `mcapp.sh:37-43`. Drop `readonly` from `SCRIPT_DIR`
only (keep `PIPED_MODE` readonly — it still means "was piped" and is only read for lib lookup and
log lines), and after `fetch_bootstrap_tree` set `SCRIPT_DIR="$tree"`.

This is the lever that fixes templates with no per-consumer change: `pick_caddy_template()`
(`packages.sh:443-445`) and the lighttpd template lookup (`packages.sh:245-246`) already prefer
`${SCRIPT_DIR}/templates/…` and only fall back to a raw URL when it is empty. In piped mode it has
always been empty; after this change it is the pinned tree, so both fallbacks go cold.

Keep the raw-URL fallbacks, but rebase `GITHUB_RAW_BASE` on the resolved ref
(`…/McApp/<ref>` instead of `…/McApp/development`) so that even the cold path is pinned. Same for
`download_webapp()` (`deploy.sh:572-573`) and `get_remote_webapp_version()`'s fallback
(`detect.sh:312`).

Cleanup still applies: the existing `trap "rm -rf '$lib_dir'" EXIT` in `source_libs()`
(`mcapp.sh:192`) becomes a trap on the extracted tree's parent temp dir — and must be installed by
the **caller**, not inside the `$( )` subshell (the comment at `mcapp.sh:212-215` explains why;
keep it).

### 3.4 When the API cannot be reached

Unauthenticated `api.github.com` is 60 requests/hour/IP, and a Pi behind a flaky link may get
nothing at all. Today piped mode works without the API (branch tip is unconditional); after the fix
it needs a ref. Proposed behavior:

| Situation                                                  | Behavior                                                                       |
| ---------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `--tag` given                                              | No API call. Pin to it. Fail hard if that ref has no fetchable bootstrap tree. |
| API resolves a tag                                         | Pin to it.                                                                     |
| API fails, no existing install                             | **Abort** with the reason and the copy-paste `--tag` form.                     |
| API fails, existing install (repair/`--skip`/`--converge`) | **Warn loudly**, fall back to `main` (`development` under `--dev`), continue.  |

Rationale for the split: a fresh install that cannot name its version has nothing to salvage and
should not silently become "whatever is on a branch today" — that is the bug. A repair run on a
live box is the one case where finishing beats being pinned, and it is exactly the case where the
operator is watching the output. Signed off as D2.

### 3.5 Script/lib API-skew guard

New script + old tag's libs is now a reachable combination (`--tag v1.5.1`, or the minute-wide
window inside a release run, see §6). Old libs simply lack functions the current `main()` calls
(`ensure_web_frontend`, `caddy_config_marker`, `write_slot_meta`, …).

After `source_libs()`, verify with `declare -F` that every function `main()` dispatches to exists.
On a miss: abort with the exact command that gets a consistent pair —

```
ERROR: v1.5.1's bootstrap libs do not provide: ensure_web_frontend caddy_config_marker
       Run that release's own installer instead:
       curl -fsSL https://raw.githubusercontent.com/DK5EN/McApp/v1.5.1/bootstrap/mcapp.sh \
         | sudo bash -s -- --tag v1.5.1
```

This is a strict improvement over today, where the mismatch is silent and lands as a Caddy install
into a June box.

### 3.6 Escape hatches

- `--ref REF` (new): force the bootstrap tree ref (branch or tag) independently of the app version.
  For developing bootstrap changes without cutting a release; `--ref development` reproduces
  today's behavior exactly, which also makes it the one-line rollback for a field problem.
- `MCAPP_BOOTSTRAP_REF` env var: same thing, for the update-runner and for scripted use.

### 3.7 Key the tree fetch on "no local libs", not on `PIPED_MODE`

`source_libs()` only downloads when `PIPED_MODE=true` (`mcapp.sh:189`). That leaves one path dead
today: `update-runner.py`'s `_download_bootstrap()` fallback (`update-runner.py:844-853`, reached
when the active slot has no `bootstrap/mcapp.sh` — a broken or freshly-migrated slot) writes the
script to a temp file and runs `bash /tmp/xxx.sh --skip`. `BASH_SOURCE` is then set, so
`PIPED_MODE=false` and `SCRIPT_DIR=/tmp`; there is no `/tmp/lib`, nothing writes `SHARE_DIR`, and
the download branch is skipped — the run dies on "Cannot find library files" before doing anything.
The recovery path that exists precisely for a broken slot cannot work.

Making the final branch of `source_libs()` "no local libs found → fetch the pinned tree" instead of
"piped → fetch from a branch" fixes that for free and pins it at the same time. Costs one condition;
add smoke case #10.

## 4. Decisions (signed off 2026-08-09)

| #      | Decision                                                                                                                                            | Resolution                                                                                                                                                                           |
| ------ | --------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **D1** | How to fetch the pinned tree.                                                                                                                       | **Checksummed release-asset tarball**, codeload fallback. Also fixes templates and old-tag lib sets.                                                                                 |
| **D2** | Behavior when the API is unreachable.                                                                                                               | **The split** (§3.4): abort on a fresh install, warn + fall back to `main`/`development` on an existing one.                                                                         |
| **D3** | Does `--tag` also pin the **script**?                                                                                                               | **Not in this release.** Phase 1 pins libs + templates + app; §3.5's guard makes a script/lib mismatch loud. Re-exec of the tag's own `mcapp.sh` is Phase 3, with its own smoke run. |
| **D4** | Reuse the fetched tarball for the app deploy (one download, provably the same artifact).                                                            | **No** this release — it grows the diff into `download_and_install_release()`. Revisit with Phase 3.                                                                                 |
| **D5** | Legacy `/usr/local/share/mcapp/lib` branch in `source_libs()` (`mcapp.sh:186-187`), preferred over the pinned tree, written by nothing in the repo. | **Delete it.** It can only shadow pinned libs with something ancient. Smoke case #9 confirms a checkout run is unaffected.                                                           |

## 5. Work breakdown

**Phase 1 — pinning (the fix)**

1. `mcapp.sh`: add `resolve_install_ref()`, `fetch_bootstrap_tree()`; delete `download_libs()`;
   rewrite `source_libs()` (drop the SHARE_DIR branch per D5, key the fetch on "no local libs" per
   §3.7, set `SCRIPT_DIR`, keep the caller-side EXIT trap); make `SCRIPT_DIR` non-readonly; rebase
   `GITHUB_RAW_BASE` on the resolved ref; remove `GITHUB_REPO_BRANCH_DEFAULT`.
2. `mcapp.sh`: `--ref` flag + `MCAPP_BOOTSTRAP_REF`, help text, `SCRIPT_VERSION` → `2.6.0`.
3. `mcapp.sh`: post-source `declare -F` skew guard (3.5).
4. `deploy.sh`: `deploy_app()` accepts and honors a pre-resolved version; `main()` passes
   `MCAPP_INSTALL_REF` through so the API is queried once.
5. `deploy.sh`/`detect.sh`: rebase the two remaining `GITHUB_RAW_BASE` consumers on the ref.
   Correction to an earlier assumption in this plan: **both are already dead**, at every ref. There
   is no `webapp/` directory anywhere in the McApp tree (`git ls-tree` on `development`, `main` and
   `v1.6.14-dev.28` all return nothing) — the built SPA only ever exists inside a release tarball
   and in the webapp repo. So `download_webapp()` (`deploy.sh:569-573`) and
   `get_remote_webapp_version()`'s fallback (`detect.sh:311-313`) fetch URLs that 404 today. They
   are only reached for a release tarball without a bundled `webapp/` (`deploy.sh:480-487`), i.e.
   pre-combined-tarball releases. Pin them anyway — one variable, no behavior change — and file
   deleting `deploy_webapp_download()`/`download_webapp()` as separate cleanup rather than smuggling
   a dead-code removal into an install-behavior release.

**Phase 2 — tests + docs**

6. New `scripts/bootstrap_pinning_tests.py`, registered in `run_startup_tests.py` `main()` (a suite
   not wired in there is gated by nothing). Follows the `caddy_config_tests.py` pattern: drive the
   real bash functions via subprocess against a local HTTP stub, never a copy of their logic.
   Cases:
   - `resolve_install_ref` returns the tag for `--tag`, **without** hitting the API stub (assert
     zero requests);
   - parses `tag_name` from a canned `/releases/latest` body with no `jq` on `PATH`;
   - picks the highest prerelease by `sort -V` from a canned `/releases` body;
   - rejects a malformed/injected tag (`v1.0.0; rm -rf /`, `../../foo`) before URL interpolation;
   - `fetch_bootstrap_tree` verifies the sha256 and refuses a corrupted tarball;
   - falls back to codeload when the asset 404s, and warns;
   - **regression tripwire**: no file under `bootstrap/` interpolates a hardcoded
     `development`/`main` into a `raw.githubusercontent.com`/`codeload` URL (grep-level assert —
     this is the invariant that would silently rot back). Two documented exceptions: the
     `mcapp-update` / `mcapp-dev-update` aliases (`deploy.sh:926-927`) and the usage comment at
     `mcapp.sh:7` — those name the branch the _script_ comes from, which is correct by design;
   - the skew guard aborts when a required function is missing;
   - a script run from a temp file with no sibling `lib/` fetches the pinned tree instead of dying
     on "Cannot find library files" (§3.7 — the update-runner recovery path).
7. `doc/update-converge.md`: replace the "do NOT use piped mode" workaround (lines ~95-105) with the
   pinned one-liner.
8. `bootstrap/README.md` + `doc/version-logic.md`: document that a piped install is pinned to the
   resolved release, and what `--ref` is for.
9. `doc/release-history.md` entry.

**Phase 3 — optional, separate release (D3)**

10. Re-exec the pinned tree's own `mcapp.sh`, loop-guarded, so `--tag` becomes a true time machine.

Est. ~150 lines of bash changed in `mcapp.sh`, ~10 elsewhere, ~250 lines of new test.

## 6. Verification

Gate (all must be clean before commit): `uvx ruff check`, `uvx ruff format --check .`,
`uv run mypy src/mcapp ble_service/src`, `uv run python scripts/run_startup_tests.py`.
Plus `shellcheck bootstrap/mcapp.sh bootstrap/lib/*.sh` — this diff is entirely shell.

**Release sequencing — no action needed.** An earlier draft of this plan wanted the tag cut before
the merge to `main`. That was wrong about how releases are made here: `./scripts/release.sh 2` does
merge → build → tag → push → publish inside one run (`release.sh:688-711`), so the merge is not a
separate event to sequence around. The only exposure is the gap between step 7 (`push_main_and_tags`
makes the new script visible on `raw.githubusercontent.com/.../main`) and step 9
(`upload_production` publishes the release the API resolves to) — roughly one tarball upload wide.
A piped install landing in that gap gets the new script and pins to the **previous** tag's libs,
which is exactly the case §3.5's guard is for, and those libs are one release old, not two months.
Nothing to change in the release procedure.

Free early signal: dev pre-releases (`release.sh 1`) tag `development` without touching `main`, so
every `--dev` install after this ships exercises the pinned path — including the resolve-latest-
prerelease branch — before the production release goes out.

**VM smoke matrix** (OrbStack Debian trixie containers, as used for the epoch rollout):

| #   | Command                                             | Expect                                                                   |
| --- | --------------------------------------------------- | ------------------------------------------------------------------------ |
| 1   | piped, fresh, no flags                              | logs the resolved tag; libs+templates from it; healthy install           |
| 2   | piped, fresh, `--dev`                               | resolves latest prerelease; same tag everywhere                          |
| 3   | piped, fresh, `--tag v1.6.13`                       | **no** Caddy in the resulting box (this is the Saturday repro, inverted) |
| 4   | piped re-run on #1's box (idempotence)              | no-op, no slot rotation                                                  |
| 5   | piped `--skip` and `--converge` on an existing box  | unchanged behavior                                                       |
| 6   | API blocked (nftables-drop `api.github.com`), fresh | aborts with the `--tag` hint                                             |
| 7   | API blocked, existing install                       | warns, falls back to `main`, completes                                   |
| 8   | corrupted asset (local stub serving a bad sha256)   | refuses, exits non-zero, box untouched                                   |
| 9   | non-piped run from a checkout                       | unchanged (local libs, local templates, no network for libs)             |
| 10  | `bash /tmp/mcapp.sh --skip` (no sibling `lib/`)     | fetches the pinned tree instead of "Cannot find library files" (§3.7)    |
| 11  | `--ref development` on a fresh box                  | reproduces today's branch-tip behavior — the field rollback works        |

**Pi run:** #1 and #4 on a spare card, then `mcapp.local` update path end-to-end
(`update-runner` → deploy → `--converge`) to confirm nothing in the update flow regressed.

## 7. Risks and rollback

| Risk                                                                                              | Mitigation                                                                                              |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| A bad script on `main` breaks **every** fresh install (this file is the front door for the field) | Smoke matrix before merge; `--ref development` is a one-line field workaround; `main` revert is instant |
| Pinned tree fetch fails where six raw fetches succeeded (proxy, size, tar)                        | Codeload fallback; keep the per-file raw fallbacks for templates; #6-#8 cover the failure paths         |
| Old tag's libs sourced into new script                                                            | 3.5 skew guard, D3 in a later release                                                                   |
| Extra API call on a rate-limited IP                                                               | Net zero: the call resolved in `mcapp.sh` replaces (not adds to) `deploy_app()`'s                       |
| Release-asset tarball not present for very old tags                                               | Codeload fallback, warns about the missing checksum                                                     |

Rollback: revert the commit on `main`; installs return to branch-tip libs. No on-box state changes,
no migration, nothing to undo on already-installed machines.
