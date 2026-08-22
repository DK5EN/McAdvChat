---
name: prod-release
description: Promote a soaked dev pre-release to a McApp production release and roll it onto mcapp.local — pre-flight both repos, write and commit the release notes, publish with scripts/release.sh 2, deploy from the webapp Update page, verify. Use for "cut a production release", "prod release", "release vX.Y.Z", "promote to production", "ship v2.0.1 to prod", "release.sh 2".
---

# Production release → mcapp.local

A production release is a **promotion, not a build**. The code has already been cut as
`vX.Y.Z-dev.N`, deployed by `/dev-release`, and soaked on the box. This skill turns that exact tree
into `vX.Y.Z`, merges it to `main` in **both** repos, publishes a stable GitHub release, and rolls
it onto the Pi.

`scripts/release.sh 2` does the mechanical part. What it does **not** do is the reason this skill
exists: it never gates the tree, it aborts on two pre-conditions you have to arrange yourself, and
it leaves one push undone that silently breaks the _next_ release.

## The one-screen version

```bash
# 0. is the thing you are promoting actually healthy in the field?
/ai-ops                                            # then read doc/ops-mcapp-health-log.md

# 1. update dependencies in BOTH repos — mandatory, not opportunistic
uv lock --upgrade && uv sync --all-packages        # + the standalone ble_service lock
cd ../webapp && npm update

# 2. re-sync the shared code with ../mc-chat, and check the corpora
diff -rq src/mcapp/contract    ../mc-chat/contract
diff -rq src/mcapp/classifier  ../mc-chat/meshcom_mock/classifier

# 3. gate BOTH repos — release.sh gates nothing, and step 1 invalidated your last run
uvx ruff check && uvx ruff format --check . \
  && uv run mypy src/mcapp ble_service/src \
  && uv run python scripts/run_startup_tests.py    # exit 0
cd ../webapp && npm run typecheck && npm run lint && npm run format:check && npm test

# 4. pre-conditions (see Stops 1 and 2), then commit the dependency bumps
for d in . ../webapp; do git -C $d rev-list --count development..origin/main; done   # both 0
$EDITOR doc/release-history.md                     # new section at the TOP
git add doc/release-history.md
git commit -m "[docs] Add release notes for vX.Y.Z" && git push origin development

# 5. publish — newline on stdin, output to a file, read the exit code
printf '\n' | ./scripts/release.sh 2 > /tmp/release.log 2>&1; echo "EXIT=$?"

# 6. deploy: webapp → version chip (bottom right) → Update to vX.Y.Z → Start Update
# 7. verify against the ACTIVE slot, not the banner
```

## Step 0 — Decide whether it is fit to promote

Run `/ai-ops` and read its verdict. A production cut needs more than a green suite:

- the dev tag has **soaked on the box** — hours, not minutes, with `NRestarts` still 0
- zero journal warnings over the soak window
- CI green on the exact commit you are promoting (`gh run list --branch development --limit 3`)
- the working trees are the tag: `git rev-parse HEAD` == `git rev-parse vX.Y.Z-dev.N` in both repos

Append the sweep to `doc/ops-mcapp-health-log.md` **before** releasing. It is the record of what the
release was signed off against, and it is worthless written afterwards from memory.

## Step 1 — Update dependencies, in both repos

**Every production release ships current dependencies.** This is a release step, not an
opportunistic chore: a patch release is the moment the whole fleet takes the new tree, so it is
the moment the dependency set should be current. Doing it any later means the next security bump
waits for the release after.

```bash
# MCProxy (workspace root — covers mcapp and the ble_service member)
uv lock --upgrade
uv sync --all-packages

# webapp
cd ../webapp && npm update
```

`npm outdated` reports **direct** dependencies only. `npm update` is what refreshes the
transitive pins in `package-lock.json`, which is what actually ships.

### The `ble_service` standalone lock does not update itself

`ble_service` is a `[tool.uv.workspace]` member **and** ships its own `ble_service/uv.lock`, used
when `mcapp-ble.service` is deployed independently. Running `uv lock --upgrade` _inside_
`ble_service/` walks up, finds the root workspace, and updates the **root** lock instead — while
its dry-run output misleadingly reports the bump you wanted. Regenerate it outside the workspace:

