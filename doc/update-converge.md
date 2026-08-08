# Update-Converge Acceptance Test (Fresh Raspberry Pi)

Step-by-step runbook for an agent to reproduce the fleet upgrade path on a
real Raspberry Pi with a fresh image: faithful v1.6.13 install → update via
the real webapp code path → converge watchdog → fully converged box. This is
the same test that passed in an OrbStack arm64/trixie VM on 2026-08-08; a Pi
run confirms it on real hardware (SD card timing, BLE, real mesh).

Design background: `implementation-update-epoch.md` (repo root). Mechanism
docs: `doc/operations-reference.md`, section "System epoch & converge".

## Prerequisites

- Raspberry Pi (Zero 2W or better) flashed with a fresh Raspberry Pi OS
  **Lite** image (arm64, Trixie era), booted, on the LAN, with WAN access.
- SSH reachable (example below uses `pi-test.local`; substitute the real
  hostname or IP) as a user with passwordless sudo.
- The Pi must NOT be `mcapp.local` — never run this test against the
  production box.
- Sanity check the image first (Pi OS ships all three; minimal cloud/VM
  images may not, which breaks v1.6.13's installer during config write):

```bash
ssh pi-test.local "python3 --version && which curl script && sudo -n true && echo sudo OK"
```

- Pick the callsign deliberately. The installer derives the MeshCom node
  address as `<CALLSIGN>.local`. An SSID that resolves on your LAN makes the
  proxy talk to that real node over UDP 1799 (fine if intended). For a
  passive test, use an unused SSID (e.g. `DK5EN-99`) so the address does not
  resolve — the installer then asks one extra confirmation (step 2).

## Step 1 — Install v1.6.13 the way the fleet got it (NOT piped)

**Do not use `curl | sudo bash` for this step.** Piped mode downloads the
bootstrap **libs from the development branch tip** (`GITHUB_RAW_BASE` in
mcapp.sh), which would install today's Caddy-aware libs and silently
converge the box at install time — destroying the degraded baseline this
test exists to reproduce. Install from the v1.6.13 tag tree instead, so the
tag's own June-era libs are used:

```bash
ssh pi-test.local "cd /tmp \
  && curl -fsSL https://codeload.github.com/DK5EN/McApp/tar.gz/refs/tags/v1.6.13 -o m.tar.gz \
  && tar xzf m.tar.gz"
```

## Step 2 — Run the installer with its prompts

The prompts read from `/dev/tty`, so run it interactively (`ssh -t`) or feed
answers through a pty. Prompt order and example answers:

| #   | Prompt                                | Answer                         |
| --- | ------------------------------------- | ------------------------------ |
| 1   | Enter your callsign                   | `DK5EN-99`                     |
| 2   | Enter your latitude                   | `48.15`                        |
| 3   | Enter your longitude                  | `11.58`                        |
| 4   | Enter your city (for weather reports) | `Muenchen`                     |
| 5   | Enter user info text `[default]`      | empty line (accept default)    |
| 6   | Re-enter configuration to fix a typo? | `n` — ONLY appears if the node |
|     | (Y/n)                                 | address does not resolve       |
| 7   | Save this configuration? (Y/n)        | `y`                            |

Interactive:

```bash
ssh -t pi-test.local "sudo bash /tmp/McApp-1.6.13/bootstrap/mcapp.sh"
```

Non-interactive (pty via `script`; drop the `n\n` if the node address WILL
resolve on your LAN, otherwise the answers misalign):

```bash
ssh pi-test.local "printf 'DK5EN-99\n48.15\n11.58\nMuenchen\n\nn\ny\n' \
  | script -qec 'sudo bash /tmp/McApp-1.6.13/bootstrap/mcapp.sh' /dev/null"
```

Expect the closing summary to report `webapp version: v1.6.13`,
`bootstrap: McApp Bootstrap v2.4.0`, `active slot: slot-0`, and **no caddy
lines anywhere**. On a Pi Zero 2W this takes considerably longer than the
VM run (apt + uv sync on SD card) — be patient before declaring a hang.

## Step 3 — Verify the degraded fleet baseline

All six must hold before continuing; if any fails, the baseline is wrong
(most likely cause: step 1 was run piped).

```bash
ssh pi-test.local "command -v caddy || echo no-caddy; \
  sudo ss -tlnp | grep -E ':80 |:8082 '; \
  sudo cat /var/lib/mcapp/system-epoch 2>&1; \
  sudo nft list ruleset | grep -c 'dport 443'; \
  cat /var/www/html/webapp/version.html; \
  systemctl is-active mcapp mcapp-update.path"
```

Expected: `no-caddy`; lighttpd listening on `:80` (not 8082); epoch file
missing; nftables `dport 443` count `0`; `v1.6.13`; both units `active`.

## Step 4 — Trigger the update through the real webapp path

This is exactly what the webapp's Update button does (same endpoint, same
trigger-file plumbing). Use `{"dev": true}` while the fix only exists as a
pre-release; once a stable ≥ v1.6.14 is published, `{}` tests the stable
channel instead:

