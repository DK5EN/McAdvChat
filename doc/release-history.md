# Release History

## Unreleased

### Backend (MCProxy)

- **[fix]** Piped installs (`curl | sudo bash`) now pin bootstrap libs, templates, and the app to one resolved release tag for the whole run, instead of always pulling libs from the `development` branch tip regardless of the app version being installed. `--tag` is now a real time machine for libs+templates+app (previously app-only); a new `--ref`/`MCAPP_BOOTSTRAP_REF` forces just the bootstrap tree ref, independently of the app version, for developing bootstrap changes without cutting a release and as a one-line field rollback. A skew guard aborts cleanly if a pinned tag's libs predate a function the running script needs, instead of installing a mismatched pair. See `doc/2026-08-09_1600-bootstrap-tag-pinning-plan.md`.

## v1.6.13 (2026-06-20)

Maintenance release: reduces journal log noise and rolls up dependency updates. No functional changes.

### Backend (MCProxy)

- **[perf]** High-frequency INFO log lines for UDP telemetry, ACK receipt, and UDP send are demoted to DEBUG. All three are confirmed to land in the database (`telemetry` table, `messages.send_success`, and echo-back ingest respectively), so logging them at INFO produced constant journald noise with no diagnostic value. Error and warning paths are untouched.
- **[chore]** `uv lock --upgrade` dependency sweeps.

### Frontend (webapp)

- **[chore]** `npm update` — minor and patch dependency bumps (vue, vue-tsc, vite-plugin-vue, typescript-eslint, transitive patches).