```bash
tmp=$(mktemp -d)
cp ble_service/pyproject.toml ble_service/uv.lock "$tmp"/
uv lock --upgrade --directory "$tmp"
uv sync --directory "$tmp" --no-install-project     # prove it installs
cp "$tmp"/uv.lock ble_service/uv.lock
```

Dependabot edits `ble_service/pyproject.toml` and never regenerates this lock, so its recorded
specifiers drift behind the pyproject silently. Diff the `specifier =` lines, not just the
versions.

### Then re-run the whole gate

A dependency bump is a change like any other. **The tree you release is not the tree you tested
until you have re-tested it** — so Step 3 runs _after_ this, never before. If `uv.lock` or
`package-lock.json` moved, the previous green run is void.

Commit the bumps in each repo independently, before the release notes.

## Step 2 — Re-sync the shared code with `../mc-chat`

Two directories in this repo are `git subtree`s from mc-chat, and four vector corpora are
hand-copied between three repos with nothing to sync them for you. A release is the checkpoint
where they must agree.

```bash
diff -rq src/mcapp/contract    ../mc-chat/contract
diff -rq src/mcapp/classifier  ../mc-chat/meshcom_mock/classifier
```

Silence is the pass. Any output is drift, and **which direction it points decides what to do**:

- **mc-chat is ahead** → the normal case. Split and pull:

  ```bash
  git -C ../mc-chat subtree split --prefix=<upstream prefix> -b <split branch>
  git subtree pull --prefix=<path> mc-chat <split branch> --squash
  ```

  | Path                    | Upstream prefix           | Split branch       |
  | ----------------------- | ------------------------- | ------------------ |
  | `src/mcapp/classifier/` | `meshcom_mock/classifier` | `classifier`       |
  | `src/mcapp/contract/`   | `contract`                | `contract-subtree` |

- **MCProxy is ahead** → someone edited a vendored subtree in place, which is the thing the
  subtree rules forbid. Do **not** "fix" it by pulling: the pull would revert the local
  improvement. Port the change into mc-chat, verify it _there_ (mc-chat is where the other branch
  of any host-repo conditional actually executes), commit, then split and pull it back. This has
  happened: `classifier/tests.py` carried an MCProxy-only mypy-portability fix for weeks.

### The four hand-copied corpora

`commands/group_dst_vectors.json`, `storage/conversation_key_vectors.json`,
`blocklist_decision_vectors.json` and `commands/hashtag_dst_vectors.json` are canonical **here**
and copied by hand to mc-chat (`tests/fixtures/`) and the webapp (`src/**/__tests__/`). They are
not a subtree; no pull will move them.

```bash
for f in group_dst_vectors conversation_key_vectors hashtag_dst_vectors blocklist_decision_vectors; do
  find . ../mc-chat ../webapp -name "$f.json" -not -path "*/node_modules/*" -exec shasum -a 256 {} +
done
```

Equal hashes per corpus, or fix it. If you change one, copy it to both siblings **and** bump the
webapp's `EXPECTED_SHA256`. Both repos carry a `.prettierignore` covering these files, because a
formatter run is enough to break byte equality while leaving the content identical — which is the
hardest version of this to spot.

## Step 3 — Gate both repos yourself

`release.sh` runs `npm run build:strict` and **nothing else**. No ruff, no mypy, no backend suite,
no eslint, no vitest. A red tree releases silently.

`config_migration` reports `SKIPPED — NOT VERIFIED` on macOS (the suite is bash-4-only by design and
macOS ships bash 3.2). That is an instrument gap, not a coverage gap — it runs on every CI push and
can be run for real on the Pi. Do not treat the skip as a failure, and do not treat it as coverage
either: check that CI is green.

## The version is not a parameter

`release.sh` reads the version from `pyproject.toml` and releases `v${that}`. There is **no
override**. The value is whatever the previous production release's `post_release_prep` left behind
(it bumps the patch and commits `[chore] Prep vX.Y.Z for next dev cycle`).

To release a minor or major instead, edit both `pyproject.toml` and `ble_service/pyproject.toml`,
commit and push, **then** release. Changing it mid-run is not possible — the value is read once,
before anything is tagged.

## Stop 1 — Release notes must be committed BEFORE you start the script

This is the ordering trap, and it looks backwards.

`release.sh` _offers_ to handle the notes: it prints a prompt, waits for you to write
`doc/release-history.md`, then `commit_release_notes` commits it. But `validate_repos_clean` runs at
the **top of `main()`**, long before that — so a repo with uncommitted notes never reaches the
prompt. It aborts with "MCProxy has uncommitted changes".