```bash
ssh pi-test.local "curl -s -X POST http://localhost/api/update/start \
  -H 'Content-Type: application/json' -d '{\"dev\": true}'"
```

Expected reply: `{"status":"launched","mode":"update",...}`. Alternatively,
open `http://pi-test.local/webapp/update` in a browser and click Update —
identical effect, plus you see the SSE progress stream the way users do.

## Step 5 — Watch the cascade

Expected sequence (VM reference timings in parentheses; a Pi Zero 2W is
slower in the deploy phase, the converge timings are similar):

1. Old v1.6.13 runner, `mode=update`: deploys the new release into the next
   slot with `--skip` — **no front-door work**. (17 s in the VM; minutes on
   a Zero 2W.)
2. New mcapp starts; watchdog logs `waiting for update runner to go idle`
   while the old runner still serves its 30 s grace period.
3. ~60 s after the old runner exits (4 idle probes × 15 s):
   `triggering update runner in converge mode` + `converge trigger written`.
4. New runner, `mode=converge`, runs the new slot's `mcapp.sh --converge`:
   `Converging system state (epoch 0 -> 1)`, nftables gains `dport 443`,
   lighttpd moves to `127.0.0.1:8082`, **Caddy is installed and
   configured**, epoch file written, full health check summary all `[OK]`.

Watch commands:

```bash
# Watchdog milestones
ssh pi-test.local "sudo journalctl -u mcapp.service -f --no-pager | grep --line-buffered 'Converge watchdog'"

# Runner activity (both passes)
ssh pi-test.local "sudo journalctl -u mcapp-update.service -f --no-pager"

# Or simply poll for the epoch marker (end-to-end done signal)
ssh pi-test.local "for i in \$(seq 1 90); do [ -f /var/lib/mcapp/system-epoch ] && { echo DONE after ~\$((i*10))s; break; }; sleep 10; done"
```

Budget ~10–20 minutes end to end on a Zero 2W before investigating.

## Step 6 — Verify the converged end state

```bash
ssh pi-test.local "sudo cat /var/lib/mcapp/system-epoch; \
  cat /var/www/html/webapp/version.html; \
  systemctl is-active caddy; \
  sudo ss -tlnp | grep -E ':443 |:8082 '; \
  sudo nft list ruleset | grep -c 'dport 443'; \
  curl -s http://localhost/api/update/slots | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d[\"system_epoch\"], [s[\"version\"] for s in d[\"slots\"]])'"
```

Expected: epoch `1`; the new version; caddy `active` and listening on
`:443`; lighttpd on `127.0.0.1:8082`; nftables count `1`;
`{'installed': 1, 'required': 1}` with **v1.6.13 still present in a slot**
(the rollback target). Then confirm idempotence and the runner verdict:

```bash
# Converge re-run must be a fast no-op
ssh pi-test.local "sudo ~/mcapp-slots/current/bootstrap/mcapp.sh --converge | tail -2"
# → "System state up to date (epoch 1)"

# Runner verdicts for both passes
ssh pi-test.local "sudo journalctl -u mcapp-update.service --no-pager | grep 'Finished:'"
```

Verdict interpretation: the update pass must be `"status": "success"`. The
converge pass should be `success` on a Pi with working BLE; `"warning"` with
`health_ok: false` is acceptable **only** when the failing check is
`ble_service` on a box without usable Bluetooth (the standard VM artifact).
Any other failing check is a finding. Note that `https://localhost` returns
nothing by design — the `:443` site is host-matched; probe HTTPS like the
bootstrap does, e.g.
`curl -skf --resolve <host>:443:127.0.0.1 https://<host>/health` with the
site name from `/etc/caddy/Caddyfile`.

## Step 7 (optional) — Failure drill

Repeat steps 1–3 on a re-flashed card, disconnect WAN after step 4's deploy
pass finishes (converge then cannot apt-install Caddy), and verify: the
watchdog logs the failed attempt, does NOT retry-loop (one attempt per boot,
6 h re-check), and the box keeps serving the webapp over HTTP. Reconnect
WAN, reboot, and confirm the watchdog converges on the next boot. Manual
recovery at any time: `sudo ~/mcapp-slots/current/bootstrap/mcapp.sh
--converge`.

## Known gotchas

- **Never install the baseline piped** — see step 1. This also means fleet
  boxes freshly installed via the README's piped command in recent weeks
  already carry the new front door; only June-era installs are degraded.
  Converge is idempotent either way.
- The Pi's locale is German — journal/error text may be localized.
- No `sqlite3` CLI on the Pis — inspect the DB via `python3` one-liners
  (quoting rules in `doc/operations-reference.md`).
- If the update pass itself fails its health checks, the old runner
  auto-rolls back to v1.6.13 — that is correct behavior, not a converge
  bug; read the runner journal before re-testing.