So the working order is: **write the notes, commit them, push, then run the script.**
`commit_release_notes` then finds nothing to do and logs
`release-history.md unchanged (already committed)`. That is the success path, not a warning.

### What the notes have to look like

`upload_production` publishes with `--notes-file doc/release-history.md` — **the whole file**, not a
section. So:

- the new section goes at the **top**, directly under `# Release History`
- the file has to read well from the top down, because that is the GitHub release body
- follow the existing shape: `## vX.Y.Z (YYYY-MM-DD)`, a lead paragraph, `### Highlights`,
  `### Backend (MCProxy)`, `### Frontend (webapp)`, `### Upgrade notes`

Get the material from the commit range in both repos:

```bash
git log v<prev>..HEAD --oneline --no-merges
git -C ../webapp log v<prev>..HEAD --oneline --no-merges
```

Write what changed **for the operator**, not a commit transcript. If the release shipped a metric or
a measurement, put the field numbers in — the release notes are where a future reader finds out that
99 % availability means one lost frame, not a degraded link.

Run `npx --yes prettier@3 --write doc/release-history.md`, then `uvx ruff format --check .` — in
that order. A docs-only commit has turned CI red here twice, because `ruff format` also formats
fenced `python` blocks inside `.md`.

## Stop 2 — `main` must not be ahead of `development`, in EITHER repo

`validate_main_mergeable` fetches `origin/main` in both repos and aborts if
`development..origin/main` is non-empty:

```
[ERROR] webapp: main has 1 commit(s) not in development (diverged)
[ERROR] Resolve this manually before releasing.
```

**The webapp is the repo that used to drift**, and it drifted because `post_release_prep` never
pushed it. That root cause is fixed (see Stop 4), so this check should now always pass — which is
exactly why it is still worth running: a non-zero count means something outside the release flow
touched `main`. Check both before you start:

```bash
for d in . ../webapp; do
  git -C $d fetch origin --quiet
  echo "$d behind: $(git -C $d rev-list --count development..origin/main)"   # must be 0
done
```

The fix is the same merge-back `release.sh` performs after every production release, so it is safe
and non-destructive — never rewrite a pushed `main`:

```bash
git -C ../webapp merge main --no-ff -m "[chore] Merge main back into development after <what>"
git -C ../webapp push origin development
```

If the trees are identical (`git diff --stat <dev> <main>` is empty) the merge is purely
topological, which is the usual case — a merge commit on `main` that was never merged back.

## Stop 3 — The script blocks on a bare `read`, and `< /dev/null` kills it

`./scripts/release.sh 2` takes the mode as `argv[1]` (`1` = dev, `2` = production), so the menu is
skipped. But the production path still calls `wait_for_release_notes`, which is a bare `read -r`.

Under `set -euo pipefail` a `read` at EOF returns 1 → the script dies → the **rollback trap fires**.
So `< /dev/null` does not make it non-interactive, it makes it fail. Feed it a newline:

```bash
printf '\n' | ./scripts/release.sh 2 > /tmp/release.log 2>&1; echo "EXIT=$?"
```

**Never pipe the script through `tail`/`head`** — the pipeline reports _tail's_ exit code, so a
rolled-back release reads as a clean one. Redirect to a file, echo `$?`, then read the file.

## What the script does, in order

Production path, after `validate_tools` / `validate_on_development` / `validate_repos_clean`:

1. resolve `v${pyproject version}`; refuse if that tag already exists in either repo
2. `validate_main_mergeable` (Stop 2)
3. print the notes prompt, wait for Enter (Stop 3), commit the notes if dirty (Stop 1)
4. merge `development` → `main` in **both** repos
5. `npm run build:strict` — writes `version.html` with the new tag
6. build the combined tarball (backend + webapp)
7. annotated tag `vX.Y.Z` in both repos
8. push `main` + tag in both repos — **before** the release exists, so `gh` cannot invent its own
9. sha256, `gh release create --verify-tag` (stable, not pre-release), upload both assets
10. back to `development`, merge `main` back in **both** repos
11. `post_release_prep`: bump both `pyproject.toml`s and the webapp's `package.json` /
    `package-lock.json` (via `npm version --no-git-tag-version`), commit in each, and push
    **both** repos' `development`

On any failure the `EXIT` trap rolls everything back: local + remote tags in both repos, the GitHub
release, the tarball and checksum, and the branch checkout. A failed run leaves nothing behind, so
diagnose and re-run rather than cleaning up by hand.

## Stop 4 — The push `release.sh` used to forget (fixed — verify it)

**Historical, and the reason Stop 2 exists.** `post_release_prep` pushed **MCProxy**
`development` only. The webapp's merge-back from step 10 was committed and never pushed:

```
## development...origin/development [ahead 2]
```

That left webapp `origin/main` ahead of `origin/development` — precisely the diverged state that
aborts the **next** release at Stop 2. The failure therefore surfaced one release later, with an
error naming the webapp rather than the release that caused it. It is what blocked the v2.0.1 cut.

`post_release_prep` now bumps and pushes both repos, and `scripts/release_prep_tests.py` pins it —
asserting against the **remotes**, because the old code committed in the webapp and simply never
pushed, so a working-tree assertion would have passed while the bug was live.

Still confirm it after every release, because this is cheap and its absence is expensive:

```bash
for d in . ../webapp; do
  git -C $d fetch origin --quiet
  echo "$d unpushed: $(git -C $d rev-list --count origin/development..development)"   # both 0
done
```

If either is non-zero, push it and work out why the script did not — do not just paper over it.

## Step 6 — Deploy to the Pi from the webapp Update page

**Not `curl | sudo bash`.** That line is classifier-blocked, GitHub rate-limits the Pi at 429, and an
unresolved version "deploys" the old slot green. The self-converging update path is the supported
one for a production tag.

1. open `https://mcapp.local/` in Chrome
2. click the **version chip** in the bottom-right status bar → `/webapp/update`
3. **Version Status** should read `LATEST RELEASE vX.Y.Z — Update available`. If it still shows the
   old release, GitHub's API has not caught up; wait, do not re-cut.
4. **Update to vX.Y.Z** → confirm dialog (`Mode: Production`) → **Start Update**
5. the modal streams `PREPARE → DEPLOY → ACTIVATE → HEALTH → DONE` over SSE

The deploy takes several minutes on a Pi Zero 2W. **Do not poll it turn by turn.** Arm one
background wait with a terminal condition and do something else until it fires:

```bash
until [ "$(curl -sk --max-time 5 https://mcapp.local/api/status \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["version"])' 2>/dev/null)" = "vX.Y.Z" ]; \
  do sleep 15; done; echo DONE
```

Auto-rollback is armed: if the health checks fail the previous slot stays active and the service
keeps running the old code.

## Step 7 — Verify

The modal's DONE is not verification. Check the box:

```bash
curl -sk https://mcapp.local/api/status | python3 -m json.tool     # version == vX.Y.Z
curl -sk https://mcapp.local/webapp/version.html                   # NOT /version.html — that 404s
ssh mcapp.local '
  readlink -f ~/mcapp-slots/current
  systemctl is-active mcapp mcapp-ble caddy lighttpd
  systemctl show mcapp -p NRestarts --value          # still 0
  grep -h "^LATEST_SCHEMA_VERSION" ~/mcapp-slots/current/src/mcapp/storage/constants.py'
```

- `version.html` reports what was **deployed**, not what the browser is running. Reload past the
  service worker (the "Update available — Reload" banner) before believing a frontend change is live.
- Slot order is not fixed: `get_target_slot` takes the empty-or-oldest of `slot-{0,1,2}`.
- If the release changed the schema, confirm the migration ran: the DB's `schema_version` must equal
  `LATEST_SCHEMA_VERSION` in the **active slot**. A mismatch is the loudest finding there is.

Then run `/ai-ops` once the box has settled and append the post-release sweep to the health log.

## Rollback

The update page has a **Rollback** button, and the previous release is still in its slot — so
rolling back is a slot switch, not a re-deploy. Use it before considering a hotfix release.

Never delete or move a published production tag to "fix" a release. Cut `vX.Y.Z+1` instead; the
version is already bumped and waiting in `pyproject.toml`.

## Related

- `/dev-release` — cutting and installing the `vX.Y.Z-dev.N` that this skill promotes
- `/ai-ops` — the health check that decides whether promotion is warranted, and its log
- `doc/version-logic.md` — how dev/production versions and tags resolve
- `doc/release-history.md` — the notes file, published verbatim as the GitHub release body
- `bootstrap/README.md` — installer flags, for the cases the Update page cannot cover
